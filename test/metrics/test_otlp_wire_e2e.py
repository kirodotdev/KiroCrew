"""End-to-end verification that telemetry actually reaches an exporter.

``test_local_exporter.py`` already proves that serialization works, but it proves
it of the exporter alone: it constructs a ``JsonlMetricExporter`` directly, feeds
it from a provider assembled in the test, and asserts valid JSONL, shard
lifecycle, and resource-level identity. What no test covered is the same question
asked of the LIVE BUILD -- that the roster ``provider._build_recorder()`` actually
registers arrives, carrying the attribute values the modules declare, at the
temporality each instrument kind requires. That gap is real: an instrument can be
missing, and an attribute value or a temporality setting can be correct in the
SDK's in-memory view and still be wrong, dropped, or unencodable by the time it
leaves the process. These tests close it by driving that live path and reading
what an exporter actually received.

Two tiers, because the OTLP exporter is an optional extra
(``pip install "kirocrew[otlp]"``) that a default dev/CI environment does not
have:

* **Tier 1 — serialization (always runs).** The live build's own JSONL exporter
  writes real records to disk. Asserts the full instrument roster arrives, that
  resource attributes survive with fidelity, that attribute values are the closed
  enums the modules declare, and that temporality is what each instrument kind
  requires.
* **Tier 2 — OTLP wire (skipped without the extra).** A real
  ``OTLPMetricExporter`` posts to an in-process HTTP receiver bound to loopback,
  whose protobuf body is decoded and asserted. No container, no downloaded
  binary, and no egress off the machine, so it runs anywhere the extra is
  installed.

A third tier — a genuine ``otel-collector`` process — is deliberately NOT here: it
needs a downloaded binary and outbound network, so it cannot run in a
network-isolated CI. ``docs/guides/telemetry-otlp-export.md`` describes how to do
that check by hand against whichever collector you actually run.
"""

import gzip
import http.server
import json
import os
import threading
from unittest.mock import patch

import pytest

from kiro_crew.metrics import inventory_gauges as ig
from kiro_crew.metrics import process_gauges as pg
from kiro_crew.metrics import provider as pm
from kiro_crew.metrics.schema import RESOURCE_ATTR_PROCESS_START_TIME

# OTEL AggregationTemporality, as it appears in serialized output.
_DELTA = 1
_CUMULATIVE = 2

#: Instruments that are ABSENT on a healthy install by design, so a roster
#: assertion must not require them. ``probe.failures`` publishes only once a probe
#: has failed -- its presence is the signal, so demanding it here would invert the
#: contract. Its positive path is covered in ``test_inventory_gauges.py``.
_HEALTHY_ABSENT = frozenset({ig.COUNTER_PROBE_FAILURES})

#: Process gauges whose SOURCE is Linux-only: ``process_gauges`` maps an
#: unavailable ``/proc`` surface to None, which is a gap by design. Exempted only
#: where that surface is actually missing, so the roster stays fully asserted on
#: Linux and a Windows or macOS runner does not read a correct gap as a regression.
_LINUX_ONLY = frozenset({pg.GAUGE_THREADS_OS, pg.GAUGE_OPEN_FDS})


def _platform_absent() -> frozenset:
    return frozenset() if os.path.isdir("/proc/self/task") else _LINUX_ONLY


def _not_required() -> frozenset:
    """Instruments a roster assertion must not demand on THIS host."""
    return _HEALTHY_ABSENT | _platform_absent()


#: Sentinel for "keep this probe's real implementation" in a readings override.
_REAL = object()

#: Instruments whose kind carries a temporality (observable counters are Sums).
#: Observable GAUGES must carry none — a temporality on a gauge would mean the
#: exporter is treating a point-in-time reading as an accumulating one.
_COUNTER_NAMES = (
    pg.COUNTER_CPU_SECONDS,
    pg.COUNTER_GC_COLLECTIONS,
    pg.COUNTER_GC_COLLECTED,
    pg.COUNTER_GC_UNCOLLECTABLE,
)


# ---------------------------------------------------------------------------
# tier 1 — real live build, real exporter, records read back off disk
# ---------------------------------------------------------------------------


