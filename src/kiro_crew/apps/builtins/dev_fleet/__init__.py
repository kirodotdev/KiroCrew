# Dev Fleet builtin app

# Required re-export: dashboard/routes/system.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.dev_fleet")`` then checks
# ``hasattr(_mod, "register_routes")`` on the PACKAGE itself, not on a submodule.
# issue_radar/__init__.py and code_review_sage/__init__.py do the same re-export.
#
# These are the AGENT-facing pod routes only. The dashboard UI keeps talking to
# this app's backend subprocess through the /apps/dev-fleet/api/* proxy; nothing
# about that path changes.
from .agent_pod_api import register_routes  # noqa: F401
