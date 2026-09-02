"""Guards for the per-instrument histogram bucket Views.

Context: bucket boundaries used to be ONE shared array applied through a single
catch-all ``View(instrument_type=Histogram)``. Its top bound was 60s, sized for
session startup, so the first ``kirocrew.turn.duration`` sample ever recorded
(227589ms) fell into the +Inf overflow bucket and the aggregator reported
``p50 == p90 == 60000`` — a ceiling artifact rendered as a real latency.

The fix replaces the catch-all with one View per instrument. That makes two
properties load-bearing, and both are asserted here:

1. **Completeness.** With no catch-all, an instrument missing from
   ``_HISTOGRAM_BUCKETS_MS`` silently falls back to OTEL's default 10s-ceiling
   boundaries. ``test_every_source_histogram_has_bounds`` fails when a histogram
   metric name appears in the source with no map entry.
2. **No duplicate streams.** The OTEL SDK applies EVERY matching View, not the
   first, so re-introducing a catch-all would publish each named instrument
   twice under one metric name. ``test_no_duplicate_streams_per_instrument``
   pins that.
"""
import ast
import re
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers.telemetry import _Hist
from kiro_crew.metrics import provider as provider_mod
from kiro_crew.metrics.provider import (
    _CREDIT_BUCKETS,
    _FAST_BUCKETS_MS,
    _HISTOGRAM_BUCKETS_BY_UNIT,
    _HISTOGRAM_BUCKETS_MS,
    _STARTUP_BUCKETS_MS,
    _TURN_BUCKETS_MS,
    _USD_BUCKETS,
    histogram_bounds,
)

_SRC = Path(provider_mod.__file__).resolve().parent.parent
# Histogram instrument names are the `.duration` metrics (all ms); counters
# end in `.count` / `.acquire` / `.action` / `.outcome` and carry no bounds.
_NAME_RE = re.compile(r'"(kirocrew\.[a-z0-9_.]*\.duration)"')


def _source_histogram_names() -> set[str]:
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.update(_NAME_RE.findall(text))
    return found


def _emitted_histogram_units() -> dict[str, set[str]]:
    """Instrument name -> the set of ``unit=`` values its emit calls pass.

    The naming scan above can only recognise a histogram by a ``.duration``
    suffix. That is not a property of the code: ``kirocrew.embed.queue_wait`` and
    ``kirocrew.embed.inference`` are millisecond histograms with no such suffix,
    and a non-duration histogram (``kirocrew.turn.credits``) has no reason to
    carry one — all three were therefore invisible to the old guard and inherited
    OTEL's default 10s-ceiling buckets in silence.

    So this walks every module for a call to ``histogram`` and reads its first
    argument (a string literal, or a module-level ``str`` constant resolved by
    name, which is how the turn family spells its instruments) together with its
    ``unit=`` keyword. Finding instruments by SHAPE is what makes the guard hold
    for a histogram nobody remembered to name in a regex, and reading the unit is
    what lets the two bucket maps be checked against what they actually claim
    rather than against a spelling convention.
    """
    found: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        consts: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    consts[target.id] = node.value.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if fname != "histogram":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                name = first.value
            elif isinstance(first, ast.Name) and first.id in consts:
                name = consts[first.id]
            else:
                continue
            if not name.startswith("kirocrew."):
                continue
            unit = "ms"  # the recorder's default
            for kw in node.keywords:
                if kw.arg == "unit" and isinstance(kw.value, ast.Constant):
                    unit = str(kw.value.value)
            found.setdefault(name, set()).add(unit)
    return found


def _emitted_histogram_names() -> set[str]:
    return set(_emitted_histogram_units())


