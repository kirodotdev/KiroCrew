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
from aiohttp import web

from . import agent_pod_api, gateway_routes


def register_routes(app: web.Application) -> None:
    """Install both in-gateway route families (``_mod.register_routes(app)`` contract)."""
    agent_pod_api.register_routes(app)
    gateway_routes.register_routes(app)
