"""Ultra Mode "Pause all" / "Resume all" for one root and its whole crew.

``POST /api/chat/slots/{slot}/crew-workers/pause`` stops the root's turn and every
running crew member's turn at every depth with the Stop button's own mechanism
(``stop_slot_turn``), leaves a member that waits on the person (a tool approval or
an open question card) as it is, and records the pause (``kiro_crew.crew_pause``):
while it stands the root and the stopped members start no turn (their queued
messages stay queued, the root's patrol wakes are refused as busy) and no one in
the crew may open a new session. The root's chat gets one notice row.

``POST /api/chat/slots/{slot}/crew-workers/resume`` drops the record and hands
each member the pause stopped back with the Continue button's own mechanism
(``continue_slot_turn``), or drains what queued for it meanwhile. Only what the
pause stopped is continued.

Both take the gate every other crew-workers route takes: app tokens are answered
404 and the caller must be the dashboard owner. Neither reaches a session outside
the named root's own tree.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew import crew_pause
from kiro_crew.dashboard.handlers.crew_workers import (
    _bad,
    _gate,
    _live_root,
    _not_root,
    _stamped_rows,
    _tree_workers,
)

logger = logging.getLogger(__name__)

#: The ``meta["kind"]`` of the root's "You paused the crew" row. A wire value the
#: dashboard keys its localized card on (``CrewPausedNoticeCard.tsx``).
CREW_PAUSED_NOTICE_KIND = "crew_paused_notice"

#: Fallback text for readers that cannot localize (channel mirrors, SSE).
_NOTICE_TEXT = "You paused the crew. {n} workers stopped. Resume from the Board."


def _waits_on_person(row: Mapping[str, Any]) -> bool:
    return bool(row.get("pending_approval") or row.get("needs_input"))


def _is_running(row: Mapping[str, Any]) -> bool:
    return bool(row.get("running") or row.get("linked_turn_running"))


#: ``stop_slot_turn`` answers that mean the turn did NOT stop: nothing ran, or the
#: session's automatic compaction declined the cooperative cancel. "no active
#: turn" is not among them: a turn still being prepared answers it, and the
#: Stop it recorded aborts that turn before dispatch, so Resume all owes it its
#: Continue.
_NOT_STOPPED = frozenset({"not running", "compacting"})


async def _stop(state: Any, key: str) -> bool:
    """Stop *key*'s turn as a first Stop press would. True only when a turn stopped."""
    from kiro_crew.dashboard import chat_handlers

    slot = state.get_slot(key)
    if slot is None:
        return False
    try:
        # Never a hard kill: a pause pressed twice is not a second decision, and a
        # hard kill would discard the queue the pause promises to keep.
        out = await chat_handlers.stop_slot_turn(
            state, slot, source="dashboard:crew_pause", escalate=False
        )
    except Exception:
        logger.warning("crew pause: stop failed for %s", key, exc_info=True)
        return False
    # A refused stop (``ok: False``, such as a cron run the pause may not cancel)
    # stopped nothing, so Resume all owes it no Continue.
    return out.get("ok") is not False and out.get("info") not in _NOT_STOPPED


#: One Pause all, Resume all or held member's own Continue at a time per root, so a
#: Resume still waiting on a stopped turn's teardown never interleaves with a newer
#: Pause all, nor a member's refused Continue with the Resume that would drop it.
_ROOT_LOCKS: dict[str, asyncio.Lock] = {}


def _root_lock(root: str) -> asyncio.Lock:
    return _ROOT_LOCKS.setdefault(root, asyncio.Lock())


