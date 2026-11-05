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
under construction, not a relay archive, and no Crew Mode worker whose root is
listed (the sidebar's ``crewModeView`` drop) -- the facts the slots projection
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

At most ``RESUME_ALL_MAX_RUNNING`` resumed turns run at once. A candidate's
Continue waits for a permit, takes it at its dispatch and hands it back when
the turn that dispatch started has ended, so the next candidate starts as an
earlier one finishes, in registration order. A turn that hands its slot to a
retry it queued for itself keeps the permit through that retry
(``BACKGROUND_START_TASKS``), so a recovery never runs beside one more resumed
turn than the bound allows. The run is over, and its outcome
frame goes out, once the last candidate has been dispatched or refused; up to
the bound's worth of turns may still be running then. A long turn, or one
parked on a tool approval, holds its place for as long as it runs, so the
run, and the 409 a later press gets while it is in flight, can last that
long too; the later press follows the run instead. This bounds the press,
not the gateway: a turn a person starts in another session never waits on it.
A global cap on attended turns at the dispatch every send and Resume reaches
is a design of its own, and replaces this bound once it lands.

Every resumed turn keeps the person's provenance (the press is the owner's)
but starts at BACKGROUND priority (``START_BACKGROUND_META_KEY`` on its queue
entry). The request answers 202 at once, so nobody waits on any one of those
starts, and a FOREGROUND claim would put each of them in the person-only start
reserve ahead of the owner's next typed message
(``docs/system-specs/modules/acp-client.md`` § Session-start gate).

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
from kiro_crew.dashboard.chat_delivery import BACKGROUND_START_TASKS, START_BACKGROUND_META_KEY
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

#: How many resumed turns one run lets run at once: the floor width of the
#: cold-start queue (``agent.cold_start_concurrency``'s ``auto`` minimum), so a
#: press over dozens of sessions never becomes dozens of concurrent turns and
#: their credit use. A candidate past the bound waits for an earlier turn to end.
RESUME_ALL_MAX_RUNNING = 4

#: The ``DashboardState`` attribute holding ``(task, run_id, total)`` for the run in
#: flight, if any. Read and written only on the event loop with no await between
#: the check and the set, so two presses cannot both start a run.
_RUN_ATTR = "_resume_all_run"


def _crew_roots(state: DashboardState) -> dict[str, str]:
    """Each live slot's Crew Mode root (``crew_root``), ``""`` on a row that is no worker.

    The stamp the slot frame sends (``crew_mode.live_stamps`` over the frame's
    ``parent`` edges), so the sidebar's ``crewModeView`` and this collect read
    the same lineage. A fault costs the stamps only, as it does in the frame:
    every row is then listed, and counted, as a plain session.
    """
    try:
        from kiro_crew import crew_mode
        from kiro_crew.dashboard.session_control_parts.crew_filing import _frame_parents

        stamps = crew_mode.live_stamps(
            state._slots, _frame_parents(state), settled=crew_mode.lineage_is_settled()
        )
    except Exception:
        logger.warning("resume-all: Crew Mode stamps unavailable", exc_info=True)
        return {}
    return {key: root for key, (root, _is_root) in stamps.items() if root}


