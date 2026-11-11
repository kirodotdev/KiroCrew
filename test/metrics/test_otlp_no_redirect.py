"""A metrics OTLP export follows no redirect.

The ``otlp_hosts`` policy and the https rule check the configured endpoint only.
A collector that answers 307/308 must not get the batch, and its headers, resent
to a host neither rule checked: the export fails instead, once, and nothing it
logs quotes a header value.

Each test runs two loopback servers: the configured endpoint answers with a
redirect toward the second one, which must receive nothing. All three ways a
destination's session is chosen are covered: the legacy ``telemetry.otlp_endpoint``
entry (process-wide header variable), a ``telemetry.otlp_endpoints`` list entry
(``headers_env``), and a destination built in code with its own ``session``.

Skipped without the optional ``otlp`` extra, like the other OTLP wire tests.
"""

from __future__ import annotations

import contextlib
import http.server
import logging
import threading

import pytest
from loopback_servers import LoopbackHTTPServer

from kiro_crew.metrics import provider as pm
from kiro_crew.platform.interfaces import OtlpDestination

_PLANTED = "planted-hdr-5c1e9a"
_HEADER = "x-api-key"
_HEADER_VAR = "KIROCREW_TEST_NOREDIRECT_KEY"


def _otlp_available() -> bool:
    try:
        import opentelemetry.exporter.otlp.proto.http.metric_exporter  # noqa: F401
        import opentelemetry.sdk.metrics  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _otlp_available(),
    reason='needs the optional extra: pip install "kirocrew[otlp]"',
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Records each POST's headers; answers the server's redirect, or 200."""

    def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler's required spelling
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.hits.append({k.lower(): v for k, v in self.headers.items()})  # type: ignore[attr-defined]
        location = self.server.location  # type: ignore[attr-defined]
        self.send_response(self.server.status if location else 200)  # type: ignore[attr-defined]
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        return


@contextlib.contextmanager
def _server(status: int = 200, location: str = ""):
    # 127.0.0.1 explicitly: a test listener must never be reachable off the machine.
    server = LoopbackHTTPServer(("127.0.0.1", 0), _Handler)
    server.hits = []  # type: ignore[attr-defined]
    server.status = status  # type: ignore[attr-defined]
    server.location = location  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _url(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/v1/metrics"


def _destination(kind: str, endpoint: str, monkeypatch) -> OtlpDestination:
    """A destination of *kind* whose export carries the planted header value."""
    metrics = frozenset({"metrics"})
    if kind == "legacy":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS", f"{_HEADER}={_PLANTED}")
        return OtlpDestination("legacy", endpoint, metrics)
    if kind == "list-entry":
        monkeypatch.setenv(_HEADER_VAR, _PLANTED)
        return OtlpDestination(
            "entry",
            endpoint,
            metrics,
            headers_env={_HEADER: _HEADER_VAR},
            process_env_credentials=False,
        )
    import requests

    session = requests.Session()
    session.headers[_HEADER] = _PLANTED
    return OtlpDestination("edition", endpoint, metrics, session=session)


class _Cfg:
    export_interval_seconds = 3600  # only force_flush exports


def _capture_session(monkeypatch) -> dict:
    """Record the kwargs ``_build_otlp_reader`` hands the real OTLP exporter."""
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as mod

    captured: dict = {}
    real = mod.OTLPMetricExporter

    def fake(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(mod, "OTLPMetricExporter", fake)
    return captured


def test_an_edition_session_is_copied_not_changed(monkeypatch):
    """The exporter gets its own no-redirect session; the edition's stays as it was."""
    import requests
    from requests.adapters import HTTPAdapter

    source = requests.Session()
    source.auth = ("edition", "rotating")
    adapter = HTTPAdapter()
    source.mount("https://collector.test/", adapter)
    source.headers[_HEADER] = _PLANTED
    before_headers = dict(source.headers)
    before_redirects = source.max_redirects

    captured = _capture_session(monkeypatch)
    dest = OtlpDestination(
        "edition", "http://127.0.0.1:1/v1/metrics", frozenset({"metrics"}), session=source
    )
    reader = pm._build_otlp_reader(dest, _Cfg())
    assert reader is not None, "the extra is installed, so the builder must produce a reader"
    try:
        used = captured["session"]
        assert used is not source
        assert used.max_redirects == 0
        assert used.auth is source.auth, "a rotating credential must stay the edition's own object"
        assert used.adapters["https://collector.test/"] is adapter
        assert used.headers[_HEADER] == _PLANTED
        assert source.max_redirects == before_redirects
        assert dict(source.headers) == before_headers, "the exporter's header updates leaked back"
    finally:
        reader.shutdown()


