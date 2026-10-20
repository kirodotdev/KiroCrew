"""The slot builders the restores and a targeted rehydrate share.

The prefetch reads, the open-tab slot build, the deferred window load of a tab
built as a row (``_PendingWindow``), ``_rehydrate_slot_from_history`` and its async
twin, and the recent-session slot build. Composed onto
:mod:`kiro_crew.dashboard.chat_persistence`; see this package's ``__init__``. New
slot construction steps go here.
"""

from __future__ import annotations

import asyncio
import time
from itertools import islice
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_persistence import (
        _SKIP_MEMBER_RESTORE,
        ConversationLog,
        DashboardState,
        KiroCrewConfig,
        _attach_variants,
        _build_kiro_model_map,
        _ChatSlot,
        _deletion_during_read,
        _has_validated_effort_marker,
        _is_app_owned_channel_row,
        _load_restore_cfg,
        _member_restore_identity,
        _metadata_codec,
        _normalize_slot_key,
        _read_mcp_app_claims,
        _recent_session_slot_name,
        _reconcile_mcp_app_claims,
        _restored_agent_name,
        carry_provenance,
        durable_row_count,
        effective_session_key,
        logger,
        redact_display_content,
        remember_unknown_row_fields,
        session_key_for,
        slot_closed_since,
        slot_transcript_key,
        update_metadata_off_loop,
        with_bounded_redaction_records,
    )


#: Sentinel default for the ``member_identity`` parameters below: the caller
#: did not prefetch, resolve inline. Distinct from ``None`` (an ordinary,
#: non-member key) — defaulting to ``None`` would silently unpin every member
#: slot restored by a caller that forgot to prefetch.
_IDENTITY_UNRESOLVED: tuple[str, str] = ("", "__unresolved__")


def _prefetch_rehydrate_inputs(
    conv_log: ConversationLog,
    history_key: str,
    *,
    adopt_closed: bool = False,
    kiro_model_map: dict[str, str] | None = None,
    with_status: bool = False,
) -> tuple[
    dict, bool, list[dict] | None, dict[str, str] | None, tuple[str, str] | None, str | None, bool
]:
    """Read everything :func:`_rehydrate_slot_from_history` needs, off the loop.

    The one prefetch seam shared by every async restore path — the metadata line,
    the chained message walk and (when the caller has no shared copy) the
    agent→model map. All three are blocking disk work; none of them touches slot
    state, so the whole function is safe to hand to ``asyncio.to_thread`` while
    the loop-affine slot mutation stays on the event loop. See
    :func:`rehydrate_slot_from_history_async` for why that split is mandatory
    rather than merely nice.

    *with_status* selects ``get_metadata_status`` over ``get_metadata`` so the
    open-tab restore keeps the readability signal it needs: ``get_metadata``
    reports ``{}`` for both "never persisted" and "could not be read after
    retries", and treating the second as the first is what silently discards a
    live tab.

    Returns ``(meta, readable, messages, model_map, member_identity, agent,
    effort_marker)``. The marker check is filesystem work too, so it belongs
    in this prefetch rather than the loop-affine apply phase.
    *messages* and *model_map* are ``None`` when there is nothing to build — no
    metadata, an unreadable read, or a session closed with ✕ that the caller did
    not opt to adopt — so a caller can decide without a second disk round trip.
    *member_identity* is the prefetched ``_member_restore_identity`` answer
    (dm.json is file IO too, and the apply half is loop-affine); it is resolved
    only when there is something to build.
    """
    if with_status:
        meta, readable = conv_log.get_metadata_status(history_key)
    else:
        meta, readable = conv_log.get_metadata(history_key), True
    if not readable or not meta or (meta.get("closed") and not adopt_closed):
        return meta or {}, readable, None, None, None, None, False
    return (
        meta,
        readable,
        conv_log.read_messages_chained(history_key),
        kiro_model_map if kiro_model_map is not None else _build_kiro_model_map(),
        # The transcript key is "dashboard:" + slot name; identity is a
        # property of the slot name.
        _member_restore_identity(history_key.removeprefix("dashboard:")),
        _restored_agent_name(str(meta.get("linked_session_key") or history_key), meta),
        _has_validated_effort_marker(meta.get("reasoning_effort")),
    )


