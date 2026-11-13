"""OTLP span export: the switch, the endpoint and header rules, the policy gate and the wire.

The wire tests post to an in-process receiver bound to 127.0.0.1 and need the
optional ``otlp`` extra; everything above them runs without it.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
from unittest.mock import patch

import pytest
from loopback_servers import LoopbackHTTPServer

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent, TurnUsage
from kiro_crew.config.sections_telemetry import TelemetryConfig
from kiro_crew.metrics import provider as metrics_provider
from kiro_crew.testing.wait import default_timeout
from kiro_crew.tracing import otlp, provider, turns

#: Obviously fake, and split so no credential-shaped literal sits in the repo.
HEADER_VALUE = "Bearer not-a-real-" + "key-0001"
VAULT_ENTRY = "TRACES_OTLP_AUTH"
LOOPBACK = "http://127.0.0.1:4318/v1/traces"
REMOTE = "https://collector.example.com/v1/traces"


def _cfg(**kw):
    return TelemetryConfig(traces_enabled=kw.pop("traces_enabled", True), **kw)


def _no_policy():
    """A process with no policy ceiling: every host is asked of ``resolve(None, …)``."""
    return patch.object(metrics_provider, "_policy_ceiling", lambda: None)


def _deny_all_policy():
    from kiro_crew.platform.governance import parse_policy

    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {
                "telemetry": {
                    "scopes": {"otlp_hosts": {"mode": "allow", "allow": ["only.example.org"]}}
                }
            },
        }
    )
    return patch.object(metrics_provider, "_policy_ceiling", lambda: ceiling)


def _messages(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# ── where spans may go ────────────────────────────────────────────────────


def test_an_env_endpoint_without_the_switch_resolves_nothing():
    env = {otlp.ENV_ENDPOINT: "http://127.0.0.1:4318", otlp.ENV_TRACES_ENDPOINT: LOOPBACK}
    assert otlp.resolve(_cfg(traces_enabled=False), env) is None
    assert otlp.resolve(_cfg(traces_enabled=False, traces_otlp_endpoint=LOOPBACK), env) is None


def test_config_wins_over_env_and_the_env_fallback_follows_the_sdk_rule():
    env = {otlp.ENV_ENDPOINT: "http://localhost:4318/"}
    assert otlp.resolve(_cfg(), env).endpoint == "http://localhost:4318/v1/traces"
    env[otlp.ENV_TRACES_ENDPOINT] = "http://127.0.0.1:9999/custom/traces"
    assert otlp.resolve(_cfg(), env).endpoint == "http://127.0.0.1:9999/custom/traces"
    dest = otlp.resolve(_cfg(traces_otlp_endpoint=REMOTE), env)
    assert (dest.endpoint, dest.source) == (REMOTE, "config")
    assert otlp.resolve(_cfg(), {}) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://collector.example.com/v1/traces",
        "http://10.0.0.5:4318/v1/traces",
        "https://user:pw@collector.example.com/v1/traces",
        "https://collector.example.com/v1/traces?key=abc",
        "ftp://collector.example.com/v1/traces",
    ],
)
def test_an_unsafe_endpoint_is_refused_without_quoting_it(endpoint, caplog):
    caplog.set_level(logging.WARNING)
    assert otlp.resolve(_cfg(traces_otlp_endpoint=endpoint), {}) is None
    assert endpoint not in _messages(caplog)
    assert "trace export off" in _messages(caplog)


@pytest.mark.parametrize(
    "endpoint",
    [LOOPBACK, "http://localhost:4318/v1/traces", "http://[::1]:4318/v1/traces", REMOTE],
)
def test_https_or_a_loopback_host_is_accepted(endpoint):
    assert otlp.resolve(_cfg(traces_otlp_endpoint=endpoint), {}).endpoint == endpoint


def test_an_lts_build_ignores_config_and_env_endpoints(monkeypatch):
    from kiro_crew import edition_locks

    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
    edition_locks.reset_logged()
    try:
        assert otlp.resolve(_cfg(traces_otlp_endpoint=REMOTE), {}) is None
        assert otlp.resolve(_cfg(), {otlp.ENV_TRACES_ENDPOINT: LOOPBACK}) is None
        assert edition_locks.config_path_locked("telemetry.traces_otlp_endpoint")
    finally:
        edition_locks.reset_logged()


# ── headers ───────────────────────────────────────────────────────────────


def test_a_literal_header_value_in_config_is_refused_and_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    cfg = _cfg(
        traces_otlp_endpoint=REMOTE, traces_otlp_headers_secret={"Authorization": HEADER_VALUE}
    )
    assert otlp.resolve(cfg, {}) is None
    assert HEADER_VALUE not in _messages(caplog)
    assert "Authorization" in _messages(caplog)


@pytest.mark.parametrize(
    "check,value,ok",
    [
        # header name: RFC 9110 token characters, 1 to 64
        ("header_name", "Authorization", True),
        ("header_name", "X-Api_Key.v1!#$%&'*+^`|~", True),
        ("header_name", "", False),
        ("header_name", "a" * 64, True),
        ("header_name", "a" * 65, False),
        ("header_name", "Bad Name", False),
        ("header_name", "X-Key\r\nX-Evil", False),
        ("header_name", "Authorization\n", False),  # what `$` would have accepted
        ("header_name", "Authorizätion", False),
        # vault entry name: [A-Za-z0-9] then [A-Za-z0-9._-], 1 to 128
        ("vault", "TRACES_OTLP_AUTH", True),
        ("vault", "a.b-c_9", True),
        ("vault", "", False),
        ("vault", "a" * 128, True),
        ("vault", "a" * 129, False),
        ("vault", "_leading", False),
        ("vault", "Bearer abc", False),
        ("vault", "NAME\r\nX", False),
        ("vault", "NAME\n", False),
        ("vault", "NÄME", False),
        # environment variable name: [A-Za-z_] then [A-Za-z0-9_], 1 to 128
        ("env", "TRACE_KEY_VAR", True),
        ("env", "_x9", True),
        ("env", "", False),
        ("env", "A" * 128, True),
        ("env", "A" * 129, False),
        ("env", "9VAR", False),
        ("env", "VAR.NAME", False),
        ("env", "VAR\r\nX", False),
        ("env", "VAR\n", False),
        ("env", "VÄR", False),
        # header value: visible ASCII, space, tab; no leading space or tab
        ("header_value", "Basic abc123==", True),
        ("header_value", "Bearer x\t", True),  # trailing whitespace stays allowed
        ("header_value", "", False),
        ("header_value", "x" * 8192, True),
        ("header_value", "x" * 8193, False),
        ("header_value", " Bearer x", False),
        ("header_value", "\tBearer x", False),
        ("header_value", "Bearer x\r\nX-Evil: 1", False),
        ("header_value", "Bearer x\n", False),  # what `$` would have accepted
        ("header_value", "Bearer x\x00", False),
        ("header_value", "Bearer ä", False),
    ],
)
def test_each_header_check_pins_its_character_set_and_bounds(check, value, ok):
    checks = {
        "header_name": otlp._is_header_name,
        "vault": otlp._is_vault_name,
        "env": otlp._is_env_name,
        "header_value": otlp._is_header_value,
    }
    assert checks[check](value) is ok
    assert checks[check](None) is False


def test_the_export_session_follows_no_redirect_and_withholds_request_detail():
    import requests

    session = otlp._export_session(requests)
    assert session.max_redirects == 0
    assert type(session).request is not requests.Session.request


@pytest.mark.parametrize(
    "field_name,ref",
    [("traces_otlp_headers_secret", VAULT_ENTRY), ("traces_otlp_headers_env", "TRACE_KEY_VAR")],
)
def test_a_header_name_or_reference_with_a_final_newline_is_refused(field_name, ref):
    for headers in ({"Authorization\n": ref}, {"Authorization": ref + "\n"}):
        cfg = _cfg(traces_otlp_endpoint=REMOTE, **{field_name: headers})
        assert otlp.resolve(cfg, {}) is None


def test_process_wide_otel_headers_never_reach_a_config_endpoint():
    env = {otlp.ENV_HEADERS: "authorization=" + HEADER_VALUE.replace(" ", "%20")}
    assert otlp.resolve(_cfg(traces_otlp_endpoint=REMOTE), env).headers == ()
    dest = otlp.resolve(_cfg(), {**env, otlp.ENV_TRACES_ENDPOINT: LOOPBACK})
    assert dest.headers == (("", otlp.FROM_OTEL_ENV, otlp.ENV_HEADERS),)
    assert otlp.read_headers(dest, env) == {"authorization": HEADER_VALUE}


def test_a_missing_vault_entry_turns_export_off_and_logs_no_reference(
    tmp_path, monkeypatch, caplog
):
    from kiro_crew.secrets.vault import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    caplog.set_level(logging.DEBUG)
    cfg = _cfg(
        traces_otlp_endpoint=REMOTE, traces_otlp_headers_secret={"Authorization": VAULT_ENTRY}
    )
    dest = otlp.resolve(cfg, {})
    assert otlp.read_headers(dest) is None
    assert VAULT_ENTRY not in _messages(caplog)  # a pasted key can look like a name
    SecretVault(tmp_path).set_sync(VAULT_ENTRY, HEADER_VALUE)
    assert otlp.read_headers(dest) == {"Authorization": HEADER_VALUE}
    assert HEADER_VALUE not in _messages(caplog)


@pytest.mark.parametrize("key", ["traces_otlp_headers_secret", "traces_otlp_headers_env"])
def test_a_header_value_pasted_in_place_of_the_map_is_masked_in_the_load_warning(key, caplog):
    from kiro_crew.config.validation import validate_config_data

    caplog.set_level(logging.WARNING)
    data = validate_config_data({"telemetry": {"traces_enabled": True, key: HEADER_VALUE}})
    assert key not in data["telemetry"]  # dropped: the field default (no headers) applies
    assert f"telemetry.{key}" in _messages(caplog)
    assert HEADER_VALUE not in _messages(caplog)


#: A Basic credential's shape (base64 padding included); fake and split.
BASIC_CRED = "cGstbGYtZmFrZTpz" + "ay1sZi1mYWtl=="


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Basic " + BASIC_CRED,  # a header line, not name=value
        "authorization=Basic%20" + BASIC_CRED + ",bad name=x",
        "authorization=",  # an empty value
    ],
)
def test_a_malformed_otel_header_variable_turns_export_off_and_is_never_logged(raw, caplog):
    caplog.set_level(logging.DEBUG)
    env = {otlp.ENV_TRACES_ENDPOINT: LOOPBACK, otlp.ENV_HEADERS: raw}
    dest = otlp.resolve(_cfg(), env)
    assert otlp.read_headers(dest, env) is None
    assert BASIC_CRED not in _messages(caplog)
    assert otlp.ENV_HEADERS in _messages(caplog)


@pytest.mark.parametrize(
    "value",
    [" Basic " + BASIC_CRED, "\tBearer x", "Bearer x\r\nX-Evil: 1", "Basic " + BASIC_CRED + "\n"],
)
def test_a_header_value_requests_would_refuse_turns_export_off_unlogged(
    value, tmp_path, monkeypatch, caplog
):
    from kiro_crew.secrets.vault import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    caplog.set_level(logging.DEBUG)
    SecretVault(tmp_path).set_sync(VAULT_ENTRY, value)
    cfg = _cfg(
        traces_otlp_endpoint=REMOTE, traces_otlp_headers_secret={"Authorization": VAULT_ENTRY}
    )
    assert otlp.read_headers(otlp.resolve(cfg, {})) is None
    assert value.strip() not in _messages(caplog)


def test_a_credential_pasted_as_a_variable_name_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    pasted = "ghp_" + "NotARealTokenButShapedLikeOne0123"  # passes the name check
    cfg = _cfg(traces_otlp_endpoint=REMOTE, traces_otlp_headers_env={"Authorization": pasted})
    assert otlp.read_headers(otlp.resolve(cfg, {}), {}) is None
    assert pasted not in _messages(caplog)
    assert "Authorization" in _messages(caplog)


def test_a_policy_refusal_warns_on_every_build_even_after_metrics_warned(caplog):
    from kiro_crew.metrics import otlp_governance

    caplog.set_level(logging.WARNING)
    dest = otlp.resolve(_cfg(traces_otlp_endpoint=REMOTE), {})
    otlp_governance._dropped_hosts_warned.add("collector.example.com")
    try:
        for _ in range(2):
            with _deny_all_policy(), patch("kiro_crew.sel.sel"):
                assert otlp.build_exporter(dest) is None
    finally:
        otlp_governance._dropped_hosts_warned.discard("collector.example.com")
    refusals = [r for r in caplog.records if "refused by the security policy" in r.getMessage()]
    assert len(refusals) == 2


def test_an_unset_header_variable_turns_export_off():
    cfg = _cfg(traces_otlp_endpoint=REMOTE, traces_otlp_headers_env={"X-Key": "TRACE_KEY_VAR"})
    dest = otlp.resolve(cfg, {})
    assert otlp.read_headers(dest, {}) is None
    assert otlp.read_headers(dest, {"TRACE_KEY_VAR": "v"}) == {"X-Key": "v"}


# ── the policy gate ───────────────────────────────────────────────────────


def test_a_denied_host_gets_no_exporter_and_an_audited_denial(monkeypatch):
    dest = otlp.resolve(_cfg(traces_otlp_endpoint=REMOTE), {})
    with _deny_all_policy(), patch("kiro_crew.sel.sel") as sel:
        assert otlp.build_exporter(dest) is None
    row = sel.return_value.log_governance_decision.call_args.kwargs
    assert (row["outcome"], row["item"]) == ("denied", "otlp_hosts:collector.example.com")


def test_a_policy_installed_at_runtime_stops_the_next_span_export():
    from opentelemetry.sdk.trace.export import SpanExportResult

    sent = []

    class _Exporter:
        def export(self, spans):
            sent.append(spans)
            return SpanExportResult.SUCCESS

    exporter = _Exporter()
    with _deny_all_policy():
        deny = metrics_provider._policy_ceiling()
    installed = {"ceiling": None}  # what the context's ceiling reads at each export
    with patch.object(metrics_provider, "_policy_ceiling", lambda: installed["ceiling"]):
        metrics_provider.gate_trace_exporter(exporter, REMOTE)
        with patch("kiro_crew.sel.sel") as sel:
            assert exporter.export(["a"]) is SpanExportResult.SUCCESS
            installed["ceiling"] = deny
            assert exporter.export(["b"]) is SpanExportResult.FAILURE
    assert sent == [["a"]]
    assert sel.return_value.log_governance_decision.call_args.kwargs["outcome"] == "denied"


# ── what leaves ───────────────────────────────────────────────────────────


def _finished_span(attributes, link_attributes=None):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import Link

    memory = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(memory))
    tracer = tp.get_tracer("t")
    other = tracer.start_span("other")
    other.end()
    links = [Link(other.get_span_context(), link_attributes)] if link_attributes else []
    tracer.start_span("s", attributes=attributes, links=links).end()
    tp.shutdown()
    return memory.get_finished_spans()[-1]


def test_local_keys_are_stripped_from_spans_and_links():
    span = _finished_span(
        {"kirocrew.local.session_key": "dashboard_chat-7", "session.id": "h"},
        {"kirocrew.local.session_key": "x", "kirocrew.link": "cause"},
    )
    out = otlp.strip_local(span)
    assert dict(out.attributes) == {"session.id": "h"}
    assert dict(out.links[0].attributes) == {"kirocrew.link": "cause"}
    assert (out.context, out.start_time, out.end_time) == (
        span.context,
        span.start_time,
        span.end_time,
    )
    plain = _finished_span({"session.id": "h"})
    assert otlp.strip_local(plain) is plain


def test_otel_service_name_names_the_trace_resource_only(monkeypatch):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv(otlp.ENV_SERVICE_NAME, "my-agents")
    state = provider.new_state(SimpleSpanProcessor(InMemorySpanExporter()), "0" * 32, None)
    try:
        assert state.provider.resource.attributes["service.name"] == "my-agents"
    finally:
        state.provider.shutdown()
    assert otlp.service_name({otlp.ENV_SERVICE_NAME: "x" * 500}) == "kirocrew"
    assert otlp.service_name({}) == "kirocrew"


# ── lifecycle: processors, swaps, threads ─────────────────────────────────


class _Recording:
    """A stand-in for the gated OTLP exporter."""

    def __init__(self):
        self.batches: list = []
        self.closed = False

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        self.batches.append([s.name for s in spans])
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.closed = True


def _write_config(home, **telemetry):
    (home / "config.json").write_text(json.dumps({"telemetry": telemetry}), encoding="utf-8")


def _otel_threads():
    return {t for t in threading.enumerate() if "Otel" in t.name and t.is_alive()}


def test_both_processors_use_the_pinned_limits_whatever_the_env_says(tmp_path, monkeypatch):
    from opentelemetry.sdk.trace import export as sdk_export

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "1")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "99999999")
    _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=LOOPBACK)
    seen = []
    real = sdk_export.BatchSpanProcessor

    class _Spy(real):
        def __init__(self, exporter, **kw):
            seen.append(kw)
            super().__init__(exporter, **kw)

    monkeypatch.setattr(sdk_export, "BatchSpanProcessor", _Spy)
    monkeypatch.setattr(otlp, "build_exporter", lambda dest: _Recording())
    provider.reset_for_testing()
    try:
        assert provider.current() is not None
    finally:
        provider.reset_for_testing()
    assert seen == [dict(provider.BATCH_LIMITS)] * 2


def test_no_endpoint_starts_no_export_thread_and_reset_stops_the_one_it_adds(tmp_path, monkeypatch):
    from conftest_floor_restores import _live_exporter_threads

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _write_config(tmp_path, traces_enabled=True)
    monkeypatch.setattr(otlp, "build_exporter", lambda dest: _Recording())
    before = _otel_threads()
    provider.reset_for_testing()
    try:
        state = provider.current()
        assert len(_otel_threads() - before) == 1  # the local sink's only
        assert state.remote.attached is False
        _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=LOOPBACK)
        provider._apply_config_change()
        added = _otel_threads() - before
        assert len(added) == 2 and added <= _live_exporter_threads()  # the test guard sees both
    finally:
        provider.reset_for_testing()
    assert not (_otel_threads() - before)


def test_a_config_write_swaps_the_exporter_and_switching_off_stops_export(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=LOOPBACK)
    built: list = []
    monkeypatch.setattr(
        otlp, "build_exporter", lambda dest: built.append(_Recording()) or built[-1]
    )
    provider.reset_for_testing()
    try:
        state = provider.current()
        first = built[0]
        _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=REMOTE)
        provider._apply_config_change()
        assert first.closed and state.remote.destination.endpoint == REMOTE
        _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=REMOTE, retention_days=3)
        provider._apply_config_change()
        assert len(built) == 2  # the same destination keeps its exporter
        _write_config(tmp_path, traces_enabled=False, traces_otlp_endpoint=REMOTE)
        provider._apply_config_change()
        assert built[1].closed and state.remote.destination is None
        state.remote.export([])  # a span a still-open turn ends now goes nowhere
        assert built[1].batches == []
    finally:
        provider.reset_for_testing()


def test_a_refused_exporter_is_retried_on_the_next_write(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=LOOPBACK)
    answers = [None, _Recording()]
    monkeypatch.setattr(otlp, "build_exporter", lambda dest: answers.pop(0))
    provider.reset_for_testing()
    try:
        state = provider.current()
        assert state.remote.destination is None and state.remote.attached is False
        _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=LOOPBACK, max_total_mb=5)
        provider._apply_config_change()
        assert state.remote.destination is not None and state.remote.attached
    finally:
        provider.reset_for_testing()


def test_the_sandbox_probe_stays_single_threaded_with_trace_export_on(tmp_path):
    """Both batch threads restart in every FORK child, which would make a forked
    userns probe multithreaded, so the probe must run in a fresh interpreter that
    no at-fork hook reaches."""
    if sys.platform != "linux":
        pytest.skip("the userns probe is Linux-only")
    program = textwrap.dedent("""
        import os, threading
        from unittest.mock import patch
        from kiro_crew import sandbox_probe as sb
        from kiro_crew.tracing import otlp, provider

        class Target:
            def export(self, spans):
                from opentelemetry.sdk.trace.export import SpanExportResult
                return SpanExportResult.SUCCESS
            def shutdown(self):
                pass

        with patch.object(otlp, "build_exporter", lambda dest: Target()):
            assert provider.current() is not None
        names = [t.name for t in threading.enumerate() if "Otel" in t.name]
        assert len(names) == 2, names
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.write(w, str(os.stat("/proc/self/task").st_nlink - 2).encode())
            os._exit(0)
        os.waitpid(pid, 0)
        assert int(os.read(r, 8)) > 1, "fork hooks no longer restart the batch threads"
        verdict = sb._probe_unshare_via_spawn()
        assert verdict is not None
        assert not sb._probe_reason_is_multithreaded_collapse(verdict[2]), verdict
        provider.shutdown()
        print("ok")
        """)
    (tmp_path / "config.json").write_text(
        json.dumps({"telemetry": {"traces_enabled": True, "traces_otlp_endpoint": LOOPBACK}}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "KIROCREW_HOME": str(tmp_path)},
        cwd=str(tmp_path),
        timeout=default_timeout(),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


# ── the wire (needs the otlp extra) ───────────────────────────────────────


def _otlp_available() -> bool:
    try:
        import opentelemetry.exporter.otlp.proto.http.trace_exporter  # noqa: F401
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


requires_otlp = pytest.mark.skipif(
    not _otlp_available(), reason='needs the optional extra: pip install "kirocrew[otlp]"'
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.received.append((self.path, dict(self.headers), body))  # type: ignore[attr-defined]
        # A hostile collector can echo the auth header back in its status line.
        echo = self.headers.get("Authorization") if self.server.echo else None  # type: ignore[attr-defined]
        self.send_response(self.server.status, echo)  # type: ignore[attr-defined]
        if self.server.location:  # type: ignore[attr-defined]
            self.send_header("Location", self.server.location)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        return


class _Receiver:
    """A loopback-only OTLP/HTTP traces receiver for one test."""

    def __init__(self, status=200, location="", echo=False):
        self._server = LoopbackHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.received = []  # type: ignore[attr-defined]
        self._server.status = status  # type: ignore[attr-defined]
        self._server.location = location  # type: ignore[attr-defined]
        self._server.echo = echo  # type: ignore[attr-defined]
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
        return f"http://{host}:{port}/v1/traces"

    @property
    def received(self):
        return self._server.received  # type: ignore[attr-defined]


def _value(any_value):
    return getattr(any_value, any_value.WhichOneof("value"))


def _decoded(received):
    """``(resource attrs, {span name: attrs})`` over every request received."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    resource, spans = {}, {}
    for _path, _headers, body in received:
        request = trace_service_pb2.ExportTraceServiceRequest()
        request.ParseFromString(body)
        for rs in request.resource_spans:
            resource.update({kv.key: _value(kv.value) for kv in rs.resource.attributes})
            for ss in rs.scope_spans:
                for span in ss.spans:
                    spans[span.name] = {kv.key: _value(kv.value) for kv in span.attributes}
    return resource, spans


