"""The spawn memory floor at the SHIPPED defaults, through the real gate.

The floor is what must remain available AFTER a start is admitted
(``agent.spawn_min_memory_gb``, default ``DEFAULT_SPAWN_MIN_MEMORY_GB``). A start
that will share its parent's runtime is priced at the dedicated projection less
the process it does not launch; a dedicated one at ``max(agent.subagent_cost_gb,
learned settled RSS)``, or the measured unlearned figure before a bucket has one.

The scenarios patch ONLY the host's free-memory reading. Config is the isolated
home's real defaults (no ``KiroCrewConfig`` patch), the free figure goes through
the real ``/proc/meminfo`` parser, and the posture reading (which spawns no
longer consult) reads the same number, so a scenario fails if any default, any
price or the sharing prediction drifts.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
from unittest.mock import MagicMock

import pytest
from overload_fakes import mock_ctx, mock_sessions, wait_taskq_open

import kiro_crew.resource_status as rs
import kiro_crew.subagent as subagent_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import AgentConfig
from kiro_crew.constants import DEFAULT_SPAWN_MIN_MEMORY_GB
from kiro_crew.subagent import (
    _DEDICATED_TOPUP_WAIT_SECS,
    _SHARED_START_MIN_GB,
    _SHARED_START_SAVING_GB,
    _UNLEARNED_DEDICATED_START_GB,
    QUEUED_REASON_LOW_MEMORY,
    SubagentInfo,
    SubagentManager,
    _startup_memory_reserve_gb,
    check_memory_available,
)

# Each scenario patches the reader ON TOP; this satisfies the host-pin ratchet.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

PARENT = "dashboard:floor"
FLOOR = DEFAULT_SPAWN_MIN_MEMORY_GB
COST = 0.5
# What starts are priced at on a fresh install, with nothing learned.
DEDICATED = _UNLEARNED_DEDICATED_START_GB
SHARED = DEDICATED - _SHARED_START_SAVING_GB
_KIB_PER_GIB = 1024 * 1024
_WAIT_SECS = 5.0  # lost-run guard, never the barrier


class _Host:
    """Free memory read through the REAL /proc/meminfo parser (explicit path)."""

    def __init__(self, monkeypatch, tmp_path, gb: float) -> None:
        self.asked: list[float] = []
        self._dir = tmp_path
        self._readings = 0
        self.set(gb)
        real = subagent_mod.check_memory_available

        def _check(min_gb, **_kw):
            self.asked.append(min_gb)
            return real(min_gb=min_gb, path=str(self._meminfo))

        monkeypatch.setattr(subagent_mod, "check_memory_available", _check)
        monkeypatch.setattr(rs, "_read_available_gb", lambda: self.gb)

    def set(self, gb: float) -> None:
        self.gb = gb
        # ceil: a GiB figure floored to whole kB reads a hair under itself.
        # Each reading is a NEW file, published by rebinding the path once it is
        # whole. The top-up reads it from a worker thread, and on Windows a file
        # that thread holds open cannot be renamed over (WinError 5), while an
        # open racing the rename fails and the parser reads that as "no reading",
        # which admits. A reader that took the old path finishes the old file.
        self._readings += 1
        reading = self._dir / f"meminfo.{self._readings}"
        reading.write_text(f"MemAvailable: {math.ceil(gb * _KIB_PER_GIB)} kB\n", encoding="utf-8")
        self._meminfo = reading

    def gated(self) -> list[float]:
        # Host-sizing probes ask with min_gb=0.0; only floor checks count.
        return [a for a in self.asked if a > 0]


async def _manager(monkeypatch, *, eligible: bool):
    sessions = mock_sessions()
    sessions.is_session_sharing_eligible = MagicMock(return_value=eligible)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    mgr = SubagentManager(sessions=sessions, ctx_builder=mock_ctx(), max_concurrent=4)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = 0.0
    mgr._last_spawn_ts = 0.0
    mgr._taskq_admit_wait_secs = 3600.0  # no re-check pass inside a scenario
    started: list[str] = []

    async def held(info) -> None:  # a start that stays warming until teardown
        started.append(info.id)
        await asyncio.Event().wait()

    monkeypatch.setattr(mgr, "_run", held)
    return mgr, started


async def _until(pred, what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_SECS
    while not pred():
        assert loop.time() < deadline, what
        await asyncio.sleep(0.01)


async def _teardown(mgr) -> None:
    mgr._shutting_down = True
    tasks = [t for t in mgr._tasks.values() if not t.done()]
    for t in tasks:
        t.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), _WAIT_SECS)
    mgr._taskq.close()


# --- one constant behind every default --------------------------------------


def test_every_default_of_the_floor_is_the_one_constant() -> None:
    assert DEFAULT_SPAWN_MIN_MEMORY_GB == 2.0
    assert AgentConfig().spawn_min_memory_gb == DEFAULT_SPAWN_MIN_MEMORY_GB
    assert (
        inspect.signature(check_memory_available).parameters["min_gb"].default
        == DEFAULT_SPAWN_MIN_MEMORY_GB
    )
    assert KiroCrewConfig.load().agent.spawn_min_memory_gb == DEFAULT_SPAWN_MIN_MEMORY_GB


@pytest.mark.parametrize("stored", ["not-a-number", None])
def test_an_unreadable_stored_floor_falls_back_to_the_constant(stored) -> None:
    from kiro_crew.config import loader

    agent = loader._build_agent_config({"spawn_min_memory_gb": stored})
    assert agent.spawn_min_memory_gb == DEFAULT_SPAWN_MIN_MEMORY_GB
    assert loader._build_agent_config({}).spawn_min_memory_gb == DEFAULT_SPAWN_MIN_MEMORY_GB


def test_the_scenarios_run_at_the_shipped_defaults() -> None:
    agent = KiroCrewConfig.load().agent
    assert agent.subagent_cost_gb == COST and agent.session_sharing is True
    assert not agent.role_models.get("subagent") and not agent.role_efforts.get("subagent")
    # The measured prices, pinned so a change is a decision, not a drift; no
    # admitted start may take the host below resource_critical_gb.
    assert DEDICATED == 1.0
    assert _SHARED_START_SAVING_GB == 0.35 and SHARED == pytest.approx(0.65)
    assert 0 < _SHARED_START_MIN_GB <= SHARED
    assert agent.resource_critical_gb <= FLOOR


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_unreadable_floor_still_prices_at_the_default(monkeypatch, tmp_path) -> None:
    """The gate's fallback is the same constant, not a stale literal."""
    real = KiroCrewConfig.load()

    class _Agent:
        def __getattr__(self, name):
            if name == "spawn_min_memory_gb":
                raise ValueError("unreadable")
            return getattr(real.agent, name)

    broken = MagicMock(wraps=real)
    broken.agent = _Agent()
    host = _Host(monkeypatch, tmp_path, 2.4)
    mgr, started = await _manager(monkeypatch, eligible=False)
    try:
        with monkeypatch.context() as m:
            m.setattr(subagent_mod.KiroCrewConfig, "load", staticmethod(lambda: broken))
            info = await mgr.spawn_async("one", parent_session_key=PARENT)
        assert info is not None and info.queued is True, info.error
        assert host.gated() == [pytest.approx(FLOOR + DEDICATED)]
    finally:
        await _teardown(mgr)


