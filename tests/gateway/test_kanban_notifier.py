import asyncio
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_root_notify_subscription_propagates_to_child_on_link(tmp_path, monkeypatch):
    """A root Slack/Telegram subscription must follow newly linked children.

    Slack-originated work is visible to users at the root request/thread level.
    If child cards do not inherit the root subscription, a blocked child goes
    silent even though it is the thing preventing the root request from moving.
    """
    db_path = tmp_path / "link-propagates-sub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Root request", assignee="pm")
        child = kb.create_task(conn, title="Draft child", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=root,
            platform="telegram",
            chat_id="chat-1",
            thread_id="thread-1",
            user_id="user-1",
            notifier_profile="gateway",
        )

        kb.link_tasks(conn, root, child)

        child_subs = kb.list_notify_subs(conn, child)
    finally:
        conn.close()

    assert len(child_subs) == 1
    assert child_subs[0]["task_id"] == child
    assert child_subs[0]["platform"] == "telegram"
    assert child_subs[0]["chat_id"] == "chat-1"
    assert child_subs[0]["thread_id"] == "thread-1"
    assert child_subs[0]["user_id"] == "user-1"
    assert child_subs[0]["notifier_profile"] == "gateway"
    assert int(child_subs[0]["last_event_id"]) == 0


def test_root_notify_subscription_propagates_to_existing_child_on_subscribe(tmp_path, monkeypatch):
    """Subscribing a root after decomposition also covers existing children."""
    db_path = tmp_path / "subscribe-propagates-sub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Root request", assignee="pm")
        child = kb.create_task(conn, title="Draft child", assignee="worker")
        kb.link_tasks(conn, root, child)

        kb.add_notify_sub(
            conn,
            task_id=root,
            platform="telegram",
            chat_id="chat-1",
            thread_id="thread-1",
            user_id="user-1",
            notifier_profile="gateway",
        )

        child_subs = kb.list_notify_subs(conn, child)
    finally:
        conn.close()

    assert len(child_subs) == 1
    assert child_subs[0]["task_id"] == child
    assert child_subs[0]["chat_id"] == "chat-1"
    assert child_subs[0]["thread_id"] == "thread-1"


def test_child_blocked_notification_names_parent_and_action_type(tmp_path, monkeypatch):
    """Child-block Slack messages should explain the root impact and who acts."""
    db_path = tmp_path / "child-blocked-context.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Prepare report", assignee="pm")
        kb.complete_task(conn, root, summary="decomposition ready")
        child = kb.create_task(conn, title="Write draft", assignee="safe")
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        kb.link_tasks(conn, root, child)
        kb.block_task(
            conn,
            child,
            reason="missing_tool_capability: file_write unavailable in safe profile",
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) >= 1
    text = next(msg["text"] for msg in adapter.sent if "blocked" in msg["text"])
    assert child in text
    assert root in text
    assert "Prepare report" in text
    assert "사용자 조치 필요 없음" in text
    assert "시스템/운영자 조치 필요" in text
    assert "missing_tool_capability" in text
    assert "Root" in text
    assert "상태:" in text
    assert "blocked=1" in text


def test_kanban_notifier_sends_rate_limited_root_queue_digest(tmp_path, monkeypatch):
    """Root subscribers should hear about queued child work even without terminal events."""
    db_path = tmp_path / "root-queue-digest.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_QUEUE_DIGEST_INTERVAL_SECONDS", "3600")
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="User visible root", assignee="pm")
        child = kb.create_task(conn, title="Queued child", assignee="worker")
        nested = kb.create_task(conn, title="Nested queued child", assignee="worker")
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        kb.link_tasks(conn, root, child)
        kb.link_tasks(conn, child, nested)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    digests = [msg["text"] for msg in adapter.sent if "Kanban 대기 작업 요약" in msg["text"]]
    assert len(digests) == 1
    assert root in digests[0]
    assert child in digests[0]
    assert nested in digests[0]
    assert "Queued child" in digests[0]
    assert "Nested queued child" in digests[0]
    assert f"부모: {root}" in digests[0]


