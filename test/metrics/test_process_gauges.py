"""Tests for the kirocrew.process.* observable instrument contract.

Three layers, mirroring the module's structure:
  * raw readers — real values on this platform, None (never a raise) when the
    underlying /proc surface is missing;
  * OTEL registration — a real in-memory pipeline collects every expected
    metric name with plausible values, and a raising reader produces a gap,
    not an exporter failure;
  * provider wiring — the live build path registers the gauges, and a
    registration blow-up does not take telemetry down with it.
"""

import gc
import os
import threading
from unittest.mock import patch

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kiro_crew.metrics import process_gauges as pg
from kiro_crew.metrics.schema import validate_name

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _collect(register=pg.register_process_gauges):
    """Build a real SDK pipeline, register, force one collection cycle."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register(provider.get_meter("test"))
    data = reader.get_metrics_data()
    provider.shutdown()
    out: dict[str, list] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = list(metric.data.data_points)
    return out


def _collect_data(register=pg.register_process_gauges):
    """Same as :func:`_collect`, but keeps each metric's DATA object.

    The data object is what carries the instrument kind: a ``Sum`` has
    ``aggregation_temporality``, a ``Gauge`` has none. Asserting on it is how the
    "these are gauges, not counters" contract is pinned at the SDK level rather
    than by reading the registration source.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register(provider.get_meter("test"))
    data = reader.get_metrics_data()
    provider.shutdown()
    out: dict[str, object] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = metric.data
    return out


# ---------------------------------------------------------------------------
# raw readers
# ---------------------------------------------------------------------------


def test_python_thread_count_positive():
    assert pg.read_python_threads() >= 1


def test_os_thread_count_matches_proc_when_available():
    count = pg.read_os_threads()
    if not os.path.isdir("/proc/self/task"):
        assert count is None
        return
    assert count is not None
    # Python threads are a subset of OS threads.
    assert count >= threading.active_count()


def test_open_fds_delegates_to_the_shared_probe():
    # The probe's behavior (fd-dir correction, Windows handle count) is pinned
    # in test_platform_compat_coverage.py; here only the delegation matters.
    with patch.object(pg.platform_compat, "count_open_fds", return_value=17):
        assert pg.read_open_fds() == 17


def test_readers_return_none_not_raise_when_proc_missing():
    with patch.object(pg.platform_compat, "process_thread_count", return_value=None):
        assert pg.read_os_threads() is None
    with patch.object(pg.platform_compat, "count_open_fds", return_value=None):
        assert pg.read_open_fds() is None


# ---------------------------------------------------------------------------
# OTEL registration + collection
# ---------------------------------------------------------------------------


def test_all_names_pass_core_namespace_validation():
    for name in pg.ALL_METRIC_NAMES:
        assert validate_name(name) == name


def test_collection_yields_expected_metrics():
    metrics = _collect()
    # Platform-independent instruments must always observe.
    for name in (
        pg.GAUGE_THREADS_PYTHON,
        pg.GAUGE_RSS,
        pg.GAUGE_CPU_SECONDS,
        pg.GAUGE_GC_COLLECTIONS,
        pg.GAUGE_GC_COLLECTED,
        pg.GAUGE_GC_UNCOLLECTABLE,
    ):
        assert name in metrics, f"{name} missing from collection"
        assert metrics[name], f"{name} produced no data points"

    # Both RSS gauges publish on every platform — platform_compat provides the
    # current/peak pair cross-platform (statm / Mach / WorkingSetSize; ru_maxrss
    # / PeakWorkingSetSize).
    assert metrics[pg.GAUGE_PEAK_RSS], "peak RSS gauge missing"

    assert metrics[pg.GAUGE_THREADS_PYTHON][0].value >= 1
    assert metrics[pg.GAUGE_RSS][0].value > 1 << 20
    assert metrics[pg.GAUGE_CPU_SECONDS][0].value > 0

    # GC counters carry the generation attribute for all three generations.
    gens = {dp.attributes["generation"] for dp in metrics[pg.GAUGE_GC_COLLECTIONS]}
    assert gens == {"0", "1", "2"}


