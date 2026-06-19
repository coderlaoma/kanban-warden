from __future__ import annotations

import sys
import types
from pathlib import Path

import kanban_warden
from kanban_warden import _context_config, _transform_tool_result
from kanban_warden.cli import main
from kanban_warden.config import KanbanWardenConfig
from kanban_warden.lock import LeaderLock
from kanban_warden.supervisor import WardenSupervisor, demo_lock_contention
from kanban_warden.warden import build_warning_text, default_scanner

FAKE_GITHUB_TOKEN = "ghp_" + "a" * 36
FAKE_OPENAI_KEY = "sk-" + "b" * 30


def test_scanner_detects_and_redacts_secret_assignments() -> None:
    findings = default_scanner().scan(f"token = {FAKE_GITHUB_TOKEN}")

    assert findings
    assert findings[0].rule_id == "github-token"
    assert FAKE_GITHUB_TOKEN not in findings[0].snippet
    assert "[REDACTED]" in findings[0].snippet


def test_scanner_ignores_redacted_values() -> None:
    findings = default_scanner().scan("password=[REDACTED]\nurl=https://example.com/path")

    assert findings == []


def test_database_url_with_credentials_is_detected() -> None:
    findings = default_scanner().scan("postgres://user:pw@example.internal:5432/app")

    assert [finding.rule_id for finding in findings] == ["database-url"]


def test_warning_text_contains_no_raw_secret() -> None:
    findings = default_scanner().scan(f"OPENAI_API_KEY={FAKE_OPENAI_KEY}")
    warning = build_warning_text(findings, task_id="t_123", tool_name="kanban_complete")

    assert "t_123" in warning
    assert "kanban_complete" in warning
    assert FAKE_OPENAI_KEY not in warning
    assert "[REDACTED]" in warning


def test_transform_tool_result_appends_warning_for_kanban_tools() -> None:
    fake_password = "fake-long-password-value"
    result = _transform_tool_result(
        "kanban_comment",
        {"body": f"temporary password = {fake_password}"},
        "ok",
        task_id="t_123",
    )

    assert result.startswith("ok")
    assert "[kanban-warden] WARNING" in result
    assert fake_password not in result


def test_transform_tool_result_ignores_non_kanban_tools() -> None:
    result = _transform_tool_result(
        "terminal",
        {"command": f"echo GH_TOKEN={FAKE_GITHUB_TOKEN}"},
        "ok",
    )

    assert result == "ok"


def test_config_loads_required_profile_keys() -> None:
    config = KanbanWardenConfig.from_mapping(
        {
            "kanban_warden": {
                "enabled": "true",
                "boards": "*",
                "leader_lock": {"enabled": True, "lease_seconds": 45, "heartbeat_seconds": 10},
                "loop": {"event_interval_seconds": 2, "health_sweep_seconds": 30},
                "notifications": {"enabled": True, "channels": ["origin"]},
                "auto_advance": {"enabled": False, "dry_run": True},
                "limits": {
                    "max_retries": 3,
                    "task_timeout_seconds": 600,
                    "stale_claim_seconds": 120,
                },
            }
        }
    )

    assert config.enabled is True
    assert config.boards == "*"
    assert config.leader_lock.lease_seconds == 45
    assert config.loop.event_interval_seconds == 2
    assert config.notifications.channels == ["origin"]
    assert config.limits.max_retries == 3


def test_plugin_context_config_falls_back_to_hermes_profile_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    hermes_config = {
        "kanban_warden": {
            "enabled": True,
            "leader_lock": {"enabled": False},
        }
    }
    hermes_cli_module = types.ModuleType("hermes_cli")
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = lambda: hermes_config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)

    class PluginContextLike:
        def register_hook(self, _name, _handler):  # type: ignore[no-untyped-def]
            return None

    assert _context_config(PluginContextLike()) is hermes_config


def test_register_starts_supervisor_from_hermes_profile_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    hermes_config = {
        "kanban_warden": {
            "enabled": True,
            "leader_lock": {"enabled": False},
            "loop": {"once": True},
        }
    }
    hermes_cli_module = types.ModuleType("hermes_cli")
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = lambda: hermes_config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setattr(kanban_warden, "_SUPERVISOR", None)

    started = {}

    class FakeSupervisor:
        def __init__(self, config, profile_name=None):  # type: ignore[no-untyped-def]
            started["enabled"] = config.enabled
            started["leader_lock"] = config.leader_lock.enabled
            started["profile"] = profile_name

        def start(self):  # type: ignore[no-untyped-def]
            started["started"] = True
            return True

    monkeypatch.setattr(kanban_warden, "WardenSupervisor", FakeSupervisor)

    class PluginContextLike:
        def __init__(self) -> None:
            self.hooks = []
            self.profile_name = "hairou-feishu"

        def register_hook(self, name, handler):  # type: ignore[no-untyped-def]
            self.hooks.append((name, handler))

    ctx = PluginContextLike()
    kanban_warden.register(ctx)

    assert [name for name, _ in ctx.hooks] == ["pre_tool_call", "transform_tool_result"]
    assert started == {
        "enabled": True,
        "leader_lock": False,
        "profile": "hairou-feishu",
        "started": True,
    }
    monkeypatch.setattr(kanban_warden, "_SUPERVISOR", None)


