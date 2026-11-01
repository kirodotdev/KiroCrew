"""``telemetry.otlp_endpoints``: a list of OTLP destinations, plus edition rows.

What already worked and must keep working, each pinned by a test here or in the
files it names:

* ``telemetry.otlp_endpoint`` alone yields ONE destination, byte-identical to the
  descriptor it produced before the list existed (``TestLegacyString``).
* No setting at all means no destination, so no egress (``TestLegacyString``).
* Every destination goes through the consent gate, the policy ``otlp_hosts``
  check and the enterprise lock on local values (``TestGates``).
* The Privacy panel and the enable gate answer from the same resolution as the
  build (``TestDisclosure``, and ``test_provider_otlp_destinations.py``).

The wire tests at the bottom drive the real build against a loopback receiver:
two entries, the second one narrowed, with a header read from the environment;
and the process-wide OTel header variable reaching the legacy entry only.
"""

from __future__ import annotations

import dataclasses
import gzip
import http.server
import socketserver
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics import otlp_endpoints as oe
from kiro_crew.metrics import provider as pm
from kiro_crew.metrics.product_metrics import PRODUCT_METRICS
from kiro_crew.platform.defaults import DefaultTelemetryProvider
from kiro_crew.platform.interfaces import OtlpDestination

OPS = "https://collector.example.com/v1/metrics"
EDITION = "https://telemetry.example.net/v1/metrics"

#: A collector a build ships: returned by its provider's code, not from config.
#: Delta, the product preset and a label, as the guide's example describes.
BUILD_DEST = OtlpDestination(
    "edition-product",
    EDITION,
    frozenset({"metrics"}),
    allow=PRODUCT_METRICS,
    point_attributes={"ClientType": "desktop"},
    temporality="delta",
)


@pytest.fixture(autouse=True)
def _fresh(_floor_monkeypatch):
    oe.reset_warnings()
    pm.reset_for_testing()
    _floor_monkeypatch.delenv("KIROCREW_TELEMETRY", raising=False)
    yield
    pm.reset_for_testing()
    oe.reset_warnings()


def _provider(monkeypatch, shipped=()):
    """Point ``metrics.provider`` at a provider that keeps the operator's entries
    and adds *shipped*, the way a build that ships a collector does."""
    prov = SimpleNamespace(
        otlp_destinations=lambda cfg: oe.config_destinations(cfg) + tuple(shipped),
    )
    monkeypatch.setattr(pm, "_telemetry_provider", lambda: prov)
    monkeypatch.setattr(pm, "_policy_ceiling", lambda: None)
    return prov


# ── the legacy string keeps working exactly as before ───────────────────────


class TestLegacyString:
    def test_the_legacy_string_alone_is_the_descriptor_it_always_was(self):
        cfg = TelemetryConfig(otlp_endpoint=f"  {OPS} ")
        assert DefaultTelemetryProvider().otlp_destinations(cfg) == (
            OtlpDestination(
                name="telemetry.otlp_endpoint",
                endpoint=OPS,
                signals=frozenset({"metrics"}),
            ),
        )

    def test_nothing_set_is_no_destination(self):
        assert DefaultTelemetryProvider().otlp_destinations(TelemetryConfig()) == ()

    def test_the_legacy_string_is_the_first_entry_of_the_list(self):
        cfg = TelemetryConfig(
            otlp_endpoint=OPS,
            otlp_endpoints=[{"name": "second", "endpoint": EDITION}],
        )
        names = [d.name for d in DefaultTelemetryProvider().otlp_destinations(cfg)]
        assert names == ["telemetry.otlp_endpoint", "second"]

    def test_a_legacy_value_is_not_validated_away(self):
        """It was passed to the exporter as written; refusing it now would be a
        silent egress change for a config that worked yesterday."""
        cfg = TelemetryConfig(otlp_endpoint="localhost:4318/v1/metrics")
        assert [d.endpoint for d in DefaultTelemetryProvider().otlp_destinations(cfg)] == [
            "localhost:4318/v1/metrics"
        ]


