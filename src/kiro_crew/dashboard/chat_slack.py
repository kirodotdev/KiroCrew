"""Slack integration — link sessions, channel listing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import strip_control_comments
from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard.chat_backfill import (
    backfill_content,
    gap_summary,
    select_backfill_messages,
    session_deep_link,
)
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    expire_slack_options,
    is_harness_slash_command,
    mint_options_token,
    remember_slack_options,
    slack_options_owner_keys_snapshot,
)
from kiro_crew.dashboard.slot_ownership import checkpoint_slot_replaced, slot_not_found
from kiro_crew.dashboard.state import (
    DashboardState,
    _expected_binding,
    _log_task_exception,
)
from kiro_crew.messaging.link import SLACK_NAMESPACE
from kiro_crew.platform.context import redact_via_context
from kiro_crew.platform.governance_profiles import vet_and_audit
from kiro_crew.security import redact_and_truncate
from kiro_crew.sel import sel
from kiro_crew.slack.channel_resolver import _CACHE_FILENAME, ChannelNameResolver
from kiro_crew.slack.format import (
    build_options_blocks,
    build_options_selected_blocks,
    escape_mrkdwn,
    extract_options,
    render_for_slack,
)
from kiro_crew.slack.outbound import OPTIONS_FALLBACK_TEXT, PostedOptions

logger = logging.getLogger(__name__)

# Fresh-anchor title fallback: when the slot has no LLM title yet
# (titles land seconds after session creation), fall back to a one-line snippet
# of the first user prompt, then to a neutral default. The raw slot key must
# never be user-visible.
_ANCHOR_TITLE_SNIPPET_CHARS = 60
_ANCHOR_TITLE_DEFAULT = "New session"

# Hold the first dashboard send only long enough for the usual Slack anchor.
# A slower request continues in the background; its thread receives the turns
# that start after it lands, and nothing is replayed into it.
AUTO_LINK_HOLD_SECS = 5.0


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
    the redact/convert/split ordering. The icon is passed as the prefix rather
    than prepended afterwards: decorating a maximally-sized part after the split
    pushes it past ``SLACK_MSG_LIMIT`` by the width of the icon plus its space.
    """
    return render_for_slack(
        strip_control_comments(content), prefix=f"{icon} ", redactor=redact_via_context
    )


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
    started_running = slot.turn_running
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

    async def _post_options(
        choices: list[str], *, interactive: bool, row_ts: str | None = None
    ) -> str | None:
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
        # Requires BOTH: without a row ts the mint would fall back to reading the
        # tail off disk, which is the locked, on-loop I/O this path exists to
        # avoid. A row with no ts therefore posts untokened -- honoured on click,
        # the same direction every other unprovable case takes.
        _token = mint_options_token(state, session_key, row_ts) if interactive and row_ts else None
        blocks = (
            build_options_blocks(choices, staleness_token=_token)
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
            # Same origin choice as send_message's session-link button: this
            # marker lands in Slack, so honor slack.use_tunnel_url — a
            # local-only origin is unreachable from a phone. No click token:
            # backfill markers can reach shared channels. The tunnel-vs-not
            # decision lives in one shared helper (tunnel_origin_if_opted_in).
            from kiro_crew.dashboard.urls import tunnel_origin_if_opted_in

            tunnel_url = tunnel_origin_if_opted_in(cfg.slack.use_tunnel_url)
            link = session_deep_link(cfg.dashboard.url, slot.key, tunnel_url=tunnel_url)
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
            posted_ts = await _post_options(
                choices, interactive=idx == newest, row_ts=row.get("ts")
            )
            if posted_ts:
                live_ts = posted_ts

    # Did the conversation move past the replayed question while we were
    # draining? A turn that was running at any point, or a transcript that grew,
    # means the newest reply we just rendered as a LIVE control is already
    # superseded — and that turn's own expiry ran before our record existed, so
    # nothing else will spend it. Expire it here rather than leaving live buttons
    # for an answer the conversation no longer wants.
    #
    # ``started_running or slot.turn_running``, not a before/after comparison: a turn
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
    # asked. The unlink abort covers the other order (a control already
    # tracked when the unlink arrives); this covers a control recorded after the
    # unlink already succeeded, where there was nothing yet for it to abort on.
    _unlinked = slot._slack_channel != channel or slot._slack_thread_ts != thread_ts
    if live_ts and (
        _unlinked or started_running or slot.turn_running or slot.total_messages != started_total
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
    function would let the two copies drift apart.
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


def _slack_link_lock(state: DashboardState, session_key: str) -> asyncio.Lock:
    """Return the per-session lock that serialises one Slack link attempt.

    Same shape as the transcript transaction locks in ``chat_handlers``: a
    weak-value registry, so an idle session's lock is reclaimed with its last
    reference and the table is bounded by live attempts, not by sessions seen.
    """
    lock = state._slack_link_locks.get(session_key)
    if lock is None:
        lock = asyncio.Lock()
        state._slack_link_locks[session_key] = lock
    return lock


class SlackLinkError(Exception):
    """A Slack link could not be made.

    ``status`` is the HTTP status the API answers with and ``code`` the
    machine-readable reason the dashboard localises; ``message`` is advisory
    English prose beside it.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# The one line every auto-opened thread starts with; the manual button's anchor
# says "linked", so a reader can tell the two apart in the channel.
_AUTO_LINK_ANCHOR_SUFFIX = "Session connected automatically from the dashboard."
_MANUAL_LINK_ANCHOR_SUFFIX = "Session linked from dashboard."


async def link_slot_to_slack(
    state: DashboardState,
    slot: Any,  # _ChatSlot (avoids import cycle)
    *,
    channel: str = "",
    existing_thread: str = "",
    backfill: bool = True,
    anchor_suffix: str = _MANUAL_LINK_ANCHOR_SUFFIX,
    operation: str = "chat.slack_link",
    governed: bool = True,
) -> dict[str, Any]:
    """Link *slot* to a Slack thread and return the API response payload.

    The ONE place a dashboard session becomes a Slack thread: the Connect to
    Slack button, the challenge-and-redirect auto-link from a thread the user
    replied in, and the ``slack.auto_link_sessions`` first-message hook all
    come through here, so they cannot drift on which fields a link sets or
    which index a reply resolves through.

    ``channel`` empty or ``"dm"`` opens the owner's DM; anything else is a
    channel ID. ``existing_thread`` links to a thread that already exists
    instead of posting a new anchor. ``backfill`` replays the slot's history
    into a NEW thread; the first-message hook passes ``False`` because the
    turn that follows echoes the only message the slot holds, and a replay
    would post it twice.

    ``governed`` applies the fail-closed channel egress check, on by default so
    a new caller cannot skip it by omission. The manual Connect row keeps its
    established direct Slack behavior by passing ``governed=False`` itself.

    Raises :class:`SlackLinkError` when the link cannot be made. Every write
    happens after the thread exists, so a refusal leaves no half-written link.
    """
    # The slot's OWN session key: a channel-born slot's turns run on the
    # channel session, so the link has to live there for the turn path and the
    # link projection (state._slot_links) to find it.
    session_key = effective_session_key(slot)
    # Serialise the complete link attempt for this session. In particular, the
    # already-linked check stays inside the lock so automatic and manual callers
    # cannot both post anchors and then replace each other's binding. The
    # registry holds the lock weakly: this local reference keeps it alive for
    # the holder and every waiter, and the entry vanishes with the last of them.
    lock = _slack_link_lock(state, session_key)
    async with lock:
        if not state.slack_client:
            raise SlackLinkError(503, "slack_not_connected", "Slack not connected")
        owner_id = getattr(state, "owner_id", None)
        if not owner_id:
            raise SlackLinkError(500, "owner_not_configured", "owner not configured")

        # The automatic anchor is EGRESS: it carries the session's title or first
        # prompt into a channel. Apply the shared fail-closed governance ladder before
        # any automatic Slack side effect, including ``open_dm``. The manual Connect
        # row keeps its established direct behavior. Off the loop: the check reads
        # policy files.
        if governed:
            try:
                decision = await asyncio.to_thread(
                    vet_and_audit,
                    "channels",
                    SLACK_NAMESPACE,
                    session_key=session_key,
                    tool_name=operation,
                    fail_closed=True,
                )
                permitted = bool(getattr(decision, "permitted", False))
            except Exception:
                logger.debug("slack link governance check failed; refusing", exc_info=True)
                permitted = False
            if not permitted:
                raise SlackLinkError(403, "channel_not_permitted", "channel is not permitted")

        # Check if already linked
        existing_ts, existing_chan = state.sessions.get_slack_link(session_key)
        if existing_ts and existing_chan:
            try:
                await state.slack_client.post_message(
                    existing_chan,
                    "🔗 Session linked from dashboard — continuing here.",
                    existing_ts,
                )
            except Exception:
                pass
            return {
                "ok": True,
                "already_linked": True,
                "thread_ts": existing_ts,
                "channel": existing_chan,
            }

        if not channel or channel == "dm":
            target_channel = await state.slack_client.open_dm(owner_id)
        else:
            target_channel = channel

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
            title = escape_mrkdwn(title)
            thread_ts = await state.slack_client.post_message(
                target_channel, f"\U0001f9f5 *{title}*\n{anchor_suffix}"
            )
            if not thread_ts:
                raise SlackLinkError(500, "thread_create_failed", "failed to create thread")

        # The Slack post above can outlive the dashboard session. A retracted or
        # closing slot must not regain a persisted binding or receive a turn after
        # teardown. Only a newly-created thread gets a courtesy note: an existing
        # thread already has an owner and must not be told it was unlinked.
        if not slot_is_live(state, slot):
            if not existing_thread:
                try:
                    await state.slack_client.post_message(
                        target_channel,
                        "\U0001f50c _Unlinked from dashboard — the session was closed._",
                        thread_ts,
                    )
                except Exception:
                    logger.debug(
                        "slack link: could not post closed-session courtesy note",
                        exc_info=True,
                    )
            raise SlackLinkError(409, "slot_closed", "session closed during link")

        # Strike the previous owner's control through on the way past, so the thread
        # does not visibly carry a question that now belongs to another conversation.
        #
        # Best effort, and nothing depends on it landing: the control's own token
        # names the conversation that asked, so a click on it is refused when it
        # arrives whether or not this edit succeeded. Our OWN key is skipped --
        # re-linking a thread to the slot that already holds it must not strike that
        # slot's live control.
        # Read BEFORE the reassign: ``link_slack`` moves the thread -> slot index onto
        # THIS slot, so resolving afterwards would name the new owner and the previous
        # conversation's control would never be found to strike through.
        _prior_owner_keys = slack_options_owner_keys_snapshot(state, thread_ts)
        _own_keys = {effective_session_key(slot), slot.key}
        _prior_keys = [k for k in _prior_owner_keys if k not in _own_keys]
        for _prior_key in _prior_keys:
            try:
                await expire_slack_options(state, _prior_key)
            except Exception:
                logger.debug(
                    "slack link: could not retire the previous owner's control",
                    exc_info=True,
                )

        # Route through the ONE canonical link writer. ``link_slack`` sets the same
        # three slot fields and persists via ``set_slack_link``, but it ALSO
        # registers the thread -> slot reverse index that inbound Slack replies
        # resolve through, and releases the thread from any slot that held it
        # before. Hand-assigning the fields here duplicated everything except that
        # index, so a reply in the mirrored thread routed and persisted correctly
        # while nothing ever told the open tab it had arrived. That same index is
        # what resolves an OPTIONS click on the control replayed below back to this
        # conversation -- without it the click would answer into a separate session.
        try:
            # `link_slack` writes the map inline on leaving its batch, so a failed
            # write can surface here as well as from the flush below; both leave
            # a binding only memory holds, and both take it back down.
            state.link_slack(slot.key, thread_ts, target_channel)
            # Persist before publishing: the map's writer is debounced, and everything
            # below -- the transcript backfilled into the thread, the slots push, the
            # `{ok, thread_ts}` answer -- tells the user the thread is linked. A gateway
            # exit before the deferred write would drop the link on restart and leave a
            # thread full of this transcript that no session owns. (Same point as the
            # unlink routes; `link_slack`'s own slot redraw precedes this, and a redraw
            # the next push corrects is not a report the user acts on.)
            await state.sessions.aflush()
        except Exception as exc:
            # A link the map could not write is not one to mirror into: a restart
            # would drop it with the turns already posted there. Take this slot's
            # binding back down so memory agrees with what the next write saves.
            _drop_unsaved_link(state, slot, session_key, thread_ts)
            raise SlackLinkError(500, "link_not_saved", "could not save the Slack link") from exc

        # Seed the new thread with readable history — only when we created a NEW
        # thread. Linking to an existing thread (challenge-and-redirect) would
        # duplicate messages the thread already contains.
        if backfill and not existing_thread:
            # No mint here. The drain mints its own token off the loop: doing it in
            # this handler put either blocking transcript I/O on the event loop, or a
            # thread-pool hop ahead of the spawn below -- and that hop cost the spawned
            # task its scheduling window under load, so the control never reached
            # Slack. Inside the task the hop delays only that task's own posting.
            _spawn_slack_backfill(state, slot, target_channel, thread_ts)

        sel().log_api_access(
            caller="dashboard",
            operation=operation,
            outcome="success",
            source="dashboard",
            resources=slot.key,
        )
        state.push_slots_update()
        return {"ok": True, "thread_ts": thread_ts, "channel": target_channel}


def _drop_unsaved_link(
    state: DashboardState,
    slot: Any,
    session_key: str,
    thread_ts: str,
) -> None:
    """Undo a ``link_slack`` whose map write failed, for THIS slot only.

    Only while the map still names *thread_ts* for the session: the write is a
    thread hop, and a binding something else wrote meanwhile is not ours to
    remove. The reverse index is dropped only while it still names this slot. A
    thread the link took from another session is not handed back: that session
    keeps whatever the map holds for it, and the person relinks by hand.
    """
    held_ts, _held_chan = state.sessions.get_slack_link(session_key)
    if held_ts != thread_ts:
        return
    state.sessions.clear_slack_link(session_key)
    slot._slack_linked = False
    slot._slack_channel = ""
    slot._slack_thread_ts = ""
    if state._slack_to_slot.get(thread_ts) == slot.key:
        state._slack_to_slot.pop(thread_ts, None)
    state.push_slots_update()


def slot_is_live(state: DashboardState, slot: Any) -> bool:  # noqa: ANN001 — _ChatSlot
    """Whether *slot* is still the dashboard's live slot under its key.

    False once teardown has begun or the key resolves to nothing or to a
    different object. Uses the same two lookups the link handler resolves a
    slot with, so a slot that handler found is one this test recognises.
    """
    if getattr(slot, "is_closing", False):
        return False
    return state.get_slot(slot.key) is slot or state._slots.get(slot.key) is slot


def _read_auto_link_settings() -> tuple[bool, bool]:
    """Read ``slack.auto_link_sessions``; off on any failure.

    Also reports whether the agent provider is Claude Code, which decides which
    first words the runner treats as harness slash commands. The load is
    mtime-cached, so a Settings change applies to the next new session without
    a restart. **Call this OFF the event loop**: the cache still stats the file.
    A failed read means no thread is opened, the direction this has to fail in.
    """
    try:
        cfg = KiroCrewConfig.load()
        return bool(cfg.slack.auto_link_sessions), is_claude_code(cfg.agent.provider)
    except Exception:
        logger.debug("slack.auto_link_sessions lookup failed; not auto-linking")
        return False, False


def auto_link_eligible(state: DashboardState, slot: Any) -> bool:  # noqa: ANN001 — _ChatSlot
    """Whether *slot* is a NEW dashboard session a person just sent the first message in.

    Decides the ``slack.auto_link_sessions`` hook from facts the slot carries,
    never from the message text:

    - ``_origin == USER`` and no ``_created_by``: created by a person in the
      dashboard. Cron, app, sub-agent and gateway-internal slots declare
      another origin or none; a slot an agent opened through session control
      carries USER but names its creator.
    - not ``channel_origin`` / not remote: a conversation that already lives on
      a channel, or whose turns run on a peer crew, has nothing to mirror here.
    - ``memory_mode == "persistent"``: an incognito or temporary session keeps
      no memory, and posting its transcript into Slack would persist it anyway.
    - exactly ONE user message held and no older rows on disk: this send is
      the session's first. A session that existed before the setting was turned
      on has more, and is left alone. A failed attempt is not retried: the
      second send has two.
    - no link yet, whichever way it was made.
    """
    if getattr(slot, "_origin", "") != dashboard_state.SlotOrigin.USER:
        return False
    # ``session_control``'s create verb stamps USER on the slots an AGENT opens
    # for its own work and records itself in ``_created_by``; the origin alone
    # does not prove a person started the session, the marker does.
    if getattr(slot, "_created_by", ""):
        return False
    if getattr(slot, "channel_origin", False) or getattr(slot, "executor", "") == "remote":
        return False
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        return False
    if getattr(slot, "_disk_older_count", 0):
        return False
    # `/clear` empties `messages` but leaves the cleared rows on disk counted
    # here, so a cleared older session does not pass for a new one.
    if getattr(slot, "_disk_older_durable_count", 0):
        return False
    user_count = sum(1 for m in slot.messages if m.get("role") == "user")
    if user_count != 1:
        return False
    existing_ts, existing_chan = state.sessions.get_slack_link(effective_session_key(slot))
    if existing_ts and existing_chan:
        return False
    return True


async def maybe_auto_link_slack(state: DashboardState, slot: Any) -> bool:  # noqa: ANN001
    """Open the owner-DM Slack thread for an eligible first dashboard send.

    The send handler waits up to :data:`AUTO_LINK_HOLD_SECS` so the normal fast
    path links before dispatch and the turn that follows mirrors its first
    message live. A slower Slack request keeps running as a tracked task: the
    link is still made when it lands, and the thread receives the turns that
    start after that. Nothing is replayed into it -- the turn that ran during
    the hold is not owed to the thread. Failures remain best effort and let the
    send proceed unlinked. Returns whether a NEW link was made inside the hold.
    """
    if not getattr(state, "slack_client", None):
        return False
    if not auto_link_eligible(state, slot):
        return False
    enabled, cc_provider = await asyncio.to_thread(_read_auto_link_settings)
    if not enabled:
        return False
    # The config hop yielded; a second send or a manual Connect could have
    # landed in the meantime.
    if not auto_link_eligible(state, slot):
        return False
    # A harness slash command stays inside the runner: it is never echoed or
    # mirrored, so a thread opened for it would hold nothing but the command.
    first_word = (_first_user_prompt(slot).split() or [""])[0]
    if is_harness_slash_command(first_word, cc_provider=cc_provider):
        return False

    def _log_late(task: asyncio.Task[dict[str, Any]]) -> None:
        # Retrieve the outcome of a link that outlived the hold, so a failure is
        # logged here instead of as an unretrieved-exception warning at shutdown.
        if task.cancelled():
            logger.debug("late Slack auto-link cancelled for %s", slot.key)
            return
        exc = task.exception()
        if isinstance(exc, SlackLinkError):
            logger.debug("late Slack auto-link skipped for %s: %s", slot.key, exc.message)
        elif exc is not None:
            logger.debug("late Slack auto-link failed for %s", slot.key, exc_info=exc)

    link_task = asyncio.create_task(
        link_slot_to_slack(
            state,
            slot,
            backfill=False,
            anchor_suffix=_AUTO_LINK_ANCHOR_SUFFIX,
            operation="chat.slack_auto_link",
            governed=True,
        )
    )
    state._background_tasks.add(link_task)
    link_task.add_done_callback(state._background_tasks.discard)
    link_task.add_done_callback(_log_late)
    try:
        result = await asyncio.wait_for(asyncio.shield(link_task), timeout=AUTO_LINK_HOLD_SECS)
    except Exception:
        # A timeout leaves the shielded task running; a failure is logged by
        # ``_log_late`` when the task settles. Either way the send goes unlinked.
        return False
    return bool(result.get("ok")) and not result.get("already_linked")


async def api_chat_slot_slack_link(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/slack-link — link a dashboard session to Slack."""

    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    body = await request.json() if request.content_length else {}
    raw_channel = body.get("channel", "")
    # When the caller supplies an existing thread_ts (challenge-and-redirect
    # auto-link from a Slack thread the user replied in), link to THAT thread
    # rather than posting a new one — this is what makes a thread reply route
    # back to its dashboard session bidirectionally.
    existing_thread = str(body.get("thread_ts", "") or "")
    try:
        result = await link_slot_to_slack(
            state, slot, channel=raw_channel, existing_thread=existing_thread, governed=False
        )
    except SlackLinkError as exc:
        return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    return web.json_response(result)


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
    # Reached from mirror-unlink after its body read, so the slot found now must
    # be the one the per-slot checkpoint judged.
    if not slot or checkpoint_slot_replaced(request, slot):
        return slot_not_found()

    # Authoritative key = the slot's own session key. Deriving it from the slot
    # NAME instead would build "dashboard:slack:<ts>" for a channel-born slot,
    # leaving the real link untouched so mirroring silently resumes next turn.
    session_key = effective_session_key(slot)

    # Link mutations stay ON the event loop: `_save()` is a small atomic
    # temp-file rename on a rare user action, and moving a clear into
    # `asyncio.to_thread` buys nothing. The map serialises its writers under its
    # own lock, and a compare-and-clear is atomic only when BOTH steps run under
    # it -- which is why the body-armed path below is one map call
    # (`clear_slack_link_if`) and this helper serves the bodiless path alone.
    def _clear_persisted_link_sync() -> bool:
        """Clear BOTH persisted key spellings for this slot's link, unconditionally.

        chat_runner copies a dashboard session's link from the bare key onto the
        "dashboard:"-prefixed one when a turn runs, so both spellings must go or
        the next turn re-inherits the link. A channel key has no such twin. For
        a caller with no row in hand there is nothing to compare against, so
        this is the plain clear; a body that names a row goes through the map's
        compare-and-clear instead.
        """
        done = state.sessions.clear_slack_link(session_key)
        if session_key.startswith("dashboard:"):
            done = state.sessions.clear_slack_link(session_key[len("dashboard:") :]) or done
        return done

    # A click carries the identity of the conversation that asked it, so one
    # arriving after the link is gone is refused on its own terms instead of
    # resolving to nothing and starting a brand-new session carrying a stale
    # answer -- so the strike-through need not run before the teardown.
    prev_channel = slot._slack_channel
    prev_thread_ts = slot._slack_thread_ts
    expected = await _expected_binding(request)
    if expected is not None:
        # Same guard as mirror-unlink, on the Slack fields the row is projected
        # from: a stale Slack row must not tear down a thread this slot was
        # re-linked to after that row was drawn. Compare and clear are ONE
        # guarded step in the map (both key spellings), so no re-link can land
        # between them. False is a mismatch and nothing was touched.
        channel_type, token = expected
        if not state.sessions.clear_slack_link_if(session_key, channel_type, token):
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slack_unlink",
                outcome="denied",
                source="dashboard",
                resources=f"{slot.key} reason=mirror_changed",
            )
            logger.info("slack unlink: %s refused, the link changed under the menu", slot.key)
            return web.json_response(
                {
                    "error": "the session's linked channel changed; nothing was unlinked",
                    "code": "mirror_changed",
                },
                status=409,
            )
        cleared = True
    else:
        cleared = _clear_persisted_link_sync()
    # Persist before publishing: the map's writer is debounced, and everything
    # below -- the slot's own fields, the courtesy note in the thread, the slots
    # push, the `{ok, was_linked}` answer -- tells the user the thread is gone.
    # A gateway exit before the deferred write would reload the link on restart
    # and make every one of those a lie. (Same point as `mirror-unlink`.)
    #
    # The in-process teardown is in the `finally` so it completes whether or not
    # the write lands. `aflush` re-raises a failed write (a full or read-only
    # data home), and the failure must surface -- the answer is the existing
    # error path, not an `ok`. But the map is already clear in memory by then,
    # and a teardown skipped by the raise would leave the slot's fields and the
    # thread's reverse-index entry asserting a thread the map does not hold:
    # the row keeps rendering from the fields, a reply in the thread still
    # resolves to this slot, and a retried Unlink is 409 because the map has no
    # thread to compare. So the fields follow the map, in success and in
    # failure alike; only what the user is TOLD waits for durability.
    #
    # And they follow the map LITERALLY: the teardown is conditional on what the
    # map holds once the await returns. The write is a real thread hop, and a
    # second same-slot request can run a whole `slack-link` inside it -- the
    # existing-thread branch reaches `link_slack` with no network await -- so
    # the map may hold a NEW binding by the time control comes back here. An
    # unconditional teardown would strip that new link's fields and reverse
    # index while the map keeps asserting it, and a re-link then short-circuits
    # on `already_linked`, so nothing in-process ever restores them. The map was
    # cleared above, so any link it holds now is that newer write, whatever its
    # thread: it keeps its fields and its index (`link_slack` already retired
    # the old thread's entry), and the answer says so. No link means the old
    # binding is the one to take down.
    relinked = False
    try:
        await state.sessions.aflush()
    finally:
        newer_thread_ts, _newer_channel = state.sessions.get_slack_link(session_key)
        if newer_thread_ts:
            relinked = True
        else:
            slot._slack_linked = False
            slot._slack_channel = ""
            slot._slack_thread_ts = ""
            if prev_thread_ts:
                # Or the thread keeps resolving to this conversation after the link is gone.
                state._slack_to_slot.pop(prev_thread_ts, None)

    # Presentation only: leave the thread without a question nothing will answer.
    # Swallowed on failure -- an un-struck control is untidy, not unsafe, because
    # the click it invites is refused when it arrives. Not when a relink landed:
    # the session is linked again, possibly to the very same thread, and a
    # control struck through there would be one the new link still answers.
    if not relinked:
        try:
            await expire_slack_options(state, session_key)
        except Exception:
            logger.debug(
                "slack unlink: could not strike the pending OPTIONS control through",
                exc_info=True,
            )

    # Best-effort courtesy note so a Slack watcher knows why the thread went
    # quiet. Same redaction path as the link endpoint; failure is non-fatal.
    # Withheld after a relink for the same reason as the strike: a thread that
    # was just linked again must not be told its replies stopped syncing.
    if cleared and not relinked and state.slack_client and prev_channel and prev_thread_ts:
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
        resources=f"{slot.key} (relinked meanwhile)" if relinked else slot.key,
    )
    state.push_slots_update()
    # `relinked` names the race for the caller: the binding it named is gone,
    # and a newer link stands -- the slots push it receives carries that row.
    return web.json_response({"ok": True, "was_linked": cleared, "relinked": relinked})


