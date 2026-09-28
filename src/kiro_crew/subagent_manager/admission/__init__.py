"""Admission behavior for the SubagentManager facade.

Admission is also where the durable task queue (``kiro_crew.taskq``) meets the
manager: a spawn is persisted BEFORE its id is returned, the in-memory
``_queue`` is only a bounded window over the store's queued rows, memory
pressure defers a row instead of refusing it, and every dispatch goes through
an atomic claim whose generation fences later writes. The ``taskq_*`` methods
are the whole of that glue; they are plain methods (not ``*_impl``), so they
run on their own module's globals and import ``taskq`` only on use.

The coordinator is one class assembled from one module per concern, each a
mixin over :class:`ManagerComponent` with no state of its own:

* :mod:`.types` -- the value types (settings, prepared row, claim and defer
  points, capacity reading) and the constants importers read.
* :mod:`.gate` -- ``spawn_impl``: every policy and capacity check between a
  request and its row, plus the typed refusal.
* :mod:`.pump` -- stagger, drain and the atomic claim -> dispatch -> register
  -> reservation-release unit (``taskq_claim``, ``claim_and_start``,
  ``release_reservation``, ``_drain_queue_sync_impl`` live together: the
  generation fence is one module, with no loop hop inside it).
* :mod:`.taskq_bridge` -- store open/accept/defer/settle, the bounded window
  and its refill, and the single owner of off-loop store writes
  (``_post_store_write`` / ``track_store_task``).
* :mod:`.fairness` -- lanes, the child reserve, the capacity view.
* :mod:`.waits` -- yielding the lane slot, resuming through admission, child
  registration and wait deadlines.

``kiro_crew.subagent_manager.admission`` stays the import path: every name an
importer or a test reads is re-exported here.

The ``*_impl`` methods are bound onto the manager's namespace by
``bind_component_globals``, which walks ``vars(SpawnAdmissionCoordinator)``
only -- an inherited method would be skipped and run on its mixin's globals,
where the manager's names do not exist. ``_hoist_impls`` therefore copies each
mixin's ``*_impl`` into this class's own ``__dict__`` at import time, so the
rebinding (and ``copy_component_docs``) sees them exactly as it did when the
class was one module.
"""

from __future__ import annotations

from types import FunctionType

from .._component import ManagerComponent
from .fairness import _FairnessMixin
from .gate import _GateMixin
from .pump import _PumpMixin
from .taskq_bridge import _TaskqBridgeMixin
from .types import (
    FAIRNESS_SETTINGS_TTL_SECS,
    TASK_STORE_UNAVAILABLE_CODE,
    CapacityView,
    ClaimPoint,
    DeferPoint,
    FairnessSettings,
    PreparedSpawn,
)
from .waits import _WaitsMixin


class SpawnAdmissionCoordinator(
    _GateMixin, _PumpMixin, _TaskqBridgeMixin, _FairnessMixin, _WaitsMixin, ManagerComponent
):
    #: Whether the pump runs as a coroutine on a running loop (its store reads
    #: on the writer thread). Production keeps it on. Deterministic harnesses
    #: -- the virtual-clock experiment driver and the test suite's
    #: ``sleep(0)`` settle loops -- switch it off so the pump completes inline
    #: on the same tick; the off-loop path has its own dedicated pin.
    pump_off_loop: bool = True
    #: Whether a manager built on a running loop opens its store on a worker
    #: (typed refusals until the attach). Production keeps it on. The same
    #: harnesses that run the pump inline construct a manager and spawn on the
    #: next line, so they open inline too; the off-loop open has its own pin.
    open_store_off_loop: bool = True

    """Own admission transitions while state remains facade-owned."""

    __slots__ = ()

    #: Read through ``self`` from the rebound ``spawn_impl``, where this
    #: package's globals are not in scope.
    TASK_STORE_UNAVAILABLE_CODE = TASK_STORE_UNAVAILABLE_CODE


def _hoist_impls(cls: type) -> None:
    """Copy every mixin ``*_impl`` into ``cls.__dict__`` (see the module docstring)."""
    own = vars(cls)
    for base in cls.__mro__[1:]:
        for name, implementation in vars(base).items():
            if (
                name.endswith("_impl")
                and isinstance(implementation, FunctionType)
                and name not in own
            ):
                setattr(cls, name, implementation)


_hoist_impls(SpawnAdmissionCoordinator)

__all__ = [
    "FAIRNESS_SETTINGS_TTL_SECS",
    "TASK_STORE_UNAVAILABLE_CODE",
    "CapacityView",
    "ClaimPoint",
    "DeferPoint",
    "FairnessSettings",
    "PreparedSpawn",
    "SpawnAdmissionCoordinator",
]
