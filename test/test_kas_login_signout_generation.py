"""A sign-out in one process stops a login pending in another from re-filling the vault.

The gateway's in-memory epoch covers its own pending logins. ``kirocrew kas logout``
runs in a shell, in a second process, with a second ``KasLoginService`` over the same
vault; its sign-out must still stop a dashboard login that was pending in the gateway
from persisting once the user approves the code. The store carries a sign-out
generation every sign-out bumps; a login snapshots it when it begins and compares it
before it writes. These tests drive two services over one store, the way the two
processes do, and also pin the two sibling fixes the same round brought: a refresh
keeps the token's recorded claims, and a refresh-rejected slot reads as signed out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.auth.refresh import ensure_fresh
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.auth.whoami import START_URL_EXTRA_KEY, resolve_signed_in


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
    def __init__(self, responses):
        self._responses = list(responses)

    def post(self, url, *, json=None, data=None, headers=None):  # noqa: A002
        return self._responses.pop(0)

    @property
    def closed(self):
        return False

    async def close(self):
        return None


def _device_auth():
    from kiro_crew.auth.login import device

    return device.DeviceAuthorization(
        device_code="dc-1",
        user_code="WXYZ-1234",
        verification_uri="https://app.kiro.dev/device",
        verification_uri_complete="https://app.kiro.dev/device?user_code=WXYZ-1234",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        interval_secs=0.0,
    )


async def _begin_social(service: KasLoginService, monkeypatch) -> str:
    from kiro_crew.auth.login import device

    async def _fake_initiate(provider, *, session):
        return _device_auth()

    monkeypatch.setattr(device, "initiate_device_authorization", _fake_initiate)
    return (await service.begin_device("google"))["login_id"]


_AUTHORIZED = {
    "status": "authorized",
    "accessToken": "at-1",
    "refreshToken": "rt-1",
    "expiresIn": 3600,
    "profileArn": "arn:aws:kiro:us-east-1:1:profile/p1",
}


@pytest.mark.asyncio
async def test_a_sign_out_in_another_process_stops_a_pending_login_from_persisting(
    tmp_path, monkeypatch
):
    store = TokenStore(tmp_path)
    gateway = KasLoginService(store, session=_FakeSession([_FakeResp(200, _AUTHORIZED)]))
    shell = KasLoginService(TokenStore(tmp_path))
    try:
        login_id = await _begin_social(gateway, monkeypatch)
        # The shell signs out while the dashboard's code is still awaiting approval.
        store.save(
            KasToken(
                access_token="old",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                provider="BuilderId",
                identity="builder_id",
            )
        )
        await shell.logout("builder_id")
        assert store.resolve() is None
        # The user approves the code; the gateway's poll must not re-fill the vault.
        result = await gateway.poll_device(login_id)
    finally:
        await gateway.close()
        await shell.close()
    assert result == {"status": "error", "code": "signed_out_during_login"}
    assert store.resolve() is None
    # Terminal for that login: it is gone from the pending table.
    from kiro_crew.auth.service import UnknownLoginError

    with pytest.raises(UnknownLoginError):
        await gateway.poll_device(login_id)


@pytest.mark.asyncio
async def test_a_login_with_no_sign_out_in_between_persists(tmp_path, monkeypatch):
    store = TokenStore(tmp_path)
    gateway = KasLoginService(store, session=_FakeSession([_FakeResp(200, _AUTHORIZED)]))
    try:
        login_id = await _begin_social(gateway, monkeypatch)
        result = await gateway.poll_device(login_id)
    finally:
        await gateway.close()
    assert result["status"] == "authorized"
    saved = store.resolve()
    assert saved is not None and saved.identity == "social"


def test_every_sign_out_moves_the_generation_and_an_absent_marker_reads_empty(tmp_path):
    store = TokenStore(tmp_path)
    assert store.signout_generation() == ""
    store.bump_signout_generation()
    first = store.signout_generation()
    assert first
    store.bump_signout_generation()
    assert store.signout_generation() not in ("", first)


@pytest.mark.asyncio
async def test_a_refresh_keeps_the_recorded_start_url(tmp_path):
    store = TokenStore(tmp_path)
    stale = KasToken(
        access_token="old",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        provider="Enterprise",
        identity="identity_center",
        refresh_token="rt",
        profile_arn="arn:aws:kiro:eu-west-1:1:profile/p1",
        region="eu-west-1",
        client_id="cid",
        client_secret="csec",
        extra={START_URL_EXTRA_KEY: "https://acme.awsapps.com/start"},
    )
    store.save(stale)
    out = await ensure_fresh(
        store,
        stale,
        session=_FakeSession([_FakeResp(200, {"accessToken": "new", "expiresIn": 3600})]),
    )
    assert out.access_token == "new"
    assert out.extra[START_URL_EXTRA_KEY] == "https://acme.awsapps.com/start"
    reread = store.resolve()
    assert (
        reread is not None and reread.extra[START_URL_EXTRA_KEY] == "https://acme.awsapps.com/start"
    )


def test_a_refresh_rejected_slot_reads_as_signed_out(tmp_path):
    store = TokenStore(tmp_path)
    token = KasToken(
        access_token="atk",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        provider="Enterprise",
        identity="identity_center",
        refresh_token="rt",
        profile_arn="arn:aws:kiro:us-east-1:1:profile/p1",
    )
    store.save(token)
    assert token.is_usable()  # a refresh token keeps the spawn predicate true
    assert resolve_signed_in(store) is not None
    store.mark_refresh_rejected("identity_center")
    # Doctor and the card call this "sign-in expired"; so does the shell verb.
    assert resolve_signed_in(store) is None


def test_a_sign_out_whose_fence_cannot_be_written_reports_failure(tmp_path, monkeypatch):
    """The marker is the cross-process fence; a sign-out must not publish success
    while a pending approval elsewhere could still re-fill the vault."""
    import asyncio

    from kiro_crew.auth.store import TokenStoreError

    store = TokenStore(tmp_path)
    store.save(
        KasToken(
            access_token="atk",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="BuilderId",
            identity="builder_id",
        )
    )

    # A marker path under a FILE: the write fails with NotADirectoryError on every
    # platform, with no permission bits to depend on.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(store, "_signout_generation_path", lambda: blocker / "signed-out")
    with pytest.raises(TokenStoreError, match="sign-out generation"):
        asyncio.run(KasLoginService(store).logout("builder_id"))
