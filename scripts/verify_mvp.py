#!/usr/bin/env python3
"""Build a disposable Kanban board and verify core kanban-warden MVP behavior.

The script exercises event collection, relationship inference, health findings,
dry-run planning, leader-lock status, reviewer-card creation, comments, unblocks,
notification outbox, and idempotency without touching the user's real Kanban DB.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kanban_warden.config import KanbanWardenConfig  # noqa: E402
from kanban_warden.supervisor import WardenSupervisor  # noqa: E402


def init_real_schema_board(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        create table tasks (
          id text primary key,
          title text not null,
          body text,
          assignee text,
          status text not null,
          priority integer default 0,
          created_by text,
          created_at integer not null,
          started_at integer,
          completed_at integer,
          workspace_kind text not null default 'scratch',
          workspace_path text,
          claim_lock text,
          claim_expires integer,
          tenant text,
          result text,
          idempotency_key text,
          consecutive_failures integer not null default 0,
          worker_pid integer,
          last_failure_error text,
          max_runtime_seconds integer,
          last_heartbeat_at integer,
          current_run_id integer,
          workflow_template_id text,
          current_step_key text,
          skills text,
          max_retries integer,
          branch_name text,
          model_override text,
          session_id text,
          goal_mode integer not null default 0,
          goal_max_turns integer
        );
        create index idx_tasks_idempotency on tasks(idempotency_key);
        create table task_events (
          id integer primary key autoincrement,
          task_id text not null,
          run_id integer,
          kind text not null,
          payload text,
          created_at integer not null
        );
        create table task_links (
          parent_id text not null,
          child_id text not null,
          primary key(parent_id, child_id)
        );
        create table task_comments (
          id integer primary key autoincrement,
          task_id text not null,
          author text not null,
          body text not null,
          created_at integer not null
        );
        """
    )
    con.commit()
    con.close()


def add_task(
    con: sqlite3.Connection,
    task_id: str,
    title: str,
    status: str,
    assignee: str,
    created_at: int,
    *,
    started_at: int | None = None,
) -> None:
    con.execute(
        """
        insert into tasks(id, title, status, assignee, created_at, started_at, workspace_kind)
        values (?, ?, ?, ?, ?, ?, 'scratch')
        """,
        (task_id, title, status, assignee, created_at, started_at),
    )


def add_event(
    con: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict[str, object],
    created_at: int,
) -> None:
    con.execute(
        "insert into task_events(task_id, kind, payload, created_at, run_id) values (?, ?, ?, ?, null)",
        (task_id, kind, json.dumps(payload), created_at),
    )


