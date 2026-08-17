"""Tests for kiro_crew.metrics.provider — consent gate + recorder singleton."""

import threading
import time

import pytest

from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics.provider import MetricsRecorder, get_recorder, reset_for_testing
from kiro_crew.metrics.provider import shutdown as provider_shutdown


def _patch_config(monkeypatch, **tel_kwargs):
    fake = KiroCrewConfig(telemetry=TelemetryConfig(**tel_kwargs))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake))
    # Keep the consent gate deterministic: a stray KIROCREW_TELEMETRY in the
    # ambient env must not flip these tests. Individual env-var tests re-set it.
    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)


def test_disabled_by_default(monkeypatch):
    reset_for_testing()
    _patch_config(monkeypatch, enabled=False)
    try:
        assert get_recorder().enabled is False
    finally:
        reset_for_testing()


def test_enabled_builds_live_recorder(tmp_path, monkeypatch):
    reset_for_testing()
    _patch_config(
        monkeypatch,
        enabled=True,
        local_dir=str(tmp_path),
        export_interval_seconds=3600,
    )
    try:
        rec = get_recorder()
        assert rec.enabled is True
        # Routes through a real MeterProvider without raising.
        rec.histogram("kirocrew.session.startup.duration", 1.0, unit="ms")
    finally:
        reset_for_testing()


def test_recorder_is_cached(monkeypatch):
    reset_for_testing()
    _patch_config(monkeypatch, enabled=False)
    try:
        assert get_recorder() is get_recorder()
    finally:
        reset_for_testing()


