"""Configuration model and path discovery for Kanban Warden."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class LeaderLockConfig:
    enabled: bool = True
    lease_seconds: float = 60.0
    heartbeat_seconds: float = 20.0
    db_path: str | None = None


@dataclass(frozen=True)
class LoopConfig:
    event_interval_seconds: float = 5.0
    health_sweep_seconds: float = 60.0
    once: bool = False


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = False
    channels: list[str] = field(default_factory=list)
    delivery_enabled: bool = False
    delivery_batch_size: int = 10
    delivery_max_attempts: int = 3
    delivery_backoff_seconds: float = 60.0
    delivery_lease_seconds: float = 300.0
    evidence_events: bool = True
    evidence_comments: bool = False


@dataclass(frozen=True)
class AutoAdvanceConfig:
    enabled: bool = True
    dry_run: bool = False


@dataclass(frozen=True)
class BlockedRemediationConfig:
    enabled: bool = False
    max_per_tick: int = 3
    assignee: str | None = None


@dataclass(frozen=True)
class LimitsConfig:
    max_retries: int = 2
    task_timeout_seconds: int = 14_400
    stale_claim_seconds: int = 3_600


@dataclass(frozen=True)
class TaskFilterConfig:
    active_statuses: list[str] = field(
        default_factory=lambda: [
            "triage",
            "todo",
            "scheduled",
            "ready",
            "running",
            "blocked",
            "review",
        ]
    )
    ignore_terminal_tasks: bool = False


@dataclass(frozen=True)
class CleanupConfig:
    enabled: bool = False
    archive_done: bool = False
    done_retention_days: int = 3
    purge_archived: bool = False
    archived_retention_days: int = 7
    gc_enabled: bool = True
    gc_retention_days: int = 7
    min_interval_seconds: float = 86_400.0
    state_retention_days: int = 7
    state_vacuum: bool = True


@dataclass(frozen=True)
class BoardDatabase:
    """A discovered Kanban board database and the board name to scan inside it."""

    name: str
    db_path: str


@dataclass(frozen=True)
class KanbanWardenConfig:
    enabled: bool = False
    boards: Literal["*"] | list[str] = "*"
    board_db_path: str | None = None
    leader_lock: LeaderLockConfig = field(default_factory=LeaderLockConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    auto_advance: AutoAdvanceConfig = field(default_factory=AutoAdvanceConfig)
    blocked_remediation: BlockedRemediationConfig = field(
        default_factory=BlockedRemediationConfig
    )
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    task_filter: TaskFilterConfig = field(default_factory=TaskFilterConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    log_level: str = "INFO"
    hermes_home: str | None = None
    state_db_path: str | None = None
    reviewer_assignee: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> KanbanWardenConfig:
        section: Mapping[str, Any] = {}
        if data:
            raw = data.get("kanban_warden", data)
            if isinstance(raw, Mapping):
                section = raw
        return cls(
            enabled=_as_bool(section.get("enabled", False)),
            boards=_parse_boards(section.get("boards", "*")),
            board_db_path=(str(section["board_db_path"]) if section.get("board_db_path") else None),
            leader_lock=LeaderLockConfig(**_pick(section.get("leader_lock"), LeaderLockConfig)),
            loop=LoopConfig(**_pick(section.get("loop"), LoopConfig)),
            notifications=NotificationConfig(
                **_pick(section.get("notifications"), NotificationConfig)
            ),
            auto_advance=AutoAdvanceConfig(**_pick(section.get("auto_advance"), AutoAdvanceConfig)),
            blocked_remediation=BlockedRemediationConfig(
                **_pick(section.get("blocked_remediation"), BlockedRemediationConfig)
            ),
            limits=LimitsConfig(**_pick(section.get("limits"), LimitsConfig)),
            task_filter=TaskFilterConfig(**_pick(section.get("task_filter"), TaskFilterConfig)),
            cleanup=CleanupConfig(**_pick(section.get("cleanup"), CleanupConfig)),
            log_level=str(section.get("log_level", "INFO")),
            hermes_home=str(section["hermes_home"]) if section.get("hermes_home") else None,
            state_db_path=str(section["state_db_path"]) if section.get("state_db_path") else None,
            reviewer_assignee=_optional_text(
                section.get("reviewer_assignee", _legacy_reviewer_assignee(section))
            ),
        )

    def board_names(self) -> list[str] | None:
        return None if self.boards == "*" else list(self.boards)

    def profile_home_path(self) -> Path:
        """Return the active profile home used by this Hermes process."""

        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()

    def shared_hermes_home_path(self) -> Path:
        """Return the shared Hermes home that owns multi-board Kanban state.

        Gateway profiles usually run with ``HERMES_HOME=~/.hermes/profiles/<name>``.
        Shared Kanban boards live under the root ``~/.hermes/kanban/boards`` tree,
        so derive that root unless the user configured ``kanban_warden.hermes_home``.
        """

        if self.hermes_home:
            return Path(self.hermes_home).expanduser()
        profile_home = self.profile_home_path()
        if profile_home.parent.name == "profiles":
            return profile_home.parent.parent
        return profile_home

    def resolved_state_db_path(self) -> str:
        if self.state_db_path:
            return str(Path(self.state_db_path).expanduser())
        return str(self.profile_home_path() / "state.db")


def discover_board_databases(config: KanbanWardenConfig) -> list[BoardDatabase]:
    """Discover Kanban board DBs for legacy and shared-board layouts.

    Discovery order is deterministic and conservative: explicit
    ``board_db_path``/``HERMES_KANBAN_DB`` first, then legacy ``kanban.db`` under
    the active profile and shared Hermes home, then named DBs under
    ``<shared-home>/kanban/boards/*/kanban.db``.
    """

    selected = _selected_names(config)
    candidates: list[BoardDatabase] = []
    explicit = config.board_db_path or os.environ.get("HERMES_KANBAN_DB")
    if explicit:
        candidates.append(BoardDatabase(_env_board_name(), str(Path(explicit).expanduser())))
    else:
        profile_legacy = config.profile_home_path() / "kanban.db"
        shared_home = config.shared_hermes_home_path()
        shared_legacy = shared_home / "kanban.db"
        candidates.extend(
            [
                BoardDatabase("default", str(profile_legacy)),
                BoardDatabase("default", str(shared_legacy)),
            ]
        )
        boards_dir = shared_home / "kanban" / "boards"
        if boards_dir.exists():
            for board_dir in sorted(path for path in boards_dir.iterdir() if path.is_dir()):
                candidates.append(BoardDatabase(board_dir.name, str(board_dir / "kanban.db")))

    seen: set[tuple[str, str]] = set()
    out: list[BoardDatabase] = []
    for candidate in candidates:
        path = str(Path(candidate.db_path).expanduser())
        if selected is not None and candidate.name not in selected:
            continue
        key = (candidate.name, path)
        if key in seen or not os.path.exists(path):
            continue
        seen.add(key)
        out.append(BoardDatabase(candidate.name, path))
    return out


def _pick(value: Any, model: type[Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = set(model.__dataclass_fields__)
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if key not in allowed:
            continue
        if key in {
            "enabled",
            "once",
            "dry_run",
            "delivery_enabled",
            "evidence_events",
            "evidence_comments",
            "ignore_terminal_tasks",
            "archive_done",
            "purge_archived",
            "gc_enabled",
            "state_vacuum",
        }:
            out[key] = _as_bool(raw)
        else:
            out[key] = raw
    return out


def _parse_boards(value: Any) -> Literal["*"] | list[str]:
    if value in (None, "*"):
        return "*"
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError("kanban_warden.boards must be '*' or a list of board names")


def _legacy_reviewer_assignee(section: Mapping[str, Any]) -> Any:
    auto_advance = section.get("auto_advance")
    if isinstance(auto_advance, Mapping):
        return auto_advance.get("reviewer_assignee")
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _selected_names(config: KanbanWardenConfig) -> set[str] | None:
    return None if config.boards == "*" else set(config.boards)


def _env_board_name() -> str:
    return os.environ.get("HERMES_KANBAN_BOARD") or "default"
