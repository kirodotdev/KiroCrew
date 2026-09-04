"""Tests for KasLoginService — the status/begin/poll/logout orchestration.

Network is faked at two seams: begin monkeypatches the device module's
initiate step, and poll drives the service's single-shot POST through a
scripted fake aiohttp session, so no test touches the real auth service.
"""

from __future__ import annotations

import asyncio
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

    async def json(self, *, content_type: str | None = "application/json"):
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


async def test_cancel_during_an_authorized_device_poll_never_persists(tmp_path, monkeypatch):
    """A cancel that lands while the approving network call is in flight wins.

    The poll learns 'authorized' only after the round-trip; by then the user may
    have clicked 'start over'. The persist step re-checks registration under the
    pending lock, so the abandoned credential is never written and the poll ends
    as an unknown login rather than a silent sign-in.
    """
    release = asyncio.Event()

    class _SlowAuthorized(_FakeResp):
        async def json(self, *, content_type: str | None = "application/json"):
            await release.wait()
            return self._payload

    payload = {
        "status": "authorized",
        "accessToken": "at-1",
        "refreshToken": "rt-1",
        "profileArn": "arn:aws:profile/x",
        "identityProvider": "google",
        "expiresIn": 3600,
    }
    service, _ = _service(tmp_path, [_SlowAuthorized(200, payload)])
    login_id = (await _begin(service, monkeypatch, _device_auth()))["login_id"]
    poll = asyncio.create_task(service.poll_device(login_id))
    await asyncio.sleep(0.05)  # the poll is now blocked inside the network call
    await service.cancel(login_id)
    release.set()
    with pytest.raises(UnknownLoginError):
        await poll
    assert TokenStore(tmp_path).resolve() is None


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
    # The code tells the dashboard not to retry on another transport — the store
    # itself is the failure, so every flavor would hit it.
    assert await service.poll_device(login_id) == {"status": "error", "code": "token_store_failed"}
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


# ---------------------------------------------------------------------------
# SSO-OIDC flavors: Builder ID and IAM Identity Center (IdC).
# begin is faked at the builder_id module seam; poll drives the single-shot
# token POST (and, for IdC, the control-plane profile POST) through the same
# scripted fake session as the social tests.
# ---------------------------------------------------------------------------

from kiro_crew.auth.login import builder_id  # noqa: E402
from kiro_crew.auth.service import MissingStartUrlError  # noqa: E402


def _oidc_auth(expires_in_secs: float = 300) -> builder_id.DeviceAuthorization:
    return builder_id.DeviceAuthorization(
        device_code="oidc-dc-1",
        user_code="WXYZ-1234",
        verification_uri="https://device.sso.us-east-1.amazonaws.com/",
        verification_uri_complete="https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_secs),
        interval_secs=0.0,
    )


async def _begin_oidc(service, monkeypatch, provider: str, **kwargs) -> dict:
    async def _fake_register(region, *, session):
        return builder_id.RegisteredClient(client_id="cid-1", client_secret="csec-1")

    async def _fake_start(client, *, region, start_url, session):
        _fake_start.seen = {"region": region, "start_url": start_url}  # type: ignore[attr-defined]
        return _oidc_auth()

    monkeypatch.setattr(builder_id, "register_client", _fake_register)
    monkeypatch.setattr(builder_id, "start_device_authorization", _fake_start)
    result = await service.begin_device(provider, **kwargs)
    result["_seen"] = getattr(_fake_start, "seen", {})
    return result


