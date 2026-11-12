"""Tool declarations of the ``messaging`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.messaging`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES

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
#: advertises exactly ONE direct target and it is available: a channel that needs
#: prior inbound state (Teams, WeCom) marks a target unavailable until that person
#: writes, and still degrades to the dashboard notification, and an ambiguous
#: allow-list is refused rather than guessed at. That gate is at the side-effect
#: boundary and reads live config, which is where it has to be — a static tuple
#: here cannot answer a runtime question.
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
                "Webex, WeCom, Weixin, WhatsApp, iMessage, Feishu) set 'channel_type' to "
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
                        "description": (
                            "Optional Slack Block Kit blocks array. When provided, "
                            "the message is sent as rich Block Kit with text as "
                            "fallback. Agent-supplied blocks cannot contain image "
                            "or video blocks, or image_url, thumbnail_url, or "
                            "video_url fields; those server-fetched media forms are "
                            "refused."
                        ),
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
                            '"weixin", "whatsapp", "imessage" or "feishu". Use it when the '
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
                    "format": {
                        "type": "string",
                        "enum": ["plain", "markdown"],
                        "description": (
                            '"markdown" renders **bold**, _italic_, `code` and links '
                            "on a channel that would otherwise show the markup "
                            "literally (Telegram), for channel_type, target_id and "
                            'channel-session sends. Default "plain" sends the text as '
                            "written; Slack and the dashboard notification ignore it."
                        ),
                    },
                    "unfurl_links": {
                        "type": "boolean",
                        "description": (
                            "Deprecated. Link previews are always off on bot "
                            "posts (a preview is a zero-click fetch of the "
                            "URL). false is accepted; true is refused."
                        ),
                    },
                    "unfurl_media": {
                        "type": "boolean",
                        "description": (
                            "Deprecated. Media previews are always off on bot "
                            "posts. false is accepted; true is refused."
                        ),
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
                    "include_session_link": {
                        "type": "boolean",
                        "description": (
                            "When true and the message is delivered to Slack, append an "
                            '"Open session" button that deep-links to THIS session\'s '
                            "dashboard chat (a cron links to its origin session). The link "
                            "is built server-side from the verified caller identity, so it "
                            "always points at the calling session — you never assemble it. "
                            "The button's embedded sign-in credential expires ~5 minutes "
                            "after send: a later tap still opens the session for a browser "
                            "holding a dashboard session cookie, and otherwise lands on "
                            "the sign-in page — so do not present the button as a durable "
                            "link. Silently omitted (message still delivered) for non-Slack "
                            "delivery, a caller with no resolvable session, or when no "
                            "dashboard origin/tunnel is available. Defaults to false."
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
                "(bell feed) through the system.agent channel. Unlike "
                "send_message, this is a pure notification: "
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
                            "critical = badge+sound+banner, default = badge, with "
                            "sound and banner per the channel's settings, "
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
                    "key": {
                        "type": "string",
                        "description": (
                            "Names what the note tracks: its kind, then the id "
                            "of that thing (e.g. 'cr:12345', 'pr:owner/repo#42'), "
                            "so watchers of different things never pick the same "
                            "key. Publishing again under the same key replaces "
                            "that note instead of adding one, quietly unless the "
                            "priority rises; dismiss_notification removes it. "
                            "Keys are shared by all your sessions and crons, so "
                            "a later run can update or dismiss it."
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
                            "render nothing."
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
            "name": "dismiss_notification",
            "description": (
                "Remove the notification published with send_notification under "
                "a key from the notification center, in every open window. Use "
                "when the condition it reported is over (a failure that "
                "recovered, a wait that ended). Any of your sessions or crons "
                "can dismiss a key another one set."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "The key the note was published with.",
                    },
                },
                "required": ["key"],
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
            "name": "update_message",
            "description": (
                "Edit a message previously sent by this bot, in place. Only works "
                "on messages authored by the Kiro Crew bot itself (Slack API "
                "constraint). Use for a rolling status message — progress, a live "
                "checklist, a result that supersedes an earlier one — instead of "
                "posting a follow-up that buries the original. Either text or "
                "blocks is required, and what you pass REPLACES the message: an "
                "edit carrying only blocks drops the old text, and one carrying "
                "only text drops the old blocks."
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
                        "description": "Timestamp of the message to edit (from send_message response).",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Replacement message text. Required unless blocks is "
                            "given; pass it alongside blocks as the notification "
                            "fallback."
                        ),
                    },
                    "blocks": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {"type": "object"},
                        "description": "Replacement Block Kit blocks (max 50).",
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
            "name": "read_slack_history",
            "description": (
                "Read recent messages in a tracked Slack channel, or one thread "
                "with thread_ts, newest first. Use it to see whether the owner "
                "replied (an approval, a follow-up). Returns [{user, ts, admitted, "
                "text}]: text is returned only when admitted is true (the sender "
                "is the owner); other senders' messages come back with no text. "
                "Bot messages are left out. Pass oldest (a Slack ts) to read "
                "only messages after it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Tracked channel ID (e.g. C0123ABC456).",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "Read this thread's replies, not the channel timeline.",
                    },
                    "oldest": {
                        "type": "string",
                        "description": "Only messages after this Slack ts.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (1-100, default 20).",
                    },
                },
                "required": ["channel"],
            },
        },
        {
            "name": "file_send",
            "description": (
                "Send a file to the user. Copies the file to the outbox and "
                "notifies the dashboard with a download link. When this "
                "session is linked to a Telegram, Discord or WeCom "
                "conversation the file is also delivered there natively; "
                "otherwise it uploads to "
                "Slack when the caller's Slack identity permits it. Use "
                "when you've generated a report, export or artifact the "
                "user should receive. Any text file is accepted, but a "
                "binary file only when it is audio, video, an image or a "
                "PDF: other binary formats (zip, tar, Office "
                "documents) are refused, so give the user their path "
                "instead. Native channel delivery is "
                "not guaranteed: the result reports skips and failures, and "
                "a Slack-linked session reports a successful Slack upload. "
                "If neither delivers, the file remains available from the "
                "dashboard. To put an IMAGE inline in a "
                "messaging conversation, reference it in your reply as "
                "![alt](/abs/path) from the session's working directory or "
                "the browser tool's own screenshot directory (the path its "
                "screenshot op returns) — the channel renderer uploads it as a "
                "native picture, "
                "which this tool's outbox copy is not eligible for."
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
