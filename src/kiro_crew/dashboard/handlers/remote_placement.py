"""Hub placement: ``session_create(work_item=)`` with no ``crew`` picks one (wave 5).

RFC §2b option (C): the hub ranks crews from fresh headroom snapshots minus its
reservations (open remote bindings plus creates in flight here, one estimate each),
and the peer gate (wave 3) alone admits. ``crew_busy`` tries the next crew; any other
refusal stops, since that create may have reached the peer. The rules are spelled
out in ``docs/system-specs/modules/session-control.md`` ("Wave 5"). Reached only
while ``work_ledger.remote_dispatch`` is on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from kiro_crew.dashboard.remote_dispatch_headroom import (
    CPU_LOAD_THRESHOLD,
    remote_dispatch_settings,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The refusal when no connected crew has room for the item.
CREW_NONE_AVAILABLE = "crew_none_available"

#: ``TunnelState.CONNECTED.value``.
_CONNECTED = "connected"

#: Per gateway state: creates in flight per crew, and the lock the choice takes.
_INFLIGHT: "weakref.WeakKeyDictionary[Any, dict[str, int]]" = weakref.WeakKeyDictionary()
_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class Candidate:
    instance_id: str
    name: str
    mem_free_gb: float
    disk_free_gb: float


#: Seconds one crew's version and headroom reads may take. Crews are probed at once,
#: so a stalled crew costs this much, well inside the tool's 30 s call budget.
PROBE_TIMEOUT_S = 5.0

#: Best effort: once this many seconds have passed since placement began, no new
#: crew is tried. A create already started (its config load, board lock and peer
#: POST included) runs to the end, so one slow create can outlast the tool's 30 s
#: wait and bind after the caller gave up; a chain of ``crew_busy`` replies cannot.
PLACEMENT_BUDGET_S = 15.0

#: The clock the budget reads; a test replaces it rather than ``time.monotonic``.
_clock = time.monotonic


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer past float range from a hostile peer
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _connected(mgr: Any, instance_id: str) -> bool:
    try:
        status = mgr.status(instance_id)
    except Exception:  # noqa: BLE001 - a status read must not fail a placement
        return False
    return getattr(getattr(status, "state", None), "value", None) == _CONNECTED


async def _candidate(
    state: "DashboardState", mgr: Any, row: Any, mem_gb: float, disk_gb: float
) -> Candidate | None:
    """*row* as a :class:`Candidate`, or ``None`` when it is not eligible."""
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    instance_id = str(getattr(row, "id", "") or "")
    if not instance_id or not _connected(mgr, instance_id):
        return None

    async def _probe() -> tuple[int, Any]:
        await routes.check_version(mgr, instance_id)
        return await routes.peer_call(mgr, instance_id, "GET", "api/chat/headroom")

    try:
        status, payload = await asyncio.wait_for(_probe(), PROBE_TIMEOUT_S)
    except routes.CrewCallError as exc:
        logger.info("placement skips crew %s (%s)", instance_id, exc.code)
        return None
    except asyncio.TimeoutError:
        logger.info("placement skips crew %s (headroom read timed out)", instance_id)
        return None
    except Exception:  # noqa: BLE001 - one crew's fault must not fail the placement
        logger.warning("placement skips crew %s (probe failed)", instance_id, exc_info=True)
        return None
    if not 200 <= status < 300 or not isinstance(payload, dict):
        return None
    mem, disk = (
        _number(payload.get("mem_free_gb")),
        _number(payload.get("disk_free_gb")),
    )
    load = _number(payload.get("load_per_cpu"))
    if mem is None or disk is None or mem < mem_gb or disk < disk_gb:
        return None
    if load is not None and load >= CPU_LOAD_THRESHOLD:
        return None
    return Candidate(instance_id, str(getattr(row, "name", "") or instance_id), mem, disk)


def _score(c: Candidate, reserved: int, mem_gb: float, disk_gb: float) -> float:
    """The scarcer of memory and disk after reservations, counted in estimates."""
    return min(
        (c.mem_free_gb - reserved * mem_gb) / mem_gb,
        (c.disk_free_gb - reserved * disk_gb) / disk_gb,
    )


async def _reserve_best(
    state: "DashboardState", pool: list[Candidate], mem_gb: float, disk_gb: float
) -> Candidate:
    """Pick the best of *pool* and count one create in flight on it, atomically."""
    from kiro_crew.dashboard.handlers.remote_ingest import open_remote_bindings

    async with _LOCKS.setdefault(state, asyncio.Lock()):
        bound = await asyncio.to_thread(open_remote_bindings)
        inflight = _INFLIGHT.setdefault(state, {})
        # ``max`` keeps the first of equal scores, so sorting by id breaks ties.
        best = max(
            sorted(pool, key=lambda c: c.instance_id),
            key=lambda c: _score(
                c,
                bound.get(c.instance_id, 0) + inflight.get(c.instance_id, 0),
                mem_gb,
                disk_gb,
            ),
        )
        inflight[best.instance_id] = inflight.get(best.instance_id, 0) + 1
        return best


def _release(state: "DashboardState", instance_id: str) -> None:
    inflight = _INFLIGHT.setdefault(state, {})
    inflight[instance_id] -= 1
    if not inflight[instance_id]:
        del inflight[instance_id]


@contextlib.asynccontextmanager
async def holding(state: "DashboardState", instance_id: str) -> AsyncIterator[None]:
    """Count one work-item create to *instance_id* in flight while it runs.

    For an explicit ``crew=`` create, so a placement that runs meanwhile counts
    it before its binding is recorded. Placement's own creates are counted by
    :func:`_reserve_best` and never pass through here.
    """
    async with _LOCKS.setdefault(state, asyncio.Lock()):
        inflight = _INFLIGHT.setdefault(state, {})
        inflight[instance_id] = inflight.get(instance_id, 0) + 1
    try:
        yield
    finally:
        _release(state, instance_id)


async def place(
    state: "DashboardState", mgr: Any, caller: str, body: dict[str, Any], *, board: str
) -> tuple[dict[str, Any], str]:
    """Create the bound worker on the best crew: ``(create result, crew name)``, or raise."""
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    if not isinstance(body.get("work_item"), str) or not body["work_item"].strip():
        # Placement is for a work item only; a plain crew session names its crew.
        raise routes.CrewCallError(400, "work_item_invalid", "work_item must be an item id")
    deadline = _clock() + PLACEMENT_BUDGET_S
    settings = await asyncio.to_thread(remote_dispatch_settings)
    if not settings.enabled:
        # Turned off between the route's check and this read: place nothing.
        from kiro_crew.remote_dispatch import (
            REMOTE_DISPATCH_DISABLED,
            REMOTE_DISPATCH_DISABLED_MESSAGE,
        )

        raise routes.CrewCallError(403, REMOTE_DISPATCH_DISABLED, REMOTE_DISPATCH_DISABLED_MESSAGE)
    mem_gb, disk_gb = settings.memory_gb, settings.disk_gb
    from kiro_crew.dashboard.handlers import remote_ingest

    # Refuse a bad item before any crew is probed, with the bound create's codes.
    await remote_ingest.open_unbound_item(board, body["work_item"])
    try:
        rows = await asyncio.to_thread(routes._instances, state)
    except Exception:
        logger.debug("crew registry unreadable", exc_info=True)
        rows = []
    found = await asyncio.gather(*(_candidate(state, mgr, r, mem_gb, disk_gb) for r in rows))
    pool = [c for c in found if c is not None]
    busy: list[str] = []
    stopped = False
    while pool:
        pick = await _reserve_best(state, pool, mem_gb, disk_gb)
        try:
            # Checked after the reservation, which can wait on the lock: past the
            # budget, no new crew is tried (best effort, see PLACEMENT_BUDGET_S).
            if _clock() >= deadline:
                stopped = True
                break
            result = await routes._create(state, mgr, caller, pick.instance_id, body, board=board)
        except routes.CrewBusyError:
            busy.append(pick.name)
            pool = [c for c in pool if c.instance_id != pick.instance_id]
            continue
        finally:
            _release(state, pick.instance_id)
        return result, pick.name
    tried = f" ({', '.join(busy)} answered crew_busy)" if busy else ""
    if stopped:
        tried += f"; placement stopped after {PLACEMENT_BUDGET_S:g} s"
    raise routes.CrewCallError(
        503,
        CREW_NONE_AVAILABLE,
        f"no connected crew has room for this item{tried}; it stays open and unbound. "
        "Retry from a turn the owner sends: a patrol wake cannot use crew placement",
    )
