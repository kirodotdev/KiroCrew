"""The warm pool holds its fill while kiro-cli is signed out, and fills once sign-in returns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from session_helpers import cfg_fixture  # noqa: F401 -- the fixture is requested by name

from kiro_crew.acp import kiro_signin_hold
from kiro_crew.acp.transport_errors import AcpAuthRequired
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.session import SessionManager

SWEEPS = 5


def _factory(starts: list, state: dict):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.memory_mode = "persistent"
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m._client = SimpleNamespace(backend=ACP_BACKEND_KAS)

        async def start():
            starts.append(1)
            if state["signed_out"]:
                raise AcpAuthRequired("Not signed in to Kiro.", backend=ACP_BACKEND_KAS)

        m.start = start
        return m

    return factory


async def _run_sweeps(mgr: SessionManager, count: int) -> None:
    for _ in range(count):
        await mgr._sweep_warm_pool_once()
        while pending := [t for t in mgr._background_tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_healthy_pool_starts_once(cfg):
    starts: list = []
    mgr = SessionManager(cfg, provider_factory=_factory(starts, {"signed_out": False}))
    mgr._pool_size = 1
    await _run_sweeps(mgr, SWEEPS)
    assert len(starts) == 1
    assert mgr._warm_pool.qsize() == 1
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_signed_out_pool_does_not_start_on_every_sweep(cfg):
    starts: list = []
    mgr = SessionManager(cfg, provider_factory=_factory(starts, {"signed_out": True}))
    mgr._pool_size = 1
    await _run_sweeps(mgr, SWEEPS)
    assert mgr._warm_pool.qsize() == 0
    assert len(starts) == 1
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_pool_fills_on_the_next_sweep_once_sign_in_returns(cfg):
    starts: list = []
    state = {"signed_out": True}
    mgr = SessionManager(cfg, provider_factory=_factory(starts, state))
    mgr._pool_size = 1
    await _run_sweeps(mgr, 2)
    assert len(starts) == 1
    # What a Kiro start that gets a session records once the user signs in again.
    state["signed_out"] = False
    kiro_signin_hold.note_signed_in(ACP_BACKEND_KAS)
    await _run_sweeps(mgr, 1)
    assert len(starts) == 2
    assert mgr._warm_pool.qsize() == 1
    await mgr.close_all()


def test_the_hold_backs_off_and_retries_when_nothing_else_signs_in(monkeypatch):
    from kiro_crew.testing.clock import ManualClock

    clock = ManualClock()
    clock.install(monkeypatch, kiro_signin_hold)
    kiro_signin_hold.note_signed_out(ACP_BACKEND_KAS)
    assert kiro_signin_hold.held(ACP_BACKEND_KAS)
    clock.advance(kiro_signin_hold._HOLD_FIRST_SECS)
    assert not kiro_signin_hold.held(ACP_BACKEND_KAS)
    # The retry failed again: the next hold is twice as long, up to the cap.
    for _ in range(10):
        kiro_signin_hold.note_signed_out(ACP_BACKEND_KAS)
    assert kiro_signin_hold._HELD[ACP_BACKEND_KAS] == (
        clock.monotonic() + kiro_signin_hold._HOLD_MAX_SECS,
        kiro_signin_hold._HOLD_MAX_SECS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("signed_in", [True, False])
async def test_a_kiro_start_that_gets_a_session_releases_the_hold(signed_in):
    from kiro_crew.providers import acp_runtime_start

    async def impl(phases, meta):
        if not signed_in:
            raise AcpAuthRequired("Not signed in to Kiro.", backend=ACP_BACKEND_KAS)

    provider = SimpleNamespace(
        _client=SimpleNamespace(_session_key=None, backend=ACP_BACKEND_KAS),
        _start_kiro_runtime_impl=impl,
        _emit_kiro_startup_metric=lambda *args: None,
    )
    kiro_signin_hold.note_signed_out(ACP_BACKEND_KAS)
    if signed_in:
        await acp_runtime_start._start_kiro_runtime(provider)
    else:
        with pytest.raises(AcpAuthRequired):
            await acp_runtime_start._start_kiro_runtime(provider)
    assert kiro_signin_hold.held(ACP_BACKEND_KAS) is not signed_in


@pytest.mark.asyncio
async def test_another_harness_neither_holds_nor_releases_the_pool():
    from kiro_crew.agent_sdk.kas_floor_wait import note_warm_pool_start_failed
    from kiro_crew.providers import acp_runtime_start

    async def impl(phases, meta):
        return None

    def pool(backend):
        return SimpleNamespace(_client=SimpleNamespace(backend=backend))

    signed_out = AcpAuthRequired("signed out")
    note_warm_pool_start_failed(signed_out, pool(ACP_BACKEND_KAS))
    note_warm_pool_start_failed(signed_out, pool("codex"))
    other = SimpleNamespace(
        _client=SimpleNamespace(_session_key=None, backend=ACP_BACKEND_KIRO),
        _start_kiro_runtime_impl=impl,
        _emit_kiro_startup_metric=lambda *args: None,
    )
    await acp_runtime_start._start_kiro_runtime(other)
    assert kiro_signin_hold.held(ACP_BACKEND_KAS)
    assert kiro_signin_hold.held("codex")
    assert not kiro_signin_hold.held(ACP_BACKEND_KIRO)
