"""The ``/kirocrew`` command being handled, for its own handler task.

``events_slash._handle_slash`` sets both in the command's task just before it spawns the
command's handler. The handler's task gets a copy of them when it is created, so two
commands handled at once each read their own. They live outside ``events_slash``, which
imports this module inside the functions that use it, because every module-level name there
is also served by the ``kiro_crew.slack.events`` facade, whose name list is at the line cap.
"""

from __future__ import annotations

import contextvars

#: Where the command was typed, ``(channel, team)``: ``conductor`` posts its parent there.
room: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "slack_slash_room", default=("", "")
)
#: The command's ``trigger_id``, which a modal-opening handler passes to ``views.open``.
trigger_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "slack_slash_trigger_id", default=""
)
