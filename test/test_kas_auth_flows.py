"""Tests for refresh routing, the KasAuthProvider contract, and login helpers."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.auth.login import builder_id
from kiro_crew.auth.login.builder_id import BuilderIdAuthError, RegisteredClient
from kiro_crew.auth.login.portal import (
    PortalAuthError,
    exchange_code,
)
from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
from kiro_crew.auth.refresh import (
    IdentitySignedOut,
    RefreshError,
    RefreshRejected,
    ensure_fresh,
)
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

    async def json(self, *, content_type: str | None = "application/json"):
        return self._payload

    async def text(self):
        return str(self._payload)


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, data=None, headers=None):  # noqa: A002
        self.calls.append((url, json if json is not None else (data or {})))
        return self._responses.pop(0)


def _fresh(identity: str, provider: str, *, ttl: int = 3600, profile="arn:x", **kw) -> KasToken:
    return KasToken(
        access_token=f"at-{identity}",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
        provider=provider,
        identity=identity,
        refresh_token=f"rt-{identity}",
        profile_arn=profile,
        **kw,
    )


# ---- PKCE / URL helpers --------------------------------------------------------
# (Synchronous helper tests live in test_kas_auth_helpers.py, which has no asyncio
#  pytestmark — mixing sync tests under this module's asyncio mark warns.)


async def test_exchange_code_success():
    session = _FakeSession(
        [
            _FakeResp(
                200,
                {
                    "accessToken": "a",
                    "refreshToken": "r",
                    "expiresIn": 3600,
                    "profileArn": "arn:x",
                },
            )
        ]
    )
    tok = await exchange_code(
        SocialProvider.GOOGLE,
        "code",
        "verifier",
        "http://localhost:3128/oauth/callback",
        session=session,
    )
    assert tok.access_token == "a"
    assert tok.provider == "Google"
    assert tok.identity == "social"


async def test_exchange_code_missing_profile_arn_raises():
    session = _FakeSession([_FakeResp(200, {"accessToken": "a", "refreshToken": "r"})])
    with pytest.raises(PortalAuthError, match="profile ARN"):
        await exchange_code(SocialProvider.GOOGLE, "c", "v", "http://x", session=session)


# ---- Builder ID SSO-OIDC -------------------------------------------------------


async def test_register_client_returns_credentials():
    session = _FakeSession([_FakeResp(200, {"clientId": "cid", "clientSecret": "csec"})])
    client = await builder_id.register_client("us-east-1", session=session)
    assert client.client_id == "cid"
    assert client.client_secret == "csec"


async def test_builder_id_poll_pending_then_token():
    client = RegisteredClient("cid", "csec")
    auth = builder_id.DeviceAuthorization(
        device_code="dc",
        user_code="U",
        verification_uri="v",
        verification_uri_complete="vc",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        interval_secs=0.0,
    )
    session = _FakeSession(
        [
            _FakeResp(400, {"error": "authorization_pending"}),
            _FakeResp(200, {"accessToken": "at", "refreshToken": "rt", "expiresIn": 3600}),
        ]
    )
    tok = await builder_id.poll_token(client, auth, region="us-east-1", session=session)
    assert tok.access_token == "at"
    assert tok.provider == "BuilderId"
    assert tok.identity == "builder_id"
    # client creds persisted for refresh
    assert tok.client_id == "cid" and tok.client_secret == "csec"


async def test_builder_id_poll_expired_raises():
    client = RegisteredClient("cid", "csec")
    auth = builder_id.DeviceAuthorization(
        device_code="dc",
        user_code="U",
        verification_uri="v",
        verification_uri_complete="vc",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        interval_secs=0.0,
    )
    session = _FakeSession([_FakeResp(400, {"error": "expired_token"})])
    with pytest.raises(BuilderIdAuthError, match="expired"):
        await builder_id.poll_token(client, auth, region="us-east-1", session=session)


def _bid_auth() -> "builder_id.DeviceAuthorization":
    return builder_id.DeviceAuthorization(
        device_code="dc",
        user_code="U",
        verification_uri="v",
        verification_uri_complete="vc",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        interval_secs=0.0,
    )


async def test_builder_id_register_client_failure_raises():
    session = _FakeSession([_FakeResp(400, "bad request")])
    with pytest.raises(BuilderIdAuthError, match="RegisterClient failed"):
        await builder_id.register_client("us-east-1", session=session)


async def test_builder_id_start_device_authorization_ok_and_failure():
    ok = _FakeResp(
        200,
        {
            "deviceCode": "dc",
            "userCode": "UUUU",
            "verificationUri": "https://v",
            "verificationUriComplete": "https://v?c=UUUU",
            "expiresIn": 600,
            "interval": 5,
        },
    )
    session = _FakeSession([ok])
    auth = await builder_id.start_device_authorization(
        RegisteredClient("cid", "csec"), region="us-east-1", session=session
    )
    assert auth.device_code == "dc"
    assert auth.interval_secs == 5.0

    bad = _FakeSession([_FakeResp(400, "nope")])
    with pytest.raises(BuilderIdAuthError, match="StartDeviceAuthorization failed"):
        await builder_id.start_device_authorization(
            RegisteredClient("cid", "csec"), region="us-east-1", session=bad
        )


async def test_builder_id_poll_slow_down_then_token():
    client = RegisteredClient("cid", "csec")
    session = _FakeSession(
        [
            _FakeResp(400, {"error": "slow_down"}),
            _FakeResp(200, {"accessToken": "at", "refreshToken": "rt", "expiresIn": 3600}),
        ]
    )
    tok = await builder_id.poll_token(client, _bid_auth(), region="us-east-1", session=session)
    assert tok.access_token == "at"


async def test_builder_id_poll_generic_error_raises():
    client = RegisteredClient("cid", "csec")
    session = _FakeSession([_FakeResp(400, {"error": "access_denied"})])
    with pytest.raises(BuilderIdAuthError, match="access_denied"):
        await builder_id.poll_token(client, _bid_auth(), region="us-east-1", session=session)


async def test_builder_id_poll_token_missing_access_token_raises():
    client = RegisteredClient("cid", "csec")
    session = _FakeSession([_FakeResp(200, {"refreshToken": "rt"})])  # no accessToken
    with pytest.raises(BuilderIdAuthError, match="no access token"):
        await builder_id.poll_token(client, _bid_auth(), region="us-east-1", session=session)


# ---- refresh -------------------------------------------------------------------


async def test_ensure_fresh_returns_unchanged_when_valid(tmp_path: Path):
    store = TokenStore(tmp_path)
    tok = _fresh("social", "Google", ttl=3600)
    session = _FakeSession([])  # must not be called
    out = await ensure_fresh(store, tok, session=session)
    assert out is tok
    assert session.calls == []


async def test_ensure_fresh_refreshes_social(tmp_path: Path):
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)  # inside refresh margin
    store.save(stale)
    session = _FakeSession(
        [
            _FakeResp(
                200,
                {
                    "accessToken": "new-at",
                    "refreshToken": "new-rt",
                    "expiresIn": 3600,
                    "profileArn": "arn:x",
                },
            )
        ]
    )
    out = await ensure_fresh(store, stale, session=session)
    assert out.access_token == "new-at"
    # persisted
    assert store.load("social").access_token == "new-at"


async def test_ensure_fresh_skips_http_if_peer_refreshed(tmp_path: Path):
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    # a peer already wrote a fresh token to the store
    store.save(_fresh("social", "Google", ttl=3600, profile="arn:peer"))
    session = _FakeSession([])  # must not be called
    out = await ensure_fresh(store, stale, session=session)
    assert out.profile_arn == "arn:peer"
    assert session.calls == []


async def test_refresh_no_refresh_token_raises(tmp_path: Path):
    store = TokenStore(tmp_path)
    tok = KasToken(
        access_token="a",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        provider="Google",
        identity="social",
        refresh_token=None,
        profile_arn="arn:x",
    )
    store.save(tok)
    with pytest.raises(RefreshError, match="no refresh token"):
        await ensure_fresh(store, tok, session=_FakeSession([]))


async def test_refresh_sso_oidc_needs_client_creds(tmp_path: Path):
    store = TokenStore(tmp_path)
    tok = KasToken(
        access_token="a",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        provider="BuilderId",
        identity="builder_id",
        refresh_token="r",
    )  # no client_id/secret
    store.save(tok)
    with pytest.raises(RefreshError, match="client credentials"):
        await ensure_fresh(store, tok, session=_FakeSession([]))


async def test_refresh_sso_oidc_success(tmp_path: Path):
    store = TokenStore(tmp_path)
    stale = KasToken(
        access_token="old",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        provider="BuilderId",
        identity="builder_id",
        refresh_token="rt",
        region="us-east-1",
        client_id="cid",
        client_secret="csec",
    )
    store.save(stale)
    session = _FakeSession([_FakeResp(200, {"accessToken": "new", "expiresIn": 3600})])
    out = await ensure_fresh(store, stale, session=session)
    assert out.access_token == "new"
    assert out.client_id == "cid"  # creds carried forward for next refresh


async def test_refresh_sso_oidc_http_error_raises(tmp_path: Path):
    store = TokenStore(tmp_path)
    stale = KasToken(
        access_token="old",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        provider="Enterprise",
        identity="identity_center",
        refresh_token="rt",
        profile_arn="arn:x",  # an IdC entry without one is dropped by store.load
        region="us-east-1",
        client_id="cid",
        client_secret="csec",
    )
    store.save(stale)
    with pytest.raises(RefreshError, match="SSO-OIDC refresh failed"):
        await ensure_fresh(store, stale, session=_FakeSession([_FakeResp(400, "bad")]))


async def test_refresh_external_idp_success(tmp_path: Path):
    store = TokenStore(tmp_path)
    stale = KasToken(
        access_token="old",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        provider="ExternalIdp",
        identity="external_idp",
        refresh_token="rt",
        auth_method="external_idp",
        client_id="cid",
        token_endpoint="https://idp.example/token",
    )
    store.save(stale)
    session = _FakeSession(
        [_FakeResp(200, {"access_token": "new", "refresh_token": "rt2", "expires_in": 1800})]
    )
    out = await ensure_fresh(store, stale, session=session)
    assert out.access_token == "new"
    assert out.auth_method == "external_idp"
    assert out.token_endpoint == "https://idp.example/token"


async def test_refresh_external_idp_needs_token_endpoint(tmp_path: Path):
    store = TokenStore(tmp_path)
    stale = KasToken(
        access_token="old",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
        provider="ExternalIdp",
        identity="external_idp",
        refresh_token="rt",
    )  # no token_endpoint
    store.save(stale)
    with pytest.raises(RefreshError, match="token endpoint"):
        await ensure_fresh(store, stale, session=_FakeSession([]))


async def test_concurrent_ensure_fresh_serializes_single_flight(tmp_path: Path):
    """Two concurrent refreshes of the same stale token must not deadlock the loop
    and must collapse to ONE HTTP refresh (the in-process lock serializes them, the
    second sees the peer's fresh token and skips the call)."""
    import asyncio

    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)

    calls = {"n": 0}

    class _CountingResp(_FakeResp):
        pass

    class _CountingSession:
        def post(self, url, *, json=None, data=None, headers=None):  # noqa: A002
            calls["n"] += 1
            return _CountingResp(
                200,
                {
                    "accessToken": "new-at",
                    "refreshToken": "new-rt",
                    "expiresIn": 3600,
                    "profileArn": "arn:x",
                },
            )

    session = _CountingSession()
    a, b = await asyncio.gather(
        ensure_fresh(store, stale, session=session),
        ensure_fresh(store, stale, session=session),
    )
    assert a.access_token == "new-at"
    assert b.access_token == "new-at"
    # single-flight: the second coroutine reused the first's result, one HTTP call
    assert calls["n"] == 1


