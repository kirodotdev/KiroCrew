"""Tests for KasLoginService — the status/begin/poll/logout orchestration.

Network is faked at two seams: begin monkeypatches the device module's
initiate step, and poll drives the service's single-shot POST through a
scripted fake aiohttp session, so no test touches the real auth service.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.auth.login import device
from kiro_crew.auth.login.device import DeviceAuthorization
from kiro_crew.auth.service import KasLoginService, UnknownLoginError, _parse_provider
from kiro_crew.auth.store import KasToken, SocialProvider, TokenStore

pytestmark = pytest.mark.asyncio


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class _FakeSession:
    """Returns scripted responses per POST call, in order."""

    def __init__(self, responses: list[_FakeResp] | None = None):
        self._responses = responses or []
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, *, json=None, headers=None):  # noqa: A002
        self.calls.append((url, json or {}))
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


def _device_auth(expires_in_secs: float = 300) -> DeviceAuthorization:
    return DeviceAuthorization(
        device_code="dc-1",
        user_code="ABCD-EFGH",
        verification_uri="https://app.kiro.dev/account/device",
        verification_uri_complete="https://app.kiro.dev/account/device?user_code=ABCD-EFGH",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_secs),
        interval_secs=0.0,
    )


def _service(tmp_path, responses=None) -> tuple[KasLoginService, _FakeSession]:
    session = _FakeSession(responses)
    return KasLoginService(TokenStore(tmp_path), session=session), session


async def _begin(service: KasLoginService, monkeypatch, auth: DeviceAuthorization) -> dict:
    async def _fake_initiate(provider, *, session):
        return auth

    monkeypatch.setattr(device, "initiate_device_authorization", _fake_initiate)
    return await service.begin_device("google")


def test_parse_provider_accepts_wire_and_lower_names():
    assert _parse_provider("Google") is SocialProvider.GOOGLE
    assert _parse_provider("github") is SocialProvider.GITHUB
    with pytest.raises(ValueError):
        _parse_provider("facebook")


async def test_status_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_AUTH_TRANSPORT", "device")
    service, _ = _service(tmp_path)
    status = await service.status()
    assert status == {
        "authenticated": False,
        "provider": "",
        "identity": "",
        "transport": "device",
    }


async def test_status_reports_stored_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_AUTH_TRANSPORT", "loopback")
    store = TokenStore(tmp_path)
    store.save(
        KasToken(
            access_token="at",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="Google",
            identity="social",
            profile_arn="arn:aws:x",
        )
    )
    service = KasLoginService(store, session=_FakeSession())
    status = await service.status()
    assert status["authenticated"] is True
    assert status["provider"] == "Google"
    assert status["identity"] == "social"
    assert status["transport"] == "loopback"


async def test_begin_device_returns_public_fields_only(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    result = await _begin(service, monkeypatch, _device_auth())
    assert set(result) == {"login_id", "user_code", "verification_uri_complete", "expires_at"}
    assert result["user_code"] == "ABCD-EFGH"
    # The deviceCode is the secret half of the flow; it must not be exposed.
    assert "dc-1" not in str(result)


async def test_poll_unknown_login_id_raises(tmp_path):
    service, _ = _service(tmp_path)
    with pytest.raises(UnknownLoginError):
        await service.poll_device("nope")


async def test_poll_pending(tmp_path, monkeypatch):
    service, session = _service(tmp_path, [_FakeResp(200, {"status": "authorization_pending"})])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    assert await service.poll_device(login_id) == {"status": "pending"}
    # Sends the stashed deviceCode, never the login_id, to the auth service.
    _, body = session.calls[0]
    assert body == {"deviceCode": "dc-1", "clientId": "Kiro-CLI"}


async def test_poll_transient_http_error_stays_pending(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, [_FakeResp(500, "boom")])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    assert await service.poll_device(login_id) == {"status": "pending"}


async def test_poll_local_expiry_forgets_login(tmp_path, monkeypatch):
    service, session = _service(tmp_path)
    login_id = (await _begin(service, monkeypatch, _device_auth(expires_in_secs=-1)))["login_id"]
    assert await service.poll_device(login_id) == {"status": "expired"}
    assert session.calls == []  # expired locally, no wasted network poll
    with pytest.raises(UnknownLoginError):
        await service.poll_device(login_id)


async def test_poll_authorized_saves_token_and_forgets(tmp_path, monkeypatch):
    authorized = _FakeResp(
        200,
        {
            "status": "authorized",
            "accessToken": "at-1",
            "refreshToken": "rt-1",
            "profileArn": "arn:aws:profile/x",
            "identityProvider": "google",
            "expiresIn": 3600,
        },
    )
    service, _ = _service(tmp_path, [authorized])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    result = await service.poll_device(login_id)
    assert result == {"status": "authorized", "provider": "Google"}
    token = TokenStore(tmp_path).load("social")
    assert token is not None
    assert token.access_token == "at-1"
    with pytest.raises(UnknownLoginError):
        await service.poll_device(login_id)


async def test_poll_malformed_json_stays_pending(tmp_path, monkeypatch):
    # A 200 with an undecodable body must not crash the poll; treat as pending
    # (the flow's own expiry bounds the caller's retries).
    class _BadResp(_FakeResp):
        async def json(self):
            raise ValueError("not json")

    service, _ = _service(tmp_path, [_BadResp(200, None)])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    assert await service.poll_device(login_id) == {"status": "pending"}


async def test_poll_non_object_json_stays_pending(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, [_FakeResp(200, ["not", "a", "dict"])])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    assert await service.poll_device(login_id) == {"status": "pending"}


async def test_poll_authorized_store_write_failure_is_error(tmp_path, monkeypatch):
    authorized = _FakeResp(
        200,
        {
            "status": "authorized",
            "accessToken": "at-1",
            "refreshToken": "rt-1",
            "profileArn": "arn:aws:profile/x",
            "identityProvider": "google",
            "expiresIn": 3600,
        },
    )
    service, _ = _service(tmp_path, [authorized])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]

    def _boom(_token):
        from kiro_crew.auth.store import TokenStoreError

        raise TokenStoreError("could not persist KAS token social")

    monkeypatch.setattr(service._store, "save", _boom)
    # Approved but unpersistable: error (not authorized), and the login is dropped.
    assert await service.poll_device(login_id) == {"status": "error"}
    with pytest.raises(UnknownLoginError):
        await service.poll_device(login_id)


async def test_poll_invalid_token_is_error(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, [_FakeResp(200, {"status": "invalid_token"})])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    assert await service.poll_device(login_id) == {"status": "error"}


async def test_poll_authorized_without_profile_arn_is_error(tmp_path, monkeypatch):
    authorized = _FakeResp(
        200,
        {"status": "authorized", "accessToken": "at", "refreshToken": "rt"},
    )
    service, _ = _service(tmp_path, [authorized])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    assert await service.poll_device(login_id) == {"status": "error"}
    assert TokenStore(tmp_path).load("social") is None


async def test_logout_deletes_identity(tmp_path):
    store = TokenStore(tmp_path)
    store.save(
        KasToken(
            access_token="at",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="Google",
            identity="social",
            profile_arn="arn:aws:x",
        )
    )
    service = KasLoginService(store, session=_FakeSession())
    await service.logout("social")
    assert store.load("social") is None


async def test_logout_unknown_identity_raises(tmp_path):
    service, _ = _service(tmp_path)
    with pytest.raises(ValueError):
        await service.logout("../../etc/passwd")


async def test_close_closes_owned_session(tmp_path):
    service, session = _service(tmp_path)
    await service.close()
    assert session.closed is True


@pytest.mark.asyncio
async def test_begin_device_evicts_expired_pending(tmp_path, monkeypatch):
    # An abandoned login (never polled again) must not grow _pending without
    # bound: begin_device evicts entries whose device code already expired.
    service, _ = _service(tmp_path)
    stale = (await _begin(service, monkeypatch, _device_auth(expires_in_secs=-1)))["login_id"]
    live = (await _begin(service, monkeypatch, _device_auth(expires_in_secs=300)))["login_id"]
    assert stale not in service._pending
    assert live in service._pending
