from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kanban_warden.config_patch import ConfigPatchError
from kanban_warden.improvement import ImprovementEngine
from kanban_warden.state import WardenStateStore


def _write_config(path: Path) -> None:
    path.write_text(
        """
kanban_warden:
  limits:
    max_retries: 2
    stale_claim_seconds: 3600
  notifications:
    enabled: true
  secrets:
    token: keep-me
""".lstrip(),
        encoding="utf-8",
    )


def test_prepare_config_patch_accepts_whitelisted_policy_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Raise max retries",
        evidence_summary="Retries are exhausted too early.",
        target="kanban_warden.limits.max_retries",
        current_value="2",
        suggested_value="3",
        reason="Observed retries were still producing progress.",
        risk="low",
        rollback_value="2",
        approval_required=True,
        patch={"kanban_warden.limits.max_retries": 3},
        created_at=100.0,
    )

    prepared = ImprovementEngine(store).prepare_config_patch(
        proposal_id=proposal["proposal_id"],
        config_path=config_path,
        created_at=101.0,
    )

    assert prepared["proposal_id"] == proposal["proposal_id"]
    assert prepared["target_file"] == str(config_path)
    assert prepared["changes"] == [
        {
            "path": "kanban_warden.limits.max_retries",
            "before": 2,
            "after": 3,
        }
    ]
    assert store.recent_improvement_audit()[0]["event_type"] == "config_patch_prepared"
    assert "max_retries: 2" in config_path.read_text(encoding="utf-8")


def test_prepare_config_patch_rejects_non_whitelisted_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Change secret",
        evidence_summary="Unsafe proposal.",
        target="kanban_warden.secrets.token",
        current_value="keep-me",
        suggested_value="replace-me",
        reason="This must never be patched by the warden.",
        risk="high",
        rollback_value="keep-me",
        approval_required=True,
        patch={"kanban_warden.secrets.token": "replace-me"},
        created_at=100.0,
    )

    with pytest.raises(ConfigPatchError, match="not whitelisted"):
        ImprovementEngine(store).prepare_config_patch(
            proposal_id=proposal["proposal_id"],
            config_path=config_path,
            created_at=101.0,
        )

    assert store.recent_improvement_audit() == []


def test_apply_config_patch_requires_approval(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Raise max retries",
        evidence_summary="Retries are exhausted too early.",
        target="kanban_warden.limits.max_retries",
        current_value="2",
        suggested_value="3",
        reason="Observed retries were still producing progress.",
        risk="low",
        rollback_value="2",
        approval_required=True,
        patch={"kanban_warden.limits.max_retries": 3},
        created_at=100.0,
    )

    with pytest.raises(ConfigPatchError, match="approved"):
        ImprovementEngine(store).apply_config_patch(
            proposal_id=proposal["proposal_id"],
            config_path=config_path,
            created_at=102.0,
        )

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["kanban_warden"]["limits"]["max_retries"] == 2


def test_apply_config_patch_rejects_approved_non_low_risk_proposal(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Raise max retries",
        evidence_summary="Retries are exhausted too early.",
        target="kanban_warden.limits.max_retries",
        current_value="2",
        suggested_value="3",
        reason="Observed retries were still producing progress.",
        risk="medium",
        rollback_value="2",
        approval_required=True,
        patch={"kanban_warden.limits.max_retries": 3},
        created_at=100.0,
    )
    engine = ImprovementEngine(store)
    engine.record_approval(
        proposal_id=proposal["proposal_id"],
        actor="hairou",
        decision="approved",
        reason="approved but still not low risk",
        created_at=101.0,
    )

    with pytest.raises(ConfigPatchError, match="low-risk"):
        engine.apply_config_patch(
            proposal_id=proposal["proposal_id"],
            config_path=config_path,
            created_at=102.0,
        )

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["kanban_warden"]["limits"]["max_retries"] == 2


def test_apply_config_patch_writes_yaml_backup_and_audit(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Raise max retries",
        evidence_summary="Retries are exhausted too early.",
        target="kanban_warden.limits.max_retries",
        current_value="2",
        suggested_value="3",
        reason="Observed retries were still producing progress.",
        risk="low",
        rollback_value="2",
        approval_required=True,
        patch={"kanban_warden.limits.max_retries": 3},
        created_at=100.0,
    )
    engine = ImprovementEngine(store)
    engine.record_approval(
        proposal_id=proposal["proposal_id"],
        actor="hairou",
        decision="approved",
        reason="safe",
        created_at=101.0,
    )

    applied = engine.apply_config_patch(
        proposal_id=proposal["proposal_id"],
        config_path=config_path,
        created_at=102.0,
    )

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["kanban_warden"]["limits"]["max_retries"] == 3
    assert applied["backup_path"].endswith("config.yaml.102000.bak")
    assert Path(applied["backup_path"]).read_text(encoding="utf-8").count("max_retries: 2") == 1
    assert store.recent_improvement_audit()[0]["event_type"] == "config_patch_applied"


def test_compare_config_patch_reports_before_after_without_writing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Raise max retries",
        evidence_summary="Retries are exhausted too early.",
        target="kanban_warden.limits.max_retries",
        current_value="2",
        suggested_value="3",
        reason="Observed retries were still producing progress.",
        risk="low",
        rollback_value="2",
        approval_required=True,
        patch={"kanban_warden.limits.max_retries": 3},
        created_at=100.0,
    )

    comparison = ImprovementEngine(store).compare_config_patch(
        proposal_id=proposal["proposal_id"],
        config_path=config_path,
        created_at=102.0,
    )

    assert comparison == {
        "proposal_id": proposal["proposal_id"],
        "before": {"kanban_warden.limits.max_retries": 2},
        "after": {"kanban_warden.limits.max_retries": 3},
        "changed_policies": ["kanban_warden.limits.max_retries"],
        "requires_stricter_approval": False,
    }
    assert [entry["event_type"] for entry in store.recent_improvement_audit(limit=2)] == [
        "dry_run_after",
        "dry_run_before",
    ]
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["kanban_warden"]["limits"]["max_retries"] == 2


def test_rollback_config_patch_restores_rollback_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    store = WardenStateStore(tmp_path / "state.db")
    proposal = store.record_improvement_proposal(
        proposal_type="config_change",
        level="E2",
        signal_id="sig-1",
        title="Raise max retries",
        evidence_summary="Retries are exhausted too early.",
        target="kanban_warden.limits.max_retries",
        current_value="2",
        suggested_value="3",
        reason="Observed retries were still producing progress.",
        risk="low",
        rollback_value="2",
        approval_required=True,
        patch={"kanban_warden.limits.max_retries": 3},
        created_at=100.0,
    )
    engine = ImprovementEngine(store)
    engine.record_approval(
        proposal_id=proposal["proposal_id"],
        actor="hairou",
        decision="approved",
        reason="safe",
        created_at=101.0,
    )
    engine.apply_config_patch(
        proposal_id=proposal["proposal_id"],
        config_path=config_path,
        created_at=102.0,
    )

    rollback = engine.rollback_config_patch(
        proposal_id=proposal["proposal_id"],
        config_path=config_path,
        created_at=103.0,
    )

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["kanban_warden"]["limits"]["max_retries"] == 2
    assert rollback["changes"] == [
        {
            "path": "kanban_warden.limits.max_retries",
            "before": 3,
            "after": 2,
        }
    ]
    assert store.recent_improvement_audit()[0]["event_type"] == "rollback_prepared"