class OnceFailingAdapter:
    def __init__(self):
        self.sent = []
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("transient digest send failure")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def test_kanban_notifier_retries_queue_digest_after_send_failure(tmp_path, monkeypatch):
    """A transient digest send failure must not hide queued work for the rate-limit window."""
    db_path = tmp_path / "root-queue-digest-retry.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_QUEUE_DIGEST_INTERVAL_SECONDS", "3600")
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Retry visible root", assignee="pm")
        child = kb.create_task(conn, title="Retry queued child", assignee="worker")
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        kb.link_tasks(conn, root, child)
    finally:
        conn.close()

    adapter = OnceFailingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.attempts == 2
    assert len(adapter.sent) == 1
    assert "Kanban 대기 작업 요약" in adapter.sent[0]["text"]
    assert child in adapter.sent[0]["text"]


def test_kanban_notifier_queue_digest_interval_prefers_config_over_env(tmp_path, monkeypatch):
    """Queue digest cadence should be configurable in config.yaml, not env-only."""
    db_path = tmp_path / "root-queue-digest-config.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_QUEUE_DIGEST_INTERVAL_SECONDS", "3600")
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Config digest root", assignee="pm")
        child = kb.create_task(conn, title="Config queued child", assignee="worker")
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        kb.link_tasks(conn, root, child)
    finally:
        conn.close()

    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"queue_digest_interval_seconds": 0}},
    )

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    digests = [msg["text"] for msg in adapter.sent if "Kanban 대기 작업 요약" in msg["text"]]
    assert len(digests) == 2


def test_inherited_child_subscription_stays_silent_when_root_tails_graph(tmp_path, monkeypatch):
    """Root + inherited child rows must not double-deliver the same child event."""
    db_path = tmp_path / "root-child-dedupe.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Root request", assignee="pm")
        child = kb.create_task(conn, title="Child work", assignee="worker")
        kb.add_notify_sub(conn, task_id=root, platform="telegram", chat_id="chat-1")
        kb.link_tasks(conn, root, child)
        assert len(kb.list_notify_subs(conn, child)) == 1
        promoted, reason = kb.promote_task(conn, child, actor="test", force=True)
        assert promoted, reason
        kb.block_task(conn, child, reason="needs_user_input: choose A or B")

        _root_cursor, root_events = kb.unseen_events_for_sub(
            conn,
            task_id=root,
            platform="telegram",
            chat_id="chat-1",
            kinds=["blocked"],
        )
        _child_cursor, child_events = kb.unseen_events_for_sub(
            conn,
            task_id=child,
            platform="telegram",
            chat_id="chat-1",
            kinds=["blocked"],
        )
    finally:
        conn.close()

    assert [event.task_id for event in root_events] == [child]
    assert child_events == []

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    blocked_messages = [msg for msg in adapter.sent if "blocked" in msg["text"].lower()]
    assert len(blocked_messages) == 1
    assert child in blocked_messages[0]["text"]


def test_notify_scope_does_not_leak_sibling_roots_through_shared_dependency(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-dependency-no-leak.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        shared = kb.create_task(conn, title="Shared dependency", assignee="worker")
        root_a = kb.create_task(conn, title="Root A private request", assignee="pm")
        root_b = kb.create_task(conn, title="Root B private request", assignee="pm")
        kb.link_tasks(conn, shared, root_a)
        kb.link_tasks(conn, shared, root_b)
        kb.add_notify_sub(conn, task_id=root_a, platform="telegram", chat_id="chat-a")
        kb.add_notify_sub(conn, task_id=root_b, platform="telegram", chat_id="chat-b")
        promoted, reason = kb.promote_task(conn, root_b, actor="test", force=True)
        assert promoted, reason
        kb.block_task(conn, root_b, reason="needs_user_input: private B choice")

        _cursor_a, events_a = kb.unseen_events_for_sub(
            conn,
            task_id=root_a,
            platform="telegram",
            chat_id="chat-a",
            kinds=["blocked"],
        )
        _cursor_b, events_b = kb.unseen_events_for_sub(
            conn,
            task_id=root_b,
            platform="telegram",
            chat_id="chat-b",
            kinds=["blocked"],
        )
    finally:
        conn.close()

    assert events_a == []
    assert [event.task_id for event in events_b] == [root_b]
