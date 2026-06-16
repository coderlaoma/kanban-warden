"""Background supervisor loop for the Kanban Warden Hermes plugin."""

from __future__ import annotations

import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import KanbanWardenConfig
from .lock import LeaderLock

LOGGER = logging.getLogger(__name__)


class WardenSupervisor:
    """Runs a lightweight non-cron background loop tied to plugin lifecycle."""

    def __init__(
        self,
        config: KanbanWardenConfig,
        *,
        profile_name: str | None = None,
        lock: LeaderLock | None = None,
    ) -> None:
        self.config = config
        self.profile_name = profile_name or os.environ.get("HERMES_PROFILE", "default")
        self.lock = lock or LeaderLock(
            _default_lock_path(config), owner=f"{self.profile_name}:{os.getpid()}"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._last_health_sweep = 0.0

    def start(self) -> bool:
        if not self.config.enabled:
            LOGGER.info("kanban-warden supervisor disabled by config")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self.run_forever, name="kanban-warden", daemon=True)
        self._thread.start()
        LOGGER.info("kanban-warden supervisor thread started profile=%s", self.profile_name)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self.lock.release()
        LOGGER.info("kanban-warden supervisor stopped profile=%s", self.profile_name)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("kanban-warden supervisor tick failed")
            if self.config.loop.once:
                return
            self._stop.wait(max(0.1, self.config.loop.event_interval_seconds))

    def tick(self) -> bool:
        now = time.time()
        if self.config.leader_lock.enabled and not self._ensure_leader(now):
            LOGGER.debug("kanban-warden skipped tick; another leader is active")
            return False
        if now - self._last_health_sweep >= self.config.loop.health_sweep_seconds:
            self._health_sweep(now)
            self._last_health_sweep = now
        LOGGER.info(
            "kanban-warden tick profile=%s boards=%s dry_run=%s notifications=%s",
            self.profile_name,
            self.config.boards,
            self.config.auto_advance.dry_run,
            self.config.notifications.enabled,
        )
        return True

    def status(self) -> dict[str, Any]:
        lock_status = self.lock.status()
        return {
            "enabled": self.config.enabled,
            "profile": self.profile_name,
            "boards": self.config.boards,
            "leader_lock": {
                "enabled": self.config.leader_lock.enabled,
                "owner": lock_status.owner,
                "active": lock_status.active,
                "expires_at": lock_status.expires_at,
                "self_owner": self.lock.owner,
            },
            "loop": {
                "event_interval_seconds": self.config.loop.event_interval_seconds,
                "health_sweep_seconds": self.config.loop.health_sweep_seconds,
            },
            "policies": {
                "notifications": self.config.notifications.__dict__,
                "auto_advance": self.config.auto_advance.__dict__,
                "limits": self.config.limits.__dict__,
            },
        }

    def _ensure_leader(self, now: float) -> bool:
        if now - self._last_heartbeat < self.config.leader_lock.heartbeat_seconds:
            return True
        if self.lock.heartbeat(lease_seconds=self.config.leader_lock.lease_seconds, now=now):
            self._last_heartbeat = now
            return True
        acquired = self.lock.acquire(lease_seconds=self.config.leader_lock.lease_seconds, now=now)
        if acquired:
            self._last_heartbeat = now
            LOGGER.info("kanban-warden acquired leader lock owner=%s", self.lock.owner)
        return acquired

    def _health_sweep(self, now: float) -> None:
        LOGGER.info(
            "kanban-warden health sweep profile=%s now=%.0f "
            "max_retries=%s task_timeout_seconds=%s stale_claim_seconds=%s",
            self.profile_name,
            now,
            self.config.limits.max_retries,
            self.config.limits.task_timeout_seconds,
            self.config.limits.stale_claim_seconds,
        )


def _default_lock_path(config: KanbanWardenConfig) -> str:
    if config.leader_lock.db_path:
        return config.leader_lock.db_path
    home = os.environ.get("HERMES_HOME") or os.path.join(Path.home(), ".hermes")
    return os.path.join(home, "kanban-warden", "leader-lock.db")


def install_signal_handlers(supervisor: WardenSupervisor) -> None:
    def _handler(_signum: int, _frame: Any) -> None:
        supervisor.stop()

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        LOGGER.debug("kanban-warden signal handlers not installed outside main thread")


def demo_lock_contention(db_path: str | None = None) -> dict[str, Any]:
    path = db_path or os.path.join(tempfile.mkdtemp(prefix="kanban-warden-"), "leader.db")
    first = LeaderLock(path, owner="demo-profile-a")
    second = LeaderLock(path, owner="demo-profile-b")
    first_acquired = first.acquire(lease_seconds=30)
    second_acquired = second.acquire(lease_seconds=30)
    status = first.status()
    return {
        "db_path": path,
        "first_acquired": first_acquired,
        "second_acquired": second_acquired,
        "active_owner": status.owner,
        "active": status.active,
    }
