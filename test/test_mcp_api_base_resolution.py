"""A refused gateway callback re-resolves its base once and replays.

The port a ``--port``-started gateway is bound to is recorded only in its run
marker, so an MCP tool server that resolved its base before the gateway came up
(or before it moved) holds a stale base. These tests lock in the recovery path
this PR adds: every verb helper routes through ``mcp_core._send``, which on a
refused connection drops the resolution caches, re-resolves, and replays the
request exactly once — and only when re-resolution actually produced a
different base. ``mcp_computer._invoke`` applies the same rule to its one
request path.

``TestSameBaseRefusalRetry`` covers the case that rule deliberately declines --
a gateway restarting on its OWN port, where there is no moved base to find. That
used to fall through to a bare errno; it now gets a short bounded retry of the
same target and, on exhaustion, an actionable message.
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


def _sock_for(port: int) -> str:
    """The socket path the product derives for *port*, derived the same way.

    Assertions about which gateway a transport reached compare against THIS,
    never a port substring of the path. The path contains the data home, whose
    isolation directory is named ``<counter>-kirocrew-home`` from a per-process
    allocation counter — so a worker that has handed out 5,476 paths puts the
    literal ``5476`` in every path it builds afterwards. A substring check then
    reports whichever port the counter happens to equal: the negative form fails
    a correct socket, and the positive form passes a wrong one.
    """
    from kiro_crew.dashboard.origin import dashboard_socket_path

    return str(dashboard_socket_path(port))


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
    """Re-resolution proving the same base earns no REPLAY — only the retry window.

    The replay exists to chase a MOVED gateway, and there is none here. What the
    unchanged base does get is the short same-target retry
    (``TestSameBaseRefusalRetry``), for the gateway that is merely restarting on
    its own port, so the dials stay on that one base.
    """
    attempts: list[str] = []
    _bases(monkeypatch, mcp, ["http://127.0.0.1:7788"])
    _retry_resolution(monkeypatch, mcp, 7788, "marker")
    monkeypatch.setattr(mcp, "_REFUSED_RETRY_BACKOFFS", (0.0, 0.0))

    def fake_open(req, timeout=None, unix_socket_path=None):
        attempts.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    assert "error" in mcp._post("/api/x", {"k": "v"})
    assert len(attempts) == 1 + len(mcp._REFUSED_RETRY_BACKOFFS)
    assert all(":7788" in u for u in attempts), attempts


def test_no_replay_when_rediscovery_falls_through_to_default(mcp: Any, monkeypatch) -> None:
    """A no-evidence default fall-through must never receive the replay.

    The marker gateway exited after the first resolution; re-resolution finds
    nothing and falls through to the default port. A listener there is
    unverified — it could be any local process — and the request carries the
    internal secret, so the replay is skipped even though the base differs.

    The SAME evidence rule bounds the same-base retry. Re-resolution proving
    nothing means the refused port is no longer proven to be ours either, so the
    retry window is not spent dialling it: one dial in total, and the caller is
    told the gateway is unreachable.
    """
    attempts: list[str] = []
    _bases(monkeypatch, mcp, ["http://127.0.0.1:9999"])
    _retry_resolution(monkeypatch, mcp, 5476, "default")
    monkeypatch.setattr(mcp, "_REFUSED_RETRY_BACKOFFS", (0.0, 0.0))

    def fake_open(req, timeout=None, unix_socket_path=None):
        attempts.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
    out = mcp._post("/api/x", {"k": "v"})
    assert "error" in out
    assert "transport_error" not in out
    # Neither the unverified default port NOR the refused base receives a
    # further secret-bearing dial: re-resolution proved nothing, so ownership of
    # 9999 is no longer established and the retry stops before sleeping again.
    assert all(":9999" in u for u in attempts), attempts
    assert len(attempts) == 1


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
    """``mcp_computer._invoke`` — the same refused-once-replay rule, now SHARED.

    #4106 item 2: this shim used to restate the rule (invalidate, re-resolve,
    check the source, compare the base) in its own words, and restating it is
    how it silently missed item 1 — it hand-built a ``retry_base`` with no
    socket half at all. It now consumes ``mcp_core._replay_target``, so the
    cases below script the rule at ITS seam (``_resolve_api_port`` on
    ``mcp_core``) and assert this shim's error wording is unchanged: a
    de-duplication of the rule must not normalise the two callers' messages.
    """

    @pytest.fixture
    def computer(self, mcp: Any, monkeypatch) -> Any:
        module = importlib.import_module("kiro_crew.mcp_computer")
        monkeypatch.setattr(module, "_internal_secret", lambda: "s")
        return module

    @staticmethod
    def _first_target(monkeypatch: pytest.MonkeyPatch, computer: Any, base: str) -> None:
        """Script the pair this shim resolves for its FIRST attempt."""
        monkeypatch.setattr(computer, "_resolve_api_target", lambda: (base, ""))

    @staticmethod
    def _unreachable(computer: Any, out: dict) -> bool:
        """This shim's own refusal wording, which the shared rule must not touch.

        Compared against the module's own constant rather than a copy of its
        text: the assertion is "still ITS message", so a reworded constant
        should keep passing while a message borrowed from ``mcp_core`` fails.
        """
        prefix = computer.ERR_GATEWAY_UNREACHABLE.split("{detail}")[0]
        return str(out.get("error", "")).startswith(prefix)

    def test_refusal_rediscovers_and_replays(self, computer: Any, mcp: Any, monkeypatch) -> None:
        urls: list[str] = []
        self._first_target(monkeypatch, computer, "http://127.0.0.1:5476")
        _retry_resolution(monkeypatch, mcp, 7788, "marker")

        def fake_open(req, timeout=None, unix_socket_path=None):
            urls.append(req.full_url)
            if ":5476" in req.full_url:
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            return _Resp(b'{"text": "done"}')

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        out = computer._invoke("dashboard:chat-1", "computer_get_state", {})
        assert out == {"text": "done"}
        assert len(urls) == 2

    def test_refusal_with_unchanged_base_is_reported(
        self, computer: Any, mcp: Any, monkeypatch
    ) -> None:
        attempts: list[str] = []
        self._first_target(monkeypatch, computer, "http://127.0.0.1:7788")
        _retry_resolution(monkeypatch, mcp, 7788, "marker")

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        out = computer._invoke("dashboard:chat-1", "computer_get_state", {})
        assert self._unreachable(computer, out), out
        assert len(attempts) == 1

    def test_no_replay_when_rediscovery_falls_through_to_default(
        self, computer: Any, mcp: Any, monkeypatch
    ) -> None:
        """Same no-evidence rule as mcp_core._send — because it is now literally
        the same code: an unverified default-port listener must never receive the
        replayed secret-bearing request."""
        attempts: list[str] = []
        self._first_target(monkeypatch, computer, "http://127.0.0.1:9999")
        _retry_resolution(monkeypatch, mcp, 5476, "default")

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        out = computer._invoke("dashboard:chat-1", "computer_get_state", {})
        assert self._unreachable(computer, out), out
        assert len(attempts) == 1  # nothing was sent to the unverified default port

    def test_the_attempt_pairs_its_unix_socket_with_its_base(
        self, computer: Any, mcp: Any, monkeypatch
    ) -> None:
        """The gap restating the rule hid: this shim dialled TCP only.

        ``loopback_urlopen`` PREFERS the unix socket, and that socket is what
        lets the gateway kernel-verify (``SO_PEERCRED`` + /proc ancestry) that
        this process really owns the session key it declares. Passing no socket
        path meant every computer-use call fell back to TCP and the
        header-on-faith path — for the one tool family whose whole point is that
        the gateway evaluates and audits it. Consuming the shared resolution
        fixes it for free, so it is pinned here.
        """
        seen: list[str] = []
        monkeypatch.setattr(mcp, "_API", "http://127.0.0.1:7788")
        monkeypatch.setattr(mcp, "_API_UNIX_SOCKET", "/tmp/seeded-7788.sock")

        def fake_open(req, timeout=None, unix_socket_path=None):
            seen.append(str(unix_socket_path or ""))
            return _Resp(b'{"text": "done"}')

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        assert computer._invoke("dashboard:chat-1", "computer_get_state", {}) == {"text": "done"}
        assert seen == ["/tmp/seeded-7788.sock"], f"the attempt dialled TCP only: {seen}"

    def test_the_replay_pairs_a_fresh_socket_with_the_re_resolved_base(
        self, computer: Any, mcp: Any, monkeypatch
    ) -> None:
        """And the replay must be paired too, from ONE fresh resolution.

        The hand-built ``retry_base`` carried no socket half, so even a shim that
        paired its first attempt would have dropped to TCP exactly when the
        gateway had just moved. The shared owner derives both halves from the
        resolution whose source it checked.
        """
        seen: list[tuple[str, str]] = []
        self._first_target(monkeypatch, computer, "http://127.0.0.1:5476")
        _retry_resolution(monkeypatch, mcp, 7788, "marker")

        def fake_open(req, timeout=None, unix_socket_path=None):
            seen.append((req.full_url, str(unix_socket_path or "")))
            if ":5476" in req.full_url:
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            return _Resp(b'{"text": "done"}')

        monkeypatch.setattr(computer, "loopback_urlopen", fake_open)
        assert computer._invoke("dashboard:chat-1", "computer_get_state", {}) == {"text": "done"}
        (_, _), (url, sock) = seen
        assert ":7788" in url
        assert sock == _sock_for(7788), f"replay dialled {url} with socket {sock!r}"

    def test_http_error_on_replay_surfaces_the_backend_body(
        self, computer: Any, mcp: Any, monkeypatch
    ) -> None:
        """A 4xx from the reached moved gateway must decode like a first-attempt
        4xx, not collapse into a stale 'gateway unreachable: refused'."""
        import io

        self._first_target(monkeypatch, computer, "http://127.0.0.1:5476")
        _retry_resolution(monkeypatch, mcp, 7788, "marker")

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
        expected = _sock_for(7788)
        assert sock == expected, f"one attempt aimed TCP at {url} and the unix socket at {sock}"

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
        assert ":5476" in url1 and sock1 == _sock_for(5476), "first attempt must be self-consistent"
        assert ":7788" in url2, "the replay must dial the re-resolved port"
        replayed = _sock_for(7788)
        assert sock2 == replayed, f"the replay aimed TCP at {url2} and the unix socket at {sock2}"
        assert sock2 != _sock_for(5476), "the replay must not reuse the refused resolution"


class TestSameBaseRefusalRetry:
    """A gateway restarting on its OWN port gets a short bounded retry.

    ``_replay_target`` deliberately answers ``None`` when re-resolution names the
    base that was just refused — there is no moved gateway to chase. That left
    ``_send`` with a bare ``return {"error": str(e)}``, so an MCP write issued
    during the sub-second window a restarting gateway is rebinding its port came
    back as the raw ``<urlopen error [Errno 61] Connection refused>``: no retry,
    and an errno the caller cannot act on.

    The retry is legal precisely because the connect never completed, so nothing
    was handed to the gateway and the request cannot have been executed. These
    tests pin that it happens for a refusal, that exhaustion is reported
    actionably, and that no other failure class is retried — an ``HTTPError`` has
    a real response, and a post-connect failure leaves acceptance undetermined,
    which spawn_run's reconcile reads off ``transport_error``.
    """

    @pytest.fixture
    def unchanged_base(self, mcp: Any, monkeypatch) -> Any:
        """Resolution and re-resolution both name 7788, so no replay is allowed."""
        _bases(monkeypatch, mcp, ["http://127.0.0.1:7788"])
        _retry_resolution(monkeypatch, mcp, 7788, "marker")
        monkeypatch.setattr(mcp, "_REFUSED_RETRY_BACKOFFS", (0.0, 0.0))
        return mcp

    @staticmethod
    def _refusal() -> urllib.error.URLError:
        return urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    @staticmethod
    def _resolution_sequence(
        monkeypatch: pytest.MonkeyPatch, mcp: Any, sequence: list[tuple[int, str]]
    ) -> None:
        """Script successive ``_resolve_api_port`` answers, last value repeating.

        ``_retry_resolution`` pins ONE answer, which cannot express a gateway
        that moves BETWEEN the replay decision and a later re-verification {EM}
        exactly the window these cases are about.
        """
        it = iter(sequence)
        last = sequence[-1]
        monkeypatch.setattr(mcp, "_resolve_api_port", lambda: next(it, last))

    def test_reverify_demands_positive_evidence_for_this_same_base(
        self, mcp: Any, monkeypatch
    ) -> None:
        """The proof is per attempt, so each re-dial must re-earn it.

        Three outcomes, one helper: the same base still named by a real source is
        the only one that authorises another dial. A default fall-through proves
        nothing, and a different base means the port was freed {EM} in both cases
        the answer is ``None`` and the caller stops.
        """
        base = "http://127.0.0.1:7788"

        _retry_resolution(monkeypatch, mcp, 7788, "marker")
        assert mcp._reverify_refused_target(base) == (base, _sock_for(7788))

        _retry_resolution(monkeypatch, mcp, 5476, "default")
        assert mcp._reverify_refused_target(base) is None

        _retry_resolution(monkeypatch, mcp, 9999, "marker")
        assert mcp._reverify_refused_target(base) is None

    def test_an_unproven_port_is_never_re_dialled(self, mcp: Any, monkeypatch) -> None:
        """``bound`` is the NORMAL source, and it carries no ownership evidence.

        ``KIROCREW_BOUND_PORT`` is inherited process state naming the gateway that
        spawned us, exported once its site was listening, and it ranks ABOVE the
        marker step -- so every gateway-spawned MCP server resolves ``bound``,
        never ``marker``. That label says the value has not changed, not that the
        gateway is still on it, so a re-dial has to prove the port is still held
        by this user's gateway.
        """
        base = "http://127.0.0.1:7788"
        _retry_resolution(monkeypatch, mcp, 7788, "bound")
        monkeypatch.setattr(mcp, "port_is_gateway_owned", lambda port: False)
        assert mcp._reverify_refused_target(base) is None

    def test_a_proven_port_still_gets_its_retry(self, mcp: Any, monkeypatch) -> None:
        """The proof is a gate, not a ban -- the retry survives where it matters.

        Refusing ``bound`` outright (or trusting only ``marker``) would have
        disabled this retry for the one deployment it exists to serve. A gateway
        that really did come back on its own port passes the proof and is dialled.
        """
        base = "http://127.0.0.1:7788"
        _retry_resolution(monkeypatch, mcp, 7788, "bound")
        asked: list[int] = []

        def owns(port: int) -> bool:
            asked.append(port)
            return True

        monkeypatch.setattr(mcp, "port_is_gateway_owned", owns)
        assert mcp._reverify_refused_target(base) == (base, _sock_for(7788))
        assert asked == [7788], f"the proof must name the port about to be dialled: {asked}"

    def test_a_marker_resolution_is_not_re_proven(self, mcp: Any, monkeypatch) -> None:
        """Step 5 already ran the proof, so running it again is pure cost.

        ``_marker_port`` discards every candidate a verified gateway does not
        hold, so a ``marker`` answer IS an ownership-checked answer. Re-forking a
        port lookup for it would double the cost of the common recovery path.
        """
        base = "http://127.0.0.1:7788"
        _retry_resolution(monkeypatch, mcp, 7788, "marker")

        def boom(port: int) -> bool:
            raise AssertionError("a marker resolution must not be re-proven")

        monkeypatch.setattr(mcp, "port_is_gateway_owned", boom)
        assert mcp._reverify_refused_target(base) == (base, _sock_for(7788))

    def test_an_unproven_port_ends_the_retry_instead_of_replaying_the_secret(
        self, mcp: Any, monkeypatch
    ) -> None:
        """End to end: one dial, and the credential is never offered a second time.

        The unit assertions above pin the rule; this pins that ``_send`` obeys it,
        which is where the exposure would actually land.
        """
        _bases(monkeypatch, mcp, ["http://127.0.0.1:7788"])
        _retry_resolution(monkeypatch, mcp, 7788, "bound")
        monkeypatch.setattr(mcp, "_REFUSED_RETRY_BACKOFFS", (0.0, 0.0))
        monkeypatch.setattr(mcp, "port_is_gateway_owned", lambda port: False)
        dials: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            dials.append(req.full_url)
            raise self._refusal()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        result = mcp._post("/api/x", {"k": "v"})
        assert len(dials) == 1, f"an unproven port must not be re-dialled: {dials}"
        assert "error" in result, result

    def test_a_gateway_not_back_yet_keeps_its_remaining_budget(self, mcp: Any, monkeypatch) -> None:
        """An unprovable target is a not-yet, not a stop signal.

        Between the old process releasing the port and the new one binding it,
        nothing is listening and no ownership can be shown -- which is exactly the
        window this retry covers. Ending the schedule on the first such reading
        would limit recovery to a gateway that rebinds inside the first backoff.
        """
        _bases(monkeypatch, mcp, ["http://127.0.0.1:7788"])
        _retry_resolution(monkeypatch, mcp, 7788, "bound")
        monkeypatch.setattr(mcp, "_REFUSED_RETRY_BACKOFFS", (0.0, 0.0))
        proofs = iter([False, True])
        monkeypatch.setattr(mcp, "port_is_gateway_owned", lambda port: next(proofs, True))
        dials: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            dials.append(req.full_url)
            if len(dials) == 1:
                raise self._refusal()
            return _Resp()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        assert mcp._post("/api/x", {"k": "v"}) == {"ok": True}
        assert len(dials) == 2, f"the second backoff must still be spent: {dials}"

    def test_the_retry_re_reads_the_credential_for_the_restarted_gateway(
        self, unchanged_base, monkeypatch
    ) -> None:
        """A restarted gateway is a new GENERATION, so the secret is re-read.

        ``read_local_secret`` resolves ``run/gateway-<port>.secret`` before the
        shared file because the credential identifies one gateway generation, and
        authenticating for a different generation than the one owning the dialled
        port earns a 403. The request was built with the pre-restart secret, so a
        retry that replays it reaches the gateway this fix exists to reach and is
        rejected — the credential has to be re-read for the re-proven target.
        """
        mcp = unchanged_base
        # Patched at the credential seam itself. The FIRST dial's header was built
        # by the caller before this test could reach it, so the property under test
        # is that the retry re-reads rather than replays -- not a literal for a
        # value this test never supplied.
        monkeypatch.setattr(mcp, "read_local_secret", lambda port: "regenerated-secret")
        sent: list[str | None] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            sent.append(req.get_header("X-internal-secret"))
            if len(sent) == 1:
                raise self._refusal()
            return _Resp()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        assert mcp._post("/api/x", {"k": "v"}) == {"ok": True}
        # The retry carries the credential belonging to the generation now on
        # that port, not the one the request was originally built with.
        assert sent[1] == "regenerated-secret", sent
        assert sent[1] != sent[0], sent

    def test_a_credential_that_cannot_be_re_read_keeps_the_original(
        self, unchanged_base, monkeypatch
    ) -> None:
        """A failed secret read must not downgrade the retry to a guaranteed 403.

        Sending an empty credential would be strictly worse than replaying the one
        the request already had, so an unreadable secret leaves the original in
        place.
        """
        mcp = unchanged_base

        def boom(port):
            raise OSError("unreadable")

        monkeypatch.setattr(mcp, "read_local_secret", boom)
        sent: list[str | None] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            sent.append(req.get_header("X-internal-secret"))
            if len(sent) == 1:
                raise self._refusal()
            return _Resp()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        assert mcp._post("/api/x", {"k": "v"}) == {"ok": True}
        assert sent[1] == sent[0], sent

    def test_a_port_freed_mid_backoff_is_never_re_dialled(self, mcp: Any, monkeypatch) -> None:
        """The security property: sleeping must not outlive the ownership proof.

        Re-resolution names the refused base while ``_replay_target`` runs, so
        there is no moved gateway to chase and the retry window opens. During the
        backoff the gateway exits and the port is taken: the next resolution
        names a DIFFERENT base. Re-dialling 7788 now would hand the internal
        secret and the session key to whatever bound it, so the retry stops with
        the unreachable message instead.
        """
        attempts: list[str] = []
        _bases(monkeypatch, mcp, ["http://127.0.0.1:7788"])
        # First answer keeps the replay closed; the second is the port moving.
        self._resolution_sequence(monkeypatch, mcp, [(7788, "marker"), (9999, "marker")])
        monkeypatch.setattr(mcp, "_REFUSED_RETRY_BACKOFFS", (0.0, 0.0))

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise self._refusal()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/x", {"k": "v"})

        assert "error" in out
        assert "not reachable" in out["error"]
        assert attempts == ["http://127.0.0.1:7788/api/x"], attempts
        assert not any(":9999" in u for u in attempts), attempts

    def test_two_refusals_then_success_yields_one_payload(
        self, unchanged_base, monkeypatch
    ) -> None:
        """The restart window closes mid-retry: one payload, no duplicate send."""
        mcp = unchanged_base
        attempts: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            if len(attempts) <= 2:
                raise self._refusal()
            return _Resp()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/learn", {"rule": "x"})
        assert out == {"ok": True}
        assert len(attempts) == 3, f"expected the first dial plus two retries: {attempts}"
        assert all(":7788" in u for u in attempts), attempts

    def test_exhaustion_names_the_port_the_restart_and_the_errno(
        self, unchanged_base, monkeypatch
    ) -> None:
        """The caller must get something to act on, without losing the diagnostic."""
        mcp = unchanged_base
        attempts: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise self._refusal()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/learn", {"rule": "x"})
        assert len(attempts) == 1 + len(mcp._REFUSED_RETRY_BACKOFFS), attempts
        message = out["error"]
        assert "127.0.0.1:7788" in message, message
        assert "restarting" in message, message
        assert "retry shortly" in message, message
        assert "[Errno 111]" in message, "the raw errno must survive for a bug report"
        assert "transport_error" not in out, "nothing reached the gateway"

    def test_the_retry_budget_is_bounded_and_brief(self, mcp: Any, monkeypatch) -> None:
        """A down gateway must not feel hung: a couple of sub-second pauses.

        Asserted on the SHIPPED backoffs (this one does not stub them), because
        the budget is the whole reason a retry is acceptable on a hot path — the
        5s keepalive poll goes through here too.
        """
        _bases(monkeypatch, mcp, ["http://127.0.0.1:7788"])
        _retry_resolution(monkeypatch, mcp, 7788, "marker")
        slept: list[float] = []
        monkeypatch.setattr(mcp.time, "sleep", slept.append)

        def fake_open(req, timeout=None, unix_socket_path=None):
            raise self._refusal()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        assert "error" in mcp._post("/api/learn", {"rule": "x"})
        assert 2 <= len(slept) <= 3, f"retry budget drifted: {slept}"
        assert sum(slept) <= 2.0, f"a refused gateway blocked the caller for {sum(slept)}s"

    def test_a_refused_replay_also_gets_the_window(self, mcp: Any, monkeypatch) -> None:
        """Both bases refused: the retry follows the fresher re-resolved base."""
        _bases(monkeypatch, mcp, ["http://127.0.0.1:5476"])
        _retry_resolution(monkeypatch, mcp, 7788, "marker")
        monkeypatch.setattr(mcp, "_REFUSED_RETRY_BACKOFFS", (0.0,))
        attempts: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise self._refusal()

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/spawn", {"tasks": ["x"]})
        assert [":5476" in attempts[0], ":7788" in attempts[1], ":7788" in attempts[2]] == [
            True,
            True,
            True,
        ], attempts
        assert "127.0.0.1:7788" in out["error"], out
        assert "transport_error" not in out

    def test_an_http_error_is_never_retried(self, unchanged_base, monkeypatch) -> None:
        """A 4xx is a real response — retrying it would re-send an accepted write."""
        mcp = unchanged_base
        attempts: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", None, None  # type: ignore[arg-type]
            )

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/learn", {"rule": "x"})
        assert len(attempts) == 1, attempts
        assert "transport_error" not in out

    def test_a_post_connect_failure_stays_ambiguous_and_unretried(
        self, unchanged_base, monkeypatch
    ) -> None:
        """Acceptance is undetermined, so a retry could double-execute the verb."""
        mcp = unchanged_base
        attempts: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise TimeoutError("read timed out after the spawn was accepted")

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/spawn", {"tasks": ["x"]})
        assert len(attempts) == 1, attempts
        assert out == {
            "error": "read timed out after the spawn was accepted",
            "transport_error": True,
        }
        assert "transport_error" not in mcp._get("/api/x"), "mark stays opt-in per verb"

    def test_a_non_refusal_urlerror_is_never_retried(self, unchanged_base, monkeypatch) -> None:
        """A connect timeout may have reached the gateway; only refusals are safe."""
        mcp = unchanged_base
        attempts: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            raise urllib.error.URLError(socket.timeout("slow"))

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/spawn", {"tasks": ["x"]})
        assert len(attempts) == 1, attempts
        assert out.get("transport_error") is True

    def test_a_refusal_that_turns_into_a_post_connect_failure_is_ambiguous(
        self, unchanged_base, monkeypatch
    ) -> None:
        """The gateway came back mid-retry and then failed after accepting."""
        mcp = unchanged_base
        attempts: list[str] = []

        def fake_open(req, timeout=None, unix_socket_path=None):
            attempts.append(req.full_url)
            if len(attempts) == 1:
                raise self._refusal()
            raise TimeoutError("read timed out after the spawn was accepted")

        monkeypatch.setattr(mcp, "_api_urlopen", fake_open)
        out = mcp._post("/api/spawn", {"tasks": ["x"]})
        assert len(attempts) == 2, "the retry must stop at the first non-refusal"
        assert out.get("transport_error") is True