def _apply_restored_open_slot(
    state: DashboardState,
    key: str,
    *,
    meta: dict,
    readable: bool,
    messages: list[dict] | None,
    model_map: dict[str, str] | None,
    unrestored: set[str],
    member_identity: tuple[str, str] | None = _IDENTITY_UNRESOLVED,
    agent: str | None = None,
    effort_marker: bool = False,
    conv_log: ConversationLog | None = None,
    started: float | None = None,
    preserve_remote_only: bool = False,
    tail: list[dict] | None = None,
    dropped: set[str] | None = None,
    pending_backfills: list[tuple[str, dict]] | None = None,
) -> int:
    """Turn one prefetched open-tab read into a slot; return 1 if it restored.

    *tail* (with *messages* ``None``) builds the tab as a sidebar row whose
    window loads on first read; see :func:`_prefetch_open_slot_inputs`.

    LOOP-AFFINE — slot construction broadcasts through
    ``asyncio.Queue.put_nowait`` / ``Event.set``, so this half must run on the
    event loop even when the read that fed it did not. Shared by both drivers so
    the "unreadable metadata keeps its reopen seed" rule has one definition.

    *pending_backfills*, when supplied by the async driver, is threaded into
    :func:`_rehydrate_slot_from_history` so the tab-id backfill this slot mints
    is collected and DRAINED (its ``os.replace`` committed) before the driver
    returns, instead of firing off the loop here — see that function's note.

    *conv_log* and *started* together opt into the POST-HOP re-checks and are
    passed only by the async driver, whose read happened in a worker thread. The
    synchronous generator reads inline with no suspension point, so its pre-read
    answers cannot have gone stale and it would only pay for redundant work.

    *preserve_remote_only* carries :func:`_transcripts_may_be_remote_only`: when
    the authority pair was restored from a remote snapshot without the
    transcripts, a listed slot with no local transcript is a remote-only reopen
    seed to keep, not a dead tab to prune.

    *dropped* collects the keys a reader should be TOLD about, which is a narrower
    set than *unrestored*. Both keep the key in the reopen seed, but they answer
    different questions: ``unrestored`` is "do not prune this", while ``dropped`` is
    "this tab should be here and is not". A remote-only seed is in the first and not
    the second -- its transcript arrives lazily by design, and it stays in the seed
    across every boot, so counting it would raise the same warning on every restart
    for a system working as intended. A warning that repeats forever is one the user
    learns to ignore, which costs the real loss its only visible signal.
    """
    if not readable:
        unrestored.add(key)
        if dropped is not None:
            dropped.add(key)
        logger.warning(
            "restore_open_slots: metadata unreadable for %s; keeping it "
            "in the reopen seed for the next restore instead of dropping it",
            key,
        )
        return 0
    if messages is None and tail is None:
        if preserve_remote_only and not meta.get("closed"):
            # The authority pair was restored from a remote snapshot without the
            # transcripts (they arrive lazily, per turn), so an absent LOCAL
            # transcript does not mean the slot is gone — its transcript lives in
            # a remote this task has not fetched yet. Keep the key as a reopen
            # seed. Dropping it lets the next 5s flush write an empty
            # open_slots.json that the sidecar commits, silently losing the
            # restored slot table on an ordinary task replacement. A tab the user
            # closed with ✕ still carries ``meta.closed`` and is NOT kept.
            unrestored.add(key)
            logger.info(
                "restore_open_slots: %s has no local transcript yet after a "
                "remote authority restore; keeping it in the reopen seed until "
                "lazy retrieval completes",
                key,
            )
            return 0
        # No metadata (never persisted) or the user closed the tab with ✕. Both
        # are confident answers, so the key is NOT carried as unrestored — the
        # synchronous helper's own guards reached the same verdict before.
        return 0
    if started is not None and slot_closed_since(state, key, started):
        # TAB-CLOSE RACE, same window ``rehydrate_slot_from_history_async`` and
        # the recent-sessions driver already guard. The user can click ✕ while
        # the transcript is in flight: the close pops the slot and records the
        # tombstone synchronously on the loop, but persists the ``closed`` flag
        # only after its own awaits — so the metadata read above still says open.
        # Rebuilding from it re-creates a tab the user dismissed, and the restored
        # slot's next flush writes metadata WITHOUT ``closed``, erasing the close
        # itself. The tombstone is the authoritative signal in this window.
        logger.info(
            "restore_open_slots: session %s was closed while its transcript "
            "loaded; not restoring a tab the user dismissed",
            key,
        )
        # A confident answer, like closed-on-disk — do NOT carry the key.
        return 0
    if conv_log is not None:
        # Synchronous and immediately before the build: no await may separate the
        # two (see _deletion_during_read).
        gone = _deletion_during_read(
            conv_log, slot_transcript_key(key), meta, messages if tail is None else tail
        )
        if gone is not None:
            logger.info(
                "restore_open_slots: session %s was %s while its transcript "
                "loaded; refusing to restore a tab whose flush would rewrite it",
                key,
                gone,
            )
            # A confident answer, like closed/absent — do NOT carry the key.
            return 0
    slot = _rehydrate_slot_from_history(
        state,
        key,
        kiro_model_map=model_map,
        _prefetched_meta=meta,
        _prefetched_messages=messages,
        _prefetched_member_identity=member_identity,
        _prefetched_agent=agent,
        _prefetched_effort_marker=effort_marker,
        _lazy_tail=tail if messages is None else None,
        pending_backfills=pending_backfills,
    )
    return 1 if slot is not None else 0


