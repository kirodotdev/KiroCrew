"""Dashboard deep links for the board's rows.

Built with :func:`~kiro_crew.dashboard.chat_backfill.session_deep_link` over the
configured dashboard origin (the tunnel when ``slack.use_tunnel_url`` opts in), the
same link the dashboard's Slack backfill marker uses. NEVER with a click token: a
token is a credential and the board is in a channel other people can read, so a
link opens the session only for someone already signed in to the dashboard.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _no_link(_key: str) -> str:
    return ""


def link_builder() -> Callable[[str], str]:
    """A ``slot key -> URL`` function ("" when no origin is configured). BLOCKING."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.chat_backfill import session_deep_link
        from kiro_crew.dashboard.urls import tunnel_origin_if_opted_in

        cfg = KiroCrewConfig.load()
        tunnel = tunnel_origin_if_opted_in(cfg.slack.use_tunnel_url)
        url = cfg.dashboard.url
    except Exception:
        logger.debug("code channel board: no dashboard link origin", exc_info=True)
        return _no_link

    def _link(key: str) -> str:
        return session_deep_link(url, key, tunnel_url=tunnel) if key else ""

    return _link
