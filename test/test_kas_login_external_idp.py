"""Tests for the organization (external IdP) sign-in of KasLoginService.

The loopback listener is real, as in ``test_kas_login_loopback.py``: tests play the
browser by GETting the portal leg and then the IdP leg against the bound port. The
discovery document, the code exchange and ``ListAvailableProfiles`` are answered by
a scripted fake session, so no test touches a real IdP or Kiro's servers.
"""

from __future__ import annotations

import socket
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.auth import refresh
from kiro_crew.auth.login import external_idp, portal
from kiro_crew.auth.login.external_idp import ExternalIdpAuthError, PortalIdpHandoff
from kiro_crew.auth.service import (
    InvalidExternalIdpInputError,
    InvalidRegionError,
    KasLoginService,
    LoopbackUnavailableError,
)
from kiro_crew.auth.shape import Transport
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.dashboard.handlers import kas_login as handlers
from kiro_crew.testing.wait import async_wait_until

_ISSUER = "https://idp.example.com"
_DISCOVERY = {
    "issuer": _ISSUER,
    "authorization_endpoint": "https://idp.example.com/authorize",
    "token_endpoint": "https://idp.example.com/token",
}
_PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEF"


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
    """Answers GETs and POSTs from separate scripts, recording every call."""

    def __init__(self, gets=None, posts=None):
        self._gets = list(gets or [])
        self._posts = list(posts or [])
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict, dict]] = []
        self.closed = False

    def get(self, url, *, headers=None):
        self.get_calls.append(url)
        return self._gets.pop(0)

    def post(self, url, *, json=None, data=None, headers=None):  # noqa: A002
        self.post_calls.append((url, json if json is not None else (data or {}), headers or {}))
        return self._posts.pop(0)

    async def close(self):
        self.closed = True


@pytest.fixture
def loopback_shape(monkeypatch):
    monkeypatch.setenv("KIRO_AUTH_TRANSPORT", Transport.LOOPBACK.value)
    monkeypatch.delenv("KIRO_AUTH_INSTALL_SHAPE", raising=False)
    # Ephemeral port, never one of the shared Cognito-allowlisted ports.
    monkeypatch.setattr(portal, "bind_allowed_port", _bind_ephemeral)


def _bind_ephemeral() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


async def _get(port: int, path: str, query: dict[str, str]) -> tuple[int, str]:
    """GET the listener without following redirects; (status, Location or body)."""
    url = f"http://127.0.0.1:{port}{path}?{urllib.parse.urlencode(query)}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, allow_redirects=False) as resp:
            if resp.status == 302:
                return resp.status, resp.headers["Location"]
            return resp.status, await resp.text()


def _portal_leg(state: str, **extra: str) -> dict[str, str]:
    leg = {
        "login_option": "external_idp",
        "state": state,
        "issuer_url": _ISSUER,
        "client_id": "org-client",
        "scopes": "openid,profile",
    }
    leg.update(extra)
    return leg


async def _poll_until_terminal(service, login_id):
    async def _terminal():
        result = await service.poll_device(login_id)
        return result if result["status"] != "pending" else None

    return await async_wait_until(
        _terminal, describe=lambda: "login never reached a terminal state"
    )


def _happy_session() -> _FakeSession:
    return _FakeSession(
        gets=[_FakeResp(200, dict(_DISCOVERY))],
        posts=[
            _FakeResp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 1800}),
            _FakeResp(200, {"profiles": [{"arn": _PROFILE_ARN, "profileName": "org"}]}),
        ],
    )


# ---- portal leg parsing --------------------------------------------------------


def test_handoff_from_callback_normalises_comma_scopes():
    handoff = external_idp.handoff_from_callback(_portal_leg("s", audience="api://kiro"))
    assert handoff.issuer_url == _ISSUER
    assert handoff.client_id == "org-client"
    assert handoff.scopes == "openid profile"
    assert handoff.audience == "api://kiro"
    assert handoff.login_hint is None


@pytest.mark.parametrize(
    "issuer",
    ["http://idp.example.com", "https://user@idp.example.com", "https://idp.example.com/#x", ""],
)
def test_handoff_refuses_a_non_https_or_credentialed_issuer(issuer):
    with pytest.raises(ExternalIdpAuthError):
        external_idp.handoff_from_callback(_portal_leg("s", issuer_url=issuer))


