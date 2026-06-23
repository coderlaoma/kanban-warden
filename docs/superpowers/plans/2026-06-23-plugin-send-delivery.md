# Plugin Send Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Kanban Warden notification outbox rows to Feishu/Weixin through Hermes send capability without modifying Hermes core.

**Architecture:** Keep Warden as the decision/outbox owner. Add a plugin-local delivery adapter that sends through `tools.send_message_tool.send_message_tool` with a CLI fallback, and update the outbox drainer to resolve `origin` recipients from `kanban_notify_subs`.

**Tech Stack:** Python 3.13, SQLite, pytest, Hermes `send_message_tool`, existing `WardenStateStore` notification outbox.

---

## File Structure

- Create `kanban_warden/delivery.py`: plugin-local send adapter, result type, message target helpers.
- Modify `kanban_warden/outbox.py`: inject/use delivery adapter, resolve origin subscribers, send formatted messages, preserve retry semantics.
- Modify `tests/test_actions.py`: extend outbox delivery tests with fake sender coverage.
- Modify `scripts/verify_mvp.py`: assert real delivery path can be exercised with a fake sender or keep the MVP report compatible if no fake hook is exposed.
- Modify `README.md` and `examples/config.yaml`: document that `delivery_enabled: true` now sends via Hermes to `kanban_notify_subs` origin subscribers.

## Task 1: Add Delivery Adapter Unit

**Files:**
- Create: `kanban_warden/delivery.py`
- Test: `tests/test_actions.py`

- [ ] **Step 1: Write failing tests for target formatting and fake send**

Add tests near existing notification outbox tests in `tests/test_actions.py`:

```python
from kanban_warden.delivery import DeliveryResult, SendTarget, target_from_subscription


def test_delivery_target_formats_thread_id() -> None:
    target = target_from_subscription(
        {"platform": "feishu", "chat_id": "chat-1", "thread_id": "thread-9"}
    )

    assert target == SendTarget(platform="feishu", chat_id="chat-1", thread_id="thread-9")
    assert target.to_hermes_target() == "feishu:chat-1:thread-9"


def test_delivery_target_formats_plain_chat() -> None:
    target = target_from_subscription(
        {"platform": "weixin", "chat_id": "chat-1", "thread_id": ""}
    )

    assert target == SendTarget(platform="weixin", chat_id="chat-1", thread_id="")
    assert target.to_hermes_target() == "weixin:chat-1"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run --group dev pytest tests/test_actions.py::test_delivery_target_formats_thread_id tests/test_actions.py::test_delivery_target_formats_plain_chat -q
```

Expected: import failure for `kanban_warden.delivery`.

- [ ] **Step 3: Implement `kanban_warden/delivery.py`**

Create:

```python
"""Hermes message delivery helpers for Kanban Warden notifications."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SendTarget:
    platform: str
    chat_id: str
    thread_id: str = ""

    def to_hermes_target(self) -> str:
        if self.thread_id:
            return f"{self.platform}:{self.chat_id}:{self.thread_id}"
        return f"{self.platform}:{self.chat_id}"


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    error: str = ""


class MessageSender(Protocol):
    def send(self, target: SendTarget, message: str) -> DeliveryResult:
        """Send one message to one Hermes target."""


class HermesMessageSender:
    """Send messages through Hermes, preferring in-process tool delivery."""

    def send(self, target: SendTarget, message: str) -> DeliveryResult:
        hermes_target = target.to_hermes_target()
        direct = self._send_in_process(hermes_target, message)
        if direct.ok:
            return direct
        fallback = self._send_with_cli(hermes_target, message)
        if fallback.ok:
            return fallback
        return DeliveryResult(
            ok=False,
            error=f"in-process send failed: {direct.error}; cli send failed: {fallback.error}",
        )

    def _send_in_process(self, target: str, message: str) -> DeliveryResult:
        try:
            from tools.send_message_tool import send_message_tool  # type: ignore[import-not-found]
        except Exception as exc:
            return DeliveryResult(ok=False, error=f"send_message_tool unavailable: {exc.__class__.__name__}")
        try:
            raw = send_message_tool({"action": "send", "target": target, "message": message})
        except Exception as exc:
            return DeliveryResult(ok=False, error=f"send_message_tool raised: {exc.__class__.__name__}")
        return _result_from_json(raw)

    def _send_with_cli(self, target: str, message: str) -> DeliveryResult:
        hermes = shutil.which("hermes")
        if not hermes:
            return DeliveryResult(ok=False, error="hermes executable not found")
        try:
            proc = subprocess.run(
                [hermes, "send", "--to", target, message],
                check=False,
                text=True,
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:
            return DeliveryResult(ok=False, error=f"hermes send failed: {exc.__class__.__name__}")
        if proc.returncode == 0:
            return DeliveryResult(ok=True)
        stderr = " ".join(proc.stderr.split())[:300]
        stdout = " ".join(proc.stdout.split())[:300]
        return DeliveryResult(ok=False, error=stderr or stdout or f"exit {proc.returncode}")


def target_from_subscription(row: Mapping[str, Any]) -> SendTarget:
    platform = _required_text(row.get("platform"), "subscription platform")
    chat_id = _required_text(row.get("chat_id"), "subscription chat_id")
    return SendTarget(
        platform=platform.lower(),
        chat_id=chat_id,
        thread_id=_text(row.get("thread_id")),
    )


def _result_from_json(raw: Any) -> DeliveryResult:
    if not isinstance(raw, str):
        return DeliveryResult(ok=bool(raw))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return DeliveryResult(ok=True)
    if isinstance(payload, dict) and payload.get("error"):
        return DeliveryResult(ok=False, error=str(payload["error"])[:300])
    if isinstance(payload, dict) and payload.get("success") is False:
        return DeliveryResult(ok=False, error=str(payload)[:300])
    return DeliveryResult(ok=True)


def _required_text(value: Any, label: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f"missing {label}")
    return text


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)
```