class _Source:
    session_id = "acp-1"
    _session_key = "dashboard_chat-7"
    _model = "auto"
    _resolved_model_id = ""
    active_agent = "kirocrew-default"


async def _one_turn():
    async def events():
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage(credits=1.5))

    async for _ in turns.traced(_Source(), events()):
        pass


def _run_turn_and_flush():
    async def run():
        await asyncio.to_thread(provider.current)  # what the boot read does
        await _one_turn()
        await provider.flush_for_exit()

    try:
        asyncio.run(run())
    finally:
        turns.reset_for_testing()
        provider.reset_for_testing()


@requires_otlp
def test_a_traced_turn_reaches_a_loopback_receiver_with_its_vault_header(tmp_path, monkeypatch):
    from kiro_crew.secrets.vault import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv(otlp.ENV_SERVICE_NAME, "crew-under-test")
    SecretVault(tmp_path).set_sync(VAULT_ENTRY, HEADER_VALUE)
    with _Receiver() as receiver, _no_policy(), patch("kiro_crew.sel.sel"):
        _write_config(
            tmp_path,
            traces_enabled=True,
            traces_otlp_endpoint=receiver.endpoint,
            traces_otlp_headers_secret={"Authorization": VAULT_ENTRY},
        )
        _run_turn_and_flush()
        received = list(receiver.received)
    assert received, "no OTLP request reached the receiver"
    assert {path for path, _h, _b in received} == {"/v1/traces"}
    assert {h.get("Authorization") for _p, h, _b in received} == {HEADER_VALUE}
    resource, spans = _decoded(received)
    assert resource["service.name"] == "crew-under-test"
    assert set(spans) == {"invoke_agent kirocrew-default", "user.prompt", "chat", "turn.complete"}
    chat = spans["chat"]
    assert chat["langfuse.observation.type"] == "generation"
    assert json.loads(chat["langfuse.observation.usage_details"]) == {"credits": 1.5}
    assert chat["kirocrew.usage.credits"] == 1.5
    every_key = {key for attrs in spans.values() for key in attrs}
    assert not [k for k in every_key if k.startswith("kirocrew.local.")]
    assert "session.id" in spans["invoke_agent kirocrew-default"]
    # The local sink still holds the raw key the wire never saw.
    local = "".join(
        p.read_text(encoding="utf-8") for p in (tmp_path / "traces").glob("spans-*.jsonl")
    )
    assert "kirocrew.local.session_key" in local
    assert HEADER_VALUE not in local


