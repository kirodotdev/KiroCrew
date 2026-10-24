"""MCP gateway pool gauges and the gatewayd export path.

Drives the production paths, not copies of them:

* the three pool gauges, through ``register_mcp_gateway_gauges`` on a real SDK
  pipeline, reading a real ``BackendPool`` via ``BackendPool.all_backends``;
* gatewayd's recorder init (``gatewayd._prepare_telemetry``), composing the
  edition telemetry provider through ``platform.bootstrap.compose_telemetry_provider``
  on a non-standalone profile, and checking that the edition's destination gets a
  reader carrying the same resource attributes as any other process;
* the off-consent case, which must compose nothing and export nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import threading
import time as time_mod

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import kiro_crew.platform.context as context_mod
from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.mcp_gateway.pool import BackendPool
from kiro_crew.metrics import mcp_gateway_gauges as gauges
from kiro_crew.metrics import provider as provider_mod
from kiro_crew.platform import bootstrap, build_default_context
from kiro_crew.platform.context import PlatformCompositionError, reset_context
from kiro_crew.platform.interfaces import OtlpDestination
from kiro_crew.testing.wait import async_wait_until, wait_until

ENDPOINT = "https://collector.example.test:4318/v1/metrics"


class _Key:
    def __init__(self, server_name):
        self.server_name = server_name


class _FakeBackend:
    """The fields the gauges read from a ``Backend``."""

    def __init__(self, server_name, *, pid, refcount, alive=True):
        self.pool_key = _Key(server_name)
        self.pid = pid
        self.refcount = refcount
        self.is_alive = alive


def _pool(*backends):
    pool = BackendPool(max_backends=8)
    for i, b in enumerate(backends):
        pool._backends[f"digest-{i}"] = b
    return pool


def _collect(register=gauges.register_mcp_gateway_gauges):
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    register(meter_provider.get_meter("test"))
    data = reader.get_metrics_data()
    meter_provider.shutdown()
    out: dict[str, dict] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = {
                    dp.attributes.get("class"): dp.value for dp in metric.data.data_points
                }
    return out


@pytest.fixture(autouse=True)
def _unbound():
    gauges._pool = None
    yield
    gauges._pool = None


# ---------------------------------------------------------------------------
# Pool gauges
# ---------------------------------------------------------------------------


def test_two_sessions_on_one_backend():
    """Two attachments sharing one live backend: attached > live, RSS summed."""
    pool = _pool(_FakeBackend("some-user-server", pid=os.getpid(), refcount=2))
    gauges.bind_pool(pool)
    got = _collect()

    assert got[gauges.GAUGE_BACKENDS_LIVE] == {"first_party": 0, "third_party": 1}
    assert got[gauges.GAUGE_SESSIONS_ATTACHED] == {"first_party": 0, "third_party": 2}
    assert (
        got[gauges.GAUGE_SESSIONS_ATTACHED]["third_party"]
        > got[gauges.GAUGE_BACKENDS_LIVE]["third_party"]
    )
    rss = got[gauges.GAUGE_BACKENDS_RSS]
    assert rss["first_party"] == 0
    # The pool's subtree probe reads /proc: Linux only, a gap elsewhere.
    if sys.platform == "linux":
        assert rss["third_party"] > 0
    else:
        assert "third_party" not in rss


def test_attributes_are_closed_and_carry_no_name():
    """Only class=first_party/third_party/other; never a server name or pid."""
    pool = _pool(
        _FakeBackend("kirocrew-core", pid=os.getpid(), refcount=3),
        _FakeBackend("secret-internal-tool", pid=os.getpid(), refcount=1),
        _FakeBackend("", pid=None, refcount=1),
    )
    gauges.bind_pool(pool)
    got = _collect()
    for name in (gauges.GAUGE_BACKENDS_LIVE, gauges.GAUGE_SESSIONS_ATTACHED):
        assert set(got[name]) == {"first_party", "third_party", "other"}
    assert got[gauges.GAUGE_BACKENDS_LIVE] == {"first_party": 1, "third_party": 1, "other": 1}
    assert got[gauges.GAUGE_SESSIONS_ATTACHED] == {
        "first_party": 3,
        "third_party": 1,
        "other": 1,
    }
    # The pid-less backend gave no RSS reading, so its class is a gap, not 0.
    # Off Linux no class has a reading, so the metric publishes no point at all.
    assert "other" not in got.get(gauges.GAUGE_BACKENDS_RSS, {})


def test_dead_backend_is_not_counted():
    pool = _pool(
        _FakeBackend("a", pid=os.getpid(), refcount=1),
        _FakeBackend("b", pid=os.getpid(), refcount=5, alive=False),
    )
    gauges.bind_pool(pool)
    data = gauges.read_pool()
    assert data["live"]["third_party"] == 1
    assert data["attached"]["third_party"] == 1


def test_unreadable_rss_is_a_gap_and_dead_pid_adds_zero():
    """A pid with no reading adds 0 and is skipped; all-unreadable -> no point."""
    pool = _pool(
        _FakeBackend("a", pid=101, refcount=1),
        _FakeBackend("b", pid=102, refcount=1),
    )
    gauges.bind_pool(pool)
    readings_kb = {101: 4, 102: -1}
    data = gauges.read_pool(rss_kb_for_pid=readings_kb.get)
    assert data["rss"]["third_party"] == 4096
    data = gauges.read_pool(rss_kb_for_pid=lambda pid: -1)
    assert "third_party" not in data["rss"]
    assert data["rss"]["first_party"] == 0


def test_gauges_and_control_snapshot_count_the_same_backends(monkeypatch):
    """One population rule and one RSS probe feed both pool memory figures.

    A draining and a connection-private backend are processes the daemon holds,
    but no session can be pooled onto either, so neither the dashboard snapshot
    nor the gauges count them, and both sum the same per-pid reading.
    """
    from kiro_crew.mcp_gateway import pool as pool_mod

    readings_kb = {201: 10, 202: 20, 203: 40, 204: 80}
    monkeypatch.setattr(pool_mod, "backend_rss_kb", lambda pid: readings_kb.get(pid, -1))
    pool = _pool(
        _FakeBackend("a", pid=201, refcount=2),
        _FakeBackend("b", pid=202, refcount=3),
    )
    for b in pool._backends.values():
        b.last_used_at = 0.0
    draining = _FakeBackend("c", pid=203, refcount=1)
    private = _FakeBackend("d", pid=204, refcount=1)
    pool._draining.append(type("_Drain", (), {"backend": draining})())
    pool._exclusive["stub-1"] = private
    assert {draining, private} <= set(pool.all_backends())
    gauges.bind_pool(pool)

    data = gauges.read_pool()
    snap = asyncio.run(pool.metrics_snapshot_async())

    rows = snap["backends"]
    assert data["live"]["third_party"] == len(rows) == 2
    assert data["attached"]["third_party"] == sum(r["stubs"] for r in rows) == 5
    assert data["rss"]["third_party"] == sum(r["rss_kb"] for r in rows) * 1024 == 30 * 1024


def test_unbound_process_publishes_nothing():
    """Every other process registers the gauges and publishes no series."""
    assert _collect() == {}


def test_unbind_only_clears_own_pool():
    first, second = _pool(), _pool()
    gauges.bind_pool(second)
    gauges.unbind_pool(first)
    assert gauges._pool is second
    gauges.unbind_pool(second)
    assert gauges._pool is None


def test_classify_unknown_maps_to_other():
    assert gauges.classify(None) == "other"
    assert gauges.classify("") == "other"
    assert gauges.classify("kirocrew-cron") == "first_party"
    assert gauges.classify("anything-else") == "third_party"


# ---------------------------------------------------------------------------
# gatewayd recorder init and export path
# ---------------------------------------------------------------------------


class _EditionTelemetry:
    def __init__(self):
        self.calls = 0

    def record_event(self, event_type, data):
        return None

    def frontend_rum_config(self):
        return None

    def otlp_destinations(self, cfg):
        self.calls += 1
        return (OtlpDestination(name="edition", endpoint=ENDPOINT, signals=frozenset({"metrics"})),)


def _join_compose_thread():
    for t in threading.enumerate():
        if t.name == "gatewayd-telemetry-compose":
            t.join(30)


@pytest.fixture
def gatewayd_env(monkeypatch, tmp_path):
    """A host whose edition supplies one OTLP destination; non-standalone by default."""
    from kiro_crew.mcp_gateway import gatewayd

    edition = _EditionTelemetry()
    composed = []
    release = threading.Event()
    release.set()
    state = {"profile": "enterprise", "release": release}

    def _discover(profile, cfg, *, entry_points=None, policy=None):
        # A test can hold composition open to see what the daemon does meanwhile.
        assert release.wait(30)
        composed.append(profile)
        base = build_default_context(cfg)
        return dataclasses.replace(base, profile=profile, telemetry=edition)

    monkeypatch.setattr(bootstrap, "plugin_entry_points", lambda: [])
    monkeypatch.setattr(
        bootstrap, "resolve_profile", lambda cfg, entry_points=None: state["profile"]
    )
    monkeypatch.setattr(bootstrap, "discover_companion_context", _discover)
    reset_context()

    def _context():
        # As in a real daemon: nothing is installed, so a non-standalone host
        # fails closed here and a standalone one gets the lazy default.
        if state["profile"] != "standalone":
            raise PlatformCompositionError("not booted")
        return build_default_context(KiroCrewConfig.load())

    monkeypatch.setattr(context_mod, "current_context", _context)
    monkeypatch.setattr(provider_mod, "current_context", _context)

    readers: list = []

    def _reader_for(dest, cfg):
        reader = InMemoryMetricReader()
        reader.dest_name = dest.name
        readers.append(reader)
        return reader

    monkeypatch.setattr(provider_mod, "_build_otlp_reader", _reader_for)

    def _config(enabled, **extra):
        fake = KiroCrewConfig(
            telemetry=TelemetryConfig(
                enabled=enabled,
                local_dir=str(tmp_path / "metrics"),
                export_interval_seconds=3600,
                **extra,
            )
        )
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake))

    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
    provider_mod.reset_for_testing()
    yield gatewayd, edition, composed, readers, _config, state
    release.set()
    _join_compose_thread()
    provider_mod.use_telemetry_provider(None)
    provider_mod.reset_for_testing()
    reset_context()


def _exported(reader):
    data = reader.get_metrics_data()
    names, resource = set(), None
    for rm in data.resource_metrics:
        resource = dict(rm.resource.attributes)
        for sm in rm.scope_metrics:
            names.update(m.name for m in sm.metrics)
    return names, resource


def test_gatewayd_init_exports_to_edition_destination(gatewayd_env):
    gatewayd, edition, composed, readers, set_config, state = gatewayd_env
    set_config(True)
    state["release"].clear()  # composition is slow: it has not finished yet

    gatewayd._prepare_telemetry()

    # The startup build returned without waiting for composition and is live at
    # once (local sink and gauges), with no egress yet.
    assert composed == []
    assert provider_mod.get_recorder().enabled
    assert readers == []
    state["release"].set()

    # Composition lands on its own thread and the recorder is rebuilt with the
    # edition's destination, through the real compose path.
    _join_compose_thread()
    assert composed == ["enterprise"]
    wait_until(
        lambda: provider_mod.get_recorder().enabled and readers,
        describe=lambda: f"readers={readers!r}",
    )
    assert [r.dest_name for r in readers] == ["edition"]

    gauges.bind_pool(_pool(_FakeBackend("x", pid=os.getpid(), refcount=2)))
    gatewayd._emit_backend_acquire_metric(1.0, warm=True)
    names, resource = _exported(readers[0])
    for expected in (*gauges.ALL_METRIC_NAMES, "kirocrew.mcp.backend.acquire.duration"):
        assert expected in names, expected
    # Same resource attributes the main process exports.
    assert resource is not None
    expected_attrs = provider_mod._resource_attributes()
    for key in ("service.name", "service.version", "os.type", "host.cpu.logical_count"):
        assert resource[key] == expected_attrs[key]
    assert resource["process.pid"] == os.getpid()
    # The daemon's series are told apart from the main process's by role, not
    # only by pid: the main process builds no kirocrew.process.role at all.
    assert resource[provider_mod.RESOURCE_ATTR_PROCESS_ROLE] == "gatewayd"

    # Nothing was installed: every other context reader in the daemon keeps the
    # behavior it had before this change.
    assert context_mod._ACTIVE is None


def test_gatewayd_standalone_exports_from_the_first_build(gatewayd_env):
    """A standalone host's configured endpoint is live at once; no rebuild."""
    gatewayd, edition, composed, readers, set_config, state = gatewayd_env
    state["profile"] = "standalone"
    set_config(True, otlp_endpoint=ENDPOINT)

    gatewayd._prepare_telemetry()
    assert [r.dest_name for r in readers] == ["telemetry.otlp_endpoint"]
    _join_compose_thread()
    assert composed == []  # standalone composes no companion
    assert len(readers) == 1  # same destination either way: nothing rebuilt


