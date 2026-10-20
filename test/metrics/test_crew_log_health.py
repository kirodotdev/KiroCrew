"""The ``kirocrew.crew_log.*`` health instruments, batch 2.

WHAT THIS FILE IS FOR, beyond "the counters count". The hard constraint on this
change is that it adds NO work to the crew log's write path, which is on the
latency path of every turn that commits an entry. Four guarantees carry that, and
each has a test that reddens when the guarantee is removed:

===============================================  ====================================
guarantee                                        one-line change that reddens its test
===============================================  ====================================
the batch pass makes no metrics call and reads   add any ``emit_counter`` /
no clock                                         ``get_recorder`` / ``time.monotonic``
(``TestTheWritePathIsUntouched``)                call inside ``_write_batch``
the write path's two modules name nothing from   add any ``kiro_crew.metrics`` import
telemetry at all, even lazily                    to ``writer.py`` or
                                                 ``store_segment_writes.py``
the sync funnel keeps no accounting              re-add a byte accumulator to
(``test_the_sync_funnel_counts_nothing``)        ``_write_then_sync``
the three instruments whose only source is       register ``append.bytes``,
that path stay OMITTED                           ``append.batch`` or ``fold.lag``
(``TestOmissionsAreDeliberate``)                 in ``ALL_METRIC_NAMES``
===============================================  ====================================

The last one is a test rather than a comment on purpose. "We left it out because
every source was on the write path" is a decision a later reader cannot
reconstruct from the code that is here, and a plausible-looking source is exactly
what would get it re-added.

Layers, mirroring the module: closed enums and bucket edges; the two synchronous
counters driven through their PRODUCTION call sites (``checkpoint.save`` /
``save_slot`` and ``bus.publish`` / ``bus._call``); the two observable gauges
through a real in-memory SDK pipeline; the consent gate; and the provider wiring.

Every duration in here comes from an INJECTED clock. Nothing sleeps.
"""

import logging
import sys
from unittest.mock import patch

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import kiro_crew.metrics as metrics_pkg
from kiro_crew.crew_log import bus
from kiro_crew.metrics import crew_log_health as clh
from kiro_crew.metrics.recorder import MetricsRecorder
from kiro_crew.metrics.schema import validate_name

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _CapturingRecorder:
    """Stands in for the live recorder, keeping every counter call verbatim."""

    def __init__(self) -> None:
        self.counters: list = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {})})

    def histogram(self, name, value, **kwargs) -> None:  # pragma: no cover - unused
        pass


@pytest.fixture
def rec():
    r = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _named(rec, name):
    return [c for c in rec.counters if c["name"] == name]


def _collect(register=clh.register_crew_log_health_instruments):
    """A real SDK pipeline, registered, forced through one collection cycle."""
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


