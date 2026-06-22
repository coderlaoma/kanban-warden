# Self-Improvement Review Decision v0.4 Implementation Plan

**Goal:** Add the next safe E3 slice: record human review decisions after a review packet is requested, without creating branches, opening PRs, pushing, deploying, or mutating source files.

**Architecture:** Extend `SelfImprovementEngine` with review-decision recording. The method accepts a reviewer, an `approved` or `rejected` decision, a reason, and optional external links produced elsewhere. It validates that a human review request exists and writes immutable audit metadata only.

**Tech Stack:** Python 3.10+, SQLite-backed state store, pytest, ruff, mypy.

---

### Task 1: Human Review Decision Audit

**Files:**
- Modify: `src/kanban_warden/self_improvement.py`
- Test: `tests/test_self_improvement.py`

- [x] Write RED test that approved review decisions are recorded as `human_review_approved`.
- [x] Write RED test that rejected review decisions are recorded as `human_review_rejected`.
- [x] Implement `record_human_review_decision()` as audit-only metadata.

### Task 2: Review Decision Guardrails

**Files:**
- Modify: `src/kanban_warden/self_improvement.py`
- Test: `tests/test_self_improvement.py`

- [x] Write RED test that a review decision requires a prior `human_review_requested` audit.
- [x] Write RED test that only `approved` and `rejected` decisions are accepted.
- [x] Keep decision recording side-effect-free except for audit persistence.

### Task 3: Verification and PR

- [x] Run `uv run ruff check .`.
- [x] Run `uv run mypy src`.
- [x] Run `uv run pytest`.
- [x] Run `git diff --check`.
- [x] Commit, push, open PR, and merge if checks allow.