def test_every_instrument_is_a_gauge_not_a_sum():
    """No instrument here may be a Sum — that is what a cumulative series is.

    The process CPU and GC readings were observable COUNTERS, which made them the
    only cumulative series Kiro Crew exported, and one cumulative series forces
    every consumer into stateful whole-hour aggregation (its hourly increment is
    last-minus-first across the whole hour, per host and per process lifetime).
    The DELTA route is not the alternative: an observable callback reads an
    external lifetime total, so the first collection after a provider rebuild
    would re-emit the entire total as one giant delta.

    Asserted off the collected DATA rather than by spying on the meter, so it
    holds against the SDK's own classification.
    """
    from opentelemetry.sdk.metrics.export import Gauge

    for name, data in _collect_data().items():
        assert isinstance(data, Gauge), f"{name} collected as {type(data).__name__}, not Gauge"
        assert not hasattr(
            data, "aggregation_temporality"
        ), f"{name} carries a temporality, so it is an accumulating instrument"


def test_lifetime_totals_are_declared_for_the_consumer_that_must_know():
    """A lifetime-total gauge is not interchangeable with a state gauge.

    Its newest sample is "since this process started", so a consumer that reports
    the newest sample as a reading shows a number that only grows. The dashboard
    aggregator differences them instead, and it finds them through this tuple —
    which must therefore name exactly the four whose reading accumulates, and
    must stay a subset of the roster.
    """
    assert set(pg.LIFETIME_TOTAL_METRICS) == {
        pg.GAUGE_CPU_SECONDS,
        pg.GAUGE_GC_COLLECTIONS,
        pg.GAUGE_GC_COLLECTED,
        pg.GAUGE_GC_UNCOLLECTABLE,
    }
    assert set(pg.LIFETIME_TOTAL_METRICS) <= set(pg.ALL_METRIC_NAMES)


def test_collection_includes_os_views_on_linux():
    if not os.path.isdir("/proc/self/task"):
        pytest.skip("Linux-only surface")
    metrics = _collect()
    assert metrics[pg.GAUGE_THREADS_OS][0].value >= metrics[pg.GAUGE_THREADS_PYTHON][0].value
    assert metrics[pg.GAUGE_OPEN_FDS][0].value >= 3


def test_peak_rss_at_least_current_rss():
    """The high-water mark can never sit below the live reading it bounds.

    The two readings come from different kernel accounting sources on Linux
    (``/proc/self/statm`` resident pages vs ``getrusage`` ``ru_maxrss``), and
    the kernel folds per-thread RSS deltas into the high-water mark lazily —
    a freshly grown process can read current a few MB above peak. Force a
    transient spike that dwarfs that lag, release it, and the invariant must
    hold: the spike lives on in the high-water mark while the live reading
    has already fallen back.
    """
    spike = bytearray(32 * 1024 * 1024)
    for i in range(0, len(spike), 4096):  # touch every page so it is resident
        spike[i] = 1
    del spike
    gc.collect()
    metrics = _collect()
    (cur,) = metrics[pg.GAUGE_RSS]
    (peak,) = metrics[pg.GAUGE_PEAK_RSS]
    assert peak.value >= cur.value


def test_raising_reader_yields_gap_not_failure():
    """A blown-up probe skips its observation; every other metric survives."""
    with patch.object(pg, "read_os_threads", side_effect=RuntimeError("boom")):
        metrics = _collect()
    assert pg.GAUGE_THREADS_OS not in metrics or not metrics[pg.GAUGE_THREADS_OS]
    assert metrics[pg.GAUGE_THREADS_PYTHON][0].value >= 1


def test_unavailable_reader_yields_no_observation():
    with patch.object(pg, "read_os_threads", return_value=None):
        metrics = _collect()
    assert pg.GAUGE_THREADS_OS not in metrics or not metrics[pg.GAUGE_THREADS_OS]


