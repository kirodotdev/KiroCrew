"""The label an HTTP route of the gateway can carry: an action name and a tier.

A :class:`RouteAccess` says what a route does (for example ``widgets.reset``) and how long its
request and response shape is promised. :data:`ROUTES` records the labels, keyed by
method and path pattern. A route the baseline does not list needs an entry here.

The table is separate from route registration on purpose. One handler can serve two
routes, so a label on the handler would be ambiguous, and registration runs on the
gateway boot path, where a label must cost nothing.

No request reads this module: it decides nothing about who may call a route.
``test/test_route_access_labels.py`` holds the registered routes against it.
Rules for picking a name and a tier: ``docs/system-specs/modules/route-access.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ACTION_NAME_RE",
    "PREVIEW",
    "ROUTES",
    "STABLE",
    "TIERS",
    "UNSTABLE",
    "RouteAccess",
]

# Request and response shapes only gain fields.
STABLE: Final = "stable"
# The shape is nearly settled; small changes are possible.
PREVIEW: Final = "preview"
# No promise: the shape changes whenever the dashboard needs it to.
UNSTABLE: Final = "unstable"

TIERS: Final = frozenset({STABLE, PREVIEW, UNSTABLE})

# ``<area>.<verb>``: lower-case parts joined by dots, at least two of them.
ACTION_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


@dataclass(frozen=True)
class RouteAccess:
    """One route's action name and stability tier."""

    action: str
    tier: str


# Keyed by (METHOD, path pattern). The pattern is the route's canonical path: the path
# as registered, with any regex dropped from a ``{name:regex}`` segment. A HEAD route
# registered beside a GET route of the same path takes the GET route's entry.
ROUTES: Final[dict[tuple[str, str], RouteAccess]] = {
    ("GET", "/api/apps/{name}/dashboard-proxy"): RouteAccess("apps.dashboard_proxy.read", UNSTABLE),
    ("PUT", "/api/apps/{name}/dashboard-proxy"): RouteAccess(
        "apps.dashboard_proxy.grant", UNSTABLE
    ),
    ("DELETE", "/api/apps/{name}/dashboard-proxy"): RouteAccess(
        "apps.dashboard_proxy.revoke", UNSTABLE
    ),
    ("POST", "/api/apps/{name}/dashboard-proxy/restart"): RouteAccess(
        "apps.dashboard_proxy.restart", UNSTABLE
    ),
}
