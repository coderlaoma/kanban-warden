from __future__ import annotations

from pathlib import Path

import pytest

from kanban_warden.self_improvement import SelfImprovementEngine
from kanban_warden.state import WardenStateStore


def test_self_improvement_creates_e3_code_change_draft_from_policy_gap(
    tmp_path: Path,
) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    signal = store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101", "trace-117", "trace-203"],
        supporting_outcome_ids=["outcome-101", "outcome-117", "outcome-203"],
        summary="Running tasks emit many events without artifacts or state transitions.",
        recommended_level="E3",
        created_at=100.0,
    )

    drafts = SelfImprovementEngine(store).create_code_change_drafts(created_at=101.0)

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["proposal_type"] == "code_change"
    assert draft["level"] == "E3"
    assert draft["signal_id"] == signal["signal_id"]
    assert draft["target"] == "detector.high_activity_low_progress"
    assert draft["suggested_value"] == "draft_code_change_plan"
    assert draft["approval_required"] is True
    assert draft["patch"] == {
        "branch_name": f"warden/improve-{draft['proposal_id'].split(':')[-1]}-high-activity-low-progress",
        "affected_files": [
            "src/kanban_warden/board.py",
            "tests/test_board_events.py",
            "docs/loop-supervisor/v0.4-self-improvement.md",
        ],
        "verification_commands": [
            "uv run pytest tests/test_board_events.py -q",
            "uv run ruff check .",
            "uv run mypy src",
        ],
        "mutates_source": False,
    }
    assert store.recent_improvement_proposals()[0]["patch"] == draft["patch"]
    assert store.recent_improvement_audit()[0]["event_type"] == "proposal_created"


def test_self_improvement_records_e3_approval_scope(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    signal = store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    draft = SelfImprovementEngine(store).create_code_change_drafts(created_at=101.0)[0]

    approval = SelfImprovementEngine(store).record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to draft implementation only.",
        created_at=102.0,
    )

    audit = store.recent_improvement_audit()[0]
    assert draft["signal_id"] == signal["signal_id"]
    assert approval["decision"] == "approved"
    assert audit["event_type"] == "human_approved"
    assert audit["payload"] == {
        "approved_level": "E3",
        "allowed_repository": "coderlaoma/hermes-kanban-warden",
        "allowed_branch_prefix": "warden/improve-",
        "verification_commands": draft["patch"]["verification_commands"],
        "reason": "Approved to draft implementation only.",
    }