async def test_logout_before_refresh_does_not_resurrect_the_token(tmp_path: Path):
    """A stale token handed to ensure_fresh whose identity is absent from the
    store is NOT refreshed and NOT re-persisted: the in-lock re-read finds nothing
    and the refresh stops. This is the delete-then-refresh half of the logout race."""
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)
    store.delete("social")  # logout landed first

    session = _FakeSession([])  # any HTTP call would IndexError
    with pytest.raises(IdentitySignedOut):
        await ensure_fresh(store, stale, session=session)
    assert store.load("social") is None
    assert session.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="contention timing assumes POSIX flock")
async def test_logout_during_refresh_leaves_no_token_behind(tmp_path: Path):
    """The refresh-then-delete half: a logout that arrives while a refresh holds
    the lock across its HTTP round-trip waits for the save, then deletes it, so
    the vault ends empty and the logout's success is true."""
    import asyncio

    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)

    in_http = asyncio.Event()
    release_http = asyncio.Event()

    class _SlowResp(_FakeResp):
        async def __aenter__(self):
            in_http.set()
            await release_http.wait()
            return self

    session = _FakeSession(
        [
            _SlowResp(
                200,
                {
                    "accessToken": "new-at",
                    "refreshToken": "new-rt",
                    "expiresIn": 3600,
                    "profileArn": "arn:x",
                },
            )
        ]
    )
    refresh = asyncio.ensure_future(ensure_fresh(store, stale, session=session))
    await in_http.wait()
    # Logout while the refresher holds the lock: delete must block until the
    # refresher has saved, then remove what it saved.
    logout = asyncio.ensure_future(asyncio.to_thread(store.delete, "social"))
    await asyncio.sleep(0.05)
    assert not logout.done(), "delete must wait for the in-flight refresh"
    release_http.set()
    refreshed = await refresh
    await logout
    assert refreshed.access_token == "new-at"
    assert store.load("social") is None


