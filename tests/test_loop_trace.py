from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kanban_warden.config import KanbanWardenConfig
from kanban_warden.state import WardenStateStore
from kanban_warden.supervisor import WardenSupervisor


def _init_board(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        create table tasks (
          id text primary key,
          title text,
          status text,
          assignee text,
          created_at integer,
          started_at integer,
          completed_at integer,
          current_run_id integer
        );
        create table task_events (
          id integer primary key autoincrement,
          task_id text,
          kind text not null,
          payload text,
          created_at integer,
          run_id integer
        );
        create table task_links (parent_id text not null, child_id text not null);
        create table task_comments (
          id integer primary key autoincrement,
          task_id text,
          body text,
          created_at integer
        );
        create table runs (
          id integer primary key,
          task_id text,
          profile text,
          status text,
          started_at integer,
          ended_at integer
        );
        """
    )
    con.commit()
    con.close()


def _event(
    db_path: Path,
    task_id: str,
    kind: str,
    payload: dict[str, object] | None = None,
    created_at: int = 100,
) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        "insert into task_events(task_id, kind, payload, created_at, run_id) values (?, ?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload) if payload is not None else None, created_at, None),
    )
    con.commit()
    con.close()


def test_state_store_records_loop_trace_and_outcome(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")

    trace = store.record_loop_trace(
        board_name="default",
        task_id="task-1",
        profile_name="tester",
        loop_state="waiting_for_review",
        observed_facts={"event_kind": "blocked", "task_status": "blocked"},
        matched_policy="review_required",
        decision="create_reviewer",
        confidence="high",
        planned_action={"kind": "create_reviewer"},
        verification_contract={"success": "reviewer_card_exists"},
        created_at=123.0,
    )
    outcome = store.record_loop_outcome(
        trace_id=trace["trace_id"],
        board_name="default",
        task_id="task-1",
        action_type="create_reviewer",
        status="planned",
        verification_status="pending",
        human_override=False,
        override_reason="",
        latency_seconds=0.0,
        created_at=124.0,
    )

    snapshot = store.snapshot()

    assert trace["trace_id"].startswith("loop-trace:")
    assert outcome["trace_id"] == trace["trace_id"]
    assert snapshot["loop_trace_count"] == 1
    assert snapshot["loop_outcome_count"] == 1
    assert snapshot["loop_traces_recent"][0]["loop_state"] == "waiting_for_review"
    assert snapshot["loop_traces_recent"][0]["observed_facts"] == {
        "event_kind": "blocked",
        "task_status": "blocked",
    }
    assert snapshot["loop_outcomes_recent"][0]["verification_status"] == "pending"


def test_state_store_uses_deterministic_loop_trace_ids(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")

    trace = store.record_loop_trace(
        board_name="default",
        task_id="task-1",
        profile_name="tester",
        loop_state="waiting_for_review",
        observed_facts={"event_kind": "blocked"},
        matched_policy="review_required",
        decision="create_reviewer",
        confidence="high",
        planned_action={"kind": "create_reviewer"},
        verification_contract={"success": "reviewer_card_exists"},
        created_at=123.0,
    )

    assert trace["trace_id"] == "loop-trace:123000:af30383c7aa39a85"


def test_state_store_deduplicates_same_loop_trace_decision(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    kwargs = {
        "board_name": "default",
        "task_id": "task-1",
        "profile_name": "tester",
        "loop_state": "waiting_for_review",
        "observed_facts": {"event_kind": "blocked"},
        "matched_policy": "review_required",
        "decision": "create_reviewer",
        "confidence": "high",
        "planned_action": {"kind": "create_reviewer"},
        "verification_contract": {"success": "reviewer_card_exists"},
        "created_at": 123.0,
    }

    first = store.record_loop_trace(**kwargs)
    second = store.record_loop_trace(**kwargs)
    store.record_loop_outcome(
        trace_id=first["trace_id"],
        board_name="default",
        task_id="task-1",
        action_type="create_reviewer",
        status="planned",
        verification_status="pending",
        created_at=123.0,
    )
    store.record_loop_outcome(
        trace_id=second["trace_id"],
        board_name="default",
        task_id="task-1",
        action_type="create_reviewer",
        status="planned",
        verification_status="pending",
        created_at=123.0,
    )

    snapshot = store.snapshot()
    assert second["trace_id"] == first["trace_id"]
    assert snapshot["loop_trace_count"] == 1
    assert snapshot["loop_outcome_count"] == 1


def test_supervisor_dry_run_includes_loop_trace_for_review_required_card(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "home" / ".hermes"
    board = hermes_home / "kanban.db"
    _init_board(board)
    con = sqlite3.connect(board)
    con.execute(
        """
        insert into tasks(id, title, status, assignee, created_at)
        values ('impl', 'Implementation', 'blocked', 'worker', 1)
        """
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: check diff"}, 2)
    config = KanbanWardenConfig.from_mapping(
        {
            "enabled": True,
            "leader_lock": {"enabled": False},
            "state_db_path": str(tmp_path / "warden-state.db"),
            "hermes_home": str(hermes_home),
            "auto_advance": {"enabled": True, "dry_run": True},
            "loop": {"health_sweep_seconds": 0},
        }
    )

    report = WardenSupervisor(config, profile_name="tester").dry_run(now=20)

    traces = report["loop_traces"]
    assert len(traces) == 1
    assert traces[0]["task_id"] == "impl"
    assert traces[0]["loop_state"] == "waiting_for_review"
    assert traces[0]["matched_policy"] == "review_required"
    assert traces[0]["decision"] == "create_reviewer"
    assert traces[0]["planned_action"]["kind"] == "create_reviewer"
    assert report["state"]["loop_trace_count"] == 1


def test_supervisor_dry_run_includes_loop_trace_for_stale_running_recovery(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "home" / ".hermes"
    board = hermes_home / "kanban.db"
    _init_board(board)
    con = sqlite3.connect(board)
    con.execute(
        """
        insert into tasks(id, title, status, assignee, created_at, started_at, current_run_id)
        values ('worker-task', 'Worker task', 'running', 'worker', 1, 1, 1)
        """
    )
    con.commit()
    con.close()
    config = KanbanWardenConfig.from_mapping(
        {
            "enabled": True,
            "leader_lock": {"enabled": False},
            "state_db_path": str(tmp_path / "warden-state.db"),
            "hermes_home": str(hermes_home),
            "auto_advance": {"enabled": True, "dry_run": True},
            "limits": {
                "max_retries": 2,
                "stale_claim_seconds": 5,
                "task_timeout_seconds": 10,
            },
            "loop": {"health_sweep_seconds": 0},
        }
    )

    report = WardenSupervisor(config, profile_name="tester").dry_run(now=20)

    no_progress_traces = [
        trace for trace in report["loop_traces"] if trace["loop_state"] == "no_progress"
    ]
    assert len(no_progress_traces) == 1
    assert no_progress_traces[0]["task_id"] == "worker-task"
    assert no_progress_traces[0]["matched_policy"] == "bounded_recovery"
    assert no_progress_traces[0]["decision"] == "retry"
    assert no_progress_traces[0]["verification_contract"] == {
        "success": "new_progress_event_or_status_change"
    }
