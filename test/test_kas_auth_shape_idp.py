"""Tests for install-shape detection, transport selection, external IdP, and the bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.auth import bridge
from kiro_crew.auth.login import external_idp
from kiro_crew.auth.login.external_idp import ExternalIdpAuthError, ExternalIdpMetadata
from kiro_crew.auth.provider import KasAuthProvider
from kiro_crew.auth.shape import InstallShape, Transport, detect_shape, select_transport
from kiro_crew.auth.store import KasToken, TokenStore


def _clear_env(monkeypatch):
    for k in (
        "KIRO_AUTH_INSTALL_SHAPE",
        "KIRO_AUTH_TRANSPORT",
        "KIRO_AUTH_CONTAINER_PORTS_MAPPED",
        "SSH_CONNECTION",
        "SSH_CLIENT",
    ):
        monkeypatch.delenv(k, raising=False)


# ---- shape detection -----------------------------------------------------------


def test_ssh_env_detected_as_remote(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    assert detect_shape() is InstallShape.REMOTE


def test_forced_shape_override(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("KIRO_AUTH_INSTALL_SHAPE", "container")
    assert detect_shape() is InstallShape.CONTAINER


def test_remote_selects_device(monkeypatch):
    _clear_env(monkeypatch)
    assert select_transport(InstallShape.REMOTE) is Transport.DEVICE


def test_desktop_selects_loopback(monkeypatch):
    _clear_env(monkeypatch)
    assert select_transport(InstallShape.DESKTOP) is Transport.LOOPBACK


def test_container_without_mapping_selects_device(monkeypatch):
    _clear_env(monkeypatch)
    assert select_transport(InstallShape.CONTAINER) is Transport.DEVICE


def test_container_with_mapping_selects_loopback(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("KIRO_AUTH_CONTAINER_PORTS_MAPPED", "1")
    assert select_transport(InstallShape.CONTAINER) is Transport.LOOPBACK


def test_forced_transport_override_wins(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("KIRO_AUTH_TRANSPORT", "device")
    assert select_transport(InstallShape.DESKTOP) is Transport.DEVICE


# ---- external IdP --------------------------------------------------------------


def _meta() -> ExternalIdpMetadata:
    return ExternalIdpMetadata(
        issuer_url="https://idp.example.com",
        client_id="cid",
        scopes="openid profile",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        login_hint="user@example.com",
    )


def test_with_offline_access_appended():
    assert "offline_access" in _meta().with_offline_access().split()


def test_with_offline_access_not_duplicated():
    m = _meta()
    m.scopes = "openid offline_access"
    assert m.with_offline_access().split().count("offline_access") == 1


def test_build_authorization_url_shape():
    url = external_idp.build_authorization_url(
        _meta(), redirect_uri="http://localhost:3128/cb", state="ST", code_challenge="CH"
    )
    assert url.startswith("https://idp.example.com/authorize?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "code_challenge=CH" in url
    assert "code_challenge_method=S256" in url
    assert "offline_access" in url
    assert "login_hint=user%40example.com" in url


class _FakeResp:
    def __init__(self, status, payload):
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
    def __init__(self, responses):
        self._responses = list(responses)

    def post(self, url, *, json=None, data=None, headers=None):  # noqa: A002
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_external_idp_exchange_code_success():
    session = _FakeSession(
        [_FakeResp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 1800})]
    )
    tok = await external_idp.exchange_code(
        _meta(), code="c", code_verifier="v", redirect_uri="http://x", session=session
    )
    assert tok.access_token == "at"
    assert tok.provider == "ExternalIdp"
    assert tok.identity == "external_idp"
    assert tok.auth_method == "external_idp"
    assert tok.token_endpoint == "https://idp.example.com/token"


@pytest.mark.asyncio
async def test_external_idp_exchange_non_200_raises():
    session = _FakeSession([_FakeResp(401, "denied")])
    with pytest.raises(ExternalIdpAuthError):
        await external_idp.exchange_code(
            _meta(), code="c", code_verifier="v", redirect_uri="http://x", session=session
        )


# ---- bridge --------------------------------------------------------------------


def _provider_with_token(tmp_path: Path) -> KasAuthProvider:
    store = TokenStore(tmp_path)
    store.save(
        KasToken(
            access_token="at-social",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="Google",
            identity="social",
            refresh_token="rt",
            profile_arn="arn:x",
        )
    )
    return KasAuthProvider(store)


@pytest.mark.asyncio
async def test_bridge_handle_get_access_token(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    provider = _provider_with_token(tmp_path)
    resp = await bridge.handle_get_access_token(provider)
    assert resp["accessToken"] == "at-social"
    assert resp["provider"] == "Google"
    assert "refreshToken" not in resp


def test_bridge_iauthprovider_surface_keys(tmp_path: Path):
    provider = _provider_with_token(tmp_path)
    surface = bridge.as_iauthprovider(provider)
    assert set(surface) == {
        "getToken",
        "getProfileArn",
        "isAuthenticated",
        "readToken",
        "resolveRequestCredential",
    }
    # readToken is the non-refreshing sync peek
    assert surface["readToken"]() == {"authMethod": None, "provider": "Google"}
