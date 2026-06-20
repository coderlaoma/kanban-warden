from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kanban_warden.config import KanbanWardenConfig, discover_board_databases
from kanban_warden.remediation import open_board_connection, run_deadlock_remediation


def _init_board(db_path: Path) -> None:
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
          created_by text,
          created_at integer not null,
          result text
        );
        create table task_links (parent_id text not null, child_id text not null, primary key(parent_id, child_id));
        create table task_comments (id integer primary key autoincrement, task_id text not null, author text, body text not null, created_at integer not null);
        create table task_events (id integer primary key autoincrement, task_id text not null, kind text not null, payload text, created_at integer not null);
        """
    )
    con.commit()
    con.close()


def _task(
    con: sqlite3.Connection,
    task_id: str,
    *,
    title: str,
    status: str,
    body: str = "",
    assignee: str = "mabu",
    created_at: int = 1,
    result: str = "",
) -> None:
    con.execute(
        """
        insert into tasks(id, title, body, assignee, status, created_by, created_at, result)
        values (?, ?, ?, ?, ?, 'test', ?, ?)
        """,
        (task_id, title, body, assignee, status, created_at, result),
    )


def _event(
    con: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict[str, object] | None = None,
    created_at: int = 10,
) -> None:
    con.execute(
        "insert into task_events(task_id, kind, payload, created_at) values (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload or {}), created_at),
    )


def test_dry_run_reports_recovery_deadlock_without_mutating(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_board(db_path)
    con = sqlite3.connect(db_path)
    _task(con, "source", title="Implement feature", status="blocked", body="review-required: fix it")
    _task(con, "fix", title="Fix needs-changes for source", status="todo", body="addresses source")
    con.execute("insert into task_links(parent_id, child_id) values ('source', 'fix')")
    _event(con, "fix", "claim_rejected", {"reason": "parents_not_done"})
    con.commit()
    con.close()

    with open_board_connection(str(db_path)) as conn:
        report = run_deadlock_remediation(
            conn, board="dev", now=20, dry_run=True, auto_advance=True, stale_claim_seconds=3600
        )

    assert len(report.proposals) == 1
    proposal = report.proposals[0]
    assert proposal.action_type == "unlink_erroneous_recovery_parent"
    assert proposal.status == "proposed"

    con = sqlite3.connect(db_path)
    assert con.execute("select count(*) from task_links").fetchone()[0] == 1
    assert con.execute("select name from sqlite_master where name = 'kanban_warden_action_log'").fetchone() is None


def test_auto_advance_reports_deadlock_without_mutating_board(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_board(db_path)
    con = sqlite3.connect(db_path)
    _task(con, "source", title="Implement feature", status="blocked", body="review-required: fix it")
    _task(con, "fix", title="Fix needs-changes for source", status="todo", body="addresses source")
    con.execute("insert into task_links(parent_id, child_id) values ('source', 'fix')")
    _event(con, "fix", "claim_rejected", {"reason": "parents_not_done"})
    con.commit()
    con.close()

    with open_board_connection(str(db_path)) as conn:
        first = run_deadlock_remediation(
            conn, board="dev", now=20, dry_run=False, auto_advance=True, stale_claim_seconds=3600
        )
        conn.commit()
        second = run_deadlock_remediation(
            conn, board="dev", now=21, dry_run=False, auto_advance=True, stale_claim_seconds=3600
        )
        conn.commit()

    assert [proposal.status for proposal in first.proposals] == ["proposed"]
    assert [proposal.status for proposal in second.proposals] == ["proposed"]

    con = sqlite3.connect(db_path)
    assert con.execute("select count(*) from task_links").fetchone()[0] == 1
    assert con.execute("select status from tasks where id = 'fix'").fetchone()[0] == "todo"
    assert con.execute("select name from sqlite_master where name = 'kanban_warden_action_log'").fetchone() is None
    assert con.execute("select count(*) from task_comments").fetchone()[0] == 0


def test_brand_new_fix_card_without_claim_rejection_or_staleness_is_not_unlinked(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_board(db_path)
    con = sqlite3.connect(db_path)
    _task(con, "source", title="Implement feature", status="blocked", body="review-required: fix it", created_at=1)
    _task(con, "fix", title="Fix needs-changes for source", status="todo", body="addresses source", created_at=95)
    con.execute("insert into task_links(parent_id, child_id) values ('source', 'fix')")
    con.commit()
    con.close()

    with open_board_connection(str(db_path)) as conn:
        report = run_deadlock_remediation(
            conn, board="dev", now=100, dry_run=False, auto_advance=True, stale_claim_seconds=3600
        )
        conn.commit()

    assert report.proposals == []
    con = sqlite3.connect(db_path)
    assert con.execute("select count(*) from task_links").fetchone()[0] == 1


def test_stale_todo_with_all_parents_done_is_promoted_but_parentless_todo_is_ignored(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_board(db_path)
    con = sqlite3.connect(db_path)
    _task(con, "parent", title="Parent", status="done", created_at=1)
    _task(con, "child", title="Child", status="todo", created_at=1)
    _task(con, "orphan", title="Orphan", status="todo", created_at=1)
    con.execute("insert into task_links(parent_id, child_id) values ('parent', 'child')")
    con.commit()
    con.close()

    with open_board_connection(str(db_path)) as conn:
        report = run_deadlock_remediation(
            conn, board="dev", now=4000, dry_run=False, auto_advance=True, stale_claim_seconds=3600
        )
        conn.commit()

    assert [proposal.action_type for proposal in report.proposals] == ["promote_stale_todo"]
    con = sqlite3.connect(db_path)
    assert con.execute("select status from tasks where id = 'child'").fetchone()[0] == "todo"
    assert con.execute("select status from tasks where id = 'orphan'").fetchone()[0] == "todo"


def test_shared_board_discovery_uses_root_hermes_home_from_profile(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "hairou-feishu"
    named_db = root / "kanban" / "boards" / "dev" / "kanban.db"
    _init_board(named_db)
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    config = KanbanWardenConfig.from_mapping({"boards": ["dev"]})

    assert [(board.name, board.db_path) for board in discover_board_databases(config)] == [
        ("dev", str(named_db))
    ]
