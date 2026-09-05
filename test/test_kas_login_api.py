"""Tests for the KAS-login HTTP surface: handlers (unit, faked request) + the
loopback callback listener (real socket, real aiohttp client).

Handlers are exercised without a running gateway: they only touch
``request.app['kas_login_service']`` and the JSON body, so a minimal fake
request object is enough to pin the HTTP contract, including the machine-
readable ``code`` on every non-2xx body.
"""

from __future__ import annotations

import asyncio
import json
import socket

import aiohttp
import pytest

from kiro_crew.auth.login.portal import PortalAuthError, wait_for_callback
from kiro_crew.auth.service import KasLoginService, LoopbackUnavailableError, UnknownLoginError
from kiro_crew.dashboard.handlers import kas_login
from kiro_crew.dashboard.handlers.kas_login import (
    api_kas_login_begin_device,
    api_kas_login_begin_loopback,
    api_kas_login_cancel,
    api_kas_login_logout,
    api_kas_login_poll,
    api_kas_login_status,
)

pytestmark = pytest.mark.asyncio


class _FakeState:
    owner_id = ""


class _FakeRequest:
    def __init__(self, service, body=None, *, owner=True):
        app = {"kas_login_service": service} if service is not None else {}
        app["state"] = _FakeState()
        self.app = app
        self._body = body
        self.path = "/api/kas-login"
        # is_owner_dashboard_request reads request["app"] (the app-scope claim, ""
        # for the dashboard owner) and request.get("user"); a local-owner subject
        # with an empty app scope is the owner.
        self._items = {"app": "", "user": "local-app" if owner else "someone-else"}

    def __getitem__(self, key):
        return self._items[key]

    def __contains__(self, key):
        return key in self._items

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def get(self, key, default=""):
        return self._items.get(key, default)


class _StubService(KasLoginService):
    """Overrides the orchestration methods so handler tests need no store/network."""

    def __init__(self):  # deliberately skips parent init: handlers never reach it
        self.calls = []

    async def status(self):
        self.calls.append("status")
        return {"authenticated": False, "provider": "", "identity": "", "transport": "device"}

    async def begin_device(self, provider_str, *, start_url="", region=""):
        self.calls.append(("begin", provider_str))
        if provider_str == "facebook":
            raise ValueError(provider_str)
        return {
            "login_id": "lid-1",
            "user_code": "ABCD",
            "verification_uri_complete": "https://v?c=ABCD",
            "expires_at": "2026-01-01T00:00:00+00:00",
        }

    async def poll_device(self, login_id):
        self.calls.append(("poll", login_id))
        if login_id == "gone":
            raise UnknownLoginError(login_id)
        return {"status": "pending"}

    async def logout(self, identity):
        self.calls.append(("logout", identity))
        if identity != "social":
            raise ValueError(identity)

    async def begin_loopback(self, provider_str):
        self.calls.append(("loopback", provider_str))
        if provider_str == "facebook":
            raise ValueError(provider_str)
        if provider_str == "github":
            raise LoopbackUnavailableError("every allowlisted port is busy")
        return {
            "login_id": "lid-lb",
            "auth_url": "https://app.kiro.dev/signin?state=s",
            "port": 8337,
            "expires_at": "2026-01-01T00:00:00+00:00",
        }

    async def cancel(self, login_id):
        self.calls.append(("cancel", login_id))


@pytest.fixture(autouse=True)
def _mute_sel(monkeypatch):
    # Audits must not write to the real SEL store from unit tests.
    class _NullSel:
        def log_api_access(self, **kwargs):
            pass

    monkeypatch.setattr(kas_login, "sel", lambda: _NullSel())


def _body(resp) -> dict:
    return json.loads(resp.text)


async def test_all_handlers_503_with_code_when_service_unavailable(monkeypatch):
    # The service is built lazily on first request; if construction fails, the
    # handlers surface a coded 503 rather than crashing.
    def _boom(*_a, **_k):
        raise RuntimeError("no data home")

    monkeypatch.setattr(kas_login, "TokenStore", _boom)
    for handler, body in (
        (api_kas_login_status, None),
        (api_kas_login_begin_device, {"provider": "google"}),
        (api_kas_login_poll, {"login_id": "x"}),
        (api_kas_login_logout, {"identity": "social"}),
        (api_kas_login_begin_loopback, {"provider": "google"}),
        (api_kas_login_cancel, {"login_id": "x"}),
    ):
        resp = await handler(_FakeRequest(None, body))
        assert resp.status == 503
        assert _body(resp)["code"] == "kas_login_unavailable"


async def test_status_ok():
    resp = await api_kas_login_status(_FakeRequest(_StubService()))
    assert resp.status == 200
    assert _body(resp)["transport"] == "device"


