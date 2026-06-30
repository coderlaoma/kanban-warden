# hermes-kanban-warden

`hermes-kanban-warden` is an MVP Hermes Agent plugin for Kanban boards. It watches Kanban task events, keeps persistent cursors, detects review/stale/failure situations, queues notification decisions, and can optionally apply small auto-advance state transitions after you have inspected `dry-run` output.

MVP version: `0.8.4`

GitHub: https://github.com/coderlaoma/hermes-kanban-warden

## Project goals

Kanban-driven Hermes deployments can involve multiple profiles, reviewers, and long-running workers. This plugin provides a low-intrusion supervisor layer that helps operators answer:

- Which boards and new task events did the profile see?
- Which implementation cards are blocked for review?
- Which reviewer results should unblock source cards?
- Which running tasks look stale or over their timeout budget?
- Which notifications should be retried later instead of being lost?
- Did durable Kanban comments/results accidentally contain likely secrets?

The MVP is deliberately active by default once enabled. Use the CLI `dry-run` command for a read-only preview before changing production profiles.

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
   - Queues notification decisions into the warden state DB outbox, then a bounded drainer sends concise messages to the task's existing Kanban subscribers through Hermes `send_message`.
   - Applies Kanban board mutations only when `auto_advance.enabled: true` and `auto_advance.dry_run: false`.

## Installation

Install the plugin through Hermes' Git plugin manager in the same profile that
will run the gateway. For the Hairou Feishu gateway profile:

```bash
hermes --profile hairou-feishu plugins install coderlaoma/hermes-kanban-warden --enable
hermes --profile hairou-feishu gateway restart
```

For another profile, replace `hairou-feishu` with that profile name. If you run
`hermes plugins install ...` without `--profile`, Hermes installs into the
default Hermes home, and a profile-scoped gateway will not discover the plugin.

Update an existing install after a new tag is published:

```bash
hermes --profile hairou-feishu plugins update kanban-warden
hermes --profile hairou-feishu gateway restart
```

Pinning to a specific release depends on the Hermes plugin manager version. If
the local CLI does not support a version flag, update the cloned plugin checkout
under the active Hermes home to tag `v0.8.4`.

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
  # Starts the background supervisor when the Hermes profile loads plugins.
  enabled: true

  # "*" scans every visible Kanban board. Use ["default"] or another board list
  # only when this profile must be pinned to specific boards.
  boards: "*"

  # Queue warden decisions into the durable outbox. "origin" sends concise
  # messages to the affected task/root's existing Kanban subscribers.
  notifications:
    enabled: true
    channels:
      - origin
    # Optional escape hatch for profiles whose Kanban subscription table is
    # temporarily empty. When enabled, explicit warden notifications fall back
    # to the configured platform home channel only after subscriber lookup
    # fails. Keep disabled for normal subscription-driven operation.
    home_fallback_enabled: false
    home_fallback_platforms: []

  # Normal operating mode. Most actions are gateway-required proposals in the
  # outbox; use the CLI dry-run command when you need a read-only preview.
  auto_advance:
    enabled: true
    dry_run: false

  # Optional autonomous unblock helper. Agent-actionable kanban_block events
  # create gateway-required remediation proposals. Human-needed blockers only
  # receive a source-task comment.
  blocked_remediation:
    enabled: true
    max_per_tick: 3

  # Optional. Leave null unless this Hermes environment has a known reviewer
  # assignee. When null, reviewer routing is left to Kanban/Hermes defaults.
  reviewer_assignee: null

  # Bounded retry/health thresholds for stale workers and repeated failures.
  limits:
    max_retries: 2
    task_timeout_seconds: 14400
    stale_claim_seconds: 3600
