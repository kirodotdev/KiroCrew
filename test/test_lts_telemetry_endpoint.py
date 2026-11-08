"""LTS build: the policy supplies the OTLP endpoint (``capabilities.telemetry.otlp_endpoint``).

RFC ``rfc-crew-lts-edition``, appendix row "Diagnostic telemetry target". In an
LTS build the policy names the collector, the metrics path builds a reader
for it, and local config or environment cannot change it. With no endpoint in the
policy nothing is sent. The ``oss`` edition does not read the key: a policy that
carries it is refused as an unknown key, as before.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from kiro_crew import edition_locks
from kiro_crew.config.sections_telemetry import TelemetryConfig
from kiro_crew.metrics import provider as metrics_provider
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.defaults import DefaultTelemetryProvider
from kiro_crew.platform.governance import (
    TIER_CENTRAL,
    TIER_HOME,
    compose_tier_ladder,
    parse_policy,
    parse_profile,
    telemetry_otlp_endpoint,
)

ADMIN = "https://otel.corp.example/v1/metrics"
LOCAL = "http://collector.example:4318/v1/metrics"
#: The same host as ADMIN, so the policy's host list cannot tell them apart.
MOVED = "https://otel.corp.example/v2/metrics"


def _policy(**telemetry: object) -> dict:
    return {"version": 1, "boot": {"fail_closed": True}, "capabilities": {"telemetry": telemetry}}


@pytest.fixture(autouse=True)
def _fresh_lock_log():
    edition_locks.reset_logged()
    yield
    edition_locks.reset_logged()


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture
def fake_sel(monkeypatch):
    from unittest.mock import MagicMock

    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return fake


def _install(monkeypatch, doc: "dict | None") -> SimpleNamespace:
    """Install *doc* as the context's ceiling and the public default provider."""
    from kiro_crew.platform import context as pc

    ctx = SimpleNamespace(governance=parse_policy(doc) if doc is not None else None)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    monkeypatch.setattr(metrics_provider, "current_context", lambda: ctx)
    monkeypatch.setattr(metrics_provider, "_telemetry_provider", DefaultTelemetryProvider)
    return ctx


# ── The policy key ──────────────────────────────────────────────────────


def test_enterprise_policy_supplies_the_endpoint(enterprise) -> None:
    ceiling = parse_policy(_policy(enabled=True, otlp_endpoint=ADMIN))
    assert telemetry_otlp_endpoint(ceiling) == ADMIN


def test_the_flat_scope_key_supplies_the_endpoint_too(enterprise) -> None:
    flat = {"version": 1, "boot": {"fail_closed": True}}
    flat["capabilities.telemetry"] = {"enabled": True, "otlp_endpoint": ADMIN}
    assert telemetry_otlp_endpoint(parse_policy(flat)) == ADMIN
    both = {
        **_policy(enabled=True, otlp_endpoint=MOVED),
        "capabilities.telemetry": {"otlp_endpoint": ADMIN},
    }
    with pytest.raises(PlatformCompositionError, match="set twice"):
        parse_policy(both)


def test_endpoint_is_empty_while_telemetry_is_off(enterprise) -> None:
    assert telemetry_otlp_endpoint(parse_policy(_policy(enabled=False, otlp_endpoint=ADMIN))) == ""
    assert telemetry_otlp_endpoint(None) == ""


def test_an_unusable_endpoint_fails_the_load(enterprise) -> None:
    with pytest.raises(PlatformCompositionError, match="otlp_endpoint"):
        parse_policy(_policy(enabled=True, otlp_endpoint="ftp://otel.corp.example"))


def test_a_profile_cannot_carry_the_endpoint(enterprise) -> None:
    with pytest.raises(PlatformCompositionError, match="policy-only"):
        parse_profile({"name": "p", "capabilities": {"telemetry": {"otlp_endpoint": ADMIN}}})


def test_oss_refuses_the_key_as_before() -> None:
    with pytest.raises(PlatformCompositionError, match="unknown key"):
        parse_policy(_policy(enabled=True, otlp_endpoint=ADMIN))


def test_only_the_central_tier_supplies_the_endpoint(enterprise) -> None:
    central = replace(parse_policy(_policy(enabled=True)), tier=TIER_CENTRAL)
    home = replace(parse_policy(_policy(enabled=True, otlp_endpoint=LOCAL)), tier=TIER_HOME)
    assert telemetry_otlp_endpoint(compose_tier_ladder(central, home)) == ""
    named = replace(parse_policy(_policy(enabled=True, otlp_endpoint=ADMIN)), tier=TIER_CENTRAL)
    assert telemetry_otlp_endpoint(compose_tier_ladder(named, home)) == ADMIN
    # A local file alone cannot turn telemetry on in an LTS build (RFC D3).
    assert telemetry_otlp_endpoint(compose_tier_ladder(home)) == ""


