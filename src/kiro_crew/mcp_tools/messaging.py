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
from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import BINARY_MIME_ALLOWLIST, redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import _SLACK_TS_RE, CHANNEL_ID_RE

#: ``session`` values that name a chat channel rather than a delivery mode. Each
#: one delivers a DM to that channel's own configured owner: the gateway resolves
#: the destination from the transport's configured-target allowlist, so the
#: agent cannot address anyone the user has not configured for that channel.
#:
#: DERIVED from the channel roster, not hand-listed, for the reason
#: ``SEND_MESSAGE_SCHEMA`` already gives about ``channel_type``: a second copy of
#: the roster goes stale when a transport is added. This one had. It read
#: ``("discord",)`` while the gateway leg it feeds
#: (``dashboard/handlers/messaging.py::_deliver_channel_dm``) was already
#: channel-neutral — it looks the transport up by name and takes the destination
#: from that transport's own ``configured_targets()`` — so the eight other
#: registered channels were refused HERE, by a validator, on their way to a leg
#: that could serve them.
#:
#: Widening the accepted set does not widen the reachable AUDIENCE. Whether a
#: given channel can actually take a proactive DM is answered per send by
#: ``_owner_dm_target``, which returns a target only when the transport
#: advertises exactly ONE available direct target: a channel that needs prior
#: inbound state (Teams, WeCom) or cannot DM proactively at all (Feishu) marks
#: its targets unavailable and still degrades to the dashboard notification, and
#: an ambiguous allow-list is refused rather than guessed at. That gate is at the
#: side-effect boundary and reads live config, which is where it has to be — a
#: static tuple here cannot answer a runtime question.
#:
#: The two exclusions live with the roster in ``constants.CHANNEL_SEND_NAMESPACES``,
#: which the gateway's accepted ``channel_type`` set and the validator's pattern
#: also read, so the subtraction is not respelled per reader. This leg reads the
#: narrower ``CHANNEL_OWNER_DM_NAMESPACES``, because inferring an OWNER needs the
#: transport to tell configured recipients from peers learned off inbound traffic
#: and not every one does -- the reason is recorded at that definition.
_CHANNEL_SESSIONS: tuple[str, ...] = CHANNEL_OWNER_DM_NAMESPACES

#: Every accepted ``session`` value. The advertised enum is built from this, so
#: the contract the model is shown and the validation a call is held to cannot
#: drift apart.
_SESSION_TARGETS: tuple[str, ...] = ("origin", "slack", *_CHANNEL_SESSIONS)

