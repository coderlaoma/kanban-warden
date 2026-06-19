"""Bounded notification outbox delivery through native Kanban evidence."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import KanbanWardenConfig
from .state import WardenStateStore
from .warden import default_scanner


@dataclass(frozen=True)
class OutboxDeliveryReport:
    enabled: bool
    dry_run: bool
    processed: int = 0
    delivered: int = 0
    retrying: int = 0
    failed: int = 0
    exhausted: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "processed": self.processed,
            "delivered": self.delivered,
            "retrying": self.retrying,
            "failed": self.failed,
            "exhausted": self.exhausted,
            "skipped": self.skipped,
        }


class NotificationOutboxDrainer:
    """Drain queued notification actions by writing observable Kanban evidence."""

    def __init__(self, config: KanbanWardenConfig, state_store: WardenStateStore) -> None:
        self.config = config
        self.state_store = state_store

    def drain(self, board_paths: Mapping[str, str | Path], *, now: float | None = None) -> dict[str, Any]:
        current_time = time.time() if now is None else now
        if not self.config.notifications.delivery_enabled:
            return OutboxDeliveryReport(enabled=False, dry_run=self.config.auto_advance.dry_run).to_dict()
        if self.config.auto_advance.dry_run:
            return OutboxDeliveryReport(enabled=True, dry_run=True).to_dict()

        rows = self.state_store.claim_notification_batch(
            limit=self.config.notifications.delivery_batch_size,
            now=current_time,
        )
        report = {
            "enabled": True,
            "dry_run": False,
            "processed": len(rows),
            "delivered": 0,
            "retrying": 0,
            "failed": 0,
            "exhausted": 0,
            "skipped": 0,
        }
        for row in rows:
            try:
                self._deliver_one(row, board_paths, now=current_time)
            except _PermanentDeliveryError as exc:
                self.state_store.mark_notification_retry(
                    row["key"],
                    error=str(exc),
                    now=current_time,
                    next_attempt_at=current_time,
                    exhausted=True,
                )
                report["exhausted"] += 1
                continue
            except _RetryableDeliveryError as exc:
                attempts_after = int(row["attempts"]) + 1
                exhausted = attempts_after >= self.config.notifications.delivery_max_attempts
                self.state_store.mark_notification_retry(
                    row["key"],
                    error=str(exc),
                    now=current_time,
                    next_attempt_at=current_time
                    + self._backoff_seconds(attempts_after),
                    exhausted=exhausted,
                )
                if exhausted:
                    report["exhausted"] += 1
                else:
                    report["retrying"] += 1
                continue
            self.state_store.mark_notification_delivered(row["key"], now=current_time)
            report["delivered"] += 1
        return report

    def _deliver_one(self, row: dict[str, Any], board_paths: Mapping[str, str | Path], *, now: float) -> None:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        board_name = _text(payload.get("board_name")) or _text(payload.get("board")) or "default"
        task_id = _text(payload.get("target_task_id")) or _text(payload.get("task_id"))
        if not task_id:
            raise _PermanentDeliveryError("missing target task")
        db_path = board_paths.get(board_name)
        if db_path is None:
            raise _RetryableDeliveryError(f"board database not discovered for board {board_name}")
        with sqlite3.connect(db_path) as con:
            if not _table_exists(con, "tasks") or not _task_exists(con, task_id):
                raise _RetryableDeliveryError("target task missing")
            if not _has_native_subscriber(con, task_id):
                raise _RetryableDeliveryError("no native kanban subscriber for target task")
            evidence = self._evidence_payload(row, payload)
            evidence_text = self._evidence_comment(row, payload)
            _assert_secret_safe(json.dumps(evidence, sort_keys=True))
            _assert_secret_safe(evidence_text)
            if (
                self.config.notifications.evidence_comments
                and _table_exists(con, "task_comments")
                and not _comment_exists(con, task_id, row["key"])
            ):
                _insert_comment(con, task_id, evidence_text, int(now))
            if (
                self.config.notifications.evidence_events
                and _table_exists(con, "task_events")
                and not _event_exists(con, task_id, row["key"])
            ):
                _insert_event(con, task_id, evidence, now)

    def _evidence_payload(self, row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "by": "kanban-warden",
            "event": "warden-notification-delivered",
            "outbox_key": row["key"],
            "attempt": int(row["attempts"]) + 1,
            "task_id": _text(payload.get("task_id")),
            "action_kind": _text(payload.get("kind")),
            "reason": _text(payload.get("reason"))[:240],
            "message": _text(payload.get("message"))[:500],
            "native_route": "kanban_notify_subs",
        }

    def _evidence_comment(self, row: dict[str, Any], payload: dict[str, Any]) -> str:
        reason = _text(payload.get("reason")) or "notification action"
        message = _text(payload.get("message"))
        body = (
            "[warden-notification] Native Kanban subscriber evidence created for "
            f"{reason}.\n\n"
            f"{message[:500]}\n\n"
            f"warden-outbox: {row['key']}"
        )
        return body.strip()

    def _backoff_seconds(self, attempts_after: int) -> float:
        base = max(0.0, float(self.config.notifications.delivery_backoff_seconds))
        return base * max(1, attempts_after)


class _RetryableDeliveryError(RuntimeError):
    pass


class _PermanentDeliveryError(RuntimeError):
    pass


def _has_native_subscriber(con: sqlite3.Connection, task_id: str) -> bool:
    if not _table_exists(con, "kanban_notify_subs"):
        return False
    row = con.execute(
        "select 1 from kanban_notify_subs where task_id = ? limit 1", (task_id,)
    ).fetchone()
    return row is not None


def _task_exists(con: sqlite3.Connection, task_id: str) -> bool:
    return con.execute("select 1 from tasks where id = ? limit 1", (task_id,)).fetchone() is not None


def _comment_exists(con: sqlite3.Connection, task_id: str, key: str) -> bool:
    row = con.execute(
        "select 1 from task_comments where task_id = ? and body like ? limit 1",
        (task_id, f"%{key}%"),
    ).fetchone()
    return row is not None


def _event_exists(con: sqlite3.Connection, task_id: str, key: str) -> bool:
    row = con.execute(
        "select 1 from task_events where task_id = ? and payload like ? limit 1",
        (task_id, f"%{key}%"),
    ).fetchone()
    return row is not None


def _insert_comment(con: sqlite3.Connection, task_id: str, body: str, now: int) -> None:
    columns = _table_columns(con, "task_comments")
    values: dict[str, Any] = {
        "task_id": task_id,
        "author": "kanban-warden",
        "body": body,
        "created_at": now,
    }
    insert_columns = [column for column in values if column in columns]
    if not insert_columns:
        return
    placeholders = ", ".join("?" for _ in insert_columns)
    con.execute(
        f"insert into task_comments({', '.join(insert_columns)}) values ({placeholders})",
        tuple(values[column] for column in insert_columns),
    )


def _insert_event(
    con: sqlite3.Connection, task_id: str, payload: dict[str, Any], now: float
) -> None:
    columns = _table_columns(con, "task_events")
    values: dict[str, Any] = {
        "task_id": task_id,
        "kind": "commented",
        "payload": json.dumps(payload, sort_keys=True),
        "created_at": now,
        "run_id": None,
    }
    insert_columns = [column for column in values if column in columns]
    if not insert_columns:
        return
    placeholders = ", ".join("?" for _ in insert_columns)
    con.execute(
        f"insert into task_events({', '.join(insert_columns)}) values ({placeholders})",
        tuple(values[column] for column in insert_columns),
    )


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?", (name,)
        ).fetchone()
        is not None
    )


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"pragma table_info({table})")}


def _assert_secret_safe(text: str) -> None:
    if default_scanner().scan(text):
        raise _PermanentDeliveryError("notification evidence contains secret-like text")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)
