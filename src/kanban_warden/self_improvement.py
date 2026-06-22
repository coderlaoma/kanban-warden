"""Controlled E3 self-improvement draft proposals."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .state import WardenStateStore


class SelfImprovementEngine:
    """Prepare auditable code-change drafts without mutating source code."""

    def __init__(self, state_store: WardenStateStore) -> None:
        self.state_store = state_store

    def create_code_change_drafts(
        self, *, created_at: float | None = None
    ) -> list[dict[str, Any]]:
        existing_proposal_ids = {
            proposal["proposal_id"]
            for proposal in self.state_store.recent_improvement_proposals(limit=1000)
        }
        drafts: list[dict[str, Any]] = []
        for signal in reversed(self.state_store.recent_improvement_signals(limit=500)):
            if signal["recommended_level"] != "E3" or signal["signal_type"] != "policy_gap":
                continue
            draft = self._draft_for_signal(signal, created_at=created_at)
            if draft["proposal_id"] not in existing_proposal_ids:
                self._record_proposal_created(draft, created_at=created_at)
            existing_proposal_ids.add(draft["proposal_id"])
            drafts.append(draft)
        return drafts

    def _draft_for_signal(
        self, signal: dict[str, Any], *, created_at: float | None
    ) -> dict[str, Any]:
        slug = _slug_from_scope(str(signal["scope"]))
        proposal_id = _proposal_id(
            proposal_type="code_change",
            level="E3",
            signal_id=str(signal["signal_id"]),
            target=str(signal["scope"]),
            suggested_value="draft_code_change_plan",
        )
        patch = {
            "branch_name": f"warden/improve-{proposal_id.split(':')[-1]}-{slug}",
            "affected_files": _affected_files_for_scope(str(signal["scope"])),
            "verification_commands": _verification_commands_for_scope(str(signal["scope"])),
            "mutates_source": False,
        }
        draft = self.state_store.record_improvement_proposal(
            proposal_type="code_change",
            level="E3",
            signal_id=str(signal["signal_id"]),
            title=f"Draft code improvement for {slug.replace('-', ' ')}",
            evidence_summary=str(signal["summary"]),
            target=str(signal["scope"]),
            current_value="not_expressible_in_config",
            suggested_value="draft_code_change_plan",
            reason="Repeated evidence indicates this behavior needs a detector or code path change.",
            risk="medium",
            rollback_value="do_not_apply_generated_branch",
            approval_required=True,
            patch=patch,
            created_at=created_at,
        )
        return draft

    def record_code_change_approval(
        self,
        *,
        proposal_id: str,
        actor: str,
        allowed_repository: str,
        allowed_branch_prefix: str,
        verification_commands: list[str],
        reason: str,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        proposal = self._proposal_by_id(proposal_id)
        if proposal["level"] != "E3" or proposal["proposal_type"] != "code_change":
            raise ValueError("only E3 code-change proposals can be approved with this method")
        branch_name = str(proposal["patch"].get("branch_name", ""))
        if not branch_name.startswith(allowed_branch_prefix):
            raise ValueError("approval branch prefix must match the proposal branch")
        if verification_commands != proposal["patch"].get("verification_commands", []):
            raise ValueError("approval verification commands must match the proposal")
        approval = self.state_store.record_improvement_approval(
            proposal_id=proposal_id,
            actor=actor,
            decision="approved",
            reason=reason,
            created_at=created_at,
        )
        self.state_store.record_improvement_audit(
            subject_id=proposal_id,
            event_type="human_approved",
            actor=actor,
            payload={
                "approved_level": "E3",
                "allowed_repository": allowed_repository,
                "allowed_branch_prefix": allowed_branch_prefix,
                "verification_commands": verification_commands,
                "reason": reason,
            },
            created_at=created_at,
        )
        return approval

    def prepare_code_change_package(
        self, *, proposal_id: str, created_at: float | None = None
    ) -> dict[str, Any]:
        proposal = self._proposal_by_id(proposal_id)
        if proposal["level"] != "E3" or proposal["proposal_type"] != "code_change":
            raise ValueError("only E3 code-change proposals can be packaged")
        if not self._is_approved(proposal_id):
            raise ValueError("code-change package can only be prepared after it is approved")
        patch = proposal["patch"]
        verification_commands = _string_list(patch.get("verification_commands", []))
        package = {
            "proposal_id": proposal_id,
            "branch_name": str(patch.get("branch_name", "")),
            "affected_files": _string_list(patch.get("affected_files", [])),
            "verification_commands": verification_commands,
            "mutates_source": False,
            "commit_message": _commit_message(proposal, verification_commands),
            "pull_request_title": str(proposal["title"]),
            "pull_request_body": _pull_request_body(proposal, verification_commands),
        }
        self.state_store.record_improvement_audit(
            subject_id=proposal_id,
            event_type="code_change_package_prepared",
            actor="kanban-warden",
            payload={
                "branch_name": package["branch_name"],
                "affected_files": package["affected_files"],
                "verification_commands": verification_commands,
                "mutates_source": False,
            },
            created_at=created_at,
        )
        return package

    def record_code_change_verification(
        self,
        *,
        proposal_id: str,
        actor: str,
        command_results: list[dict[str, Any]],
        created_at: float | None = None,
    ) -> dict[str, Any]:
        proposal = self._proposal_by_id(proposal_id)
        if proposal["level"] != "E3" or proposal["proposal_type"] != "code_change":
            raise ValueError("only E3 code-change proposals can record verification")
        if not self._is_package_prepared(proposal_id):
            raise ValueError("code-change package must be prepared before verification is recorded")
        expected_commands = _string_list(proposal["patch"].get("verification_commands", []))
        normalized_results = [_command_result(result) for result in command_results]
        if [result["command"] for result in normalized_results] != expected_commands:
            raise ValueError("verification commands must match the prepared package")
        self.state_store.record_improvement_audit(
            subject_id=proposal_id,
            event_type="verification_started",
            actor=actor,
            payload={"commands": expected_commands, "result_count": len(normalized_results)},
            created_at=created_at,
        )
        failed_commands = [
            str(result["command"]) for result in normalized_results if result["exit_code"] != 0
        ]
        status = "failed" if failed_commands else "passed"
        verification = {
            "proposal_id": proposal_id,
            "status": status,
            "failed_commands": failed_commands,
            "command_results": normalized_results,
        }
        self.state_store.record_improvement_audit(
            subject_id=proposal_id,
            event_type=f"verification_{status}",
            actor=actor,
            payload={
                "status": status,
                "failed_commands": failed_commands,
                "command_results": normalized_results,
            },
            created_at=created_at,
        )
        return verification

    def prepare_human_review_packet(
        self, *, proposal_id: str, actor: str, created_at: float | None = None
    ) -> dict[str, Any]:
        proposal = self._proposal_by_id(proposal_id)
        if proposal["level"] != "E3" or proposal["proposal_type"] != "code_change":
            raise ValueError("only E3 code-change proposals can be reviewed")
        package_payload = self._audit_payload(proposal_id, "code_change_package_prepared")
        if package_payload is None:
            raise ValueError("human review packet requires prepared package")
        verification_payload = self._audit_payload(proposal_id, "verification_passed")
        if verification_payload is None:
            raise ValueError("human review packet requires passed verification")
        signal = self._signal_by_id(str(proposal["signal_id"]))
        approval = self._approval_by_id(proposal_id)
        packet = {
            "proposal_id": proposal_id,
            "proposal_summary": {
                "title": proposal["title"],
                "target": proposal["target"],
                "level": proposal["level"],
                "risk": proposal["risk"],
                "reason": proposal["reason"],
            },
            "evidence": {
                "signal_id": signal["signal_id"],
                "summary": signal["summary"],
                "supporting_trace_ids": signal["supporting_trace_ids"],
                "supporting_outcome_ids": signal["supporting_outcome_ids"],
            },
            "package_summary": {
                "branch_name": package_payload.get("branch_name", ""),
                "affected_files": _string_list(package_payload.get("affected_files", [])),
                "mutates_source": bool(package_payload.get("mutates_source", True)),
                "commit_message": _commit_message(
                    proposal,
                    _string_list(package_payload.get("verification_commands", [])),
                ),
                "pull_request_title": str(proposal["title"]),
            },
            "verification": {
                "status": str(verification_payload.get("status", "")),
                "failed_commands": _string_list(verification_payload.get("failed_commands", [])),
                "command_results": _command_results(
                    verification_payload.get("command_results", [])
                ),
            },
            "approval": {
                "approval_id": approval["approval_id"],
                "actor": approval["actor"],
                "reason": approval["reason"],
            },
            "links": {"branch": "", "pull_request": ""},
            "rollback_plan": proposal["rollback_value"],
        }
        self.state_store.record_improvement_audit(
            subject_id=proposal_id,
            event_type="human_review_requested",
            actor=actor,
            payload={
                "proposal_id": proposal_id,
                "verification_status": packet["verification"]["status"],
                "branch_name": packet["package_summary"]["branch_name"],
                "pull_request": "",
            },
            created_at=created_at,
        )
        return packet

    def record_human_review_decision(
        self,
        *,
        proposal_id: str,
        reviewer: str,
        decision: str,
        reason: str,
        branch_url: str = "",
        pull_request_url: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        proposal = self._proposal_by_id(proposal_id)
        if proposal["level"] != "E3" or proposal["proposal_type"] != "code_change":
            raise ValueError("only E3 code-change proposals can record human review")
        if self._audit_payload(proposal_id, "human_review_requested") is None:
            raise ValueError("human review request must be prepared before recording a decision")
        if decision not in {"approved", "rejected"}:
            raise ValueError("human review decision must be approved or rejected")
        review = {
            "proposal_id": proposal_id,
            "reviewer": reviewer,
            "decision": decision,
            "reason": reason,
            "branch_url": branch_url,
            "pull_request_url": pull_request_url,
        }
        self.state_store.record_improvement_audit(
            subject_id=proposal_id,
            event_type=f"human_review_{decision}",
            actor=reviewer,
            payload=review,
            created_at=created_at,
        )
        return review

    def _record_proposal_created(
        self, proposal: dict[str, Any], *, created_at: float | None
    ) -> None:
        self.state_store.record_improvement_audit(
            subject_id=str(proposal["proposal_id"]),
            event_type="proposal_created",
            actor="kanban-warden",
            payload={
                "proposal_type": proposal["proposal_type"],
                "level": proposal["level"],
                "target": proposal["target"],
                "mutates_source": proposal["patch"]["mutates_source"],
            },
            created_at=created_at,
        )

    def _proposal_by_id(self, proposal_id: str) -> dict[str, Any]:
        for proposal in self.state_store.recent_improvement_proposals(limit=1000):
            if proposal["proposal_id"] == proposal_id:
                return proposal
        raise ValueError(f"unknown improvement proposal: {proposal_id}")

    def _signal_by_id(self, signal_id: str) -> dict[str, Any]:
        for signal in self.state_store.recent_improvement_signals(limit=1000):
            if signal["signal_id"] == signal_id:
                return signal
        raise ValueError(f"unknown improvement signal: {signal_id}")

    def _approval_by_id(self, proposal_id: str) -> dict[str, Any]:
        for approval in self.state_store.recent_improvement_approvals(limit=1000):
            if approval["proposal_id"] == proposal_id and approval["decision"] == "approved":
                return approval
        raise ValueError(f"missing approval for improvement proposal: {proposal_id}")

    def _is_approved(self, proposal_id: str) -> bool:
        for approval in self.state_store.recent_improvement_approvals(limit=1000):
            if approval["proposal_id"] == proposal_id and approval["decision"] == "approved":
                return True
        return False

    def _is_package_prepared(self, proposal_id: str) -> bool:
        for audit in self.state_store.recent_improvement_audit(limit=1000):
            if (
                audit["subject_id"] == proposal_id
                and audit["event_type"] == "code_change_package_prepared"
            ):
                return True
        return False

    def _audit_payload(self, proposal_id: str, event_type: str) -> dict[str, Any] | None:
        for audit in self.state_store.recent_improvement_audit(limit=1000):
            if audit["subject_id"] == proposal_id and audit["event_type"] == event_type:
                payload = audit["payload"]
                return payload if isinstance(payload, dict) else {}
        return None


def _slug_from_scope(scope: str) -> str:
    return scope.split(".")[-1].replace("_", "-")[:80]


def _affected_files_for_scope(scope: str) -> list[str]:
    if scope == "detector.high_activity_low_progress":
        return [
            "src/kanban_warden/board.py",
            "tests/test_board_events.py",
            "docs/loop-supervisor/v0.4-self-improvement.md",
        ]
    return ["src/kanban_warden/board.py", "tests/test_board_events.py"]


def _verification_commands_for_scope(scope: str) -> list[str]:
    if scope.startswith("detector."):
        return [
            "uv run pytest tests/test_board_events.py -q",
            "uv run ruff check .",
            "uv run mypy src",
        ]
    return ["uv run pytest", "uv run ruff check .", "uv run mypy src"]


def _proposal_id(
    *,
    proposal_type: str,
    level: str,
    signal_id: str,
    target: str,
    suggested_value: str,
) -> str:
    seed = {
        "proposal_type": proposal_type,
        "level": level,
        "signal_id": signal_id,
        "target": target,
        "suggested_value": suggested_value,
    }
    digest = hashlib.sha256(
        json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"prop:{proposal_type}:{digest}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _command_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "command": str(value.get("command", "")),
        "exit_code": int(value.get("exit_code", 1)),
        "output": str(value.get("output", ""))[:2000],
    }


def _command_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_command_result(item) for item in value if isinstance(item, dict)]


def _commit_message(proposal: dict[str, Any], verification_commands: list[str]) -> str:
    slug = _slug_from_scope(str(proposal["target"]))
    verification = "; ".join(verification_commands)
    return (
        f"feat(warden): add {slug} loop improvement\n\n"
        f"Proposal: {proposal['proposal_id']}\n"
        f"Evidence: {proposal['signal_id']}\n"
        f"Verification: {verification}"
    )


def _pull_request_body(proposal: dict[str, Any], verification_commands: list[str]) -> str:
    verification = "\n".join(f"- `{command}`" for command in verification_commands)
    return (
        "## Summary\n"
        f"- prepare E3 code-change work for `{proposal['target']}`\n"
        f"- evidence: `{proposal['signal_id']}`\n"
        "- this package does not create branches or mutate source\n\n"
        "## Verification\n"
        f"{verification}"
    )
