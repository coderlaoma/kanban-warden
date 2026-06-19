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


def _config(
    tmp_path: Path,
    *,
    dry_run: bool = True,
    max_retries: int = 2,
    implementer_assignee: str | None = None,
    delivery_enabled: bool = True,
    delivery_max_attempts: int = 3,
    delivery_backoff_seconds: float = 30.0,
) -> KanbanWardenConfig:
    return KanbanWardenConfig.from_mapping(
        {
            "enabled": True,
            "hermes_home": str(tmp_path / "home" / ".hermes"),
            "state_db_path": str(tmp_path / "state.db"),
            "leader_lock": {"enabled": False},
            "notifications": {
                "enabled": True,
                "channels": ["origin"],
                "delivery_enabled": delivery_enabled,
                "delivery_batch_size": 10,
                "delivery_max_attempts": delivery_max_attempts,
                "delivery_backoff_seconds": delivery_backoff_seconds,
                "evidence_events": True,
                "evidence_comments": True,
            },
            "auto_advance": {
                "enabled": True,
                "dry_run": dry_run,
                "review_required": True,
                "stale_claims": True,
                "reviewer_assignee": "reviewer",
                "implementer_assignee": implementer_assignee,
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
        expected_status = "done" if verdict == "approve" else "ready"
        assert (
            con.execute("select status from tasks where id = 'impl'").fetchone()[0]
            == expected_status
        )
        assert con.execute("select count(*) from task_comments where task_id = 'impl'").fetchone()[
            0
        ] == (2 if verdict == "approve" else 1)



def test_review_needs_changes_creates_dedicated_implementer_followup_and_backfills_subscription(
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
    _insert_real_task(con, "impl", title="Impl", status="blocked", assignee="hairou", created_at=1)
    _insert_real_task(
        con, "review_impl", title="Review", status="done", assignee="reviewer", created_at=2
    )
    con.execute("insert into task_links(parent_id, child_id) values (?, ?)", ("impl", "review_impl"))
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values (?, ?, ?, ?, ?, ?, ?, ?)",
        ("impl", "telegram", "chat-1", "thread-1", "user-1", "hairou-feishu", 3, 0),
    )
    con.commit()
    con.close()
    review_body = "NEEDS-CHANGES: add focused regression test and preserve existing flows"
    _event(
        board,
        "review_impl",
        "completed",
        {"verdict": "NEEDS-CHANGES", "source_task": "impl", "body": review_body},
        4,
    )

    supervisor = WardenSupervisor(config, profile_name="tester")
    report = supervisor.collect(now=20)
    second = supervisor.collect(now=21)

    followup_actions = [
        action for action in report["planned_actions"] if action["kind"] == "create_implementer_followup"
    ]
    assert len(followup_actions) == 1
    con = sqlite3.connect(board)
    followup = con.execute(
        "select id, title, body, status, assignee, created_by, idempotency_key from tasks where id = ?",
        ("fix_impl_review_impl",),
    ).fetchone()
    assert followup is not None
    assert followup[1] == "Fix review changes for impl"
    assert "NEEDS-CHANGES" in followup[2]
    assert review_body in followup[2]
    assert followup[3] == "ready"
    assert followup[4] == "hairou"
    assert followup[5] == "kanban-warden"
    assert followup[6] == "implementer-followup:default:review_impl:impl"
    assert con.execute(
        "select count(*) from task_links where parent_id = ? and child_id = ?",
        ("impl", "fix_impl_review_impl"),
    ).fetchone()[0] == 1
    assert con.execute(
        "select count(*) from kanban_notify_subs where task_id = ?", ("fix_impl_review_impl",)
    ).fetchone()[0] == 1
    assert con.execute(
        "select last_event_id from kanban_notify_subs where task_id = ?", ("fix_impl_review_impl",)
    ).fetchone()[0] >= 2
    assert con.execute("select count(*) from tasks where id = ?", ("fix_impl_review_impl",)).fetchone()[0] == 1
    assert not any(
        result["applied"] and result["kind"] == "create_implementer_followup"
        for result in second["action_results"]
    )
    assert any(
        row["key"] == "implementer-followup:default:review_impl:impl" and row["status"] == "done"
        for row in WardenStateStore(config.state_db_path or "").snapshot()["action_log"]
    )
    assert report["state"]["notification_outbox_count"] >= 1


def test_review_needs_changes_without_source_assignee_does_not_assign_followup_to_reviewer(
    tmp_path: Path,
) -> None:
    for case_name, source_assignee in (("blank", ""), ("missing", None)):
        config = _config(tmp_path / case_name, dry_run=False)
        board = Path(config.hermes_home or "") / "kanban.db"
        _init_real_schema_board(board)
        con = sqlite3.connect(board)
        con.execute(
            """
            insert into tasks(id, title, status, assignee, created_at, workspace_kind)
            values (?, ?, ?, ?, ?, ?)
            """,
            ("impl", "Impl", "blocked", source_assignee, 1, "scratch"),
        )
        _insert_real_task(
            con, "review_impl", title="Review", status="done", assignee="reviewer", created_at=2
        )
        con.execute(
            "insert into task_links(parent_id, child_id) values (?, ?)", ("impl", "review_impl")
        )
        con.commit()
        con.close()
        _event(
            board,
            "review_impl",
            "completed",
            {"verdict": "NEEDS-CHANGES", "source_task": "impl", "body": "fix it"},
            4,
        )

        report = WardenSupervisor(config, profile_name="tester").collect(now=20)

        con = sqlite3.connect(board)
        assert (
            con.execute("select count(*) from tasks where id = ?", ("fix_impl_review_impl",)).fetchone()[0]
            == 0
        )
        assert not con.execute(
            "select 1 from tasks where id = ? and assignee = ?",
            ("fix_impl_review_impl", "reviewer"),
        ).fetchone()
        comments = [
            row[0] for row in con.execute("select body from task_comments where task_id = ?", ("impl",))
        ]
        assert any("missing implementer assignee" in body for body in comments)
        assert any(
            result["kind"] == "create_implementer_followup"
            and result["note"] == "missing-implementer-assignee"
            for result in report["action_results"]
        )



def test_review_needs_changes_uses_configured_implementer_fallback_not_reviewer(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, dry_run=False, implementer_assignee="hairou")
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        """
        insert into tasks(id, title, status, assignee, created_at, workspace_kind)
        values (?, ?, ?, ?, ?, ?)
        """,
        ("impl", "Impl", "blocked", None, 1, "scratch"),
    )
    _insert_real_task(
        con, "review_impl", title="Review", status="done", assignee="reviewer", created_at=2
    )
    con.execute("insert into task_links(parent_id, child_id) values (?, ?)", ("impl", "review_impl"))
    con.commit()
    con.close()
    _event(
        board,
        "review_impl",
        "completed",
        {"verdict": "NEEDS-CHANGES", "source_task": "impl", "body": "fix it"},
        4,
    )

    WardenSupervisor(config, profile_name="tester").collect(now=20)

    con = sqlite3.connect(board)
    followup = con.execute(
        "select assignee, status from tasks where id = ?", ("fix_impl_review_impl",)
    ).fetchone()
    assert followup == ("hairou", "ready")


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


def _insert_real_task(
    con: sqlite3.Connection,
    task_id: str,
    *,
    title: str,
    status: str,
    assignee: str = "hairou",
    created_at: int = 1,
) -> None:
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values (?, ?, ?, ?, ?, ?)",
        (task_id, title, status, assignee, created_at, "scratch"),
    )


def test_review_approve_finalizes_source_when_review_child_is_done(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    _insert_real_task(con, "impl", title="Impl", status="blocked", created_at=1)
    _insert_real_task(
        con, "review_impl", title="Review", status="done", assignee="reviewer", created_at=2
    )
    con.execute(
        "insert into task_links(parent_id, child_id) values (?, ?)", ("impl", "review_impl")
    )
    con.commit()
    con.close()
    _event(board, "review_impl", "completed", {"verdict": "APPROVE", "source_task": "impl"}, 3)

    report = WardenSupervisor(config, profile_name="tester").collect(now=20)

    assert any(action["kind"] == "finalize" for action in report["planned_actions"])
    con = sqlite3.connect(board)
    assert con.execute("select status from tasks where id = ?", ("impl",)).fetchone()[0] == "done"
    assert (
        con.execute(
            "select count(*) from task_events where task_id = ? and kind = ?", ("impl", "completed")
        ).fetchone()[0]
        == 1
    )


def test_blocker_done_promotes_blocked_downstream_and_root_all_children_done_finalizes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    _insert_real_task(con, "blocker", title="Blocker", status="done", created_at=1)
    _insert_real_task(con, "downstream", title="Downstream", status="blocked", created_at=2)
    _insert_real_task(con, "root", title="Root", status="blocked", assignee="planner", created_at=3)
    _insert_real_task(con, "child", title="Child", status="done", created_at=4)
    con.execute(
        "insert into task_links(parent_id, child_id) values (?, ?)", ("blocker", "downstream")
    )
    con.execute("insert into task_links(parent_id, child_id) values (?, ?)", ("root", "child"))
    con.commit()
    con.close()
    _event(board, "blocker", "completed", {"summary": "fixed blocker"}, 5)

    report = WardenSupervisor(config, profile_name="tester").collect(now=20)

    assert any(
        action["kind"] == "promote" and action["task_id"] == "downstream"
        for action in report["planned_actions"]
    )
    assert any(
        action["kind"] == "finalize" and action["task_id"] == "root"
        for action in report["planned_actions"]
    )
    con = sqlite3.connect(board)
    assert (
        con.execute("select status from tasks where id = ?", ("downstream",)).fetchone()[0]
        == "ready"
    )
    assert con.execute("select status from tasks where id = ?", ("root",)).fetchone()[0] == "done"


def test_key_comment_markers_are_notificationized_from_comment_events(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    _insert_real_task(con, "impl", title="Impl", status="done", created_at=1)
    con.execute(
        "insert into task_comments(task_id, author, body, created_at) values (?, ?, ?, ?)",
        ("impl", "reviewer", "NEEDS-CHANGES: add focused tests only", 2),
    )
    con.commit()
    con.close()
    _event(
        board,
        "impl",
        "commented",
        {"author": "reviewer", "body": "NEEDS-CHANGES: add focused tests only"},
        3,
    )

    report = WardenSupervisor(config, profile_name="tester").collect(now=20)

    assert any(action["kind"] == "notify" for action in report["planned_actions"])
    assert report["state"]["notification_outbox_count"] >= 1


def test_stale_running_health_also_repairs_subscription_and_queues_notification(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, dry_run=False, max_retries=1)
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
    _insert_real_task(con, "root", title="Root", status="running", assignee="planner", created_at=1)
    _insert_real_task(con, "stale", title="Stale", status="running", created_at=2)
    con.execute("insert into task_links(parent_id, child_id) values (?, ?)", ("root", "stale"))
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values (?, ?, ?, ?, ?, ?, ?, ?)",
        ("root", "telegram", "chat-1", "thread-1", "user-1", "hairou-feishu", 3, 0),
    )
    con.commit()
    con.close()

    report = WardenSupervisor(config, profile_name="tester").collect(now=20)

    assert any(
        action["kind"] == "ensure_subscription" and action["task_id"] == "stale"
        for action in report["planned_actions"]
    )
    assert any(
        action["kind"] == "retry" and action["task_id"] == "stale"
        for action in report["planned_actions"]
    )
    assert report["state"]["notification_outbox_count"] >= 1
    con = sqlite3.connect(board)
    assert (
        con.execute(
            "select count(*) from kanban_notify_subs where task_id = ?", ("stale",)
        ).fetchone()[0]
        == 1
    )



def test_notification_outbox_stale_in_progress_rows_are_reclaimed(tmp_path: Path) -> None:
    store = WardenStateStore(str(tmp_path / "state.db"))
    assert store.enqueue_notification(
        "stale-key",
        {"board_name": "default", "target_task_id": "impl", "kind": "review_required"},
    )

    first = store.claim_notification_batch(limit=1, now=20)
    assert [row["key"] for row in first] == ["stale-key"]
    con = sqlite3.connect(tmp_path / "state.db")
    assert con.execute(
        "select status, attempts, next_attempt_at from notification_outbox where key = ?",
        ("stale-key",),
    ).fetchone() == ("in_progress", 0, 320.0)
    assert store.claim_notification_batch(limit=1, now=319) == []
    con.execute(
        "update notification_outbox set next_attempt_at = 0 where key = ?",
        ("stale-key",),
    )
    con.commit()

    reclaimed = store.claim_notification_batch(limit=1, now=10_000)

    assert [row["key"] for row in reclaimed] == ["stale-key"]
    assert reclaimed[0]["attempts"] == 0
    store.mark_notification_delivered("stale-key", now=10_001)
    assert con.execute(
        "select status, attempts, last_error, next_attempt_at from notification_outbox where key = ?",
        ("stale-key",),
    ).fetchone() == ("delivered", 1, None, None)


def test_notification_outbox_drain_delivers_to_native_subscriber_evidence(
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
    _insert_real_task(con, "impl", title="Impl", status="blocked", created_at=1)
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values (?, ?, ?, ?, ?, ?, ?, ?)",
        ("impl", "feishu", "chat-1", "", "user-1", "hairou-feishu", 2, 0),
    )
    con.commit()
    con.close()
    _event(board, "impl", "completed", {"summary": "worker finished"}, 3)

    report = WardenSupervisor(config, profile_name="tester").collect(now=20)

    assert report["outbox_delivery"]["delivered"] >= 1
    store_con = sqlite3.connect(config.state_db_path or "")
    outbox = store_con.execute(
        "select status, attempts, last_error from notification_outbox order by key"
    ).fetchall()
    assert outbox
    assert {row[0] for row in outbox} == {"delivered"}
    assert all(row[1] == 1 for row in outbox)
    assert all(row[2] is None for row in outbox)
    board_con = sqlite3.connect(board)
    evidence_events = board_con.execute(
        "select payload from task_events where task_id = ? and kind = ?",
        ("impl", "commented"),
    ).fetchall()
    assert len(evidence_events) >= 1
    assert any("warden-notification-delivered" in row[0] for row in evidence_events)
    comments = board_con.execute(
        "select author, body from task_comments where task_id = ? order by id", ("impl",)
    ).fetchall()
    assert any(row[0] == "kanban-warden" and "warden-notification" in row[1] for row in comments)


def test_notification_outbox_no_subscriber_retries_with_backoff_then_exhausts(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        dry_run=False,
        delivery_max_attempts=2,
        delivery_backoff_seconds=10.0,
    )
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
    _insert_real_task(con, "impl", title="Impl", status="done", created_at=1)
    con.commit()
    con.close()
    _event(board, "impl", "completed", {"summary": "worker finished"}, 3)
    supervisor = WardenSupervisor(config, profile_name="tester")

    first = supervisor.collect(now=20)
    second = supervisor.collect(now=21)
    third = supervisor.collect(now=31)

    assert first["outbox_delivery"]["retrying"] >= 1
    assert second["outbox_delivery"]["processed"] == 0
    assert third["outbox_delivery"]["exhausted"] >= 1
    store_con = sqlite3.connect(config.state_db_path or "")
    rows = store_con.execute(
        "select status, attempts, last_error from notification_outbox order by key"
    ).fetchall()
    assert rows
    assert {row[0] for row in rows} == {"exhausted"}
    assert all(row[1] == 2 for row in rows)
    assert all("no native kanban subscriber" in row[2] for row in rows)
    board_con = sqlite3.connect(board)
    assert (
        board_con.execute("select count(*) from task_events where kind = 'commented'").fetchone()[
            0
        ]
        == 0
    )


def test_notification_outbox_delivered_rows_are_not_redelivered(tmp_path: Path) -> None:
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
    _insert_real_task(con, "impl", title="Impl", status="done", created_at=1)
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('impl', 'feishu', 'chat-1', '', 'user-1', 'hairou-feishu', 2, 0)"
    )
    con.commit()
    con.close()
    _event(board, "impl", "completed", {"summary": "worker finished"}, 3)
    supervisor = WardenSupervisor(config, profile_name="tester")

    supervisor.collect(now=20)
    delivered_count = sqlite3.connect(board).execute(
        "select count(*) from task_events where task_id = 'impl' and kind = 'commented'"
    ).fetchone()[0]
    report = supervisor.collect(now=40)

    assert report["outbox_delivery"]["processed"] == 0
    assert (
        sqlite3.connect(board)
        .execute("select count(*) from task_events where task_id = 'impl' and kind = 'commented'")
        .fetchone()[0]
        == delivered_count
    )


def test_notification_outbox_dry_run_does_not_deliver(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=True)
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
    _insert_real_task(con, "impl", title="Impl", status="blocked", created_at=1)
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('impl', 'feishu', 'chat-1', '', 'user-1', 'hairou-feishu', 2, 0)"
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: check diff"}, 3)

    report = WardenSupervisor(config, profile_name="tester").dry_run(now=20)

    assert report["outbox_delivery"]["dry_run"] is True
    assert report["outbox_delivery"]["processed"] == 0
    assert (
        sqlite3.connect(config.state_db_path or "")
        .execute("select count(*) from notification_outbox")
        .fetchone()[0]
        == 0
    )
    assert (
        sqlite3.connect(board)
        .execute("select count(*) from task_events where kind = 'commented'")
        .fetchone()[0]
        == 0
    )
