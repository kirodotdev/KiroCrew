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
from kiro_crew.mcp_tools.catalog.messaging import (  # noqa: F401
    _CHANNEL_SESSIONS,
    _SESSION_TARGETS,
    schemas,
)
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import BINARY_MIME_ALLOWLIST, redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import _SLACK_TS_RE, CHANNEL_ID_RE, CHANNEL_MAX_LEN

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
    if args.get("format"):
        payload["format"] = args["format"]
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
    # Server-side deep-link button opt-in. Forwarded as a plain flag; the gateway
    # resolves the caller identity and builds the URL, so no session key is sent.
    if args.get("include_session_link"):
        payload["include_session_link"] = True
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
        if resp.get("code") in ("channel_delivery_failed", "channel_not_permitted"):
            # "Error:" prefix: call_tool_with_logging classifies only
            # "Error:"-prefixed returns as failures, and a channel send that
            # reached nobody -- a fault or a permission refusal -- must land in
            # the audit trail as one; "Failed:" would be recorded as a completed
            # call.
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
    for key in ("body", "priority", "url", "group_key", "key", "actions"):
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


def dismiss_notification(name: str, args: dict[str, Any]) -> str:
    caller_session, _strict_err = mcp_core.require_strict_session_key(
        "Error: cannot verify caller identity for dismiss_notification "
        "(no gateway-injected session key or HMAC-verified pid). "
        "Refusing to remove a notification without a trusted governance identity."
    )
    if not caller_session:
        return _strict_err
    # The same boundaries as send_notification: a channel agent may not reach the
    # notification center, and a configured messaging denial holds fail-closed.
    _chan_deny = mcp_core._deny_channel_agent_messaging(caller_session, "dismiss_notification")
    if _chan_deny:
        return _chan_deny
    _gov_note = mcp_core._vet_messaging_governance(
        caller_session, tool_name="dismiss_notification", fail_closed=True
    )
    if _gov_note:
        return f"Error: {_gov_note}"
    resp = mcp_core._post("/api/notifications/agent/dismiss", {"key": args["key"]})
    if not resp.get("ok"):
        return f"Error: {resp}"
    if not resp.get("dismissed"):
        return f"No notification holds the key {args['key']!r}; nothing to dismiss."
    return f"Notification {args['key']!r} dismissed from the notification center."


def delete_message(name: str, args: dict[str, Any]) -> str:
    channel = args.get("channel", "")
    msg_ts = args.get("ts", "")
    if len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
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


