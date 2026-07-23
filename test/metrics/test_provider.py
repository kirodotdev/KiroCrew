"""Tests for kiro_crew.metrics.provider — consent gate + recorder singleton."""

from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics.provider import get_recorder, reset_for_testing


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

    mod = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.http.metric_exporter"
    )
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
    _patch_config(monkeypatch, enabled=False, local_dir=str(tmp_path),
                  export_interval_seconds=3600)
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

    mod = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.http.metric_exporter"
    )
    mod.OTLPMetricExporter = _StubOTLPMetricExporter  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        mod,
    )

    cfg = TelemetryConfig(
        enabled=True, otlp_endpoint="http://localhost:4318/v1/metrics"
    )
    reader = _build_otlp_reader(cfg)
    assert reader is not None, "opt-in endpoint must build an OTLP reader"
    assert captured["endpoint"] == "http://localhost:4318/v1/metrics"
    # Clean shutdown so the reader's daemon thread doesn't linger.
    try:
        reader.shutdown()
    except Exception:
        pass
