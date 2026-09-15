"""Tests for the loopback (PKCE) sign-in path of KasLoginService.

The listener is real: ``begin_loopback`` binds one of the allowlisted loopback
ports and serves the portal redirect. Tests play the browser by GETting the
callback URL with aiohttp, and the token exchange is the scripted fake session
the device tests use — so no test touches Kiro's servers.

The allowlisted ports are shared machine-wide, so every test cancels its login
(or drives it to a terminal state) before returning: a leaked listener would make
the next test bind a different port, not fail, but would hold the port for the
whole 300s deadline.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import urllib.parse

import aiohttp
import pytest

from kiro_crew.auth.login import portal
from kiro_crew.auth.service import (
    LOOPBACK_TIMEOUT_SECS,
    KasLoginService,
    LoopbackUnavailableError,
    UnknownLoginError,
)
from kiro_crew.auth.shape import Transport
from kiro_crew.auth.store import TokenStore

pytestmark = pytest.mark.asyncio


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, *, content_type: str | None = "application/json"):
        return self._payload

    async def text(self):
        return str(self._payload)


class _FakeSession:
    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, *, json=None, headers=None):  # noqa: A002
        self.calls.append((url, json or {}))
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


@pytest.fixture
def loopback_shape(monkeypatch):
    monkeypatch.setenv("KIRO_AUTH_TRANSPORT", Transport.LOOPBACK.value)
    monkeypatch.delenv("KIRO_AUTH_INSTALL_SHAPE", raising=False)
    # Bind an ephemeral port, not one of the ten shared Cognito-allowlisted
    # ports: those belong to the operator's real sign-in (and to any parallel
    # test worker), so a test must neither collide with them nor squat on them.
    # The rest of the flow is port-agnostic — the redirect URI and the token
    # exchange both take whatever port the socket reports.
    monkeypatch.setattr(portal, "bind_allowed_port", _bind_ephemeral)


def _bind_ephemeral() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


def _port_is_closed(port: int) -> bool:
    """True when nothing listens on the loopback port any more."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", port))
    except OSError:
        return True
    else:
        return False
    finally:
        probe.close()


def _service(tmp_path, responses=None):
    session = _FakeSession(responses)
    return KasLoginService(TokenStore(tmp_path), session=session), session


