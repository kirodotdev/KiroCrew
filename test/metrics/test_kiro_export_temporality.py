"""The Kiro destination's temporality preset and native histograms.

A sibling of ``test_kiro_export_destination.py``, whose fixtures and helpers it
reuses (its autouse ``_clean_env`` included), so both files stay under the line
cap. Three claims, each on the real path:

* the descriptor asks for the delta preset, and the core's preset is the
  OpenTelemetry delta preset with every instrument kind pinned;
* in one live build, ONLY this destination's reader is on that preset: the local
  JSONL sink and an operator destination resolve exactly what a reader over a
  plain exporter of each kind resolves, with and without the operator's
  OpenTelemetry temporality variable set;
* a histogram reaches this destination's real exporter as a native OTLP
  histogram with delta counts, not as bucket-labelled counters.
"""

import pytest

from .test_kiro_export_destination import (  # noqa: F401 - _clean_env is an autouse fixture
    _Adapter,
    _Cfg,
    _clean_env,
    _destinations,
    _install_id,
    _sign_in,
    _token,
    kiro_export,
    requires_otlp,
)


def test_this_destination_asks_for_the_otel_delta_preset_the_core_owns():
    """Delta for counters, observable counters and histograms; cumulative for up/down.

    How the other Kiro clients send to this endpoint. The descriptor only asks
    for it; the map is the core's.
    """
    from opentelemetry.sdk.metrics import (
        Counter,
        Histogram,
        ObservableCounter,
        ObservableGauge,
        ObservableUpDownCounter,
        UpDownCounter,
    )
    from opentelemetry.sdk.metrics.export import AggregationTemporality as T

    from kiro_crew.metrics import temporality

    (dest,) = _destinations(_Cfg())
    assert dest.delta_temporality is True
    assert temporality.otel_delta_preset() == {
        Counter: T.DELTA,
        UpDownCounter: T.CUMULATIVE,
        Histogram: T.DELTA,
        ObservableCounter: T.DELTA,
        ObservableUpDownCounter: T.CUMULATIVE,
        ObservableGauge: T.CUMULATIVE,
    }


def _reader_temporality(reader):
    """A reader's resolved kind -> temporality map, keyed by public kind name.

    ``MetricReader`` resolves its exporter's preference at construction into a
    map keyed by the SDK's internal instrument classes (``_Counter`` ...); the
    names are what a test can compare across SDK releases.
    """
    return {
        kind.__name__.lstrip("_"): value
        for kind, value in reader._instrument_class_temporality.items()
    }


def _reference_reader_temporality(exporter):
    """What a reader over *exporter* resolves to, built the way the core built one."""
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=3_600_000)
    try:
        return _reader_temporality(reader)
    finally:
        reader.shutdown()


