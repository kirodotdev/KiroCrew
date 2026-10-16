"""``POST /api/chat/slots/resume-all`` -- press Resume on every interrupted idle
session at once.

A gateway restart, a laptop sleep or a run of backend errors leaves many
sessions in the same state: the last turn ended without a reply, the composer
offers Resume, and the sidebar flags the row as interrupted. Recovering them
one tab at a time is the chore this endpoint removes. It is the bulk form of
``POST /api/chat/slots/{slot}/continue``: the same ``continue_slot_turn`` runs
for each candidate, so every refusal that endpoint knows (a running turn, a
queued message, a pending approval, running sub-agents, a repeated start
failure) is honoured here per slot and reported back by code instead of
aborting the whole run.

Candidates are the slots the sidebar itself counts: idle (``slot.turn_running``
is False, the fact the projection serializes as ``running``), ``is_turn_interrupted`` on the transcript, no live sub-agents, not
under construction and not a relay archive -- the facts the slots projection
feeds into the row's ``interrupted`` and ``subagents_running`` fields, so the
count on the button and the ``total`` the server answers are the same number.

Owner-only. ``require_owner_dashboard_request`` refuses app tokens AND
non-owner dashboard sessions: a Slack-allowlisted ``!dashboard`` user carries
an empty app claim and would otherwise be able to dispatch an agent turn on
every slot on the install. The single-slot Continue route gates per slot
through ``deny_app_slot_access``; a route that touches every slot is a
different blast radius and takes the owner gate the other all-slot surfaces
(the dashboard card read, the worktree create) already take.

The press is not one long request. The handler collects the candidates,
answers ``202 {ok, total, run_id}`` at once and runs the waves as a
gateway-owned task held in ``state._background_tasks``. One run at a time: a
press while a run is in flight answers ``409 resume_all_running`` and
dispatches nothing. The outcome reaches owner sockets as one
``resume_all_done`` frame (``broadcast_ws_owners``); the per-slot changes
reach every client through the ordinary slots push as each slot starts.

Launches go out in waves of ``RESUME_ALL_WAVE_SIZE`` with a fixed
``RESUME_ALL_STAGGER_SECS`` pause between waves. Each resumed slot starts an
agent process, and a burst of forty starts at once loses lanes that the same
forty, four at a time, do not. A wave runs as its own tracked task behind
``asyncio.shield``: a cancellation of the run task itself stops it BETWEEN
waves, never inside one, so no slot is left with a queued recovery entry and
no runner.

The shield does not cover gateway shutdown. Nothing cancels the run task on
its own at shutdown; loop teardown cancels every task, the wave included. A
slot whose dispatch was in flight then keeps its queued recovery entry in
memory only, and a restart reloads the queue.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard import chat_handlers as _ch
from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.relay_archive import is_relay_archive
from kiro_crew.dashboard.state import _log_task_exception, is_turn_interrupted

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: How many continuations are dispatched concurrently in one wave. Four is the
#: largest batch of simultaneous agent starts this gateway has booted without
#: hitting the session-start cap (see the module docstring).
RESUME_ALL_WAVE_SIZE = 4
#: Fixed pause between waves. It paces the starts; it does not wait for any
#: signal from the previous wave.
RESUME_ALL_STAGGER_SECS = 1.5

#: The ``via`` recorded on each slot's ``dashboard_continue`` audit line, so a
#: resume that came from this button can be told from a per-slot press.
RESUME_ALL_VIA = "resume_all"

#: The 409 code a press gets while an earlier press's run is still going.
RESUME_ALL_RUNNING_CODE = "resume_all_running"
#: The owner-socket frame that carries a finished run's outcome.
RESUME_ALL_DONE_FRAME = "resume_all_done"

#: The ``DashboardState`` attribute holding ``(task, run_id, total)`` for the run in
#: flight, if any. Read and written only on the event loop with no await between
#: the check and the set, so two presses cannot both start a run.
_RUN_ATTR = "_resume_all_run"


def collect_interrupted_idle(state: DashboardState) -> list[tuple[str, _ChatSlot]]:
    """The ``(name, slot)`` pairs the button acts on, in registration order.

    The same facts the slots projection serializes for the row: a slot under
    construction is not in the payload at all, ``running`` (``turn_running``)
    and ``subagents_running`` must both be false, and ``interrupted`` is
    ``is_turn_interrupted`` on the transcript. Relay archives are skipped
    first: they never run, and the sidebar leaves remote rows out of its count.
    """
    subs = getattr(state, "subagents", None)
    under_construction = getattr(state, "_slots_under_construction", None) or ()
    picked: list[tuple[str, _ChatSlot]] = []
    # Snapshot: ``continue_slot_turn`` awaits, so the live dict must not be
    # iterated across the dispatch below.
    for name, slot in list(state._slots.items()):
        if slot.key in under_construction or is_relay_archive(slot):
            continue
        if slot.turn_running:
            continue
        if subs and subs.running_agents_for(effective_session_key(slot)):
            continue
        if not is_turn_interrupted(slot.messages):
            continue
        picked.append((name, slot))
    return picked


async def _pause(secs: float) -> None:
    """The inter-wave pause, one seam so tests can observe the stagger."""
    await asyncio.sleep(secs)


def _track(state: DashboardState, task: asyncio.Task[Any]) -> None:
    """Hold *task* in the state's background set until it finishes.

    The three-callback shape every background task here takes: a strong
    reference, a discard on completion, and ``_log_task_exception`` so an
    exception that escapes the task is logged (redacted) instead of surfacing
    only as an unretrieved-exception warning.
    """
    task.add_done_callback(_log_task_exception)
    background = getattr(state, "_background_tasks", None)
    if background is None:
        return
    background.add(task)
    task.add_done_callback(background.discard)


def _new_outcome(total: int, run_id: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "total": total,
        "resumed": [],
        "skipped": [],
        "failed": [],
    }
    if run_id:
        out["run_id"] = run_id
    return out


async def _resume_waves(
    state: DashboardState,
    candidates: list[tuple[str, _ChatSlot]],
    claims: Mapping[str, Any],
    outcome: dict[str, Any],
    waves: list[asyncio.Task[None]] | None = None,
) -> None:
    """Continue *candidates* wave by wave, recording each slot in *outcome*.

    Each wave's task is appended to *waves* when given, so a cancelled caller
    can tell whether a wave is still finishing.
    """
    resumed: list[str] = outcome["resumed"]
    skipped: list[dict[str, str]] = outcome["skipped"]
    failed: list[dict[str, str]] = outcome["failed"]

    async def _one(name: str, slot: _ChatSlot) -> None:
        try:
            # require_interrupted: a candidate whose turn ended cleanly between
            # the collect and its wave is refused as ``turn_not_failed``, so a
            # finished session never gets the carry-on body nobody asked for.
            await _ch.continue_slot_turn(
                state,
                slot,
                directive_user_origin=True,
                via=RESUME_ALL_VIA,
                require_interrupted=True,
                replaced=lambda: _ch._slot_replaced_while_queued(
                    state, slot, name, claims, "chat.slots_resume_all"
                ),
            )
        except SlotContinueRefusal as exc:
            skipped.append({"slot": name, "code": exc.code})
            return
        except Exception as exc:
            logger.warning("resume-all: continue failed for slot %s", name, exc_info=True)
            failed.append({"slot": name, "error": type(exc).__name__})
            return
        resumed.append(name)

    async def _wave(batch: list[tuple[str, _ChatSlot]]) -> None:
        await asyncio.gather(*(_one(name, slot) for name, slot in batch))

    for start in range(0, len(candidates), RESUME_ALL_WAVE_SIZE):
        if start:
            await _pause(RESUME_ALL_STAGGER_SECS)
        # The wave is its own tracked task behind a shield: a cancellation of
        # the run lands here and stops the loop, but the wave already started
        # runs to its end, so a slot whose recovery entry is queued always gets
        # its dispatch. Cancellation can only stop the run BETWEEN waves.
        wave = asyncio.ensure_future(_wave(candidates[start : start + RESUME_ALL_WAVE_SIZE]))
        _track(state, wave)
        if waves is not None:
            waves.append(wave)
        await asyncio.shield(wave)

    if candidates:
        logger.info(
            "resume-all: %d interrupted idle slot(s): resumed %d, skipped %d, failed %d",
            len(candidates),
            len(resumed),
            len(skipped),
            len(failed),
        )


async def resume_all_interrupted(
    state: DashboardState, request: web.Request | Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Continue every interrupted idle slot; the outcome, never an exception.

    ``resumed`` lists the slot keys whose continuation was queued and
    dispatched. ``skipped`` carries ``{slot, code}`` for every
    :class:`SlotContinueRefusal` -- the slot was a candidate when collected but
    ``continue_slot_turn`` found a reason not to (its turn finished meanwhile, a
    message queued meanwhile, a pending approval, running sub-agents, two failed
    starts in a row, or a same-name re-registration during the lock wait).
    ``failed`` carries ``{slot, error}`` with the exception's class name for
    anything else; the other slots in the wave are unaffected. *request* is the
    caller's request (or a mapping with its ``app`` claim), passed to the same
    replaced-while-queued probe the single-slot route uses.
    """
    candidates = collect_interrupted_idle(state)
    outcome = _new_outcome(len(candidates))
    await _resume_waves(state, candidates, _claims(request), outcome)
    return outcome