def test_gatewayd_off_consent_composes_and_exports_nothing(gatewayd_env):
    gatewayd, edition, composed, readers, set_config, _ = gatewayd_env
    set_config(False)

    gatewayd._prepare_telemetry()
    _join_compose_thread()
    rec = provider_mod.get_recorder()
    gauges.bind_pool(_pool(_FakeBackend("x", pid=os.getpid(), refcount=2)))
    gatewayd._emit_backend_acquire_metric(1.0, warm=True)

    assert composed == []
    assert edition.calls == 0
    assert readers == []
    assert not rec.enabled  # no-op recorder: no provider, no reader, no export
    # A later opt-in composes lazily on the provider's own rebuild, off the loop.
    assert provider_mod._provider_factory is gatewayd._compose_telemetry_provider


def test_failing_composition_degrades_to_local_only(gatewayd_env, monkeypatch):
    gatewayd, edition, composed, readers, set_config, _ = gatewayd_env
    set_config(True)

    def _boom(profile, cfg, *, entry_points=None, policy=None):
        raise PlatformCompositionError("companion missing")

    monkeypatch.setattr(bootstrap, "discover_companion_context", _boom)
    gatewayd._prepare_telemetry()
    _join_compose_thread()
    rec = provider_mod.get_recorder()
    assert readers == []
    assert rec.enabled  # still collecting locally