def test_registration_failure_is_swallowed():
    """register_process_gauges never raises, even on a hostile meter."""

    class _HostileMeter:
        def __getattr__(self, name):
            raise RuntimeError("no instruments for you")

    pg.register_process_gauges(_HostileMeter())  # must not raise


def test_observable_counters_survive_provider_rebuild(tmp_path):
    """Observable counters export CUMULATIVE and rebuilds cannot double-count.

    DELTA export was tried and reverted: the delta baseline lives in the
    provider, and telemetry consent changes rebuild the provider in-process —
    the first post-rebuild collection would re-emit the process-lifetime total
    as one giant delta. With CUMULATIVE export each cycle re-emits the running
    snapshot, and the aggregator reduces the (PID, process-identity, attrs)
    stream time-ordered and window-relative — both providers run in THIS
    process, so the exporter stamps the same module-cached identity on every
    record and the segments stitch into one stream (on a platform without a
    start-time read the records carry no identity and the value heuristic
    stitches them identically). Two providers writing to the same shard (an
    off/on toggle) still aggregate to the in-window activity: never a doubled
    250, never the four export cycles summed (500), and never the raw lifetime
    snapshot — the stream's first sample (100) is the baseline, so the growth
    to 150 reports as 50.
    """
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    from kiro_crew.dashboard.handlers.telemetry import _aggregate
    from kiro_crew.metrics.local_exporter import JsonlMetricExporter

    def run_provider(value: float) -> None:
        exporter = JsonlMetricExporter(tmp_path)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10_000_000)
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("cumulative-test")
        meter.create_observable_counter(
            "kirocrew.process.cpu.seconds",
            callbacks=[lambda options: iter([Observation(value)])],
            unit="s",
        )
        try:
            reader.collect()
            reader.collect()
        finally:
            # A failed collect must not leak the exporter thread into later
            # tests.
            provider.shutdown()

    # First provider lifetime: cumulative reaches 100. Rebuild (telemetry
    # off/on): a fresh provider re-observes the same OS-level cumulative,
    # which keeps growing to 150. Same PID, same shard.
    run_provider(100.0)
    run_provider(150.0)

    result = _aggregate(sorted(tmp_path.glob("metrics-*.jsonl")))
    rows = [o for o in result["other"] if o["name"] == "kirocrew.process.cpu.seconds"]
    assert rows, "cpu.seconds missing from aggregation"
    # Window-relative per (PID, attrs) stream: baseline 100, max 150 → 50 of
    # in-window activity — never 100+150 summed across rebuilds, never
    # inflated by the four export cycles (100+100+150+150 = 500), and never
    # the raw lifetime snapshot (150) presented as window activity.
    assert rows[0]["total"] == 50.0, f"got {rows[0]['total']}"


# ---------------------------------------------------------------------------
# provider wiring
# ---------------------------------------------------------------------------


def _enable_telemetry(monkeypatch):
    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")


def test_live_build_registers_process_gauges(monkeypatch):
    from kiro_crew.metrics import provider as provider_mod

    _enable_telemetry(monkeypatch)
    provider_mod.reset_for_testing()
    try:
        with patch("kiro_crew.metrics.process_gauges.register_process_gauges") as register:
            rec = provider_mod.get_recorder()
            assert rec.enabled
            register.assert_called_once()
    finally:
        provider_mod.reset_for_testing()


def test_gauge_registration_failure_keeps_telemetry_alive(monkeypatch):
    from kiro_crew.metrics import provider as provider_mod

    _enable_telemetry(monkeypatch)
    provider_mod.reset_for_testing()
    try:
        with patch(
            "kiro_crew.metrics.process_gauges.register_process_gauges",
            side_effect=RuntimeError("boom"),
        ):
            rec = provider_mod.get_recorder()
            assert rec.enabled, "gauge failure must not disable telemetry"
    finally:
        provider_mod.reset_for_testing()
