"""auto-improvement builtin app — measured self-improvement against a GitHub repo.

The gateway imports this package at startup and calls ``register_routes(app)`` if
it exists (see ``dashboard/server.py``), which is the whole reason this re-export
is here. Keep it a plain re-export: anything heavier runs on every gateway boot,
including boots where the app is disabled.
"""

from .backend.routes import register_routes

__all__ = ["register_routes"]
