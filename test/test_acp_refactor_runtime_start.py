"""Characterization of the session-start machinery: the gate, its permits, collectors.

Pins every ``StartCollector`` outcome with the invariants each must leave behind
(permit released once, gate drained, collector unregistered, ``adopt`` refused
once settled), how ``session_start_gate`` sizes itself (live snapshot first, the
off-loop config read as the fallback), the gate/permit bookkeeping, the
cold-start admission's cancel-while-queued path, the controller sample, the
start-outcome tokens and the start-budget resolvers' defaults and clamps.

The code is reached only through the ``kiro_crew.acp.runtime`` facade. Only names
defined there, or attributes of shared modules (``kiro_crew.config.live``,
``kiro_crew.adaptive.controller``, the config class), are patched, so these tests
hold unchanged before and after the definitions move to their owner module.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import kiro_crew.adaptive.controller as controller_mod
import kiro_crew.config.live as live_mod
from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp.session_handle import AcpRuntimeDead
from kiro_crew.config.loader import KiroCrewConfig

# Upper bound for an await the test itself must unblock. It exists so a hang fails
# here by name rather than as pytest's --timeout; no passing run measures it.
_BACKSTOP = 10.0
_REQ_ID = 7


@pytest.fixture(autouse=True)
def _fresh_gates():
    # Same shape as test_session_start_gate's _fast_paths: a fresh gate per test.
    acp_runtime._session_start_gates.clear()
    yield
    acp_runtime._session_start_gates.clear()


@pytest.fixture
def gate_sized_two(monkeypatch):
    """``session_start_gate()`` sized by the test, never by the host's config file."""
    monkeypatch.setattr(live_mod, "snapshot", lambda: None)
    monkeypatch.setattr(acp_runtime, "_resolve_session_start_concurrency", lambda: 2)


def _live_concurrency(value: object):
    return lambda: SimpleNamespace(agent=SimpleNamespace(session_start_concurrency=value))


class _FakeRuntime:
    """The four runtime members a StartCollector touches."""

    def __init__(self) -> None:
        self._pending_requests: dict[int, Any] = {}
        self._dead = False
        self._start_collectors: dict[int, Any] = {}
        self.torn: list[str] = []

    async def _teardown_late_session(self, session_id: str) -> None:
        self.torn.append(session_id)


async def _adopter_raises(session_id: str, response: dict) -> bool:
    raise ValueError("adopter failed")


async def _adopter_accepts(session_id: str, response: dict) -> bool:
    return True


async def _adopter_declines(session_id: str, response: dict) -> bool:
    return False


def test_start_outcome_tokens():
    assert (
        acp_runtime.START_OUTCOME_ADOPTED,
        acp_runtime.START_OUTCOME_TORN_DOWN,
        acp_runtime.START_OUTCOME_ABANDONED,
        acp_runtime.START_OUTCOME_RUNTIME_DEAD,
        acp_runtime.START_OUTCOME_ERROR,
    ) == ("adopted", "torn_down", "abandoned", "runtime_dead", "error")


def test_start_budget_constants():
    assert acp_runtime._START_COLLECT_TIMEOUT_DEFAULT == 300.0
    assert acp_runtime._SESSION_NEW_TIMEOUT == 90.0
    assert acp_runtime._SESSION_START_CONCURRENCY_DEFAULT == 2
    assert acp_runtime._SESSION_START_CONCURRENCY_FLOOR == 1
    assert acp_runtime._COLLECTOR_PERMIT_HEADROOM == 1
    assert acp_runtime._COLD_START_MAX_CONCURRENT == 2


