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
    """Feed the first-attempt resolution a scripted sequence of bases.

    Scripted at ``_resolve_api_target`` rather than ``_api_base``: one attempt
    now resolves its ``(base, socket_path)`` pair once and threads both halves
    through (#4106 item 1). The empty socket keeps these TCP-only cases dialling
    the base they name, which is what they assert on.
    """
    it = iter(sequence)
    last = sequence[-1]
    monkeypatch.setattr(mcp, "_resolve_api_target", lambda: (next(it, last), ""))


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

    def fake_open(req, timeout=None, unix_socket_path=None):
        urls.append(req.full_url)
        if ":5476" in req.full_url:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        return _Resp()

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    return mcp, urls


def _scripted_resolutions(
    monkeypatch: pytest.MonkeyPatch, mcp: Any, sequence: list[tuple[int, str]]
) -> list[tuple[int, str]]:
    """Script ``_resolve_api_port`` and RECORD every run of the discovery chain.

    ``_resolve_api_port`` is the one seam the whole chain funnels through, and
    it is what actually forks ``lsof`` on the marker step -- so counting calls
    here counts real discovery cost without needing a gateway, a marker, or a
    live port. The returned list is the count.
    """
    runs: list[tuple[int, str]] = []
    it = iter(sequence)
    last = sequence[-1]

    def _resolve() -> tuple[int, str]:
        answer = next(it, last)
        runs.append(answer)
        return answer

    monkeypatch.setattr(mcp, "_resolve_api_port", _resolve)
    return runs


