# Self-Improvement Verification v0.4 Implementation Plan

**Goal:** Add the next safe E3 slice: record verification results for prepared code-change packages without executing shell commands, creating branches, writing patches, pushing, deploying, or mutating source files.

**Architecture:** Extend `SelfImprovementEngine` with verification-result recording. The caller supplies command results after running the package commands elsewhere. The engine validates that the reported commands exactly match the prepared package and writes auditable `verification_started` plus `verification_passed` or `verification_failed` events.

**Tech Stack:** Python 3.10+, SQLite-backed state store, pytest, ruff, mypy.

---

### Task 1: Verification Result Audit

**Files:**
- Modify: `src/kanban_warden/self_improvement.py`
- Test: `tests/test_self_improvement.py`

- [x] Write RED test that successful command results record `verification_started` and `verification_passed`.
- [x] Write RED test that failed command results record `verification_failed` with failed commands.
- [x] Implement verification-result normalization and audit persistence.

### Task 2: Verification Guardrails

**Files:**
- Modify: `src/kanban_warden/self_improvement.py`
- Test: `tests/test_self_improvement.py`

- [x] Write RED test that verification command results must exactly match the prepared package commands.
- [x] Write RED test that verification cannot be recorded before the code-change package is prepared.
- [x] Keep verification recording side-effect-free except for audit persistence.

### Task 3: Verification and PR

- [x] Run `uv run ruff check .`.
- [x] Run `uv run mypy src`.
- [x] Run `uv run pytest`.
- [x] Run `git diff --check`.
- [x] Commit, push, open PR, and merge if checks allow.
