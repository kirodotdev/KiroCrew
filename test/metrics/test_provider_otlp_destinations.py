"""Tests for the pluggable OTLP metric-egress seam.

``TelemetryProvider.otlp_destinations`` lets a deployment say WHERE metrics go —
including to a collector whose credential rotates during process lifetime, which
the once-at-construction ``OTEL_EXPORTER_OTLP_HEADERS`` injection cannot express.
The core keeps WHAT may leave: the consent gate, attribute sanitisation, the
histogram views, the export cadence, and the local JSONL sink that an edition can
never remove.

These assert PROPERTIES rather than examples: egress stays off by default in what
ships, provider-supplied readers join the same reaping list as the built-in one,
and every way a provider can misbehave (empty, raising, half-populated, aimed at
a signal this core does not emit) leaves the local reader working.
"""

from __future__ import annotations

import dataclasses
import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics.provider import get_recorder, reset_for_testing
from kiro_crew.platform import build_default_context
from kiro_crew.platform.context import (
    PlatformCompositionError,
    reset_context,
    set_context,
)
from kiro_crew.platform.interfaces import OtlpDestination

ENDPOINT = "https://collector.example.internal:4318/v1/metrics"


def _patch_config(monkeypatch, **tel_kwargs):
    fake = KiroCrewConfig(telemetry=TelemetryConfig(**tel_kwargs))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake))
    monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _StubProvider:
    """A TelemetryProvider whose only job is to answer otlp_destinations."""

    def __init__(self, destinations=(), raises=None):
        self._destinations = destinations
        self._raises = raises

    def record_event(self, event_type: str, data: dict) -> None:
        return None

    def frontend_rum_config(self):
        return None

    def otlp_destinations(self, cfg):
        if self._raises is not None:
            raise self._raises
        return self._destinations


def _install(provider):
    """Install a PlatformContext whose telemetry slot is *provider*."""
    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, telemetry=provider))


@pytest.fixture(autouse=True)
def _clean_context():
    # Reset BEFORE as well as after: get_recorder() caches its recorder module-
    # globally behind a recheck window, so a recorder built by an earlier test in
    # this worker would be handed back without re-reading the patched config -
    # which is how this file flaked under xdist before the leading reset.
    reset_for_testing()
    yield
    reset_context()
    reset_for_testing()


def _fake_readers(monkeypatch, *, fail_on=None):
    """Patch the CONSUMER module's reader/provider globals.

    Patched on ``kiro_crew.metrics.provider`` where the names are looked up, not
    on ``opentelemetry.*`` that defines them — the module binds them as globals
    precisely so a stand-in survives the lazy SDK load.
    """
    import kiro_crew.metrics.provider as provider_mod

    built: list = []
    shut: list = []
    captured: dict = {}

    class FakeReader:
        def __init__(self, *a, **k):
            built.append(self)
            self.index = len(built)
            if fail_on is not None and self.index == fail_on:
                raise RuntimeError(f"reader {self.index} construction failed")

        def shutdown(self, *a, **k):
            shut.append(self.index)

    class FakeMeterProvider:
        def __init__(self, *, metric_readers, resource=None, views=None):
            captured["readers"] = list(metric_readers)

        def get_meter(self, *a, **k):
            return MagicMock()

        def shutdown(self, *a, **k):
            return None

        def force_flush(self, *a, **k):
            return True

    monkeypatch.setattr(provider_mod, "PeriodicExportingMetricReader", FakeReader)
    monkeypatch.setattr(provider_mod, "MeterProvider", FakeMeterProvider)
    # The local sink's exporter is irrelevant here; keep it out of the filesystem.
    monkeypatch.setattr(provider_mod, "JsonlMetricExporter", lambda *a, **k: MagicMock())
    return built, shut, captured


def _stub_otlp_readers(monkeypatch):
    """Make each destination yield one reader, recording the destinations seen."""
    import kiro_crew.metrics.provider as provider_mod

    seen: list = []

    def _build(dest, cfg):
        seen.append(dest)
        return provider_mod.PeriodicExportingMetricReader()

    monkeypatch.setattr(provider_mod, "_build_otlp_reader", _build)
    return seen


