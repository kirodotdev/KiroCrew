"""The Kiro telemetry destination the public build ships (``metrics/kiro_export.py``).

What already worked and must keep working, pinned here:

* Every consent gate applies to it unchanged: recording on only by default does
  not export, and every off switch builds no reader (``TestWhoSends``).
* The operator's own entries keep their place, first (``TestProvider``).
* An LTS build, CI runners and non-default homes do not list it (``TestProvider``).
* A policy ``otlp_hosts`` list can refuse its host (``TestProvider``).

The last test drives the real public provider and the real build against a
loopback receiver standing in for the endpoint.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import json
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import beacon
from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics import kiro_export
from kiro_crew.metrics import provider as pm
from kiro_crew.metrics.product_metrics import PRODUCT_METRICS
from kiro_crew.platform.defaults import DefaultTelemetryProvider

OPS = "https://collector.example.com/v1/metrics"

_ENV_VARS = (
    "KIROCREW_TELEMETRY",
    "KIROCREW_TELEMETRY_DISABLED",
    "DO_NOT_TRACK",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    *beacon._CI_ENV_VARS,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private data home, no telemetry or CI env vars, the destination on."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kiro_export, "_suppressed_for_tests", False)
    monkeypatch.setattr(kiro_export, "_session", None)
    # The private home is not the default one, which hides the destination.
    monkeypatch.setattr(beacon, "is_default_home", lambda: True)
    monkeypatch.setattr(pm, "_telemetry_provider", DefaultTelemetryProvider)
    monkeypatch.setattr(pm, "_policy_ceiling", lambda: None)
    _write_config(tmp_path, {})
    pm.reset_for_testing()
    yield tmp_path
    pm.reset_for_testing()


def _write_config(home: Path, telemetry: dict, ack: int = kiro_export.NOTICE_VERSION) -> None:
    """Write *telemetry*, with the privacy notice acknowledged at version *ack*."""
    doc = {"telemetry": telemetry, "dashboard": {"privacy_ack_version": ack}}
    (home / "config.json").write_text(json.dumps(doc), encoding="utf-8")


def _built(monkeypatch, home: Path) -> "tuple[bool, list[str]]":
    """Run the real build; return (recording, names of the readers it built)."""
    built: list = []
    monkeypatch.setattr(pm, "_build_otlp_reader", lambda d, c: built.append(d.name) or None)
    with patch.object(pm, "_default_metrics_dir", return_value=home / "metrics"):
        with patch("kiro_crew.sel.sel"):
            recording = pm.get_recorder().enabled
    return recording, built


class TestWhoSends:
    """One row per line of the table in docs/guides/telemetry-otlp-export.md."""

    def test_a_new_install_records_but_does_not_export(self, home, monkeypatch):
        assert _built(monkeypatch, home) == (True, [])

    def test_an_upgrade_with_the_heartbeat_on_does_not_export(self, home, monkeypatch):
        _write_config(home, {"beacon_enabled": True})
        assert _built(monkeypatch, home) == (True, [])

    def test_an_upgrade_with_the_heartbeat_off_records_nothing(self, home, monkeypatch):
        _write_config(home, {"beacon_enabled": False})
        assert _built(monkeypatch, home) == (False, [])

    def test_a_written_true_exports(self, home, monkeypatch):
        _write_config(home, {"enabled": True})
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    def test_a_written_true_under_an_older_notice_sends_nothing(self, home, monkeypatch):
        """Settings wrote it when the switch said metrics stay on this machine."""
        _write_config(home, {"enabled": True}, ack=kiro_export.NOTICE_VERSION - 1)
        assert _built(monkeypatch, home) == (True, [])

    def test_the_env_opt_in_exports(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    @pytest.mark.parametrize("value", [False, "true", 1, None])
    def test_a_written_false_or_non_bool_records_nothing(self, home, monkeypatch, value):
        _write_config(home, {"enabled": value})
        assert _built(monkeypatch, home) == (False, [])

    @pytest.mark.parametrize(
        ("name", "value"), [("DO_NOT_TRACK", "1"), ("KIROCREW_TELEMETRY", "0")]
    )
    def test_an_env_opt_out_wins_over_a_written_true(self, home, monkeypatch, name, value):
        _write_config(home, {"enabled": True})
        monkeypatch.setenv(name, value)
        assert _built(monkeypatch, home) == (False, [])

    def test_ci_records_but_does_not_list_or_export_it(self, home, monkeypatch):
        _write_config(home, {"enabled": True})
        monkeypatch.setenv("CI", "true")
        assert _built(monkeypatch, home) == (True, [])

    def test_the_operator_entry_is_built_first(self, home, monkeypatch):
        _write_config(home, {"enabled": True, "otlp_endpoints": [{"name": "ops", "endpoint": OPS}]})
        assert _built(monkeypatch, home) == (True, ["ops", kiro_export.DESTINATION_NAME])


class TestProvider:
    def test_it_follows_the_operator_entries_and_is_narrowed(self, home):
        dests = DefaultTelemetryProvider().otlp_destinations(TelemetryConfig(otlp_endpoint=OPS))
        assert [d.name for d in dests] == ["telemetry.otlp_endpoint", "kiro-telemetry"]
        kiro = dests[1]
        assert kiro.endpoint == kiro_export.ENDPOINT
        assert kiro.allow == PRODUCT_METRICS
        assert dict(kiro.point_attributes) == {"ClientType": "kiro-crew"}
        assert "service.instance.id" not in kiro.resource_allow
        assert not any(key.startswith("host.") for key in kiro.resource_allow)
        assert kiro.process_env_credentials is False
        assert kiro.headers_env == {}

    def test_the_disclosure_names_it_as_included_by_the_build(self, home):
        rows = pm.otlp_egress_listing(TelemetryConfig())
        assert [(r.destination.name, r.source, r.locked) for r in rows] == [
            ("kiro-telemetry", "provider", True)
        ]
        assert pm.otlp_egress_active(TelemetryConfig()) is True

    def test_telemetry_status_names_it(self, home):
        from kiro_crew.cli_commands import _format_recording_status

        text = _format_recording_status(KiroCrewConfig.load())
        assert "Collectors:  kiro-telemetry (included by this build)" in text
        assert "until telemetry.enabled: true is written" in text

    @pytest.mark.parametrize("hidden_by", ["ci", "home", "lts", "tests"])
    def test_it_is_not_listed_where_it_must_not_send(self, home, monkeypatch, hidden_by):
        if hidden_by == "ci":
            monkeypatch.setenv("GITHUB_ACTIONS", "true")
        elif hidden_by == "home":
            monkeypatch.setattr(beacon, "is_default_home", lambda: False)
        elif hidden_by == "lts":
            monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        else:
            monkeypatch.setattr(kiro_export, "_suppressed_for_tests", True)
        assert kiro_export.destinations() == ()
        assert DefaultTelemetryProvider().otlp_destinations(TelemetryConfig()) == ()

    def test_before_the_notice_nothing_is_listed_so_enabling_stays_local(self, home):
        """No destination means the Settings switch and ``telemetry enable`` still
        write ``telemetry.enabled`` exactly as they do on a host with none."""
        _write_config(home, {}, ack=kiro_export.NOTICE_VERSION - 1)
        assert pm.otlp_egress_listing(TelemetryConfig()) == ()
        assert pm.otlp_egress_active(TelemetryConfig()) is False

    def test_an_unreadable_acknowledgement_lists_nothing(self, home, monkeypatch):
        def boom(cls):
            raise OSError("unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
        assert kiro_export.destinations() == ()

    def test_an_lts_policy_collector_is_the_only_destination(self, home, monkeypatch):
        from kiro_crew import edition_locks
        from kiro_crew.platform import context as pc
        from kiro_crew.platform.governance import parse_policy

        admin = "https://otel.corp.example/v1/metrics"
        monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        edition_locks.reset_logged()
        ctx = SimpleNamespace(
            governance=parse_policy(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    "capabilities": {"telemetry": {"enabled": True, "otlp_endpoint": admin}},
                }
            )
        )
        monkeypatch.setattr(pc, "current_context", lambda: ctx)
        monkeypatch.setattr(pm, "current_context", lambda: ctx)
        monkeypatch.setattr(pm, "_policy_ceiling", lambda: ctx.governance)
        with patch("kiro_crew.sel.sel"):
            dests = pm._otlp_destinations(TelemetryConfig())
        assert [(d.name, d.endpoint) for d in dests] == [("policy", admin)]
        edition_locks.reset_logged()

    def test_a_policy_host_list_can_refuse_it(self, home, monkeypatch):
        from kiro_crew.platform.governance import parse_policy

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
        with patch("kiro_crew.sel.sel"):
            dests = pm._otlp_destinations(TelemetryConfig(otlp_endpoint=OPS), ceiling)
        assert [d.name for d in dests] == ["telemetry.otlp_endpoint"]

    def test_the_session_is_built_once(self, home):
        first = kiro_export.destinations()[0].session
        assert kiro_export.destinations()[0].session is first


class TestIdentity:
    @pytest.fixture
    def install_id(self, monkeypatch):
        monkeypatch.setattr(beacon, "install_id", lambda create=True: "a" * 32)
        return hashlib.sha256(
            ("kirocrew-metrics-machine-id-v1:" + "a" * 32).encode("utf-8")
        ).hexdigest()

    def test_only_the_machine_id_is_sent(self, home, install_id):
        assert kiro_export.identity_headers() == {"x-kiro-machineid": install_id}

    def test_a_stored_sign_in_is_never_sent(self, home, monkeypatch, install_id):
        from kiro_crew.auth.store import KasToken, TokenStore

        token = KasToken(
            access_token="tok",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="BuilderId",
            identity="builder_id",
        )
        monkeypatch.setattr(TokenStore, "resolve", lambda self: token)
        assert kiro_export.identity_headers() == {"x-kiro-machineid": install_id}

    def test_no_install_id_sends_nothing(self, home, monkeypatch):
        monkeypatch.setattr(beacon, "install_id", lambda create=True: "")
        assert kiro_export.identity_headers() == {}
        session = kiro_export.build_session()
        with patch("requests.Session.send") as send:
            response = session.post(kiro_export.ENDPOINT, data=b"x")
        assert response.status_code == 401
        send.assert_not_called()

    def test_another_url_is_never_sent(self, home, install_id):
        session = kiro_export.build_session()
        with patch("requests.Session.send") as send:
            response = session.post(OPS, data=b"x")
        assert response.status_code == 401
        send.assert_not_called()


# ── the real path, on the wire ──────────────────────────────────────────────


def _otlp_available() -> bool:
    try:
        import opentelemetry.exporter.otlp.proto.http.metric_exporter  # noqa: F401
        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class _LoopbackServer(http.server.HTTPServer):
    # Reached by port, never by name: skip the getfqdn lookup on bind.
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


@pytest.mark.skipif(not _otlp_available(), reason="needs the otlp extra")
def test_the_real_provider_sends_product_metrics_to_the_endpoint(home, monkeypatch):
    """Written opt-in, the public provider, the real build and exporter. The
    loopback server stands in for the endpoint; nothing else is contacted."""
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2

    from kiro_crew.metrics import inventory_gauges as ig

    # Loopback only: this listens for our own export.
    server = _LoopbackServer(("127.0.0.1", 0), _Handler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    monkeypatch.setattr(kiro_export, "ENDPOINT", f"http://{host}:{port}/v1/metrics")
    # Set for an operator's own collector; must never reach this one.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-env-auth=env-secret")
    _write_config(home, {"enabled": True, "export_interval_seconds": 3600})
    try:
        ig.reset_for_testing()
        ig.mark_install_reporter()
        with patch.object(pm, "_default_metrics_dir", return_value=home / "metrics"):
            with patch("kiro_crew.sel.sel"):
                assert pm.get_recorder().enabled
                assert pm._provider is not None
                pm._provider.force_flush()
    finally:
        pm.reset_for_testing()
        ig.reset_for_testing()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert server.received, "nothing reached the endpoint"
    expected_id = hashlib.sha256(
        ("kirocrew-metrics-machine-id-v1:" + beacon.install_id(create=False)).encode("utf-8")
    ).hexdigest()
    names, labels, resource_keys = set(), set(), set()
    for path, headers, body in server.received:
        assert path == "/v1/metrics"
        assert headers.get("x-kiro-machineid") == expected_id
        assert "Authorization" not in headers
        assert "x-env-auth" not in {k.lower() for k in headers}
        request = metrics_service_pb2.ExportMetricsServiceRequest()
        request.ParseFromString(body)
        for rm in request.resource_metrics:
            resource_keys.update(attr.key for attr in rm.resource.attributes)
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    names.add(metric.name)
                    data = metric.WhichOneof("data")
                    for point in getattr(metric, data).data_points:
                        labels.update(
                            attr.value.string_value
                            for attr in point.attributes
                            if attr.key == "ClientType"
                        )
    assert names and names <= PRODUCT_METRICS
    assert labels == {"kiro-crew"}
    assert resource_keys <= kiro_export.RESOURCE_ALLOW
    assert "service.instance.id" not in resource_keys


def test_the_suite_never_sees_it_by_default():
    """The conftest guard every other test relies on."""
    assert kiro_export._suppressed_for_tests is True
    assert kiro_export.destinations() == ()
