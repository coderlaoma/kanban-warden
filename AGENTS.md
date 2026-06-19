# AGENTS.md

Guidance for generic AI coding agents working in this repository.

## Project background and mission

`hermes-kanban-warden` is the human-facing display name for this Hermes Agent plugin for safer Kanban worker output. The current implementation has two layers:

1. hook-based scanning of durable Kanban output (`kanban_comment`, `kanban_complete`, and `kanban_block`) for likely secrets or unsafe connection details;
2. a profile-scoped supervisor that runs from the Hermes plugin lifecycle and can also be exercised through the `kanban-warden` debug CLI; and
3. an optional notification outbox drainer that creates safe Kanban-native evidence events/comments for existing `kanban_notify_subs` subscribers.

The project is still intentionally conservative. It warns and logs with redacted snippets instead of blocking tool calls. Board mutations are limited to explicit auto-advance actions and optional notification evidence handoff, both guarded by config and idempotency.

## Naming map

Use these names consistently:

- Project/display name: `hermes-kanban-warden`
- GitHub repository slug: `coderlaoma/hermes-kanban-warden` (`https://github.com/coderlaoma/hermes-kanban-warden`)
- Python import/config namespace: `kanban_warden`
- Python distribution / Hermes plugin entry point / CLI slug: `kanban-warden`

`kanban-warden` remains the current package/CLI/plugin technical slug for this documentation-only update. Do not rename `pyproject.toml` package metadata, `src/kanban_warden/plugin.yaml`, CLI commands, runtime log prefixes, database paths, or Python imports unless a future task explicitly scopes a breaking slug migration with aliases and compatibility notes. Historical repository slugs such as `coderlaoma/kanban-warden` may redirect here, but new documentation should point to `coderlaoma/hermes-kanban-warden`.

## Current implementation scope

Implemented in the current skeleton:

- Hermes plugin registration through the `hermes_agent.plugins` entry point and directory-plugin metadata.
- `register(ctx)` / `unregister(ctx)` hooks that install Kanban safety scanning and start/stop the optional supervisor when profile config enables it.
- Non-blocking scanner hooks for durable Kanban coordination output, with redacted findings and warning text.
- YAML-backed scanner rules and allowlisted redaction placeholders.
- Typed configuration dataclasses for supervisor, leader lock, loop cadence, notification policy, auto-advance policy, and safety limits.
- A SQLite leader lock with lease, heartbeat, status, release, and contention-demo behavior.
- `WardenSupervisor`, a daemon-thread plugin lifecycle loop that obtains the leader lock before each tick, tails board events, preserves root/child relationship metadata, and performs safe structured logging plus health sweeps.
- A bounded notification outbox drainer with retry/backoff state that writes redacted `kanban-warden` evidence through the Kanban board DB so the native notifier/gateway/Feishu route can observe subscribed tasks.
- `kanban-warden` CLI commands: `status`, `dry-run`, `run-once`, and `demo-lock`.
- Unit tests for scanner behavior, plugin result transformation, config parsing, leader-lock contention, supervisor dry ticks, outbox delivery lifecycle, and CLI demo behavior.

Not yet implemented as real operational behavior:

- Kanban board event tailing and checkpointing are now implemented as read-only collection; state-machine remediation remains future work.
- Direct platform credential delivery to chat, email, Feishu, or other channels. The implemented path is native Kanban evidence handoff through existing subscriptions.
- Real Kanban auto-advance, task unblocking, retry resets, stale-claim repair, or other remediation writes.
- Mutating board health remediation; current health sweep only reports candidate states.
- Integration tests against a temporary Hermes Kanban database or live Hermes gateway.

Treat direct platform transport settings as future policy until implementation lands. Existing `notifications.delivery_*`, `auto_advance.*`, retry limits, stale-claim limits, and timeout settings are active configuration surfaces and must stay aligned with tests and README.

## Root-only subscription policy

