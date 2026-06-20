"""Kanban dependency-deadlock detection and safe remediation helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .sqlite_utils import managed_connection

_TERMINAL_PARENT_STATUSES = {"done", "archived"}
_FIX_RE = re.compile(
    r"\b(fix|repair|recovery|follow[- ]?up|address needs[- ]changes|"
    r"changes requested|NEEDS-CHANGES)\b",
    re.IGNORECASE,
)
_REVIEW_RE = re.compile(r"\b(review|reviewer|re-review|approve|approval|verify)\b", re.IGNORECASE)
_TASK_ID_RE = re.compile(r"t_[0-9A-Za-z]+")


@dataclass(frozen=True)
class DeadlockProposal:
    """One proposed or applied remediation action."""

    action_key: str
    action_type: str
    primary_task_id: str
    secondary_task_id: str | None
    edge_parent_id: str | None
    edge_child_id: str | None
    predicate: str
    evidence: dict[str, Any]
    planned_actions: list[str]
    would_mutate: bool
    dry_run: bool
    status: str = "proposed"


@dataclass(frozen=True)
class DeadlockScanReport:
    """Result of a dependency-deadlock remediation scan."""

    board: str
    dry_run: bool
    auto_advance: bool
    proposals: list[DeadlockProposal] = field(default_factory=list)


def report_to_dict(report: DeadlockScanReport) -> dict[str, Any]:
    """Return deterministic, secret-safe scan output for CLI/status reporting."""

    counts = {
        "recovery_deadlocks": 0,
        "reviewer_deadlocks": 0,
        "stale_todo_all_parents_done": 0,
        "report_only": 0,
    }
    proposals: list[dict[str, Any]] = []
    for proposal in report.proposals:
        if proposal.action_type == "unlink_erroneous_recovery_parent":
            counts["recovery_deadlocks"] += 1
        elif proposal.action_type == "unlink_erroneous_reviewer_parent":
            counts["reviewer_deadlocks"] += 1
        elif proposal.action_type == "promote_stale_todo":
            counts["stale_todo_all_parents_done"] += 1
        elif not proposal.would_mutate:
            counts["report_only"] += 1
        proposals.append(
            {
                "marker": "MUTATE" if proposal.would_mutate else "REPORT",
                "action_key": proposal.action_key,
                "action_type": proposal.action_type,
                "primary_task_id": proposal.primary_task_id,
                "secondary_task_id": proposal.secondary_task_id,
                "edge_parent_id": proposal.edge_parent_id,
                "edge_child_id": proposal.edge_child_id,
                "predicate": proposal.predicate,
                "planned_actions": proposal.planned_actions,
                "evidence": proposal.evidence,
                "would_mutate": proposal.would_mutate,
                "dry_run": proposal.dry_run,
                "status": proposal.status,
            }
        )
    return {
        "board": report.board,
        "dry_run": report.dry_run,
        "auto_advance": report.auto_advance,
        "counts": counts,
        "proposals": proposals,
    }


@dataclass(frozen=True)
class _TaskSnapshot:
    id: str
    title: str
    body: str
    assignee: str
    status: str
    created_by: str
    created_at: int
    result: str

    @property
    def text(self) -> str:
        return "\n".join([self.title, self.body, self.result])


def run_deadlock_remediation(
    conn: sqlite3.Connection,
    *,
    board: str,
    now: int,
    dry_run: bool,
    auto_advance: bool,
    max_retries: int = 2,
    stale_claim_seconds: int = 3_600,
) -> DeadlockScanReport:
    """Scan one Kanban board connection for safe dependency-deadlock remediations.

    The function is intentionally conservative: it returns deterministic proposals
    only. Kanban Warden may write its own state database, but board database writes
    are owned by the gateway/Hermes path so scans never create action-log rows or
    change board task state.
    """

    conn.row_factory = sqlite3.Row
    tasks = _load_tasks(conn)
    comments = _load_comments(conn)
    events = _load_events(conn)
    proposals: list[DeadlockProposal] = []
    for child_id, parent_id in _non_done_parent_edges(conn, tasks):
        child = tasks[child_id]
        parent = tasks[parent_id]
        proposal = _build_edge_proposal(
            board=board,
            child=child,
            parent=parent,
            comments=comments,
            events=events,
            now=now,
            stale_claim_seconds=stale_claim_seconds,
            dry_run=dry_run,
        )
        if proposal is None:
            continue
        proposals.append(proposal)
    remediated_children = {
        proposal.primary_task_id for proposal in proposals if proposal.would_mutate
    }
    for child in tasks.values():
        if child.id in remediated_children:
            continue
        proposal = _build_stale_ready_proposal(
            conn,
            board=board,
            task=child,
            now=now,
            stale_claim_seconds=stale_claim_seconds,
            dry_run=dry_run,
        )
        if proposal is None:
            continue
        proposals.append(proposal)
    return DeadlockScanReport(
        board=board,
        dry_run=dry_run,
        auto_advance=auto_advance,
        proposals=proposals,
    )


def open_board_connection(db_path: str):
    """Open a Hermes Kanban SQLite database for health-sweep remediation."""

    @contextlib.contextmanager
    def _with_row_factory():
        with managed_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    return _with_row_factory()


def _build_stale_ready_proposal(
    conn: sqlite3.Connection,
    *,
    board: str,
    task: _TaskSnapshot,
    now: int,
    stale_claim_seconds: int,
    dry_run: bool,
) -> DeadlockProposal | None:
    if task.status != "todo":
        return None
    if not _has_parent(conn, task.id):
        return None
    if not _all_parents_done_or_archived(conn, task.id):
        return None
    age_seconds = max(0, now - task.created_at)
    if age_seconds < stale_claim_seconds:
        return None
    evidence = {
        "task_status": task.status,
        "age_seconds": age_seconds,
        "parents": "done_or_archived",
    }
    action_key = _action_key(
        board,
        "promote_stale_todo",
        task.id,
        "none",
        "stale_todo_all_parents_done",
        evidence,
    )
    return DeadlockProposal(
        action_key=action_key,
        action_type="promote_stale_todo",
        primary_task_id=task.id,
        secondary_task_id=None,
        edge_parent_id=None,
        edge_child_id=task.id,
        predicate="stale_todo_all_parents_done",
        evidence=evidence,
        planned_actions=["promote"],
        would_mutate=True,
        dry_run=dry_run,
    )




def _build_edge_proposal(
    *,
    board: str,
    child: _TaskSnapshot,
    parent: _TaskSnapshot,
    comments: Mapping[str, list[str]],
    events: Mapping[str, list[dict[str, Any]]],
    now: int,
    stale_claim_seconds: int,
    dry_run: bool,
) -> DeadlockProposal | None:
    child_text = _combined_text(child, comments)
    parent_text = _combined_text(parent, comments)
    has_claim_rejection = _has_claim_rejected_parents_not_done(events.get(child.id, []))
    child_age_seconds = max(0, now - child.created_at)
    stale_enough = child_age_seconds >= stale_claim_seconds
    is_stale_todo_like = child.status in {"todo", "ready"}
    references_parent = parent.id in child_text or child.id in parent_text
    needs_changes = _has_needs_changes(child_text) or _has_needs_changes(parent_text)
    review_required = _has_review_required(parent_text, events.get(parent.id, []))
    child_is_review = _is_review(child)
    child_is_fix = _is_fix(child) or needs_changes

    if not is_stale_todo_like:
        return None
    if not has_claim_rejection and not stale_enough:
        return None
    if not references_parent and not (needs_changes or review_required):
        return None
    if parent.status not in {"todo", "running", "blocked"}:
        return None

    if child_is_fix and (needs_changes or review_required or references_parent):
        action_type = "unlink_erroneous_recovery_parent"
        predicate = "claim_rejected_parents_not_done_recovery"
        evidence_kind = "needs_changes" if needs_changes else "recovery_references_source"
    elif child_is_review and (review_required or references_parent):
        action_type = "unlink_erroneous_reviewer_parent"
        predicate = "claim_rejected_parents_not_done_reviewer"
        evidence_kind = "review_required" if review_required else "reviewer_references_source"
    else:
        return None

    evidence = {
        "child_status": child.status,
        "parent_status": parent.status,
        "claim_rejected": has_claim_rejection,
        "claim_rejected_reason": "parents_not_done" if has_claim_rejection else None,
        "child_age_seconds": child_age_seconds,
        "stale_threshold_seconds": stale_claim_seconds,
        "stale_threshold_met": stale_enough,
        "evidence": evidence_kind,
    }
    if needs_changes:
        evidence["review_conclusion"] = "NEEDS-CHANGES"
    if review_required:
        evidence["source_state"] = "review-required"
    action_key = _action_key(board, action_type, child.id, parent.id, predicate, evidence)
    return DeadlockProposal(
        action_key=action_key,
        action_type=action_type,
        primary_task_id=child.id,
        secondary_task_id=parent.id,
        edge_parent_id=parent.id,
        edge_child_id=child.id,
        predicate=predicate,
        evidence=evidence,
        planned_actions=["comment_child", "comment_parent", "unlink", "promote"],
        would_mutate=True,
        dry_run=dry_run,
    )




def _load_tasks(conn: sqlite3.Connection) -> dict[str, _TaskSnapshot]:
    rows = conn.execute(
        "SELECT id, title, body, assignee, status, created_by, created_at, result FROM tasks"
    ).fetchall()
    return {
        str(row["id"]): _TaskSnapshot(
            id=str(row["id"]),
            title=str(row["title"] or ""),
            body=str(row["body"] or ""),
            assignee=str(row["assignee"] or ""),
            status=str(row["status"] or ""),
            created_by=str(row["created_by"] or ""),
            created_at=int(row["created_at"] or 0),
            result=str(row["result"] or ""),
        )
        for row in rows
    }


def _load_comments(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT task_id, body FROM task_comments ORDER BY created_at, id"
    ).fetchall()
    comments: dict[str, list[str]] = {}
    for row in rows:
        comments.setdefault(str(row["task_id"]), []).append(str(row["body"] or ""))
    return comments


def _load_events(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT task_id, kind, payload FROM task_events ORDER BY created_at, id"
    ).fetchall()
    events: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = _json_loads(row["payload"])
        events.setdefault(str(row["task_id"]), []).append(
            {"kind": str(row["kind"]), "payload": payload}
        )
    return events


def _non_done_parent_edges(
    conn: sqlite3.Connection, tasks: Mapping[str, _TaskSnapshot]
) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
    ).fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        parent_id = str(row["parent_id"])
        child_id = str(row["child_id"])
        if (
            parent_id in tasks
            and child_id in tasks
            and tasks[parent_id].status not in _TERMINAL_PARENT_STATUSES
        ):
            out.append((child_id, parent_id))
    return out


def _combined_text(task: _TaskSnapshot, comments: Mapping[str, list[str]]) -> str:
    return "\n".join([task.text, *comments.get(task.id, [])])


def _is_fix(task: _TaskSnapshot) -> bool:
    return bool(_FIX_RE.search(task.text))


def _is_review(task: _TaskSnapshot) -> bool:
    return task.assignee.lower() == "reviewer" or bool(_REVIEW_RE.search(task.text))


def _has_needs_changes(text: str) -> bool:
    return "needs-changes" in text.lower() or "changes_requested" in text.lower()


def _has_review_required(text: str, events: list[dict[str, Any]]) -> bool:
    if "review-required:" in text.lower():
        return True
    for event in events:
        payload = event.get("payload")
        if isinstance(payload, Mapping) and "review-required:" in json.dumps(payload).lower():
            return True
    return False


def _has_claim_rejected_parents_not_done(events: list[dict[str, Any]]) -> bool:
    for event in events:
        if event.get("kind") != "claim_rejected":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping) and payload.get("reason") == "parents_not_done":
            return True
    return False




def _has_parent(conn: sqlite3.Connection, task_id: str) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1", (task_id,)).fetchone()
    )


def _all_parents_done_or_archived(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
          FROM task_links l JOIN tasks p ON p.id = l.parent_id
         WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived')
         LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return row is None


def _action_key(
    board: str,
    action_type: str,
    primary_task_id: str,
    secondary_task_id: str,
    predicate: str,
    evidence: Mapping[str, Any],
) -> str:
    safe_evidence = {
        "primary": primary_task_id,
        "secondary": secondary_task_id,
        "predicate": predicate,
        "statuses": [evidence.get("child_status"), evidence.get("parent_status")],
        "flags": sorted(str(k) for k, v in evidence.items() if v is True or isinstance(v, str)),
    }
    digest = hashlib.sha256(json.dumps(safe_evidence, sort_keys=True).encode()).hexdigest()[:12]
    return f"kw:v1:{board}:{action_type}:{primary_task_id}:{secondary_task_id}:{digest}"


def _board_from_key(action_key: str) -> str:
    parts = action_key.split(":", 4)
    return parts[2] if len(parts) >= 3 else "default"




def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return None


def referenced_task_ids(text: str) -> set[str]:
    """Return task-id references from text; useful for future semantic matching."""

    return set(_TASK_ID_RE.findall(text))
