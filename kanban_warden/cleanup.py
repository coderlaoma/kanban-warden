"""Cleanup planning and official-Hermes execution for Kanban Warden."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
_SECONDS_PER_DAY = 24 * 3600


@dataclass(frozen=True)
class StateCleanupConfig:
    retention_days: int = 7
    vacuum: bool = True


@dataclass(frozen=True)
class CleanupPlan:
    archive_done_ids: list[str] = field(default_factory=list)
    purge_archived_ids: list[str] = field(default_factory=list)
    should_run_gc: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_done_ids": list(self.archive_done_ids),
            "purge_archived_ids": list(self.purge_archived_ids),
            "should_run_gc": self.should_run_gc,
        }


def plan_cleanup(
    conn: sqlite3.Connection,
    *,
    now: float,
    done_retention_days: int,
    archived_retention_days: int,
    archive_done: bool,
    purge_archived: bool,
) -> CleanupPlan:
    conn.row_factory = sqlite3.Row
    archive_done_ids: list[str] = []
    purge_archived_ids: list[str] = []
    if archive_done:
        cutoff = int(now) - int(done_retention_days) * _SECONDS_PER_DAY
        archive_done_ids = _ids_for_terminal_cutoff(conn, "done", cutoff)
    if purge_archived:
        cutoff = int(now) - int(archived_retention_days) * _SECONDS_PER_DAY
        purge_archived_ids = _ids_for_terminal_cutoff(conn, "archived", cutoff)
    return CleanupPlan(
        archive_done_ids=archive_done_ids,
        purge_archived_ids=purge_archived_ids,
        should_run_gc=bool(archive_done_ids or purge_archived_ids),
    )


def execute_cleanup_plan(
    plan: CleanupPlan,
    *,
    board: str,
    db_path: str | os.PathLike[str],
    gc_retention_days: int,
    run_gc: bool,
) -> dict[str, Any]:
    results: dict[str, Any] = {"plan": plan.to_dict(), "commands": []}
    if plan.archive_done_ids:
        results["commands"].append(
            _run_hermes(["kanban", "archive", *plan.archive_done_ids], db_path=db_path)
        )
    if plan.purge_archived_ids:
        results["commands"].append(
            _run_hermes(["kanban", "archive", "--rm", *plan.purge_archived_ids], db_path=db_path)
        )
    if run_gc and plan.should_run_gc:
        days = str(int(gc_retention_days))
        results["commands"].append(
            _run_hermes([
                "kanban",
                "gc",
                "--event-retention-days",
                days,
                "--log-retention-days",
                days,
            ],
                db_path=db_path,
            )
        )
    return results



def prune_state_store(
    db_path: str | os.PathLike[str],
    *,
    now: float,
    config: StateCleanupConfig,
) -> dict[str, Any]:
    """Prune bounded Warden runtime state that is safe to rebuild or no longer actionable."""

    path = Path(db_path).expanduser()
    if not path.exists():
        return {
            "state_db_path": str(path),
            "skipped": "missing",
            "processed_keys_deleted": 0,
            "action_log_deleted": 0,
            "notification_outbox_deleted": 0,
            "retry_budgets_deleted": 0,
            "vacuumed": False,
        }
    cutoff = float(now) - int(config.retention_days) * _SECONDS_PER_DAY
    with sqlite3.connect(path) as conn:
        processed = _delete_if_table_exists(
            conn,
            "processed_keys",
            "created_at < ?",
            (cutoff,),
        )
        actions = _delete_if_table_exists(
            conn,
            "action_log",
            "status in ('done', 'failed') and updated_at < ?",
            (cutoff,),
        )
        notifications = _delete_if_table_exists(
            conn,
            "notification_outbox",
            "status in ('delivered', 'exhausted') and updated_at < ?",
            (cutoff,),
        )
        retries = _delete_if_table_exists(
            conn,
            "retry_budgets",
            "updated_at < ?",
            (cutoff,),
        )
        conn.commit()
        vacuumed = False
        if config.vacuum and any((processed, actions, notifications, retries)):
            conn.execute("pragma wal_checkpoint(truncate)")
            conn.execute("vacuum")
            vacuumed = True
    return {
        "state_db_path": str(path),
        "retention_days": int(config.retention_days),
        "processed_keys_deleted": processed,
        "action_log_deleted": actions,
        "notification_outbox_deleted": notifications,
        "retry_budgets_deleted": retries,
        "vacuumed": vacuumed,
    }


def _delete_if_table_exists(
    conn: sqlite3.Connection,
    table: str,
    predicate: str,
    params: tuple[Any, ...],
) -> int:
    exists = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?", (table,)
    ).fetchone()
    if not exists:
        return 0
    before = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
    conn.execute(f"delete from {table} where {predicate}", params)
    after = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
    return before - after


def _ids_for_terminal_cutoff(conn: sqlite3.Connection, status: str, cutoff: int) -> list[str]:
    rows = conn.execute(
        """
        select id from tasks
        where status = ?
          and coalesce(completed_at, created_at, 0) > 0
          and coalesce(completed_at, created_at, 0) < ?
        order by id
        """,
        (status, cutoff),
    ).fetchall()
    return [str(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]


def _run_hermes(args: list[str], *, db_path: str | os.PathLike[str]) -> dict[str, Any]:
    cmd = [_hermes_python(), "-m", "hermes_cli.main", *args]
    env = os.environ.copy()
    env["HERMES_KANBAN_DB"] = str(Path(db_path).expanduser())
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=300, env=env)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        LOGGER.warning("kanban-warden cleanup command failed rc=%s cmd=%s", result.returncode, cmd)
    return {"cmd": cmd, "returncode": result.returncode, "output_tail": output[-2000:]}


def _hermes_python() -> str:
    configured = os.environ.get("KANBAN_WARDEN_HERMES_PYTHON")
    if configured:
        return configured
    service_python = Path.home() / ".local" / "share" / "hermes-agent-venv" / "bin" / "python"
    if service_python.exists():
        return str(service_python)
    return sys.executable
