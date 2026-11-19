"""Pull a board's remote worker reports on its patrol tick (remote-dispatch RFC OQ 1).

A remote worker's ``work_report`` lands in the peer's ``hub:<conductor>`` mailbox,
and the hub learns it only by pulling (:func:`remote_ingest.ingest_board`). A local
worker needs no pull: its report writes the ledger itself. So the work-ledger
patrol tick (``autonudge_service.firing``) calls :func:`pull_on_patrol` for its
own board just before the person-wait hold and the probe read it. A pulled
report is filed as an ordinary recorded report, so the hold and the gate see it
on that same tick and wake the conductor exactly as for a local one. The pull
itself spends no model turn.

Only a board with an open crew-bound item (or a retired slot still to close) is
pulled; a board with only local items makes no peer call. Each crew is read in one batch call per tick
(``remote_batch_pull``). Nothing new crosses the
tunnel: the hub still pulls and the peer never connects back, through the same
``peer_call`` (path fence, byte cap, ``_redact_peer_payload``).

Bounds. One board per tick, cut off at :data:`BOARD_TIMEOUT_SECS`, and inside it
each item at ``PULL_TIMEOUT_SECS``, so an unreachable crew delays only its own
conductor's tick. A board a read is already pulling is skipped by
``ingest_board``'s own guard. Behind ``work_ledger.remote_dispatch``: off, nothing
is pulled.

A tick that pulled also starts the held wait (:func:`start_waits`): one open request
per crew the board has open items on, which the crew answers as soon as one of them
has a new report, so a remote report reaches the conductor in about the time a
local one does rather than at the next tick.

Each tick, and once at boot, :func:`sweep_closes` also closes retired peer slots
on every board here, so a slot retired before its conductor stopped patrolling is
still closed when its crew comes back, and a close that failed is tried again on
a later tick once its backoff has passed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from kiro_crew import work_ledger

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Seconds one board's pull may take in all, its lock waits included. Above the
#: read path's own pull budget (``remote_ingest.READ_DEADLINE_SECS``) and well
#: under the patrol interval.
BOARD_TIMEOUT_SECS = 10.0


def _needs_pull(board: str) -> bool:
    """Switch on, an open crew-bound item, and a clean cache. BLOCKING.

    A dirty board waits for a rebuild; pulling it would only refuse, and each refusal
    writes a ``denied`` audit row no caller caused, every tick until the rebuild.
    """
    from kiro_crew.remote_dispatch import remote_dispatch_enabled

    return (
        remote_dispatch_enabled()
        and work_ledger.board_has_open_remote(board)
        and work_ledger.cache_dirty(board) is None
    )


async def pull_on_patrol(state: DashboardState, board: str) -> bool:
    """Pull *board*'s remote reports once, for its patrol tick. Never raises.

    True when a pull ran (finished, cut off or failed); False when the board needed
    none, so no peer call was made.
    """
    from kiro_crew.dashboard.handlers import remote_ingest

    try:
        needed = await asyncio.to_thread(_needs_pull, board)
    except Exception:
        logger.debug("remote pull: board check failed", exc_info=True)
        return False
    if not needed:
        return False
    try:
        await asyncio.wait_for(remote_ingest.ingest_board(state, board), BOARD_TIMEOUT_SECS)
    except TimeoutError:
        logger.info("remote pull: pull for one board passed %ss", BOARD_TIMEOUT_SECS)
    except Exception:
        logger.warning("remote pull: pull failed for one board", exc_info=True)
    return True


#: Most boards one close sweep visits; the rest wait for the next tick or boot.
MAX_CLOSE_BOARDS_PER_SWEEP = 64

#: Where the next sweep's window of boards starts, so a capped sweep never leaves
#: the same tail unvisited every time.
_close_cursor = 0


def _close_window(boards: list[str]) -> list[str]:
    """At most :data:`MAX_CLOSE_BOARDS_PER_SWEEP` of *boards*, rotating between sweeps."""
    global _close_cursor
    if len(boards) <= MAX_CLOSE_BOARDS_PER_SWEEP:
        _close_cursor = 0
        return boards
    start = _close_cursor % len(boards)
    _close_cursor = start + MAX_CLOSE_BOARDS_PER_SWEEP
    return (boards[start:] + boards[:start])[:MAX_CLOSE_BOARDS_PER_SWEEP]


def _boards_to_close() -> list[str]:
    """Boards with a retired slot not yet closed, clean cache, switch on. BLOCKING."""
    from kiro_crew.remote_dispatch import remote_dispatch_enabled

    if not remote_dispatch_enabled():
        return []
    boards = _close_window(work_ledger.boards_with_pending_close())
    return [b for b in boards if work_ledger.cache_dirty(b) is None]


async def sweep_closes(state: "DashboardState") -> list[str]:
    """Start the close of every retired peer slot whose crew reads connected. Never raises.

    The pull closes a retired slot only on a board someone still patrols or reads.
    Once its items close, that conductor's patrol stops, so a crew that comes back
    later would keep the old worker running unwatched. This sweep finds those
    slots from local files alone and hands each board to
    ``remote_ingest._schedule_closes``, which sends a DELETE only to a crew whose
    tunnel already reads connected, and only for a slot that is neither in flight
    nor inside the backoff a failed close left (``remote_ingest._CLOSE_RETRY``),
    so a failed close is tried again by a later sweep, not forgotten. It runs
    once at boot, after the startup tunnel revive, and on every work-ledger patrol
    tick, with no timer of its own. Each board's items are read SETTLED, under its
    board lock as ``ingest_board`` reads them, so a retire still being recorded
    (and possibly rolled back) never closes its worker. The whole sweep is cut off
    at :data:`BOARD_TIMEOUT_SECS`.

    shortcut: with no conductor patrolling anywhere, a crew that reconnects after
    boot is closed only at the next boot or patrol tick; add a tunnel-connect hook
    if that wait matters.

    Answers the boards it handed on, for tests and the debug log.
    """
    swept: list[str] = []
    try:
        await asyncio.wait_for(_sweep(state, swept), BOARD_TIMEOUT_SECS)
    except Exception:  # noqa: BLE001 - a timeout or scan fault ends this sweep
        logger.debug("remote close sweep stopped early", exc_info=True)
    return swept


async def _sweep(state: "DashboardState", swept: list[str]) -> None:
    from kiro_crew.dashboard.handlers import remote_ingest
    from kiro_crew.dashboard.handlers.work_ledger import _board_lock

    for board in await asyncio.to_thread(_boards_to_close):
        try:
            async with _board_lock(board):
                if await asyncio.to_thread(work_ledger.cache_dirty, board) is not None:
                    continue
                items = await asyncio.to_thread(work_ledger.list_work_items, board)
            remote_ingest._schedule_closes(state, board, items)
            swept.append(board)
        except Exception:  # noqa: BLE001 - one board must never stop the rest
            logger.warning("remote close sweep failed for one board", exc_info=True)


async def on_patrol_tick(state: "DashboardState", board: str) -> None:
    """The patrol tick's remote step: pull *board*, keep its waits held, then sweep
    closes. Never raises."""
    if await pull_on_patrol(state, board):
        await start_waits(state, board)
    await sweep_closes(state)


# ── the held wait (W3b) ──────────────────────────────────────────────────────
#
# Between two patrol pulls, the hub holds one wait open per crew a board has open
# items on (``remote_batch_pull.wait_crew``). The crew answers it as soon as a
# worker report moves one of those items past the hub's cursor, and the hub then
# pulls the board through the patrol's own path (:func:`pull_on_patrol`). The
# report is filed as a recorded report, so the crew-log bus wakes the conductor
# (``conductor_wake``) as for a local one. The patrol pull stays the fallback.
#
# How it stops. Each wait re-reads the board first and ends once the switch is
# off, the cache is dirty or the crew has no open item left on it. A crew that
# answers 404 serves no wait: it is left to the patrol pull and not asked again
# for :data:`WAIT_UNSUPPORTED_RETRY_SECS`. A failed wait, a quiet one answered
# sooner than :data:`WAIT_FAST_SECS`, or a second instant news in a row backs
# off, doubling up to the patrol interval, so no path asks a crew in a tight loop;
# any other news resets it. Anything unexpected ends the wait; the next
# patrol tick starts it again.

#: Seconds a wait may take on the hub beyond the crew's hold: its ready check,
#: the cursor reads and the reply.
WAIT_SLACK_SECS = 15.0

#: First and longest pause after a failed or fast-answered wait. The longest is
#: the patrol interval: a crew failing for long is asked no more often than pulled.
WAIT_BACKOFF_MIN_SECS = 5.0
WAIT_BACKOFF_MAX_SECS = 300.0

#: A wait answered sooner than this did not wait at all: counted as a failure for
#: the backoff, so a crew that always answers at once is not asked in a tight loop.
WAIT_FAST_SECS = 2.0

#: Seconds a crew that answered 404 is left to the patrol pull before it is asked
#: again (it may have been upgraded, or had the preview turned on).
WAIT_UNSUPPORTED_RETRY_SECS = 3600.0

#: Most items one wait names: the peer route's own cap.
WAIT_MAX_ITEMS = 32

#: ``(board, crew)`` -> its running wait. At most one per pair, never one per item.
_waits: dict[tuple[str, str], "asyncio.Task[None]"] = {}

#: ``(board, crew)`` -> clock reading before which a 404 crew is not waited on.
_unsupported: dict[tuple[str, str], float] = {}

#: The clock and the pause, as module names so tests drive them with no real sleep.
_clock = time.monotonic
_sleep = asyncio.sleep


def _open_by_crew(board: str) -> dict[str, list[work_ledger.WorkItem]]:
    """Crew -> *board*'s open items bound there; empty when the board needs no pull
    (:func:`_needs_pull`). BLOCKING."""
    from kiro_crew.dashboard.handlers.remote_ingest_report import _is_remote_open

    if not _needs_pull(board):
        return {}
    out: dict[str, list[work_ledger.WorkItem]] = {}
    for item in work_ledger.list_work_items(board):
        if _is_remote_open(item):
            out.setdefault(str(item.worker_instance_id), []).append(item)
    # shortcut: a crew holding more than WAIT_MAX_ITEMS of one board's items has
    # the rest reach the hub on the patrol pull only; name them in turns if needed.
    return {crew: items[:WAIT_MAX_ITEMS] for crew, items in out.items()}


async def start_waits(state: "DashboardState", board: str) -> list[str]:
    """Start a wait for each crew *board* has open items on that has none running.
    Never raises. Answers the crews a wait was started for."""
    try:
        crews = await asyncio.to_thread(_open_by_crew, board)
    except Exception:  # noqa: BLE001 - the patrol pull still runs
        logger.debug("remote wait: board check failed", exc_info=True)
        return []
    now = _clock()
    for key in [k for k, until in _unsupported.items() if until <= now]:
        del _unsupported[key]
    started: list[str] = []
    loop = asyncio.get_running_loop()
    for crew in crews:
        key = (board, crew)
        task = _waits.get(key)
        if key in _unsupported or (task is not None and not task.done()):
            continue
        _waits[key] = loop.create_task(_wait_loop(state, board, crew), name="remote-crew-wait")
        started.append(crew)
    return started


async def _wait_loop(state: "DashboardState", board: str, crew: str) -> None:
    """Hold waits on *crew* for *board* until it has no open item there. Never raises."""
    from kiro_crew.dashboard.handlers import remote_batch_pull as batch

    key = (board, crew)
    backoff = 0.0
    last_fast_news = False
    try:
        while True:
            items = (await asyncio.to_thread(_open_by_crew, board)).get(crew)
            if not items:
                return
            began = _clock()
            try:
                outcome = await asyncio.wait_for(
                    batch.wait_crew(
                        state, getattr(state, "instances_manager", None), board, crew, items
                    ),
                    batch.WAIT_SECS + WAIT_SLACK_SECS,
                )
            except asyncio.TimeoutError:
                outcome = batch.WAIT_FAILED
            # Taken before the pull: a slow pull must not make an instant answer
            # look like a held wait.
            fast = _clock() - began < WAIT_FAST_SECS
            if outcome == batch.WAIT_UNSUPPORTED:
                _unsupported[key] = _clock() + WAIT_UNSUPPORTED_RETRY_SECS
                return
            if outcome == batch.WAIT_NEWS:
                await pull_on_patrol(state, board)
            # An instant news answer is real news when the one before it was not:
            # a report landed while this loop slept. Two in a row mean the pull did
            # not move the cursor, so that backs off as a failure does.
            fast_news = fast and outcome == batch.WAIT_NEWS
            stuck = fast_news and last_fast_news
            last_fast_news = fast_news
            if outcome == batch.WAIT_FAILED or (fast and not fast_news) or stuck:
                backoff = min(max(backoff * 2, WAIT_BACKOFF_MIN_SECS), WAIT_BACKOFF_MAX_SECS)
                await _sleep(backoff)
            else:
                backoff = 0.0
    except Exception:  # noqa: BLE001 - the next patrol tick starts it again
        logger.warning("remote wait on one crew stopped", exc_info=True)
    finally:
        if _waits.get(key) is asyncio.current_task():
            del _waits[key]


def ensure_running(_state: DashboardState) -> None:
    """No-op: no timer runs in the background; the held waits start from the
    patrol tick (:func:`start_waits`).

    shortcut: kept only for the bind route's call (``remote_dispatch_routes``,
    owned by another change); delete with that call.
    """
