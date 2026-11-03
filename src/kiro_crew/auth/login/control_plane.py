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

# Control-plane (CPS) deployments, by the region of their own hostname. These
# are the hosts that serve ``ListAvailableProfiles``; there is one per Kiro
# control plane, NOT one per AWS region.
#
# The IAM Identity Center region a user signs in with (the region hint for the
# start URL) is independent of which control plane serves their Kiro profile:
# an eu-west-1 IdC instance can hold a profile served from us-east-1 OR from
# eu-central-1, and some Kiro capabilities are us-east-1-only. So the login
# region is NOT a reliable key for the CPS host. At sign-in time the profile
# ARN -- the only thing that names the profile's real region -- does not exist
# yet (resolving it is the whole point of the call), so login resolves the
# profile by DISCOVERY: it probes every known CPS host and uses the one that
# answers, exactly as the Kiro web sign-in does. See
# :func:`list_available_profiles`.
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
    """CPS base URL for ``region``, falling back to us-east-1.

    Used AFTER sign-in, keyed by :func:`profile_region` -- the region parsed from
    the resolved profile ARN, which authoritatively names the profile's control
    plane. (Login itself does not use this; it discovers the host, see
    :func:`list_available_profiles`.)
    """
    return _CPS_ENDPOINTS.get(region) or _CPS_ENDPOINTS[_DEFAULT_CPS_REGION]


def _discovery_hosts(region: str) -> list[str]:
    """CPS hosts to probe at sign-in, the region's own first when it has one.

    Ordering is a latency hint only -- discovery uses whichever host answers --
    so putting the login region's own CPS first lets the common single-region
    case resolve on the first probe. The remaining known hosts follow, de-duped,
    so a profile on another control plane is still found.
    """
    ordered = [_CPS_ENDPOINTS.get(region) or _CPS_ENDPOINTS[_DEFAULT_CPS_REGION]]
    ordered += _CPS_ENDPOINTS.values()
    hosts: list[str] = []
    for host in ordered:
        if host not in hosts:
            hosts.append(host)
    return hosts


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


async def _list_profiles_on_host(
    url: str, access_token: str, session: aiohttp.ClientSession
) -> list[KiroProfile]:
    """``ListAvailableProfiles`` against one CPS host, first page only.

    Raises ControlPlaneError on a non-200 or malformed body from that host.
    """
    headers = cps_headers(_TARGET, access_token)
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


async def list_available_profiles(
    access_token: str, *, region: str, session: aiohttp.ClientSession
) -> list[KiroProfile]:
    """Discover the caller's Kiro profiles across ALL known control planes.

    ``ListAvailableProfiles`` is a per-control-plane view: each CPS host answers
    only for the profiles IT serves, so a us-east-1 host returns the us-east-1
    profile and nothing about an eu-central-1 one, and vice versa. The signed-in
    profile's control plane is not known at sign-in time (the ARN that would name
    it is what this call resolves), and the login region does not reliably
    identify it. So this probes EVERY known CPS host (:func:`_discovery_hosts`,
    the login region's own first) and MERGES the results, de-duped by ARN with
    first-seen order preserved -- the same whole-picture the Kiro web sign-in and
    kiro-cli show. Returning the first non-empty host instead would hide a second
    profile served from another control plane, and the caller would silently sign
    in to whichever host answered first rather than letting the user choose.

    An empty list means every reachable host returned zero profiles: the identity
    is authenticated but has no entitled Kiro profile anywhere (a provisioning
    gap), which the caller surfaces as "no available Kiro profiles". A
    ControlPlaneError is raised only when EVERY host errored and none returned a
    profile -- an IdC login without a resolvable profile ARN is unusable, so a
    pure-failure outcome must be loud, not stored. A host that errors while
    another answers is logged and does not fail the resolution.
    """
    last_error: ControlPlaneError | None = None
    reachable = False
    merged: list[KiroProfile] = []
    seen: set[str] = set()
    for url in _discovery_hosts(region):
        try:
            profiles = await _list_profiles_on_host(url, access_token, session)
        except ControlPlaneError as err:
            last_error = err
            logger.info("profile discovery: %s did not answer (%s)", url, err)
            continue
        reachable = True
        logger.info("profile discovery: %d profile(s) from %s", len(profiles), url)
        for profile in profiles:
            if profile.arn not in seen:
                seen.add(profile.arn)
                merged.append(profile)
    if not merged and not reachable and last_error is not None:
        # Every host errored: nothing was reachable, so this is a hard failure
        # rather than an empty (authenticated-but-unentitled) result.
        raise last_error
    if merged:
        logger.info("profile discovery: %d profile(s) total across control planes", len(merged))
    return merged