class TestCompleteness:
    def test_source_scan_finds_the_known_instruments(self):
        """Guard the guard: a scan that matches nothing would pass vacuously."""
        names = _source_histogram_names()
        assert "kirocrew.turn.duration" in names
        assert "kirocrew.session.startup.duration" in names
        assert len(names) >= 7

    def test_every_source_histogram_has_bounds(self):
        missing = sorted(_source_histogram_names() - set(_HISTOGRAM_BUCKETS_MS))
        assert not missing, (
            "These duration histograms have no entry in "
            "provider._HISTOGRAM_BUCKETS_MS, so they would silently fall back to "
            f"OTEL's default 10s-ceiling buckets: {missing}. Add each one to the "
            "map with boundaries covering its real range."
        )

    def test_no_stale_map_entries(self):
        """A name dropped from the source should not linger in the map.

        Checked against the union of both scans: the ms map legitimately holds
        millisecond histograms whose names do not end in ``.duration`` (the embed
        pair), which the name scan alone cannot see.
        """
        live = _source_histogram_names() | _emitted_histogram_names()
        stale = sorted(set(_HISTOGRAM_BUCKETS_MS) - live)
        assert not stale, f"map entries with no emitting call site: {stale}"


class TestNonDurationHistograms:
    """A histogram whose values are not milliseconds needs the same guarantees.

    Two properties, both load-bearing and neither covered by the name-suffix scan
    above:

    1. **Explicit buckets.** `kirocrew.turn.credits` spans 0.03 to 658 in real
       data. OTEL's default boundaries top out at 10000 with nothing below 5, so
       every one of those samples would land in the FIRST bucket and the reported
       p50/p90 would be a constant.
    2. **Unit separation.** `_HISTOGRAM_BUCKETS_MS` is the map the dashboard's
       generic aggregation trusts when it reports every histogram under `*_ms`
       keys, so a non-ms instrument must not be registered there.
    """

    def test_emitted_scan_finds_both_families(self):
        """Guard the guard, again: an empty AST harvest would pass vacuously."""
        units = _emitted_histogram_units()
        assert units.get("kirocrew.turn.duration") == {"ms"}
        assert units.get("kirocrew.turn.credits") == {"credit"}
        assert units.get("kirocrew.turn.cost_usd") == {"usd"}
        # The pair that motivated the unit-based check: ms, no `.duration` suffix.
        assert units.get("kirocrew.embed.queue_wait") == {"ms"}

    def test_every_emitted_histogram_is_registered_somewhere(self):
        missing = sorted(_emitted_histogram_names() - set(histogram_bounds()))
        assert not missing, (
            "These histograms are recorded in the source but have no bucket "
            f"entry in either provider bucket map: {missing}. Without a View "
            "they inherit OTEL's default 0..10000 boundaries. Register a "
            "millisecond instrument in _HISTOGRAM_BUCKETS_MS and anything else "
            "in _HISTOGRAM_BUCKETS_BY_UNIT."
        )

    def test_by_unit_entries_have_an_emitting_call_site(self):
        stale = sorted(set(_HISTOGRAM_BUCKETS_BY_UNIT) - _emitted_histogram_names())
        assert not stale, f"non-ms map entries with no emitting call site: {stale}"

    def test_the_two_maps_are_disjoint(self):
        """Merge order in histogram_bounds() must not be able to hide an entry."""
        overlap = sorted(set(_HISTOGRAM_BUCKETS_MS) & set(_HISTOGRAM_BUCKETS_BY_UNIT))
        assert not overlap, f"registered in both bucket maps: {overlap}"
        assert len(histogram_bounds()) == len(_HISTOGRAM_BUCKETS_MS) + len(
            _HISTOGRAM_BUCKETS_BY_UNIT
        )

    def test_ms_map_holds_only_millisecond_instruments(self):
        """The invariant the dashboard's `*_ms` reporting actually depends on.

        Checked against the emitted ``unit=``, not against a ``.duration`` name
        suffix: the suffix is a convention two shipped ms histograms do not
        follow, so asserting on it would either reject them or force a rename
        that changes an instrument's published name for a test's benefit.
        """
        units = _emitted_histogram_units()
        wrong = sorted(
            name for name in _HISTOGRAM_BUCKETS_MS
            if name in units and units[name] != {"ms"}
        )
        assert not wrong, (
            f"non-millisecond instruments in the ms map: {wrong}. The dashboard "
            "reports every histogram in this map under *_ms keys."
        )

    def test_by_unit_map_holds_no_millisecond_instruments(self):
        units = _emitted_histogram_units()
        wrong = sorted(
            name for name in _HISTOGRAM_BUCKETS_BY_UNIT
            if units.get(name) == {"ms"}
        )
        assert not wrong, f"millisecond instruments belong in the ms map: {wrong}"

    def test_one_instrument_never_carries_two_units(self):
        """Two units under one name make every reported statistic meaningless."""
        mixed = sorted(n for n, u in _emitted_histogram_units().items() if len(u) > 1)
        assert not mixed, f"instruments emitted with conflicting units: {mixed}"

    def test_credit_bounds_cover_the_measured_range(self):
        """Calibration facts from 17,240 real per-turn credit rows."""
        assert _CREDIT_BUCKETS[0] < 0.0304, "observed minimum would floor at the first bound"
        assert _CREDIT_BUCKETS[-1] > 658, "observed maximum would fall into +Inf"
        # p50 6.76 and p90 53.3 must each sit strictly inside a bucket, not on a
        # boundary that a percentile can only report as a floor.
        for observed in (6.76, 53.3, 155.1):
            assert any(
                lo < observed <= hi
                for lo, hi in zip(_CREDIT_BUCKETS, _CREDIT_BUCKETS[1:])
            ), observed

    def test_usd_bounds_span_sub_cent_to_tens_of_dollars(self):
        assert _USD_BUCKETS[0] <= 0.001
        assert _USD_BUCKETS[-1] >= 100

    def test_default_otel_buckets_would_have_destroyed_the_credit_signal(self):
        """Why an explicit View is required rather than nice to have.

        OTEL's default boundaries start at 0 and 5. Against the measured credit
        distribution (p50 6.8, p90 53) they are not merely coarse — over half the
        population lands in the first two buckets, so the reported p50 could only
        ever be 0 or 5.
        """
        otel_default = [0.0, 5.0, 10.0, 25.0, 50.0, 75.0, 100.0, 250.0, 500.0, 750.0,
                        1000.0, 2500.0, 5000.0, 7500.0, 10000.0]
        below_five = sum(1 for b in _CREDIT_BUCKETS if b <= 5.0)
        assert below_five >= 7, "the credit array must resolve the sub-5 decade"
        assert sum(1 for b in otel_default if 0 < b <= 5.0) == 1