class TestAttachment:
    def test_supplied_destinations_are_attached_to_the_meter_provider(self, tmp_path, monkeypatch):
        """Every metrics destination becomes a reader on the MeterProvider, and
        the local sink stays FIRST — an edition adds destinations, it never
        replaces the sink the dashboard reads."""
        _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))
        built, _shut, captured = _fake_readers(monkeypatch)
        seen = _stub_otlp_readers(monkeypatch)
        _install(
            _StubProvider(
                (
                    OtlpDestination("primary", ENDPOINT, frozenset({"metrics"})),
                    OtlpDestination("secondary", ENDPOINT + "/2", frozenset({"metrics"})),
                )
            )
        )

        assert get_recorder().enabled is True
        assert len(captured["readers"]) == 3, "local sink + one reader per destination"
        assert captured["readers"][0] is built[0], "the local sink is attached first"
        assert [d.name for d in seen] == ["primary", "secondary"]

    def test_the_export_cadence_stays_a_core_decision(self, tmp_path, monkeypatch):
        """A destination says where to send, not how often: the reader's interval
        comes from telemetry.export_interval_seconds, not from the descriptor."""
        import sys
        import types

        from kiro_crew.metrics.provider import _build_otlp_reader

        captured: dict = {}

        class _StubExporter:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http.metric_exporter")
        mod.OTLPMetricExporter = _StubExporter  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules,
            "opentelemetry.exporter.otlp.proto.http.metric_exporter",
            mod,
        )
        import kiro_crew.metrics.provider as provider_mod

        seen: dict = {}

        class FakeReader:
            def __init__(self, exporter, export_interval_millis=None):
                seen["interval"] = export_interval_millis

        monkeypatch.setattr(provider_mod, "PeriodicExportingMetricReader", FakeReader)

        cfg = TelemetryConfig(enabled=True, export_interval_seconds=17)
        _build_otlp_reader(OtlpDestination("d", ENDPOINT, frozenset({"metrics"})), cfg)
        assert seen["interval"] == 17_000.0


