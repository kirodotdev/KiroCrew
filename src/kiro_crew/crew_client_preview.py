"""The ``crew_client`` feature preview: edition hooks for a Crew client lane.

Behind ``dashboard.feature_previews.crew_client``, default off. On, Crew honours
what an edition supplies through the platform seam (``DashboardContributor``):

- ``idc_sign_in_profiles()``: an IAM Identity Center start URL's own client
  registration form, and a pinned profile ARN so its sign-in asks no control plane
  (``auth/login/endpoints.py``). A stored sign-in on such a start URL also fences
  the usage refresh and local KAS starts;
- ``kas_engine_auth()``: how Crew's own KAS engine bundle gets its credential
  (``agent_sdk/kas_engine_auth.py``).

Off, every one of them is ignored and Crew behaves as on ``main``. Nothing here
holds a start URL, an ARN, a host or a token: an edition supplies them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The preview's name under ``dashboard.feature_previews``.
CREW_CLIENT_PREVIEW: Final[str] = "crew_client"


def _config(cfg: Any) -> Any:
    """*cfg*, else the live snapshot, else the config file; None when unreadable."""
    if cfg is not None:
        return cfg
    try:
        from kiro_crew.config import live

        snap = live.snapshot()
        if snap is not None:
            return snap
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 -- unread means off
        logger.debug("crew_client preview: config unreadable; treating as off", exc_info=True)
        return None


def preview_on(cfg: Any = None) -> bool:
    """Whether the edition permits an exact ``True`` stored ``crew_client`` switch.

    Read through ``edition_locks.feature_previews`` like every server reader of a
    preview switch, so an LTS build reads it as off. Never raises.
    """
    from kiro_crew.edition_locks import feature_previews

    previews = feature_previews(
        getattr(getattr(_config(cfg), "dashboard", None), "feature_previews", None)
    )
    return isinstance(previews, Mapping) and previews.get(CREW_CLIENT_PREVIEW) is True


def usage_fenced() -> bool:
    """Whether the usage poll must not read Crew's person vault (``crew_client`` on).

    True while the stored sign-in is on an edition's own start URL, or while an
    edition engine credential serves the engine (the vault then holds no account to
    read usage for). Blocking; call off the loop. Never raises.
    """
    from kiro_crew.agent_sdk.kas_engine_auth import active_engine_auth
    from kiro_crew.auth.login.endpoints import stored_sign_in_registered

    try:
        return stored_sign_in_registered() or active_engine_auth() is not None
    except Exception:  # noqa: BLE001 -- unreadable means today's path
        logger.debug("crew_client preview: usage fence unreadable", exc_info=True)
        return False
