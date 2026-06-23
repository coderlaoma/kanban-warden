"""Debug CLI for Kanban Warden."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from .config import KanbanWardenConfig
from .supervisor import WardenSupervisor, demo_lock_contention


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kanban-warden")
    parser.add_argument("--config", type=Path, help="YAML file containing kanban_warden config")
    parser.add_argument("--profile", default=None, help="profile name for logs/status")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print effective config and leader lock status")
    sub.add_parser("dry-run", help="run one supervisor tick without changing Kanban state")
    sub.add_parser("run-once", help="run one supervisor tick with configured policies")
    sub.add_parser("demo-lock", help="demonstrate that only one instance acquires the leader lock")
    args = parser.parse_args(argv)

    config = _load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
    )
    if args.command == "demo-lock":
        print(json.dumps(demo_lock_contention(), indent=2, sort_keys=True))
        return 0
    supervisor = WardenSupervisor(config, profile_name=args.profile)
    if args.command == "status":
        print(json.dumps(supervisor.status(), indent=2, sort_keys=True))
        return 0
    if args.command in {"dry-run", "run-once"}:
        if args.command == "dry-run":
            config = KanbanWardenConfig.from_mapping(
                {
                    "kanban_warden": {
                        **_to_dict(config),
                        "enabled": True,
                        "auto_advance": {**config.auto_advance.__dict__, "dry_run": True},
                        "loop": {**config.loop.__dict__, "once": True},
                    }
                }
            )
            supervisor = WardenSupervisor(config, profile_name=args.profile)
        if args.command == "dry-run":
            report = supervisor.dry_run()
            print(
                json.dumps(
                    {"ran": True, "dry_run": report, "status": supervisor.status()},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            ran = supervisor.tick()
            print(json.dumps({"ran": ran, "status": supervisor.status()}, indent=2, sort_keys=True))
        return 0
    parser.error(f"unknown command {args.command}")
    return 2


def _load_config(path: Path | None) -> KanbanWardenConfig:
    if path is None:
        return KanbanWardenConfig()
    with path.open("r", encoding="utf-8") as fh:
        data: Any = yaml.safe_load(fh) or {}
    return KanbanWardenConfig.from_mapping(data)


def _to_dict(config: KanbanWardenConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "boards": config.boards,
        "board_db_path": config.board_db_path,
        "leader_lock": config.leader_lock.__dict__,
        "loop": config.loop.__dict__,
        "notifications": config.notifications.__dict__,
        "auto_advance": config.auto_advance.__dict__,
        "limits": config.limits.__dict__,
        "log_level": config.log_level,
        "hermes_home": config.hermes_home,
        "state_db_path": config.state_db_path,
    }


if __name__ == "__main__":
    raise SystemExit(main())
