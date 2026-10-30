"""Kiro control-plane bearer API — the one call IdC login needs.

An IAM Identity Center sign-in yields an SSO-OIDC access token, but KAS routes
enterprise traffic by ``profile ARN`` (the ``X-Kiro-Profile-Arn`` header), which the
token itself does not carry. kiro-cli resolves it by calling the Kiro control-plane
service's (CPS) ``ListAvailableProfiles`` with the fresh token; we do the same.
Contract mirrored from kiro-cli's ``list_available_profiles`` (its bearer client
pointed at ``Endpoint::cps_for_region``, AWS JSON 1.0 protocol, bearer auth):

  POST https://management.<region>.kiro.dev/
    Content-Type: application/x-amz-json-1.0
    X-Amz-Target: AmazonCodeWhispererService.ListAvailableProfiles
    Authorization: Bearer <accessToken>
    body: {"maxResults": ...}
  -> {"profiles": [{"arn": ..., "profileName": ...}, ...], "nextToken": ...}

The host is the CPS deployment name under ``kiro.dev``, NOT the service id of the
vendored client (``KiroControlPlaneBearerService``): that id is only the Smithy
service name and has no DNS record, so deriving a hostname from it makes every IdC
poll die in the resolver right after the device code has been redeemed. The CPS
answers an invalid bearer with HTTP 400 ``AccessDeniedException`` rather than a
connection error, which is how a reachable-but-unauthorized call is told apart from
a wrong host. Every failure path here surfaces as a coded error rather than a
stored-but-unusable credential.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

from kiro_crew.auth.login.endpoints import USER_AGENT

logger = logging.getLogger(__name__)

# One page is plenty: the common enterprise case is a single profile, and callers
# that see several pick the first (multi-profile selection is a documented follow-up).
_MAX_RESULTS = 10
_TARGET = "AmazonCodeWhispererService.ListAvailableProfiles"

# Control-plane (CPS) endpoints per region, mirroring kiro-cli's
# ``Endpoint::cps_for_region``. Regions without a dedicated CPS fall back to
# us-east-1, which is also what kiro-cli's ``DEFAULT_ENDPOINT`` resolves to.
_CPS_ENDPOINTS = {
    "us-east-1": "https://management.us-east-1.kiro.dev/",
    "eu-central-1": "https://management.eu-central-1.kiro.dev/",
}
_DEFAULT_CPS_REGION = "us-east-1"

# A Kiro profile ARN; group 1 is the region.
_PROFILE_ARN_RE = re.compile(
    r"arn:[-a-z0-9]{1,63}:codewhisperer:([-a-z0-9]{1,63}):\d{12}:profile/[A-Za-z0-9]{1,64}"
)


class ControlPlaneError(Exception):
    """Profile resolution against the Kiro control plane failed."""


@dataclass
class KiroProfile:
    arn: str
    profile_name: str


def control_plane_url(region: str) -> str:
    """CPS base URL for ``region``, falling back to us-east-1 like kiro-cli does."""
    return _CPS_ENDPOINTS.get(region) or _CPS_ENDPOINTS[_DEFAULT_CPS_REGION]


def profile_region(profile_arn: str) -> str:
    """The region of a Kiro profile ARN (``arn:<partition>:codewhisperer:<region>:...``).

    Raises ``ControlPlaneError`` when *profile_arn* is not one. The answer only picks
    an entry of :data:`_CPS_ENDPOINTS` through :func:`control_plane_url`, so an
    unknown region still lands on a host from that table.
    """
    match = _PROFILE_ARN_RE.fullmatch(profile_arn or "")
    if match is None:
        raise ControlPlaneError("the signed-in profile is not a Kiro profile ARN")
    return match.group(1)


def cps_headers(target: str, access_token: str, *, token_type: str = "") -> dict[str, str]:
    """The headers of one AWS JSON 1.0 bearer call to the control plane.

    *token_type* is ``EXTERNAL_IDP`` for an External IdP token (kiro-cli sends the
    ``TokenType`` header on those so CPS picks the right authenticator), else empty.
    """
    headers = {
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": target,
        "Authorization": f"Bearer {access_token}",
        "User-Agent": USER_AGENT,
    }
    if token_type:
        headers["TokenType"] = token_type
    return headers


async def list_available_profiles(
    access_token: str, *, region: str, session: aiohttp.ClientSession
) -> list[KiroProfile]:
    """Return the caller's Kiro profiles, first page only.

    Raises ControlPlaneError on any non-200 or malformed body: an IdC login without
    a resolvable profile ARN is unusable, so failures must be loud, not stored.
    """
    headers = cps_headers(_TARGET, access_token)
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
