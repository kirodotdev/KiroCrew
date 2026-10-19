"""The restart restore drivers.

The open-tab restore (its plan, the restore budget, the bounded tail of a tab
built as a row and the reports of the tabs left unrestored) and the
recent-session restore, each with its async twin, and the MCP-app claim recovery
a restored slot awaits. Composed onto :mod:`kiro_crew.dashboard.chat_persistence`;
see this package's ``__init__``. New restore driver steps go here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING

from kiro_crew.dashboard.state import SlotOrigin

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_persistence import (
        RESTORE_SLOT_BUDGET,
        ConversationLog,
        DashboardState,
        _apply_recent_session,
        _apply_restored_open_slot,
        _build_kiro_model_map,
        _ChatSlot,
        _deletion_during_read,
        _has_validated_effort_marker,
        _load_restore_cfg,
        _local_turn_generation,
        _member_restore_identity,
        _prefetch_recent_session,
        _prefetch_rehydrate_inputs,
        _read_mcp_app_claims,
        _read_open_slots_snapshot,
        _recent_session_slot_name,
        _reconcile_mcp_app_claims,
        _recover_mcp_app_claims,
        _restored_agent_name,
        _sanitize_open_slot_key,
        _sync_dashboard_slots,
        _transcripts_may_be_remote_only,
        crew_bound_on_disk,
        effective_session_key,
        live_loop_slot_keys,
        logger,
        notify_left_in_history,
        redact_display_content,
        slot_closed_since,
        slot_transcript_key,
        stored_loop_slot_keys,
    )


#: How many open tabs the startup restore builds with their window loaded,
#: newest first. Every tab after these comes back as a sidebar row whose window
#: loads when the tab is first opened (or anything else first reads its rows),
#: so boot cost stops scaling with the size of every transcript ever left open.
EAGER_WINDOW_RESTORES = 8

#: Rows of a sidebar row's newest tail read at restore. The sidebar summary
#: (last message, its options, the unread watermark, the interrupted flag)
#: projects from these until the window loads.
LAZY_TAIL_ROWS = 100


#: The origins of a tab a person opened: the dashboard's own, and an untagged
#: tab from a build that did not record one.
_PERSON_ORIGINS = frozenset({"", SlotOrigin.USER})


def _window_can_wait(meta: dict) -> bool:
    """Whether a tab's window may load after startup instead of during it.

    Only a person's own dashboard tab waits. Something other than the person
    drives every other kind -- a linked channel, cron or workflow session, an
    app, a remote binding, a session another session created -- and its next
    delivery would arrive on a path that reads the window before anyone opens
    the tab. Not either when the line carries a local turn the previous process
    never tore down, nor held notes: settling those reconciles rows into the
    window, and the sidebar would show the tab as it was before that settles.
    The two kinds only the slot key can tell (a crew member's tab, a crew worker
    bound to an open work item) are asked by :func:`_prefetch_open_slot_inputs`.
    """
    return (
        str(meta.get("origin") or "") in _PERSON_ORIGINS
        and not meta.get("linked_session_key")
        and not meta.get("app")
        and not meta.get("created_by")
        and not meta.get("remote_slot")
        and not _local_turn_generation(meta)
        and not meta.get("deferred_notes")
    )


def _may_restore_as_row(restored: int, key: str, looped: set[str] | None) -> bool:
    """Whether the open-tab restore may build *key* as a sidebar row.

    Past the newest :data:`EAGER_WINDOW_RESTORES` tabs only, and never a tab an
    armed auto-nudge loop drives (its next fire reads the window), nor any tab
    while which tabs a loop drives is unknown.
    """
    return restored >= EAGER_WINDOW_RESTORES and looped is not None and key not in looped


#: The longest text a sidebar row's tail keeps of one row. A longer message keeps
#: its start (the sidebar preview) and its end (where a closing options line
#: sits), joined by an ellipsis line.
TAIL_TEXT_BOUND = 4096
#: The longest presentation class a tail row keeps; a longer one is dropped. The
#: summary reads a class only for a stop or permission marker, both short.
_TAIL_CLS_BOUND = 1024
#: The one-word markers the summary reads off a tail row's ``meta``: a notice's
#: ``kind`` and ``notice`` (``is_system_notice``), a dispatching inject's
#: ``injectKind`` (``_is_turn_inject``), and the applied quiet end's trusted tool
#: identity and flag (``is_quiet_end_row``), without which a turn that ended
#: through ``nothing_to_do`` reads as interrupted until the window loads. Each is
#: kept only as a short scalar; every other key (tool payloads, variants,
#: records) is left out of the tail.
_TAIL_META_KEYS = ("kind", "injectKind", "notice", "tool_name", "mcp_server", "ends_turn")
_TAIL_SCALAR_BOUND = 128


def _bounded_text(text: str) -> str:
    if len(text) <= TAIL_TEXT_BOUND:
        return text
    half = TAIL_TEXT_BOUND // 2
    return f"{text[:half]}\n…\n{text[-half:]}"


def _short_scalar(value: object) -> object | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and len(value) <= _TAIL_SCALAR_BOUND:
        return value
    return None


def _display_tail(rows: list[dict]) -> list[dict]:
    """A sidebar row's tail: a bounded projection of what the summary reads.

    The tail stands in for the window in every summary read (the newest row's
    time, the last message and its options, the interrupted flag, the source-link
    chips), and it lives as long as the row stays unopened, so it keeps only
    those fields and bounds each one. Non-user content is redacted exactly as the
    window replay redacts it, before it is bounded, so a credential a legacy or
    agent-written row carries never reaches the sidebar raw.
    """
    shown: list[dict] = []
    for m in rows:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "assistant")
        role = role if isinstance(role, str) and len(role) <= _TAIL_SCALAR_BOUND else ""
        content = m.get("content", "")
        content = content if isinstance(content, str) else ""
        if role != "user":
            content = redact_display_content(content)
        row: dict = {"role": role, "content": _bounded_text(content)}
        if isinstance(ts := m.get("ts"), str) and len(ts) <= _TAIL_SCALAR_BOUND:
            row["ts"] = ts
        if isinstance(cls := m.get("cls"), str) and len(cls) <= _TAIL_CLS_BOUND:
            row["cls"] = cls
        meta = m.get("meta")
        if isinstance(meta, dict):
            kept = {
                key: value
                for key in _TAIL_META_KEYS
                if (value := _short_scalar(meta.get(key))) is not None
            }
            if kept:
                row["meta"] = kept
        shown.append(row)
    return shown


def _prefetch_open_slot_inputs(
    conv_log: ConversationLog,
    history_key: str,
    *,
    kiro_model_map: dict[str, str],
    lazy: bool,
) -> tuple[
    dict,
    bool,
    list[dict] | None,
    dict[str, str] | None,
    tuple[str, str] | None,
    str | None,
    bool,
    list[dict] | None,
]:
    """The open-tab restore's read for one tab, off the loop. BLOCKING.

    :func:`_prefetch_rehydrate_inputs`, plus an eighth value: the tail rows of a
    tab built as a sidebar row, else ``None``. With *lazy* and a person's own tab
    whose window can wait, the transcript is read only as far as that bounded
    tail; the messages value is then ``None`` and the tail is not. A tab whose
    tail holds no row at all takes the full read, so a fork whose rows all live
    in its ancestors still shows what it says.
    """
    if lazy:
        slot_name = history_key.removeprefix("dashboard:")
        meta, readable = conv_log.get_metadata_status(history_key)
        if (
            readable
            and meta
            and not meta.get("closed")
            and _window_can_wait(meta)
            and _member_restore_identity(slot_name) is None
            and not crew_bound_on_disk(slot_name)
        ):
            tail = _display_tail(conv_log.read_messages_chained_tail(history_key, LAZY_TAIL_ROWS))
            if tail:
                return (
                    meta,
                    readable,
                    None,
                    kiro_model_map,
                    None,
                    _restored_agent_name(history_key, meta),
                    _has_validated_effort_marker(meta.get("reasoning_effort")),
                    tail,
                )
    return (
        *_prefetch_rehydrate_inputs(
            conv_log, history_key, kiro_model_map=kiro_model_map, with_status=True
        ),
        None,
    )


def _open_slot_restore_plan(
    conv_log: ConversationLog, keys: list[object]
) -> tuple[list[object], set[str] | None]:
    """Order the open-tab keys newest first and read which tabs a loop drives. BLOCKING.

    Newest is the transcript's mtime, which advances on every message, so the
    tabs the restore budget keeps are the ones most recently used. The second
    value is the set of slot keys an armed auto-nudge loop drives (always
    restored), or ``None`` when the loop store cannot be read; the restore then
    applies no budget rather than leave a loop's tab unbuilt, which ends the loop.
    """

    def _mtime(raw: object) -> float:
        key = _sanitize_open_slot_key(raw)
        if key is None:
            return 0.0
        mtime = conv_log.session_mtime(slot_transcript_key(key))
        return float(mtime) if isinstance(mtime, (int, float)) else 0.0

    looped = live_loop_slot_keys()
    if looped is None:
        looped = stored_loop_slot_keys()
    return sorted(keys, key=_mtime, reverse=True), looped


def _over_restore_budget(state: DashboardState, key: str, looped: set[str] | None) -> bool:
    """Whether *key* is past the restore budget and not driven by a loop."""
    if looped is None or key in looped:
        return False
    return state.live_slot_count() >= RESTORE_SLOT_BUDGET


def _kept_past_budget(conv_log: ConversationLog, key: str) -> bool:
    """Whether slot *key* takes the normal path past the budget. BLOCKING.

    True for a pinned tab, for a tab in a folder (the recent-sessions restore
    and the channel reconciler rebuild every foldered tab whatever its age, so
    leaving one out here would neither hold the budget nor keep it in History),
    for a crew worker tab bound to an open work item (the wake gate reads a
    worker with no slot as closed), and for one with no
    readable local metadata: an unreadable read or an absent transcript costs
    the normal path no transcript read, and that path's own guards decide
    whether the key stays in the reopen seed (unreadable, or remote-only after a
    remote authority restore) or goes.
    """
    meta, readable = conv_log.get_metadata_status(slot_transcript_key(key))
    if not readable or not meta or bool(meta.get("pinned")) or bool(meta.get("folder_id")):
        return True
    return crew_bound_on_disk(key)


def _report_restore_budget(state: DashboardState, skipped: int) -> None:
    if not skipped:
        return
    logger.info(
        "restore_open_slots: left %d older tab(s) in history; the restore budget "
        "is %d open sessions",
        skipped,
        RESTORE_SLOT_BUDGET,
    )
    notify_left_in_history(
        state,
        skipped,
        f"{skipped} older open tab(s) were left in History at startup to keep "
        f"{RESTORE_SLOT_BUDGET} sessions open at most.",
    )


def _restore_open_slots_steps(state: DashboardState) -> "Iterator[int]":
    """Drive the open-tab restore one tab at a time, yielding the running count.

    Exposed as a generator so a plain synchronous caller
    (:func:`restore_open_slots`) can spin through it. The event-loop path does
    NOT drive this generator: it needs each per-tab disk read hoisted into a
    worker thread, which a synchronous generator cannot express, so
    :func:`restore_open_slots_async` runs its own prefetch-then-apply loop over
    the same shared helpers (:func:`_read_open_slots_keys`,
    :func:`_sanitize_open_slot_key`, :func:`_prefetch_rehydrate_inputs`). See
    :func:`restore_open_slots` for the behavioural contract.
    """
    if not state.conversation_log:
        # Persistence is a no-op this process, so no listed tab went unshown: a real
        # zero, and one the dashboard must hear (see _report_restore_with_no_seed).
        _report_restore_with_no_seed(state)
        return
    keys, known = _read_open_slots_snapshot()
    # An unknown set must not license the latch its caller flips, so say so before
    # returning: an unreadable registry is not an empty one. Published BEFORE the
    # report below, which reads it to decide which answer it is publishing.
    state.open_slots_knowable = known
    if not keys:
        # Both ways: a readable registry holding no keys reports a measured zero, and
        # an unreadable one reports that the set could not be learned. The second is
        # the state the user most needs to hear -- tabs may be missing and this boot
        # cannot say -- and reporting neither leaves the dashboard polling in silence.
        _report_restore_with_no_seed(state)
        return
    restored = 0
    # Rebound each pass so it reflects only THIS restore: a key that becomes
    # readable later must stop being carried, and a fresh set() keeps mutation
    # off the class-level frozenset baseline.
    unrestored: set[str] = set()
    state.unrestored_slot_keys = unrestored
    # The subset a reader is told about -- see _apply_restored_open_slot.
    dropped: set[str] = set()
    # Read once per restore: whether this boot's authority pair came from a
    # remote snapshot whose transcripts are fetched lazily (see
    # _transcripts_may_be_remote_only).
    preserve_remote_only = _transcripts_may_be_remote_only()
    # Built once and shared across every tab — it is identical per slot.
    kiro_model_map = _build_kiro_model_map()
    # main's restore budget plan, kept: past the budget only a pinned or crew-bound
    # tab is still built. The rest stay in history, out of the reopen seed.
    keys, looped = _open_slot_restore_plan(state.conversation_log, keys)
    skipped = 0
    # Which key the loop is on, so an abort can name the ones it never read. A
    # generator is ABANDONED by a GeneratorExit thrown AT its yield rather than by
    # returning, so this driver reaches the same half-done state the async one does
    # on cancellation, and owes the same tail-carry.
    reached = 0
    try:
        for reached, raw in enumerate(keys):
            key = _sanitize_open_slot_key(raw)
            if key is None or key in state._slots:
                continue
            # Past the budget only a pinned or crew-bound tab is still built. The
            # rest stay in history, out of the reopen seed: they were not shown to
            # be unreadable.
            if _over_restore_budget(state, key, looped):
                try:
                    pinned = _kept_past_budget(state.conversation_log, key)
                except Exception:
                    logger.debug("restore_open_slots: pin read failed for %s", key, exc_info=True)
                    pinned = True
                if not pinned:
                    skipped += 1
                    yield restored
                    continue
            try:
                # Ask whether the metadata READ succeeded, not just whether it came
                # back empty (``with_status``) — see _prefetch_rehydrate_inputs.
                #
                # These reads MUST stay inside the per-tab guard. The async driver
                # has no except at its call site either, so anything escaping here
                # aborts dashboard startup and costs every LATER tab too.
                meta, readable, messages, model_map, member_identity, agent, effort_marker, tail = (
                    _prefetch_open_slot_inputs(
                        state.conversation_log,
                        slot_transcript_key(key),
                        kiro_model_map=kiro_model_map,
                        lazy=_may_restore_as_row(restored, key, looped),
                    )
                )
                restored += _apply_restored_open_slot(
                    state,
                    key,
                    meta=meta,
                    readable=readable,
                    messages=messages,
                    model_map=model_map,
                    member_identity=member_identity,
                    agent=agent,
                    effort_marker=effort_marker,
                    unrestored=unrestored,
                    preserve_remote_only=preserve_remote_only,
                    tail=tail,
                    dropped=dropped,
                )
            except Exception:
                logger.debug("restore_open_slots: rehydrate failed for %s", key, exc_info=True)
                # Same epistemic position as an unreadable read: the session was not
                # shown to be gone, so keep its key rather than erasing the seed.
                unrestored.add(key)
                dropped.add(key)
                # No rollback here: _rehydrate_slot_from_history undoes its own
                # partial slot and restricted key, so every caller gets it rather
                # than only the ones that remembered to compensate.
            # Restore-time recovery of an app flag whose claim outlived it, after the
            # handler above rather than inside it: a failure here must not mark the
            # tab unrestored over a display flag. This driver's reads are inline by
            # construction, so the spool read is too.
            _recovered_slot = state._slots.get(key)
            # A sidebar row recovers its claims when its window loads.
            if _recovered_slot is not None and not _recovered_slot.window_pending:
                _recover_mcp_app_claims(_recovered_slot)
            # One yield point per tab, reached on EVERY outcome. A failing tab still
            # costs real I/O, so a run of failing tabs that skipped the yield would
            # monopolise the loop and feed the stall watchdog. The sync driver just
            # spins through it; the async driver has its own per-tab yield.
            yield restored
    except BaseException:
        # Every key from the one in hand onward is UNREAD, so it goes back into the
        # reopen seed -- see _carry_unread_slot_keys for why the latch flipping in the
        # caller's finally makes that the difference between a stale tab and a lost
        # one. ``BaseException`` because the throw that ends a generator is
        # GeneratorExit, which is not an Exception. Carrying the key in hand too even
        # when it restored is harmless: _persist_open_slots dedups the union.
        _carry_unread_slot_keys(keys[reached:], unrestored)
        raise
    if restored:
        logger.info("Restored %d open tab(s) from open_slots.json", restored)
    _report_restore_budget(state, skipped)
    # After the loop, so the counts are final. Not in a finally: on the abort path the
    # keys were just carried back into the seed, and this restore has no final answer
    # about them to record.
    _record_unrestored_open_tabs(state, listed=len(keys), restored=restored, kept=dropped)


def _carry_unread_slot_keys(raw_keys: "Sequence[object]", unrestored: set[str]) -> int:
    """Keep every key in *raw_keys* in the reopen seed; return how many were added.

    What a restore owes the keys it never got to. ``open_slots_restored`` tells the
    persist writers that ``_slots`` is the authoritative open-tab set, which is a
    true statement only for a restore that RAN TO THE END -- so a pass cancelled at
    tab 1 of 16 leaves it down, and a flush then merges instead of pruning the file
    to the one tab that made it.

    The carry is the other half, and the two are independent rather than alternative.
    An unread key's honest position is the one an unreadable read already gets -- the
    session was not shown to be gone -- so it joins the same ``unrestored_slot_keys``
    set that path uses, and ``_persist_open_slots`` folds that set into every
    snapshot it writes, licensed to prune or not. One rule then covers every key this
    restore has no answer about, whether it stopped at the key or never reached it;
    the latch separately denies the licence. Either alone keeps the file whole, which
    is the point: the exit this guards against is the one where the process is being
    torn down and the remaining code may not get to run.
    """
    carried = 0
    for raw in raw_keys:
        key = _sanitize_open_slot_key(raw)
        if key is None or key in unrestored:
            continue
        unrestored.add(key)
        carried += 1
    return carried


def _record_unrestored_open_tabs(
    state: DashboardState,
    *,
    listed: int,
    restored: int,
    kept: "Iterable[str]",
) -> None:
    """Leave a durable account of the listed tabs that should be here and are not.

    *kept* is the DROPPED subset rather than every key in the reopen seed, for the
    reason :func:`_apply_restored_open_slot` gives: a remote-only seed is expected to
    sit in that set across boots, and counting it would raise this notice on every
    restart of a system behaving correctly.

    Blocking: one store read per kept key to resolve the log it writes, then one
    queued append each. Called from a worker thread, and only when a key was
    actually kept -- the healthy restore pays nothing.

    Also publishes the count the dashboard reads for its notice. That is here rather
    than at each ``unrestored.add`` site so the number the user is shown and the
    number the log records are one count taken at one moment.

    The published report carries ``unknowable``, read off the same latch half the
    persist writers read. A count is a measurement of a set that was READ, so a
    restore whose registry no generation of which could be read has no count to
    offer -- and the honest report there is not a zero but "the set could not be
    learned", which the endpoint hands to the dashboard's error notice. It rides on
    the report rather than on a second attribute because the two answers are one
    fact taken at one moment, and because a reader that has to consult two fields to
    know whether the first one means anything eventually consults only the first.
    """
    keys = sorted(kept)
    state.unrestored_slot_notice = {
        "count": len(keys),
        "keys": keys,
        "unknowable": not bool(getattr(state, "open_slots_knowable", True)),
    }
    if not keys:
        return
    try:
        from kiro_crew.crew_log import emit as crew_log_emit
    except Exception:  # pragma: no cover - crew log unavailable
        return
    if not crew_log_emit.enabled():
        return
    for key in keys:
        try:
            sid, _decided, _complete = crew_log_emit.slot_previous_store(key)
            if not sid:
                # No store under this slot to append to. The notice still carries the
                # key, which is the half the user acts on.
                continue
            crew_log_emit.on_open_tab_unrestored(
                sid, listed=listed, restored=restored, kept=len(keys)
            )
        except Exception:
            logger.debug("restore_open_slots: could not record the drop of %s", key, exc_info=True)


def _report_restore_with_no_seed(state: DashboardState) -> None:
    """Publish the settled report of a restore that had no keys to enumerate.

    An empty or absent registry restores nothing, and that is an ANSWER rather than
    the absence of one -- the default answer, in fact: a fresh install, and anyone
    whose last session closed every tab. Left unpublished, the notice stays ``None``,
    ``GET /api/chat/slots/unrestored`` answers ``reported: false`` for the life of the
    process, and ``useUnrestoredTabs`` re-asks every 5s on every dashboard page for
    the life of the page. A poll that then fails past the retry ladder -- a gateway
    restart is enough -- shows the red "could not check restored tabs" notice to a
    user who lost nothing.

    Routed through the one publisher so the shape cannot drift from the reporting
    path, and safe to call on the event loop: the publisher's per-key store reads
    only run when a key was kept, and here none was. Which of the two answers it
    publishes -- a measured zero, or "the set could not be learned" -- the publisher
    takes from ``open_slots_knowable``, so an unreadable registry reaches the
    dashboard as the failure it is instead of as a zero.

    For an exit that FINISHED, either way. A pass cancelled before it got through
    the seed has no answer about the keys it never read, and ``None`` -- "the restore
    has not reported" -- stays the honest reading for that one.
    """
    _record_unrestored_open_tabs(state, listed=0, restored=0, kept=())


def restore_open_slots(state: DashboardState) -> int:
    """Restore the tabs the user had open at the previous shutdown.

    Reads ``<config_dir>/open_slots.json`` (written by
    ``DashboardState._persist_open_slots`` on every flush) and rehydrates
    each listed key from on-disk session metadata so it shows up in the
    Sessions sidebar exactly as it did before the restart — independent of
    the ``restore_window_minutes`` mtime cutoff used by
    ``restore_recent_sessions``.

    Path resolves through ``config_dir()`` (honors ``KIROCREW_HOME``) so
    dev/test instances with non-default homes don't read the production
    ``~/.kiro/crew`` snapshot.

    Returns the number of slots restored. Missing / malformed file is a
    no-op (returns 0). Sessions that have been explicitly closed
    (``meta.closed``) are skipped via _rehydrate_slot_from_history's own
    guard, so closing a tab and then restarting still loses the tab.

    Tabs are built newest first. Once ``live_slot_count()`` reaches
    ``RESTORE_SLOT_BUDGET`` only pinned tabs, tabs an armed auto-nudge loop
    drives and crew worker tabs bound to an open work item are still built; the
    rest stay in history, unrestored and undeleted, and one notification says so.

    Blocking: restores every tab without yielding. Startup on the event loop must
    use :func:`restore_open_slots_async` instead — see the note there.
    """
    restored = 0
    # Same condition the async driver spells out, read off the generator's own
    # lifecycle: a ``for`` over a generator only falls through when the generator
    # RETURNS, so reaching this line means every key was looked at (or there were
    # none). Anything that propagates -- including a raise from the snapshot read
    # itself -- skips it and leaves the latch down.
    enumerated = False
    try:
        for restored in _restore_open_slots_steps(state):
            pass
        enumerated = True
    finally:
        # The open-tab restore has run this boot (even for a missing/empty
        # file): an empty ``_slots`` from here on is authoritative, so the
        # periodic flush may snapshot open_slots.json again — see
        # DashboardState._persist_open_slots.
        #
        # Unless the set was never KNOWABLE, or this pass never reached the end of
        # it. Either way this process has no basis for calling ``_slots``
        # authoritative, and leaving the latch down keeps the persist writers in
        # merge mode for the rest of its life — which is the correct posture, not a
        # stuck flag: a process that never learned the set must never prune it.
        #
        # The visible cost of that posture, for whoever reads this next: with the
        # latch down every flush takes the merge branch, which re-reads the live
        # registry. So a boot whose registry was REFUSED freezes the file -- the
        # merge read is refused too and skips the write -- and a boot whose registry
        # was merely damaged can only add to it. Either way a tab the user closes in
        # this process is back in the sidebar on the next boot, and in the refused
        # case a tab opened in it is not saved at all, until a restart or a read that
        # succeeds. The registry keeps the set it had, which is the cheaper error
        # than deleting a set nobody managed to read: this fails closed deliberately.
        state.open_slots_restored = enumerated and bool(getattr(state, "open_slots_knowable", True))
    return restored


async def restore_open_slots_async(state: DashboardState) -> int:
    """:func:`restore_open_slots`, with the disk reads off the loop.

    Restoring a tab reads and redacts a transcript, so a user with many large
    tabs can spend tens of seconds in here. Doing that synchronously monopolizes
    the event loop — and because ``_loop_heartbeat`` pets the
    ``LoopStallWatchdog`` *from a coroutine*, a blocked loop cannot pet it. The
    watchdog's 25s ``exit_after`` timer then fires, dumps thread stacks and
    ``_exit``s the gateway, which is exactly the observed startup crash-loop: the
    app never finished booting.

    Yielding between tabs (the ``sleep(0)`` below) was the first fix and is kept:
    it bounds how long any ONE tab can hold the loop. But it only ever moved the
    boundary — the whole per-tab read still ran ON the loop, so a single large
    transcript could stall it for seconds before the next yield arrived. So the
    reads themselves now move: ``open_slots.json``, the agent→model map, and each
    tab's metadata + chained transcript walk are hoisted into
    ``asyncio.to_thread``, and only the slot mutation stays here.

    That mutation cannot follow them. Creating a slot broadcasts via
    ``asyncio.Queue.put_nowait`` / ``asyncio.Event.set``, neither of which is
    thread-safe, and ``_spawn_ws_send``'s ``ensure_future`` raises off-loop into
    a broad ``except`` that marks every connected dashboard client dead and drops
    it *without a close frame* — browsers then never reconnect. So this driver
    runs its own prefetch-then-apply loop (the shape
    ``rehydrate_slot_from_history_async`` established) rather than driving
    :func:`_restore_open_slots_steps`, whose reads are inline by construction.
    The generator stays for the synchronous callers.

    Because this yields, the 5s periodic flush (already running by this point)
    can interleave — so ``restoring_open_slots`` is held for the duration to stop
    it snapshotting a half-restored slot set over open_slots.json.
    """
    restored = 0
    state.restoring_open_slots = True
    # Whether this restore reached the END of the seed, which is the only thing that
    # makes ``_slots`` the authoritative open-tab set. Set at each exit that really
    # is a completion -- including the two "nothing to restore" returns below -- and
    # left False by every other exit, so a cancellation anywhere leaves the writers
    # in merge mode instead of licensing a prune. It starts False BEFORE the first
    # await for the reason the finally gives: the awaits ahead of the per-tab loop
    # are suspension points too, and at the first of them not even the seed is read.
    enumerated = False
    # Tab-id backfills this restore mints. Drained (its os.replace committed)
    # before we return, so a caller's immediate on-loop transcript read cannot
    # race a backfill still in flight on Windows. See _apply_restored_open_slot's
    # pending_backfills note, and restore_recent_sessions_async's equivalent drain.
    pending_backfills: list[tuple[str, dict]] = []
    try:
        # No conversation log means persistence is a no-op this process, but the
        # restore has still "run": fall through to the finally so the latch
        # flips and the persist writers resume pruning instead of staying in the
        # permanent pre-restore merge mode.
        if not state.conversation_log:
            enumerated = True
            # A zero the dashboard must hear, like the generator's copy of this exit.
            _report_restore_with_no_seed(state)
            return 0
        keys, known = await asyncio.to_thread(_read_open_slots_snapshot)
        # Published before the early return below, for the reason the generator's own
        # copy of this line gives: the latch in the finally reads it, and so does the
        # report, which is a measured zero or an unknowable set depending on it.
        state.open_slots_knowable = known
        if not keys:
            enumerated = True
            # Both ways, like the generator: a readable registry with no keys reports
            # a zero, an unreadable one reports that the set could not be learned.
            _report_restore_with_no_seed(state)
            return 0
        # Rebound only once there is a snapshot to restore FROM, matching the
        # generator: a missing/malformed file must not clear a carried set.
        unrestored: set[str] = set()
        state.unrestored_slot_keys = unrestored
        # The subset a reader is told about -- see _apply_restored_open_slot.
        dropped: set[str] = set()
        conv_log = state.conversation_log
        # Which key the loop is on, so a cancellation can name the ones it never
        # read. This driver awaits once per tab, so a shutdown landing in the middle
        # of a 16-tab restore is the ordinary case rather than an exotic one. Zero
        # means none, which is what the model-map await below needs it to mean.
        reached = 0
        skipped = 0
        try:
            # Read once per restore (see _transcripts_may_be_remote_only).
            preserve_remote_only = _transcripts_may_be_remote_only()
            # INSIDE the carry guard. It is an await like any other, so a shutdown
            # can land on it with the seed read and not one key looked at -- and
            # outside the guard that exit carried nothing, which is the difference
            # between the next flush merging and pruning.
            kiro_model_map = await asyncio.to_thread(_build_kiro_model_map)
            # main's restore budget plan, kept.
            keys, looped = await asyncio.to_thread(_open_slot_restore_plan, conv_log, keys)
            for reached, raw in enumerate(keys):
                key = _sanitize_open_slot_key(raw)
                if key is None or key in state._slots:
                    continue
                # Same budget as the inline driver, with the pin read off the loop.
                if _over_restore_budget(state, key, looped):
                    try:
                        pinned = await asyncio.to_thread(_kept_past_budget, conv_log, key)
                    except Exception:
                        logger.debug(
                            "restore_open_slots: pin read failed for %s", key, exc_info=True
                        )
                        pinned = True
                    if not pinned:
                        skipped += 1
                        await asyncio.sleep(0)
                        continue
                try:
                    started = time.time()
                    (
                        meta,
                        readable,
                        messages,
                        model_map,
                        member_identity,
                        agent,
                        effort_marker,
                        tail,
                    ) = await asyncio.to_thread(
                        _prefetch_open_slot_inputs,
                        conv_log,
                        slot_transcript_key(key),
                        kiro_model_map=kiro_model_map,
                        lazy=_may_restore_as_row(restored, key, looped),
                    )
                    restored += _apply_restored_open_slot(
                        state,
                        key,
                        meta=meta,
                        readable=readable,
                        messages=messages,
                        model_map=model_map,
                        member_identity=member_identity,
                        agent=agent,
                        effort_marker=effort_marker,
                        unrestored=unrestored,
                        # Opts into the post-hop re-checks (close tombstone +
                        # deletion): this driver's read ran in a worker thread, so
                        # its answers can have gone stale.
                        conv_log=conv_log,
                        started=started,
                        preserve_remote_only=preserve_remote_only,
                        tail=tail,
                        dropped=dropped,
                        # Collect the tab-id backfill this slot mints instead of
                        # firing it off the loop; drained below before we return.
                        pending_backfills=pending_backfills,
                    )
                except Exception:
                    logger.debug("restore_open_slots: rehydrate failed for %s", key, exc_info=True)
                    unrestored.add(key)
                    dropped.add(key)
                # Same recovery as the inline driver, with the spool read awaited:
                # this driver runs on the loop, where a scan would stall the gateway.
                _recovered_slot = state._slots.get(key)
                # A sidebar row recovers its claims when its window loads.
                if _recovered_slot is not None and not _recovered_slot.window_pending:
                    await _recover_mcp_app_claims_async(_recovered_slot)
                # sleep(0) yields to the ready queue without adding wall-clock delay.
                # Reached on EVERY outcome, including a failing tab (see the
                # generator's note) — and still needed with the reads offloaded,
                # because the apply half above runs here.
                await asyncio.sleep(0)
        except BaseException:
            # Cancelled (or aborted) mid-restore: every key from the one in hand
            # onward is UNREAD. ``BaseException`` because asyncio.CancelledError is
            # not an Exception, and cancellation is precisely the case the latch in
            # the finally below gets wrong -- it tells the persist writers ``_slots``
            # is authoritative when this loop never looked at most of the file.
            _carry_unread_slot_keys(keys[reached:], unrestored)
            raise
        # Every key in the seed has now been looked at.
        enumerated = True
        if restored:
            logger.info("Restored %d open tab(s) from open_slots.json", restored)
        _report_restore_budget(state, skipped)
        # Off the loop: the per-key store read that resolves which log to append to
        # lists a directory per key (see emit.slot_previous_store), which is exactly
        # the blocking work the rest of this driver exists to hoist.
        await asyncio.to_thread(
            _record_unrestored_open_tabs,
            state,
            listed=len(keys),
            restored=restored,
            kept=dropped,
        )
        # Commit the tab-id backfills this restore minted BEFORE returning. Each
        # is an os.replace of a session file; left in flight (the fire-and-forget
        # off-loop helper), it would still be mid-replace when a caller reads the
        # same transcript on the loop right after restore, and on Windows that
        # open() is a sharing violation the on-loop read cannot wait out, so the
        # read re-raises PermissionError (the intermittent dropped-tab / read
        # error on restart). Awaited one at a time off the loop so the
        # flock/os.close/os.replace stay off the event-loop thread.
        #
        # Through ``update_metadata_if(require_existing=True)``, NOT the plain
        # upserting ``update_metadata``: deferring every backfill to here widens
        # the gap between a tab restoring and its write landing, and a
        # ``DELETE /api/sessions/{key}`` can fall inside it. The checked variant
        # re-reads existence UNDER the write lock, so a session unlinked during
        # the restore window is left deleted instead of being recreated as a
        # metadata-only transcript the next restore reopens as an empty tab. The
        # guard always proceeds; existence is the only gate, and the minted
        # tab_id has no on-disk siblings so a backfill that is correctly skipped
        # loses nothing. Best-effort: a backfill is metadata already reflected in
        # the restored slot, so a lock timeout / I/O error / a skipped (deleted)
        # write is logged, not raised. Mirrors restore_recent_sessions_async.
        for _bk_key, _bk_fields in pending_backfills:
            try:
                await asyncio.to_thread(
                    conv_log.update_metadata_if,
                    _bk_key,
                    _bk_fields,
                    lambda _meta: True,
                    require_existing=True,
                )
            except Exception:  # noqa: BLE001 - best-effort metadata backfill
                logger.warning(
                    "restore_open_slots: tab-id backfill failed for %s",
                    _bk_key,
                    exc_info=True,
                )
    finally:
        # Always clear, even if a rehydrate raises — a stuck flag would silently
        # disable open-tab persistence for the rest of the process's life.
        state.restoring_open_slots = False
        # ``_slots`` is the authoritative open-tab set only once this restore has
        # ENUMERATED the whole seed and the seed was knowable in the first place.
        # Both halves are necessary and neither implies the other: a cancellation
        # during the snapshot await leaves a knowable-by-default state with nothing
        # read, and an unreadable registry leaves a loop that enumerated an empty
        # list. Either way the writers stay in merge mode, where they cannot prune --
        # see DashboardState._persist_open_slots.
        state.open_slots_restored = enumerated and bool(getattr(state, "open_slots_knowable", True))
    return restored


async def _recover_mcp_app_claims_async(slot: _ChatSlot) -> None:
    """:func:`_recover_mcp_app_claims`, with the spool read off the event loop.

    Same prefetch-then-apply split the async restore drivers already use for
    their transcript reads: the scan runs on a worker thread, the flagging is
    pure memory and stays here.
    """
    _reconcile_mcp_app_claims(
        slot, await asyncio.to_thread(_read_mcp_app_claims, effective_session_key(slot))
    )


def _restore_recent_sessions_steps(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> "Iterator[int]":
    """Drive :func:`restore_recent_sessions` one session at a time.

    Generator so a plain synchronous caller can spin through it. The event-loop
    path does NOT drive this one either: it needs ``list_sessions()`` and each
    per-session read hoisted into a worker thread, which a synchronous generator
    cannot express, so :func:`restore_recent_sessions_async` runs its own
    prefetch-then-apply loop over the same shared helpers. This path restores
    every folder'd/pinned session regardless of the mtime window, so it can be
    just as slow as the open-tab restore — measured at 13.6s for 76 sessions.
    """
    if not state.conversation_log:
        return
    conv_log = state.conversation_log
    cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
    restored = 0

    kiro_model_map = _build_kiro_model_map()
    _restore_cfg = _load_restore_cfg()
    for s in conv_log.list_sessions():
        key = s.get("key", "")
        slot_name = _recent_session_slot_name(key)
        if slot_name is None or slot_name in state._slots:
            continue
        meta, messages, _member_id, agent, effort_marker = _prefetch_recent_session(
            conv_log, key, s, folders_only=folders_only, cutoff=cutoff
        )
        if meta is None or messages is None:
            continue
        _apply_recent_session(
            state,
            key,
            slot_name,
            s,
            meta,
            messages,
            conv_log=conv_log,
            kiro_model_map=kiro_model_map,
            restore_cfg=_restore_cfg,
            member_identity=_member_id,
            agent=agent,
            effort_marker=effort_marker,
        )
        restored += 1
        # Recover an app flag whose claim outlived its row, as the open-slots
        # driver does. This driver's reads are inline by construction.
        _recovered_slot = state._slots.get(slot_name)
        if _recovered_slot is not None:
            _recover_mcp_app_claims(_recovered_slot)
        # One yield point per restored session (see _restore_open_slots_steps).
        yield restored
    _sync_dashboard_slots(state)


def restore_recent_sessions(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> int:
    """Restore sessions as chat slots.

    Blocking: see :func:`restore_recent_sessions_async` for the startup path.
    """
    restored = 0
    for restored in _restore_recent_sessions_steps(
        state, window_minutes, folders_only=folders_only
    ):
        pass
    return restored


async def restore_recent_sessions_async(
    state: DashboardState, window_minutes: int = 30, *, folders_only: bool = False
) -> int:
    """:func:`restore_recent_sessions`, with the disk reads off the loop.

    Same rationale as :func:`restore_open_slots_async` — keeps the stall-watchdog
    heartbeat alive while a large restore proceeds, and holds
    ``restoring_open_slots`` so an interleaved flush cannot snapshot a partial
    slot set (this path adds slots to the same sidebar).

    Everything blocking is hoisted into ``asyncio.to_thread``: ``list_sessions()``
    (which globs + stats + reads the first line of EVERY session file), the
    agent→model map, the config load, and each candidate's metadata + chained
    transcript walk. Only :func:`_apply_recent_session` stays on the loop,
    because slot construction is loop-affine for the reasons
    :func:`rehydrate_slot_from_history_async` documents.

    Yielding per session is kept alongside the offload: the apply half still runs
    here, so the loop must get a turn between sessions.
    """
    if not state.conversation_log:
        return 0
    restored = 0
    state.restoring_open_slots = True
    # Tab-id backfills this restore mints. Drained (its os.replace committed)
    # before we return, so a caller's immediate on-loop transcript read cannot
    # race a backfill still in flight on Windows. See _apply_recent_session's
    # pending_backfills note, and the open-slot driver's equivalent drain.
    pending_backfills: list[tuple[str, dict]] = []
    try:
        conv_log = state.conversation_log
        cutoff = time.time() - (window_minutes * 60) if window_minutes > 0 else None
        sessions = await asyncio.to_thread(conv_log.list_sessions)
        kiro_model_map = await asyncio.to_thread(_build_kiro_model_map)
        _restore_cfg = await asyncio.to_thread(_load_restore_cfg)
        for s in sessions:
            key = s.get("key", "")
            slot_name = _recent_session_slot_name(key)
            if slot_name is None or slot_name in state._slots:
                continue
            started = time.time()
            meta, messages, _member_id, agent, effort_marker = await asyncio.to_thread(
                _prefetch_recent_session,
                conv_log,
                key,
                s,
                folders_only=folders_only,
                cutoff=cutoff,
            )
            if meta is None or messages is None:
                continue
            # POST-HOP REVALIDATION. Before the reads were offloaded, each item's
            # check-then-apply ran atomically on the loop — nothing could get
            # between them. The read window is now seconds wide, so the pre-hop
            # answers are stale and BOTH must be asked again here, on the loop,
            # before anything mutates slot state:
            #
            #   * the slot may now EXIST (a resume, a nudge, or the user opening
            #     the tab published it while the transcript loaded).
            #     ``_apply_recent_session`` calls ``get_or_create_slot``, which
            #     returns that live slot, and the replay below would then append
            #     500 on-disk messages onto state that already has them and
            #     persist the duplicates.
            #   * the tab may have been CLOSED with ✕. The close pops the slot and
            #     records a tombstone synchronously, but persists the ``closed``
            #     flag only after its own awaits — so the metadata just read still
            #     says open, and rebuilding from it would resurrect a dismissed
            #     tab and then fire a nudge turn into it. The tombstone is the
            #     authoritative signal in that window.
            #
            # ``rehydrate_slot_from_history_async`` guards the same two windows
            # after its own hop, and the open-tab driver inherits the ``_slots``
            # half from ``_rehydrate_slot_from_history``'s internal re-check. This
            # is the one converted surface that has to spell both out.
            if slot_name in state._slots:
                logger.debug(
                    "Restore skipped: session %s was published while its " "transcript loaded",
                    slot_name,
                )
                continue
            if slot_closed_since(state, slot_name, started):
                logger.info(
                    "Restore abandoned: session %s was closed while its " "transcript loaded",
                    slot_name,
                )
                continue
            # Third window: the session may have been permanently DELETED (or
            # deleted and recreated) during the read. Synchronous and last, so no
            # await separates it from the build it gates.
            gone = _deletion_during_read(conv_log, key, meta, messages)
            if gone is not None:
                logger.info(
                    "Restore abandoned: session %s was %s while its transcript "
                    "loaded; refusing to restore a slot whose flush would "
                    "rewrite it",
                    slot_name,
                    gone,
                )
                continue
            _apply_recent_session(
                state,
                key,
                slot_name,
                s,
                meta,
                messages,
                conv_log=conv_log,
                kiro_model_map=kiro_model_map,
                restore_cfg=_restore_cfg,
                member_identity=_member_id,
                agent=agent,
                effort_marker=effort_marker,
                pending_backfills=pending_backfills,
            )
            restored += 1
            # Same recovery, with the spool read awaited: this driver is
            # loop-affine, so a scan here would stall the gateway.
            _recovered_slot = state._slots.get(slot_name)
            if _recovered_slot is not None:
                await _recover_mcp_app_claims_async(_recovered_slot)
            await asyncio.sleep(0)
        _sync_dashboard_slots(state)
        # Commit the tab-id backfills this restore minted BEFORE returning. Each
        # is an os.replace of a session file; left in flight (the fire-and-forget
        # off-loop helper), it would still be mid-replace when a caller reads the
        # same transcript on the loop right after restore, and on Windows that
        # open() is a sharing violation the on-loop read cannot wait out, so the
        # read re-raises PermissionError. Awaited one at a time off the loop so
        # the flock/os.close/os.replace stay off the event-loop thread.
        #
        # Through ``update_metadata_if(require_existing=True)``, NOT the plain
        # upserting ``update_metadata``: deferring every backfill to here widens
        # the gap between a tab restoring and its write landing, and a
        # ``DELETE /api/sessions/{key}`` can fall inside it. The checked variant
        # re-reads existence UNDER the write lock, so a session unlinked during
        # the restore window is left deleted instead of being recreated as a
        # metadata-only transcript the next recent-session restore reopens as an
        # empty tab. The guard always proceeds; existence is the only gate, and
        # the minted tab_id has no on-disk siblings so a backfill that is
        # correctly skipped loses nothing. Best-effort: a backfill is metadata
        # already reflected in the restored slot, so a lock timeout / I/O error /
        # a skipped (deleted) write is logged, not raised.
        for _bk_key, _bk_fields in pending_backfills:
            try:
                await asyncio.to_thread(
                    conv_log.update_metadata_if,
                    _bk_key,
                    _bk_fields,
                    lambda _meta: True,
                    require_existing=True,
                )
            except Exception:  # noqa: BLE001 - best-effort metadata backfill
                logger.warning(
                    "restore_recent_sessions: tab-id backfill failed for %s",
                    _bk_key,
                    exc_info=True,
                )
    finally:
        state.restoring_open_slots = False
    return restored