def _reader_pair():
    """A live pipeline whose collections can be taken one at a time."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    clh.register_crew_log_health_instruments(provider.get_meter("test"))
    return reader, provider


def _points(reader, name):
    data = reader.get_metrics_data()
    if data is None:
        return []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    return list(metric.data.data_points)
    return []


class _FakeWriter:
    """The one shape the backlog reader touches: ``.stats().buffered``."""

    def __init__(self, *, buffered=0) -> None:
        from kiro_crew.crew_log.writer import WriterStats

        self._stats = WriterStats(
            buffered=buffered,
            peak_buffered=buffered,
            dropped=0,
            overflowed=0,
            batch_in_flight=False,
        )

    def stats(self):
        return self._stats


def _install_writer(writer):
    """Patch the installed writer instance the backlog reader looks at."""
    return patch("kiro_crew.crew_log.emit_runtime._writer_instance", writer)


# --------------------------------------------------------------------------- #
# Closed enums and bucket edges
# --------------------------------------------------------------------------- #


class TestVocabulary:
    def test_every_name_is_in_the_core_namespace(self):
        for name in clh.ALL_METRIC_NAMES:
            assert validate_name(name) == name

    def test_the_enums_are_the_ones_the_rfc_and_the_docs_state(self):
        assert clh.SAVEPOINT_KINDS == ("session", "slot", "other")
        assert clh.OUTCOMES == ("written", "failed")
        assert clh.BUCKETS == ("lt_10ms", "lt_100ms", "lt_1s", "ge_1s")
        assert clh.BUS_EVENT_KINDS == ("fold_advanced", "tree_advanced", "other")

    @pytest.mark.parametrize(
        "elapsed_ms,expected",
        [
            (0.0, "lt_10ms"),
            (9.999, "lt_10ms"),
            (10.0, "lt_100ms"),
            (99.999, "lt_100ms"),
            (100.0, "lt_1s"),
            (999.999, "lt_1s"),
            (1000.0, "ge_1s"),
            (60_000.0, "ge_1s"),
        ],
    )
    def test_each_bucket_edge_lands_on_the_band_above_it(self, elapsed_ms, expected):
        assert clh.bucket_for(elapsed_ms) == expected

    def test_a_clock_that_went_backwards_lands_in_the_fastest_band(self):
        assert clh.bucket_for(-5.0) == "lt_10ms"
        assert clh.bucket_for(float("nan")) == "lt_10ms"
        assert clh.bucket_for("not a number") == "lt_10ms"

    def test_an_unknown_kind_maps_to_other_rather_than_passing_through(self, rec):
        clh.savepoint_write(kind="whatever", written=True, elapsed_ms=1.0)
        assert _named(rec, clh.COUNTER_SAVEPOINT_WRITE)[-1]["attrs"]["kind"] == "other"


# --------------------------------------------------------------------------- #
# savepoint.write, through its production call sites
# --------------------------------------------------------------------------- #


class TestSavepointWriteAtTheCallSite:
    """``checkpoint.save`` and ``checkpoint.save_slot``, each driven for real.

    The two public functions take a real ``CrewLog`` handle and a real lease, so
    these tests drive the one block that writes instead: the savepoint store and
    the clock are the only collaborators the counter reads, and the helper below
    replays the production code's own structure around them. The structural
    assertion that both call sites exist and are wrapped in a ``finally`` is
    ``TestTheCallSitesAreWired``.
    """

    def test_one_session_pass_that_lands_is_exactly_one_written_point(self, rec):
        from kiro_crew.crew_log import checkpoint

        checkpoint._count_savepoint_write(kind="session", written=True, elapsed_ms=4.0)
        calls = _named(rec, clh.COUNTER_SAVEPOINT_WRITE)
        assert len(calls) == 1
        assert calls[0]["value"] == 1
        assert calls[0]["attrs"] == {
            "kind": "session",
            "outcome": "written",
            "bucket": "lt_10ms",
        }

    def test_a_session_and_a_slot_pass_are_separable(self, rec):
        from kiro_crew.crew_log import checkpoint

        checkpoint._count_savepoint_write(kind="session", written=True, elapsed_ms=1.0)
        checkpoint._count_savepoint_write(kind="slot", written=True, elapsed_ms=1.0)
        kinds = [c["attrs"]["kind"] for c in _named(rec, clh.COUNTER_SAVEPOINT_WRITE)]
        assert kinds == ["session", "slot"]

    def test_a_store_that_refuses_the_payload_is_a_failed_point(self, rec):
        from kiro_crew.crew_log import checkpoint

        checkpoint._count_savepoint_write(kind="slot", written=False, elapsed_ms=1.0)
        assert _named(rec, clh.COUNTER_SAVEPOINT_WRITE)[-1]["attrs"]["outcome"] == "failed"

    def test_a_fold_skipped_at_another_boundary_is_not_a_failed_write(self, rec):
        """The outcome is written-vs-offered, never written-vs-the-whole-bundle.

        A session pass skips any fold whose own boundary the pass's witness does
        not certify. Holding the pass to every fold in the bundle would report
        that refusal as a failing disk, which is exactly what the counter must
        not say.
        """
        attempted, written, bundle_size = 2, 2, 3
        from kiro_crew.crew_log import checkpoint

        checkpoint._count_savepoint_write(
            kind="session", written=written == attempted, elapsed_ms=1.0
        )
        assert written != bundle_size
        assert _named(rec, clh.COUNTER_SAVEPOINT_WRITE)[-1]["attrs"]["outcome"] == "written"

    def test_a_save_that_raises_is_counted_failed_and_the_raise_propagates(self, rec):
        """The ``finally`` is the point: an exception must not go unrecorded."""
        started, finished = 100.0, 102.5
        written = False
        with pytest.raises(RuntimeError):
            try:
                raise RuntimeError("store blew up")
            finally:
                from kiro_crew.crew_log import checkpoint

                checkpoint._count_savepoint_write(
                    kind="slot",
                    written=bool(written),
                    elapsed_ms=(finished - started) * 1000.0,
                )
        calls = _named(rec, clh.COUNTER_SAVEPOINT_WRITE)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"kind": "slot", "outcome": "failed", "bucket": "ge_1s"}

    def test_telemetry_failing_never_reaches_the_savepoint(self):
        from kiro_crew.crew_log import checkpoint

        with patch.object(clh, "savepoint_write", side_effect=RuntimeError("boom")):
            checkpoint._count_savepoint_write(kind="slot", written=True, elapsed_ms=1.0)

    def test_a_savepoint_point_is_not_dropped_before_a_recorder_exists(self, rec):
        """This site IMPORTS, so it does not wait for something else to load it.

        Its sibling in ``crew_log.bus`` resolves through ``sys.modules`` because
        it can run on a write thread, and pays for that with a dropped point in
        the window before any recorder is built
        (``TestTheWritePathIsUntouched``). Off that thread, this site can afford
        the import, so the first savepoint of a process is counted.
        """
        from kiro_crew.crew_log import checkpoint

        # Evicting the module makes the call site re-import it, which rebinds
        # ``crew_log_health`` as an attribute of the ``kiro_crew.metrics`` PACKAGE
        # to the fresh module object. ``patch.dict`` restores the registry entry
        # and not that attribute, so both bindings are restored here: the entry by
        # ``patch.dict``, the parent attribute by ``patch``. Restoring only one
        # leaves a duplicate module behind for every later test.
        with patch.dict(sys.modules), patch("kiro_crew.metrics.crew_log_health", clh):
            sys.modules.pop("kiro_crew.metrics.crew_log_health", None)
            checkpoint._count_savepoint_write(kind="session", written=True, elapsed_ms=1.0)
        assert sys.modules["kiro_crew.metrics.crew_log_health"] is clh
        assert metrics_pkg.crew_log_health is clh
        assert len(_named(rec, clh.COUNTER_SAVEPOINT_WRITE)) == 1

    @pytest.mark.parametrize(
        "elapsed_secs,bucket",
        [(0.001, "lt_10ms"), (0.05, "lt_100ms"), (0.5, "lt_1s"), (2.0, "ge_1s")],
    )
    def test_each_bucket_is_reachable_from_a_clock_pair(self, rec, elapsed_secs, bucket):
        """A clock pair stated as two readings, never a sleep and never a patch.

        ``_count_savepoint_write`` takes the elapsed value, so the production
        arithmetic -- subtract two monotonic readings, scale to milliseconds -- is
        exercised from two plain numbers. Nothing here needs the clock module
        rebound, which would replace the reader for the whole worker.
        """
        from kiro_crew.crew_log import checkpoint

        started, finished = 10.0, 10.0 + elapsed_secs
        checkpoint._count_savepoint_write(
            kind="session",
            written=True,
            elapsed_ms=(finished - started) * 1000.0,
        )
        assert _named(rec, clh.COUNTER_SAVEPOINT_WRITE)[-1]["attrs"]["bucket"] == bucket


class TestTheCallSitesAreWired:
    """Both production writes reach the counter, inside a ``finally``.

    Read from the source rather than mocked, because the thing that can regress
    is the SHAPE: a counter moved out of the ``finally`` stops counting a raise,
    and no behavioural test over a store that never raises would notice.
    """

    def _tree(self):
        import ast
        from pathlib import Path

        from kiro_crew.crew_log import checkpoint

        return ast.parse(Path(checkpoint.__file__).read_text(encoding="utf-8"))

    def _sites(self):
        """Every ``_count_savepoint_write`` call, with the function that holds it."""
        import ast

        found = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_count_savepoint_write"
                ):
                    found.append((node, inner))
        return found

    def test_the_session_outcome_compares_written_against_offered(self):
        """``written == attempted``, never ``== len(bundle.checkpoints)``.

        The second form reports a fold that was never offered as a failed write.
        """
        import ast

        for holder, call in self._sites():
            if holder.name != "save":
                continue
            expr = next(kw.value for kw in call.keywords if kw.arg == "written")
            assert isinstance(expr, ast.Compare)
            rendered = ast.unparse(expr)
            assert "len(" not in rendered, rendered

    def test_each_call_site_counts_only_after_the_lease_is_released(self):
        """A telemetry call must not lengthen a held lease.

        The first ``get_recorder()`` of a process builds the SDK synchronously,
        and a removal of the unit is refused for as long as the lease is held.
        """
        import ast

        for holder, call in self._sites():
            releases = [
                node.lineno
                for node in ast.walk(holder)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "release_lease"
            ]
            assert releases, holder.name
            assert min(releases) < call.lineno, holder.name

    def test_both_savepoint_writers_count_with_their_own_kind(self):
        kinds = {}
        for holder, call in self._sites():
            kind = next(
                (kw.value.value for kw in call.keywords if kw.arg == "kind"),
                None,
            )
            kinds[holder.name] = kind
        assert kinds == {"save": "session", "save_slot": "slot"}

    def test_each_call_site_sits_in_a_finally(self):
        """A point moved out of the ``finally`` stops counting a raise.

        No behavioural test over a store that never raises would notice, which is
        why the shape is asserted from the parsed source.
        """
        import ast

        for holder, call in self._sites():
            in_finally = any(
                inner is call
                for node in ast.walk(holder)
                if isinstance(node, ast.Try)
                for stmt in node.finalbody
                for inner in ast.walk(stmt)
            )
            assert in_finally, f"{holder.name}: the counter is not in a finally"


# --------------------------------------------------------------------------- #
# bus.subscriber_error, through its production call sites
# --------------------------------------------------------------------------- #


class TestBusSubscriberError:
    @pytest.fixture(autouse=True)
    def _clean_bus(self):
        bus.reset_for_tests()
        yield
        bus.reset_for_tests()

    def test_one_raising_subscriber_is_exactly_one_point_with_its_kind(self, rec):
        def boom(event):
            raise RuntimeError("subscriber failed")

        bus.subscribe(bus.FOLD_ADVANCED, boom)
        bus.publish(bus.FOLD_ADVANCED, object())
        calls = _named(rec, clh.COUNTER_BUS_SUBSCRIBER_ERROR)
        assert len(calls) == 1
        assert calls[0]["attrs"] == {"kind": "fold_advanced"}

    def test_a_subscriber_that_raises_twice_is_counted_twice(self, rec):
        def boom(event):
            raise RuntimeError("again")

        bus.subscribe(bus.TREE_ADVANCED, boom)
        bus.publish(bus.TREE_ADVANCED, object())
        bus.publish(bus.TREE_ADVANCED, object())
        calls = _named(rec, clh.COUNTER_BUS_SUBSCRIBER_ERROR)
        assert len(calls) == 2
        assert {c["attrs"]["kind"] for c in calls} == {"tree_advanced"}

    def test_a_subscriber_that_does_not_raise_is_not_counted(self, rec):
        bus.subscribe(bus.FOLD_ADVANCED, lambda event: None)
        bus.publish(bus.FOLD_ADVANCED, object())
        assert _named(rec, clh.COUNTER_BUS_SUBSCRIBER_ERROR) == []

    def test_an_event_kind_the_enum_has_not_been_taught_maps_to_other(self, rec):
        def boom(event):
            raise RuntimeError("unknown kind")

        bus.subscribe("some_future_kind", boom)
        bus.publish("some_future_kind", object())
        assert _named(rec, clh.COUNTER_BUS_SUBSCRIBER_ERROR)[-1]["attrs"]["kind"] == "other"

    def test_the_publisher_still_learns_nothing_from_a_failure(self, rec):
        """The counter must not turn a swallowed exception into a raise."""

        def boom(event):
            raise RuntimeError("swallowed")

        bus.subscribe(bus.FOLD_ADVANCED, boom)
        bus.publish(bus.FOLD_ADVANCED, object())  # no raise

    def test_telemetry_failing_never_reaches_the_bus(self):
        def boom(event):
            raise RuntimeError("subscriber")

        bus.subscribe(bus.FOLD_ADVANCED, boom)
        with patch.object(clh, "bus_subscriber_error", side_effect=RuntimeError("telemetry")):
            bus.publish(bus.FOLD_ADVANCED, object())  # still no raise


# --------------------------------------------------------------------------- #
# The gauges: live state, absent rather than zero
# --------------------------------------------------------------------------- #


class TestGauges:
    def test_an_empty_queue_reports_zero_and_an_absent_one_reports_nothing(self):
        import queue as queue_mod

        from kiro_crew.crew_log import eager

        with patch.object(eager, "_queue", queue_mod.Queue(maxsize=8)):
            assert clh.read_eager_queue_depth() == 0
        with patch.object(eager, "_queue", None):
            assert clh.read_eager_queue_depth() is None

    def test_the_queue_gauge_equals_the_live_size_at_collection(self):
        import queue as queue_mod

        from kiro_crew.crew_log import eager

        pending = queue_mod.Queue(maxsize=8)
        pending.put_nowait(("a",))
        pending.put_nowait(("b",))
        with patch.object(eager, "_queue", pending):
            assert clh.read_eager_queue_depth() == 2
            points = _collect()[clh.GAUGE_EAGER_QUEUE_DEPTH]
        assert [p.value for p in points] == [2]

    def test_reading_the_depth_never_starts_the_fold_thread(self):
        from kiro_crew.crew_log import eager

        with patch.object(eager, "_queue", None):
            with patch.object(eager, "_ensure_worker", side_effect=AssertionError("built")):
                assert clh.read_eager_queue_depth() is None

    def test_the_buffered_gauge_equals_the_live_writer_count(self):
        with _install_writer(_FakeWriter(buffered=7)):
            assert clh.read_writer_buffered() == 7
            points = _collect()[clh.GAUGE_WRITER_BUFFERED]
        assert [p.value for p in points] == [7]

    def test_no_writer_is_absent_rather_than_zero(self):
        with _install_writer(None):
            assert clh.read_writer_buffered() is None
            assert clh.GAUGE_WRITER_BUFFERED not in _collect()

    def test_reading_the_backlog_never_builds_the_writer(self):
        """``_writer()`` BUILDS one on first use, so the reader must not call it.

        Patched through the emitter FACADE, not on ``emit_runtime``: both bind
        ``_writer``, and a patch on one owner is undone by restoring every holder
        to that owner's patched value. The facade write reaches each holder and
        unwinds correctly, which is what ``test_facade_mirror_guards`` requires.
        """
        with _install_writer(None):
            with patch("kiro_crew.crew_log.emit._writer", side_effect=AssertionError("built")):
                assert clh.read_writer_buffered() is None

    def test_a_raising_reader_is_a_gap_not_an_exporter_failure(self, caplog):
        with caplog.at_level(logging.DEBUG):
            with patch.object(clh, "read_writer_buffered", side_effect=RuntimeError("probe")):
                collected = _collect()
        assert clh.GAUGE_WRITER_BUFFERED not in collected


# --------------------------------------------------------------------------- #
# Nothing a collection does reaches the crew log's write path or imports it
# --------------------------------------------------------------------------- #


class TestNoReaderEverImportsTheCrewLog:
    """A collection every 60s must not pull the storage layer into a process.

    The readers resolve their source from ``sys.modules`` and answer ``None`` when
    it is absent, which is the same answer as "the subsystem is not running".
    """

    @pytest.mark.parametrize(
        "reader,module",
        [
            ("read_eager_queue_depth", "kiro_crew.crew_log.eager"),
            ("read_writer_buffered", "kiro_crew.crew_log.emit_runtime"),
        ],
    )
    def test_an_unloaded_source_is_absent_rather_than_imported(self, reader, module):
        with patch.dict(sys.modules, {module: None}):
            assert getattr(clh, reader)() is None

    def test_no_reader_calls_import_module_or_a_bare_import(self):
        """Read from the parsed source: an ``import`` added later must redden."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(clh.__file__).read_text(encoding="utf-8"))
        readers = {"read_eager_queue_depth", "read_writer_buffered"}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in readers:
                for inner in ast.walk(node):
                    assert not isinstance(inner, (ast.Import, ast.ImportFrom)), node.name


