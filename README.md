# hermes-kanban-warden

`hermes-kanban-warden` is an MVP Hermes Agent plugin for Kanban boards. It watches Kanban task events, keeps persistent cursors, detects review/stale/failure situations, queues notification decisions, and can optionally apply small auto-advance state transitions after you have inspected `dry-run` output.

MVP version: `0.5.0`

GitHub: https://github.com/coderlaoma/hermes-kanban-warden

## Project goals

Kanban-driven Hermes deployments can involve multiple profiles, reviewers, and long-running workers. This plugin provides a low-intrusion supervisor layer that helps operators answer:

- Which boards and new task events did the profile see?
- Which implementation cards are blocked for review?
- Which reviewer results should unblock source cards?
- Which running tasks look stale or over their timeout budget?
- Which notifications should be retried later instead of being lost?
- Did durable Kanban comments/results accidentally contain likely secrets?

The MVP is deliberately conservative. `dry-run` is the default posture for auto-advance; real board mutations require explicit configuration.

## Naming map

This project uses `hermes-kanban-warden` as the human-facing display name. The technical slugs are intentionally stable:

- GitHub repository: `coderlaoma/hermes-kanban-warden`
- Python import/config namespace: `kanban_warden`
- Hermes plugin slug: `kanban-warden`

Do not rename the plugin slug, Python package, runtime log prefix, database paths, or config namespace unless a future migration explicitly scopes that breaking change.

## Design overview

The plugin has three cooperating layers:

1. Kanban output scanner
   - Hook-style transform for durable Kanban tool output such as `kanban_comment`, `kanban_complete`, and `kanban_block`.
   - Scans user-visible text for likely secrets or unsafe connection strings.
   - Emits warnings with redacted snippets; it does not preserve raw matched values.

2. Supervisor event collector
   - Starts from Hermes plugin registration when `kanban_warden.enabled` is true.
   - Uses a SQLite leader lock so only one supervisor owner acts at a time.
   - Discovers legacy and named Kanban boards, including shared root boards when running from a profile-scoped Hermes home.
   - Tails `task_events`, persists per-board cursors, enriches events with task relationships, and runs health sweeps.
   - Health sweeps include dependency-deadlock remediation proposals for recovery cards stuck behind their own blocked source card and stale TODO cards whose parents are already done.

3. Notification and auto-advance state machine
   - Plans actions for review-required blocks, reviewer approve/needs-changes outcomes, stale running tasks, worker failures, and retry exhaustion.
   - Uses a durable idempotency store so replayed events do not duplicate reviewer cards, comments, unblocks, or outbox notifications.
   - Queues notification decisions into the warden state DB outbox, then a bounded drainer can write safe `kanban-warden` evidence events/comments on subscribed target tasks so the existing Kanban native notifier, gateway, and Feishu subscription path can observe them.
   - Applies Kanban board mutations only when `auto_advance.enabled: true` and `auto_advance.dry_run: false`.

## Installation

Install the plugin through Hermes' Git plugin manager:

```bash
hermes plugins install coderlaoma/hermes-kanban-warden
```

Update an existing install after a new tag is published:

```bash
hermes plugins update kanban-warden
```

Pinning to a specific release depends on the Hermes plugin manager version. If
the local CLI does not support a version flag, update the cloned plugin checkout
under the active Hermes home to tag `v0.5.0`.

Development setup from a source checkout:

```bash
uv sync --group dev
```

Hermes discovers this repository as a directory plugin through the root
`plugin.yaml` and `__init__.py` files.

## Enable the Hermes plugin

Merge this into the target Hermes profile `config.yaml`:

```yaml
plugins:
  enabled:
    - kanban-warden

kanban_warden:
  enabled: true
  boards: "*"
```

Then restart that Hermes CLI/gateway/profile process so plugin discovery and profile config are reloaded.

A complete sample is in `examples/config.yaml`.

## Configuration example

```yaml
plugins:
  enabled:
    - kanban-warden

kanban_warden:
  enabled: true
  boards: "*"

  leader_lock:
    enabled: true
    lease_seconds: 60
    heartbeat_seconds: 20
    db_path: null

  loop:
    event_interval_seconds: 5
    health_sweep_seconds: 60

  # Optional overrides. By default the state DB stays in the active profile home,
  # while board discovery scans the shared ~/.hermes/kanban/boards tree when the
  # plugin runs from ~/.hermes/profiles/<profile>.
  state_db_path: null
  board_db_path: null
  hermes_home: null
  log_level: INFO

  notifications:
    enabled: true
    channels:
      - origin
    review_required: true
    stale_tasks: true
    crash_alerts: true
    delivery_enabled: false
    delivery_batch_size: 10
    delivery_max_attempts: 3
    delivery_backoff_seconds: 60
    delivery_lease_seconds: 300
    evidence_events: true
    evidence_comments: false

  auto_advance:
    enabled: false
    dry_run: true
    review_required: true
    stale_claims: true
    reviewer_assignee: reviewer

  limits:
    max_retries: 2
    task_timeout_seconds: 14400
    stale_claim_seconds: 3600
```