#: Options that exist only in Slack's protocol: a Block Kit layout, a Slack
#: channel/user id, a Slack thread timestamp and its broadcast flag, and Slack's
#: link/media unfurling. Combining one with a ``_CHANNEL_SESSIONS`` value is
#: refused rather than delivered with the option dropped, because the caller has
#: no way to observe the drop: a threaded reply would arrive as a fresh DM, and a
#: send addressed at a named Slack channel would land in a private DM instead.
_SLACK_ONLY_FIELDS: tuple[str, ...] = (
    "channel",
    "user",
    "blocks",
    "thread_ts",
    "reply_broadcast",
    "unfurl_links",
    "unfurl_media",
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the messaging tools."""
    return [
        {
            "name": "send_message",
            "description": (
                "Send a message to the user. By default delivers a dashboard "
                "notification only. Use this whenever you decide someone should "
                "be notified — most commonly in silent cron jobs, but applicable "
                "any time proactive notification is needed."
                "\n\nsession param (optional):"
                "\n  omitted   — dashboard notification only (default)."
                '\n  "slack"   — Slack DM + dashboard notification.'
                '\n  a channel name ("discord", "telegram", "webex", "teams",'
                ' "whatsapp", "imessage", "feishu") — a DM to'
                " that channel's own configured owner + dashboard notification."
                " Only a destination the user already allow-listed for that channel"
                " is reachable, and only when exactly one is configured: an"
                " ambiguous allow-list, a channel that is not connected, and a"
                " channel that cannot DM proactively all fall back to the"
                " dashboard notification and say so rather than reporting success."
                ' Not "wecom" or "weixin": each advertises peers learned from'
                " inbound traffic beside configured ones, so an owner cannot be told"
                " apart from whoever messaged the bot. Reach those with channel_type,"
                " which addresses a conversation instead of inferring a recipient."
                '\n  "origin"  — inject into the dashboard session that spawned'
                " this cron. Falls through to notification-only if origin is"
                " unreachable (tab closed, history deleted, or cron has no origin)."
                "\n\nSlack only: set 'channel' to target a tracked channel, or "
                "'user' to DM an allowed user, at most one, not both, and either "
                "one always sends to Slack. channel, user, blocks, thread_ts, "
                "reply_broadcast and unfurl_links/unfurl_media are Slack protocol "
                "options: combining any of them with a channel session is REFUSED, "
                "not silently ignored."
                "\n\nOn a non-Slack messaging channel (Telegram, Discord, Teams, "
                "Webex, WeCom, Weixin, WhatsApp, iMessage) set 'channel_type' to "
                "that channel's name to post into the conversation you are already "
                "talking in. Use it in preference to a channel session whenever you "
                "are talking on that channel — it reaches the conversation at hand, "
                "where a channel session reaches the channel's configured owner "
                "wherever the call came from. The Slack-only options above are "
                "rejected alongside it."
                "\n\nTo reach a NON-Slack channel (Webex, Telegram, Discord, …) at"
                " a specific destination, pass channel_type plus target_id, where"
                " target_id is one of the opaque ids that channel exposes as a"
                " configured destination. The channel's own allow-list is"
                " re-checked when the message is sent, so an id that is no longer"
                " configured is refused. This pair addresses the destination"
                " directly, so combining it with session or any Slack option"
                " (channel, user, blocks, thread_ts, reply_broadcast, unfurl_*) is"
                " REFUSED, not silently ignored."
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
                        "description": "Slack-only. Target channel ID (e.g. C0123ABC456). Must be a tracked channel. Omit to send to owner DM.",
                    },
                    "user": {
                        "type": "string",
                        "description": "Slack-only. Target user ID (e.g. U0123ABC456) to DM. Must be an allowed user. Omit to send to owner DM.",
                    },
                    "channel_type": {
                        "type": "string",
                        "description": (
                            "Deliver into the non-Slack messaging conversation this "
                            "session belongs to, named by its transport: "
                            '"telegram", "discord", "teams", "webex", "wecom", '
                            '"weixin", "whatsapp" or "imessage". Use it when the '
                            "[RUNTIME] marker says you are talking over one of "
                            "those channels and you want a proactive message "
                            "(a silent cron's report, a finished background task) "
                            "to reach the user THERE rather than only in the "
                            'dashboard bell. Not for Slack — use session="slack". '
                            "Cannot be combined with 'channel', 'user' or "
                            "'thread_ts', which are Slack-only routing fields. "
                            "Pass target_id alongside it to name an EXPLICIT "
                            "destination on that transport instead of this "
                            "session's own conversation."
                        ),
                    },
                    "target_id": {
                        "type": "string",
                        "description": (
                            "Opaque configured-destination id on channel_type "
                            "(e.g. 'user:someone@example.com'), as that channel "
                            "advertises it. Requires channel_type. Omit it to reach "
                            "the conversation this session already belongs to. The "
                            "channel's own allow-list is re-checked at send time, so "
                            "an id that is no longer configured is refused."
                        ),
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
                        "enum": list(_SESSION_TARGETS),
                        "description": (
                            "Delivery routing. Omit for notification bell only (default). "
                            '"slack" adds Slack DM delivery. A channel name (e.g. '
                            '"discord", "webex", "telegram") sends a DM on that channel '
                            "to its configured owner instead; it takes none of the "
                            "Slack-only options above, and falls back to the "
                            "notification (saying so) when that channel is not "
                            "connected or no single configured recipient can be "
                            'resolved. "origin" injects into the dashboard session '
                            "that spawned this cron (falls back to notification if "
                            "unreachable)."
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
                "notifies the dashboard with a download link. When this "
                "session is linked to a Telegram conversation the file is "
                "also delivered there natively; otherwise it uploads to "
                "Slack when the caller's Slack identity permits it. Use "
                "when you've generated a report, export, artifact, or any "
                "file the user should receive."
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
    session = args.get("session") or ""
    if session and session not in _SESSION_TARGETS:
        return f"Error: session must be one of {', '.join(_SESSION_TARGETS)}."
    if session in _CHANNEL_SESSIONS:
        # Validated before anything is built or posted: a refusal must not have
        # already delivered part of the send. See _SLACK_ONLY_FIELDS for why the
        # option is refused rather than dropped. Presence, not truthiness --
        # unfurl_links=False is still a Slack option the caller asked for.
        slack_only = [field for field in _SLACK_ONLY_FIELDS if args.get(field) is not None]
        if slack_only:
            return (
                f'Error: session="{session}" cannot carry the Slack-only '
                f"option(s) {', '.join(slack_only)}. Re-send without them, or use "
                'session="slack" if Slack is the surface you meant.'
            )
    text = args["text"]
    title = args.get("title", "Agent Message")
    payload = {"text": text, "title": title}
    # ``channel_type`` names a non-Slack transport and is mutually exclusive with
    # the Slack-only routing fields. Refused with a message rather than resolved
    # by precedence: either order silently drops a destination the caller named,
    # and the caller cannot tell which one it lost.
    channel_type = str(args.get("channel_type") or "").strip()
    if channel_type:
        conflicting = [key for key in ("channel", "user", "thread_ts") if args.get(key)]
        if args.get("session") == "slack":
            conflicting.append('session="slack"')
        if conflicting:
            return (
                f"Error: channel_type={channel_type!r} cannot be combined with "
                f"{', '.join(conflicting)} — those route to Slack only. Send one "
                "message per destination."
            )
        payload["channel_type"] = channel_type
    if args.get("blocks"):
        payload["blocks"] = args["blocks"]
    # Only meaningful beside ``channel_type`` (validated above): it narrows that
    # transport from "this session's conversation" to one explicit configured
    # destination on it. The gateway rejects it without one.
    if args.get("target_id"):
        payload["target_id"] = args["target_id"]
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
    if session:
        payload["session"] = session
    # Always tell the gateway when the caller is a cron — even on a bare
    # send (no session/channel) — so it can apply the documented
    # "cron → Slack DM by default" routing and report where the message
    # actually landed.
    caller_session = mcp_core._resolve_session_key()
    # A channel_type send posts into ONE named conversation, so it is resolved
    # STRICTLY. The lenient resolver walks process ancestors, and a sub-agent
    # resolving to its parent would deliver into the parent's conversation —
    # someone else's chat window. Refuse rather than guess; the strict sources
    # (gateway-injected caller context, injected session key, HMAC-verified host
    # pid) are all published by the channel transports, so this is reachable on
    # every surface that can legitimately ask for it.
    verified_session = ""
    if channel_type:
        verified_session, _strict_err = mcp_core.require_strict_session_key(
            "Error: cannot verify caller identity for a channel_type send "
            "(no gateway-injected session key or HMAC-verified pid). "
            "Refusing to post into a conversation that cannot be attributed."
        )
        if not verified_session:
            return _strict_err
    elif session in _CHANNEL_SESSIONS:
        # A channel SESSION leaves over the same transports as ``channel_type``, so
        # it is held to the same identity bar and refused the same way.
        #
        # The refusal is the POINT, not a side effect. ``gov_session`` below falls
        # back to the LENIENT resolver, which walks process ancestors -- so an
        # unidentified sub-agent resolves to its parent, and the channel-agent
        # containment check (``_deny_channel_agent_messaging``, keyed on an
        # identity starting ``channel:``) then does not fire for a contained agent.
        # That is a confinement bypass onto the owner-DM egress surface, and the
        # gateway's fail-closed ``channels`` re-vet does NOT backstop it: that gate
        # covers the transport scope, not channel-agent containment.
        #
        # Refusing costs no legitimate caller. The gateway injects
        # ``KIROCREW_SESSION_KEY`` into every agent/ACP subprocess
        # (``acp/client.py``) and cron runs carry ``cron:<job_id>`` in it
        # (``cron_script.py``), with the HMAC-verified host-pid sidecar covering
        # PID-namespace-sandboxed sessions. It is dropped only when there is no
        # session key to inject -- exactly the caller that cannot be attributed.
        verified_session, _strict_err = mcp_core.require_strict_session_key(
            f"Error: cannot verify caller identity for a session={session!r} send "
            "(no gateway-injected session key or HMAC-verified pid). "
            "Refusing to send a DM that cannot be attributed to a caller."
        )
        if not verified_session:
            return _strict_err
    # ``gov_session`` is the identity every gate below is keyed on. It is the
    # STRICT key whenever one was required, so the identity that is checked is
    # the identity the request is later sent under (``_post`` gets the same
    # value); re-resolving leniently after a strict gate would check one session
    # and write as another.
    gov_session = verified_session or caller_session
    # Channel-agent containment: channel agents
    # communicate exclusively through channel posts. The channel.py
    # permission-request guard only fires when kiro-cli ASKS — an
    # auto-approved kirocrew-core call (default allowedTools) emits no
    # permission event — so the boundary must also hold here at MCP
    # dispatch, keyed on the verified caller identity.
    _chan_deny = mcp_core._deny_channel_agent_messaging(gov_session, "send_message")
    if _chan_deny:
        return _chan_deny
    is_cron = caller_session.startswith("cron:")
    # Forward the identity the gateway will re-vet under. The STRICT key whenever
    # one was established above -- never the lenient one, which walks process
    # ancestors and would hand a sub-agent its PARENT's channel permissions at the
    # egress chokepoint. BOTH channel legs require one, so every channel egress
    # forwards a strictly-resolved identity or was already refused above; a cron
    # keeps forwarding its own key for the Slack/dashboard routing it drives.
    if verified_session:
        payload["caller_session"] = verified_session
    elif is_cron:
        payload["caller_session"] = caller_session
    # Governance: outbound messaging is a capability gate (exfil surface).
    # A policy/profile may disable proactive messaging for a surface/app.
    _gov_msg = mcp_core._vet_messaging_governance(gov_session)
    if _gov_msg:
        return f"Error: {_gov_msg}"
    # Governance: the per-transport ``channels`` allowlist is finer-grained
    # than the on/off messaging gate — a policy may permit messaging but
    # restrict it to specific transports (e.g. Slack only). Vet the ONE
    # transport this send actually egresses on: the gate must name the transport
    # the message will ACTUALLY leave over, not a stand-in. Vetting "slack" for a
    # Telegram send would evaluate a Telegram denial against Slack's rule and let
    # it through, or refuse a permitted Telegram send because Slack is denied.
    #
    # The destinations are exclusive, mirroring the gateway. A channel_type send
    # suppresses the Slack leg there (a failed channel delivery must not fall
    # through to an audience the caller never named), and a channel session takes
    # that routing over including the cron default, so Slack is not a destination
    # of either and is never additionally vetted: vetting Slack too would let a
    # Slack-denying policy block a Discord DM that never touches Slack. Otherwise
    # the gateway routes to Slack whenever session=="slack" OR an explicit
    # channel/user is set OR the caller is a cron (see
    # messaging.api_send_message), so we mirror that exact predicate: checking
    # only session=="slack" would let a channel=/user=-addressed send reach Slack
    # while bypassing the gate. A bare send (no session/channel/user/channel_type,
    # non-cron) is the in-process dashboard notification path, governed by the
    # messaging gate above.
    #
    # Defence in depth, not the authority: the gateway re-vets the same
    # ``channels`` scope fail-closed at the egress chokepoint, which is where a
    # denial is decided (this one degrades open on an evaluation error).
    # ``channel_type`` covers BOTH channel legs -- this session's own conversation
    # and, with ``target_id``, an explicit destination on the same transport -- so
    # one branch vets the transport the message actually leaves over either way.
    if channel_type:
        egress_transport = channel_type
    elif session in _CHANNEL_SESSIONS:
        egress_transport = session
    elif session == "slack" or bool(payload.get("channel")) or bool(payload.get("user")) or is_cron:
        egress_transport = "slack"
    else:
        egress_transport = ""
    if egress_transport:
        _gov_chan = mcp_core._vet_channel_governance(gov_session, egress_transport)
        if _gov_chan:
            return f"Error: {_gov_chan}"
    if verified_session:
        resp = mcp_core._post("/api/send-message", payload, session_key=verified_session)
    else:
        resp = mcp_core._post("/api/send-message", payload)
    if not resp.get("ok"):
        if resp.get("code") == "channel_delivery_failed":
            # "Error:" prefix: call_tool_with_logging classifies only
            # "Error:"-prefixed returns as failures, and a channel send that
            # reached nobody must land in the audit trail as one — "Failed:"
            # would be recorded as a completed call.
            return f"Error: {resp.get('error') or resp}"
        return f"Failed: {resp}"
    # The channel-addressed leg posts ONLY to the named target and publishes no
    # dashboard notification, and its target may be a room rather than a DM, so it
    # cannot borrow the DM-leg's "DM + notification" string below (that leg always
    # notifies first and only ever targets the owner's DM). Report what actually
    # happened, keyed off the pair this call sent.
    if payload.get("channel_type"):
        parts = resp.get("parts", 1)
        suffix = "" if parts == 1 else f" ({parts} parts)"
        return f"Message sent to {payload['channel_type']} target {payload.get('target_id', '')}{suffix}."
    # Prefer the gateway's explicit delivery channel when present
    # (delivered_to ∈ {"slack", "session", "notification"} or a channel TYPE, which
    # is what both channel legs report); fall back to the legacy slack/session
    # booleans for older gateways.
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
    # A channel delivery reports its own channel TYPE as delivered_to, so a
    # transport added later is reported without a branch here. Worded as the
    # conversation rather than a DM because this arm also carries the channel_type
    # leg, whose audience can be a forum Topic.
    if delivered_to and delivered_to != "notification":
        return f"Message sent to the {delivered_to} conversation + notification."
    # Reached the dashboard notification only. Warn loudly when a chat surface
    # was intended (explicit session=slack or a channel session, or a cron —
    # which defaults to Slack) so the caller can detect the miss and retry
    # instead of reading a success string for a notification-only send.
    if session == "slack":
        return "⚠️ Slack unavailable — delivered as dashboard notification only (NOT in Slack)."
    if session in _CHANNEL_SESSIONS:
        return (
            f"⚠️ {session} unavailable (not connected, or no configured DM target) — "
            f"delivered as dashboard notification only (NOT in {session})."
        )
    if session:
        return "Session injection unavailable — delivered as notification."
    if is_cron:
        return (
            "⚠️ Cron send reached the dashboard notification only — NOT posted to Slack "
            "(owner DM unavailable: no Slack client or owner_id). Verify Slack delivery."
        )
    return "Notification delivered."


def send_notification(name: str, args: dict[str, Any]) -> str:
    caller_session, _strict_err = mcp_core.require_strict_session_key(
        "Error: cannot verify caller identity for send_notification "
        "(no gateway-injected session key or HMAC-verified pid). "
        "Refusing to publish without a trusted governance identity."
    )
    if not caller_session:
        return _strict_err
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
    # Native channel delivery first: when the caller's session is linked to a
    # non-Slack conversation with a document-capable transport (a Telegram
    # chat today), the file belongs THERE — the user who asked for it is
    # reading that surface, and the dashboard card is the fallback, not the
    # delivery. The endpoint resolves the destination exclusively from the
    # caller's session map entry (this tool cannot name a conversation) and
    # answers ``delivered: false`` for every "no destination here" case — in
    # which case the Slack leg below runs exactly as it always has.
    #
    # The caller is resolved STRICTLY and pinned on the wire. The default
    # lenient resolution includes a /proc ancestor walk, under which an
    # unidentified subagent resolves to its PARENT slot — and the file would
    # deliver into the parent's linked conversation. No verified identity, no
    # native delivery; the Slack leg keeps its own three-state classifier.
    #
    # An EXPLICIT ``channel`` argument names a destination the caller chose,
    # so the session-link inference must stand down entirely: running the
    # native leg first would reroute the file to the linked chat instead of
    # the named Slack channel.
    channel_warning = ""
    # Resolve-half of the shared strict gate only: file_send degrades (skips
    # the native channel leg) rather than refusing when identity is absent.
    strict_key, _ = mcp_core.require_strict_session_key("file_send native delivery")
    if strict_key and not args.get("channel"):
        channel_resp = mcp_core._post(
            "/api/channel/upload-file",
            {"file_path": str(dest), "filename": dest.name, "description": desc},
            session_key=strict_key,
        )
        if channel_resp.get("delivered"):
            via = channel_resp.get("channel_type") or "channel"
            msg = f"File sent: {dest.name} ({desc})" if desc else f"File sent: {dest.name}"
            return f"{msg} (delivered to {via})"
        if channel_resp.get("error"):
            channel_warning = f" (channel upload failed: {channel_resp['error']})"
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
    return msg + channel_warning + slack_warning


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "send_message": send_message,
    "send_notification": send_notification,
    "delete_message": delete_message,
    "read_slack_profile": read_slack_profile,
    "file_send": file_send,
}
