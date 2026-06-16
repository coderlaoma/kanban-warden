"""Kanban Warden Hermes plugin.

The plugin registers lightweight guard hooks around Kanban coordination tools and,
when enabled from profile config, starts a background supervisor loop tied to the
profile/gateway lifecycle. It never blocks writes: it appends actionable warnings
to tool results so the agent can correct leaked values before the task state
becomes durable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from .config import KanbanWardenConfig
from .supervisor import WardenSupervisor
from .warden import ScanFinding, build_warning_text, default_scanner

LOGGER = logging.getLogger(__name__)
_KANBAN_TOOLS = {"kanban_comment", "kanban_complete", "kanban_block"}
_TEXT_FIELDS = ("body", "summary", "result", "reason")
_SUPERVISOR: WardenSupervisor | None = None


def _extract_text(args: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    for field in _TEXT_FIELDS:
        value = args.get(field)
        if isinstance(value, str) and value.strip():
            chunks.append(f"{field}: {value}")
    metadata = args.get("metadata")
    if metadata is not None:
        try:
            chunks.append("metadata: " + json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        except TypeError:
            chunks.append(f"metadata: {metadata!r}")
    return "\n".join(chunks)


def _scan_tool_call(tool_name: str, args: Mapping[str, Any]) -> list[ScanFinding]:
    if tool_name not in _KANBAN_TOOLS:
        return []
    text = _extract_text(args)
    if not text:
        return []
    return default_scanner().scan(text)


def _pre_tool_call(
    tool_name: str,
    args: dict[str, Any],
    task_id: str | None = None,
    **_: Any,
) -> None:
    """Log findings before durable Kanban output is written.

    Hermes pre_tool_call hooks are observers. We log only redacted snippets here;
    the transform hook appends the user-facing warning to the result afterwards.
    """
    findings = _scan_tool_call(tool_name, args or {})
    if findings:
        LOGGER.warning(
            "kanban-warden detected %d finding(s) before %s for task %s: %s",
            len(findings),
            tool_name,
            task_id or "unknown",
            ", ".join(f.rule_id for f in findings),
        )


def _transform_tool_result(
    tool_name: str,
    args: dict[str, Any],
    result: str,
    task_id: str | None = None,
    **_: Any,
) -> str:
    """Append a warning to Kanban tool results when unsafe text is detected."""
    findings = _scan_tool_call(tool_name, args or {})
    if not findings:
        return result
    warning = build_warning_text(findings, task_id=task_id, tool_name=tool_name)
    return f"{result}\n\n{warning}" if result else warning


def register(ctx: Any) -> None:
    """Register Kanban safety hooks and optional supervisor with Hermes."""
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    _start_supervisor_if_enabled(ctx)


def unregister(_ctx: Any = None) -> None:
    """Stop the supervisor when Hermes/plugin manager supports unload hooks."""
    global _SUPERVISOR
    if _SUPERVISOR is not None:
        _SUPERVISOR.stop()
        _SUPERVISOR = None


def _start_supervisor_if_enabled(ctx: Any) -> None:
    global _SUPERVISOR
    config = KanbanWardenConfig.from_mapping(_context_config(ctx))
    if not config.enabled:
        LOGGER.info("kanban-warden loaded; supervisor disabled")
        return
    profile_name = _profile_name(ctx)
    _SUPERVISOR = WardenSupervisor(config, profile_name=profile_name)
    _SUPERVISOR.start()


def _context_config(ctx: Any) -> Mapping[str, Any]:
    for attr in ("config", "profile_config", "settings"):
        value = getattr(ctx, attr, None)
        if isinstance(value, Mapping):
            return value
    get_config = getattr(ctx, "get_config", None)
    if callable(get_config):
        value = get_config()
        if isinstance(value, Mapping):
            return value
    return {}


def _profile_name(ctx: Any) -> str | None:
    for attr in ("profile", "profile_name"):
        value = getattr(ctx, attr, None)
        if isinstance(value, str):
            return value
    return None
