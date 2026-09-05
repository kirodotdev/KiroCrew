"""Tests for kernel-attested peer verification of internal-API session claims.

Covers the four layers of the unix-socket peer-identity feature:

* :mod:`kiro_crew.peer_resolve` — the shared /proc ancestry walk (extracted
  from gatewayd; its original tests in ``test_mcp_gateway_claim.py`` keep
  covering the gatewayd wrapper seams).
* ``dashboard.token_auth`` — the middleware branch: deny-on-mismatch,
  allow-on-match (with ``peer_verified`` set), status-quo on unresolvable,
  and the guarantee that TCP requests never engage the branch.
* ``dashboard.server._start_unix_site`` — POSIX-only bind with 0700 dir,
  Windows skip, degrade-to-TCP-only on bind failure.
* ``loopback_http`` — the stdlib unix-socket client transport and its
  fall-back-only-when-nothing-answered semantics.

The end-to-end test uses a real ``web.UnixSite`` + ``AF_UNIX`` connection so
the kernel populates real peer credentials (Linux ``SO_PEERCRED`` / macOS
``LOCAL_PEERPID``); pure-unit tests fake the socketsec seams instead so they
run identically on every platform.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from tmpdir_helpers import short_tmp_base

import kiro_crew.dashboard.token_auth as ta
from kiro_crew import platform_compat
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_gateway.socketsec import PeerCredResult
from kiro_crew.peer_resolve import resolve_peer_identity


@pytest.fixture
def short_sock_dir() -> "Iterator[Path]":
    """A SHORT base for every AF_UNIX bind/connect path in this module.

    ``sun_path`` caps at 104 bytes on macOS, and a pytest ``tmp_path`` under a
    deep TMPDIR (or xdist) exceeds it, failing ``bind()`` with ``OSError:
    AF_UNIX path too long`` (#8610). Same pattern as ``test_socketsec.py``:
    ``mkdtemp`` under :func:`short_tmp_base`, removed at teardown because
    ``mkdtemp`` registers no finalizer.
    """
    base = Path(tempfile.mkdtemp(dir=short_tmp_base()))
    yield base
    shutil.rmtree(base, ignore_errors=True)


SECRET = "test-internal-secret"
INTERNAL = frozenset({"/api/spawn"})


# ---------------------------------------------------------------------------
# peer_resolve — the shared ancestry walk
# ---------------------------------------------------------------------------


def _ppid_map(mapping: dict[int, int]):
    def _fn(pid: int) -> int:
        return mapping.get(pid, 0)

    return _fn


def test_resolve_peer_identity_finds_key_and_chain(tmp_path: Path) -> None:
    (tmp_path / "session_pid_50.txt").write_text("dashboard:chat-1-abc", encoding="utf-8")
    key, chain = resolve_peer_identity(
        100,
        config_dir_fn=lambda: tmp_path,
        ppid_fn=_ppid_map({100: 50, 50: 20, 20: 1}),
    )
    assert key == "dashboard:chat-1-abc"
    assert chain == [100, 50, 20]


def test_resolve_peer_identity_no_pidfile_returns_empty_key(tmp_path: Path) -> None:
    key, chain = resolve_peer_identity(
        300, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({300: 1})
    )
    assert key == ""
    assert chain == [300]


def test_resolve_peer_identity_config_dir_error_degrades(tmp_path: Path) -> None:
    def _boom() -> Path:
        raise RuntimeError("boom")

    assert resolve_peer_identity(999, config_dir_fn=_boom, ppid_fn=_ppid_map({})) == ("", [])


def test_resolve_peer_identity_cycle_terminates(tmp_path: Path) -> None:
    """A pid cycle (possible with pid reuse mid-walk) must not loop forever."""
    key, chain = resolve_peer_identity(
        10, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({10: 20, 20: 10})
    )
    assert key == ""
    assert chain == [10, 20]


def test_signed_only_refuses_forged_unsigned_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION (review finding): the bare .txt is same-uid agent-writable,
    so an attacker planting session_pid_<own_pid>.txt with a victim's key
    must NOT satisfy the authorization walk — signed_only requires the HMAC
    sidecar the attacker cannot produce."""
    from kiro_crew import session_pid_sig as sps

    monkeypatch.setattr(sps, "_load_hmac_key", lambda: b"K" * 32)
    (tmp_path / "session_pid_50.txt").write_text("dashboard:chat-victim", encoding="utf-8")
    key, chain = resolve_peer_identity(
        50, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({50: 1}), signed_only=True
    )
    assert key == ""  # unsigned mapping refused
    assert chain == [50]
    # ...and a forged sidecar (wrong MAC) is refused too.
    (tmp_path / "session_pid_50.sig").write_text("0" * 64, encoding="utf-8")
    key, _ = resolve_peer_identity(
        50, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({50: 1}), signed_only=True
    )
    assert key == ""


def test_signed_only_accepts_gateway_signed_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import session_pid_sig as sps

    _hmac_key = b"K" * 32
    monkeypatch.setattr(sps, "_load_hmac_key", lambda: _hmac_key)
    (tmp_path / "session_pid_50.txt").write_text("dashboard:chat-1", encoding="utf-8")
    (tmp_path / "session_pid_50.sig").write_text(
        sps._compute_sig(_hmac_key, 50, "dashboard:chat-1"), encoding="utf-8"
    )
    key, chain = resolve_peer_identity(
        100,
        config_dir_fn=lambda: tmp_path,
        ppid_fn=_ppid_map({100: 50, 50: 1}),
        signed_only=True,
    )
    assert key == "dashboard:chat-1"
    assert chain == [100, 50]


# ---------------------------------------------------------------------------
# token_auth middleware — unit tests with faked socketsec seams
# ---------------------------------------------------------------------------


class _FakeUnixSock:
    # getattr guard: socket.AF_UNIX does not exist on Windows CPython; module
    # collection must still succeed there (unit tests fake the family value —
    # the middleware compares against the same getattr-resolved constant).
    family = getattr(socket, "AF_UNIX", None)


def _make_request(
    path: str = "/api/spawn",
    headers: dict | None = None,
    remote: str | None = None,
    unix: bool = False,
) -> tuple[MagicMock, dict]:
    """Mock request + the dict backing its item assignment (``store``)."""
    req = MagicMock(spec=web.Request)
    req.path = path
    req.query = {}
    req.cookies = {}
    req.remote = remote if remote is not None else ("" if unix else "127.0.0.1")
    req.headers = headers or {}
    req.method = "POST"
    store: dict = {}
    req.__setitem__.side_effect = store.__setitem__
    req.__getitem__.side_effect = store.__getitem__
    transport = MagicMock()
    transport.get_extra_info = (
        (lambda name: _FakeUnixSock() if name == "socket" else None)
        if unix
        else (lambda name: None)
    )
    req.transport = transport
    return req, store


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# The middleware deliberately never engages on Windows (AF_UNIX resolves to
# None and the server binds no UnixSite), so a faked unix-transport request —
# an impossible shape there — is treated as plain non-loopback and denied by
# the ordinary internal-path rules. Every test that fakes a unix transport is
# therefore POSIX-only, engagement and pass-through alike.
_posix_only = pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only"
)