# ── entry parsing ───────────────────────────────────────────────────────────


class TestEntries:
    def test_every_field_reaches_the_descriptor(self):
        cfg = TelemetryConfig(
            otlp_endpoints=[
                {
                    "name": "ops",
                    "endpoint": OPS,
                    "headers_env": {"Authorization": "OPS_OTLP_AUTH"},
                    "temporality": "cumulative",
                    "allow": ["kirocrew.session.started"],
                    "point_attributes": {"ClientType": "cli"},
                    "resource_allow": ["service.name"],
                }
            ]
        )
        (dest,) = DefaultTelemetryProvider().otlp_destinations(cfg)
        assert dest.endpoint == OPS
        assert dict(dest.headers_env) == {"Authorization": "OPS_OTLP_AUTH"}
        assert dest.process_env_credentials is False
        assert dest.temporality == "cumulative"
        assert dest.allow == frozenset({"kirocrew.session.started"})
        assert dict(dest.point_attributes) == {"ClientType": "cli"}
        assert dest.resource_allow == frozenset({"service.name"})
        assert dest.session is None

    def test_a_preset_name_resolves_to_the_reviewed_set(self):
        entry = oe.parse_entry(
            {"endpoint": OPS, "allow": "PRODUCT_METRICS"}, label="e", source=oe.SOURCE_CONFIG
        )
        assert entry.allow == PRODUCT_METRICS

    def test_an_unknown_preset_refuses_the_entry_rather_than_sending_everything(self, caplog):
        cfg = TelemetryConfig(otlp_endpoints=[{"endpoint": OPS, "allow": "EVERYTHING"}])
        assert DefaultTelemetryProvider().otlp_destinations(cfg) == ()
        assert "unknown preset" in caplog.text

    @pytest.mark.parametrize(
        "raw",
        [
            {"endpoint": "ftp://x.example/v1/metrics"},
            {"endpoint": ""},
            {"name": "has space", "endpoint": OPS},
            {"endpoint": OPS, "temporality": "sometimes"},
            {"endpoint": OPS, "point_attributes": {"user": "me"}},
            {"endpoint": OPS, "point_attributes": {"ClientType": "has space"}},
            {"endpoint": OPS, "headers_env": {"Authorization": "not-a-var"}},
            # A literal header value would be a credential in config.json.
            {"endpoint": OPS, "headers": {"X-Scope": "team-a"}},
            {"endpoint": OPS, "headers": {}},
            {"endpoint": OPS, "unknown_key": 1},
            # A mistyped opt-out must not read as "on".
            {"endpoint": OPS, "enabled": "false"},
            {"endpoint": OPS, "enabled": 0},
            # Only a build may lock an entry.
            {"endpoint": OPS, "locked": True},
            "not an object",
        ],
    )
    def test_a_malformed_entry_is_skipped_and_the_rest_stay(self, raw, caplog):
        cfg = TelemetryConfig(otlp_endpoints=[raw, {"name": "good", "endpoint": OPS}])
        assert [d.name for d in DefaultTelemetryProvider().otlp_destinations(cfg)] == ["good"]

    def test_the_warning_names_the_field_never_the_value(self, caplog):
        cfg = TelemetryConfig(
            otlp_endpoints=[{"name": "x", "endpoint": OPS, "headers_env": {"A": "s3cret value"}}]
        )
        DefaultTelemetryProvider().otlp_destinations(cfg)
        assert "x.headers_env[A]" in caplog.text
        assert "s3cret" not in caplog.text

    def test_a_literal_headers_key_is_refused_and_points_at_headers_env(self, caplog):
        cfg = TelemetryConfig(
            otlp_endpoints=[
                {"endpoint": OPS, "headers": {"Authorization": "Bearer s3cret"}},
                {"name": "good", "endpoint": OPS},
            ]
        )
        assert [d.name for d in DefaultTelemetryProvider().otlp_destinations(cfg)] == ["good"]
        assert "telemetry.otlp_endpoints[0].headers is not supported" in caplog.text
        assert "headers_env" in caplog.text
        assert "s3cret" not in caplog.text

    def test_only_the_legacy_entry_may_use_the_process_wide_otel_credentials(self):
        cfg = TelemetryConfig(otlp_endpoint=OPS, otlp_endpoints=[{"endpoint": EDITION}])
        legacy, entry = DefaultTelemetryProvider().otlp_destinations(cfg)
        assert (legacy.process_env_credentials, entry.process_env_credentials) == (True, False)

    def test_the_warn_once_memory_is_bounded_and_keeps_no_message_text(self):
        """Every config revision with a new malformed entry adds a key; a
        long-lived gateway must not keep all of them, nor their full text."""
        for i in range(oe.MAX_WARNED * 3):
            cfg = TelemetryConfig(
                otlp_endpoints=[{"endpoint": OPS, f"unknown_{i}_" + "x" * 500: 1}]
            )
            DefaultTelemetryProvider().otlp_destinations(cfg)
        assert 0 < len(oe._warned) <= oe.MAX_WARNED
        assert all(isinstance(k, bytes) and len(k) == 16 for k in oe._warned)

    def test_a_repeated_warning_is_logged_once(self, caplog):
        cfg = TelemetryConfig(otlp_endpoints=[{"endpoint": OPS, "unknown_key": 1}])
        for _ in range(3):
            DefaultTelemetryProvider().otlp_destinations(cfg)
        assert caplog.text.count("unknown key(s)") == 1

    def test_a_disabled_entry_is_skipped_without_being_checked(self, caplog):
        cfg = TelemetryConfig(otlp_endpoints=[{"name": "off", "enabled": False}])
        assert DefaultTelemetryProvider().otlp_destinations(cfg) == ()
        assert "ignored" not in caplog.text

    def test_a_repeated_name_keeps_the_first(self):
        cfg = TelemetryConfig(
            otlp_endpoints=[
                {"name": "a", "endpoint": OPS},
                {"name": "a", "endpoint": EDITION},
            ]
        )
        assert [d.endpoint for d in DefaultTelemetryProvider().otlp_destinations(cfg)] == [OPS]

    def test_entries_past_the_bound_are_not_used(self):
        rows = [{"name": f"e{i}", "endpoint": OPS} for i in range(oe.MAX_ENTRIES + 3)]
        dests = DefaultTelemetryProvider().otlp_destinations(TelemetryConfig(otlp_endpoints=rows))
        assert len(dests) == oe.MAX_ENTRIES

    def test_the_loader_keeps_every_row_so_each_is_warned_under_its_index(self, caplog):
        from kiro_crew.config.loader_builders import _build_telemetry_config

        tel = _build_telemetry_config({"otlp_endpoints": ["junk", {"endpoint": OPS}, 3]})
        assert tel.otlp_endpoints == ["junk", {"endpoint": OPS}, 3]
        with caplog.at_level("WARNING"):
            dests = oe.config_destinations(tel)
        assert [d.name for d in dests] == ["otlp_endpoints.1"]
        text = caplog.text
        assert "telemetry.otlp_endpoints[0]" in text
        assert "telemetry.otlp_endpoints[2]" in text
        assert "junk" not in text

    def test_the_loader_reads_a_non_list_as_empty(self):
        from kiro_crew.config.loader_builders import _build_telemetry_config

        assert _build_telemetry_config({"otlp_endpoints": "http://x"}).otlp_endpoints == []
        assert _build_telemetry_config({}).otlp_endpoints == []