class TestTemporalityIsACoreDecision:
    """Both of a MeterProvider's destinations must encode the same instruments
    the same way — unless the operator says otherwise.

    These run WITHOUT the ``otlp`` extra (the exporter module is stubbed), which
    is the point: the end-to-end tier in ``test_otlp_wire_e2e.py`` needs the
    optional extra and is skipped wherever it is absent, so the divergence this
    class pins could reach main unnoticed. What is asserted here is the ONE
    decision the builder makes; the tier-2 tests assert the exporter honors it.
    """

    @staticmethod
    def _captured_kwargs(monkeypatch):
        """Build one reader against a stub exporter and return its kwargs."""
        import sys
        import types

        from kiro_crew.metrics.provider import _build_otlp_reader

        captured: dict = {}

        class _StubExporter:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http.metric_exporter")
        mod.OTLPMetricExporter = _StubExporter  # type: ignore[attr-defined]
        monkeypatch.setitem(
            sys.modules,
            "opentelemetry.exporter.otlp.proto.http.metric_exporter",
            mod,
        )
        import kiro_crew.metrics.provider as provider_mod

        class FakeReader:
            def __init__(self, exporter, export_interval_millis=None):
                pass

        monkeypatch.setattr(provider_mod, "PeriodicExportingMetricReader", FakeReader)
        _build_otlp_reader(
            OtlpDestination("d", ENDPOINT, frozenset({"metrics"})),
            TelemetryConfig(enabled=True),
        )
        return captured

    def test_the_otlp_leg_gets_the_map_the_local_sink_uses(self, tmp_path, monkeypatch):
        """The defect: the OTLP exporter was built with endpoint/session/interval
        and nothing else, so it kept the SDK's CUMULATIVE default while the local
        sink asked for DELTA. Compared against the local exporter's RESOLVED map
        rather than against a literal, so a change to one sink cannot pass here
        while leaving the other behind."""
        from kiro_crew.metrics.local_exporter import JsonlMetricExporter
        from kiro_crew.metrics.temporality import TEMPORALITY_ENV_VAR

        monkeypatch.delenv(TEMPORALITY_ENV_VAR, raising=False)
        captured = self._captured_kwargs(monkeypatch)

        assert "preferred_temporality" in captured, (
            "no temporality reached the OTLP exporter, so it falls back to "
            "CUMULATIVE while the local sink uses DELTA"
        )
        local = JsonlMetricExporter(tmp_path)._preferred_temporality
        for kind, temporality in captured["preferred_temporality"].items():
            assert local[kind] == temporality, f"{kind.__name__} disagrees with the local sink"

    def test_delta_is_what_both_sinks_prefer(self, monkeypatch):
        """Named explicitly: DELTA is the direction, not merely "the same as the
        other one". A cumulative histogram re-ships every bucket of every series
        every cycle whether or not anything happened; an idle DELTA series sends
        nothing."""
        from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
        from opentelemetry.sdk.metrics.export import AggregationTemporality

        from kiro_crew.metrics.temporality import TEMPORALITY_ENV_VAR

        monkeypatch.delenv(TEMPORALITY_ENV_VAR, raising=False)
        preference = self._captured_kwargs(monkeypatch)["preferred_temporality"]

        assert preference == {
            Counter: AggregationTemporality.DELTA,
            UpDownCounter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
        }

    def test_an_operator_preference_is_left_to_the_exporter(self, monkeypatch):
        """The escape hatch stays an escape hatch. The exporter applies a passed
        dict ON TOP of whichever base the env var chose, so a builder that always
        passes one would override exactly the operator who asked for CUMULATIVE.
        Passing nothing is what hands every instrument kind back at once."""
        from kiro_crew.metrics.temporality import TEMPORALITY_ENV_VAR

        for preference in ("CUMULATIVE", "DELTA", "LOWMEMORY", "  delta  "):
            monkeypatch.setenv(TEMPORALITY_ENV_VAR, preference)
            captured = self._captured_kwargs(monkeypatch)
            assert (
                "preferred_temporality" not in captured
            ), f"{TEMPORALITY_ENV_VAR}={preference!r} was overridden by our own map"

    def test_a_blank_variable_is_not_a_preference(self, monkeypatch):
        """An exported-but-empty variable is how a shell wrapper spells "unset".
        Reading it as a decision would silently restore the CUMULATIVE default."""
        from kiro_crew.metrics.temporality import TEMPORALITY_ENV_VAR

        monkeypatch.setenv(TEMPORALITY_ENV_VAR, "   ")
        assert "preferred_temporality" in self._captured_kwargs(monkeypatch)