def _wait_for(predicate, timeout=5.0):
    """Poll until predicate holds. Used where the background work is a trivial
    no-op that completes in microseconds (e.g. a fake reader shutdown call, or
    draining a real worker after a deliberately-triggered timeout in
    ``TestResetForTestingWaitsOutInFlightWorker``). Tests that need to observe
    a real consent-worker RUN use event-based synchronization instead — see
    ``_worker_event``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _worker_event(monkeypatch):
    """Wrap ``_consent_worker`` so it signals a threading.Event on completion.

    Returns the event. Tests wait on the event with a generous bound instead of
    polling a predicate against a wall-clock deadline. This eliminates flakiness
    under host load because the test never wakes up until the worker has actually
    finished, regardless of how long it was starved.

    The event is re-armed (cleared) each time the worker starts so tests that
    trigger multiple worker runs can wait again after the first.

    Relies on every test's own ``reset_for_testing()`` call to wait out a worker
    left running by a prior test, so this helper does not touch
    ``_check_in_flight`` itself — see ``reset_for_testing``'s docstring.
    """
    import kiro_crew.metrics.provider as provider_mod

    done = threading.Event()
    real_worker = provider_mod._consent_worker

    def _signaling_worker(generation: int) -> None:
        done.clear()
        try:
            real_worker(generation)
        finally:
            done.set()

    monkeypatch.setattr(provider_mod, "_consent_worker", _signaling_worker)
    return done


def test_reader_thread_reaped_when_meterprovider_init_fails(tmp_path, monkeypatch):
    """PeriodicExportingMetricReader starts its daemon ticker thread in
    __init__. If a later init step (MeterProvider) raises, the reader is already
    ticking — the provider must shut it down before degrading, or an orphaned
    thread spams export WARNINGs for the whole process lifetime."""
    import kiro_crew.metrics.provider as provider_mod

    reset_for_testing()
    _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))

    shutdown_calls = {"n": 0}

    class FakeReader:
        def __init__(self, *a, **k):
            pass  # stand-in for the real reader's thread-starting __init__

        def shutdown(self, *a, **k):
            shutdown_calls["n"] += 1

    def _boom(*a, **k):
        raise RuntimeError("meter provider init failed")

    monkeypatch.setattr(provider_mod, "PeriodicExportingMetricReader", FakeReader)
    monkeypatch.setattr(provider_mod, "MeterProvider", _boom)
    try:
        rec = get_recorder()
        assert rec.enabled is False  # degraded to no-op
        assert _wait_for(lambda: shutdown_calls["n"] == 1)  # reaped, not orphaned
    finally:
        reset_for_testing()


def test_the_otlp_reader_is_reaped_too_when_init_fails(tmp_path, monkeypatch):
    """With telemetry.otlp_endpoint set there are TWO started readers.

    Reaping only the first leaves the OTLP reader's ticker alive while telemetry
    reports itself disabled — an egress thread surviving a failure that the caller
    is told turned collection off. get_recorder() rebuilds on a consent change, so
    a repeating failure would leak one per flip rather than one per process.
    """
    import kiro_crew.metrics.provider as provider_mod

    reset_for_testing()
    _patch_config(
        monkeypatch,
        enabled=True,
        local_dir=str(tmp_path),
        otlp_endpoint="https://collector.example.internal:4318",
    )

    shut = []

    class FakeReader:
        def __init__(self, *a, **k):
            self.name = "local"

        def shutdown(self, *a, **k):
            shut.append(self.name)

    class FakeOtlpReader(FakeReader):
        def __init__(self, *a, **k):
            self.name = "otlp"

    def _boom(*a, **k):
        raise RuntimeError("meter provider init failed")

    monkeypatch.setattr(provider_mod, "PeriodicExportingMetricReader", FakeReader)
    monkeypatch.setattr(provider_mod, "_build_otlp_reader", lambda cfg: FakeOtlpReader())
    monkeypatch.setattr(provider_mod, "MeterProvider", _boom)
    try:
        assert get_recorder().enabled is False
        assert _wait_for(
            lambda: sorted(shut) == ["local", "otlp"]
        ), f"expected both readers reaped, got {sorted(shut)}"
    finally:
        reset_for_testing()


def test_degrades_to_noop_when_otel_missing(monkeypatch):
    """with opentelemetry absent from the env closure, the provider
    must degrade to a no-op recorder instead of crashing the eager boot chain."""
    import kiro_crew.metrics.provider as provider_mod

    reset_for_testing()
    _patch_config(monkeypatch, enabled=True, local_dir="/tmp/does-not-matter")
    monkeypatch.setattr(provider_mod, "_OTEL_AVAILABLE", False)
    try:
        rec = get_recorder()
        assert rec.enabled is False
        # A histogram call on the no-op recorder must not raise.
        rec.histogram("kirocrew.session.startup.duration", 1.0, unit="ms")
    finally:
        reset_for_testing()


# ── OTLP opt-in egress (rec #1: no egress by default) ─────────────────────


def test_otlp_reader_none_by_default():
    """Empty otlp_endpoint => no OTLP reader => no network egress (default)."""
    from kiro_crew.config.loader import TelemetryConfig
    from kiro_crew.metrics.provider import _build_otlp_reader

    assert _build_otlp_reader(TelemetryConfig(enabled=True)) is None


def test_otlp_reader_degrades_without_logging_endpoint(monkeypatch, caplog):
    """Missing exporter degrades locally without logging credential-bearing URL."""
    import builtins

    from kiro_crew.config.loader import TelemetryConfig
    from kiro_crew.metrics.provider import _build_otlp_reader

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if "otlp" in name:
            raise ImportError("simulated missing otlp extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    endpoint = "https://user:super-secret@example.test/v1/metrics?token=hidden"
    cfg = TelemetryConfig(enabled=True, otlp_endpoint=endpoint)
    # Must NOT raise — returns None and telemetry stays local-only.
    assert _build_otlp_reader(cfg) is None
    assert endpoint not in caplog.text
    assert "super-secret" not in caplog.text
    assert "token=hidden" not in caplog.text


def test_otlp_constructor_failure_never_logs_endpoint(monkeypatch, caplog):
    """Constructor errors must not echo credential-bearing endpoint URLs."""
    import sys
    import types

    from kiro_crew.config.loader import TelemetryConfig
    from kiro_crew.metrics.provider import _build_otlp_reader

    endpoint = "https://user:super-secret@example.test/v1/metrics?token=hidden"

    class _FailingOTLPMetricExporter:
        def __init__(self, *, endpoint):
            raise ValueError(f"invalid endpoint: {endpoint}")

    mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    mod.OTLPMetricExporter = _FailingOTLPMetricExporter  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        mod,
    )

    cfg = TelemetryConfig(enabled=True, otlp_endpoint=endpoint)
    assert _build_otlp_reader(cfg) is None
    assert endpoint not in caplog.text
    assert "super-secret" not in caplog.text
    assert "token=hidden" not in caplog.text


def test_retention_config_defaults():
    """Retention caps default off so upgrades never delete existing shards."""
    from kiro_crew.config.loader import TelemetryConfig

    cfg = TelemetryConfig()
    assert cfg.retention_days == 0
    assert cfg.max_total_mb == 0
    # Negative values are clamped to 0 (disabled) rather than pruning everything.
    clamped = TelemetryConfig(retention_days=-5, max_total_mb=-1)
    assert clamped.retention_days == 0
    assert clamped.max_total_mb == 0


# ── Env-var opt-in (rec #14: easy opt-in) ─────────────────────────────────


def test_env_var_opts_in_when_config_disabled(tmp_path, monkeypatch):
    """KIROCREW_TELEMETRY=1 enables LOCAL telemetry even if the config flag is off."""
    reset_for_testing()
    _patch_config(monkeypatch, enabled=False, local_dir=str(tmp_path), export_interval_seconds=3600)
    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    try:
        assert get_recorder().enabled is True
    finally:
        reset_for_testing()


def test_env_var_opts_out_when_config_enabled(monkeypatch):
    """KIROCREW_TELEMETRY=0 force-disables telemetry even if the config flag is on."""
    reset_for_testing()
    _patch_config(monkeypatch, enabled=True, local_dir="/tmp/should-not-matter")
    monkeypatch.setenv("KIROCREW_TELEMETRY", "0")
    try:
        assert get_recorder().enabled is False
    finally:
        reset_for_testing()


def test_env_var_blank_defers_to_config(monkeypatch):
    """A blank/unknown env value defers to the config flag (still default-off)."""
    from kiro_crew.metrics.provider import _consent_enabled

    monkeypatch.setenv("KIROCREW_TELEMETRY", "   ")
    assert _consent_enabled(TelemetryConfig(enabled=False)) is False
    assert _consent_enabled(TelemetryConfig(enabled=True)) is True


# ── OTLP opt-in ENABLES egress (rec #1: only when explicitly configured) ──


def test_otlp_reader_built_when_endpoint_set(monkeypatch):
    """A non-empty otlp_endpoint yields a live OTLP reader (opt-in enables export)."""
    import sys
    import types

    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

    from kiro_crew.metrics.provider import _build_otlp_reader

    # Stub the optional OTLP/HTTP exporter extra so the test never needs the
    # real package (or a network endpoint); asserts the opt-in wiring path.
    captured = {}

    class _StubOTLPMetricExporter(MetricExporter):
        def __init__(self, *, endpoint):
            super().__init__()
            captured["endpoint"] = endpoint

        def export(self, metrics_data, timeout_millis=10_000, **kwargs):
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis=10_000):
            return True

        def shutdown(self, timeout_millis=30_000, **kwargs):
            return None

    mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    mod.OTLPMetricExporter = _StubOTLPMetricExporter  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        mod,
    )

    cfg = TelemetryConfig(enabled=True, otlp_endpoint="http://localhost:4318/v1/metrics")
    reader = _build_otlp_reader(cfg)
    assert reader is not None, "opt-in endpoint must build an OTLP reader"
    assert captured["endpoint"] == "http://localhost:4318/v1/metrics"
    # Clean shutdown so the reader's daemon thread doesn't linger.
    try:
        reader.shutdown()
    except Exception:
        pass


# ── Consent can move under a running process ──────────────────────────────


class TestConsentRecheck:
    """A config edit from OUTSIDE this process must take effect without a restart.

    `kirocrew config set telemetry.enabled true` writes config.json from a separate
    process. The recorder is memoized, so without a recheck it stays a no-op for the
    life of the gateway while the dashboard — which reads config live — reports
    collection as on. That combination is the failure these tests pin: the product
    documents a command that silently records nothing.

    The recheck is rate-limited rather than per-call, so both halves matter: it must
    fire once the window elapses, and it must NOT read config on every metric call.
    """

    def _elapse_window(self, monkeypatch):
        """Push the recheck clock past its window without sleeping."""
        import kiro_crew.metrics.provider as provider_mod

        monkeypatch.setattr(
            provider_mod, "_consent_checked_at", 0.0
        )  # monotonic() - 0.0 always exceeds the window

    def test_enabling_out_of_band_starts_collection(self, tmp_path, monkeypatch):
        """Opting in lands once the off-thread build completes, not on that call.

        The flip is noticed on the calling thread — the event loop, for the
        route-latency middleware — but the build imports the OTel SDK, so it runs
        on a worker. The call that notices therefore returns a no-op and a later
        one returns the live recorder.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            assert get_recorder().enabled is False
            done = _worker_event(monkeypatch)

            # What `config set` does: the value on disk changes, nothing calls us.
            _patch_config(
                monkeypatch,
                enabled=True,
                local_dir=str(tmp_path),
                export_interval_seconds=3600,
            )
            self._elapse_window(monkeypatch)

            get_recorder()  # notices the flip and schedules the build
            assert done.wait(timeout=10), "consent worker never completed"
            assert get_recorder().enabled is True
            assert provider_mod._built_consent is True
        finally:
            reset_for_testing()

    def test_the_rebuild_does_not_run_on_the_calling_thread(self, tmp_path, monkeypatch):
        """The build must not happen on the thread that noticed the flip.

        `_build_recorder` imports the OTel SDK (~120 modules) and touches the
        filesystem; `get_recorder()` is called on the event loop by the
        route-latency middleware for every request.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()
            caller = threading.get_ident()
            build_threads: list[int] = []
            build_done = threading.Event()
            real_build = provider_mod._build_recorder

            def _spy():
                build_threads.append(threading.get_ident())
                build_done.set()
                return real_build()

            monkeypatch.setattr(provider_mod, "_build_recorder", _spy)
            _patch_config(
                monkeypatch,
                enabled=True,
                local_dir=str(tmp_path),
                export_interval_seconds=3600,
            )
            self._elapse_window(monkeypatch)

            get_recorder()
            assert build_done.wait(timeout=10), "the rebuild never ran"
            assert caller not in build_threads, "the build ran on the calling thread"
        finally:
            reset_for_testing()

    def test_a_reenable_during_an_in_flight_build_is_not_stranded(self, tmp_path, monkeypatch):
        """Flapping consent during a build must not leave collection off forever.

        The older worker discards its result on a generation mismatch, and
        `_built_consent` already records the new value — so if the re-enable did not
        start its own worker, the recheck would see no difference and never retry,
        and recording would stay a no-op for the life of the process.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()

            first_in_build = threading.Event()
            release_first = threading.Event()
            real_build = provider_mod._build_recorder
            calls = {"n": 0}
            second_done = threading.Event()

            def _build(*a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    first_in_build.set()
                    release_first.wait(timeout=10)
                result = real_build()
                if calls["n"] >= 2:
                    second_done.set()
                return result

            monkeypatch.setattr(provider_mod, "_build_recorder", _build)
            done = _worker_event(monkeypatch)
            live = dict(enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600)
            _patch_config(monkeypatch, **live)
            self._elapse_window(monkeypatch)
            get_recorder()  # schedules build #1
            assert first_in_build.wait(timeout=10), "first build never started"

            # Flap: off, then back on, while build #1 is still running.
            _patch_config(monkeypatch, enabled=False)
            provider_shutdown()
            get_recorder()
            _patch_config(monkeypatch, **live)
            provider_shutdown()
            get_recorder()  # tries to schedule build #2, but #1 is still in flight
            release_first.set()

            # Wait for the first worker to finish (clears _check_in_flight).
            assert done.wait(timeout=10), "first consent worker never completed"

            # Now a get_recorder() call can schedule the second worker for the
            # current generation (the re-enable).
            done.clear()
            self._elapse_window(monkeypatch)
            get_recorder()
            assert done.wait(timeout=10), "second consent worker never completed"
            assert get_recorder().enabled is True, "collection stayed a no-op after the re-enable"
        finally:
            release_first.set()
            reset_for_testing()

    def test_a_superseded_build_does_not_flush_under_the_lock(self, tmp_path, monkeypatch):
        """The discard path must release `_lock` before flushing the doomed provider.

        A provider shutdown joins its reader threads (30s deadline), so holding the
        lock across it blocks every get_recorder() on the event loop on lock
        ACQUISITION — the same stall as flushing inline, one level removed.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(
            monkeypatch, enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
        try:
            release_flush = threading.Event()
            flush_started = threading.Event()
            lock_free_during_flush = {"v": None}

            def _slow_flush(doomed):
                flush_started.set()
                # Whoever calls us must NOT be holding the lock.
                got = provider_mod._lock.acquire(timeout=2)
                lock_free_during_flush["v"] = got
                if got:
                    provider_mod._lock.release()
                release_flush.wait(timeout=5)

            monkeypatch.setattr(provider_mod, "_flush_detached_provider", _slow_flush)

            built = provider_mod._Build(MetricsRecorder(None), object(), True)

            def _build_then_supersede():
                # Stand in for a shutdown landing while the build was running.
                with provider_mod._lock:
                    provider_mod._build_generation += 1
                return built

            monkeypatch.setattr(provider_mod, "_build_recorder", _build_then_supersede)

            with provider_mod._lock:
                provider_mod._built_consent = False  # force "consent changed"
                gen = provider_mod._build_generation
            worker = threading.Thread(target=provider_mod._consent_worker, args=(gen,), daemon=True)
            worker.start()
            assert flush_started.wait(timeout=5), "the discard flush never ran"
            release_flush.set()
            worker.join(timeout=5)
            assert lock_free_during_flush["v"] is True, "_lock was held across the discard flush"
        finally:
            release_flush.set()
            reset_for_testing()

    def test_a_disable_landing_mid_rebuild_is_not_undone_by_it(self, tmp_path, monkeypatch):
        """A build that finishes after a withdrawal must not install itself.

        Otherwise turning recording off during the enable window would silently
        come back on when the in-flight build completed — collection the user
        explicitly stopped.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()

            in_build = threading.Event()
            release = threading.Event()
            real_build = provider_mod._build_recorder

            def _slow_build():
                in_build.set()
                release.wait(timeout=10)
                return real_build()

            monkeypatch.setattr(provider_mod, "_build_recorder", _slow_build)
            done = _worker_event(monkeypatch)
            _patch_config(
                monkeypatch,
                enabled=True,
                local_dir=str(tmp_path),
                export_interval_seconds=3600,
            )
            self._elapse_window(monkeypatch)
            get_recorder()  # schedules the build
            assert in_build.wait(timeout=10), "the build never started"

            # The user turns it back off while the build is still running.
            _patch_config(monkeypatch, enabled=False)
            provider_shutdown()
            release.set()

            # Wait for the superseded worker to finish (its finally clears
            # _check_in_flight and signals done).
            assert done.wait(timeout=10), "consent worker never completed"
            assert get_recorder().enabled is False, "a stale build resurrected collection"
        finally:
            release.set()
            reset_for_testing()

    def test_disabling_out_of_band_stops_collection_and_flushes(self, tmp_path, monkeypatch):
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(
            monkeypatch, enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
        try:
            assert get_recorder().enabled is True
            live_provider = provider_mod._provider
            assert live_provider is not None
            flushed = threading.Event()

            # SPY, never a replacement. A stub that only records leaves the
            # provider's PeriodicExportingMetricReader ticker thread running for the
            # life of the process, and OTel restarts that thread in every fork child
            # via `os.register_at_fork` -- which makes the sandbox userns probe's
            # child multithreaded, and `unshare(CLONE_NEWUSER)` then returns EINVAL
            # (it implies CLONE_THREAD). That verdict is cached as "this host has no
            # sandbox backend", so every later sandboxed spawn on the worker fails
            # closed: 19 failures across two app suites, none of them here.
            _real_shutdown = live_provider.shutdown

            def _observed_shutdown(*a, **k):
                flushed.set()
                return _real_shutdown(*a, **k)

            monkeypatch.setattr(live_provider, "shutdown", _observed_shutdown)

            done = _worker_event(monkeypatch)
            _patch_config(monkeypatch, enabled=False)
            self._elapse_window(monkeypatch)

            get_recorder()  # notices and schedules the consent worker
            assert done.wait(timeout=10), "consent worker never completed"
            assert get_recorder().enabled is False
            # Withdrawing consent must flush what was already aggregated rather
            # than dropping the reader on the floor. The flush runs on the worker,
            # so wait for it rather than asserting synchronously.
            assert flushed.wait(timeout=10), "detached flush never ran"
        finally:
            reset_for_testing()

    def test_the_flush_does_not_run_on_the_calling_thread(self, tmp_path, monkeypatch):
        """get_recorder() runs on the event loop; the flush must not.

        A provider shutdown joins each reader's export thread (30s deadline) and,
        with telemetry.otlp_endpoint set, ends in a synchronous network POST. Doing
        that inline in get_recorder() — which the route-latency middleware calls in a
        finally on every request — stalls every task on the loop.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(
            monkeypatch, enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
        try:
            get_recorder()
            live_provider = provider_mod._provider
            assert live_provider is not None

            caller = threading.get_ident()
            seen: dict[str, object] = {}
            released = threading.Event()
            flush_entered = threading.Event()

            _real_shutdown = live_provider.shutdown

            def _slow_shutdown(*a, **k):
                seen["thread"] = threading.get_ident()
                flush_entered.set()
                released.wait(timeout=10)  # stands in for the 30s join + final POST
                seen["done"] = True
                # Then do the real thing: the stand-in models the DELAY, and
                # skipping the shutdown leaks the reader's ticker thread (see the
                # sibling test above for what that thread goes on to break).
                return _real_shutdown(*a, **k)

            monkeypatch.setattr(live_provider, "shutdown", _slow_shutdown)
            done = _worker_event(monkeypatch)
            _patch_config(monkeypatch, enabled=False)
            self._elapse_window(monkeypatch)

            # Returns while the flush is still blocked — i.e. the caller was not
            # made to wait for it. The recheck is eventual in both directions now:
            # the noticing call schedules the worker and keeps serving the live
            # recorder until it lands, which is immaterial against the window the
            # recheck already sits behind.
            get_recorder()
            # Wait until the flush function has been entered on the worker.
            assert flush_entered.wait(timeout=10), "flush never started"
            assert seen.get("done") is not True
            # The worker is blocked in the flush; recorder should be disabled now.
            assert get_recorder().enabled is False
            assert seen.get("done") is not True  # still blocked, nobody waited
            released.set()
            assert done.wait(timeout=10), "consent worker never completed"
            assert seen["thread"] != caller
        finally:
            released.set()
            reset_for_testing()

    def test_unchanged_consent_does_not_rebuild(self, tmp_path, monkeypatch):
        # A rebuild tears down the exporter and its reader thread, so an idle
        # recheck that finds no change must leave the live recorder alone.
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(
            monkeypatch, enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
        try:
            first = get_recorder()
            provider_before = provider_mod._provider
            self._elapse_window(monkeypatch)
            assert get_recorder() is first
            assert provider_mod._provider is provider_before
        finally:
            reset_for_testing()

    def test_hot_path_does_not_read_config_every_call(self, monkeypatch):
        """Inside the window the recorder is returned without touching config."""
        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()  # builds, stamps the clock

            reads = {"n": 0}
            real_load = KiroCrewConfig.load

            def counting_load(cls=None):
                reads["n"] += 1
                return real_load()

            monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: counting_load()))
            for _ in range(50):
                get_recorder()
            assert reads["n"] == 0
        finally:
            reset_for_testing()

    def test_unreadable_config_keeps_the_live_recorder(self, tmp_path, monkeypatch):
        """Losing the ability to READ the setting is not consent being withdrawn."""
        reset_for_testing()
        _patch_config(
            monkeypatch, enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
        try:
            assert get_recorder().enabled is True

            def _boom(cls=None):
                raise OSError("config unreadable")

            monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _boom()))
            self._elapse_window(monkeypatch)

            assert get_recorder().enabled is True
        finally:
            reset_for_testing()

    def test_a_superseded_check_does_not_defer_the_next_one(self, tmp_path, monkeypatch):
        """A stale worker must not stamp the clock.

        If it does, and the replacement check was skipped because one was already
        in flight, no check runs for the current generation and the refreshed clock
        defers the next one by a full window — leaving the setting unapplied for up
        to `_CONSENT_RECHECK_SECS` even though the user already changed it.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()
            with provider_mod._lock:
                stale_generation = provider_mod._build_generation - 1
                before = provider_mod._consent_checked_at

            # Run a worker whose generation is already superseded.
            worker = threading.Thread(
                target=provider_mod._consent_worker, args=(stale_generation,), daemon=True
            )
            worker.start()
            worker.join(timeout=5)

            assert (
                provider_mod._consent_checked_at == before
            ), "a superseded check refreshed the recheck clock"
            assert provider_mod._check_in_flight is False, "the in-flight flag leaked"
        finally:
            reset_for_testing()

    def test_get_recorder_never_reads_config_on_the_calling_thread(self, monkeypatch):
        """The recheck's config read must happen on a worker, not the caller.

        `KiroCrewConfig.load()` is a fingerprint-cache hit in the steady state
        (~0.3ms) but a full read plus schema validation when the file actually
        changed (~14ms) — and that is exactly when the recheck fires. The
        route-latency middleware calls get_recorder() on the event loop for every
        request, so the read cannot happen there.
        """
        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()  # first build; stamps the clock
            caller = threading.get_ident()
            read_threads: list[int] = []
            read_done = threading.Event()
            real_load = KiroCrewConfig.load

            def _tracking_load(cls=None):
                read_threads.append(threading.get_ident())
                read_done.set()
                return real_load()

            monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _tracking_load()))
            done = _worker_event(monkeypatch)
            self._elapse_window(monkeypatch)
            get_recorder()
            assert done.wait(timeout=10), "the recheck never read config at all"
            assert read_threads, "no config reads recorded"
            assert (
                caller not in read_threads
            ), f"config was read on the calling thread ({caller}): {read_threads}"
        finally:
            reset_for_testing()

    def test_recheck_clock_is_stamped_after_a_failed_read(self, monkeypatch):
        """A failing read must not turn every later call into a fresh read."""
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()
            reads = {"n": 0}
            read_done = threading.Event()

            def _boom(cls=None):
                reads["n"] += 1
                read_done.set()
                raise OSError("config unreadable")

            monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _boom()))
            done = _worker_event(monkeypatch)
            self._elapse_window(monkeypatch)
            get_recorder()  # schedules the check; the worker does the failing read
            assert done.wait(timeout=10), "consent worker never completed"
            assert reads["n"] >= 1, "the recheck never read config"
            assert provider_mod._consent_checked_at > 0.0, "a failed read left the clock unstamped"
            for _ in range(20):
                get_recorder()
            # One read for the recheck that failed; the rest are inside the
            # freshly-stamped window.
            assert reads["n"] == 1
        finally:
            reset_for_testing()

    def test_the_fast_path_never_returns_none_during_a_concurrent_shutdown(
        self, tmp_path, monkeypatch
    ):
        """The guard spans a Python call, so the return must not re-read the global.

        The config route runs shutdown() on an asyncio.to_thread worker while requests
        keep calling get_recorder() on the loop. If the fast path re-read `_recorder`
        to return it, a shutdown landing between the guard and the return would hand
        back None from a `-> MetricsRecorder` signature.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(
            monkeypatch, enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
        try:
            get_recorder()

            # Null the globals from inside the guard's own call, which is the exact
            # interleaving a concurrent shutdown() produces.
            real_due = provider_mod._consent_recheck_due

            def _due_then_clear():
                verdict = real_due()
                provider_mod._recorder = None
                provider_mod._initialized = False
                return verdict

            monkeypatch.setattr(provider_mod, "_consent_recheck_due", _due_then_clear)
            rec = get_recorder()
            assert rec is not None, "fast path returned None mid-shutdown"
            assert isinstance(rec, MetricsRecorder)
        finally:
            monkeypatch.undo()
            reset_for_testing()

    def test_shutdown_applies_a_change_without_waiting_out_the_window(self, tmp_path, monkeypatch):
        """The dashboard's own write path: apply now, don't wait out the window.

        `shutdown()` drops the recorder so the change is picked up on the next
        metric rather than up to 30s later. The enable still builds off-thread —
        the config route calls this from an `asyncio.to_thread` worker, but the
        next `get_recorder()` is on the event loop.
        """
        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            assert get_recorder().enabled is False
            _patch_config(
                monkeypatch,
                enabled=True,
                local_dir=str(tmp_path),
                export_interval_seconds=3600,
            )
            done = _worker_event(monkeypatch)
            # No window elapsed — shutdown() is what makes it immediate.
            provider_shutdown()
            get_recorder()  # schedules the rebuild
            assert done.wait(timeout=10), "consent worker never completed"
            assert get_recorder().enabled is True
        finally:
            reset_for_testing()

    def test_shutdown_does_not_hold_the_lock_across_the_flush(self, tmp_path, monkeypatch):
        """A flush under `_lock` stalls the loop on lock ACQUISITION, not just on IO.

        The config route calls shutdown() via asyncio.to_thread, so the flush runs on
        a worker — but if it held `_lock`, the next get_recorder() on the event loop
        would block until the 30s reader join finished. Holding the lock is therefore
        the same defect as flushing inline, one level removed.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(
            monkeypatch, enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
        try:
            get_recorder()
            live_provider = provider_mod._provider
            assert live_provider is not None

            in_flush = threading.Event()
            release = threading.Event()

            _real_shutdown = live_provider.shutdown

            def _slow_shutdown(*a, **k):
                in_flush.set()
                release.wait(timeout=5)  # stands in for the reader join + final POST
                return _real_shutdown(*a, **k)

            monkeypatch.setattr(live_provider, "shutdown", _slow_shutdown)
            worker = threading.Thread(target=provider_shutdown, daemon=True)
            worker.start()
            assert in_flush.wait(timeout=5), "flush never started"

            # The flush is in progress. A concurrent caller must not be blocked by
            # it — acquire the lock from this thread with a short timeout.
            acquired = provider_mod._lock.acquire(timeout=2)
            if acquired:
                provider_mod._lock.release()
            assert acquired, "_lock is held across the flush; the event loop would stall"

            release.set()
            worker.join(timeout=5)
        finally:
            release.set()
            reset_for_testing()

    def test_env_pin_still_wins_after_a_config_edit(self, tmp_path, monkeypatch):
        """A pinned host must not start collecting because config.json changed."""
        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        monkeypatch.setenv("KIROCREW_TELEMETRY", "0")
        try:
            assert get_recorder().enabled is False
            fake = KiroCrewConfig(telemetry=TelemetryConfig(enabled=True, local_dir=str(tmp_path)))
            monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake))
            monkeypatch.setenv("KIROCREW_TELEMETRY", "0")
            self._elapse_window(monkeypatch)

            assert get_recorder().enabled is False
        finally:
            reset_for_testing()