def test_compose_telemetry_provider_applies_the_boot_security_floor(gatewayd_env, monkeypatch):
    """A companion boot would refuse is refused for the daemon's egress too."""
    gatewayd, edition, composed, readers, set_config, _ = gatewayd_env
    set_config(True)
    cfg = KiroCrewConfig.load()
    # Wrapped only to carry the companion's ceiling; the provider is the edition's.
    assert bootstrap.compose_telemetry_provider(cfg)._inner is edition

    def _weak(profile, cfg, *, entry_points=None, policy=None):
        base = build_default_context(cfg)
        return dataclasses.replace(base, profile=profile, telemetry=edition, security=object())

    monkeypatch.setattr(bootstrap, "discover_companion_context", _weak)
    with pytest.raises(PlatformCompositionError, match="PolicyAuthority"):
        bootstrap.compose_telemetry_provider(cfg)


def _exporting_daemon(gatewayd_env):
    gatewayd, edition, composed, readers, set_config, _ = gatewayd_env
    set_config(True)
    gatewayd._prepare_telemetry()
    _join_compose_thread()
    wait_until(lambda: provider_mod.get_recorder().enabled and readers)
    wait_until(lambda: not provider_mod._check_in_flight)
    assert provider_mod._provider is not None
    return gatewayd, set_config