# ── edition rows ────────────────────────────────────────────────────────────


class TestBuildShippedDestinations:
    def test_it_is_listed_after_the_operator_entries_as_locked(self, monkeypatch):
        _provider(monkeypatch, [BUILD_DEST])
        cfg = TelemetryConfig(otlp_endpoint=OPS, otlp_endpoints=[{"name": "mine", "endpoint": OPS}])
        rows = pm.otlp_egress_listing(cfg)
        assert [(r.destination.name, r.source, r.locked) for r in rows] == [
            ("telemetry.otlp_endpoint", oe.SOURCE_LEGACY, False),
            ("mine", oe.SOURCE_CONFIG, False),
            ("edition-product", oe.SOURCE_PROVIDER, True),
        ]

    def test_config_cannot_turn_it_off(self, monkeypatch):
        _provider(monkeypatch, [BUILD_DEST])
        cfg = TelemetryConfig(otlp_endpoints=[{"name": "edition-product", "enabled": False}])
        assert [d.name for d in pm._otlp_destinations(cfg)] == ["edition-product"]
        assert pm.otlp_egress_active(cfg) is True

    def test_the_operator_can_still_add_entries(self, monkeypatch):
        _provider(monkeypatch, [BUILD_DEST])
        cfg = TelemetryConfig(otlp_endpoints=[{"name": "mine", "endpoint": OPS}])
        assert [d.name for d in pm._otlp_destinations(cfg)] == ["mine", "edition-product"]

    def test_an_operator_entry_with_its_name_does_not_relabel_it(self, monkeypatch):
        """Same name, different endpoint: the build's collector stays labelled as
        the build's, so the panel never promises an off switch it lacks."""
        _provider(monkeypatch, [BUILD_DEST])
        cfg = TelemetryConfig(
            otlp_endpoints=[{"name": "edition-product", "endpoint": OPS}],
        )
        rows = pm.otlp_egress_listing(cfg)
        assert [(r.destination.endpoint, r.source, r.locked) for r in rows] == [
            (OPS, oe.SOURCE_CONFIG, False),
            (EDITION, oe.SOURCE_PROVIDER, True),
        ]

    def test_a_malformed_operator_entry_cannot_stop_collection(self, monkeypatch):
        """A value no check anticipated (an unhashable temporality) skips that
        entry only; the build-shipped destination and the rest stay."""
        _provider(monkeypatch, [BUILD_DEST])
        cfg = TelemetryConfig(
            otlp_endpoints=[
                {"name": "bad", "endpoint": OPS, "temporality": []},
                {"name": "mine", "endpoint": OPS},
            ]
        )
        assert [d.name for d in pm._otlp_destinations(cfg)] == ["mine", "edition-product"]
        assert [r.destination.name for r in pm.otlp_egress_listing(cfg)] == [
            "mine",
            "edition-product",
        ]


