"""Kiro control-plane bearer API — the one call IdC login needs.

An IAM Identity Center sign-in yields an SSO-OIDC access token, but KAS routes
enterprise traffic by ``profile ARN`` (the ``X-Kiro-Profile-Arn`` header), which the
token itself does not carry. The desktop clients resolve it by calling the Kiro
control-plane bearer service's ``ListAvailableProfiles`` with the fresh token; we do
the same. Contract mirrored from the KAS bundle's vendored
``@amzn/kiro-control-plane-bearer-client`` (AWS JSON 1.0 protocol, bearer auth):

  POST https://kirocontrolplanebearerservice.<region>.amazonaws.com/
    Content-Type: application/x-amz-json-1.0
    X-Amz-Target: KiroControlPlaneBearerService.ListAvailableProfiles
    Authorization: Bearer <accessToken>
    body: {"maxResults": ...}
  -> {"profiles": [{"arn": ..., "profileName": ...}, ...], "nextToken": ...}

NOT yet verified against a live enterprise tenant — the request/response shape is
taken from the client the Kiro desktop app itself ships, and every failure path here
surfaces as a coded error rather than a stored-but-unusable credential.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from kiro_crew.auth.login.endpoints import USER_AGENT

logger = logging.getLogger(__name__)

# One page is plenty: the common enterprise case is a single profile, and callers
# that see several pick the first (multi-profile selection is a documented follow-up).
_MAX_RESULTS = 10
_TARGET = "KiroControlPlaneBearerService.ListAvailableProfiles"


class ControlPlaneError(Exception):
    """Profile resolution against the Kiro control plane failed."""


@dataclass
class KiroProfile:
    arn: str
    profile_name: str


def control_plane_url(region: str) -> str:
    return f"https://kirocontrolplanebearerservice.{region}.amazonaws.com/"


async def list_available_profiles(
    access_token: str, *, region: str, session: aiohttp.ClientSession
) -> list[KiroProfile]:
    """Return the caller's Kiro profiles, first page only.

    Raises ControlPlaneError on any non-200 or malformed body: an IdC login without
    a resolvable profile ARN is unusable, so failures must be loud, not stored.
    """
    headers = {
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": _TARGET,
        "Authorization": f"Bearer {access_token}",
        "User-Agent": USER_AGENT,
    }
    url = control_plane_url(region)
    async with session.post(url, json={"maxResults": _MAX_RESULTS}, headers=headers) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise ControlPlaneError(
                f"ListAvailableProfiles failed: HTTP {resp.status} {body[:500]}"
            )
        try:
            # content_type=None: AWS JSON 1.0 replies carry
            # `application/x-amz-json-1.0`, which aiohttp's default
            # application/json gate would reject as ContentTypeError.
            data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as err:
            raise ControlPlaneError("ListAvailableProfiles returned an undecodable body") from err
    if not isinstance(data, dict):
        raise ControlPlaneError("ListAvailableProfiles returned a non-object body")
    raw = data.get("profiles")
    if not isinstance(raw, list):
        raise ControlPlaneError("ListAvailableProfiles response has no 'profiles' list")
    profiles: list[KiroProfile] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        arn = entry.get("arn")
        if isinstance(arn, str) and arn:
            profiles.append(KiroProfile(arn=arn, profile_name=str(entry.get("profileName") or "")))
    return profiles
