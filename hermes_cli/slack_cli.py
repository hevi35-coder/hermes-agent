"""``hermes slack ...`` CLI subcommands.

``hermes slack manifest`` generates the Slack app manifest JSON for
registering every gateway command as a native Slack slash (``/btw``,
``/stop``, ``/model``, …) so users get the same first-class slash UX
Discord and Telegram already have. ``hermes slack doctor`` runs live,
non-secret Slack gateway readiness checks.

Typical workflow::

    $ hermes slack manifest > slack-manifest.json
    # or:
    $ hermes slack manifest --write

Then paste the printed JSON into the Slack app config (Features → App
Manifest → Edit) and click Save. Slack diffs the manifest and prompts
for reinstall when scopes/commands change.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _build_full_manifest(bot_name: str, bot_description: str) -> dict:
    """Build a full Slack manifest merging display info + our slash list.

    The slash-command list is always generated from ``COMMAND_REGISTRY`` so
    it stays in sync with the rest of Hermes. Other manifest sections
    (display info, OAuth scopes, socket mode) are set to sensible defaults
    for a Hermes deployment — users can tweak them in the Slack UI after
    pasting.
    """
    from hermes_cli.commands import slack_app_manifest

    partial = slack_app_manifest()
    slashes = partial["features"]["slash_commands"]

    return {
        "_metadata": {
            "major_version": 1,
            "minor_version": 1,
        },
        "display_information": {
            "name": bot_name[:35],
            "description": (bot_description or "Your Hermes agent on Slack")[:140],
            "background_color": "#1a1a2e",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": bot_name[:80],
                "always_online": True,
            },
            "slash_commands": slashes,
            "assistant_view": {
                "assistant_description": "Chat with Hermes in threads and DMs.",
            },
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "assistant:write",
                    "channels:history",
                    "channels:read",
                    "chat:write",
                    "commands",
                    "files:read",
                    "files:write",
                    "groups:history",
                    "groups:read",
                    "im:history",
                    "im:read",
                    "im:write",
                    "users:read",
                ],
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": [
                    "app_mention",
                    "assistant_thread_context_changed",
                    "assistant_thread_started",
                    "message.channels",
                    "message.groups",
                    "message.im",
                ],
            },
            "interactivity": {
                "is_enabled": True,
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def _expected_slack_bot_scopes() -> list[str]:
    """Return bot scopes expected by Hermes' generated Slack manifest."""
    manifest = _build_full_manifest("Hermes", "Your Hermes agent on Slack")
    return list(manifest["oauth_config"]["scopes"]["bot"])


def _expected_slack_bot_events() -> list[str]:
    """Return bot events expected by Hermes' generated Slack manifest."""
    manifest = _build_full_manifest("Hermes", "Your Hermes agent on Slack")
    return list(manifest["settings"]["event_subscriptions"]["bot_events"])


def _build_slack_scope_checklist(
    *,
    installed_scopes: list[str] | None,
    missing_scopes: list[str] | None,
) -> list[dict[str, Any]]:
    """Build the expected-manifest scope checklist for Slack doctor.

    Slack does not consistently expose installed bot scopes through a bot/app
    token in every workspace. When we cannot enumerate installed scopes, keep
    the expected list visible and mark each scope as ``unknown`` unless an API
    probe explicitly returned it in a ``needed`` missing-scope field.
    """
    installed = set(installed_scopes) if installed_scopes is not None else None
    missing = set(missing_scopes or [])
    checklist: list[dict[str, Any]] = []
    for scope in _expected_slack_bot_scopes():
        if installed is None:
            status = "missing" if scope in missing else "unknown"
            installed_value: bool | None = False if scope in missing else None
        elif scope in installed:
            status = "present"
            installed_value = True
        else:
            status = "missing"
            installed_value = False
        checklist.append(
            {
                "scope": scope,
                "expected": True,
                "installed": installed_value,
                "status": status,
            }
        )
    return checklist