def _wire_peer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: PeerCredResult = PeerCredResult.MATCH,
    peer_pid: int | None = 4242,
    resolved: str = "",
) -> list[dict]:
    """Fake socketsec + resolver seams; return the captured SEL calls."""
    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(ta, "_sel_fn", lambda: _FakeSel())
    monkeypatch.setattr(ta, "check_peer_is_self", lambda sock: verdict)
    monkeypatch.setattr(ta, "get_peer_pid", lambda sock: peer_pid)
    monkeypatch.setattr(ta, "resolve_peer_identity", lambda pid, **kw: (resolved, [pid]))
    return calls


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_match_allowed_and_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_peer(monkeypatch, resolved="dashboard:chat-1-abc")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_mismatch_denied_with_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_peer(monkeypatch, resolved="dashboard:chat-1-abc")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-9-EVIL"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    mismatches = [c for c in calls if c.get("operation") == "dashboard.peer-identity-mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0]["outcome"] == "denied"
    assert "peer_pid=4242" in mismatches[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_mismatch_denied_even_with_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The peer check runs before either auth flavor — impersonation is denied
    regardless of what credentials the caller carries."""
    _wire_peer(monkeypatch, resolved="dashboard:chat-1-abc")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": "wrong", "X-Session-Key": "dashboard:chat-9-EVIL"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_unresolvable_proceeds_status_quo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty resolved key (warm pool before claim, cron, pooled backend) must
    keep today's semantics: valid secret grants."""
    _wire_peer(monkeypatch, resolved="")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert "peer_verified" not in store


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_no_pid_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_peer(monkeypatch, peer_pid=None, resolved="never-consulted")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_uid_mismatch_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_peer(monkeypatch, verdict=PeerCredResult.MISMATCH)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert any(c.get("outcome") == "denied" for c in calls)


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_uid_unverifiable_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNVERIFIABLE peer principal is DENIED (deny-by-default, mirroring
    gatewayd's register-path policy): on supported POSIX platforms an
    accepted AF_UNIX connection always yields peer credentials, so a failed
    read means the attestation mechanism itself broke."""
    calls = _wire_peer(monkeypatch, verdict=PeerCredResult.UNVERIFIABLE, resolved="")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert any("unverifiable" in c.get("error", "") for c in calls)


@_posix_only
@pytest.mark.asyncio
async def test_unix_no_session_key_skips_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No X-Session-Key → nothing session-scoped claimed → no walk at all."""

    def _explode(pid, **kw):  # pragma: no cover — the assertion IS that it never runs
        raise AssertionError("resolver must not run without a session claim")

    _wire_peer(monkeypatch)
    monkeypatch.setattr(ta, "resolve_peer_identity", _explode)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(headers={"X-Internal-Secret": SECRET}, unix=True)
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_tcp_request_never_engages_peer_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(sock):  # pragma: no cover — the assertion IS that it never runs
        raise AssertionError("peer check must not run for TCP requests")

    monkeypatch.setattr(ta, "check_peer_is_self", _explode)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=False,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@_posix_only
@pytest.mark.asyncio
async def test_resolver_exception_degrades_to_status_quo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_peer(monkeypatch)

    def _boom(pid, **kw):
        raise RuntimeError("proc walk exploded")

    monkeypatch.setattr(ta, "resolve_peer_identity", _boom)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# End-to-end over a real UnixSite (POSIX): kernel-populated peer credentials
# ---------------------------------------------------------------------------


def _unix_http_request(
    sock_path: str, path: str, headers: dict[str, str], timeout: float = 5.0
) -> tuple[int, bytes]:
    """Minimal raw-HTTP client over AF_UNIX (independent of loopback_http)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    try:
        lines = [f"GET {path} HTTP/1.1", "Host: localhost:5476", "Connection: close"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        status = int(buf.split(b" ", 2)[1])
        return status, buf
    finally:
        s.close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
@pytest.mark.asyncio
async def test_unix_site_end_to_end_peer_verification(
    tmp_path: Path, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real UnixSite + real AF_UNIX connect: the kernel reports OUR pid/uid,
    so with a session_pid file for an ancestor of this test process the
    middleware must deny a foreign declared key and allow the matching one."""
    import os

    from kiro_crew import session_pid_sig as sps

    # Publish a SIGNED pidfile for THIS process so the ancestry walk (starting
    # at the kernel-reported peer pid == our pid) resolves immediately under
    # the middleware's signed_only=True discipline. The HMAC trust root is
    # pinned so the sidecar can be computed against the tmp config dir.
    _hmac_key = b"K" * 32
    monkeypatch.setattr(sps, "_load_hmac_key", lambda: _hmac_key)
    pid = os.getpid()
    (tmp_path / f"session_pid_{pid}.txt").write_text("dashboard:chat-e2e", encoding="utf-8")
    (tmp_path / f"session_pid_{pid}.sig").write_text(
        sps._compute_sig(_hmac_key, pid, "dashboard:chat-e2e"), encoding="utf-8"
    )
    monkeypatch.setattr(
        ta,
        "resolve_peer_identity",
        lambda p, **kw: resolve_peer_identity(p, config_dir_fn=lambda: tmp_path, **kw),
    )

    app = web.Application()
    app.middlewares.append(
        ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    )

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"peer_verified": bool(request.get("peer_verified"))})

    app.router.add_get("/api/spawn", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock_path = str(short_sock_dir / "dash-test.sock")
    site = web.UnixSite(runner, sock_path)
    await site.start()
    try:
        loop = asyncio.get_running_loop()
        status_ok, body_ok = await loop.run_in_executor(
            None,
            _unix_http_request,
            sock_path,
            "/api/spawn",
            {"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-e2e"},
        )
        assert status_ok == 200
        assert b'"peer_verified": true' in body_ok
        status_evil, _ = await loop.run_in_executor(
            None,
            _unix_http_request,
            sock_path,
            "/api/spawn",
            {"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-OTHER"},
        )
        assert status_evil == 403
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# CSRF: the no-Origin branch trusts the unix transport
# ---------------------------------------------------------------------------


@_posix_only
def test_check_origin_trusts_unix_transport_without_origin() -> None:
    """An AF_UNIX request has no loopback request.remote; without this trust
    the CSRF middleware would 403 every mutating internal call on the socket
    before token auth ever ran (review finding)."""
    from kiro_crew.dashboard.origin import check_origin

    req, _store = _make_request(unix=True)
    req.app = {"allowed_origins": set()}
    assert check_origin(req, require=True) is True


def test_check_origin_still_rejects_plain_remote_without_origin() -> None:
    from kiro_crew.dashboard.origin import check_origin

    req, _store = _make_request(remote="10.0.0.1", unix=False)
    req.app = {"allowed_origins": set()}
    assert check_origin(req, require=True) is False


# ---------------------------------------------------------------------------
# server startup — _start_unix_site
# ---------------------------------------------------------------------------


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
@pytest.mark.asyncio
async def test_start_unix_site_binds_and_removes_stale(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import server as srv

    monkeypatch.setattr(
        "kiro_crew.dashboard.server.dashboard_socket_path",
        lambda port: short_sock_dir / f"dashboard-{port}.sock",
    )
    # Plant a stale socket file (bound then abandoned) to prove self-healing.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(short_sock_dir / "dashboard-5999.sock"))
    stale.close()

    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        path = await srv._start_unix_site(runner, 5999)
        assert path is not None
        assert path.exists()
        import stat as _stat

        assert _stat.S_ISSOCK(path.stat().st_mode)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_start_unix_site_skipped_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard import server as srv

    monkeypatch.setattr(srv.platform_compat, "IS_WINDOWS", True)
    assert await srv._start_unix_site(MagicMock(), 5999) is None


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
@pytest.mark.asyncio
async def test_start_unix_site_bind_failure_degrades(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-socket file squatting the path makes the bind fail; startup must
    degrade to TCP-only (return None), never raise."""
    from kiro_crew.dashboard import server as srv

    squatter = short_sock_dir / "dashboard-6001.sock"
    squatter.write_text("not a socket", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.dashboard.server.dashboard_socket_path", lambda port: squatter)
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        assert await srv._start_unix_site(runner, 6001) is None
        assert squatter.read_text(encoding="utf-8") == "not a socket"  # left in place
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# loopback_http client — unix transport preference + fallback semantics
# ---------------------------------------------------------------------------


@pytest.fixture()
def unix_http_server(short_sock_dir: Path):
    """A minimal threaded HTTP server on an AF_UNIX socket."""
    import http.server
    import socketserver
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib contract
            body = json.dumps({"via": "unix"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    class _UnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

        # BaseHTTPRequestHandler derives client_address[0]; unix sockets
        # provide '' — normalize so the handler does not crash.
        def get_request(self):
            request, _ = super().get_request()
            return request, ("unix", 0)

    sock_path = str(short_sock_dir / "client-test.sock")
    server = _UnixServer(sock_path, _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield sock_path
    server.shutdown()
    server.server_close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_loopback_urlopen_uses_unix_socket(unix_http_server: str) -> None:
    req = urllib.request.Request("http://localhost:59999/api/anything")
    with loopback_urlopen(req, timeout=5, unix_socket_path=unix_http_server) as resp:
        assert json.loads(resp.read()) == {"via": "unix"}


def test_loopback_urlopen_absent_socket_falls_back_to_tcp(short_sock_dir: Path) -> None:
    """Socket file missing → straight to TCP (refused on a dead port)."""
    req = urllib.request.Request("http://127.0.0.1:1/api/x")
    with pytest.raises(urllib.error.URLError):
        loopback_urlopen(req, timeout=2, unix_socket_path=str(short_sock_dir / "nope.sock"))


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_loopback_urlopen_stale_socket_falls_back_to_tcp(short_sock_dir: Path) -> None:
    """Socket file exists but nobody listens → connect refused → TCP fallback.

    The TCP side serves a real response, proving the fallback actually runs
    the request rather than re-raising."""
    import http.server
    import threading

    stale_path = short_sock_dir / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale_path))
    s.close()  # bound but never listened/accepting → ECONNREFUSED

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib contract
            body = b'{"via": "tcp"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    tcp = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = tcp.server_address[1]
    t = threading.Thread(target=tcp.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/x")
        with loopback_urlopen(req, timeout=5, unix_socket_path=str(stale_path)) as resp:
            assert json.loads(resp.read()) == {"via": "tcp"}
    finally:
        tcp.shutdown()
        tcp.server_close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_loopback_urlopen_http_error_propagates_no_fallback(short_sock_dir: Path) -> None:
    """A 4xx over the unix socket is a REAL response — it must propagate as
    HTTPError, never trigger a duplicate TCP send."""
    import http.server
    import socketserver
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib contract
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    class _UnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

        def get_request(self):
            request, _ = super().get_request()
            return request, ("unix", 0)

    sock_path = str(short_sock_dir / "err.sock")
    server = _UnixServer(sock_path, _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request("http://127.0.0.1:1/api/x")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            loopback_urlopen(req, timeout=5, unix_socket_path=sock_path)
        assert exc_info.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# mcp_core client — socket preference wiring
# ---------------------------------------------------------------------------


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_mcp_core_post_prefers_unix_socket(
    unix_http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.mcp_core as mcp_core

    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", unix_http_server)
    monkeypatch.setattr(mcp_core, "_API", "http://127.0.0.1:1")  # TCP would refuse
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")
    assert mcp_core._get("/api/anything") == {"via": "unix"}


def test_mcp_core_post_falls_back_to_tcp_when_socket_absent(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error shape of _get is unchanged when neither transport answers."""
    import kiro_crew.mcp_core as mcp_core

    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", str(short_sock_dir / "absent.sock"))
    monkeypatch.setattr(mcp_core, "_API", "http://127.0.0.1:1")
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")
    out = mcp_core._get("/api/anything")
    assert "error" in out


@_posix_only
@pytest.mark.asyncio
async def test_non_loopback_mixed_denial_names_which_credential_was_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mixed-path arm must name the sides too, like the loopback arm does.

    The loopback arm records both credential fingerprints, so an ABSENT
    credential (a caller that could read no credential file at all) is visibly
    different from a caller holding the wrong one. This arm was left on a bare
    string, so a denial here still said only "wrong secret" -- the exact
    ambiguity the fingerprint exists to remove, in the one place a remote
    mixed-path caller lands.
    """
    calls = _wire_peer(monkeypatch)
    mw = ta.token_auth_middleware(
        internal_paths=INTERNAL,
        internal_secret=SECRET,
        mixed_internal_paths=INTERNAL,
        local_only=True,
    )

    # Non-loopback remote on a mixed internal path, header present but EMPTY.
    req, _ = _make_request(headers={"X-Internal-Secret": ""}, remote="10.1.2.3")
    resp = await mw(req, _ok_handler)
    assert resp.status == 403

    denials = [
        c for c in calls if c.get("operation") == "internal_auth" and c.get("outcome") == "denied"
    ]
    assert len(denials) == 1
    err = denials[0]["error"]
    assert "non-loopback mixed" in err, err
    assert (
        "received=absent" in err
    ), "the mixed arm still cannot say an absent credential from a wrong one"
    assert f"expected={ta._credential_fingerprint(SECRET)}" in err, err