# --- real-path scenarios ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_4_5_gib_admits_the_first_and_second_dedicated_start(monkeypatch, tmp_path):
    host = _Host(monkeypatch, tmp_path, 4.5)
    mgr, started = await _manager(monkeypatch, eligible=False)
    try:
        first = await mgr.spawn_async("one", parent_session_key=PARENT)
        await _until(lambda: first.id in started, "first dedicated start never ran")
        second = await mgr.spawn_async("two", parent_session_key=PARENT)
        await _until(lambda: second.id in started, "second dedicated start never ran")
        assert first.queued is False and second.queued is False
        # The next start, then the next start plus the first still warming.
        assert host.gated() == [
            pytest.approx(FLOOR + DEDICATED),
            pytest.approx(FLOOR + 2 * DEDICATED),
        ]
        assert first._start_price_gb == pytest.approx(DEDICATED)
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_2_4_gib_queues_a_dedicated_start(monkeypatch, tmp_path):
    host = _Host(monkeypatch, tmp_path, 2.4)
    mgr, started = await _manager(monkeypatch, eligible=False)
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT)
        assert info is not None and info.queued is True and info.done is False
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY
        assert started == []
        assert host.gated() == [pytest.approx(FLOOR + DEDICATED)]
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    ("free_gb", "eligible", "admitted"),
    [(2.7, True, True), (2.6, True, False), (2.7, False, False)],
    ids=["shared-admits", "shared-queues", "dedicated-control-queues"],
)
async def test_a_shared_start_admits_where_a_dedicated_one_waits(
    monkeypatch, tmp_path, free_gb, eligible, admitted
):
    host = _Host(monkeypatch, tmp_path, free_gb)
    mgr, started = await _manager(monkeypatch, eligible=eligible)
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT)
        if admitted:
            await _until(lambda: info.id in started, "shared start never ran")
            # The claim re-entry registered the price its first half checked.
            assert info._start_price_gb == pytest.approx(SHARED)
            assert info._start_priced_shared is True and mgr._claim_prices == {}
        else:
            assert info.queued_reason == QUEUED_REASON_LOW_MEMORY and started == []
        # The control proves the shared PRICE admitted it, not a lower floor.
        assert host.gated() == [pytest.approx(FLOOR + (SHARED if eligible else DEDICATED))]
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_shared_wave_carries_its_price_to_the_next_admission(monkeypatch, tmp_path):
    """The second start's bar charges the first at the price it was admitted at."""
    host = _Host(monkeypatch, tmp_path, 3.4)
    mgr, started = await _manager(monkeypatch, eligible=True)
    try:
        first = await mgr.spawn_async("one", parent_session_key=PARENT)
        await _until(lambda: first.id in started, "first shared start never ran")
        second = await mgr.spawn_async("two", parent_session_key=PARENT)
        await _until(lambda: second.id in started, "second shared start never ran")
        assert host.gated() == [
            pytest.approx(FLOOR + SHARED),
            pytest.approx(FLOOR + 2 * SHARED),
        ]
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "override",
    [
        {"keep": True},
        {"model": "pinned-model"},
        {"reasoning_effort": "high"},
        {"allowed_tools": ["read"]},
        {"bare": True},
    ],
)
async def test_a_start_the_run_will_not_share_is_priced_dedicated(monkeypatch, tmp_path, override):
    """The gate's prediction is the run's own decision, not a copy of it."""
    host = _Host(monkeypatch, tmp_path, 2.8)  # admits a shared start, not a dedicated one
    mgr, started = await _manager(monkeypatch, eligible=True)
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT, **override)
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY and started == []
        assert host.gated() == [pytest.approx(FLOOR + DEDICATED)]
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_role_model_pin_prices_every_start_dedicated(monkeypatch, tmp_path):
    _write_agent_config({"role_models": {"subagent": "pinned-model"}})
    host = _Host(monkeypatch, tmp_path, 2.8)
    mgr, started = await _manager(monkeypatch, eligible=True)
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT)
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY and started == []
        assert host.gated() == [pytest.approx(FLOOR + DEDICATED)]
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_learned_settled_size_raises_the_dedicated_price(monkeypatch, tmp_path):
    """A burst of dedicated starts is reserved at what each will settle at."""
    host = _Host(monkeypatch, tmp_path, 4.5)
    mgr, started = await _manager(monkeypatch, eligible=False)
    mgr._learned_settled_gb = {"kirocrew": 1.4, "other": 9.0}
    try:
        first = await mgr.spawn_async("one", parent_session_key=PARENT)
        await _until(lambda: first.id in started, "first dedicated start never ran")
        second = await mgr.spawn_async("two", parent_session_key=PARENT)
        assert second.queued_reason == QUEUED_REASON_LOW_MEMORY
        # Its own bucket's figure only, never another bucket's.
        assert host.gated() == [pytest.approx(FLOOR + 1.4), pytest.approx(FLOOR + 2 * 1.4)]
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_learned_settled_size_raises_the_shared_price_too(monkeypatch, tmp_path):
    """A shared start still launches the agent's MCP servers; it saves only the
    process, so a heavy roster raises its price with the dedicated projection."""
    host = _Host(monkeypatch, tmp_path, 4.0)
    mgr, started = await _manager(monkeypatch, eligible=True)
    mgr._learned_settled_gb = {"kirocrew": 1.5}
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT)
        await _until(lambda: info.id in started, "shared start never ran")
        assert host.gated() == [pytest.approx(FLOOR + 1.5 - _SHARED_START_SAVING_GB)]
        assert info._start_price_gb == pytest.approx(1.5 - _SHARED_START_SAVING_GB)
    finally:
        await _teardown(mgr)


# --- the reserve's arithmetic --------------------------------------------------


def _write_agent_config(agent: dict) -> None:
    """Write the isolated home's config.json and drop the load cache."""
    import json

    from kiro_crew.config import loader
    from kiro_crew.config.paths import config_dir

    (config_dir() / "config.json").write_text(json.dumps({"agent": agent}), encoding="utf-8")
    loader._invalidate_config_cache()


def _row(**kw) -> SubagentInfo:
    info = SubagentInfo(id=kw.pop("id", "r"), task="t", agent="kirocrew")
    for k, v in kw.items():
        setattr(info, k, v)
    return info


