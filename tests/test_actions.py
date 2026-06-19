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
          created_at real,
          started_at real,
          completed_at real,
          current_run_id integer
        );
        create table task_events (
          id integer primary key autoincrement,
          task_id text,
          kind text not null,
          payload text,
          created_at real,
          run_id integer
        );
        create table task_links (parent_id text not null, child_id text not null);
        create table task_comments (id integer primary key autoincrement, task_id text, body text, created_at real);
        create table runs (id integer primary key, task_id text, profile text, status text, started_at real, ended_at real);
        """
    )
    con.commit()
    con.close()


def _init_real_schema_board(db_path: Path) -> None:
    """Create the subset of the current Hermes Kanban schema that actions mutate."""
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


def _config(tmp_path: Path, *, dry_run: bool = True, max_retries: int = 2) -> KanbanWardenConfig:
    return KanbanWardenConfig.from_mapping(
        {
            "enabled": True,
            "hermes_home": str(tmp_path / "home" / ".hermes"),
            "state_db_path": str(tmp_path / "state.db"),
            "leader_lock": {"enabled": False},
            "notifications": {"enabled": True, "channels": ["origin"]},
            "auto_advance": {
                "enabled": True,
                "dry_run": dry_run,
                "review_required": True,
                "stale_claims": True,
                "reviewer_assignee": "reviewer",
            },
            "limits": {
                "max_retries": max_retries,
                "stale_claim_seconds": 5,
                "task_timeout_seconds": 10,
            },
            "loop": {"health_sweep_seconds": 0},
        }
    )


def test_review_required_dry_run_plans_notification_and_reviewer_without_mutating_board(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, dry_run=True)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at) values ('impl', 'Impl', 'blocked', 'hairou', 1)"
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: check diff"}, 2)

    report = WardenSupervisor(config, profile_name="tester").dry_run(now=20)

    kinds = [action["kind"] for action in report["planned_actions"]]
    assert "notify" in kinds
    assert "create_reviewer" in kinds
    assert all(
        result["applied"] is False and result["note"] == "dry-run"
        for result in report["action_results"]
    )
    con = sqlite3.connect(board)
    assert con.execute("select count(*) from tasks where id = 'review_impl'").fetchone()[0] == 0


def test_review_required_apply_creates_one_reviewer_with_idempotency(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at) values ('impl', 'Impl', 'blocked', 'hairou', 1)"
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: check diff"}, 2)
    supervisor = WardenSupervisor(config, profile_name="tester")

    first = supervisor.collect(now=20)
    second = supervisor.collect(now=21)

    assert any(
        result["applied"] and result["kind"] == "create_reviewer"
        for result in first["action_results"]
    )
    con = sqlite3.connect(board)
    assert con.execute("select count(*) from tasks where id = 'review_impl'").fetchone()[0] == 1
    assert (
        con.execute("select assignee from tasks where id = 'review_impl'").fetchone()[0]
        == "reviewer"
    )
    assert not any(
        result["applied"] and result["kind"] == "create_reviewer"
        for result in second["action_results"]
    )


def test_review_approve_and_needs_changes_comment_and_unblock_source_once(tmp_path: Path) -> None:
    for verdict in ("approve", "needs-changes"):
        config = _config(tmp_path / verdict, dry_run=False)
        board = Path(config.hermes_home or "") / "kanban.db"
        _init_board(board)
        con = sqlite3.connect(board)
        con.execute(
            "insert into tasks(id, title, status, assignee, created_at) values ('impl', 'Impl', 'blocked', 'hairou', 1)"
        )
        con.execute(
            "insert into tasks(id, title, status, assignee, created_at) values ('review_impl', 'Review', 'done', 'reviewer', 2)"
        )
        con.execute("insert into task_links(parent_id, child_id) values ('impl', 'review_impl')")
        con.commit()
        con.close()
        _event(board, "review_impl", "completed", {"verdict": verdict, "source_task": "impl"}, 3)

        WardenSupervisor(config, profile_name="tester").collect(now=20)
        WardenSupervisor(config, profile_name="tester").collect(now=21)

        con = sqlite3.connect(board)
        assert con.execute("select status from tasks where id = 'impl'").fetchone()[0] == "ready"
        assert (
            con.execute("select count(*) from task_comments where task_id = 'impl'").fetchone()[0]
            == 1
        )


def test_stale_running_retry_budget_escalates_after_retries(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False, max_retries=1)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, started_at) values ('stale', 'Stale', 'running', 'hairou', 1, 1)"
    )
    con.commit()
    con.close()
    supervisor = WardenSupervisor(config, profile_name="tester")

    first = supervisor.collect(now=20)
    second = supervisor.collect(now=21)

    assert any(action["kind"] == "retry" for action in first["planned_actions"])
    assert any(action["kind"] == "escalate" for action in second["planned_actions"])
    assert (
        WardenStateStore(config.state_db_path or "").peek_retry("default", "stale", "stale-running")
        == 1
    )


def test_real_schema_create_reviewer_populates_required_task_columns(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('impl', 'Impl', 'blocked', 'hairou', 1, 'scratch')"
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: check diff"}, 2)

    WardenSupervisor(config, profile_name="tester").collect(now=20)

    con = sqlite3.connect(board)
    row = con.execute(
        "select assignee, status, workspace_kind, created_by, idempotency_key from tasks where id = 'review_impl'"
    ).fetchone()
    assert row == ("reviewer", "ready", "scratch", "kanban-warden", "reviewer:default:impl")


def test_real_schema_comment_paths_populate_required_author_column(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False, max_retries=0)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('impl', 'Impl', 'blocked', 'hairou', 1, 'scratch')"
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('review_impl', 'Review', 'done', 'reviewer', 2, 'scratch')"
    )
    con.execute("insert into task_links(parent_id, child_id) values ('impl', 'review_impl')")
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, started_at, workspace_kind) values ('stale', 'Stale', 'running', 'hairou', 1, 1, 'scratch')"
    )
    con.commit()
    con.close()
    _event(
        board, "review_impl", "completed", {"verdict": "needs-changes", "source_task": "impl"}, 3
    )

    supervisor = WardenSupervisor(config, profile_name="tester")
    supervisor.collect(now=20)
    supervisor.collect(now=21)

    con = sqlite3.connect(board)
    comments = con.execute("select task_id, author, body from task_comments order by id").fetchall()
    assert {row[0] for row in comments} == {"impl", "stale"}
    assert {row[1] for row in comments} == {"kanban-warden"}
    assert any("warden-review-needs-changes" in row[2] for row in comments)
    assert any("retry budget exhausted" in row[2] for row in comments)


def test_blocked_child_event_ensures_root_and_child_subscriptions_idempotently(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.executescript(
        """
        create table kanban_notify_subs (
          task_id text not null,
          platform text not null,
          chat_id text not null,
          thread_id text not null default '',
          user_id text,
          notifier_profile text,
          created_at integer not null,
          last_event_id integer not null default 0,
          primary key (task_id, platform, chat_id, thread_id)
        );
        """
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('root', 'Root', 'running', 'planner', 1, 'scratch')"
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('child', 'Child', 'blocked', 'hairou', 2, 'scratch')"
    )
    con.execute("insert into task_links(parent_id, child_id) values ('root', 'child')")
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('root', 'weixin', 'chat-1', '', 'user-1', 'default', 3, 7)"
    )
    con.execute(
        "insert into task_events(task_id, kind, payload, created_at, run_id) values ('root', 'created', '{}', 1, null)"
    )
    con.execute(
        "insert into task_events(task_id, kind, payload, created_at, run_id) values ('child', 'created', '{}', 2, null)"
    )
    con.execute(
        "insert into task_events(task_id, kind, payload, created_at, run_id) values ('child', 'claimed', '{}', 3, null)"
    )
    con.commit()
    con.close()
    _event(board, "child", "blocked", {"reason": "worker-failure: gave_up"}, 4)

    supervisor = WardenSupervisor(config, profile_name="tester")
    first = supervisor.collect(now=20)
    supervisor.collect(now=21)

    assert any(
        result["applied"] and result["kind"] == "ensure_subscription"
        for result in first["action_results"]
    )
    con = sqlite3.connect(board)
    rows = con.execute(
        "select task_id, platform, chat_id, thread_id, user_id, notifier_profile, last_event_id from kanban_notify_subs order by task_id"
    ).fetchall()
    assert rows == [
        ("child", "weixin", "chat-1", "", "user-1", "default", 3),
        ("root", "weixin", "chat-1", "", "user-1", "default", 7),
    ]
    assert con.execute("select count(*) from kanban_notify_subs").fetchone()[0] == 2
    payload = con.execute(
        "select payload from task_events where task_id = 'child' and kind = 'commented'"
    ).fetchone()[0]
    assert "ensured root/stuck-task notify subscriptions" in payload


def test_dependency_deadlock_health_surfaces_child_and_ensures_subscription(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=True)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('parent', 'Parent', 'blocked', 'planner', 1, 'scratch')"
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('child', 'Child', 'todo', 'hairou', 2, 'scratch')"
    )
    con.execute("insert into task_links(parent_id, child_id) values ('parent', 'child')")
    con.commit()
    con.close()

    report = WardenSupervisor(config, profile_name="tester").dry_run(now=20)

    assert any(
        finding["kind"] == "dependency_blocked_by_stuck_parent"
        and finding["task_id"] == "child"
        and finding["parent_id"] == "parent"
        for finding in report["health"]
    )
    assert any(
        action["kind"] == "ensure_subscription" and action["task_id"] == "child"
        for action in report["planned_actions"]
    )


def test_ensure_subscription_cursor_exposes_only_current_stuck_event(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.executescript(
        """
        create table kanban_notify_subs (
          task_id text not null,
          platform text not null,
          chat_id text not null,
          thread_id text not null default '',
          user_id text,
          notifier_profile text,
          created_at integer not null,
          last_event_id integer not null default 0,
          primary key (task_id, platform, chat_id, thread_id)
        );
        """
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('root', 'Root', 'running', 'planner', 1, 'scratch')"
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('child', 'Child', 'blocked', 'hairou', 2, 'scratch')"
    )
    con.execute("insert into task_links(parent_id, child_id) values ('root', 'child')")
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('root', 'weixin', 'chat-1', '', 'user-1', 'default', 3, 0)"
    )
    con.commit()
    con.close()
    _event(board, "child", "created", {}, 4)
    _event(board, "child", "heartbeat", {}, 5)
    _event(board, "child", "blocked", {"reason": "current worker-failure: gave_up"}, 6)

    first = WardenSupervisor(config, profile_name="tester").collect(now=20)

    assert any(
        result["applied"] and result["kind"] == "ensure_subscription"
        for result in first["action_results"]
    )
    con = sqlite3.connect(board)
    current_event_id = con.execute(
        "select max(id) from task_events where task_id = 'child' and kind in ('blocked', 'gave_up')"
    ).fetchone()[0]
    child_cursor = con.execute(
        "select last_event_id from kanban_notify_subs where task_id = 'child'"
    ).fetchone()[0]
    assert child_cursor == current_event_id - 1


def test_ensure_subscription_no_source_health_finding_is_retryable_when_source_appears(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.executescript(
        """
        create table kanban_notify_subs (
          task_id text not null,
          platform text not null,
          chat_id text not null,
          thread_id text not null default '',
          user_id text,
          notifier_profile text,
          created_at integer not null,
          last_event_id integer not null default 0,
          primary key (task_id, platform, chat_id, thread_id)
        );
        """
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, started_at, workspace_kind) values ('root', 'Root', 'running', 'planner', 1, 1, 'scratch')"
    )
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, started_at, workspace_kind) values ('child', 'Child', 'blocked', 'hairou', 2, 2, 'scratch')"
    )
    con.execute("insert into task_links(parent_id, child_id) values ('root', 'child')")
    con.commit()
    con.close()

    supervisor = WardenSupervisor(config, profile_name="tester")
    first = supervisor.collect(now=20)
    assert any(
        result["kind"] == "ensure_subscription"
        and result["note"] == "no-related-subscription-source"
        for result in first["action_results"]
    )

    con = sqlite3.connect(board)
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('root', 'weixin', 'chat-1', '', 'user-1', 'default', 30, 0)"
    )
    con.commit()
    con.close()

    second = supervisor.collect(now=31)
    assert any(
        result["applied"] and result["kind"] == "ensure_subscription"
        for result in second["action_results"]
    )
    con = sqlite3.connect(board)
    assert (
        con.execute("select count(*) from kanban_notify_subs where task_id = 'child'").fetchone()[0]
        == 1
    )