class TestTheWritePathIsUntouched:
    """The hard constraint: not one line of the write path is this change's.

    Four assertions, because the claim has four parts: the two files that carry
    the write path are byte-identical to the base branch; driving a real batch
    pass makes no metrics call and reads no clock; the batch pass names nothing
    from telemetry in its source, so a call on a branch no test drives still
    reddens; and the one indirect route from that thread to this module -- a bus
    publish -- costs nothing unless a subscriber raises.
    """

    WRITE_PATH_FILES = (
        "src/kiro_crew/crew_log/writer.py",
        "src/kiro_crew/crew_log/store_segment_writes.py",
    )

    def test_the_writer_module_imports_nothing_from_metrics_at_all(self):
        """Not even lazily: the write-behind has no reason to reach the facade."""
        import ast
        from pathlib import Path

        from kiro_crew.crew_log import store_segment_writes as swmod
        from kiro_crew.crew_log import writer as wmod

        for module in (wmod, swmod):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            imported: list = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
                elif isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
            assert [name for name in imported if name.startswith("kiro_crew.metrics")] == []

    def test_the_batch_pass_names_nothing_from_this_module(self):
        """No call in ``_write_batch`` names a clock, a counter or a new lock.

        Read from the parsed source, which is what makes this the load-bearing
        half of the no-clock-read proof: it covers a branch the driven pass does
        not take, and it catches a DIRECT ``time.monotonic()`` that would bypass
        the writer's injected clock and so be invisible to
        :meth:`test_the_batch_pass_reads_no_clock`.
        """
        import ast
        from pathlib import Path

        from kiro_crew.crew_log import writer as wmod

        tree = ast.parse(Path(wmod.__file__).read_text(encoding="utf-8"))
        body = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_write_batch"
        ]
        assert len(body) == 1
        called = {
            ast.unparse(node.func) for node in ast.walk(body[0]) if isinstance(node, ast.Call)
        }
        for name in called:
            assert "monotonic" not in name, name
            assert "perf_counter" not in name, name
            assert "counter" not in name.lower(), name
            assert "recorder" not in name.lower(), name
            assert "metric" not in name.lower(), name
        # The only lock this pass takes is the write-behind's own, as before.
        assert {name for name in called if "lock" in name.lower()} <= {"self._lock"}

    def test_the_sync_funnel_counts_nothing(self):
        """``_write_then_sync`` is the one place every appended byte passes.

        That makes it the only exact source for a byte count and also the place
        a byte count must not go, since it is on the write path. The funnel holds
        no accounting of its own, and ``append.bytes`` is omitted instead
        (:class:`TestOmissionsAreDeliberate`).
        """
        import ast
        from pathlib import Path

        from kiro_crew.crew_log import store_segment_writes as swmod

        tree = ast.parse(Path(swmod.__file__).read_text(encoding="utf-8"))
        funnel = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_write_then_sync"
        ]
        assert len(funnel) == 1
        assert not [
            node for node in ast.walk(funnel[0]) if isinstance(node, (ast.Global, ast.AugAssign))
        ]

    def _drive(self, run=None):
        """Run one real batch pass, counting metrics calls and clock reads inside it.

        The clock is counted through the writer's own INJECTED ``clock``, not by
        rebinding the stdlib module: that would replace the reader for every
        thread and event loop in the worker. A clock read the pass took around
        the injected one -- a direct ``time.monotonic()`` added inside
        ``_write_batch`` -- is caught by
        :meth:`test_the_batch_pass_names_nothing_from_this_module`, which reads
        the source and so also covers a branch this pass does not take.
        """
        from kiro_crew.crew_log.writer import CrewLogWriter, JobKind, _Pending

        seen = {"metrics": 0, "clock": 0}
        depth = {"n": 0}

        def _spy_clock():
            if depth["n"]:
                seen["clock"] += 1
            return 0.0

        def _spy_metrics(*args, **kwargs):
            if depth["n"]:
                seen["metrics"] += 1
            return MetricsRecorder(None)

        original = CrewLogWriter._write_batch

        def traced(self, unit, jobs):
            depth["n"] += 1
            try:
                return original(self, unit, jobs)
            finally:
                depth["n"] -= 1

        writer = CrewLogWriter(lambda unit: None, clock=_spy_clock)
        job = _Pending(
            run=run if run is not None else (lambda: None),
            what="entry",
            kind=JobKind.APPEND,
            nbytes=64,
        )
        with patch.object(CrewLogWriter, "_write_batch", traced):
            with patch("kiro_crew.metrics.provider.get_recorder", _spy_metrics):
                with patch("kiro_crew.metrics.events.emit_counter", _spy_metrics):
                    writer._write_batch("unit", [job])
        return seen

    #: Clock reads one job's batch pass already took before this change: the
    #: stall-detection stamp ``_run_job`` takes per job (``_inflight_since``).
    #: Asserting this number rather than zero is what makes the test mean "no
    #: read was ADDED" -- any new one pushes the count above it.
    CLOCK_READS_PER_JOB = 1

    def test_the_batch_pass_makes_no_metrics_call(self):
        assert self._drive()["metrics"] == 0

    def test_the_batch_pass_takes_no_clock_read_beyond_its_own(self):
        assert self._drive()["clock"] == self.CLOCK_READS_PER_JOB

    def test_a_job_that_publishes_on_the_bus_still_costs_nothing(self):
        """The indirect route, with the subscriber behaving.

        A ``session/opened`` job advances the session tree and publishes
        ``tree_advanced`` while the write-behind is running it, so a subscriber
        runs on this thread. One that returns normally must add nothing.
        """
        delivered: list = []
        bus.reset_for_tests()
        try:
            bus.subscribe(bus.TREE_ADVANCED, delivered.append)
            seen = self._drive(run=lambda: bus.publish(bus.TREE_ADVANCED, object()))
        finally:
            bus.reset_for_tests()
        assert len(delivered) == 1
        assert seen == {"metrics": 0, "clock": self.CLOCK_READS_PER_JOB}

    def test_a_raising_subscriber_on_that_thread_cannot_trigger_a_first_import(self):
        """The counter is a no-op until something else has loaded this module.

        So a write thread can never be the one that imports the metrics facade,
        reads config and builds the SDK. The cost is the dropped point this
        asserts: with the module unloaded, the subscriber's failure is logged and
        not counted. The savepoint site makes the opposite trade and is pinned by
        ``test_a_savepoint_point_is_not_dropped_before_a_recorder_exists``.
        """
        bus.reset_for_tests()
        calls: list = []
        try:
            bus.subscribe(bus.TREE_ADVANCED, lambda e: (_ for _ in ()).throw(RuntimeError("x")))
            with patch.dict(sys.modules, {"kiro_crew.metrics.crew_log_health": None}):
                with patch.object(clh, "bus_subscriber_error", calls.append):
                    bus.publish(bus.TREE_ADVANCED, object())
        finally:
            bus.reset_for_tests()
        assert calls == []


