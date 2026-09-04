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
from typing import TYPE_CHECKING

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


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{target[0]}")
    return getattr(module, target[1])


def __dir__():
    return sorted(__all__)


if TYPE_CHECKING:  # names for static type checkers only
    from kiro_crew.dashboard.server import start_api_server, start_dashboard
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _fmt_duration

__all__ = ["start_api_server", "start_dashboard", "DashboardState", "_ChatSlot", "_fmt_duration"]