def count(con: sqlite3.Connection, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kanban-warden-mvp-") as tmp:
        root = Path(tmp)
        hermes_home = root / "home" / ".hermes"
        board = hermes_home / "kanban.db"
        dry_state_db = root / "warden-dry-state.db"
        apply_state_db = root / "warden-apply-state.db"
        state_db = apply_state_db
        lock_db = root / "leader-lock.db"
        init_real_schema_board(board)
        con = sqlite3.connect(board)
        add_task(con, "impl", "Implement feature", "blocked", "hairou", 1)
        add_task(con, "review_impl", "Review feature", "done", "reviewer", 2)
        add_task(con, "stale", "Stale worker", "running", "hairou", 1, started_at=1)
        con.execute("insert into task_links(parent_id, child_id) values ('impl', 'review_impl')")
        add_event(con, "impl", "blocked", {"reason": "review-required: inspect diff"}, 3)
        add_event(
            con,
            "review_impl",
            "completed",
            {
                "verdict": "needs-changes",
                "source_task": "impl",
                "body": "NEEDS-CHANGES: add focused regression coverage",
            },
            4,
        )
        con.commit()
        con.close()

        base: dict[str, Any] = {
            "enabled": True,
            "hermes_home": str(hermes_home),
            "state_db_path": str(dry_state_db),
            "leader_lock": {
                "enabled": True,
                "db_path": str(lock_db),
                "lease_seconds": 60,
                "heartbeat_seconds": 20,
            },
            "loop": {"event_interval_seconds": 5, "health_sweep_seconds": 0},
            "notifications": {"enabled": True, "channels": ["origin"]},
            "auto_advance": {
                "enabled": True,
                "dry_run": True,
            },
            "limits": {"max_retries": 1, "task_timeout_seconds": 10, "stale_claim_seconds": 5},
        }
        dry_config = KanbanWardenConfig.from_mapping(base)
        dry_report = WardenSupervisor(dry_config, profile_name="verify-dry").dry_run(now=20)
        dry_kinds = {action["kind"] for action in dry_report["planned_actions"]}
        assert {"create_reviewer", "create_implementer_followup", "comment", "unblock", "retry"}.issubset(
            dry_kinds
        ), dry_kinds
        assert "notify" not in dry_kinds
        assert all(
            result["applied"] is False and result["note"] == "dry-run"
            for result in dry_report["action_results"]
        )

        apply_base = {**base, "state_db_path": str(apply_state_db)}
        apply_config = KanbanWardenConfig.from_mapping(
            {**apply_base, "auto_advance": {**base["auto_advance"], "dry_run": False}}
        )
        supervisor = WardenSupervisor(apply_config, profile_name="verify-apply")
        assert supervisor._ensure_leader(__import__("time").time()) is True
        apply_report = supervisor.collect(now=20)
        second_report = supervisor.collect(now=21)
        status = supervisor.status()

        con = sqlite3.connect(board)
        reviewer_count = count(con, "select count(*) from tasks where id = 'review_impl'")
        followup_count = count(con, "select count(*) from tasks where id = 'fix_impl_review_impl'")
        impl_status = con.execute("select status from tasks where id = 'impl'").fetchone()[0]
        comments = con.execute(
            "select task_id, author, body from task_comments order by id"
        ).fetchall()
        con.close()
        state_con = sqlite3.connect(state_db)
        outbox_count = count(state_con, "select count(*) from notification_outbox")
        outbox_keys = {
            row[0]
            for row in state_con.execute("select key from notification_outbox order by key").fetchall()
        }
        state_con.close()

        assert reviewer_count == 1
        assert followup_count == 0
        assert impl_status == "blocked"
        assert comments == []
        assert outbox_count >= 1
        assert {
            "reviewer:default:impl",
            "implementer-followup:default:review_impl:impl",
            "review-needs-changes:default:review_impl:impl",
            "review-needs-changes-unblock:default:review_impl:impl",
        }.issubset(outbox_keys)
        assert not any(key.startswith("event:") and ":notify:" in key for key in outbox_keys)
        # A second collection may see events emitted by the first mutation pass
        # (for example created/escalated comments), but it must not duplicate
        # the reviewer card or regress the source task status.
        con = sqlite3.connect(board)
        assert count(con, "select count(*) from tasks where id = 'review_impl'") == 1
        assert con.execute("select status from tasks where id = 'impl'").fetchone()[0] == "blocked"
        con.close()
        assert status["leader_lock"]["active"] is True

        summary = {
            "ok": True,
            "workspace": tmp,
            "dry_run_planned_actions": sorted(dry_kinds),
            "apply_results": [r for r in apply_report["action_results"] if r["applied"]],
            "second_run_applied_count": sum(
                1 for r in second_report["action_results"] if r["applied"]
            ),
            "impl_status_after_apply": impl_status,
            "reviewer_task_count": reviewer_count,
            "implementer_followup_count": followup_count,
            "comment_count": len(comments),
            "notification_outbox_count": outbox_count,
            "gateway_required_outbox_count": sum(
                1
                for result in apply_report["action_results"]
                if result["note"] == "board-write-disabled"
            ),
            "leader_lock_active": status["leader_lock"]["active"],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
