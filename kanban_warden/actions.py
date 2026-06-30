"""Notification and auto-advance state machine for Kanban Warden.

The module intentionally keeps business-code concerns out of the plugin. It only
observes Kanban events, plans bounded orchestration actions, and optionally applies
small Kanban state transitions through SQLite when auto-advance is enabled.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .board import BoardEvent
from .config import KanbanWardenConfig
from .state import WardenStateStore

ActionKind = Literal[
    "ensure_subscription",
    "notify",
    "create_reviewer",
    "create_implementer_followup",
    "create_blocked_remediation",
    "comment",
    "unblock",
    "promote",
    "finalize",
    "retry",
    "escalate",
]

_BOARD_WRITE_DISABLED = "board-write-disabled"
_HERMES_NATIVE_BLOCKED_REASON_CHARS = 160
_HERMES_NATIVE_COMPLETED_SUMMARY_CHARS = 200


@dataclass(frozen=True)
class PlannedAction:
    """A dry-run-safe action emitted by the warden state machine."""

    kind: ActionKind
    board_name: str
    task_id: str | None
    idempotency_key: str
    reason: str
    message: str
    target_task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    max_attempts: int = 0
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActionResult:
    action: PlannedAction
    applied: bool
    skipped: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = self.action.to_dict()
        data.update({"applied": self.applied, "skipped": self.skipped, "note": self.note})
        return data


class KanbanActionEngine:
    """Plan and optionally apply Kanban notification/auto-advance transitions.

    Idempotency is durable in ``WardenStateStore``. Every external effect receives
    a stable key before it is executed so replayed events and process restarts do
    not duplicate reviewer cards, comments, unblock transitions, or notifications.
    """

    def __init__(self, config: KanbanWardenConfig, state_store: WardenStateStore) -> None:
        self.config = config
        self.state_store = state_store

    def plan_for_events(self, events: list[BoardEvent]) -> list[PlannedAction]:
        actions: list[PlannedAction] = []
        blocked_remediations = 0
        for event in events:
            for action in self._plan_event(event):
                if action.kind == "create_blocked_remediation":
                    limit = max(0, int(self.config.blocked_remediation.max_per_tick))
                    if blocked_remediations >= limit:
                        continue
                    blocked_remediations += 1
                actions.append(action)
        return actions

    def plan_for_health(self, findings: list[dict[str, Any]]) -> list[PlannedAction]:
        actions: list[PlannedAction] = []
        planned_recoveries: set[tuple[str, str, str]] = set()
        for finding in findings:
            task_id = _text(finding.get("task_id"))
            board_name = _text(finding.get("board")) or "default"
            kind = _text(finding.get("kind"))
            if not task_id or not kind:
                continue
            if kind in {"running_without_recent_heartbeat", "running_exceeded_task_timeout"}:
                actions.append(
                    self._ensure_subscription(
                        board_name,
                        task_id,
                        f"health:{board_name}:{task_id}:{kind}:ensure-subscription",
                        f"ensure root/stuck-task subscriptions for health finding: {kind}",
                        payload=finding,
                    )
                )
                recovery_key = (board_name, task_id, "stale-running")
                if recovery_key in planned_recoveries:
                    continue
                planned_recoveries.add(recovery_key)
                actions.extend(
                    self._bounded_recovery(
                        board_name=board_name,
                        task_id=task_id,
                        recovery_kind="stale-running",
                        notify_reason="stale running task detected",
                        event_key=f"health:{board_name}:{task_id}:{kind}",
                        payload=finding,
                    )
                )
            elif kind in {
                "long_term_blocked",
                "review_approved_but_still_blocked",
                "root_not_closed_after_children_done",
                "dependency_blocked_by_stuck_parent",
                "blocked_with_all_parents_done",
            }:
                actions.append(
                    self._ensure_subscription(
                        board_name,
                        task_id,
                        f"health:{board_name}:{task_id}:{kind}:ensure-subscription",
                        f"ensure root/stuck-task subscriptions for health finding: {kind}",
                        payload=finding,
                    )
                )
                if kind == "blocked_with_all_parents_done":
                    actions.append(
                        self._promote(
                            board_name,
                            task_id,
                            f"health:{board_name}:{task_id}:{kind}:promote",
                            "all parents are done",
                        )
                    )
                elif kind == "root_not_closed_after_children_done":
                    actions.append(
                        self._finalize(
                            board_name,
                            task_id,
                            f"health:{board_name}:{task_id}:{kind}:finalize",
                            "all child cards are done",
                        )
                    )
        return actions

    def apply(self, db_path: str | Path, actions: list[PlannedAction]) -> list[ActionResult]:
        results: list[ActionResult] = []
        for action in actions:
            if action.dry_run or not self.config.auto_advance.enabled:
                results.append(ActionResult(action, applied=False, note="dry-run"))
                continue
            if not self.state_store.mark_action_started(action.idempotency_key):
                results.append(ActionResult(action, applied=False, skipped=True, note="duplicate"))
                continue
            try:
                note = self._apply_one(Path(db_path), action)
            except Exception as exc:  # pragma: no cover - defensive runtime safety
                self.state_store.mark_action_failed(action.idempotency_key, str(exc))
                raise
            if _retryable_no_effect(action, note):
                self.state_store.mark_action_failed(action.idempotency_key, note)
                results.append(ActionResult(action, applied=False, skipped=True, note=note))
                continue
            self.state_store.mark_action_done(action.idempotency_key, note)
            if note == _BOARD_WRITE_DISABLED:
                results.append(ActionResult(action, applied=False, note=note))
                continue
            results.append(ActionResult(action, applied=True, note=note))
        return results

    def _plan_event(self, event: BoardEvent) -> list[PlannedAction]:
        task_id = event.task_id
        if not task_id:
            return []
        actions: list[PlannedAction] = []
        event_key = event.idempotency_key()
        kind = event.kind
        payload = event.payload or {}
        if _is_warden_notification_evidence(payload):
            return []
        reason = _text(payload.get("reason"))
        outcome = _text(payload.get("outcome")) or _text(payload.get("verdict"))
        status = event.task_status or ""

        if self._should_ensure_subscription_event(kind, status, reason, outcome):
            actions.append(
                self._ensure_subscription(
                    event.board_name,
                    task_id,
                    f"{event_key}:ensure-subscription:{_slug(kind, status, reason, outcome)}",
                    f"ensure root/stuck-task subscriptions for kanban event {kind} status={status or 'unknown'}",
                    payload=event.summary(),
                )
            )

        tail_notify = self._plan_blocked_reason_tail(event, reason)
        if tail_notify is not None:
            actions.append(tail_notify)
        completed_tail_notify = self._plan_completed_summary_tail(event)
        if completed_tail_notify is not None:
            actions.append(completed_tail_notify)

        actions.extend(self._plan_blocked_remediation(event, reason))

        if _is_review_required(event):
            actions.append(
                PlannedAction(
                    kind="create_reviewer",
                    board_name=event.board_name,
                    task_id=task_id,
                    target_task_id=None,
                    idempotency_key=f"reviewer:{event.board_name}:{task_id}",
                    reason="review-required blocked implementation card",
                    message=f"Create/dispatch reviewer for {task_id}",
                    payload=_without_none(
                        {
                            "source_event": event.summary(),
                            "assignee": self.config.reviewer_assignee,
                        }
                    ),
                    max_attempts=self.config.limits.max_retries,
                    dry_run=self.config.auto_advance.dry_run,
                )
            )

        verdict = _review_verdict(event)
        source_task = None if _is_generated_followup_review_event(event, payload) else _review_source_task(event)
        if verdict == "approve" and source_task:
            actions.append(
                self._comment(
                    event.board_name,
                    source_task,
                    f"review-approve:{event.board_name}:{task_id}:{source_task}",
                    f"[warden-review-approved] reviewer {task_id} approved; unblock downstream work.",
                )
            )
            actions.append(
                self._unblock(
                    event.board_name,
                    source_task,
                    f"review-approve-unblock:{event.board_name}:{task_id}:{source_task}",
                    "review approve",
                )
            )
            actions.append(
                self._finalize(
                    event.board_name,
                    source_task,
                    f"review-approve-finalize:{event.board_name}:{task_id}:{source_task}",
                    "review approve",
                )
            )
        elif verdict == "needs-changes" and source_task:
            actions.append(
                self._comment(
                    event.board_name,
                    source_task,
                    f"review-needs-changes:{event.board_name}:{task_id}:{source_task}",
                    f"[warden-review-needs-changes] reviewer {task_id} requested changes; implementation follow-up will be dispatched.",
                )
            )
            actions.append(
                PlannedAction(
                    kind="create_implementer_followup",
                    board_name=event.board_name,
                    task_id=source_task,
                    target_task_id=None,
                    idempotency_key=f"implementer-followup:{event.board_name}:{task_id}:{source_task}",
                    reason="review needs changes",
                    message=f"Create/dispatch implementer follow-up for {source_task} from review {task_id}",
                    payload={
                        "source_event": event.summary(),
                        "review_task": task_id,
                        "source_task": source_task,
                        "review_payload": payload,
                    },
                    max_attempts=self.config.limits.max_retries,
                    dry_run=self.config.auto_advance.dry_run,
                )
            )
            actions.append(
                self._unblock(
                    event.board_name,
                    source_task,
                    f"review-needs-changes-unblock:{event.board_name}:{task_id}:{source_task}",
                    "review needs changes",
                )
            )

        if _is_worker_failure(kind, status, reason, outcome):
            actions.extend(
                self._bounded_recovery(
                    board_name=event.board_name,
                    task_id=task_id,
                    recovery_kind="worker-failure",
                    notify_reason="worker crash/protocol violation/gave_up",
                    event_key=event_key,
                    payload=event.summary(),
                )
            )
        return actions

    def _plan_blocked_reason_tail(
        self, event: BoardEvent, reason: str
    ) -> PlannedAction | None:
        if not self.config.notifications.enabled:
            return None
        if event.kind != "blocked" or event.task_status != "blocked" or not event.task_id:
            return None
        if len(reason) <= _HERMES_NATIVE_BLOCKED_REASON_CHARS:
            return None
        return self._notify(
            event.board_name,
            event.task_id,
            f"blocked-tail:{event.board_name}:{event.task_id}:{event.event_id}",
            "blocked reason continuation after native truncation",
            payload={
                "source_event": event.summary(),
                "message_template": "blocked_reason_tail",
                "native_reason_limit": _HERMES_NATIVE_BLOCKED_REASON_CHARS,
                "reason_tail": reason[_HERMES_NATIVE_BLOCKED_REASON_CHARS:],
            },
        )

    def _plan_completed_summary_tail(self, event: BoardEvent) -> PlannedAction | None:
        if not self.config.notifications.enabled:
            return None
        if event.kind != "completed" or not event.task_id:
            return None
        summary = _text((event.payload or {}).get("summary"))
        if len(summary) <= _HERMES_NATIVE_COMPLETED_SUMMARY_CHARS:
            return None
        return self._notify(
            event.board_name,
            event.task_id,
            f"completed-tail:{event.board_name}:{event.task_id}:{event.event_id}",
            "completed summary continuation after native truncation",
            payload={
                "source_event": event.summary(),
                "message_template": "completed_summary_tail",
                "native_summary_limit": _HERMES_NATIVE_COMPLETED_SUMMARY_CHARS,
                "summary_tail": summary[_HERMES_NATIVE_COMPLETED_SUMMARY_CHARS:],
            },
        )

    def _plan_blocked_remediation(self, event: BoardEvent, reason: str) -> list[PlannedAction]:
        if not self.config.blocked_remediation.enabled:
            return []
        if event.kind != "blocked" or event.task_status != "blocked" or not event.task_id:
            return []
        if _is_review_required(event) or _is_self_generated_blocked_remediation(event):
            return []
        if event.relationship.open_remediation_task_ids:
            return [
                self._comment(
                    event.board_name,
                    event.task_id,
                    f"blocked-remediation-existing:{event.board_name}:{event.task_id}:{event.event_id}",
                    "[warden-blocked-remediation skipped] Existing open remediation task already covers this source task: "
                    + ", ".join(event.relationship.open_remediation_task_ids),
                    reason="blocked remediation already open",
                )
            ]
        source_task_id = event.task_id
        classification = _blocked_remediation_classification(reason, event.payload or {})
        if classification == "human-needed":
            return [
                self._comment(
                    event.board_name,
                    source_task_id,
                    f"blocked-remediation-human-needed:{event.board_name}:{source_task_id}:{event.event_id}",
                    "[warden-blocked-remediation human-needed] This block appears to require a human decision, credential, permission, account, cost, production change, merge, or release approval. Warden will not create an autonomous remediation task.",
                    reason="blocked remediation human-needed",
                )
            ]
        if classification != "agent-actionable":
            return []
        title = _blocked_remediation_title(source_task_id, event.task_title)
        body = _blocked_remediation_body(event, reason)
        payload = _without_none(
            {
                "classification": classification,
                "source_task_id": source_task_id,
                "blocked_event_id": event.event_id,
                "source_event": event.summary(),
                "title": title,
                "body": body,
                "assignee": self.config.blocked_remediation.assignee,
                "idempotency_key": f"blocked-remediation:{event.board_name}:{source_task_id}:{event.event_id}",
                "created_by": "kanban-warden",
            }
        )
        return [
            PlannedAction(
                kind="create_blocked_remediation",
                board_name=event.board_name,
                task_id=source_task_id,
                target_task_id=None,
                idempotency_key=payload["idempotency_key"],
                reason="agent-actionable blocked task",
                message=f"Create/dispatch blocked remediation for {source_task_id}",
                payload=payload,
                max_attempts=self.config.limits.max_retries,
                dry_run=self.config.auto_advance.dry_run,
            )
        ]

    def _bounded_recovery(
        self,
        *,
        board_name: str,
        task_id: str,
        recovery_kind: str,
        notify_reason: str,
        event_key: str,
        payload: dict[str, Any],
    ) -> list[PlannedAction]:
        attempt = self.state_store.peek_retry(board_name, task_id, recovery_kind) + 1
        if attempt <= self.config.limits.max_retries:
            return [
                PlannedAction(
                    kind="retry",
                    board_name=board_name,
                    task_id=task_id,
                    target_task_id=task_id,
                    idempotency_key=f"{event_key}:retry:{attempt}",
                    reason=notify_reason,
                    message=f"Recover {task_id} from {recovery_kind} attempt {attempt}/{self.config.limits.max_retries}",
                    payload={**payload, "recovery_kind": recovery_kind},
                    attempt=attempt,
                    max_attempts=self.config.limits.max_retries,
                    dry_run=self.config.auto_advance.dry_run,
                )
            ]
        return [
            PlannedAction(
                kind="escalate",
                board_name=board_name,
                task_id=task_id,
                target_task_id=task_id,
                idempotency_key=f"{event_key}:escalate:{recovery_kind}",
                reason=f"retry exhausted for {recovery_kind}",
                message=f"Escalate {task_id}: retry budget exhausted for {recovery_kind}",
                payload={**payload, "recovery_kind": recovery_kind},
                attempt=attempt,
                max_attempts=self.config.limits.max_retries,
                dry_run=self.config.auto_advance.dry_run,
            )
        ]

    def _ensure_subscription(
        self, board_name: str, task_id: str, key: str, reason: str, *, payload: dict[str, Any]
    ) -> PlannedAction:
        return PlannedAction(
            kind="ensure_subscription",
            board_name=board_name,
            task_id=task_id,
            target_task_id=task_id,
            idempotency_key=key,
            reason=reason,
            message=f"Ensure root/stuck-task subscriptions: {reason} task={task_id}",
            payload=payload,
            dry_run=self.config.auto_advance.dry_run,
        )

    def _notify(
        self, board_name: str, task_id: str, key: str, reason: str, *, payload: dict[str, Any]
    ) -> PlannedAction:
        return PlannedAction(
            kind="notify",
            board_name=board_name,
            task_id=task_id,
            idempotency_key=key,
            reason=reason,
            message=f"Notify subscribers: {reason} task={task_id}",
            payload={"channels": self.config.notifications.channels, **payload},
            dry_run=self.config.auto_advance.dry_run,
        )

    def _comment(
        self,
        board_name: str,
        task_id: str,
        key: str,
        message: str,
        *,
        reason: str = "review follow-up",
    ) -> PlannedAction:
        return PlannedAction(
            "comment",
            board_name,
            task_id,
            key,
            reason,
            message,
            task_id,
            {},
            dry_run=self.config.auto_advance.dry_run,
        )

    def _unblock(self, board_name: str, task_id: str, key: str, reason: str) -> PlannedAction:
        return PlannedAction(
            "unblock",
            board_name,
            task_id,
            key,
            reason,
            f"Unblock {task_id}: {reason}",
            task_id,
            {},
            dry_run=self.config.auto_advance.dry_run,
        )

    def _promote(self, board_name: str, task_id: str, key: str, reason: str) -> PlannedAction:
        return PlannedAction(
            "promote",
            board_name,
            task_id,
            key,
            reason,
            f"Promote {task_id}: {reason}",
            task_id,
            {},
            dry_run=self.config.auto_advance.dry_run,
        )

    def _finalize(self, board_name: str, task_id: str, key: str, reason: str) -> PlannedAction:
        return PlannedAction(
            "finalize",
            board_name,
            task_id,
            key,
            reason,
            f"Finalize {task_id}: {reason}",
            task_id,
            {},
            dry_run=self.config.auto_advance.dry_run,
        )

    def _should_ensure_subscription_event(
        self, kind: str, status: str, reason: str, outcome: str
    ) -> bool:
        if not self.config.notifications.enabled:
            return False
        return kind in {"blocked", "gave_up"} or _is_worker_failure(kind, status, reason, outcome)

    def _apply_one(self, db_path: Path, action: PlannedAction) -> str:
        if action.kind == "ensure_subscription":
            task_id = action.target_task_id or action.task_id
            if task_id and _has_native_subscription(db_path, task_id):
                return "subscription-exists"
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind == "notify":
            self.state_store.enqueue_notification(action.idempotency_key, action.to_dict())
            return "queued-notification"
        if action.kind == "create_reviewer":
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind == "create_implementer_followup":
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind == "create_blocked_remediation":
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind == "comment":
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind in {"unblock", "retry"}:
            self.state_store.bump_retry(
                action.board_name,
                action.target_task_id or action.task_id or "",
                _text(action.payload.get("recovery_kind")) or action.kind,
            )
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind == "promote":
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind == "finalize":
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        if action.kind == "escalate":
            self._queue_gateway_proposal(action)
            return _BOARD_WRITE_DISABLED
        return "noop"

    def _queue_gateway_proposal(self, action: PlannedAction) -> None:
        payload = action.to_dict()
        payload["delivery"] = "gateway-required"
        self.state_store.enqueue_notification(action.idempotency_key, payload)



def _is_review_required(event: BoardEvent) -> bool:
    payload = event.payload or {}
    reason = _text(payload.get("reason")).lower()
    return event.task_status == "blocked" and (
        "review-required" in reason or event.relationship.review_required
    )


def _review_verdict(event: BoardEvent) -> str | None:
    payload = event.payload or {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    assert isinstance(metadata, dict)

    for source in (metadata, payload):
        approved = source.get("approved")
        if isinstance(approved, bool):
            return "approve" if approved else "needs-changes"
        for key in ("verdict", "outcome"):
            verdict = _classify_review_verdict(_text(source.get(key)))
            if verdict:
                return verdict

    if not event.relationship.review_required:
        return None
    for key in ("summary", "result", "reason", "body", "comment"):
        verdict = _classify_review_verdict(_text(payload.get(key)))
        if verdict:
            return verdict
    return None


def _classify_review_verdict(text: str) -> str | None:
    normalized = text.strip().lower()
    if not normalized:
        return None
    if re.search(r"^\s*(?:approve|approved)\b", normalized):
        return "approve"
    if re.search(r"^\s*needs[- ]changes\b", normalized):
        return "needs-changes"
    return None


def _generated_followup_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        _text(payload.get(key))
        for key in ("body", "comment", "comments", "context", "summary", "result", "reason")
    )


def _is_generated_followup_review_event(event: BoardEvent, payload: dict[str, Any]) -> bool:
    task_id = event.task_id or ""
    if task_id.startswith("fix_") or task_id.startswith("review_fix_"):
        return True
    text = _generated_followup_text(payload).lower()
    return bool(
        re.search(r"\bfollow-up implementation for review\b.+\bon source task\b", text, re.DOTALL)
    )


def _review_source_task(event: BoardEvent) -> str | None:
    payload = event.payload or {}
    if _is_generated_followup_review_event(event, payload):
        return None
    for key in ("source_task", "source_task_id", "implementation_task", "reviewed_task"):
        value = _text(payload.get(key))
        if value:
            return value
    explicit_source = _review_source_task_from_text(payload)
    if explicit_source:
        return explicit_source
    if event.relationship.parents:
        return event.relationship.parents[0]
    return None


def _review_source_task_from_text(payload: dict[str, Any]) -> str | None:
    text = _generated_followup_text(payload)
    if not text:
        return None
    # Manual reviewer follow-up cards may only mention the original card in prose,
    # e.g. "Source task t_041385e0".  Require the source/implementation/reviewed
    # cue immediately before the id so arbitrary task mentions stay ambiguous.
    match = re.search(
        r"\b(?:source|implementation|implemented|reviewed)\s+(?:task|card)?\s*[:#-]?\s*(t_[A-Za-z0-9_-]+)\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)
    return None


def _is_worker_failure(kind: str, status: str, reason: str, outcome: str) -> bool:
    text = " ".join([kind, status, reason, outcome]).lower()
    return any(
        token in text
        for token in ("crash", "protocol violation", "gave_up", "gave up", "timed_out", "timed out")
    )


def _blocked_remediation_classification(reason: str, payload: dict[str, Any]) -> str | None:
    text = _generated_followup_text(payload)
    haystack = " ".join([reason, text]).lower()
    if not haystack.strip():
        return None
    if _requires_human(haystack):
        return "human-needed"
    if _is_agent_actionable_block(haystack):
        return "agent-actionable"
    return None


def _requires_human(text: str) -> bool:
    human_patterns = [
        r"\bneed(?:s|ed)? user\b",
        r"\bask(?:ing)? user\b",
        r"\bwaiting for (?:human|user|operator)\b",
        r"\bhuman[- ]needed\b",
        r"\buser decision\b",
        r"\bchoose\b",
        r"\bapproval\b",
        r"\bapprove\b",
        r"\bcredential\b",
        r"\bpermission\b",
        r"\baccount\b",
        r"\blogin\b",
        r"\bpassword\b",
        r"\bsecret\b",
        r"\btoken\b",
        r"\bapi key\b",
        r"\bcost\b",
        r"\bfee\b",
        r"\bpayment\b",
        r"\bproduction\b",
        r"\bprod\b",
        r"\bmerge approval\b",
        r"\brelease approval\b",
        r"\bdeploy approval\b",
        r"用户.*(?:决策|确认|授权|审批|账号|密码|费用|发布|合并|生产)",
        r"(?:需要|等待).*(?:人工|人类|用户).*(?:处理|判断|决策|确认|授权|审批)",
    ]
    return any(re.search(pattern, text) for pattern in human_patterns)


def _is_agent_actionable_block(text: str) -> bool:
    agent_patterns = [
        r"\bworker\b",
        r"\bagent\b",
        r"\borchestrator\b",
        r"\bplanner\b",
        r"\bdispatch(?:er)?\b",
        r"\bturn budget\b",
        r"\bmax turns\b",
        r"\bgoal[- ]mode\b",
        r"\btimed? out\b",
        r"\btimeout\b",
        r"\bgave[_ -]?up\b",
        r"\bcrash\b",
        r"\bprotocol violation\b",
        r"\bno progress\b",
        r"\bstuck\b",
        r"\bblocked\b",
        r"\bfail(?:ed|ure)?\b",
        r"推进不下去",
        r"卡住",
    ]
    return any(re.search(pattern, text) for pattern in agent_patterns)


def _is_self_generated_blocked_remediation(event: BoardEvent) -> bool:
    task_id = event.task_id or ""
    title = (event.task_title or "").lower()
    created_by = (event.task_created_by or "").lower()
    idempotency_key = event.task_idempotency_key or ""
    if created_by == "kanban-warden":
        return True
    if task_id.startswith("remediate_"):
        return True
    if idempotency_key.startswith("blocked-remediation:"):
        return True
    return title.startswith("resolve blocked task ")


def _blocked_remediation_title(source_task_id: str, source_title: str | None) -> str:
    source_label = (source_title or "").strip() or source_task_id
    return f"Resolve blocked task {source_task_id}: {source_label}"[:180]


def _blocked_remediation_body(event: BoardEvent, reason: str) -> str:
    source_title = event.task_title or event.task_id or ""
    lines = [
        "You are the kanban orchestrator for an autonomous blocked-task remediation.",
        "",
        f"Source board: {event.board_name}",
        f"Source task: {event.task_id}",
        f"Source title: {source_title}",
        f"Blocked event: {event.event_id}",
    ]
    if reason:
        lines.append(f"Blocked reason: {reason}")
    lines.extend(
        [
            "",
            "Read the source task, comments, run history, linked tasks, and available workspace evidence. Advance the work by commenting with findings, unblocking when safe, splitting into smaller tasks, or reassigning through the normal Kanban workflow.",
            "",
            "Ask the user only when progress truly requires a decision, missing credential, account or permission, cost approval, production change, merge approval, release approval, or conflicting acceptance criteria.",
            "",
            "Do not make this remediation task a child of the blocked source task; dependency on the blocked card can prevent the remediation from becoming runnable.",
        ]
    )
    return "\n".join(lines)


def _is_warden_notification_evidence(payload: dict[str, Any]) -> bool:
    return (
        _text(payload.get("by")) == "kanban-warden"
        and _text(payload.get("event")) == "warden-notification-delivered"
    )


def _retryable_no_effect(action: PlannedAction, note: str) -> bool:
    return action.kind == "ensure_subscription" and note in {
        "no-related-subscription-source",
        "notify-subs-table-missing",
        "task-missing",
        "missing-task",
    }


def _has_native_subscription(db_path: str | Path, task_id: str) -> bool:
    with sqlite3.connect(db_path) as con:
        if (
            con.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                ("kanban_notify_subs",),
            ).fetchone()
            is None
        ):
            return False
        row = con.execute(
            "select 1 from kanban_notify_subs where task_id = ? limit 1",
            (task_id,),
        ).fetchone()
        return row is not None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _without_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _slug(*parts: str) -> str:
    raw = ":".join(part for part in parts if part)
    return "".join(ch if ch.isalnum() else "-" for ch in raw.lower())[:80] or "event"
