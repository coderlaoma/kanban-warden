"""Persistent state store for Kanban Warden event processing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .sqlite_utils import managed_connection


class WardenStateStore:
    """Small SQLite store for cursors, idempotency keys, retries, and runtime metadata."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get_cursor(self, board_name: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "select cursor from board_cursors where board_name = ?", (board_name,)
            ).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, board_name: str, cursor: int) -> None:
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                insert into board_cursors(board_name, cursor, updated_at) values (?, ?, ?)
                on conflict(board_name) do update set cursor = excluded.cursor, updated_at = excluded.updated_at
                """,
                (board_name, cursor, now),
            )

    def mark_processed(self, key: str) -> bool:
        try:
            with self._connect() as con:
                con.execute(
                    "insert into processed_keys(key, created_at) values (?, ?)", (key, time.time())
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def bump_retry(self, board_name: str, task_id: str, action: str) -> int:
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                insert into retry_budgets(board_name, task_id, action, attempts, updated_at)
                values (?, ?, ?, 1, ?)
                on conflict(board_name, task_id, action) do update set
                  attempts = retry_budgets.attempts + 1,
                  updated_at = excluded.updated_at
                """,
                (board_name, task_id, action, now),
            )
            row = con.execute(
                "select attempts from retry_budgets where board_name = ? and task_id = ? and action = ?",
                (board_name, task_id, action),
            ).fetchone()
        return int(row[0])

    def peek_retry(self, board_name: str, task_id: str, action: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "select attempts from retry_budgets where board_name = ? and task_id = ? and action = ?",
                (board_name, task_id, action),
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_action_started(self, key: str) -> bool:
        now = time.time()
        with self._connect() as con:
            row = con.execute("select status from action_log where key = ?", (key,)).fetchone()
            if row:
                if str(row[0]) == "done":
                    return False
                con.execute(
                    """
                    update action_log
                    set status = 'started', attempts = attempts + 1, updated_at = ?
                    where key = ?
                    """,
                    (now, key),
                )
                return True
            con.execute(
                "insert into action_log(key, status, attempts, created_at, updated_at) values (?, 'started', 1, ?, ?)",
                (key, now, now),
            )
        return True

    def mark_action_done(self, key: str, note: str = "") -> None:
        with self._connect() as con:
            con.execute(
                "update action_log set status = 'done', note = ?, updated_at = ? where key = ?",
                (note, time.time(), key),
            )

    def mark_action_failed(self, key: str, error: str) -> None:
        with self._connect() as con:
            con.execute(
                "update action_log set status = 'failed', note = ?, updated_at = ? where key = ?",
                (error[:1000], time.time(), key),
            )

    def enqueue_notification(self, key: str, payload: dict[str, Any]) -> bool:
        now = time.time()
        try:
            with self._connect() as con:
                con.execute(
                    "insert into notification_outbox(key, payload_json, status, attempts, created_at, updated_at, next_attempt_at) values (?, ?, 'queued', 0, ?, ?, ?)",
                    (key, json.dumps(payload, sort_keys=True), now, now, 0.0),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def claim_notification_batch(
        self,
        *,
        limit: int,
        now: float,
        owner: str = "kanban-warden",
        lease_seconds: float = 300.0,
    ) -> list[dict[str, Any]]:
        """Mark eligible outbox rows in-progress and return their decoded payloads.

        ``in_progress`` rows carry a lease in ``next_attempt_at``. If a worker
        crashes after claiming but before it marks the row delivered/retrying,
        a later claim may reclaim the row after the lease expires without
        incrementing attempts. Legacy rows without a lease fall back to
        ``updated_at + lease_seconds`` so already stranded rows also recover.
        """

        if limit <= 0:
            return []
        lease_seconds = max(0.0, float(lease_seconds))
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                select key, payload_json, status, attempts, last_error, created_at, updated_at, next_attempt_at
                from notification_outbox
                where (
                    status in ('queued', 'retrying')
                    and coalesce(next_attempt_at, 0) <= ?
                )
                or (
                    status = 'in_progress'
                    and coalesce(nullif(next_attempt_at, 0), updated_at + ?) <= ?
                )
                order by created_at, key
                limit ?
                """,
                (now, lease_seconds, now, limit),
            ).fetchall()
            if not rows:
                return []
            keys = [str(row["key"]) for row in rows]
            placeholders = ",".join("?" for _ in keys)
            con.execute(
                f"""
                update notification_outbox
                set status = 'in_progress',
                    updated_at = ?,
                    next_attempt_at = ?,
                    last_error = null
                where key in ({placeholders})
                """,
                (now, now + lease_seconds, *keys),
            )
        claimed: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                payload = {}
            claimed.append(
                {
                    "key": str(row["key"]),
                    "payload": payload,
                    "status": str(row["status"]),
                    "attempts": int(row["attempts"]),
                    "last_error": row["last_error"],
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                    "next_attempt_at": (
                        float(row["next_attempt_at"])
                        if row["next_attempt_at"] is not None
                        else None
                    ),
                    "owner": owner,
                }
            )
        return claimed

    def mark_notification_delivered(self, key: str, *, now: float) -> None:
        with self._connect() as con:
            con.execute(
                """
                update notification_outbox
                set status = 'delivered',
                    attempts = attempts + 1,
                    last_error = null,
                    next_attempt_at = null,
                    updated_at = ?
                where key = ? and status = 'in_progress'
                """,
                (now, key),
            )

    def mark_notification_retry(
        self,
        key: str,
        *,
        error: str,
        now: float,
        next_attempt_at: float,
        exhausted: bool = False,
    ) -> None:
        status = "exhausted" if exhausted else "retrying"
        with self._connect() as con:
            con.execute(
                """
                update notification_outbox
                set status = ?,
                    attempts = attempts + 1,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                where key = ? and status = 'in_progress'
                """,
                (status, error[:1000], next_attempt_at if not exhausted else None, now, key),
            )

    def record_loop_trace(
        self,
        *,
        board_name: str,
        task_id: str,
        profile_name: str,
        loop_state: str,
        observed_facts: dict[str, Any],
        matched_policy: str,
        decision: str,
        confidence: str,
        planned_action: dict[str, Any],
        verification_contract: dict[str, Any],
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if created_at is None else created_at
        seed = {
            "board_name": board_name,
            "task_id": task_id,
            "profile_name": profile_name,
            "loop_state": loop_state,
            "matched_policy": matched_policy,
            "decision": decision,
            "planned_action": planned_action,
            "created_at": now,
        }
        trace_id = _loop_trace_id(seed, now)
        with self._connect() as con:
            con.execute(
                """
                insert or ignore into loop_trace(
                  trace_id, board_name, task_id, profile_name, loop_state,
                  observed_facts_json, matched_policy, decision, confidence,
                  planned_action_json, verification_contract_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    board_name,
                    task_id,
                    profile_name,
                    loop_state,
                    json.dumps(observed_facts, sort_keys=True),
                    matched_policy,
                    decision,
                    confidence,
                    json.dumps(planned_action, sort_keys=True),
                    json.dumps(verification_contract, sort_keys=True),
                    now,
                ),
            )
        return {
            "trace_id": trace_id,
            "board_name": board_name,
            "task_id": task_id,
            "profile_name": profile_name,
            "loop_state": loop_state,
            "observed_facts": observed_facts,
            "matched_policy": matched_policy,
            "decision": decision,
            "confidence": confidence,
            "planned_action": planned_action,
            "verification_contract": verification_contract,
            "created_at": now,
        }

    def record_loop_outcome(
        self,
        *,
        trace_id: str,
        board_name: str,
        task_id: str,
        action_type: str,
        status: str,
        verification_status: str,
        human_override: bool = False,
        override_reason: str = "",
        latency_seconds: float = 0.0,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if created_at is None else created_at
        with self._connect() as con:
            con.execute(
                """
                insert or ignore into loop_outcome(
                  trace_id, board_name, task_id, action_type, status,
                  verification_status, human_override, override_reason,
                  latency_seconds, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    board_name,
                    task_id,
                    action_type,
                    status,
                    verification_status,
                    int(human_override),
                    override_reason,
                    latency_seconds,
                    now,
                ),
            )
        return {
            "trace_id": trace_id,
            "board_name": board_name,
            "task_id": task_id,
            "action_type": action_type,
            "status": status,
            "verification_status": verification_status,
            "human_override": human_override,
            "override_reason": override_reason,
            "latency_seconds": latency_seconds,
            "created_at": now,
        }

    def recent_loop_traces(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select trace_id, board_name, task_id, profile_name, loop_state,
                       observed_facts_json, matched_policy, decision, confidence,
                       planned_action_json, verification_contract_json, created_at
                from loop_trace
                order by created_at desc, trace_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "trace_id": str(row[0]),
                "board_name": str(row[1]),
                "task_id": str(row[2]),
                "profile_name": str(row[3]),
                "loop_state": str(row[4]),
                "observed_facts": _json_object(row[5]),
                "matched_policy": str(row[6]),
                "decision": str(row[7]),
                "confidence": str(row[8]),
                "planned_action": _json_object(row[9]),
                "verification_contract": _json_object(row[10]),
                "created_at": float(row[11]),
            }
            for row in rows
        ]

    def recent_loop_outcomes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select trace_id, board_name, task_id, action_type, status,
                       verification_status, human_override, override_reason,
                       latency_seconds, created_at
                from loop_outcome
                order by created_at desc, trace_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "trace_id": str(row[0]),
                "board_name": str(row[1]),
                "task_id": str(row[2]),
                "action_type": str(row[3]),
                "status": str(row[4]),
                "verification_status": str(row[5]),
                "human_override": bool(row[6]),
                "override_reason": str(row[7]),
                "latency_seconds": float(row[8]),
                "created_at": float(row[9]),
            }
            for row in rows
        ]

    def record_improvement_signal(
        self,
        *,
        signal_type: str,
        scope: str,
        severity: str,
        supporting_trace_ids: list[str],
        supporting_outcome_ids: list[str],
        summary: str,
        recommended_level: str,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if created_at is None else created_at
        seed = {
            "signal_type": signal_type,
            "scope": scope,
            "supporting_trace_ids": supporting_trace_ids,
            "supporting_outcome_ids": supporting_outcome_ids,
        }
        signal_id = _stable_id("sig", signal_type, seed)
        with self._connect() as con:
            con.execute(
                """
                insert or ignore into improvement_signal(
                  signal_id, signal_type, scope, severity, supporting_trace_ids_json,
                  supporting_outcome_ids_json, summary, recommended_level, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    signal_type,
                    scope,
                    severity,
                    json.dumps(supporting_trace_ids, sort_keys=True),
                    json.dumps(supporting_outcome_ids, sort_keys=True),
                    summary,
                    recommended_level,
                    now,
                ),
            )
        return {
            "signal_id": signal_id,
            "signal_type": signal_type,
            "scope": scope,
            "severity": severity,
            "supporting_trace_ids": supporting_trace_ids,
            "supporting_outcome_ids": supporting_outcome_ids,
            "summary": summary,
            "recommended_level": recommended_level,
            "created_at": now,
        }

    def recent_improvement_signals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select signal_id, signal_type, scope, severity, supporting_trace_ids_json,
                       supporting_outcome_ids_json, summary, recommended_level, created_at
                from improvement_signal
                order by created_at desc, signal_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "signal_id": str(row[0]),
                "signal_type": str(row[1]),
                "scope": str(row[2]),
                "severity": str(row[3]),
                "supporting_trace_ids": _json_list(row[4]),
                "supporting_outcome_ids": _json_list(row[5]),
                "summary": str(row[6]),
                "recommended_level": str(row[7]),
                "created_at": float(row[8]),
            }
            for row in rows
        ]

    def record_improvement_audit(
        self,
        *,
        subject_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if created_at is None else created_at
        seed = {
            "subject_id": subject_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "created_at": now,
        }
        audit_id = _stable_id("audit", event_type, seed)
        with self._connect() as con:
            con.execute(
                """
                insert or ignore into improvement_audit(
                  audit_id, subject_id, event_type, actor, payload_json, created_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (audit_id, subject_id, event_type, actor, json.dumps(payload, sort_keys=True), now),
            )
        return {
            "audit_id": audit_id,
            "subject_id": subject_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "created_at": now,
        }

    def recent_improvement_audit(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select audit_id, subject_id, event_type, actor, payload_json, created_at
                from improvement_audit
                order by created_at desc, audit_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "audit_id": str(row[0]),
                "subject_id": str(row[1]),
                "event_type": str(row[2]),
                "actor": str(row[3]),
                "payload": _json_object(row[4]),
                "created_at": float(row[5]),
            }
            for row in rows
        ]

    def record_improvement_proposal(
        self,
        *,
        proposal_type: str,
        level: str,
        signal_id: str,
        title: str,
        evidence_summary: str,
        target: str,
        current_value: str,
        suggested_value: str,
        reason: str,
        risk: str,
        rollback_value: str,
        approval_required: bool,
        patch: dict[str, Any],
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if created_at is None else created_at
        seed = {
            "proposal_type": proposal_type,
            "level": level,
            "signal_id": signal_id,
            "target": target,
            "suggested_value": suggested_value,
        }
        proposal_id = _stable_id("prop", proposal_type, seed)
        with self._connect() as con:
            con.execute(
                """
                insert or ignore into improvement_proposal(
                  proposal_id, proposal_type, level, signal_id, title, evidence_summary,
                  target, current_value, suggested_value, reason, risk, rollback_value,
                  approval_required, patch_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    proposal_type,
                    level,
                    signal_id,
                    title,
                    evidence_summary,
                    target,
                    current_value,
                    suggested_value,
                    reason,
                    risk,
                    rollback_value,
                    int(approval_required),
                    json.dumps(patch, sort_keys=True),
                    now,
                ),
            )
        return {
            "proposal_id": proposal_id,
            "proposal_type": proposal_type,
            "level": level,
            "signal_id": signal_id,
            "title": title,
            "evidence_summary": evidence_summary,
            "target": target,
            "current_value": current_value,
            "suggested_value": suggested_value,
            "reason": reason,
            "risk": risk,
            "rollback_value": rollback_value,
            "approval_required": approval_required,
            "patch": patch,
            "created_at": now,
        }

    def recent_improvement_proposals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select proposal_id, proposal_type, level, signal_id, title, evidence_summary,
                       target, current_value, suggested_value, reason, risk, rollback_value,
                       approval_required, patch_json, created_at
                from improvement_proposal
                order by created_at desc, proposal_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "proposal_id": str(row[0]),
                "proposal_type": str(row[1]),
                "level": str(row[2]),
                "signal_id": str(row[3]),
                "title": str(row[4]),
                "evidence_summary": str(row[5]),
                "target": str(row[6]),
                "current_value": str(row[7]),
                "suggested_value": str(row[8]),
                "reason": str(row[9]),
                "risk": str(row[10]),
                "rollback_value": str(row[11]),
                "approval_required": bool(row[12]),
                "patch": _json_object(row[13]),
                "created_at": float(row[14]),
            }
            for row in rows
        ]

    def record_improvement_approval(
        self,
        *,
        proposal_id: str,
        actor: str,
        decision: str,
        reason: str,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if created_at is None else created_at
        seed = {
            "proposal_id": proposal_id,
            "actor": actor,
            "decision": decision,
            "created_at": now,
        }
        approval_id = _stable_id("approval", decision, seed)
        with self._connect() as con:
            con.execute(
                """
                insert or ignore into improvement_approval(
                  approval_id, proposal_id, actor, decision, reason, created_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (approval_id, proposal_id, actor, decision, reason, now),
            )
        return {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "actor": actor,
            "decision": decision,
            "reason": reason,
            "created_at": now,
        }

    def recent_improvement_approvals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select approval_id, proposal_id, actor, decision, reason, created_at
                from improvement_approval
                order by created_at desc, approval_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "approval_id": str(row[0]),
                "proposal_id": str(row[1]),
                "actor": str(row[2]),
                "decision": str(row[3]),
                "reason": str(row[4]),
                "created_at": float(row[5]),
            }
            for row in rows
        ]

    def set_runtime_metadata(self, key: str, value: dict[str, Any]) -> None:
        with self._connect() as con:
            con.execute(
                """
                insert into runtime_metadata(key, value_json, updated_at) values (?, ?, ?)
                on conflict(key) do update set value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, sort_keys=True), time.time()),
            )

    def get_runtime_metadata(self, key: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "select value_json from runtime_metadata where key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        raw = json.loads(str(row[0]))
        return raw if isinstance(raw, dict) else None

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as con:
            cursors = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    "select board_name, cursor from board_cursors order by board_name"
                )
            }
            retry_rows = [
                {
                    "board_name": str(row[0]),
                    "task_id": str(row[1]),
                    "action": str(row[2]),
                    "attempts": int(row[3]),
                }
                for row in con.execute(
                    "select board_name, task_id, action, attempts from retry_budgets order by board_name, task_id, action"
                )
            ]
            processed_count = int(con.execute("select count(*) from processed_keys").fetchone()[0])
            action_rows = [
                {
                    "key": str(row[0]),
                    "status": str(row[1]),
                    "attempts": int(row[2]),
                    "last_note": str(row[3]),
                }
                for row in con.execute(
                    "select key, status, attempts, note from action_log order by updated_at desc, key limit 50"
                )
            ]
            outbox_count = int(con.execute("select count(*) from notification_outbox").fetchone()[0])
            outbox_by_status = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    "select status, count(*) from notification_outbox group by status order by status"
                )
            }
            outbox_recent = [
                {
                    "key": str(row[0]),
                    "status": str(row[1]),
                    "attempts": int(row[2]),
                    "last_error": str(row[3]) if row[3] is not None else None,
                    "next_attempt_at": float(row[4]) if row[4] is not None else None,
                }
                for row in con.execute(
                    """
                    select key, status, attempts, last_error, next_attempt_at
                    from notification_outbox
                    order by updated_at desc, key
                    limit 20
                    """
                )
            ]
            loop_trace_count = int(con.execute("select count(*) from loop_trace").fetchone()[0])
            loop_outcome_count = int(
                con.execute("select count(*) from loop_outcome").fetchone()[0]
            )
            improvement_signal_count = int(
                con.execute("select count(*) from improvement_signal").fetchone()[0]
            )
            improvement_audit_count = int(
                con.execute("select count(*) from improvement_audit").fetchone()[0]
            )
            improvement_proposal_count = int(
                con.execute("select count(*) from improvement_proposal").fetchone()[0]
            )
            improvement_approval_count = int(
                con.execute("select count(*) from improvement_approval").fetchone()[0]
            )
        return {
            "cursors": cursors,
            "processed_key_count": processed_count,
            "retry_budgets": retry_rows,
            "action_log": action_rows,
            "notification_outbox_count": outbox_count,
            "notification_outbox_by_status": outbox_by_status,
            "notification_outbox_recent": outbox_recent,
            "loop_trace_count": loop_trace_count,
            "loop_outcome_count": loop_outcome_count,
            "loop_traces_recent": self.recent_loop_traces(limit=20),
            "loop_outcomes_recent": self.recent_loop_outcomes(limit=20),
            "improvement_signal_count": improvement_signal_count,
            "improvement_proposal_count": improvement_proposal_count,
            "improvement_approval_count": improvement_approval_count,
            "improvement_audit_count": improvement_audit_count,
            "improvement_signals_recent": self.recent_improvement_signals(limit=20),
            "improvement_proposals_recent": self.recent_improvement_proposals(limit=20),
            "improvement_approvals_recent": self.recent_improvement_approvals(limit=20),
            "improvement_audit_recent": self.recent_improvement_audit(limit=20),
        }

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("pragma journal_mode = wal")
        return con

    def _init_db(self) -> None:
        with managed_connection(self.db_path) as con:
            con.executescript(
                """
                create table if not exists board_cursors (
                  board_name text primary key,
                  cursor integer not null default 0,
                  updated_at real not null
                );
                create table if not exists processed_keys (
                  key text primary key,
                  created_at real not null
                );
                create table if not exists retry_budgets (
                  board_name text not null,
                  task_id text not null,
                  action text not null,
                  attempts integer not null default 0,
                  updated_at real not null,
                  primary key(board_name, task_id, action)
                );
                create table if not exists runtime_metadata (
                  key text primary key,
                  value_json text not null,
                  updated_at real not null
                );
                create table if not exists action_log (
                  key text primary key,
                  status text not null,
                  attempts integer not null default 0,
                  note text not null default '',
                  created_at real not null,
                  updated_at real not null
                );
                create table if not exists notification_outbox (
                  key text primary key,
                  payload_json text not null,
                  status text not null default 'queued',
                  attempts integer not null default 0,
                  last_error text,
                  created_at real not null,
                  updated_at real not null,
                  next_attempt_at real
                );
                create table if not exists loop_trace (
                  trace_id text primary key,
                  board_name text not null,
                  task_id text not null,
                  profile_name text not null,
                  loop_state text not null,
                  observed_facts_json text not null,
                  matched_policy text not null,
                  decision text not null,
                  confidence text not null,
                  planned_action_json text not null,
                  verification_contract_json text not null,
                  created_at real not null
                );
                create table if not exists loop_outcome (
                  trace_id text not null,
                  board_name text not null,
                  task_id text not null,
                  action_type text not null,
                  status text not null,
                  verification_status text not null,
                  human_override integer not null default 0,
                  override_reason text not null default '',
                  latency_seconds real not null default 0,
                  created_at real not null,
                  primary key (trace_id, action_type, status, verification_status, created_at)
                );
                create table if not exists improvement_signal (
                  signal_id text primary key,
                  signal_type text not null,
                  scope text not null,
                  severity text not null,
                  supporting_trace_ids_json text not null,
                  supporting_outcome_ids_json text not null,
                  summary text not null,
                  recommended_level text not null,
                  created_at real not null
                );
                create table if not exists improvement_audit (
                  audit_id text primary key,
                  subject_id text not null,
                  event_type text not null,
                  actor text not null,
                  payload_json text not null,
                  created_at real not null
                );
                create table if not exists improvement_proposal (
                  proposal_id text primary key,
                  proposal_type text not null,
                  level text not null,
                  signal_id text not null,
                  title text not null,
                  evidence_summary text not null,
                  target text not null,
                  current_value text not null,
                  suggested_value text not null,
                  reason text not null,
                  risk text not null,
                  rollback_value text not null,
                  approval_required integer not null,
                  patch_json text not null,
                  created_at real not null
                );
                create table if not exists improvement_approval (
                  approval_id text primary key,
                  proposal_id text not null,
                  actor text not null,
                  decision text not null,
                  reason text not null,
                  created_at real not null
                );
                """
            )
            columns = {
                str(row[1]) for row in con.execute("pragma table_info(notification_outbox)")
            }
            if "next_attempt_at" not in columns:
                con.execute("alter table notification_outbox add column next_attempt_at real")


def _json_object(raw: Any) -> dict[str, Any]:
    value = json.loads(str(raw))
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[str]:
    value = json.loads(str(raw))
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _loop_trace_id(seed: dict[str, Any], created_at: float) -> str:
    digest = hashlib.sha256(
        json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"loop-trace:{int(created_at * 1000)}:{digest}"


def _stable_id(prefix: str, kind: str, seed: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}:{kind}:{digest}"
