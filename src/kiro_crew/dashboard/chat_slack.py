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
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    expire_slack_options,
    options_records,
    remember_slack_options,
    slack_options_owner_keys_snapshot,
    slot_history_key,
)
from kiro_crew.dashboard.state import DashboardState, _log_task_exception
from kiro_crew.platform.context import redact_via_context
from kiro_crew.security import redact_and_truncate
from kiro_crew.sel import sel
from kiro_crew.slack.channel_resolver import _CACHE_FILENAME, ChannelNameResolver
from kiro_crew.slack.format import (
    build_options_blocks,
    build_options_selected_blocks,
    extract_options,
    render_for_slack,
)
from kiro_crew.slack.outbound import (
    OPTIONS_FALLBACK_TEXT,
    PostedOptions,
    answer_routing_in_flight,
)
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
    # Baseline for detecting that the conversation moved on while we work. Taken
    # BEFORE the selection await, not after: selection reads the on-disk
    # transcript and can take a while, so a turn that completes during it would
    # be invisible to a baseline captured afterwards -- leaving a superseded
    # control clickable. Compared against after the posting loops.
    #
    # ``total_messages``, not ``len(slot.messages)``: the message list is capped
    # at _MAX_SLOT_MESSAGES and trimmed from the front on append, so a slot
    # sitting at the cap grows and trims in the same step and its LENGTH never
    # changes. A turn completing mid-drain would then be undetectable on the one
    # slot busy enough to make the race likely. total_messages is a lifetime
    # counter and survives trimming.
    started_running = slot.running
    started_total = slot.total_messages
    session_key = effective_session_key(slot)

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

    async def _post_options(choices: list[str], *, interactive: bool) -> str | None:
        """Post a replayed OPTIONS tag as a control instead of literal text.

        The body and the control are separate Slack messages, so this composes
        with the body pipeline above rather than replacing it -- the body keeps
        its table-safe conversion and full-length redaction, and the choices ride
        in a Block Kit message of their own.

        *interactive* only for the newest reply. Every earlier one asked a
        question this replay has already moved past, so it renders struck through
        and cannot be answered.

        Returns the ts of the control recorded as LIVE, so the caller can spend
        exactly that one later without touching controls another turn recorded in
        the same slot. ``None`` when nothing live was recorded.
        """
        blocks = (
            build_options_blocks(choices)
            if interactive
            else build_options_selected_blocks(choices, [])
        )
        try:
            ts = await client.post_blocks(channel, blocks, OPTIONS_FALLBACK_TEXT, thread_ts)
        except Exception:
            logger.debug("slack backfill: options control post failed", exc_info=True)
            return None
        if interactive and ts:
            remember_slack_options(
                state,
                session_key,
                PostedOptions(
                    channel=channel,
                    ts=ts,
                    choices=tuple(choices),
                    blocks=tuple(blocks),
                ),
            )
            return ts
        return None

    for row in selection.first_turn:
        icon = _USER_ICON if row.get("role") == "user" else _AGENT_ICON
        content, choices = _split_backfill_options(row)
        for part in _format_backfill_parts(content, icon):
            if not await _post(part):
                return
        if choices:
            # The opening turn is superseded by definition — spent, never live.
            await _post_options(choices, interactive=False)

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

    newest = len(selection.recent_rows) - 1
    live_ts: str | None = None
    for idx, row in enumerate(selection.recent_rows):
        icon = _USER_ICON if row.get("role") == "user" else _AGENT_ICON
        content, choices = _split_backfill_options(row)
        for part in _format_backfill_parts(content, icon):
            if not await _post(part):
                return
        if choices:
            posted_ts = await _post_options(choices, interactive=idx == newest)
            if posted_ts:
                live_ts = posted_ts

    # Did the conversation move past the replayed question while we were
    # draining? A turn that was running at any point, or a transcript that grew,
    # means the newest reply we just rendered as a LIVE control is already
    # superseded — and that turn's own expiry ran before our record existed, so
    # nothing else will spend it. Expire it here rather than leaving live buttons
    # for an answer the conversation no longer wants.
    #
    # ``started_running or slot.running``, not a before/after comparison: a turn
    # that is already in flight when the drain begins and is STILL in flight when
    # it ends (a long cron or injected turn) leaves the flag identical at both
    # ends and may not have appended a row yet, so both a `!=` on running and the
    # total_messages check see nothing. The agent is mid-reply the whole time,
    # which is exactly when the replayed question is most certainly stale.
    #
    # Narrowed to OUR ts, never a session-wide drain: the very turn that makes
    # the replayed question stale can finish mid-drain and record its OWN fresh
    # control in this slot, and spending the whole slot would strike that newer
    # question through — silencing the one the conversation is now waiting on.
    # No live control of ours means there is nothing here to spend.
    # ...and the link itself may be gone. A link followed immediately by an unlink
    # removes the routing before this drain finishes posting, so the control we
    # just rendered as live belongs to a thread nothing owns any more: a click on
    # it starts a FRESH Slack session and answers a question that session never
    # asked. Round 32's unlink abort covers the other order (a control already
    # tracked when the unlink arrives); this covers a control recorded after the
    # unlink already succeeded, where there was nothing yet for it to abort on.
    _unlinked = slot._slack_channel != channel or slot._slack_thread_ts != thread_ts
    if live_ts and (
        _unlinked or started_running or slot.running or slot.total_messages != started_total
    ):
        try:
            await expire_slack_options(state, session_key, ts=live_ts)
        except Exception:
            logger.debug(
                "slack backfill: could not expire a control superseded mid-drain",
                exc_info=True,
            )