def _find_last_slack_inbound_event(log_text: str) -> dict[str, str] | None:
    """Return the latest Slack inbound event parsed from gateway.log text."""
    last: dict[str, str] | None = None
    pattern = re.compile(
        r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?"
        r"inbound message: platform=slack\b.*?\bchat=(?P<chat>\S+)",
    )
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        match = pattern.search(line)
        if match:
            last = {
                "timestamp": match.group("timestamp"),
                "chat": match.group("chat"),
                "line": line,
            }
    return last


def _status_icon(ok: bool | None) -> str:
    if ok is True:
        return "✓"
    if ok is False:
        return "✗"
    return "?"


def _format_bool(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return "unknown"
    return str(value)


def _format_slack_doctor_text(report: dict[str, Any]) -> str:
    """Render a human-readable Slack doctor report."""
    lines = ["Slack doctor", "============", ""]

    bot_auth = report.get("bot_auth") or {}
    bot_ok = bool(bot_auth.get("ok"))
    bot_detail = []
    for key in ("team", "user", "user_id", "bot_id", "error"):
        if bot_auth.get(key):
            bot_detail.append(f"{key}={bot_auth[key]}")
    lines.append(f"{_status_icon(bot_ok)} Bot token auth: " + (", ".join(bot_detail) or _format_bool(bot_ok)))

    app_auth = report.get("app_auth") or {}
    app_ok = bool(app_auth.get("ok"))
    if app_auth.get("error"):
        app_detail = f"error={app_auth['error']}"
    else:
        app_detail = _format_bool(app_ok)
    lines.append(f"{_status_icon(app_ok)} App token auth: {app_detail}")

    socket_mode = report.get("socket_mode") or {}
    socket_ok = bool(socket_mode.get("ok"))
    socket_detail = "connections:write ok" if socket_ok else f"error={socket_mode.get('error', 'unknown')}"
    if socket_mode.get("last_connected_at"):
        socket_detail += f", last log connect={socket_mode['last_connected_at']}"
    lines.append(f"{_status_icon(socket_ok)} App token / Socket Mode: {socket_detail}")

    target = report.get("target_channel") or {}
    if target:
        lines.append("")
        lines.append(f"Target channel: {target.get('id')}")
        membership = target.get("membership") or {}
        membership_ok = bool(membership.get("ok")) and membership.get("is_member") is not False
        membership_detail = f"is_member={_format_bool(membership.get('is_member'))}"
        if membership.get("name"):
            membership_detail += f", name={membership['name']}"
        if membership.get("error"):
            membership_detail += f", error={membership['error']}"
        lines.append(f"{_status_icon(membership_ok)} Target channel membership: {membership_detail}")

        write = target.get("write") or {}
        write_ok = write.get("ok")
        write_detail = "ok" if write_ok else f"error={write.get('error', 'skipped')}"
        if write.get("ts"):
            write_detail += f", ts={write['ts']}"
        lines.append(f"{_status_icon(write_ok if write else None)} Target channel write: {write_detail}")

        history = target.get("history") or {}
        history_ok = history.get("ok")
        history_detail = "ok" if history_ok else f"error={history.get('error', 'skipped')}"
        lines.append(f"{_status_icon(history_ok if history else None)} Target channel history: {history_detail}")

    lines.append("")
    lines.append("Expected manifest scope checklist:")
    for item in report.get("scope_checklist") or []:
        scope = item["scope"]
        status = item["status"]
        icon = "✓" if status == "present" else ("✗" if status == "missing" else "?")
        marker = "  <-- required for native slash commands" if scope == "commands" else ""
        lines.append(f"  {icon} {scope}: {status}{marker}")

    expected_events = report.get("expected_bot_events") or []
    if expected_events:
        lines.append("")
        lines.append("Expected bot events: " + ", ".join(expected_events))

    last_inbound = report.get("last_inbound")
    lines.append("")
    if last_inbound:
        lines.append(
            f"✓ Last inbound Slack event: {last_inbound.get('timestamp')} chat={last_inbound.get('chat')}"
        )
    else:
        lines.append("? Last inbound Slack event: none found in gateway.log")

    warnings = report.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)

    return "\n".join(lines) + "\n"