# ── the process-wide gates apply to every entry ─────────────────────────────


class TestGates:
    def test_the_policy_host_check_applies_to_a_build_shipped_destination(self, monkeypatch):
        from kiro_crew.platform.governance import parse_policy

        _provider(monkeypatch, [BUILD_DEST])
        ceiling = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {
                    "telemetry": {
                        "scopes": {
                            "otlp_hosts": {"mode": "allow", "allow": ["collector.example.com"]}
                        }
                    }
                },
            }
        )
        monkeypatch.setattr(pm, "_policy_ceiling", lambda: ceiling)
        cfg = TelemetryConfig(otlp_endpoints=[{"name": "mine", "endpoint": OPS}])
        with patch("kiro_crew.sel.sel"):
            assert [d.name for d in pm._otlp_destinations(cfg)] == ["mine"]

    def test_consent_off_builds_no_reader_even_for_a_locked_one(self, monkeypatch, tmp_path):
        _provider(monkeypatch, [BUILD_DEST])
        built: list = []
        monkeypatch.setattr(pm, "_build_otlp_reader", lambda d, c: built.append(d) or None)
        cfg = KiroCrewConfig(telemetry=TelemetryConfig(enabled=False))
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
            assert pm.get_recorder().enabled is False
        assert built == []

    def test_the_env_opt_out_builds_no_reader_even_for_a_locked_one(self, monkeypatch, tmp_path):
        _provider(monkeypatch, [BUILD_DEST])
        built: list = []
        monkeypatch.setattr(pm, "_build_otlp_reader", lambda d, c: built.append(d) or None)
        cfg = KiroCrewConfig(telemetry=TelemetryConfig(enabled=True))
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        monkeypatch.setenv("KIROCREW_TELEMETRY", "0")
        with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
            assert pm.get_recorder().enabled is False
        assert built == []

    def test_an_enterprise_build_ignores_the_local_list_but_keeps_the_build_one(self, monkeypatch):
        from kiro_crew import edition_locks

        monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        edition_locks.reset_logged()
        from kiro_crew.platform.governance import parse_policy

        _provider(monkeypatch, [BUILD_DEST])
        cfg = TelemetryConfig(otlp_endpoint=OPS, otlp_endpoints=[{"name": "mine", "endpoint": OPS}])
        assert [r.destination.name for r in pm.otlp_egress_listing(cfg)] == ["edition-product"]
        assert edition_locks.config_path_locked("telemetry.otlp_endpoints")
        # The build's destination still needs the policy: with none, the enterprise
        # baseline refuses every host; a policy that names it lets it through.
        with patch("kiro_crew.sel.sel"):
            assert pm._otlp_destinations(cfg) == ()
            ceiling = parse_policy(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    "capabilities": {
                        "telemetry": {
                            "scopes": {
                                "otlp_hosts": {"mode": "allow", "allow": ["telemetry.example.net"]}
                            }
                        }
                    },
                }
            )
            monkeypatch.setattr(pm, "_policy_ceiling", lambda: ceiling)
            assert [d.name for d in pm._otlp_destinations(cfg)] == ["edition-product"]
        edition_locks.reset_logged()


