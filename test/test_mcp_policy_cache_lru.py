"""A session that resolved its tool policy once is not refused when the gateway is slow.

The cache holds ``_EXCLUDED_TOOLS_CACHE_MAX`` sessions. Evicting the oldest
inserted entry dropped a session that is still calling tools, and its next read of
a busy gateway timed out and refused every tool. The cache now evicts the least
recently used session, an evicted session keeps a bounded last-good record that a
timed-out read answers from, and a session that never resolved still refuses.

The gateway here is a real loopback HTTP server whose answer delay is set per test.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import kiro_crew.mcp_shared as mcp_shared
import kiro_crew.mcp_shared_policy as policy_mod

_delay = {"seconds": 0.0}
_status = {"code": 200}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        time.sleep(_delay["seconds"])
        if _status["code"] == 409:
            body = json.dumps({"code": "policy_unreadable", "reason": "spec"}).encode("ascii")
        else:
            body = json.dumps({"exclude": ["denied_tool"]}).encode("ascii")
        try:
            self.send_response(_status["code"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the client gave up on a slow answer

    def log_message(self, *args: object) -> None:
        pass


class _Server(http.server.ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Skips HTTPServer's socket.getfqdn lookup, which can stall on macOS.
        socketserver.TCPServer.server_bind(self)
        self.server_port = self.server_address[1]


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    server = _Server(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "run").mkdir(mode=0o700)
    (tmp_path / "run" / f"gateway-{port}.secret").write_text("synthetic", encoding="ascii")
    monkeypatch.setenv("KIROCREW_BOUND_PORT", str(port))
    monkeypatch.delenv("KIROCREW_PORT", raising=False)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:in-use")
    for name, value in (
        ("_last_failure_time", 0.0),
        ("_last_probe_time", 0.0),
        ("_last_startup_race_time", 0.0),
        ("_last_startup_race_key", ""),
        ("_failure_count", 0),
        ("_last_failure_class", ""),
    ):
        monkeypatch.setattr(mcp_shared, name, value)
    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_included_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_retired_policy_by_session", {})
    monkeypatch.setattr(mcp_shared, "_policy_last_used", {})
    _delay["seconds"] = 0.0
    _status["code"] = 200
    yield
    _delay["seconds"] = 0.0
    _status["code"] = 200
    server.shutdown()
    server.server_close()


def test_session_in_use_survives_a_full_cache_and_a_busy_gateway(gateway: None) -> None:
    assert policy_mod._resolve_tool_policy("dashboard:in-use").excluded == {"denied_tool"}
    for n in range(policy_mod._EXCLUDED_TOOLS_CACHE_MAX - 1):
        policy_mod._resolve_tool_policy(f"dashboard:other-{n}")
        # The session keeps calling tools between the newer sessions' first reads.
        assert policy_mod._resolve_tool_policy("dashboard:in-use").unresolved == ""
    policy_mod._resolve_tool_policy("dashboard:one-more")

    _delay["seconds"] = 6.5  # past the 5 s read timeout
    answered = policy_mod._resolve_tool_policy("dashboard:in-use", ignore_negative_cache=True)

    assert answered.unresolved == ""
    assert answered.excluded == {"denied_tool"}


def test_evicted_session_is_served_its_last_good_policy_on_a_busy_gateway(gateway: None) -> None:
    assert policy_mod._resolve_tool_policy("dashboard:idle").excluded == {"denied_tool"}
    for n in range(policy_mod._EXCLUDED_TOOLS_CACHE_MAX):
        policy_mod._resolve_tool_policy(f"dashboard:other-{n}")
    assert "dashboard:idle" not in policy_mod._excluded_tools_by_session

    _delay["seconds"] = 6.5
    answered = policy_mod._resolve_tool_policy("dashboard:idle", ignore_negative_cache=True)

    assert answered.unresolved == ""
    assert answered.excluded == {"denied_tool"}


def test_explicit_refusal_drops_the_last_good_policy(gateway: None) -> None:
    policy_mod._resolve_tool_policy("dashboard:idle")
    for n in range(policy_mod._EXCLUDED_TOOLS_CACHE_MAX):
        policy_mod._resolve_tool_policy(f"dashboard:other-{n}")
    assert "dashboard:idle" in policy_mod._retired_policy_by_session

    _status["code"] = 409  # the agent spec became unreadable
    refused = policy_mod._resolve_tool_policy("dashboard:idle", ignore_negative_cache=True)
    assert refused.unresolved == "policy_unreadable"

    _status["code"] = 200
    _delay["seconds"] = 6.5
    answered = policy_mod._resolve_tool_policy("dashboard:idle", ignore_negative_cache=True)

    assert answered.unresolved == "resolution_failed"
    assert answered.excluded == frozenset()


def test_last_good_record_is_bounded(gateway: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy_mod, "_EXCLUDED_TOOLS_CACHE_MAX", 2)
    monkeypatch.setattr(policy_mod, "_RETIRED_POLICY_CACHE_MAX", 3)
    for n in range(10):
        policy_mod._resolve_tool_policy(f"dashboard:s-{n}")

    assert len(policy_mod._excluded_tools_by_session) == 2
    assert len(policy_mod._retired_policy_by_session) == 3


def test_session_that_never_resolved_is_still_refused_on_a_busy_gateway(gateway: None) -> None:
    _delay["seconds"] = 6.5
    answered = policy_mod._resolve_tool_policy("dashboard:new", ignore_negative_cache=True)

    assert answered.unresolved == "resolution_failed"
    assert answered.excluded == frozenset()
