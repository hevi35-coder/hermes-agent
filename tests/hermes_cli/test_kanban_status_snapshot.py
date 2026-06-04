import argparse
import json

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


def _init_board(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban-status.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    return kb.connect()


def test_root_status_snapshot_groups_descendants_and_explains_blockers(tmp_path, monkeypatch):
    conn = _init_board(tmp_path, monkeypatch)
    try:
        root = kb.create_task(conn, title="Slack root request", assignee="pm")
        ready = kb.create_task(conn, title="Ready child", assignee="worker", parents=[root])
        blocked = kb.create_task(conn, title="Needs user answer", assignee="worker")
        kb.block_task(conn, blocked, reason="needs_user_input: Which account should I use?")
        kb.link_tasks(conn, root, blocked)
        nested = kb.create_task(conn, title="Nested queued task", assignee="worker", parents=[blocked])

        snapshot = kb.build_root_status_snapshot(conn, root)
    finally:
        conn.close()

    assert snapshot["root"]["id"] == root
    assert snapshot["root"]["title"] == "Slack root request"
    assert snapshot["counts"]["blocked"] == 1
    assert snapshot["counts"]["todo"] >= 2
    assert {item["id"] for item in snapshot["blocked"]} == {blocked}
    assert snapshot["blocked"][0]["reason"] == "needs_user_input: Which account should I use?"
    assert snapshot["blocked"][0]["action_needed"] == "user"
    assert snapshot["blocked"][0]["parent_chain"] == [root]
    assert nested in {item["id"] for item in snapshot["queued"]}
    assert snapshot["total_tasks"] == 4


def test_compact_root_status_format_is_bounded_and_actionable(tmp_path, monkeypatch):
    conn = _init_board(tmp_path, monkeypatch)
    try:
        root = kb.create_task(conn, title="Slack root request", assignee="pm")
        child = kb.create_task(conn, title="Needs user answer", assignee="worker")
        kb.block_task(conn, child, reason="needs_user_input: Pick a region")
        kb.link_tasks(conn, root, child)
        snapshot = kb.build_root_status_snapshot(conn, root)
    finally:
        conn.close()

    text = kb.format_root_status_compact(snapshot)

    assert "Root" in text
    assert root in text
    assert "blocked=1" in text
    assert "사용자 답변 필요" in text
    assert "Pick a region" in text
    assert len(text.splitlines()) <= 8


def test_root_queue_digest_lists_queued_children_with_parent_context(tmp_path, monkeypatch):
    conn = _init_board(tmp_path, monkeypatch)
    try:
        root = kb.create_task(conn, title="Slack root request", assignee="pm")
        child = kb.create_task(conn, title="Collect source docs", assignee="research")
        nested = kb.create_task(conn, title="Draft final answer", assignee="writer")
        kb.link_tasks(conn, root, child)
        kb.link_tasks(conn, child, nested)
        snapshot = kb.build_root_status_snapshot(conn, root)
    finally:
        conn.close()

    text = kb.format_root_queue_digest(snapshot)

    assert "Kanban 대기 작업 요약" in text
    assert root in text
    assert child in text
    assert nested in text
    assert "Collect source docs" in text
    assert "Draft final answer" in text
    assert f"부모: {root}" in text
    assert f"부모: {root} > {child}" in text
    assert len(text.splitlines()) <= 10


def test_kanban_status_cli_emits_json_snapshot(tmp_path, monkeypatch, capsys):
    conn = _init_board(tmp_path, monkeypatch)
    try:
        root = kb.create_task(conn, title="CLI status root", assignee="pm")
        child = kb.create_task(conn, title="Waiting child", assignee="worker")
        kb.block_task(conn, child, reason="missing_tool_capability: browser")
        kb.link_tasks(conn, root, child)
    finally:
        conn.close()

    rc = cli._cmd_status(argparse.Namespace(task_id=root, json=True, verbose=False))
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 0
    assert payload["root"]["id"] == root
    assert payload["counts"]["blocked"] == 1
    assert payload["blocked"][0]["action_needed"] == "operator"


def test_blocker_classification_exposes_reason_category_and_action_owner(tmp_path, monkeypatch):
    conn = _init_board(tmp_path, monkeypatch)
    try:
        root = kb.create_task(conn, title="Root", assignee="pm")
        user_block = kb.create_task(conn, title="Needs answer", assignee="worker")
        system_block = kb.create_task(conn, title="Needs tools", assignee="safe")
        kb.block_task(conn, user_block, reason="needs_user_input: Which workspace should I use?")
        kb.block_task(conn, system_block, reason="missing_tool_capability: file_write unavailable")
        kb.link_tasks(conn, root, user_block)
        kb.link_tasks(conn, root, system_block)

        snapshot = kb.build_root_status_snapshot(conn, root)
    finally:
        conn.close()

    by_id = {item["id"]: item for item in snapshot["blocked"]}
    assert by_id[user_block]["blocker_reason"] == "needs_user_input"
    assert by_id[user_block]["action_owner"] == "user"
    assert by_id[user_block]["action_needed"] == "user"
    assert by_id[system_block]["blocker_reason"] == "missing_tool_capability"
    assert by_id[system_block]["action_owner"] == "system"
    assert by_id[system_block]["action_needed"] == "operator"


def test_dependency_style_notify_root_remains_user_facing_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "dependency-root.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        prereq = kb.create_task(conn, title="Prerequisite work", assignee="worker")
        root = kb.create_task(conn, title="User-facing Slack root", assignee="pm")
        kb.link_tasks(conn, prereq, root)
        kb.add_notify_sub(conn, task_id=root, platform="slack", chat_id="C123", thread_id="T1")

        recorded = kb.record_feedback_for_notify_thread(
            conn,
            platform="slack",
            chat_id="C123",
            thread_id="T1",
            text="AACC",
            user_id="U123",
        )
        claimed, snapshot, digest = kb.claim_root_queue_digest_for_sub(
            conn,
            task_id=root,
            platform="slack",
            chat_id="C123",
            thread_id="T1",
            min_interval_seconds=0,
        )
        comments_root = kb.list_comments(conn, root)
        comments_prereq = kb.list_comments(conn, prereq)
    finally:
        conn.close()

    assert recorded == root
    assert any("AACC" in comment.body for comment in comments_root)
    assert comments_prereq == []
    assert claimed is True
    assert snapshot["root"]["id"] == root
    assert "User-facing Slack root" in digest

