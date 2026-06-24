"""Notification and auto-advance state machine for Kanban Warden.

The module intentionally keeps business-code concerns out of the plugin. It only
observes Kanban events, plans bounded orchestration actions, and optionally applies
small Kanban state transitions through SQLite when auto-advance is enabled.
"""

from __future__ import annotations

import re
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
    "comment",
    "unblock",
    "promote",
    "finalize",
    "retry",
    "escalate",
]

_BOARD_WRITE_DISABLED = "board-write-disabled"


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
        for event in events:
            actions.extend(self._plan_event(event))
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
                actions.append(
                    self._notify(
                        board_name,
                        task_id,
                        f"health:{board_name}:{task_id}:{kind}:notify",
                        f"health finding: {kind}",
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
                actions.append(
                    self._notify(
                        board_name,
                        task_id,
                        f"health:{board_name}:{task_id}:{kind}:notify",
                        f"health finding: {kind}",
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

        if self._should_notify_event(kind, status, reason, outcome):
            actions.append(
                self._notify(
                    event.board_name,
                    task_id,
                    f"{event_key}:notify:{_slug(kind, status, reason, outcome)}",
                    f"kanban event {kind} status={status or 'unknown'}",
                    payload=event.summary(),
                )
            )

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

    def _comment(self, board_name: str, task_id: str, key: str, message: str) -> PlannedAction:
        return PlannedAction(
            "comment",
            board_name,
            task_id,
            key,
            "review follow-up",
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

    def _should_notify_event(self, kind: str, status: str, reason: str, outcome: str) -> bool:
        if not self.config.notifications.enabled:
            return False
        if kind in {"blocked", "completed", "done", "gave_up"}:
            return True
        if status in {"blocked", "done", "completed"}:
            return True
        if "review-required" in reason or outcome in {"approve", "needs-changes"}:
            return True
        return _is_worker_failure(kind, status, reason, outcome)

    def _apply_one(self, db_path: Path, action: PlannedAction) -> str:
        if action.kind == "ensure_subscription":
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




def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _without_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _slug(*parts: str) -> str:
    raw = ":".join(part for part in parts if part)
    return "".join(ch if ch.isalnum() else "-" for ch in raw.lower())[:80] or "event"
