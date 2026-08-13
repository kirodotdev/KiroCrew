"""A refused gateway callback re-resolves its base once and replays.

The port a ``--port``-started gateway is bound to is recorded only in its run
marker, so an MCP tool server that resolved its base before the gateway came up
(or before it moved) holds a stale base. These tests lock in the recovery path
this PR adds: every verb helper routes through ``mcp_core._send``, which on a
refused connection drops the resolution caches, re-resolves, and replays the
request exactly once — and only when re-resolution actually produced a
different base. ``mcp_computer._invoke`` applies the same rule to its one
request path.
"""

from __future__ import annotations

import importlib
import socket
import urllib.error
from typing import Any

import pytest


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module("kiro_crew.mcp_core")
    monkeypatch.setattr(module, "_API_PORT", None)
    monkeypatch.setattr(module, "_API", None)
    monkeypatch.setattr(module, "_API_UNIX_SOCKET", None)
    monkeypatch.setattr(module, "_internal_secret", lambda: "s")
    monkeypatch.setattr(module, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(module, "_session_key_header_error", lambda sk: None)
    return module


def _bases(monkeypatch: pytest.MonkeyPatch, mcp: Any, sequence: list[str]) -> None:
    """Feed ``_api_base`` a scripted resolution sequence (first attempts)."""
    it = iter(sequence)
    last = sequence[-1]
    monkeypatch.setattr(mcp, "_api_base", lambda: next(it, last))


def _retry_resolution(monkeypatch: pytest.MonkeyPatch, mcp: Any, port: int, source: str) -> None:
    """Script what ``_resolve_api_port`` answers when the replay re-resolves."""
    monkeypatch.setattr(mcp, "_resolve_api_port", lambda: (port, source))


class TestInvalidation:
    def test_invalidate_drops_all_three_caches(self, mcp: Any, monkeypatch) -> None:
        """URL, port and socket path must expire together — both transports
        derive from one resolution, and clearing only the URL would leave the
        socket aimed at the old gateway."""
        monkeypatch.setattr(mcp, "_API_PORT", 7788)
        monkeypatch.setattr(mcp, "_API", "http://127.0.0.1:7788")
        monkeypatch.setattr(mcp, "_API_UNIX_SOCKET", "/tmp/dashboard-7788.sock")
        mcp._invalidate_api_base()
        assert mcp._API_PORT is None
        assert mcp._API is None
        assert mcp._API_UNIX_SOCKET is None


@pytest.fixture
def refusing_gateway(mcp: Any, monkeypatch):
    """First attempt refused on the default port; re-resolution proves 7788."""
    urls: list[str] = []
    _bases(monkeypatch, mcp, ["http://127.0.0.1:5476"])
    _retry_resolution(monkeypatch, mcp, 7788, "marker")

    def fake_open(req, timeout=None):
        urls.append(req.full_url)
        if ":5476" in req.full_url:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        return _Resp()

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    return mcp, urls


class _Resp:
    def __init__(self, payload: bytes = b'{"ok": true}') -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


@pytest.mark.parametrize("verb", ["_post", "_get", "_patch", "_put", "_delete"])
def test_every_verb_rediscovers_and_replays(refusing_gateway, verb: str) -> None:
    """A stale base must not survive in one verb after another has learned better."""
    mcp, urls = refusing_gateway
    call = getattr(mcp, verb)
    out = call("/api/x") if verb == "_get" else call("/api/x", {"k": "v"})
    assert out == {"ok": True}
    assert len(urls) == 2
    assert ":5476" in urls[0]
    assert ":7788" in urls[1]


def test_no_replay_when_rediscovery_returns_the_same_base(mcp: Any, monkeypatch) -> None:
    """Retrying an unchanged dead base would only double the latency."""
    attempts: list[str] = []
    _bases(monkeypatch, mcp, ["http://127.0.0.1:7788"])
    _retry_resolution(monkeypatch, mcp, 7788, "marker")

    def fake_open(req, timeout=None):
        attempts.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    assert "error" in mcp._post("/api/x", {"k": "v"})
    assert len(attempts) == 1


def test_no_replay_when_rediscovery_falls_through_to_default(mcp: Any, monkeypatch) -> None:
    """A no-evidence default fall-through must never receive the replay.

    The marker gateway exited after the first resolution; re-resolution finds
    nothing and falls through to the default port. A listener there is
    unverified — it could be any local process — and the request carries the
    internal secret, so the replay is skipped even though the base differs.
    """
    attempts: list[str] = []
    _bases(monkeypatch, mcp, ["http://127.0.0.1:9999"])
    _retry_resolution(monkeypatch, mcp, 5476, "default")

    def fake_open(req, timeout=None):
        attempts.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/x", {"k": "v"})
    assert "error" in out
    assert "transport_error" not in out
    assert len(attempts) == 1  # nothing was sent to the unverified default port


def test_only_post_reports_transport_error(mcp: Any, monkeypatch) -> None:
    """``transport_error`` is spawn_run's signal; other verbs keep their shape."""
    _bases(monkeypatch, mcp, ["http://127.0.0.1:5476"])

    def fake_open(req, timeout=None):
        raise urllib.error.URLError(socket.timeout("slow"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    assert mcp._post("/api/x", {}).get("transport_error") is True
    assert "transport_error" not in mcp._get("/api/x")
    assert "transport_error" not in mcp._patch("/api/x", {})


def test_replay_that_fails_after_connecting_stays_ambiguous(mcp: Any, monkeypatch) -> None:
    """A spawn accepted by the rediscovered gateway must not be reported as lost.

    spawn_run reconciles a member down on a definite rejection. If the replay
    reaches the gateway and only the response read fails, acceptance is
    undetermined — collapsing that to a plain error orphans a still-running
    subagent and closes the batch early.
    """
    _bases(monkeypatch, mcp, ["http://127.0.0.1:5476"])
    _retry_resolution(monkeypatch, mcp, 7788, "marker")

    def fake_open(req, timeout=None):
        if ":5476" in req.full_url:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        raise TimeoutError("read timed out after the spawn was accepted")

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/spawn", {"tasks": ["x"]})
    assert out.get("transport_error") is True


def test_replay_refused_again_is_a_definite_rejection(mcp: Any, monkeypatch) -> None:
    """Refused on both bases means nothing was ever accepted."""
    _bases(monkeypatch, mcp, ["http://127.0.0.1:5476"])
    _retry_resolution(monkeypatch, mcp, 7788, "marker")

    def fake_open(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/spawn", {"tasks": ["x"]})
    assert "error" in out
    assert "transport_error" not in out


def test_http_error_on_replay_surfaces_the_backend_body(mcp: Any, monkeypatch) -> None:
    """A 4xx from the rediscovered gateway must decode like a first-attempt 4xx."""
    _bases(monkeypatch, mcp, ["http://127.0.0.1:5476"])
    _retry_resolution(monkeypatch, mcp, 7788, "marker")

    def fake_open(req, timeout=None):
        if ":5476" in req.full_url:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", None, None  # type: ignore[arg-type]
        )

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/x", {})
    assert "error" in out
    assert "transport_error" not in out


class TestMcpComputerReplay:
    """``mcp_computer._invoke`` — the same refused-once-replay rule."""

    @pytest.fixture
    def computer(self, mcp: Any, monkeypatch) -> Any:
        module = importlib.import_module("kiro_crew.mcp_computer")
        monkeypatch.setattr(module, "_internal_secret", lambda: "s")
        return module

    def test_refusal_rediscovers_and_replays(self, computer: Any, mcp: Any, monkeypatch) -> None:
        urls: list[str] = []
        monkeypatch.setattr(computer, "_api_base", lambda: "http://127.0.0.1:5476")
        monkeypatch.setattr(computer, "_resolve_api_port", lambda: (7788, "marker"))

        def fake_open(req, timeout=None):
            urls.append(req.full_url)
            if ":5476" in req.full_url:
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            return _Resp(b'{"text": "done"}')

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        out = computer._invoke("dashboard:chat-1", "computer_get_state", {})
        assert out == {"text": "done"}
        assert len(urls) == 2

    def test_refusal_with_unchanged_base_is_reported(self, computer: Any, monkeypatch) -> None:
        attempts: list[str] = []
        monkeypatch.setattr(computer, "_api_base", lambda: "http://127.0.0.1:7788")
        monkeypatch.setattr(computer, "_resolve_api_port", lambda: (7788, "marker"))

        def fake_open(req, timeout=None):
            attempts.append(req.full_url)
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        out = computer._invoke("dashboard:chat-1", "computer_get_state", {})
        assert "error" in out
        assert len(attempts) == 1

    def test_no_replay_when_rediscovery_falls_through_to_default(
        self, computer: Any, monkeypatch
    ) -> None:
        """Same no-evidence rule as mcp_core._send: an unverified default-port
        listener must never receive the replayed secret-bearing request."""
        attempts: list[str] = []
        monkeypatch.setattr(computer, "_api_base", lambda: "http://127.0.0.1:9999")
        monkeypatch.setattr(computer, "_resolve_api_port", lambda: (5476, "default"))

        def fake_open(req, timeout=None):
            attempts.append(req.full_url)
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        out = computer._invoke("dashboard:chat-1", "computer_get_state", {})
        assert "error" in out
        assert len(attempts) == 1  # nothing was sent to the unverified default port

    def test_http_error_on_replay_surfaces_the_backend_body(
        self, computer: Any, monkeypatch
    ) -> None:
        """A 4xx from the reached moved gateway must decode like a first-attempt
        4xx, not collapse into a stale 'gateway unreachable: refused'."""
        import io

        monkeypatch.setattr(computer, "_api_base", lambda: "http://127.0.0.1:5476")
        monkeypatch.setattr(computer, "_resolve_api_port", lambda: (7788, "marker"))

        def fake_open(req, timeout=None):
            if ":5476" in req.full_url:
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                None,  # type: ignore[arg-type]
                io.BytesIO(b'{"error": "unknown session"}'),
            )

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        out = computer._invoke("dashboard:chat-1", "computer_get_state", {})
        assert out == {"error": "unknown session"}