class _PendingWindow:
    """The deferred window load of a tab the open-tab restore built as a row.

    Installed as ``slot._lazy_window`` and run once, by the slot's first read of
    ``messages`` (or by ``state.load_window_off_loop``, which does :meth:`read`
    on a worker thread first). Holds the ``metadata_codec.apply`` result so the load
    settles the turn marker, held notes and title mark exactly as an eager
    restore does, only later.
    """

    __slots__ = ("_applied", "_history_key", "_state")

    def __init__(self, state: DashboardState, history_key: str, applied: Any) -> None:
        self._state = state
        self._history_key = history_key
        self._applied = applied

    def read(self, slot: _ChatSlot) -> tuple[list[dict], list[set[str]]]:
        """Every disk read the load needs. BLOCKING; touches no slot state."""
        conv_log = self._state.conversation_log
        messages = conv_log.read_messages_chained(self._history_key) if conv_log else []
        return messages, _read_mcp_app_claims(effective_session_key(slot))

    def __call__(
        self,
        slot: _ChatSlot,
        messages: list[dict] | None = None,
        claims: list[set[str]] | None = None,
    ) -> None:
        if messages is None or claims is None:
            messages, claims = self.read(slot)
        # A pending row can still owe a metadata edit (a rename, a pin) that the
        # window replay's clean mark would otherwise forget.
        was_dirty = slot._dirty
        _load_rehydrated_window(self._state, slot, messages, self._applied)
        if was_dirty:
            slot._dirty = True
        _reconcile_mcp_app_claims(slot, claims)


def _load_rehydrated_window(
    state: DashboardState, slot: _ChatSlot, messages: list[dict], applied: Any
) -> None:
    """Replay *messages* into *slot* as its window and settle what needs it.

    The window half of :func:`_rehydrate_slot_from_history`, shared with the
    deferred load of a sidebar row (:class:`_PendingWindow`). LOOP-AFFINE like
    the rest of slot construction. *applied* is the ``metadata_codec.apply``
    result for this slot, whose ``settle`` needs the loaded rows.
    """
    # Only the recent window is loaded into memory; older on-disk lines become
    # the FROZEN PREFIX that saves never rewrite. _disk_older_count must
    # therefore count those older lines so the save model preserves them.
    older_cut = max(0, len(messages) - 500)
    slot._disk_older_count = older_cut
    # Recomputed from the on-disk rows on every load (never trusted from any
    # stored value): the durable-only view of the same prefix, which is what
    # absolute message positions are built over. See ``init_transcript_fields`` in
    # dashboard/state_parts/slot_transcript.py.
    # ``islice``, not ``messages[:older_cut]`` — this can run on the event
    # loop for a large transcript, and a slice would copy the whole prefix.
    slot._disk_older_durable_count = durable_row_count(islice(messages, older_cut))
    for m in messages[-500:]:
        role = m.get("role", "assistant")
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        # Neither content nor meta is redacted here. Redaction happens where the
        # data is EMITTED (chat_utils._prepare_messages for the slot detail
        # endpoint, _ChatSlot.to_dict for the sidebar payload,
        # _build_history_prefix for the ACP prompt) — every path a client or model
        # can observe.
        #
        # CONTENT, however, is redacted right here, on load. That split is
        # deliberate and measured, and it is the crux of this change:
        #
        #   field    | read sites | share of the ~7s load cost
        #   ---------|------------|---------------------------
        #   content  |    ~204    | ~0.4s  (6%)
        #   meta     |     31     | ~5.5s  (79%)
        #
        # `meta.tool_input` carries the large tool payloads, so meta is where the
        # boot cost actually lives — and its 31 readers are tractable: outside the
        # emit sites (which redact and are covered by
        # test_display_time_redaction.py) every one reads only CONTROL fields
        # (`done`, `tool_call_id`), never payload text. Deferring meta to display
        # time is therefore both where the win is and safely enumerable.
        #
        # `content` is the opposite on both axes: it is cheap (0.4s) and it has
        # ~204 readers across the dashboard, so "every reader must remember to
        # redact" is not an invariant anyone can hold. Three separate egress paths
        # (the side-chat prompt, the orchestrator stage-result file, and the
        # title-model prompt) can each leak restored content if a reader forgets.
        # Paying 0.4s here restores the single chokepoint — any present or FUTURE
        # reader of `m["content"]` gets clean bytes — instead of relying on an
        # enumeration of every reader.
        # `role != "user"`, never `not in ("user", "system")`: user-authored text
        # stays raw because its author is its only reader, but `system` MUST be
        # redacted — the write path excludes it, so system bytes reach disk raw.
        if role != "user":
            content = redact_display_content(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            # broadcast=False: replaying history must not emit N `chat_message`
            # events. _broadcast_chat_message redacts non-user content (parity
            # with _prepare_messages) but deliberately not meta, and this
            # helper also runs for on-demand cold-slot rehydrates while clients
            # ARE connected, so broadcasting here would push unredacted meta
            # straight to them. Clients get the transcript from the slot detail
            # endpoint (redacted) and the sidebar from the coalesced slots push.
            broadcast=False,
            # meta is NOT redacted here — same reasoning as content, and
            # it is where the cost actually was: tool `meta.tool_input` carries
            # the large payloads, so meta redaction was ~5.5s of a ~7s restore
            # while content redaction was only ~0.4s. Redacted at emit instead
            # (chat_utils._prepare_messages), which is the only path that returns
            # meta to a client. Blocked-link records are the exception: they are
            # BOUNDED here because this is where the slot retains them, and the
            # bound is a no-op for a row that carries none.
            meta=(
                with_bounded_redaction_records(m["meta"])
                if isinstance(m.get("meta"), dict)
                else None
            ),
            mint_mid=False,
        )
        # Provenance is not a slot.append() argument, so carry it onto the
        # message the append just created. Without this the window loses where
        # each turn came from and the next flush restamps it "dashboard".
        carry_provenance(slot.messages[-1], m)
        remember_unknown_row_fields(slot, m)
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # The whole in-memory window is already on disk → it is the on-disk window
    # region. Saves re-serialize the window in place; the frozen prefix (older
    # turns counted above) is never rewritten.
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    # A local turn admitted by the previous process and never torn down, the
    # held notes the window already delivered, and the title refresh mark
    # re-based against the rows the window holds -- the fields whose read
    # needs the window.
    applied.settle(messages)


