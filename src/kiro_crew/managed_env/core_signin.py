"""The lane's credential: Crew's own sign-in on the lane's start URL, never another one.

The operator names one IAM Identity Center start URL (``instances.managed_env_start_url``)
and its profile ARN. The platform seam's ``idc_sign_in_profiles()`` registers that start
URL's form and pins the profile (:func:`.settings.sign_in_profile`), so Crew's own sign-in
under Settings > Kiro account is the lane's one sign-in.

:func:`vault_credential` is the token from Crew's vault, refreshed if due, and only when
the sign-in was made on the lane's start URL. Any other sign-in is refused, so another
account's token never reaches the lane's service.
"""

from __future__ import annotations

import hashlib
import logging

from kiro_crew.managed_env import settings
from kiro_crew.managed_env.client import Credential

logger = logging.getLogger(__name__)

NOT_SIGNED_IN = (
    "Sign in again under Settings > Kiro account with your managed-environment start URL."
)


class NotLaneSignIn(LookupError):
    """Crew's vault holds no usable sign-in on the lane's start URL."""


def token_is_lane_user(token: object) -> bool:
    """Whether a stored token is an Identity Center sign-in on the lane's start URL."""
    from kiro_crew.auth.whoami import START_URL_EXTRA_KEY

    if getattr(token, "identity", None) != "identity_center":
        return False
    extra = getattr(token, "extra", None)
    url = extra.get(START_URL_EXTRA_KEY) if isinstance(extra, dict) else None
    return settings.read_settings().is_lane_start_url(url)


def vault_credential() -> Credential:
    """The lane user's credential from Crew's vault, or raise :class:`NotLaneSignIn`.

    The identity is judged on the token the refresh returned, so a sign-in that changes
    during the read is never used. ``sign_in`` names that sign-in without carrying a
    secret: a new sign-in ends the client's backoff. Blocking.
    """
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated, kas_profile_arn
    from kiro_crew.auth.refresh import RefreshRejected
    from kiro_crew.lts_sign_in import run_coroutine

    provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
    try:
        token = run_coroutine(provider.current())
    except NotAuthenticated:
        raise NotLaneSignIn(NOT_SIGNED_IN) from None
    except RefreshRejected:
        logger.info("the stored sign-in can't be renewed; a new sign-in is needed")
        raise NotLaneSignIn(NOT_SIGNED_IN) from None
    except Exception as exc:  # noqa: BLE001 - a refresh error can quote response bytes
        logger.info("the stored sign-in could not be renewed (%s)", type(exc).__name__)
        raise NotLaneSignIn(NOT_SIGNED_IN) from None
    if not token_is_lane_user(token) or not token.access_token:
        raise NotLaneSignIn(NOT_SIGNED_IN)
    seed = token.refresh_token or token.client_id or token.access_token
    sign_in = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return Credential(token.access_token, sign_in, kas_profile_arn(token) or "")
