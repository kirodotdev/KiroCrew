"""Kiro sign-in and token renewal requests go through the proxy the environment names.

Every request ``kiro_crew.auth`` sends to the sign-in service passes an explicit
``proxy=`` chosen by :func:`kiro_crew.http_proxy.resolve_proxy`, so ``HTTPS_PROXY`` /
``HTTP_PROXY`` / ``NO_PROXY`` apply while the session keeps ``trust_env`` off (no
``~/.netrc`` credentials). The fake session records each request's keyword arguments
and then fails the connection, so no test touches the network.
"""

from __future__ import annotations

import urllib.request
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from kiro_crew import http_proxy
from kiro_crew.auth import refresh
from kiro_crew.auth.login import builder_id, control_plane, device, external_idp, portal
from kiro_crew.auth.login.builder_id import DeviceAuthorization as OidcDeviceAuthorization
from kiro_crew.auth.login.builder_id import RegisteredClient
from kiro_crew.auth.login.device import DeviceAuthorization
from kiro_crew.auth.login.external_idp import ExternalIdpMetadata
from kiro_crew.auth.provider import KasAuthProvider
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import KasToken, SocialProvider, TokenStore

pytestmark = pytest.mark.asyncio

_PROXY = "http://proxy.example:3128"
_SOCIAL = "https://auth.issuer.example"
_IDP_TOKEN = "https://idp.example/oauth2/token"


class _Refused:
    """A request whose connection fails, which every auth caller handles or raises."""

    async def __aenter__(self):
        raise aiohttp.ClientConnectionError("test double: no network")

    async def __aexit__(self, *a):
        return False


class _RecordingSession:
    closed = False

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return _Refused()

    async def close(self):
        return None


@pytest.fixture
def proxy_env(monkeypatch):
    # Pin the readers to environment-only variants on every OS, as test_http_proxy does.
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    monkeypatch.setenv("KIRO_AUTH_SERVICE_URL", _SOCIAL)
    monkeypatch.setattr(
        http_proxy.urllib.request, "getproxies", urllib.request.getproxies_environment
    )
    monkeypatch.setattr(
        http_proxy.urllib.request, "proxy_bypass", urllib.request.proxy_bypass_environment
    )
    return monkeypatch


def _later(secs: int = 300) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=secs)


def _social_auth() -> DeviceAuthorization:
    return DeviceAuthorization(
        device_code="dc",
        user_code="UC",
        verification_uri="https://app.example/device",
        verification_uri_complete="https://app.example/device?c=UC",
        expires_at=_later(),
        interval_secs=0.0,
    )


def _oidc_auth() -> OidcDeviceAuthorization:
    return OidcDeviceAuthorization(
        device_code="dc",
        user_code="UC",
        verification_uri="https://device.example",
        verification_uri_complete="https://device.example?c=UC",
        expires_at=_later(),
        interval_secs=0.0,
    )


_CLIENT = RegisteredClient(client_id="cid", client_secret="cs")


def _token(identity: str, **kw) -> KasToken:
    return KasToken(
        access_token="at",
        expires_at=_later(-60),
        provider="p",
        identity=identity,
        refresh_token="rt",
        profile_arn="arn:x",
        region="us-east-1",
        **kw,
    )


_IDP = ExternalIdpMetadata(
    issuer_url="https://idp.example",
    client_id="cid",
    scopes="openid",
    authorization_endpoint="https://idp.example/authorize",
    token_endpoint=_IDP_TOKEN,
)

# One entry per request site in kiro_crew.auth: (label, coroutine factory).
_SITES = [
    (
        "social device authorization",
        lambda s: device.initiate_device_authorization(SocialProvider.GOOGLE, session=s),
    ),
    (
        "social device poll",
        lambda s: device.poll_device_token(_social_auth(), SocialProvider.GOOGLE, session=s),
    ),
    (
        "portal code exchange",
        lambda s: portal.exchange_code(
            SocialProvider.GOOGLE, "code", "verifier", "http://127.0.0.1:3128", session=s
        ),
    ),
    ("OIDC register client", lambda s: builder_id.register_client("us-east-1", session=s)),
    (
        "OIDC device authorization",
        lambda s: builder_id.start_device_authorization(_CLIENT, region="us-east-1", session=s),
    ),
    (
        "OIDC poll once",
        lambda s: builder_id.poll_token_once(_CLIENT, _oidc_auth(), region="us-east-1", session=s),
    ),
    (
        "OIDC poll loop",
        lambda s: builder_id.poll_token(_CLIENT, _oidc_auth(), region="us-east-1", session=s),
    ),
    (
        "external IdP code exchange",
        lambda s: external_idp.exchange_code(
            _IDP, code="c", code_verifier="v", redirect_uri="http://127.0.0.1:3128", session=s
        ),
    ),
    (
        "control-plane profile page",
        lambda s: control_plane._profiles_page(
            "https://cps.issuer.example/", {}, {}, s  # noqa: SLF001
        ),
    ),
    ("social refresh", lambda s: refresh._refresh(_token("social"), session=s)),  # noqa: SLF001
    (
        "SSO-OIDC refresh",
        lambda s: refresh._refresh(  # noqa: SLF001
            _token("identity_center", client_id="cid", client_secret="cs"), session=s
        ),
    ),
    (
        "external IdP refresh",
        lambda s: refresh._refresh(  # noqa: SLF001
            _token("external_idp", token_endpoint=_IDP_TOKEN), session=s
        ),
    ),
]


async def _drive(factory) -> _RecordingSession:
    session = _RecordingSession()
    try:
        await factory(session)
    except Exception:  # noqa: BLE001 - the double refuses every connection
        pass
    assert session.requests, "the site sent no request"
    return session


@pytest.mark.parametrize(("label", "factory"), _SITES, ids=[s[0] for s in _SITES])
async def test_every_auth_request_goes_through_the_env_proxy(proxy_env, label, factory):
    session = await _drive(factory)
    for url, kwargs in session.requests:
        assert kwargs.get("proxy") == _PROXY, f"{label}: {url} sent without the proxy"


@pytest.mark.parametrize(("label", "factory"), _SITES, ids=[s[0] for s in _SITES])
async def test_no_proxy_host_is_sent_direct(proxy_env, label, factory):
    proxy_env.setenv("NO_PROXY", ".example,.amazonaws.com")
    session = await _drive(factory)
    for url, kwargs in session.requests:
        assert "proxy" in kwargs, f"{label}: {url} never chose a proxy"
        assert kwargs["proxy"] is None, f"{label}: {url} used a proxy for a NO_PROXY host"


async def test_login_service_poll_goes_through_the_env_proxy(proxy_env, tmp_path):
    session = _RecordingSession()
    service = KasLoginService(TokenStore(tmp_path), session=session)  # type: ignore[arg-type]

    async def _fake_initiate(provider, *, session):
        return _social_auth()

    proxy_env.setattr(device, "initiate_device_authorization", _fake_initiate)
    login_id = (await service.begin_device("google"))["login_id"]
    await service.poll_device(login_id)
    assert [kw.get("proxy") for _, kw in session.requests] == [_PROXY]


async def test_renewal_session_is_bounded_and_ignores_env_credentials(tmp_path):
    provider = KasAuthProvider(TokenStore(tmp_path))
    session, temp = await provider._session_or_temp()  # noqa: SLF001
    try:
        assert temp
        # aiohttp's own default is five minutes, far past the engine's patience.
        assert session.timeout.total is not None and session.timeout.total <= 30
        assert session.trust_env is False
    finally:
        await session.close()