def _split_backfill_options(row: dict[str, Any]) -> tuple[str, list[str]]:
    """Split a replayed row into body text and OPTIONS choices.

    Only AGENT-authored rows are parsed. A person's own message can legitimately
    contain the OPTIONS syntax — quoting it, or discussing it — and lifting the
    tag out of their words would render choices they never offered, so a user row
    is returned verbatim with no choices.

    No redaction happens here on purpose. ``build_options_blocks`` runs every
    choice through ``redact_for_display``, which canonicalises the form Slack
    actually shows (ANSI, emphasis and backtick splits, link markup) before
    scanning — strictly stronger than redacting the raw bytes here, and the body
    is covered by ``_format_backfill_parts``. Duplicating the ordering in this
    function is what previously let the two copies drift apart.
    """
    content = backfill_content(row)
    if row.get("role") == "user":
        return content, []
    return extract_options(content)


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

    # Whose control is already live in this thread, captured BEFORE the reassign.
    # ``link_slack`` moves the thread -> slot index onto THIS slot, so resolving
    # afterwards names the new owner and the previous conversation's record
    # becomes unreachable: no dashboard turn would ever expire it, and a click on
    # those still-live buttons would answer into this session instead.
    _prior_owner_keys = slack_options_owner_keys_snapshot(state, thread_ts)

    # Route through the ONE canonical link writer. ``link_slack`` sets the same
    # three slot fields and persists via ``set_slack_link``, but it ALSO
    # registers the thread -> slot reverse index that inbound Slack replies
    # resolve through, and releases the thread from any slot that held it
    # before. Hand-assigning the fields here duplicated everything except that
    # index, so a reply in the mirrored thread routed and persisted correctly
    # while nothing ever told the open tab it had arrived. That same index is
    # what resolves an OPTIONS click on the control replayed below back to this
    # conversation -- without it the click would answer into a separate session.
    state.link_slack(slot.key, thread_ts, target_channel)

    # An ownership change IS supersession, the same call rounds made for a control
    # posted while the owner moved: the question belongs to a conversation that no
    # longer owns this thread, so striking it through is right and re-keying it to
    # us would hand this session an answer to a question it never asked. Our OWN
    # key is skipped -- re-linking a thread to the slot that already holds it must
    # not spend that slot's live control.
    _own_keys = {effective_session_key(slot), slot.key}
    for _prior_key in _prior_owner_keys:
        if _prior_key in _own_keys:
            continue
        try:
            await expire_slack_options(state, _prior_key)
        except Exception:
            logger.debug(
                "slack link: could not retire the previous owner's control",
                exc_info=True,
            )

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

    # Link mutations stay ON the event loop, deliberately. Moving this clear into
    # `asyncio.to_thread` (an earlier revision of this PR) was wrong twice over:
    # the worker runs CONCURRENTLY with the loop, so a compare-and-clear inside it
    # is not atomic against a loop-side relink at all -- the thread can read the
    # captured link, a relink can write its replacement, and the thread then clears
    # that replacement, losing the routing. The session map has no cross-thread
    # lock, so the loop is the only thing serialising its writers. `_save()` is a
    # small atomic temp-file rename on a rare user action; that cost is the price
    # of serialization, and it is the cheaper side of the trade.
    def _clear_persisted_link_sync() -> bool:
        """Clear BOTH persisted key spellings for this slot's link.

        chat_runner copies a dashboard session's link from the bare key onto the
        "dashboard:"-prefixed one when a turn runs, so both spellings must go or
        the next turn re-inherits the link. A channel key has no such twin.

        No compare-and-clear here, and none is needed: this runs on the event loop
        with no await between the read and the write, so nothing can interleave. An
        earlier revision of this PR did compare against a captured value, because
        the clear had been moved into a thread — the serialization above is what
        makes that guard unnecessary, and the test asserting no ``to_thread`` in
        this handler is what keeps it that way.
        """
        done = state.sessions.clear_slack_link(session_key)
        if session_key.startswith("dashboard:"):
            done = state.sessions.clear_slack_link(session_key[len("dashboard:") :]) or done
        return done

    def _restore_persisted_link_sync() -> None:
        """Put back the link this handler cleared, if nothing else claimed it.

        Read first, and by PRESENCE: a link sitting here now was written during the
        await by ``link_slack``, and that writer's value is the live one.
        """
        persisted, _chan = state.sessions.get_slack_link(session_key)
        if not persisted and (prev_thread_ts or prev_channel):
            state.sessions.set_slack_link(session_key, prev_thread_ts, prev_channel)

    # Capture BEFORE the clear's await. A pure dict read, no persistence write, so
    # it costs nothing to do inline -- and doing it here is what lets the clear be
    # conditional on the link not having moved underneath us.
    prev_channel = slot._slack_channel
    prev_thread_ts = slot._slack_thread_ts
    cleared = _clear_persisted_link_sync()
    # Expire FIRST, while the routing is still intact, then clear the link only if
    # it is still the one we captured. Neither plain ordering is safe on its own:
    #
    #   teardown-then-expire  -- between the two the buttons are still live while
    #     the reverse index is already gone, so a click resolves to nothing and
    #     starts a BRAND-NEW session carrying a stale answer.
    #   expire-then-teardown  -- expiry awaits a Slack edit, long enough for
    #     another tab to relink this slot mid-await; resuming would then clear the
    #     REPLACEMENT link's in-memory fields while its persisted link survived,
    #     leaving the two disagreeing.
    #
    # Compare-and-clear satisfies both. The click keeps working (and keeps
    # resolving to THIS slot) for as long as the control is still answerable, and
    # the teardown is conditional on the link not having moved underneath us.
    try:
        await expire_slack_options(state, session_key)
    except asyncio.CancelledError:
        # Gateway shutdown (or any handler cancellation) mid-expiry would otherwise
        # leave this unlink half-committed: persistence was cleared at the top, the
        # in-memory fields were never touched, and none of the restoration below
        # runs. After a restart the routing is gone while the controls are still
        # live on screen — a click then resolves to nothing and starts a fresh
        # session, the exact failure the compare-and-clear ordering exists to
        # prevent, reached by a third route.
        #
        # Restored INLINE, like every other link write here. Off-loading it needed
        # a shield plus a fallback for the loop-teardown case, and the fallback was
        # itself a blocking write on the loop -- all of that complexity bought
        # nothing once link mutations went back to being serialized on the loop.
        _restore_persisted_link_sync()
        raise

    # Persistence was cleared at the top, so anything here now was written DURING
    # the await. Read once: both branches below turn on it.
    persisted_ts, persisted_chan = state.sessions.get_slack_link(session_key)

    if options_records(state, session_key) or answer_routing_in_flight(prev_thread_ts):
        # Two reasons to keep the thread linked, both ending the same way.
        #
        # A tracked record: a returned expiry does NOT prove the control was
        # spent. Records whose Slack edit failed transiently (429, 5xx, network)
        # stay tracked deliberately, so the buttons are still live on screen — and
        # tearing the reverse index down now is precisely the teardown-then-expire
        # order rejected above: a later click resolves to nothing and starts a
        # BRAND-NEW session carrying a stale answer. A failed expiry is
        # teardown-then-NEVER-expire, which is worse.
        #
        # An answer still routing: a click that SUCCEEDED forgot its record and
        # dispatched the answer as a task. The record — this guard's other signal
        # — is already gone while the answer has not yet resolved which session it
        # belongs to, so popping the reverse index now sends the user's selection
        # into a brand-new session. Same corruption, reached from the other side.
        #
        # So abort. Keeping the thread linked is recoverable and visible — the
        # caller retries and the next turn's expiry spends the control — whereas
        # completing the unlink silently corrupts a future conversation. Restore
        # the persisted link we cleared at the top so persistence agrees with the
        # in-memory fields, which were never touched; skip that if something else
        # wrote a link while we awaited, since that writer's value is the live one.
        if not persisted_ts and (prev_thread_ts or prev_channel):
            # ON the loop, deliberately, and this is the settled position after
            # three passes over it. `_save()` serialises the whole session map, so
            # it is a real blocking write -- measured on this box at 0.17ms for 50
            # sessions, 1.0ms for 500 (58KB), 9.8ms at an unrealistic 5000. The two
            # ways to avoid it are both worse:
            #
            #   asyncio.to_thread -- the worker runs CONCURRENTLY with the loop, so
            #     the read-then-write below stops being atomic: a relink can land
            #     between them and this restore would overwrite the replacement
            #     with the link we captured. That is the exact race that forced the
            #     earlier off-loop rewrite of this handler to be reverted.
            #   dropping the restore -- persistence stays cleared while the
            #     in-memory fields still hold the link, so a restart loses the
            #     routing while the buttons are still live. That is the corruption
            #     the abort exists to prevent.
            #
            # Every other session-map mutation in the codebase (including
            # `link_slack` on the link path) writes synchronously on the loop for
            # the same reason, so singling this line out would cost correctness and
            # buy no measurable latency. If the blocking write is ever worth
            # removing, the fix is to serialise ALL session-map writers behind one
            # lock and offload them together -- not to make this one call racy.
            state.sessions.set_slack_link(session_key, prev_thread_ts, prev_channel)
        sel().log_api_access(
            caller="dashboard",
            operation="chat.slack_unlink",
            outcome="deferred",
            source="dashboard",
            resources=slot.key,
            error="an OPTIONS control could not be expired; link kept to keep it routable",
        )
        # 503, not a 200 that lies: the session IS still linked. The dashboard
        # already treats a non-2xx here as "session stays linked", so its view
        # matches the state we just restored without any client change.
        return web.json_response(
            {
                "error": (
                    "An OPTIONS control is still pending or its answer is still "
                    "routing; the session stays linked"
                ),
                "code": "slack_options_pending",
            },
            status=503,
        )

    if slot._slack_channel == prev_channel and slot._slack_thread_ts == prev_thread_ts:
        # Equality does NOT prove nothing moved. A relink to the SAME thread can
        # land during the expiry await and restore the identical channel/ts, which
        # in memory is byte-for-byte indistinguishable from an untouched link.
        #
        # Persistence settles it, and by PRESENCE rather than by value: it was
        # cleared at the top of this handler, and the only writer is ``link_slack``.
        # So a persisted link sitting here now was necessarily written DURING the
        # await -- positive evidence of a relink -- while an empty one is positive
        # evidence that nothing moved. Comparing the channel/ts alone can only
        # guess, and guessing either way breaks the other case: assume "relinked"
        # and an ordinary unlink silently no-ops, assume "nothing moved" and a
        # successful relink is torn down behind the user's back.
        if persisted_ts and persisted_ts == prev_thread_ts and persisted_chan == prev_channel:
            # A relink won the race. Its link is the live one, so leave the slot
            # fields, persistence and the reverse index exactly as it left them --
            # they already agree with each other, which is all the teardown was
            # ever protecting.
            #
            # ``cleared`` goes False so this reports as a no-op: the session is
            # still linked, so claiming ``was_linked`` would tell the UI a teardown
            # happened, and the courtesy note below would announce "replies here no
            # longer sync" into the very thread that is still syncing.
            cleared = False
            logger.debug(
                "slack unlink: same-thread relink landed during expiry, "
                "leaving the replacement intact"
            )
        else:
            # Nothing relinked. Re-clear persistence anyway: it is idempotent
            # here, and it also drops the "dashboard:"-prefixed twin that a turn
            # running mid-await could otherwise re-inherit the link from.
            cleared = _clear_persisted_link_sync() or cleared
            slot._slack_linked = False
            slot._slack_channel = ""
            slot._slack_thread_ts = ""
            # Drop the thread -> slot reverse index too, or the thread keeps
            # resolving to this conversation after the link is gone.
            if prev_thread_ts:
                state._slack_to_slot.pop(prev_thread_ts, None)
    else:
        # A relink landed during the expiry await. Its link is the live one now, so
        # leave every field and the reverse index exactly as the relink left them.
        logger.debug(
            "slack unlink: link changed during expiry, leaving the replacement intact"
        )

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
