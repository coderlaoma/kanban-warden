"""Background supervisor loop for the Kanban Warden Hermes plugin."""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import tempfile
import threading
import time
from typing import Any

from .actions import KanbanActionEngine, PlannedAction
from .board import BoardEventTailer, analyze_health, default_hermes_home, discover_boards
from .cleanup import StateCleanupConfig, execute_cleanup_plan, plan_cleanup, prune_state_store
from .config import BoardDatabase, KanbanWardenConfig, discover_board_databases
from .lock import LeaderLock
from .outbox import NotificationOutboxDrainer
from .remediation import open_board_connection, report_to_dict, run_deadlock_remediation
from .state import WardenStateStore

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
        self.state_store = WardenStateStore(_default_state_path(config))
        self.event_tailer = BoardEventTailer(self.state_store)
        self.action_engine = KanbanActionEngine(config, self.state_store)
        self.outbox_drainer = NotificationOutboxDrainer(config, self.state_store)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._last_health_sweep = 0.0
        self._last_cleanup = 0.0
        self._last_health_report: dict[str, Any] | None = None
        self._last_cleanup_report: dict[str, Any] | None = None

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
        report = self.collect(now=now)
        if now - self._last_health_sweep >= self.config.loop.health_sweep_seconds:
            self._health_sweep(now, report)
            self._last_health_sweep = now
        LOGGER.info(
            "kanban-warden tick profile=%s boards=%s new_events=%s health_findings=%s dry_run=%s notifications=%s",
            self.profile_name,
            len(report["boards"]),
            sum(int(board["new_events"]) for board in report["boards"]),
            len(report["health"]),
            self.config.auto_advance.dry_run,
            self.config.notifications.enabled,
        )
        return True

    def collect(self, *, now: float | None = None) -> dict[str, Any]:
        """Discover boards, tail new events, persist state, and return a dry-run-safe report."""

        current_time = time.time() if now is None else now
        boards = discover_boards(
            self.config.boards,
            hermes_home=self.config.hermes_home or default_hermes_home(),
        )
        board_reports: list[dict[str, Any]] = []
        recent_events: list[dict[str, Any]] = []
        relationships: dict[str, dict[str, Any]] = {}
        health: list[dict[str, Any]] = []
        planned_actions: list[dict[str, Any]] = []
        action_results: list[dict[str, Any]] = []
        loop_traces: list[dict[str, Any]] = []
        board_paths: dict[str, str] = {}
        for board in boards:
            board_paths[board.name] = str(board.db_path)
            cursor_before = self.state_store.get_cursor(board.name)
            active_statuses = set(self.config.task_filter.active_statuses)
            events = self.event_tailer.tail(
                board.name,
                board.db_path,
                active_statuses=active_statuses
                if self.config.task_filter.ignore_terminal_tasks
                else None,
            )
            cursor_after = self.state_store.get_cursor(board.name)
            board_reports.append(
                {
                    "name": board.name,
                    "kind": board.kind,
                    "db_path": str(board.db_path),
                    "cursor_before": cursor_before,
                    "cursor_after": cursor_after,
                    "new_events": len(events),
                }
            )
            for event in events[-10:]:
                recent_events.append(event.summary())
                if event.task_id:
                    relationships[f"{board.name}:{event.task_id}"] = event.relationship.to_dict()
            event_actions = self.action_engine.plan_for_events(events)
            planned_actions.extend(action.to_dict() for action in event_actions)
            loop_traces.extend(
                self._record_loop_trace_for_action(action, created_at=current_time)
                for action in event_actions
                if _is_traceworthy_action(action)
            )
            action_results.extend(
                result.to_dict()
                for result in self.action_engine.apply(board.db_path, event_actions)
            )
            board_health = analyze_health(
                board.name,
                board.db_path,
                now=current_time,
                stale_claim_seconds=self.config.limits.stale_claim_seconds,
                task_timeout_seconds=self.config.limits.task_timeout_seconds,
            )
            health.extend(board_health)
            health_actions = self.action_engine.plan_for_health(board_health)
            planned_actions.extend(action.to_dict() for action in health_actions)
            loop_traces.extend(
                self._record_loop_trace_for_action(action, created_at=current_time)
                for action in health_actions
                if _is_traceworthy_action(action)
            )
            action_results.extend(
                result.to_dict()
                for result in self.action_engine.apply(board.db_path, health_actions)
            )
        outbox_delivery = self.outbox_drainer.drain(board_paths, now=current_time)
        report = {
            "profile": self.profile_name,
            "dry_run": self.config.auto_advance.dry_run,
            "boards": board_reports,
            "recent_events": recent_events,
            "relationships": list(relationships.values()),
            "health": health,
            "loop_traces": loop_traces,
            "planned_actions": planned_actions,
            "action_results": action_results,
            "outbox_delivery": outbox_delivery,
            "state": self.state_store.snapshot(),
        }
        self.state_store.set_runtime_metadata(
            "last_collect", {"at": current_time, "boards": [b["name"] for b in board_reports]}
        )
        return report

    def _record_loop_trace_for_action(
        self, action: PlannedAction, *, created_at: float
    ) -> dict[str, Any]:
        trace = self.state_store.record_loop_trace(
            board_name=action.board_name,
            task_id=action.task_id or "",
            profile_name=self.profile_name,
            loop_state=_loop_state_for_action(action),
            observed_facts={
                "reason": action.reason,
                "action_kind": action.kind,
                "target_task_id": action.target_task_id,
            },
            matched_policy=_matched_policy_for_action(action),
            decision=action.kind,
            confidence=_confidence_for_action(action),
            planned_action=action.to_dict(),
            verification_contract=_verification_contract_for_action(action),
            created_at=created_at,
        )
        self.state_store.record_loop_outcome(
            trace_id=trace["trace_id"],
            board_name=action.board_name,
            task_id=action.task_id or "",
            action_type=action.kind,
            status="planned" if action.dry_run else "pending_apply",
            verification_status="pending",
            created_at=created_at,
        )
        return trace

    def dry_run(self, *, now: float | None = None) -> dict[str, Any]:
        return self.collect(now=now)

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
            "state": self.state_store.snapshot(),
            "policies": {
                "notifications": self.config.notifications.__dict__,
                "auto_advance": self.config.auto_advance.__dict__,
                "limits": self.config.limits.__dict__,
            },
            "last_health_report": self._last_health_report,
            "last_cleanup_report": self._last_cleanup_report,
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

    def _health_sweep(self, now: float, report: dict[str, Any] | None = None) -> None:
        report = report or self.collect(now=now)
        remediation_report = self._run_remediation(now)
        if remediation_report is not None:
            self._last_health_report = remediation_report
        cleanup_report = self._run_cleanup(now)
        if cleanup_report is not None:
            self._last_cleanup_report = cleanup_report
        LOGGER.info(
            "kanban-warden health sweep profile=%s now=%.0f boards=%s findings=%s",
            self.profile_name,
            now,
            len(report.get("boards", [])),
            len(report.get("health", [])),
        )

    def _run_remediation(self, now: float) -> dict[str, Any] | None:
        board_dbs = discover_board_databases(self.config)
        if not board_dbs:
            return None
        reports: list[dict[str, Any]] = []
        for board_db in board_dbs:
            report = self._run_board_remediation(board_db, now=now)
            if report is not None:
                reports.append(report)
        if not reports:
            return None
        if len(reports) == 1:
            return reports[0]
        return {
            "board": "*",
            "boards_scanned": len(reports),
            "dry_run": self.config.auto_advance.dry_run,
            "auto_advance": self.config.auto_advance.enabled,
            "reports": reports,
            "proposals": [proposal for report in reports for proposal in report.get("proposals", [])],
        }

    def _run_board_remediation(self, board_db: BoardDatabase, *, now: float) -> dict[str, Any] | None:
        try:
            with open_board_connection(board_db.db_path) as conn:
                report = run_deadlock_remediation(
                    conn,
                    board=board_db.name,
                    now=int(now),
                    dry_run=self.config.auto_advance.dry_run,
                    auto_advance=self.config.auto_advance.enabled,
                    max_retries=self.config.limits.max_retries,
                    stale_claim_seconds=self.config.limits.stale_claim_seconds,
                )
                if self.config.auto_advance.enabled and not self.config.auto_advance.dry_run:
                    conn.commit()
                return report_to_dict(report)
        except sqlite3.Error as exc:
            LOGGER.warning(
                "kanban-warden health scan skipped board=%s db_path=%s sqlite_error=%s",
                board_db.name,
                board_db.db_path,
                exc.__class__.__name__,
            )
            return {"board": board_db.name, "error": exc.__class__.__name__, "proposals": []}

    def _run_cleanup(self, now: float) -> dict[str, Any] | None:
        cleanup = self.config.cleanup
        if not cleanup.enabled:
            return None
        if now - self._last_cleanup < cleanup.min_interval_seconds:
            return None
        board_dbs = discover_board_databases(self.config)
        reports: list[dict[str, Any]] = []
        for board_db in board_dbs:
            try:
                with open_board_connection(board_db.db_path) as conn:
                    plan = plan_cleanup(
                        conn,
                        now=now,
                        done_retention_days=cleanup.done_retention_days,
                        archived_retention_days=cleanup.archived_retention_days,
                        archive_done=cleanup.archive_done,
                        purge_archived=cleanup.purge_archived,
                    )
                if not plan.should_run_gc and not plan.archive_done_ids and not plan.purge_archived_ids:
                    continue
                report: dict[str, Any] = {"board": board_db.name, "plan": plan.to_dict()}
                if self.config.auto_advance.dry_run:
                    report["dry_run"] = True
                else:
                    report["dry_run"] = False
                    report["result"] = execute_cleanup_plan(
                        plan,
                        board=board_db.name,
                        db_path=board_db.db_path,
                        gc_retention_days=cleanup.gc_retention_days,
                        run_gc=cleanup.gc_enabled,
                    )
                reports.append(report)
            except sqlite3.Error as exc:
                reports.append({"board": board_db.name, "error": exc.__class__.__name__})
        state_report = prune_state_store(
            _default_state_path(self.config),
            now=now,
            config=StateCleanupConfig(
                retention_days=cleanup.state_retention_days,
                vacuum=cleanup.state_vacuum,
            ),
        )
        if any(
            state_report.get(key, 0)
            for key in (
                "processed_keys_deleted",
                "action_log_deleted",
                "notification_outbox_deleted",
                "retry_budgets_deleted",
            )
        ):
            reports.append({"state": state_report})
        self._last_cleanup = now
        if not reports:
            return None
        return {"board": "*", "reports": reports}


