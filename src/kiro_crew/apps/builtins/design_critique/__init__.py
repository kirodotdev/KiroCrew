"""Design Critique — builtin app package marker.

The startup route loop in ``dashboard/routes/system.py`` imports this package and
reads ``register_routes`` off the package object, inside the guard that skips an
app whose files cannot be read.

The core's ``apps.seams`` imports this package to reach ``seams.py`` while
``dashboard.chat_handlers`` is itself being imported, so the route module (which
imports the dashboard) is not imported here. ``register_routes`` is served by the
module ``__getattr__`` below instead, which imports the route module on first access:
that access is the route loop's guarded lookup, so an unreadable route file is
skipped there like any other unreadable app.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.design_critique.backend.routes import (  # noqa: F401
        register_routes,
    )

#: Each lazily served name and its owner's dotted name.
_LAZY_OWNERS: dict[str, str] = {
    "register_routes": "kiro_crew.apps.builtins.design_critique.backend.routes",
}


def __getattr__(name: str) -> Any:
    owner = _LAZY_OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(owner), name)
