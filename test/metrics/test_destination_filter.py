"""Per-destination narrowing, asserted on what each exporter actually received.

One ``MeterProvider``, three readers on it at once: the real local JSONL sink, an
operator destination that narrows nothing, and one or more product destinations
that do. Everything is driven through ``force_flush`` on the real SDK, so what is
asserted is the encoded batch each exporter was handed -- not the wrapper's view
of its own work.

The claim under test has two halves and both need the other two readers present
to mean anything:

* a narrowed destination receives ONLY its allowed metrics, every data point
  carrying its fixed attributes, under a Resource reduced to its allowed keys;
* the local sink and every other destination receive what they received before --
  every metric, no fixed attribute, the full Resource.

``TestNearbyInputs`` walks the inputs that reach the same code: an empty
allowlist, an allowlist naming an unknown metric, observable gauges, a point that
already carries the fixed key, two destinations with different allowlists, a
refused attribute key, and a failure raised inside the narrowing itself.
"""

from __future__ import annotations

import json
import math

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource

from kiro_crew.metrics import destination_filter as df
from kiro_crew.metrics.local_exporter import JsonlMetricExporter
from kiro_crew.metrics.temporality import delta_preference
from kiro_crew.platform.interfaces import OtlpDestination

#: Two product metrics and one that is deliberately NOT exported, so "only the
#: allowed one arrived" is a statement about filtering rather than about an
#: instrument that happened to be the only one recording.
ALLOWED = "kirocrew.cron.fires"
ALSO_ALLOWED = "kirocrew.artifact.created"
NOT_ALLOWED = "kirocrew.turn.timeout.cause"
GAUGE = "kirocrew.inventory.crons.active"

CLIENT_ATTRS = {"ClientType": "kiro-crew"}

#: A resource shaped like the live one: a product destination may receive the
#: first two keys; the rest are install- and host-scoped and must not reach it.
FULL_RESOURCE = {
    "service.name": "kirocrew",
    "service.version": "9.9.9",
    "service.instance.id": "install-id-0000",
    "os.type": "linux",
    "process.pid": 4242,
    "host.name": "a-host",
}


class _RecordingExporter:
    """A metric exporter that keeps every batch it is handed, in memory.

    Mirrors the two underscore attributes ``PeriodicExportingMetricReader`` reads
    off an exporter at construction, so it stands in for a real one without a
    network or the optional OTLP extra.
    """

    def __init__(self):
        self._preferred_temporality = delta_preference()
        self._preferred_aggregation = None
        self.batches: list = []
        self.calls = 0

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
        self.calls += 1
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        return None

    # -- reading back what arrived ------------------------------------------

    def metric_names(self) -> "set[str]":
        return {m.name for m in self._metrics()}

    def points(self, name: str) -> list:
        return [p for m in self._metrics() if m.name == name for p in m.data.data_points]

    def attrs(self, name: str) -> "list[dict]":
        return [dict(p.attributes or {}) for p in self.points(name)]

    def resources(self) -> "list[dict]":
        return [
            dict(rm.resource.attributes or {})
            for batch in self.batches
            for rm in batch.resource_metrics
        ]

    def _metrics(self):
        for batch in self.batches:
            for rm in batch.resource_metrics:
                for sm in rm.scope_metrics:
                    yield from sm.metrics


def _reader(exporter) -> PeriodicExportingMetricReader:
    """A reader that never ticks on its own: only force_flush exports.

    ``math.inf`` is the SDK's own spelling for "no periodic thread", so each test
    drives exactly one export and no background thread outlives it.
    """
    return PeriodicExportingMetricReader(exporter, export_interval_millis=math.inf)


