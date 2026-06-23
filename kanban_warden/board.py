"""Kanban board discovery, event tailing, and read-only relationship analysis."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .sqlite_utils import managed_connection
from .state import WardenStateStore


@dataclass(frozen=True)
class BoardRef:
    name: str
    db_path: Path
    kind: Literal["legacy", "named", "explicit"]


@dataclass(frozen=True)
class TaskRelationship:
    task_id: str
    parents: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    root_task_id: str | None = None
    review_required: bool = False
    comments_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BoardEvent:
    board_name: str
    event_id: int
    task_id: str | None
    kind: str
    payload: dict[str, Any] | None
    created_at: float | None
    run_id: int | None
    task_status: str | None
    relationship: TaskRelationship

    def idempotency_key(self) -> str:
        return f"event:{self.board_name}:{self.event_id}"

    def summary(self) -> dict[str, Any]:
        return {
            "board": self.board_name,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "task_status": self.task_status,
            "relationship": self.relationship.to_dict(),
        }


def default_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def discover_boards(
    boards: Literal["*"] | list[str], *, hermes_home: str | Path | None = None
) -> list[BoardRef]:
    """Discover legacy and named Hermes Kanban boards.

    ``boards == "*"`` rescans the filesystem on every call so boards created while the
    supervisor is running are picked up without a configuration change.
    """

    home = Path(hermes_home) if hermes_home is not None else default_hermes_home()
    discovered: list[BoardRef] = []
    legacy = home / "kanban.db"
    if boards == "*":
        if _looks_like_board_db(legacy):
            discovered.append(BoardRef("default", legacy, "legacy"))
        boards_dir = home / "kanban" / "boards"
        if boards_dir.is_dir():
            for entry in sorted(boards_dir.iterdir(), key=lambda p: p.name):
                path = _board_path_for_name(entry.name, home)
                if _looks_like_board_db(path):
                    discovered.append(BoardRef(entry.name, path, "named"))
        return discovered

    for name in boards:
        if name == "default":
            path = legacy
            kind: Literal["legacy", "named", "explicit"] = "legacy"
        else:
            path = _board_path_for_name(name, home)
            kind = "named"
        if _looks_like_board_db(path):
            discovered.append(BoardRef(name, path, kind))
    return discovered


class BoardEventTailer:
    def __init__(self, state_store: WardenStateStore) -> None:
        self.state_store = state_store

    def tail(
        self,
        board_name: str,
        db_path: str | Path,
        *,
        limit: int = 500,
        active_statuses: set[str] | None = None,
    ) -> list[BoardEvent]:
        path = Path(db_path)
        cursor = self.state_store.get_cursor(board_name)
        events = _read_events(board_name, path, cursor, limit=limit, active_statuses=active_statuses)
        max_event_id = cursor
        active_events: list[BoardEvent] = []
        for event in events:
            max_event_id = max(max_event_id, event.event_id)
            if active_statuses is not None and event.task_status not in active_statuses:
                continue
            if self.state_store.mark_processed(event.idempotency_key()):
                active_events.append(event)
        if max_event_id != cursor:
            self.state_store.set_cursor(board_name, max_event_id)
        return active_events

    def recent(self, board_name: str, db_path: str | Path, *, limit: int = 10) -> list[BoardEvent]:
        return _read_events(board_name, Path(db_path), 0, limit=limit, newest=True)


def analyze_health(
    board_name: str,
    db_path: str | Path,
    *,
    now: float,
    stale_claim_seconds: int,
    task_timeout_seconds: int,
) -> list[dict[str, Any]]:
    path = Path(db_path)
    if not _looks_like_board_db(path):
        return []
    findings: list[dict[str, Any]] = []
    with managed_connection(path) as con:
        con.row_factory = sqlite3.Row
        if _has_table(con, "tasks"):
            rows = _safe_select(
                con,
                """
                select id, status, assignee, started_at, created_at, current_run_id
                from tasks where status in ('running', 'blocked')
                """,
            )
            for row in rows:
                task_id = _row_text(row, "id")
                status = _row_text(row, "status")
                last_heartbeat = _last_event_at(con, task_id, "heartbeat")
                started_at = _row_float(row, "started_at") or _row_float(row, "created_at") or 0.0
                if status == "running":
                    last_signal = max(started_at, last_heartbeat or 0.0)
                    if now - last_signal >= stale_claim_seconds:
                        findings.append(
                            {
                                "board": board_name,
                                "task_id": task_id,
                                "kind": "running_without_recent_heartbeat",
                                "age_seconds": int(now - last_signal),
                                "candidate_action": "inspect_worker_or_reclaim_claim",
                            }
                        )
                    if started_at and now - started_at >= task_timeout_seconds:
                        findings.append(
                            {
                                "board": board_name,
                                "task_id": task_id,
                                "kind": "running_exceeded_task_timeout",
                                "age_seconds": int(now - started_at),
                                "candidate_action": "review_long_running_task",
                            }
                        )
                elif status == "blocked":
                    blocked_at = _last_event_at(con, task_id, "blocked") or started_at
                    if blocked_at and now - blocked_at >= task_timeout_seconds:
                        findings.append(
                            {
                                "board": board_name,
                                "task_id": task_id,
                                "kind": "long_term_blocked",
                                "age_seconds": int(now - blocked_at),
                                "candidate_action": "surface_to_operator",
                            }
                        )
                relationship = build_relationship(con, task_id)
                if (
                    status == "blocked"
                    and relationship.review_required
                    and _has_review_approval_without_unblock(con, task_id)
                ):
                    findings.append(
                        {
                            "board": board_name,
                            "task_id": task_id,
                            "kind": "review_approved_but_still_blocked",
                            "candidate_action": "unblock_or_complete_after_review",
                        }
                    )
        if _has_table(con, "task_links") and _has_table(con, "tasks"):
            dependency_rows = _safe_select(
                con,
                """
                select child.id as child_id, child.status as child_status,
                       parent.id as parent_id, parent.status as parent_status
                from task_links l
                join tasks child on child.id = l.child_id
                join tasks parent on parent.id = l.parent_id
                where coalesce(child.status, '') in ('todo', 'ready', 'blocked')
                  and coalesce(parent.status, '') in ('blocked', 'gave_up', 'failed')
                """,
            )
            for row in dependency_rows:
                findings.append(
                    {
                        "board": board_name,
                        "task_id": _row_text(row, "child_id"),
                        "kind": "dependency_blocked_by_stuck_parent",
                        "parent_id": _row_text(row, "parent_id"),
                        "child_status": _row_text(row, "child_status"),
                        "parent_status": _row_text(row, "parent_status"),
                        "candidate_action": "surface_dependency_blocker_to_operator",
                    }
                )

            runnable_rows = _safe_select(
                con,
                """
                select child.id as child_id, child.status as child_status
                from tasks child
                where coalesce(child.status, '') in ('todo', 'blocked')
                  and exists (select 1 from task_links l where l.child_id = child.id)
                  and not exists (
                    select 1 from task_links l
                    join tasks parent on parent.id = l.parent_id
                    where l.child_id = child.id
                      and coalesce(parent.status, '') not in ('done', 'completed', 'cancelled', 'archived')
                  )
                """,
            )
            for row in runnable_rows:
                findings.append(
                    {
                        "board": board_name,
                        "task_id": _row_text(row, "child_id"),
                        "kind": "blocked_with_all_parents_done",
                        "child_status": _row_text(row, "child_status"),
                        "candidate_action": "promote_child_after_blockers_done",
                    }
                )

            root_rows = _safe_select(
                con,
                """
                select t.id from tasks t
                where not exists (select 1 from task_links l where l.child_id = t.id)
                  and exists (select 1 from task_links l where l.parent_id = t.id)
                  and coalesce(t.status, '') not in ('done', 'completed', 'cancelled')
                """,
            )
            for row in root_rows:
                root_id = _row_text(row, "id")
                child_rows = _safe_select(
                    con,
                    "select t.status from task_links l join tasks t on t.id = l.child_id where l.parent_id = ?",
                    (root_id,),
                )
                statuses = [_row_text(child, "status") for child in child_rows]
                if statuses and all(
                    status in {"done", "completed", "cancelled"} for status in statuses
                ):
                    findings.append(
                        {
                            "board": board_name,
                            "task_id": root_id,
                            "kind": "root_not_closed_after_children_done",
                            "candidate_action": "review_root_for_closure",
                        }
                    )
    return findings


def build_relationship(con: sqlite3.Connection, task_id: str | None) -> TaskRelationship:
    if not task_id:
        return TaskRelationship(task_id="")
    parents: list[str] = []
    children: list[str] = []
    if _has_table(con, "task_links"):
        parents = [
            _row_text(row, 0)
            for row in _safe_select(
                con,
                "select parent_id from task_links where child_id = ? order by parent_id",
                (task_id,),
            )
        ]
        children = [
            _row_text(row, 0)
            for row in _safe_select(
                con,
                "select child_id from task_links where parent_id = ? order by child_id",
                (task_id,),
            )
        ]
    root = task_id
    seen = {task_id}
    frontier = list(parents)
    while frontier:
        parent = frontier.pop(0)
        if parent in seen:
            continue
        seen.add(parent)
        root = parent
        if _has_table(con, "task_links"):
            frontier.extend(
                _row_text(row, 0)
                for row in _safe_select(
                    con,
                    "select parent_id from task_links where child_id = ? order by parent_id",
                    (parent,),
                )
            )
    comments_count = 0
    review_required = False
    if _has_table(con, "task_comments"):
        comment_rows = _safe_select(
            con, "select body from task_comments where task_id = ?", (task_id,)
        )
        comments_count = len(comment_rows)
        review_required = any(
            "review" in _row_text(row, 0).lower() or "approve" in _row_text(row, 0).lower()
            for row in comment_rows
        )
    if _task_title_or_status_contains_review(con, task_id):
        review_required = True
    return TaskRelationship(
        task_id=task_id,
        parents=parents,
        children=children,
        root_task_id=root,
        review_required=review_required,
        comments_count=comments_count,
    )


def _read_events(
    board_name: str,
    db_path: Path,
    cursor: int,
    *,
    limit: int,
    newest: bool = False,
    active_statuses: set[str] | None = None,
) -> list[BoardEvent]:
    if not _looks_like_board_db(db_path):
        return []
    op = ">=" if newest else ">"
    order = "desc" if newest else "asc"
    with managed_connection(db_path) as con:
        con.row_factory = sqlite3.Row
        has_tasks = _has_table(con, "tasks")
        status_select = ", t.status as task_status" if has_tasks else ", null as task_status"
        status_join = " left join tasks t on t.id = e.task_id" if has_tasks else ""
        rows = _safe_select(
            con,
            f"""
            select e.id, e.task_id, e.kind, e.payload, e.created_at, e.run_id{status_select}
            from task_events e{status_join}
            where e.id {op} ?
            order by e.id {order}
            limit ?
            """,
            (cursor if newest else cursor, limit),
        )
        if newest:
            rows = list(reversed(rows))
        return [_event_from_row(con, board_name, row, active_statuses=active_statuses) for row in rows]


def _event_from_row(
    con: sqlite3.Connection,
    board_name: str,
    row: sqlite3.Row,
    *,
    active_statuses: set[str] | None = None,
) -> BoardEvent:
    task_id = _row_text(row, "task_id") or None
    payload = _parse_json(_row_text(row, "payload"))
    task_status = _row_text(row, "task_status") or None
    if active_statuses is not None and task_status not in active_statuses:
        relationship = TaskRelationship(task_id=task_id or "")
    elif (
        payload
        and isinstance(payload.get("reason"), str)
        and "review" in str(payload["reason"]).lower()
    ):
        relationship = build_relationship(con, task_id)
        relationship = TaskRelationship(**{**relationship.to_dict(), "review_required": True})
    else:
        relationship = build_relationship(con, task_id)
    return BoardEvent(
        board_name=board_name,
        event_id=_row_int(row, "id"),
        task_id=task_id,
        kind=_row_text(row, "kind"),
        payload=payload,
        created_at=_row_float(row, "created_at"),
        run_id=_row_int_optional(row, "run_id"),
        task_status=task_status,
        relationship=relationship,
    )


def _board_path_for_name(name: str, hermes_home: Path) -> Path:
    raw = Path(name).expanduser()
    if raw.is_absolute():
        return raw
    if raw.suffix == ".db" or "/" in name:
        return hermes_home / raw
    return hermes_home / "kanban" / "boards" / name / "kanban.db"


def _looks_like_board_db(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with managed_connection(path) as con:
            return _has_table(con, "task_events")
    except sqlite3.Error:
        return False


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?", (table,)
    ).fetchone()
    return row is not None


def _safe_select(
    con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[sqlite3.Row]:
    try:
        return list(con.execute(sql, params))
    except sqlite3.Error:
        return []


def _parse_json(value: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _task_status(con: sqlite3.Connection, task_id: str | None) -> str | None:
    if not task_id or not _has_table(con, "tasks"):
        return None
    rows = _safe_select(con, "select status from tasks where id = ?", (task_id,))
    return _row_text(rows[0], 0) if rows else None


def _task_title_or_status_contains_review(con: sqlite3.Connection, task_id: str) -> bool:
    if not _has_table(con, "tasks"):
        return False
    rows = _safe_select(con, "select title, status from tasks where id = ?", (task_id,))
    if not rows:
        return False
    haystack = " ".join(_row_text(rows[0], key).lower() for key in (0, 1))
    return "review" in haystack or "approve" in haystack


def _last_event_at(con: sqlite3.Connection, task_id: str, kind: str) -> float | None:
    if not _has_table(con, "task_events"):
        return None
    rows = _safe_select(
        con,
        "select max(created_at) from task_events where task_id = ? and kind = ?",
        (task_id, kind),
    )
    return _row_float(rows[0], 0) if rows else None


def _has_review_approval_without_unblock(con: sqlite3.Connection, task_id: str) -> bool:
    if not _has_table(con, "task_comments"):
        return False
    comments = "\n".join(
        _row_text(row, 0).lower()
        for row in _safe_select(con, "select body from task_comments where task_id = ?", (task_id,))
    )
    if "approve" not in comments and "approved" not in comments and "通过" not in comments:
        return False
    unblocked = _last_event_at(con, task_id, "unblocked") or _last_event_at(
        con, task_id, "promoted"
    )
    return not bool(unblocked)


def _row_text(row: sqlite3.Row, key: str | int) -> str:
    value = row[key]
    return "" if value is None else str(value)


def _row_int(row: sqlite3.Row, key: str | int) -> int:
    return int(row[key])


def _row_int_optional(row: sqlite3.Row, key: str | int) -> int | None:
    value = row[key]
    return int(value) if value is not None else None


def _row_float(row: sqlite3.Row, key: str | int) -> float | None:
    value = row[key]
    return float(value) if value is not None else None
