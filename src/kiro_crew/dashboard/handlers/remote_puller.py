"""The hub's background puller for remote workers (rfc-conductor-remote-dispatch §2, OQ 1).

A remote worker's ``work_report`` lands in the peer's ``hub:<conductor>`` mailbox,
and the hub learns it only by pulling (:func:`remote_ingest.ingest_board`). Pulling
only inside ``work_ledger_read`` meant a remote ``done`` / ``question`` /
``blocked`` / ``reply`` woke nobody until the conductor's next patrol turn. This
task runs that same pull with no model turn, every
:data:`~kiro_crew.work_ledger_remote.REMOTE_PULL_INTERVAL_SECS`, for each ledger
here holding an open crew-bound item. A pulled report is filed as an ordinary
recorded report, so the crew-log push (:mod:`kiro_crew.conductor_wake`) and the
work-ledger probe wake the conductor exactly as they do for a local one.

Nothing new crosses the tunnel: the hub still pulls and the peer never connects
back, through the same ``peer_call`` (path fence, byte cap,
``_redact_peer_payload``), and the item stays the dedupe, keyed by the peer's
``last_report_at``.

Bounds. One run at a time (a single task that sleeps between runs, so runs never
overlap); a board already being pulled by a read is skipped by ``ingest_board``'s
own guard. Boards are pulled concurrently, at most :data:`PARALLEL_BOARDS` at once,
each cut off at :data:`BOARD_TIMEOUT_SECS`, well under the interval, and inside it
each item at ``PULL_TIMEOUT_SECS``, so one unreachable crew never stalls another.

Behind ``work_ledger.remote_dispatch``, read on every run. Off, the task is never
started (:func:`ensure_running` returns at once) and a running one ends at its next
run. It is started after the dashboard binds when the switch is on
(``server_runtime.service_hooks``), and again by a crew bind or a ledger read, so
turning the switch on needs no restart. The dashboard's cleanup hook stops it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew import work_ledger

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Seconds between two runs; the store module owns the value the probe reads too.
PULL_INTERVAL_SECS = work_ledger.REMOTE_PULL_INTERVAL_SECS

#: Seconds one board's pull may take in all, its lock waits included. Above the
#: read path's own pull budget (``remote_ingest.READ_DEADLINE_SECS``) and well
#: under :data:`PULL_INTERVAL_SECS`.
BOARD_TIMEOUT_SECS = 10.0

#: Boards pulled at once in one run.
PARALLEL_BOARDS = 8

#: Most boards one run pulls; the rest wait for the next run.
MAX_BOARDS_PER_RUN = 64


def _enabled() -> bool:
    from kiro_crew.remote_dispatch import remote_dispatch_enabled

    return remote_dispatch_enabled()


class _Puller:
    def __init__(self, state: "DashboardState") -> None:
        self.state = state
        self.task: asyncio.Task[None] | None = None
        #: Set by :func:`ensure_running` while the task is alive, so a run that read
        #: the switch off just before a bind or read turned it on runs once more
        #: rather than ending with that item unpulled.
        self.kicked = False

    def alive(self) -> bool:
        return self.task is not None and not self.task.done()


_puller: _Puller | None = None

#: Set by :func:`stop`; :func:`ensure_running` starts nothing after it.
_stopped = False


def _pullable_boards() -> list[str]:
    """The boards with an open crew-bound item whose cache is not flagged dirty. BLOCKING.

    A dirty board waits for a rebuild; pulling it would only refuse, and each refusal
    writes a ``denied`` audit row no caller caused, every run until the rebuild.
    """
    return [b for b in work_ledger.boards_with_open_remote() if work_ledger.cache_dirty(b) is None]


#: Where the next run's window of boards starts, so a run capped at
#: :data:`MAX_BOARDS_PER_RUN` never leaves the same tail unpulled every time.
_cursor = 0


def _next_window(boards: list[str]) -> list[str]:
    """At most :data:`MAX_BOARDS_PER_RUN` of *boards*, rotating between runs."""
    global _cursor
    if len(boards) <= MAX_BOARDS_PER_RUN:
        _cursor = 0
        return boards
    start = _cursor % len(boards)
    _cursor = start + MAX_BOARDS_PER_RUN
    return (boards[start:] + boards[:start])[:MAX_BOARDS_PER_RUN]


async def run_once(state: "DashboardState") -> list[str]:
    """Pull every ledger here holding an open crew-bound item, once. Never raises.

    Answers the boards it pulled, for tests and the debug log.
    """
    from kiro_crew.dashboard.handlers import remote_ingest

    try:
        boards = await asyncio.to_thread(_pullable_boards)
    except Exception:  # noqa: BLE001 - a scan fault skips this run
        logger.debug("remote puller: board scan failed", exc_info=True)
        return []
    boards = _next_window(boards)
    gate = asyncio.Semaphore(PARALLEL_BOARDS)

    async def _one(board: str) -> None:
        async with gate:
            try:
                await asyncio.wait_for(remote_ingest.ingest_board(state, board), BOARD_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                logger.info("remote puller: pull for one board passed %ss", BOARD_TIMEOUT_SECS)
            except Exception:  # noqa: BLE001 - one board must never stop the rest
                logger.warning("remote puller: pull failed for one board", exc_info=True)

    await asyncio.gather(*(_one(board) for board in boards))
    return boards


async def _run(puller: _Puller) -> None:
    while True:
        puller.kicked = False
        try:
            on = await asyncio.to_thread(_enabled)
        except Exception:  # noqa: BLE001 - fails closed, as the switch does
            on = False
        if not on:
            if puller.kicked:
                continue
            return
        await run_once(puller.state)
        await asyncio.sleep(PULL_INTERVAL_SECS)


def ensure_running(state: "DashboardState") -> None:
    """Start the puller if it is not running. Call on the event loop, switch on only.

    Cheap and idempotent: a live task is only kicked. Starts nothing once
    :func:`stop` ran.
    """
    global _puller
    if _stopped:
        return
    if _puller is not None and _puller.alive() and _puller.state is state:
        _puller.kicked = True
        return
    if _puller is not None and _puller.alive():
        _puller.task.cancel()  # type: ignore[union-attr]
    _puller = _Puller(state)
    _puller.task = asyncio.get_running_loop().create_task(
        _run(_puller), name="remote-report-puller"
    )


async def start_if_enabled(state: "DashboardState") -> None:
    """Boot: start the puller only when the switch is on. A new dashboard in the
    same process starts again after an earlier one's :func:`stop`."""
    global _stopped
    _stopped = False
    try:
        on = await asyncio.to_thread(_enabled)
    except Exception:  # noqa: BLE001
        on = False
    if on:
        ensure_running(state)


async def stop() -> None:
    """Cancel the puller and wait for it to end. Safe when it never started."""
    global _puller, _stopped
    _stopped = True
    puller, _puller = _puller, None
    if puller is None or puller.task is None:
        return
    puller.task.cancel()
    try:
        await puller.task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.debug("remote puller ended with an error", exc_info=True)


def reset_for_tests() -> None:
    global _puller, _stopped, _cursor
    _puller = None
    _stopped = False
    _cursor = 0