# ── how each reader is built ────────────────────────────────────────────────


class _Cfg:
    export_interval_seconds = 3600


def _capture_exporter(monkeypatch):
    """Record the kwargs ``_build_otlp_reader`` hands the OTLP exporter."""
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as mod

    captured: dict = {}
    real = mod.OTLPMetricExporter

    def fake(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(mod, "OTLPMetricExporter", fake)
    return captured


def _otlp_available() -> bool:
    try:
        import opentelemetry.exporter.otlp.proto.http.metric_exporter  # noqa: F401
        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


requires_otlp = pytest.mark.skipif(
    not _otlp_available(),
    reason='needs the optional extra: pip install "kirocrew[otlp]"',
)


@requires_otlp
class TestReaderBuild:
    def _build(self, dest):
        reader = pm._build_otlp_reader(dest, _Cfg())
        if reader is not None:
            reader.shutdown()
        return reader

    def test_a_header_is_read_from_the_environment(self, monkeypatch):
        captured = _capture_exporter(monkeypatch)
        monkeypatch.setenv("OPS_OTLP_AUTH", "Bearer t0ken")
        dest = OtlpDestination(
            "ops",
            "http://127.0.0.1:1/v1/metrics",
            frozenset({"metrics"}),
            headers_env={"Authorization": "OPS_OTLP_AUTH"},
        )
        assert self._build(dest) is not None
        assert captured["headers"] == {"Authorization": "Bearer t0ken"}

    def test_an_unset_header_variable_refuses_only_that_destination(self, monkeypatch, caplog):
        monkeypatch.delenv("OPS_OTLP_AUTH", raising=False)
        dest = OtlpDestination(
            "ops",
            "http://127.0.0.1:1/v1/metrics",
            frozenset({"metrics"}),
            headers_env={"Authorization": "OPS_OTLP_AUTH"},
        )
        assert self._build(dest) is None
        assert "OPS_OTLP_AUTH" in caplog.text

    def test_no_headers_passes_no_headers_kwarg(self, monkeypatch):
        """A destination that keeps the process-wide fallbacks (the legacy entry,
        one built in code) is built exactly as before."""
        captured = _capture_exporter(monkeypatch)
        self._build(OtlpDestination("x", "http://127.0.0.1:1/v1/metrics", frozenset({"metrics"})))
        assert "headers" not in captured
        assert "session" not in captured

    def test_an_own_headers_only_destination_blocks_both_fallbacks(self, monkeypatch):
        import requests

        captured = _capture_exporter(monkeypatch)
        dest = OtlpDestination(
            "x",
            "http://127.0.0.1:1/v1/metrics",
            frozenset({"metrics"}),
            process_env_credentials=False,
        )
        assert self._build(dest) is not None
        # Truthy though empty, so the exporter cannot swap in the env headers.
        assert captured["headers"] == {} and bool(captured["headers"]) is True
        assert isinstance(captured["session"], requests.Session)

    @pytest.mark.parametrize("env", ["", "CUMULATIVE", "DELTA"])
    def test_a_named_temporality_wins_over_the_variable(self, monkeypatch, env):
        from opentelemetry.sdk.metrics import Counter, Histogram, UpDownCounter
        from opentelemetry.sdk.metrics.export import AggregationTemporality as T

        from kiro_crew.metrics.temporality import TEMPORALITY_ENV_VAR

        monkeypatch.setenv(TEMPORALITY_ENV_VAR, env)
        captured = _capture_exporter(monkeypatch)
        url = "http://127.0.0.1:1/v1/metrics"
        self._build(OtlpDestination("d", url, frozenset({"metrics"}), temporality="delta"))
        pref = captured["preferred_temporality"]
        assert (pref[Counter], pref[Histogram], pref[UpDownCounter]) == (
            T.DELTA,
            T.DELTA,
            T.CUMULATIVE,
        )
        self._build(OtlpDestination("c", url, frozenset({"metrics"}), temporality="cumulative"))
        assert set(captured["preferred_temporality"].values()) == {T.CUMULATIVE}

    def test_no_temporality_keeps_the_core_default(self, monkeypatch):
        from kiro_crew.metrics.temporality import TEMPORALITY_ENV_VAR, delta_preference

        monkeypatch.delenv(TEMPORALITY_ENV_VAR, raising=False)
        captured = _capture_exporter(monkeypatch)
        self._build(OtlpDestination("x", "http://127.0.0.1:1/v1/metrics", frozenset({"metrics"})))
        assert captured["preferred_temporality"] == delta_preference()


# ── the disclosure surfaces ─────────────────────────────────────────────────


class TestDisclosure:
    def test_a_list_entry_alone_is_egress(self, monkeypatch):
        _provider(monkeypatch)
        assert pm.otlp_egress_active(TelemetryConfig(otlp_endpoints=[{"endpoint": OPS}]))

    def test_a_build_shipped_destination_alone_is_egress(self, monkeypatch):
        _provider(monkeypatch, [BUILD_DEST])
        assert pm.otlp_egress_active(TelemetryConfig()) is True

    def test_an_entry_refused_at_build_still_counts(self, monkeypatch):
        """An unset header variable refuses the reader, but setting it needs no
        config change, so the panel must still say metrics can leave."""
        monkeypatch.delenv("OPS_OTLP_AUTH", raising=False)
        _provider(monkeypatch)
        cfg = TelemetryConfig(
            otlp_endpoints=[{"endpoint": OPS, "headers_env": {"Authorization": "OPS_OTLP_AUTH"}}]
        )
        assert pm.otlp_egress_active(cfg) is True

    def test_the_panel_lists_every_destination_by_name_only(self, monkeypatch):
        from kiro_crew.dashboard.handlers import telemetry as h

        _provider(monkeypatch, [BUILD_DEST])
        cfg = KiroCrewConfig(
            telemetry=TelemetryConfig(
                enabled=True,
                otlp_endpoint="https://user:pw@legacy.example/v1/metrics",
                otlp_endpoints=[{"name": "mine", "endpoint": OPS}],
            )
        )
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        state = h._telemetry_cfg()
        assert state.otlp_configured is True
        assert list(state.otlp_destinations) == [
            {"name": "telemetry.otlp_endpoint", "source": "otlp_endpoint", "locked": False},
            {"name": "mine", "source": "otlp_endpoints", "locked": False},
            {"name": "edition-product", "source": "provider", "locked": True},
        ]
        text = repr(state.otlp_destinations)
        for leaked in ("legacy.example", "pw", "collector.example.com", "telemetry.example"):
            assert leaked not in text

    def test_an_unresolvable_posture_reports_egress_with_no_names(self, monkeypatch):
        from kiro_crew.dashboard.handlers import telemetry as h

        def boom(cfg):
            raise RuntimeError("provider exploded")

        prov = _provider(monkeypatch)
        prov.otlp_destinations = boom
        cfg = KiroCrewConfig(telemetry=TelemetryConfig(enabled=True))
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        state = h._telemetry_cfg()
        assert (state.otlp_configured, state.otlp_destinations) == (True, ())


# ── a process with no config watcher follows the list ───────────────────────


class TestRecheckFollowsTheList:
    """The MCP gateway daemon registers no config watcher; its sweeper only runs
    the consent recheck. A list entry turned off or removed there must lose its
    reader on that recheck, while an unchanged list keeps the live provider."""

    def _live(self, monkeypatch, tmp_path, entries):
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        _provider(monkeypatch)
        # One name per reader still running: a reader leaves when its provider
        # shuts it down, which is what dropping the old recorder does.
        live: list = []

        class _Tracked(InMemoryMetricReader):
            def __init__(self, name):
                super().__init__()
                self.dest_name = name
                live.append(self)

            def shutdown(self, *args, **kwargs):
                if self in live:
                    live.remove(self)
                return super().shutdown(*args, **kwargs)

        def fake_reader(dest, cfg):
            return _Tracked(dest.name)

        monkeypatch.setattr(pm, "_build_otlp_reader", fake_reader)
        holder = {}

        def set_entries(rows):
            holder["cfg"] = KiroCrewConfig(
                telemetry=TelemetryConfig(
                    enabled=True,
                    local_dir=str(tmp_path),
                    export_interval_seconds=3600,
                    otlp_endpoints=rows,
                )
            )

        set_entries(entries)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: holder["cfg"]))

        def live_names():
            return sorted(r.dest_name for r in live)

        return set_entries, live_names

    def _recheck(self):
        pm._consent_worker(pm._build_generation)

    @pytest.mark.parametrize(
        "after",
        [
            [{"name": "a", "endpoint": OPS}, {"name": "b", "endpoint": EDITION, "enabled": False}],
            [{"name": "a", "endpoint": OPS}],
        ],
        ids=["disabled", "removed"],
    )
    def test_an_entry_turned_off_loses_its_reader_on_the_recheck(
        self, monkeypatch, tmp_path, after
    ):
        set_entries, live_names = self._live(
            monkeypatch,
            tmp_path,
            [{"name": "a", "endpoint": OPS}, {"name": "b", "endpoint": EDITION}],
        )
        try:
            assert pm.get_recorder().enabled is True
            first = pm._provider
            assert live_names() == ["a", "b"]

            # Nothing moved: the recheck keeps the live provider.
            self._recheck()
            assert pm._provider is first

            set_entries(after)
            self._recheck()
            assert pm._provider is not first
            assert live_names() == ["a"]
        finally:
            pm.reset_for_testing()


