# Config Patch v0.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add approved E2 config patch preparation, application, rollback, and a deterministic before/after comparison summary for kanban-warden policies.

**Architecture:** Keep file mutation in a new `src/kanban_warden/config_patch.py` module. `ImprovementEngine` will orchestrate proposal preparation and approval checks, while config patch code handles YAML loading, whitelist validation, backup files, patch application, rollback, and diff summaries.

**Tech Stack:** Python 3.10+, PyYAML, pathlib, pytest, ruff.

---

### Task 1: Whitelisted Patch Preparation

**Files:**
- Create: `src/kanban_warden/config_patch.py`
- Modify: `src/kanban_warden/improvement.py`
- Test: `tests/test_config_patch.py`

- [ ] Write RED tests that a patch for `kanban_warden.limits.max_retries` is accepted and a patch for a secret/plugin path is rejected.
- [ ] Implement whitelist validation and dot-path patch planning.
- [ ] Record `config_patch_prepared` audit for valid proposals.

### Task 2: Approved Patch Application With Backup

**Files:**
- Modify: `src/kanban_warden/config_patch.py`
- Modify: `src/kanban_warden/improvement.py`
- Test: `tests/test_config_patch.py`

- [ ] Write RED test that an approved E2 proposal applies YAML changes and creates a backup.
- [ ] Implement approval lookup, backup creation, YAML write, and `config_patch_applied` audit.
- [ ] Ensure unapproved proposals cannot mutate files.

### Task 3: Rollback and Before/After Summary

**Files:**
- Modify: `src/kanban_warden/config_patch.py`
- Modify: `src/kanban_warden/improvement.py`
- Test: `tests/test_config_patch.py`

- [ ] Write RED test that rollback restores values from proposal rollback data.
- [ ] Write RED test for deterministic before/after comparison from patch diff.
- [ ] Implement rollback write, `rollback_prepared` audit, and comparison summary.

### Task 4: Verification and PR

- [ ] Run `uv run pytest`.
- [ ] Run `uv run ruff check src/kanban_warden/config_patch.py src/kanban_warden/improvement.py tests/test_config_patch.py`.
- [ ] Run `git diff --check`.
- [ ] Commit, push, open PR, and merge if checks allow.
