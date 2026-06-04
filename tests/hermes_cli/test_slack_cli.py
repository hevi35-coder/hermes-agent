"""Tests for Slack CLI helpers."""
from hermes_cli.slack_cli import (
    _build_full_manifest,
    _build_slack_scope_checklist,
    _extract_missing_scopes,
    _find_last_slack_inbound_event,
    _format_slack_doctor_text,
)


class TestSlackFullManifest:
    """Generated full Slack app manifest used by `hermes slack manifest`."""

    def test_app_home_messages_are_writable(self):
        manifest = _build_full_manifest("Hermes", "Your Hermes agent on Slack")

        assert manifest["features"]["app_home"] == {
            "home_tab_enabled": False,
            "messages_tab_enabled": True,
            "messages_tab_read_only_enabled": False,
        }

    def test_private_channel_directory_scope_is_included(self):
        manifest = _build_full_manifest("Hermes", "Your Hermes agent on Slack")

        bot_scopes = manifest["oauth_config"]["scopes"]["bot"]
        assert "groups:read" in bot_scopes

    def test_assistant_features_remain_enabled(self):
        manifest = _build_full_manifest("Hermes", "Your Hermes agent on Slack")

        assert "assistant_view" in manifest["features"]
        assert "assistant:write" in manifest["oauth_config"]["scopes"]["bot"]
        bot_events = manifest["settings"]["event_subscriptions"]["bot_events"]
        assert "assistant_thread_started" in bot_events


class TestSlackDoctorHelpers:
    """Pure helpers used by `hermes slack doctor`."""

    def test_scope_checklist_marks_commands_as_required(self):
        checklist = _build_slack_scope_checklist(
            installed_scopes=["chat:write", "app_mentions:read"],
            missing_scopes=[],
        )

        commands = next(item for item in checklist if item["scope"] == "commands")
        assert commands["expected"] is True
        assert commands["installed"] is False
        assert commands["status"] == "missing"

    def test_scope_checklist_marks_unknown_when_installed_scopes_unavailable(self):
        checklist = _build_slack_scope_checklist(installed_scopes=None, missing_scopes=[])

        commands = next(item for item in checklist if item["scope"] == "commands")
        assert commands["expected"] is True
        assert commands["installed"] is None
        assert commands["status"] == "unknown"

    def test_find_last_slack_inbound_event_extracts_latest_timestamp_and_chat(self):
        log_text = """
2026-06-04 20:12:32,752 INFO gateway.memory_monitor: [MEMORY]
2026-06-04 20:52:14,001 INFO gateway.run: inbound message: platform=slack user=이준협 chat=C123 msg='첫 번째'
2026-06-04 20:53:15,999 INFO gateway.run: response ready: platform=slack chat=C123 time=1.2s
2026-06-04 20:54:16,002 INFO gateway.run: inbound message: platform=slack user=이준협 chat=D456 msg='두 번째'
"""

        event = _find_last_slack_inbound_event(log_text)

        assert event == {
            "timestamp": "2026-06-04 20:54:16,002",
            "chat": "D456",
            "line": "2026-06-04 20:54:16,002 INFO gateway.run: inbound message: platform=slack user=이준협 chat=D456 msg='두 번째'",
        }

    def test_format_slack_doctor_text_includes_requested_checks(self):
        report = {
            "bot_auth": {"ok": True, "team": "Hermes", "user_id": "U123", "bot_id": "B123"},
            "app_auth": {"ok": True},
            "socket_mode": {"ok": True},
            "target_channel": {
                "id": "C123",
                "membership": {"ok": True, "is_member": True},
                "write": {"ok": True},
                "history": {"ok": False, "error": "missing_scope"},
            },
            "scope_checklist": [
                {"scope": "commands", "expected": True, "installed": False, "status": "missing"}
            ],
            "last_inbound": {"timestamp": "2026-06-04 20:54:16,002", "chat": "D456"},
        }

        text = _format_slack_doctor_text(report)

        assert "Bot token auth" in text
        assert "App token / Socket Mode" in text
        assert "Target channel membership" in text
        assert "Target channel write" in text
        assert "Target channel history" in text
        assert "commands" in text
        assert "Last inbound Slack event" in text

    def test_extract_missing_scopes_ignores_provided_installed_scopes(self):
        missing = _extract_missing_scopes(
            {"ok": False, "error": "missing_scope", "needed": "channels:history", "provided": "chat:write,commands"}
        )

        assert missing == ["channels:history"]
