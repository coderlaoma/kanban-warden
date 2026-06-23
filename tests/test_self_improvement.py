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


def test_self_improvement_records_code_change_publication(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )

    publication = engine.record_code_change_publication(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        branch_name=draft["patch"]["branch_name"],
        branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        created_at=107.0,
    )

    assert publication == {
        "proposal_id": draft["proposal_id"],
        "branch_name": draft["patch"]["branch_name"],
        "branch_url": "https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        "pull_request_url": "https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
    }
    audits = store.recent_improvement_audit(limit=2)
    assert [entry["event_type"] for entry in audits] == ["branch_pushed", "mr_created"]
    assert audits[0]["payload"] == {
        "proposal_id": draft["proposal_id"],
        "branch_name": draft["patch"]["branch_name"],
        "branch_url": "https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
    }
    assert audits[1]["payload"] == publication


def test_self_improvement_publication_requires_approved_human_review(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)

    with pytest.raises(ValueError, match="approved human review"):
        SelfImprovementEngine(store).record_code_change_publication(
            proposal_id=draft["proposal_id"],
            actor="kanban-warden",
            branch_name=draft["patch"]["branch_name"],
            branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
            pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
            created_at=107.0,
        )


def test_self_improvement_publication_rejects_branch_mismatch(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )

    with pytest.raises(ValueError, match="branch name"):
        engine.record_code_change_publication(
            proposal_id=draft["proposal_id"],
            actor="kanban-warden",
            branch_name="feature/unapproved",
            branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/feature/unapproved",
            pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
            created_at=107.0,
        )


def test_self_improvement_publication_requires_pull_request_url(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )

    with pytest.raises(ValueError, match="pull request"):
        engine.record_code_change_publication(
            proposal_id=draft["proposal_id"],
            actor="kanban-warden",
            branch_name=draft["patch"]["branch_name"],
            branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
            pull_request_url="",
            created_at=107.0,
        )


def test_self_improvement_records_external_merge_result(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )
    engine.record_code_change_publication(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        branch_name=draft["patch"]["branch_name"],
        branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        created_at=107.0,
    )

    merge = engine.record_code_change_merge(
        proposal_id=draft["proposal_id"],
        actor="release-bot",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        base_branch="main",
        merge_commit_sha="abc1234",
        merged_by="lead",
        merged_at="2026-06-23T10:00:00Z",
        created_at=108.0,
    )

    assert merge == {
        "proposal_id": draft["proposal_id"],
        "pull_request_url": "https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        "base_branch": "main",
        "merge_commit_sha": "abc1234",
        "merged_by": "lead",
        "merged_at": "2026-06-23T10:00:00Z",
    }
    audit = store.recent_improvement_audit()[0]
    assert audit["event_type"] == "mr_merged"
    assert audit["payload"] == merge


def test_self_improvement_merge_requires_publication(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )

    with pytest.raises(ValueError, match="publication"):
        engine.record_code_change_merge(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
            base_branch="main",
            merge_commit_sha="abc1234",
            merged_by="lead",
            merged_at="2026-06-23T10:00:00Z",
            created_at=108.0,
        )