def test_each_warming_row_owes_its_admitted_price_until_it_settles() -> None:
    rows = [
        _row(id="shared-unbound", _start_price_gb=SHARED),
        _row(id="dedicated", _start_price_gb=1.4, last_rss_gb=0.4, _rss_samples=1),
        _row(id="settled", _start_price_gb=1.4, last_rss_gb=1.5, _rss_samples=2),
        # Bound to its parent's runtime, but its MCP servers start after the
        # bind and its reading is a share of the runtime: the full price.
        _row(id="bound", _start_price_gb=SHARED, _session_sharing=True, last_rss_gb=0.4),
        _row(id="bound-settled", _start_price_gb=SHARED, _session_sharing=True, _rss_samples=2),
        _row(id="queued", queued=True),
    ]
    reserve = _startup_memory_reserve_gb(rows, running_count=5, cost_gb=COST, next_start_gb=SHARED)
    # In full until settled: no credit for the summed-RSS reading a row shows.
    assert reserve == pytest.approx(SHARED + SHARED + 1.4 + SHARED)


def test_a_row_nothing_can_measure_settles_once_its_session_has_long_answered() -> None:
    """macOS/Windows have no subtree reading, so ``_rss_samples`` never moves there."""
    now = 10_000.0
    long_ago = now - subagent_mod._SETTLE_AFTER_SECS
    fresh = _row(id="fresh", _start_price_gb=DEDICATED, _first_stream_mono=now - 5)
    old = _row(id="old", _start_price_gb=DEDICATED, _first_stream_mono=long_ago)
    # A respawned process (generation moved on) is warming again, whatever its
    # predecessor's session did.
    respawned = _row(
        id="respawned", _start_price_gb=DEDICATED, _first_stream_mono=long_ago, _rss_generation=1
    )
    for row in (fresh, old, respawned):
        row._first_stream_generation = 0
    reserve = _startup_memory_reserve_gb(
        [fresh, old, respawned], running_count=3, cost_gb=COST, next_start_gb=0.0, now=now
    )
    assert reserve == pytest.approx(2 * DEDICATED)


def test_a_claim_awaiting_registration_is_charged_its_checked_price() -> None:
    reserve = _startup_memory_reserve_gb(
        [], running_count=2, cost_gb=COST, next_start_gb=0.0, claim_prices=[DEDICATED]
    )
    assert reserve == pytest.approx(DEDICATED + COST)


@pytest.mark.parametrize(
    ("learned", "expected"),
    [(1.4, 1.4), (0.3, COST), (5.02, 2.0), (float("inf"), DEDICATED), (float("nan"), DEDICATED)],
    ids=["learned", "never-below-cost", "ceiling", "infinite", "nan"],
)
def test_the_dedicated_projection_is_bounded(learned, expected) -> None:
    """A few outlier readings must not price a bucket out of admission for good."""
    price = subagent_mod._dedicated_start_price_gb(COST, {"kirocrew": learned}, "")
    assert price == pytest.approx(expected)


def test_an_unpriced_row_owes_the_dedicated_projection_for_its_bucket() -> None:
    rows = [_row(id="a"), _row(id="b", agent="heavy"), _row(id="c", agent="fresh")]
    reserve = _startup_memory_reserve_gb(
        rows, running_count=3, cost_gb=COST, settled_gb={"kirocrew": 1.2, "heavy": 0.3}
    )
    # next start (cost) + a at its settled 1.2 + b at max(cost, 0.3) + c unlearned
    assert reserve == pytest.approx(COST + 1.2 + COST + DEDICATED)


# --- a shared price is topped up before the start turns dedicated -------------


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_start_admitted_shared_is_repriced_dedicated_before_its_process(
    monkeypatch, tmp_path
) -> None:
    host = _Host(monkeypatch, tmp_path, 4.0)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents[info.id] = info
    mgr._running_count = 1
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        assert info._start_price_gb == pytest.approx(DEDICATED)
        # The re-check charges this row at the dedicated price, no next start.
        assert host.gated() == [pytest.approx(FLOOR + DEDICATED)]
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    ("parent", "outcomes"),
    [
        pytest.param(
            PARENT, ["dedicated_start_under_memory_pressure"], id="root-held-then-bounded"
        ),
        pytest.param("subagent:busy", [], id="nested-child-never-held"),
    ],
)
async def test_the_top_up_waits_on_the_kernel_pressure_hold(
    monkeypatch, tmp_path, parent: str, outcomes: list[str]
) -> None:
    """A shared start turning dedicated meets the pressure hold too, under the
    top-up's own bound; the figure clears the floor here, so only the hold waits."""
    from kiro_crew import platform_compat

    _Host(monkeypatch, tmp_path, 16.0)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_WAIT_SECS", 0.2)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_POLL_SECS", 0.05)
    monkeypatch.setattr(platform_compat, "memory_pressure_level", lambda: 2)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    busy = _row(id="busy")  # a dedicated runtime of ours
    mgr._agents[busy.id] = busy
    info = _row(
        id="b3", _start_price_gb=SHARED, _start_priced_shared=True, parent_session_key=parent
    )
    mgr._agents[info.id] = info
    mgr._running_count = 2
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        seen = [c.kwargs["outcome"] for c in audit.return_value.log_tool_invocation.mock_calls]
        assert seen == outcomes
        assert info._start_priced_shared is False
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_below_the_floor_it_waits_then_starts_and_says_so(monkeypatch, tmp_path) -> None:
    _Host(monkeypatch, tmp_path, 2.2)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_WAIT_SECS", 0.2)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_POLL_SECS", 0.05)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    info._exec_started = 1.0
    mgr._agents[info.id] = info
    mgr._running_count = 1
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        outcomes = [c.kwargs["outcome"] for c in audit.return_value.log_tool_invocation.mock_calls]
        assert outcomes == ["dedicated_start_below_floor"]
        # The start clock was paused for the wait instead of charging it.
        assert info._gate_wait_started is None and info._exec_started == 1.0
        assert info._start_queue_wait_ms > 0
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_wait_reads_memory_off_the_event_loop(monkeypatch, tmp_path) -> None:
    """The Linux reader walks cgroup files and the top-up polls it."""
    _Host(monkeypatch, tmp_path, 4.0)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = subagent_mod.check_memory_available

    def _check(min_gb, **kw):
        seen.append(threading.get_ident())
        return real(min_gb=min_gb, **kw)

    monkeypatch.setattr(subagent_mod, "check_memory_available", _check)
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents[info.id] = info
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        assert seen and loop_thread not in seen
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("mode", ["persistent", "incognito", "no-store"])
async def test_the_gate_reads_memory_off_the_event_loop(monkeypatch, tmp_path, mode) -> None:
    """Every event-loop entry to the gate reads the floor on a worker.

    ``spawn_async`` (a durable row, a non-durable start that runs the whole gate
    in its prepare pass, and a manager with the task queue off) and the
    coroutine pump re-checking a deferred row: the reading walks cgroup files,
    so none of them may take it on the loop. The bar each asked with is still
    the gate's own. With no store the pump is still the coroutine on a running
    loop: the in-memory wait's wake re-pumps it, and an inline pump there would
    read the host on the loop on every retry.
    """
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    # Production's pump: a coroutine whose store reads run off the loop. The
    # suite switches it off for its inline settle loops.
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    host = _Host(monkeypatch, tmp_path, 2.4)
    mgr, started = await _manager(monkeypatch, eligible=False)
    mgr._taskq_admit_wait_secs = 0.05
    store = mgr._taskq
    if mode == "no-store":
        mgr._taskq = None
    loop_thread = threading.get_ident()
    seen: list[int] = []
    timed = subagent_mod.check_memory_available

    def _check(min_gb, **kw):
        seen.append(threading.get_ident())
        return timed(min_gb=min_gb, **kw)

    monkeypatch.setattr(subagent_mod, "check_memory_available", _check)
    try:
        info = await mgr.spawn_async(
            "one",
            parent_session_key=PARENT,
            _memory_mode="persistent" if mode == "no-store" else mode,
        )
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY and started == []
        assert seen and seen[0] != loop_thread
        host.set(4.5)
        # The pump's re-check, once the admit wait has passed.
        await _until(lambda: info.id in started, "the deferred start never ran")
        assert host.gated() == [pytest.approx(FLOOR + DEDICATED)] * 2
        assert loop_thread not in seen
    finally:
        mgr._taskq = store
        await _teardown(mgr)


