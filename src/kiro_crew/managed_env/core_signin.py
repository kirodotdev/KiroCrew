"""The lane on Crew's own sign-in: the token comes from Crew's vault, never kiro-cli.

The operator names one IAM Identity Center start URL (``instances.managed_env_start_url``).
The platform seam's ``idc_sign_in_profiles()`` registers that start URL's form and pins
its profile (:func:`kiro_crew.managed_env.settings.sign_in_profile`), so Crew's own
``idc`` sign-in under Settings > Kiro account is the lane's one sign-in.

:func:`vault_token` is the lane's token: the vault's sign-in, refreshed if due, and only
when it was made on the lane's start URL. Any other sign-in is refused, so another
account's token never reaches the lane's service.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.managed_env import settings

if TYPE_CHECKING:
    from kiro_crew.auth.store import KasToken, TokenStore

logger = logging.getLogger(__name__)

NOT_SIGNED_IN = "Sign in under Settings → Kiro account with your managed-environment start URL."
#: Plain lines for a sign-in the issuer won't renew (its session limit) and for one that
#: couldn't be renewed for another reason. No error class name.
SIGN_IN_ENDED = "Your sign-in reached its time limit. Sign in again under Settings → Kiro account."
SIGN_IN_NOT_RENEWED = (
    "Crew couldn't renew your sign-in right now. Sign in again under Settings → Kiro account."
)
SIGNED_IN = "Signed in to Kiro."


class NotLaneSignIn(LookupError):
    """Crew's vault holds no usable sign-in on the lane's start URL."""


def token_is_lane_user(token: object) -> bool:
    """Whether a stored token's sign-in recorded the lane's start URL."""
    from kiro_crew.auth.whoami import START_URL_EXTRA_KEY

    extra = getattr(token, "extra", None)
    url = extra.get(START_URL_EXTRA_KEY) if isinstance(extra, dict) else None
    return settings.read_settings().is_lane_start_url(url)


def _renewal_refused(store: TokenStore, token: KasToken) -> bool:
    """Whether the issuer refused *token*'s renewal since it was stored.

    ``is_usable`` stays true on a refused refresh token, so the refusal is read the way
    the core's own "signed in" answer reads it: this process's memory first, then the
    strict marker. The store clears the marker on every save of the slot, so a fresh
    sign-in reads as signed in again."""
    if store.refused_in_process(token):
        return True
    return store.refresh_rejected_strict(token.identity) is not None


def sign_in_status() -> dict:
    """The lane's token-free sign-in status.

    ``state`` is ``signed_in`` only for what :func:`vault_token` accepts (a usable
    ``identity_center`` sign-in on the lane's start URL), read without a refresh, and
    only while the issuer has not refused its renewal. ``local_agent`` is ``off`` while
    the core's registered-sign-in fence holds. Blocking file IO; never raises; carries
    no token."""
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.login.endpoints import stored_sign_in_registered

    ended = False
    try:
        store = default_token_store()
        token = store.resolve()
        mine = (
            token is not None
            and token.identity == "identity_center"
            and token_is_lane_user(token)
            and token.is_usable()
        )
        ended = mine and token is not None and _renewal_refused(store, token)
        ours = mine and not ended
    except Exception:  # noqa: BLE001 - an unreadable vault is not a sign-in
        ours = False
    try:
        fenced = stored_sign_in_registered()
    except Exception:  # noqa: BLE001 - the fence's own reader decides; unread is "on"
        fenced = False
    return {
        "state": "signed_in" if ours else "signin_required",
        "message": SIGNED_IN if ours else (SIGN_IN_ENDED if ended else NOT_SIGNED_IN),
        "local_agent": "off" if fenced else "on",
    }


def vault_token() -> str:
    """The lane user's access token from Crew's vault, or raise :class:`NotLaneSignIn`.

    The identity is judged on the token the refresh returned, so a sign-in that changes
    during the read is never used. Blocking (runs the refresh on its own loop)."""
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
    from kiro_crew.auth.refresh import RefreshRejected
    from kiro_crew.lts_sign_in import run_coroutine

    provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
    try:
        token = run_coroutine(provider.current())
    except NotAuthenticated:
        raise NotLaneSignIn(NOT_SIGNED_IN) from None
    except RefreshRejected:
        # The issuer won't renew it (its session limit): only a new sign-in helps.
        logger.info("the stored sign-in can't be renewed; a new sign-in is needed")
        raise NotLaneSignIn(SIGN_IN_ENDED) from None
    except Exception as exc:  # noqa: BLE001 - a refresh error can quote response bytes
        logger.info("the stored sign-in could not be renewed (%s)", type(exc).__name__)
        raise NotLaneSignIn(SIGN_IN_NOT_RENEWED) from None
    if token.identity != "identity_center" or not token_is_lane_user(token):
        raise NotLaneSignIn(NOT_SIGNED_IN)
    if not token.access_token:
        raise NotLaneSignIn(NOT_SIGNED_IN)
    return token.access_token
