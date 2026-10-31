"""A small client for Tailscale's device-invite API.

Covers only what in-Crew invites need, against ``https://api.tailscale.com/api/v2``:

* ``GET /tailnet/-/devices`` to find this machine's device id;
* ``GET /tailnet/-/keys/{keyId}`` to read the key's expiry;
* ``POST /device/{deviceId}/device-invites`` (a JSON array of
  ``{email?, multiUse, allowExitNode}``), ``GET`` of the same path, and
  ``DELETE /device-invites/{id}`` plus ``POST .../resend``.

The credential is a user-owned API access key. Tailscale refuses device-invite
creation with an OAuth-client token, because a share belongs to a user. The key
is sent as HTTP basic auth (key as username, empty password) and only ever to
:data:`API_BASE`: redirects are refused, so a redirect can never carry the key
to another host. Nothing here logs the key or a response body.

Every failure raises :class:`TailscaleApiError` with a short ``code``; a 429
carries the server's ``Retry-After`` so the caller can back off.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from kiro_crew.dashboard.handlers._shared import read_capped_response

API_BASE = "https://api.tailscale.com/api/v2"
REQUEST_TIMEOUT_SECS = 15.0
#: Largest response body read, so a misbehaving server cannot exhaust memory.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: ``tskey-api-<keyId>-<secret>``: the id names the key in ``/keys/{keyId}``.
_API_KEY_RE = re.compile(r"\Atskey-api-([A-Za-z0-9]+)-[A-Za-z0-9]+\Z")
_SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")

CODE_AUTH = "auth_failed"
CODE_FORBIDDEN = "forbidden"
CODE_NOT_FOUND = "not_found"
CODE_RATE_LIMITED = "rate_limited"
CODE_UNAVAILABLE = "unavailable"
CODE_BAD_RESPONSE = "bad_response"
CODE_BAD_REQUEST = "bad_request"


class TailscaleApiError(Exception):
    """A failed call. ``code`` is one of the ``CODE_*`` values above."""

    def __init__(self, code: str, status: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def api_key_id(key: str) -> str:
    """The key id inside an API access key, or ``""`` when *key* is not one."""
    match = _API_KEY_RE.match(key.strip())
    return match.group(1) if match else ""


def basic_auth(key: str) -> str:
    """The ``Authorization`` value for *key*: basic auth, key as user, no password."""
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode("ascii")


def looks_like_api_key(key: str) -> bool:
    return bool(api_key_id(key))


@dataclass(frozen=True)
class AcceptedBy:
    """The user who accepted an invite, as Tailscale reports them."""

    user_id: str
    login: str
    profile_pic_url: str = ""


@dataclass(frozen=True)
class DeviceInvite:
    id: str
    device_id: str
    email: str
    invite_url: str
    multi_use: bool
    accepted: bool
    accepted_by: Optional[AcceptedBy] = None
    created: str = ""
    last_email_sent_at: str = ""


@dataclass(frozen=True)
class Device:
    node_id: str
    legacy_id: str
    node_key: str
    addresses: tuple[str, ...] = field(default_factory=tuple)
    hostname: str = ""


def _str(raw: object) -> str:
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, (str, int)):
        return str(raw)
    return ""


def parse_invite(raw: object) -> DeviceInvite:
    if not isinstance(raw, dict):
        raise TailscaleApiError(CODE_BAD_RESPONSE)
    invite_id = _str(raw.get("id"))
    if not _SAFE_ID_RE.match(invite_id):
        raise TailscaleApiError(CODE_BAD_RESPONSE)
    accepted_by: Optional[AcceptedBy] = None
    by = raw.get("acceptedBy")
    if isinstance(by, dict) and _str(by.get("id")) and _str(by.get("loginName")):
        accepted_by = AcceptedBy(
            user_id=_str(by.get("id")),
            login=_str(by.get("loginName")).strip(),
            profile_pic_url=_str(by.get("profilePicUrl")),
        )
    return DeviceInvite(
        id=invite_id,
        device_id=_str(raw.get("deviceId")),
        email=_str(raw.get("email")).strip(),
        invite_url=_str(raw.get("inviteUrl")),
        multi_use=raw.get("multiUse") is True,
        accepted=raw.get("accepted") is True,
        accepted_by=accepted_by,
        created=_str(raw.get("created")),
        last_email_sent_at=_str(raw.get("lastEmailSentAt")),
    )


def _parse_device(raw: object) -> Optional[Device]:
    if not isinstance(raw, dict):
        return None
    node_id = _str(raw.get("nodeId")) or _str(raw.get("id"))
    if not node_id:
        return None
    addresses = raw.get("addresses")
    return Device(
        node_id=node_id,
        legacy_id=_str(raw.get("id")),
        node_key=_str(raw.get("nodeKey")),
        addresses=(
            tuple(a for a in addresses if isinstance(a, str)) if isinstance(addresses, list) else ()
        ),
        hostname=_str(raw.get("hostname")),
    )


def _retry_after(raw: str | None) -> float:
    try:
        value = float(raw or 0)
    except ValueError:
        return 0.0
    return max(0.0, min(value, 3600.0))


SessionFactory = Callable[[], aiohttp.ClientSession]
Sleep = Callable[[float], Awaitable[None]]


class TailscaleApi:
    """One API key, one base URL. Tests pass the fake server's URL as *base_url*."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str = API_BASE,
        session_factory: Optional[SessionFactory] = None,
    ) -> None:
        self._key = key
        self._base = base_url.rstrip("/")
        self._session_factory = session_factory

    def __repr__(self) -> str:
        return f"TailscaleApi(base={self._base!r}, key=****)"

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        url = self._base + path
        session = (
            self._session_factory()
            if self._session_factory is not None
            else aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS))
        )
        try:
            async with session:
                async with session.request(
                    method,
                    url,
                    json=body,
                    allow_redirects=False,
                    headers={"Accept": "application/json", "Authorization": basic_auth(self._key)},
                ) as resp:
                    raw = await read_capped_response(resp, MAX_RESPONSE_BYTES)
                    status = resp.status
                    retry_after = _retry_after(resp.headers.get("Retry-After"))
        except TailscaleApiError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise TailscaleApiError(CODE_UNAVAILABLE) from exc
        if 300 <= status < 400:
            raise TailscaleApiError(CODE_BAD_RESPONSE, status)
        if status == 401:
            raise TailscaleApiError(CODE_AUTH, status)
        if status == 403:
            raise TailscaleApiError(CODE_FORBIDDEN, status)
        if status == 404:
            raise TailscaleApiError(CODE_NOT_FOUND, status)
        if status == 429:
            raise TailscaleApiError(CODE_RATE_LIMITED, status, retry_after or 60.0)
        if 400 <= status < 500:
            raise TailscaleApiError(CODE_BAD_REQUEST, status)
        if status >= 500:
            raise TailscaleApiError(CODE_UNAVAILABLE, status, retry_after)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TailscaleApiError(CODE_BAD_RESPONSE, status)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise TailscaleApiError(CODE_BAD_RESPONSE, status) from exc

    @staticmethod
    def _id(value: str) -> str:
        if not _SAFE_ID_RE.match(value or ""):
            raise TailscaleApiError(CODE_BAD_REQUEST)
        return value

    async def list_devices(self) -> list[Device]:
        body = await self._call("GET", "/tailnet/-/devices")
        rows = body.get("devices") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise TailscaleApiError(CODE_BAD_RESPONSE)
        return [d for d in (_parse_device(r) for r in rows) if d is not None]

    async def key_expiry(self, key_id: str) -> str:
        """The key's ``expires`` timestamp, ``""`` when the server gives none."""
        body = await self._call("GET", f"/tailnet/-/keys/{self._id(key_id)}")
        return _str(body.get("expires")) if isinstance(body, dict) else ""

    async def create_invites(
        self, device_id: str, emails: list[str], *, multi_use: bool = False
    ) -> list[DeviceInvite]:
        """One invite per email, or one link invite when *emails* is empty.

        ``allowExitNode`` is always false: a teammate reaches the Crew, never
        this machine as an exit node.
        """
        entries: list[dict[str, Any]] = (
            [{"email": e, "multiUse": False, "allowExitNode": False} for e in emails]
            if emails
            else [{"multiUse": bool(multi_use), "allowExitNode": False}]
        )
        body = await self._call("POST", f"/device/{self._id(device_id)}/device-invites", entries)
        if not isinstance(body, list):
            raise TailscaleApiError(CODE_BAD_RESPONSE)
        return [parse_invite(r) for r in body]

    async def list_invites(self, device_id: str) -> list[DeviceInvite]:
        body = await self._call("GET", f"/device/{self._id(device_id)}/device-invites")
        if not isinstance(body, list):
            raise TailscaleApiError(CODE_BAD_RESPONSE)
        return [parse_invite(r) for r in body]

    async def delete_invite(self, invite_id: str) -> None:
        await self._call("DELETE", f"/device-invites/{self._id(invite_id)}")

    async def resend_invite(self, invite_id: str) -> None:
        await self._call("POST", f"/device-invites/{self._id(invite_id)}/resend")


__all__ = [
    "API_BASE",
    "AcceptedBy",
    "Device",
    "DeviceInvite",
    "TailscaleApi",
    "TailscaleApiError",
    "api_key_id",
    "looks_like_api_key",
    "parse_invite",
]