def test_self_improvement_rejects_expanded_approval_scope(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    draft = SelfImprovementEngine(store).create_code_change_drafts(created_at=101.0)[0]

    with pytest.raises(ValueError, match="verification commands must match"):
        SelfImprovementEngine(store).record_code_change_approval(
            proposal_id=draft["proposal_id"],
            actor="hairou",
            allowed_repository="coderlaoma/hermes-kanban-warden",
            allowed_branch_prefix="warden/improve-",
            verification_commands=["uv run pytest"],
            reason="Expanded verification scope.",
            created_at=102.0,
        )

    with pytest.raises(ValueError, match="branch prefix"):
        SelfImprovementEngine(store).record_code_change_approval(
            proposal_id=draft["proposal_id"],
            actor="hairou",
            allowed_repository="coderlaoma/hermes-kanban-warden",
            allowed_branch_prefix="feature/",
            verification_commands=draft["patch"]["verification_commands"],
            reason="Wrong branch prefix.",
            created_at=103.0,
        )


def test_self_improvement_prepares_approved_code_change_package(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    signal = store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101", "trace-117"],
        supporting_outcome_ids=["outcome-101", "outcome-117"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    engine.record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )

    package = engine.prepare_code_change_package(
        proposal_id=draft["proposal_id"],
        created_at=103.0,
    )

    assert package["proposal_id"] == draft["proposal_id"]
    assert package["branch_name"] == draft["patch"]["branch_name"]
    assert package["affected_files"] == draft["patch"]["affected_files"]
    assert package["verification_commands"] == draft["patch"]["verification_commands"]
    assert package["mutates_source"] is False
    assert package["commit_message"] == (
        "feat(warden): add high-activity-low-progress loop improvement\n\n"
        f"Proposal: {draft['proposal_id']}\n"
        f"Evidence: {signal['signal_id']}\n"
        "Verification: uv run pytest tests/test_board_events.py -q; uv run ruff check .; uv run mypy src"
    )
    assert "does not create branches or mutate source" in package["pull_request_body"]

    audit = store.recent_improvement_audit()[0]
    assert audit["event_type"] == "code_change_package_prepared"
    assert audit["payload"] == {
        "branch_name": draft["patch"]["branch_name"],
        "affected_files": draft["patch"]["affected_files"],
        "verification_commands": draft["patch"]["verification_commands"],
        "mutates_source": False,
    }


def test_self_improvement_package_requires_approval(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    draft = SelfImprovementEngine(store).create_code_change_drafts(created_at=101.0)[0]

    with pytest.raises(ValueError, match="approved"):
        SelfImprovementEngine(store).prepare_code_change_package(
            proposal_id=draft["proposal_id"],
            created_at=102.0,
        )


def test_self_improvement_records_passed_code_change_verification(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    signal = store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    engine.record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )
    engine.prepare_code_change_package(proposal_id=draft["proposal_id"], created_at=103.0)

    verification = engine.record_code_change_verification(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        command_results=[
            {
                "command": "uv run pytest tests/test_board_events.py -q",
                "exit_code": 0,
                "output": "12 passed",
            },
            {"command": "uv run ruff check .", "exit_code": 0, "output": "All checks passed!"},
            {"command": "uv run mypy src", "exit_code": 0, "output": "Success"},
        ],
        created_at=104.0,
    )

    assert verification["proposal_id"] == draft["proposal_id"]
    assert verification["status"] == "passed"
    assert verification["failed_commands"] == []
    audits = store.recent_improvement_audit(limit=2)
    assert [entry["event_type"] for entry in audits] == [
        "verification_passed",
        "verification_started",
    ]
    assert audits[0]["payload"] == {
        "status": "passed",
        "failed_commands": [],
        "command_results": verification["command_results"],
    }
    assert audits[1]["payload"] == {
        "commands": draft["patch"]["verification_commands"],
        "result_count": 3,
    }
    assert draft["signal_id"] == signal["signal_id"]


def test_self_improvement_records_failed_code_change_verification(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    engine.record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )
    engine.prepare_code_change_package(proposal_id=draft["proposal_id"], created_at=103.0)

    verification = engine.record_code_change_verification(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        command_results=[
            {
                "command": "uv run pytest tests/test_board_events.py -q",
                "exit_code": 1,
                "output": "failed",
            },
            {"command": "uv run ruff check .", "exit_code": 0, "output": "All checks passed!"},
            {"command": "uv run mypy src", "exit_code": 0, "output": "Success"},
        ],
        created_at=104.0,
    )

    assert verification["status"] == "failed"
    assert verification["failed_commands"] == ["uv run pytest tests/test_board_events.py -q"]
    assert store.recent_improvement_audit()[0]["event_type"] == "verification_failed"


def test_self_improvement_verification_rejects_unapproved_commands(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    engine.record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )
    engine.prepare_code_change_package(proposal_id=draft["proposal_id"], created_at=103.0)

    with pytest.raises(ValueError, match="verification commands must match"):
        engine.record_code_change_verification(
            proposal_id=draft["proposal_id"],
            actor="kanban-warden",
            command_results=[
                {"command": "uv run pytest", "exit_code": 0, "output": "83 passed"},
            ],
            created_at=104.0,
        )


def test_self_improvement_verification_requires_prepared_package(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    engine.record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )

    with pytest.raises(ValueError, match="package"):
        engine.record_code_change_verification(
            proposal_id=draft["proposal_id"],
            actor="kanban-warden",
            command_results=[
                {
                    "command": "uv run pytest tests/test_board_events.py -q",
                    "exit_code": 0,
                    "output": "12 passed",
                },
                {
                    "command": "uv run ruff check .",
                    "exit_code": 0,
                    "output": "All checks passed!",
                },
                {"command": "uv run mypy src", "exit_code": 0, "output": "Success"},
            ],
            created_at=103.0,
        )


def test_self_improvement_prepares_human_review_packet_after_verification(
    tmp_path: Path,
) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    signal = store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101", "trace-117"],
        supporting_outcome_ids=["outcome-101", "outcome-117"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    approval = engine.record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )
    package = engine.prepare_code_change_package(proposal_id=draft["proposal_id"], created_at=103.0)
    verification = engine.record_code_change_verification(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        command_results=[
            {
                "command": "uv run pytest tests/test_board_events.py -q",
                "exit_code": 0,
                "output": "12 passed",
            },
            {"command": "uv run ruff check .", "exit_code": 0, "output": "All checks passed!"},
            {"command": "uv run mypy src", "exit_code": 0, "output": "Success"},
        ],
        created_at=104.0,
    )

    packet = engine.prepare_human_review_packet(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        created_at=105.0,
    )

    assert packet == {
        "proposal_id": draft["proposal_id"],
        "proposal_summary": {
            "title": draft["title"],
            "target": "detector.high_activity_low_progress",
            "level": "E3",
            "risk": "medium",
            "reason": draft["reason"],
        },
        "evidence": {
            "signal_id": signal["signal_id"],
            "summary": "Need code detector.",
            "supporting_trace_ids": ["trace-101", "trace-117"],
            "supporting_outcome_ids": ["outcome-101", "outcome-117"],
        },
        "package_summary": {
            "branch_name": package["branch_name"],
            "affected_files": package["affected_files"],
            "mutates_source": False,
            "commit_message": package["commit_message"],
            "pull_request_title": package["pull_request_title"],
        },
        "verification": {
            "status": "passed",
            "failed_commands": [],
            "command_results": verification["command_results"],
        },
        "approval": {
            "approval_id": approval["approval_id"],
            "actor": "hairou",
            "reason": "Approved to prepare implementation package only.",
        },
        "links": {"branch": "", "pull_request": ""},
        "rollback_plan": "do_not_apply_generated_branch",
    }
    audit = store.recent_improvement_audit()[0]
    assert audit["event_type"] == "human_review_requested"
    assert audit["payload"] == {
        "proposal_id": draft["proposal_id"],
        "verification_status": "passed",
        "branch_name": package["branch_name"],
        "pull_request": "",
    }


def test_self_improvement_review_packet_requires_passed_verification(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    engine.record_code_change_approval(
        proposal_id=draft["proposal_id"],
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )
    engine.prepare_code_change_package(proposal_id=draft["proposal_id"], created_at=103.0)

    with pytest.raises(ValueError, match="passed verification"):
        engine.prepare_human_review_packet(
            proposal_id=draft["proposal_id"],
            actor="kanban-warden",
            created_at=104.0,
        )


def test_self_improvement_records_approved_human_review_decision(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)

    decision = SelfImprovementEngine(store).record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        created_at=106.0,
    )

    assert decision == {
        "proposal_id": draft["proposal_id"],
        "reviewer": "lead",
        "decision": "approved",
        "reason": "Reviewed evidence and verification.",
        "branch_url": "https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        "pull_request_url": "https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
    }
    audit = store.recent_improvement_audit()[0]
    assert audit["event_type"] == "human_review_approved"
    assert audit["payload"] == decision


def test_self_improvement_records_rejected_human_review_decision(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)

    decision = SelfImprovementEngine(store).record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="rejected",
        reason="Evidence is not strong enough.",
        created_at=106.0,
    )

    assert decision["decision"] == "rejected"
    assert decision["branch_url"] == ""
    assert decision["pull_request_url"] == ""
    assert store.recent_improvement_audit()[0]["event_type"] == "human_review_rejected"


def test_self_improvement_review_decision_requires_requested_review(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101"],
        supporting_outcome_ids=["outcome-101"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    draft = SelfImprovementEngine(store).create_code_change_drafts(created_at=101.0)[0]

    with pytest.raises(ValueError, match="human review request"):
        SelfImprovementEngine(store).record_human_review_decision(
            proposal_id=draft["proposal_id"],
            reviewer="lead",
            decision="approved",
            reason="No review packet yet.",
            created_at=102.0,
        )


def test_self_improvement_review_decision_rejects_unknown_decision(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)

    with pytest.raises(ValueError, match="approved or rejected"):
        SelfImprovementEngine(store).record_human_review_decision(
            proposal_id=draft["proposal_id"],
            reviewer="lead",
            decision="needs_changes",
            reason="Unsupported state.",
            created_at=106.0,
        )


def test_self_improvement_rejects_non_code_change_approval(tmp_path: Path) -> None:
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
        created_at=100.0,
    )

    with pytest.raises(ValueError, match="only E3 code-change proposals"):
        SelfImprovementEngine(store).record_code_change_approval(
            proposal_id=proposal["proposal_id"],
            actor="hairou",
            allowed_repository="coderlaoma/hermes-kanban-warden",
            allowed_branch_prefix="warden/improve-",
            verification_commands=["uv run pytest"],
            reason="Wrong level.",
            created_at=101.0,
        )


def _prepare_requested_human_review(store: WardenStateStore) -> dict[str, object]:
    store.record_improvement_signal(
        signal_type="policy_gap",
        scope="detector.high_activity_low_progress",
        severity="high",
        supporting_trace_ids=["trace-101", "trace-117"],
        supporting_outcome_ids=["outcome-101", "outcome-117"],
        summary="Need code detector.",
        recommended_level="E3",
        created_at=100.0,
    )
    engine = SelfImprovementEngine(store)
    draft = engine.create_code_change_drafts(created_at=101.0)[0]
    engine.record_code_change_approval(
        proposal_id=str(draft["proposal_id"]),
        actor="hairou",
        allowed_repository="coderlaoma/hermes-kanban-warden",
        allowed_branch_prefix="warden/improve-",
        verification_commands=draft["patch"]["verification_commands"],
        reason="Approved to prepare implementation package only.",
        created_at=102.0,
    )
    engine.prepare_code_change_package(proposal_id=str(draft["proposal_id"]), created_at=103.0)
    engine.record_code_change_verification(
        proposal_id=str(draft["proposal_id"]),
        actor="kanban-warden",
        command_results=[
            {
                "command": "uv run pytest tests/test_board_events.py -q",
                "exit_code": 0,
                "output": "12 passed",
            },
            {"command": "uv run ruff check .", "exit_code": 0, "output": "All checks passed!"},
            {"command": "uv run mypy src", "exit_code": 0, "output": "Success"},
        ],
        created_at=104.0,
    )
    engine.prepare_human_review_packet(
        proposal_id=str(draft["proposal_id"]),
        actor="kanban-warden",
        created_at=105.0,
    )
    return draft