# ── The metrics path ────────────────────────────────────────────────────


def test_the_policy_endpoint_gets_the_only_reader(enterprise, fake_sel, monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", LOCAL)
    _install(monkeypatch, _policy(enabled=True, otlp_endpoint=ADMIN))
    cfg = TelemetryConfig(otlp_endpoint=LOCAL, otlp_endpoints=[{"name": "ops", "endpoint": LOCAL}])
    dests = metrics_provider._otlp_destinations(cfg)
    assert [(d.name, d.endpoint) for d in dests] == [("policy", ADMIN)]
    # No process-wide OTel header or credential variable reaches it.
    assert dests[0].process_env_credentials is False
    rows = metrics_provider.otlp_egress_listing(cfg)
    assert [(r.destination.name, r.source, r.locked) for r in rows] == [
        ("policy", "provider", True)
    ]


def test_no_policy_endpoint_sends_nothing(enterprise, fake_sel, monkeypatch) -> None:
    _install(monkeypatch, _policy(enabled=True))
    cfg = TelemetryConfig(otlp_endpoint=LOCAL)
    assert metrics_provider._otlp_destinations(cfg) == ()
    assert metrics_provider.otlp_egress_active(cfg) is False


def test_the_policy_host_list_still_applies(enterprise, fake_sel, monkeypatch) -> None:
    hosts = {"otlp_hosts": {"mode": "allow", "allow": ["other.corp.example"]}}
    _install(monkeypatch, _policy(enabled=True, otlp_endpoint=ADMIN, scopes=hosts))
    assert metrics_provider._otlp_destinations(TelemetryConfig()) == ()


def test_oss_local_endpoint_still_applies(fake_sel, monkeypatch) -> None:
    _install(monkeypatch, None)
    dests = metrics_provider._otlp_destinations(TelemetryConfig(otlp_endpoint=LOCAL))
    assert [(d.name, d.endpoint) for d in dests] == [("telemetry.otlp_endpoint", LOCAL)]


def test_a_provider_without_destinations_still_lists_the_policy_collector(
    enterprise, fake_sel, monkeypatch
) -> None:
    _install(monkeypatch, _policy(enabled=True, otlp_endpoint=ADMIN))
    monkeypatch.setattr(metrics_provider, "_telemetry_provider", lambda: SimpleNamespace())
    cfg = TelemetryConfig()
    assert [d.name for d in metrics_provider._otlp_destinations(cfg)] == ["policy"]
    assert [r.destination.name for r in metrics_provider.otlp_egress_listing(cfg)] == ["policy"]
    assert metrics_provider.otlp_egress_active(cfg) is True


# ── A policy that moves its collector while a reader runs ───────────────


def test_a_moved_policy_endpoint_changes_the_rebuild_fingerprint(enterprise, monkeypatch) -> None:
    ctx = _install(monkeypatch, _policy(enabled=True, otlp_endpoint=ADMIN))
    before = metrics_provider._endpoints_key(TelemetryConfig())
    ctx.governance = parse_policy(_policy(enabled=True, otlp_endpoint=MOVED))
    assert metrics_provider._endpoints_key(TelemetryConfig()) != before


def test_a_running_policy_reader_stops_when_the_policy_moves_its_collector(
    enterprise, fake_sel, monkeypatch
) -> None:
    exporter_mod = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    from opentelemetry.sdk.metrics.export import MetricExportResult

    OTLPMetricExporter = exporter_mod.OTLPMetricExporter

    sent: list = []
    monkeypatch.setattr(
        OTLPMetricExporter,
        "export",
        lambda self, data, *a, **k: sent.append(data) or MetricExportResult.SUCCESS,
    )
    ctx = _install(monkeypatch, _policy(enabled=True, otlp_endpoint=ADMIN))
    (dest,) = metrics_provider._otlp_destinations(TelemetryConfig())
    reader = metrics_provider._build_otlp_reader(dest, TelemetryConfig())
    assert reader is not None
    try:
        exporter = reader._exporter
        assert exporter.export("batch-1") is MetricExportResult.SUCCESS
        ctx.governance = parse_policy(_policy(enabled=True, otlp_endpoint=MOVED))
        assert exporter.export("batch-2") is MetricExportResult.FAILURE
        ctx.governance = parse_policy(_policy(enabled=True))
        assert exporter.export("batch-3") is MetricExportResult.FAILURE
        assert sent == ["batch-1"]
    finally:
        reader.shutdown()
    rows = [c[1] for c in fake_sel.log_governance_decision.call_args_list]
    assert ("otlp_hosts:otel.corp.example", "denied") in [(r["item"], r["outcome"]) for r in rows]


def test_a_process_without_a_context_follows_the_live_policy(
    enterprise, fake_sel, monkeypatch
) -> None:
    """The MCP gateway daemon on core defaults: no context, no companion."""
    exporter_mod = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    from opentelemetry.sdk.metrics.export import MetricExportResult

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import bootstrap

    def no_context():
        raise PlatformCompositionError("no context installed")

    sent: list = []
    monkeypatch.setattr(
        exporter_mod.OTLPMetricExporter,
        "export",
        lambda self, data, *a, **k: sent.append(data) or MetricExportResult.SUCCESS,
    )
    monkeypatch.setattr(metrics_provider, "current_context", no_context)
    monkeypatch.setattr(bootstrap, "plugin_entry_points", lambda: [])
    monkeypatch.setattr(bootstrap, "_LIVE_POLICY_TTL_SECS", 0.0)
    policy = {"now": parse_policy(_policy(enabled=True, otlp_endpoint=ADMIN))}
    monkeypatch.setattr(bootstrap, "load_security_policy", lambda: policy["now"])
    metrics_provider.use_telemetry_provider(
        lambda: bootstrap.compose_telemetry_provider(KiroCrewConfig())
    )
    reader = None
    try:
        cfg = TelemetryConfig(otlp_endpoint=LOCAL)
        (dest,) = metrics_provider._otlp_destinations(cfg)
        assert (dest.name, dest.endpoint) == ("policy", ADMIN)
        key = metrics_provider._endpoints_key(cfg)
        reader = metrics_provider._build_otlp_reader(dest, cfg)
        assert reader is not None
        assert reader._exporter.export("batch-1") is MetricExportResult.SUCCESS
        # The admin turns telemetry off: the next export stops, and the rebuild
        # fingerprint moves so the consent worker drops the reader.
        policy["now"] = parse_policy(_policy(enabled=False))
        assert reader._exporter.export("batch-2") is MetricExportResult.FAILURE
        assert metrics_provider._endpoints_key(cfg) != key
        assert sent == ["batch-1"]
    finally:
        if reader is not None:
            reader.shutdown()
        metrics_provider.use_telemetry_provider(None)


def test_a_collector_added_during_a_build_is_picked_up_on_the_recheck(
    enterprise, fake_sel, monkeypatch, tmp_path
) -> None:
    """A policy refresh lands between resolving the destinations and recording the key."""
    from unittest.mock import patch

    from kiro_crew.config.loader import KiroCrewConfig

    # The suite pins collection off by env; this test needs it on.
    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
    ctx = _install(monkeypatch, _policy(enabled=True))
    cfg = KiroCrewConfig(
        telemetry=TelemetryConfig(
            enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    built: list = []
    monkeypatch.setattr(
        metrics_provider, "_build_otlp_reader", lambda d, c: built.append(d.endpoint) or None
    )
    real = metrics_provider._otlp_destinations

    def refresh_mid_build(*args):
        dests = real(*args)
        ctx.governance = parse_policy(_policy(enabled=True, otlp_endpoint=ADMIN))
        return dests

    monkeypatch.setattr(metrics_provider, "_otlp_destinations", refresh_mid_build)
    metrics_provider.reset_for_testing()
    try:
        with patch.object(metrics_provider, "_default_metrics_dir", return_value=tmp_path):
            metrics_provider.get_recorder()
            assert built == []
            monkeypatch.setattr(metrics_provider, "_otlp_destinations", real)
            metrics_provider._consent_worker(metrics_provider._build_generation)
            assert built == [ADMIN]
    finally:
        metrics_provider.reset_for_testing()


def test_a_policy_that_starts_permitting_the_host_gets_a_reader_on_the_recheck(
    enterprise, fake_sel, monkeypatch, tmp_path
) -> None:
    from unittest.mock import patch

    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
    deny = {"otlp_hosts": {"mode": "allow", "allow": ["other.corp.example"]}}
    ctx = _install(monkeypatch, _policy(enabled=True, otlp_endpoint=ADMIN, scopes=deny))
    cfg = KiroCrewConfig(
        telemetry=TelemetryConfig(
            enabled=True, local_dir=str(tmp_path), export_interval_seconds=3600
        )
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    built: list = []
    monkeypatch.setattr(
        metrics_provider, "_build_otlp_reader", lambda d, c: built.append(d.endpoint) or None
    )
    metrics_provider.reset_for_testing()
    try:
        with patch.object(metrics_provider, "_default_metrics_dir", return_value=tmp_path):
            metrics_provider.get_recorder()
            assert built == []
            ctx.governance = parse_policy(_policy(enabled=True, otlp_endpoint=ADMIN))
            metrics_provider._consent_worker(metrics_provider._build_generation)
            assert built == [ADMIN]
    finally:
        metrics_provider.reset_for_testing()
