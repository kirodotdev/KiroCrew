"""The ``auto`` start gates widen when the host has more free memory than at boot.

A gateway that boots while memory is short sizes its ``session/new`` gate and
the two cold-start widths from that moment's reading. A later reading raises
them; nothing narrows them, nothing passes the width the cores give, and an
explicit setting is left alone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import cold_start_sizing
from kiro_crew import session_start_sizing as sss
from kiro_crew import start_gate_widening as widening
from kiro_crew.acp import runtime_start
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import await_until


def _agent(session_start: object = "auto") -> SimpleNamespace:
    return SimpleNamespace(
        session_start_concurrency=session_start,
        cold_start_concurrency="auto",
        runtime_spawn_concurrency="auto",
    )


@pytest.fixture
def host(monkeypatch):
    """A host whose cores and free memory the test sets; ``host.free_gb`` is re-read."""
    state = SimpleNamespace(cpus=32, free_gb=7.9)

    def _probe() -> sss.HostCapacity:
        return sss.HostCapacity(
            affinity_cpus=state.cpus, quota_cpus=None, cpus=state.cpus, available_gb=state.free_gb
        )

    sss._reset_for_tests()
    monkeypatch.setattr(sss, "probe_host_capacity", _probe)
    monkeypatch.setattr(sss, "_read_available_gb", lambda: state.free_gb)
    yield state
    sss._reset_for_tests()


@pytest.fixture
def configured(monkeypatch):
    """The live config and the loaded one both answer with the agent the test sets."""
    holder = SimpleNamespace(agent=_agent())
    monkeypatch.setattr(runtime_start.live, "snapshot", lambda: holder)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: holder))
    runtime_start._session_start_gates.clear()
    yield holder
    runtime_start._session_start_gates.clear()


class _Sessions:
    def __init__(self) -> None:
        self.widened = 0

    def widen_cold_start_queues(self) -> None:
        self.widened += 1


@pytest.mark.asyncio
async def test_a_gateway_booted_short_of_memory_reaches_the_wider_gates_later(host, configured):
    gate = await runtime_start.session_start_gate()
    assert gate.limit == 2
    assert cold_start_sizing.cached_runtime_spawn_concurrency(configured.agent) == 2
    sessions = _Sessions()

    host.free_gb = 40.0
    grew = await widening.reread_host_and_widen(configured.agent, sessions)

    assert grew is True
    assert sessions.widened == 1
    # min(32 // 4, 40 // 3): the cores set it now.
    assert (await runtime_start.session_start_gate()) is gate
    assert gate.limit == 8
    assert cold_start_sizing.cached_cold_start_concurrency(configured.agent) == 8
    assert cold_start_sizing.cached_runtime_spawn_concurrency(configured.agent) == 4


@pytest.mark.asyncio
async def test_a_gateway_that_boots_wide_is_unchanged(host, configured):
    host.free_gb = 200.0
    gate = await runtime_start.session_start_gate()
    assert gate.limit == 8
    sessions = _Sessions()

    async def _never(_secs: float) -> None:
        raise AssertionError("the cores already set the width: nothing to wait for")

    await widening.widen_start_gates_as_memory_frees(configured.agent, sessions, sleep=_never)
    host.free_gb = 400.0
    assert await widening.reread_host_and_widen(configured.agent, sessions) is False

    assert (await runtime_start.session_start_gate()).limit == 8
    assert sessions.widened == 0


@pytest.mark.asyncio
async def test_a_lower_reading_never_narrows_a_gate(host, configured):
    host.free_gb = 13.0
    gate = await runtime_start.session_start_gate()
    assert gate.limit == 4

    host.free_gb = 2.0
    assert await widening.reread_host_and_widen(configured.agent, _Sessions()) is False

    assert sss.host_capacity().available_gb == 13.0
    assert (await runtime_start.session_start_gate()).limit == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(("cpus", "bound"), [(32, 8), (128, 16)], ids=["cores", "ceiling"])
async def test_no_reading_widens_past_what_the_cores_give(host, configured, cpus, bound):
    host.cpus = cpus
    gate = await runtime_start.session_start_gate()
    assert gate.limit == 2

    host.free_gb = 5000.0
    await widening.reread_host_and_widen(configured.agent, _Sessions())

    assert (await runtime_start.session_start_gate()).limit == bound
    assert sss.memory_limits_auto_width() is False


@pytest.mark.asyncio
async def test_an_explicit_setting_still_wins(host, configured):
    configured.agent = _agent(session_start=3)
    gate = await runtime_start.session_start_gate()
    assert gate.limit == 3

    async def _never(_secs: float) -> None:
        raise AssertionError("an explicit width is fixed until a restart")

    host.free_gb = 400.0
    sessions = _Sessions()
    await widening.widen_start_gates_as_memory_frees(configured.agent, sessions, sleep=_never)
    assert await widening.reread_host_and_widen(configured.agent, sessions) is False

    assert (await runtime_start.session_start_gate()).limit == 3
    assert sessions.widened == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("built_from", "edited_to", "limit_after"),
    [(1, "auto", 1), ("auto", 3, 8)],
    ids=["explicit-then-auto", "auto-then-explicit"],
)
async def test_an_edit_of_the_setting_changes_no_gate_before_a_restart(
    host, configured, built_from, edited_to, limit_after
):
    configured.agent = _agent(session_start=built_from)
    gate = await runtime_start.session_start_gate()
    built_at = gate.limit

    configured.agent = _agent(session_start=edited_to)
    assert (await runtime_start.session_start_gate()).limit == built_at
    host.free_gb = 40.0
    sss.refresh_host_capacity()

    # The gate keeps the mode it was built with: an explicit one stays fixed, an
    # ``auto`` one still follows the host.
    assert (await runtime_start.session_start_gate()) is gate
    assert gate.limit == limit_after


@pytest.mark.asyncio
async def test_the_host_is_re_read_until_the_cores_set_the_width(host, configured):
    assert sss.host_capacity().available_gb == 7.9
    waits: list[float] = []

    async def _memory_frees(secs: float) -> None:
        waits.append(secs)
        host.free_gb += 9.0

    sessions = _Sessions()
    await widening.widen_start_gates_as_memory_frees(
        configured.agent, sessions, sleep=_memory_frees
    )

    # 16.9 GB gives 5, 25.9 GB gives 8: two re-reads, each one a step wider.
    assert waits == [widening.HOST_REREAD_INTERVAL_SECS] * 2
    assert sessions.widened == 2
    assert sss.effective_session_start_concurrency("auto") == 8


@pytest.mark.asyncio
async def test_a_failed_re_read_widens_nothing(host, configured, monkeypatch):
    gate = await runtime_start.session_start_gate()

    def _unreadable() -> bool:
        raise OSError("the host files are unreadable")

    monkeypatch.setattr(widening, "refresh_host_capacity", _unreadable)
    host.free_gb = 40.0
    sessions = _Sessions()

    assert await widening.reread_host_and_widen(configured.agent, sessions) is False
    assert sessions.widened == 0
    assert (await runtime_start.session_start_gate()) is gate and gate.limit == 2


@pytest.mark.asyncio
async def test_a_failed_queue_widening_still_widens_the_gate(host, configured):
    gate = await runtime_start.session_start_gate()

    class _Broken:
        def widen_cold_start_queues(self) -> None:
            raise RuntimeError("the manager is closing")

    host.free_gb = 40.0

    assert await widening.reread_host_and_widen(configured.agent, _Broken()) is True
    assert (await runtime_start.session_start_gate()) is gate and gate.limit == 8


@pytest.mark.asyncio
async def test_widening_a_gate_in_use_neither_loses_nor_doubles_a_permit(host, configured):
    gate = await runtime_start.session_start_gate()
    first = await gate.acquire(StartPriority.BACKGROUND)
    second = await gate.acquire(StartPriority.FOREGROUND)
    queued = asyncio.ensure_future(gate.acquire(StartPriority.BACKGROUND))
    await asyncio.sleep(0)
    assert not queued.done() and gate.queued == 1

    host.free_gb = 13.0
    await widening.reread_host_and_widen(configured.agent, None)
    await runtime_start.session_start_gate()

    # The waiter takes one of the two new permits; the held ones stay held.
    third = await await_until(queued, what="the queued start's permit")
    assert (gate.limit, gate.active, gate.queued) == (4, 3, 0)
    for permit in (first, second, third):
        permit.release()
        permit.release()
    assert (gate.active, gate.releases) == (0, 3)
    held = [await gate.acquire() for _ in range(4)]
    assert gate.semaphore.locked()
    for permit in held:
        permit.release()