class _Harness:
    """One MeterProvider, the real local sink, and the destinations under test."""

    def __init__(self, tmp_path, destinations):
        self.local_dir = tmp_path / "local"
        self.local_dir.mkdir()
        self.exporters: "dict[str, _RecordingExporter]" = {}
        readers = [_reader(JsonlMetricExporter(self.local_dir))]
        for dest in destinations:
            inner = _RecordingExporter()
            self.exporters[dest.name] = inner
            readers.append(_reader(df.wrap_if_narrowed(dest, inner)))
        self.provider = MeterProvider(metric_readers=readers, resource=Resource(FULL_RESOURCE))
        self.meter = self.provider.get_meter("kiro_crew")

    def record(self, *, gauge_value: "int | None" = None, attrs: "dict | None" = None):
        """Emit one point on each of three counters, plus an observable gauge."""
        for name in (ALLOWED, ALSO_ALLOWED, NOT_ALLOWED):
            self.meter.create_counter(name).add(1, attributes=attrs or {})
        if gauge_value is not None:
            from opentelemetry.metrics import Observation

            self.meter.create_observable_gauge(
                GAUGE, callbacks=[lambda _o: [Observation(gauge_value)]]
            )

    def flush(self):
        self.provider.force_flush()

    def shutdown(self):
        self.provider.shutdown()

    def local_records(self) -> "tuple[set[str], list[dict], list[dict]]":
        """(metric names, every data point, every resource) off the real shards."""
        names: "set[str]" = set()
        points: "list[dict]" = []
        resources: "list[dict]" = []
        for shard in sorted(self.local_dir.glob("metrics-*.jsonl")):
            for line in shard.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                for rm in json.loads(line).get("resource_metrics") or []:
                    resources.append((rm.get("resource") or {}).get("attributes") or {})
                    for sm in rm.get("scope_metrics") or []:
                        for metric in sm.get("metrics") or []:
                            names.add(metric["name"])
                            points.extend((metric.get("data") or {}).get("data_points") or [])
        return names, points, resources


@pytest.fixture
def harness(tmp_path):
    built: list = []

    def _build(destinations):
        h = _Harness(tmp_path, destinations)
        built.append(h)
        return h

    yield _build
    for h in built:
        h.shutdown()


def _operator() -> OtlpDestination:
    """A destination exactly as one exists today: it narrows nothing."""
    return OtlpDestination("operator", "https://op.example/v1/metrics", frozenset({"metrics"}))


def _product(**overrides) -> OtlpDestination:
    kwargs = {
        "allow": frozenset({ALLOWED}),
        "point_attributes": CLIENT_ATTRS,
        "resource_allow": frozenset({"service.name", "service.version"}),
    }
    kwargs.update(overrides)
    return OtlpDestination(
        "product", "https://product.example/v1/metrics", frozenset({"metrics"}), None, **kwargs
    )


