"""Fork session — copy messages into a new tab."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop, session_was_deleted
from kiro_crew.dashboard.chat_utils import (
    _sync_dashboard_slots,
    effective_session_key,
    history_corpus_unreadable,
    slot_history_key,
)
from kiro_crew.dashboard.state import (
    _MAX_SLOT_MESSAGES,
    MAX_LIVE_SLOTS,
    DashboardState,
    request_slot_origin,
)
from kiro_crew.history import carry_provenance
from kiro_crew.history_projection import drop_persisted_tail_prefix as _drop_persisted_tail_prefix
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_directive import SECTION_MARKER_ROLE

logger = logging.getLogger(__name__)

_FORK_TITLE_MARKER = "↳ "

# Attempts to land a transcript read and the unpersisted tail on ONE consistent
# view of the slot. Matches session_transfer._SNAPSHOT_ATTEMPTS and
# chat_persistence._FLUSH_SNAPSHOT_RETRIES, which bound the same race.
_SNAPSHOT_ATTEMPTS = 4

# Fork direction: "head" copies messages up to and including the fork point
# (the default); "tail" copies only the messages after it.
_FORK_DIRECTION_HEAD = "head"
_FORK_DIRECTION_TAIL = "tail"
_FORK_DIRECTIONS = (_FORK_DIRECTION_HEAD, _FORK_DIRECTION_TAIL)
_MAX_MESSAGE_ID_CHARS = 256

# Roles a fork COPIES. Wider than the visible index space above on purpose: a
# section marker is a boundary between turns, so dropping it moves the boundary.
_FORKED_ROLES = ("user", "assistant", SECTION_MARKER_ROLE)


def _slice_boundary(visible_positions: list[int], at_index: int, corpus_len: int) -> int:
    """Corpus position that divides visible turn ``at_index`` from the next one.

    The NEXT VISIBLE row, not ``position + 1``: a section marker closes the section
    the preceding turn ended, so every non-visible row between the two travels with
    the turn ABOVE it. Using ``position + 1`` splits the pair the other way, which
    drops the marker from a head fork and makes it the orphaned first row of a tail.
    """
    following = at_index + 1
    if following < len(visible_positions):
        return visible_positions[following]
    return corpus_len


def drop_persisted_tail_prefix(full_disk: list[dict], tail: list[dict]) -> list[dict]:
    """Re-exported from ``history_projection``, which owns the identity rule.

    Kept importable from here because this module was its first consumer and is
    where callers and tests reach for it.
    """
    return _drop_persisted_tail_prefix(full_disk, tail)


async def api_chat_slot_fork(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/fork — fork session into a new tab.

    With ``direction="head"`` (default) copies messages up to and including
    ``at_message_index``. With ``direction="tail"`` copies
    only the messages after ``at_message_index``; the head is dropped.
    An optional ``prompt`` is returned so the frontend can send it.

    Body: ``{ at_message_index?: number, at_message_id?: string, prompt?: string,
    mode?: string, direction?: "head"|"tail" }``
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # Rate/resource guard: reject if we're already at the cap. Counts slots still
    # under construction too (``live_slot_count``): the import path retracts a
    # slot from ``_slots`` while it is built, and those are allocated memory this
    # cap would otherwise ignore.
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_fork",
            outcome="denied",
            source="rate_limit",
            resources=f"slot={name},slot_count={state.live_slot_count()}",
            error="slot cap reached",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "slot_cap_reached",
            },
            status=429,
        )

    # App ownership check (App Kit §5.2)
    if request_app:
        if not slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="chat.slot_fork",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot fork unscoped slots",
            )
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
        if slot._app != request_app:
            sel().log_api_access(
                caller=request_app,
                operation="chat.slot_fork",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app does not own this slot",
            )
            # Return 404 (not 403) so a slot owned by another app / an unscoped
            # slot is indistinguishable from a non-existent one — prevents an
            # app-scoped caller enumerating slots across the isolation boundary
            # (CWE-204). The true reason is recorded server-side via SEL above.
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    if slot.memory_mode != "persistent":
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_fork",
            outcome="denied",
            source="dashboard",
            resources=f"slot={name},memory_mode={slot.memory_mode}",
            error="non-persistent slot",
        )
        return web.json_response(
            {
                "error": "cannot fork a non-persistent session",
                "code": "slot_not_persistent",
            },
            status=400,
        )
    if request.body_exists:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid JSON body", "code": "invalid_json"}, status=400
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "body must be a JSON object", "code": "body_not_object"},
                status=400,
            )
    else:
        body = {}
    at_index = body.get("at_message_index")
    at_message_id = body.get("at_message_id")
    if at_message_id is not None and (
        not isinstance(at_message_id, str)
        or not at_message_id.strip()
        or len(at_message_id) > _MAX_MESSAGE_ID_CHARS
    ):
        return web.json_response(
            {
                "error": (
                    "at_message_id must be a non-empty string of at most "
                    f"{_MAX_MESSAGE_ID_CHARS} characters"
                ),
                "code": "invalid_field_type",
            },
            status=400,
        )
    prompt = body.get("prompt")
    mode_override = body.get("mode")
    if mode_override is not None and mode_override not in ("", "orchestrator", "crew"):
        return web.json_response(
            {
                "error": "mode must be '', 'orchestrator' or 'crew'",
                "code": "invalid_mode",
            },
            status=400,
        )
    direction = body.get("direction", _FORK_DIRECTION_HEAD)
    if direction not in _FORK_DIRECTIONS:
        return web.json_response(
            {
                "error": f"direction must be one of {list(_FORK_DIRECTIONS)}",
                "code": "invalid_direction",
            },
            status=400,
        )
    if direction == _FORK_DIRECTION_TAIL and not KiroCrewConfig.load().dashboard.tail_fork_enabled:
        # Server-side gate: tail-fork requested but disabled in config —
        # fall back to a normal head-fork rather than reject the request outright.
        # outcome="allowed" (not "denied"): the request still succeeds, just as a
        # head-fork instead of the requested tail-fork; "denied" would misleadingly
        # suggest the fork itself was rejected.
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_fork",
            outcome="allowed",
            source="dashboard",
            resources=f"slot={name},direction=tail",
            error="tail_fork_enabled is False; falling back to head-fork",
        )
        direction = _FORK_DIRECTION_HEAD
    if prompt is not None and not isinstance(prompt, str):
        return web.json_response(
            {"error": "prompt must be a string", "code": "invalid_field_type"},
            status=400,
        )
    prompt = (prompt or "").strip()
    if len(prompt) > 32_768:
        return web.json_response(
            {
                "error": "prompt too long (max 32768 chars)",
                "code": "prompt_too_long",
            },
            status=400,
        )

    # Read disk FIRST (full history). Stable message IDs are resolved against this
    # complete corpus; the legacy index fallback also has to use the same chained
    # view the fully-loaded frontend renders. Without the chained read, an archived
    # target is reported missing (ID path) or indices past the current file boundary
    # fail out of range (legacy path).
    async with slot._fork_lock:
        all_messages: list[dict] = []
        new_msgs: list[dict] = []
        # Two tail candidates, because a boundary ahead of the resident window has
        # TWO causes needing OPPOSITE remedies and they can only be told apart
        # once the true on-disk length is known -- i.e. after the read. Both are
        # snapshotted ON THE LOOP so whichever is chosen still pairs with the
        # boundary the read observed.
        tail: list[dict] | None = None
        capped_tail: list[dict] | None = None
        # True when the read proved disk holds rows the slot's counters do not
        # represent (the capped-restore signature). Gates BOTH flush sites in this
        # handler, because the save's frozen prefix cannot protect those rows.
        disk_holds_unrepresented = False
        if state.conversation_log:
            # Pair the disk read with the unpersisted tail on ONE consistent view
            # of the slot, using the idiom session_transfer._snapshot_transcript
            # already uses: capture the boundary, read, re-check, retry on change.
            #
            # The offset is ``_disk_window_len`` -- "how many window messages are
            # now on disk", advanced by the save path (chat_persistence
            # ``_save_slot_to_history``). Two nearby counters cannot serve here:
            #
            #   * ``_resumed_count`` records only how many messages were loaded
            #     when the slot was rehydrated. The flush never advances it, so a
            #     persisted tail stays inside the slice and reconciles in twice.
            #     session_transfer hit exactly this and documents it.
            #   * a length captured on a ``_dirty`` transition fails the same way.
            #     ``_dirty`` is a boolean, so it cannot distinguish "never flushed"
            #     from "flushed, then re-dirtied by an append" -- and in that
            #     interleaving no transition registers at all.
            #
            # ``_dirty_gen`` (monotonic, bumped centrally by the ``_dirty`` setter)
            # catches in-place edits that move neither boundary nor length; the
            # length is a backstop for any path that mutates ``slot.messages``
            # without marking dirty. There is deliberately no ``_dirty`` gate on
            # the merge below: the boundary alone is authoritative and the slice is
            # empty when everything is persisted, so a flush clearing ``_dirty``
            # mid-read cannot skip the reconciliation and drop the tail.
            # ``pending_retry`` carries a SUSPICION across the ``continue`` below.
            # The pending-rewrite save below clears ``_pending_rewrite``
            # unconditionally once the archive-safe rewrite succeeds
            # (``chat_persistence``: ``if rewrite:
            # slot._pending_rewrite = False``) with NO check that the flag it clears
            # is the one its own snapshot was taken for. So a rewind landing while
            # that save is suspended has its flag erased, and without this carry the
            # next attempt reads ``False`` and falls through to a disk read holding
            # the turns the rewind just discarded.
            pending_retry = False
            for _ in range(_SNAPSHOT_ATTEMPTS):
                if slot._pending_rewrite or pending_retry:
                    # Disk is KNOWN stale: a rewind/regenerate discarded a tail in
                    # memory and the truncating rewrite has not been written yet, so
                    # the file still holds the PRE-EDIT transcript. None of the four
                    # counters captured below carries that state -- ``chat_rewind``
                    # sets ``_dirty``, zeroes ``_resumed_count`` and sets this flag,
                    # but never touches ``_disk_window_len`` -- so the boundary keeps
                    # its pre-rewind value and can still satisfy the authoritative
                    # predicate. The read would then return the discarded turns and
                    # the post-await re-check would PASS, because nothing moved
                    # during the read: it measures stability, not correctness.
                    # ``session_transfer._guard_snapshot`` refuses on exactly this
                    # flag for exactly this reason.
                    #
                    # SAVE and retry rather than refuse outright: a rewrite save
                    # clears the flag (chat_persistence sets ``_pending_rewrite =
                    # False`` once the archive-safe rewrite succeeds), so this is
                    # the recoverable path, and a fork is required to still succeed.
                    # The 503 below is the terminal arm for a source that cannot be
                    # persisted, and the loop's own 503 covers a flag that keeps
                    # being re-set within the attempt budget.
                    # Capture the generation BEFORE the suspension point, so a
                    # re-dirty that lands while the save is awaited is witnessed.
                    gen_at_save = slot._dirty_gen
                    try:
                        # ``rewrite=True`` UNCONDITIONALLY, because every entry into
                        # this arm means "disk is stale because an EDIT truncated the
                        # window", and that is exactly what the archive-safe path is
                        # for. ``_save_slot_to_history`` only ever PROMOTES this flag
                        # (``if messages is not None or slot._pending_rewrite: rewrite
                        # = True``) and gates both the archive-diff (``if rewrite and
                        # path.exists()``) and the ``rotation_generation`` bump behind
                        # it -- so on the RETRY, where no snapshot is passed and
                        # ``_pending_rewrite`` has already been cleared by the first
                        # save, neither promotion input is present. Without this the
                        # retry would persist the rewind's truncation through the
                        # PLAIN path, deleting the discarded turns with no archive
                        # copy: strictly worse than the bug it recovers from, which
                        # at least left them readable on disk.
                        #
                        # Passing it on the first entry too is a no-op rather than a
                        # widening -- ``_pending_rewrite`` is still set there, so the
                        # promotion above already produces True
                        # (``test_a_first_entry_pending_rewrite_save_archives_as_
                        # before`` pins that). Stating it here removes the dependence
                        # on that promotion, which is the thing that silently failed.
                        saved = await save_slot_off_loop(
                            state, slot, rewrite=True, best_effort=False
                        )
                    except Exception:
                        logger.warning(
                            "chat_fork: could not persist the pending rewrite for "
                            "slot=%s; refusing the fork rather than copying the "
                            "discarded turns still on disk",
                            slot.key,
                            exc_info=True,
                        )
                        return web.json_response(
                            {
                                "error": "the source session is being written to; " "please retry",
                                "code": "fork_snapshot_unstable",
                            },
                            status=503,
                        )
                    if not saved:
                        # Delete-won: the source session was permanently deleted
                        # while this flush awaited the lock. Do not fork — the
                        # copy would republish the destroyed conversation under
                        # a fresh key (see the identical check at the plain
                        # flush site below).
                        logger.warning(
                            "chat_fork: source slot=%s was permanently deleted "
                            "during the pending-rewrite flush; aborting fork",
                            slot.key,
                        )
                        return web.json_response(
                            {
                                "error": "the source session was permanently deleted",
                                "code": "fork_source_deleted",
                            },
                            status=409,
                        )
                    # A moved generation means a genuine re-dirty landed across the
                    # await -- i.e. a rewind, whose ``_pending_rewrite`` this save
                    # has just erased along with its own. Two properties make the
                    # witness clean rather than a permanent trip: ``_dirty_gen``
                    # advances ONLY on a True assignment (see the ``_dirty`` setter
                    # in ``state.py``), so this save clearing ``_dirty`` cannot move
                    # it; and ``best_effort=False`` PROPAGATES a failure instead of
                    # re-marking the slot dirty, so the save cannot bump it either.
                    #
                    # RETRY rather than refuse, per the reasoning above: this is the
                    # recoverable path and a fork is required to still succeed. The
                    # next attempt re-enters this arm and persists the rewind that
                    # was missed. A flag that keeps being re-set simply spends the
                    # attempt budget and is caught by the loop's own terminal 503 --
                    # exactly the division of labour described above. Mirrors
                    # ``flush_slot_now``'s generation compare in ``state.py``, which
                    # distinguishes "the True I started this save under" from "a NEW
                    # True set during it" for this same reason.
                    pending_retry = slot._dirty_gen != gen_at_save
                    continue
                disk_len_before = slot._disk_window_len
                gen_before = slot._dirty_gen
                count_before = len(slot.messages)
                older_before = slot._disk_older_count
                # Witnessed for STABILITY ONLY -- see the guard below. Captured here
                # so the branch selector at ``elif slot._dirty`` and the post-await
                # check read the same value.
                dirty_before = slot._dirty
                # Snapshot the tail ON THE LOOP, before the await, so it pairs
                # with the boundary the read is about to observe.
                if disk_len_before <= count_before:
                    # The boundary is a usable index and authoritative: the slice
                    # is empty exactly when everything is persisted. Deliberately
                    # NO ``_dirty`` gate here -- a gate is what lets a flush
                    # clearing ``_dirty`` mid-read skip the merge and drop the tail.
                    tail = list(slot.messages[disk_len_before:])
                elif slot._dirty:
                    # The boundary can run AHEAD of the resident window, and is
                    # then unusable as an index. It has TWO causes and they need
                    # OPPOSITE remedies, so this branch must not pick one blind:
                    #
                    #   * a CAPPED RESTORE dropped leading messages from memory
                    #     without bumping ``_disk_older_count``. Disk legitimately
                    #     holds MORE than memory, and flushing would write the
                    #     smaller window over it. The frozen prefix is keyed on
                    #     ``_disk_older_count`` (chat_persistence
                    #     ``_load_frozen_prefix``), which the cap never moved, so
                    #     the prefix is EMPTY and the save truncates disk to the
                    #     window -- destroying every persisted message the cap
                    #     dropped. ``test_fork_preserves_full_history_when_dirty_
                    #     and_capped`` is the guard for exactly that.
                    #   * a mid-stream ``_flush_segment`` reassigned
                    #     ``slot.messages`` to drop a trailing chunk run. Disk and
                    #     the counters still agree, so flushing is safe and is what
                    #     re-syncs the boundary.
                    #
                    # ``_resumed_count`` is not a blanket substitute either: the
                    # save never advances it -- ``_save_slot_to_history`` only READS
                    # it, in its no-op skip -- so for a slot created in this gateway
                    # run it stays 0 and slicing from it appends the whole resident
                    # window onto the disk read, duplicating every persisted turn.
                    # It IS the right offset in the capped-restore case, where the
                    # restore sets it to the capped length, which is precisely "how
                    # many resident messages came from disk".
                    #
                    # So snapshot that candidate here and decide below, once the
                    # read has supplied the only authoritative discriminator: the
                    # true on-disk length.
                    tail = None
                    capped_tail = list(slot.messages[slot._resumed_count :])
                else:
                    tail = []
                all_messages = await asyncio.to_thread(
                    state.conversation_log.read_messages_chained, slot_history_key(slot)
                )
                if not (
                    slot._disk_window_len == disk_len_before
                    and slot._dirty_gen == gen_before
                    and len(slot.messages) == count_before
                    # ``_disk_older_count`` too: it is half of the window's identity
                    # (channel_slots: the window is
                    # ``messages[_disk_older_count:][:len(window)]``), and the
                    # discriminator below is computed from it, so a move here
                    # invalidates the pairing exactly as a boundary move does.
                    and slot._disk_older_count == older_before
                    # ``_pending_rewrite`` is checked on BOTH sides of the await, as
                    # session_transfer's guard documents. It was False when this
                    # attempt began (the branch above continues otherwise), so True
                    # here means a rewind landed DURING the threaded read -- which it
                    # can, because ``slot._fork_lock`` has exactly one acquirer in the
                    # tree and no rewind path takes it. Equality against the other
                    # counters cannot see this: a rewind moves none of them back.
                    and not slot._pending_rewrite
                    # ``_dirty`` as a STABILITY WITNESS, never as a merge gate. The
                    # other four cannot see a save that merely COMPLETES under the
                    # read: an in-place content edit (a variant switch) moves neither
                    # ``len(slot.messages)`` nor ``_disk_older_count``, the save
                    # re-assigns ``_disk_window_len`` to the value it already had
                    # because the window length did not change, and CLEARING
                    # ``_dirty`` cannot move ``_dirty_gen`` (the setter advances it
                    # only on a True assignment). So the threaded read can return
                    # pre-save bytes while the slot reports everything persisted, and
                    # the boundary-derived tail is empty -- leaving the fork to adopt
                    # the stale read verbatim and carry the SUPERSEDED content.
                    #
                    # A mismatch RETRIES; it does not skip the reconciliation. That
                    # distinction is the whole reason this is safe to add: the two
                    # comments above rejecting a ``_dirty`` gate are about the MERGE
                    # decision, which stays keyed on the boundary alone. The next
                    # attempt re-reads a disk that now holds the completed save.
                    and slot._dirty == dirty_before
                ):
                    logger.debug(
                        "chat_fork: slot=%s changed during the transcript read; retrying",
                        slot.key,
                    )
                    continue
                if tail is not None:
                    new_msgs = tail
                    break
                # Boundary ahead AND dirty. Discriminate on the invariant
                # channel_slots states for these counters: disk holds
                # ``_disk_older_count`` frozen rows followed by the window, so
                # ``older + window`` is everything the counters claim is on disk.
                # Disk holding MORE than that means rows exist which the counters
                # do not represent -- the capped-restore signature. channel_slots
                # ``_window_matches_disk`` tests the same arithmetic in the
                # opposite direction.
                if len(all_messages) > older_before + count_before:
                    # Do NOT flush: it would truncate those unrepresented rows.
                    # Merge from ``_resumed_count`` instead, which the restore set.
                    disk_holds_unrepresented = True
                    new_msgs = capped_tail or []
                    break
                # Counters agree with disk, so the window shrank mid-stream and the
                # flush is what re-syncs the boundary: the save assigns
                # ``_disk_window_len = len(window)`` and never READS the boundary,
                # so flushing while it is ahead cannot mislead the write. Then spend
                # the attempt; the next one re-derives from the authoritative branch
                # above. If it still does not settle the loop's 503 refuses, which
                # is what session_transfer's ``_guard_snapshot`` does here -- but
                # retrying first is what keeps a fork succeeding once the stream
                # that moved the window has finalized.
                try:
                    await save_slot_off_loop(state, slot, best_effort=False)
                except Exception:
                    logger.warning(
                        "chat_fork: could not persist slot=%s to re-sync the "
                        "persisted boundary; refusing the fork",
                        slot.key,
                        exc_info=True,
                    )
                    return web.json_response(
                        {
                            "error": "the source session is being written to; " "please retry",
                            "code": "fork_snapshot_unstable",
                        },
                        status=503,
                    )
                continue
            else:
                # Do NOT fall through with the mismatched pair: that is precisely
                # the state the loop exists to reject, and taking it either drops
                # the tail or duplicates it. Both are silent; a retryable 503 is
                # not, and this handler already uses that shape below.
                logger.warning(
                    "chat_fork: slot=%s did not settle in %d attempts; refusing the fork",
                    slot.key,
                    _SNAPSHOT_ATTEMPTS,
                )
                return web.json_response(
                    {
                        "error": "the source session is being written to; please retry",
                        "code": "fork_snapshot_unstable",
                    },
                    status=503,
                )
        _fork_tail_len = len(new_msgs) if (all_messages and new_msgs) else 0
        if all_messages and new_msgs:
            # REBIND, never ``extend``. ``read_messages_chained`` hands back the
            # SHARED ``_msg_cache`` list BY IDENTITY whenever it falls through to
            # ``_read_messages``: for a session with no ``tab_id``, for a tid whose
            # index resolves to no keys, and when every chained read comes back
            # empty. ``_read_messages``'s docstring makes the contract explicit --
            # callers MUST treat the result as immutable and slice or ``list(...)``
            # it before mutating.
            #
            # An in-place mutation is unsafe here because this handler can finish
            # with no durable save at all, and only a save invalidates the cache
            # entry: the skip-the-save branch below is deliberate, and both save
            # arms are gated on ``slot._dirty``, so neither runs when it is
            # already False -- while the tail snapshot above is ungated and can
            # still be non-empty. Nothing in this handler then corrects the entry,
            # so readers of this key see the UNPERSISTED tail as though it were
            # history until the next write to this transcript invalidates it, or
            # the LRU evicts it.
            #
            # A rebind rather than ``list(all_messages)`` + ``extend``: one
            # expression, the surrounding control flow untouched, and it leaves no
            # in-place mutation of this object anywhere in the handler for a future
            # edit to reintroduce. Nothing below depends on the identity -- the
            # remaining uses are a truthiness test, a rebind and a comprehension.
            all_messages = all_messages + new_msgs
        if slot._dirty and disk_holds_unrepresented:
            # Same rule as the boundary-ahead branch above, applied to the second
            # flush site in this handler: NEVER flush a slot whose disk holds rows
            # its counters do not represent. The save's frozen prefix is
            # ``body[:_disk_older_count]``, so rows outside that are not protected
            # and the write truncates them -- 250 -> 52 in the capped-restore case.
            #
            # Skipping it leaves ``_dirty`` SET deliberately, so the unwritten
            # source messages stay queued for the periodic flusher rather than
            # being stranded by a premature clean-mark. The fork itself does not
            # need the flush: it already holds the full transcript from disk plus
            # the merged tail.
            #
            # NOTE (pre-existing, not introduced here): the periodic flusher can
            # still truncate a slot left in this state, because the destructive
            # write lives in the save path. This only stops the FORK from causing
            # it; the durable fix is for ``_disk_older_count`` to cover the rows a
            # cap drops, which is out of scope for this handler.
            logger.warning(
                "chat_fork: slot=%s has %d frozen-prefix rows for a %d-message "
                "window but disk holds more; skipping the durable save so it "
                "cannot truncate the persisted history",
                slot.key,
                slot._disk_older_count,
                len(slot.messages),
            )
        elif slot._dirty:
            # Persist with best_effort=False so a lock timeout / I/O failure
            # PROPAGATES instead of being swallowed. The fork treats disk as the
            # source of truth (it re-reads the full history above) and clears
            # ``_dirty`` below — which also disables the periodic retry that
            # would otherwise re-flush the slot. Clearing ``_dirty`` after a
            # silently-dropped save would strand the unwritten source messages
            # and lose them permanently on the next gateway restart. Only mark
            # the slot clean once the durable write is CONFIRMED; on failure,
            # abort the fork (leaving ``_dirty`` set) rather than fork from a
            # partially-persisted source.
            try:
                saved = await save_slot_off_loop(state, slot, best_effort=False)
            except Exception:
                logger.warning(
                    "chat_fork: durable save of source slot=%s failed; "
                    "aborting fork to avoid losing unwritten messages",
                    slot.key,
                    exc_info=True,
                )
                return web.json_response(
                    {
                        "error": "could not persist source session before fork; " "please retry",
                        "code": "source_save_failed",
                    },
                    status=503,
                )
            if not saved:
                # The delete-won guard skipped the write: the source session was
                # permanently deleted while the flush awaited the lock. Forking
                # now would republish the destroyed conversation under a fresh
                # key (a brand-new slot carries no delete evidence, so ITS save
                # would proceed) — the exact resurrection the guard exists to
                # prevent, laundered through a copy. Abort instead; the delete's
                # reported success stands.
                logger.warning(
                    "chat_fork: source slot=%s was permanently deleted during "
                    "the pre-fork flush; aborting fork",
                    slot.key,
                )
                return web.json_response(
                    {
                        "error": "the source session was permanently deleted",
                        "code": "fork_source_deleted",
                    },
                    status=409,
                )
            slot._resumed_count = len(slot.messages)
            slot._dirty = False
        if not all_messages:
            # The whole corpus is the in-memory window: disk contributed
            # nothing, so for the mid-rotation rebuild below it is ALL tail.
            all_messages = list(slot.messages)
            _fork_tail_len = len(all_messages)
        # Direct delete check, independent of the flush arms above: if the
        # periodic 5s flush hit the delete-won guard first, it cleared
        # ``_dirty`` and this handler's own flush arms never ran — the disk
        # read came back empty and ``all_messages`` just fell back to the
        # in-memory window of a permanently deleted conversation. The two
        # ``saved``-checks above only cover a delete observed by THIS
        # handler's flush; this probe answers regardless of who consumed the
        # signal.
        if session_was_deleted(state, slot):
            logger.warning(
                "chat_fork: source slot=%s belongs to a permanently deleted "
                "session; refusing to fork it",
                slot.key,
            )
            return web.json_response(
                {
                    "error": "the source session was permanently deleted",
                    "code": "fork_source_deleted",
                },
                status=409,
            )
    # Fork indices arrive in the PAGINATED corpus's visible-row space, which
    # prepends each chain key's size-rotated archive head
    # (read_messages_chained_full). Mirror that corpus here, or every index
    # sent after the reader paged past a rotation boundary resolves short by
    # the archived visible-row count, silently forking the WRONG message —
    # and archived rows could not be forked at all. Rotated rows are BY
    # DEFINITION no longer in the live files, so this cannot duplicate
    # anything the flush arms above already placed in ``all_messages``.
    #
    # Two shapes, matching the slot-detail handler exactly:
    # - Archive only on the FIRST chain member: the archived rows are a
    #   contiguous prefix of the chained corpus, so a flat prepend is exact.
    # - A LATER member also rotated (``chain_mid_rotation``): the paginated
    #   corpus interleaves rot/live per key, so a flat prepend would shift
    #   ``at_message_index`` by the sandwiched rows. Rebuild the disk part
    #   from the true chained corpus and re-append the unflushed tail the
    #   arms above collected (``_fork_tail_len`` rows).
    if state.conversation_log:
        try:
            _rotated_head = await asyncio.to_thread(
                state.conversation_log.read_rotated_messages_chained,
                slot_history_key(slot),
            )
        except Exception:
            # NOT `_rotated_head = []`. Empty means "no archive" here, so folding
            # the failure into it drops the archived head and shifts every index
            # below it -- and this path forks BY INDEX, so it would copy a
            # different cutoff than the one the reader pointed at, silently. Same
            # retryable shape the snapshot loop below already returns.
            logger.warning("rotated-archive read failed for fork", exc_info=True)
            return history_corpus_unreadable("fork_corpus_unreadable")
        if _rotated_head:
            _mid_rotation = False
            try:
                _mid_rotation = await asyncio.to_thread(
                    state.conversation_log.chain_mid_rotation,
                    slot_history_key(slot),
                )
            except Exception:
                # Same reasoning as the slot-detail probe: a False fallback picks
                # the flat-prepend path, which is only correct when the rotation is
                # on the first chain member. Getting that wrong here forks BY INDEX
                # off a misindexed corpus, so it copies different messages than the
                # ones the reader pointed at.
                logger.warning("mid-rotation probe failed for fork", exc_info=True)
                return history_corpus_unreadable("fork_corpus_unreadable")
            _rebuilt = False
            if _mid_rotation:
                try:
                    _full_disk = await asyncio.to_thread(
                        state.conversation_log.read_messages_chained_full,
                        slot_history_key(slot),
                    )
                    _tail = (
                        all_messages[len(all_messages) - _fork_tail_len :] if _fork_tail_len else []
                    )
                    # `_tail` was derived as "unflushed" against the corpus the
                    # snapshot loop read. THIS is a later read, and two
                    # `to_thread` suspensions separate them, so a save landing in
                    # that window puts those same rows on disk — appending the
                    # tail blind then duplicates them, surfacing as an ambiguous
                    # fork id or a doubled fork tail. The loop's stability
                    # guarantee does not reach across this read, so re-derive
                    # against what this read actually returned.
                    all_messages = _full_disk + drop_persisted_tail_prefix(_full_disk, _tail)
                    _rebuilt = True
                except Exception:
                    # FAIL CLOSED. The flat prepend below puts only THIS key's
                    # rotated head in front, so when the rotation is on a later
                    # chain member the earlier members' rotated rows are still
                    # missing and every index shifts. An index-addressed fork
                    # then copies DIFFERENT messages than the ones the reader
                    # pointed at, silently — worse than not forking at all,
                    # which the reader can see and retry. Same retryable shape
                    # the snapshot loop above already returns.
                    logger.warning("chained-full fork corpus read failed", exc_info=True)
                    return history_corpus_unreadable("fork_corpus_unreadable")
            if not _rebuilt:
                # Same crash window as `read_messages_chained_full`'s concatenation,
                # reached by a different branch: rotation archives the dropped lines
                # first and rewrites the live file's head second, so a kill between
                # those two steps leaves the archived rows in BOTH files. Prepending
                # blind then serves them twice, and in this corpus a duplicated row
                # is what makes a legacy fork index select the wrong cutoff.
                #
                # This branch is not covered by that function's own guard because it
                # runs when the chained-full read was not used at all.
                all_messages = _rotated_head + drop_persisted_tail_prefix(
                    _rotated_head, all_messages
                )
    # ``visible`` is the fork-point INDEX SPACE (at_message_index, the id lookup,
    # head_count). Markers are copied without entering it, via these positions.
    # Derived AFTER the archive prepend above, so the positions index the rebuilt
    # corpus rather than the pre-rotation one.
    visible_positions = [
        i for i, m in enumerate(all_messages) if m.get("role") in ("user", "assistant")
    ]
    visible = [all_messages[i] for i in visible_positions]
    if not visible:
        return web.json_response(
            {"error": "no messages to fork", "code": "no_messages_to_fork"},
            status=400,
        )
    if at_message_id is not None:
        matches = []
        for index, message in enumerate(visible):
            meta = message.get("meta")
            mid = meta.get("mid") if isinstance(meta, dict) else None
            if mid == at_message_id:
                matches.append(index)
        if not matches:
            return web.json_response(
                {
                    "error": "the selected message is no longer present in this session",
                    "code": "fork_message_not_found",
                },
                status=409,
            )
        if len(matches) > 1:
            # ``meta`` can originate with a caller. Never guess when a malformed
            # transcript reuses an id: choosing either occurrence silently forks
            # from a different point than the user selected.
            return web.json_response(
                {
                    "error": "the selected message id is ambiguous in this session",
                    "code": "fork_message_ambiguous",
                },
                status=409,
            )
        at_index = matches[0]
    elif at_index is not None:
        if isinstance(at_index, bool) or not isinstance(at_index, int) or at_index < 0:
            return web.json_response(
                {
                    "error": "at_message_index must be a non-negative integer",
                    "code": "invalid_field_type",
                },
                status=400,
            )
        if at_index >= len(visible):
            return web.json_response(
                {
                    "error": f"at_message_index {at_index} out of range (have {len(visible)} visible messages)",
                    "code": "value_out_of_range",
                },
                status=400,
            )

    head_messages: list[dict] = []
    # Copy span in ``all_messages`` coordinates, so a marker between two visible
    # turns travels with them while ``visible`` keeps its own indices.
    copy_lo = 0
    copy_hi = len(all_messages)
    if direction == _FORK_DIRECTION_TAIL:
        if at_index is None:
            return web.json_response(
                {
                    "error": "at_message_index is required for a tail fork",
                    "code": "at_message_index_required",
                },
                status=400,
            )
        head_messages = visible[: at_index + 1]
        copy_lo = _slice_boundary(visible_positions, at_index, len(all_messages))
        visible = visible[at_index + 1 :]
        if not visible:
            return web.json_response(
                {
                    "error": "no messages after the fork point",
                    "code": "no_messages_after_fork_point",
                },
                status=400,
            )
    elif at_index is not None:
        copy_hi = _slice_boundary(visible_positions, at_index, len(all_messages))
        visible = visible[: at_index + 1]

    copied_messages = [m for m in all_messages[copy_lo:copy_hi] if m.get("role") in _FORKED_ROLES]
    # The destination's append trims from the FRONT at capacity, and nothing has
    # reached disk during the copy, so those evicted rows are unrecoverable.
    if len(copied_messages) > _MAX_SLOT_MESSAGES:
        return web.json_response(
            {
                "error": (
                    f"conversation too large to fork: {len(copied_messages)} rows exceed the "
                    f"{_MAX_SLOT_MESSAGES}-row session capacity. Fork at a message "
                    f"(at_message_index or at_message_id) to copy a smaller slice."
                ),
                "code": "fork_corpus_too_large",
            },
            status=400,
        )

    # A fork of a member DM thread is an ordinary chat, never a second "member"
    # slot: the fork mints a chat-* key, so member mode would make it invisible
    # everywhere (excluded from Sessions by surface mode, and absent from the
    # roster, whose threads live only on member-<slug> keys). The override
    # allowlist deliberately cannot name "member".
    if mode_override is not None:
        fork_mode = mode_override
    elif slot.mode == "member":
        fork_mode = ""
    else:
        fork_mode = slot.mode

    new_slot = state.get_or_create_slot(
        name=None,
        agent=slot.agent,
        workspace=slot.workspace,
        model=slot.model,
        mode=fork_mode,
        app=request_app,
        origin=request_slot_origin(request_app),
        # Human request-layer path: a person forking a conversation. The
        # origin conjunct in state.py still excludes app-token callers.
        count_user_session=True,
    )
    new_slot.forked_from = effective_session_key(slot)
    new_slot.reasoning_effort = slot.reasoning_effort
    # Inherit the active project directory so the fork keeps the parent's working
    # context (agent resolution, steering files, CWD) instead of falling back to
    # the config/workspace default on first message.
    new_slot.project = slot.project
    # Inherit the sidebar folder so the fork appears next to its parent in the UI.
    new_slot.folder_id = slot.folder_id
    # Inherit tags (copied, so later edits to either slot's list stay independent).
    new_slot.tags = list(slot.tags)
    parent_title = slot.title if slot._titled else "Untitled"
    parent_title, _ = redact_exfiltration_urls(parent_title)
    parent_title, _ = redact_credentials(parent_title)
    # Strip a leading marker from the parent so it never compounds on a
    # fork-of-a-fork.
    parent_title = parent_title.removeprefix(_FORK_TITLE_MARKER)
    fork_word = "Tail of" if direction == _FORK_DIRECTION_TAIL else "Fork of"
    new_slot.title = f"{_FORK_TITLE_MARKER}{fork_word} {parent_title}"
    new_slot._titled = True

    try:
        for m in copied_messages:
            role = m.get("role", "assistant")
            content = m.get("content", "")
            if role != "user":
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
            if role == SECTION_MARKER_ROLE:
                cls = ""
            else:
                cls = "msg msg-u" if role == "user" else "msg msg-a"
            new_slot.append(
                role, content, cls, ts=m.get("ts", ""), meta=m.get("meta"), broadcast=False
            )
            # A fork copies the parent's messages into a new session. Origin is
            # a property of the message, not of the file, so a copied inbound
            # channel turn keeps the origin it actually had.
            carry_provenance(new_slot.messages[-1], m)
        new_slot.drain()
        await save_slot_off_loop(state, new_slot)
        new_slot._resumed_count = len(new_slot.messages)
    except Exception:
        state._slots.pop(new_slot.key, None)
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_fork",
            outcome="error",
            source="dashboard",
            resources=f"from={slot.key},to={new_slot.key}",
            error="fork finalisation failed",
        )
        raise
    # Acknowledgment boundary. The destination save above is the last await
    # before this fork is answered, and it takes the DESTINATION's history lock
    # -- nothing in it serialises against the SOURCE's permanent delete. So a
    # delete that began after the pre-copy probe passed can commit inside that
    # await, and the copy now on disk is a conversation the user has since
    # destroyed. ``build_transfer_bundle_async`` already closes its equivalent
    # window by re-probing after bundle assembly and before the peer send is
    # acknowledged; this is the same rule at the same boundary, for the copy
    # this handler makes.
    #
    # A delete that commits AFTER this point is deliberately out of scope: a
    # fork acknowledged while its source was alive is its own session and
    # survives the source, the way a repo fork outlives what it came from.
    # Acknowledgment is the only boundary a handler owns, so it is the line
    # drawn here.
    if session_was_deleted(state, slot):
        # Roll the destination back. It has been neither acknowledged nor
        # broadcast (``push_slots_update`` is below, and every ``append`` above
        # passed ``broadcast=False``), so nothing outside this handler has seen
        # it: removing it leaves no trace of the deleted conversation instead of
        # a fresh key holding a full copy. Removing the destination cannot harm
        # the source -- different key, different lock -- so a false positive
        # here (``session_was_deleted`` fails CLOSED when a stat or metadata
        # read is unverifiable) costs a retryable 409 against a still-live
        # source, not data.
        removed = False
        if state.conversation_log is not None:
            try:
                removed = bool(
                    await asyncio.to_thread(
                        state.conversation_log.delete_session,
                        slot_history_key(new_slot),
                    )
                )
            except Exception:
                removed = False
                logger.warning(
                    "chat_fork: removing the unacknowledged fork transcript for %s raised",
                    new_slot.key,
                    exc_info=True,
                )
        state._slots.pop(new_slot.key, None)
        if removed:
            logger.warning(
                "chat_fork: source slot=%s was permanently deleted during the "
                "destination save; removed the unacknowledged fork %s",
                slot.key,
                new_slot.key,
            )
        else:
            # The copy is still on disk and WILL be listed in Older Sessions.
            # Loud, because this is the resurrection the guard exists to
            # prevent and it now needs a human -- but still a 409: reporting
            # success would additionally hide it.
            logger.error(
                "chat_fork: source slot=%s was permanently deleted during the "
                "destination save, but the fork transcript for %s could not be "
                "removed; a copy of the deleted conversation remains on disk",
                slot.key,
                new_slot.key,
            )
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_fork",
            outcome="denied",
            source="dashboard",
            resources=(
                f"from={slot.key},to={new_slot.key},"
                f"rollback={'removed' if removed else 'failed'}"
            ),
            error="source session permanently deleted during the destination save",
        )
        return web.json_response(
            {
                "error": "the source session was permanently deleted",
                "code": "fork_source_deleted",
            },
            status=409,
        )
    sel().log_api_access(
        caller=request_app or "dashboard",
        operation="chat.slot_fork",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"from={slot.key},to={new_slot.key},messages={len(visible)},"
            f"at_index={at_index if at_index is not None else 'last'},"
            f"direction={direction},"
            f"head_count={len(head_messages)},"
            f"prompt_len={len(prompt)},mode={new_slot.mode}"
        ),
    )
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "key": new_slot.key,
            "title": new_slot.title,
            "messages": len(visible),
            "prompt": prompt,
            "folder_id": new_slot.folder_id or None,
            "direction": direction,
        }
    )
