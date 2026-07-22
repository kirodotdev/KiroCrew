"""Tests for kiro_crew.metrics.provider — consent gate + recorder singleton."""

from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics.provider import get_recorder, reset_for_testing


def _patch_config(monkeypatch, **tel_kwargs):
    fake = KiroCrewConfig(telemetry=TelemetryConfig(**tel_kwargs))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake))


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