Key settings:

- `kanban_warden.enabled`: starts the background supervisor at plugin registration.
- `kanban_warden.boards`: `"*"` discovers all visible boards; a list pins specific board names.
- `kanban_warden.board_db_path`: optional single-board DB override; otherwise discovery honors `HERMES_KANBAN_DB`, legacy `kanban.db`, and shared named boards under `~/.hermes/kanban/boards/*/kanban.db`.
- `kanban_warden.hermes_home`: optional shared Hermes home override. When omitted from a profile home such as `~/.hermes/profiles/hairou`, named board discovery automatically uses the root `~/.hermes` tree.
- `leader_lock.enabled`: protects against duplicate supervisors. `lease_seconds` controls lock expiry; `heartbeat_seconds` controls refresh cadence.
- `loop.event_interval_seconds`: event polling interval for the background loop.
- `loop.health_sweep_seconds`: interval for stale/health checks.
- `notifications.*`: controls which decisions are queued to the durable outbox and whether the native Kanban evidence drainer is active.
- `notifications.delivery_enabled`: drains queued notification actions by writing redacted evidence to subscribed Kanban tasks. Keep false until dry-run/status output has been inspected.
- `notifications.delivery_batch_size`, `delivery_max_attempts`, `delivery_backoff_seconds`, and `delivery_lease_seconds`: bound each supervisor tick, retry cadence, and stale `in_progress` lease recovery. Rows move through `queued`, `in_progress`, `delivered`, `retrying`, and `exhausted`.
- `notifications.evidence_events`: writes a `task_events.kind='commented'` evidence row for the native notifier path.
- `notifications.evidence_comments`: also writes a human-visible `task_comments` audit comment. This is noisier and defaults off.
- `auto_advance.enabled`: master switch for applying state-machine actions.
- `auto_advance.dry_run`: when true, plans actions without mutating Kanban boards.
- `limits.max_retries`: retry budget before escalation.
- `limits.task_timeout_seconds`: long-running task timeout threshold.
- `limits.stale_claim_seconds`: heartbeat/claim staleness threshold.

## CLI usage

The checkout exposes a debug CLI module for inspection and smoke testing.

```bash
uv run --group dev python -m kanban_warden.cli --config examples/config.yaml status
uv run --group dev python -m kanban_warden.cli --config examples/config.yaml dry-run
uv run --group dev python -m kanban_warden.cli --config examples/config.yaml run-once
uv run --group dev python -m kanban_warden.cli demo-lock
```

`status` prints effective config, leader-lock state, runtime metadata, and policy settings.

`dry-run` runs one collection pass with auto-advance forced into dry-run mode. It prints JSON containing discovered boards, cursor movement, recent events, relationship summaries, health findings, planned actions, action results, and the warden state snapshot.

`run-once` runs one collection pass using the supplied config. It may mutate Kanban boards only if both `auto_advance.enabled: true` and `auto_advance.dry_run: false` are set.

When `notifications.delivery_enabled: true` and `auto_advance.dry_run: false`, `run-once` also drains one bounded notification outbox batch after planning/applying actions. Delivery means creating secret-scanned warden evidence on the target Kanban task; the existing `kanban_notify_subs` native notifier/gateway path remains responsible for final platform delivery.


## Root-only subscription policy and decomposed task propagation

The gateway/entry side should subscribe only to root Kanban tasks. A root task is the top-level card that has no parent in `task_links` and may own one or more decomposed child implementation, review, or documentation cards.

Do not manually subscribe every decomposed child task as the normal operating model. Child-task events are intentionally propagated through the Kanban event stream and the `hermes-kanban-warden`/Kanban notification plugin path:

- `BoardEvent` summaries include relationship metadata, including parents, children, `root_task_id`, `review_required`, and comment count.
- The supervisor tails child events, preserves per-board cursors, and feeds the notification/action state machine from those events.
- Notification decisions are idempotent and queued in the warden state DB outbox when notifications are enabled.
- When delivery is enabled, queued decisions are drained by writing safe evidence to subscribed target tasks, so normal root subscriptions continue to be the gateway-facing route.
- Health sweeps detect root/child coordination problems such as a root task not being closed after all children are done, or a child that cannot proceed because an upstream dependency is blocked/failed.
- When a blocked/gave-up/worker-failure child or dependency deadlock is detected, the fallback `ensure_subscription` action copies an existing root subscription to the stuck child (and ensures the root has the same subscription) using `insert or ignore`. This keeps normal entry creation root-only while allowing the native notifier to route the stuck child back to the user during incidents.