def test_self_improvement_prepares_deployment_plan_after_merge(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_merged_code_change(store)
    engine = SelfImprovementEngine(store)

    plan = engine.prepare_code_change_deployment_plan(
        proposal_id=draft["proposal_id"],
        actor="release-bot",
        target_profiles=["hermes-dev"],
        commit_sha="abc1234",
        plugin_version="0.4.0+abc1234",
        config_changes={"policies": "unchanged"},
        restart_commands=["systemctl reload hermes-kanban-warden"],
        health_check_commands=["kanban-warden --profile hermes-dev dry-run"],
        monitor_window="30m",
        rollback_commands=["git revert abc1234", "systemctl reload hermes-kanban-warden"],
        created_at=108.0,
    )

    assert plan == {
        "proposal_id": draft["proposal_id"],
        "target_profiles": ["hermes-dev"],
        "commit_sha": "abc1234",
        "plugin_version": "0.4.0+abc1234",
        "config_changes": {"policies": "unchanged"},
        "restart_commands": ["systemctl reload hermes-kanban-warden"],
        "health_check_commands": ["kanban-warden --profile hermes-dev dry-run"],
        "monitor_window": "30m",
        "rollback_commands": ["git revert abc1234", "systemctl reload hermes-kanban-warden"],
        "mutates_runtime": False,
    }
    audit = store.recent_improvement_audit()[0]
    assert audit["event_type"] == "deployment_plan_prepared"
    assert audit["payload"] == plan


def test_self_improvement_deployment_plan_requires_merge(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )
    engine.record_code_change_publication(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        branch_name=draft["patch"]["branch_name"],
        branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        created_at=107.0,
    )

    with pytest.raises(ValueError, match="merge"):
        engine.prepare_code_change_deployment_plan(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            target_profiles=["hermes-dev"],
            commit_sha="abc1234",
            plugin_version="0.4.0+abc1234",
            config_changes={"policies": "unchanged"},
            restart_commands=["systemctl reload hermes-kanban-warden"],
            health_check_commands=["kanban-warden --profile hermes-dev dry-run"],
            monitor_window="30m",
            rollback_commands=["git revert abc1234"],
            created_at=109.0,
        )


def test_self_improvement_records_external_deployment_result(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_deployment_planned_code_change(store)
    engine = SelfImprovementEngine(store)

    deployment = engine.record_code_change_deployment(
        proposal_id=draft["proposal_id"],
        actor="release-bot",
        target_profiles=["hermes-dev"],
        commit_sha="abc1234",
        plugin_version="0.4.0+abc1234",
        config_changes={"policies": "unchanged"},
        restart_commands=["systemctl reload hermes-kanban-warden"],
        health_check_result={"status": "passed", "summary": "dry-run healthy"},
        monitor_window="30m",
        rollback_commands=["git revert abc1234", "systemctl reload hermes-kanban-warden"],
        status="succeeded",
        created_at=110.0,
    )

    assert deployment == {
        "proposal_id": draft["proposal_id"],
        "status": "succeeded",
        "target_profiles": ["hermes-dev"],
        "commit_sha": "abc1234",
        "plugin_version": "0.4.0+abc1234",
        "config_changes": {"policies": "unchanged"},
        "restart_commands": ["systemctl reload hermes-kanban-warden"],
        "health_check_result": {"status": "passed", "summary": "dry-run healthy"},
        "monitor_window": "30m",
        "rollback_commands": ["git revert abc1234", "systemctl reload hermes-kanban-warden"],
    }
    audits = store.recent_improvement_audit(limit=2)
    assert [entry["event_type"] for entry in audits] == [
        "deployment_started",
        "deployment_succeeded",
    ]
    assert audits[0]["payload"] == {
        "proposal_id": draft["proposal_id"],
        "target_profiles": ["hermes-dev"],
        "commit_sha": "abc1234",
        "plugin_version": "0.4.0+abc1234",
        "restart_commands": ["systemctl reload hermes-kanban-warden"],
        "monitor_window": "30m",
    }
    assert audits[1]["payload"] == deployment


def test_self_improvement_deployment_requires_deployment_plan(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_merged_code_change(store)

    with pytest.raises(ValueError, match="deployment plan"):
        SelfImprovementEngine(store).record_code_change_deployment(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            target_profiles=["hermes-dev"],
            commit_sha="abc1234",
            plugin_version="0.4.0+abc1234",
            config_changes={"policies": "unchanged"},
            restart_commands=["systemctl reload hermes-kanban-warden"],
            health_check_result={"status": "passed"},
            monitor_window="30m",
            rollback_commands=["git revert abc1234"],
            status="succeeded",
            created_at=108.0,
        )


def test_self_improvement_deployment_rejects_unknown_status(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_deployment_planned_code_change(store)

    with pytest.raises(ValueError, match="deployment status"):
        SelfImprovementEngine(store).record_code_change_deployment(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            target_profiles=["hermes-dev"],
            commit_sha="abc1234",
            plugin_version="0.4.0+abc1234",
            config_changes={},
            restart_commands=["systemctl reload hermes-kanban-warden"],
            health_check_result={"status": "passed"},
            monitor_window="30m",
            rollback_commands=["git revert abc1234"],
            status="running",
            created_at=108.0,
        )


def test_self_improvement_deployment_must_match_prepared_plan(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_deployment_planned_code_change(store)

    with pytest.raises(ValueError, match="prepared plan"):
        SelfImprovementEngine(store).record_code_change_deployment(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            target_profiles=["hermes-dev"],
            commit_sha="different-sha",
            plugin_version="0.4.0+abc1234",
            config_changes={"policies": "unchanged"},
            restart_commands=["systemctl reload hermes-kanban-warden"],
            health_check_result={"status": "passed"},
            monitor_window="30m",
            rollback_commands=["git revert abc1234"],
            status="succeeded",
            created_at=110.0,
        )


def test_self_improvement_records_external_rollback_result(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_deployed_code_change(store)
    engine = SelfImprovementEngine(store)

    rollback = engine.record_code_change_rollback(
        proposal_id=draft["proposal_id"],
        actor="release-bot",
        reason="Post-deploy health check regressed.",
        target_profiles=["hermes-dev"],
        restored_commit_sha="prev1234",
        restored_plugin_version="0.3.9+prev1234",
        rollback_commands=["git revert abc1234", "systemctl reload hermes-kanban-warden"],
        health_check_result={"status": "passed", "summary": "previous version healthy"},
        created_at=111.0,
    )

    assert rollback == {
        "proposal_id": draft["proposal_id"],
        "reason": "Post-deploy health check regressed.",
        "target_profiles": ["hermes-dev"],
        "restored_commit_sha": "prev1234",
        "restored_plugin_version": "0.3.9+prev1234",
        "rollback_commands": ["git revert abc1234", "systemctl reload hermes-kanban-warden"],
        "health_check_result": {"status": "passed", "summary": "previous version healthy"},
    }
    audits = store.recent_improvement_audit(limit=2)
    assert [entry["event_type"] for entry in audits] == [
        "rollback_started",
        "rollback_succeeded",
    ]
    assert audits[0]["payload"] == {
        "proposal_id": draft["proposal_id"],
        "reason": "Post-deploy health check regressed.",
        "target_profiles": ["hermes-dev"],
        "rollback_commands": ["git revert abc1234", "systemctl reload hermes-kanban-warden"],
    }
    assert audits[1]["payload"] == rollback


def test_self_improvement_rollback_requires_deployment_record(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )
    engine.record_code_change_publication(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        branch_name=draft["patch"]["branch_name"],
        branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        created_at=107.0,
    )

    with pytest.raises(ValueError, match="deployment"):
        engine.record_code_change_rollback(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            reason="No deployment record.",
            target_profiles=["hermes-dev"],
            restored_commit_sha="prev1234",
            restored_plugin_version="0.3.9+prev1234",
            rollback_commands=["git revert abc1234"],
            health_check_result={"status": "passed"},
            created_at=109.0,
        )


def test_self_improvement_rollback_must_match_prepared_plan(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_deployed_code_change(store)

    with pytest.raises(ValueError, match="prepared plan"):
        SelfImprovementEngine(store).record_code_change_rollback(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            reason="Unexpected rollback command.",
            target_profiles=["hermes-dev"],
            restored_commit_sha="prev1234",
            restored_plugin_version="0.3.9+prev1234",
            rollback_commands=["rm -rf /opt/hermes"],
            health_check_result={"status": "passed"},
            created_at=111.0,
        )


def test_self_improvement_records_external_monitor_summary(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_deployed_code_change(store)
    engine = SelfImprovementEngine(store)

    summary = engine.record_post_deploy_monitoring(
        proposal_id=draft["proposal_id"],
        actor="release-bot",
        monitor_window="30m",
        target_profiles=["hermes-dev"],
        metrics={
            "new_detector_trigger_count": 4,
            "false_positive_rate": 0.0,
            "verification_failure_rate": 0.1,
            "manual_override_rate": 0.0,
            "gateway_error_count": 1,
        },
        regressions=["gateway error count increased by 1"],
        recommendation="rollback_if_repeated",
        created_at=111.0,
    )

    assert summary == {
        "proposal_id": draft["proposal_id"],
        "monitor_window": "30m",
        "target_profiles": ["hermes-dev"],
        "metrics": {
            "new_detector_trigger_count": 4,
            "false_positive_rate": 0.0,
            "verification_failure_rate": 0.1,
            "manual_override_rate": 0.0,
            "gateway_error_count": 1,
        },
        "regressions": ["gateway error count increased by 1"],
        "recommendation": "rollback_if_repeated",
    }
    audit = store.recent_improvement_audit()[0]
    assert audit["event_type"] == "post_deploy_monitor_recorded"
    assert audit["payload"] == summary


def test_self_improvement_monitoring_requires_deployment_record(tmp_path: Path) -> None:
    store = WardenStateStore(tmp_path / "state.db")
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=draft["proposal_id"],
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )
    engine.record_code_change_publication(
        proposal_id=draft["proposal_id"],
        actor="kanban-warden",
        branch_name=draft["patch"]["branch_name"],
        branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        created_at=107.0,
    )

    with pytest.raises(ValueError, match="deployment"):
        engine.record_post_deploy_monitoring(
            proposal_id=draft["proposal_id"],
            actor="release-bot",
            monitor_window="30m",
            target_profiles=["hermes-dev"],
            metrics={"gateway_error_count": 0},
            regressions=[],
            recommendation="continue_monitoring",
            created_at=109.0,
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


def _prepare_deployed_code_change(store: WardenStateStore) -> dict[str, object]:
    draft = _prepare_deployment_planned_code_change(store)
    engine = SelfImprovementEngine(store)
    engine.record_code_change_deployment(
        proposal_id=str(draft["proposal_id"]),
        actor="release-bot",
        target_profiles=["hermes-dev"],
        commit_sha="abc1234",
        plugin_version="0.4.0+abc1234",
        config_changes={"policies": "unchanged"},
        restart_commands=["systemctl reload hermes-kanban-warden"],
        health_check_result={"status": "failed", "summary": "health check regressed"},
        monitor_window="30m",
        rollback_commands=["git revert abc1234", "systemctl reload hermes-kanban-warden"],
        status="failed",
        created_at=110.0,
    )
    return draft


def _prepare_deployment_planned_code_change(store: WardenStateStore) -> dict[str, object]:
    draft = _prepare_merged_code_change(store)
    SelfImprovementEngine(store).prepare_code_change_deployment_plan(
        proposal_id=str(draft["proposal_id"]),
        actor="release-bot",
        target_profiles=["hermes-dev"],
        commit_sha="abc1234",
        plugin_version="0.4.0+abc1234",
        config_changes={"policies": "unchanged"},
        restart_commands=["systemctl reload hermes-kanban-warden"],
        health_check_commands=["kanban-warden --profile hermes-dev dry-run"],
        monitor_window="30m",
        rollback_commands=["git revert abc1234", "systemctl reload hermes-kanban-warden"],
        created_at=108.0,
    )
    return draft


def _prepare_merged_code_change(store: WardenStateStore) -> dict[str, object]:
    draft = _prepare_requested_human_review(store)
    engine = SelfImprovementEngine(store)
    engine.record_human_review_decision(
        proposal_id=str(draft["proposal_id"]),
        reviewer="lead",
        decision="approved",
        reason="Reviewed evidence and verification.",
        created_at=106.0,
    )
    engine.record_code_change_publication(
        proposal_id=str(draft["proposal_id"]),
        actor="kanban-warden",
        branch_name=str(draft["patch"]["branch_name"]),
        branch_url="https://github.com/coderlaoma/hermes-kanban-warden/tree/warden/improve",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        created_at=107.0,
    )
    engine.record_code_change_merge(
        proposal_id=str(draft["proposal_id"]),
        actor="release-bot",
        pull_request_url="https://github.com/coderlaoma/hermes-kanban-warden/pull/99",
        base_branch="main",
        merge_commit_sha="abc1234",
        merged_by="lead",
        merged_at="2026-06-23T10:00:00Z",
        created_at=108.0,
    )
    return draft