async def api_crew_pause(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/crew-workers/pause -- pause the whole crew."""
    refused = await _gate(request, "crew_mode.pause")
    if refused is not None:
        return refused
    async with _root_lock(request.match_info.get("slot", "")):
        return await _pause(request)


async def _pause(request: web.Request) -> web.Response:
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    rows = _stamped_rows(state)
    kind, root_row = _live_root(rows, root)
    if kind != "crew" or root_row is None:
        return _not_root()
    members = _tree_workers(rows, root)
    tree = [str(row.get("key")) for row in members]
    targets = [
        str(row.get("key")) for row in members if _is_running(row) and not _waits_on_person(row)
    ]
    # The root is held for the whole pause; it is continued on resume only when
    # the pause stopped a turn of its own.
    # A root waiting on the person (an approval, a question card) is left as it
    # is, like a member in that state: stopping it would cancel the wait.
    own = [root] if _is_running(root_row) and not _waits_on_person(root_row) else []
    # What an earlier Pause all already owes Resume all: a stop below that finds
    # such a turn gone (it ended during the write) must not erase that obligation.
    await crew_pause.ensure_loaded()
    earlier = set((crew_pause.record(root) or {}).get("stopped") or [])
    # Recorded BEFORE the stops: a stopped turn's own end must already find its
    # queue held, or it would start the next queued message on its way out.
    await asyncio.to_thread(crew_pause.pause, root, tree, own + targets)
    asked = [*own, *targets]
    # Read again now the hold is published: a turn that started during the write
    # (a patrol wake, a send) is stopped too, and a member created meanwhile is held.
    rows = _stamped_rows(state)
    late_members = _tree_workers(rows, root)
    _kind, late_root = _live_root(rows, root)
    late = [
        str(row.get("key"))
        for row in [*late_members, *([late_root] if late_root is not None else [])]
        if str(row.get("key")) not in asked and _is_running(row) and not _waits_on_person(row)
    ]
    late_tree = [str(row.get("key")) for row in late_members]
    if late or set(late_tree) - set(tree):
        await asyncio.to_thread(crew_pause.pause, root, late_tree, late)
        targets = [*targets, *(key for key in late if key != root)]
        asked = [*asked, *late]
    results = await asyncio.gather(*(_stop(state, key) for key in asked))
    really = [key for key, ok in zip(asked, results, strict=True) if ok]
    # Only a turn that stopped is continued on resume. One that kept running (a
    # compaction declined the cancel) stays held when it ends, like the rest.
    if len(really) != len(asked):
        await asyncio.to_thread(crew_pause.settle_stopped, root, asked, really, earlier)
    workers_stopped = [key for key in really if key != root]
    slot = state.get_slot(root)
    if slot is not None:
        from kiro_crew.dashboard.state import append_and_surface

        await asyncio.to_thread(
            append_and_surface,
            state,
            slot,
            "notice",
            _NOTICE_TEXT.format(n=len(workers_stopped)),
            "msg msg-info",
            meta={"kind": CREW_PAUSED_NOTICE_KIND, "stopped": len(workers_stopped)},
        )
    state.push_slots_update()
    not_stopped = [key for key in targets if key not in really]
    _audit(root, "pause", f"stopped={len(workers_stopped)} not_stopped={len(not_stopped)}")
    return web.json_response(
        {"ok": True, "paused": True, "stopped": workers_stopped, "not_stopped": not_stopped}
    )


def _queue_is_restored(slot: Any) -> bool:
    """Whether every queued entry came from disk (a restart during the pause)."""
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    queue = list(getattr(slot, "_queue", None) or [])
    return bool(queue) and all(entry.get(RESTORED_QUEUE_KEY) for entry in queue)


async def memory_ready_for_drain(state: Any) -> bool:
    """Wait a turn's own grace for memory preparation; ``False`` when it is still not ready.

    While memory prepares, the queue drain parks instead of starting a card, and
    the drain that follows can drop that card as stale with no Continue. Resume
    all and a stopped member's own Continue wait here first, while the pause
    record still owes the member its hand-back, and change nothing when memory
    is still not ready; so the card starts or drops while the hand-back can still
    Continue it.
    """
    from kiro_crew.memory_startup import (
        MemoryStartupUnavailable,
        memory_startup_preparing,
        wait_for_memory_preparation,
    )

    task = getattr(state, "memory_startup_task", None)
    if task is None:
        # Startup publishes the task only after the listener is up: until then a
        # preparing startup refuses every turn, so nothing may be handed back yet.
        return not memory_startup_preparing()
    if task.done():
        return True
    try:
        await wait_for_memory_preparation(task)
    except MemoryStartupUnavailable:
        return False
    return True


async def drain_held_queue(state: Any, slot: Any, *, send_restored: bool = False) -> str:
    """Start *slot*'s next queued turn under its lock, as an idle drain does.

    Re-checked under the lock: a send or Continue that started a turn while the
    caller awaited must not get a second runner. With *send_restored* (a member
    the pause stopped), a queue wholly restored from disk (a restart during the
    pause) has its first card marked chosen, as a person's "run this next" does:
    the person's Resume is that choice. An idle drain never sends an unchosen
    restored card, and Continue refuses over one, so without this the stopped
    member would never run again. The mark outlives a drain that parks (memory
    still preparing): the drain that follows sends that card.

    Answers ``"drained"`` (a turn started), ``"held"`` (nothing started; the
    queue waits, a chosen card included) or ``"dropped"`` (the chosen card, or
    every entry, was judged stale and is gone, so nothing will send it).
    """
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_SEND_CHOSEN_KEY

    async with slot._lock:
        if (
            state.get_slot(slot.key) is not slot
            or slot.running
            or slot.linked_turn_running
            or slot._turn_admission_reserved
            or slot.is_closing
            or slot._stop_state != "idle"
        ):
            return "running"
        chosen = None
        if send_restored and _queue_is_restored(slot):
            chosen = slot._queue[0].get("id")
            slot._queue[0][RESTORED_SEND_CHOSEN_KEY] = True
        slot._turn_admission_reserved = True
        try:
            started = await chat_handlers._start_next_queued_turn(state, slot)
        finally:
            slot._turn_admission_reserved = False
    if started:
        return "drained"
    if chosen is not None and not any(e.get("id") == chosen for e in slot._queue or []):
        return "dropped"
    if not slot._queue:
        # Every entry was judged stale and dropped: nothing will start on its own.
        return "dropped"
    return "held"


#: ``_hand_back`` answers that leave nothing owed: the session was continued, its
#: queue drained or kept by its own hold, it had no turn to continue, or it is gone.
_HANDED_BACK = frozenset({"continued", "drained", "held", "idle", "closed"})

#: How long a stopped turn is given to finish tearing down before resume.
_SETTLE_TRIES = 25
_SETTLE_STEP_SECS = 0.2


def _rearm_withheld_synthesis(state: Any, slot: Any) -> None:
    """Re-ask a sub-agent synthesis the pause withheld, for an idle session.

    While paused the synthesis stays armed, and the recheck that found it held
    is spent; with no turn to end and nothing queued, nothing else fires it.
    """
    if not getattr(slot, "_pending_synthesis", False) or getattr(
        slot, "_synthesis_inflight", False
    ):
        return
    from kiro_crew.dashboard import chat_runner

    slot._synthesis_rechecks = 0
    chat_runner._arm_synthesis_recheck(state, slot)


async def _hand_back(
    state: Any, key: str, *, stopped: bool = True, started: int | None = None
) -> str:
    """Continue *key* as its own Continue button would; drain its queue if one waits.

    A key the pause did not stop (*stopped* False: the root when it was idle, a
    member that was idle or waiting on the person) only has its held queue
    drained: it gets no Continue it never had a turn for. A queue restored from
    disk is never drained on its own, so a stopped turn is continued over it, as
    the Continue button does. *started* is the slot's ``_turns_started`` read
    before the hold lifted: a turn this slot started since carries the thread
    on, so it gets no Continue and is not held again while it runs. A dispatch
    that ends before its turn starts does not count.
    """
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.state import load_window_off_loop

    slot = state.get_slot(key)
    if slot is None:
        return "closed"
    # A stopped turn may still be tearing down: give it a short while to settle,
    # so its continuation is not lost to a "still running" reading.
    for _ in range(_SETTLE_TRIES if stopped else 0):
        if not (slot.running or slot.linked_turn_running):
            break
        await asyncio.sleep(_SETTLE_STEP_SECS)
    if started is not None and slot._turns_started != started:
        return "drained"
    if slot.running or slot.linked_turn_running:
        return "running"
    # A restored tab loads its window off the loop before anything reads it.
    try:
        await load_window_off_loop(slot)
    except Exception:
        logger.warning("crew resume: window of %s could not be loaded", key, exc_info=True)
        return "history_unavailable"
    if state.get_slot(key) is not slot:
        return "closed"
    try:
        if slot.queue_depth and (stopped or not _queue_is_restored(slot)):
            # What queued while paused runs first; it carries the thread on.
            # A member the pause stopped sends its first restored card too.
            outcome = await drain_held_queue(state, slot, send_restored=stopped)
            if outcome != "dropped":
                return outcome
            # The restored card was stale and is gone: the stopped turn is
            # still owed, so Continue it (or stay paused when that is refused).
        if not stopped:
            _rearm_withheld_synthesis(state, slot)
            return "idle"
        await chat_handlers.continue_slot_turn(
            state, slot, directive_user_origin=True, allow_restored_queue=True
        )
        return "continued"
    except chat_handlers.SlotContinueRefusal as exc:
        return exc.code
    except Exception:
        logger.warning("crew resume: continue failed for %s", key, exc_info=True)
        return "error"


async def api_crew_resume(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/crew-workers/resume -- continue what the pause stopped.

    Answers for a paused root whose tab is closed too, so a pause can always be
    lifted: the record is dropped and only what is still open is handed back.
    """
    refused = await _gate(request, "crew_mode.resume")
    if refused is not None:
        return refused
    async with _root_lock(request.match_info.get("slot", "")):
        return await _resume(request)


async def _resume(request: web.Request) -> web.Response:
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    await crew_pause.ensure_loaded()
    kind, _root_row = _live_root(_stamped_rows(state), root)
    held = crew_pause.record(root)
    if kind != "crew" and held is None:
        return _not_root()
    if (held or {}).get("stopped") and not await memory_ready_for_drain(state):
        # A stopped member's card would only park: the pause stays as it is.
        return _bad("memory_preparing", "memory is still being prepared; try again", status=503)
    # Read while the hold stands: once it lifts, a stopped turn's own tail may
    # start the next turn before its hand-back looks. Every session the hold
    # covers is read, not just those stopped so far: a held turn reaching the
    # pause gate before the record drops joins ``stopped`` after this read.
    held_keys = {root}.union(*((held or {}).get(f) or [] for f in ("tree", "held", "stopped")))
    started = {
        key: slot._turns_started for key in held_keys if (slot := state.get_slot(key)) is not None
    }
    record = await asyncio.to_thread(crew_pause.resume, root)
    if record is None:
        return _bad("not_paused", "this crew is not paused", status=409)
    # Every session the record held or stopped that still has a tab, read from
    # the record itself: after a restart with the root's tab closed, or once a
    # member left the tree, the live stamps do not name them, and their held
    # queues must still drain.
    stopped = set(record.get("stopped") or [])
    keys = sorted(
        key
        for key in set(record.get("tree") or []) | stopped
        if key != root and state.get_slot(key) is not None
    )
    done: set[str] = set()

    async def _hand_back_one(key: str) -> str:
        outcome = await _hand_back(
            state, key, stopped=key in stopped, started=started.get(key) if key in stopped else None
        )
        if outcome in _HANDED_BACK:
            done.add(key)
            if key in stopped:
                # Stored as handed back at once: a restart now does not continue it twice.
                await asyncio.to_thread(crew_pause.forget_resuming, [key])
        return outcome

    try:
        outcomes = await asyncio.gather(*(_hand_back_one(key) for key in [root, *keys]))
    finally:
        # A stopped turn not handed back (still tearing down after the wait, its
        # Continue refused, or the route unwound first) is held again on its own,
        # in the same write that ends this Resume, so the next Resume all
        # continues it instead of losing it. Shielded: a second cancel must not
        # leave the gate ending these turns with nobody to continue them.
        root_pending = root in stopped and root not in done
        pending = [k for k in keys if k in stopped and k not in done]
        # Every write stores the whole state, so this last one decides what the
        # file holds, whatever the per-member writes above answered.
        saved = await asyncio.shield(
            asyncio.to_thread(
                partial(
                    crew_pause.forget_resuming,
                    sorted(stopped),
                    root=root,
                    pending=pending,
                    root_pending=root_pending,
                )
            )
        )
    # A refusal is reported as ``failed``.
    failed = [
        k
        for k, o in zip([root, *keys], outcomes, strict=True)
        if o not in _HANDED_BACK and o != "running" and (k in stopped)
    ]
    state.push_slots_update()
    _audit(root, "resume", f"handed_back={len(keys)} pending={len(pending)} saved={saved}")
    if not saved:
        # The crew runs again in memory; the file still owes its stopped members
        # until a later write stores this, so a restart before that holds them
        # again and the next Resume all can continue one a second time.
        return _bad(
            "pause_file_unsaved",
            "the crew resumed, but its pause file could not be saved; the next change retries it",
            status=500,
        )
    return web.json_response(
        {
            "ok": True,
            "paused": bool(pending or root_pending),
            "root": outcomes[0],
            "resumed": dict(zip(keys, outcomes[1:], strict=True)),
            "failed": failed,
        }
    )


def pause_view(root: str) -> dict[str, Any] | None:
    """What the worker list says about *root*'s pause: ``{stopped, paused_at}`` or ``None``.

    ``stopped`` names the workers only: the Board counts them, and the main chat's
    notice counts the same set. BLOCKING: the worker list calls it off-loop, so
    it may read the pause file when the startup preload has not yet.
    """
    crew_pause.load()
    rec = crew_pause.record(root)
    if rec is None:
        return None
    stopped = [k for k in rec.get("stopped") or [] if k != root]
    return {"stopped": stopped, "paused_at": rec.get("paused_at") or ""}


def _audit(root: str, action: str, detail: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=f"dashboard:{root}",
            operation=f"crew_mode.{action}",
            outcome="ok",
            source="dashboard",
            resources=detail,
        )
    except Exception:
        logger.debug("crew %s: audit failed", action, exc_info=True)
