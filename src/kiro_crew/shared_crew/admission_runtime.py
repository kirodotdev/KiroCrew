"""Runtime ownership for fair admission: liveness proof, orphan reaping, slice lifecycle.

The admission gate (``admission_gate``) decides; this module answers what the
host holds. Every function shells out or reads cgroup files, so callers run
them off-loop.

* :func:`systemd_unit_liveness` proves whether a record's runtime still holds a
  process: a ``.scope`` unit is asked directly, a principal ``.slice`` is
  alive while any runtime scope in it is populated.
* :func:`systemd_reap` stops the runtime scopes a previous gateway process left
  running, which have no reader any more.
* :func:`sweep_idle_slices` and :func:`release_stale_slices` stop per-principal
  slices nothing uses and remove their runtime drop-ins.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Iterable

from kiro_crew.shared_crew import containment as _containment
from kiro_crew.shared_crew.admission import Liveness, RuntimeOwnershipRef

#: How long a principal's slice stays sized after its last work ended.
SLICE_IDLE_SECONDS = 600.0

Busy = Callable[[str], "list[str] | None"]


def systemd_unit_liveness(ref: RuntimeOwnershipRef) -> Liveness:
    """ALIVE while the work's runtime unit holds a process; STOPPED otherwise.

    A ``.scope`` unit is asked directly. A ``.slice`` (the principal slice the
    work's runtime scopes run under) is ALIVE while any runtime scope in it is
    populated: after a restart nothing of this process has started there yet,
    so a populated scope is the work's own pre-restart runtime.
    """
    unit = ref.containment_unit
    if unit and unit.endswith(".slice"):
        scopes = _containment.populated_scopes(unit)
        if scopes is None:
            return Liveness.UNKNOWN
        return Liveness.ALIVE if scopes else Liveness.STOPPED
    if not unit or not unit.endswith(".scope"):
        # No unit of its own: an in-gateway execution, which cannot outlive the
        # process that ran it.
        return Liveness.STOPPED
    state = _containment.unit_active_state(unit)
    if state is None:
        return Liveness.UNKNOWN
    if state in ("active", "activating", "deactivating", "reloading"):
        return Liveness.ALIVE
    if state in ("inactive", "failed", "unknown", ""):
        return Liveness.STOPPED
    return Liveness.UNKNOWN


def systemd_reap(ref: RuntimeOwnershipRef) -> int:
    """Stop the pre-restart runtime scopes *ref* names; how many were asked to stop.

    For reconciliation only: a runtime the previous gateway process started has
    no reader left, so it is stopped rather than left holding a slot forever.
    """
    unit = ref.containment_unit
    if unit and unit.endswith(".scope"):
        return 1 if _containment.stop_unit(unit) else 0
    if not unit or not unit.endswith(".slice"):
        return 0
    stopped = 0
    for scope in _containment.populated_scopes(unit) or []:
        if _containment.stop_unit(scope):
            stopped += 1
    return stopped


async def sweep_idle_slices(
    slices: _containment.SliceManager,
    *,
    budgets: Iterable[str],
    idle: dict[str, float],
    now: float,
    busy: Busy,
    force: bool = False,
) -> int:
    """Stop and remove per-principal slices with no work left; how many were released.

    *budgets* are the principals with live work; *idle* records since when each
    other slice has been unused. A slice is released once unused for
    :data:`SLICE_IDLE_SECONDS`, and only while *busy* names no populated runtime
    scope in it. *force* (the gateway's stop) releases at once, including the
    slice of a principal whose turn the stop interrupted: that record ends with
    the process, and the populated-scope check still keeps a slice a runtime
    holds.
    """
    try:
        instance = _containment.instance_slice_name()
        live = {_containment.principal_slice_name(b) for b in budgets}
    except Exception:  # noqa: BLE001 -- no names, nothing to release
        return 0
    released = 0
    for name in slices.applied():
        if name == instance:
            continue
        if name in live and not force:
            idle.pop(name, None)
            continue
        since = idle.setdefault(name, now)
        if not force and now - since < SLICE_IDLE_SECONDS:
            continue
        if await asyncio.to_thread(slices.release, name, busy=busy):
            released += 1
            idle.pop(name, None)
    return released


def principal_slices_left(slices: _containment.SliceManager) -> list[str]:
    """The per-principal slices this process sized and still holds."""
    try:
        instance = _containment.instance_slice_name()
    except Exception:  # noqa: BLE001 -- no name, nothing to report
        return []
    return [name for name in slices.applied() if name != instance]


def release_instance_slice(
    slices: _containment.SliceManager,
    *,
    populated: Callable[[str], "bool | None"] = _containment.slice_populated,
) -> int:
    """At the gateway's stop, release the instance slice once nothing runs under it.

    Only once every per-principal slice is gone, and only when the slice's own
    control group reports it empty; an unknown answer keeps it. Blocking.
    """
    try:
        name = _containment.instance_slice_name()
    except Exception:  # noqa: BLE001 -- no name, nothing to release
        return 0
    if name not in slices.applied() or principal_slices_left(slices):
        return 0
    if populated(name) is not False:
        return 0
    return 1 if slices.release(name, busy=lambda _n: []) else 0


async def release_stale_slices(slices: _containment.SliceManager, *, busy: Busy) -> int:
    """Release the idle per-principal slices an earlier gateway process left behind."""
    names = await asyncio.to_thread(slices.principal_slices)
    owned = set(slices.applied())
    released = 0
    for name in names or []:
        if name in owned:
            continue
        if await asyncio.to_thread(slices.release, name, busy=busy, owned_only=False):
            released += 1
    return released


__all__ = [
    "SLICE_IDLE_SECONDS",
    "principal_slices_left",
    "release_instance_slice",
    "release_stale_slices",
    "sweep_idle_slices",
    "systemd_reap",
    "systemd_unit_liveness",
]