- [ ] **Step 4: Run target tests**

Run:

```bash
uv run --group dev pytest tests/test_actions.py::test_delivery_target_formats_thread_id tests/test_actions.py::test_delivery_target_formats_plain_chat -q
```

Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add kanban_warden/delivery.py tests/test_actions.py
git commit -m "feat: add hermes delivery adapter"
```

## Task 2: Deliver Origin Outbox Rows

**Files:**
- Modify: `kanban_warden/outbox.py`
- Test: `tests/test_actions.py`

- [ ] **Step 1: Write failing outbox delivery test**

Add this fake sender and test near notification outbox tests:

```python
from dataclasses import dataclass, field

from kanban_warden.delivery import DeliveryResult, SendTarget


@dataclass
class FakeSender:
    sent: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    def send(self, target: SendTarget, message: str) -> DeliveryResult:
        self.sent.append((target.to_hermes_target(), message))
        if self.fail:
            return DeliveryResult(ok=False, error="synthetic send failure")
        return DeliveryResult(ok=True)


def test_notification_outbox_delivers_to_origin_subscribers(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False, delivery_enabled=True)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('impl', 'Impl', 'blocked', 'hairou', 1, 'scratch')"
    )
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('impl', 'feishu', 'chat-1', 'thread-1', 'user-1', 'hairou-feishu', 2, 0)"
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: inspect diff"}, 3)

    sender = FakeSender()
    report = WardenSupervisor(config, profile_name="tester", message_sender=sender).dry_run(now=20)

    assert report["outbox_delivery"]["delivered"] >= 1
    assert sender.sent
    assert sender.sent[0][0] == "feishu:chat-1:thread-1"
    assert "[Kanban Warden]" in sender.sent[0][1]
    assert "impl" in sender.sent[0][1]
```

If `WardenSupervisor` does not accept `message_sender` yet, this test should fail with an unexpected argument error.

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
uv run --group dev pytest tests/test_actions.py::test_notification_outbox_delivers_to_origin_subscribers -q
```

Expected: failure because sender injection and real send path do not exist.

- [ ] **Step 3: Add sender injection to supervisor**

Modify `kanban_warden/supervisor.py` constructor:

```python
from .delivery import HermesMessageSender, MessageSender
```

Change signature:

```python
message_sender: MessageSender | None = None,
```

Change drainer construction:

```python
self.outbox_drainer = NotificationOutboxDrainer(
    config,
    self.state_store,
    message_sender=message_sender or HermesMessageSender(),
)
```

- [ ] **Step 4: Implement origin delivery in outbox**

Update `NotificationOutboxDrainer.__init__`:

```python
from .delivery import HermesMessageSender, MessageSender, target_from_subscription
```

```python
def __init__(
    self,
    config: KanbanWardenConfig,
    state_store: WardenStateStore,
    *,
    message_sender: MessageSender | None = None,
) -> None:
    self.config = config
    self.state_store = state_store
    self.message_sender = message_sender or HermesMessageSender()
```

