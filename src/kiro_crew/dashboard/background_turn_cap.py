"""Size, and enforce, the cap on concurrent unattended (background) turns.

The ceiling on the cap is sized from host memory, so a small host is not told
to run more turns than it can hold and a large one is not held to a small
count:

    reserve_gb  = max(total_gb * os_reserve_pct / 100, os_reserve_gb)
    budget_gb   = min(total_gb - reserve_gb, agents_slice_memory_high_gb)
    memory_term = floor(budget_gb / turn_mem_gb)
    ceiling     = clamp(memory_term, LEGACY_CEIL, ABSOLUTE_CEIL)

``agents_slice_memory_high_gb`` is ``kirocrew-agents.slice``'s ``memory.high``
(75% of RAM by default, ``sandbox_slice._SLICE_MEMORY_HIGH_FRACTION``) when the
slice applies: past it the kernel throttles every agent process, so turns
priced above it would only run slower, never more. ``LEGACY_CEIL`` (16) is a floor
under the ceiling, so a configured count up to 16 runs on every host. ``ABSOLUTE_CEIL`` stands in for what memory does not
model: the model provider's concurrency, file descriptors and PIDs. It matches
``agent.subagent_auto_max``'s ceiling (``SUBAGENT_AUTO_MAX_CEILING``).

Because idle sessions hold memory too and the ceiling is priced from TOTAL RAM,
every permit above ``LEGACY_CEIL`` is also gated on LIVE memory: it is granted
only while the host still has ``reserve_gb + turn_mem_gb`` available and the
tightest of host, container and agents-slice headroom has ``turn_mem_gb``.
Otherwise the turn waits, exactly as it waits at the cap, and is re-checked
every ``POLL_SECS``. When memory cannot be read the ceiling is ``LEGACY_CEIL``
and no extra permit is granted, so an unreadable host behaves as before.

``dashboard.max_background_turns`` keeps its meaning as a count, and ``0`` now
means auto (the ceiling), the same sentinel ``agent.max_subagents`` uses. Every
input is read live from ``config.json``, and :class:`TurnLimiter` resizes while
turns run, so a change takes effect within ``POLL_SECS`` with no restart.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from kiro_crew.config.sections_dashboard import (
    BACKGROUND_TURN_MEM_GB_DEFAULT as DEFAULT_TURN_MEM_GB,
)
from kiro_crew.config.sections_dashboard import BACKGROUND_TURN_MEM_GB_MAX as TURN_MEM_GB_MAX
from kiro_crew.config.sections_dashboard import BACKGROUND_TURN_MEM_GB_MIN as TURN_MEM_GB_MIN
from kiro_crew.config.sections_dashboard import (
    BACKGROUND_TURN_OS_RESERVE_GB_DEFAULT as DEFAULT_OS_RESERVE_GB,
)
from kiro_crew.config.sections_dashboard import (
    BACKGROUND_TURN_OS_RESERVE_GB_MAX as OS_RESERVE_GB_MAX,
)
from kiro_crew.config.sections_dashboard import (
    BACKGROUND_TURN_OS_RESERVE_GB_MIN as OS_RESERVE_GB_MIN,
)
from kiro_crew.config.sections_dashboard import (
    BACKGROUND_TURN_OS_RESERVE_PCT_DEFAULT as DEFAULT_OS_RESERVE_PCT,
)
from kiro_crew.config.sections_dashboard import (
    BACKGROUND_TURN_OS_RESERVE_PCT_MAX as OS_RESERVE_PCT_MAX,
)
from kiro_crew.config.sections_dashboard import (
    BACKGROUND_TURN_OS_RESERVE_PCT_MIN as OS_RESERVE_PCT_MIN,
)
from kiro_crew.config.sections_dashboard import BACKGROUND_TURNS_ABSOLUTE_CEIL as ABSOLUTE_CEIL
from kiro_crew.config.sections_dashboard import BACKGROUND_TURNS_DEFAULT as DEFAULT_TURNS
from kiro_crew.config.sections_dashboard import BACKGROUND_TURNS_LEGACY_CEIL as LEGACY_CEIL

logger = logging.getLogger(__name__)

# LEGACY_CEIL (16) is the floor under the memory-sized ceiling, and the live memory gate only applies to permits above it. The
# constants live in the config section so Settings and this module share one
# copy. DEFAULT_TURN_MEM_GB was measured on a 128 GB host running 27 sessions:
# 0.9 GB median per session process tree (kiro-cli plus its MCP servers), 3.4 GB
# p90.

#: How often a queued turn re-reads config and live memory while it waits.
POLL_SECS = 2.0
#: How long one memory reading is reused across the turns that ask for it.
_MEMORY_CACHE_SECS = 1.0
#: How long the host's static figures (total RAM, slice ceiling) are reused.
_STATIC_CACHE_SECS = 60.0

_GIB = 1024**3


@dataclass(frozen=True)
class CapSizing:
    """Every figure behind the cap, for the status payload and the log."""

    configured: int  # dashboard.max_background_turns as read; 0 = auto
    effective: int  # the cap in force
    ceiling: int  # the highest cap this host allows
    memory_term: int | None  # turns the memory budget fits, None when unreadable
    total_gb: float | None
    reserve_gb: float
    budget_gb: float | None
    turn_mem_gb: float

    @property
    def auto(self) -> bool:
        return self.configured == 0

    def as_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "auto": self.auto,
            "ceiling": self.ceiling,
            "memory_term": self.memory_term,
            "total_gb": None if self.total_gb is None else round(self.total_gb, 1),
            "reserve_gb": round(self.reserve_gb, 1),
            "budget_gb": None if self.budget_gb is None else round(self.budget_gb, 1),
            "turn_mem_gb": self.turn_mem_gb,
        }


def _num(raw: Any, default: float, lo: float, hi: float) -> float:
    """*raw* as a float clamped to ``[lo, hi]``; garbage reads as *default*."""
    if isinstance(raw, bool):
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if math.isnan(val):
        return default
    return min(hi, max(lo, val))


def _configured_turns(raw: Any) -> int:
    """``dashboard.max_background_turns`` as an int: 0 = auto, a negative reads as 1.

    A negative is a mistyped count, not a request for auto, so it gets the
    smallest cap rather than the largest; garbage reads as the default.
    """
    if isinstance(raw, bool):
        return DEFAULT_TURNS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TURNS
    return 1 if val < 0 else val


def reserve_gb(total_gb: float | None, pct: float, floor_gb: float) -> float:
    """GB kept for the OS and the gateway: the larger of *pct* of RAM and *floor_gb*."""
    if not total_gb or total_gb <= 0:
        return floor_gb
    return max(total_gb * pct / 100.0, floor_gb)


def size_cap(
    dashboard: dict[str, Any],
    *,
    total_gb: float | None,
    slice_high_gb: float | None,
) -> CapSizing:
    """The cap for this host and this ``dashboard`` config section. Pure; never raises."""
    turn_mem = _num(
        dashboard.get("background_turn_mem_gb"),
        DEFAULT_TURN_MEM_GB,
        TURN_MEM_GB_MIN,
        TURN_MEM_GB_MAX,
    )
    pct = _num(
        dashboard.get("background_turn_os_reserve_pct"),
        DEFAULT_OS_RESERVE_PCT,
        OS_RESERVE_PCT_MIN,
        OS_RESERVE_PCT_MAX,
    )
    floor_gb = _num(
        dashboard.get("background_turn_os_reserve_gb"),
        DEFAULT_OS_RESERVE_GB,
        OS_RESERVE_GB_MIN,
        OS_RESERVE_GB_MAX,
    )
    configured = _configured_turns(dashboard.get("max_background_turns", DEFAULT_TURNS))
    reserve = reserve_gb(total_gb, pct, floor_gb)
    budget: float | None = None
    memory_term: int | None = None
    if total_gb and total_gb > 0:
        budget = max(0.0, total_gb - reserve)
        if slice_high_gb and slice_high_gb > 0:
            budget = min(budget, slice_high_gb)
        memory_term = math.floor(budget / turn_mem)
    ceiling = (
        LEGACY_CEIL if memory_term is None else max(LEGACY_CEIL, min(memory_term, ABSOLUTE_CEIL))
    )
    effective = ceiling if configured == 0 else max(1, min(configured, ceiling))
    return CapSizing(
        configured=configured,
        effective=effective,
        ceiling=ceiling,
        memory_term=memory_term,
        total_gb=total_gb if total_gb and total_gb > 0 else None,
        reserve_gb=reserve,
        budget_gb=budget,
        turn_mem_gb=turn_mem,
    )


def extra_permit_fits(
    sizing: CapSizing,
    *,
    host_available_gb: float | None,
    tightest_available_gb: float | None,
) -> bool:
    """Whether live memory has room for one more turn above ``LEGACY_CEIL``.

    The host must keep the OS reserve free after the turn, and the tightest of
    host, container and agents-slice headroom must fit the turn. An unreadable
    figure denies: above the old ceiling, an unknown host gets nothing extra.
    """
    if host_available_gb is None or host_available_gb <= 0:
        return False
    if host_available_gb - sizing.turn_mem_gb < sizing.reserve_gb:
        return False
    if tightest_available_gb is not None and tightest_available_gb >= 0:
        return tightest_available_gb >= sizing.turn_mem_gb
    return True


# -- host readings -----------------------------------------------------------

_static_cache: tuple[float, float | None, float | None] | None = None
_live_cache: tuple[float, float | None, float | None] | None = None


def _read_static() -> tuple[float | None, float | None]:
    """(total RAM GB, agents-slice memory.high GB), each None when unreadable."""
    total: float | None = None
    slice_high: float | None = None
    try:
        from kiro_crew import platform_compat

        mib = platform_compat.host_total_mib()
        total = mib / 1024 if mib > 0 else None
    except Exception:
        logger.debug("host total memory unreadable", exc_info=True)
    try:
        from kiro_crew import platform_compat

        if platform_compat.IS_LINUX:
            from kiro_crew import sandbox

            slice_dir = sandbox._agents_slice_cgroup_dir()
            if slice_dir is not None:
                limits = [
                    v
                    for v in (
                        sandbox.read_cgroup_int(slice_dir / "memory.high"),
                        sandbox.read_cgroup_int(slice_dir / "memory.max"),
                    )
                    if v is not None
                ]
                if limits:
                    slice_high = min(limits) / _GIB
    except Exception:
        logger.debug("agents-slice ceiling unreadable", exc_info=True)
    return total, slice_high


def host_static(now: float | None = None) -> tuple[float | None, float | None]:
    """Cached :func:`_read_static`; total RAM does not move and the slice rarely does."""
    global _static_cache
    now = time.monotonic() if now is None else now
    if _static_cache is None or now - _static_cache[0] > _STATIC_CACHE_SECS:
        total, slice_high = _read_static()
        _static_cache = (now, total, slice_high)
    return _static_cache[1], _static_cache[2]


def _read_live() -> tuple[float | None, float | None]:
    """(host MemAvailable GB, tightest of host/container/slice headroom GB)."""
    host: float | None = None
    tightest: float | None = None
    try:
        from kiro_crew import platform_compat

        mib = platform_compat.host_available_mib()
        host = mib / 1024 if mib > 0 else None
    except Exception:
        logger.debug("host available memory unreadable", exc_info=True)
    try:
        from kiro_crew.subagent import _available_memory_gb

        val = _available_memory_gb()
        tightest = val if val >= 0 else None
    except Exception:
        logger.debug("tightest memory headroom unreadable", exc_info=True)
    return host, tightest


def host_live(now: float | None = None) -> tuple[float | None, float | None]:
    """Cached :func:`_read_live`, so a burst of releases reads memory once."""
    global _live_cache
    now = time.monotonic() if now is None else now
    if _live_cache is None or now - _live_cache[0] > _MEMORY_CACHE_SECS:
        host, tightest = _read_live()
        _live_cache = (now, host, tightest)
    return _live_cache[1], _live_cache[2]


# -- the limiter -------------------------------------------------------------


class TurnLimiter:
    """A FIFO counting limiter whose capacity can change while permits are held.

    ``asyncio.Semaphore`` cannot be resized, and a busy fleet is never idle
    long enough to swap one. Here a raise admits waiters at once,
    and a cut lets running turns finish and holds new ones until the count is
    under the new cap. Permits above ``LEGACY_CEIL`` also need
    ``admit_extra()`` to say live memory fits one more turn.

    Waiters are served strictly in arrival order, as ``asyncio.Semaphore``
    serves them: a newcomer never overtakes a queued turn. While anything
    waits, each waiter re-runs ``refresh`` (re-read config, resize) and the
    grant loop every ``poll_secs``, which is how a config edit or freed memory
    reaches a queue that no release would wake.
    """

    def __init__(
        self,
        capacity: int,
        *,
        refresh: Callable[[], None] | None = None,
        admit_extra: Callable[[], bool] | None = None,
        poll_secs: float = POLL_SECS,
    ) -> None:
        self._capacity = max(1, int(capacity))
        self._held = 0
        self._waiters: collections.deque[asyncio.Future[None]] = collections.deque()
        self._refresh = refresh
        self._admit_extra = admit_extra
        self._poll_secs = poll_secs
        self._last_refresh = 0.0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def held(self) -> int:
        return self._held

    @property
    def waiting(self) -> int:
        return sum(1 for f in self._waiters if not f.done())

    def locked(self) -> bool:
        """True when a new acquire would wait (same contract as ``Semaphore.locked``)."""
        return bool(self.waiting) or not self._room()

    def resize(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._grant()

    def _room(self) -> bool:
        if self._held >= self._capacity:
            return False
        if self._held < LEGACY_CEIL or self._admit_extra is None:
            return True
        try:
            return bool(self._admit_extra())
        except Exception:
            logger.debug(
                "background-turn memory gate failed; denying the extra permit", exc_info=True
            )
            return False

    def _grant(self) -> None:
        while self._waiters:
            head = self._waiters[0]
            if head.done():  # cancelled while queued
                self._waiters.popleft()
                continue
            if not self._room():
                return
            self._waiters.popleft()
            self._held += 1
            head.set_result(None)

    def _poll(self) -> None:
        # One refresh per poll interval, however many turns are waiting: each
        # refresh reads config.json, and every waiter's timer lands here.
        now = time.monotonic()
        if self._refresh is not None and now - self._last_refresh >= self._poll_secs / 2:
            self._last_refresh = now
            try:
                self._refresh()
            except Exception:
                logger.debug("background-turn cap refresh failed", exc_info=True)
        self._grant()

    async def acquire(self) -> bool:
        if not self._waiters and self._room():
            self._held += 1
            return True
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            while not fut.done():
                try:
                    await asyncio.wait_for(asyncio.shield(fut), self._poll_secs)
                except asyncio.TimeoutError:
                    self._poll()
        except BaseException:
            if fut.done() and not fut.cancelled():
                # Granted in the same tick it was cancelled: hand the permit on.
                self.release()
            else:
                fut.cancel()
                self._grant()
            raise
        return True

    def release(self) -> None:
        if self._held <= 0:
            raise ValueError("TurnLimiter released more than acquired")
        self._held -= 1
        self._grant()
