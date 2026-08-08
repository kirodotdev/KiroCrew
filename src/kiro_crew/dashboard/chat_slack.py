"""Slack integration — link sessions, handoff, channel listing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard.chat_backfill import (
    backfill_content,
    gap_summary,
    select_backfill_messages,
    session_deep_link,
)
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.state import DashboardState, _log_task_exception
from kiro_crew.platform.context import redact_via_context
from kiro_crew.security import redact_and_truncate
from kiro_crew.sel import sel
from kiro_crew.slack.channel_resolver import _CACHE_FILENAME, ChannelNameResolver
from kiro_crew.slack.format import render_for_slack
from kiro_crew.sync_bridge import handoff_to_slack

logger = logging.getLogger(__name__)

# Fresh-anchor title fallback: when the slot has no LLM title yet
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
    resolving to the real ``~/.kiro/crew`` dir in production.
    """
    if state._channel_resolver is None:
        cache_path = dashboard_state.config_dir() / _CACHE_FILENAME
        state._channel_resolver = ChannelNameResolver(cache_path=cache_path)
    return state._channel_resolver


_USER_ICON = "\U0001f9d1"
_AGENT_ICON = "\U0001f916"


def _format_backfill_parts(content: str, icon: str) -> list[str]:
    """Render one transcript row into postable Slack parts, icon included.

    Thin delegate to :func:`kiro_crew.slack.format.render_for_slack`, which owns
    the redact/convert/split ordering this path used to implement privately. The
    icon is passed as the prefix rather than prepended afterwards: decorating a
    maximally-sized part after the split pushed it past ``SLACK_MSG_LIMIT`` by
    the width of the icon plus its space.
    """
    return render_for_slack(content, prefix=f"{icon} ", redactor=redact_via_context)


async def drain_slack_backfill(
    state: DashboardState,
    slot: Any,
    channel: str,
    thread_ts: str,
) -> None:
    """Seed a freshly linked Slack thread with readable conversation history.

    Posts the opening turn, a gap marker naming how many turns were skipped, then
    the last few turns in full. Runs as a background task rather than inline in
    the link request: Slack accepts roughly one message per second per channel,
    so a long history split across many parts would hold the HTTP request open
    long enough for the browser fetch to time out while posts kept landing --
    the user would see a failure on a link that actually worked.

    Backgrounding is safe here specifically because the Slack link path has no
    per-message governance gate to fail closed on (unlike the configured-channel
    mirror in ``chat_mirror.py``, which stays inline for that reason).
    """
    client = state.slack_client
    if client is None:
        return
    # Offloaded: selection reads the on-disk transcript when the opening turn is
    # off-window, and read_messages_chained parses every tab_id sibling file (and
    # globs the sessions dir to rebuild a stale index). On the loop thread that
    # would stall every other chat turn and the liveness heartbeat.
    selection = await asyncio.to_thread(select_backfill_messages, state, slot)
    if not selection.messages:
        return

    async def _post(text: str) -> bool:
        try:
            await client.post_message(channel, text, thread_ts)
            return True
        except Exception:
            # Best-effort: a partially seeded thread is still usable, and the
            # link itself is already persisted. Never bare-pass -- a silent
            # swallow here is what made the original failure invisible.
            logger.debug("slack backfill: post failed", exc_info=True)
            return False

    for row in selection.first_turn:
        icon = _USER_ICON if row.get("role") == "user" else _AGENT_ICON
        for part in _format_backfill_parts(backfill_content(row), icon):
            if not await _post(part):
                return

    if selection.skipped_turns and selection.recent:
        summary = gap_summary(selection.skipped_turns)
        link = ""
        try:
            # Offloaded: KiroCrewConfig.load() reads and validates the config
            # file, which is blocking I/O like the transcript read above.
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            link = session_deep_link(cfg.dashboard.url, slot.key)
        except Exception:
            logger.debug("slack backfill: could not build session link", exc_info=True)
        marker = f"_… {summary} — <{link}|open in the dashboard>_" if link else f"_… {summary}_"
        await _post(marker)

    for row in selection.recent_rows:
        icon = _USER_ICON if row.get("role") == "user" else _AGENT_ICON
        for part in _format_backfill_parts(backfill_content(row), icon):
            if not await _post(part):
                return


