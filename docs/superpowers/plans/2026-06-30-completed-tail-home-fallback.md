# Completed Tail Home Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make completed-summary tail supplements match Hermes native truncation and keep working when a profile has no Kanban notify subscription rows.

**Architecture:** Keep Warden's existing outbox and `MessageSender` abstraction. Change only completed-tail thresholding and delivery target resolution: subscription rows still win, but a config-gated profile home-channel fallback can send to a bare platform target such as `feishu` when no subscription exists.

**Tech Stack:** Python 3.10+, pytest, mypy, ruff, Hermes plugin config YAML.

---

### Task 1: Completed Tail Threshold

**Files:**
- Modify: `kanban_warden/actions.py`
- Test: `tests/test_actions.py`

- [ ] Write a failing test that builds a completed summary with exactly 200 prefix characters and asserts only the tail after character 200 is delivered.
- [ ] Run the focused test and confirm it fails because the current implementation sends the 160-character tail.
- [ ] Change `_HERMES_NATIVE_COMPLETED_SUMMARY_CHARS` from `160` to `200`.
- [ ] Run the focused completed-tail tests and confirm they pass.

### Task 2: Config-Gated Home Fallback

**Files:**
- Modify: `kanban_warden/config.py`
- Modify: `kanban_warden/delivery.py`
- Modify: `kanban_warden/outbox.py`
- Test: `tests/test_actions.py`
- Docs: `README.md`, `examples/config.yaml`

- [ ] Add failing tests for `notifications.home_fallback_enabled` and `notifications.home_fallback_platforms` parsing.
- [ ] Add a failing outbox delivery test where the board has no `kanban_notify_subs`, fallback is enabled for `feishu`, and the message is sent to bare target `feishu`.
- [ ] Add config fields with safe defaults: fallback disabled and no platforms.
- [ ] Allow `SendTarget` to represent bare platform targets.
- [ ] In outbox delivery, use subscribers first; if none exist and fallback is enabled, send to configured bare platforms.
- [ ] Keep the existing retry behavior when fallback is disabled.
- [ ] Document the fallback as an explicit operational escape hatch, not routine delivery.

### Task 3: Verification, Release, Deploy

**Files:**
- Modify: `plugin.yaml`, `README.md`, release/version tests if present.

- [ ] Run targeted tests for completed tail and outbox fallback.
- [ ] Run full `uv run --group dev pytest`.
- [ ] Run `uv run --group dev ruff check .`.
- [ ] Run `uv run --group dev mypy kanban_warden`.
- [ ] Bump version to the next patch release.
- [ ] Commit, push, open PR, merge PR, create release tag.
- [ ] Update the Hermes `hairou-feishu` plugin checkout to the new release, restart gateway, and verify warden tick plus plugin version.
