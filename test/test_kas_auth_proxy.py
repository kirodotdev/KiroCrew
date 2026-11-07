"""Sign-in and refresh requests go through the configured proxy, not around it.

Every issuer request in ``kiro_crew.auth`` passes ``proxy=resolve_proxy(url)`` so a
host whose only egress is an HTTP(S) proxy can still sign in and refresh. The
session keeps ``trust_env`` off (netrc credentials are never attached); the proxy
comes from ``kiro_crew.http_proxy`` alone. The network floor in conftest keeps
this file honest by never letting a real connection out.
"""

import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from kiro_crew import cli_kas_login
from kiro_crew.auth import refresh
from kiro_crew.auth.login import builder_id, control_plane, device, external_idp, portal
from kiro_crew.auth.login.control_plane import control_plane_url
from kiro_crew.auth.login.endpoints import oidc_url, social_service_url
from kiro_crew.auth.service import KasLoginService
from kiro_crew.auth.store import KasToken, SocialProvider, TokenStore

_HTTPS_PROXY = "http://https-proxy.example:3128"
_HTTP_PROXY = "http://http-proxy.example:3128"
_IDP_TOKEN_URL = "https://idp.example.test/oauth2/token"
_REGION = "us-east-1"


def _resp(payload: dict, *, status: int = 200) -> MagicMock:
    resp = MagicMock(spec=aiohttp.ClientResponse)
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value=str(payload))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.fixture
def transport(monkeypatch):
    """Stub aiohttp's single request seam; keep sessions real so ``trust_env`` shows."""
    request = AsyncMock()
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)
    sessions = []
    session_class = aiohttp.ClientSession

    def make_session(*args, **kwargs):
        session = session_class(*args, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", make_session)
    return request, sessions


def _meta() -> external_idp.ExternalIdpMetadata:
    return external_idp.ExternalIdpMetadata(
        issuer_url="https://idp.example.test",
        client_id="idp-client",
        scopes="openid",
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint=_IDP_TOKEN_URL,
    )


def _oidc_auth() -> builder_id.DeviceAuthorization:
    return builder_id.DeviceAuthorization(
        device_code="dc",
        user_code="uc",
        verification_uri="https://device.example/verify",
        verification_uri_complete="https://device.example/verify?c=uc",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        interval_secs=5.0,
    )


def _token(identity: str) -> KasToken:
    return KasToken(
        access_token="at",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider="BuilderId",
        identity=identity,
        refresh_token="rt",
        client_id="ci",
        client_secret="cs",
        token_endpoint=_IDP_TOKEN_URL if identity == "external_idp" else None,
    )


# One call site per issuer endpoint the sign-in and refresh paths touch. The
# response payloads are the smallest bodies each parser accepts; token shape
# correctness belongs to each flow's own suite.
_CASES: dict[str, tuple[str, dict]] = {
    "social-authorize": (
        f"{social_service_url()}/oauth/device/authorization",
        {
            "deviceCode": "dc",
            "userCode": "uc",
            "verificationUri": "https://device.example/verify",
            "verificationUriComplete": "https://device.example/verify?c=uc",
            "expiresInMilliseconds": 300_000,
            "intervalInMilliseconds": 5_000,
        },
    ),
    "oidc-register": (
        f"{oidc_url(_REGION)}/client/register",
        {"clientId": "ci", "clientSecret": "cs"},
    ),
    "oidc-token-poll": (
        f"{oidc_url(_REGION)}/token",
        {"accessToken": "at", "expiresIn": 3600, "refreshToken": "rt"},
    ),
    "portal-exchange": (
        f"{social_service_url()}/oauth/token",
        {"accessToken": "at", "profileArn": "arn:aws:profile/x", "expiresIn": 3600},
    ),
    "idp-exchange": (
        _IDP_TOKEN_URL,
        {"access_token": "at", "expires_in": 3600},
    ),
    "control-plane": (
        control_plane_url(_REGION),
        {"profiles": []},
    ),
    "refresh-social": (
        f"{social_service_url()}/refreshToken",
        {"accessToken": "at", "expiresIn": 3600, "profileArn": "arn:aws:profile/x"},
    ),
    "refresh-oidc": (
        f"{oidc_url(_REGION)}/token",
        {"accessToken": "at", "expiresIn": 3600},
    ),
    "refresh-idp": (
        _IDP_TOKEN_URL,
        {"access_token": "at", "expires_in": 3600},
    ),
}


async def _drive(site: str, session) -> None:
    if site == "social-authorize":
        await device.initiate_device_authorization(SocialProvider.GOOGLE, session=session)
    elif site == "oidc-register":
        await builder_id.register_client(_REGION, session=session)
    elif site == "oidc-token-poll":
        await builder_id.poll_token_once(
            builder_id.RegisteredClient("ci", "cs"),
            _oidc_auth(),
            region=_REGION,
            session=session,
        )
    elif site == "portal-exchange":
        await portal.exchange_code(
            SocialProvider.GOOGLE, "code", "verifier", "http://x/cb", session=session
        )
    elif site == "idp-exchange":
        await external_idp.exchange_code(
            _meta(), code="c", code_verifier="v", redirect_uri="http://x/cb", session=session
        )
    elif site == "control-plane":
        await control_plane.list_available_profiles("at", region=_REGION, session=session)
    elif site == "refresh-social":
        await refresh._refresh(_token("social"), session=session)
    elif site == "refresh-oidc":
        await refresh._refresh(_token("builder_id"), session=session)
    elif site == "refresh-idp":
        await refresh._refresh(_token("external_idp"), session=session)
    else:  # pragma: no cover - guards the table, not the product
        raise AssertionError(f"unknown site {site}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        ({"https": _HTTPS_PROXY, "http": _HTTP_PROXY}, _HTTPS_PROXY),
        ({"https": _HTTPS_PROXY}, _HTTPS_PROXY),
        ({"http": _HTTP_PROXY}, _HTTP_PROXY),
        ({}, None),
    ],
    ids=["https-first", "https-only", "http-fallback", "direct"],
)
@pytest.mark.parametrize("site", list(_CASES))
async def test_auth_request_uses_the_env_proxy(monkeypatch, transport, site, proxies, expected):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
    bypass = MagicMock(return_value=False)
    monkeypatch.setattr(urllib.request, "proxy_bypass", bypass)
    request, sessions = transport
    url, payload = _CASES[site]
    request.return_value = _resp(payload)

    async with aiohttp.ClientSession() as session:
        await _drive(site, session)

    request.assert_awaited_once()
    assert request.await_args.args == ("POST", url)
    assert request.await_args.kwargs.get("proxy") == expected
    assert session.trust_env is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("site", "no_proxy", "expected"),
    [
        ("oidc-register", "oidc.us-east-1.amazonaws.com", None),
        ("idp-exchange", ".example.test", None),
        ("idp-exchange", "*", None),
        ("idp-exchange", "other.example", _HTTPS_PROXY),
        ("idp-exchange", "idp.example.test:443", None),
    ],
    ids=[
        "oidc-exact-host",
        "idp-domain",
        "idp-wildcard",
        "idp-other-host",
        "idp-port-qualified",
    ],
)
async def test_auth_request_honors_no_proxy(monkeypatch, transport, site, no_proxy, expected):
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _HTTPS_PROXY)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    # Use the environment readers on every OS, independent of system settings.
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass", urllib.request.proxy_bypass_environment)
    request, _ = transport
    url, payload = _CASES[site]
    request.return_value = _resp(payload)

    async with aiohttp.ClientSession() as session:
        await _drive(site, session)

    request.assert_awaited_once()
    assert request.await_args.kwargs.get("proxy") == expected


