from __future__ import annotations

import yaml

from kanban_warden.config import KanbanWardenConfig


def test_default_config_runs_in_active_non_dry_run_mode() -> None:
    config = KanbanWardenConfig.from_mapping({"kanban_warden": {}})

    assert config.auto_advance.enabled is True
    assert config.auto_advance.dry_run is False
    assert config.blocked_remediation.enabled is False
    assert config.blocked_remediation.max_per_tick == 3
    assert config.blocked_remediation.assignee is None
    assert config.reviewer_assignee is None


def test_example_config_keeps_only_common_operator_settings() -> None:
    with open("examples/config.yaml", encoding="utf-8") as fh:
        raw = fh.read()
    config = yaml.safe_load(raw)
    warden = config["kanban_warden"]

    assert warden["enabled"] is True
    assert warden["boards"] == "*"
    assert warden["notifications"] == {"enabled": True, "channels": ["origin"]}
    assert warden["auto_advance"] == {"enabled": True, "dry_run": False}
    assert warden["blocked_remediation"] == {"enabled": True, "max_per_tick": 3}
    assert warden["reviewer_assignee"] is None
    assert set(warden["limits"]) == {
        "max_retries",
        "task_timeout_seconds",
        "stale_claim_seconds",
    }
    assert "leader_lock" not in warden
    assert "loop" not in warden
    assert "task_filter" not in warden
    assert "cleanup" not in warden
    assert "review_required" not in raw
    assert "stale_claims" not in raw


def test_blocked_remediation_config_parses_minimal_settings() -> None:
    config = KanbanWardenConfig.from_mapping(
        {
            "kanban_warden": {
                "blocked_remediation": {
                    "enabled": "true",
                    "max_per_tick": 2,
                }
            }
        }
    )

    assert config.blocked_remediation.enabled is True
    assert config.blocked_remediation.max_per_tick == 2
    assert config.blocked_remediation.assignee is None


def test_reviewer_assignee_is_top_level_and_legacy_path_is_still_read() -> None:
    config = KanbanWardenConfig.from_mapping(
        {"kanban_warden": {"reviewer_assignee": "review-team"}}
    )
    legacy = KanbanWardenConfig.from_mapping(
        {"kanban_warden": {"auto_advance": {"reviewer_assignee": "legacy-reviewer"}}}
    )

    assert config.reviewer_assignee == "review-team"
    assert legacy.reviewer_assignee == "legacy-reviewer"


def test_task_filter_and_cleanup_config_parse_from_mapping() -> None:
    config = KanbanWardenConfig.from_mapping(
        {
            "kanban_warden": {
                "task_filter": {
                    "ignore_terminal_tasks": "true",
                    "active_statuses": ["todo", "blocked"],
                },
                "cleanup": {
                    "enabled": "true",
                    "archive_done": "true",
                    "done_retention_days": 3,
                    "purge_archived": "true",
                    "archived_retention_days": 15,
                    "gc_enabled": "true",
                    "gc_retention_days": 7,
                    "min_interval_seconds": 120,
                    "state_retention_days": 7,
                    "state_vacuum": "false",
                },
            }
        }
    )

    assert config.task_filter.ignore_terminal_tasks is True
    assert config.task_filter.active_statuses == ["todo", "blocked"]
    assert config.cleanup.enabled is True
    assert config.cleanup.archive_done is True
    assert config.cleanup.done_retention_days == 3
    assert config.cleanup.purge_archived is True
    assert config.cleanup.archived_retention_days == 15
    assert config.cleanup.gc_enabled is True
    assert config.cleanup.gc_retention_days == 7
    assert config.cleanup.min_interval_seconds == 120
    assert config.cleanup.state_retention_days == 7
    assert config.cleanup.state_vacuum is False
