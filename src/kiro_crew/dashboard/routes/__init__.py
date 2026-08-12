"""The dashboard's HTTP route table, split into ordered slices.

``start_dashboard`` used to carry all 447 registrations inline. They live here
instead, one module per section of the original table, each exposing
``register(app)``.

**The order of ``_REGISTRARS`` is load-bearing and must not be sorted.** aiohttp
resolves a request against its routes in REGISTRATION order, and this table
depends on that in several places -- a literal path is deliberately registered
before a pattern that would otherwise swallow it (``/api/skills/-/tree`` before
``/api/skills/{name:.+}``, ``/api/chat/slots/cleanup`` before
``/api/chat/slots/{slot}``, ``/api/steering/search`` before
``/api/steering/{key}``, and others called out in the slice comments). Splitting
the table into contiguous slices and calling them in the original sequence keeps
global ordering identical by construction; reordering this tuple, or the lines
inside any slice, can silently shadow a route.

``test_dashboard_route_table.py`` pins the resulting (method, path, handler)
sequence, so a reordering shows up as a failing test rather than as a 404 or a
handler quietly answering the wrong path.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.routes import (
    agent_config,
    agents,
    chat,
    connections,
    memory,
    messaging,
    realtime,
    sessions,
    skills,
    system,
    taskrunner,
    themes,
)

# Original registration order. NOT alphabetical -- see the module docstring.
_REGISTRARS: tuple[tuple[str, object], ...] = (
    ("realtime", realtime.register),
    ("memory", memory.register),
    ("messaging", messaging.register),
    ("skills", skills.register),
    ("themes", themes.register),
    ("agent_config", agent_config.register),
    ("chat", chat.register),
    ("agents", agents.register),
    ("sessions", sessions.register),
    ("taskrunner", taskrunner.register),
    ("connections", connections.register),
    ("system", system.register),
)

REGISTRAR_NAMES: tuple[str, ...] = tuple(name for name, _ in _REGISTRARS)


def register_all(app: web.Application) -> None:
    """Register every dashboard route on *app*, in the original order."""
    for _name, register in _REGISTRARS:
        register(app)  # type: ignore[operator]
