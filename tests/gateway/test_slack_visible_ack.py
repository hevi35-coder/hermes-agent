import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban_db as kb


class AckProbeAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.SLACK)
        self.sent = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "group"}


def _event(text="새 작업 해줘"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="C123",
            chat_type="group",
            user_id="U123",
            thread_id="171000.000100",
        ),
        message_id="171000.000101",
    )


@pytest.mark.asyncio
async def test_slack_message_gets_visible_receipt_before_slow_agent(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_IMMEDIATE_ACK", "true")
    monkeypatch.setenv("HERMES_GATEWAY_ACK_DELAY_SECONDS", "0")
    adapter = AckProbeAdapter()
    release = asyncio.Event()

    async def slow_handler(event):
        await release.wait()
        return "최종 답변"

    adapter.set_message_handler(slow_handler)

    await adapter.handle_message(_event())
    await asyncio.sleep(0.05)

    assert adapter.sent, "사용자가 볼 수 있는 접수 ACK가 먼저 발송되어야 함"
    assert "접수" in adapter.sent[0]["content"]
    assert adapter.sent[0]["metadata"] == {"thread_id": "171000.000100"}

    release.set()
    await asyncio.sleep(0.05)
    assert adapter.sent[-1]["content"] == "최종 답변"


@pytest.mark.asyncio
async def test_slack_busy_followup_gets_visible_queue_ack(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK", "true")
    adapter = AckProbeAdapter()
    event = _event("AACC")
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    task = asyncio.current_task()
    assert task is not None
    adapter._session_tasks[session_key] = task

    async def never_called(event):
        raise AssertionError("busy follow-up should not start a new agent turn immediately")

    adapter.set_message_handler(never_called)

    await adapter.handle_message(event)

    assert session_key in adapter._text_debounce
    assert adapter._text_debounce[session_key].event.text == "AACC"
    assert adapter.sent, "busy follow-up에도 사용자-visible ACK가 필요함"
    assert "의견 접수" in adapter.sent[0]["content"]
    assert "반영" in adapter.sent[0]["content"]
    assert adapter.sent[0]["metadata"] == {"thread_id": "171000.000100"}


@pytest.mark.asyncio
async def test_slack_choice_reply_to_kanban_thread_is_recorded_and_acked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Root Slack request", assignee="pm")
        kb.add_notify_sub(
            conn,
            task_id=root,
            platform="slack",
            chat_id="C123",
            thread_id="171000.000100",
            notifier_profile="gateway",
        )
    finally:
        conn.close()

    adapter = AckProbeAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: adapter}
    runner._kanban_notifier_profile = "gateway"

    handled = await runner._capture_kanban_thread_feedback(_event("AACC"))

    assert handled is True
    assert adapter.sent
    assert "AACC" in adapter.sent[0]["content"]
    assert "기록" in adapter.sent[0]["content"]
    assert adapter.sent[0]["metadata"] == {"thread_id": "171000.000100"}

    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, root)
        assert any("AACC" in comment.body for comment in comments)
        events = kb.list_events(conn, root)
        feedback_events = [event for event in events if event.kind == "user_feedback_received"]
        assert feedback_events
        payload = feedback_events[-1].payload
        assert payload is not None
        assert payload["text"] == "AACC"
        assert payload["platform"] == "slack"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_slack_pending_clarify_reply_wins_over_kanban_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("SLACK_ALLOW_ALL_USERS", "true")
    kb.init_db()
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="Root Slack request", assignee="pm")
        kb.add_notify_sub(
            conn,
            task_id=root,
            platform="slack",
            chat_id="C123",
            thread_id="171000.000100",
            notifier_profile="gateway",
        )
    finally:
        conn.close()

    event = _event("A")
    adapter = AckProbeAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True)})
    runner.adapters = {Platform.SLACK: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = None
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner._kanban_notifier_profile = "gateway"
    runner._handle_message_with_agent = AsyncMock(return_value="agent should not run")

    from tools import clarify_gateway

    session_key = runner._session_key_for_source(event.source)
    entry = clarify_gateway.register(
        "clarify-1",
        session_key,
        question="Choose one",
        choices=["A", "B"],
    )
    clarify_gateway.mark_awaiting_text(entry.clarify_id)
    result = await runner._handle_message(event)

    assert result == ""
    assert entry.response == "A"
    runner._handle_message_with_agent.assert_not_awaited()
    assert adapter.sent == []

    conn = kb.connect()
    try:
        events = kb.list_events(conn, root)
        assert [event.kind for event in events if event.kind == "user_feedback_received"] == []
    finally:
        conn.close()
