"""pptx-maker builtin app — spec-driven PowerPoint generation.

Wraps the public ``spec-driven-presentation-maker`` engine (AWS Samples, MIT-0)
at a pinned tag: the engine does the slide composition and .pptx writing, and
this app supplies the KiroCrew integration — the agents that drive it, the
in-dashboard creation studio, and the deck/style/template library API.

Originally contributed as a standalone app by ``sktok``; see ``ATTRIBUTION.md``.
"""

# Required re-export: dashboard/server.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.pptx_maker")`` then checks
# ``hasattr(_mod, "register_routes")`` on the PACKAGE itself (not the
# backend.routes submodule) — matching issue_radar/__init__.py and
# code_review_sage/__init__.py, which do the same re-export.
from .backend.routes import register_routes

__all__ = ["register_routes"]
