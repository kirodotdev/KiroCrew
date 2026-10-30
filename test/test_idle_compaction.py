"""``session.idle_compaction``: compact a quiet dashboard chat once, before its cache expires.

Every test drives the idle timer on a ``ManualClock`` through a fake scheduler, so
no test waits on real time: advancing the clock is what makes a timer due, and the
fake scheduler runs exactly the callbacks the advance made due.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    CLAUDE_PROMPT_CACHE_TTL_ENV,
    idle_compaction_delay_secs,
    prompt_cache_lifetime_secs,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader_builders import _build_session_config
from kiro_crew.dashboard import chat_utils_completion
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_COMPACTED,
    COMPACT_OUTCOME_COMPACTED_IDLE,
    COMPACT_OUTCOME_RECYCLED,
    COMPACT_TRIGGER_IDLE,
    COMPACT_TRIGGER_THRESHOLD,
)
from kiro_crew.session_idle_compaction import (
    IDLE_COMPACT_DRAIN_SECS,
    IdleCompactionTimers,
    provider_prompt_cache_lifetime,
)
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, until_parked

KEY = "dashboard:idle-compaction"
#: The lost-run bound for awaiting the coordinator's own background tasks.
_LOST_RUN_SECS = 30.0


# ── Fakes ──


class _Handle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeScheduler:
    """``call_later`` on a ``ManualClock``: :meth:`advance` runs what became due."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.pending: list[tuple[float, Any, _Handle]] = []

    def __call__(self, delay: float, callback: Any) -> _Handle:
        handle = _Handle()
        self.pending.append((self.clock.monotonic() + delay, callback, handle))
        return handle

    def live(self) -> int:
        return sum(1 for _due, _cb, h in self.pending if not h.cancelled)

    def advance(self, secs: float) -> None:
        self.clock.advance(secs)
        now = self.clock.monotonic()
        due = [entry for entry in self.pending if entry[0] <= now]
        self.pending = [entry for entry in self.pending if entry[0] > now]
        for _due, callback, handle in due:
            if not handle.cancelled:
                callback()


def _provider_factory(
    *,
    backend: Any = ACP_BACKEND_KIRO,
    pct: float = 50.0,
    pct_after: float = 10.0,
    status: str = "completed",
    extra_env: dict[str, str] | None = None,
    unmanaged: str | None = None,
    gate: asyncio.Event | None = None,
    providers: list[Any] | None = None,
):
    """A kiro-cli-shaped provider at *pct* whose ``/compact`` reports *status*."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        state = {"compacted": False}
        m.context_usage_pct = lambda: pct_after if state["compacted"] else pct
        m.context_usage_unknown = lambda: False
        m.manual_compact_unsupported_backend = unmanaged
        m.harness_managed_compaction_backend = None
        m.compaction_unmanaged_backend = unmanaged
        m.client = SimpleNamespace(
            backend=backend, _extra_env=dict(extra_env or {}), _agent_env_snapshot={}
        )
        # Synchronous on a real provider; a teardown calls them without awaiting.
        m._client = m.client
        m.disown_work_dir = MagicMock()
        # ``_turn`` writes the last request's start here, as a real stream does.
        m.essential_delivery = SimpleNamespace(request_floor=None)
        m.cancel = AsyncMock(return_value="acked")

        async def _stream(_cmd):
            if gate is not None:
                await gate.wait()
            for ev in []:
                yield ev  # pragma: no cover - the status arrives through the wait

        m.stream_command = MagicMock(side_effect=_stream)

        async def _wait(timeout=None):
            state["compacted"] = status == "completed"
            return {"type": status}

        m.wait_for_compaction = AsyncMock(side_effect=_wait)
        if providers is not None:
            providers.append(m)
        return m

    return factory


@pytest.fixture(autouse=True)
def _no_inherited_cache_tier(_floor_monkeypatch):
    """The lifetime lookup starts from ``os.environ``; a Claude cache tier the
    caller's shell exports must not decide whether a test arms."""
    _floor_monkeypatch.delenv(CLAUDE_PROMPT_CACHE_TTL_ENV, raising=False)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def cfg() -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.idle_compaction = True
    c.session.idle_compaction_min_context_pct = 40.0
    c.session.autocompact_pct = 70.0
    return c


@pytest.fixture
def counters(monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "kiro_crew.session_compaction.emit_counter",
        lambda name, attrs: seen.append((name, dict(attrs))),
    )
    return seen


async def _drain(mgr: SessionManager) -> None:
    pending = [t for t in mgr._background_tasks if not t.done()]
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=_LOST_RUN_SECS
        )


@contextlib.asynccontextmanager
async def _managed(cfg: KiroCrewConfig, clock: ManualClock, factory: Any, *, probe: Any = None):
    """A manager on the fake clock, with the dashboard's idle probe answering *probe*."""
    mgr = SessionManager(cfg, provider_factory=factory)
    sched = FakeScheduler(clock)
    mgr._compaction.idle.timers = IdleCompactionTimers(
        fire=mgr._compaction.idle.fire,
        background_tasks=mgr._background_tasks,
        clock=clock.monotonic,
        schedule=sched,
    )
    mgr.set_idle_compaction_probe(probe or (lambda _key: None))
    mgr.sched = sched  # type: ignore[attr-defined]
    try:
        yield mgr
    finally:
        await _drain(mgr)
        await mgr.close_all()


async def _turn(
    mgr: SessionManager, key: str = KEY, *, request_started: float | None = None
) -> Any:
    """One finished turn: claim, the turn-end usage check, release.

    The turn's last request started at *request_started* on the timers' clock,
    or at the turn end when it is ``None`` (a turn that took no time).
    """
    provider, _new, _resumed = await mgr.get_or_create(key)
    now = mgr._compaction.idle.timers.clock()
    provider.essential_delivery.request_floor = now if request_started is None else request_started
    mgr.check_context_usage(key, provider)
    mgr.release(key)
    return provider


def _compactions(provider: Any) -> int:
    return sum(1 for c in provider.stream_command.call_args_list if c.args == ("/compact",))


# ── The lifetime table ──


class TestLifetimeTable:
    def test_kiro_and_codex_have_fixed_lifetimes(self):
        assert prompt_cache_lifetime_secs(ACP_BACKEND_KIRO, {}) == 300.0
        assert prompt_cache_lifetime_secs(ACP_BACKEND_CODEX, {}) == 1800.0

    def test_claude_needs_an_explicit_tier(self):
        assert prompt_cache_lifetime_secs(ACP_BACKEND_CLAUDE, {}) is None
        tier = CLAUDE_PROMPT_CACHE_TTL_ENV
        assert prompt_cache_lifetime_secs(ACP_BACKEND_CLAUDE, {tier: "5m"}) == 300.0
        assert prompt_cache_lifetime_secs(ACP_BACKEND_CLAUDE, {tier: "1h"}) == 3600.0
        assert prompt_cache_lifetime_secs(ACP_BACKEND_CLAUDE, {tier: "2h"}) is None

    @pytest.mark.parametrize(
        "backend",
        [
            ACP_BACKEND_KAS,
            ACP_BACKEND_OPENCODE,
            ACP_BACKEND_GOOSE,
            ACP_BACKEND_PI,
            ACP_BACKEND_DEEPSEEK,
        ],
    )
    def test_every_other_backend_is_skipped(self, backend):
        assert prompt_cache_lifetime_secs(backend, {CLAUDE_PROMPT_CACHE_TTL_ENV: "1h"}) is None

    def test_fire_time_margin_and_fraction(self):
        assert idle_compaction_delay_secs(300.0) == 255.0
        assert idle_compaction_delay_secs(600.0) == 555.0
        assert idle_compaction_delay_secs(1800.0) == pytest.approx(1620.0)
        assert idle_compaction_delay_secs(3600.0) == pytest.approx(3240.0)

    def test_launch_env_layers_win_over_the_gateway_env(self, monkeypatch):
        monkeypatch.setenv(CLAUDE_PROMPT_CACHE_TTL_ENV, "5m")
        client = SimpleNamespace(
            backend=ACP_BACKEND_CLAUDE, _agent_env_snapshot=None, _extra_env={}
        )
        assert provider_prompt_cache_lifetime(SimpleNamespace(client=client)) == 300.0
        client._agent_env_snapshot = {CLAUDE_PROMPT_CACHE_TTL_ENV: "1h"}
        assert provider_prompt_cache_lifetime(SimpleNamespace(client=client)) == 3600.0
        client._extra_env = {CLAUDE_PROMPT_CACHE_TTL_ENV: "bogus"}
        assert provider_prompt_cache_lifetime(SimpleNamespace(client=client)) is None

    def test_a_provider_with_no_string_backend_has_no_lifetime(self):
        assert provider_prompt_cache_lifetime(MagicMock()) is None
        assert provider_prompt_cache_lifetime(object()) is None