def _claims(request: web.Request | Mapping[str, Any] | None) -> dict[str, Any]:
    """The caller's ``app`` claim as a plain mapping the background run can keep."""
    return {"app": request.get("app", "") if request is not None else ""}


def _broadcast_outcome(state: DashboardState, outcome: dict[str, Any]) -> None:
    frame = {k: list(v) if isinstance(v, list) else v for k, v in outcome.items()}
    try:
        state.broadcast_ws_owners(RESUME_ALL_DONE_FRAME, frame)
    except Exception:
        logger.warning(
            "resume-all: outcome push failed (run %s)", outcome.get("run_id"), exc_info=True
        )


async def _run_in_background(
    state: DashboardState,
    candidates: list[tuple[str, _ChatSlot]],
    claims: Mapping[str, Any],
    outcome: dict[str, Any],
) -> None:
    """The run the handler starts: the waves, then one outcome frame.

    Every way the run ends sends exactly one frame, from the ``finally``:

    - A cancellation of the run task stops it between waves. The frame carries
      ``cancelled: true`` and goes out once the wave that was in flight has
      finished, so it reports what that wave did, or at once when the run was
      cancelled during a pause. This does not cover gateway shutdown, where
      loop teardown cancels the wave too and the sockets are closing (see the
      module docstring).
    - An exception that escapes the waves outside the per-slot work marks the
      run ``ok: false`` and lists every candidate not yet recorded as failed
      with that exception's class name. The exception is re-raised for
      ``_log_task_exception``.

    A tab waiting on the run is never left pending.
    """
    waves: list[asyncio.Task[None]] = []
    deferred = False
    try:
        await _resume_waves(state, candidates, claims, outcome, waves)
    except asyncio.CancelledError:
        outcome["cancelled"] = True
        finishing = [w for w in waves if not w.done()]
        if finishing:
            finishing[-1].add_done_callback(lambda _w: _broadcast_outcome(state, outcome))
            deferred = True
        raise
    except Exception as exc:
        outcome["ok"] = False
        recorded = set(outcome["resumed"])
        recorded.update(entry["slot"] for entry in outcome["skipped"])
        recorded.update(entry["slot"] for entry in outcome["failed"])
        outcome["failed"].extend(
            {"slot": name, "error": type(exc).__name__}
            for name, _slot in candidates
            if name not in recorded
        )
        raise
    finally:
        if not deferred:
            _broadcast_outcome(state, outcome)