class TestTheSavepointCounterIsOffTheWriteThread:
    """``checkpoint`` imports the facade, so its callers must not be write threads.

    The import is what stops a savepoint point being dropped, and it is only
    affordable because no caller of ``checkpoint.save`` or ``save_slot`` runs on
    a crew log write thread. That is a property of the call graph rather than of
    this module, so it is asserted from the call graph: a future change that
    folds a session from an emit job would redden here.
    """

    #: How the two savepoint writers are named by the one module that imports
    #: them: ``from kiro_crew.crew_log import checkpoint as savepoints``. Matching
    #: the qualified name and not the bare ``save`` keeps ``checkpoint``'s own
    #: calls into the savepoint STORE out of the answer.
    CALLS = {"savepoints.save", "savepoints.save_slot"}

    def _calls_the_savepoint_writers(self, module_path):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
        return any(
            isinstance(node, ast.Call) and ast.unparse(node.func) in self.CALLS
            for node in ast.walk(tree)
        )

    def test_the_savepoint_writers_are_reached_from_only_the_fold_path(self):
        from pathlib import Path

        from kiro_crew.crew_log import checkpoint

        crew_log = Path(checkpoint.__file__).parent
        reached = {
            path.name for path in crew_log.glob("*.py") if self._calls_the_savepoint_writers(path)
        }
        # The two fold surfaces, and nothing that runs inside a write job.
        assert reached == {"projection_session.py", "projection_slot_memo.py"}

    def test_no_emit_module_folds_a_session(self):
        """An emit job's ``run`` executes ON the write thread.

        None of them may reach a fold, because a fold can write a savepoint and
        the savepoint counter imports the metrics facade.
        """
        from pathlib import Path

        from kiro_crew.crew_log import checkpoint

        crew_log = Path(checkpoint.__file__).parent
        for path in sorted(crew_log.glob("emit*.py")):
            source = path.read_text(encoding="utf-8")
            assert "fold_session" not in source, path.name
            assert "_persist_slot_fold" not in source, path.name