def test_require_https_url_names_the_field():
    with pytest.raises(ExternalIdpAuthError, match="start URL"):
        external_idp.require_https_url("http://x.example", "start URL")


def test_handoff_refuses_an_issuer_with_a_query():
    with pytest.raises(ExternalIdpAuthError):
        external_idp.handoff_from_callback(_portal_leg("s", issuer_url=_ISSUER + "/?t=1"))


def test_handoff_requires_a_client_id():
    with pytest.raises(ExternalIdpAuthError):
        external_idp.handoff_from_callback(_portal_leg("s", client_id=""))


def test_handoff_defaults_scopes_to_openid():
    assert external_idp.handoff_from_callback(_portal_leg("s", scopes="")).scopes == "openid"


# ---- discovery -----------------------------------------------------------------


def _handoff() -> PortalIdpHandoff:
    return PortalIdpHandoff(issuer_url=_ISSUER, client_id="cid", scopes="openid")


@pytest.mark.asyncio
async def test_discover_reads_the_issuers_own_endpoints():
    session = _FakeSession(gets=[_FakeResp(200, dict(_DISCOVERY))])
    meta = await external_idp.discover(_handoff(), session=session)
    assert session.get_calls == [_ISSUER + "/.well-known/openid-configuration"]
    assert meta.authorization_endpoint == _DISCOVERY["authorization_endpoint"]
    assert meta.token_endpoint == _DISCOVERY["token_endpoint"]


@pytest.mark.asyncio
async def test_discover_accepts_the_root_slash_equivalence():
    doc = dict(_DISCOVERY, issuer=_ISSUER + "/")
    meta = await external_idp.discover(_handoff(), session=_FakeSession(gets=[_FakeResp(200, doc)]))
    assert meta.issuer_url == _ISSUER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc",
    [
        dict(_DISCOVERY, issuer="https://other.example.com"),
        dict(_DISCOVERY, issuer=_ISSUER + "/tenant"),
        dict(_DISCOVERY, token_endpoint="http://idp.example.com/token"),
        dict(_DISCOVERY, authorization_endpoint=None),
        ["not", "an", "object"],
    ],
)
async def test_discover_refuses_a_document_that_does_not_match(doc):
    with pytest.raises(ExternalIdpAuthError):
        await external_idp.discover(_handoff(), session=_FakeSession(gets=[_FakeResp(200, doc)]))


@pytest.mark.asyncio
async def test_discover_refuses_a_non_200():
    with pytest.raises(ExternalIdpAuthError):
        await external_idp.discover(_handoff(), session=_FakeSession(gets=[_FakeResp(404, "x")]))


# ---- exchange keeps what the refresh needs --------------------------------------


def _meta(**kw) -> external_idp.ExternalIdpMetadata:
    fields = dict(
        issuer_url=_ISSUER,
        client_id="cid",
        scopes="openid",
        authorization_endpoint=_DISCOVERY["authorization_endpoint"],
        token_endpoint=_DISCOVERY["token_endpoint"],
    )
    fields.update(kw)
    return external_idp.ExternalIdpMetadata(**fields)


def test_authorization_url_keeps_the_endpoints_own_query():
    meta = _meta(authorization_endpoint="https://idp.example.com/authorize?p=b2c_signin")
    url = external_idp.build_authorization_url(
        meta, redirect_uri="http://localhost:1/oauth/callback", state="ST", code_challenge="CH"
    )
    q = _query(url)
    assert url.count("?") == 1
    assert q["p"] == "b2c_signin"
    assert q["response_type"] == "code"
    assert q["state"] == "ST"


def test_authorization_url_without_a_query_starts_one():
    url = external_idp.build_authorization_url(
        _meta(), redirect_uri="http://localhost:1/x", state="ST", code_challenge="CH"
    )
    assert url.startswith(_DISCOVERY["authorization_endpoint"] + "?response_type=code")


@pytest.mark.asyncio
async def test_exchange_code_stores_the_granted_scope():
    session = _FakeSession(
        posts=[_FakeResp(200, {"access_token": "at", "scope": "openid  offline_access"})]
    )
    token = await external_idp.exchange_code(
        _meta(scopes="openid profile"),
        code="c",
        code_verifier="v",
        redirect_uri="http://x",
        session=session,
    )
    assert token.scopes == "openid offline_access"