def _default_state_path(config: KanbanWardenConfig) -> str:
    if config.state_db_path:
        return config.state_db_path
    return config.resolved_state_db_path()


def _default_lock_path(config: KanbanWardenConfig) -> str:
    if config.leader_lock.db_path:
        return config.leader_lock.db_path
    return str(config.profile_home_path() / "kanban-warden" / "leader-lock.db")


def _is_traceworthy_action(action: PlannedAction) -> bool:
    return action.kind in {
        "create_reviewer",
        "create_implementer_followup",
        "unblock",
        "promote",
        "finalize",
        "retry",
        "escalate",
    }


def _loop_state_for_action(action: PlannedAction) -> str:
    if action.kind == "create_reviewer":
        return "waiting_for_review"
    if action.kind == "create_implementer_followup":
        return "waiting_for_worker"
    if action.kind in {"retry", "escalate"}:
        return "no_progress"
    return "action_planned" if action.dry_run else "verifying_action"


def _matched_policy_for_action(action: PlannedAction) -> str:
    if action.kind == "create_reviewer":
        return "review_required"
    if action.kind == "create_implementer_followup":
        return "review_needs_changes"
    if action.kind in {"retry", "escalate"}:
        return "bounded_recovery"
    return action.reason.replace(" ", "_")[:80] or action.kind


def _confidence_for_action(action: PlannedAction) -> str:
    if action.kind in {"create_reviewer", "unblock", "finalize"}:
        return "high"
    return "medium"


def _verification_contract_for_action(action: PlannedAction) -> dict[str, Any]:
    if action.kind == "create_reviewer":
        return {"success": "reviewer_card_exists", "target_task_id": action.target_task_id}
    if action.kind == "create_implementer_followup":
        return {"success": "implementer_followup_exists"}
    if action.kind == "unblock":
        return {"success": "source_card_unblocked"}
    if action.kind == "finalize":
        return {"success": "source_card_closed"}
    if action.kind == "retry":
        return {"success": "new_progress_event_or_status_change"}
    if action.kind == "escalate":
        return {"success": "human_ack_or_manual_update"}
    return {"success": "action_observable_on_next_sweep"}


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
