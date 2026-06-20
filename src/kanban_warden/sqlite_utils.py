"""SQLite connection helpers for long-lived warden processes."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def managed_connection(
    db_path: str | Path,
    **kwargs: Any,
) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection, commit/rollback like sqlite3's context manager, and close it.

    ``with sqlite3.connect(...) as con`` commits or rolls back but deliberately
    leaves the connection open. The warden supervisor runs inside the long-lived
    gateway process and polls many boards every few seconds, so leaked
    connections accumulate stale ``kanban.db-wal``/``kanban.db-shm`` file
    descriptors and can make later WAL setup fail with ``disk I/O error``.
    """

    conn = sqlite3.connect(db_path, **kwargs)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()
