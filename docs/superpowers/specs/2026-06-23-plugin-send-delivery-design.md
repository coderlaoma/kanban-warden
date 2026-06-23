# Plugin Send Delivery Design

## Goal

Close the Kanban Warden notification loop without changing Hermes core source code.

Warden should keep detecting Kanban review, retry, stale, and escalation conditions, but queued notification outbox rows must now be delivered to the same Feishu/Weixin channels that already subscribe to the relevant Kanban task.

## Boundary

This feature lives entirely inside the `hermes-kanban-warden` plugin.

It must not modify Hermes gateway watchers, Hermes Kanban schema, platform adapters, or the Hermes repository. The plugin may reuse existing Hermes runtime capabilities that are already importable in the gateway process, especially `tools.send_message_tool.send_message_tool`, and may fall back to the `hermes send` CLI when direct import is unavailable.

The plugin must not import or call Feishu or Weixin SDKs directly. Platform credentials, home-channel resolution, and message sending remain owned by Hermes.

## Delivery Model

The source of recipients is `kanban_notify_subs` in each Kanban board database.

When `notifications.channels` contains `origin`, Warden resolves the affected task to existing subscriber rows:

- `platform`
- `chat_id`
- `thread_id`
- `notifier_profile`

For each matching subscription, Warden builds a Hermes send target:

- `platform:chat_id` when `thread_id` is empty.
- `platform:chat_id:thread_id` when `thread_id` is present.

The plugin formats a short operator-facing message from the outbox payload and sends it through a delivery adapter.

## Delivery Adapter

Create a small plugin-local adapter with this responsibility:

- Accept a target and message.
- Try in-process Hermes delivery first by importing `tools.send_message_tool.send_message_tool`.
- If import or send execution fails because the runtime surface is unavailable, fall back to `hermes send --to <target> <message>`.
- Return a typed result that says whether delivery succeeded and includes a safe error string.

The adapter must not inspect platform credentials and must not log secrets.

## Outbox Semantics

The existing `notification_outbox` table remains the reliability boundary:

- Rows are claimed in batches using the existing lease.
- Every target send must succeed before the outbox row is marked delivered.
- A transient send failure marks the row retrying with the existing backoff.
- A permanent validation failure, such as missing target task or unsafe message text, exhausts the row.
- Delivery stays disabled when `notifications.delivery_enabled` is false.
- No sends happen when `auto_advance.dry_run` is true.

Idempotency remains at the outbox row level. A retried row may resend to a target if an earlier target succeeded and a later target failed. That is acceptable for the first implementation because the row-level outbox already favors at-least-once delivery, and the message contains the stable outbox key for dedupe by humans.

## Message Content

Messages should be short and safe:

```text
[Kanban Warden] <title>

Board: <board>
Task: <task_id>
Action: <action_kind>
Reason: <reason>

<message>

Outbox: <key>
```

Titles are derived from action kind:

- `create_reviewer`: `Review required`
- `create_implementer_followup`: `Changes requested`
- `retry`: `Task retry planned`
- `escalate`: `Retry exhausted`
- `promote`: `Task can be promoted`
- `finalize`: `Task can be finalized`
- `notify`: the payload reason, capped to one line
- all other kinds: `Notification`

Before sending, scan the full message with the existing secret scanner. A secret-like match is a permanent delivery failure.

## Configuration

Keep the operator config simple:

```yaml
kanban_warden:
  notifications:
    enabled: true
    channels:
      - origin
    delivery_enabled: true
```

No Feishu or Weixin target must be configured in Warden for the default path.

Advanced delivery options can be added later, but this implementation should only support `origin`. Unknown channels are ignored with a skipped count or safe warning in the delivery report.

## Observability

The delivery report returned from `NotificationOutboxDrainer.drain()` should remain compatible with current callers and include enough counts to diagnose delivery:

- processed
- delivered
- retrying
- exhausted
- skipped

Safe error text is stored through the existing retry/exhaustion path.

Logs should include action key, board, task id, and target count, but not message bodies or credentials.

## Tests

Add unit tests that prove:

- Origin delivery reads `kanban_notify_subs`, formats targets, and calls the send adapter.
- `thread_id` is included in the target when present.
- Multiple subscribers must all be sent before the row is delivered.
- Send failure retries the outbox row with backoff.
- Missing subscriber retries and eventually exhausts as it does today.
- Secret-like message text exhausts the row without sending.
- Dry-run and `delivery_enabled: false` do not send.

Existing full validation remains:

```bash
uv run --group dev ruff check .
uv run --group dev mypy kanban_warden
uv run --group dev pytest
uv run --group dev python scripts/check_hairou_warden.py --config examples/config.yaml --skip-dry-run
uv run --group dev python scripts/verify_mvp.py
git diff --check
```
