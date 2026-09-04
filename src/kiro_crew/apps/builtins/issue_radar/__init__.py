"""Issue Radar — an issue triage assistant for GitHub repo owners.

Investigates issues and surfaces linked PR/commit status, keeping findings in a
local per-repo ledger. Authenticates via the user's existing ``gh`` CLI
session; no GitHub App, no PAT management, no KiroCrew-hosted storage.
"""

# Required re-export: dashboard/server.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.issue_radar")`` then
# checks ``hasattr(_mod, "register_routes")`` on the PACKAGE itself (not the
# backend.routes submodule). code_review_sage/__init__.py does the same
# re-export.
from .backend.routes import register_routes  # noqa: F401
