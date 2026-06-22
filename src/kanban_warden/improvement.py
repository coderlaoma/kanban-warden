"""Evidence-backed improvement signal and proposal generation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .state import WardenStateStore


class ImprovementEngine:
    """Derive conservative improvement records from loop trace outcomes."""

    def __init__(self, state_store: WardenStateStore) -> None:
        self.state_store = state_store

    def aggregate_signals(self, *, created_at: float | None = None) -> list[dict[str, Any]]:
        traces = {trace["trace_id"]: trace for trace in self.state_store.recent_loop_traces(limit=500)}
        outcomes = self.state_store.recent_loop_outcomes(limit=500)
        signals: list[dict[str, Any]] = []
        signals.extend(self._false_positive_signals(traces, outcomes, created_at=created_at))
        signals.extend(self._verification_failure_signals(traces, outcomes, created_at=created_at))
        return signals

    def create_recommendation_proposals(
        self, *, created_at: float | None = None
    ) -> list[dict[str, Any]]:
        proposals: list[dict[str, Any]] = []
        existing_proposal_ids = {
            proposal["proposal_id"]
            for proposal in self.state_store.recent_improvement_proposals(limit=1000)
        }
        for signal in reversed(self.state_store.recent_improvement_signals(limit=500)):
            if signal["recommended_level"] != "E1":
                continue
            proposal = self._recommendation_for_signal(
                signal,
                created_at=created_at,
                existing_proposal_ids=existing_proposal_ids,
            )
            if proposal is not None:
                proposals.append(proposal)
                existing_proposal_ids.add(proposal["proposal_id"])
        return proposals

    def record_approval(
        self,
        *,
        proposal_id: str,
        actor: str,
        decision: str,
        reason: str,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        approval = self.state_store.record_improvement_approval(
            proposal_id=proposal_id,
            actor=actor,
            decision=decision,
            reason=reason,
            created_at=created_at,
        )
        event_type = "human_approved" if decision == "approved" else "human_rejected"
        self.state_store.record_improvement_audit(
            subject_id=proposal_id,
            event_type=event_type,
            actor=actor,
            payload={"decision": decision, "reason": reason},
            created_at=created_at,
        )
        return approval

    def _false_positive_signals(
        self,
        traces: dict[str, dict[str, Any]],
        outcomes: list[dict[str, Any]],
        *,
        created_at: float | None,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for outcome in outcomes:
            if not outcome["human_override"] or outcome["verification_status"] != "human_override":
                continue
            trace = traces.get(outcome["trace_id"])
            if trace is None:
                continue
            grouped[str(trace["matched_policy"])].append(outcome)
        signals: list[dict[str, Any]] = []
        for policy, policy_outcomes in sorted(grouped.items()):
            if len(policy_outcomes) < 3:
                continue
            policy_outcomes = sorted(policy_outcomes, key=lambda outcome: outcome["created_at"])
            trace_ids = [str(outcome["trace_id"]) for outcome in policy_outcomes]
            outcome_ids = [_outcome_id(outcome) for outcome in policy_outcomes]
            signals.append(
                self.state_store.record_improvement_signal(
                    signal_type="false_positive",
                    scope=f"policy.{policy}",
                    severity="medium",
                    supporting_trace_ids=trace_ids,
                    supporting_outcome_ids=outcome_ids,
                    summary=(
                        f"Policy {policy} produced {len(policy_outcomes)} human-overridden "
                        "actions; consider loosening the threshold or routing."
                    ),
                    recommended_level="E1",
                    created_at=created_at,
                )
            )
        return signals

    def _verification_failure_signals(
        self,
        traces: dict[str, dict[str, Any]],
        outcomes: list[dict[str, Any]],
        *,
        created_at: float | None,
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for outcome in outcomes:
            if outcome["verification_status"] != "failed":
                continue
            trace = traces.get(outcome["trace_id"])
            if trace is None:
                continue
            grouped[(str(trace["matched_policy"]), str(outcome["action_type"]))].append(outcome)
        signals: list[dict[str, Any]] = []
        for (policy, action_type), policy_outcomes in sorted(grouped.items()):
            if len(policy_outcomes) < 2:
                continue
            policy_outcomes = sorted(policy_outcomes, key=lambda outcome: outcome["created_at"])
            trace_ids = [str(outcome["trace_id"]) for outcome in policy_outcomes]
            outcome_ids = [_outcome_id(outcome) for outcome in policy_outcomes]
            signals.append(
                self.state_store.record_improvement_signal(
                    signal_type="verification_failure",
                    scope=f"policy.{policy}.{action_type}",
                    severity="high",
                    supporting_trace_ids=trace_ids,
                    supporting_outcome_ids=outcome_ids,
                    summary=(
                        f"Action {action_type} under policy {policy} failed verification "
                        f"{len(policy_outcomes)} times."
                    ),
                    recommended_level="E1",
                    created_at=created_at,
                )
            )
        return signals

    def _recommendation_for_signal(
        self,
        signal: dict[str, Any],
        *,
        created_at: float | None,
        existing_proposal_ids: set[str],
    ) -> dict[str, Any] | None:
        if signal["signal_type"] == "false_positive" and signal["scope"] == "policy.review_required":
            proposal = self.state_store.record_improvement_proposal(
                proposal_type="recommend",
                level="E1",
                signal_id=str(signal["signal_id"]),
                title="Reduce review-required alert noise",
                evidence_summary=str(signal["summary"]),
                target="policies.review_loop.review_required",
                current_value="enabled",
                suggested_value="increase_threshold_or_route_to_digest",
                reason="Human overrides indicate the current review-required signal is too noisy.",
                risk="low",
                rollback_value="enabled",
                approval_required=False,
                patch={},
                created_at=created_at,
            )
            if proposal["proposal_id"] not in existing_proposal_ids:
                self._record_proposal_created(proposal, created_at=created_at)
            return proposal
        if signal["signal_type"] == "verification_failure":
            proposal = self.state_store.record_improvement_proposal(
                proposal_type="recommend",
                level="E1",
                signal_id=str(signal["signal_id"]),
                title="Review failed loop verification policy",
                evidence_summary=str(signal["summary"]),
                target=str(signal["scope"]),
                current_value="enabled",
                suggested_value="manual_policy_review",
                reason="Repeated verification failures need human review before config changes.",
                risk="medium",
                rollback_value="enabled",
                approval_required=False,
                patch={},
                created_at=created_at,
            )
            if proposal["proposal_id"] not in existing_proposal_ids:
                self._record_proposal_created(proposal, created_at=created_at)
            return proposal
        return None

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
                "risk": proposal["risk"],
            },
            created_at=created_at,
        )


def _outcome_id(outcome: dict[str, Any]) -> str:
    return f"{outcome['trace_id']}:{outcome['action_type']}:{outcome['verification_status']}"
