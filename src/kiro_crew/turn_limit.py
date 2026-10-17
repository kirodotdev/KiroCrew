"""The wall-clock limit of the chat turn running in the current context.

A dashboard chat turn runs under a resolved limit: the session's own turn time
limit when it has one, else ``agent.chat_turn_timeout_secs``. The ACP
transport's per-prompt wait has to outlive that limit, or the transport cuts a
long turn first and the user gets a raw transport timeout instead of the
turn-limit card. The transport sits several layers below the dispatch
(provider, essential delivery, session handle), none of which knows which
session the turn belongs to, so the dispatch publishes the limit here and the
transport reads it when it sizes the wait.

A ``ContextVar`` rather than a parameter for that reason: the prompt is awaited
inside the task the dispatch created, so the value reaches the one wait that
needs it without widening every provider's ``stream`` signature. Tasks started
inside the turn inherit it too. That is harmless by construction: the
transport only ever takes the LARGER of this value and its configured
deadlines, so an inherited limit can lengthen a wait, never shorten one.

This module imports nothing from the dashboard or the ACP layer, so both can
reach it without crossing the agent-SDK boundary in either direction.
"""

from __future__ import annotations

from contextvars import ContextVar

#: Seconds the running chat turn may take, or ``None`` outside a bounded turn.
TURN_LIMIT_SECS: ContextVar[float | None] = ContextVar("kirocrew_turn_limit_secs", default=None)


def current_turn_limit_secs() -> float | None:
    """The limit the running chat turn was dispatched with, if any."""
    value = TURN_LIMIT_SECS.get()
    if value is None or value <= 0:
        return None
    return float(value)