def test_a_signing_session_subclass_still_signs_its_exports(monkeypatch):
    """An edition that signs in a ``Session`` subclass keeps signing: the copy is
    the same class with the subclass's own state, and the original is untouched."""
    import requests
    from opentelemetry.sdk.metrics import MeterProvider

    class _Signing(requests.Session):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key  # subclass state that copy.copy would drop

        def request(self, method, url, **kwargs):
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "x-signature": self.key}
            return super().request(method, url, **kwargs)

    source = _Signing("sig-7f3a")
    before_redirects = source.max_redirects
    before_headers = dict(source.headers)
    captured = _capture_session(monkeypatch)
    with _server() as collector:
        dest = OtlpDestination("edition", _url(collector), frozenset({"metrics"}), session=source)
        reader = pm._build_otlp_reader(dest, _Cfg())
        assert reader is not None, "the extra is installed, so the builder must produce a reader"
        provider = MeterProvider(metric_readers=[reader])
        try:
            provider.get_meter("noredirect-test").create_counter("kirocrew.test.signed").add(1)
            provider.force_flush()
            hits = list(collector.hits)
        finally:
            provider.shutdown()

    used = captured["session"]
    assert type(used) is _Signing and used is not source
    assert used.max_redirects == 0
    assert [hit.get("x-signature") for hit in hits] == ["sig-7f3a"], "the export went out unsigned"
    assert source.max_redirects == before_redirects
    assert dict(source.headers) == before_headers


@pytest.mark.parametrize(
    "provider_var", ["", "OTEL_PYTHON_EXPORTER_OTLP_HTTP_METRICS_CREDENTIAL_PROVIDER"]
)
def test_a_missing_sdk_session_helper_keeps_legacy_export_without_redirects(
    provider_var, monkeypatch, caplog
):
    """The legacy path reads the SDK's private session loader. Without it, that
    destination keeps exporting, as on main, through a plain no-redirect session,
    and one WARNING says so, naming the credential-provider variable when set."""
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as mod
    import requests

    monkeypatch.delattr(mod, "_load_session_from_envvar")
    monkeypatch.delenv("OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER", raising=False)
    monkeypatch.delenv("OTEL_PYTHON_EXPORTER_OTLP_HTTP_METRICS_CREDENTIAL_PROVIDER", raising=False)
    if provider_var:
        monkeypatch.setenv(provider_var, "edition-provider")
    captured = _capture_session(monkeypatch)
    dest = OtlpDestination("legacy", "http://127.0.0.1:1/v1/metrics", frozenset({"metrics"}))
    with caplog.at_level(logging.WARNING):
        reader = pm._build_otlp_reader(dest, _Cfg())
    assert reader is not None, "the legacy entry must keep exporting"
    try:
        session = captured["session"]
        assert type(session) is requests.Session and session.max_redirects == 0
    finally:
        reader.shutdown()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    loader = [m for m in warnings if "no credential-provider session loader" in m]
    assert len(loader) == 1, warnings
    assert ("was NOT loaded" in loader[0]) is bool(provider_var)
    if provider_var:
        assert provider_var in loader[0] and "edition-provider" not in loader[0]
    assert not any("init failed" in m for m in warnings)


@pytest.mark.parametrize("kind", ["legacy", "list-entry", "edition-session"])
@pytest.mark.parametrize("status", [307, 308])
def test_a_redirect_fails_the_export_and_reaches_no_other_host(kind, status, monkeypatch, caplog):
    from opentelemetry.sdk.metrics import MeterProvider

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS", raising=False)
    with _server() as target, _server(status, _url(target)) as collector:
        reader = pm._build_otlp_reader(_destination(kind, _url(collector), monkeypatch), _Cfg())
        assert reader is not None, "the extra is installed, so the builder must produce a reader"
        provider = MeterProvider(metric_readers=[reader])
        try:
            provider.get_meter("noredirect-test").create_counter("kirocrew.test.noredirect").add(1)
            with caplog.at_level(logging.DEBUG):
                provider.force_flush()
            collector_hits, target_hits = list(collector.hits), list(target.hits)
        finally:
            provider.shutdown()

    # The header was really sent to the configured collector, so the log and
    # target assertions below are not vacuous.
    assert [hit.get(_HEADER) for hit in collector_hits] == [
        _PLANTED
    ], "expected exactly one attempt at the configured collector (no retries)"
    assert target_hits == [], f"the {status} was followed to a host no policy checked"
    assert any(
        r.levelno >= logging.ERROR and "Failed to export metrics batch" in r.getMessage()
        for r in caplog.records
    ), "the redirect did not fail the export"
    leaked = [r.getMessage() for r in caplog.records if _PLANTED in r.getMessage()]
    assert leaked == [], "a log record quotes the header value"