def _state_from(auth_url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["state"][0]


async def _hit_callback(port: int, query: dict[str, str]) -> str:
    url = f"http://127.0.0.1:{port}/oauth/callback?{urllib.parse.urlencode(query)}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as resp:
            return await resp.text()


async def _poll_until_terminal(service, login_id, tries=50):
    for _ in range(tries):
        result = await service.poll_device(login_id)
        if result["status"] != "pending":
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("login never reached a terminal state")


async def test_begin_loopback_refused_when_transport_is_device(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_AUTH_TRANSPORT", Transport.DEVICE.value)
    service, _ = _service(tmp_path)
    with pytest.raises(LoopbackUnavailableError):
        await service.begin_loopback("google")


async def test_begin_loopback_rejects_non_social_provider(tmp_path, loopback_shape):
    service, _ = _service(tmp_path)
    with pytest.raises(ValueError):
        await service.begin_loopback("builder_id")


async def test_begin_loopback_returns_portal_url_and_holds_a_port(tmp_path, loopback_shape):
    service, _ = _service(tmp_path)
    begin = await service.begin_loopback("google")
    try:
        assert 0 < begin["port"] < 65536
        assert not _port_is_closed(begin["port"])  # the listener is up before we return
        parsed = urllib.parse.urlparse(begin["auth_url"])
        q = urllib.parse.parse_qs(parsed.query)
        assert parsed.path == "/signin"
        assert q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == [f"http://localhost:{begin['port']}"]
        assert q["redirect_from"] == ["kirocli"]
        assert begin["auth_url"] == begin["verification_uri_complete"]
        assert await service.poll_device(begin["login_id"]) == {"status": "pending"}
    finally:
        await service.cancel(begin["login_id"])


async def test_loopback_callback_exchanges_code_and_persists(tmp_path, loopback_shape):
    service, session = _service(
        tmp_path,
        responses=[
            _FakeResp(
                200,
                {
                    "accessToken": "at-lb",
                    "refreshToken": "rt-lb",
                    "expiresIn": 3600,
                    "profileArn": "arn:aws:kiro::1:profile/social",
                },
            )
        ],
    )
    begin = await service.begin_loopback("github")
    page = await _hit_callback(
        begin["port"],
        {"login_option": "github", "code": "code-1", "state": _state_from(begin["auth_url"])},
    )
    assert "Login complete" in page
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result == {"status": "authorized", "provider": "Github"}
    saved = TokenStore(tmp_path).resolve()
    assert saved is not None and saved.identity == "social"
    # The exchange carried the code, the PKCE verifier, and the rebuilt redirect URI.
    url, body = session.calls[-1]
    assert url.endswith("/oauth/token")
    assert body["code"] == "code-1"
    assert body["code_verifier"]
    assert body["redirect_uri"] == (
        f"http://localhost:{begin['port']}/oauth/callback?login_option=github"
    )
    # Terminal: the entry is gone and the port is released.
    with pytest.raises(UnknownLoginError):
        await service.poll_device(begin["login_id"])


async def test_loopback_state_mismatch_is_error_and_saves_nothing(tmp_path, loopback_shape):
    service, _ = _service(tmp_path)
    begin = await service.begin_loopback("google")
    page = await _hit_callback(
        begin["port"], {"login_option": "google", "code": "c", "state": "not-ours"}
    )
    assert "Login failed" in page
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result["status"] == "error"
    assert result["code"] == "loopback_failed"
    assert TokenStore(tmp_path).resolve() is None


async def test_loopback_portal_error_is_error(tmp_path, loopback_shape):
    service, _ = _service(tmp_path)
    begin = await service.begin_loopback("google")
    await _hit_callback(begin["port"], {"error": "access_denied"})
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result == {
        "status": "error",
        "code": "loopback_failed",
        "error": "portal returned error: access_denied",
    }


async def test_loopback_timeout_reports_expired_with_fallback_code(
    tmp_path, loopback_shape, monkeypatch
):
    """A listener nobody reaches (browser on another machine) must degrade, not hang."""
    monkeypatch.setattr("kiro_crew.auth.service.LOOPBACK_TIMEOUT_SECS", 0.05)
    service, _ = _service(tmp_path)
    begin = await service.begin_loopback("google")
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result == {"status": "expired", "code": "loopback_timeout"}
    assert LOOPBACK_TIMEOUT_SECS == 300.0  # the module default is untouched


async def test_cancel_releases_the_port_for_reuse(tmp_path, loopback_shape):
    service, _ = _service(tmp_path)
    first = await service.begin_loopback("google")
    assert not _port_is_closed(first["port"])
    await service.cancel(first["login_id"])
    # Let the cancelled listener task unwind and close its socket.
    await asyncio.sleep(0.05)
    assert _port_is_closed(first["port"])
    with pytest.raises(UnknownLoginError):
        await service.poll_device(first["login_id"])
    # A fresh login binds again without tripping over the released listener.
    second = await service.begin_loopback("google")
    try:
        assert not _port_is_closed(second["port"])
    finally:
        await service.cancel(second["login_id"])
    # Cancelling an unknown id is a no-op, not an error.
    await service.cancel("nope")


async def test_close_releases_pending_loopback_logins(tmp_path, loopback_shape):
    service, _ = _service(tmp_path)
    begin = await service.begin_loopback("google")
    await service.close()
    await asyncio.sleep(0.05)
    # The listener is gone, and a fresh service can bind again.
    assert _port_is_closed(begin["port"])
    other, _ = _service(tmp_path)
    again = await other.begin_loopback("google")
    try:
        assert not _port_is_closed(again["port"])
    finally:
        await other.cancel(again["login_id"])


async def test_all_ports_busy_is_loopback_unavailable(tmp_path, loopback_shape, monkeypatch):
    def _no_ports():
        raise portal.PortalAuthError("all callback ports in use")

    monkeypatch.setattr(portal, "bind_allowed_port", _no_ports)
    service, _ = _service(tmp_path)
    with pytest.raises(LoopbackUnavailableError):
        await service.begin_loopback("google")


async def test_loopback_provider_follows_the_portal_choice_not_the_button(tmp_path, loopback_shape):
    """The Kiro portal is where Google vs GitHub is actually picked.

    Clicking Google here only opened the portal; if the user picks GitHub on it,
    the stored credential must say GitHub — KAS classifies the account by it.
    """
    ok = {"accessToken": "a", "expiresIn": 3600, "profileArn": "arn:p"}
    service, _ = _service(tmp_path, responses=[_FakeResp(200, ok), _FakeResp(200, ok)])
    begin = await service.begin_loopback("google")
    await _hit_callback(
        begin["port"],
        {"login_option": "github", "code": "c", "state": _state_from(begin["auth_url"])},
    )
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result == {"status": "authorized", "provider": "Github"}

    # An unrecognised/empty login_option falls back to the clicked provider.
    begin2 = await service.begin_loopback("google")
    await _hit_callback(begin2["port"], {"code": "c2", "state": _state_from(begin2["auth_url"])})
    result2 = await _poll_until_terminal(service, begin2["login_id"])
    assert result2 == {"status": "authorized", "provider": "Google"}


async def test_cancelled_loopback_never_persists_even_after_callback(tmp_path, loopback_shape):
    """Persistence happens in the poll, not the listener task.

    The callback can land (and the exchange succeed) after the user has already
    clicked "use a code instead": the credential must not appear behind their back.
    """
    ok = {"accessToken": "a", "expiresIn": 3600, "profileArn": "arn:p"}
    service, _ = _service(tmp_path, responses=[_FakeResp(200, ok)])
    begin = await service.begin_loopback("google")
    await _hit_callback(
        begin["port"],
        {"login_option": "google", "code": "c", "state": _state_from(begin["auth_url"])},
    )
    # Let the task finish the exchange before anyone polls, then cancel the login.
    await asyncio.sleep(0.1)
    await service.cancel(begin["login_id"])
    assert TokenStore(tmp_path).resolve() is None
    with pytest.raises(UnknownLoginError):
        await service.poll_device(begin["login_id"])


async def test_cancel_cannot_interleave_with_an_in_flight_save(tmp_path, loopback_shape):
    """A cancel issued while the poll is persisting waits for the write to land.

    The poll holds the pending lock across the store write, so cancel either runs
    before it (nothing is ever written) or after it (the poll has already answered
    ``authorized``). It must never return in between, with the abandoned
    credential still on its way to disk.
    """
    ok = {"accessToken": "a", "expiresIn": 3600, "profileArn": "arn:p"}
    service, _ = _service(tmp_path, responses=[_FakeResp(200, ok)])
    begin = await service.begin_loopback("google")
    await _hit_callback(
        begin["port"],
        {"login_option": "google", "code": "c", "state": _state_from(begin["auth_url"])},
    )
    await asyncio.sleep(0.1)  # let the listener finish the exchange

    save_started = threading.Event()
    release_save = threading.Event()
    real_save = service._store.save
    saves: list[str] = []

    def slow_save(token):
        save_started.set()
        assert release_save.wait(5), "test never released the save"
        saves.append(token.identity)
        real_save(token)

    service._store.save = slow_save  # type: ignore[method-assign]

    poll = asyncio.create_task(service.poll_device(begin["login_id"]))
    await asyncio.to_thread(save_started.wait, 5)
    cancel = asyncio.create_task(service.cancel(begin["login_id"]))
    await asyncio.sleep(0.1)
    # The write is still blocked, so cancel must still be waiting on the lock.
    assert not cancel.done()
    release_save.set()
    result = await poll
    await cancel
    assert result == {"status": "authorized", "provider": "Google"}
    assert saves == ["social"]  # written exactly once, after which cancel was a no-op
    assert TokenStore(tmp_path).resolve() is not None


# ── bind_allowed_port (the production binder, driven against ephemeral ports) ──


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_bind_allowed_port_skips_a_busy_port_and_listens_on_the_next(monkeypatch):
    # A squatter on the first candidate must be skipped, not fatal; the port
    # actually bound must already be listening when the binder returns (a
    # redirect can arrive before the serving task starts).
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    free = _free_port()
    monkeypatch.setattr(portal, "CALLBACK_PORTS", (busy.getsockname()[1], free))
    try:
        sock, port = portal.bind_allowed_port()
        try:
            assert port == free
            assert not _port_is_closed(port)
        finally:
            sock.close()
    finally:
        busy.close()


def test_bind_allowed_port_reports_when_every_port_is_taken(monkeypatch):
    holders = []
    for _ in range(2):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        holders.append(s)
    monkeypatch.setattr(portal, "CALLBACK_PORTS", tuple(s.getsockname()[1] for s in holders))
    try:
        with pytest.raises(portal.PortalAuthError, match="all callback ports in use"):
            portal.bind_allowed_port()
    finally:
        for s in holders:
            s.close()
