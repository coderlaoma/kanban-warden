#!/usr/bin/env python3
"""Safe operator check for kanban-warden in the Hairou environment.

The script intentionally prints only booleans, paths, counts, and policy flags. It
must not dump full profile config because profile files can contain credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_hairou_warden.py")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".hermes" / "profiles" / "hairou" / "config.yaml",
        help="Hermes profile config.yaml to inspect",
    )
    parser.add_argument(
        "--profile",
        default="hairou",
        help="profile name passed to kanban_warden CLI for status/dry-run",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="optional expected HERMES_HOME for board discovery hints",
    )
    parser.add_argument(
        "--skip-dry-run",
        action="store_true",
        help="only inspect config and command availability; do not run kanban-warden dry-run",
    )
    args = parser.parse_args(argv)

    if not args.config.exists():
        print(json.dumps({"ok": False, "error": f"config not found: {args.config}"}, indent=2))
        return 2

    data = _load_yaml(args.config)
    plugins_enabled = _plugins_enabled(data)
    warden = data.get("kanban_warden") if isinstance(data.get("kanban_warden"), dict) else {}
    enabled = _as_bool(warden.get("enabled", False))
    auto_advance = (
        warden.get("auto_advance") if isinstance(warden.get("auto_advance"), dict) else {}
    )
    notifications = (
        warden.get("notifications") if isinstance(warden.get("notifications"), dict) else {}
    )
    leader_lock = warden.get("leader_lock") if isinstance(warden.get("leader_lock"), dict) else {}

    report: dict[str, Any] = {
        "ok": True,
        "config": str(args.config),
        "profile": args.profile,
        "plugin_listed": "kanban-warden" in plugins_enabled,
        "kanban_warden_enabled": enabled,
        "boards": warden.get("boards", "*"),
        "leader_lock_enabled": _as_bool(leader_lock.get("enabled", True)),
        "notifications_enabled": _as_bool(notifications.get("enabled", False)),
        "notification_channel_count": len(notifications.get("channels", []) or []),
        "auto_advance_enabled": _as_bool(auto_advance.get("enabled", False)),
        "auto_advance_dry_run": _as_bool(auto_advance.get("dry_run", True)),
        "hermes_home_hint": str(args.hermes_home)
        if args.hermes_home
        else str(warden.get("hermes_home") or "default"),
        "expected_startup_logs": [
            "kanban-warden loaded; supervisor enabled profile=<profile>",
            "kanban-warden supervisor thread started profile=<profile>",
            "kanban-warden tick profile=<profile> boards=<n> new_events=<n> health_findings=<n> dry_run=<bool> notifications=<bool>",
        ],
        "warnings": [],
    }
    warnings = report["warnings"]
    if "kanban-warden" not in plugins_enabled:
        warnings.append("plugins.enabled does not include kanban-warden")
    if not enabled:
        warnings.append("kanban_warden.enabled is false; supervisor will not start")
    if _as_bool(auto_advance.get("enabled", False)) and not _as_bool(
        auto_advance.get("dry_run", True)
    ):
        warnings.append(
            "auto_advance can mutate Kanban boards because enabled=true and dry_run=false"
        )

    if not args.skip_dry_run:
        cli = _run(_cli_command(args.config, args.profile, "status"))
        report["status_command"] = _safe_command_summary(cli)
        dry = _run(_cli_command(args.config, args.profile, "dry-run"))
        report["dry_run_command"] = _safe_command_summary(dry)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["plugin_listed"] and enabled else 1


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise SystemExit(f"config root must be a mapping: {path}")
    return loaded


def _plugins_enabled(data: dict[str, Any]) -> list[str]:
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else {}
    enabled = plugins.get("enabled", []) if isinstance(plugins, dict) else []
    if isinstance(enabled, list):
        return [str(item) for item in enabled]
    return []


def _safe_command_summary(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"returncode": result.returncode}
    parsed = _json_loads(result.stdout)
    if isinstance(parsed, dict):
        summary["json_keys"] = sorted(str(key) for key in parsed)
        status = parsed.get("status") if isinstance(parsed.get("status"), dict) else parsed
        if isinstance(status, dict):
            summary["enabled"] = status.get("enabled")
            summary["profile"] = status.get("profile")
            leader_lock = (
                status.get("leader_lock") if isinstance(status.get("leader_lock"), dict) else {}
            )
            summary["leader_lock_active"] = (
                leader_lock.get("active") if isinstance(leader_lock, dict) else None
            )
            state = status.get("state") if isinstance(status.get("state"), dict) else {}
            summary["state_keys"] = (
                sorted(str(key) for key in state) if isinstance(state, dict) else []
            )
        dry = parsed.get("dry_run") if isinstance(parsed.get("dry_run"), dict) else None
        if isinstance(dry, dict):
            summary["dry_run_board_count"] = len(dry.get("boards", []) or [])
            summary["dry_run_health_count"] = len(dry.get("health", []) or [])
            summary["dry_run_planned_action_count"] = len(dry.get("planned_actions", []) or [])
    else:
        summary["stdout_line_count"] = len(result.stdout.splitlines())
    if result.stderr:
        summary["stderr_line_count"] = len(result.stderr.splitlines())
    return summary


def _json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _cli_command(config: Path, profile: str, command: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "kanban_warden.cli",
        "--config",
        str(config),
        "--profile",
        profile,
        command,
    ]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=60, check=False, cwd=ROOT)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
