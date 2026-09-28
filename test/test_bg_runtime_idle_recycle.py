"""The shared background runtime is recycled even when nobody reuses it.

``_is_stale()``'s ceilings (6 h uptime, 500 MiB RSS) were only ever evaluated on
the reuse path in ``get_bg_session``, and the shared runtime's pid is shielded
from the periodic orphan sweep for its whole life. A runtime that stopped being
reused was therefore never asked the question again and never reaped by anything
else: it lived until the gateway restarted. Observed on an operator host as 11
agent runtimes holding 6.0 GB, six of them 14.5 h old, against 4 live slots.

These tests pin the periodic caller that asks off the reuse path, and the two
properties that keep it safe: it never touches a runtime with a live or
initializing session, and it PARKS rather than kills, because ``get_bg_session``
pins the runtime under the lock and only raises
``_session_inits_in_flight`` after releasing it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        return m

    return factory


def _runtime(*, busy: bool, stale: str | None, alive: bool = True):
    rt = AsyncMock()
    rt.pid = _UNALLOCATABLE_PID
    rt.is_alive = lambda: alive
    rt.has_active_or_initializing_sessions = lambda: busy
    rt._is_stale = AsyncMock(return_value=stale)
    rt.kill = AsyncMock()
    return rt


@pytest.mark.asyncio
async def test_an_idle_stale_runtime_is_parked_and_the_slot_freed(cfg):
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    rt = _runtime(busy=False, stale="rss")
    mgr._bg_runtime = rt

    assert await mgr._reap_idle_stale_bg_runtime() is True

    assert mgr._bg_runtime is None
    assert mgr._draining_bg_runtimes == [rt]
    # Parked, NOT killed here: get_bg_session pins the runtime under the lock
    # and releases it before create_session raises _session_inits_in_flight, so
    # a kill in that window would surface as AcpRuntimeDead on an innocent
    # caller. The drain reaper kills it on a later tick.
    rt.kill.assert_not_awaited()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_busy_runtime_is_left_alone(cfg):
    """A co-tenant session must never be displaced by this sweep."""
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    rt = _runtime(busy=True, stale="age")
    mgr._bg_runtime = rt

    assert await mgr._reap_idle_stale_bg_runtime() is False

    assert mgr._bg_runtime is rt
    assert mgr._draining_bg_runtimes == []
    rt.kill.assert_not_awaited()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_fresh_idle_runtime_is_left_alone(cfg):
    """The false-positive guard: idle is not a reason to recycle, staleness is."""
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    rt = _runtime(busy=False, stale=None)
    mgr._bg_runtime = rt

    assert await mgr._reap_idle_stale_bg_runtime() is False

    assert mgr._bg_runtime is rt
    await mgr.close_all()


@pytest.mark.asyncio
async def test_an_unanswerable_session_probe_preserves_the_runtime(cfg):
    """Fail toward keeping work: a probe that raises must not retire anything."""
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    rt = _runtime(busy=False, stale="rss")
    rt.has_active_or_initializing_sessions = MagicMock(side_effect=RuntimeError("no answer"))
    mgr._bg_runtime = rt

    assert await mgr._reap_idle_stale_bg_runtime() is False

    assert mgr._bg_runtime is rt
    await mgr.close_all()


@pytest.mark.asyncio
async def test_an_unanswerable_staleness_probe_preserves_the_runtime(cfg):
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    rt = _runtime(busy=False, stale=None)
    rt._is_stale = AsyncMock(side_effect=OSError("/proc unreadable"))
    mgr._bg_runtime = rt

    assert await mgr._reap_idle_stale_bg_runtime() is False

    assert mgr._bg_runtime is rt
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_slot_replaced_while_the_probe_awaited_is_not_retired(cfg):
    """The staleness probe awaits an executor, so the slot can move under it.

    Retiring on that stale reading would park a runtime that is now somebody
    else's live shared process.
    """
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    old = _runtime(busy=False, stale="age")
    replacement = _runtime(busy=False, stale=None)

    async def _stale_then_swap():
        mgr._bg_runtime = replacement
        return "age"

    old._is_stale = AsyncMock(side_effect=_stale_then_swap)
    mgr._bg_runtime = old

    assert await mgr._reap_idle_stale_bg_runtime() is False

    assert mgr._bg_runtime is replacement
    assert mgr._draining_bg_runtimes == []
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_closing_manager_parks_nothing(cfg):
    """Parking after the shutdown sweep would strand a shielded process."""
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    rt = _runtime(busy=False, stale="rss")
    mgr._bg_runtime = rt
    mgr._closing = True

    assert await mgr._reap_idle_stale_bg_runtime() is False

    assert mgr._draining_bg_runtimes == []
    mgr._closing = False
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_dead_runtime_is_not_this_sweeps_business(cfg):
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    rt = _runtime(busy=False, stale="rss", alive=False)
    mgr._bg_runtime = rt

    assert await mgr._reap_idle_stale_bg_runtime() is False
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_watchdog_hook_runs_the_sweep_with_nothing_parked(cfg):
    """The sweep runs even when the drain list is empty.

    Gating the whole hook body on a non-empty drain list would run the periodic
    caller only on a gateway that already had a parked runtime -- i.e. never, on
    the idle host where the unbounded growth was measured.
    """
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    assert mgr._draining_bg_runtimes == []

    with patch.object(mgr, "_reap_idle_stale_bg_runtime", AsyncMock(return_value=True)) as sweep:
        await mgr._bg_drain_reap_hook()

    sweep.assert_awaited_once()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_failing_sweep_does_not_break_the_hook(cfg):
    mgr = SessionManager(cfg, provider_factory=_provider_factory())

    with patch.object(
        mgr, "_reap_idle_stale_bg_runtime", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await mgr._bg_drain_reap_hook()  # must not raise

    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_hook_still_reaps_drained_runtimes(cfg):
    """The pre-existing behaviour, kept intact alongside the new pass."""
    mgr = SessionManager(cfg, provider_factory=_provider_factory())
    drained = _runtime(busy=False, stale=None)
    mgr._draining_bg_runtimes = [drained]

    await mgr._bg_drain_reap_hook()

    drained.kill.assert_awaited_once()
    assert mgr._draining_bg_runtimes == []
    await mgr.close_all()