def collect_interrupted_idle(
    state: DashboardState, *, crew_mode: bool = False
) -> list[tuple[str, _ChatSlot]]:
    """The ``(name, slot)`` pairs the button acts on, in registration order.

    The same facts the slots projection serializes for the row: a slot under
    construction is not in the payload at all, ``running`` (``turn_running``)
    and ``subagents_running`` must both be false, and ``interrupted`` is
    ``is_turn_interrupted`` on the transcript. Relay archives are skipped
    first: they never run, and the sidebar leaves remote rows out of its count.

    *crew_mode* is whether the pressing tab has the Crew Mode preview on
    (``crewModeEnabled``). Only then does the sidebar's ``crewModeView`` drop a
    worker whose root is listed, so only then is that worker left out here.
    """
    subs = getattr(state, "subagents", None)
    under_construction = getattr(state, "_slots_under_construction", None) or ()
    picked: list[tuple[str, _ChatSlot]] = []
    # Snapshot: ``continue_slot_turn`` awaits, so the live dict must not be
    # iterated across the dispatch below.
    slots = list(state._slots.items())
    # The chat page's own rows, as the slots payload carries them to the
    # sidebar: the list a Crew Mode root must be in for its workers to leave it.
    listed = {
        slot.key
        for _name, slot in slots
        if slot.key not in under_construction
        and (getattr(slot, "mode", "") or "") in CHAT_PAGE_MODES
    }
    crew_roots = _crew_roots(state) if crew_mode else {}
    for name, slot in slots:
        if slot.key in under_construction or is_relay_archive(slot):
            continue
        # The chat page's own sessions only, as the sidebar lists them
        # (``isChatPageSurface``): a member's DM thread or a sub-agent row lives
        # on another page, so the button never counts it.
        if (getattr(slot, "mode", "") or "") not in CHAT_PAGE_MODES:
            continue
        # A Crew Mode worker whose root is listed: the sidebar's ``crewModeView``
        # drops it (the root's Crew panel lists it), so the button never counts
        # it and its conductor, not this press, decides when it goes on.
        root = crew_roots.get(slot.key, "")
        if root and root in listed:
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
    """Continue every candidate, at most ``RESUME_ALL_MAX_RUNNING`` turns at once.

    Each candidate takes a permit before its dispatch, in registration order
    (``asyncio.Semaphore`` wakes waiters first in, first out). A dispatch that
    started a turn hands its permit back when that turn's task is done; a refusal
    or a failure hands it back at once. This returns once every candidate is
    recorded, not once every turn has ended.

    ``resumed`` lists the slot keys whose continuation was queued and
    dispatched. ``skipped`` carries ``{slot, code}`` for every
    :class:`SlotContinueRefusal`: the slot was a candidate when collected but
    ``continue_slot_turn`` found a reason not to (its turn finished meanwhile, a
    message queued meanwhile, a pending approval, running sub-agents, two failed
    starts in a row, or a same-name re-registration during the lock wait). A
    candidate that waited for a permit is checked when its turn comes, so one
    that its owner resumed by hand meanwhile is skipped, not resumed twice.
    ``failed`` carries ``{slot, error}`` with the exception's class name for
    anything else; the other slots are unaffected. *claims* is the caller's
    ``app`` claim, passed to the same replaced-while-queued probe the
    single-slot route uses.
    """
    resumed: list[str] = outcome["resumed"]
    skipped: list[dict[str, str]] = outcome["skipped"]
    failed: list[dict[str, str]] = outcome["failed"]
    permits = asyncio.Semaphore(RESUME_ALL_MAX_RUNNING)

    def _release_after(slot: _ChatSlot) -> None:
        # The turn the dispatch started holds the permit until its task is done.
        # A dispatch that lost its queue entry to a concurrent dequeue leaves
        # whichever turn took it, which is the turn running on the slot either way.
        # When that turn hands the slot to a retry it queued for itself (a
        # process death, a failed start), the retry is still this resume's work,
        # so the permit follows it; a person's message drained next is not, and
        # the permit comes back then.
        def _follow(done: asyncio.Task[Any] | None) -> None:
            task = slot.task
            if task is None or task.done() or (done is not None and task is done):
                permits.release()
                return
            if done is not None and task not in BACKGROUND_START_TASKS:
                permits.release()
                return
            task.add_done_callback(_follow)

        _follow(None)

    async def _one(name: str, slot: _ChatSlot) -> None:
        await permits.acquire()
        try:
            # While the gateway's memory preparation runs, the queue drain parks
            # a dispatch instead of starting its turn, and the parked queues all
            # start together once it ends. Waiting here first keeps every one of
            # them behind a permit.
            await _memory_preparation_settled(state)
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
                # The owner's press, as on the per-row Resume: an interrupted
                # session whose queue a restart restored from disk is resumed,
                # and its restored cards stay queued for the owner to send.
                allow_restored_queue=True,
                extra_meta={START_BACKGROUND_META_KEY: True},
                replaced=lambda: _ch._slot_replaced_while_queued(
                    state, slot, name, claims, "chat.slots_resume_all"
                ),
            )
        except SlotContinueRefusal as exc:
            permits.release()
            skipped.append({"slot": name, "code": exc.code})
            return
        except asyncio.CancelledError:
            _release_after(slot)
            raise
        except Exception as exc:
            # Any other exception, a failed slots push after the dispatch
            # included, fails this slot only; the others carry on.
            _release_after(slot)
            logger.warning("resume-all: continue failed for slot %s", name, exc_info=True)
            failed.append({"slot": name, "error": type(exc).__name__})
            return
        _release_after(slot)
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


