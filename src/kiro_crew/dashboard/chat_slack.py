"""Slack integration — link sessions, handoff, channel listing."""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_and_truncate
from kiro_crew.sel import sel
from kiro_crew.slack.channel_resolver import _CACHE_FILENAME, ChannelNameResolver
from kiro_crew.sync_bridge import handoff_to_slack

logger = logging.getLogger(__name__)

# Fresh-anchor title fallback (B-lite): when the slot has no LLM title yet
# (titles land seconds after session creation), fall back to a one-line snippet
# of the first user prompt, then to a neutral default. The raw slot key must
# never be user-visible.
_ANCHOR_TITLE_SNIPPET_CHARS = 60
_ANCHOR_TITLE_DEFAULT = "New session"


def _first_user_prompt(slot) -> str:  # noqa: ANN001 — _ChatSlot (avoids import cycle)
    """Return the slot's first user prompt collapsed to a single line, or ""."""
    for m in slot.messages:
        if m.get("role") == "user":
            text = " ".join(str(m.get("content") or "").split())
            if text:
                return text
    return ""


def _get_channel_resolver(state: DashboardState) -> ChannelNameResolver:
    """Lazily construct the shared ChannelNameResolver on first use.

    The cache path is derived from ``dashboard_state.config_dir`` (accessed as a
    module attribute, not a ``from`` import) so it flows through the same seam
    tests patch — isolating the on-disk cache to ``tmp_path`` under test while
    resolving to the real ``~/.kirocrew`` dir in production.
    """
    if state._channel_resolver is None:
        cache_path = dashboard_state.config_dir() / _CACHE_FILENAME
        state._channel_resolver = ChannelNameResolver(cache_path=cache_path)
    return state._channel_resolver