def _rehydrate_slot_from_history(
    state: DashboardState,
    slot_name: str,
    *,
    kiro_model_map: dict[str, str] | None = None,
    adopt_closed: bool = False,
    _prefetched_meta: dict | None = None,
    _prefetched_messages: list[dict] | None = None,
    _prefetched_member_identity: tuple[str, str] | None = _IDENTITY_UNRESOLVED,
    _prefetched_agent: str | None = None,
    _prefetched_effort_marker: bool = False,
    _lazy_tail: list[dict] | None = None,
    pending_backfills: list[tuple[str, dict]] | None = None,
) -> _ChatSlot | None:
    """Rehydrate a single dashboard slot from persisted history.

    *_lazy_tail* builds the slot as a sidebar row instead: every metadata field
    is applied as usual, but the transcript window is left to load on the
    slot's first read of ``messages`` (:class:`_PendingWindow`), and the tail
    rows the caller already read stand in for the sidebar summary until then.

    *kiro_model_map* lets a bulk caller build the agent→model map once and share
    it across every slot instead of paying a fresh directory glob + JSON parse
    per tab; omit it and one is built on demand for single-slot callers.

    *pending_backfills* collects the ``(key, fields)`` of any tab-id metadata
    backfill this slot mints, instead of firing it off the loop here. The async
    open-slot driver passes a list and DRAINS it (its ``os.replace`` committed)
    before it returns, so a caller's immediate on-loop transcript read right
    after restore cannot land inside that replace window and trip a Windows
    sharing violation. When ``None`` (every synchronous caller) the backfill
    fires off the loop as before. Mirrors the drain :func:`_apply_recent_session`
    performs for the recent-session driver.

    Unlike ``state.get_or_create_slot`` (which creates a fresh, empty slot with
    default ``memory_mode='persistent'``), this helper reads the session's
    metadata and messages from ``conversation_log`` so the restored slot has
    the original title/agent/model/memory_mode and its message history
    populated. Returns ``None`` if the session does not exist on disk (so
    callers can fall through to other delivery paths without creating a
    phantom empty tab).

    Intended for targeted resume paths (e.g. cron→origin injection after
    gateway restart). Bulk startup restore still uses ``restore_recent_sessions``.
    """
    if not state.conversation_log:
        return None
    # Canonicalize to the filename-charset key (idempotent) so callers holding
    # a stale raw display-style key (e.g. a cron's caller_session recorded
    # before slot-key normalization) resolve to the same slot the restore
    # paths create — get_or_create_slot() below applies the same fold.
    slot_name = _normalize_slot_key(slot_name)
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = slot_transcript_key(slot_name)
    # ``_prefetched_*`` let an async caller hoist the two disk reads (this
    # metadata line and the chained message walk further down) into a worker
    # thread and then run the REST of this function on the event loop — see
    # ``rehydrate_slot_from_history_async``. Slot construction below must stay
    # loop-affine: it broadcasts through ``asyncio.Queue.put_nowait`` and
    # ``Event.set``, neither of which is thread-safe. Omit them and the reads
    # happen inline, which is what the synchronous callers want.
    meta = (
        _prefetched_meta
        if _prefetched_meta is not None
        else (state.conversation_log.get_metadata(history_key))
    )
    # No metadata → session was never persisted. Don't create a phantom slot.
    if not meta:
        return None
    if _is_app_owned_channel_row(meta, history_key):
        return None
    # ``adopt_closed`` restores a session that was archived with ``closed``.
    # Off by default so a session the user closed stays closed; app-owned worker
    # slots pass it, because their lifecycle belongs to the app (their own delete
    # path ends them) and idle-slot cleanup marks them closed without the user
    # ever asking for that.
    if meta.get("closed") and not adopt_closed:
        return None
    _restore_cfg = _load_restore_cfg()
    # Same kiro-agent model map as restore_recent_sessions so legacy sessions
    # without a persisted `model` still resolve correctly. Reuse the caller's
    # when bulk-restoring — it is identical for every slot.
    if kiro_model_map is None:
        kiro_model_map = _build_kiro_model_map()
    # Captured BEFORE the slot is created so the rollback below can tell what
    # this call actually added from what was already there. Only the restricted
    # key needs the test: the early return above means the slot itself is always
    # this call's own creation.
    restricted_key = f"dashboard:{slot_name}"
    preexisting_restricted = restricted_key in state._restricted_keys
    # Member keys resolve their pin from dm.json BEFORE construction (the
    # constructor's member-* reservation refuses a bare member key); a member
    # key without a binding is skipped, not published — the member-thread
    # endpoint re-creates and re-binds it on the next open. Async callers
    # prefetch the binding read in their worker-thread step (dm.json is file
    # IO and this half is loop-affine); the inline resolve serves the
    # synchronous callers, whose reads already happen inline.
    _member_identity = (
        _member_restore_identity(slot_name)
        if _prefetched_member_identity is _IDENTITY_UNRESOLVED
        else _prefetched_member_identity
    )
    if _member_identity is _SKIP_MEMBER_RESTORE:
        return None
    purpose = _metadata_codec.Restore(
        name=slot_name,
        member=_member_identity,
        agent=_prefetched_agent,
        cfg=_restore_cfg,
        model_map=kiro_model_map,
        effort_marker=_prefetched_effort_marker,
    )
    try:
        slot = state.get_or_create_slot(slot_name, **_metadata_codec.slot_args(meta, purpose))
        # Every field the line carries is applied through the one field table
        # (``slot_persistence.metadata_codec``), from the metadata line already
        # read above. The title deliberately does not come from
        # ``list_sessions()``: that globs, stats and reads the first line of EVERY
        # session file, once per restored slot -- a boot with many open tabs then
        # blocks the event loop long enough to trip the LoopStallWatchdog.
        applied = _metadata_codec.apply(state, slot, meta, purpose)
        # Use read_messages_chained (not read_messages) so the loaded window walks
        # the tab_id ancestry across forks, matching restore_recent_sessions.
        # read_messages alone caps visible history at 200 lines from THIS file and
        # drops the ancestor chain — long-running forked sessions would lose 200+
        # messages of context on every gateway restart.
        # A sidebar row (``_lazy_tail``) reads no transcript here at all.
        messages: list[dict]
        if _prefetched_messages is not None:
            messages = _prefetched_messages
        elif _lazy_tail is not None:
            messages = []
        else:
            messages = state.conversation_log.read_messages_chained(history_key)
        if applied.minted_tab_id is not None:
            # Persist the freshly-minted tab_id AFTER reading the transcript above,
            # never before. update_metadata_off_loop dispatches an os.replace() of
            # THIS session file to a worker thread; scheduling it before the read
            # let that replace race the loop-thread transcript read of the very
            # same file. On Windows a concurrent replace makes the reader's open()
            # fail with a sharing violation (PermissionError, an OSError subclass),
            # and the on-loop read retry cannot pause (a loop sleep would starve the
            # LoopStallWatchdog heartbeat), so the immediate retries expire while the
            # replace is still in flight, _read_messages re-raises, and the
            # except-BaseException arm below rolls the whole tab back — the
            # intermittent `restored == N-1` open-tabs drop on restart
            # (test_restore_open_slots_async_yields_between_tabs, Windows shard).
            # Reading first removes the self-inflicted race: the file is quiescent
            # for the read, and the backfill lands once nothing is reading it. The
            # id is freshly minted with no on-disk siblings, so read_messages_chained
            # returns the identical window whether it is written before or after.
            # Kept off the loop because update_metadata enters _locked (flock +
            # os.close), a blocking-on-loop-prohibited op.
            #
            # Reading first makes THIS slot's own read quiescent, but firing the
            # backfill off the loop here still leaves its os.replace in flight
            # when the restore driver returns. A caller that reads this transcript
            # on the loop right after restore (a sidebar read, a resume) can then
            # land inside the replace window — the same Windows sharing violation.
            # When the driver supplies a drain list we hand the backfill to it so
            # the replace is committed before the driver returns; otherwise it
            # fires off the loop. Mirrors _apply_recent_session's pending_backfills.
            if pending_backfills is not None:
                pending_backfills.append((history_key, {"tab_id": applied.minted_tab_id}))
            else:
                update_metadata_off_loop(
                    state.conversation_log, history_key, {"tab_id": applied.minted_tab_id}
                )
        if _lazy_tail is not None:
            # A sidebar row: the window waits for its first reader. The tail
            # stands in for the summary, and nothing here read the transcript,
            # so the tab_id backfill above has no read left to race.
            slot._lazy_tail = _lazy_tail
            # The loader keeps the parsed fields its settle needs, never the raw
            # line: that line still carries whatever the restore bounds refused
            # (an oversized queue, say), and the row may stay unopened for the
            # life of the process. A row that can wait carries no local-turn
            # marker, so settle reads the same answer from an empty line.
            applied.meta = {}
            slot._lazy_window = _PendingWindow(state, history_key, applied)
            slot._dirty = False
            logger.info("Restored session %s (%s) as a sidebar row", slot_name, slot.title)
            return slot
        _load_rehydrated_window(state, slot, messages, applied)
        logger.info("Rehydrated session %s (%s) from history", slot_name, slot.title)
        return slot
    except BaseException:
        # Undo what THIS call added.
        #
        # Owned here rather than in each caller: get_or_create_slot runs before
        # the fallible work (the transcript read, redaction, slot.append), so a
        # failure leaves an empty slot registered in state._slots. A caller that
        # forgets to compensate leaves restore_recent_sessions to hit its
        # `if slot_name in state._slots: continue` dedup guard and skip the
        # proper restore -- the user then sees a tab with the right title and
        # agent but empty or wrong history.
        #
        # Unconditional pop: the function returns early when the slot already
        # exists, so reaching here means this call created it.
        state._slots.pop(slot_name, None)
        if not preexisting_restricted:
            # Otherwise a later get_or_create_slot (default memory_mode
            # 'persistent') silently inherits restricted status, blocking
            # consolidation and lessons for what should be a normal session.
            state._restricted_keys.discard(restricted_key)
        raise


