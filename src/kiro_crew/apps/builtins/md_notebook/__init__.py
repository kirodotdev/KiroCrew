# Notes builtin app — git-backed markdown vaults.
#
# Two halves. The Notes backend (``server.py``) is a SPAWNED, sandboxed process the
# gateway reverse-proxies at ``/apps/md-notebook/api/``. ONE route lives in the
# GATEWAY process instead, installed through the ``register_routes`` the
# ``BUILTIN_NAMES`` loop in ``dashboard/routes/system.py`` picks up on this package
# (``importlib.import_module("kiro_crew.apps.builtins.md_notebook")`` then
# ``_mod.register_routes(app)`` — the hook is on the PACKAGE, not a submodule, as in
# ``dev_fleet/__init__.py``):
#
# * ``gateway_routes.py`` — the state-file publish, which must run in the gateway
#   process because the backend's bind-mount namespace makes the staged rename
#   cross-mount (``EXDEV``); see that module for why.
from aiohttp import web

from . import gateway_routes


def register_routes(app: web.Application) -> None:
    """Install the in-gateway state-write route (``_mod.register_routes(app)`` contract)."""
    gateway_routes.register_routes(app)