def test_idle_daemon_stops_exporting_after_consent_is_withdrawn(gatewayd_env):
    """The consent sweeper alone, with no acquire, applies a withdrawal.

    The provider's recheck window stays at its real value and was just stamped,
    exactly the state a sweeper tick finds when it lands short of the window. A
    tick that only asks ``get_recorder()`` sees no recheck due and keeps
    exporting; a tick must re-read consent itself.
    """
    from kiro_crew.mcp_gateway.daemon.sweepers import _telemetry_consent_sweeper

    gatewayd, set_config = _exporting_daemon(gatewayd_env)
    assert provider_mod._CONSENT_RECHECK_SECS >= 30
    assert not provider_mod._consent_recheck_due()

    set_config(False)

    async def _run():
        stop = asyncio.Event()
        task = asyncio.create_task(_telemetry_consent_sweeper(0.01, stop))
        try:
            await async_wait_until(
                lambda: provider_mod._provider is None,
                timeout=5,
                describe=lambda: f"provider={provider_mod._provider!r}",
            )
        finally:
            stop.set()
            await task

    asyncio.run(_run())
    assert not provider_mod.get_recorder().enabled


class _Clock:
    """A monotonic clock the test moves; everything else is the real ``time``."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time_mod, name)


def test_withdrawal_lands_within_one_window_at_gatewayd_cadence(gatewayd_env, monkeypatch):
    """At gatewayd's own tick interval a withdrawal is applied inside one window.

    Drives the provider's real recheck on a fake clock: each sweeper tick moves
    the clock by ``_CONSENT_SWEEP_SECS``, and every config read takes time, so
    the worker's stamp lands after the tick that started it -- the timing that
    made a once-per-window tick miss every other window.
    """
    from kiro_crew.mcp_gateway.daemon import sweepers

    gatewayd, set_config = _exporting_daemon(gatewayd_env)
    window = provider_mod._CONSENT_RECHECK_SECS
    assert gatewayd._CONSENT_SWEEP_SECS <= window / 2

    clock = _Clock()
    monkeypatch.setattr(provider_mod, "time", clock)
    set_config(False)
    off = KiroCrewConfig.load()

    def _slow_load(cls):
        clock.now += 0.25  # a read is never instantaneous
        return off

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_slow_load))
    # The recorder was verified a moment ago, then consent was withdrawn.
    provider_mod._consent_checked_at = clock.now
    withdrawn_at = clock.now

    for _tick in range(2):
        clock.now += gatewayd._CONSENT_SWEEP_SECS
        sweepers.recheck_consent()
        wait_until(lambda: not provider_mod._check_in_flight, timeout=5)
        if provider_mod._provider is None:
            break
    assert provider_mod._provider is None, "still exporting after two ticks"
    assert clock.now - withdrawn_at <= window
    assert not provider_mod.get_recorder().enabled
