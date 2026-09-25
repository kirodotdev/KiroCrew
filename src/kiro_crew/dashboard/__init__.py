"""Lightweight web dashboard — status page at ``localhost:5476``.

Uses ``aiohttp`` for HTTP serving and native ``EventSource`` (SSE) for live
updates.  Serves static assets from the ``static/`` directory.

The package is split into:
- ``state``    — ChatSlot / DashboardState data classes
- ``handlers`` — status, system, cron, lesson, spawn, log endpoints
- ``chat``     — multi-slot chat endpoints + background LLM runner
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

# Lazy attribute access (PEP 562) so importing anything under this package —
# e.g. ``dashboard.urls`` for the two URL helpers the CLI needs, or
# ``dashboard.origin`` from mcp_core / cli_doctor — does NOT eagerly pull
# ``dashboard.server`` and, through it, the entire handler tree. Importing
# ``dashboard.origin`` measured 605ms / 1124 modules with the eager exports
# below, paid by every CLI invocation and every MCP stdio subprocess even
# though no dashboard is ever served in those processes. Submodules load on
# first attribute access; ``from kiro_crew.dashboard import <submodule>``
# (server.py does this for channel_slots, chat, handlers, ...) is unaffected —
# the import system falls back to importing the submodule when the attribute
# lookup misses. Same pattern as ``mcp_gateway/__init__.py``.
_LAZY = {
    "start_api_server": ("server", "start_api_server"),
    "start_dashboard": ("server", "start_dashboard"),
    "DashboardState": ("state", "DashboardState"),
    "_ChatSlot": ("state", "_ChatSlot"),
    "_fmt_duration": ("state", "_fmt_duration"),
}

#: Package attribute -> ``(owning module, symbol on that module)``. A re-exported
#: name lives in exactly one place, the submodule that defines it: reads resolve
#: that submodule's current value and writes go to it, so the two spellings of a
#: name cannot hold different values.
_OWNED: dict[str, tuple[str, str]] = {
    name: (f"{__name__}.{module}", symbol) for name, (module, symbol) in _LAZY.items()
}

_OWNERS: dict[str, ModuleType] = {}


def _owner(name: str) -> ModuleType:
    """Return the module that defines ``name``, importing it on first use."""
    module_name = _OWNED[name][0]
    owner = _OWNERS.get(module_name)
    if owner is None:
        owner = _OWNERS[module_name] = importlib.import_module(module_name)
    return owner


def __getattr__(name: str) -> Any:
    """Read a re-exported name from the module that owns it (:pep:`562`)."""
    if name not in _OWNED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner(name), _OWNED[name][1])


class _ReExportModule(ModuleType):
    """Send a write to a re-exported name to the module that owns it.

    Binding the name in this package's own namespace instead would shadow the
    owner permanently, because ``__getattr__`` runs only for a name the package
    does not already hold: the shadow would win every later read, and the owner's
    value would become unreachable through this package. Forwarding the write
    leaves one value for a test harness to remember and one to put back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _OWNED:
            setattr(_owner(name), _OWNED[name][1], value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _OWNED:
            delattr(_owner(name), _OWNED[name][1])
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _ReExportModule


def __dir__():
    return sorted(__all__)


if TYPE_CHECKING:  # names for static type checkers only
    from kiro_crew.dashboard.server import start_api_server, start_dashboard
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _fmt_duration

__all__ = ["start_api_server", "start_dashboard", "DashboardState", "_ChatSlot", "_fmt_duration"]