Manual child-task subscription is reserved for explicit operator requests, debugging, or temporary recovery when the normal warden/notification path is unavailable. Remove temporary child subscriptions after the incident so routine decomposed traffic continues to flow through the root-only entry policy.

Why this matters: subscribing both a root task and all decomposed children at the entry layer duplicates messages and can hide regressions where `kanban_warden.enabled: true` is configured but the supervisor is not actually running.

## Hairou environment runbook

Canonical company checkout:

```bash
cd /data/hairou/project/kanban-warden
```

When operating from the central Hermes host instead of an already-routed hairoudev shell, prefix commands with SSH, for example:

```bash
ssh hairoudev 'cd /data/hairou/project/kanban-warden && uv run --group dev pytest -q'
```

### Configuration checks

Inspect the active Hairou profile configuration without printing secrets. The only required values for supervisor startup are the plugin entry and `kanban_warden.enabled: true`:

```bash
uv run --group dev python scripts/check_hairou_warden.py --config ~/.hermes/profiles/hairou/config.yaml --skip-dry-run
```

The script reports whether `plugins.enabled` contains `kanban-warden`, whether `kanban_warden.enabled` parses as true, the configured board selector, notification/auto-advance booleans, and safe supervisor log hints. It does not print token-like values.

If the target profile uses a different Hermes home or config file, pass it explicitly:

```bash
uv run --group dev python scripts/check_hairou_warden.py --config /path/to/config.yaml --hermes-home /path/to/.hermes --profile hairou
```

### Dry-run and status

Run a read-only collection pass before enabling real auto-advance:

```bash
uv run --group dev python -m kanban_warden.cli --config ~/.hermes/profiles/hairou/config.yaml --profile hairou dry-run
```

Check effective supervisor status and leader-lock ownership:

```bash
uv run --group dev python -m kanban_warden.cli --config ~/.hermes/profiles/hairou/config.yaml --profile hairou status
```

Expected healthy signs:

- `enabled` is `true` in status output.
- `leader_lock.enabled` is `true` unless intentionally disabled for a one-shot test.
- `leader_lock.active` is true after a running supervisor or explicit `run-once` has acquired the lease.
- `state` includes board cursors/runtime metadata after dry-run or normal ticks.
- `state.notification_outbox_by_status` shows queued/delivered/retrying/exhausted counts when notification decisions exist.
- `dry_run.status.policies.auto_advance.dry_run` remains true unless an operator intentionally enables board mutations.

### Native notification evidence

To enable the production evidence drainer after dry-run review:

```yaml
kanban_warden:
  notifications:
    enabled: true
    delivery_enabled: true
    delivery_batch_size: 10
    delivery_max_attempts: 3
    delivery_backoff_seconds: 60
    delivery_lease_seconds: 300
    evidence_events: true
    evidence_comments: false
```

The drainer does not use platform credentials and does not print subscriber identifiers. It requires the target task to have at least one row in `kanban_notify_subs`; otherwise the outbox row is retried with backoff and eventually marked `exhausted`. Evidence event payloads include the outbox key, task id, action kind, reason, and `native_route: kanban_notify_subs`.

Safe hairou verification queries:

```bash
sqlite3 ~/.hermes/profiles/hairou/kanban-warden/state.db \
  "select status, attempts, count(*) from notification_outbox group by status, attempts;"

sqlite3 ~/.hermes/kanban/boards/<board>/kanban.db \
  "select task_id, kind, payload from task_events where payload like '%warden-notification-delivered%' order by id desc limit 5;"
```

### Supervisor health and logs

The plugin writes log lines with the `kanban-warden` prefix. Key startup/runtime lines are:

- `kanban-warden loaded; supervisor enabled profile=<profile>`
- `kanban-warden supervisor thread started profile=<profile>`
- `kanban-warden acquired leader lock owner=<profile>:<pid>`
- `kanban-warden health sweep profile=<profile> ... findings=<n>`
- `kanban-warden tick profile=<profile> boards=<n> new_events=<n> health_findings=<n> dry_run=<bool> notifications=<bool>`

If logs show `kanban-warden loaded; supervisor disabled` while the profile config has `kanban_warden.enabled: true`, treat it as a regression in plugin config loading or profile routing and run the tests in `tests/test_warden.py` before changing production settings.