# (how the late answer arrives, adopter, collector timeout, outcome, session id, torn down)
_COLLECTOR_ROWS = [
    pytest.param(
        lambda fut: fut.set_exception(ValueError("late")),
        None,
        5.0,
        "error",
        "",
        [],
        id="late-generic-exception",
    ),
    pytest.param(
        lambda fut: fut.set_result({"foo": 1}),
        None,
        5.0,
        "error",
        "",
        [],
        id="answer-without-session-id",
    ),
    pytest.param(
        lambda fut: fut.set_result({"sessionId": "s1"}),
        _adopter_raises,
        5.0,
        "torn_down",
        "s1",
        ["s1"],
        id="adopter-raises-tears-down",
    ),
    pytest.param(
        lambda fut: fut.set_result({"sessionId": "s1"}),
        _adopter_accepts,
        5.0,
        "adopted",
        "s1",
        [],
        id="adopted",
    ),
    pytest.param(
        lambda fut: fut.set_result({"sessionId": "s1"}),
        _adopter_declines,
        5.0,
        "torn_down",
        "s1",
        ["s1"],
        id="adopter-declines",
    ),
    pytest.param(
        lambda fut: fut.set_exception(AcpRuntimeDead("dead")),
        None,
        5.0,
        "runtime_dead",
        "",
        [],
        id="runtime-dead",
    ),
    pytest.param(None, None, 0.05, "abandoned", "", [], id="never-answered"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolve, adopter, timeout, outcome, session_id, torn", _COLLECTOR_ROWS)
async def test_start_collector_outcome(resolve, adopter, timeout, outcome, session_id, torn):
    rt = _FakeRuntime()
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    gate = acp_runtime.SessionStartGate(2)
    permit = await asyncio.wait_for(gate.acquire(), timeout=_BACKSTOP)
    assert permit.hold_for_collector() is True
    collector = acp_runtime.StartCollector(rt, _REQ_ID, future, permit=permit, timeout=timeout)
    rt._start_collectors[_REQ_ID] = collector
    if adopter is not None:
        assert collector.adopt(adopter) is True

    assert collector.start() is collector
    task = collector._task
    assert collector.start() is collector
    assert collector._task is task
    try:
        if resolve is not None:
            resolve(future)
        await asyncio.wait_for(collector.settled.wait(), timeout=_BACKSTOP)
        await asyncio.wait_for(task, timeout=_BACKSTOP)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert (collector.outcome, collector.session_id, rt.torn) == (outcome, session_id, torn)
    assert permit.released is True
    assert (gate.active, gate.collector_holds, gate.releases) == (0, 0, 1)
    assert _REQ_ID not in rt._start_collectors
    assert collector.is_settled is True
    assert collector.gate_released() is True
    assert collector.adopt(_adopter_accepts) is False


@pytest.mark.asyncio
async def test_gate_is_sized_from_the_live_snapshot_when_armed(monkeypatch):
    resolved: list[bool] = []
    monkeypatch.setattr(live_mod, "snapshot", _live_concurrency(3))
    monkeypatch.setattr(
        acp_runtime, "_resolve_session_start_concurrency", lambda: resolved.append(True) or 9
    )
    gate = await asyncio.wait_for(acp_runtime.session_start_gate(), timeout=_BACKSTOP)
    assert (gate.limit, gate.collector_hold_ceiling) == (3, 2)
    assert await acp_runtime.session_start_gate() is gate
    assert resolved == []


@pytest.mark.asyncio
async def test_unreadable_live_value_falls_back_to_the_config_read(monkeypatch):
    monkeypatch.setattr(live_mod, "snapshot", _live_concurrency("x"))
    monkeypatch.setattr(acp_runtime, "_resolve_session_start_concurrency", lambda: 5)
    gate = await asyncio.wait_for(acp_runtime.session_start_gate(), timeout=_BACKSTOP)
    assert gate.limit == 5


def test_gate_counts_outside_a_running_loop():
    assert acp_runtime.session_start_gate_counts() == (0, 0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("gate_sized_two")
async def test_gate_counts_track_the_current_loop_gate():
    assert acp_runtime.session_start_gate_counts() == (0, 0)
    gate = await asyncio.wait_for(acp_runtime.session_start_gate(), timeout=_BACKSTOP)
    assert gate.limit == 2
    permit = await asyncio.wait_for(gate.acquire(), timeout=_BACKSTOP)
    assert acp_runtime.session_start_gate_counts() == (1, 0)
    permit.release()
    assert acp_runtime.session_start_gate_counts() == (0, 0)


@pytest.mark.asyncio
async def test_permit_bookkeeping_is_idempotent():
    gate = acp_runtime.SessionStartGate(3)
    permit = await asyncio.wait_for(gate.acquire(), timeout=_BACKSTOP)
    assert permit.queue_wait_ms >= 0.0
    assert (permit.hold_for_collector(), permit.hold_for_collector()) == (True, False)
    assert gate.collector_holds == 1
    assert (permit.release(), permit.release()) == (True, False)
    assert (gate.active, gate.collector_holds, gate.releases) == (0, 0, 1)
    assert permit.hold_for_collector() is False


@pytest.mark.asyncio
async def test_single_permit_gate_reserves_its_only_slot():
    gate = acp_runtime.SessionStartGate(1)
    assert gate.collector_hold_ceiling == 0
    permit = await asyncio.wait_for(gate.acquire(), timeout=_BACKSTOP)
    assert permit.hold_for_collector() is False
    assert permit.collector_held is False
    permit.release()


@pytest.mark.parametrize("requested, limit", [(0, 1), (-3, 1), ("4", 4)])
def test_gate_limit_is_clamped_to_the_floor(requested, limit):
    assert acp_runtime.SessionStartGate(requested).limit == limit


@pytest.mark.asyncio
async def test_cold_start_admission_cancelled_while_queued():
    admission = acp_runtime._ColdStartAdmission(1)
    await asyncio.wait_for(admission.acquire(), timeout=_BACKSTOP)
    queued = asyncio.ensure_future(admission.acquire())
    try:

        async def _until_queued() -> None:
            while admission.queued != 1:
                await asyncio.sleep(0)

        await asyncio.wait_for(_until_queued(), timeout=_BACKSTOP)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(queued, timeout=_BACKSTOP)
    finally:
        if not queued.done():
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
    assert (admission.queued, admission.active) == (0, 1)
    admission.release()
    assert admission.active == 0


def test_record_session_start_swallows_a_raising_controller(monkeypatch):
    received: list[tuple[tuple, dict]] = []

    class _Controller:
        def record_start(self, *args, **kwargs):
            received.append((args, kwargs))
            raise RuntimeError("controller broke")

    monkeypatch.setattr(controller_mod, "current", lambda: _Controller())
    acp_runtime._record_session_start(time.monotonic(), ok=False, attributable_timeout=True)
    [(args, kwargs)] = received
    assert len(args) == 1 and args[0] >= 0.0
    assert kwargs == {"ok": False, "attributable_timeout": True, "key": "acp:session/new"}


def test_record_session_start_without_a_controller_is_a_no_op(monkeypatch):
    monkeypatch.setattr(controller_mod, "current", lambda: None)
    assert acp_runtime._record_session_start(time.monotonic(), ok=True) is None


@pytest.mark.parametrize(
    "agent, expected",
    [
        pytest.param(None, (300.0, 90.0, 2), id="unreadable-config-uses-defaults"),
        pytest.param(
            SimpleNamespace(
                start_collect_timeout_secs=5,
                session_start_timeout_secs=30,
                session_start_concurrency=0,
            ),
            (10.0, 90.0, 1),
            id="values-below-the-floors-are-clamped",
        ),
        pytest.param(
            SimpleNamespace(
                start_collect_timeout_secs=42,
                session_start_timeout_secs=120,
                session_start_concurrency=4,
            ),
            (42.0, 120.0, 4),
            id="values-above-the-floors-are-kept",
        ),
    ],
)
def test_start_budget_resolvers(agent, expected):
    def _load(cls, *args, **kwargs):
        if agent is None:
            raise OSError("config unreadable")
        return SimpleNamespace(agent=agent)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(KiroCrewConfig, "load", classmethod(_load))
        resolved = (
            acp_runtime._resolve_start_collect_timeout(),
            acp_runtime._resolve_session_start_timeout(),
            acp_runtime._resolve_session_start_concurrency(),
        )
    assert resolved == expected