async def _undurable_low_memory_wait(monkeypatch, tmp_path, mode, **spawn_kwargs):
    """A non-durable start waiting below the floor on production's coroutine
    pump: an incognito row beside a store, or any row with the task queue off.
    The host is short until the caller raises it."""
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    host = _Host(monkeypatch, tmp_path, 2.4)
    mgr, started = await _manager(monkeypatch, eligible=False)
    mgr._taskq_admit_wait_secs = 0.05
    store = mgr._taskq
    if mode == "no-store":
        mgr._taskq = None
    info = await mgr.spawn_async(
        "one",
        parent_session_key=PARENT,
        _memory_mode="persistent" if mode == "no-store" else mode,
        **spawn_kwargs,
    )
    assert info.queued_reason == QUEUED_REASON_LOW_MEMORY and started == []
    return host, mgr, started, store, info


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("mode", ["incognito", "no-store"])
async def test_a_non_durable_row_whose_dispatch_raises_goes_back_to_the_window(
    monkeypatch, tmp_path, mode
) -> None:
    """The coroutine pump pops a row and then awaits its off-loop reads before
    the gate sees it. A non-durable row has no store copy, so a read that raises
    there (a pool that cannot start a thread) must put it back in the window, to
    start on a later pass, instead of dropping the only copy of accepted work."""
    real_policy = subagent_mod.parent_spawn_policy
    failing: list[bool] = []

    def _policy(parent_session_key):
        if failing and failing.pop():
            raise RuntimeError("can't start new thread")
        return real_policy(parent_session_key)

    monkeypatch.setattr(subagent_mod, "parent_spawn_policy", _policy)
    host, mgr, started, store, info = await _undurable_low_memory_wait(monkeypatch, tmp_path, mode)
    try:
        failing.append(True)  # the pump's next dispatch raises once
        host.set(4.5)
        await _until(lambda: not failing, "the pump never dispatched the row")
        await _until(lambda: info.id in started, "a failed dispatch lost the row")
        assert mgr._undurable_in_dispatch == {}
    finally:
        mgr._taskq = store
        await _teardown(mgr)