#: How often a candidate looks for the memory preparation task while startup
#: is preparing it but has not published it on the state yet.
_MEMORY_TASK_POLL_SECONDS = 0.1


async def _memory_preparation_settled(state: DashboardState) -> None:
    """Return once the gateway's memory preparation has ended.

    The task the queue drain parks on (``_memory_preparation_pending``). Startup
    publishes it on the state only after the listener is up, so while this
    process is still preparing (``memory_startup_preparing``) and nothing is
    published yet, the candidate waits for the task to appear, as a restored
    send does (``slot_queue_repository``); a turn started then would be refused
    by the memory barrier. A failed or cancelled preparation counts as ended:
    the turn refuses on its own then, as it does for any send.
    """
    from kiro_crew.memory_startup import memory_startup_preparing

    while getattr(state, "memory_startup_task", None) is None and memory_startup_preparing():
        await asyncio.sleep(_MEMORY_TASK_POLL_SECONDS)
    task = getattr(state, "memory_startup_task", None)
    if task is None or task.done():
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            raise
    except Exception:
        pass


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
    state: DashboardState,
    claims: Mapping[str, Any],
    *,
    crew_mode: bool = False,
    only: frozenset[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Start a run, or refuse when one is in flight. ``(status, body)`` for the route.

    *only*, when given, is the set of session keys the press counted (and the
    owner confirmed): a candidate outside it is left alone, so a session that
    became interrupted after the press never joins its run.

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
    candidates = collect_interrupted_idle(state, crew_mode=crew_mode)
    if only is not None:
        candidates = [(name, slot) for name, slot in candidates if name in only]
    if not candidates:
        return 200, {"ok": True, "total": 0, "run_id": ""}
    run_id = uuid.uuid4().hex[:12]
    outcome = _new_outcome(len(candidates), run_id)
    task = asyncio.ensure_future(_run_in_background(state, candidates, claims, outcome))
    _track(state, task)
    setattr(state, _RUN_ATTR, (task, run_id, len(candidates)))
    task.add_done_callback(lambda t: _clear_run(state, t))
    return 202, {"ok": True, "total": len(candidates), "run_id": run_id}


#: The most session keys a press may name. Far above any sidebar's count; past
#: it the press names nothing, and nothing is resumed.
RESUME_ALL_MAX_KEYS = 2000


async def _press_body(request: web.Request) -> tuple[bool, frozenset[str] | None]:
    """``(crew_mode, only)`` from the press's optional body (see the route).

    Only a press with no body at all resumes every candidate. A body that is not
    a JSON object, or a ``slots`` value that is not a list of strings or names
    more than ``RESUME_ALL_MAX_KEYS``, narrows the run to nothing rather than
    widening it to every interrupted session.
    """
    if not request.can_read_body:
        return False, None
    try:
        body = await request.json()
    except Exception:
        return False, frozenset()
    if not isinstance(body, dict):
        return False, frozenset()
    crew_mode = body.get("crew_mode") is True
    if "slots" not in body:
        return crew_mode, None
    keys = body["slots"]
    if (
        not isinstance(keys, list)
        or len(keys) > RESUME_ALL_MAX_KEYS
        or not all(isinstance(k, str) for k in keys)
    ):
        return crew_mode, frozenset()
    return crew_mode, frozenset(keys)


async def api_chat_slots_resume_all(request: web.Request) -> web.Response:
    """POST /api/chat/slots/resume-all -- Resume every interrupted idle session.

    An optional body ``{"crew_mode": true, "slots": [key, ...]}``. ``crew_mode``
    says the pressing tab has the Crew Mode preview on, so its count left out the
    workers of listed roots and the run leaves them out too; without it they
    count. ``slots`` names the sessions the press counted: the run resumes only
    the candidates among them, so it never reaches past what the owner saw and
    confirmed. Without it every candidate is resumed. Owner-only (see the module docstring for why the per-slot app
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
    crew_mode, only = await _press_body(request)
    status, body = start_resume_all(state, _claims(request), crew_mode=crew_mode, only=only)
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