@pytest.mark.asyncio
async def test_exchange_code_keeps_requested_scopes_and_audience():
    meta = external_idp.ExternalIdpMetadata(
        issuer_url=_ISSUER,
        client_id="cid",
        scopes="openid",
        authorization_endpoint=_DISCOVERY["authorization_endpoint"],
        token_endpoint=_DISCOVERY["token_endpoint"],
        audience="api://kiro",
    )
    session = _FakeSession(posts=[_FakeResp(200, {"access_token": "at", "expires_in": 60})])
    token = await external_idp.exchange_code(
        meta, code="c", code_verifier="v", redirect_uri="http://localhost:1/x", session=session
    )
    assert token.scopes == "openid offline_access"
    assert token.audience == "api://kiro"
    url, form, _ = session.post_calls[0]
    assert url == _DISCOVERY["token_endpoint"]
    assert form["code_verifier"] == "v"
    assert "client_secret" not in form


@pytest.mark.asyncio
async def test_exchange_code_refuses_a_non_object_body():
    meta = external_idp.ExternalIdpMetadata(
        issuer_url=_ISSUER,
        client_id="cid",
        scopes="openid",
        authorization_endpoint=_DISCOVERY["authorization_endpoint"],
        token_endpoint=_DISCOVERY["token_endpoint"],
    )
    session = _FakeSession(posts=[_FakeResp(200, ["x"])])
    with pytest.raises(ExternalIdpAuthError):
        await external_idp.exchange_code(
            meta, code="c", code_verifier="v", redirect_uri="http://x", session=session
        )


# ---- refresh repeats scope and audience -----------------------------------------


@pytest.mark.asyncio
async def test_external_idp_refresh_sends_scope_and_audience():
    token = KasToken(
        access_token="old",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        provider="ExternalIdp",
        identity="external_idp",
        refresh_token="rt",
        profile_arn=_PROFILE_ARN,
        auth_method="external_idp",
        client_id="cid",
        token_endpoint=_DISCOVERY["token_endpoint"],
        scopes="openid offline_access",
        audience="api://kiro",
    )
    session = _FakeSession(posts=[_FakeResp(200, {"access_token": "new", "expires_in": 60})])
    fresh = await refresh._refresh(token, session=session)
    _, form, _ = session.post_calls[0]
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": "rt",
        "client_id": "cid",
        "scope": "openid offline_access",
        "audience": "api://kiro",
    }
    assert fresh.access_token == "new"
    assert (fresh.scopes, fresh.audience) == ("openid offline_access", "api://kiro")


@pytest.mark.asyncio
async def test_external_idp_refresh_keeps_a_narrowed_grant():
    token = KasToken(
        access_token="old",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        provider="ExternalIdp",
        identity="external_idp",
        refresh_token="rt",
        auth_method="external_idp",
        client_id="cid",
        token_endpoint=_DISCOVERY["token_endpoint"],
        scopes="openid profile offline_access",
    )
    session = _FakeSession(
        posts=[_FakeResp(200, {"access_token": "new", "scope": "openid offline_access"})]
    )
    fresh = await refresh._refresh(token, session=session)
    assert fresh.scopes == "openid offline_access"


def test_token_round_trips_the_new_fields_and_old_entries_still_load():
    token = KasToken(
        access_token="at",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider="ExternalIdp",
        identity="external_idp",
        scopes="openid",
        audience="aud",
    )
    again = KasToken.from_json(token.to_json())
    assert (again.scopes, again.audience) == ("openid", "aud")
    legacy = (
        '{"access_token": "at", "expires_at": "2099-01-01T00:00:00+00:00",'
        ' "provider": "ExternalIdp", "identity": "external_idp"}'
    )
    assert KasToken.from_json(legacy).scopes is None


# ---- portal URL ------------------------------------------------------------------


def test_build_auth_url_adds_only_the_given_hints():
    url = portal.build_auth_url(
        3128,
        "ST",
        "CH",
        signin_methods="external_idp",
        external_idp={
            "external_idp_domain": "example.com",
            "external_idp_start_url": "",
            "external_idp_region": "eu-central-1",
            "unrelated": "x",
        },
    )
    q = _query(url)
    assert q["signin_methods"] == "external_idp"
    assert q["external_idp_domain"] == "example.com"
    assert q["external_idp_region"] == "eu-central-1"
    assert "external_idp_start_url" not in q
    assert "unrelated" not in q