async def _stop(mgr, how: str, agent_id: str) -> bool:
    """Stop *agent_id* the way each stop path does; True when it was stopped."""
    if how == "cancel":
        return await mgr.cancel(agent_id)
    if how == "stop-all":
        return sum(await mgr.cancel_for_parent(PARENT)) == 1
    if how == "parent-end":
        ids = mgr.snapshot_teardown_children(PARENT)
        return (
            ids == (agent_id,)
            and await mgr.cancel_for_teardown(ids, parent_session_key=PARENT) == 1
        )
    raise AssertionError(f"unknown stop path {how!r}")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "blocked_in",
    [
        "policy-read",
        # The floor disabled: the gate takes no host reading and starts the row
        # in the same call, so the pump's check after its policy read is the
        # only thing between a stop that landed there and the start.
        "policy-read-floor-off",
        # The gate's off-loop host read, after every pump-side check has passed:
        # only ``_spawn_after_memory_read``'s check after the read sees the stop.
        "host-read",
    ],
)
@pytest.mark.parametrize("how", ["cancel", "stop-all", "parent-end"])
@pytest.mark.parametrize("mode", ["incognito", "no-store"])
async def test_a_stop_reaches_a_non_durable_row_the_pump_is_dispatching(
    monkeypatch, tmp_path, mode, how, blocked_in
) -> None:
    """Between the pump's pop and the gate's start a non-durable row is in
    neither ``_queue`` nor ``_agents`` and has no claim to re-check a stop. A
    stop landing while any of its off-loop reads runs (the pump's policy read,
    with the floor on or off, or the gate's host-memory read), by any of the
    stop paths, must still find it, report it stopped, and keep the pump from
    starting it."""
    blocking: list[bool] = []
    entered = threading.Event()
    release = threading.Event()

    def _blocked(real):
        def _read(*args, **kwargs):
            if blocking:
                entered.set()
                release.wait(_WAIT_SECS)
            return real(*args, **kwargs)

        return _read

    if blocked_in == "host-read":
        monkeypatch.setattr(
            subagent_mod, "_host_memory_reading", _blocked(subagent_mod._host_memory_reading)
        )
        # The read must answer once released: an unanswered one re-queues the
        # row, which would hide a missing check after the read.
        monkeypatch.setattr(subagent_mod, "_HOST_READ_OFF_LOOP_SECS", 2 * _WAIT_SECS)
    else:
        monkeypatch.setattr(
            subagent_mod, "parent_spawn_policy", _blocked(subagent_mod.parent_spawn_policy)
        )
    host, mgr, started, store, info = await _undurable_low_memory_wait(monkeypatch, tmp_path, mode)
    try:
        blocking.append(True)
        if blocked_in == "policy-read-floor-off":
            _write_agent_config({"spawn_min_memory_gb": 0.0})
        host.set(4.5)
        await _until(entered.is_set, "the pump never dispatched the row")
        try:
            assert await _stop(mgr, how, info.id) is True
        finally:
            release.set()
        await _until(
            lambda: mgr._drain_task is None or mgr._drain_task.done(),
            "the pump pass never finished",
        )
        assert info.id not in started
        assert mgr._agents[info.id].user_stopped is True
        assert mgr._undurable_in_dispatch == {}
    finally:
        release.set()
        mgr._taskq = store
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("grant", ["a-stop-lands", "it-raises"])
@pytest.mark.parametrize("mode", ["incognito", "no-store"])
async def test_a_non_durable_row_is_held_from_its_pop_through_the_pass_grants(
    monkeypatch, tmp_path, mode, grant
) -> None:
    """A pass pops its pick, then awaits the resume grants it took in the same
    pick before it dispatches. A non-durable row is held from the pop: a stop
    landing during a grant still finds it (and the pass does not start it), and
    a grant that raises sends it back to the window instead of losing it."""
    from kiro_crew.subagent_manager.admission import (
        MEMORY_WAIT_UNTIL_KEY,
        SpawnAdmissionCoordinator,
    )

    host, mgr, started, store, info = await _undurable_low_memory_wait(monkeypatch, tmp_path, mode)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _grant(_admission, _entry) -> bool:
        entered.set()
        await release.wait()
        if grant == "it-raises":
            raise RuntimeError("can't start new thread")
        return True

    # The pass grants the stand-in resident resume queued below.
    monkeypatch.setattr(SpawnAdmissionCoordinator, "resume_reserve", lambda _a, _e: True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "resume_grant_async", _grant)
    try:
        (row,) = mgr._queue
        row.pop(MEMORY_WAIT_UNTIL_KEY)  # eligible on the very next pass
        host.set(4.5)
        mgr._queue.append({"_resume_id": "resident", "parent_session_key": PARENT})
        mgr._drain_queue()
        await asyncio.wait_for(entered.wait(), _WAIT_SECS)
        assert mgr._queue == [] and mgr._undurable_in_dispatch == {info.id: row}
        if grant == "a-stop-lands":
            try:
                assert await mgr.cancel(info.id) is True
            finally:
                release.set()
            await _until(
                lambda: mgr._drain_task is None or mgr._drain_task.done(),
                "the pump pass never finished",
            )
            assert info.id not in started
            assert mgr._agents[info.id].user_stopped is True
        else:
            release.set()
            await _until(lambda: info.id in started, "a raising grant lost the row")
        assert mgr._undurable_in_dispatch == {}
    finally:
        release.set()
        mgr._taskq = store
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("mode", ["incognito", "no-store"])
async def test_a_spawn_with_no_row_registers_its_parent_wait_off_the_loop(
    monkeypatch, tmp_path, mode
) -> None:
    """A spawn with no row to write (non-persistent, or the task queue off) runs
    the whole gate in ``spawn_async``'s own passes. Its nested-child
    registration (a parent blocked in ``spawn_sub_agents`` yields its slot,
    taskq.waits W3) reads the store's ledger, so it is awaited off the loop,
    as the durable path does, and never taken synchronously by the gate."""
    _Host(monkeypatch, tmp_path, 2.4)
    mgr, started = await _manager(monkeypatch, eligible=False)
    store = mgr._taskq
    if mode == "no-store":
        mgr._taskq = None
    coordinator = type(mgr._admission)  # slotted: patched on the class
    sync = MagicMock()
    monkeypatch.setattr(coordinator, "taskq_child_registered", sync)
    registered: list[str] = []

    async def _registered_async(_self, child) -> None:
        registered.append(child.id)

    monkeypatch.setattr(coordinator, "taskq_child_registered_async", _registered_async)
    try:
        info = await mgr.spawn_async(
            "one",
            parent_session_key=PARENT,
            _memory_mode="incognito" if mode == "incognito" else "persistent",
        )
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY and started == []
        assert registered == [info.id]
        sync.assert_not_called()
    finally:
        mgr._taskq = store
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("store", [True, False])
async def test_a_memory_wait_woken_early_still_starts(monkeypatch, tmp_path, store) -> None:
    """A loop timer may fire up to a clock tick before its time (asyncio runs
    whatever falls due within its clock resolution: 15.6 ms on Windows). A
    non-durable memory wait woken before its not-before stamp must re-arm, not
    drain a pass that skips it and leaves nothing armed."""
    host = _Host(monkeypatch, tmp_path, 2.4)
    mgr, started = await _manager(monkeypatch, eligible=False)
    admit_wait = 0.3
    mgr._taskq_admit_wait_secs = admit_wait
    keep = mgr._taskq
    if not store:
        mgr._taskq = None
    loop = asyncio.get_running_loop()
    on_time = loop.call_later

    def early(delay, callback, *args, **kwargs):
        # A coarse clock, exaggerated: every timer of the admit wait or longer
        # runs 0.1 s early.
        return on_time(delay - 0.1 if delay >= admit_wait else delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", early)
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT, _memory_mode="incognito")
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY and started == []
        host.set(4.5)
        await _until(lambda: info.id in started, "an early wake stranded the wait")
    finally:
        monkeypatch.setattr(loop, "call_later", on_time)
        mgr._taskq = keep
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("pool", ["stuck", "refuses-a-thread"])
@pytest.mark.parametrize("mode", ["persistent", "incognito"])
async def test_an_unanswered_gate_read_waits_and_never_reads_on_the_loop(
    monkeypatch, tmp_path, pool, mode
) -> None:
    """The gate's off-loop read has no on-loop fallback. A worker that misses
    ``_HOST_READ_OFF_LOOP_SECS``, or a pool that cannot start a thread, read
    nothing: the start waits as ``low_memory`` (never fails open on a host
    nobody measured) and starts on the re-check once the pool answers."""
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    # Production's pump, as in the off-loop test above: its re-check reads off
    # the loop too, so the whole round trip can be pinned.
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    host = _Host(monkeypatch, tmp_path, 8.0)  # ample: any reading would admit it
    mgr, started = await _manager(monkeypatch, eligible=False)
    mgr._taskq_admit_wait_secs = 0.05
    monkeypatch.setattr(subagent_mod, "_HOST_READ_OFF_LOOP_SECS", 0.05)
    loop_thread = threading.get_ident()
    on_loop: list[float] = []
    real_check = subagent_mod.check_memory_available

    def _check(min_gb, **kw):
        if threading.get_ident() == loop_thread:
            on_loop.append(min_gb)
        return real_check(min_gb, **kw)

    monkeypatch.setattr(subagent_mod, "check_memory_available", _check)
    real_to_thread = asyncio.to_thread
    answering = False
    # A stuck read answers once the pool recovers, as a hung cgroup read does.
    recovered = asyncio.Event()

    async def _pool(func, /, *args, **kwargs):
        if func is not subagent_mod._host_memory_reading or answering:
            return await real_to_thread(func, *args, **kwargs)
        if pool == "refuses-a-thread":
            raise RuntimeError("can't start new thread")
        await recovered.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _pool)
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT, _memory_mode=mode)
        assert info.queued and not info.done and started == []
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY
        assert "did not answer" in info.queued_reason_detail
        assert on_loop == [] and host.gated() == []
        answering = True
        recovered.set()
        await _until(lambda: info.id in started, "the unanswered wait never re-checked")
        assert on_loop == []
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("mode", ["persistent", "incognito"])
async def test_a_disabled_floor_takes_no_reading_so_an_unanswered_pool_cannot_hold_it(
    monkeypatch, tmp_path, caplog, mode
) -> None:
    """``agent.spawn_min_memory_gb: 0`` disables the floor. The gate then takes
    no host reading at all, so a pool that cannot start a thread (an unanswered
    read) never defers the start as ``low_memory``, and a floor nobody asked to
    check is not reported on Linux as a guard that could not run."""
    import logging

    _write_agent_config({"spawn_min_memory_gb": 0.0})
    _Host(monkeypatch, tmp_path, 0.5)  # short: any floor read would hold it
    mgr, started = await _manager(monkeypatch, eligible=False)
    monkeypatch.setattr(subagent_mod.platform_compat, "IS_LINUX", True)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    reads: list[float] = []
    real_to_thread = asyncio.to_thread

    async def _refuses(func, /, *args, **kwargs):
        if func is subagent_mod._host_memory_reading:
            reads.append(args[0] if args else kwargs.get("min_gb"))
            raise RuntimeError("can't start new thread")
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _refuses)
    caplog.set_level(logging.WARNING, logger="kiro_crew.subagent")
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT, _memory_mode=mode)
        assert info.queued_reason != QUEUED_REASON_LOW_MEMORY and not info.done
        await _until(lambda: info.id in started, "a disabled floor held the start")
        outcomes = [c.kwargs["outcome"] for c in audit.return_value.log_tool_invocation.mock_calls]
        assert "deferred_low_memory" not in outcomes
        assert "memory_check_unavailable" not in outcomes
        assert "memory guard could not run" not in caplog.text
        assert reads == []
    finally:
        await _teardown(mgr)