class TestTheObservableOutcome:
    """The whole claim, in one export, with all three sinks attached."""

    def test_only_the_allowed_metric_reaches_the_narrowed_destination(self, harness):
        h = harness([_operator(), _product()])
        h.record()
        h.flush()

        product = h.exporters["product"]
        assert product.metric_names() == {ALLOWED}, (
            "the narrowed destination received something outside its allowlist, or "
            "nothing at all"
        )

    def test_every_point_of_the_narrowed_destination_carries_the_fixed_attribute(self, harness):
        h = harness([_operator(), _product()])
        h.record()
        h.flush()

        points = h.exporters["product"].attrs(ALLOWED)
        assert points, "the allowed metric arrived with no data points"
        for attributes in points:
            assert attributes.get("ClientType") == "kiro-crew", attributes

    def test_the_narrowed_destination_sees_only_its_allowed_resource_keys(self, harness):
        h = harness([_operator(), _product()])
        h.record()
        h.flush()

        resources = h.exporters["product"].resources()
        assert resources, "no resource block reached the narrowed destination"
        for resource in resources:
            assert set(resource) == {"service.name", "service.version"}, resource
            assert resource["service.name"] == "kirocrew"

    def test_the_operator_destination_is_untouched(self, harness):
        """Every metric, no fixed attribute, the full resource -- as today."""
        h = harness([_operator(), _product()])
        h.record()
        h.flush()

        operator = h.exporters["operator"]
        assert {ALLOWED, ALSO_ALLOWED, NOT_ALLOWED} <= operator.metric_names()
        for name in (ALLOWED, ALSO_ALLOWED, NOT_ALLOWED):
            for attributes in operator.attrs(name):
                assert "ClientType" not in attributes, (
                    f"{name}: the product destination's fixed attribute reached the "
                    "operator destination"
                )
        for resource in operator.resources():
            assert set(resource) == set(FULL_RESOURCE), resource
            assert resource["service.instance.id"] == "install-id-0000"

    def test_the_local_sink_is_untouched(self, harness):
        """Asserted off the real JSONL shards, not a stand-in exporter."""
        h = harness([_operator(), _product()])
        h.record()
        h.flush()

        names, points, resources = h.local_records()
        assert {ALLOWED, ALSO_ALLOWED, NOT_ALLOWED} <= names
        assert points, "the local sink wrote no data points"
        for point in points:
            assert "ClientType" not in (point.get("attributes") or {}), point
        assert resources, "the local sink wrote no resource block"
        for resource in resources:
            assert resource.get("service.instance.id") == "install-id-0000"

    def test_a_destination_that_narrows_nothing_is_not_wrapped_at_all(self):
        """No behaviour change for an existing destination, by construction.

        The exporter object handed to the reader is the one the builder built, so
        there is no wrapper in the path to differ by -- which is a stronger
        statement than "the wrapper happens to be a no-op".
        """
        inner = _RecordingExporter()
        assert df.wrap_if_narrowed(_operator(), inner) is inner
        assert df.narrows_export(_operator()) is False

    def test_each_narrowing_alone_is_enough_to_wrap(self):
        for dest in (
            _product(point_attributes={}, resource_allow=None),
            _product(allow=None, resource_allow=None),
            _product(allow=None, point_attributes={}),
        ):
            assert df.narrows_export(dest) is True


