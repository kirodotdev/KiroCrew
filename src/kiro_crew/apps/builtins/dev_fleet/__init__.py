# Dev Fleet builtin app.
#
# Two halves. The fleet backend (``server.py``) is a SPAWNED, sandboxed process the
# gateway reverse-proxies at ``/apps/dev-fleet/api/``. Two route families live in the
# GATEWAY process instead, both installed through the one ``register_routes`` the
# ``BUILTIN_NAMES`` loop in ``dashboard/routes/system.py`` picks up on this package
# (``importlib.import_module("kiro_crew.apps.builtins.dev_fleet")`` then
# ``_mod.register_routes(app)`` — the hook is on the PACKAGE, not a submodule, as in
# issue_radar/__init__.py and code_review_sage/__init__.py):
#
# * ``agent_pod_api.py`` — the AGENT-facing pod lifecycle routes.
# * ``gateway_routes.py`` — the live-target cutover, restart, read broker and removal
#   leases, which reach the bind-masked pointer and so cannot run in the backend
#   (see that module for why).
#
# What the app asks of the host (the BUILD rlimit, the bound port, the env it needs
# forwarded, its config section, its strict-internal and relay-refused paths) is the
# data in ``host_grants.py``, read by the core's ``apps.builtin_host_grants`` seam.
# That seam imports this package from the MCP servers and the config loader too, so
# the two route modules (seconds of imports) load inside ``register_routes`` only.
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web


def register_routes(app: web.Application) -> None:
    """Install both in-gateway route families (``_mod.register_routes(app)`` contract)."""
    from . import agent_pod_api, gateway_routes

    agent_pod_api.register_routes(app)
    gateway_routes.register_routes(app)
