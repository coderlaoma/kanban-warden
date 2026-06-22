# Improvement Proposals v0.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first v0.3 loop-improvement slice: durable improvement signals, E1 recommendation proposals, approval records, and audit events.

**Architecture:** Keep policy inference in a new `src/kanban_warden/improvement.py` module. Add small persistence methods and tables to `WardenStateStore`, but do not put aggregation rules there. v0.3 E2 config patching remains a later PR unless this first slice is complete and verified.

**Tech Stack:** Python 3.10+, SQLite state store, pytest, ruff.

---

### Task 1: Store Improvement Signals and Audit Events

**Files:**
- Modify: `src/kanban_warden/state.py`
- Test: `tests/test_improvement.py`

- [ ] Write RED tests for `record_improvement_signal`, `recent_improvement_signals`, `record_improvement_audit`, and snapshot counts.
- [ ] Implement `improvement_signal` and `improvement_audit` tables plus state-store methods.
- [ ] Run `uv run pytest tests/test_improvement.py -q` until green.

### Task 2: Aggregate Signals from Loop Outcomes

**Files:**
- Create: `src/kanban_warden/improvement.py`
- Test: `tests/test_improvement.py`

- [ ] Write RED tests for false-positive and verification-failure aggregation from v0.2 loop traces/outcomes.
- [ ] Implement `ImprovementEngine.aggregate_signals()` using state-store recent trace/outcome APIs.
- [ ] Persist generated signals idempotently and include trace/outcome evidence.

### Task 3: Create E1 Recommendation Proposals

**Files:**
- Modify: `src/kanban_warden/improvement.py`
- Modify: `src/kanban_warden/state.py`
- Test: `tests/test_improvement.py`

- [ ] Write RED test that a false-positive signal creates an E1 `recommend` proposal without config mutation.
- [ ] Add `improvement_proposal` persistence and proposal generation.
- [ ] Include target, current value, suggested value, reason, risk, rollback, and approval requirement.

### Task 4: Record Human Approval and Audit

**Files:**
- Modify: `src/kanban_warden/improvement.py`
- Modify: `src/kanban_warden/state.py`
- Test: `tests/test_improvement.py`

- [ ] Write RED test for approval record and audit events.
- [ ] Add approval persistence and `ImprovementEngine.record_approval()`.
- [ ] Verify audit events include proposal id, actor, timestamp, and sanitized payload.

### Task 5: Verification and PR

- [ ] Run `uv run pytest`.
- [ ] Run `uv run ruff check src/kanban_warden/state.py src/kanban_warden/improvement.py tests/test_improvement.py`.
- [ ] Run `git diff --check`.
- [ ] Commit, push, open PR, and merge if checks allow.