class TestNearbyInputs:
    """The inputs that reach the same code, each with its decided answer."""

    def test_an_empty_allowlist_sends_nothing(self, harness):
        """frozenset() is a real decision, not "unset": nothing is exported, and
        no empty request is made either -- an empty POST carries no information
        and still costs a round trip."""
        h = harness([_operator(), _product(allow=frozenset())])
        h.record()
        h.flush()

        assert h.exporters["product"].batches == []
        assert h.exporters["product"].calls == 0
        assert ALLOWED in h.exporters["operator"].metric_names(), "the sibling still exported"

    def test_an_allowlist_naming_an_unknown_metric_sends_nothing_and_does_not_raise(self, harness):
        """A name no instrument declares is simply never matched. It cannot be a
        hard error here: the allowlist is resolved long before anyone knows which
        instruments a given process will actually record."""
        h = harness([_operator(), _product(allow=frozenset({"kirocrew.no.such.metric"}))])
        h.record()
        h.flush()

        assert h.exporters["product"].metric_names() == set()
        assert h.exporters["operator"].metric_names()

    def test_an_observable_gauge_is_filtered_and_labelled_like_a_counter(self, harness):
        """A gauge's points come from a callback at collection time rather than
        from an add(), so they are a different object path through the wrapper --
        and over half of PRODUCT_METRICS is observable gauges."""
        h = harness([_operator(), _product(allow=frozenset({GAUGE}))])
        h.record(gauge_value=7)
        h.flush()

        product = h.exporters["product"]
        assert product.metric_names() == {GAUGE}
        points = product.points(GAUGE)
        assert [p.value for p in points] == [7]
        assert all(dict(p.attributes or {}) == CLIENT_ATTRS for p in points)
        # The operator destination still sees the gauge WITHOUT the label.
        assert h.exporters["operator"].attrs(GAUGE) == [{}]

    def test_attributes_already_on_a_point_are_kept_beside_the_fixed_ones(self, harness):
        h = harness([_operator(), _product()])
        h.record(attrs={"kind": "agent", "trigger": "schedule"})
        h.flush()

        assert h.exporters["product"].attrs(ALLOWED) == [
            {"kind": "agent", "trigger": "schedule", "ClientType": "kiro-crew"}
        ]

    def test_the_destinations_fixed_value_wins_over_one_already_on_the_point(self, harness):
        """DECIDED: the destination's constant overwrites a colliding key.

        The receiving side routes on this value, and it is fixed at construction
        from a reviewed set. A call site that managed to emit the same key must
        not be able to change where this destination's data lands -- so the
        collision resolves toward the descriptor, not toward the emitter.
        """
        h = harness([_operator(), _product()])
        h.record(attrs={"ClientType": "not-kiro-crew"})
        h.flush()

        assert h.exporters["product"].attrs(ALLOWED) == [{"ClientType": "kiro-crew"}]
        # The operator destination keeps what the call site actually passed.
        assert h.exporters["operator"].attrs(ALLOWED) == [{"ClientType": "not-kiro-crew"}]

    def test_two_narrowed_destinations_do_not_see_each_others_narrowing(self, harness):
        first = OtlpDestination(
            "first",
            "https://first.example/v1/metrics",
            frozenset({"metrics"}),
            None,
            frozenset({ALLOWED}),
            {"ClientType": "kiro-crew"},
            frozenset({"service.name"}),
        )
        second = OtlpDestination(
            "second",
            "https://second.example/v1/metrics",
            frozenset({"metrics"}),
            None,
            frozenset({ALSO_ALLOWED}),
            None,
            None,
        )
        h = harness([first, second, _operator()])
        h.record()
        h.flush()

        assert h.exporters["first"].metric_names() == {ALLOWED}
        assert h.exporters["second"].metric_names() == {ALSO_ALLOWED}
        assert h.exporters["first"].attrs(ALLOWED) == [CLIENT_ATTRS]
        assert h.exporters["second"].attrs(ALSO_ALLOWED) == [{}], (
            "the first destination's point attribute reached the second one, so the "
            "wrapper mutated an object both exporters read"
        )
        assert set(h.exporters["first"].resources()[0]) == {"service.name"}
        assert set(h.exporters["second"].resources()[0]) == set(FULL_RESOURCE)
        assert {ALLOWED, ALSO_ALLOWED, NOT_ALLOWED} <= h.exporters["operator"].metric_names()

    def test_a_resource_key_absent_from_the_shared_resource_is_simply_absent(self, harness):
        """Nothing is invented. A destination may name a key the build does not
        set -- an edition's allowlist outliving a resource attribute -- and the
        answer is a missing key, never an empty or placeholder value."""
        h = harness([_product(resource_allow=frozenset({"service.name", "cloud.account.id"}))])
        h.record()
        h.flush()

        assert set(h.exporters["product"].resources()[0]) == {"service.name"}

    def test_a_failure_inside_the_narrowing_drops_only_that_batch(self, harness, monkeypatch):
        """Fail CLOSED, and contained.

        Forwarding the unnarrowed batch is the one outcome that turns a bug in
        here into an egress of data the destination may not receive, so a raise
        drops the batch. The other two sinks and the sibling destination are
        unaffected, and nothing propagates out of force_flush.
        """

        def _boom(self, metric):
            raise RuntimeError("narrowing blew up")

        monkeypatch.setattr(df.FilteredMetricExporter, "_narrow_metric", _boom)
        h = harness([_operator(), _product()])
        h.record()
        h.flush()  # must not raise

        assert h.exporters["product"].batches == [], "an unnarrowed batch was forwarded"
        assert {ALLOWED, ALSO_ALLOWED, NOT_ALLOWED} <= h.exporters["operator"].metric_names()
        names, points, _ = h.local_records()
        assert {ALLOWED, ALSO_ALLOWED, NOT_ALLOWED} <= names
        assert points

    def test_the_first_narrowing_failure_warns_and_the_rest_are_debug(self, caplog):
        """A per-minute reader that kept warning would write one line per
        interval forever; the first one still has to be visible at WARNING,
        because a default install collects WARNING and above."""
        import logging

        wrapper = df.FilteredMetricExporter(
            _RecordingExporter(), name="product", allow=frozenset({ALLOWED})
        )

        class _Exploding:
            @property
            def resource_metrics(self):
                raise RuntimeError("collection blew up")

        with caplog.at_level(logging.DEBUG, logger=df.logger.name):
            assert wrapper.export(_Exploding()) is MetricExportResult.FAILURE
            assert wrapper.export(_Exploding()) is MetricExportResult.FAILURE

        levels = [r.levelno for r in caplog.records if r.name == df.logger.name]
        assert levels.count(logging.WARNING) == 1, levels
        assert levels.count(logging.DEBUG) == 1, levels
        assert "product" in caplog.text
        assert "product.example" not in caplog.text, "the endpoint was logged"