Gateway/entry subscriptions should track root Kanban tasks only. Decomposed child events must propagate through the Kanban event stream and the `hermes-kanban-warden`/Kanban notification plugin path. Manually subscribing child cards is reserved for explicit operator requests, debugging, or temporary recovery. Tests and docs should guard against regressions where profile config enables the plugin but the supervisor remains disabled.

## Core design principles

Follow these principles for all changes:

- Plugin-style integration: integrate through Hermes plugin hooks and packaging entry points instead of invasive Hermes core edits.
- Low intrusion: the plugin should warn and guide agents without silently changing user data or blocking unrelated tool calls.
- Supervisor is lifecycle-bound: the background loop is tied to Hermes plugin/profile lifecycle, not a separate Hermes cron job.
- Leader lock: long-running watcher or supervisor behavior must ensure only one active warden instance acts on the same board at a time.
- Event tail plus health sweep: future board-watching behavior should combine incremental event processing with periodic full-board health checks.
- Bounded retries, timeouts, and loop prevention: every automated action must have a ceiling and must avoid repeatedly re-triggering itself.
- Dry-run first: remediation workflows should default to preview/dry-run behavior before mutating durable board state.
- No secret logging: never log or persist raw tokens, credentials, private keys, or connection strings.
- Auditability: warnings and future automated actions should include enough redacted context for a human to understand what happened.

## Agent workflow expectations

Before editing:

1. Read `README.md`, `pyproject.toml`, and this `AGENTS.md`.
2. Inspect the current tree with Git status so you do not overwrite unrelated work.
3. Keep changes focused on the assigned task.
4. Do not invent commands, package names, paths, or behavior that is not present in the repository.

While working:

- Preserve the non-blocking plugin posture unless the task explicitly changes it.
- Keep findings redacted; tests may use synthetic placeholders but must not contain real credentials.
- Update tests when changing scanner rules, warning text, hook registration, supervisor behavior, configuration, CLI commands, or packaging metadata.
- Prefer small, reviewable changes and document any assumptions.

Before handing off:

- Run the relevant safe checks listed below when tooling is available.
- Report changed paths, exact verification commands, and results.
- If a section of this file becomes stale, update it in the same change.

## Repository organization

Current confirmed layout:

- `README.md` — project overview, install notes, profile configuration example, supervisor scope, CLI usage, development commands, and security posture.
- `pyproject.toml` — setuptools package metadata, Hermes plugin entry point, console script, dependencies, pytest/ruff/mypy configuration.
- `LICENSE` — project license.
- `AGENTS.md` — this repository guidance document.
- `src/kanban_warden/__init__.py` — Hermes plugin hook registration, Kanban tool argument extraction, warning transformation, and optional supervisor startup.
- `src/kanban_warden/warden.py` — secret scanner, YAML rule loading, redaction, and warning rendering.
- `src/kanban_warden/config.py` — typed configuration model for supervisor, leader lock, loop, notification, auto-advance, and limits settings.
- `src/kanban_warden/lock.py` — SQLite leader-lock implementation with lease and heartbeat semantics.
- `src/kanban_warden/supervisor.py` — lifecycle-bound supervisor loop, leader-lock coordination, event collection, dry-run/status reporting, read-only health sweep, and demo lock contention helper.
- `src/kanban_warden/board.py` — read-only board discovery, task_events tailing, relationship inference, and candidate health findings.
- `src/kanban_warden/state.py` — SQLite persistent state for per-board cursors, idempotency keys, retry budgets, notification outbox lifecycle, and runtime metadata.
- `src/kanban_warden/outbox.py` — bounded notification outbox drainer that writes Kanban-native evidence events/comments for existing subscribers.
- `src/kanban_warden/cli.py` — debug CLI for status, dry-run/run-once ticks, and leader-lock demonstration.
- `src/kanban_warden/rules.yaml` — packaged detection rules and allowlist values.
- `src/kanban_warden/plugin.yaml` — directory-plugin metadata for Hermes.
- `src/kanban_warden/py.typed` — marker for typed package consumers.
- `tests/test_warden.py` — scanner, plugin-result transformation, config, leader-lock, supervisor, and CLI tests.
- `src/kanban_warden.egg-info/` and `dist/` — generated packaging artifacts from prior builds. Avoid editing generated metadata directly unless the task explicitly concerns packaging artifacts.