class _HeldRead:
    """The floor's off-loop host read, parked on demand: with ``held`` set, the
    next read waits for :meth:`release`, so a test can change the world DURING
    it. Every other ``to_thread`` call passes straight through."""

    def __init__(self, monkeypatch) -> None:
        self.held = False
        self.entered = asyncio.Event()
        self._released = asyncio.Event()
        real = asyncio.to_thread

        async def _pool(func, /, *args, **kwargs):
            if func is subagent_mod._host_memory_reading and self.held:
                self.held = False
                self.entered.set()
                await self._released.wait()
            return await real(func, *args, **kwargs)

        monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _pool)
        # The read's own bound is not what these tests exercise.
        monkeypatch.setattr(subagent_mod, "_HOST_READ_OFF_LOOP_SECS", _WAIT_SECS)

    def release(self) -> None:
        self._released.set()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("mode", ["persistent", "incognito", "no-store"])
async def test_governance_tightened_during_the_read_still_refuses(
    monkeypatch, tmp_path, mode
) -> None:
    """The memory read's re-entry re-runs the policy gates: a spawn admitted by
    governance before the read and forbidden during it is refused, not run.
    ``persistent`` is the default path, whose row ``spawn_async`` committed
    before the read: the refusal fails that row, so it cannot run later."""
    from kiro_crew import taskq as _taskq

    _Host(monkeypatch, tmp_path, 8.0)  # ample: only governance can stop it
    mgr, started = await _manager(monkeypatch, eligible=False)
    store = mgr._taskq
    if mode == "no-store":
        mgr._taskq = None
    read = _HeldRead(monkeypatch)
    read.held = True
    try:
        pending = asyncio.ensure_future(
            mgr.spawn_async(
                "one",
                parent_session_key=PARENT,
                _memory_mode="persistent" if mode == "no-store" else mode,
            )
        )
        await asyncio.wait_for(read.entered.wait(), _WAIT_SECS)
        monkeypatch.setattr(
            subagent_mod, "_vet_spawn_governance", lambda *_a, **_k: "spawning is off"
        )
        read.release()
        info = await asyncio.wait_for(pending, _WAIT_SECS)
        assert info.done and "spawn refused by governance" in info.error
        if mode == "persistent":
            await _until(
                lambda: store.state_of(info.id) == _taskq.FAILED,
                "the refused row is still runnable",
            )
            mgr._drain_queue()
            await asyncio.sleep(0.1)
        assert started == []
    finally:
        mgr._taskq = store
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_governance_tightened_during_a_drain_read_fails_the_row(
    monkeypatch, tmp_path
) -> None:
    """The coroutine pump's re-check of a deferred row reads the floor off the
    loop too; a governance change made during that read fails the row
    (``_refuse_row``) instead of starting it."""
    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    host = _Host(monkeypatch, tmp_path, 2.4)
    mgr, started = await _manager(monkeypatch, eligible=False)
    mgr._taskq_admit_wait_secs = 0.05
    read = _HeldRead(monkeypatch)
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT)
        assert info.queued_reason == QUEUED_REASON_LOW_MEMORY
        # No await since the defer: the pump's wake has not run yet.
        read.held = True
        host.set(8.0)
        await asyncio.wait_for(read.entered.wait(), _WAIT_SECS)
        monkeypatch.setattr(
            subagent_mod, "_vet_spawn_governance", lambda *_a, **_k: "spawning is off"
        )
        read.release()
        await _until(
            lambda: mgr._taskq.state_of(info.id) == _taskq.FAILED, "the refused row was not failed"
        )
        assert started == []
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_admission_closing_during_the_read_fails_the_committed_row(
    monkeypatch, tmp_path
) -> None:
    """``spawn_async`` commits a durable row before it reads the floor. If gateway
    admission closes during that read, the caller is refused, so the row is
    failed with it: left queued, it would run once admission reopens."""
    from kiro_crew import taskq as _taskq

    _Host(monkeypatch, tmp_path, 8.0)
    mgr, started = await _manager(monkeypatch, eligible=False)
    read = _HeldRead(monkeypatch)
    read.held = True
    try:
        pending = asyncio.ensure_future(mgr.spawn_async("one", parent_session_key=PARENT))
        await asyncio.wait_for(read.entered.wait(), _WAIT_SECS)
        mgr._sessions.admission_closed = True
        read.release()
        info = await asyncio.wait_for(pending, _WAIT_SECS)
        assert info.done and info.error == "spawn refused: gateway admission is closed"
        await _until(
            lambda: mgr._taskq.state_of(info.id) == _taskq.FAILED,
            "the refused row is still runnable",
        )
        mgr._sessions.admission_closed = False
        mgr._drain_queue()
        await asyncio.sleep(0.1)
        assert started == []
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("reading", ["unanswered", "unmeasurable", "measured-low"])
async def test_only_a_read_that_answered_unmeasurable_proceeds_unchecked(
    monkeypatch, tmp_path, caplog, reading
) -> None:
    """On Linux a -1 reading that the reader returned means the guard could not
    run: the start proceeds, with a WARNING and a ``memory_check_unavailable``
    SEL row. An unanswered read also carries -1 but measured nothing: it waits
    as ``low_memory``, is never audited as a start that proceeded, and its
    defer audit and wait label carry no ``available_gb`` figure. A read that
    measured a short host waits as ``low_memory`` too, and both carry the
    figure it measured."""
    import logging

    _Host(monkeypatch, tmp_path, 8.0)
    mgr, started = await _manager(monkeypatch, eligible=False)
    monkeypatch.setattr(subagent_mod.platform_compat, "IS_LINUX", True)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    waits: list[dict] = []
    emit = mgr._emit_queue_depth

    def _emit(*args, **kwargs):
        waits.append(dict(kwargs.get("wait") or {}))
        return emit(*args, **kwargs)

    monkeypatch.setattr(mgr, "_emit_queue_depth", _emit)
    if reading == "unanswered":
        real_to_thread = asyncio.to_thread

        async def _refuses(func, /, *args, **kwargs):
            if func is subagent_mod._host_memory_reading:
                raise RuntimeError("can't start new thread")
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _refuses)
    else:
        answer = (True, -1.0) if reading == "unmeasurable" else (False, 0.5)
        monkeypatch.setattr(subagent_mod, "check_memory_available", lambda *_a, **_k: answer)
    caplog.set_level(logging.WARNING, logger="kiro_crew.subagent")
    try:
        info = await mgr.spawn_async("one", parent_session_key=PARENT, _memory_mode="incognito")
        calls = audit.return_value.log_tool_invocation.mock_calls
        outcomes = [c.kwargs["outcome"] for c in calls]
        unchecked = "memory guard could not run" in caplog.text
        if reading == "unmeasurable":
            await _until(lambda: info.id in started, "an unmeasurable host must fail open")
            assert "memory_check_unavailable" in outcomes and unchecked
            assert "deferred_low_memory" not in outcomes
            return
        assert info.queued and info.queued_reason == QUEUED_REASON_LOW_MEMORY
        assert started == []
        assert "memory_check_unavailable" not in outcomes and not unchecked
        (deferred,) = [
            c.kwargs["metadata"] for c in calls if c.kwargs["outcome"] == "deferred_low_memory"
        ]
        (wait,) = waits
        assert wait["reason"] == QUEUED_REASON_LOW_MEMORY and "required_gb" in wait
        if reading == "unanswered":
            assert deferred["cause"] == subagent_mod.MEMORY_CAUSE_READ_UNANSWERED
            assert "available_gb" not in deferred and "available_gb" not in wait
        else:
            assert deferred["available_gb"] == 0.5 and wait["available_gb"] == 0.5
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("pool", ["stuck", "refuses-a-thread"])
async def test_an_unanswered_top_up_read_keeps_waiting_and_never_reads_on_the_loop(
    monkeypatch, tmp_path, pool
) -> None:
    """An unanswered off-loop read is headroom unknown, as it is to the gate: the
    top-up never fails open on it and never re-reads on the loop; it keeps
    waiting and starts once a read answers."""
    _Host(monkeypatch, tmp_path, 4.0)  # ample: any reading would let it start
    mgr, _ = await _manager(monkeypatch, eligible=True)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    monkeypatch.setattr(subagent_mod, "_HOST_READ_OFF_LOOP_SECS", 0.05)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_POLL_SECS", 0.05)
    loop_thread = threading.get_ident()
    on_loop: list[float] = []
    real_check = subagent_mod.check_memory_available

    def _check(min_gb, **kw):
        if threading.get_ident() == loop_thread:
            on_loop.append(min_gb)
        return real_check(min_gb, **kw)

    monkeypatch.setattr(subagent_mod, "check_memory_available", _check)
    real_to_thread = asyncio.to_thread
    answering = False
    # A stuck read answers once the pool recovers, as a hung cgroup read does.
    recovered = asyncio.Event()
    submitted: list[float] = []

    async def _pool(func, /, *args, **kwargs):
        if func is not subagent_mod._host_memory_reading or answering:
            return await real_to_thread(func, *args, **kwargs)
        submitted.append(args[0])
        if pool == "refuses-a-thread":
            raise RuntimeError("can't start new thread")
        await recovered.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _pool)
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents[info.id] = info
    try:
        task = asyncio.ensure_future(mgr._ensure_dedicated_start_priced(info))
        await asyncio.sleep(0.3)  # several unanswered polls
        assert not task.done() and info._start_priced_shared is True
        if pool == "stuck":
            # Every poll after the first waits on the read already in flight:
            # a hung reader holds one worker, not one more per poll.
            assert len(submitted) == 1
        else:
            assert len(submitted) > 1  # a refused start leaves nothing in flight
        answering = True
        recovered.set()
        await asyncio.wait_for(task, _WAIT_SECS)
        assert info._start_priced_shared is False
        assert audit.return_value.log_tool_invocation.mock_calls == []
        assert on_loop == []
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_callers_with_different_bars_share_one_in_flight_host_read(
    monkeypatch, tmp_path
) -> None:
    """The reading does not depend on the bar, and the bar differs per agent
    bucket and moves as warming rows settle. So a hung reader must hold one
    executor thread whatever bars its waiters hold: a caller with a different
    bar awaits the read already in flight, and each gets the same figure to
    compare against its own bar."""
    _Host(monkeypatch, tmp_path, 3.0)
    monkeypatch.setattr(subagent_mod, "_HOST_READ_OFF_LOOP_SECS", _WAIT_SECS)
    real_to_thread = asyncio.to_thread
    held = asyncio.Event()
    submitted: list[float] = []

    async def _pool(func, /, *args, **kwargs):
        if func is subagent_mod._host_memory_reading:
            submitted.append(args[0])
            await held.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _pool)
    low = asyncio.ensure_future(subagent_mod._host_memory_reading_off_loop(FLOOR + SHARED))
    await _until(lambda: submitted, "the first read never started")
    high = asyncio.ensure_future(subagent_mod._host_memory_reading_off_loop(FLOOR + DEDICATED))
    try:
        # A few loop turns: enough for the second caller to reach its wait, and
        # for any read task it started to submit its worker.
        for _ in range(5):
            await asyncio.sleep(0)
        assert submitted == [FLOOR + SHARED] and not low.done() and not high.done()
    finally:
        held.set()
    first, second = await asyncio.wait_for(asyncio.gather(low, high), _WAIT_SECS)
    assert first == second and first[0] == pytest.approx(3.0)
    assert submitted == [FLOOR + SHARED]
    assert subagent_mod._host_read_in_flight is None


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_top_up_that_never_got_an_answer_starts_and_says_headroom_unknown(
    monkeypatch, tmp_path, caplog
) -> None:
    import logging

    _Host(monkeypatch, tmp_path, 4.0)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_WAIT_SECS", 0.2)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_POLL_SECS", 0.05)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    real_to_thread = asyncio.to_thread

    async def _refuses(func, /, *args, **kwargs):
        if func is subagent_mod._host_memory_reading:
            raise RuntimeError("can't start new thread")
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _refuses)
    caplog.set_level(logging.WARNING, logger="kiro_crew.subagent")
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents[info.id] = info
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        (call,) = audit.return_value.log_tool_invocation.mock_calls
        assert call.kwargs["outcome"] == "dedicated_start_below_floor"
        assert call.kwargs["metadata"]["cause"] == subagent_mod.MEMORY_CAUSE_READ_UNANSWERED
        assert "available_gb" not in call.kwargs["metadata"]
        assert "memory headroom unknown" in caplog.text
        assert info._start_priced_shared is False
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_memory_that_frees_during_the_wait_lets_it_start(monkeypatch, tmp_path) -> None:
    host = _Host(monkeypatch, tmp_path, 2.2)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_POLL_SECS", 0.05)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents[info.id] = info
    mgr._running_count = 1
    try:
        task = asyncio.ensure_future(mgr._ensure_dedicated_start_priced(info))
        # Change the host only once a read has SEEN it below the floor.
        await _until(lambda: len(host.gated()) >= 1, "never read the host")
        await asyncio.sleep(0.1)
        assert not task.done(), "it must be waiting, not started"
        host.set(4.0)
        await asyncio.wait_for(task, _WAIT_SECS)
        assert audit.return_value.log_tool_invocation.mock_calls == []
        assert _DEDICATED_TOPUP_WAIT_SECS > 0.05
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_raised_projection_never_re_checks_a_dedicated_admission(
    monkeypatch, tmp_path
) -> None:
    """Only a start admitted at the SHARED price is topped up: a dedicated row
    whose bucket learned a higher figure since was checked at its own price."""
    host = _Host(monkeypatch, tmp_path, 0.5)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    mgr._learned_settled_gb = {"kirocrew": 1.8}
    info = _row(id="b3", _start_price_gb=DEDICATED)
    mgr._agents[info.id] = info
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        assert host.gated() == [] and info._start_price_gb == DEDICATED
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_two_fallbacks_that_fit_one_at_a_time_both_start_in_turn(
    monkeypatch, tmp_path
) -> None:
    """Waiters do not each count the other's raised price and both hold: the one
    holding the turn re-checks without the one queued behind it."""
    host = _Host(monkeypatch, tmp_path, 2.9)  # not even one dedicated start fits yet
    mgr, _ = await _manager(monkeypatch, eligible=True)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_POLL_SECS", 0.02)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    a = _row(id="a", _start_price_gb=SHARED, _start_priced_shared=True)
    b = _row(id="b", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents.update({a.id: a, b.id: b})
    mgr._running_count = 2
    try:
        first = asyncio.ensure_future(mgr._ensure_dedicated_start_priced(a))
        await _until(lambda: host.gated(), "a never checked")
        second = asyncio.ensure_future(mgr._ensure_dedicated_start_priced(b))
        await _until(lambda: b._topup_waiting, "b never queued behind a")
        host.set(3.6)  # room for exactly one dedicated start
        await asyncio.wait_for(first, _WAIT_SECS)
        # a passed at the bar for itself alone (b, queued, launched nothing)...
        assert pytest.approx(FLOOR + DEDICATED) in host.gated(), "a counted b"
        # ...and b now counts a, which has not settled.
        await _until(lambda: host.gated()[-1] == pytest.approx(FLOOR + 2 * DEDICATED), "b")
        assert not second.done()
        a._rss_samples = 2  # a's process is up and inside the reading
        await asyncio.wait_for(second, _WAIT_SECS)
        assert host.gated()[-1] == pytest.approx(FLOOR + DEDICATED)
        assert audit.return_value.log_tool_invocation.mock_calls == []
    finally:
        await _teardown(mgr)


def test_a_new_reading_lands_while_the_poller_holds_the_last_one(monkeypatch, tmp_path):
    """``_Host.set`` runs while the top-up's worker thread may be inside the read:
    holding the current file open is that thread mid-read, on every host."""
    host = _Host(monkeypatch, tmp_path, 2.9)
    with open(host._meminfo, encoding="utf-8"):
        host.set(3.6)
    assert subagent_mod.check_memory_available(min_gb=3.5) == (True, 3.6)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_cancel_during_the_wait_leaves_no_frozen_start_clock(monkeypatch, tmp_path):
    _Host(monkeypatch, tmp_path, 2.2)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    monkeypatch.setattr(subagent_mod, "_DEDICATED_TOPUP_POLL_SECS", 0.05)
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents[info.id] = info
    try:
        task = asyncio.ensure_future(mgr._ensure_dedicated_start_priced(info))
        await _until(lambda: info._gate_wait_started is not None, "never waited")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _WAIT_SECS)
        assert info._gate_wait_started is None and not info._topup_waiting
        assert not mgr._dedicated_topup_lock.locked()
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    ("price", "floor"),
    [(None, FLOOR), (DEDICATED, FLOOR), (SHARED, 0.0)],
    ids=["never-priced", "already-dedicated", "floor-disabled"],
)
async def test_no_recheck_when_nothing_was_underpriced_or_the_floor_is_off(
    monkeypatch, tmp_path, price, floor
) -> None:
    _write_agent_config({"spawn_min_memory_gb": floor})
    host = _Host(monkeypatch, tmp_path, 0.5)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    info = _row(id="b3", _start_price_gb=price, _start_priced_shared=price == SHARED)
    mgr._agents[info.id] = info
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        assert host.gated() == []
        if price == SHARED:
            assert info._start_price_gb == pytest.approx(DEDICATED), "still topped up"
    finally:
        await _teardown(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_retained_claim_keeps_its_checked_price(monkeypatch, tmp_path) -> None:
    """A claim re-entry that does not proceed still holds its slot, so its price
    must stay charged until it registers or is released."""
    _Host(monkeypatch, tmp_path, 8.0)
    mgr, _ = await _manager(monkeypatch, eligible=False)
    retained = mgr._admission.CLAIM_RETAINED
    mgr._claim_prices["held"] = (DEDICATED, False)
    try:
        mgr.spawn(
            "one", parent_session_key=PARENT, _preassigned_id="held", _claimed=(1, False, retained)
        )
        assert mgr._claim_prices == {"held": (DEDICATED, False)}
        mgr._admission.release_reservation("held")
        assert mgr._claim_prices == {}
    finally:
        await _teardown(mgr)
