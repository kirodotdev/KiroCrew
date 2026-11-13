"""``metrics.otlp_session`` without the optional ``otlp`` extra.

The loopback tests in ``test_otlp_no_redirect.py`` drive the real exporter and
skip when the extra is absent, as it is in CI. These cover every branch of the
session choice with the SDK module stubbed, so they always run.
"""

from __future__ import annotations

import base64
import contextlib
import http.server
import logging
import sys
import threading
import types

import pytest
import requests
from loopback_servers import LoopbackHTTPServer

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SDK_MODULE = "opentelemetry.exporter.otlp.proto.http.metric_exporter"
_GENERIC_VAR = "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER"
_METRICS_VAR = "OTEL_PYTHON_EXPORTER_OTLP_HTTP_METRICS_CREDENTIAL_PROVIDER"


@pytest.fixture
def otlp_session():
    """The module under test, imported per test so collection never depends on it."""
    from kiro_crew.metrics import otlp_session as module

    return module


@pytest.fixture
def sdk(monkeypatch):
    """A stand-in SDK exporter module; tests add or omit the session loader."""
    for var in (_GENERIC_VAR, _METRICS_VAR):
        monkeypatch.delenv(var, raising=False)
    mod = types.ModuleType(_SDK_MODULE)
    monkeypatch.setitem(sys.modules, _SDK_MODULE, mod)
    return mod


def test_a_supplied_session_is_copied_with_its_class_and_state(otlp_session):
    class _Signing(requests.Session):
        pass

    source = _Signing()
    source.key = "k"  # subclass state
    source.headers["x-api-key"] = "v"
    before = source.max_redirects

    used = otlp_session.export_session(source, own_only=False)
    used.headers["User-Agent"] = "sdk"

    assert type(used) is _Signing and used is not source
    assert used.key == "k" and used.headers["x-api-key"] == "v"
    assert used.max_redirects == 0 and source.max_redirects == before
    assert source.headers.get("User-Agent") != "sdk"


def test_a_list_entry_gets_a_plain_session_and_never_the_loader(otlp_session, sdk):
    def _never(_variable):  # pragma: no cover - asserted not called
        raise AssertionError("an otlp_endpoints entry must not load the provider session")

    sdk._load_session_from_envvar = _never
    used = otlp_session.export_session(None, own_only=True)
    assert type(used) is requests.Session and used.max_redirects == 0


@pytest.mark.parametrize("loaded", [True, False])
def test_the_legacy_entry_gets_the_loaded_session_else_a_plain_one(otlp_session, sdk, loaded):
    provided = requests.Session()
    asked: list[str] = []

    def _load(variable):
        asked.append(variable)
        return provided if loaded else None

    sdk._load_session_from_envvar = _load
    used = otlp_session.export_session(None, own_only=False)
    assert asked == [_METRICS_VAR]
    assert (used is provided) is loaded
    assert type(used) is requests.Session and used.max_redirects == 0


@pytest.mark.parametrize("var", ["", _GENERIC_VAR, _METRICS_VAR])
def test_a_missing_loader_keeps_the_legacy_entry_on_a_plain_session(
    otlp_session, sdk, monkeypatch, caplog, var
):
    if var:
        monkeypatch.setenv(var, "edition-provider")
    with caplog.at_level(logging.WARNING, logger=otlp_session.__name__):
        used = otlp_session.export_session(None, own_only=False)
    assert type(used) is requests.Session and used.max_redirects == 0
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert ("was NOT loaded" in messages[0]) is bool(var)
    if var:
        assert var in messages[0] and "edition-provider" not in messages[0]


class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler's required spelling
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.auth.append(self.headers.get("Authorization"))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        return


@contextlib.contextmanager
def _receiver():
    # 127.0.0.1 explicitly: a test listener must never be reachable off the machine.
    server = LoopbackHTTPServer(("127.0.0.1", 0), _Recorder)
    server.auth = []  # type: ignore[attr-defined]
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


def _netrc_for_loopback(tmp_path, monkeypatch) -> None:
    netrc = tmp_path / "netrc"
    # Fake and split, so no scanner reads it as a credential.
    netrc.write_text("machine 127.0.0.1 login unrelated password not-" + "for-metrics\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))


def _session_for(otlp_session, sdk, path: str, edition: requests.Session):
    sdk._load_session_from_envvar = lambda _var: edition if path == "legacy-loaded" else None
    return otlp_session.export_session(
        edition if path == "supplied" else None, own_only=path == "list-entry"
    )


def _sent_authorization(session, userinfo: str = "") -> list:
    """The ``Authorization`` a loopback receiver saw for one POST through *session*."""
    with _receiver() as receiver:
        url = "http://%s%s:%d/v1/metrics" % (userinfo, *receiver.server_address[:2])
        session.post(url, data=b"batch", timeout=FALLBACK_TIMEOUT_SECS)
        return list(receiver.auth)


def _basic(login: str) -> str:
    return "Basic " + base64.b64encode(login.encode()).decode()


_NO_AUTH_PATHS = ["list-entry", "legacy-plain", "legacy-loaded", "supplied"]


@pytest.mark.parametrize(
    ("path", "own_auth"),
    [(path, False) for path in _NO_AUTH_PATHS] + [("legacy-loaded", True), ("supplied", True)],
)
def test_no_session_sends_a_netrc_login_and_an_own_auth_is_kept(
    otlp_session, sdk, tmp_path, monkeypatch, path, own_auth
):
    """A session with no ``auth`` gets one that skips ``$NETRC`` for the
    collector's host; a session that has its own keeps it."""
    _netrc_for_loopback(tmp_path, monkeypatch)
    edition = requests.Session()
    if own_auth:
        edition.auth = ("edition", "rotating")

    used = _session_for(otlp_session, sdk, path, edition)
    sent = _sent_authorization(used)

    if own_auth:
        assert used.auth is edition.auth, "the session's own credential must stay its object"
        assert sent == [_basic("edition:rotating")]
    else:
        assert sent == [None]
    assert used.trust_env, "proxy and CA settings from the environment still apply"
    if path == "supplied":
        assert edition.auth == (("edition", "rotating") if own_auth else None), "edition changed"


@pytest.mark.parametrize("path", _NO_AUTH_PATHS)
def test_credentials_in_the_endpoint_url_are_still_sent_instead_of_netrc(
    otlp_session, sdk, tmp_path, monkeypatch, path
):
    """What ``requests`` sends with no ``auth`` and no ``.netrc`` match: the URL's own login."""
    _netrc_for_loopback(tmp_path, monkeypatch)
    used = _session_for(otlp_session, sdk, path, requests.Session())
    assert _sent_authorization(used, "in-url:configured@") == [_basic("in-url:configured")]