Replace `_deliver_one` read-only no-op with:

```python
with _readonly_connection(db_path) as con:
    if not _table_exists(con, "tasks") or not _task_exists(con, task_id):
        raise _RetryableDeliveryError("target task missing")
    subscribers = _native_subscribers(con, task_id)
if not subscribers:
    raise _RetryableDeliveryError("no native kanban subscriber for target task")
message = self._delivery_message(row, payload, board_name=board_name, task_id=task_id)
_assert_secret_safe(message)
targets = [target_from_subscription(subscriber) for subscriber in subscribers]
for target in targets:
    result = self.message_sender.send(target, message)
    if not result.ok:
        raise _RetryableDeliveryError(result.error or "message send failed")
```

Add helper:

```python
def _native_subscribers(con: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    if not _table_exists(con, "kanban_notify_subs"):
        return []
    rows = con.execute(
        "select platform, chat_id, thread_id, user_id, notifier_profile from kanban_notify_subs where task_id = ? order by platform, chat_id, thread_id",
        (task_id,),
    ).fetchall()
    return [dict(row) if isinstance(row, sqlite3.Row) else {
        "platform": row[0],
        "chat_id": row[1],
        "thread_id": row[2],
        "user_id": row[3],
        "notifier_profile": row[4],
    } for row in rows]
```

Add message builder:

```python
def _title_for_action(kind: str, reason: str) -> str:
    titles = {
        "create_reviewer": "Review required",
        "create_implementer_followup": "Changes requested",
        "retry": "Task retry planned",
        "escalate": "Retry exhausted",
        "promote": "Task can be promoted",
        "finalize": "Task can be finalized",
    }
    if kind == "notify" and reason:
        return reason.splitlines()[0][:80]
    return titles.get(kind, "Notification")
```

```python
def _delivery_message(
    self,
    row: dict[str, Any],
    payload: dict[str, Any],
    *,
    board_name: str,
    task_id: str,
) -> str:
    action_kind = _text(payload.get("kind"))
    reason = _text(payload.get("reason"))
    message = _text(payload.get("message"))
    title = _title_for_action(action_kind, reason)
    lines = [
        f"[Kanban Warden] {title}",
        "",
        f"Board: {board_name}",
        f"Task: {task_id}",
        f"Action: {action_kind or 'notify'}",
    ]
    if reason:
        lines.append(f"Reason: {reason[:240]}")
    if message:
        lines.extend(["", message[:800]])
    lines.extend(["", f"Outbox: {row['key']}"])
    return "\n".join(lines).strip()
```

- [ ] **Step 5: Run delivery test**

Run:

```bash
uv run --group dev pytest tests/test_actions.py::test_notification_outbox_delivers_to_origin_subscribers -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add kanban_warden/outbox.py kanban_warden/supervisor.py tests/test_actions.py
git commit -m "feat: deliver warden outbox via hermes send"
```

## Task 3: Retry, Secret, and Dry-Run Coverage

**Files:**
- Modify: `tests/test_actions.py`
- Modify if needed: `kanban_warden/outbox.py`

- [ ] **Step 1: Add failure retry test**

```python
def test_notification_outbox_send_failure_retries(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False, delivery_enabled=True, delivery_max_attempts=2)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('impl', 'Impl', 'blocked', 'hairou', 1, 'scratch')"
    )
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('impl', 'feishu', 'chat-1', '', 'user-1', 'hairou-feishu', 2, 0)"
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: inspect diff"}, 3)

    sender = FakeSender(fail=True)
    WardenSupervisor(config, profile_name="tester", message_sender=sender).dry_run(now=20)

    state = sqlite3.connect(config.resolved_state_db_path())
    rows = state.execute(
        "select status, attempts, last_error from notification_outbox order by key"
    ).fetchall()
    state.close()
    assert any(row[0] == "retrying" and row[1] == 1 and "synthetic send failure" in row[2] for row in rows)
```

- [ ] **Step 2: Add dry-run no-send test**

```python
def test_notification_outbox_dry_run_does_not_call_sender(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=True, delivery_enabled=True)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('impl', 'Impl', 'blocked', 'hairou', 1, 'scratch')"
    )
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('impl', 'feishu', 'chat-1', '', 'user-1', 'hairou-feishu', 2, 0)"
    )
    con.commit()
    con.close()
    _event(board, "impl", "blocked", {"reason": "review-required: inspect diff"}, 3)

    sender = FakeSender()
    report = WardenSupervisor(config, profile_name="tester", message_sender=sender).dry_run(now=20)

    assert report["outbox_delivery"]["dry_run"] is True
    assert sender.sent == []
```

