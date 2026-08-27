"""Tests for the social device-code login flow.

Uses a fake aiohttp ClientSession that returns scripted responses, so the flow's
contract handling (authorization -> pending polls -> authorized, plus the error
statuses and missing-field guards) is exercised with no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.auth.login import device
from kiro_crew.auth.login.device import (
    DeviceAuthError,
    DeviceAuthorization,
    initiate_device_authorization,
    poll_device_token,
)
from kiro_crew.auth.store import SocialProvider

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

    def __init__(self, responses: list[_FakeResp]):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, headers=None):  # noqa: A002
        self.calls.append((url, json or {}))
        return self._responses.pop(0)


async def test_initiate_returns_codes():
    resp = _FakeResp(
        200,
        {
            "deviceCode": "dc-1",
            "userCode": "ABCD-EFGH",
            "verificationUri": "https://app.kiro.dev/account/device",
            "verificationUriComplete": "https://app.kiro.dev/account/device?user_code=ABCD-EFGH",
            "expiresInMilliseconds": 300_000,
            "intervalInMilliseconds": 5_000,
        },
    )
    session = _FakeSession([resp])
    auth = await initiate_device_authorization(SocialProvider.GOOGLE, session=session)
    assert auth.device_code == "dc-1"
    assert auth.user_code == "ABCD-EFGH"
    assert auth.interval_secs == 5.0
    # sent the CLI-identifying clientId and the wire provider name
    _, body = session.calls[0]
    assert body == {"clientId": "Kiro-CLI", "loginProvider": "Google"}


async def test_initiate_non_200_raises():
    session = _FakeSession([_FakeResp(500, "boom")])
    with pytest.raises(DeviceAuthError):
        await initiate_device_authorization(SocialProvider.GITHUB, session=session)


async def test_initiate_malformed_200_raises_device_auth_error():
    # A 200 missing required fields must raise DeviceAuthError, not a bare KeyError
    # bubbling out as an HTTP 500.
    session = _FakeSession([_FakeResp(200, {"userCode": "X"})])  # no deviceCode etc.
    with pytest.raises(DeviceAuthError, match="malformed"):
        await initiate_device_authorization(SocialProvider.GOOGLE, session=session)


async def test_initiate_non_object_200_raises_device_auth_error():
    session = _FakeSession([_FakeResp(200, ["not", "a", "dict"])])
    with pytest.raises(DeviceAuthError):
        await initiate_device_authorization(SocialProvider.GOOGLE, session=session)


def _auth(interval: float = 0.0) -> DeviceAuthorization:
    return DeviceAuthorization(
        device_code="dc-1",
        user_code="X",
        verification_uri="https://v",
        verification_uri_complete="https://v?c=X",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        interval_secs=interval,
    )


async def test_poll_pending_then_authorized_returns_token():
    session = _FakeSession(
        [
            _FakeResp(200, {"status": "authorization_pending"}),
            _FakeResp(
                200,
                {
                    "status": "authorized",
                    "accessToken": "at-x",
                    "refreshToken": "rt-x",
                    "identityProvider": "google",
                    "expiresIn": 7200,
                    "profileArn": "arn:aws:codewhisperer:us-east-1:1:profile/P",
                },
            ),
        ]
    )
    tok = await poll_device_token(_auth(), SocialProvider.GOOGLE, session=session)
    assert tok.access_token == "at-x"
    assert tok.refresh_token == "rt-x"
    assert tok.provider == "Google"
    assert tok.identity == "social"
    assert tok.profile_arn.endswith("profile/P")
    # ~2h expiry
    assert tok.expires_at > datetime.now(timezone.utc) + timedelta(seconds=7000)


async def test_poll_expired_raises():
    session = _FakeSession([_FakeResp(200, {"status": "expired_token"})])
    with pytest.raises(DeviceAuthError, match="expired"):
        await poll_device_token(_auth(), SocialProvider.GOOGLE, session=session)


async def test_poll_invalid_raises():
    session = _FakeSession([_FakeResp(200, {"status": "invalid_token"})])
    with pytest.raises(DeviceAuthError, match="invalid"):
        await poll_device_token(_auth(), SocialProvider.GOOGLE, session=session)


async def test_poll_authorized_missing_profile_arn_raises():
    session = _FakeSession(
        [
            _FakeResp(
                200,
                {"status": "authorized", "accessToken": "a", "refreshToken": "r"},
            )
        ]
    )
    with pytest.raises(DeviceAuthError, match="profileArn"):
        await poll_device_token(_auth(), SocialProvider.GOOGLE, session=session)


async def test_poll_authorized_missing_tokens_raises():
    session = _FakeSession([_FakeResp(200, {"status": "authorized", "profileArn": "arn:x"})])
    with pytest.raises(DeviceAuthError, match="accessToken"):
        await poll_device_token(_auth(), SocialProvider.GOOGLE, session=session)


async def test_poll_expires_in_defaults_when_absent():
    session = _FakeSession(
        [
            _FakeResp(
                200,
                {
                    "status": "authorized",
                    "accessToken": "a",
                    "refreshToken": "r",
                    "profileArn": "arn:x",
                },
            )
        ]
    )
    tok = await poll_device_token(_auth(), SocialProvider.GITHUB, session=session)
    # default 3600s applied; provider falls back to the requested one
    assert tok.provider == "Github"
    assert tok.expires_at > datetime.now(timezone.utc) + timedelta(seconds=3400)


async def test_poll_unknown_identity_provider_falls_back():
    session = _FakeSession(
        [
            _FakeResp(
                200,
                {
                    "status": "authorized",
                    "accessToken": "a",
                    "refreshToken": "r",
                    "identityProvider": "weird",
                    "profileArn": "arn:x",
                },
            )
        ]
    )
    tok = await poll_device_token(_auth(), SocialProvider.GOOGLE, session=session)
    assert tok.provider == "Google"  # requested provider, unknown wire value ignored


async def test_provider_resolver_case_insensitive():
    assert device._resolve_provider("GITHUB", SocialProvider.GOOGLE) is SocialProvider.GITHUB


async def test_initiate_oversized_expiry_raises_device_auth_error():
    # An absurd expiresInMilliseconds (beyond timedelta's range) must surface as
    # DeviceAuthError, not an uncaught OverflowError -> uncoded HTTP 500.
    resp = _FakeResp(
        200,
        {
            "deviceCode": "dc",
            "userCode": "UC",
            "verificationUri": "https://x",
            "verificationUriComplete": "https://x?c=UC",
            "expiresInMilliseconds": 10**300,
        },
    )
    with pytest.raises(DeviceAuthError):
        await initiate_device_authorization(SocialProvider.GOOGLE, session=_FakeSession([resp]))


def test_token_from_poll_rejects_non_string_credential_fields():
    # Object/number-valued token fields must raise, never be persisted as an
    # "authenticated" credential the KAS bridge then fails on. Covers the whole
    # field class via _require_str: accessToken, refreshToken, profileArn.
    base = {
        "accessToken": "at",
        "refreshToken": "rt",
        "profileArn": "arn:aws:profile/x",
        "expiresIn": 3600,
    }
    for field in ("accessToken", "refreshToken", "profileArn"):
        for bad in (123, {"v": "x"}, ["x"], "", None):
            data = dict(base)
            data[field] = bad
            with pytest.raises(DeviceAuthError):
                device._token_from_poll(data, SocialProvider.GOOGLE)


async def test_json_or_error_maps_decode_failure():
    # A 200 whose body is not JSON must surface as DeviceAuthError, not an
    # uncaught JSONDecodeError/ContentTypeError -> uncoded HTTP 500.
    class _BadResp:
        async def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    with pytest.raises(device.DeviceAuthError):
        await device._json_or_error(_BadResp())


async def test_provider_resolver_non_string_falls_back():
    # A malformed 200 can carry a number/object in identityProvider; that must
    # fall back to the requested provider, not crash the poll with AttributeError.
    assert device._resolve_provider(123, SocialProvider.GOOGLE) is SocialProvider.GOOGLE
    assert device._resolve_provider({"p": "github"}, SocialProvider.GOOGLE) is SocialProvider.GOOGLE
    assert device._resolve_provider(None, SocialProvider.GITHUB) is SocialProvider.GITHUB
    assert device._resolve_provider("google", SocialProvider.GITHUB) is SocialProvider.GOOGLE
    assert device._resolve_provider(None, SocialProvider.GOOGLE) is SocialProvider.GOOGLE