class TestBoundaryShape:
    @pytest.mark.parametrize("name,bounds", sorted(histogram_bounds().items()))
    def test_bounds_are_sorted_positive_and_unique(self, name, bounds):
        assert bounds, name
        assert all(b > 0 for b in bounds), name
        assert list(bounds) == sorted(bounds), name
        assert len(set(bounds)) == len(bounds), name

    def test_turn_ceiling_covers_a_realistic_agent_turn(self):
        """227589ms is the real first sample that exposed the overflow bug."""
        assert _TURN_BUCKETS_MS[-1] >= 227589 * 2
        assert _TURN_BUCKETS_MS[-1] == 3_600_000

    def test_fast_family_keeps_sub_millisecond_resolution(self):
        # backend.acquire and skill.lazy_load sit at ~1ms; without a sub-ms
        # bound their p50 collapses onto the first boundary.
        assert _FAST_BUCKETS_MS[0] < 1

    def test_fast_family_ceiling_is_not_lowered_from_the_historical_array(self):
        """Slow-but-ordinary endpoints (installers, provisioning) exceed 30s.

        Tightening this ceiling to 30s would floor their percentile at 30000ms
        — the same overflow artifact this change exists to remove, relocated to
        a different threshold.
        """
        assert _FAST_BUCKETS_MS[-1] == 60_000

    def test_startup_family_unchanged_range(self):
        assert _STARTUP_BUCKETS_MS[-1] == 60_000