class TestDegradation:
    """Every way a provider can fail must leave local collection working."""

    def test_an_empty_sequence_is_local_only(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))
        _built, _shut, captured = _fake_readers(monkeypatch)
        _install(_StubProvider(()))

        assert get_recorder().enabled is True
        assert len(captured["readers"]) == 1, "local sink only, no egress"

    def test_a_raising_provider_is_local_only(self, tmp_path, monkeypatch, caplog):
        """A provider that raises contributes no destinations and says so at
        WARNING — not debug. A default install collects WARNING and above, so a
        debug-only line would make a broken provider an undiagnosable no-op."""
        import logging

        _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))
        _built, _shut, captured = _fake_readers(monkeypatch)
        _install(_StubProvider(raises=RuntimeError("provider exploded")))

        with caplog.at_level(logging.WARNING):
            assert get_recorder().enabled is True
        assert len(captured["readers"]) == 1
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "otlp egress disabled" in r.getMessage().lower() for r in warnings
        ), f"expected a WARNING naming the degrade, got {[r.getMessage() for r in warnings]}"

    def test_a_composition_failure_is_local_only(self, tmp_path, monkeypatch, caplog):
        """For an EGRESS seam the closed state is 'no destinations', so a host
        that cannot compose its companion degrades to local-only rather than
        turning a metric call into a raise. Deliberate, and documented at the
        call site: degrading here can only REMOVE egress, never add any."""
        _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))
        _built, _shut, captured = _fake_readers(monkeypatch)
        _install(_StubProvider(raises=PlatformCompositionError("no companion")))

        assert get_recorder().enabled is True
        assert len(captured["readers"]) == 1
        assert "platform context unavailable" in caplog.text.lower()

    def test_a_destination_for_another_signal_is_not_built(self, tmp_path, monkeypatch):
        """A traces-only destination contributes no METRIC reader. This is what
        makes the descriptor safe to reuse when the core starts emitting other
        signals: adding a signal must not silently start exporting it."""
        _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))
        _built, _shut, captured = _fake_readers(monkeypatch)
        seen = _stub_otlp_readers(monkeypatch)
        _install(_StubProvider((OtlpDestination("traces-only", ENDPOINT, frozenset({"traces"})),)))

        assert get_recorder().enabled is True
        assert len(captured["readers"]) == 1
        assert seen == []

    def test_a_destination_without_an_endpoint_is_dropped(self, tmp_path, monkeypatch):
        """Deny-by-default: a half-populated descriptor is dropped, never coerced
        into an egress nobody asked for."""
        _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))
        _built, _shut, captured = _fake_readers(monkeypatch)
        seen = _stub_otlp_readers(monkeypatch)
        _install(
            _StubProvider(
                (
                    OtlpDestination("blank", "", frozenset({"metrics"})),
                    OtlpDestination("whitespace", "   ", frozenset({"metrics"})),
                )
            )
        )

        assert get_recorder().enabled is True
        assert len(captured["readers"]) == 1
        assert seen == []