Currently absent or not documented as first-class project areas:

- No `docs/` directory.
- `scripts/` contains disposable verification and operator check scripts; keep them secret-safe and runnable from the checkout.
- No `Makefile` or `justfile`.
- No direct platform notification transport backend or packaged state DB migration system yet.

## Development and test commands

Commands documented by the repository:

```sh
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest
python -m build
```

Use these only in an appropriate Python environment. The development host may include a `.venv/`; do not assume it exists elsewhere or commit environment-specific files.

Safe lightweight checks for documentation-only edits in the current development checkout:

```sh
test -f AGENTS.md
test ! -f AGENT.md
test ! -f CLAUDE.md
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Run `.venv/bin/python -m mypy src` and `.venv/bin/python -m build` when changing typed Python code, packaging, or release metadata. If tooling is missing, report the missing command instead of inventing a substitute.

## Safety rules

- Tokens and secrets: never commit credentials, print secret values, or include raw tokens in logs, task comments, fixtures, or examples.
- Test data: use clearly fake synthetic values only, and assert that warnings do not echo the raw secret-like value.
- Hermes core changes: do not edit Hermes core from this repository unless a task explicitly scopes that integration and explains the safety plan.
- Database writes: current code may write explicit auto-advance mutations and notification evidence when configured. Treat any new database write as a sensitive side effect requiring dry-run behavior, idempotency, and audit trail.
- Idempotency: future remediation must avoid duplicate comments, endless task creation, repeated resets, and retry loops.
- Dry-run behavior: new supervisor actions should expose a dry-run preview before making board changes.
- Audit trail: when the warden changes durable board state in future work, record the redacted condition detected, action taken, and guardrails applied.
- Loop prevention: automated recovery must not create infinite task/retry/comment loops.
- Timeouts and retries: external calls, subprocesses, and board scans must have bounded runtime and retry counts.
- Sensitive payloads: sanitize board event bodies, environment dumps, stack traces, and subprocess output before logging.

## Recommended future task decomposition

For broader warden development, split work into focused tasks:

- Scanner quality: tune `rules.yaml`, allowlist behavior, and false-positive coverage.
- Plugin integration: harden Hermes hook registration and compatibility with supported Hermes versions.
- Configuration: refine explicit controls for enabled tools, rule files, severity thresholds, warning behavior, supervisor policies, and operator-facing defaults.
- Board access layer: if future supervision needs board reads/writes, add safe query helpers and explicit write primitives.
- Event tailer: add incremental event processing with checkpointing.
- Health sweep: replace the placeholder log-only sweep with detection of stale runs, stuck tasks, retry exhaustion, and orphaned locks.
- Notification delivery: implement real delivery backends with redaction, rate limits, and failure handling.
- Remediation policy: implement dry-run decisions, bounded retries, loop prevention, idempotency keys, and human-readable audit comments before enabling writes.
- Tests: add unit tests for scanner/policy logic and integration-style tests against temporary board databases when board access exists.
- Documentation: keep README quickstart, operational notes, CLI examples, and this file current.

## Maintenance rules

- Keep README and `AGENTS.md` aligned with the real project shape.
- Distinguish implemented skeleton from placeholders every time supervisor behavior changes.
- Replace provisional future-supervisor notes with concrete paths and commands as soon as implementation lands.
- Remove stale instructions promptly; misleading agent guidance is worse than no guidance.
- When changing safety-sensitive behavior, update docs and tests in the same change so future agents understand the guardrails.
