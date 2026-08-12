"""The outbound delivery to the user across chat surfaces tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import BINARY_MIME_ALLOWLIST, redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import _SLACK_TS_RE, CHANNEL_ID_RE


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the messaging tools."""
    return [
        {
            "name": "send_message",
            "description": (
                "Send a message to the user. By default delivers a dashboard "
                'notification only. Set session="slack" to also send a Slack DM. '
                "Set 'channel' to target a tracked channel, or 'user' to DM an "
                "allowed user — specify at most one, not both. "
                "Use this whenever you decide someone should be notified — most "
                "commonly in silent cron jobs, but applicable any time proactive "
                "notification is needed."
                "\n\nsession param (optional):"
                "\n  omitted  — dashboard notification only (default)."
                '\n  "slack"  — Slack DM + dashboard notification.'
                '\n  "origin" — inject into the dashboard session that spawned'
                " this cron. Falls through to notification-only if origin is"
                " unreachable (tab closed, history deleted, or cron has no origin)."
                "\n\nExplicit channel=... or user=... always sends to Slack."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Message text. Also used as fallback when blocks are provided.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title for the notification",
                    },
                    "blocks": {
                        "type": "array",
                        "description": "Optional Slack Block Kit blocks array. When provided, the message is sent as a rich Block Kit message with text as fallback.",
                        "items": {"type": "object"},
                        "maxItems": 50,
                    },
                    "channel": {
                        "type": "string",
                        "description": "Target channel ID (e.g. C0123ABC456). Must be a tracked channel. Omit to send to owner DM.",
                    },
                    "user": {
                        "type": "string",
                        "description": "Target user ID (e.g. U0123ABC456) to DM. Must be an allowed user. Omit to send to owner DM.",
                    },
                    "unfurl_links": {
                        "type": "boolean",
                        "description": "Whether to unfurl URL link previews. Defaults to true.",
                    },
                    "unfurl_media": {
                        "type": "boolean",
                        "description": "Whether to unfurl media (images/video) previews. Defaults to true.",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": (
                            "Optional Slack thread timestamp (e.g. '1712793600.123456'). "
                            "When provided, the message is posted as a threaded reply under "
                            "that parent message. Works with 'channel' (thread in channel) "
                            "or 'user' (thread in DM)."
                        ),
                    },
                    "reply_broadcast": {
                        "type": "boolean",
                        "description": (
                            "When true and 'thread_ts' is set, also broadcast the threaded reply "
                            "to the channel's main message list. Requires 'thread_ts' — passing "
                            "reply_broadcast=true without thread_ts returns 400. Defaults to false."
                        ),
                    },
                    "session": {
                        "type": "string",
                        "enum": ["origin", "slack"],
                        "description": (
                            "Delivery routing. Omit for notification bell only (default). "
                            '"slack" adds Slack DM delivery. '
                            '"origin" injects into the dashboard session that spawned '
                            "this cron (falls back to notification if unreachable)."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "send_notification",
            "description": (
                "Publish a notification to the Kiro Crew notification center "
                "(bell feed) through the system.agent channel (RFC notification "
                "bus Phase 5). Unlike send_message, this is a pure notification: "
                "it never sends chat messages or DMs. "
                "Supports priority tiers, a dashboard-internal "
                "deep link, and group stacking. Use for structured, glanceable "
                "signals (job done, threshold crossed); use send_message for "
                "conversational delivery."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Notification title (required, <= 500 chars).",
                    },
                    "body": {
                        "type": "string",
                        "description": "Body text (markdown, <= 20000 chars).",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["critical", "default", "passive"],
                        "description": (
                            "critical = badge+sound+banner, default = badge+sound, "
                            "passive = feed-only. Omit for the channel default."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "Dashboard-internal deep link (path starting with '/', "
                            "e.g. '/schedule'). External URLs are rejected."
                        ),
                    },
                    "group_key": {
                        "type": "string",
                        "description": (
                            "Notes sharing a group_key collapse into one stack in "
                            "the feed. Use for repeated signals of the same kind."
                        ),
                    },
                    "actions": {
                        "type": "array",
                        "maxItems": 4,
                        "description": (
                            "Inline action buttons (max 4). Each item: "
                            '{"id": str (<=64), "label": str (<=40), "url"?: '
                            "dashboard-internal path (<=500)}. Rendered on the "
                            "notification card; url-less actions are legal but "
                            "render nothing today."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["id", "label"],
                        },
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": "delete_message",
            "description": (
                "Delete a message previously sent by this bot. Only works on "
                "messages authored by the Kiro Crew bot itself (Slack API constraint). "
                "Use to clean up transient notifications after the user acknowledges them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID where the message was posted.",
                    },
                    "ts": {
                        "type": "string",
                        "description": "Timestamp of the message to delete (from send_message response).",
                    },
                },
                "required": ["channel", "ts"],
            },
        },
        {
            "name": "read_slack_profile",
            "description": (
                "Read a Slack user's profile. Returns display name, title, "
                "status, timezone, and other profile fields. Rate limited to "
                "5 lookups per minute."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "Slack user ID (e.g. U0123ABC456).",
                    },
                },
                "required": ["user"],
            },
        },
        {
            "name": "file_send",
            "description": (
                "Send a file to the user. Copies the file to the outbox and "
                "notifies the dashboard/Slack with a download link. Use when "
                "you've generated a report, export, artifact, or any file the "
                "user should receive."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to send"},
                    "description": {
                        "type": "string",
                        "description": "Brief description of what the file is",
                    },
                    "channel": {
                        "type": "string",
                        "description": (
                            "Optional Slack channel ID (e.g. C0123ABC456) to upload "
                            "the file to. Must be a tracked channel the bot is a "
                            "member of. Omit to send to the owner's DM."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    ]


def send_message(name: str, args: dict[str, Any]) -> str:
    text = args["text"]
    title = args.get("title", "Agent Message")
    payload = {"text": text, "title": title}
    if args.get("blocks"):
        payload["blocks"] = args["blocks"]
    if args.get("channel"):
        payload["channel"] = args["channel"]
    if args.get("user"):
        payload["user"] = args["user"]
    if "unfurl_links" in args:
        payload["unfurl_links"] = args["unfurl_links"]
    if "unfurl_media" in args:
        payload["unfurl_media"] = args["unfurl_media"]
    if args.get("thread_ts"):
        payload["thread_ts"] = args["thread_ts"]
    if args.get("reply_broadcast"):
        payload["reply_broadcast"] = args["reply_broadcast"]
    if args.get("session"):
        if args["session"] not in ("origin", "slack"):
            return 'Error: session must be "origin" or "slack".'
        payload["session"] = args["session"]
    # Always tell the gateway when the caller is a cron — even on a bare
    # send (no session/channel) — so it can apply the documented
    # "cron → Slack DM by default" routing and report where the message
    # actually landed.
    caller_session = mcp_core._resolve_session_key()
    # Channel-agent containment: channel agents
    # communicate exclusively through channel posts. The channel.py
    # permission-request guard only fires when kiro-cli ASKS — an
    # auto-approved kirocrew-core call (default allowedTools) emits no
    # permission event — so the boundary must also hold here at MCP
    # dispatch, keyed on the verified caller identity.
    _chan_deny = mcp_core._deny_channel_agent_messaging(caller_session, "send_message")
    if _chan_deny:
        return _chan_deny
    is_cron = caller_session.startswith("cron:")
    if is_cron:
        payload["caller_session"] = caller_session
    # Governance: outbound messaging is a capability gate (exfil surface).
    # A policy/profile may disable proactive messaging for a surface/app.
    _gov_msg = mcp_core._vet_messaging_governance(caller_session)
    if _gov_msg:
        return f"Error: {_gov_msg}"
    # Governance: the per-transport ``channels`` allowlist is finer-grained
    # than the on/off messaging gate — a policy may permit messaging but
    # restrict it to specific transports (e.g. Slack only). Slack is the only
    # transport Kiro Crew sends over today. The gateway routes a send to Slack
    # whenever session=="slack" OR an explicit channel/user is set OR the
    # caller is a cron (see messaging.api_send_message), so we mirror that
    # exact predicate here — checking only session=="slack" would let a
    # channel=/user=-addressed send reach Slack while bypassing the gate. A
    # bare send (no session/channel/user, non-cron) is the in-process
    # dashboard notification path, governed by the messaging gate above.
    slack_bound = (
        payload.get("session") == "slack"
        or bool(payload.get("channel"))
        or bool(payload.get("user"))
        or is_cron
    )
    if slack_bound:
        _gov_chan = mcp_core._vet_channel_governance(caller_session, "slack")
        if _gov_chan:
            return f"Error: {_gov_chan}"
    resp = mcp_core._post("/api/send-message", payload)
    if not resp.get("ok"):
        return f"Failed: {resp}"
    # Prefer the gateway's explicit delivery channel when present
    # (delivered_to ∈ {"slack", "session", "notification"}); fall back to
    # the legacy slack/session booleans for older gateways.
    delivered_to = resp.get("delivered_to")
    ts = resp.get("ts", "")
    if delivered_to == "session" or (delivered_to is None and resp.get("session")):
        return "Message injected into target session."
    if delivered_to == "slack" or (delivered_to is None and resp.get("slack")):
        return (
            f"Message sent to Slack + notification. ts={ts}"
            if ts
            else "Message sent to Slack + notification."
        )
    # Reached the dashboard notification only. Warn loudly when Slack was
    # intended (explicit session=slack, or a cron — which now defaults to
    # Slack) so the caller can detect the miss and retry instead of
    # reading a success string for a notification-only send.
    if args.get("session") == "slack":
        return "⚠️ Slack unavailable — delivered as dashboard notification only (NOT in Slack)."
    if args.get("session"):
        return "Session injection unavailable — delivered as notification."
    if is_cron:
        return (
            "⚠️ Cron send reached the dashboard notification only — NOT posted to Slack "
            "(owner DM unavailable: no Slack client or owner_id). Verify Slack delivery."
        )
    return "Notification delivered."


def send_notification(name: str, args: dict[str, Any]) -> str:
    caller_session = mcp_core._resolve_session_key_strict()
    if not caller_session:
        return (
            "Error: cannot verify caller identity for send_notification "
            "(no gateway-injected session key or HMAC-verified pid). "
            "Refusing to publish without a trusted governance identity."
        )
    # Channel-agent containment: same boundary
    # as send_message — an auto-approved call emits no permission event,
    # so channel.py's guard alone cannot hold it.
    _chan_deny = mcp_core._deny_channel_agent_messaging(caller_session, "send_notification")
    if _chan_deny:
        return _chan_deny
    # Fail-closed: unlike send_message (chat reply surface, degrade-open
    # is the lesser harm), a notification publish is purely proactive —
    # a governance-evaluation error must deny, not bypass a configured
    # messaging denial (deny-by-default backend rule).
    _gov_note = mcp_core._vet_messaging_governance(
        caller_session, tool_name="send_notification", fail_closed=True
    )
    if _gov_note:
        return f"Error: {_gov_note}"
    payload = {"title": args["title"]}
    for key in ("body", "priority", "url", "group_key", "actions"):
        if args.get(key):
            payload[key] = args[key]
    resp = mcp_core._post("/api/notifications/agent", payload)
    if not resp.get("ok"):
        # "Error:" prefix (not "Failed:"): call_tool_with_logging
        # classifies only "Error:"-prefixed strings as failures, so a
        # "Failed:" return would be SEL-recorded as completed and hide
        # the error from the audit trail.
        return f"Error: {resp}"
    note = resp.get("note") or {}
    return (
        f"Notification published to the notification center "
        f"(channel={note.get('channel', 'system.agent')}, "
        f"priority={note.get('priority', 'default')})."
    )


def delete_message(name: str, args: dict[str, Any]) -> str:
    channel = args.get("channel", "")
    msg_ts = args.get("ts", "")
    if not CHANNEL_ID_RE.match(channel):
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="delete_message",
            outcome="error",
        )
        return "Error: invalid channel ID format."
    if not _SLACK_TS_RE.match(msg_ts):
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="delete_message",
            outcome="error",
        )
        return "Error: invalid message timestamp format."
    resp = mcp_core._post("/api/delete-message", {"channel": channel, "ts": msg_ts})
    if resp.get("error"):
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="delete_message",
            outcome="error",
        )
        return f"Failed: {resp['error']}"
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="delete_message",
        outcome="success",
    )
    return "Message deleted."


