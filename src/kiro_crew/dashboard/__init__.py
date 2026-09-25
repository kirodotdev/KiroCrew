"""Lightweight web dashboard — status page at ``localhost:5476``.

Uses ``aiohttp`` for HTTP serving and native ``EventSource`` (SSE) for live
updates.  Serves static assets from the ``static/`` directory.

The package is split into:
- ``state``    — ChatSlot / DashboardState data classes
- ``handlers`` — status, system, cron, lesson, spawn, log endpoints
- ``chat``     — multi-slot chat endpoints + background LLM runner
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew import lazy_exports as _lazy_exports

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

#: A re-exported name lives in exactly one place, the submodule that defines it:
#: reads resolve that submodule's current value and writes go to it, so the two
#: spellings of a name cannot hold different values.
__getattr__ = _lazy_exports.bind(
    __name__,
    {name: (f"{__name__}.{module}", symbol) for name, (module, symbol) in _LAZY.items()},
)


def __dir__():
    return sorted(__all__)


if TYPE_CHECKING:  # names for static type checkers only
    from kiro_crew.dashboard.server import start_api_server, start_dashboard
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _fmt_duration

__all__ = ["start_api_server", "start_dashboard", "DashboardState", "_ChatSlot", "_fmt_duration"]