@pytest.mark.skipif(sys.platform == "win32", reason="contention timing assumes POSIX flock")
async def test_account_switch_during_refresh_is_not_overwritten(tmp_path: Path):
    """A sign-in that lands a different account in the slot while a refresh of the
    old one is in flight waits for the refresher's save and then replaces it, so
    the vault ends on the account the operator just chose -- never on a renewed
    copy of the one they left."""
    import asyncio

    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)

    in_http = asyncio.Event()
    release_http = asyncio.Event()

    class _SlowResp(_FakeResp):
        async def __aenter__(self):
            in_http.set()
            await release_http.wait()
            return self

    session = _FakeSession(
        [
            _SlowResp(
                200,
                {
                    "accessToken": "renewed-old",
                    "refreshToken": "rt-old-2",
                    "expiresIn": 3600,
                    "profileArn": "arn:x",
                },
            )
        ]
    )
    refresh = asyncio.ensure_future(ensure_fresh(store, stale, session=session))
    await in_http.wait()
    new_account = _fresh("social", "Github", ttl=3600, profile="arn:new")
    login = asyncio.ensure_future(asyncio.to_thread(store.save, new_account))
    await asyncio.sleep(0.05)
    assert not login.done(), "the sign-in save must wait for the in-flight refresh"
    release_http.set()
    await refresh
    await login
    kept = store.load("social")
    assert kept is not None
    assert kept.provider == "Github" and kept.profile_arn == "arn:new"