def _clear_run(state: DashboardState, task: asyncio.Task[Any]) -> None:
    current = getattr(state, _RUN_ATTR, None)
    if current is not None and current[0] is task:
        setattr(state, _RUN_ATTR, None)


def start_resume_all(
    state: DashboardState, claims: Mapping[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Start a run, or refuse when one is in flight. ``(status, body)`` for the route.

    Synchronous end to end: the in-flight check, the collect and the task
    registration happen with no await between them, so of two presses landing
    together exactly one starts a run and the other gets the 409.
    """
    current = getattr(state, _RUN_ATTR, None)
    if current is not None and not current[0].done():
        return 409, {
            "error": "a resume-all run is already in progress",
            "code": RESUME_ALL_RUNNING_CODE,
            "run_id": current[1],
            "total": current[2],
        }
    candidates = collect_interrupted_idle(state)
    if not candidates:
        return 200, {"ok": True, "total": 0, "run_id": ""}
    run_id = uuid.uuid4().hex[:12]
    outcome = _new_outcome(len(candidates), run_id)
    task = asyncio.ensure_future(_run_in_background(state, candidates, claims, outcome))
    _track(state, task)
    setattr(state, _RUN_ATTR, (task, run_id, len(candidates)))
    task.add_done_callback(lambda t: _clear_run(state, t))
    return 202, {"ok": True, "total": len(candidates), "run_id": run_id}


async def api_chat_slots_resume_all(request: web.Request) -> web.Response:
    """POST /api/chat/slots/resume-all -- Resume every interrupted idle session.

    No body. Owner-only (see the module docstring for why the per-slot app
    check is not enough here). Answers ``202 {ok, total, run_id}`` as soon as
    the run is started; the outcome
    ``{ok, run_id, total, resumed: [key], skipped: [{slot, code}], failed: [{slot, error}]}``
    follows as a ``resume_all_done`` owner frame. Nothing to resume answers
    ``200 {ok, total: 0, run_id: ""}`` and starts nothing. A press while a run is
    in flight answers ``409 resume_all_running`` with that run's id and total.
    """
    denied = await require_owner_dashboard_request(request, "chat.slots_resume_all")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    status, body = start_resume_all(state, _claims(request))
    return web.json_response(body, status=status)
