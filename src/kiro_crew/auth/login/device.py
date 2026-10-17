"""Social login via the device-code flow (Google / GitHub).

This transport needs no loopback callback port, so it works on every install shape —
desktop, container, and especially the remote/headless case where the browser is on
the user's laptop and the gateway is elsewhere. It is the flow kiro-cli takes when
``is_remote()``.

Wire contract (kiro-cli auth/social.rs), validated end-to-end with a real Google login:

  POST {service}/oauth/device/authorization  {clientId, loginProvider}
    -> {deviceCode, userCode, verificationUri, verificationUriComplete,
        expiresInMilliseconds, intervalInMilliseconds}
  (user approves verificationUriComplete in any browser)
  POST {service}/oauth/device/poll  {deviceCode, clientId}
    -> {status, accessToken?, refreshToken?, identityProvider?, expiresIn?, profileArn?}
       status in {authorization_pending, expired_token, invalid_token, authorized}
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from kiro_crew.auth.login.endpoints import (
    DEFAULT_TOKEN_TTL_SECS,
    USER_AGENT,
    social_service_url,
)
from kiro_crew.auth.store import KasToken, SocialProvider

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json", "User-Agent": USER_AGENT}


class DeviceAuthError(Exception):
    """Device-code authorization or polling failed."""


@dataclass
class DeviceAuthorization:
    """Codes returned by the authorization step for the user to approve."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_at: datetime
    interval_secs: float


async def initiate_device_authorization(
    provider: SocialProvider,
    *,
    session: aiohttp.ClientSession,
) -> DeviceAuthorization:
    """Step 1 — request a device code the user will approve in a browser."""
    url = f"{social_service_url()}/oauth/device/authorization"
    payload = {"clientId": USER_AGENT, "loginProvider": provider.value}
    async with session.post(url, json=payload, headers=_HEADERS) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise DeviceAuthError(f"device authorization failed: HTTP {resp.status} {body}")
        data = await _json_or_error(resp)

    # A malformed 200 (missing/foreign fields) must surface as DeviceAuthError, not
    # an uncaught KeyError/ValueError/TypeError bubbling out as an HTTP 500.
    if not isinstance(data, dict):
        raise DeviceAuthError("device authorization returned a non-object body")
    try:
        expires_ms = int(data.get("expiresInMilliseconds", 300_000))
        interval_ms = int(data.get("intervalInMilliseconds", 5_000))
        return DeviceAuthorization(
            device_code=_require_str(data, "deviceCode"),
            user_code=_require_str(data, "userCode"),
            verification_uri=_require_str(data, "verificationUri"),
            verification_uri_complete=_require_str(data, "verificationUriComplete"),
            expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=expires_ms),
            interval_secs=interval_ms / 1000.0,
        )
    except (KeyError, ValueError, TypeError, OverflowError) as err:
        raise DeviceAuthError(f"malformed device authorization response: {err}") from err


async def poll_device_token(
    auth: DeviceAuthorization,
    provider: SocialProvider,
    *,
    session: aiohttp.ClientSession,
) -> KasToken:
    """Step 2 — poll until the user approves, then return the resolved token.

    Raises DeviceAuthError on expiry, an invalid code, or a missing profile ARN.
    """
    url = f"{social_service_url()}/oauth/device/poll"
    payload = {"deviceCode": auth.device_code, "clientId": USER_AGENT}

    while datetime.now(timezone.utc) < auth.expires_at:
        async with session.post(url, json=payload, headers=_HEADERS) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("device poll HTTP %s: %s", resp.status, body)
                await asyncio.sleep(auth.interval_secs)
                continue
            data = await _json_or_error(resp)
        if not isinstance(data, dict):
            raise DeviceAuthError("device poll returned a non-object body")

        status = data.get("status", "")
        if status == "authorization_pending":
            await asyncio.sleep(auth.interval_secs)
            continue
        if status == "expired_token":
            raise DeviceAuthError("device code expired before approval")
        if status == "invalid_token":
            raise DeviceAuthError("invalid device code")
        if status == "authorized":
            return _token_from_poll(data, provider)
        # Unknown status — treat as transient and keep polling.
        logger.debug("unexpected device poll status: %r", status)
        await asyncio.sleep(auth.interval_secs)

    raise DeviceAuthError("timed out waiting for device approval")


def _require_str(data: dict, key: str) -> str:
    """Return ``data[key]`` as a non-empty string, or raise DeviceAuthError.

    Closes the whole malformed-credential-field class in one place: a missing,
    empty, or NON-STRING value (a number/object where a token belongs) must
    surface as a coded upstream failure — never be persisted as an
    "authenticated" credential the KAS bridge then fails on.
    """
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DeviceAuthError(f"auth service response has a missing or malformed {key}")
    return value


def _token_from_poll(data: dict, provider: SocialProvider) -> KasToken:
    access_token = _require_str(data, "accessToken")
    refresh_token = _require_str(data, "refreshToken")
    profile_arn = _require_str(data, "profileArn")

    # identityProvider echoes 'google'/'github'/etc; fall back to the requested one.
    resolved = _resolve_provider(data.get("identityProvider"), provider)
    try:
        expires_in = int(data.get("expiresIn") or DEFAULT_TOKEN_TTL_SECS)
        # timedelta itself can overflow on a value int() accepts (> ~8.6e13 s),
        # so the arithmetic stays inside the guard, mirroring initiate.
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    except (ValueError, TypeError, OverflowError) as err:
        raise DeviceAuthError(f"malformed expiresIn in device poll: {err}") from err

    return KasToken(
        access_token=access_token,
        expires_at=expires_at,
        provider=resolved.value,  # 'Google' | 'Github' — the governance classification
        identity="social",
        refresh_token=refresh_token,
        profile_arn=profile_arn,
    )


async def _json_or_error(resp: aiohttp.ClientResponse) -> object:
    """Decode a JSON body, mapping malformed content to DeviceAuthError.

    A 200 with a non-JSON or wrongly-typed body must surface as DeviceAuthError
    (a coded upstream failure), not an uncaught JSONDecodeError/ContentTypeError
    bubbling out of the handler as an uncoded HTTP 500.
    """
    try:
        return await resp.json()
    except (ValueError, aiohttp.ContentTypeError) as err:
        raise DeviceAuthError(f"auth service returned malformed JSON: {err}") from err


def _resolve_provider(wire: object, requested: SocialProvider) -> SocialProvider:
    if not isinstance(wire, str) or not wire:
        # A missing OR non-string identityProvider (a malformed 200 can carry a
        # number/object here) falls back to the requested provider rather than
        # crashing the poll with an AttributeError -> HTTP 500.
        return requested
    normalized = wire.strip().lower()
    for member in SocialProvider:
        if member.value.lower() == normalized:
            return member
    return requested
