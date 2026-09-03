"""Design Critique — builtin app package marker.

The startup route loop in ``dashboard/routes/system.py`` imports this package and
checks ``hasattr(module, "register_routes")`` on the package object, so the
handler must be re-exported here.
"""

from .backend.routes import register_routes  # noqa: F401