async def test_begin_device_ok():
    resp = await api_kas_login_begin_device(_FakeRequest(_StubService(), {"provider": "google"}))
    assert resp.status == 200
    assert _body(resp)["login_id"] == "lid-1"


async def test_begin_device_missing_and_unknown_provider():
    resp = await api_kas_login_begin_device(_FakeRequest(_StubService(), {}))
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_provider"

    resp = await api_kas_login_begin_device(_FakeRequest(_StubService(), {"provider": "facebook"}))
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_provider"


async def test_begin_device_malformed_json_is_400():
    resp = await api_kas_login_begin_device(_FakeRequest(_StubService(), None))
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_provider"


async def test_poll_ok_and_missing_and_unknown():
    resp = await api_kas_login_poll(_FakeRequest(_StubService(), {"login_id": "lid-1"}))
    assert resp.status == 200
    assert _body(resp)["status"] == "pending"

    resp = await api_kas_login_poll(_FakeRequest(_StubService(), {}))
    assert resp.status == 400
    assert _body(resp)["code"] == "missing_login_id"

    resp = await api_kas_login_poll(_FakeRequest(_StubService(), {"login_id": "gone"}))
    assert resp.status == 404
    assert _body(resp)["code"] == "unknown_login_id"


async def test_logout_ok_and_invalid_identity():
    resp = await api_kas_login_logout(_FakeRequest(_StubService(), {"identity": "social"}))
    assert resp.status == 200
    assert _body(resp) == {"ok": True}

    resp = await api_kas_login_logout(_FakeRequest(_StubService(), {"identity": "bogus"}))
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_identity"


async def test_credential_mutations_reject_non_owner():
    # begin/poll/logout mutate the machine-global Kiro credential, so a non-owner
    # dashboard caller must get an audited 403 and never reach the service.
    for handler, body in (
        (api_kas_login_begin_device, {"provider": "google"}),
        (api_kas_login_poll, {"login_id": "lid-1"}),
        (api_kas_login_logout, {"identity": "social"}),
        (api_kas_login_begin_loopback, {"provider": "google"}),
        (api_kas_login_cancel, {"login_id": "lid-lb"}),
    ):
        svc = _StubService()
        resp = await handler(_FakeRequest(svc, body, owner=False))
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"
        assert svc.calls == []  # short-circuited before the service


async def test_logout_store_failure_returns_coded_500():
    from kiro_crew.auth.store import TokenStoreError

    class _BrokenStoreService(_StubService):
        async def logout(self, identity):
            raise TokenStoreError("could not delete KAS token social")

    resp = await api_kas_login_logout(_FakeRequest(_BrokenStoreService(), {"identity": "social"}))
    assert resp.status == 500
    assert _body(resp)["code"] == "logout_failed"


async def test_status_open_to_non_owner():
    # status is a read (no credential mutation), so it stays available to any
    # allowed dashboard user.
    resp = await api_kas_login_status(_FakeRequest(_StubService(), owner=False))
    assert resp.status == 200


async def test_begin_and_poll_return_502_on_transport_error():
    import aiohttp

    class _OfflineService(_StubService):
        async def begin_device(self, provider_str, *, start_url="", region=""):
            raise aiohttp.ClientError("connection refused")

        async def poll_device(self, login_id):
            raise aiohttp.ClientError("connection refused")

    resp = await api_kas_login_begin_device(_FakeRequest(_OfflineService(), {"provider": "google"}))
    assert resp.status == 502
    assert _body(resp)["code"] == "auth_service_unreachable"

    resp = await api_kas_login_poll(_FakeRequest(_OfflineService(), {"login_id": "lid-1"}))
    assert resp.status == 502
    assert _body(resp)["code"] == "auth_service_unreachable"


async def test_status_store_failure_returns_coded_500():
    from kiro_crew.auth.store import TokenStoreError

    class _BrokenStoreService(_StubService):
        async def status(self):
            raise TokenStoreError("refusing linked token directory")

    resp = await api_kas_login_status(_FakeRequest(_BrokenStoreService()))
    assert resp.status == 500
    assert _body(resp)["code"] == "token_store_failed"


# ── loopback begin / cancel handlers ────────────────────────────────────────


async def test_begin_loopback_ok_returns_portal_url_and_port():
    svc = _StubService()
    resp = await api_kas_login_begin_loopback(_FakeRequest(svc, {"provider": "google"}))
    assert resp.status == 200
    body = _body(resp)
    assert body["login_id"] == "lid-lb"
    assert body["auth_url"].startswith("https://")
    assert body["port"] == 8337
    assert svc.calls == [("loopback", "google")]


