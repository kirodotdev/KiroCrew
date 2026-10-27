"""Describing the end of a turn: the chunk generation stamped on every stream frame, the
attached sub-agent probes and synthesis verdict a turn's end consults, and ``chat_done_payload``.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from kiro_crew.dashboard.chat_utils_injected_rows import SUBAGENT_DELIVERY_KINDS
from kiro_crew.dashboard.chat_utils_slots import dashboard_slot_key, effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.chat_utils")


#: The generation every chat_chunk of this process carries (see chunk_generation).
_CHUNK_GENERATION: str = uuid.uuid4().hex[:8]


def chunk_generation() -> str:
    """The ``gen`` stamped on every chat_chunk frame and window row this process
    emits: one random value per gateway process.

    Chunk ``seq`` numbers are a per-slot counter that continues across turns,
    so a client's replay floor -- the newest seq its transcript holds -- orders
    every later chunk above it. The counter lives in memory and restarts with
    the gateway, so a floor from before a restart could sit above the new
    process's early seqs; the client compares ``gen`` first and replaces its
    floor when the generation changes, instead of dropping those chunks as
    replays. Not a secret and not an identity: it only says "same process".
    """
    return _CHUNK_GENERATION


def subagents_attached(
    state: DashboardState, slot: _ChatSlot | None, session_key: str, operation: str
) -> bool:
    """Whether sub-agent children are attached to *session_key*.

    True means an action that tears the session down, or dispatches into it,
    would discard a child's work. Every such caller shares THIS predicate: a
    second copy is how the probes diverge, and both callers must fail toward
    keeping a child's work.

    *slot* may be ``None`` when no tab displays the session: the in-flight
    delivery probe then reads as 0 (``getattr`` on ``None`` returns its
    default) and the two registry probes still decide.

    Three probes, none optional:

    * ``running_agents_for`` on the true session key. QUEUED children count too:
      a spawn that hit the concurrency/stagger gate is deliberately absent from
      ``_agents`` (see ``SubagentInfo.queued``), yet it WILL start on its own.
    * IN-FLIGHT RESULT DELIVERY: the last child can finish — emptying both
      probes — while its ``[Subagent completion event]`` injection is still
      landing, and that injection needs both the transcript order and the
      session it reports to.
    * Fail closed on a None running-probe: that is the probe FAILING, not a slot
      with no children, and mistaking the two is exactly the hazard this guard
      exists to prevent.

    The queued probe fails closed in TWO layers, because a raise is not how the
    common failure arrives: an unreadable task store is absorbed one layer down
    and answers ``taskq_bridge.UNKNOWN_PENDING`` rather than 0, so the ``except``
    below is what covers a probe that raises for any OTHER reason — a registry
    double without the count, an attribute gone. Neither layer may answer 0 for a
    queue nobody could read.

    A state with no ``subagents`` registry answers False — there is no runtime
    for a child to be attached to.

    The ``slot_subagents_running`` 409 names what each probe found
    (``chat_api.slot_guards._subagents_attached_detail``), so a probe added here
    needs a field there too.

    SYNCHRONOUS variant, for a caller with no event loop under it. The queued
    probe counts rows that live only in the task store, so it takes the SQLite
    connection: a caller ON the gateway loop takes
    :func:`subagents_attached_async` instead.
    """
    subs = getattr(state, "subagents", None)
    if subs is None:
        return False
    running = subs.running_agents_for(session_key)
    queued = 0
    if running is not None:
        try:
            queued = subs._queued_depth(session_key)
        except Exception:
            # An unreadable queue is unknown children, not zero children.
            logger.debug("%s: queued-depth probe failed", operation, exc_info=True)
            queued = 1
    return _attached_verdict(running, queued, slot)


async def subagents_attached_async(
    state: DashboardState, slot: _ChatSlot | None, session_key: str, operation: str
) -> bool:
    """:func:`subagents_attached` for a caller on the event loop.

    The same three probes with the same fail-closed rules; only WHERE the
    queued half runs differs. ``_queued_depth`` counts this parent's rows that
    live only in the store, so it takes the connection — and the store's writer
    thread holds that connection's lock across ``BEGIN IMMEDIATE``'s busy wait,
    so taking it here would stall every session's turn and the watchdog
    heartbeat behind one teardown probe. ``queued_count_for_async`` is the same
    count with the read on the writer thread.

    A manager double without the async sibling is asked synchronously — it
    models the pre-queue manager and has no store to block on — which is the
    probe ``slack.gateway._subagent_queued_count`` and
    ``handlers.messaging._spawn_on_loop`` already make.
    """
    subs = getattr(state, "subagents", None)
    if subs is None:
        return False
    running = subs.running_agents_for(session_key)
    if _attached_verdict(running, 0, slot):
        # Running, unknown, or still delivering: attached whatever the store
        # says, so the store read is not taken.
        return True
    try:
        queued = await _queued_depth_off_loop(subs, session_key)
    except Exception:
        # An unreadable queue is unknown children, not zero children.
        logger.debug("%s: queued-depth probe failed", operation, exc_info=True)
        queued = 1
    return _attached_verdict(running, queued, slot)


#: :func:`synthesis_fire_verdict` answers.
SYNTHESIS_CLEAR = "clear"
SYNTHESIS_HELD = "held"
SYNTHESIS_UNKNOWN = "unknown"


async def synthesis_fire_verdict(state: DashboardState, slot: _ChatSlot) -> str:
    """Whether the post-fan-out synthesis may fire for *slot* now.

    The ONE place the synthesis decision reads the task store (the arm in the
    gateway's completion path is in-memory only). The registry terms read the
    slot's real session key (:func:`effective_session_key`, which a channel- or
    cron-born tab does not spell ``dashboard:<slot>``):

    * a result waiting in the slot's own queue: a sign-in hold keeps the queue
      while still allowing a successor, and synthesis would then claim a
      result was processed before its turn ran;
    * a RUNNING child, or a result still being delivered to the slot;
    * the in-memory pending work ``has_pending_work_for`` adds: a spawn in the
      dispatch window, a run whose report still waits on its teardown, a live
      follow-up watcher;
    * a QUEUED child that lives only in the store: gate-deferred, waiting for a
      slot, or claimed and not registered.

    ``SYNTHESIS_UNKNOWN`` is a store nobody could read (or a probe that
    failed), told apart from children that are really waiting, because only
    the first can resolve without a further completion to re-trigger the
    check (the caller re-checks on a timer).
    """
    subs = getattr(state, "subagents", None)
    if subs is None:
        return SYNTHESIS_HELD
    if _slot_holds_delivery(slot):
        return SYNTHESIS_HELD
    key = effective_session_key(slot)
    try:
        running = subs.running_agents_for(key)
        if running is None:
            return SYNTHESIS_UNKNOWN
        if running:
            return SYNTHESIS_HELD
        if subs.has_in_memory_pending_work_for(key) is True:
            return SYNTHESIS_HELD
        queued = await subs.queued_count_or_none_async(key)
    except Exception:
        logger.debug("synthesis fire probe failed for slot %s", slot.key, exc_info=True)
        return SYNTHESIS_UNKNOWN
    if queued is None:
        return SYNTHESIS_UNKNOWN
    # Read the slot again after the store await: a child that finished during
    # it is now delivering to the slot or queued in it.
    if int(queued) > 0 or _slot_holds_delivery(slot):
        return SYNTHESIS_HELD
    return SYNTHESIS_CLEAR


def _slot_holds_delivery(slot: _ChatSlot) -> bool:
    """A sub-agent result is being delivered to *slot* or waits in its queue."""
    if getattr(slot, "_subagent_deliveries_inflight", 0):
        return True
    return any(
        entry.get("kind") in SUBAGENT_DELIVERY_KINDS
        for entry in getattr(slot, "_queue", None) or ()
    )


async def _queued_depth_off_loop(subs: Any, session_key: str) -> int:
    """This parent's queued-spawn count with its store half off the loop."""
    import inspect

    entry = getattr(subs, "queued_count_for_async", None)
    if inspect.iscoroutinefunction(entry):
        return int(await entry(session_key))
    return int(subs._queued_depth(session_key))


