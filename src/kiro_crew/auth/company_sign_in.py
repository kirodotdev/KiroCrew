"""The start URL and region the company (IAM Identity Center) sign-in form starts with.

Most people in one company sign in through the same access portal, so the form
should not make each of them look its URL up. The answer comes from, in order:

1. the saved ``agent.kas_sso_start_url`` / ``agent.kas_sso_region`` setting
   (Settings -> Advanced), so a person's own choice always wins;
2. the edition's ``DashboardContributor.company_sign_in_defaults()``; the public
   Default supplies nothing;
3. nothing: an empty pair, the form as it was before this existed.

The pair travels together: a saved URL never borrows the edition's region. Each
value is checked the way a submitted one is, so a value that sign-in would refuse
fills nothing. The form stays editable, and what this browser last submitted
still wins over all three (``website/src/utils/companySignIn.ts``).
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.auth.service import _REGION_RE, InvalidStartUrlError, _canonical_start_url
from kiro_crew.platform import safe_context_call
from kiro_crew.platform.context import installed_context

logger = logging.getLogger(__name__)

_EMPTY = {"start_url": "", "region": ""}


def _checked(start_url: Any, region: Any) -> dict[str, str] | None:
    """The pair in canonical form, or ``None`` when the URL is not usable."""
    if not isinstance(start_url, str) or not start_url.strip():
        return None
    try:
        url = _canonical_start_url(start_url)
    except InvalidStartUrlError:
        logger.debug("company sign-in default %r is not a usable start URL", start_url)
        return None
    # The region goes into the SSO-OIDC hostname, so only the region grammar passes.
    region = region.strip() if isinstance(region, str) else ""
    if region and not _REGION_RE.match(region):
        region = ""
    return {"start_url": url, "region": region}


def company_sign_in_default(agent: Any) -> dict[str, str]:
    """``{"start_url", "region"}`` for the sign-in form; both ``""`` when none applies."""
    saved = _checked(getattr(agent, "kas_sso_start_url", ""), getattr(agent, "kas_sso_region", ""))
    if saved is not None:
        return saved
    ctx = installed_context()
    if ctx is None:
        return dict(_EMPTY)
    edition = safe_context_call(
        lambda: ctx.dashboard.company_sign_in_defaults(),
        fallback=None,
        log_message="dashboard.company_sign_in_defaults failed; no default start URL",
    )
    if isinstance(edition, dict):
        hit = _checked(edition.get("start_url"), edition.get("region"))
        if hit is not None:
            return hit
    return dict(_EMPTY)