def _load_profile_env_for_slack_doctor() -> None:
    """Best-effort load of profile .env values when command code needs them."""
    try:
        from hermes_constants import get_hermes_home

        env_path = Path(get_hermes_home()) / ".env"
    except Exception:
        env_path = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")) / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith("SLACK_") and key not in os.environ:
            os.environ[key] = value


def _slack_api_call(token: str, method: str, data: dict[str, Any] | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - exercised by live doctor only
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _extract_missing_scopes(*responses: dict[str, Any]) -> list[str]:
    """Return only scopes Slack explicitly reports as missing.

    Slack ``missing_scope`` responses use ``needed`` for the absent scope(s)
    and ``provided`` for the scopes already available to the token. Treating
    ``provided`` as missing makes a healthy install look broken, so this helper
    intentionally ignores it.
    """
    scopes: set[str] = set()
    for response in responses:
        value = response.get("needed")
        if isinstance(value, str):
            for piece in re.split(r"[,\s]+", value):
                if piece and ((":" in piece) or piece == "commands"):
                    scopes.add(piece)
    return sorted(scopes)


def _find_last_socket_connected_event(log_text: str) -> str | None:
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Socket Mode connected")
    last = None
    for raw_line in log_text.splitlines():
        match = pattern.search(raw_line.strip())
        if match:
            last = match.group(1)
    return last


def _read_gateway_log_text() -> str:
    try:
        from hermes_constants import get_hermes_home

        log_path = Path(get_hermes_home()) / "logs" / "gateway.log"
    except Exception:
        log_path = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")) / "logs" / "gateway.log"
    try:
        return log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def slack_doctor_command(args) -> int:
    """Run non-secret Slack gateway diagnostics."""
    _load_profile_env_for_slack_doctor()
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    channel = getattr(args, "channel", None) or os.environ.get("SLACK_HOME_CHANNEL", "").strip() or None
    timeout = float(getattr(args, "timeout", 15.0) or 15.0)

    warnings: list[str] = []
    api_responses: list[dict[str, Any]] = []

    if bot_token:
        bot_auth = _slack_api_call(bot_token, "auth.test", timeout=timeout)
        api_responses.append(bot_auth)
    else:
        bot_auth = {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
        warnings.append("SLACK_BOT_TOKEN is not configured.")

    if app_token:
        app_auth = _slack_api_call(app_token, "auth.test", timeout=timeout)
        socket_mode = _slack_api_call(app_token, "apps.connections.open", timeout=timeout)
        api_responses.extend([app_auth, socket_mode])
    else:
        app_auth = {"ok": False, "error": "SLACK_APP_TOKEN not set"}
        socket_mode = {"ok": False, "error": "SLACK_APP_TOKEN not set"}
        warnings.append("SLACK_APP_TOKEN is not configured.")

    target_report: dict[str, Any] | None = None
    if channel:
        target_report = {"id": channel}
        if bot_token:
            membership_response = _slack_api_call(
                bot_token,
                "conversations.info",
                {"channel": channel},
                timeout=timeout,
            )
            api_responses.append(membership_response)
            channel_info = membership_response.get("channel") or {}
            target_report["membership"] = {
                "ok": bool(membership_response.get("ok")),
                "error": membership_response.get("error"),
                "name": channel_info.get("name"),
                "is_member": channel_info.get("is_member"),
            }

            history_response = _slack_api_call(
                bot_token,
                "conversations.history",
                {"channel": channel, "limit": "1"},
                timeout=timeout,
            )
            api_responses.append(history_response)
            target_report["history"] = {
                "ok": bool(history_response.get("ok")),
                "error": history_response.get("error"),
            }

            if getattr(args, "send_test_message", False):
                message = (
                    "[Hermes Slack doctor] target channel write check. "
                    f"ts={time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                write_response = _slack_api_call(
                    bot_token,
                    "chat.postMessage",
                    {"channel": channel, "text": message},
                    timeout=timeout,
                )
                api_responses.append(write_response)
                target_report["write"] = {
                    "ok": bool(write_response.get("ok")),
                    "error": write_response.get("error"),
                    "ts": write_response.get("ts"),
                }
            else:
                target_report["write"] = {
                    "ok": None,
                    "error": "skipped; pass --send-test-message to perform chat.postMessage",
                }
        else:
            target_report["membership"] = {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
            target_report["history"] = {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
            target_report["write"] = {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
    else:
        warnings.append("No target channel provided and SLACK_HOME_CHANNEL is not set; channel checks skipped.")

    log_text = _read_gateway_log_text()
    last_connected_at = _find_last_socket_connected_event(log_text)
    if last_connected_at:
        socket_mode["last_connected_at"] = last_connected_at

    missing_scopes = _extract_missing_scopes(*api_responses)
    scope_checklist = _build_slack_scope_checklist(
        installed_scopes=None,
        missing_scopes=missing_scopes,
    )

    if any(item["scope"] == "commands" and item["status"] != "present" for item in scope_checklist):
        warnings.append(
            "Cannot prove installed Slack scopes from bot/app token alone; verify the Slack app has the `commands` bot scope and reinstall after manifest changes."
        )

    report: dict[str, Any] = {
        "bot_auth": {
            "ok": bool(bot_auth.get("ok")),
            "error": bot_auth.get("error"),
            "team": bot_auth.get("team"),
            "team_id": bot_auth.get("team_id"),
            "user": bot_auth.get("user"),
            "user_id": bot_auth.get("user_id"),
            "bot_id": bot_auth.get("bot_id"),
        },
        "app_auth": {
            "ok": bool(app_auth.get("ok")),
            "error": app_auth.get("error"),
        },
        "socket_mode": {
            "ok": bool(socket_mode.get("ok")),
            "error": socket_mode.get("error"),
            "last_connected_at": socket_mode.get("last_connected_at"),
        },
        "target_channel": target_report,
        "scope_checklist": scope_checklist,
        "expected_bot_events": _expected_slack_bot_events(),
        "last_inbound": _find_last_slack_inbound_event(log_text),
        "warnings": warnings,
    }

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(_format_slack_doctor_text(report))
    return 0 if report["bot_auth"]["ok"] and report["socket_mode"]["ok"] else 1



def slack_manifest_command(args) -> int:
    """Print or write a Slack app manifest JSON.

    Flags (all parsed in ``hermes_cli/main.py``):
      --write [PATH]  Write to file instead of stdout (default path:
                      ``$HERMES_HOME/slack-manifest.json``)
      --name NAME     Override the bot display name (default: "Hermes")
      --description DESC  Override the bot description
      --slashes-only  Emit only the ``features.slash_commands`` array (for
                      merging into an existing manifest manually)
    """
    name = getattr(args, "name", None) or "Hermes"
    description = getattr(args, "description", None) or "Your Hermes agent on Slack"

    if getattr(args, "slashes_only", False):
        from hermes_cli.commands import slack_app_manifest

        manifest = slack_app_manifest()["features"]["slash_commands"]
    else:
        manifest = _build_full_manifest(name, description)

    payload = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    write_target = getattr(args, "write", None)
    if write_target is not None:
        if isinstance(write_target, bool) and write_target:
            # --write with no value → default location
            try:
                from hermes_constants import get_hermes_home

                target = Path(get_hermes_home()) / "slack-manifest.json"
            except Exception:
                target = Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")) / "slack-manifest.json"
        else:
            target = Path(write_target).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"Slack manifest written to: {target}", file=sys.stderr)
        print(
            "\nNext steps:\n"
            "  1. Open https://api.slack.com/apps and pick your Hermes app\n"
            "     (or create a new one: Create New App → From an app manifest).\n"
            f"  2. Features → App Manifest → paste the contents of\n"
            f"     {target}\n"
            "  3. Save; Slack will prompt to reinstall the app if scopes or\n"
            "     slash commands changed.\n"
            "  4. Make sure Socket Mode is enabled and you have a bot token\n"
            "     (xoxb-...) and app token (xapp-...) configured via\n"
            "     `hermes setup`.\n",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload)
    return 0
