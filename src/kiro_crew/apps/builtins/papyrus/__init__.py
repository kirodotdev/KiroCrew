"""Papyrus — a LaTeX paper editor with live PDF preview and an AI co-author.

Ported from the "Papyrus" app by tricatte (see ``ATTRIBUTION.md``). Papers live
under the app's own data dir; compilation runs ``pdflatex``/``tectonic`` on the
gateway host through the sandbox spawn chokepoint, never with shell escape. A host
with no TeX at all can provision a digest-pinned Tectonic binary into the app's own
data dir in one click — see ``backend/tectonic.py``.
"""

# Required re-export: dashboard/server.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.papyrus")`` then checks
# ``hasattr(_mod, "register_routes")`` on the PACKAGE itself (not the
# backend.routes submodule) — same as issue_radar/__init__.py.
from .backend.routes import register_routes  # noqa: F401