# ── The config keys ──


class TestConfig:
    def test_defaults(self):
        session = KiroCrewConfig().session
        assert session.idle_compaction is False
        assert session.idle_compaction_min_context_pct == 50.0

    @pytest.mark.parametrize(
        "raw, loaded",
        [
            (0, 40.0),
            (5, 40.0),
            (39.9, 40.0),
            (40, 40.0),
            (45, 45.0),
            (90, 90.0),
            (90.1, 90.0),
            (100, 90.0),
            ("x", 50.0),
        ],
    )
    def test_min_pct_is_clamped_to_40_through_90(self, raw, loaded):
        built = _build_session_config(
            {"idle_compaction": True, "idle_compaction_min_context_pct": raw}
        )
        assert built.idle_compaction is True
        assert built.idle_compaction_min_context_pct == loaded


# ── The timer ──


class TestTimer:
    @pytest.mark.asyncio
    async def test_off_arms_nothing_and_compacts_nothing(self, cfg, clock):
        cfg.session.idle_compaction = False
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            assert not mgr._compaction.idle.timers.armed(KEY)
            assert mgr.sched.live() == 0
            mgr.sched.advance(3600)
            await _drain(mgr)
            assert _compactions(provider) == 0

    @pytest.mark.asyncio
    async def test_kiro_compacts_once_at_255_seconds(self, cfg, clock, counters):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            assert mgr._compaction.idle.timers.due_at(KEY) == clock.monotonic() + 255.0
            mgr.sched.advance(254)
            await _drain(mgr)
            assert _compactions(provider) == 0
            mgr.sched.advance(1)
            await _drain(mgr)
            assert _compactions(provider) == 1
            # The idle compaction's own /compact does not re-arm: nothing more fires.
            assert not mgr._compaction.idle.timers.armed(KEY)
            mgr.sched.advance(3600)
            await _drain(mgr)
            assert _compactions(provider) == 1
            assert mgr._sessions[KEY].provider is provider
        assert (
            "kirocrew.context.compactions",
            {"success": True, "trigger": COMPACT_TRIGGER_IDLE},
        ) in counters

    @pytest.mark.asyncio
    async def test_a_fire_delivered_after_the_cache_expired_compacts_nothing(
        self, cfg, clock, counters
    ):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            # The loop was paused past the kiro lifetime (300 s) before the
            # 255 s callback ran.
            mgr.sched.advance(301)
            await _drain(mgr)
            assert _compactions(provider) == 0
            assert not mgr.is_compacting(KEY)
            assert not mgr._compaction.idle.timers.armed(KEY)
            assert mgr._sessions[KEY].provider is provider
            # The same decline, named, at exactly the expiry instant too.
            provider.essential_delivery.request_floor = clock.monotonic() - 300.0
            assert await mgr.compact_if_idle(KEY) == "expired"
            assert _compactions(provider) == 0
        assert not [a for _n, a in counters if a.get("trigger") == COMPACT_TRIGGER_IDLE]

    @pytest.mark.asyncio
    async def test_a_fire_after_a_host_suspend_compacts_nothing(self, cfg, clock, counters):
        boot = {"t": 1000.0}
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            mgr._compaction.idle.timers.boot_clock = lambda: boot["t"]
            provider = await _turn(mgr)
            # The host sleeps 600 s: the monotonic clock stands still, the boot
            # clock and the provider's cache lifetime do not.
            boot["t"] += 600.0
            boot["t"] += 255.0
            mgr.sched.advance(255)
            await _drain(mgr)
            assert _compactions(provider) == 0
            assert mgr._sessions[KEY].provider is provider
        assert not [a for _n, a in counters if a.get("trigger") == COMPACT_TRIGGER_IDLE]

    def test_suspended_time_is_the_boot_clock_running_ahead(self, clock):
        async def fire(_key: str, _generation: int) -> None:
            return None

        boot = {"t": 50.0}
        timers = IdleCompactionTimers(
            fire=fire,
            background_tasks=set(),
            clock=clock.monotonic,
            schedule=FakeScheduler(clock),
            boot_clock=lambda: boot["t"],
        )
        timers.arm(KEY, 255)
        clock.advance(10.0)
        boot["t"] += 10.0 + 600.0
        assert timers.suspended_since_arm(KEY) == pytest.approx(600.0)
        timers.cancel(KEY)
        assert timers.suspended_since_arm(KEY) == 0.0

    @pytest.mark.asyncio
    async def test_codex_arms_at_ninety_percent_of_its_lifetime(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(backend=ACP_BACKEND_CODEX)) as mgr:
            await _turn(mgr)
            assert mgr._compaction.idle.timers.due_at(KEY) == pytest.approx(
                clock.monotonic() + 1620.0
            )

    @pytest.mark.asyncio
    async def test_claude_arms_only_with_an_explicit_tier(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(backend=ACP_BACKEND_CLAUDE)) as mgr:
            await _turn(mgr)
            assert not mgr._compaction.idle.timers.armed(KEY)
        factory = _provider_factory(
            backend=ACP_BACKEND_CLAUDE, extra_env={CLAUDE_PROMPT_CACHE_TTL_ENV: "5m"}
        )
        async with _managed(cfg, clock, factory) as mgr:
            await _turn(mgr)
            assert mgr._compaction.idle.timers.due_at(KEY) == clock.monotonic() + 255.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case",
        ["below_minimum", "skipped_backend", "channel_key", "cron_key", "above_threshold"],
    )
    async def test_nothing_is_armed(self, cfg, clock, case):
        key = KEY
        kwargs: dict[str, Any] = {}
        if case == "below_minimum":
            kwargs["pct"] = 39.0
        elif case == "skipped_backend":
            kwargs["backend"] = ACP_BACKEND_OPENCODE
        elif case == "channel_key":
            key = "channel:slack:C1:1.0"
        elif case == "cron_key":
            key = "cron:job-1"
        elif case == "above_threshold":
            # The threshold compacts on this reading; no idle compaction follows it.
            kwargs["pct"] = 80.0
        async with _managed(cfg, clock, _provider_factory(**kwargs)) as mgr:
            await _turn(mgr, key)
            await _drain(mgr)
            assert not mgr._compaction.idle.timers.armed(key)
            assert not mgr._compaction.idle.timers.armed(mgr._fold_key(key))

    @pytest.mark.asyncio
    async def test_a_turn_at_254_seconds_cancels_the_timer(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(pct=50.0)) as mgr:
            provider = await _turn(mgr)
            mgr.sched.advance(254)
            await mgr.get_or_create(KEY)
            assert not mgr._compaction.idle.timers.armed(KEY)
            mgr.sched.advance(10)
            await _drain(mgr)
            assert _compactions(provider) == 0
            mgr.release(KEY)

    @pytest.mark.asyncio
    async def test_a_turn_after_the_compaction_began_waits_for_it(self, cfg, clock):
        gate = asyncio.Event()
        async with _managed(cfg, clock, _provider_factory(gate=gate)) as mgr:
            provider = await _turn(mgr)
            mgr.sched.advance(255)
            semaphore = mgr._sessions[KEY].semaphore
            await async_wait_until(
                lambda: semaphore.locked() and provider.stream_command.called,
                describe=lambda: f"locked={semaphore.locked()}",
            )
            assert mgr.is_compacting(KEY)
            claim = asyncio.ensure_future(mgr.get_or_create(KEY))
            await until_parked(semaphore)
            assert not claim.done()
            gate.set()
            claimed, _new, _resumed = await asyncio.wait_for(claim, timeout=_LOST_RUN_SECS)
            assert claimed is provider
            assert _compactions(provider) == 1
            mgr.release(KEY)

    @pytest.mark.asyncio
    async def test_reset_cancels_the_timer(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _turn(mgr)
            assert mgr._compaction.idle.timers.armed(KEY)
            await mgr.reset(KEY)
            assert not mgr._compaction.idle.timers.armed(KEY)

    @pytest.mark.asyncio
    async def test_turning_the_setting_off_cancels_every_timer(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _turn(mgr)
            assert mgr.sched.live() == 1
            new = KiroCrewConfig()
            new.session.idle_compaction = False
            await mgr._on_config_change(
                ConfigChange(old=cfg, new=new, changed=frozenset({"session.idle_compaction"}))
            )
            assert not mgr._compaction.idle.timers.armed(KEY)
            assert mgr.sched.live() == 0

    @pytest.mark.asyncio
    async def test_after_a_restart_the_timer_arms_from_restore_time(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _turn(mgr)
        # A fresh manager is the restarted gateway: nothing carried over.
        clock.advance(10_000)
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            assert not mgr._compaction.idle.timers.armed(KEY)
            restored_at = clock.monotonic()
            provider = await _turn(mgr)
            assert mgr._compaction.idle.timers.due_at(KEY) == restored_at + 255.0
            mgr.sched.advance(255)
            await _drain(mgr)
            assert _compactions(provider) == 1


class TestTimerUnit:
    @pytest.mark.asyncio
    async def test_a_stale_generation_does_nothing(self, clock):
        fired: list[tuple[str, int]] = []

        async def fire(key: str, generation: int) -> None:
            fired.append((key, generation))

        sched = FakeScheduler(clock)
        timers = IdleCompactionTimers(
            fire=fire, background_tasks=set(), clock=clock.monotonic, schedule=sched
        )
        first = timers.arm(KEY, 10)
        stale_callback = sched.pending[0][1]
        second = timers.arm(KEY, 10)
        assert second != first
        clock.advance(10)
        tasks: set[asyncio.Task[Any]] = timers._background_tasks
        stale_callback()  # a callback already queued when the re-arm landed
        assert not tasks  # it started nothing
        sched.advance(0)
        await async_wait_until(lambda: fired, describe=lambda: f"fired={fired}")
        assert fired == [(KEY, second)]

    @pytest.mark.asyncio
    async def test_cancel_all_makes_an_in_flight_fire_stale(self, clock):
        async def fire(_key: str, _generation: int) -> None:
            return None

        sched = FakeScheduler(clock)
        timers = IdleCompactionTimers(
            fire=fire, background_tasks=set(), clock=clock.monotonic, schedule=sched
        )
        generation = timers.arm(KEY, 5)
        sched.advance(5)
        # The callback ran and left ``_armed``; its fire is still in flight.
        assert not timers.armed(KEY)
        assert timers.is_current(KEY, generation)
        timers.cancel_all()  # the setting turned off, then back on
        assert not timers.is_current(KEY, generation)

    @pytest.mark.asyncio
    async def test_cancel_after_the_fire_makes_its_generation_stale(self, clock):
        async def fire(_key: str, _generation: int) -> None:
            return None

        sched = FakeScheduler(clock)
        timers = IdleCompactionTimers(
            fire=fire, background_tasks=set(), clock=clock.monotonic, schedule=sched
        )
        generation = timers.arm(KEY, 5)
        sched.advance(5)
        assert timers.is_current(KEY, generation)
        assert timers.cancel(KEY) is False
        assert not timers.is_current(KEY, generation)

    @pytest.mark.asyncio
    async def test_removal_drops_the_entry_and_a_stale_timer_still_does_not_fire(self, clock):
        fired: list[tuple[str, int]] = []

        async def fire(key: str, generation: int) -> None:
            fired.append((key, generation))

        sched = FakeScheduler(clock)
        timers = IdleCompactionTimers(
            fire=fire, background_tasks=set(), clock=clock.monotonic, schedule=sched
        )
        first = timers.arm(KEY, 10)
        stale_callback = sched.pending[0][1]
        timers.arm("dashboard:other", 10)
        timers.cancel(KEY)  # the session was removed
        assert KEY not in timers._generations
        assert len(timers._generations) == 1
        # Re-armed after the drop: never the number the stale callback carries.
        second = timers.arm(KEY, 10)
        assert second != first
        assert not timers.is_current(KEY, first)
        clock.advance(10)
        stale_callback()
        assert not timers._background_tasks
        timers.cancel(KEY)
        timers.cancel("dashboard:other")
        assert timers._generations == {}

    @pytest.mark.asyncio
    async def test_removing_a_session_prunes_its_generation(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _turn(mgr)
            timers = mgr._compaction.idle.timers
            assert KEY in timers._generations
            await mgr.reset(KEY)
            assert KEY not in timers._generations

    def test_an_early_callback_reschedules_for_the_remainder(self, clock):
        sched = FakeScheduler(clock)
        timers = IdleCompactionTimers(
            fire=AsyncMock(), background_tasks=set(), clock=clock.monotonic, schedule=sched
        )
        timers.arm(KEY, 10)
        clock.advance(4)
        sched.pending[0][1]()  # the loop ran it early
        assert timers.armed(KEY)
        assert sched.pending[-1][0] == pytest.approx(clock.monotonic() + 6)


# ── The gate ladder without the threshold rung ──


class TestGateRungSkipThreshold:
    """``_gate_rung(skip_threshold=True)`` drops only ``below_threshold``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rung",
        [None, "cc_managed", "compact_unsupported", "unconfirmed", "in_progress", "cooldown"],
    )
    async def test_every_other_rung_still_refuses(self, cfg, rung):
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        co = mgr._compaction
        provider = _provider_factory()()
        if rung == "cc_managed":
            co._deps = dataclasses.replace(co._deps, is_cc_managed=lambda _p: True)
        elif rung == "compact_unsupported":
            provider.manual_compact_unsupported_backend = ACP_BACKEND_KAS
            provider.harness_managed_compaction_backend = ACP_BACKEND_KAS
        elif rung == "unconfirmed":
            co._deps = dataclasses.replace(co._deps, context_pct_is_unknown=lambda _p: True)
        elif rung == "in_progress":
            co.state.compacting.add(KEY)
        elif rung == "cooldown":
            co.state.cooldown_until[KEY] = float("inf")
        try:
            # 10% is far below the 70% threshold; only the threshold rung would refuse it.
            assert co._gate_rung(KEY, provider, 10.0, log=False, skip_threshold=True) == rung
        finally:
            co.state.compacting.discard(KEY)
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_threshold_path_still_refuses_below_threshold(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        co = mgr._compaction
        provider = _provider_factory()()
        try:
            assert co._gate_rung(KEY, provider, 10.0, log=False) == "below_threshold"
            assert co._compaction_gate_decision(KEY, provider, 10.0) == "below_threshold"
            assert co._gate_rung(KEY, provider, 80.0, log=False) is None
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_idle_path_compacts_below_the_threshold(self, cfg, clock):
        # 50% is under the 70% auto-compact threshold and over the 40% idle minimum.
        async with _managed(cfg, clock, _provider_factory(pct=50.0)) as mgr:
            await _turn(mgr)
            assert mgr._compaction.effective_autocompact_pct(KEY) == 70.0
            assert await mgr.compact_if_idle(KEY) == "ok"


# ── What blocks the fire ──


class TestLiveSignals:
    @pytest.mark.asyncio
    async def test_the_clear_case_compacts(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _turn(mgr)
            assert await mgr.compact_if_idle(KEY) == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "signal, expected",
        [
            ("held_permit", "busy:turn"),
            ("active_turn", "busy:turn"),
            ("lifecycle_turn", "busy:turn"),
            ("queued_message", "busy:queue"),
            ("recycling", "busy:recycling"),
            ("pending_injection", "busy:injection"),
            ("subagents", "subagents"),
            ("subagent_probe_raises", "subagents"),
            ("dashboard_question", "busy:question"),
            ("dashboard_probe_raises", "busy:probe_error"),
            ("no_dashboard_probe", "busy:no_activity_probe"),
            ("cooldown", "cooldown"),
            ("pending_verdict", "pending_verdict"),
            ("threshold_in_flight", "in_progress"),
            ("setting_off", "disabled"),
        ],
    )
    async def test_each_signal_blocks_the_fire(self, cfg, clock, signal, expected):
        probe = (lambda _k: "question") if signal == "dashboard_question" else None
        async with _managed(cfg, clock, _provider_factory(), probe=probe) as mgr:
            provider = await _turn(mgr)
            session = mgr._sessions[KEY]
            held = False
            if signal == "held_permit":
                await mgr.get_or_create(KEY)
                held = True
            elif signal == "active_turn":
                provider.has_active_turn = lambda: True
            elif signal == "lifecycle_turn":
                session.lifecycle_turn_active = True
            elif signal == "queued_message":
                mgr.enqueue(KEY, "1.0", "hello", force=True)
            elif signal == "recycling":
                mgr._recycling[KEY] = session
            elif signal == "pending_injection":
                mgr.set_injection_probe(lambda _k: True)
            elif signal == "subagents":

                async def _attached(_k):
                    return True

                mgr.set_subagent_probe(_attached)
            elif signal == "subagent_probe_raises":

                def _boom(_k):
                    raise RuntimeError("probe down")

                mgr.set_subagent_probe(_boom)
            elif signal == "dashboard_probe_raises":

                def _boom2(_k):
                    raise RuntimeError("probe down")

                mgr.set_idle_compaction_probe(_boom2)
            elif signal == "no_dashboard_probe":
                mgr.set_idle_compaction_probe(None)
            elif signal == "cooldown":
                mgr._compaction.state.cooldown_until[KEY] = float("inf")
            elif signal == "pending_verdict":
                mgr._compaction.state.pending_verdict[KEY] = 50.0
            elif signal == "threshold_in_flight":
                mgr._compaction._set_compacting(KEY, True)
            elif signal == "setting_off":
                cfg.session.idle_compaction = False
            try:
                assert await mgr.compact_if_idle(KEY) == expected
                assert _compactions(provider) == 0
            finally:
                if signal == "threshold_in_flight":
                    mgr._compaction._set_compacting(KEY, False)
                mgr._recycling.pop(KEY, None)
                if held:
                    mgr.release(KEY)

    @pytest.mark.asyncio
    async def test_a_blocked_fire_ends_the_stretch(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(), probe=lambda _k: "approval") as mgr:
            provider = await _turn(mgr)
            mgr.sched.advance(255)
            await _drain(mgr)
            mgr.set_idle_compaction_probe(lambda _k: None)
            mgr.sched.advance(3600)
            await _drain(mgr)
            assert _compactions(provider) == 0

    @pytest.mark.asyncio
    async def test_an_unmanaged_backend_is_never_recycled(self, cfg, clock):
        factory = _provider_factory(unmanaged=ACP_BACKEND_DEEPSEEK)
        async with _managed(cfg, clock, factory) as mgr:
            provider = await _turn(mgr)
            assert await mgr.compact_if_idle(KEY) == "unmanaged"
            assert mgr._sessions[KEY].provider is provider
            provider.shutdown.assert_not_awaited()


# ── Dedupe with the threshold, and failure ──


class TestThresholdAndFailure:
    @pytest.mark.asyncio
    async def test_threshold_and_idle_never_compact_the_same_stretch_twice(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(pct=80.0, pct_after=40.0)) as mgr:
            provider = await _turn(mgr)
            await _drain(mgr)
            assert _compactions(provider) == 1  # the threshold compaction
            assert not mgr._compaction.idle.timers.armed(KEY)
            mgr.sched.advance(3600)
            await _drain(mgr)
            assert _compactions(provider) == 1

    @pytest.mark.asyncio
    async def test_a_failed_idle_compaction_cools_down_and_keeps_the_session(
        self, cfg, clock, counters, monkeypatch
    ):
        restarts: list[str] = []

        async def _no_restart(*args, **kwargs):
            restarts.append("restart")
            return "recycled"

        async with _managed(cfg, clock, _provider_factory(status="failed")) as mgr:
            monkeypatch.setattr(mgr._compaction, "_restart_held", _no_restart)
            monkeypatch.setattr(mgr._compaction, "_await_cotenants", _no_restart)
            provider = await _turn(mgr)
            mgr.sched.advance(255)
            await _drain(mgr)
            assert _compactions(provider) == 1
            assert restarts == []
            assert mgr._compaction.state.cooldown_until[KEY] > 0
            assert mgr._sessions[KEY].provider is provider
            provider.shutdown.assert_not_awaited()
            assert not mgr.is_compacting(KEY)
        assert (
            "kirocrew.context.compactions",
            {"success": False, "trigger": COMPACT_TRIGGER_IDLE},
        ) in counters

    @pytest.mark.asyncio
    async def test_a_still_critical_idle_compaction_does_not_reset(self, cfg, clock, monkeypatch):
        resets: list[str] = []

        async def _no_reset(*args, **kwargs):
            resets.append("reset")
            return True

        async with _managed(cfg, clock, _provider_factory(pct=60.0, pct_after=59.0)) as mgr:
            monkeypatch.setattr(mgr._compaction, "_reset_still_critical", _no_reset)
            # A reading of 59% after the compaction is still critical at this bar.
            mgr._compaction._deps = dataclasses.replace(
                mgr._compaction._deps, post_compact_reset_pct=50.0
            )
            await _turn(mgr)
            assert await mgr.compact_if_idle(KEY) == "ok"
            assert resets == []

    @pytest.mark.asyncio
    async def test_the_threshold_counter_says_threshold(self, cfg, clock, counters):
        async with _managed(cfg, clock, _provider_factory(pct=80.0, pct_after=40.0)) as mgr:
            await _turn(mgr)
            await _drain(mgr)
        assert (
            "kirocrew.context.compactions",
            {"success": True, "trigger": COMPACT_TRIGGER_THRESHOLD},
        ) in counters


# ── The dashboard probe ──


class TestDashboardProbe:
    def _state(self, slot: Any, approvals: dict | None = None) -> Any:
        return SimpleNamespace(
            get_slot=lambda name: slot if name == "slot-a" else None,
            _pending_approvals=approvals or {},
        )

    def _slot(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "key": "slot-a",
            "running": False,
            "_wake_starting_task": None,
            "queue_depth": 0,
            "_question_pending": {},
            "_compacting": False,
            "_pending_synthesis": False,
            "_subagent_deliveries_inflight": 0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @pytest.fixture(autouse=True)
    def _routes(self, _floor_monkeypatch):
        # Through the facade: several chat_utils owners bind this name, and the
        # facade mirrors a patch into each of them.
        _floor_monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
            lambda key: "slot-a" if key == KEY else "",
        )
        _floor_monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)

    @pytest.mark.parametrize(
        "overrides, expected",
        [
            ({}, None),
            ({"running": True}, "slot_turn"),
            ({"queue_depth": 2}, "slot_queue"),
            ({"_question_pending": {"q1": {}}}, "question"),
            ({"_pending_synthesis": True}, "slot_activity"),
        ],
    )
    def test_slot_signals(self, overrides, expected):
        state = self._state(self._slot(**overrides))
        assert chat_utils_completion.idle_compaction_blocker(state, KEY) == expected

    def test_no_open_tab_is_refused(self):
        assert chat_utils_completion.idle_compaction_blocker(self._state(None), KEY) == "no_slot"

    def test_a_pending_approval_on_the_slot_blocks(self):
        state = self._state(self._slot(), {"a1": {"slot": "slot-a"}})
        assert chat_utils_completion.idle_compaction_blocker(state, KEY) == "approval"

    def test_an_unfinished_workflow_blocks(self):
        state = self._state(self._slot())
        state.workflow_service = SimpleNamespace(
            registry=SimpleNamespace(has_pending_work_for=lambda key: key == KEY)
        )
        assert chat_utils_completion.idle_compaction_blocker(state, KEY) == "workflow"

    def test_a_workflow_for_another_session_does_not_block(self):
        state = self._state(self._slot())
        state.workflow_service = SimpleNamespace(
            registry=SimpleNamespace(has_pending_work_for=lambda key: False)
        )
        assert chat_utils_completion.idle_compaction_blocker(state, KEY) is None

    def test_an_armed_monitor_loop_blocks(self, monkeypatch):
        service = SimpleNamespace(get_by_slot=lambda key: SimpleNamespace(active=True))
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: service)
        state = self._state(self._slot())
        assert chat_utils_completion.idle_compaction_blocker(state, KEY) == "monitor_loop"


# ── An idle /compact that reports no status ──


def _waits(mgr: SessionManager, provider: Any, results: list[dict]) -> list[bool]:
    """Answer *provider*'s status waits with *results* in turn; record the permit at each."""
    locked: list[bool] = []
    answers = iter(results)

    async def _wait(timeout=None):
        locked.append(mgr._sessions[KEY].semaphore.locked())
        return next(answers, {"type": "timeout"})

    provider.wait_for_compaction = AsyncMock(side_effect=_wait)
    return locked


class TestUnsettledIdleCompaction:
    @pytest.mark.asyncio
    async def test_a_late_completion_is_drained_before_the_permit_is_released(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            locked = _waits(mgr, provider, [{"type": "timeout"}, {"type": "completed"}])
            assert await mgr.compact_if_idle(KEY) == "ok"
            # The late status was read by the drain, with the permit still held.
            assert locked == [True, True]
            provider.cancel.assert_awaited_once()
            assert mgr._sessions[KEY].provider is provider
            assert not mgr.is_compacting(KEY)
            # The next turn gets the permit and no status wait of its own.
            claimed, _new, _resumed = await mgr.get_or_create(KEY)
            assert claimed is provider
            assert provider.wait_for_compaction.await_count == 2
            mgr.release(KEY)

    @pytest.mark.asyncio
    async def test_a_late_failure_cools_down_and_keeps_the_session(self, cfg, clock, monkeypatch):
        restarts: list[Any] = []
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            monkeypatch.setattr(
                mgr._compaction, "_restart_held", lambda *a, **k: restarts.append(k)
            )
            provider = await _turn(mgr)
            locked = _waits(mgr, provider, [{"type": "timeout"}, {"type": "failed"}])
            assert await mgr.compact_if_idle(KEY) == "failed"
            assert locked == [True, True]
            assert restarts == []
            assert mgr._compaction.state.cooldown_until[KEY] > 0
            assert mgr._sessions[KEY].provider is provider

    @pytest.mark.asyncio
    async def test_a_streamed_failure_is_kept_when_the_stream_then_times_out(
        self, cfg, clock, monkeypatch
    ):
        from kiro_crew.acp.types import EVENT_COMPACTION_STATUS

        restarts: list[dict] = []
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            monkeypatch.setattr(
                mgr._compaction, "_restart_held", lambda *a, **k: restarts.append(k)
            )
            monkeypatch.setattr(
                mgr._compaction,
                "_deps",
                dataclasses.replace(
                    mgr._compaction._deps,
                    compact_wait_timeout_secs=lambda: 0.05,
                    compact_result_wait_margin_secs=0.05,
                ),
            )
            provider = await _turn(mgr)
            never = asyncio.Event()

            async def _stream(_cmd):
                yield SimpleNamespace(kind=EVENT_COMPACTION_STATUS, text="failed")
                await never.wait()  # the harness never ends the turn

            provider.stream_command = MagicMock(side_effect=_stream)
            assert await mgr.compact_if_idle(KEY) == "failed"
            # The failure was already reported: no drain, no restart, cooldown only.
            provider.cancel.assert_not_awaited()
            assert restarts == []
            assert mgr._compaction.state.cooldown_until[KEY] > 0
            assert mgr._sessions[KEY].provider is provider

    @pytest.mark.asyncio
    async def test_a_streamed_completion_is_a_success_when_the_stream_then_times_out(
        self, cfg, clock, monkeypatch
    ):
        from kiro_crew.acp.types import EVENT_COMPACTION_STATUS

        restarts: list[dict] = []
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            monkeypatch.setattr(
                mgr._compaction, "_restart_held", lambda *a, **k: restarts.append(k)
            )
            monkeypatch.setattr(
                mgr._compaction,
                "_deps",
                dataclasses.replace(
                    mgr._compaction._deps,
                    compact_wait_timeout_secs=lambda: 0.05,
                    compact_result_wait_margin_secs=0.05,
                ),
            )
            provider = await _turn(mgr)
            never = asyncio.Event()

            async def _stream(_cmd):
                yield SimpleNamespace(kind=EVENT_COMPACTION_STATUS, text="completed")
                await never.wait()  # the command's own response never lands

            provider.stream_command = MagicMock(side_effect=_stream)
            assert await mgr.compact_if_idle(KEY) == "ok"
            provider.cancel.assert_not_awaited()
            assert restarts == []
            assert mgr._sessions[KEY].provider is provider

    @pytest.mark.asyncio
    async def test_no_status_keeps_the_session_and_cools_down(
        self, cfg, clock, monkeypatch, counters
    ):
        starts: list[dict] = []
        notices: list[tuple[bool, str]] = []
        marked: list[tuple[Any, float]] = []

        async def _cb(_key, _pct, *, success, outcome):
            notices.append((success, outcome))

        monkeypatch.setattr(
            "kiro_crew.agent_sdk.stale_compaction.discard_provider_stale_compaction_status",
            lambda provider, *, within_secs: marked.append((provider, within_secs)) or True,
        )
        cfg.session.compact_wait_secs = 42.0
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            mgr.set_compact_callback(_cb)
            provider = await _turn(mgr)
            session = mgr._sessions[KEY]
            original = mgr.get_or_create

            async def _spy(key, **kwargs):
                starts.append(kwargs)
                return await original(key, **kwargs)

            monkeypatch.setattr(mgr, "get_or_create", _spy)
            reset = AsyncMock(return_value=True)
            monkeypatch.setattr(mgr, "reset", reset)
            timeouts: list[Any] = []
            locked: list[bool] = []

            async def _wait(timeout=None):
                timeouts.append(timeout)
                locked.append(mgr._sessions[KEY].semaphore.locked())
                return {"type": "timeout"}

            provider.wait_for_compaction = AsyncMock(side_effect=_wait)
            assert await mgr.compact_if_idle(KEY) == "unconfirmed"
            await _drain(mgr)
            assert locked == [True, True]
            # The drain is capped at IDLE_COMPACT_DRAIN_SECS, below the budget.
            assert timeouts[1] == IDLE_COMPACT_DRAIN_SECS == 10.0
            provider.cancel.assert_awaited_once()
            # Kept: no teardown, no restart, no successor.
            reset.assert_not_awaited()
            provider.shutdown.assert_not_awaited()
            assert starts == []
            assert mgr._sessions[KEY] is session
            assert session.provider is provider
            # Its late status, if it ever comes, is dropped for one more budget.
            assert marked == [(provider, 42.0)]
            assert mgr._compaction.state.cooldown_until[KEY] > 0
            assert not session.semaphore.locked()
            assert not mgr.is_compacting(KEY)
            assert notices == [(False, COMPACT_OUTCOME_COMPACTED_IDLE)]
            # The next turn takes the same session.
            claimed, is_new, _resumed = await original(KEY)
            assert claimed is provider
            assert not is_new
            mgr.release(KEY)
        idle = [a for _name, a in counters if a.get("trigger") == COMPACT_TRIGGER_IDLE]
        assert idle == [{"success": False, "trigger": COMPACT_TRIGGER_IDLE}]

    @pytest.mark.asyncio
    async def test_no_status_keeps_a_queued_message_on_the_kept_session(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _turn(mgr)
            entry = ("ts-1", "keep me", {})
            held = mgr._sessions[KEY]
            calls = 0

            async def _wait(timeout=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    # A message queues behind the permit during the drain.
                    held.queue.append(entry)
                return {"type": "timeout"}

            provider.wait_for_compaction = AsyncMock(side_effect=_wait)
            assert await mgr.compact_if_idle(KEY) == "unconfirmed"
            await _drain(mgr)
            provider.shutdown.assert_not_awaited()
            assert mgr._sessions[KEY] is held
            assert entry in held.queue
            held.queue.clear()  # this test runs no drain for it

    @pytest.mark.asyncio
    async def test_a_session_replaced_during_the_drain_is_left_alone(self, cfg, clock, monkeypatch):
        notices: list[tuple[bool, str]] = []

        async def _cb(_key, _pct, *, success, outcome):
            notices.append((success, outcome))

        async with _managed(cfg, clock, _provider_factory()) as mgr:
            mgr.set_compact_callback(_cb)
            provider = await _turn(mgr)
            held = mgr._sessions[KEY]
            stand_in = SimpleNamespace(provider=MagicMock())
            calls = 0

            async def _wait(timeout=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    # Another teardown put a different session under the key.
                    mgr._sessions[KEY] = stand_in
                return {"type": "timeout"}

            provider.wait_for_compaction = AsyncMock(side_effect=_wait)
            reset = AsyncMock(return_value=True)
            monkeypatch.setattr(mgr, "reset", reset)
            try:
                assert await mgr.compact_if_idle(KEY) == "absent"
            finally:
                mgr._sessions[KEY] = held  # the real session, for close_all
            reset.assert_not_awaited()
            provider.shutdown.assert_not_awaited()
            assert notices == []

    @pytest.mark.asyncio
    async def test_a_force_stop_during_the_drain_restarts_nothing(self, cfg, clock, monkeypatch):
        notices: list[tuple[bool, str]] = []

        async def _cb(_key, _pct, *, success, outcome):
            notices.append((success, outcome))

        async with _managed(cfg, clock, _provider_factory()) as mgr:
            mgr.set_compact_callback(_cb)
            provider = await _turn(mgr)
            starts: list[dict] = []
            original = mgr.get_or_create

            async def _spy(key, **kwargs):
                starts.append(kwargs)
                return await original(key, **kwargs)

            calls = 0

            async def _wait(timeout=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    # A force Stop lands while the drain reads the late status.
                    monkeypatch.setattr(mgr, "stop_generation", lambda _k: 10**6)
                return {"type": "timeout"}

            provider.wait_for_compaction = AsyncMock(side_effect=_wait)
            monkeypatch.setattr(mgr, "get_or_create", _spy)
            await mgr.compact_if_idle(KEY)
            assert starts == [], "the Stop already replaced the session"
            provider.shutdown.assert_not_awaited()
            assert all(outcome != COMPACT_OUTCOME_RECYCLED for _s, outcome in notices)
            assert not any(success for success, _o in notices)

    @pytest.mark.asyncio
    async def test_a_threshold_restart_still_reports_recycled(self, cfg, clock):
        notices: list[tuple[bool, str]] = []

        async def _cb(_key, _pct, *, success, outcome):
            notices.append((success, outcome))

        async with _managed(cfg, clock, _provider_factory()) as mgr:
            mgr.set_compact_callback(_cb)
            provider = await _turn(mgr)
            session = mgr._sessions[KEY]
            await session.semaphore.acquire()
            assert await mgr._compaction._restart_held(KEY, session, 85.0) == "recycled"
            assert mgr._sessions[KEY].provider is not provider
        assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]


# ── The anchor: the start of the turn's last request ──


class TestRequestAnchor:
    @pytest.mark.asyncio
    async def test_a_final_stream_past_the_margin_arms_nothing(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _turn(mgr, request_started=clock.monotonic() - 256.0)
            assert not mgr._compaction.idle.timers.armed(KEY)
            assert mgr.sched.live() == 0

    @pytest.mark.asyncio
    async def test_a_long_final_stream_arms_for_what_is_left(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            now = clock.monotonic()
            await _turn(mgr, request_started=now - 100.0)
            assert mgr._compaction.idle.timers.due_at(KEY) == pytest.approx(now + 155.0)

    @pytest.mark.asyncio
    async def test_a_provider_with_no_request_start_arms_nothing(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider, _new, _resumed = await mgr.get_or_create(KEY)
            provider.essential_delivery = SimpleNamespace(request_floor=None)
            mgr.check_context_usage(KEY, provider)
            mgr.release(KEY)
            assert not mgr._compaction.idle.timers.armed(KEY)


def _event(kind: str, **fields: Any) -> Any:
    base = {
        "kind": kind,
        "text": "",
        "control_notice": False,
        "stop_reason": "",
        "synthetic_completion": False,
        "refusal": None,
        "tool_final": False,
        "tool_status": "",
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _scripted_send(clock: ManualClock, script: list[Any]) -> Any:
    """A transport that yields *script*; a float in it advances the clock."""

    async def send(_message: str):
        for step in script:
            if isinstance(step, float):
                clock.advance(step)
            else:
                yield step

    return send


async def _stream(
    delivery: EssentialDelivery,
    clock: ManualClock,
    script: list[Any],
    *,
    requests_model: bool = True,
) -> None:
    """Run one stream through *delivery*; a float in *script* advances the clock."""
    send = _scripted_send(clock, script)
    async for _ in delivery.stream(
        "hello", send, lambda: "conversation", requests_model=requests_model
    ):
        pass


class TestRequestFloor:
    @pytest.mark.asyncio
    async def test_a_plain_turn_floors_at_its_start(self, clock):
        delivery = EssentialDelivery(clock=clock.monotonic)
        started = clock.monotonic()
        await _stream(
            delivery,
            clock,
            [
                5.0,
                _event("text_chunk", text="hi"),
                120.0,
                _event("complete", stop_reason="end_turn"),
            ],
        )
        assert delivery.request_floor == started

    @pytest.mark.asyncio
    async def test_a_tool_turn_floors_at_the_tool_end(self, clock):
        delivery = EssentialDelivery(clock=clock.monotonic)
        started = clock.monotonic()
        await _stream(
            delivery,
            clock,
            [
                _event("tool_call"),
                30.0,
                _event("tool_result", tool_status="in_progress"),
                30.0,
                _event("tool_result", tool_final=True, tool_status="completed"),
                2.0,
                _event("text_chunk", text="done"),
                200.0,
                _event("complete", stop_reason="end_turn"),
            ],
        )
        assert delivery.request_floor == started + 60.0

    @pytest.mark.asyncio
    async def test_a_failed_tool_counts_as_ended(self, clock):
        delivery = EssentialDelivery(clock=clock.monotonic)
        started = clock.monotonic()
        await _stream(
            delivery,
            clock,
            [
                _event("tool_call"),
                10.0,
                _event("tool_result", tool_status="failed"),
                _event("text_chunk", text="it failed"),
            ],
        )
        assert delivery.request_floor == started + 10.0

    @pytest.mark.asyncio
    async def test_a_tool_end_with_no_output_after_it_does_not_move_the_floor(self, clock):
        delivery = EssentialDelivery(clock=clock.monotonic)
        started = clock.monotonic()
        await _stream(
            delivery,
            clock,
            [
                _event("tool_call"),
                400.0,
                _event("tool_result", tool_final=True, tool_status="completed"),
                _event("complete", stop_reason="cancelled"),
            ],
        )
        assert delivery.request_floor == started

    @pytest.mark.asyncio
    async def test_a_stream_with_no_model_output_keeps_the_previous_floor(self, clock):
        delivery = EssentialDelivery(clock=clock.monotonic)
        started = clock.monotonic()
        await _stream(delivery, clock, [_event("text_chunk", text="hi")])
        clock.advance(200.0)
        # A slash command such as ``/context`` makes no model request.
        await _stream(delivery, clock, [_event("complete", stop_reason="end_turn")])
        assert delivery.request_floor == started

    @pytest.mark.asyncio
    async def test_a_first_stream_with_no_model_output_leaves_no_floor(self, clock):
        delivery = EssentialDelivery(clock=clock.monotonic)
        await _stream(delivery, clock, [_event("complete", stop_reason="end_turn")])
        assert delivery.request_floor is None

    @pytest.mark.asyncio
    async def test_a_transport_with_no_model_request_keeps_the_floor_despite_text(self, clock):
        delivery = EssentialDelivery(clock=clock.monotonic)
        started = clock.monotonic()
        await _stream(delivery, clock, _MODEL_TURN)
        clock.advance(200.0)
        await _stream(
            delivery,
            clock,
            [
                _event("tool_call"),
                _event("tool_result", tool_final=True, tool_status="completed"),
                *_COMMAND_OUTPUT,
            ],
            requests_model=False,
        )
        assert delivery.request_floor == started


_COMMAND_OUTPUT = [
    _event("text_chunk", text="context: 41%"),
    _event("complete", stop_reason="end_turn"),
]


def _kiro_session_provider(clock: ManualClock, *, native: bool) -> Any:
    """An ``AcpSessionProvider`` whose handle sends commands natively or not."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handle = MagicMock()
    handle.command_is_native = lambda _command: native
    runtime = MagicMock()
    runtime.acp_backend = ACP_BACKEND_KIRO
    provider = AcpSessionProvider(handle, runtime)
    provider.essential_delivery = EssentialDelivery(clock=clock.monotonic)
    return provider


def _direct_acp_provider(clock: ManualClock, backend: str) -> Any:
    """The fields ``acp_turns.stream_command`` reads from an ``AcpProvider``."""
    client = SimpleNamespace(
        backend=backend,
        stream_command=_scripted_send(clock, _COMMAND_OUTPUT),
        stream_events=_scripted_send(clock, _COMMAND_OUTPUT),
    )
    return SimpleNamespace(
        _client=client,
        _ready_confirmed_session=AsyncMock(),
        essential_delivery=EssentialDelivery(clock=clock.monotonic),
        context_incarnation="conversation",
        _to_llm_event=lambda event: event,
    )


class TestNativeCommandFloor:
    """Kiro's native ``commands/execute`` returns output without a model request."""

    @pytest.mark.asyncio
    async def test_a_native_session_command_keeps_the_real_turns_floor(self, clock):
        provider = _kiro_session_provider(clock, native=True)
        provider._handle.stream_command = _scripted_send(clock, _COMMAND_OUTPUT)
        started = clock.monotonic()
        await _stream(provider.essential_delivery, clock, _MODEL_TURN)
        clock.advance(200.0)
        events = [e async for e in provider.stream_command("/context")]
        assert any(e.text for e in events)
        assert provider.essential_delivery.request_floor == started

    @pytest.mark.asyncio
    async def test_a_prompt_transport_session_command_moves_the_floor(self, clock):
        provider = _kiro_session_provider(clock, native=False)
        provider._handle.stream_command = _scripted_send(clock, _COMMAND_OUTPUT)
        await _stream(provider.essential_delivery, clock, _MODEL_TURN)
        clock.advance(200.0)
        command_started = clock.monotonic()
        _ = [e async for e in provider.stream_command("/help")]
        assert provider.essential_delivery.request_floor == command_started

    @pytest.mark.asyncio
    async def test_a_native_direct_command_keeps_the_real_turns_floor(self, clock):
        from kiro_crew.providers import acp_turns

        provider = _direct_acp_provider(clock, ACP_BACKEND_KIRO)
        started = clock.monotonic()
        await _stream(provider.essential_delivery, clock, _MODEL_TURN)
        clock.advance(200.0)
        events = [e async for e in acp_turns.stream_command(provider, "/context")]
        assert any(e.text for e in events)
        assert provider.essential_delivery.request_floor == started

    @pytest.mark.asyncio
    async def test_a_kiro_client_that_prompts_help_moves_the_floor(self, clock):
        from kiro_crew.providers import acp_turns

        provider = _direct_acp_provider(clock, ACP_BACKEND_KIRO)
        # A session handle behind the provider keeps /help on session/prompt.
        provider._client.command_is_native = lambda command: command != "/help"
        await _stream(provider.essential_delivery, clock, _MODEL_TURN)
        clock.advance(200.0)
        command_started = clock.monotonic()
        _ = [e async for e in acp_turns.stream_command(provider, "/help")]
        assert provider.essential_delivery.request_floor == command_started

    @pytest.mark.asyncio
    async def test_a_prompt_transport_direct_command_moves_the_floor(self, clock):
        from kiro_crew.providers import acp_turns

        provider = _direct_acp_provider(clock, ACP_BACKEND_CLAUDE)
        await _stream(provider.essential_delivery, clock, _MODEL_TURN)
        clock.advance(200.0)
        command_started = clock.monotonic()
        _ = [e async for e in acp_turns.stream_command(provider, "/context")]
        assert provider.essential_delivery.request_floor == command_started

    @pytest.mark.parametrize(
        ("backend", "command", "native"),
        [
            (ACP_BACKEND_KIRO, "/context", True),
            (ACP_BACKEND_KIRO, "/compact", False),
            (ACP_BACKEND_KIRO, "/help", False),
            (ACP_BACKEND_KAS, "/context", False),
        ],
    )
    def test_the_handle_names_its_native_commands(self, backend, command, native):
        from kiro_crew.acp import session_handle_turn

        handle = SimpleNamespace(_runtime=SimpleNamespace(acp_backend=backend))
        assert session_handle_turn.command_is_native(handle, command) is native


async def _streamed_turn(mgr: SessionManager, clock: ManualClock, script: list[Any]) -> Any:
    """One finished turn whose floor comes from a real stream through the delivery."""
    provider, _new, _resumed = await mgr.get_or_create(KEY)
    if not isinstance(provider.essential_delivery, EssentialDelivery):
        provider.essential_delivery = EssentialDelivery(clock=clock.monotonic)
    await _stream(provider.essential_delivery, clock, script)
    mgr.check_context_usage(KEY, provider)
    mgr.release(KEY)
    return provider


_MODEL_TURN = [_event("text_chunk", text="hi"), _event("complete", stop_reason="end_turn")]
_SLASH_TURN = [_event("complete", stop_reason="end_turn")]


class TestSlashCommandTurn:
    @pytest.mark.asyncio
    async def test_the_rearm_counts_from_the_last_model_request(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            provider = await _streamed_turn(mgr, clock, _MODEL_TURN)
            mgr.sched.advance(100)
            await _streamed_turn(mgr, clock, _SLASH_TURN)
            mgr.sched.advance(154)
            await _drain(mgr)
            assert _compactions(provider) == 0
            mgr.sched.advance(1)
            await _drain(mgr)
            assert _compactions(provider) == 1

    @pytest.mark.asyncio
    async def test_no_rearm_once_the_fire_time_from_the_last_request_passed(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _streamed_turn(mgr, clock, _MODEL_TURN)
            # Past the fire time with the timer cancelled by the slash turn's claim.
            mgr._compaction.idle.timers.cancel(KEY)
            clock.advance(300.0)
            provider = await _streamed_turn(mgr, clock, _SLASH_TURN)
            assert not mgr._compaction.idle.timers.armed(KEY)
            mgr.sched.advance(600)
            await _drain(mgr)
            assert _compactions(provider) == 0

    @pytest.mark.asyncio
    async def test_a_first_turn_with_no_model_request_arms_nothing(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            await _streamed_turn(mgr, clock, _SLASH_TURN)
            assert not mgr._compaction.idle.timers.armed(KEY)


# ── The dashboard notice and the skipped-backend log ──


def _dashboard_slot(mgr: SessionManager) -> MagicMock:
    """Wire the dashboard's real compact callback onto *mgr*; return the slot it writes."""
    from kiro_crew.dashboard.state_parts.session_events import wire_session_compact_callback

    slot = MagicMock()
    slot._compacting = False
    surface = SimpleNamespace(
        sessions=mgr,
        get_slot=lambda _slot_key: slot,
        broadcast_context_usage=MagicMock(),
        push_slots_update=MagicMock(),
        _notify_channel_compaction=AsyncMock(),
    )
    wire_session_compact_callback(surface)
    return slot


def _notices(slot: MagicMock) -> list[str]:
    return [c.args[1] for c in slot.append.call_args_list]


class TestIdleNotice:
    @pytest.mark.asyncio
    async def test_an_idle_compaction_says_it_ran_while_idle(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(pct=54.0, pct_after=15.0)) as mgr:
            slot = _dashboard_slot(mgr)
            await _turn(mgr)
            mgr.sched.advance(255)
            await _drain(mgr)
        assert _notices(slot) == [
            "🔄 Compacted at 54% while this chat was idle, before its prompt cache expired."
        ]

    @pytest.mark.asyncio
    async def test_a_threshold_compaction_keeps_the_old_notice(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(pct=80.0, pct_after=40.0)) as mgr:
            slot = _dashboard_slot(mgr)
            await _turn(mgr)
            await _drain(mgr)
        assert _notices(slot) == ["🔄 Auto-compacted at 80%."]

    @pytest.mark.asyncio
    async def test_a_failed_idle_compaction_says_nothing_was_changed(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory(pct=54.0, status="failed")) as mgr:
            slot = _dashboard_slot(mgr)
            await _turn(mgr)
            mgr.sched.advance(255)
            await _drain(mgr)
        assert _notices(slot) == [
            "⚠ Compaction while this chat was idle didn't succeed; nothing was changed."
        ]

    @pytest.mark.asyncio
    async def test_a_threshold_failure_keeps_the_old_notice(self, cfg, clock):
        async with _managed(cfg, clock, _provider_factory()) as mgr:
            slot = _dashboard_slot(mgr)
            callback = mgr._compaction.state.on_compacted
            await callback(KEY, 80.0, success=False, outcome=COMPACT_OUTCOME_COMPACTED)
        assert _notices(slot) == [
            "⚠ Auto-compact failed at 80% — will retry after cooldown. "
            "You can run `/compact` manually."
        ]


class TestSkippedBackendLog:
    @pytest.mark.asyncio
    async def test_a_backend_with_no_lifetime_is_logged_once_per_session(self, cfg, clock, caplog):
        caplog.set_level("INFO", logger="kiro_crew.session")
        async with _managed(cfg, clock, _provider_factory(backend=ACP_BACKEND_KAS)) as mgr:
            for _ in range(3):
                await _turn(mgr)
            assert not mgr._compaction.idle.timers.armed(KEY)
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kiro_crew.session" and "does not run on backend" in r.getMessage()
        ]
        assert lines == [
            f"Session {KEY} idle compaction does not run on backend {ACP_BACKEND_KAS}: "
            "no known prompt-cache lifetime"
        ]

    @pytest.mark.asyncio
    async def test_a_removed_session_drops_its_key_and_its_successor_logs_again(
        self, cfg, clock, caplog
    ):
        caplog.set_level("INFO", logger="kiro_crew.session")
        async with _managed(cfg, clock, _provider_factory(backend=ACP_BACKEND_KAS)) as mgr:
            idle = mgr._compaction.idle
            await _turn(mgr)
            assert KEY in idle._skip_logged
            await mgr.reset(KEY)
            assert KEY not in idle._skip_logged
            await _turn(mgr)
            await _turn(mgr)
            assert KEY in idle._skip_logged
        lines = [r for r in caplog.records if "does not run on backend" in r.getMessage()]
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_nothing_is_logged_with_the_setting_off(self, cfg, clock, caplog):
        cfg.session.idle_compaction = False
        caplog.set_level("INFO", logger="kiro_crew.session")
        async with _managed(cfg, clock, _provider_factory(backend=ACP_BACKEND_KAS)) as mgr:
            await _turn(mgr)
        assert not [r for r in caplog.records if "does not run on backend" in r.getMessage()]
