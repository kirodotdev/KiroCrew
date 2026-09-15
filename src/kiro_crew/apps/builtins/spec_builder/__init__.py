"""Spec Builder — spec-driven development inside Kiro Crew.

Turns a feature idea into three reviewable markdown files
(Requirements → Design → Tasks) via an embedded per-spec agent, then hands the
approved plan off to an autonomous execution loop. The output is the
Kiro-standard ``.kiro/specs/<name>/`` layout, portable to the Kiro IDE/CLI.
"""

# Required re-export: dashboard/server.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.spec_builder")`` then
# checks ``hasattr(_mod, "register_routes")`` on the PACKAGE itself (not the
# backend.routes submodule) — matches issue_radar/__init__.py and
# code_review_sage/__init__.py, which do the same re-export.
from .backend.routes import register_routes  # noqa: F401