class TestResetForTestingWaitsOutInFlightWorker:
    """Pins the fix for the force-clear this module's ``_worker_event`` helper
    used to do: ``reset_for_testing`` must wait for a REAL in-flight
    consent-check worker to finish, rather than a test helper force-clearing
    ``_check_in_flight`` out from under a worker that is still actually
    running. These tests drive the flag only through the product's own
    scheduling path (``get_recorder()`` -> ``_schedule_consent_check_locked``
    -> ``_consent_worker``) and never assign it directly.
    """

    def test_waits_for_a_real_worker_to_finish_before_returning(self, tmp_path, monkeypatch):
        """A worker started by an earlier flip must finish before
        ``reset_for_testing`` returns, even though nothing here calls
        ``get_recorder()`` again afterwards.

        The worker is a deliberately slow, event-gated fake build (never a
        wall-clock sleep) sitting inside the product's real
        ``_consent_worker``, so ``_check_in_flight`` is set and cleared by the
        product code path, not by this test. A hook on the wait loop's
        ``time.sleep`` call proves ``reset_for_testing`` actually entered its
        wait loop, so the ordering is established by that signal rather than
        a guessed delay.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()

            in_build = threading.Event()
            release = threading.Event()
            real_build = provider_mod._build_recorder

            def _slow_build():
                in_build.set()
                release.wait(timeout=10)
                return real_build()

            monkeypatch.setattr(provider_mod, "_build_recorder", _slow_build)
            _patch_config(
                monkeypatch,
                enabled=True,
                local_dir=str(tmp_path),
                export_interval_seconds=3600,
            )
            monkeypatch.setattr(provider_mod, "_consent_checked_at", 0.0)

            get_recorder()  # schedules the real worker; _check_in_flight -> True
            assert in_build.wait(timeout=10), "the worker never started its build"

            poll_entered = threading.Event()
            real_sleep = time.sleep

            def _tracking_sleep(seconds):
                poll_entered.set()
                real_sleep(seconds)

            monkeypatch.setattr(provider_mod.time, "sleep", _tracking_sleep)

            reset_done = threading.Event()
            reset_error = {}

            def _call_reset():
                try:
                    provider_mod.reset_for_testing()
                except Exception as exc:  # pragma: no cover - asserted below
                    reset_error["error"] = exc
                finally:
                    reset_done.set()

            thread = threading.Thread(target=_call_reset, daemon=True)
            thread.start()
            try:
                assert poll_entered.wait(timeout=5), "the wait loop never polled"
                # The real worker is still blocked in its build, so it has not
                # reached its finally yet and _check_in_flight is still True.
                assert (
                    not reset_done.is_set()
                ), "reset_for_testing returned while the worker was still in flight"

                release.set()  # let the worker finish; its own finally clears the flag

                assert reset_done.wait(timeout=10), "reset_for_testing never returned"
                assert "error" not in reset_error, reset_error.get("error")
            finally:
                release.set()
                thread.join(timeout=5)
        finally:
            reset_for_testing()

    def test_raises_when_the_bound_expires(self, tmp_path, monkeypatch):
        """A worker that never clears the flag must fail loudly, not silently.

        A silent timeout would reintroduce exactly the cross-test leak class
        this wait exists to prevent: a test would proceed believing it has a
        clean state while a stale worker can still mutate module globals.
        """
        import kiro_crew.metrics.provider as provider_mod

        reset_for_testing()
        _patch_config(monkeypatch, enabled=False)
        try:
            get_recorder()

            stuck_in_build = threading.Event()
            release = threading.Event()
            real_build = provider_mod._build_recorder

            def _stuck_build():
                stuck_in_build.set()
                release.wait(timeout=10)
                return real_build()

            monkeypatch.setattr(provider_mod, "_build_recorder", _stuck_build)
            _patch_config(
                monkeypatch,
                enabled=True,
                local_dir=str(tmp_path),
                export_interval_seconds=3600,
            )
            monkeypatch.setattr(provider_mod, "_consent_checked_at", 0.0)

            get_recorder()  # schedules the real worker; _check_in_flight -> True
            assert stuck_in_build.wait(timeout=10), "the worker never started its build"

            monkeypatch.setattr(provider_mod, "_RESET_WAIT_BOUND_SECS", 0.05)
            monkeypatch.setattr(provider_mod, "_RESET_WAIT_POLL_SECS", 0.01)
            with pytest.raises(RuntimeError, match="in flight"):
                provider_mod.reset_for_testing()
        finally:
            release.set()
            # Let the real worker actually finish (its own finally clears the
            # flag) so the next test's reset_for_testing() has nothing to wait
            # out.
            assert _wait_for(lambda: provider_mod._check_in_flight is False, timeout=10)
            reset_for_testing()