def _capture_transport(
    monkeypatch: pytest.MonkeyPatch, mcp: Any, *, refuse: str | None = None
) -> list[tuple[str, str]]:
    """Record the ``(url, unix_socket_path)`` each attempt actually dials.

    Patched at ``loopback_urlopen`` rather than at ``_api_urlopen``: the socket
    path is chosen inside the latter, and a fake there would hide the very pair
    these tests are about.
    """
    seen: list[tuple[str, str]] = []

    def fake_loopback(req, timeout=None, unix_socket_path=None):
        seen.append((req.full_url, str(unix_socket_path or "")))
        if refuse is not None and refuse in req.full_url:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        return _Resp()

    monkeypatch.setattr(mcp, "loopback_urlopen", fake_loopback)
    return seen


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

    def fake_open(req, timeout=None, unix_socket_path=None):
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

    def fake_open(req, timeout=None, unix_socket_path=None):
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

    def fake_open(req, timeout=None, unix_socket_path=None):
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

    def fake_open(req, timeout=None, unix_socket_path=None):
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

    def fake_open(req, timeout=None, unix_socket_path=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/spawn", {"tasks": ["x"]})
    assert "error" in out
    assert "transport_error" not in out


def test_http_error_on_replay_surfaces_the_backend_body(mcp: Any, monkeypatch) -> None:
    """A 4xx from the rediscovered gateway must decode like a first-attempt 4xx."""
    _bases(monkeypatch, mcp, ["http://127.0.0.1:5476"])
    _retry_resolution(monkeypatch, mcp, 7788, "marker")

    def fake_open(req, timeout=None, unix_socket_path=None):
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

        def fake_open(req, timeout=None, unix_socket_path=None):
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

        def fake_open(req, timeout=None, unix_socket_path=None):
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

        def fake_open(req, timeout=None, unix_socket_path=None):
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

        def fake_open(req, timeout=None, unix_socket_path=None):
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


class TestOneResolutionPerAttempt:
    """#4106 item 1: one request attempt resolves ONE ``(base, socket_path)`` pair.

    On a marker-discovered port -- the zero-config case -- neither the port, the
    base nor the socket path is cached: a marker resolution is proven for that
    instant only, so every secret-bearing request must re-run the discovery
    chain and re-prove ownership. That rule is right, but ``_api_base()`` and
    ``_api_unix_socket()`` each apply it INDEPENDENTLY, so one attempt runs the
    chain twice and its two transports are derived from two different
    resolutions. ``loopback_urlopen`` prefers the socket, so the component that
    actually carries the request is the one the refusal logic never inspected.

    These tests pin the pair, not the plumbing: what matters is that one attempt
    resolves once, and that both of its transports name the same gateway.
    """

    def test_one_attempt_resolves_the_chain_once(self, mcp: Any, monkeypatch) -> None:
        """Two independent resolutions per attempt is two ``lsof`` forks per
        gateway call, on hot callers (the 5s keepalive poll, spawn batches)."""
        runs = _scripted_resolutions(monkeypatch, mcp, [(7788, "marker")])
        _capture_transport(monkeypatch, mcp)

        assert mcp._send("/api/x", headers={}, method="GET") == {"ok": True}
        assert len(runs) == 1, f"one attempt ran the discovery chain {len(runs)} times: {runs}"

    def test_both_transports_of_one_attempt_name_one_gateway(self, mcp: Any, monkeypatch) -> None:
        """The disagreement is reachable, not theoretical.

        A gateway that exits and is replaced between the two resolutions is
        exactly the event the no-caching rule exists for, so the two calls can
        legitimately answer differently. When they do, the TCP base aims at one
        gateway and the preferred unix socket at another — and the socket wins.
        """
        _scripted_resolutions(monkeypatch, mcp, [(7788, "marker"), (9999, "marker")])
        seen = _capture_transport(monkeypatch, mcp)

        assert mcp._send("/api/x", headers={}, method="GET") == {"ok": True}
        ((url, sock),) = seen
        assert ":7788" in url
        assert (
            "7788" in sock and "9999" not in sock
        ), f"one attempt aimed TCP at {url} and the unix socket at {sock}"

    def test_a_stable_source_still_resolves_once_and_pins(self, mcp: Any, monkeypatch) -> None:
        """Control: the caching rule for stable sources must not change.

        ``env`` / ``bound`` / ``config`` are user decisions, cached for the
        process lifetime. This already resolved once; it must still resolve
        once, and still pin.
        """
        runs = _scripted_resolutions(monkeypatch, mcp, [(7788, "env")])
        _capture_transport(monkeypatch, mcp)

        assert mcp._send("/api/x", headers={}, method="GET") == {"ok": True}
        assert len(runs) == 1
        assert mcp._API_PORT == 7788, "a stable source must still be pinned"

    def test_a_seeded_pair_is_handed_back_untouched(self, mcp: Any, monkeypatch) -> None:
        """The resolver must answer exactly what the standalone getters answer.

        ``_api_base`` and ``_api_unix_socket`` return a populated memo whatever
        the port cache holds, and the cache comment documents that tests may
        pre-seed any of the three with a concrete value —
        ``test_dashboard_peer_auth`` seeds the socket and base to point the
        client at a real ``AF_UNIX`` server.

        A resolver that additionally demanded ``_API_PORT`` would discard that
        seeded pair and re-resolve, sending the request somewhere else entirely.
        That is drift between ``_send`` and the getters, which is the exact
        thing this function exists to remove — so it is asserted here rather
        than left to a POSIX-only integration test to catch.
        """
        monkeypatch.setattr(mcp, "_API_PORT", None)
        monkeypatch.setattr(mcp, "_API", "http://127.0.0.1:1")
        monkeypatch.setattr(mcp, "_API_UNIX_SOCKET", "/tmp/seeded.sock")
        runs = _scripted_resolutions(monkeypatch, mcp, [(7788, "marker")])

        assert mcp._resolve_api_target() == ("http://127.0.0.1:1", "/tmp/seeded.sock")
        assert runs == [], "a populated memo must not re-run the discovery chain"
        assert mcp._api_base() == "http://127.0.0.1:1"
        assert mcp._api_unix_socket() == "/tmp/seeded.sock"

    def test_the_replay_resolves_one_fresh_pair_and_uses_both_halves(
        self, mcp: Any, monkeypatch
    ) -> None:
        """The replay is where the split resolution bites hardest.

        ``_send`` builds ``retry_base`` from the very resolution whose source it
        just checked — deliberately, so the check and the dial cannot race --
        but the socket path is then resolved AGAIN, independently. The replay's
        two transports can therefore name different gateways, and the
        ``retry_base == base`` guard that decides whether to replay at all is
        computed on the half that may never be dialled.

        Ownership re-proof is per ATTEMPT, so the replay must resolve a FRESH
        pair (never reuse the refused one) — exactly one, used for both halves.
        """
        # A distinct port per run: the pair invariant is then visible without
        # the script having to predict HOW MANY times the chain is asked.
        runs = _scripted_resolutions(
            monkeypatch,
            mcp,
            [(5476, "marker"), (7788, "marker"), (9999, "marker"), (11111, "marker")],
        )
        seen = _capture_transport(monkeypatch, mcp, refuse=":5476")

        assert mcp._send("/api/x", headers={}, method="GET") == {"ok": True}
        assert len(seen) == 2, "the refused attempt must still be replayed once"
        assert len(runs) == 2, f"two attempts ran the discovery chain {len(runs)} times: {runs}"
        (url1, sock1), (url2, sock2) = seen
        assert ":5476" in url1 and "5476" in sock1, "first attempt must be self-consistent"
        assert ":7788" in url2, "the replay must dial the re-resolved port"
        assert (
            "7788" in sock2 and "9999" not in sock2
        ), f"the replay aimed TCP at {url2} and the unix socket at {sock2}"
        assert "5476" not in sock2, "the replay must not reuse the refused resolution"
