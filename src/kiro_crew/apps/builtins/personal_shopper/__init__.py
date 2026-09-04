"""Personal Shopper — a life-problem advisor with shopping capability.

Diagnoses the user's actual need through conversation and recommends products
only when a purchase genuinely solves the problem. Preferences live in an
app-owned sqlite store with vector search, so the advisor recalls sizes,
brands, budget and restrictions across sessions. It never purchases: the
output is always advice plus a link the user acts on themselves.
"""

# Required re-export: dashboard/server.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.personal_shopper")`` then
# checks ``hasattr(_mod, "register_routes")`` on the PACKAGE itself (not the
# backend.routes submodule).
from .backend.routes import register_routes  # noqa: F401
