"""Meetings — an AI meeting assistant with live transcription, notes and tasks.

Transcribes a live meeting through KiroCrew's own streaming speech-to-text, fans
each line out to a small crew of background agents (a note-taker, a diagram
sketcher, a task extractor), and lets the user review the extracted action items
before filing them.

Two provider seams keep the app organization-neutral: the **task provider**
(where a reviewed action item gets filed) and the **calendar provider** (where
upcoming meetings come from). Each ships exactly one public implementation — a
local task ledger, and a stdlib iCalendar reader — plus a registry an out-of-repo
edition can add its own to. See ``backend/providers/``.
"""

# Required re-export: dashboard/server.py's startup route registration does
# ``importlib.import_module("kiro_crew.apps.builtins.meetings")`` then checks
# ``hasattr(_mod, "register_routes")`` on the PACKAGE itself (not the
# backend.routes submodule) — same as issue_radar/__init__.py.
from kiro_crew.apps.builtins.meetings.backend.routes import register_routes  # noqa: F401