def update_message(name: str, args: dict[str, Any]) -> str:
    """Edit one of the bot's own Slack messages in place.

    Gated like ``send_message``'s Slack leg, NOT like ``delete_message``. Deleting
    retracts content the audience already has; an edit PUBLISHES new agent-authored
    text to that same audience, which is the exfil surface every outbound gate
    exists for. Without them a session whose ``capabilities.messaging`` was turned
    off, or a channel agent confined to channel posts, could still push arbitrary
    text into Slack by editing a message it posted earlier — the gate would hold on
    ``send_message`` and leak here.

    Shape is validated before identity so a malformed call is refused without a
    governance-profile evaluation, and every return path lands on the SEL trail.
    """
    channel = args.get("channel", "")
    msg_ts = args.get("ts", "")
    text = args.get("text") or ""
    blocks = args.get("blocks")

    def _audit(outcome: str, session_key: str = "") -> None:
        mcp_core.sel().log_tool_invocation(
            session_key=session_key or mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="update_message",
            outcome=outcome,
        )

    if not CHANNEL_ID_RE.match(channel):
        _audit("error")
        return "Error: invalid channel ID format."
    if not _SLACK_TS_RE.match(msg_ts):
        _audit("error")
        return "Error: invalid message timestamp format."
    if not text and not blocks:
        # chat.update needs replacement content: an edit carrying neither is at
        # best a no-op and at worst blanks the message, so refuse it here.
        _audit("error")
        return "Error: text or blocks required."
    # Strict identity, for the reason send_message's channel legs need it: the
    # lenient resolver walks process ancestors, so an unattributable subagent
    # would be gated (and audited) as its PARENT — and the channel-agent
    # containment check below is keyed on exactly that identity.
    caller, strict_err = mcp_core.require_strict_session_key(
        "Error: cannot verify caller identity for an update_message edit "
        "(no gateway-injected session key or HMAC-verified pid). "
        "Refusing to publish text that cannot be attributed to a caller."
    )
    if not caller:
        _audit("denied")
        return strict_err
    # Channel agents communicate exclusively through channel posts. kirocrew-core
    # is auto-approved, so no permission event reaches channel.py's guard and the
    # boundary has to hold here at MCP dispatch. This helper writes its own
    # ``rejected_blocked_tool`` SEL record, so no _audit() call beside it.
    chan_deny = mcp_core._deny_channel_agent_messaging(caller, "update_message")
    if chan_deny:
        return chan_deny
    gov_msg = mcp_core._vet_messaging_governance(caller, tool_name="update_message")
    if gov_msg:
        _audit("denied", caller)
        return f"Error: {gov_msg}"
    # The per-transport ``channels`` allowlist as well: an edit leaves over Slack
    # and only Slack, so a policy that permits messaging but not Slack must refuse
    # it exactly as it refuses a Slack send.
    gov_chan = mcp_core._vet_channel_governance(caller, "slack", tool_name="update_message")
    if gov_chan:
        _audit("denied", caller)
        return f"Error: {gov_chan}"
    payload: dict[str, Any] = {"channel": channel, "ts": msg_ts}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
    # Send the key the gate returned, never a re-resolved one: re-resolving would
    # check one identity and write as another.
    resp = mcp_core._post("/api/update-message", payload, session_key=caller)
    if resp.get("error"):
        _audit("error", caller)
        return f"Failed: {resp['error']}"
    _audit("success", caller)
    return "Message updated."


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


def read_slack_history(name: str, args: dict[str, Any]) -> str:
    # Strict identity and channel-agent containment, as update_message holds
    # them: kirocrew-core is auto-approved, so channel.py's permission guard never
    # sees this call, and a channel agent could relay one channel's messages into
    # its own thread.
    caller, strict_err = mcp_core.require_strict_session_key(
        "Error: cannot verify caller identity for a read_slack_history call "
        "(no gateway-injected session key or HMAC-verified pid). "
        "Refusing to read channel messages for a caller that cannot be identified."
    )
    if not caller:
        return strict_err
    chan_deny = mcp_core._deny_channel_agent_messaging(caller, "read_slack_history")
    if chan_deny:
        return chan_deny

    def _audit(outcome: str) -> None:
        mcp_core.sel().log_tool_invocation(
            session_key=caller, source="mcp", tool_name="read_slack_history", outcome=outcome
        )

    # The caller's own ``channels`` scope: the gateway route checks the host's,
    # so a session or app narrowed off Slack would otherwise still read it.
    gov_chan = mcp_core._vet_channel_governance(caller, "slack", tool_name="read_slack_history")
    if gov_chan:
        _audit("denied")
        return f"Error: {gov_chan}"
    resp = mcp_core._post("/api/slack-history", args, session_key=caller)
    if resp.get("error"):
        _audit("error")
        return f"Error: {resp['error']}"
    _audit("success")
    messages = resp.get("messages", [])
    # Defence-in-depth, as read_slack_profile: redact again before the LLM.
    for m in messages:
        text, _ = redact_exfiltration_urls(str(m.get("text") or ""))
        m["text"], _ = redact_credentials(text)
    return json.dumps(messages, indent=2)