async def test_begin_builder_id_uses_default_start_url(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    result = await _begin_oidc(service, monkeypatch, "builder_id")
    assert result["user_code"] == "WXYZ-1234"
    assert result["_seen"]["start_url"] == "https://view.awsapps.com/start"
    assert result["_seen"]["region"] == "us-east-1"


async def test_begin_idc_requires_start_url(tmp_path):
    service, _ = _service(tmp_path)
    with pytest.raises(MissingStartUrlError):
        await service.begin_device("idc")
    with pytest.raises(MissingStartUrlError):
        await service.begin_device("idc", start_url="   ")


async def test_begin_idc_uses_company_start_url_and_region(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    result = await _begin_oidc(
        service, monkeypatch, "idc", start_url="https://acme.awsapps.com/start", region="eu-west-1"
    )
    assert result["_seen"] == {
        "start_url": "https://acme.awsapps.com/start",
        "region": "eu-west-1",
    }


async def test_poll_builder_id_pending_then_authorized(tmp_path, monkeypatch):
    service, session = _service(
        tmp_path,
        responses=[
            _FakeResp(400, {"error": "authorization_pending"}),
            _FakeResp(200, {"accessToken": "at-1", "refreshToken": "rt-1", "expiresIn": 3600}),
        ],
    )
    begin = await _begin_oidc(service, monkeypatch, "builder_id")
    assert await service.poll_device(begin["login_id"]) == {"status": "pending"}
    result = await service.poll_device(begin["login_id"])
    assert result == {"status": "authorized", "provider": "BuilderId"}
    saved = TokenStore(tmp_path).resolve()
    assert saved is not None
    assert saved.identity == "builder_id"
    assert saved.profile_arn is None
    # The registered client rides along for refresh.
    assert saved.client_id == "cid-1"


async def test_poll_idc_resolves_profile_arn(tmp_path, monkeypatch):
    service, session = _service(
        tmp_path,
        responses=[
            _FakeResp(200, {"accessToken": "at-2", "refreshToken": "rt-2", "expiresIn": 3600}),
            _FakeResp(
                200,
                {"profiles": [{"arn": "arn:aws:kiro:us-east-1:1:profile/p1", "profileName": "P1"}]},
            ),
        ],
    )
    begin = await _begin_oidc(
        service, monkeypatch, "idc", start_url="https://acme.awsapps.com/start"
    )
    result = await service.poll_device(begin["login_id"])
    assert result == {"status": "authorized", "provider": "Enterprise"}
    saved = TokenStore(tmp_path).resolve()
    assert saved is not None
    assert saved.identity == "identity_center"
    assert saved.profile_arn == "arn:aws:kiro:us-east-1:1:profile/p1"
    # The control-plane call carried the fresh bearer token.
    cp_url, cp_body = session.calls[-1]
    assert "kirocontrolplanebearerservice" in cp_url
    assert cp_body == {"maxResults": 10}


async def test_poll_idc_with_no_profiles_is_error_and_saves_nothing(tmp_path, monkeypatch):
    service, _ = _service(
        tmp_path,
        responses=[
            _FakeResp(200, {"accessToken": "at-3", "expiresIn": 3600}),
            _FakeResp(200, {"profiles": []}),
        ],
    )
    begin = await _begin_oidc(
        service, monkeypatch, "idc", start_url="https://acme.awsapps.com/start"
    )
    assert await service.poll_device(begin["login_id"]) == {"status": "error"}
    assert TokenStore(tmp_path).resolve() is None
    # Terminal: the entry is dropped, a re-poll is unknown.
    with pytest.raises(UnknownLoginError):
        await service.poll_device(begin["login_id"])


async def test_poll_idc_control_plane_failure_is_error(tmp_path, monkeypatch):
    service, _ = _service(
        tmp_path,
        responses=[
            _FakeResp(200, {"accessToken": "at-4", "expiresIn": 3600}),
            _FakeResp(403, {"message": "forbidden"}),
        ],
    )
    begin = await _begin_oidc(
        service, monkeypatch, "idc", start_url="https://acme.awsapps.com/start"
    )
    assert await service.poll_device(begin["login_id"]) == {"status": "error"}
    assert TokenStore(tmp_path).resolve() is None


async def test_poll_oidc_expired_token_reports_expired(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, responses=[_FakeResp(400, {"error": "expired_token"})])
    begin = await _begin_oidc(service, monkeypatch, "builder_id")
    assert await service.poll_device(begin["login_id"]) == {"status": "expired"}
    with pytest.raises(UnknownLoginError):
        await service.poll_device(begin["login_id"])


async def test_poll_oidc_multi_profile_picks_first(tmp_path, monkeypatch):
    service, _ = _service(
        tmp_path,
        responses=[
            _FakeResp(200, {"accessToken": "at-5", "expiresIn": 3600}),
            _FakeResp(
                200,
                {
                    "profiles": [
                        {"arn": "arn:one", "profileName": "One"},
                        {"arn": "arn:two", "profileName": "Two"},
                    ]
                },
            ),
        ],
    )
    begin = await _begin_oidc(
        service, monkeypatch, "idc", start_url="https://acme.awsapps.com/start"
    )
    assert await service.poll_device(begin["login_id"]) == {
        "status": "authorized",
        "provider": "Enterprise",
    }
    saved = TokenStore(tmp_path).resolve()
    assert saved is not None and saved.profile_arn == "arn:one"


async def test_begin_oidc_rejects_url_metacharacter_regions(tmp_path):
    """A crafted region must never reach hostname interpolation.

    ``oidc_url`` builds ``https://oidc.<region>.amazonaws.com`` by string
    interpolation, so a region like ``evil.com/`` or ``x@attacker/`` would
    redirect client registration and token exchange to an attacker host.
    """
    service, _ = _service(tmp_path)
    from kiro_crew.auth.service import InvalidRegionError

    for bad in (
        "evil.com/",
        "us-east-1.evil.com",
        "us-east-1/",
        "x@attacker",
        "us-east-1:443",
        "US-EAST-1",
        "useast1",
        "us-east-",
    ):
        with pytest.raises(InvalidRegionError):
            await service.begin_device("builder_id", region=bad)
        with pytest.raises(InvalidRegionError):
            await service.begin_device("idc", start_url="https://a.awsapps.com/start", region=bad)


async def test_begin_oidc_accepts_real_region_grammar(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    for good in ("us-east-1", "eu-west-1", "us-gov-west-1", "ap-southeast-3"):
        result = await _begin_oidc(
            service, monkeypatch, "idc", start_url="https://a.awsapps.com/start", region=good
        )
        assert result["_seen"]["region"] == good


async def test_control_plane_tolerates_amz_json_content_type(tmp_path, monkeypatch):
    """The control plane replies application/x-amz-json-1.0, not application/json.

    aiohttp's default ``resp.json()`` gate raises ContentTypeError for that, which
    would turn EVERY IdC profile resolution into {"status": "error"}. Pin that the
    reader passes content_type=None by faking aiohttp's strict behavior.
    """

    class _AmzResp(_FakeResp):
        async def json(self, *, content_type: str | None = "application/json"):
            if content_type is not None:
                import aiohttp

                raise aiohttp.ContentTypeError(request_info=None, history=())
            return self._payload

    service, session = _service(tmp_path)
    session._responses = [
        _FakeResp(200, {"accessToken": "at-ct", "expiresIn": 3600}),
        _AmzResp(200, {"profiles": [{"arn": "arn:ct", "profileName": "CT"}]}),
    ]
    begin = await _begin_oidc(
        service, monkeypatch, "idc", start_url="https://acme.awsapps.com/start"
    )
    result = await service.poll_device(begin["login_id"])
    assert result == {"status": "authorized", "provider": "Enterprise"}
    saved = TokenStore(tmp_path).resolve()
    assert saved is not None and saved.profile_arn == "arn:ct"


async def test_poll_oidc_undecodable_body_is_pending_not_500(tmp_path, monkeypatch):
    """A malformed token-endpoint body (proxy/LB outage page) must read as pending.

    The social poll already treats the identical condition as a transient hiccup;
    an unguarded ``resp.json()`` here would instead propagate ValueError out of
    the API handler as an uncoded 500.
    """

    class _BrokenResp(_FakeResp):
        async def json(self, *, content_type: str | None = "application/json"):
            raise ValueError("unexpected mimetype / undecodable body")

    service, session = _service(tmp_path)
    session._responses = [
        _BrokenResp(200, None),
        _FakeResp(200, {"accessToken": "at-r", "refreshToken": "rt-r", "expiresIn": 3600}),
    ]
    begin = await _begin_oidc(service, monkeypatch, "builder_id")
    # Transient: pending, entry retained; the next poll succeeds normally.
    assert await service.poll_device(begin["login_id"]) == {"status": "pending"}
    assert await service.poll_device(begin["login_id"]) == {
        "status": "authorized",
        "provider": "BuilderId",
    }


async def test_oidc_malformed_200_bodies_raise_flow_error_not_crash():
    """A 200 with an unexpected JSON shape must raise BuilderIdAuthError.

    Bare KeyError/AttributeError/ValueError would escape the API handler (which
    catches only the flow's own error types) as an uncoded HTTP 500.
    """
    import aiohttp as _aiohttp  # noqa: F401  (parity with prod import path)

    cases = [
        ("register non-object", _FakeResp(200, ["not", "a", "dict"]), "register"),
        ("register missing clientId", _FakeResp(200, {"clientSecret": "s"}), "register"),
        ("device-auth non-object", _FakeResp(200, 42), "device"),
        ("device-auth missing deviceCode", _FakeResp(200, {"userCode": "U"}), "device"),
        (
            "device-auth non-numeric expiresIn",
            _FakeResp(
                200,
                {
                    "deviceCode": "d",
                    "userCode": "u",
                    "verificationUri": "v",
                    "verificationUriComplete": "vc",
                    "expiresIn": "soon",
                },
            ),
            "device",
        ),
    ]
    for label, resp, step in cases:
        session = _FakeSession([resp])
        with pytest.raises(builder_id.BuilderIdAuthError):
            if step == "register":
                await builder_id.register_client("us-east-1", session=session)
            else:
                client = builder_id.RegisteredClient(client_id="c", client_secret="s")
                await builder_id.start_device_authorization(
                    client, region="us-east-1", session=session
                )


async def test_oidc_poll_non_object_200_is_terminal_error(tmp_path, monkeypatch):
    """A decodable-but-non-object CreateToken 200 ends the login as a coded error."""
    service, session = _service(tmp_path)
    session._responses = [_FakeResp(200, ["weird"])]
    begin = await _begin_oidc(service, monkeypatch, "builder_id")
    assert await service.poll_device(begin["login_id"]) == {"status": "error"}
    assert TokenStore(tmp_path).resolve() is None