Common log locations depend on how Hermes is supervised in the environment. Check the active process manager first, then inspect the configured stdout/stderr target. Example local checks:

```bash
ps -ef | grep -E 'hermes|kanban-warden' | grep -v grep
journalctl --user -u hermes -n 200 --no-pager  # if a user systemd unit is used
```

### Regression test commands

Run these in the canonical checkout before handing off changes:

```bash
uv run --group dev pytest -q
uv run --group dev ruff check .
uv run --group dev mypy kanban_warden
uv run --group dev python scripts/check_hairou_warden.py --config examples/config.yaml --skip-dry-run
uv run --group dev python scripts/verify_mvp.py
```

No command above should require or print secrets. Use synthetic test data only.

`demo-lock` shows that two independent owners cannot both hold the active leader lease:

```json
{
  "active": true,
  "active_owner": "demo-profile-a",
  "first_acquired": true,
  "second_acquired": false
}
```

## Verification script

Run the MVP verification script in the development environment:

```bash
uv run --group dev python scripts/verify_mvp.py
```

The script creates a disposable Kanban database and verifies:

- event collection and persistent cursors;
- relationship inference from `task_links`;
- dry-run planning for notify, reviewer creation, comments, unblocks, and retry;
- real-schema reviewer/comment/unblock mutations when dry-run is disabled;
- durable notification outbox entries and native evidence delivery;
- idempotency on repeated collection; and
- active leader lock status.

A successful run prints JSON with `"ok": true` and explanatory counts.

Development checks:

```bash
uv run --group dev pytest
uv run --group dev ruff check .
uv run --group dev mypy kanban_warden
```

## Safety and security

- Scanner findings never include raw matched secrets; snippets are redacted.
- The scanner is conservative and can produce false positives. Treat warnings as a prompt to review durable Kanban output.
- `dry-run` should be inspected before enabling real auto-advance.
- Real board mutations are small and idempotent, but they still affect shared Kanban state.
- Do not store tokens, private keys, passwords, raw database URLs, or personal credentials in config files, README examples, task comments, or run metadata.
- `examples/config.yaml` contains placeholders and safe defaults only.

## Notification reliability boundary

The MVP drains notification decisions by creating native Kanban evidence on tasks that already have `kanban_notify_subs` subscribers. This proves handoff to the Hermes/Kanban notifier path without adding direct Feishu, WeChat, or other platform credentials to the warden.

Known operational boundary: Feishu, WeChat/iLink, or other gateway rate limits can still cause downstream notifier backoff and retries. Warden records notification intent and native evidence handoff; final user-visible delivery must be validated against the real gateway behavior in the target deployment.

## Troubleshooting

No boards discovered:
- Confirm `kanban_warden.boards` is `"*"` or names the target board.
- Confirm the running profile has the expected `HERMES_HOME` or set `kanban_warden.hermes_home` explicitly.
- Run `uv run --group dev python -m kanban_warden.cli --config examples/config.yaml dry-run` and inspect `boards`.

Supervisor does not start:
- Confirm the package is installed in the Python environment used by Hermes.
- Confirm `plugins.enabled` includes `kanban-warden`.
- Confirm `kanban_warden.enabled: true`.
- Restart the Hermes process after changing plugin config.

Duplicate actions or missing actions:
- Inspect `state_db_path` and the `state` section from `status`/`dry-run`.
- Verify all supervisor instances share the intended state DB and leader-lock DB.
- Check whether a previous dry-run advanced event cursors before a later apply run.

Real mutations did not happen:
- `auto_advance.enabled` must be true.
- `auto_advance.dry_run` must be false.
- The action must not already be marked done in the idempotency store.
- The current Kanban schema must contain the required columns used by the action path.

Secret scanner warning appears:
- Replace raw credentials with `[REDACTED]`.
- Prefer stable references such as secret names or vault paths instead of values.

## MVP limitations

- Direct platform transport delivery is not implemented; the drainer hands off through Kanban native notifier evidence and existing subscriptions.
- State-machine policies are intentionally narrow and focused on common Kanban workflow events.
- The plugin depends on current Hermes Kanban SQLite schema details for mutation paths.
- There is no packaged migration system for future state DB schema changes yet.
- Multi-profile production rollout should validate leader-lock and state DB paths per deployment topology.

## Suggested next iterations

1. Add gateway-level delivery acknowledgements if Hermes exposes them, so warden can distinguish native evidence handoff from final platform receipt.
2. Add config validation with clearer startup errors for invalid policy combinations.
3. Add state DB migrations and version reporting.
4. Add integration tests against a live Hermes Kanban board fixture.
5. Add operator dashboards or concise status summaries for pending outbox items and retry exhaustion.