class TestMixedBoundaryGenerations:
    """A boundary change makes the 14-day window straddle two generations.

    Bounds are baked into each data point at record time, so after this change
    lands the reader sees pre-change and post-change shards for the same metric.
    Merging them positionally fabricates percentiles: the OLD shared array and
    ``_TURN_BUCKETS_MS`` have the SAME bucket-count length, so a naive
    length-only check would add a pre-change sample sitting in the old ``+Inf``
    bucket into the new ``+Inf`` bucket (reporting p90 = 1 hour) and drop a 5s
    sample into a 5-minute bucket.
    """

    OLD_SHARED_BOUNDS = [
        1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 3000,
        5000, 7500, 10000, 15000, 20000, 30000, 45000, 60000,
    ]

    def _dp(self, bounds, landed_index, count=1, total=None, ns=None):
        counts = [0] * (len(bounds) + 1)
        counts[landed_index] = count
        val = total if total is not None else count * 1000.0
        dp = {
            "attributes": {},
            "count": count,
            "sum": float(val),
            "min": 1.0,
            "max": float(val),
            "bucket_counts": counts,
            "explicit_bounds": list(bounds),
        }
        if ns is not None:
            dp["time_unix_nano"] = ns
        return dp

    def test_old_and_new_turn_bounds_have_the_same_length(self):
        """Pins the precondition — without it the bug needs no guard."""
        assert len(self.OLD_SHARED_BOUNDS) == len(_TURN_BUCKETS_MS)

    def test_legacy_overflow_point_cannot_fabricate_a_one_hour_p90(self):
        h = _Hist()
        # Pre-change: a 227589ms turn, recorded as old-bounds +Inf overflow.
        h.add(self._dp(self.OLD_SHARED_BOUNDS, len(self.OLD_SHARED_BOUNDS),
                       count=1, total=227589))
        # Post-change: three ordinary ~30s turns under the new bounds.
        for _ in range(3):
            h.add(self._dp(_TURN_BUCKETS_MS, 5, count=1, total=30000))

        s = h.stats()
        assert s["p90_ms"] != 3_600_000.0, "legacy overflow leaked into new bounds"
        assert s["p90_ms"] <= _TURN_BUCKETS_MS[5]
        # Dominant generation is the new one (3 samples vs 1), and count is
        # consistent with the percentiles rather than summing both populations.
        assert s["count"] == 3
        assert h.other_generations == 1

    def test_generations_do_not_cross_contaminate_buckets(self):
        h = _Hist()
        h.add(self._dp(self.OLD_SHARED_BOUNDS, 11, count=5))   # 5 old samples
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=9))           # 9 new samples
        assert h.bounds == list(_TURN_BUCKETS_MS)
        assert sum(h.buckets) == 9, "old-generation counts bled into new buckets"

    def test_outnumbered_new_generation_still_wins_on_recency(self):
        """The reported scenario: 5 legacy turns vs 1 new turn.

        Majority selection would keep the legacy bounds — so the page would
        still serve the ceiling-pinned 60000ms percentiles and omit the new
        sample — for as long as the old generation out-counted the new one,
        which right after an upgrade is up to the whole 14-day window.
        """
        h = _Hist()
        for _ in range(5):
            h.add(self._dp(self.OLD_SHARED_BOUNDS, len(self.OLD_SHARED_BOUNDS),
                           count=1, total=227589, ns=1_000))
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=1, total=5000, ns=2_000))

        assert h.bounds == list(_TURN_BUCKETS_MS), "stale generation kept winning"
        assert h.stats()["count"] == 1
        assert h.stats()["p90_ms"] != 60000.0
        assert h.other_generations == 1

    def test_selection_prefers_recency_over_volume_either_direction(self):
        """A revert must also take effect immediately, not after out-counting."""
        h = _Hist()
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=50, ns=1_000))
        h.add(self._dp(self.OLD_SHARED_BOUNDS, 11, count=1, ns=9_999))
        assert h.bounds == list(self.OLD_SHARED_BOUNDS)

    def test_count_is_the_tiebreak_when_no_timestamps_exist(self):
        """Synthetic/older shards carry no time_unix_nano; all groups tie at 0."""
        h = _Hist()
        h.add(self._dp(self.OLD_SHARED_BOUNDS, 11, count=10))
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=11))
        assert h.bounds == list(_TURN_BUCKETS_MS)

    def test_single_generation_is_unaffected(self):
        """The startup family did not change bounds, so it must behave as before."""
        h = _Hist()
        h.add(self._dp(_STARTUP_BUCKETS_MS, 3, count=2, total=8000))
        h.add(self._dp(_STARTUP_BUCKETS_MS, 3, count=2, total=8000))
        s = h.stats()
        assert s["count"] == 4
        assert h.other_generations == 0
        assert s["mean_ms"] == 4000.0

    def test_outcomes_are_scoped_to_the_reported_generation(self):
        """Otherwise the outcome bar totals more than `count`.

        Scoping buckets and count but not outcomes would show N turns beside an
        outcome breakdown summing to more than N, with a fault rate computed
        over a different population than the latency next to it.
        """
        h = _Hist()
        # Pre-change generation: one error turn.
        h.add(self._dp(self.OLD_SHARED_BOUNDS, 11, count=1), outcome="error")
        # Post-change generation: three successful turns.
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=3), outcome="ok")

        assert h.outcomes == {"ok": 3}, "legacy outcome leaked into the new generation"
        assert sum(h.outcomes.values()) == h.stats()["count"]

    def test_fault_rate_matches_the_reported_population(self):
        h = _Hist()
        h.add(self._dp(self.OLD_SHARED_BOUNDS, 11, count=9), outcome="error")
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=3), outcome="ok")
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=1), outcome="error")
        oc = h.outcomes
        total = sum(oc.values())
        faults = sum(v for k, v in oc.items() if k != "ok")
        # New generation dominates (4 vs 9? no — 4 < 9), so old wins here and
        # the point stands either way: totals must agree with count.
        assert total == h.stats()["count"]
        assert 0.0 <= faults / total <= 1.0

    def test_outcomes_accumulate_within_one_generation(self):
        h = _Hist()
        h.add(self._dp(_TURN_BUCKETS_MS, 2, count=3), outcome="ok")
        h.add(self._dp(_TURN_BUCKETS_MS, 4, count=2), outcome="ok")
        h.add(self._dp(_TURN_BUCKETS_MS, 6, count=1), outcome="timeout")
        assert h.outcomes == {"ok": 5, "timeout": 1}
        assert sum(h.outcomes.values()) == h.stats()["count"] == 6


