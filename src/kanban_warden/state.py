"""Persistent state store for Kanban Warden event processing."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class WardenStateStore:
    """Small SQLite store for cursors, idempotency keys, retries, and runtime metadata."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get_cursor(self, board_name: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "select cursor from board_cursors where board_name = ?", (board_name,)
            ).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, board_name: str, cursor: int) -> None:
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                insert into board_cursors(board_name, cursor, updated_at) values (?, ?, ?)
                on conflict(board_name) do update set cursor = excluded.cursor, updated_at = excluded.updated_at
                """,
                (board_name, cursor, now),
            )

    def mark_processed(self, key: str) -> bool:
        try:
            with self._connect() as con:
                con.execute(
                    "insert into processed_keys(key, created_at) values (?, ?)", (key, time.time())
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def bump_retry(self, board_name: str, task_id: str, action: str) -> int:
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                insert into retry_budgets(board_name, task_id, action, attempts, updated_at)
                values (?, ?, ?, 1, ?)
                on conflict(board_name, task_id, action) do update set
                  attempts = retry_budgets.attempts + 1,
                  updated_at = excluded.updated_at
                """,
                (board_name, task_id, action, now),
            )
            row = con.execute(
                "select attempts from retry_budgets where board_name = ? and task_id = ? and action = ?",
                (board_name, task_id, action),
            ).fetchone()
        return int(row[0])

    def peek_retry(self, board_name: str, task_id: str, action: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "select attempts from retry_budgets where board_name = ? and task_id = ? and action = ?",
                (board_name, task_id, action),
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_action_started(self, key: str) -> bool:
        now = time.time()
        with self._connect() as con:
            row = con.execute("select status from action_log where key = ?", (key,)).fetchone()
            if row:
                if str(row[0]) == "done":
                    return False
                con.execute(
                    """
                    update action_log
                    set status = 'started', attempts = attempts + 1, updated_at = ?
                    where key = ?
                    """,
                    (now, key),
                )
                return True
            con.execute(
                "insert into action_log(key, status, attempts, created_at, updated_at) values (?, 'started', 1, ?, ?)",
                (key, now, now),
            )
        return True

    def mark_action_done(self, key: str, note: str = "") -> None:
        with self._connect() as con:
            con.execute(
                "update action_log set status = 'done', note = ?, updated_at = ? where key = ?",
                (note, time.time(), key),
            )

    def mark_action_failed(self, key: str, error: str) -> None:
        with self._connect() as con:
            con.execute(
                "update action_log set status = 'failed', note = ?, updated_at = ? where key = ?",
                (error[:1000], time.time(), key),
            )

    def enqueue_notification(self, key: str, payload: dict[str, Any]) -> bool:
        now = time.time()
        try:
            with self._connect() as con:
                con.execute(
                    "insert into notification_outbox(key, payload_json, status, attempts, created_at, updated_at) values (?, ?, 'queued', 0, ?, ?)",
                    (key, json.dumps(payload, sort_keys=True), now, now),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def set_runtime_metadata(self, key: str, value: dict[str, Any]) -> None:
        with self._connect() as con:
            con.execute(
                """
                insert into runtime_metadata(key, value_json, updated_at) values (?, ?, ?)
                on conflict(key) do update set value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, sort_keys=True), time.time()),
            )

    def get_runtime_metadata(self, key: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "select value_json from runtime_metadata where key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        raw = json.loads(str(row[0]))
        return raw if isinstance(raw, dict) else None

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as con:
            cursors = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    "select board_name, cursor from board_cursors order by board_name"
                )
            }
            retry_rows = [
                {
                    "board_name": str(row[0]),
                    "task_id": str(row[1]),
                    "action": str(row[2]),
                    "attempts": int(row[3]),
                }
                for row in con.execute(
                    "select board_name, task_id, action, attempts from retry_budgets order by board_name, task_id, action"
                )
            ]
            processed_count = int(con.execute("select count(*) from processed_keys").fetchone()[0])
            action_rows = [
                {"key": str(row[0]), "status": str(row[1]), "attempts": int(row[2])}
                for row in con.execute(
                    "select key, status, attempts from action_log order by updated_at desc, key limit 50"
                )
            ]
            outbox_count = int(
                con.execute("select count(*) from notification_outbox").fetchone()[0]
            )
        return {
            "cursors": cursors,
            "processed_key_count": processed_count,
            "retry_budgets": retry_rows,
            "action_log": action_rows,
            "notification_outbox_count": outbox_count,
        }

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("pragma journal_mode = wal")
        return con

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.executescript(
                """
                create table if not exists board_cursors (
                  board_name text primary key,
                  cursor integer not null default 0,
                  updated_at real not null
                );
                create table if not exists processed_keys (
                  key text primary key,
                  created_at real not null
                );
                create table if not exists retry_budgets (
                  board_name text not null,
                  task_id text not null,
                  action text not null,
                  attempts integer not null default 0,
                  updated_at real not null,
                  primary key(board_name, task_id, action)
                );
                create table if not exists runtime_metadata (
                  key text primary key,
                  value_json text not null,
                  updated_at real not null
                );
                create table if not exists action_log (
                  key text primary key,
                  status text not null,
                  attempts integer not null default 0,
                  note text not null default '',
                  created_at real not null,
                  updated_at real not null
                );
                create table if not exists notification_outbox (
                  key text primary key,
                  payload_json text not null,
                  status text not null default 'queued',
                  attempts integer not null default 0,
                  last_error text,
                  created_at real not null,
                  updated_at real not null
                );
                """
            )
