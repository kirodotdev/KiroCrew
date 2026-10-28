"""``kirocrew.crew_mode.*`` -- how often Crew Mode is turned on, in two counters.

Answers two questions and nothing else: how often Crew Mode is turned on, and what
share of new chats that is. A share is two counters, not a gauge:
``turned_on / new_chats`` over any window a backend picks.

No attribute carries a session key, an item id, a title, a message, a summary or an
agent name: every value is a member of a closed tuple declared here or a bool, and an
unrecognised one maps to :data:`OTHER`. Telemetry consent gates every emit, because
``emit_counter`` resolves a no-op recorder when telemetry is off, and an emit never
raises into the instrumented path.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.metrics.events import emit_counter

#: One per chat that turns Crew Mode on (a new chat born on a conductor). ``via``
#: says how; ``repeat`` is true when a Crew Mode chat already existed, which is the
#: repeat-use signal.
CREW_MODE_TURNED_ON = "kirocrew.crew_mode.turned_on"

#: One per new chat, with ``crew_mode`` saying whether it was born in Crew Mode.
#: The denominator of the share: ``sum(turned_on) / sum(new_chats)``.
CREW_MODE_NEW_CHATS = "kirocrew.crew_mode.new_chats"

OTHER = "other"

#: How Crew Mode was turned on.
TURNED_ON_VIA: tuple[str, ...] = ("create",)


def _member(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


def record_turned_on(via: str, *, repeat: bool) -> None:
    emit_counter(CREW_MODE_TURNED_ON, {"via": _member(via, TURNED_ON_VIA), "repeat": bool(repeat)})


def record_new_chat(*, crew_mode: bool) -> None:
    emit_counter(CREW_MODE_NEW_CHATS, {"crew_mode": bool(crew_mode)})