#: Fixed readings installed over the real probes for the duration of a driven
#: build. What these tiers verify is the SERIALIZATION contract — roster,
#: resource fidelity, attribute enums, temporality — and every probe's own
#: behavior is pinned in ``test_inventory_gauges.py``. Leaving the real probes in
#: place would make these assertions depend on whether the host running them
#: happens to have a knowledge database, any crons, or an MCP roster, which is a
#: flake rather than a contract. Values are distinct from one another so a
#: mixed-up instrument shows as a wrong number rather than a coincidence.
_PINNED_READINGS = {
    "read_active_crons": 3,
    "read_active_monitor_loops": 2,
    "read_installed_skills": 41,
    "read_memory_migrated": 1,
    "read_knowledge_documents": 12,
    "read_lessons": 300,
    "read_mcp_server_classes": {
        ig.MCP_CLASS_FIRST_PARTY: 4,
        ig.MCP_CLASS_THIRD_PARTY: 1,
    },
    "read_config_toggles": {"beacon": 1, "stt": 0},
}


class _Exported:
    """Flattened view of everything one live build wrote."""

    def __init__(self, resources, metrics):
        self.resources = resources
        self.metrics = metrics

    def points(self, name):
        return self.metrics.get(name, {}).get("points", [])

    def temporality(self, name):
        return self.metrics.get(name, {}).get("temporality")

    def attr_values(self, name, key):
        return {p.get("attributes", {}).get(key) for p in self.points(name)}


