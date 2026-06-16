"""Configuration model for Kanban Warden."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    review_required: bool = True
    stale_tasks: bool = True
    crash_alerts: bool = True


@dataclass(frozen=True)
class AutoAdvanceConfig:
    enabled: bool = False
    dry_run: bool = True
    review_required: bool = False
    stale_claims: bool = False


@dataclass(frozen=True)
class LimitsConfig:
    max_retries: int = 2
    task_timeout_seconds: int = 14_400
    stale_claim_seconds: int = 3_600


@dataclass(frozen=True)
class KanbanWardenConfig:
    enabled: bool = False
    boards: Literal["*"] | list[str] = "*"
    leader_lock: LeaderLockConfig = field(default_factory=LeaderLockConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    auto_advance: AutoAdvanceConfig = field(default_factory=AutoAdvanceConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    log_level: str = "INFO"

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
            leader_lock=LeaderLockConfig(**_pick(section.get("leader_lock"), LeaderLockConfig)),
            loop=LoopConfig(**_pick(section.get("loop"), LoopConfig)),
            notifications=NotificationConfig(
                **_pick(section.get("notifications"), NotificationConfig)
            ),
            auto_advance=AutoAdvanceConfig(**_pick(section.get("auto_advance"), AutoAdvanceConfig)),
            limits=LimitsConfig(**_pick(section.get("limits"), LimitsConfig)),
            log_level=str(section.get("log_level", "INFO")),
        )

    def board_names(self) -> list[str] | None:
        return None if self.boards == "*" else list(self.boards)


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
            "review_required",
            "stale_claims",
            "stale_tasks",
            "crash_alerts",
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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)