def _describe_channel_skip(reason: str) -> str:
    """Render a channel-leg skip *reason* as an actionable warning clause.

    The endpoint already decided, audited and serialized why native delivery
    could not happen; this only makes that decision visible to the caller. The
    reason codes are a closed vocabulary
    (:mod:`kiro_crew.dashboard.upload_destination`), so an unrecognized value is
    reported verbatim rather than guessed at — a new code must degrade to "we
    told you the code we got", never to silence.

    ``restricted_session`` deliberately gets NO remedy. It is a ceiling, not a
    missing capability: the renderer's extraction path enforces the same shared
    predicate, so the inline route is equally refused there. Suggesting it would
    both fail and read as advice to route around a privacy boundary.

    ``channel_upload_unsupported`` gets no remedy either, for the neighbouring
    reason: it fires for any channel with no document verb wired, and that set
    spans both capabilities. Discord and Feishu (in a DM) would honour an inline
    reference (``files_outbound=True``) but Weixin and iMessage declare
    ``files_outbound=False``, so their renderers leave the reference in the text
    as literal markup. The reason string cannot tell those cases apart, and
    naming a route that silently does nothing on half of them is worse than
    naming none — the channel type is already in the reason for a caller that
    wants to look further.

    This docstring is the ONE place that roster is written down; the spec and the
    tests point here rather than restating it, so a channel flipping the flag
    invalidates a single site instead of four.
    """
    head = f" (native channel delivery skipped: {reason}"
    if reason == "restricted_session":
        return (
            f"{head}; this session may not ship local file bytes to a channel, "
            "so the dashboard card is the only delivery)"
        )
    if reason.startswith("channel_upload_unsupported"):
        return f"{head}; this channel has no file-upload path from this tool)"
    if reason == "no_channel_destination":
        # The one reason with a route worth naming: no destination here does not
        # mean no destination anywhere, and the renderer's own outbound-image
        # extraction uploads a real image referenced inline in the reply.
        return (
            f"{head}; for an inline image on a messaging channel, reference it as "
            "![alt](/abs/path) from the session's working directory or the browser "
            "tool's output directory instead)"
        )
    return f"{head})"


def _describe_slack_skip(reason: str) -> str:
    """Render a SLACK-leg skip *reason* as a warning clause.

    The Slack endpoint answers its own "cannot deliver here" the same way the
    channel one does — ``{"ok": true, "skipped": "<reason>"}``, from `no_slack`
    when no client is configured and from the shared destination oracle
    otherwise — and this tool is that endpoint's only caller. Reporting only
    ``error`` left a skip indistinguishable from an upload, which is the very
    pattern the channel leg above stopped doing; fixing one branch and leaving
    its sibling is what makes a point patch out of a general fix.

    No remedy is named. Unlike a channel skip there is no alternative route to
    point at: Slack either has a permitted destination for this caller or the
    dashboard card is the delivery.
    """
    return f" (Slack upload skipped: {reason}; the file is available in the dashboard)"


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
    # The owner's own outbox and dashboard get the bytes as-is: no content scan
    # here. Binary still needs an allow-listed MIME type. The Slack and channel
    # upload legs scan on their own in ``_gate_upload_file``.
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
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
    channel_skip_reason = ""
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
        elif channel_resp.get("skipped"):
            # A SKIP is the endpoint's "no destination here" answer, and it
            # carries the reason it decided that. Reporting it is the whole
            # point: without it the caller reads a bare "File sent" and cannot
            # tell a delivery from a dashboard-only copy, so an agent that
            # picked the wrong tool has nothing to correct against.
            channel_skip_reason = str(channel_resp["skipped"])
            channel_warning = _describe_channel_skip(channel_skip_reason)
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
    # Slack-linked sessions cannot have a native document-channel destination.
    # Its routine no-destination skip says nothing about the Slack leg below.
    if identity == "thread" and channel_skip_reason == "no_channel_destination":
        channel_warning = ""
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
        elif slack_resp.get("skipped"):
            # Same three-state response as the channel leg above, same rule: a
            # skip the endpoint computed and audited must not read as an upload.
            slack_warning = _describe_slack_skip(str(slack_resp["skipped"]))
        elif slack_resp.get("ok") is True:
            # ok-without-skipped means the endpoint actually delivered, for
            # every resolved identity (owner DM, session-map thread, tracked
            # channel) — not just Slack-linked threads.
            slack_warning = " (delivered to Slack)"
    msg = f"File sent: {dest.name} ({desc})" if desc else f"File sent: {dest.name}"
    return msg + channel_warning + slack_warning


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "send_message": send_message,
    "send_notification": send_notification,
    "dismiss_notification": dismiss_notification,
    "delete_message": delete_message,
    "update_message": update_message,
    "read_slack_profile": read_slack_profile,
    "read_slack_history": read_slack_history,
    "file_send": file_send,
}