class TestViewWiring:
    """Drive the real SDK to prove the Views behave as intended."""

    def _provider(self):
        pytest.importorskip("opentelemetry.sdk.metrics")
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from opentelemetry.sdk.metrics.view import (
            ExplicitBucketHistogramAggregation,
            View,
        )

        reader = InMemoryMetricReader()
        mp = MeterProvider(
            metric_readers=[reader],
            views=[
                View(
                    instrument_name=name,
                    aggregation=ExplicitBucketHistogramAggregation(bounds),
                )
                for name, bounds in histogram_bounds().items()
            ],
        )
        return mp, reader

    def _points(self, reader, name):
        data = reader.get_metrics_data()
        out = []
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == name:
                        out.extend(m.data.data_points)
        return out

    def test_long_turn_does_not_land_in_the_overflow_bucket(self):
        """The regression: 227589ms must fall inside an explicit bucket."""
        mp, reader = self._provider()
        mp.get_meter("t").create_histogram(
            "kirocrew.turn.duration", unit="ms"
        ).record(227589)

        (dp,) = self._points(reader, "kirocrew.turn.duration")
        counts = list(dp.bucket_counts)
        overflow_index = len(dp.explicit_bounds)
        assert counts[overflow_index] == 0, "sample fell into the +Inf bucket"
        assert sum(counts) == 1
        # ...and the bucket it landed in is bounded above, so an interpolated
        # percentile can report a real value rather than a floor.
        landed = next(i for i, c in enumerate(counts) if c)
        assert landed < overflow_index

    def test_no_duplicate_streams_per_instrument(self):
        mp, reader = self._provider()
        meter = mp.get_meter("t")
        meter.create_histogram("kirocrew.turn.duration", unit="ms").record(5000)
        meter.create_histogram(
            "kirocrew.session.startup.duration", unit="ms"
        ).record(4400)

        assert len(self._points(reader, "kirocrew.turn.duration")) == 1
        assert len(self._points(reader, "kirocrew.session.startup.duration")) == 1

    def test_each_instrument_gets_its_own_family(self):
        mp, reader = self._provider()
        meter = mp.get_meter("t")
        meter.create_histogram("kirocrew.turn.duration", unit="ms").record(60000)
        meter.create_histogram(
            "kirocrew.mcp.backend.acquire.duration", unit="ms"
        ).record(1)

        (turn,) = self._points(reader, "kirocrew.turn.duration")
        (fast,) = self._points(reader, "kirocrew.mcp.backend.acquire.duration")
        assert list(turn.explicit_bounds) == list(_TURN_BUCKETS_MS)
        assert list(fast.explicit_bounds) == list(_FAST_BUCKETS_MS)

    def test_billing_histograms_get_their_own_non_ms_bounds(self):
        """A credit and a dollar must not inherit a duration family's array."""
        mp, reader = self._provider()
        meter = mp.get_meter("t")
        meter.create_histogram("kirocrew.turn.credits", unit="credit").record(6.76)
        meter.create_histogram("kirocrew.turn.cost_usd", unit="usd").record(0.0032)

        (credits,) = self._points(reader, "kirocrew.turn.credits")
        (cost,) = self._points(reader, "kirocrew.turn.cost_usd")
        assert list(credits.explicit_bounds) == list(_CREDIT_BUCKETS)
        assert list(cost.explicit_bounds) == list(_USD_BUCKETS)
        # Each sample sits inside a bounded bucket, so a percentile over it can
        # report a real amount rather than a floor.
        for dp in (credits, cost):
            counts = list(dp.bucket_counts)
            overflow = len(dp.explicit_bounds)
            assert counts[overflow] == 0
            landed = next(i for i, c in enumerate(counts) if c)
            assert 0 < landed < overflow, "sample landed in the unbounded first bucket"


class TestAggregatorReadsRealPercentiles:
    """End-to-end: the fix must change what the Telemetry page reports."""

    def test_turn_percentiles_are_no_longer_pinned_to_the_ceiling(self):
        from kiro_crew.dashboard.handlers.telemetry import _pct_from_buckets

        # Same sample, old vs new boundaries.
        old_bounds = _STARTUP_BUCKETS_MS  # what every histogram used to get
        old_counts = [0] * len(old_bounds) + [1]  # 227589ms -> overflow
        assert _pct_from_buckets(old_counts, old_bounds, 0.50) == 60000.0
        assert _pct_from_buckets(old_counts, old_bounds, 0.90) == 60000.0

        new_bounds = _TURN_BUCKETS_MS
        landed = next(
            i for i, b in enumerate(new_bounds) if b >= 227589
        )
        new_counts = [0] * (len(new_bounds) + 1)
        new_counts[landed] = 1
        p50 = _pct_from_buckets(new_counts, new_bounds, 0.50)
        p90 = _pct_from_buckets(new_counts, new_bounds, 0.90)
        # Reported inside the bucket that actually contains the sample.
        assert new_bounds[landed - 1] <= p50 <= new_bounds[landed]
        assert p50 != 60000.0
        assert p90 != 60000.0
