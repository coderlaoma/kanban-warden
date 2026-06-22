# Self-Improvement Package v0.4 Implementation Plan

**Goal:** Add the next safe E3 slice: validate approval scope and prepare an auditable code-change implementation package without creating branches, writing patches, pushing, deploying, or mutating source files.

**Architecture:** Extend `SelfImprovementEngine` so an approved E3 `code_change` proposal can be converted into inert implementation metadata. The package contains branch name, affected files, verification commands, commit message, and PR body. It is still planning data only.

**Tech Stack:** Python 3.10+, SQLite-backed state store, pytest, ruff, mypy.

---

### Task 1: Approval Scope Guardrails

**Files:**
- Modify: `src/kanban_warden/self_improvement.py`
- Test: `tests/test_self_improvement.py`

- [x] Write RED test that approval rejects verification commands that differ from the proposal draft.
- [x] Write RED test that approval rejects a branch prefix that does not match the proposal branch.
- [x] Implement branch-prefix and verification-command validation before approval/audit records are written.

### Task 2: Prepare Code-Change Package

**Files:**
- Modify: `src/kanban_warden/self_improvement.py`
- Test: `tests/test_self_improvement.py`

- [x] Write RED test that an approved E3 code-change proposal produces inert package metadata.
- [x] Write RED test that package preparation requires approval.
- [x] Implement `prepare_code_change_package()` and `code_change_package_prepared` audit records.
- [x] Keep package preparation side-effect-free except for audit persistence.

### Task 3: Verification and PR

- [x] Run `uv run ruff check .`.
- [x] Run `uv run mypy src`.
- [x] Run `uv run pytest`.
- [x] Run `git diff --check`.
- [x] Commit, push, open PR, and merge if checks allow.