async def rehydrate_slot_from_history_async(
    state: DashboardState,
    slot_name: str,
    *,
    kiro_model_map: dict[str, str] | None = None,
    adopt_closed: bool = False,
) -> _ChatSlot | None:
    """:func:`_rehydrate_slot_from_history` with the disk reads off the loop.

    Same contract and return values as the synchronous form, including
    returning ``None`` for a session the user closed with ✕.

    Why split rather than simply wrapping the whole thing in
    ``asyncio.to_thread``: slot construction is loop-affine. It reaches
    ``get_or_create_slot`` → ``push_slots_update`` → ``_broadcast``, which uses
    ``asyncio.Queue.put_nowait`` and ``Event.set`` — neither thread-safe — and
    ``_spawn_ws_send``'s ``ensure_future`` raises off-loop. That raise lands in
    a broad ``except`` that marks every connected dashboard client dead and
    drops it *without a close frame*, so browsers never reconnect and stop
    receiving frames until a manual reload. ``restore_open_slots_async``
    documents the same invariant.

    So only the reads move: the metadata line, the chained message walk (tens of
    MB of read plus JSON parse on a large session) and the agent→model map.
    Everything that touches slot state runs on the loop, as the synchronous
    callers do. The reads are shared with the two bulk restore drivers via
    :func:`_prefetch_rehydrate_inputs`, so all three prefetch identically.

    Because the read is offloaded, the state it observed can be stale by the time
    the build runs. Both post-hop windows are re-checked below, synchronously and
    immediately before the build so no await can reopen them: the close tombstone
    (a ✕ during the read) and :func:`_deletion_during_read` (the session deleted,
    or deleted and recreated, during the read). Returning ``None`` for either is
    part of the contract — the callers already handle a ``None`` result.

    A rebuilt slot also gets its MCP-app claim recovery here, so every caller has
    it without asking; see the comment at that call for why it is not each
    caller's job.
    """
    if not state.conversation_log:
        return None
    slot_name = _normalize_slot_key(slot_name)
    if slot_name in state._slots:
        return state._slots[slot_name]
    history_key = slot_transcript_key(slot_name)
    conv_log = state.conversation_log

    started = time.time()
    _meta, _readable, messages, model_map, _member_id, agent, effort_marker = (
        await asyncio.to_thread(
            _prefetch_rehydrate_inputs,
            conv_log,
            history_key,
            adopt_closed=adopt_closed,
            kiro_model_map=kiro_model_map,
        )
    )
    # ``messages is None`` covers both "never persisted" and "closed with ✕"
    # — the prefetch already applied the same guards the synchronous form does.
    if messages is None:
        return None
    meta = _meta
    # Read the spool HERE, in the awaiting phase, not after the build. The two
    # race re-checks below are placed synchronously and immediately before the
    # build precisely so no await can reopen the windows they close, and an await
    # AFTER the build reopens the deletion one a step later: the slot is published
    # by then, so a deletion landing during that await leaves the caller holding a
    # slot for a session that has been deleted, and the delete-won save discards
    # what it accepts. The remedy is the split this module uses everywhere else --
    # filesystem work in the prefetch phase, the apply synchronous.
    #
    # The key has to be derived rather than asked of a slot, because there is no
    # slot yet: ``session_key_for`` is the same rule ``effective_session_key``
    # applies, and the builder below sets ``linked_session_key`` from this very
    # metadata field, so the two answer alike. ``history_key`` is NOT the fallback
    # -- it resolves the transcript FILE and its own docstring says the session key
    # cannot be recovered that way, so a channel-born slot would be addressed as a
    # ``dashboard:`` session that does not exist.
    _claims = await asyncio.to_thread(
        _read_mcp_app_claims,
        session_key_for(slot_name, str(meta.get("linked_session_key") or "")),
    )
    # Tab-close race. The user can click ✕ while the read above is in flight.
    # The close pops the slot and records a tombstone synchronously on the loop,
    # but persists the ``closed`` flag only after its own awaits — so the
    # metadata just read still says open, and rebuilding from it would re-create
    # a tab the user dismissed and then fire a nudge turn into it. The tombstone
    # is the authoritative signal in that window; the surface reconciler
    # consults it after its own awaits for the same reason.
    # Skipped for ``adopt_closed`` callers: those are app-owned worker slots
    # whose lifecycle belongs to the app rather than the user, and they have
    # already opted into restoring a session carrying the closed flag.
    if not adopt_closed and slot_closed_since(state, slot_name, started):
        logger.info(
            "Rehydration abandoned: session %s was closed while its transcript loaded",
            slot_name,
        )
        return None
    # DELETION race, the same window and the same remedy the two bulk restore
    # drivers apply. This wrapper's read is offloaded too, so it carries the same
    # window — the guard is folded in here rather than left as the one uncovered
    # instance, because a class of defect handled at two of three call sites
    # simply returns through the third.
    #
    # NOT gated on ``adopt_closed``: that opt-in is about the ``closed`` FLAG (an
    # app-owned worker slot whose lifecycle belongs to the app), not about the
    # session having been permanently deleted. Nothing wants to resurrect a
    # deleted transcript.
    #
    # Synchronous and immediately before the build, so no await can reopen the
    # window it closes.
    gone = _deletion_during_read(conv_log, history_key, meta, messages)
    if gone is not None:
        logger.info(
            "Rehydration abandoned: session %s was %s while its transcript "
            "loaded; refusing to rebuild a slot whose flush would rewrite it",
            slot_name,
            gone,
        )
        return None
    _restored = _rehydrate_slot_from_history(
        state,
        slot_name,
        kiro_model_map=model_map,
        adopt_closed=adopt_closed,
        _prefetched_meta=meta,
        _prefetched_messages=messages,
        _prefetched_member_identity=_member_id,
        _prefetched_agent=agent,
        _prefetched_effort_marker=effort_marker,
    )
    if _restored is not None:
        # Claim recovery belongs HERE, not in each caller: this function is how
        # anything outside this module rebuilds one slot from disk by TARGETED
        # history rehydration -- `channel_slots.surface_channel_session` reaches a
        # slot by its own route, so "the only way" would overstate it -- and it has
        # eight such callers (members, messaging, files, two in the Slack
        # gateway, Issue Radar, Spec Builder). Hooking them one at a time is how
        # the bulk drivers were first wired and two of four were missed; a caller
        # added later would silently restore a row whose flag the crash lost.
        #
        # SYNCHRONOUS, and after the build so the slot the recovery may mark dirty
        # is the one the caller receives. No await may separate the deletion
        # re-check above from this line, which is why the spool read happened back
        # in the prefetch phase -- the apply itself only touches memory.
        #
        # The live-slot early return above is deliberately NOT covered: nothing was
        # read from disk there, its flags are already in memory, and messaging's
        # cache hit would pay a spool scan per lookup.
        _reconcile_mcp_app_claims(_restored, _claims)
    return _restored