async def api_chat_slot_slack_link(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/slack-link — link a dashboard session to Slack."""

    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    if not state.slack_client:
        return web.json_response({"error": "Slack not connected"}, status=503)
    owner_id = getattr(state, "owner_id", None)
    if not owner_id:
        return web.json_response({"error": "owner not configured"}, status=500)

    session_key = _history_key_for(name)

    # Check if already linked
    existing_ts, existing_chan = state.sessions.get_slack_link(session_key)
    if existing_ts and existing_chan:
        try:
            await state.slack_client.post_message(
                existing_chan, "🔗 Session linked from dashboard — continuing here.", existing_ts
            )
        except Exception:
            pass
        return web.json_response(
            {"ok": True, "already_linked": True, "thread_ts": existing_ts, "channel": existing_chan}
        )

    body = await request.json() if request.content_length else {}
    raw_channel = body.get("channel", "")
    # When the caller supplies an existing thread_ts (challenge-and-redirect
    # auto-link from a Slack thread the user replied in), link to THAT thread
    # rather than posting a new one — this is what makes a thread reply route
    # back to its dashboard session bidirectionally.
    existing_thread = str(body.get("thread_ts", "") or "")
    if not raw_channel or raw_channel == "dm":
        target_channel = await state.slack_client.open_dm(owner_id)
    else:
        target_channel = raw_channel

    if existing_thread:
        thread_ts = existing_thread
    else:
        # redact_and_truncate applies both redact_exfiltration_urls +
        # redact_credentials. Fallback chain: LLM title → first-prompt snippet
        # → neutral default. Redaction runs on the full snippet text before
        # truncation so a truncation boundary can never split (and hide) a
        # credential. Fork adaptation: slots initialize title to their raw key
        # (state.py), so gate on display_title — a slot still showing
        # NEW_SESSION_TITLE has no real title, while cron/plan/handoff slots
        # (real titles, _titled unset) pass their title through.
        base = slot.title if slot.display_title != dashboard_state.NEW_SESSION_TITLE else ""
        title = redact_and_truncate(base, max_chars=200)
        if not title:
            title = redact_and_truncate(
                _first_user_prompt(slot), max_chars=_ANCHOR_TITLE_SNIPPET_CHARS
            )
        if not title:
            title = _ANCHOR_TITLE_DEFAULT
        thread_ts = await state.slack_client.post_message(
            target_channel, f"\U0001f9f5 *{title}*\nSession linked from dashboard."
        )
        if not thread_ts:
            return web.json_response({"error": "failed to create thread"}, status=500)

    state.sessions.set_slack_link(session_key, thread_ts, target_channel)
    slot._slack_linked = True
    slot._slack_channel = target_channel
    slot._slack_thread_ts = thread_ts

    # Post last 5 messages as context — only when we created a NEW thread.
    # Linking to an existing thread (challenge-and-redirect) would duplicate
    # messages the thread already contains.
    if not existing_thread:
        for m in slot.messages[-5:]:
            role = m.get("role", "")
            txt = redact_and_truncate(m.get("content") or "", max_chars=2000)
            if role in ("user", "assistant") and txt:
                icon = "\U0001f9d1" if role == "user" else "\U0001f916"
                try:
                    await state.slack_client.post_message(
                        target_channel, f"{icon} {txt}", thread_ts
                    )
                except Exception:
                    pass

    sel().log_api_access(
        caller="dashboard",
        operation="chat.slack_link",
        outcome="success",
        source="dashboard",
        resources=slot.key,
    )
    state.push_slots_update()
    return web.json_response({"ok": True, "thread_ts": thread_ts, "channel": target_channel})


async def api_chat_slot_slack_unlink(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/slack-unlink — stop mirroring to Slack.

    Symmetric counterpart to ``api_chat_slot_slack_link``. Clears the Slack
    link so subsequent dashboard turns are no longer mirrored, while keeping
    the session, its history, and the existing Slack thread intact. Idempotent:
    unlinking a session with no link returns ``{ok, was_linked: false}``.

    Auth posture is identical to slack-link, with no new auth surface: both are
    reachable as mixed-internal via the ``/api/chat`` prefix in
    ``mixed_internal_paths`` (server.py; token_auth.py prefix-matches sub-routes),
    so on loopback they accept the internal secret and otherwise fall back to
    normal dashboard-token + CSRF auth. No separate allowlist entry is needed —
    and it must NOT be added to the strict ``internal_paths`` set, which would
    wrongly restrict this browser action to loopback-only callers.
    """
    state: DashboardState = request.app["state"]
    # Keys off _history_key_for(name), NOT slot.linked_session_key — the latter
    # only diverges for cron/takeover slots, which are never Slack-linked.
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    session_key = _history_key_for(name)
    cleared = state.sessions.clear_slack_link(session_key)
    # ⚠️ chat_runner.py copies the link onto the "dashboard:"-prefixed key when
    # a turn runs, so unlink MUST clear BOTH the raw and "dashboard:"-prefixed
    # keys or the next turn silently re-inherits the link and mirroring resumes.
    if session_key.startswith("dashboard:"):
        cleared = state.sessions.clear_slack_link(session_key[len("dashboard:"):]) or cleared
    else:
        # Defensive: _history_key_for always returns a dashboard:-prefixed key,
        # so this branch is currently unreachable — kept to stay correct if the
        # key scheme ever changes.
        cleared = state.sessions.clear_slack_link(f"dashboard:{session_key}") or cleared

    prev_channel = slot._slack_channel
    prev_thread_ts = slot._slack_thread_ts
    slot._slack_linked = False
    slot._slack_channel = ""
    slot._slack_thread_ts = ""

    # Best-effort courtesy note so a Slack watcher knows why the thread went
    # quiet. Same redaction path as the link endpoint; failure is non-fatal.
    if cleared and state.slack_client and prev_channel and prev_thread_ts:
        try:
            await state.slack_client.post_message(
                prev_channel,
                "\U0001f50c _Unlinked from dashboard — replies here no longer sync._",
                prev_thread_ts,
            )
        except Exception:
            logger.debug("Failed to post unlink courtesy note to Slack", exc_info=True)

    sel().log_api_access(
        caller="dashboard",
        operation="chat.slack_unlink",
        outcome="success" if cleared else "noop",
        source="dashboard",
        resources=slot.key,
    )
    state.push_slots_update()
    return web.json_response({"ok": True, "was_linked": cleared})


async def api_slack_channels(request: web.Request) -> web.Response:
    """GET /api/slack/channels — list channels the bot can reply in.

    Returns ``[{id, name}]``. Channel names that aren't already stored in
    config are resolved via ``ChannelNameResolver`` (single ``conversations.list``
    call cached for 1h) so the dropdown shows ``# pcn-orchestrator-interest``
    instead of ``# C0AU38Q0E4B``.
    """
    state: DashboardState = request.app["state"]
    cfg = KiroCrewConfig.load()
    channels: list[dict] = [{"id": "dm", "name": "Direct Message"}]
    seen: set[str] = set()
    unresolved: list[str] = []  # channel IDs that need name lookup

    for tc in cfg.slack.tracking_channels:
        cid = tc.get("channel_id", "")
        if cid and cid not in seen:
            name = tc.get("name") or ""
            channels.append({"id": cid, "name": name or cid})
            seen.add(cid)
            if not name:
                unresolved.append(cid)
    for cid, cc in cfg.slack_channels.items():
        if cid not in seen and cc.activation in ("always", "mention", "observe"):
            channels.append({"id": cid, "name": cid})  # placeholder — resolved below
            seen.add(cid)
            unresolved.append(cid)

    # Resolve placeholder names via cached Slack API call
    if unresolved and state.slack_client is not None:
        try:
            resolver = _get_channel_resolver(state)
            resolved = await resolver.resolve_many(state.slack_client, unresolved)
            for ch in channels:
                if ch["id"] in unresolved:
                    ch["name"] = resolved.get(ch["id"], ch["id"])
        except Exception:
            # Resolution failure leaves placeholder names in place — non-fatal
            logger.debug("Channel name resolution failed", exc_info=True)

    return web.json_response(channels)


async def api_chat_slot_handoff(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/handoff — hand off session to Slack DM thread."""

    state: DashboardState = request.app["state"]
    name = request.match_info.get("slot") or request.match_info.get("name", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    if not state.slack_client:
        return web.json_response({"error": "Slack not connected"}, status=503)
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=500)

    try:
        _save_slot_to_history(state, slot)
    except Exception:
        pass

    channel = None
    try:
        body = await request.json()
        channel = body.get("channel")
    except Exception:
        pass

    history_key = _history_key_for(slot.key)
    thread_ts = await handoff_to_slack(
        state.slack_client,
        state.owner_id,
        state.conversation_log,
        history_key,
        title=slot.title if slot._titled else "",
        channel=channel,
        sessions=state.sessions,
    )
    if not thread_ts:
        return web.json_response({"error": "handoff failed"}, status=500)

    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_handoff",
        outcome="allowed",
        source="dashboard",
        resources=slot.key,
    )
    return web.json_response({"ok": True, "thread_ts": thread_ts})


async def api_handoff_channels(request: web.Request) -> web.Response:
    """GET /api/handoff-channels — deprecated, use /api/slack/channels instead."""
    return web.json_response({})
