# hermes-kanban-warden

hermes-kanban-warden is a Hermes Agent plugin for Kanban workers. It provides two low-intrusion safety layers:

1. hook-based scanning of durable Kanban output (`kanban_comment`, `kanban_complete`, `kanban_block`) for likely secrets; and
2. an optional profile-scoped background supervisor loop that runs with the Hermes profile/gateway lifecycle, not Hermes cron.

The first implementation is intentionally conservative: it warns and logs with redacted snippets instead of blocking tool calls or mutating the Kanban database.

## Naming map

This project uses `hermes-kanban-warden` as the human-facing project/display name. The existing technical slugs remain unchanged in this documentation-only update:

- Project/display name: `hermes-kanban-warden`
- GitHub repository slug: `coderlaoma/kanban-warden`
- Python import/config namespace: `kanban_warden`
- Python distribution / Hermes plugin entry point / CLI slug: `kanban-warden`

Keep these names distinct when documenting or changing the project. Do not rename the repository, package distribution, plugin entry point, CLI command, runtime log prefix, database path, or Python namespace unless a future migration task explicitly scopes that breaking change.

## What it checks

hermes-kanban-warden scans user-visible text fields such as `body`, `summary`, `result`, `reason`, and JSON-serialized `metadata`.

Packaged rules detect common high-risk patterns, including:

- token/API key assignments
- GitHub, Slack, and OpenAI-style tokens
- JWT-like bearer tokens
- PEM private key headers
- database URLs and generic URLs containing inline credentials

Allowed placeholders such as `[REDACTED]` and `<redacted>` are ignored.

## Install from a checkout

```bash
python -m pip install .
```

Hermes discovers the plugin through the `hermes_agent.plugins` entry point named `kanban-warden`.

## Enable from Hermes profile config

Add this section to the target profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - kanban-warden

kanban_warden:
  enabled: true
  boards: "*"
  log_level: INFO
  leader_lock:
    enabled: true
    lease_seconds: 60
    heartbeat_seconds: 20
    # Optional; defaults under $HERMES_HOME/kanban-warden/leader-lock.db
    db_path: null
  loop:
    event_interval_seconds: 5
    health_sweep_seconds: 60
  notifications:
    enabled: false
    channels: []
    review_required: true
    stale_tasks: true
    crash_alerts: true
  auto_advance:
    enabled: false
    dry_run: true
    review_required: false
    stale_claims: false
  limits:
    max_retries: 2
    task_timeout_seconds: 14400
    stale_claim_seconds: 3600
```

When `kanban_warden.enabled` is true, the plugin starts `WardenSupervisor` as a daemon thread during plugin registration. The supervisor emits safe structured logs, obtains a SQLite leader lock before each tick, and performs a placeholder health sweep. This is a plugin lifecycle loop, not a `hermes cron` job.

## Debug CLI

The package installs a small CLI for dry-run/status checks:

```bash
kanban-warden --config config.yaml status
kanban-warden --config config.yaml dry-run
kanban-warden --config config.yaml run-once
kanban-warden demo-lock
```

`demo-lock` demonstrates that two independent owners using the same SQLite lock cannot both obtain the active lease:

```json
{
  "active": true,
  "active_owner": "demo-profile-a",
  "first_acquired": true,
  "second_acquired": false
}
```

## Directory plugin form

If using Hermes directory plugins instead of Python packaging, copy the package directory or place a plugin directory containing `plugin.yaml` and `__init__.py` under:

```text
~/.hermes/plugins/kanban-warden/
```

Then enable it with the normal Hermes plugin command for the active profile.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest
python -m build
```

## Current scope and adaptation notes

- The supervisor loop and config schema are implemented as an independent plugin layer with safe placeholders for notification, auto-advance, retry, and timeout policy.
- Direct Kanban board mutation is not implemented in this skeleton; `auto_advance.enabled` remains opt-in and dry-run-first.
- Hermes plugin lifecycle APIs can differ by installed Hermes version. This package uses the documented `register(ctx)` hook style and defensively reads `ctx.config`, `ctx.profile_config`, `ctx.settings`, or `ctx.get_config()`.
- Unload support is exposed via `unregister(ctx)` for plugin managers that call it.

## Security posture

hermes-kanban-warden never returns raw matched secrets. Findings include only rule id, severity, location, and a redacted snippet. The scanner is conservative and may produce false positives; warnings should be treated as prompts to review and redact durable Kanban output.