@requires_otlp
@pytest.mark.parametrize("status", [307, 308])
def test_a_redirect_is_never_followed_so_no_unchecked_host_gets_spans_or_headers(
    tmp_path, monkeypatch, status
):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("TRACE_KEY_VAR", HEADER_VALUE)
    with _Receiver() as elsewhere:
        with _Receiver(status=status, location=elsewhere.endpoint) as collector:
            with _no_policy(), patch("kiro_crew.sel.sel"):
                _write_config(
                    tmp_path,
                    traces_enabled=True,
                    traces_otlp_endpoint=collector.endpoint,
                    traces_otlp_headers_env={"X-Api-Key": "TRACE_KEY_VAR"},
                )
                _run_turn_and_flush()
            assert collector.received, "the configured collector was asked"
        assert elsewhere.received == []


@requires_otlp
@pytest.mark.parametrize("vault_header", [True, False])
def test_a_netrc_entry_for_the_collector_host_is_never_sent(tmp_path, monkeypatch, vault_header):
    from kiro_crew.secrets.vault import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    netrc = tmp_path / "netrc"
    # Fake and split, like every credential in this file.
    netrc.write_text("machine 127.0.0.1 login unrelated password not-" + "for-tracing\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    headers = {}
    if vault_header:
        SecretVault(tmp_path).set_sync(VAULT_ENTRY, HEADER_VALUE)
        headers = {"traces_otlp_headers_secret": {"Authorization": VAULT_ENTRY}}
    with _Receiver() as receiver, _no_policy(), patch("kiro_crew.sel.sel"):
        _write_config(
            tmp_path, traces_enabled=True, traces_otlp_endpoint=receiver.endpoint, **headers
        )
        _run_turn_and_flush()
        received = list(receiver.received)
    assert received
    sent = {h.get("Authorization") for _p, h, _b in received}
    assert sent == ({HEADER_VALUE} if vault_header else {None})


@requires_otlp
def test_a_rotated_vault_key_is_sent_after_tracing_is_switched_off_and_on(tmp_path, monkeypatch):
    """What the guide tells an operator to do after rotating a key."""
    from kiro_crew.secrets.vault import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    vault = SecretVault(tmp_path)
    vault.set_sync(VAULT_ENTRY, HEADER_VALUE)
    rotated = HEADER_VALUE + "-rotated"
    with _Receiver() as receiver, _no_policy(), patch("kiro_crew.sel.sel"):
        settings = {
            "traces_otlp_endpoint": receiver.endpoint,
            "traces_otlp_headers_secret": {"Authorization": VAULT_ENTRY},
        }
        _write_config(tmp_path, traces_enabled=True, **settings)

        async def run():
            await asyncio.to_thread(provider.current)
            await _one_turn()
            provider.current().provider.force_flush()
            vault.set_sync(VAULT_ENTRY, rotated)
            for enabled in (False, True):
                _write_config(tmp_path, traces_enabled=enabled, **settings)
                await asyncio.to_thread(provider._apply_config_change)
            await _one_turn()
            await provider.flush_for_exit()

        try:
            asyncio.run(run())
        finally:
            turns.reset_for_testing()
            provider.reset_for_testing()
        sent = [h.get("Authorization") for _p, h, _b in receiver.received]
    assert sent[0] == HEADER_VALUE and sent[-1] == rotated


@requires_otlp
def test_a_denied_host_receives_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    with _Receiver() as receiver, _deny_all_policy(), patch("kiro_crew.sel.sel"):
        _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=receiver.endpoint)
        _run_turn_and_flush()
        assert receiver.received == []
    assert list((tmp_path / "traces").glob("spans-*.jsonl")), "the local sink keeps working"


