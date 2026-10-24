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
answers ``202 {ok, total, run_id}`` at once and runs the resumes as a
gateway-owned task held in ``state._background_tasks``. One run at a time: a
press while a run is in flight answers ``409 resume_all_running`` and
dispatches nothing. The outcome reaches owner sockets as one
``resume_all_done`` frame (``broadcast_ws_owners``); the per-slot changes
reach every client through the ordinary slots push as each slot starts.

Every candidate is dispatched at once, in one ``gather``, and this module sets
no pace of its own. Each resumed turn's cold start takes a permit from
``SessionManager._start_sem``, the queue ``agent.cold_start_concurrency``
sizes, and a start beyond its width waits there for a permit instead of being
refused.

Nothing cancels the run task on its own. Loop teardown at gateway shutdown
cancels it with every other task; a slot whose dispatch was in flight then
keeps its queued recovery entry in memory only, and a restart reloads the
queue.
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
from kiro_crew.dashboard.slot_projection import summary_rows
from kiro_crew.dashboard.state import (
    _log_task_exception,
    is_turn_interrupted,
    load_window_off_loop,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: The slot modes the chat page lists, mirroring the frontend's
#: ``isChatPageSurface`` (``website/src/utils/channelOrigin.ts``): an ordinary
#: chat (``""``) and the retired Autopilot mode it folds in.
CHAT_PAGE_MODES = frozenset({"", "orchestrator"})

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
        # The chat page's own sessions only, as the sidebar lists them
        # (``isChatPageSurface``): a member's DM thread or a sub-agent row lives
        # on another page, so the button never counts it.
        if (getattr(slot, "mode", "") or "") not in CHAT_PAGE_MODES:
            continue
        # A channel-path turn on the linked session is in flight too: the
        # projection reports that row as running, not interrupted.
        if slot.turn_running or getattr(slot, "linked_turn_running", False) is True:
            continue
        if subs and subs.running_agents_for(effective_session_key(slot)):
            continue
        # ``summary_rows``: the rows the sidebar reads, so a restored row whose
        # window is still pending answers from its tail and nothing is read from
        # disk on the loop here. Each candidate's window loads off the loop in
        # its own dispatch, before its Continue.
        if not is_turn_interrupted(summary_rows(slot)):
            continue
        picked.append((name, slot))
    return picked


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


def _new_outcome(total: int, run_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "total": total,
        "run_id": run_id,
        "resumed": [],
        "skipped": [],
        "failed": [],
    }


async def _resume_candidates(
    state: DashboardState,
    candidates: list[tuple[str, _ChatSlot]],
    claims: Mapping[str, Any],
    outcome: dict[str, Any],
) -> None:
    """Continue every candidate at once, recording each slot in *outcome*.

    One ``gather`` over the whole list: the cold starts the resumed turns lead
    to wait in ``SessionManager._start_sem`` (see the module docstring).

    ``resumed`` lists the slot keys whose continuation was queued and
    dispatched. ``skipped`` carries ``{slot, code}`` for every
    :class:`SlotContinueRefusal`: the slot was a candidate when collected but
    ``continue_slot_turn`` found a reason not to (its turn finished meanwhile, a
    message queued meanwhile, a pending approval, running sub-agents, two failed
    starts in a row, or a same-name re-registration during the lock wait).
    ``failed`` carries ``{slot, error}`` with the exception's class name for
    anything else; the other slots are unaffected. *claims* is the caller's
    ``app`` claim, passed to the same replaced-while-queued probe the
    single-slot route uses.
    """
    resumed: list[str] = outcome["resumed"]
    skipped: list[dict[str, str]] = outcome["skipped"]
    failed: list[dict[str, str]] = outcome["failed"]

    async def _one(name: str, slot: _ChatSlot) -> None:
        try:
            # A restored row's transcript is read on a worker thread, not by the
            # ``slot.messages`` reads inside ``continue_slot_turn``.
            await load_window_off_loop(slot)
            # require_interrupted: a candidate whose turn ended cleanly between
            # the collect and its dispatch is refused as ``turn_not_failed``, so
            # a finished session never gets the carry-on body nobody asked for.
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
            # Any other exception, a failed slots push after the dispatch
            # included, fails this slot only; the others carry on.
            logger.warning("resume-all: continue failed for slot %s", name, exc_info=True)
            failed.append({"slot": name, "error": type(exc).__name__})
            return
        resumed.append(name)

    await asyncio.gather(*(_one(name, slot) for name, slot in candidates))

    if candidates:
        logger.info(
            "resume-all: %d interrupted idle slot(s): resumed %d, skipped %d, failed %d",
            len(candidates),
            len(resumed),
            len(skipped),
            len(failed),
        )


def _claims(request: web.Request) -> dict[str, Any]:
    """The caller's ``app`` claim as a plain mapping the background run can keep."""
    return {"app": request.get("app", "")}


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
    """The run the handler starts: every candidate, then one outcome frame.

    Every way the run ends sends exactly one frame, from the ``finally``:

    - A cancellation of the run task (loop teardown at gateway shutdown, see
      the module docstring) marks the frame ``cancelled: true``; it reports the
      slots recorded before the cancellation landed.
    - An exception that escapes the per-slot work marks the run ``ok: false``
      and lists every candidate not yet recorded as failed with that
      exception's class name. The exception is re-raised for
      ``_log_task_exception``.

    A tab waiting on the run is never left pending.
    """
    try:
        await _resume_candidates(state, candidates, claims, outcome)
    except asyncio.CancelledError:
        outcome["cancelled"] = True
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
    # One literal status per answer, so the error-code contract can read that
    # the only error here (the 409) carries its ``code``.
    if status == 409:
        return web.json_response(
            {
                "error": body["error"],
                "code": RESUME_ALL_RUNNING_CODE,
                "run_id": body["run_id"],
                "total": body["total"],
            },
            status=409,
        )
    if status == 202:
        return web.json_response(body, status=202)
    return web.json_response(body)
