"""The spawn memory floor at the SHIPPED defaults, through the real gate.

The floor is what must remain available AFTER a start is admitted
(``agent.spawn_min_memory_gb``, default ``DEFAULT_SPAWN_MIN_MEMORY_GB``). A start
that will share its parent's runtime is priced at the dedicated projection less
the process it does not launch; a dedicated one at ``max(agent.subagent_cost_gb,
learned settled RSS)``, or the measured unlearned figure before a bucket has one.

The scenarios patch ONLY the host's free-memory reading. Config is the isolated
home's real defaults (no ``KiroCrewConfig`` patch), the free figure goes through
the real ``/proc/meminfo`` parser, and the posture verdict is computed for real
from the same number, so a scenario fails if any default, any price or the
sharing prediction drifts.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import threading
from unittest.mock import MagicMock

import pytest
from overload_fakes import mock_ctx, mock_sessions

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
        self._meminfo = tmp_path / "meminfo"
        self.set(gb)
        real = subagent_mod.check_memory_available

        def _check(min_gb, **_kw):
            self.asked.append(min_gb)
            return real(min_gb=min_gb, path=str(self._meminfo))

        monkeypatch.setattr(subagent_mod, "check_memory_available", _check)
        monkeypatch.setattr(rs, "_read_available_gb", lambda: self.gb)
        monkeypatch.setattr(subagent_mod, "cached_admission_check", lambda: rs.admission_check())

    def set(self, gb: float) -> None:
        self.gb = gb
        # ceil: a GiB figure floored to whole kB reads a hair under itself.
        # Replaced atomically: the top-up reads it from a worker thread, which
        # must never see a truncated file.
        staged = self._meminfo.with_suffix(".next")
        staged.write_text(f"MemAvailable: {math.ceil(gb * _KIB_PER_GIB)} kB\n", encoding="utf-8")
        os.replace(staged, self._meminfo)

    def gated(self) -> list[float]:
        # Host-sizing probes ask with min_gb=0.0; only floor checks count.
        return [a for a in self.asked if a > 0]


async def _manager(monkeypatch, *, eligible: bool):
    sessions = mock_sessions()
    sessions.is_session_sharing_eligible = MagicMock(return_value=eligible)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    mgr = SubagentManager(sessions=sessions, ctx_builder=mock_ctx(), max_concurrent=4)
    await asyncio.wait_for(mgr.wait_taskq_ready(), _WAIT_SECS)
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
        # The start clock restarted after the wait instead of charging it.
        assert info._gate_wait_started is None and info._exec_started > 1.0
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
@pytest.mark.parametrize("pool", ["stuck", "refuses-a-thread"])
async def test_a_pool_that_cannot_answer_gets_the_synchronous_reading(
    monkeypatch, tmp_path, pool
) -> None:
    """An unanswered off-loop read is never mistaken for 'below the floor':
    the poller takes the reading on the loop instead, as the gate does."""
    _Host(monkeypatch, tmp_path, 4.0)
    mgr, _ = await _manager(monkeypatch, eligible=True)
    audit = MagicMock()
    monkeypatch.setattr(subagent_mod, "sel", audit)
    monkeypatch.setattr(subagent_mod, "_HOST_READ_OFF_LOOP_SECS", 0.05)

    async def _stuck(func, /, *args, **kwargs):
        if pool == "refuses-a-thread":
            raise RuntimeError("can't start new thread")
        await asyncio.Event().wait()

    monkeypatch.setattr(subagent_mod.asyncio, "to_thread", _stuck)
    info = _row(id="b3", _start_price_gb=SHARED, _start_priced_shared=True)
    mgr._agents[info.id] = info
    try:
        await asyncio.wait_for(mgr._ensure_dedicated_start_priced(info), _WAIT_SECS)
        assert audit.return_value.log_tool_invocation.mock_calls == []
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
