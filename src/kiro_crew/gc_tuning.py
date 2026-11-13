"""Opt-in cyclic-GC tuning for the gateway, behind ``KIROCREW_GC_WARM_FREEZE``.

The gateway's full (generation 2) collections stop every thread, the event loop
included, for as long as it takes to walk every long-lived tracked object. Two
things make them frequent and long on a busy gateway:

* Most of the long-lived heap is built during warm-up and then never dies, yet
  each full pass walks it again. The boot freeze in ``service_boot.run`` happens at
  socket bind, so what the gateway builds after it (restored tabs, channel
  conversations, warm caches) is not frozen.
* CPython runs a full pass whenever the objects promoted since the last one reach
  a quarter of the long-lived total, so objects that live for seconds or minutes
  keep triggering passes over the whole heap.

With the switch on, the gateway does two things:

* :func:`apply_threshold` raises the generation-2 threshold to
  :data:`GEN2_THRESHOLD` once, at boot, so a full pass needs 100 generation-1
  collections instead of 10.
* :func:`freeze_when_warm` calls ``gc.freeze()`` once, after warm-up finishes (the
  dashboard's ``startup_tabs_rebuilt`` event), so later full passes skip the heap
  that warm-up built.

Cost: a frozen object is never collected by the cyclic collector. An object that
was alive at the freeze and later becomes part of an unreachable cycle stays in
memory for the life of the process, and finalizers on such a cycle never run.
Freezing once bounds that to whatever was alive at that moment, which is why
:func:`freeze_after_warm_up` freezes at most once per process.

Off by default. With the switch off nothing here touches the collector.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

#: Turns the tuning on when set to ``1``, ``true``, ``yes`` or ``on``.
SWITCH_ENV: Final[str] = "KIROCREW_GC_WARM_FREEZE"
#: Generation-1 collections between two full collections while the switch is on
#: (CPython's default is 10).
GEN2_THRESHOLD: Final[int] = 100

_TRUTHY_ON: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_state: dict[str, bool] = {"frozen": False}


def enabled() -> bool:
    """Whether ``KIROCREW_GC_WARM_FREEZE`` turns the tuning on."""
    return os.environ.get(SWITCH_ENV, "").strip().lower() in _TRUTHY_ON


def apply_threshold() -> bool:
    """Set the generation-2 threshold to :data:`GEN2_THRESHOLD` when the switch is on."""
    if not enabled():
        return False
    gen0, gen1, _gen2 = gc.get_threshold()
    gc.set_threshold(gen0, gen1, GEN2_THRESHOLD)
    return True


def freeze_after_warm_up() -> bool:
    """``gc.freeze()`` once per process, when the switch is on. ``True`` if it froze."""
    if not enabled() or _state["frozen"]:
        return False
    _state["frozen"] = True
    # O(1): it splices the generation lists. No gc.get_freeze_count() here, which
    # walks the whole permanent generation on the loop.
    gc.freeze()
    logger.info("GC: froze the warm-up heap (%s)", SWITCH_ENV)
    return True


async def freeze_when_warm(warm: asyncio.Event) -> None:
    """Wait for *warm* (the dashboard's ``startup_tabs_rebuilt``), then freeze once."""
    await warm.wait()
    freeze_after_warm_up()


def reset_for_tests() -> None:
    _state["frozen"] = False