@pytest.mark.asyncio
async def test_port_qualified_no_proxy_bypasses_a_custom_idp_port(monkeypatch, transport):
    """``NO_PROXY=host:port`` bypasses an IdP endpoint on a non-443 port.

    Enterprise IdP endpoints are operator-configured, so the shared resolver
    checks the effective port alongside the bare host -- the finding the GPT 6.1
    lane upheld on #31732.
    """
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _HTTPS_PROXY)
    monkeypatch.setenv("NO_PROXY", "idp.example.test:8443")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(urllib.request, "proxy_bypass", urllib.request.proxy_bypass_environment)
    request, _ = transport
    request.return_value = _resp({"access_token": "at", "expires_in": 3600})
    meta = external_idp.ExternalIdpMetadata(
        issuer_url="https://idp.example.test:8443",
        client_id="idp-client",
        scopes="openid",
        authorization_endpoint="https://idp.example.test:8443/authorize",
        token_endpoint="https://idp.example.test:8443/oauth2/token",
    )

    async with aiohttp.ClientSession() as session:
        await external_idp.exchange_code(
            meta, code="c", code_verifier="v", redirect_uri="http://x/cb", session=session
        )

    request.assert_awaited_once()
    assert request.await_args.kwargs.get("proxy") is None


@pytest.mark.asyncio
async def test_service_begin_and_poll_go_through_the_proxy(monkeypatch, transport, tmp_path):
    """The full `kas login` path: the service's own shared session carries proxy=.

    Zedmor's repro in #20185 was this end-to-end flow -- begin_device plus the
    approval poll -- timing out on a proxy-only host.
    """
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": _HTTPS_PROXY})
    monkeypatch.setattr(urllib.request, "proxy_bypass", MagicMock(return_value=False))
    request, sessions = transport
    request.side_effect = [
        _resp(_CASES["social-authorize"][1]),
        _resp(
            {
                "status": "authorized",
                "accessToken": "at",
                "refreshToken": "rt",
                "profileArn": "arn:aws:profile/x",
                "identityProvider": "google",
                "expiresIn": 3600,
            }
        ),
    ]
    service = KasLoginService(TokenStore(tmp_path))
    try:
        begun = await service.begin_device("google")
        result = await service.poll_device(begun["login_id"])
    finally:
        await service.close()

    assert result.get("status") == "authorized"
    assert request.await_count == 2
    for call in request.await_args_list:
        assert call.kwargs.get("proxy") == _HTTPS_PROXY
    assert sessions and all(s.trust_env is False for s in sessions)


@pytest.mark.parametrize(
    ("provider", "expected_url"),
    [
        ("google", social_service_url()),
        ("builder_id", oidc_url("us-east-1")),
        ("idc", oidc_url("us-east-1")),
    ],
)
def test_unreachable_message_names_the_configured_proxy(monkeypatch, provider, expected_url):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": _HTTPS_PROXY})
    monkeypatch.setattr(urllib.request, "proxy_bypass", MagicMock(return_value=False))

    note = cli_kas_login._configured_proxy_note(provider, "")

    assert _HTTPS_PROXY in note
    assert "proxy" in note


def test_unreachable_message_stays_quiet_without_a_proxy(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})

    assert cli_kas_login._configured_proxy_note("google", "") == ""


def test_unreachable_message_stays_quiet_when_bypassed(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": _HTTPS_PROXY})
    monkeypatch.setattr(urllib.request, "proxy_bypass", MagicMock(return_value=True))

    assert cli_kas_login._configured_proxy_note("google", "") == ""
