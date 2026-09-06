"""Chat Status Tags — SDLC status + health tagging for dashboard chats."""

# The gateway's builtin loader imports this PACKAGE and checks
# ``hasattr(_mod, "register_routes")`` on it — not on the ``backend.routes``
# module the manifest names — same convention as every other builtin
# (see e.g. ops_mission_control/__init__.py). Without this re-export the
# routes never register and the app page's API 404s.
from kiro_crew.apps.builtins.chat_status_tags.backend.routes import (  # noqa: F401
    register_routes,
)

__all__ = ["register_routes"]