@requires_otlp
def test_an_env_endpoint_sends_only_while_the_switch_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    with _Receiver() as receiver, _no_policy(), patch("kiro_crew.sel.sel"):
        monkeypatch.setenv(otlp.ENV_ENDPOINT, receiver.endpoint.removesuffix("/v1/traces"))
        _write_config(tmp_path, traces_enabled=False)
        _run_turn_and_flush()
        assert receiver.received == []
        _write_config(tmp_path, traces_enabled=True)
        _run_turn_and_flush()
        assert receiver.received


@requires_otlp
@pytest.mark.parametrize("status", [200, 400, 401])  # non-retryable: no timing involved
def test_no_header_value_is_logged_on_any_export_outcome(tmp_path, monkeypatch, caplog, status):
    from kiro_crew.secrets.vault import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SecretVault(tmp_path).set_sync(VAULT_ENTRY, HEADER_VALUE)
    caplog.set_level(logging.DEBUG)  # urllib3 and requests included
    with _Receiver(status=status) as receiver, _no_policy(), patch("kiro_crew.sel.sel"):
        _write_config(
            tmp_path,
            traces_enabled=True,
            traces_otlp_endpoint=receiver.endpoint,
            traces_otlp_headers_secret={"Authorization": VAULT_ENTRY},
        )
        _run_turn_and_flush()
        assert receiver.received
    text = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert HEADER_VALUE not in text
    assert HEADER_VALUE.split()[-1] not in text