@requires_otlp
@pytest.mark.parametrize("operator_env", [None, "CUMULATIVE"])
def test_only_this_destinations_reader_is_built_on_the_delta_preset(
    monkeypatch, tmp_path, operator_env
):
    """The live build: THIS reader is delta; the local sink and the operator's keep theirs.

    One provider, three readers, inspected where ``PeriodicExportingMetricReader``
    actually resolved temporality. The operator's reader and the local sink are
    compared against readers built exactly the way the core built them before
    presets existed, so "unchanged" is measured rather than restated. With the
    operator's OpenTelemetry variable set, the operator's reader follows it and
    this one still does not.
    """
    from unittest.mock import patch

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics.export import MetricExportResult

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.metrics import destination_filter
    from kiro_crew.metrics import inventory_gauges as ig
    from kiro_crew.metrics import provider as pm
    from kiro_crew.metrics import temporality
    from kiro_crew.metrics.local_exporter import JsonlMetricExporter

    if operator_env is None:
        monkeypatch.delenv(temporality.TEMPORALITY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(temporality.TEMPORALITY_ENV_VAR, operator_env)
    # No reader in this test may reach a socket, including on the shutdown flush.
    monkeypatch.setattr(
        OTLPMetricExporter, "export", lambda self, *a, **k: MetricExportResult.SUCCESS
    )
    _sign_in(monkeypatch, _token())
    _install_id(monkeypatch)

    cfg = KiroCrewConfig()
    cfg.telemetry.enabled = True
    cfg.telemetry.kiro_export = True
    cfg.telemetry.otlp_endpoint = "http://127.0.0.1:4318/v1/metrics"
    cfg.telemetry.export_interval_seconds = 3600

    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    ig.reset_for_testing()
    pm.reset_for_testing()
    try:
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
                assert pm.get_recorder().enabled
                readers = list(pm._provider._sdk_config.metric_readers)
                by_role = {}
                for reader in readers:
                    exporter = reader._exporter
                    if isinstance(exporter, JsonlMetricExporter):
                        by_role["local"] = reader
                    elif isinstance(exporter, destination_filter.FilteredMetricExporter):
                        by_role[exporter._name] = reader
                    elif isinstance(exporter, OTLPMetricExporter):
                        by_role["operator"] = reader
                assert set(by_role) == {"local", "operator", kiro_export.DESTINATION_NAME}, by_role

                kiro = _reader_temporality(by_role[kiro_export.DESTINATION_NAME])
                operator = _reader_temporality(by_role["operator"])
                local = _reader_temporality(by_role["local"])

                preference = temporality.otlp_preference()
                operator_before = _reference_reader_temporality(
                    OTLPMetricExporter(
                        endpoint="http://127.0.0.1:4318/v1/metrics",
                        **({} if preference is None else {"preferred_temporality": preference}),
                    )
                )
                local_before = _reference_reader_temporality(
                    JsonlMetricExporter(directory=tmp_path / "reference")
                )
    finally:
        pm.reset_for_testing()
        ig.reset_for_testing()

    from opentelemetry.sdk.metrics.export import AggregationTemporality as T

    assert kiro["Counter"] is T.DELTA
    assert kiro["Histogram"] is T.DELTA
    assert kiro["ObservableCounter"] is T.DELTA
    assert kiro["UpDownCounter"] is T.CUMULATIVE
    assert kiro["ObservableUpDownCounter"] is T.CUMULATIVE

    assert operator == operator_before, "the operator destination's temporality changed"
    assert local == local_before, "the local sink's temporality changed"
    if operator_env == "CUMULATIVE":
        assert operator["Counter"] is T.CUMULATIVE, "the operator's variable no longer applies"
    else:
        assert operator["Counter"] is T.DELTA
        assert operator["UpDownCounter"] is T.DELTA, "the operator default moved to the preset"


@requires_otlp
def test_a_histogram_reaches_this_destination_as_a_native_delta_histogram(monkeypatch):
    """A distribution leaves as an OTLP histogram, not as bucket-labelled counters.

    The real descriptor (allowlist unchanged), the real reader build, the real
    exporter and a decoded protobuf body. ``kirocrew.session.duration`` is the
    exported histogram, recorded twice across two exports: each export carries
    ONE observation, which is what delta means on the wire, and the buckets
    arrive as the histogram's own fields.
    """
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
    from opentelemetry.proto.metrics.v1 import metrics_pb2
    from opentelemetry.sdk.metrics import MeterProvider

    from kiro_crew.metrics import provider as pm
    from kiro_crew.metrics.product_metrics import PRODUCT_METRICS

    histogram_name = "kirocrew.session.duration"
    assert histogram_name in pm.histogram_bounds(), "not a histogram in this tree"
    assert histogram_name in PRODUCT_METRICS, "the exported histogram left the list"
    counter_name = "kirocrew.session.started"

    _sign_in(monkeypatch, _token())
    _install_id(monkeypatch)
    adapter = _Adapter((200,))

    def _session_factory():
        session = kiro_export.KiroExportSession()
        session.mount("https://", adapter)
        return session

    monkeypatch.setattr(kiro_export, "build_session", _session_factory)
    (dest,) = _destinations(_Cfg())
    reader = pm._build_otlp_reader(dest, _Cfg(export_interval_seconds=3600))
    assert reader is not None
    provider = MeterProvider(metric_readers=[reader])
    try:
        meter = provider.get_meter("kiro_crew")
        histogram = meter.create_histogram(histogram_name, unit="ms")
        counter = meter.create_counter(counter_name)
        histogram.record(12.0, {"end_reason": "reset", "session_source": "chat"})
        counter.add(1, {"session_source": "chat"})
        provider.force_flush()
        histogram.record(40.0, {"end_reason": "reset", "session_source": "chat"})
        counter.add(1, {"session_source": "chat"})
        provider.force_flush()
    finally:
        provider.shutdown()

    exports = []
    for request, _kwargs in adapter.sent:
        payload = metrics_service_pb2.ExportMetricsServiceRequest()
        payload.ParseFromString(request.body)
        exports.append(
            {
                metric.name: metric
                for rm in payload.resource_metrics
                for sm in rm.scope_metrics
                for metric in sm.metrics
            }
        )
    assert len(exports) >= 2, f"expected two exports, got {len(exports)}"
    delta = metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA
    for sent, value in zip(exports[:2], (12.0, 40.0)):
        metric = sent[histogram_name]
        assert metric.WhichOneof("data") == "histogram", metric.WhichOneof("data")
        assert metric.histogram.aggregation_temporality == delta
        (point,) = metric.histogram.data_points
        assert point.count == 1, "a delta histogram carries only this interval"
        assert point.sum == value
        assert list(point.explicit_bounds), "the buckets did not arrive"
        assert sum(point.bucket_counts) == 1
        assert {a.key: a.value.string_value for a in point.attributes} == {
            "end_reason": "reset",
            "session_source": "chat",
            "ClientType": "kiro-crew",
        }
        assert not [
            name for name in sent if name.startswith(histogram_name + ".")
        ], "a histogram was split into per-bucket series"
        counter_metric = sent[counter_name]
        assert counter_metric.sum.aggregation_temporality == delta
        assert [p.as_int for p in counter_metric.sum.data_points] == [1]