class TestRefusedPointAttributes:
    """An unreviewed key refuses the destination rather than dropping the label."""

    def test_an_unreviewed_key_raises_at_construction(self):
        with pytest.raises(ValueError) as excinfo:
            df.FilteredMetricExporter(_RecordingExporter(), point_attributes={"UserId": "u-1"})
        assert "UserId" in str(excinfo.value)
        assert "u-1" not in str(excinfo.value), "the attribute VALUE is in the message"

    def test_the_reviewed_key_is_accepted(self):
        wrapper = df.FilteredMetricExporter(_RecordingExporter(), point_attributes=CLIENT_ATTRS)
        assert wrapper._point_attributes == CLIENT_ATTRS
        assert df.ALLOWED_POINT_ATTRIBUTE_KEYS == frozenset({"ClientType"})

    def test_a_refused_key_costs_that_destination_its_reader_and_nothing_else(
        self, monkeypatch, caplog
    ):
        """The builder's answer to the raise: this destination gets NO reader, the
        local sink and every other destination keep working, and the log names the
        key rather than reading as a constructor failure."""
        import logging

        from kiro_crew.config.loader import TelemetryConfig
        from kiro_crew.metrics.provider import _build_otlp_reader

        dest = OtlpDestination(
            "refused",
            "https://refused.example/v1/metrics",
            frozenset({"metrics"}),
            None,
            None,
            {"AccountId": "123456789012"},
            None,
        )
        with caplog.at_level(logging.WARNING):
            assert _build_otlp_reader(dest, TelemetryConfig(enabled=True)) is None
        assert "AccountId" in caplog.text
        assert "123456789012" not in caplog.text, "the attribute VALUE was logged"
        assert "refused.example" not in caplog.text, "the endpoint was logged"

    def test_no_mapping_at_all_is_valid_and_adds_nothing(self):
        assert df.validate_point_attributes(None) == {}
        assert df.validate_point_attributes({}) == {}


class TestTheWrapperKeepsTheCoresDecisions:
    def test_temporality_and_aggregation_are_mirrored_from_the_wrapped_exporter(self):
        """A destination says where to send; temporality stays a core decision.

        PeriodicExportingMetricReader reads both off its exporter at
        construction, so a wrapper that answered None for them would silently
        put this destination back on the SDK's CUMULATIVE default while the local
        sink stayed on DELTA.
        """
        inner = _RecordingExporter()
        wrapper = df.FilteredMetricExporter(inner, allow=frozenset({ALLOWED}))
        assert wrapper._preferred_temporality is inner._preferred_temporality
        assert wrapper._preferred_temporality == delta_preference()
        assert wrapper._preferred_aggregation is inner._preferred_aggregation

    def test_force_flush_and_shutdown_reach_the_wrapped_exporter(self):
        calls: list = []

        class _Recording(_RecordingExporter):
            def force_flush(self, timeout_millis: float = 10_000) -> bool:
                calls.append(("force_flush", timeout_millis))
                return True

            def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
                calls.append(("shutdown", timeout_millis))

        wrapper = df.FilteredMetricExporter(_Recording(), allow=frozenset({ALLOWED}))
        assert wrapper.force_flush(1234) is True
        wrapper.shutdown(5678)
        assert calls == [("force_flush", 1234), ("shutdown", 5678)]