def test_build_auth_url_without_hints_is_unchanged():
    assert "signin_methods" not in portal.build_auth_url(3128, "ST", "CH")


# ---- service: begin -------------------------------------------------------------


def _service(tmp_path, session):
    return KasLoginService(TokenStore(tmp_path), session=session)


@pytest.mark.asyncio
async def test_begin_external_idp_refused_off_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_AUTH_TRANSPORT", Transport.DEVICE.value)
    with pytest.raises(LoopbackUnavailableError):
        await _service(tmp_path, _FakeSession()).begin_loopback("external_idp")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"external_idp_domain": "not a domain"}, InvalidExternalIdpInputError),
        ({"external_idp_domain": "example.com/evil"}, InvalidExternalIdpInputError),
        ({"external_idp_start_url": "http://kiro.dev/start/K-1"}, InvalidExternalIdpInputError),
        ({"external_idp_start_url": "https://a@kiro.dev/start"}, InvalidExternalIdpInputError),
        ({"region": "evil.com/"}, InvalidRegionError),
    ],
)
async def test_begin_external_idp_refuses_malformed_hints(tmp_path, loopback_shape, kwargs, error):
    with pytest.raises(error):
        await _service(tmp_path, _FakeSession()).begin_loopback("external_idp", **kwargs)


@pytest.mark.asyncio
async def test_begin_external_idp_sends_the_hints_to_the_portal(tmp_path, loopback_shape):
    service = _service(tmp_path, _FakeSession())
    begin = await service.begin_loopback(
        "external_idp",
        external_idp_domain="Someone@Example.COM",
        external_idp_start_url="https://kiro.dev/start/K-ABC",
        region="eu-central-1",
    )
    try:
        q = _query(begin["auth_url"])
        assert q["signin_methods"] == "external_idp"
        assert q["external_idp_domain"] == "example.com"
        assert q["external_idp_start_url"] == "https://kiro.dev/start/K-ABC"
        assert q["external_idp_region"] == "eu-central-1"
        assert q["redirect_uri"] == f"http://localhost:{begin['port']}"
        assert await service.poll_device(begin["login_id"]) == {"status": "pending"}
    finally:
        await service.cancel(begin["login_id"])


# ---- service: the two legs -------------------------------------------------------


@pytest.mark.asyncio
async def test_external_idp_two_leg_sign_in_saves_the_token(tmp_path, loopback_shape):
    session = _happy_session()
    service = _service(tmp_path, session)
    begin = await service.begin_loopback("external_idp", region="us-east-1")
    port = begin["port"]
    state = _query(begin["auth_url"])["state"]

    status, location = await _get(port, "/signin/callback", _portal_leg(state))
    assert status == 302
    assert location.startswith(_DISCOVERY["authorization_endpoint"] + "?")
    idp = _query(location)
    assert idp["client_id"] == "org-client"
    assert idp["redirect_uri"] == f"http://localhost:{port}/oauth/callback"
    assert idp["code_challenge_method"] == "S256"
    assert "offline_access" in idp["scope"].split()
    # A fresh state for the IdP leg, never the portal's.
    assert idp["state"] != state

    status, _ = await _get(port, "/oauth/callback", {"code": "the-code", "state": idp["state"]})
    assert status == 200
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result == {"status": "authorized", "provider": "ExternalIdp"}

    token_url, form, _ = session.post_calls[0]
    assert token_url == _DISCOVERY["token_endpoint"]
    assert form["code"] == "the-code"
    assert form["redirect_uri"] == f"http://localhost:{port}/oauth/callback"
    _, _, cps_headers = session.post_calls[1]
    assert cps_headers["TokenType"] == "EXTERNAL_IDP"

    stored = TokenStore(tmp_path).load("external_idp")
    assert stored is not None
    assert stored.auth_method == "external_idp"
    assert stored.profile_arn == _PROFILE_ARN
    assert stored.token_endpoint == _DISCOVERY["token_endpoint"]
    assert stored.region == "us-east-1"


