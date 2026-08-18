"""Kanban — a task board that runs its cards as real agent sessions.

Five columns (Backlog, Todo, Running, Done, Failed).  A card carries an
execution prompt; running it opens a real chat session (visible in the Sessions
list, openable in the dashboard) rather than a detached subagent, and the card
settles from that session's outcome.

Required re-export: ``dashboard/routes/system.py``'s startup route registration
imports this PACKAGE and checks ``hasattr(_mod, "register_routes")`` on it (not
on the ``backend.routes`` submodule) — matching ``spec_builder/__init__.py``
and ``issue_radar/__init__.py``.
"""

from .backend.routes import register_routes  # noqa: F401