async def api_chat_slot_slack_pause(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/slack-pause — set whether turns reach the thread.

    Body: ``{"paused": bool}``, defaulting to ``true``. This is the whole of the
    dashboard's Slack connect/disconnect control for a session that already has a
    thread, in BOTH directions — which is why it SETS a state rather than only
    muting. Reconnecting by re-issuing ``slack-link`` cannot serve a session that
    was BORN in its thread: there is no binding to re-establish, so the row would
    render with no way back. One endpoint that sets either way keeps every row
    behaving identically regardless of how its conversation started.

    Disconnecting is not unlinking. The thread binding, both coordinate fields and
    the reverse index all survive, so a reply in the thread still resolves to THIS
    session and resumes it rather than forking a new one; only outbound turn
    mirroring stops (see ``chat_utils.slack_mirror_is_paused`` for the exact
    scope).

    The write stays ON the event loop, matching ``slack-unlink``: the session map
    has no cross-thread lock, so the loop is the only thing serialising its
    writers, and a flag write moved into a worker could interleave with a
    loop-side relink.

    Idempotent, reporting the prior state as ``was_paused``. Auth posture is
    identical to slack-link and slack-unlink — mixed-internal via the
    ``/api/chat`` prefix, needing no new entry in either path set.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    # Only an explicit boolean `false` connects. Everything else — a missing key,
    # `null`, `0`, `""` — disconnects, because disconnecting only ever reduces
    # what leaves the process, so ambiguous input should fail toward the quiet
    # side rather than start delivering into a channel on a malformed request.
    paused = body.get("paused", True) is not False

    # The slot's OWN session key. Deriving it from the slot NAME would build
    # "dashboard:slack:<ts>" for a channel-born slot and set the flag on a session
    # that does not exist, leaving the real thread delivering.
    session_key = effective_session_key(slot)
    thread_ts, channel_id = state.sessions.get_slack_link(session_key)
    if not (thread_ts and channel_id):
        return web.json_response({"error": "not linked", "code": "slack_not_linked"}, status=409)

    # Coerced, not passed through: this value is serialised into the response, so
    # a SessionManager stub or an older implementation returning a non-bool would
    # turn a working disconnect into a 500 at the JSON boundary.
    # Called ON the loop deliberately, NOT via ``to_thread``. ``SessionMap._save``
    # branches on whether its caller has a running loop: on the loop it marks the
    # map dirty and schedules ONE debounced flush that does the disk write in a
    # worker, so the loop never pays the write inline; with no running
    # loop it writes inline on the calling thread. Offloading therefore selects
    # the inline-write branch and does that write while holding ``_MAP_LOCK``, so
    # any loop-side mutator then blocks the whole loop on the lock — strictly
    # worse than calling it here.
    was_paused = bool(state.sessions.set_slack_paused(session_key, paused))
    # Persist before publishing: the flag's write is debounced, and everything
    # below -- the note in the thread, the slots push, the `{ok, paused}` answer
    # -- reports a pause (or resume) the user just acted on. A gateway exit before
    # the deferred write would revert it on restart without a word: a thread the
    # user muted starts delivering again. (Same point as the unlink routes.)
    await state.sessions.aflush()

    # Posted INTO the Slack thread, not shown in the dashboard. Without it the
    # thread simply dead-ends and anyone watching cannot tell a disconnected
    # conversation from a stalled one. Only on the transition, so an idempotent
    # re-disconnect stays silent. It states the fact and stops: that a reply
    # reconnects is a given, not something to advertise.
    #
    # The note is EGRESS, so it is governed like any other send. The disconnect
    # itself is NOT gated: disconnecting only ever reduces what leaves the
    # process, and refusing it because the channel is denied would strand the user
    # connected to a channel they are trying to leave. So a denial silences the
    # note and keeps the disconnect.
    if paused and not was_paused and state.slack_client:
        note_permitted = False
        try:
            decision = await asyncio.to_thread(
                vet_and_audit,
                "channels",
                SLACK_NAMESPACE,
                session_key=session_key,
                tool_name="chat.slack_disconnect_note",
                fail_closed=True,
            )
            note_permitted = bool(getattr(decision, "permitted", False))
        except Exception:
            logger.debug("disconnect note governance check failed", exc_info=True)
            note_permitted = False
        if note_permitted:
            try:
                await state.slack_client.post_message(
                    channel_id,
                    "\U0001f50c _Disconnected — the conversation continues in the dashboard._",
                    thread_ts,
                )
            except Exception:
                logger.debug("disconnect note delivery failed", exc_info=True)

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slack_pause" if paused else "chat.slack_resume",
        outcome="noop" if was_paused == paused else "success",
        source="dashboard",
        resources=slot.key,
    )
    logger.info("slack-pause: %s paused=%s (was=%s)", slot.key, paused, was_paused)
    return web.json_response({"ok": True, "was_paused": was_paused, "paused": paused})


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
