"""Hermes message delivery helpers for Kanban Warden notifications."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SendTarget:
    platform: str
    chat_id: str
    thread_id: str = ""

    def to_hermes_target(self) -> str:
        if not self.chat_id:
            return self.platform
        if self.thread_id:
            return f"{self.platform}:{self.chat_id}:{self.thread_id}"
        return f"{self.platform}:{self.chat_id}"


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    error: str = ""


class MessageSender(Protocol):
    def send(self, target: SendTarget, message: str) -> DeliveryResult:
        """Send one message to one Hermes target."""


class HermesMessageSender:
    """Send messages through Hermes, preferring in-process tool delivery."""

    def send(self, target: SendTarget, message: str) -> DeliveryResult:
        hermes_target = target.to_hermes_target()
        direct = self._send_in_process(hermes_target, message)
        if direct.ok:
            return direct
        fallback = self._send_with_cli(hermes_target, message)
        if fallback.ok:
            return fallback
        return DeliveryResult(
            ok=False,
            error=f"in-process send failed: {direct.error}; cli send failed: {fallback.error}",
        )

    def _send_in_process(self, target: str, message: str) -> DeliveryResult:
        try:
            from tools.send_message_tool import send_message_tool  # type: ignore[import-not-found]
        except Exception as exc:
            return DeliveryResult(
                ok=False,
                error=f"send_message_tool unavailable: {exc.__class__.__name__}",
            )
        try:
            raw = send_message_tool({"action": "send", "target": target, "message": message})
        except Exception as exc:
            return DeliveryResult(
                ok=False,
                error=f"send_message_tool raised: {exc.__class__.__name__}",
            )
        return _result_from_json(raw)

    def _send_with_cli(self, target: str, message: str) -> DeliveryResult:
        hermes = shutil.which("hermes")
        if not hermes:
            return DeliveryResult(ok=False, error="hermes executable not found")
        try:
            proc = subprocess.run(
                [hermes, "send", "--to", target, message],
                check=False,
                text=True,
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:
            return DeliveryResult(ok=False, error=f"hermes send failed: {exc.__class__.__name__}")
        if proc.returncode == 0:
            return DeliveryResult(ok=True)
        stderr = " ".join(proc.stderr.split())[:300]
        stdout = " ".join(proc.stdout.split())[:300]
        return DeliveryResult(ok=False, error=stderr or stdout or f"exit {proc.returncode}")


def target_from_subscription(row: Mapping[str, Any]) -> SendTarget:
    platform = _required_text(row.get("platform"), "subscription platform")
    chat_id = _required_text(row.get("chat_id"), "subscription chat_id")
    return SendTarget(
        platform=platform.lower(),
        chat_id=chat_id,
        thread_id=_text(row.get("thread_id")),
    )


def _result_from_json(raw: Any) -> DeliveryResult:
    if not isinstance(raw, str):
        return DeliveryResult(ok=bool(raw))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return DeliveryResult(ok=True)
    if isinstance(payload, dict) and payload.get("error"):
        return DeliveryResult(ok=False, error=str(payload["error"])[:300])
    if isinstance(payload, dict) and payload.get("success") is False:
        return DeliveryResult(ok=False, error=str(payload)[:300])
    return DeliveryResult(ok=True)


def _required_text(value: Any, label: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f"missing {label}")
    return text


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)