async def test_provider_maps_signed_out_during_refresh_to_not_authenticated(tmp_path: Path):
    """What the KAS callback sees: a sign-out that raced the refresh reads as
    'not signed in', the same verdict as an empty vault."""
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)
    provider = KasAuthProvider(store, session=_FakeSession([]))

    real_resolve = store.resolve

    def resolve_then_logout():
        token = real_resolve()
        store.delete("social")
        return token

    store.resolve = resolve_then_logout  # type: ignore[method-assign]
    with pytest.raises(NotAuthenticated):
        await provider.current()


# ---- KasAuthProvider -----------------------------------------------------------


async def test_provider_not_authenticated_when_empty(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    provider = KasAuthProvider(TokenStore(tmp_path), session=_FakeSession([]))
    assert provider.is_authenticated() is False
    with pytest.raises(NotAuthenticated):
        await provider.current()


async def test_provider_env_api_key_bypass(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "sk-test")
    provider = KasAuthProvider(TokenStore(tmp_path), session=_FakeSession([]))
    assert provider.is_authenticated() is True
    tok = await provider.current()
    assert tok.access_token == "sk-test"
    assert tok.provider == "ApiKey"


async def test_provider_env_api_key_bypass_can_be_disabled(tmp_path: Path, monkeypatch):
    """A vault-only consumer (the relay callback) opts out: an ambient key is
    neither an identity nor authentication, and an empty vault stays empty."""
    monkeypatch.setenv("KIRO_API_KEY", "sk-test")
    provider = KasAuthProvider(
        TokenStore(tmp_path), session=_FakeSession([]), allow_env_api_key=False
    )
    assert provider.is_authenticated() is False
    with pytest.raises(NotAuthenticated):
        await provider.current()


async def test_provider_callback_shape(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    store = TokenStore(tmp_path)
    store.save(_fresh("social", "Google", ttl=3600))
    provider = KasAuthProvider(store, session=_FakeSession([]))
    resp = await provider.get_access_token_callback()
    assert resp["accessToken"] == "at-social"
    assert resp["provider"] == "Google"
    assert resp["profileArn"] == "arn:x"
    # expiresAt is ISO-8601 and comfortably beyond the refresh margin
    exp = datetime.fromisoformat(resp["expiresAt"])
    assert exp > datetime.now(timezone.utc) + timedelta(seconds=200)
    # refresh token never leaks into the callback
    assert "refreshToken" not in resp


async def test_provider_read_token_no_refresh(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    store = TokenStore(tmp_path)
    store.save(_fresh("external_idp", "ExternalIdp", ttl=3600, auth_method="external_idp"))
    provider = KasAuthProvider(store, session=_FakeSession([]))
    peek = provider.read_token()
    assert peek == {"authMethod": "external_idp", "provider": "ExternalIdp"}


async def test_provider_resolve_request_credential(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    store = TokenStore(tmp_path)
    store.save(_fresh("social", "Google", ttl=3600))
    provider = KasAuthProvider(store, session=_FakeSession([]))
    cred = await provider.resolve_request_credential()
    assert cred == {"accessToken": "at-social", "profileArn": "arn:x", "provider": "Google"}


# ---- issuer refusal is recorded, transient failure is not ------------------------


async def test_refresh_rejected_by_issuer_is_recorded_on_the_store(tmp_path: Path):
    """A 4xx from the issuer is a REFUSAL of the grant: RefreshRejected, and the
    store carries the marker the dashboard card and doctor read. The token in
    the vault is untouched -- nothing here demotes or deletes the identity."""
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)
    assert store.refresh_rejected("social") is None
    with pytest.raises(RefreshRejected, match="social refresh failed: HTTP 401"):
        await ensure_fresh(store, stale, session=_FakeSession([_FakeResp(401, "invalid_grant")]))
    when = store.refresh_rejected("social")
    assert when is not None
    assert when.tzinfo is not None
    assert datetime.now(timezone.utc) - when < timedelta(minutes=1)
    # Still stored, still the same credential: a refusal is reported, not acted on.
    assert store.load("social").access_token == stale.access_token
    # RefreshRejected is still a RefreshError, so every existing handler keeps working.
    assert issubclass(RefreshRejected, RefreshError)


async def test_refresh_transient_failure_is_not_recorded(tmp_path: Path):
    """A 5xx means "try later", not "the grant is dead": plain RefreshError, no marker."""
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)
    with pytest.raises(RefreshError) as excinfo:
        await ensure_fresh(store, stale, session=_FakeSession([_FakeResp(503, "busy")]))
    assert not isinstance(excinfo.value, RefreshRejected)
    assert store.refresh_rejected("social") is None


async def test_successful_refresh_after_rejection_clears_the_marker(tmp_path: Path):
    """The refresher's own save goes through TokenStore.save, which clears the marker:
    a grant the issuer honours again (or a fresh sign-in) stops reading as 'expired'."""
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)
    store.mark_refresh_rejected("social")
    assert store.refresh_rejected("social") is not None
    session = _FakeSession(
        [
            _FakeResp(
                200,
                {
                    "accessToken": "new-at",
                    "refreshToken": "new-rt",
                    "expiresIn": 3600,
                    "profileArn": "arn:x",
                },
            )
        ]
    )
    out = await ensure_fresh(store, stale, session=session)
    assert out.access_token == "new-at"
    assert store.refresh_rejected("social") is None


async def test_provider_maps_rejection_to_the_same_failure_as_before(tmp_path: Path):
    """KasAuthProvider.current still raises the refresh error (the KAS callback
    turns it into the engine's sign-in prompt); the marker is a side record."""
    store = TokenStore(tmp_path)
    stale = _fresh("social", "Google", ttl=10)
    store.save(stale)
    provider = KasAuthProvider(store, session=_FakeSession([_FakeResp(400, "invalid_grant")]))
    with pytest.raises(RefreshRejected):
        await provider.current()
    assert store.refresh_rejected("social") is not None