def _drive_live_build(tmp_path, monkeypatch, readings=None):
    """Enable telemetry, build for real, force one export, read the shards back.

    Patches only the metrics DIRECTORY and the probe readings, so the provider,
    the meter, the gauge registrations and the exporter are all the production
    objects. *readings* maps reader name to its pinned value; a name mapped to
    the sentinel ``_REAL`` keeps its real implementation. Returns the live
    provider's own resource alongside the serialized one, which is what lets the
    fidelity assertion compare them rather than restate a fixed list.
    """
    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    for name, value in (readings if readings is not None else _PINNED_READINGS).items():
        if value is _REAL:
            continue
        monkeypatch.setattr(ig, name, lambda _v=value: _v)

    ig.reset_for_testing()
    # Stand in for the gateway's own claim: install-scoped inventory publishes only
    # from the elected reporter, so without this the roster below would be empty.
    ig.mark_install_reporter()
    pm.reset_for_testing()
    live_resource = None
    try:
        with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
            recorder = pm.get_recorder()
            assert recorder.enabled, "live build did not enable telemetry"
            provider = pm._provider
            assert provider is not None, "live build produced no MeterProvider"
            live_resource = dict(provider._sdk_config.resource.attributes)
            provider.force_flush()
    finally:
        pm.reset_for_testing()
        ig.reset_for_testing()

    shards = sorted(tmp_path.glob("metrics-*.jsonl"))
    assert shards, "the live build wrote no metric shard"

    resources: list[dict] = []
    metrics: dict[str, dict] = {}
    for shard in shards:
        for line in shard.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            for rm in payload.get("resource_metrics") or []:
                resources.append((rm.get("resource") or {}).get("attributes") or {})
                for sm in rm.get("scope_metrics") or []:
                    for metric in sm.get("metrics") or []:
                        data = metric.get("data") or {}
                        entry = metrics.setdefault(
                            metric["name"],
                            {"points": [], "temporality": data.get("aggregation_temporality")},
                        )
                        entry["points"].extend(data.get("data_points") or [])
    return _Exported(resources, metrics), live_resource


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    """One live build shared by the tier-1 assertions.

    Module-scoped because building the provider registers observable instruments
    and forces an export; doing that once and asserting many things about the
    result is both faster and closer to how a real export cycle behaves than
    rebuilding per test.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        yield _drive_live_build(tmp_path_factory.mktemp("otlp-e2e"), mp)
    finally:
        mp.undo()


def test_every_declared_instrument_reaches_the_exporter(exported):
    """The roster contract: nothing declared may be missing from the wire."""
    data, _ = exported
    expected = (set(pg.ALL_METRIC_NAMES) | set(ig.ALL_METRIC_NAMES)) - _not_required()
    missing = sorted(name for name in expected if not data.points(name))
    assert not missing, f"declared but never exported: {missing}"
    for name in _HEALTHY_ABSENT:
        assert not data.points(name), f"{name} must publish nothing on a healthy build"


def test_a_probe_that_cannot_answer_yields_a_gap_not_a_zero(tmp_path, monkeypatch):
    """A None reading must produce NO series, all the way through the exporter.

    The reader-level test proves the callback yields nothing; this proves the
    absence survives to the wire, which is where a fake 0 would actually mislead
    — it would be indistinguishable from a running install with none armed.
    """
    readings = dict(_PINNED_READINGS)
    readings["read_active_monitor_loops"] = None
    data, _ = _drive_live_build(tmp_path, monkeypatch, readings)
    assert not data.points(ig.GAUGE_MONITOR_LOOPS_ACTIVE)
    # The rest of the roster is unaffected — one dark probe is not an outage.
    assert data.points(ig.GAUGE_CRONS_ACTIVE)


def test_resource_attributes_survive_serialization_with_fidelity(exported):
    """Every attribute the live provider set must appear on the wire, unchanged.

    Compared against the provider's OWN resource rather than a hardcoded list, so
    this keeps holding as the resource attribute set grows: an attribute added to
    ``_build_recorder``'s ``Resource`` is covered here the moment it lands, and an
    attribute that the exporter silently drops fails here instead of going
    unnoticed in a backend.
    """
    data, live_resource = exported
    assert live_resource, "the live provider exposed no resource attributes"
    assert data.resources, "no resource block reached the exporter"
    for serialized in data.resources:
        for key, value in live_resource.items():
            assert key in serialized, f"resource attribute {key!r} lost in serialization"
            assert serialized[key] == value, (
                f"resource attribute {key!r} changed on the wire: "
                f"{value!r} -> {serialized[key]!r}"
            )


def test_service_name_identifies_the_product_on_the_wire(exported):
    data, _ = exported
    for serialized in data.resources:
        assert serialized.get("service.name") == "kirocrew"


def test_local_shards_carry_the_process_identity_token(exported):
    """The local sink stamps process identity so counter resets are detectable.

    This is a LOCAL-sink contract: the token is host-local and the exporter adds
    it per record. Pinned here because it is the only thing that makes a
    cumulative counter's reset distinguishable from a decrease within one shard.
    """
    data, _ = exported
    for serialized in data.resources:
        assert RESOURCE_ATTR_PROCESS_START_TIME in serialized


def test_observable_counters_export_cumulative(exported):
    data, _ = exported
    for name in _COUNTER_NAMES:
        assert data.temporality(name) == _CUMULATIVE, (
            f"{name} must export CUMULATIVE: its delta baseline lives in the "
            "provider, which telemetry consent changes rebuild in-process"
        )


def test_observable_gauges_carry_no_temporality(exported):
    data, _ = exported
    for name in ig.ALL_METRIC_NAMES:
        if not data.points(name):
            continue
        assert data.temporality(name) is None, (
            f"{name} is a gauge; a temporality would mean the exporter is "
            "treating a point-in-time reading as accumulating"
        )


def test_inventory_attribute_values_are_the_declared_closed_enums(exported):
    """Attribute VALUES on the wire must be exactly the declared enums."""
    data, _ = exported
    assert data.attr_values(ig.GAUGE_MCP_SERVERS, "class") == {
        ig.MCP_CLASS_FIRST_PARTY,
        ig.MCP_CLASS_THIRD_PARTY,
    }
    assert data.attr_values(ig.GAUGE_CONFIG_TOGGLE, "key") == {"beacon", "stt"}


def test_plain_count_gauges_carry_their_reading(exported):
    data, _ = exported
    assert data.points(ig.GAUGE_CRONS_ACTIVE)[0]["value"] == 3
    assert data.points(ig.GAUGE_MONITOR_LOOPS_ACTIVE)[0]["value"] == 2
    assert data.points(ig.GAUGE_SKILLS_INSTALLED)[0]["value"] == 41
    assert data.points(ig.GAUGE_MEMORY_MIGRATED)[0]["value"] == 1


def test_store_backed_counts_cross_the_wire_unaggregated(exported):
    """The exact reading arrives, under no attribute.

    These two are the counts that could plausibly have been banded at emit time.
    Asserting the raw value here is what makes the shape a contract rather than an
    implementation detail: a reintroduced band would show up as a constant 1 plus
    an attribute, and both halves of that fail below.
    """
    data, _ = exported
    for name, expected in (
        (ig.GAUGE_KNOWLEDGE_DOCUMENTS, _PINNED_READINGS["read_knowledge_documents"]),
        (ig.GAUGE_LESSONS, _PINNED_READINGS["read_lessons"]),
    ):
        points = data.points(name)
        assert points, f"{name} published nothing"
        # One series, not one point: the fixture drives more than one export cycle,
        # so the invariant is that every point shares the single empty attribute set.
        assert {tuple(sorted((p.get("attributes") or {}).items())) for p in points} == {
            ()
        }, f"{name} must be a single attribute-free series, got {points}"
        assert {p["value"] for p in points} == {expected}


def test_config_toggle_values_are_boolean_shaped(exported):
    data, _ = exported
    for point in data.points(ig.GAUGE_CONFIG_TOGGLE):
        assert point["value"] in (0, 1)


def test_no_mcp_server_name_appears_anywhere_on_the_wire(tmp_path, monkeypatch):
    """Privacy ratchet at the serialization boundary, not just at the reader.

    Runs the REAL classification (the one probe left unpinned here) over an
    injected roster carrying a distinctive name, then asserts that name occurs
    nowhere in the exported payload. The reader-level test proves the
    classification discards names; this proves nothing between the reader and the
    wire puts them back.
    """
    secret = "acme-confidential-internal-mcp"
    readings = dict(_PINNED_READINGS)
    readings["read_mcp_server_classes"] = _REAL

    with patch(
        "kiro_crew.mcp_discovery._load_agent_config",
        return_value={"mcpServers": {secret: {}}},
    ):
        with patch("kiro_crew.mcp_discovery._load_mcp_json_by_source", return_value={}):
            data, _ = _drive_live_build(tmp_path, monkeypatch, readings)

    points = data.points(ig.GAUGE_MCP_SERVERS)
    assert points, "the MCP gauge must still publish a count"
    serialized = json.dumps({name: entry["points"] for name, entry in data.metrics.items()})
    assert secret not in serialized, f"MCP server name {secret!r} leaked onto the wire"
    assert secret not in json.dumps(data.resources), "server name leaked into the resource"
    classes = {p["attributes"]["class"]: p["value"] for p in points}
    assert classes.get(ig.MCP_CLASS_THIRD_PARTY) == 1, "the roster was not actually observed"


# ---------------------------------------------------------------------------
# tier 2 — real OTLP/HTTP export into an in-process receiver
# ---------------------------------------------------------------------------


def _otlp_available():
    """Whether the optional OTLP extra and its protobuf decoder are importable."""
    try:
        import opentelemetry.exporter.otlp.proto.http.metric_exporter  # noqa: F401
        from opentelemetry.proto.collector.metrics.v1 import (  # noqa: F401
            metrics_service_pb2,
        )
    except Exception:  # noqa: BLE001
        return False
    return True


requires_otlp = pytest.mark.skipif(
    not _otlp_available(),
    reason='OTLP wire tier needs the optional extra: pip install "kirocrew[otlp]"',
)


class _CollectingHandler(http.server.BaseHTTPRequestHandler):
    """Minimal OTLP/HTTP metrics receiver. Bodies land on the server object."""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's required spelling
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.server.received.append((self.path, body))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()
        self.wfile.write(b"")

    def log_message(self, *args):  # keep pytest output clean
        return


class _Receiver:
    """Loopback-only OTLP receiver, started for the duration of one test."""

    def __init__(self):
        # 127.0.0.1 explicitly: this listens for our own export and must never be
        # reachable off the machine.
        self._server = http.server.HTTPServer(("127.0.0.1", 0), _CollectingHandler)
        self._server.received = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    @property
    def endpoint(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1/metrics"

    @property
    def received(self):
        return self._server.received  # type: ignore[attr-defined]


@requires_otlp
def test_otlp_export_delivers_every_instrument_and_the_resource(monkeypatch, tmp_path):
    """A real OTLP/HTTP POST, decoded from protobuf and asserted.

    Drives the production destination seam (``telemetry.otlp_endpoint`` ->
    ``_build_otlp_reader`` -> ``OTLPMetricExporter``) rather than constructing an
    exporter by hand, so what is verified is the path an operator actually
    configures.
    """
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2

    from kiro_crew.config.loader import KiroCrewConfig

    with _Receiver() as receiver:
        cfg = KiroCrewConfig()
        cfg.telemetry.enabled = True
        cfg.telemetry.otlp_endpoint = receiver.endpoint
        cfg.telemetry.export_interval_seconds = 3600  # only force_flush should export

        monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
        for name, value in _PINNED_READINGS.items():
            monkeypatch.setattr(ig, name, lambda _v=value: _v)
        ig.reset_for_testing()
        ig.mark_install_reporter()  # stand in for the gateway's claim
        pm.reset_for_testing()
        try:
            with patch.object(KiroCrewConfig, "load", return_value=cfg):
                with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
                    recorder = pm.get_recorder()
                    assert recorder.enabled
                    provider = pm._provider
                    assert provider is not None
                    provider.force_flush()
        finally:
            pm.reset_for_testing()
            ig.reset_for_testing()

        assert receiver.received, "no OTLP request reached the receiver"

        names = set()
        resource_keys = set()
        for path, body in receiver.received:
            assert path.endswith("/v1/metrics")
            request = metrics_service_pb2.ExportMetricsServiceRequest()
            request.ParseFromString(body)
            for rm in request.resource_metrics:
                for attr in rm.resource.attributes:
                    resource_keys.add(attr.key)
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        names.add(metric.name)

    assert "service.name" in resource_keys, "resource attributes did not reach the wire"
    # The process-identity token is a LOCAL-sink contract: the JSONL exporter stamps
    # it per record, and it is host-local and reboot-unique, so it must never
    # egress. Tier 1 asserts it IS on the local shard; this asserts the same token
    # is absent from the OTLP payload. Both halves are needed -- either one alone
    # would pass while the attribute sat on the wrong side of the boundary.
    assert RESOURCE_ATTR_PROCESS_START_TIME not in resource_keys, (
        f"{RESOURCE_ATTR_PROCESS_START_TIME} egressed over OTLP; it is a host-local "
        "token stamped by the local exporter only"
    )
    expected = (set(pg.ALL_METRIC_NAMES) | set(ig.ALL_METRIC_NAMES)) - _not_required()
    missing = sorted(expected - names)
    assert not missing, f"instruments absent from the OTLP payload: {missing}"


@requires_otlp
def test_otlp_temporality_preference_is_honored(monkeypatch, tmp_path):
    """``OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`` must reach the exporter.

    Asserted through ``_build_otlp_reader``, not against a hand-built exporter.
    The claim is about OUR builder: it passes no explicit temporality, deliberately,
    so the exporter keeps its own env-var fallback — which is what lets an operator
    match a backend that requires DELTA (Datadog, Statsig) without a code change,
    and what the guide documents. A hand-built exporter would only pin the SDK's
    env handling, leaving an explicit preference added in the builder undetected;
    reaching into the reader the builder returned is what makes that regression
    visible here.
    """
    from opentelemetry.sdk.metrics import Counter

    from kiro_crew.metrics.provider import _build_otlp_reader
    from kiro_crew.platform.interfaces import OtlpDestination

    class _Cfg:
        export_interval_seconds = 60

    def _temporality_for(preference):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", preference)
        dest = OtlpDestination("test", "http://127.0.0.1:1/v1/metrics", frozenset({"metrics"}))
        reader = _build_otlp_reader(dest, _Cfg())
        assert reader is not None, "the extra is installed, so the builder must produce a reader"
        try:
            return reader._exporter._preferred_temporality[Counter].value
        finally:
            reader.shutdown()

    # Both directions, so the assertion cannot pass on a builder that pins one of
    # them: an explicit preference in _build_otlp_reader fails whichever value it
    # is not, and ignoring the env entirely fails one of the two.
    assert (
        _temporality_for("DELTA") == _DELTA
    ), "DELTA not honored: the builder is overriding the exporter's env-var fallback"
    assert (
        _temporality_for("CUMULATIVE") == _CUMULATIVE
    ), "CUMULATIVE not honored: the builder is overriding the exporter's env-var fallback"