@requires_otlp
@pytest.mark.parametrize("echoed_by", ["library_error", "collector_reason"])
def test_a_header_echoed_back_by_a_failure_reaches_no_log_record(
    tmp_path, monkeypatch, caplog, echoed_by
):
    """Defence in depth: the value passes validation, then something quotes it."""
    import requests

    from kiro_crew.secrets.vault import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    SecretVault(tmp_path).set_sync(VAULT_ENTRY, HEADER_VALUE)
    if echoed_by == "library_error":

        def send(self, request, **kwargs):
            # Non-retryable, so one attempt and no backoff.
            raise requests.exceptions.InvalidHeader(f"refused {request.headers['Authorization']}")

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    caplog.set_level(logging.DEBUG)
    echo = echoed_by == "collector_reason"
    with _Receiver(status=401, echo=echo) as receiver:
        with _no_policy(), patch("kiro_crew.sel.sel"):
            _write_config(
                tmp_path,
                traces_enabled=True,
                traces_otlp_endpoint=receiver.endpoint,
                traces_otlp_headers_secret={"Authorization": VAULT_ENTRY},
            )
            _run_turn_and_flush()
        if echo:
            assert receiver.received, "the collector was reached and echoed the header"
    formatter = logging.Formatter("%(name)s %(message)s")  # tracebacks included
    text = "\n".join(formatter.format(record) for record in caplog.records)
    assert "Failed to export span batch" in text, "the failure was logged"
    assert HEADER_VALUE.split()[-1] not in text


@requires_otlp
def test_the_exit_flush_sends_spans_still_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    # Far past the test's runtime, so only the exit flush can send.
    monkeypatch.setitem(provider.BATCH_LIMITS, "schedule_delay_millis", 3_600_000)
    with _Receiver() as receiver, _no_policy(), patch("kiro_crew.sel.sel"):
        _write_config(tmp_path, traces_enabled=True, traces_otlp_endpoint=receiver.endpoint)

        async def run():
            await asyncio.to_thread(provider.current)
            await _one_turn()
            # No scheduled export can run first: every span is still queued.
            assert receiver.received == []
            await provider.flush_for_exit()

        try:
            asyncio.run(run())
        finally:
            turns.reset_for_testing()
            provider.reset_for_testing()
        assert len(_decoded(receiver.received)[1]) == 4