def test_config_boolean_string_false_values_are_false() -> None:
    config = KanbanWardenConfig.from_mapping(
        {
            "kanban_warden": {
                "enabled": "false",
                "leader_lock": {"enabled": "off"},
                "loop": {"once": "no"},
                "auto_advance": {"enabled": "0", "dry_run": "false"},
                "notifications": {"enabled": "no"},
            }
        }
    )

    assert config.enabled is False
    assert config.leader_lock.enabled is False
    assert config.loop.once is False
    assert config.auto_advance.enabled is False
    assert config.auto_advance.dry_run is False
    assert config.notifications.enabled is False


def test_context_mapping_takes_precedence_over_hermes_loader(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx_config = {"kanban_warden": {"enabled": False}}
    hermes_config = {"kanban_warden": {"enabled": True}}
    hermes_cli_module = types.ModuleType("hermes_cli")
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = lambda: hermes_config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)

    class ContextWithConfig:
        config = ctx_config

    assert _context_config(ContextWithConfig()) is ctx_config


def test_register_does_not_start_supervisor_when_profile_config_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    hermes_cli_module = types.ModuleType("hermes_cli")
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = lambda: {"kanban_warden": {"enabled": False}}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setattr(kanban_warden, "_SUPERVISOR", None)

    class ExplodingSupervisor:
        def __init__(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("disabled config must not instantiate supervisor")

    monkeypatch.setattr(kanban_warden, "WardenSupervisor", ExplodingSupervisor)

    class PluginContextLike:
        def __init__(self) -> None:
            self.hooks = []

        def register_hook(self, name, handler):  # type: ignore[no-untyped-def]
            self.hooks.append((name, handler))

    ctx = PluginContextLike()
    kanban_warden.register(ctx)

    assert [name for name, _ in ctx.hooks] == ["pre_tool_call", "transform_tool_result"]
    assert kanban_warden._SUPERVISOR is None


def test_register_does_not_start_duplicate_supervisor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    hermes_cli_module = types.ModuleType("hermes_cli")
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = lambda: {"kanban_warden": {"enabled": True}}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)

    existing = object()
    monkeypatch.setattr(kanban_warden, "_SUPERVISOR", existing)

    class ExplodingSupervisor:
        def __init__(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("existing supervisor must be reused")

    monkeypatch.setattr(kanban_warden, "WardenSupervisor", ExplodingSupervisor)

    class PluginContextLike:
        def register_hook(self, _name, _handler):  # type: ignore[no-untyped-def]
            return None

    kanban_warden.register(PluginContextLike())

    assert kanban_warden._SUPERVISOR is existing
    monkeypatch.setattr(kanban_warden, "_SUPERVISOR", None)


def test_leader_lock_allows_only_one_active_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "leader.db"
    first = LeaderLock(db_path, owner="profile-a")
    second = LeaderLock(db_path, owner="profile-b")

    assert first.acquire(lease_seconds=60, now=1000) is True
    assert second.acquire(lease_seconds=60, now=1001) is False
    assert first.heartbeat(lease_seconds=60, now=1002) is True
    assert second.acquire(lease_seconds=60, now=1061) is False
    assert second.acquire(lease_seconds=60, now=1063) is True


def test_supervisor_dry_tick_acquires_leader_lock(tmp_path: Path) -> None:
    config = KanbanWardenConfig.from_mapping(
        {
            "enabled": True,
            "leader_lock": {"db_path": str(tmp_path / "leader.db"), "lease_seconds": 60},
            "loop": {"health_sweep_seconds": 0},
        }
    )
    supervisor = WardenSupervisor(config, profile_name="tester")

    assert supervisor.tick() is True
    status = supervisor.status()
    assert status["leader_lock"]["active"] is True
    assert status["leader_lock"]["owner"] == "tester:" + str(__import__("os").getpid())


def test_demo_lock_contention() -> None:
    result = demo_lock_contention()

    assert result["first_acquired"] is True
    assert result["second_acquired"] is False
    assert result["active_owner"] == "demo-profile-a"


def test_cli_demo_lock(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["demo-lock"]) == 0
    out = capsys.readouterr().out
    assert "first_acquired" in out
    assert "second_acquired" in out
