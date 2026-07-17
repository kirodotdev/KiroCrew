"""Lightweight web dashboard — status page at ``localhost:5476``.

Uses ``aiohttp`` for HTTP serving and native ``EventSource`` (SSE) for live
updates.  Serves static assets from the ``static/`` directory.

The package is split into:
- ``state``    — ChatSlot / DashboardState data classes
- ``handlers`` — status, system, cron, lesson, spawn, log endpoints
- ``chat``     — multi-slot chat endpoints + background LLM runner
"""

from __future__ import annotations

from kiro_crew.dashboard.server import start_api_server, start_dashboard
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, _fmt_duration

__all__ = ["start_api_server", "start_dashboard", "DashboardState", "_ChatSlot", "_fmt_duration"]