@pytest.mark.asyncio
async def test_external_idp_code_before_the_hand_off_fails(tmp_path, loopback_shape):
    service = _service(tmp_path, _happy_session())
    begin = await service.begin_loopback("external_idp")
    state = _query(begin["auth_url"])["state"]
    await _get(begin["port"], "/oauth/callback", {"code": "c", "state": state})
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result["status"] == "error"
    assert result["code"] == "loopback_failed"
    assert TokenStore(tmp_path).load("external_idp") is None


@pytest.mark.asyncio
async def test_external_idp_portal_state_is_refused_on_the_idp_leg(tmp_path, loopback_shape):
    service = _service(tmp_path, _happy_session())
    begin = await service.begin_loopback("external_idp")
    port = begin["port"]
    state = _query(begin["auth_url"])["state"]
    status, _ = await _get(port, "/signin/callback", _portal_leg(state))
    assert status == 302
    await _get(port, "/oauth/callback", {"code": "c", "state": state})
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result["status"] == "error"
    assert TokenStore(tmp_path).load("external_idp") is None


@pytest.mark.asyncio
async def test_external_idp_discovery_mismatch_fails_without_redirecting(tmp_path, loopback_shape):
    session = _FakeSession(gets=[_FakeResp(200, dict(_DISCOVERY, issuer="https://evil.test"))])
    service = _service(tmp_path, session)
    begin = await service.begin_loopback("external_idp")
    state = _query(begin["auth_url"])["state"]
    status, body = await _get(begin["port"], "/signin/callback", _portal_leg(state))
    assert status == 200
    assert "Login failed" in body
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result["status"] == "error"
    assert session.post_calls == []


@pytest.mark.asyncio
async def test_external_idp_without_profiles_is_not_saved(tmp_path, loopback_shape):
    session = _FakeSession(
        gets=[_FakeResp(200, dict(_DISCOVERY))],
        posts=[
            _FakeResp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 1800}),
            _FakeResp(200, {"profiles": []}),
        ],
    )
    service = _service(tmp_path, session)
    begin = await service.begin_loopback("external_idp")
    port = begin["port"]
    state = _query(begin["auth_url"])["state"]
    _, location = await _get(port, "/signin/callback", _portal_leg(state))
    await _get(port, "/oauth/callback", {"code": "c", "state": _query(location)["state"]})
    result = await _poll_until_terminal(service, begin["login_id"])
    assert result["status"] == "error"
    assert TokenStore(tmp_path).load("external_idp") is None


# ---- handler -----------------------------------------------------------------------


class _StubService(KasLoginService):
    def __init__(self, raises=None):
        self.calls: list[tuple[str, dict]] = []
        self._raises = raises

    async def begin_loopback(self, provider_str, **kwargs):  # type: ignore[override]
        self.calls.append((provider_str, kwargs))
        if self._raises is not None:
            raise self._raises
        return {"login_id": "l", "auth_url": "u", "port": 1}


async def _client(service) -> TestClient:
    app = web.Application()
    app["kas_login_service"] = service
    app.router.add_post("/api/kas-login/loopback", handlers.api_kas_login_begin_loopback)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _r: True)

    # Audits must not write to the real SEL store from unit tests.
    class _NullSel:
        def log_api_access(self, **kwargs):
            pass

    monkeypatch.setattr(handlers, "sel", lambda: _NullSel())


@pytest.mark.asyncio
async def test_handler_threads_the_organization_hints(owner):
    service = _StubService()
    client = await _client(service)
    try:
        resp = await client.post(
            "/api/kas-login/loopback",
            json={
                "provider": "external_idp",
                "external_idp_domain": "example.com",
                "external_idp_start_url": "https://kiro.dev/start/K-1",
                "region": "us-east-1",
            },
        )
        assert resp.status == 200
    finally:
        await client.close()
    assert service.calls == [
        (
            "external_idp",
            {
                "replaces": "",
                "external_idp_domain": "example.com",
                "external_idp_start_url": "https://kiro.dev/start/K-1",
                "region": "us-east-1",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raises, code",
    [
        (InvalidExternalIdpInputError("x"), "invalid_external_idp"),
        (InvalidRegionError("x"), "invalid_region"),
    ],
)
async def test_handler_codes_malformed_hints(owner, raises, code):
    client = await _client(_StubService(raises=raises))
    try:
        resp = await client.post("/api/kas-login/loopback", json={"provider": "external_idp"})
        assert resp.status == 400
        assert (await resp.json())["code"] == code
    finally:
        await client.close()