- [ ] **Step 3: Add secret permanent failure test**

Use an AWS key-like string in the action message:

```python
def test_notification_outbox_secret_like_message_exhausts_without_sending(tmp_path: Path) -> None:
    config = _config(tmp_path, dry_run=False, delivery_enabled=True)
    board = Path(config.hermes_home or "") / "kanban.db"
    _init_real_schema_board(board)
    con = sqlite3.connect(board)
    con.execute(
        "insert into tasks(id, title, status, assignee, created_at, workspace_kind) values ('impl', 'Impl', 'blocked', 'hairou', 1, 'scratch')"
    )
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id) values ('impl', 'feishu', 'chat-1', '', 'user-1', 'hairou-feishu', 2, 0)"
    )
    con.commit()
    con.close()
    state = WardenStateStore(config.resolved_state_db_path())
    state.enqueue_notification(
        "secret-row",
        {
            "kind": "notify",
            "board_name": "default",
            "task_id": "impl",
            "reason": "manual test",
            "message": "leaked key AKIAIOSFODNN7EXAMPLE",
        },
    )

    sender = FakeSender()
    report = WardenSupervisor(config, profile_name="tester", message_sender=sender).dry_run(now=20)

    assert report["outbox_delivery"]["exhausted"] >= 1
    assert sender.sent == []
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run --group dev pytest tests/test_actions.py -q
```

Expected: all action tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_actions.py kanban_warden/outbox.py
git commit -m "test: cover warden send delivery reliability"
```

## Task 4: Documentation and Example Config

**Files:**
- Modify: `README.md`
- Modify: `examples/config.yaml`
- Modify: `scripts/check_hairou_warden.py` if delivery warnings need adjustment.

- [ ] **Step 1: Update README delivery wording**

In the configuration section, replace advanced wording that implies delivery only creates evidence with:

```markdown
- `notifications.delivery_enabled`: drains queued warden notifications by sending short messages to the task's existing `kanban_notify_subs` origin subscribers through Hermes `send_message`. Keep enabled for normal gateway profiles; disable for read-only diagnostics.
- `notifications.delivery_batch_size`, `delivery_max_attempts`, `delivery_backoff_seconds`, and `delivery_lease_seconds`: bound each supervisor tick, retry cadence, and stale `in_progress` lease recovery. Rows move through `queued`, `in_progress`, `delivered`, `retrying`, and `exhausted`.
```

- [ ] **Step 2: Update example config comment**

Ensure `examples/config.yaml` keeps:

```yaml
notifications:
  enabled: true
  channels:
    - origin
```

Do not add delivery knobs to the simple example unless the current example already includes them. The default config stays compact.

- [ ] **Step 3: Run config example test**

Run:

```bash
uv run --group dev pytest tests/test_config.py -q
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add README.md examples/config.yaml scripts/check_hairou_warden.py tests/test_config.py
git commit -m "docs: document plugin send delivery"
```

## Task 5: Full Validation

**Files:**
- No planned source edits unless validation fails.

- [ ] **Step 1: Run full validation**

Run:

```bash
uv run --group dev ruff check .
uv run --group dev mypy kanban_warden
uv run --group dev pytest
uv run --group dev python scripts/check_hairou_warden.py --config examples/config.yaml --skip-dry-run
uv run --group dev python scripts/verify_mvp.py
git diff --check
```

Expected:

- Ruff reports `All checks passed!`
- Mypy reports `Success: no issues found`
- Pytest passes all tests
- `check_hairou_warden.py` returns JSON with `"ok": true`
- `verify_mvp.py` returns JSON with `"ok": true`
- `git diff --check` exits zero

- [ ] **Step 2: Inspect final diff**

Run:

```bash
git status --short
git diff --stat origin/main...HEAD
```

Expected: only delivery, outbox, tests, README/config docs changed.

- [ ] **Step 3: Prepare handoff**

Summarize:

- design doc path
- plan path
- commits created
- validation output

Do not tag or release until the implementation branch is reviewed and merged.

## Self-Review

- Spec coverage: tasks cover adapter, outbox delivery, retry semantics, secret safety, dry-run behavior, and documentation.
- Placeholder scan: no TBD or open implementation placeholder remains.
- Type consistency: `SendTarget`, `DeliveryResult`, `MessageSender`, `HermesMessageSender`, and `target_from_subscription` are consistently named across tasks.