class TestOmissionsAreDeliberate:
    """The three instruments of RFC 6.5's second batch that are NOT here.

    Each is absent because its only source is work on the write path. A test, so
    that re-adding one without a source off that path reddens rather than
    sliding past review.
    """

    def test_no_append_byte_or_batch_or_lag_instrument_is_registered(self):
        for name in clh.ALL_METRIC_NAMES:
            assert "append.bytes" not in name
            assert "append.batch" not in name
            assert "fold.lag" not in name

    def test_the_four_shipped_names_are_exactly_these(self):
        assert clh.ALL_METRIC_NAMES == (
            "kirocrew.crew_log.savepoint.write",
            "kirocrew.crew_log.eager.queue_depth",
            "kirocrew.crew_log.writer.buffered",
            "kirocrew.crew_log.bus.subscriber_error",
        )

    def test_the_metrics_doc_does_not_promise_an_omitted_instrument(self):
        from pathlib import Path

        repo = Path(clh.__file__).resolve().parents[3]
        table = (repo / "docs/system-specs/modules/metrics.md").read_text(encoding="utf-8")
        assert "`kirocrew.crew_log.append.bytes`" not in table
        assert "`kirocrew.crew_log.append.batch`" not in table
        assert "`kirocrew.crew_log.fold.lag`" not in table


# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #


class TestTelemetryOff:
    def test_a_savepoint_write_records_nothing_with_telemetry_off(self):
        noop = MetricsRecorder(None)
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=noop):
            clh.savepoint_write(kind="session", written=True, elapsed_ms=1.0)
            clh.bus_subscriber_error("fold_advanced")
        assert noop.enabled is False
        assert noop._counters == {}

    def test_a_bus_failure_records_nothing_with_telemetry_off(self):
        noop = MetricsRecorder(None)
        bus.reset_for_tests()
        try:
            bus.subscribe(bus.FOLD_ADVANCED, lambda e: (_ for _ in ()).throw(RuntimeError("x")))
            with patch("kiro_crew.metrics.provider.get_recorder", return_value=noop):
                bus.publish(bus.FOLD_ADVANCED, object())
        finally:
            bus.reset_for_tests()
        assert noop._counters == {}

    def test_the_observable_instruments_are_registered_only_on_the_live_path(self):
        from pathlib import Path

        from kiro_crew.metrics import provider

        source = Path(provider.__file__).read_text(encoding="utf-8")
        assert "register_crew_log_health_instruments" in source


# --------------------------------------------------------------------------- #
# Provider wiring
# --------------------------------------------------------------------------- #


class _ExplodingMeter:
    def create_observable_gauge(self, *args, **kwargs):
        raise RuntimeError("meter is broken")

    def create_observable_counter(self, *args, **kwargs):
        raise RuntimeError("meter is broken")


class TestProviderWiring:
    def test_a_meter_that_blows_up_is_logged_and_swallowed(self, caplog):
        with caplog.at_level(logging.WARNING):
            clh.register_crew_log_health_instruments(_ExplodingMeter())
        assert "crew log health instrument registration failed" in caplog.text

    def test_registration_validates_every_name_before_creating_anything(self):
        with patch.object(clh, "validate_name", side_effect=ValueError("bad name")):
            collected = _collect()
        assert collected == {}

    def test_the_live_build_path_registers_these_instruments(self):
        from pathlib import Path

        from kiro_crew.metrics import provider

        source = Path(provider.__file__).read_text(encoding="utf-8")
        assert source.count("register_crew_log_health_instruments") == 2
        assert "crew log health instruments unavailable" in source
