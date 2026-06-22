from __future__ import annotations

from pathlib import Path

from kanban_warden.improvement import ImprovementEngine
from kanban_warden.state import WardenStateStore


def test_state_store_records_improvement_signal_and_audit(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")

    signal = store.record_improvement_signal(
        signal_type="false_positive",
        scope="policies.review_loop.reviewer_timeout",
        severity="medium",
        supporting_trace_ids=["trace-1", "trace-2"],
        supporting_outcome_ids=["trace-1:create_reviewer", "trace-2:create_reviewer"],
        summary="Reviewer timeout alerts were repeatedly ignored.",
        recommended_level="E1",
        created_at=123.0,
    )
    audit = store.record_improvement_audit(
        subject_id=signal["signal_id"],
        event_type="signal_created",
        actor="kanban-warden",
        payload={"signal_type": "false_positive", "traces": 2},
        created_at=124.0,
    )

    snapshot = store.snapshot()
    recent_signals = store.recent_improvement_signals()
    recent_audit = store.recent_improvement_audit()

    assert signal["signal_id"].startswith("sig:false_positive:")
    assert audit["event_type"] == "signal_created"
    assert snapshot["improvement_signal_count"] == 1
    assert snapshot["improvement_audit_count"] == 1
    assert recent_signals[0]["supporting_trace_ids"] == ["trace-1", "trace-2"]
    assert recent_signals[0]["supporting_outcome_ids"] == [
        "trace-1:create_reviewer",
        "trace-2:create_reviewer",
    ]
    assert recent_audit[0]["payload"] == {"signal_type": "false_positive", "traces": 2}


def test_improvement_engine_aggregates_false_positive_signal(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    expected_trace_ids: list[str] = []
    for index in range(3):
        trace = store.record_loop_trace(
            board_name="default",
            task_id=f"review-{index}",
            profile_name="tester",
            loop_state="waiting_for_review",
            observed_facts={"reason": "review-required"},
            matched_policy="review_required",
            decision="create_reviewer",
            confidence="high",
            planned_action={"kind": "create_reviewer"},
            verification_contract={"success": "reviewer_card_exists"},
            created_at=100.0 + index,
        )
        expected_trace_ids.append(str(trace["trace_id"]))
        store.record_loop_outcome(
            trace_id=trace["trace_id"],
            board_name="default",
            task_id=f"review-{index}",
            action_type="create_reviewer",
            status="ignored",
            verification_status="human_override",
            human_override=True,
            override_reason="too noisy",
            created_at=200.0 + index,
        )

    signals = ImprovementEngine(store).aggregate_signals(created_at=300.0)

    assert len(signals) == 1
    assert signals[0]["signal_type"] == "false_positive"
    assert signals[0]["scope"] == "policy.review_required"
    assert signals[0]["recommended_level"] == "E1"
    assert signals[0]["supporting_trace_ids"] == expected_trace_ids
    assert store.snapshot()["improvement_signal_count"] == 1


def test_improvement_engine_aggregates_verification_failure_signal(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    expected_outcome_ids: list[str] = []
    for index in range(2):
        trace = store.record_loop_trace(
            board_name="default",
            task_id=f"retry-{index}",
            profile_name="tester",
            loop_state="no_progress",
            observed_facts={"reason": "stale running task detected"},
            matched_policy="bounded_recovery",
            decision="retry",
            confidence="medium",
            planned_action={"kind": "retry"},
            verification_contract={"success": "new_progress_event_or_status_change"},
            created_at=110.0 + index,
        )
        expected_outcome_ids.append(f"{trace['trace_id']}:retry:failed")
        store.record_loop_outcome(
            trace_id=trace["trace_id"],
            board_name="default",
            task_id=f"retry-{index}",
            action_type="retry",
            status="applied",
            verification_status="failed",
            created_at=210.0 + index,
        )

    signals = ImprovementEngine(store).aggregate_signals(created_at=300.0)

    assert len(signals) == 1
    assert signals[0]["signal_type"] == "verification_failure"
    assert signals[0]["scope"] == "policy.bounded_recovery.retry"
    assert signals[0]["severity"] == "high"
    assert signals[0]["supporting_outcome_ids"] == expected_outcome_ids


def test_improvement_engine_creates_recommendation_proposal_from_false_positive_signal(
    tmp_path: Path,
) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    signal = store.record_improvement_signal(
        signal_type="false_positive",
        scope="policy.review_required",
        severity="medium",
        supporting_trace_ids=["trace-1", "trace-2", "trace-3"],
        supporting_outcome_ids=["outcome-1", "outcome-2", "outcome-3"],
        summary="Policy review_required produced 3 human-overridden actions.",
        recommended_level="E1",
        created_at=300.0,
    )

    proposals = ImprovementEngine(store).create_recommendation_proposals(created_at=301.0)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["proposal_id"].startswith("prop:recommend:")
    assert proposal["proposal_type"] == "recommend"
    assert proposal["level"] == "E1"
    assert proposal["signal_id"] == signal["signal_id"]
    assert proposal["target"] == "policies.review_loop.review_required"
    assert proposal["current_value"] == "enabled"
    assert proposal["suggested_value"] == "increase_threshold_or_route_to_digest"
    assert proposal["approval_required"] is False
    assert proposal["patch"] == {}
    assert store.recent_improvement_audit()[0]["event_type"] == "proposal_created"
    assert store.snapshot()["improvement_proposal_count"] == 1


def test_improvement_engine_does_not_duplicate_existing_recommendation_audit(
    tmp_path: Path,
) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="false_positive",
        scope="policy.review_required",
        severity="medium",
        supporting_trace_ids=["trace-1", "trace-2", "trace-3"],
        supporting_outcome_ids=["outcome-1", "outcome-2", "outcome-3"],
        summary="Policy review_required produced 3 human-overridden actions.",
        recommended_level="E1",
        created_at=300.0,
    )
    engine = ImprovementEngine(store)

    first = engine.create_recommendation_proposals(created_at=301.0)
    second = engine.create_recommendation_proposals(created_at=302.0)

    snapshot = store.snapshot()
    assert second[0]["proposal_id"] == first[0]["proposal_id"]
    assert snapshot["improvement_proposal_count"] == 1
    assert snapshot["improvement_audit_count"] == 1


def test_improvement_engine_records_human_approval_and_audit(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Raise retry threshold",
        evidence_summary="Retries are exhausted too early.",
        target="limits.max_retries",
        current_value="2",
        suggested_value="3",
        reason="Observed retries were still producing progress.",
        risk="low",
        rollback_value="2",
        approval_required=True,
        patch={"kanban_warden.limits.max_retries": 3},
        created_at=400.0,
    )

    approval = ImprovementEngine(store).record_approval(
        proposal_id=proposal["proposal_id"],
        actor="hairou",
        decision="approved",
        reason="Safe low-risk config change.",
        created_at=401.0,
    )

    snapshot = store.snapshot()
    audit = store.recent_improvement_audit()
    approvals = store.recent_improvement_approvals()
    assert approval["proposal_id"] == proposal["proposal_id"]
    assert approval["actor"] == "hairou"
    assert approval["decision"] == "approved"
    assert snapshot["improvement_approval_count"] == 1
    assert approvals[0]["reason"] == "Safe low-risk config change."
    assert audit[0]["subject_id"] == proposal["proposal_id"]
    assert audit[0]["event_type"] == "human_approved"
    assert audit[0]["payload"] == {"decision": "approved", "reason": "Safe low-risk config change."}