```

Key settings:

- `kanban_warden.enabled`: starts the background supervisor at plugin registration.
- `kanban_warden.boards`: `"*"` discovers all visible boards; a list pins specific board names.
- `notifications.enabled`: enables native subscription maintenance such as root/stuck-task subscription propagation. Warden does not duplicate normal Kanban terminal-event notifications into its own outbox, except for explicit tail continuations when native blocked reasons or completed summaries exceed the configured safe prefix.
- `notifications.channels`: reserved for explicitly enqueued notification rows; routine blocked/completed/gave-up/crashed messages are expected to come from Hermes' native Kanban notifier.
- `notifications.home_fallback_enabled`: when true, explicit warden notification rows with `origin` delivery may fall back to configured platform home channels if the affected task/root has no `kanban_notify_subs` subscriber. This is an operational escape hatch for profile misconfiguration or subscription gaps, not the normal delivery path.
- `notifications.home_fallback_platforms`: bare Hermes platform targets to use for the fallback, for example `["feishu"]`.
- `auto_advance.enabled`: turns on the state-machine actions. Current write-like actions are recorded as gateway-required outbox proposals unless a dedicated delivery path handles them.
- `auto_advance.dry_run`: when true, plans actions without applying them. The normal enabled profile value is `false`; use the CLI `dry-run` command for previews.
- `blocked_remediation.enabled`: when true, explicitly blocked tasks are classified. Agent-actionable blocks queue a `create_blocked_remediation` gateway proposal; human-needed blocks only get a source-task comment and stay blocked.
- `blocked_remediation.max_per_tick`: caps newly proposed blocked-remediation tasks in one supervisor pass.
- `reviewer_assignee`: optional fixed reviewer assignee. Leave `null` for portable configs so Kanban/Hermes can route reviews using its own defaults.
- `limits.max_retries`: retry budget before escalation.
- `limits.task_timeout_seconds`: long-running task timeout threshold.
- `limits.stale_claim_seconds`: heartbeat/claim staleness threshold.

Advanced settings are available but intentionally omitted from the default example:

- `leader_lock.*`: duplicate-supervisor protection. Defaults are suitable for normal gateway profiles.
- `loop.*`: supervisor polling and health sweep cadence.
- `state_db_path`, `board_db_path`, `hermes_home`, `log_level`: path and logging overrides for unusual deployments.
- `notifications.delivery_*`: Hermes send delivery controls for environments that intentionally enqueue explicit warden notifications to origin subscribers.
- `task_filter.*`: active/terminal task filtering for large boards.
- `cleanup.*`: optional board/state cleanup maintenance.

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

When `notifications.delivery_enabled: true` and `auto_advance.dry_run: false`, `run-once` also drains one bounded notification outbox batch after planning/applying actions. Normal blocked/completed/gave-up/crashed task messages are not enqueued by Warden; delivery is for explicitly queued warden notification rows only.


## Root-only subscription policy and decomposed task propagation

The gateway/entry side should subscribe only to root Kanban tasks. A root task is the top-level card that has no parent in `task_links` and may own one or more decomposed child implementation, review, or documentation cards.

Do not manually subscribe every decomposed child task as the normal operating model. Hermes' native Kanban notifier delivers terminal task events. Warden only helps when a stuck child/root subscription is missing:

- `BoardEvent` summaries include relationship metadata, including parents, children, `root_task_id`, `review_required`, and comment count.
- The supervisor tails child events, preserves per-board cursors, and feeds the action state machine from those events.
- Warden does not duplicate native terminal-event messages into its own outbox, keeping gateway conversation context smaller. It only queues explicit continuation rows for known native truncation cases, such as long blocked reasons and long completed summaries.
- Explicitly queued warden notifications can still be delivered through Hermes `send_message`, but this is not the routine blocked-task path.
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
- `state.notification_outbox_by_status` shows queued/delivered/retrying/exhausted counts when explicit warden notification rows or gateway-required proposals exist.
- `dry_run.status.policies.auto_advance.dry_run` remains true unless an operator intentionally enables board mutations.

### Hermes send delivery

To enable production delivery after dry-run review:

```yaml
kanban_warden:
  notifications:
    enabled: true
    channels:
      - origin
    delivery_enabled: true
    delivery_batch_size: 10
    delivery_max_attempts: 3
    delivery_backoff_seconds: 60
    delivery_lease_seconds: 300
    home_fallback_enabled: false
    home_fallback_platforms: []
```

The drainer does not use platform credentials and does not print subscriber identifiers. It first looks for subscribers on the target task, then falls back to root or parent subscribers carried by the source event relationship. If no subscriber is found and `home_fallback_enabled` is false, the outbox row is retried with backoff and eventually marked `exhausted`. If `home_fallback_enabled` is true, Warden sends the explicit notification to each configured bare platform target, such as `feishu`, through Hermes' platform home channel. Each real subscriber row is converted to a Hermes target such as `feishu:<chat_id>` or `weixin:<chat_id>:<thread_id>`, then delivered through the Hermes `send_message` capability. This path is for explicit warden notifications and tail continuations, not for duplicating normal native Kanban terminal-event notifications.

Safe hairou verification queries:

```bash
sqlite3 ~/.hermes/profiles/hairou/kanban-warden/state.db \
  "select status, attempts, count(*) from notification_outbox group by status, attempts;"
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
- dry-run planning for reviewer creation, comments, unblocks, retry, and subscription maintenance;
- real-schema reviewer/comment/unblock mutations when dry-run is disabled;
- durable explicit notification outbox entries and Hermes send delivery through fake subscribers;
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

The MVP can drain explicitly queued notification rows by sending concise messages to tasks that already have `kanban_notify_subs` subscribers. A config-gated home-channel fallback is available for subscription gaps. The plugin does not add direct Feishu, WeChat, or other platform credentials; it delegates transport to Hermes `send_message`.

Known operational boundary: Feishu, WeChat/iLink, or other gateway rate limits can still cause send failures. For explicit notification rows, Warden records notification intent and retries failed sends from its own outbox; normal blocked-task delivery remains the responsibility of Hermes' native Kanban notifier.

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

- Delivery depends on Hermes `send_message` remaining available in-process or through the `hermes send` CLI fallback.
- State-machine policies are intentionally narrow and focused on common Kanban workflow events.
- The plugin depends on current Hermes Kanban SQLite schema details for mutation paths.
- There is no packaged migration system for future state DB schema changes yet.
- Multi-profile production rollout should validate leader-lock and state DB paths per deployment topology.

## Suggested next iterations

1. Add gateway-level delivery acknowledgements if Hermes exposes them, so warden can distinguish send acceptance from final platform receipt.
2. Add config validation with clearer startup errors for invalid policy combinations.
3. Add state DB migrations and version reporting.
4. Add integration tests against a live Hermes Kanban board fixture.
5. Add operator dashboards or concise status summaries for pending outbox items and retry exhaustion.
