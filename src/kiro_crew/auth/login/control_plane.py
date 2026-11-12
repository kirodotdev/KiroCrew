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
    body: {"maxResults": ..., "nextToken": <from the previous page, if any>}
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

# Page size per request. The listing follows ``nextToken`` until a page lists a
# profile: the service may answer a page with no profiles and a token, and stopping
# there would report zero profiles for an account that has one.
_MAX_RESULTS = 10
# Bound on pages per listing, so a service that keeps handing out fresh tokens
# cannot hold a sign-in open. 20 pages of 10 is far beyond any real account.
_MAX_PAGES = 20
_TARGET = "AmazonCodeWhispererService.ListAvailableProfiles"

# Control-plane (CPS) endpoints per region, mirroring kiro-cli's
# ``Endpoint::cps_for_region``. Regions without a dedicated CPS fall back to
# us-east-1, which is also what kiro-cli's ``DEFAULT_ENDPOINT`` resolves to.
# eu-west-1 has no CPS host of its own (the name does not resolve); its
# profiles are served by the Frankfurt control plane, which is where kiro-cli
# sends EU regions. Sign-in asks every host here, the login region's first.
_CPS_ENDPOINTS = {
    "us-east-1": "https://management.us-east-1.kiro.dev/",
    "eu-central-1": "https://management.eu-central-1.kiro.dev/",
    "eu-west-1": "https://management.eu-central-1.kiro.dev/",
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


@dataclass
class ProfileListing:
    """Every profile one listing returned, plus where and how it was read.

    ``url`` and ``pages`` exist for diagnostics: an empty listing is reported with
    the host that answered and how many pages it took to say so.
    """

    profiles: list[KiroProfile]
    url: str
    pages: int


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


async def _profiles_page(
    url: str, body: dict[str, object], headers: dict[str, str], session: aiohttp.ClientSession
) -> tuple[list[KiroProfile], str | None]:
    """One ``ListAvailableProfiles`` page: its profiles and the next page's token.

    The token is ``None`` when the listing is complete. Raises ControlPlaneError on
    a non-200, an undecodable or malformed body, or a ``nextToken`` that is not a
    string.
    """
    async with session.post(url, json=body, headers=headers) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise ControlPlaneError(
                f"ListAvailableProfiles failed: HTTP {resp.status} {text[:500]}"
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
    next_token = data.get("nextToken")
    if next_token is not None and not isinstance(next_token, str):
        raise ControlPlaneError("ListAvailableProfiles response has a non-string 'nextToken'")
    profiles: list[KiroProfile] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        arn = entry.get("arn")
        if isinstance(arn, str) and arn:
            profiles.append(KiroProfile(arn=arn, profile_name=str(entry.get("profileName") or "")))
    return profiles, next_token or None


async def list_available_profiles(
    access_token: str, *, region: str, session: aiohttp.ClientSession
) -> ProfileListing:
    """Return the caller's Kiro profiles from the first control plane that has any.

    Every CPS host is asked, *region*'s own first, because the Identity Center region
    does not name the control plane serving the profile (a us-east-1 instance can
    hold an eu-central-1 profile). Each host's listing follows ``nextToken`` until a
    page lists a profile (:func:`_list_profiles_at`). A host that refuses, answers
    malformed or reaches neither a profile nor its last page is skipped;
    ControlPlaneError is raised when a host refused and none had profiles, since the
    refusing host may hold the profile and an empty list would wrongly say there is
    none. A transport error is not caught here. When
    every host answered and listed none, the empty result names those hosts and the
    pages they took, for the diagnostic log.
    """
    urls = dict.fromkeys([control_plane_url(region), *_CPS_ENDPOINTS.values()])
    errors: list[ControlPlaneError] = []
    empty: list[ProfileListing] = []
    for url in urls:
        try:
            listing = await _list_profiles_at(url, access_token, session)
        except ControlPlaneError as err:
            logger.warning("ListAvailableProfiles at %s failed: %s", url, err)
            errors.append(err)
            continue
        if listing.profiles:
            return listing
        empty.append(listing)
    if errors:
        raise errors[0]
    return ProfileListing(
        profiles=[],
        url=", ".join(listing.url for listing in empty),
        pages=sum(listing.pages for listing in empty),
    )


async def _list_profiles_at(
    url: str, access_token: str, session: aiohttp.ClientSession
) -> ProfileListing:
    """The profiles of the first page of CPS host *url* that lists any.

    Follows ``nextToken`` past empty pages and stops at the first page with a
    profile, since sign-in uses only the first. Before that page, raises
    ControlPlaneError on a non-200 or malformed page, on a token the service already
    handed out (a loop), and after :data:`_MAX_PAGES` pages.
    """
    headers = cps_headers(_TARGET, access_token)
    seen_tokens: set[str] = set()
    next_token: str | None = None
    for page in range(1, _MAX_PAGES + 1):
        body: dict[str, object] = {"maxResults": _MAX_RESULTS}
        if next_token is not None:
            body["nextToken"] = next_token
        profiles, next_token = await _profiles_page(url, body, headers, session)
        if profiles or next_token is None:
            return ProfileListing(profiles=profiles, url=url, pages=page)
        if next_token in seen_tokens:
            raise ControlPlaneError(
                f"ListAvailableProfiles repeated a page token after {page} page(s)"
            )
        seen_tokens.add(next_token)
    raise ControlPlaneError(f"ListAvailableProfiles did not finish within {_MAX_PAGES} pages")
