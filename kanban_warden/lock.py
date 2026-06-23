"""SQLite leader lock with lease and heartbeat semantics."""

from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .sqlite_utils import managed_connection


@dataclass(frozen=True)
class LockStatus:
    name: str
    owner: str | None
    expires_at: float | None
    now: float

    @property
    def active(self) -> bool:
        return bool(self.owner and self.expires_at and self.expires_at > self.now)


class LeaderLock:
    """A small SQLite lock safe for multi-process plugin instances."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        name: str = "kanban-warden",
        owner: str | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.name = name
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def acquire(self, *, lease_seconds: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        expires = now + lease_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, expires_at FROM locks WHERE name=?", (self.name,)
            ).fetchone()
            if row and row[0] != self.owner and float(row[1]) > now:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO locks(name, owner, expires_at, heartbeat_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "owner=excluded.owner, "
                "expires_at=excluded.expires_at, "
                "heartbeat_at=excluded.heartbeat_at",
                (self.name, self.owner, expires, now),
            )
            conn.commit()
            return True

    def heartbeat(self, *, lease_seconds: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        expires = now + lease_seconds
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE locks SET expires_at=?, heartbeat_at=? WHERE name=? AND owner=?",
                (expires, now, self.name, self.owner),
            )
            return cur.rowcount == 1

    def release(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM locks WHERE name=? AND owner=?", (self.name, self.owner))

    def status(self) -> LockStatus:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner, expires_at FROM locks WHERE name=?", (self.name,)
            ).fetchone()
        if not row:
            return LockStatus(self.name, None, None, now)
        return LockStatus(self.name, str(row[0]), float(row[1]), now)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self) -> None:
        with managed_connection(self.db_path, timeout=10) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS locks ("
                "name TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, "
                "expires_at REAL NOT NULL, "
                "heartbeat_at REAL NOT NULL)"
            )