async def test_begin_loopback_missing_unknown_and_malformed_provider_are_400():
    resp = await api_kas_login_begin_loopback(_FakeRequest(_StubService(), {}))
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_provider"

    resp = await api_kas_login_begin_loopback(
        _FakeRequest(_StubService(), {"provider": "facebook"})
    )
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_provider"

    resp = await api_kas_login_begin_loopback(_FakeRequest(_StubService(), None))
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_provider"


async def test_begin_loopback_unavailable_is_coded_409():
    # The dashboard keys its device-code fallback off this exact status + code,
    # so the shape is a contract, not a courtesy.
    resp = await api_kas_login_begin_loopback(_FakeRequest(_StubService(), {"provider": "github"}))
    assert resp.status == 409
    assert _body(resp)["code"] == "loopback_unavailable"


async def test_cancel_ok_and_missing_login_id():
    svc = _StubService()
    resp = await api_kas_login_cancel(_FakeRequest(svc, {"login_id": "lid-lb"}))
    assert resp.status == 200
    assert _body(resp) == {"ok": True}
    assert svc.calls == [("cancel", "lid-lb")]

    svc = _StubService()
    resp = await api_kas_login_cancel(_FakeRequest(svc, {}))
    assert resp.status == 400
    assert _body(resp)["code"] == "missing_login_id"
    assert svc.calls == []  # nothing to cancel, service untouched


async def test_cancel_is_idempotent_for_unknown_login_id():
    # Cancel is called on every start-over path in the dashboard, including after
    # the login already finished or expired, so an unknown id is a no-op 200.
    svc = _StubService()
    resp = await api_kas_login_cancel(_FakeRequest(svc, {"login_id": "never-existed"}))
    assert resp.status == 200
    assert _body(resp) == {"ok": True}


# ── loopback callback listener ──────────────────────────────────────────────


def _bound_loopback() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


async def _hit(port: int, path: str, params: dict) -> None:
    async with aiohttp.ClientSession() as client:
        async with client.get(f"http://127.0.0.1:{port}{path}", params=params) as resp:
            assert resp.status == 200


async def test_wait_for_callback_returns_query_fields():
    sock, port = _bound_loopback()
    task = asyncio.create_task(wait_for_callback(sock, port, "st-1", timeout_secs=10))
    await asyncio.sleep(0.05)  # let the listener start
    await _hit(port, "/oauth/callback", {"login_option": "google", "code": "c1", "state": "st-1"})
    result = await asyncio.wait_for(task, timeout=5)
    assert result["login_option"] == "google"
    assert result["code"] == "c1"
    assert result["state"] == "st-1"
    assert result["path"] == "/oauth/callback"


async def test_wait_for_callback_accepts_signin_path():
    sock, port = _bound_loopback()
    task = asyncio.create_task(wait_for_callback(sock, port, "st-2", timeout_secs=10))
    await asyncio.sleep(0.05)
    await _hit(port, "/signin/callback", {"login_option": "github", "code": "c2", "state": "st-2"})
    result = await asyncio.wait_for(task, timeout=5)
    assert result["path"] == "/signin/callback"


async def test_wait_for_callback_state_mismatch_raises():
    sock, port = _bound_loopback()
    task = asyncio.create_task(wait_for_callback(sock, port, "expected", timeout_secs=10))
    await asyncio.sleep(0.05)
    await _hit(port, "/oauth/callback", {"login_option": "google", "code": "c", "state": "evil"})
    with pytest.raises(PortalAuthError, match="state mismatch"):
        await asyncio.wait_for(task, timeout=5)


async def test_wait_for_callback_portal_error_raises():
    sock, port = _bound_loopback()
    task = asyncio.create_task(wait_for_callback(sock, port, "st", timeout_secs=10))
    await asyncio.sleep(0.05)
    await _hit(port, "/oauth/callback", {"error": "access_denied", "state": "st"})
    with pytest.raises(PortalAuthError, match="access_denied"):
        await asyncio.wait_for(task, timeout=5)


async def test_wait_for_callback_times_out():
    sock, port = _bound_loopback()
    with pytest.raises(PortalAuthError, match="timed out"):
        await wait_for_callback(sock, port, "st", timeout_secs=0.2)


def test_handlers_package_does_not_import_kas_at_boot():
    """Gateway boot must not load the KAS auth subsystem (GPT: no-new-work-on-boot).

    Route registration uses lazy per-request wrappers; the kas_login module (and
    the crypto stack behind it) must only load on the first /api/kas-login hit.
    """
    import subprocess
    import sys

    code = (
        "import sys; import kiro_crew.dashboard.handlers; "
        "bad = [m for m in sys.modules if 'kas_login' in m or m.startswith('kiro_crew.auth')]; "
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, f"boot import pulled in KAS modules: {proc.stdout}{proc.stderr}"