# ── the real path, on the wire ──────────────────────────────────────────────


class _LoopbackServer(http.server.HTTPServer):
    # http.server resolves socket.getfqdn(host) on bind, which can stall on some
    # hosts; this server is reached by port, never by name, so skip the lookup.
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.server.received.append((self.path, dict(self.headers), body))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args):
        return


@requires_otlp
def test_two_entries_reach_their_own_collectors_with_their_own_narrowing(monkeypatch, tmp_path):
    """Entry 1 (config) gets every metric; destination 2 (shipped by the build)
    gets only the product preset, labelled, with its auth header read from the
    environment. Both through the live build."""
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2

    from kiro_crew.metrics import inventory_gauges as ig

    # Loopback only: these listen for our own export.
    server = _LoopbackServer(("127.0.0.1", 0), _Handler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        _provider(
            monkeypatch,
            [
                dataclasses.replace(
                    BUILD_DEST,
                    endpoint=f"{base}/product/v1/metrics",
                    headers_env={"Authorization": "EDITION_OTLP_AUTH"},
                )
            ],
        )
        monkeypatch.setenv("EDITION_OTLP_AUTH", "Bearer edition-token")
        monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
        cfg = KiroCrewConfig()
        cfg.telemetry = dataclasses.replace(
            cfg.telemetry,
            enabled=True,
            export_interval_seconds=3600,
            otlp_endpoints=[{"name": "ops", "endpoint": f"{base}/ops/v1/metrics"}],
        )
        ig.reset_for_testing()
        ig.mark_install_reporter()
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
                recorder = pm.get_recorder()
                assert recorder.enabled
                assert pm._provider is not None
                pm._provider.force_flush()
    finally:
        pm.reset_for_testing()
        ig.reset_for_testing()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    by_path: dict = {}
    for path, headers, body in server.received:
        request = metrics_service_pb2.ExportMetricsServiceRequest()
        request.ParseFromString(body)
        by_path.setdefault(path, []).append((headers, request))
    assert set(by_path) == {"/ops/v1/metrics", "/product/v1/metrics"}

    def names_and_labels(path):
        names, labels = set(), set()
        for _headers, request in by_path[path]:
            for rm in request.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        names.add(metric.name)
                        data = metric.WhichOneof("data")
                        for point in getattr(metric, data).data_points:
                            for attr in point.attributes:
                                if attr.key == "ClientType":
                                    labels.add(attr.value.string_value)
        return names, labels

    ops_names, ops_labels = names_and_labels("/ops/v1/metrics")
    product_names, product_labels = names_and_labels("/product/v1/metrics")
    assert product_names and product_names <= PRODUCT_METRICS
    assert ops_names - PRODUCT_METRICS, "the operator entry must get every metric"
    assert product_labels == {"desktop"}
    assert not ops_labels
    for headers, _request in by_path["/product/v1/metrics"]:
        assert headers.get("Authorization") == "Bearer edition-token"
    for headers, _request in by_path["/ops/v1/metrics"]:
        assert "Authorization" not in headers


@requires_otlp
@pytest.mark.parametrize(
    "env_var", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_METRICS_HEADERS"]
)
def test_a_list_entry_never_sends_the_process_wide_otel_headers(monkeypatch, tmp_path, env_var):
    """The OTel header variable was set for the legacy collector. A list entry
    with no headers sends none of it, one with headers_env sends only its own,
    and the legacy entry still gets the variable as it always did."""
    # Loopback only: this listens for our own export.
    server = _LoopbackServer(("127.0.0.1", 0), _Handler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        _provider(monkeypatch)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS", raising=False)
        monkeypatch.setenv(env_var, "x-env-auth=env-secret")
        monkeypatch.setenv("OWN_OTLP_AUTH", "own-secret")
        monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
        cfg = KiroCrewConfig()
        cfg.telemetry = dataclasses.replace(
            cfg.telemetry,
            enabled=True,
            export_interval_seconds=3600,
            otlp_endpoint=f"{base}/legacy/v1/metrics",
            otlp_endpoints=[
                {"name": "plain", "endpoint": f"{base}/plain/v1/metrics"},
                {
                    "name": "own",
                    "endpoint": f"{base}/own/v1/metrics",
                    "headers_env": {"x-own-auth": "OWN_OTLP_AUTH"},
                },
            ],
        )
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            with patch.object(pm, "_default_metrics_dir", return_value=tmp_path):
                assert pm.get_recorder().enabled
                assert pm._provider is not None
                pm._provider.force_flush()
    finally:
        pm.reset_for_testing()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    sent: dict = {}
    for path, headers, _body in server.received:
        sent.setdefault(path, []).append({k.lower(): v for k, v in headers.items()})
    assert set(sent) == {"/legacy/v1/metrics", "/plain/v1/metrics", "/own/v1/metrics"}
    for headers in sent["/legacy/v1/metrics"]:
        assert headers.get("x-env-auth") == "env-secret"
    for headers in sent["/plain/v1/metrics"]:
        assert "x-env-auth" not in headers
        assert "env-secret" not in repr(headers)
    for headers in sent["/own/v1/metrics"]:
        assert headers.get("x-own-auth") == "own-secret"
        assert "x-env-auth" not in headers