def _prefetch_recent_session(
    conv_log: ConversationLog,
    key: str,
    session: dict,
    *,
    folders_only: bool,
    cutoff: float | None,
) -> tuple[dict | None, list[dict] | None, tuple[str, str] | None, str | None, bool]:
    """Read one candidate session's metadata + transcript, off the loop.

    Applies the selection filters BETWEEN the two reads so a session that is
    going to be skipped never pays for its transcript walk — the metadata read is
    what the filters need, and it is the cheap one.

    Returns ``(None, None, None, None, False)`` for a session this pass must skip (not
    folder'd / pinned under ``folders_only``, closed with ✕, or outside the
    mtime window). The third element is the prefetched
    ``_member_restore_identity`` answer — dm.json is file IO too, and the apply
    half is loop-affine. Pure disk work: no slot state is touched, so the whole
    function is safe in ``asyncio.to_thread`` while the loop-affine apply half
    stays on the loop.
    """
    meta = conv_log.get_metadata(key)
    if not meta:
        # No metadata line at all. ``list_sessions()`` is a SNAPSHOT taken one
        # thread hop before this read, so a session can be
        # deleted in between and still appear in the list — or the read itself
        # came back empty. Either way there is nothing to build from, and
        # building anyway is destructive rather than merely useless: an empty
        # ``meta`` sails past the folder/pin/closed/cutoff filters below (all of
        # which read falsy), reaches ``get_or_create_slot``, and registers a
        # PHANTOM slot whose next flush RECREATES the transcript the user
        # deleted. ``_rehydrate_slot_from_history`` already refuses on empty
        # metadata for exactly this reason ("don't create a phantom slot"); this
        # makes the recent-sessions path agree with it.
        return None, None, None, None, False
    has_folder = bool(meta.get("folder_id"))
    has_pin = bool(meta.get("pinned"))
    if folders_only and not has_folder and not has_pin:
        return None, None, None, None, False
    if meta.get("closed"):
        return None, None, None, None, False
    if not has_folder and not has_pin:
        if cutoff is not None and session.get("modified", 0) < cutoff:
            return None, None, None, None, False
    return (
        meta,
        conv_log.read_messages_chained(key),
        _member_restore_identity(_recent_session_slot_name(key) or ""),
        _restored_agent_name(
            str(
                meta.get("linked_session_key")
                or slot_transcript_key(_recent_session_slot_name(key) or key)
            ),
            meta,
        ),
        _has_validated_effort_marker(meta.get("reasoning_effort")),
    )


