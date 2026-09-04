"""AWS Builder ID / IAM Identity Center login via SSO-OIDC (device-code).

Fully independent of any Kiro-controlled gate: we register our own public OAuth client
at runtime (``RegisterClient``) and run the standard AWS SSO-OIDC device-code flow, so
this works for Builder ID and for any customer IAM Identity Center ``start_url``. We hit
the SSO-OIDC REST endpoints directly over aiohttp (rather than the boto3 SDK) to keep the
auth module on one HTTP client and testable with the same fake session as the social
flow.

Endpoints (all under ``https://oidc.<region>.amazonaws.com``):
  POST /client/register          -> {clientId, clientSecret, clientSecretExpiresAt}
  POST /device_authorization     -> {deviceCode, userCode, verificationUri,
                                     verificationUriComplete, expiresIn, interval}
  POST /token (grant device_code) -> {accessToken, refreshToken, expiresIn, ...}

RegisterClient returns a client_id + client_secret; both are persisted with the token so
refresh (a separate CreateToken call) can reuse them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from kiro_crew.auth.login.endpoints import (
    BUILDER_ID_START_URL,
    OIDC_CLIENT_NAME,
    OIDC_SCOPES,
    USER_AGENT,
    oidc_url,
)
from kiro_crew.auth.store import KasToken

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/x-amz-json-1.1", "User-Agent": USER_AGENT}
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class BuilderIdAuthError(Exception):
    """Builder ID / IdC SSO-OIDC login failed."""


async def _parse_json(resp: aiohttp.ClientResponse, step: str) -> dict:
    """Decode a 200 body into a dict, or raise BuilderIdAuthError.

    Every parsing failure — undecodable body, non-object JSON — must surface as
    the flow's own error type: anything else escapes the API handler as an
    uncoded HTTP 500.
    """
    try:
        data = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError) as err:
        raise BuilderIdAuthError(f"{step} returned an undecodable body") from err
    if not isinstance(data, dict):
        raise BuilderIdAuthError(f"{step} returned a non-object body")
    return data


def _require_str(data: dict, key: str, step: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BuilderIdAuthError(f"{step} response is missing {key!r}")
    return value


@dataclass
class RegisteredClient:
    client_id: str
    client_secret: str


@dataclass
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_at: datetime
    interval_secs: float


async def register_client(region: str, *, session: aiohttp.ClientSession) -> RegisteredClient:
    """Dynamic client registration — no Kiro-controlled gate, any app may do this."""
    url = f"{oidc_url(region)}/client/register"
    payload = {
        "clientName": OIDC_CLIENT_NAME,
        "clientType": "public",
        "scopes": list(OIDC_SCOPES),
    }
    async with session.post(url, json=payload, headers=_HEADERS) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            raise BuilderIdAuthError(f"RegisterClient failed: HTTP {resp.status} {body}")
        data = await _parse_json(resp, "RegisterClient")
    return RegisteredClient(
        client_id=_require_str(data, "clientId", "RegisterClient"),
        client_secret=_require_str(data, "clientSecret", "RegisterClient"),
    )


async def start_device_authorization(
    client: RegisteredClient,
    *,
    region: str,
    start_url: str = BUILDER_ID_START_URL,
    session: aiohttp.ClientSession,
) -> DeviceAuthorization:
    url = f"{oidc_url(region)}/device_authorization"
    payload = {
        "clientId": client.client_id,
        "clientSecret": client.client_secret,
        "startUrl": start_url,
    }
    async with session.post(url, json=payload, headers=_HEADERS) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise BuilderIdAuthError(f"StartDeviceAuthorization failed: HTTP {resp.status} {body}")
        data = await _parse_json(resp, "StartDeviceAuthorization")
    try:
        expires_in = int(data.get("expiresIn", 600))
        interval = float(data.get("interval", 5))
    except (TypeError, ValueError) as err:
        raise BuilderIdAuthError("StartDeviceAuthorization returned non-numeric timing") from err
    return DeviceAuthorization(
        device_code=_require_str(data, "deviceCode", "StartDeviceAuthorization"),
        user_code=_require_str(data, "userCode", "StartDeviceAuthorization"),
        verification_uri=_require_str(data, "verificationUri", "StartDeviceAuthorization"),
        verification_uri_complete=_require_str(
            data, "verificationUriComplete", "StartDeviceAuthorization"
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        interval_secs=interval,
    )


async def poll_token_once(
    client: RegisteredClient,
    auth: DeviceAuthorization,
    *,
    region: str,
    identity: str = "builder_id",
    provider: str = "BuilderId",
    session: aiohttp.ClientSession,
) -> KasToken | None:
    """One non-blocking poll of the token endpoint.

    Returns the token when the user has approved, ``None`` while approval is still
    pending (``authorization_pending`` / ``slow_down`` — the caller owns the cadence,
    so slow_down is just "not yet" here), and raises BuilderIdAuthError for
    ``expired_token`` or any other terminal rejection.
    """
    url = f"{oidc_url(region)}/token"
    payload = {
        "clientId": client.client_id,
        "clientSecret": client.client_secret,
        "grantType": _DEVICE_GRANT,
        "deviceCode": auth.device_code,
    }
    async with session.post(url, json=payload, headers=_HEADERS) as resp:
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            # Malformed/empty body (e.g. a proxy or LB error page during an
            # outage): "not yet", mirroring the social poll's guard — the
            # flow's own expiry bounds how long a caller retries.
            return None
        if resp.status == 200:
            if not isinstance(data, dict):
                raise BuilderIdAuthError("CreateToken returned a non-object body")
            return _token_from_create(data, client, region, identity, provider)
        err = data.get("error", "") if isinstance(data, dict) else ""
    if err in ("authorization_pending", "slow_down"):
        return None
    if err == "expired_token":
        raise BuilderIdAuthError("device code expired before approval")
    raise BuilderIdAuthError(f"token poll failed: {err or 'unknown error'}")


async def poll_token(
    client: RegisteredClient,
    auth: DeviceAuthorization,
    *,
    region: str,
    identity: str = "builder_id",
    provider: str = "BuilderId",
    session: aiohttp.ClientSession,
) -> KasToken:
    """Poll the token endpoint until the user approves the device code."""
    url = f"{oidc_url(region)}/token"
    payload = {
        "clientId": client.client_id,
        "clientSecret": client.client_secret,
        "grantType": _DEVICE_GRANT,
        "deviceCode": auth.device_code,
    }
    while datetime.now(timezone.utc) < auth.expires_at:
        async with session.post(url, json=payload, headers=_HEADERS) as resp:
            data = await resp.json()
            if resp.status == 200:
                return _token_from_create(data, client, region, identity, provider)
            # SSO-OIDC signals pending/slow-down via an error code with non-200.
            err = (data or {}).get("error", "")
        if err in ("authorization_pending", "slow_down"):
            await asyncio.sleep(auth.interval_secs + (5 if err == "slow_down" else 0))
            continue
        if err == "expired_token":
            raise BuilderIdAuthError("device code expired before approval")
        raise BuilderIdAuthError(f"token poll failed: {err or 'unknown error'}")

    raise BuilderIdAuthError("timed out waiting for device approval")


def _token_from_create(
    data: dict, client: RegisteredClient, region: str, identity: str, provider: str
) -> KasToken:
    access_token = data.get("accessToken")
    if not access_token:
        raise BuilderIdAuthError("CreateToken returned no access token")
    try:
        expires_in = int(data.get("expiresIn") or 3600)
    except (TypeError, ValueError) as err:
        raise BuilderIdAuthError("CreateToken returned a non-numeric expiry") from err
    return KasToken(
        access_token=access_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        provider=provider,
        identity=identity,
        refresh_token=data.get("refreshToken"),
        region=region,
        client_id=client.client_id,
        client_secret=client.client_secret,
    )
