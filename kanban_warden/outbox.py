"""Bounded notification outbox delivery without mutating Kanban board databases."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import KanbanWardenConfig
from .delivery import HermesMessageSender, MessageSender, target_from_subscription
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
    """Drain queued notification actions to Hermes message targets."""

    def __init__(
        self,
        config: KanbanWardenConfig,
        state_store: WardenStateStore,
        *,
        message_sender: MessageSender | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.message_sender = message_sender or HermesMessageSender()

    def drain(
        self, board_paths: Mapping[str, str | Path], *, now: float | None = None
    ) -> dict[str, Any]:
        current_time = time.time() if now is None else now
        if not self.config.notifications.delivery_enabled:
            return OutboxDeliveryReport(
                enabled=False, dry_run=self.config.auto_advance.dry_run
            ).to_dict()
        if self.config.auto_advance.dry_run:
            return OutboxDeliveryReport(enabled=True, dry_run=True).to_dict()

        rows = self.state_store.claim_notification_batch(
            limit=self.config.notifications.delivery_batch_size,
            now=current_time,
            lease_seconds=self.config.notifications.delivery_lease_seconds,
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
                delivered = self._deliver_one(row, board_paths, now=current_time)
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
            if delivered:
                report["delivered"] += 1
            else:
                report["skipped"] += 1
        return report

    def _deliver_one(
        self, row: dict[str, Any], board_paths: Mapping[str, str | Path], *, now: float
    ) -> bool:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        if not _origin_channel_enabled(payload):
            return False
        board_name = _text(payload.get("board_name")) or _text(payload.get("board")) or "default"
        task_id = _text(payload.get("target_task_id")) or _text(payload.get("task_id"))
        if not task_id:
            raise _PermanentDeliveryError("missing target task")
        db_path = board_paths.get(board_name)
        if db_path is None:
            raise _RetryableDeliveryError(f"board database not discovered for board {board_name}")
        with _readonly_connection(db_path) as con:
            if not _table_exists(con, "tasks") or not _task_exists(con, task_id):
                raise _RetryableDeliveryError("target task missing")
            subscribers = _native_subscribers(con, task_id)
        if not subscribers:
            raise _RetryableDeliveryError("no native kanban subscriber for target task")
        message = self._delivery_message(row, payload, board_name=board_name, task_id=task_id)
        _assert_secret_safe(message)
        for subscriber in subscribers:
            target = target_from_subscription(subscriber)
            result = self.message_sender.send(target, message)
            if not result.ok:
                raise _RetryableDeliveryError(result.error or "message send failed")
        return True

    def _delivery_message(
        self,
        row: dict[str, Any],
        payload: dict[str, Any],
        *,
        board_name: str,
        task_id: str,
    ) -> str:
        action_kind = _text(payload.get("kind"))
        reason = _text(payload.get("reason"))
        message = _text(payload.get("message"))
        title = _title_for_action(action_kind, reason)
        lines = [
            f"[Kanban Warden] {title}",
            "",
            f"Board: {board_name}",
            f"Task: {task_id}",
            f"Action: {action_kind or 'notify'}",
        ]
        if reason:
            lines.append(f"Reason: {reason[:240]}")
        if message:
            lines.extend(["", message[:800]])
        lines.extend(["", f"Outbox: {row['key']}"])
        return "\n".join(lines).strip()

    def _backoff_seconds(self, attempts_after: int) -> float:
        base = max(0.0, float(self.config.notifications.delivery_backoff_seconds))
        return base * max(1, attempts_after)


class _RetryableDeliveryError(RuntimeError):
    pass


class _PermanentDeliveryError(RuntimeError):
    pass


def _native_subscribers(con: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    if not _table_exists(con, "kanban_notify_subs"):
        return []
    rows = con.execute(
        """
        select platform, chat_id, thread_id, user_id, notifier_profile
        from kanban_notify_subs
        where task_id = ?
        order by platform, chat_id, thread_id
        """,
        (task_id,),
    ).fetchall()
    return [
        {
            "platform": row[0],
            "chat_id": row[1],
            "thread_id": row[2],
            "user_id": row[3],
            "notifier_profile": row[4],
        }
        for row in rows
    ]


@contextlib.contextmanager
def _readonly_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{Path(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _task_exists(con: sqlite3.Connection, task_id: str) -> bool:
    return (
        con.execute("select 1 from tasks where id = ? limit 1", (task_id,)).fetchone()
        is not None
    )

def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?", (name,)
        ).fetchone()
        is not None
    )

def _assert_secret_safe(text: str) -> None:
    if default_scanner().scan(text):
        raise _PermanentDeliveryError("notification evidence contains secret-like text")


def _origin_channel_enabled(payload: dict[str, Any]) -> bool:
    action_payload = payload.get("payload")
    channels = action_payload.get("channels") if isinstance(action_payload, dict) else None
    if channels is None:
        channels = payload.get("channels")
    if not isinstance(channels, list):
        return False
    return any(channel == "origin" for channel in channels)


def _title_for_action(kind: str, reason: str) -> str:
    titles = {
        "create_reviewer": "Review required",
        "create_implementer_followup": "Changes requested",
        "create_blocked_remediation": "Blocked remediation",
        "retry": "Task retry planned",
        "escalate": "Retry exhausted",
        "promote": "Task can be promoted",
        "finalize": "Task can be finalized",
    }
    if kind == "notify" and reason:
        return reason.splitlines()[0][:80]
    return titles.get(kind, "Notification")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)