def read_slack_profile(name: str, args: dict[str, Any]) -> str:
    user_id = args["user"]
    resp = mcp_core._post("/api/slack-profile", {"user": user_id})
    if resp.get("error"):
        return f"Error: {resp['error']}"
    profile = resp.get("profile", {})
    # Defence-in-depth: redact profile values before returning to LLM.

    for key in list(profile):
        val = profile[key]
        if isinstance(val, str) and key != "id":
            val, _ = redact_exfiltration_urls(val)
            val, _ = redact_credentials(val)
            profile[key] = val
    return json.dumps(profile, indent=2)


def file_send(name: str, args: dict[str, Any]) -> str:
    src = Path(args.get("path", ""))
    desc = redact(args.get("description", ""))
    try:
        raw = safe_read_file_bytes(str(src))
    except FileTooLargeError as e:
        mcp_core.sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="denied",
            error=f"file_too_large: {e}",
        )
        return f"Error: {e}"
    if raw is None:
        mcp_core.sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="denied",
            error=f"path_not_allowed: {src}",
        )
        return f"Error: file not found or access denied: {src}"
    clean_name = src.name
    if redact(clean_name) != clean_name:
        mcp_core.sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="denied",
            error=f"sensitive_filename: {redact(clean_name)}",
        )
        return "Error: filename contains sensitive content. Rename the file first."
    # For text files, check content for sensitive data; binary files skip this
    # and validate MIME against the shared BINARY_MIME_ALLOWLIST (deny-by-default).
    is_text = True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        is_text = False
        guessed = mimetypes.guess_type(clean_name)[0] or ""
        if guessed not in BINARY_MIME_ALLOWLIST:
            mcp_core.sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error=f"binary_mime_not_allowed: {guessed}",
            )
            return f"Error: binary file type not allowed: {guessed or 'unknown'}. Allowed: audio, video, image, PDF."
        mcp_core.sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="info",
            error="binary_file_skipping_content_scan",
        )
    if is_text and redact(text) != text:
        mcp_core.sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="denied",
            error="sensitive_content_detected",
        )
        return "Error: file content contains sensitive data; send aborted"
    dest = mcp_core.outbox_dir() / clean_name
    try:
        with dest.open("xb") as f:
            f.write(raw)
    except FileExistsError:
        dest = (
            mcp_core.outbox_dir()
            / f"{Path(clean_name).stem}_{uuid.uuid4().hex}{Path(clean_name).suffix}"
        )
        dest.write_bytes(raw)
    mcp_core.sel().log_tool_invocation(
        session_key="mcp_core",
        source="mcp",
        tool_name="file_send",
        outcome="completed",
        resources=f"src={src} dest={dest}",
    )
    # Notify dashboard (renders file card in chat UI)
    d = mcp_core._post(
        "/api/outbox/notify",
        {
            "path": str(dest),
            "filename": dest.name,
            "description": desc,
            "size": dest.stat().st_size,
        },
    )
    if d.get("error"):
        return f"Error: {d['error']}"
    # Also upload to Slack when the caller's Slack identity permits it.
    #
    # Resolve identity as a THREE-state result (see
    # _classify_slack_identity). When strict resolution FAILS we must NOT
    # fall through to a threadless upload: with an explicit tracked channel
    # supplied the handler uploads at the CHANNEL ROOT (thread_ts=None +
    # channel), exposing a file meant for one thread to the whole channel —
    # a reachable cross-session disclosure (fail-OPEN w.r.t. audience). A
    # warm-pool-claimed Slack session is exactly such an unresolved caller.
    # Fail CLOSED for audience: refuse the Slack upload when the caller
    # cannot be attributed. A RESOLVED non-Slack session keeps its existing,
    # authorized routing (owner DM / session-map-linked thread / explicit
    # tracked channel) because its identity is known and none of those paths
    # broadcast at channel root for an unknown caller.
    identity, thread_ts = mcp_core._classify_slack_identity()
    slack_warning = ""
    if identity == "unresolved":
        mcp_core.sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="denied",
            downstream_service="slack",
            error="slack_identity_unresolved_upload_refused",
        )
        slack_warning = (
            " (Slack upload skipped: the caller's Slack identity could not "
            "be resolved, so a threaded upload cannot be guaranteed and a "
            "channel-root broadcast is refused. The file is available in "
            "the dashboard.)"
        )
    else:
        slack_resp = mcp_core._post(
            "/api/slack/upload-file",
            {
                "file_path": str(dest),
                "filename": dest.name,
                "thread_ts": thread_ts,
                "channel": args.get("channel", ""),
            },
        )
        if slack_resp.get("error"):
            slack_warning = f" (Slack upload failed: {slack_resp['error']})"
    msg = f"File sent: {dest.name} ({desc})" if desc else f"File sent: {dest.name}"
    return msg + slack_warning


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "send_message": send_message,
    "send_notification": send_notification,
    "delete_message": delete_message,
    "read_slack_profile": read_slack_profile,
    "file_send": file_send,
}