def _apply_recent_session(
    state: DashboardState,
    key: str,
    slot_name: str,
    session: dict,
    meta: dict,
    messages: list[dict],
    *,
    conv_log: "ConversationLog",
    kiro_model_map: dict[str, str],
    restore_cfg: "KiroCrewConfig | None",
    member_identity: tuple[str, str] | None = _IDENTITY_UNRESOLVED,
    agent: str | None = None,
    effort_marker: bool = False,
    pending_backfills: list[tuple[str, dict]] | None = None,
) -> None:
    """Build the slot for one prefetched recent session.

    LOOP-AFFINE — everything here mutates slot state, and slot creation
    broadcasts through ``asyncio.Queue.put_nowait`` / ``Event.set`` (neither
    thread-safe). Split out of :func:`_restore_recent_sessions_steps` so the
    synchronous generator and :func:`restore_recent_sessions_async` share one
    definition of the build while differing only in where the reads that feed it
    happen.

    *pending_backfills* collects the ``(key, fields)`` of any tab-id metadata
    backfill this slot mints, instead of firing it off the loop here. The async
    driver passes a list and DRAINS it (its ``os.replace`` committed) before it
    returns, so a caller's immediate on-loop transcript read right after restore
    cannot land inside that replace window and trip a Windows sharing violation.
    When ``None`` (the synchronous :func:`restore_recent_sessions` driver) the
    backfill fires off the loop as before. Mirrors the drain
    :func:`_rehydrate_slot_from_history` performs for the open-slot driver.
    """
    _restore_cfg = restore_cfg
    # Member keys resolve their pin from dm.json BEFORE construction (the
    # constructor's member-* reservation refuses a bare member key); a member
    # key without a binding is skipped, not published. Async callers prefetch
    # the binding read in their worker-thread step (this half is loop-affine);
    # the inline resolve serves synchronous callers.
    _member_identity = (
        _member_restore_identity(slot_name)
        if member_identity is _IDENTITY_UNRESOLVED
        else member_identity
    )
    if _member_identity is _SKIP_MEMBER_RESTORE:
        return
    if _is_app_owned_channel_row(meta, key):
        return
    purpose = _metadata_codec.Recent(
        name=slot_name,
        member=_member_identity,
        agent=agent,
        cfg=_restore_cfg,
        model_map=kiro_model_map,
        effort_marker=effort_marker,
        listing=session,
        history_key=key,
    )
    # No channel origin here: the caller skips every non-dashboard key, so a
    # channel-born session never reaches this — ``channel_slot_reconciler`` owns
    # surfacing those.
    slot = state.get_or_create_slot(slot_name, **_metadata_codec.slot_args(meta, purpose))
    # The title is the session list's (it names an untitled session after its
    # first message); every other field comes from the line, through the one
    # field table (``slot_persistence.metadata_codec``).
    applied = _metadata_codec.apply(state, slot, meta, purpose)
    if applied.minted_tab_id is not None:
        # restore_recent_sessions runs during on_startup (event loop live) — keep
        # the _locked flock/os.close off the loop via the off-loop backfill
        # helper. Dispatched AFTER the transcript read (the caller prefetches
        # messages first) so its os.replace() cannot race the read of the same
        # file — see the equivalent note in _rehydrate_slot_from_history.
        #
        # Reading first makes THIS slot's own read quiescent, but firing the
        # backfill off the loop here still leaves its os.replace in flight when
        # the restore driver returns. A caller that reads this transcript on the
        # loop right after restore (a sidebar read, a resume) can then land inside
        # the replace window — the same Windows sharing violation. When the driver
        # supplies a drain list we hand the backfill to it so the replace is
        # committed before the driver returns; otherwise it fires off the loop.
        if pending_backfills is not None:
            pending_backfills.append((key, {"tab_id": applied.minted_tab_id}))
        else:
            update_metadata_off_loop(conv_log, key, {"tab_id": applied.minted_tab_id})
    older_cut = max(0, len(messages) - 500)
    slot._disk_older_count = older_cut
    # Durable-only view of the same prefix, recomputed from disk on every load —
    # see the equivalent line (and the islice rationale) in
    # _rehydrate_slot_from_history.
    slot._disk_older_durable_count = durable_row_count(islice(messages, older_cut))
    for m in messages[-500:]:
        role = m.get("role", "assistant")
        cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = m.get("content", "")
        # CONTENT is redacted on load; META is deferred to the emit sites.
        # See the equivalent loop in _rehydrate_slot_from_history for the
        # measured rationale (content ~0.4s / ~204 readers, meta ~5.5s /
        # 31 readers that touch only control fields outside the emit sites).
        if role != "user":
            content = redact_display_content(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            broadcast=False,
            # Blocked-link records bounded where the slot retains them; see the
            # equivalent append in _rehydrate_slot_from_history.
            meta=(
                with_bounded_redaction_records(m["meta"])
                if isinstance(m.get("meta"), dict)
                else None
            ),
            mint_mid=False,
        )
        # See the equivalent call in _rehydrate_slot_from_history.
        carry_provenance(slot.messages[-1], m)
        remember_unknown_row_fields(slot, m)
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # Loaded window is the on-disk window region; older lines (counted in
    # _disk_older_count above) are the frozen prefix saves never rewrite.
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    # The held notes the window already delivered, the local-turn marker and
    # the title refresh mark: the fields whose read needs the loaded window.
    applied.settle(messages)
    logger.info("Restored session %s (%s)", slot_name, slot.title)
