"""Whitelisted YAML config patch planning and application."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ConfigPatchError(ValueError):
    """Raised when a config patch is unsafe or cannot be applied."""


_WHITELISTED_PREFIXES = (
    "kanban_warden.limits.",
    "kanban_warden.notifications.",
    "kanban_warden.auto_advance.",
    "kanban_warden.task_filter.",
    "kanban_warden.cleanup.",
)


def prepare_config_patch(
    *, proposal: dict[str, Any], config_path: str | Path
) -> dict[str, Any]:
    path = Path(config_path)
    config = _load_yaml(path)
    patch = _patch_from_proposal(proposal)
    changes = []
    for dotted_path, after in patch.items():
        _ensure_whitelisted(dotted_path)
        before = _get_path(config, dotted_path)
        changes.append({"path": dotted_path, "before": before, "after": after})
    return {
        "proposal_id": proposal["proposal_id"],
        "target_file": str(path),
        "changes": changes,
    }


def apply_config_patch(
    *, proposal: dict[str, Any], config_path: str | Path, created_at: float | None = None
) -> dict[str, Any]:
    path = Path(config_path)
    prepared = prepare_config_patch(proposal=proposal, config_path=path)
    config = _load_yaml(path)
    backup_path = _backup_path(path, created_at=created_at)
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    for change in prepared["changes"]:
        _set_path(config, str(change["path"]), change["after"])
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return {**prepared, "backup_path": str(backup_path)}


def compare_config_patch(*, proposal: dict[str, Any], config_path: str | Path) -> dict[str, Any]:
    prepared = prepare_config_patch(proposal=proposal, config_path=config_path)
    before = {str(change["path"]): change["before"] for change in prepared["changes"]}
    after = {str(change["path"]): change["after"] for change in prepared["changes"]}
    return {
        "proposal_id": proposal["proposal_id"],
        "before": before,
        "after": after,
        "changed_policies": list(after),
        "requires_stricter_approval": False,
    }


def rollback_config_patch(
    *, proposal: dict[str, Any], config_path: str | Path, created_at: float | None = None
) -> dict[str, Any]:
    path = Path(config_path)
    config = _load_yaml(path)
    patch = _patch_from_proposal(proposal)
    rollback_value = _coerce_rollback_value(proposal["rollback_value"])
    changes = []
    backup_path = _backup_path(path, created_at=created_at)
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    for dotted_path in patch:
        _ensure_whitelisted(dotted_path)
        before = _get_path(config, dotted_path)
        _set_path(config, dotted_path, rollback_value)
        changes.append({"path": dotted_path, "before": before, "after": rollback_value})
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return {
        "proposal_id": proposal["proposal_id"],
        "target_file": str(path),
        "backup_path": str(backup_path),
        "changes": changes,
    }


def _patch_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    patch = proposal.get("patch")
    if not isinstance(patch, dict) or not patch:
        raise ConfigPatchError("config-change proposal must include a non-empty patch")
    return {str(key): value for key, value in patch.items()}


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigPatchError("config file must contain a YAML mapping")
    return raw


def _ensure_whitelisted(dotted_path: str) -> None:
    if not dotted_path.startswith(_WHITELISTED_PREFIXES):
        raise ConfigPatchError(f"config path is not whitelisted: {dotted_path}")


def _get_path(config: dict[str, Any], dotted_path: str) -> Any:
    current: Any = config
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(config: dict[str, Any], dotted_path: str, value: Any) -> None:
    current = config
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _backup_path(path: Path, *, created_at: float | None) -> Path:
    suffix = "manual" if created_at is None else str(int(created_at * 1000))
    return path.with_name(f"{path.name}.{suffix}.bak")


def _coerce_rollback_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return yaml.safe_load(value)