class TestEgressPostureIsSingleSourced:
    """The disclosure surfaces must answer from the RESOLVED destination set.

    Before this seam, "is egress on?" had exactly one answer:
    ``telemetry.otlp_endpoint``. An edition that supplies its own collector makes
    that key an unreliable proxy — so the Privacy panel's ``otlp_configured`` and
    the config route's refusal to ENABLE collection both have to ask the same
    resolver ``_build_recorder`` uses. Otherwise the panel reports "nothing is
    exported" while metrics leave the machine: two answers to one consent
    question, which is precisely the defect this seam must not introduce.
    """

    def test_a_provider_destination_counts_as_egress_with_no_config_endpoint(self):
        from kiro_crew.metrics.provider import otlp_egress_active

        cfg = TelemetryConfig(enabled=True)  # otlp_endpoint deliberately EMPTY
        assert otlp_egress_active(cfg) is False, "default provider: no destination"

        _install(
            _StubProvider((OtlpDestination("edition-collector", ENDPOINT, frozenset({"metrics"})),))
        )
        assert otlp_egress_active(cfg) is True, (
            "an edition-supplied destination IS egress, even with an empty "
            "telemetry.otlp_endpoint"
        )

    def test_posture_ignores_a_destination_for_another_signal(self):
        from kiro_crew.metrics.provider import otlp_egress_active

        _install(_StubProvider((OtlpDestination("traces-only", ENDPOINT, frozenset({"traces"})),)))
        assert otlp_egress_active(TelemetryConfig(enabled=True)) is False

    def test_a_provider_without_the_method_answers_no_not_unknown(self, monkeypatch):
        """An edition built BEFORE this seam composes fine (structural matching,
        and the contract assertion only compares a version integer). It has no
        method, which is a KNOWN answer: no destinations. Reading it as "unknown"
        would report egress that provably cannot happen and brick the dashboard
        enable switch, while the build path on the same host is local-only - the
        two surfaces disagreeing is the defect this seam exists to remove."""
        from kiro_crew.dashboard.handlers import telemetry as telemetry_handlers
        from kiro_crew.metrics.provider import _otlp_destinations, otlp_egress_active

        class _StaleProvider:
            def record_event(self, event_type, data):
                return None

            def frontend_rum_config(self):
                return None

        cfg = TelemetryConfig(enabled=True)
        _patch_config(monkeypatch, enabled=True)
        _install(_StaleProvider())

        assert otlp_egress_active(cfg) is False
        assert _otlp_destinations(cfg) == (), "build path agrees: local-only"
        assert telemetry_handlers._telemetry_cfg().otlp_configured is False

    def test_an_attribute_error_from_inside_the_method_still_raises(self):
        """Only an ABSENT attribute is read as no-destinations. A provider whose
        implementation itself raises AttributeError is a broken provider, not a
        stale one, so posture stays unknown and the gate stays closed."""
        from kiro_crew.metrics.provider import otlp_egress_active

        _install(_StubProvider(raises=AttributeError("typo inside the provider")))
        with pytest.raises(AttributeError):
            otlp_egress_active(TelemetryConfig(enabled=True))

    def test_an_unresolvable_provider_makes_posture_raise_not_answer_no(self):
        """The gate's closed direction is the OPPOSITE of the build path's.

        At build time an unresolvable provider must not export, so
        _otlp_destinations degrades to (). At gate time it must not read as "no
        egress": a caller that saw False would permit an enable that the
        recovered provider turns into egress. So posture raises and the callers
        decide.
        """
        from kiro_crew.metrics.provider import _otlp_destinations, otlp_egress_active

        cfg = TelemetryConfig(enabled=True)
        _install(_StubProvider(raises=RuntimeError("provider exploded")))

        with pytest.raises(RuntimeError):
            otlp_egress_active(cfg)
        # Same provider, build path: lenient, so local collection survives.
        assert _otlp_destinations(cfg) == ()

    def test_the_panel_reports_egress_when_posture_is_unresolvable(self, monkeypatch):
        """A disclosure surface fails closed by assuming it EXPORTS - promising
        local-only on an unresolved posture is the lie this must not tell."""
        from kiro_crew.dashboard.handlers import telemetry as telemetry_handlers

        _patch_config(monkeypatch, enabled=True)
        _install(_StubProvider(raises=RuntimeError("provider exploded")))
        assert telemetry_handlers._telemetry_cfg().otlp_configured is True

    def test_the_privacy_panel_reports_the_resolved_posture(self, monkeypatch):
        """otlp_configured is what the Settings → Privacy panel renders. The
        enforcement half — the config route refusing to ENABLE collection on such
        a host — is pinned at the route itself, in
        test/test_config_patch.py::TestTelemetryEnabledEgressGate."""
        from kiro_crew.dashboard.handlers import telemetry as telemetry_handlers

        _patch_config(monkeypatch, enabled=True)  # empty otlp_endpoint
        _install(
            _StubProvider((OtlpDestination("edition-collector", ENDPOINT, frozenset({"metrics"})),))
        )
        assert telemetry_handlers._telemetry_cfg().otlp_configured is True


class TestReaping:
    def test_a_later_reader_failure_leaves_no_earlier_reader_running(self, tmp_path, monkeypatch):
        """Provider-supplied readers join the SAME reaping list as the built-in
        one. PeriodicExportingMetricReader starts its ticker inside __init__, so
        a third reader that raises must not leave the first two ticking while
        telemetry reports itself disabled."""
        _patch_config(monkeypatch, enabled=True, local_dir=str(tmp_path))
        built, shut, _captured = _fake_readers(monkeypatch, fail_on=3)
        _stub_otlp_readers(monkeypatch)
        _install(
            _StubProvider(
                (
                    OtlpDestination("first", ENDPOINT, frozenset({"metrics"})),
                    OtlpDestination("second", ENDPOINT + "/2", frozenset({"metrics"})),
                )
            )
        )

        assert get_recorder().enabled is False, "degraded to a no-op recorder"
        assert _wait_for(
            lambda: sorted(shut) == [1, 2]
        ), f"expected readers 1 and 2 reaped, got {sorted(shut)}"
        assert len(built) == 3, "the third reader raised inside its constructor"