def _spawn_slack_backfill(
    state: DashboardState,
    slot: Any,
    channel: str,
    thread_ts: str,
) -> None:
    """Fire the backfill drain as a tracked background task.

    Uses the established three-callback shape: keep a strong reference so the
    task is not garbage-collected mid-flight, discard it on completion, and log
    any exception through ``_log_task_exception`` (which redacts first). Omitting
    the third callback is a documented defect -- the failure would surface only
    as an unretrieved-exception warning at interpreter shutdown.

    ``state._background_tasks`` is never cancelled at shutdown, so a gateway stop
    mid-drain abandons the task and leaves a partially seeded thread. That is
    accepted: the link is already persisted and the thread is live.
    """
    task = asyncio.create_task(drain_slack_backfill(state, slot, channel, thread_ts))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    task.add_done_callback(_log_task_exception)


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

    # The slot's OWN session key: a channel-born slot's turns run on the
    # channel session, so the link has to live there for the turn path and the
    # link projection (state._slot_links) to find it.
    session_key = effective_session_key(slot)

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
        # credential. Slots initialize title to their raw key
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

    # Route through the ONE canonical link writer. ``link_slack`` sets the same
    # three slot fields and persists via ``set_slack_link``, but it ALSO
    # registers the thread -> slot reverse index that inbound Slack replies
    # resolve through, and releases the thread from any slot that held it
    # before. Hand-assigning the fields here duplicated everything except that
    # index, so a reply in the mirrored thread routed and persisted correctly
    # while nothing ever told the open tab it had arrived.
    state.link_slack(slot.key, thread_ts, target_channel)

    # Seed the new thread with readable history — only when we created a NEW
    # thread. Linking to an existing thread (challenge-and-redirect) would
    # duplicate messages the thread already contains.
    if not existing_thread:
        _spawn_slack_backfill(state, slot, target_channel, thread_ts)

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
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    # Authoritative key = the slot's own session key. Deriving it from the slot
    # NAME instead would build "dashboard:slack:<ts>" for a channel-born slot,
    # leaving the real link untouched so mirroring silently resumes next turn.
    session_key = effective_session_key(slot)
    cleared = state.sessions.clear_slack_link(session_key)
    # chat_runner copies a dashboard session's link from the bare key onto the
    # "dashboard:"-prefixed one when a turn runs, so both spellings must go or
    # the next turn re-inherits the link. A channel key has no such twin.
    if session_key.startswith("dashboard:"):
        cleared = state.sessions.clear_slack_link(session_key[len("dashboard:") :]) or cleared

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


async def list_slack_channels(state: DashboardState) -> list[dict]:
    """List configured Slack destinations, resolving display names."""
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

    return channels


async def api_slack_channels(request: web.Request) -> web.Response:
    """GET /api/slack/channels — list channels the bot can reply in."""
    state: DashboardState = request.app["state"]
    return web.json_response(await list_slack_channels(state))


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
        await save_slot_off_loop(state, slot)
    except Exception:
        pass

    channel = None
    try:
        body = await request.json()
        channel = body.get("channel")
    except Exception:
        pass

    history_key = effective_session_key(slot)
    transcript_key = slot_history_key(slot)
    if transcript_key != history_key:
        # The tab's conversation is stored somewhere other than the session it
        # runs on -- an unbound channel tab. Handing off would seed the thread
        # from the channel transcript while every later reply persisted under
        # the session's own key, splitting one conversation across two files;
        # a crash before the next slot flush would drop those replies entirely.
        # Refuse rather than straddle.
        return web.json_response(
            {
                "error": (
                    "this tab's conversation lives in a channel transcript, so it "
                    "cannot be handed off to a new Slack thread"
                ),
                "code": "transcript_not_own_session",
            },
            status=409,
        )
    thread_ts = await handoff_to_slack(
        state.slack_client,
        state.owner_id,
        state.conversation_log,
        history_key,
        title=slot.title if slot._titled else "",
        channel=channel,
        sessions=state.sessions,
        transcript_key=transcript_key,
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