def _attached_verdict(running: Any, queued: int, slot: _ChatSlot | None) -> bool:
    """The verdict both entry points return, so the two cannot drift.

    *slot* may be ``None``: ``getattr`` on ``None`` reads the in-flight
    delivery probe as 0 and the two registry probes still decide.
    """
    inflight = getattr(slot, "_subagent_deliveries_inflight", 0)
    return bool(running is None or running or queued or inflight)


async def chat_done_payload(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    continuing: bool = False,
    queue_held: bool = False,
    ends_turn: bool = True,
) -> dict[str, Any]:
    """Describe whether a turn boundary actually hands the floor to the user.

    Slot snapshots are coalesced, so a sound decision cannot use stale
    child/plan state from the browser when this frame arrives. Read the same
    attached-child guard that protects session teardown, including queued spawns
    and results still being delivered. This is a notification hint only; it never
    changes dispatch, transcript finalization, or the slot's running state.

    ``turn`` / ``turn_gen`` name the turn this frame ENDS, and the dashboard
    records that turn as ended. A caller whose turn keeps running past the frame
    (the deferred ``/compact`` acknowledgement) passes ``ends_turn=False`` and
    the identity is omitted, so the frame cannot fence the live turn.

    A coroutine for one reason: that guard's queued half reads the task store,
    and every caller here is a turn-boundary frame on the gateway loop, so the
    read belongs on the store's writer thread
    (:func:`subagents_attached_async`).

    ``queue_held`` says the turn ending here held the queue (a sign-in,
    memory-preparation or setup failure): nothing drains it until the user's
    next send, so its entries are not work that continues.
    """
    # Capture before the queued-subagent probe yields: a successor can start
    # while that store read is in flight, but this frame still ends the turn
    # that called the helper.
    turn = slot._turn_generation
    turn_gen = chunk_generation()

    # Avoid a circular import: autonudge's slot lookup imports dashboard.state.
    from kiro_crew.autonudge import get_instance

    try:
        service = get_instance()
        loop = service.get_by_slot(slot.key) if service is not None else None
        workflows = getattr(state, "workflow_service", None)
        continuing = bool(
            continuing
            or slot._pending_synthesis
            or (slot.queue_depth and not queue_held and not slot._last_turn_auth_required)
            or await subagents_attached_async(
                state, slot, effective_session_key(slot), "completion_sound"
            )
            or (
                workflows is not None
                and workflows.registry.has_pending_work_for(effective_session_key(slot))
            )
            or (loop is not None and loop.active)
        )
    except Exception:
        # Unknown activity must not announce a finished conversation, but a
        # notification failure must never prevent the terminal frame itself.
        logger.warning("Completion activity unavailable for slot %s", slot.key, exc_info=True)
        continuing = True
    payload: dict[str, Any] = {"slot": slot.key}
    if ends_turn:
        payload["turn"] = turn
        payload["turn_gen"] = turn_gen
    payload["continuing"] = continuing
    payload["needs_input"] = bool(slot._question_pending)
    return payload


def wire_session_subagent_probe(state: DashboardState) -> None:
    """Hand ``SessionManager`` the sub-agent probe its RSS ceiling consults.

    The manager cannot see the dashboard's sub-agent registry or slots, so the
    predicate is built here, over :func:`subagents_attached_async`, and
    installed via ``set_subagent_probe``. The slot is resolved through
    :func:`dashboard_slot_key` (the same mapping the recycle notice uses); a
    session with no open tab passes ``None``, which the predicate accepts.

    A COROUTINE predicate: the RSS sweep that consults it runs on the gateway
    loop, and the queued half of the guard reads the task store. The manager
    accepts either shape and awaits an awaitable answer, so a sync probe (a
    test double, a build with no queue) still works.
    """

    async def _probe(session_key: str) -> bool:
        slot_key = dashboard_slot_key(session_key)
        slot = state.get_slot(slot_key) if slot_key else None
        return await subagents_attached_async(state, slot, session_key, "rss_recycle")

    state.sessions.set_subagent_probe(_probe)
