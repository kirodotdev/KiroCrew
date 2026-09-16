"""Kiro Crew Guide builtin app — a searchable troubleshooting knowledge base.

The gateway imports this package at startup and calls ``register_routes(app)``
if it exists (see ``dashboard/routes/system.py``), which is the whole reason for
this re-export. Keep it a plain re-export: anything heavier would run on every
gateway boot, including boots where the app is disabled.
"""

from .backend.routes import register_routes

__all__ = ["register_routes"]
