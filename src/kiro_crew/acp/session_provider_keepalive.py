"""What ``/api/session-keepalive`` stamps on one session: its touch and its tool heartbeat.

A blocking Kiro Crew tool (``wait``, ``spawn_sub_agents``, ``ask_question``) pings the
keepalive route while it runs. The route touches the session (:func:`touch_activity`),
and when the body names the harness call the tool runs under it restarts that call's
silence timer (:func:`note_tool_heartbeat`). Only a handle the route's registry holds
gets that heartbeat, so the registry marks it (:func:`mark_keepalive_routed`).

Each function here is an ``AcpSessionProvider`` method: the class binds it by name, so
it reads and writes the provider's own state on the instance, and a patch of the method
on the class or on an instance reaches every caller.

Instance fields read: ``_handle``, ``_runtime``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider


def touch_activity(self: AcpSessionProvider) -> None:
    """Refresh activity timestamp on the runtime, and stamp this session's handle.

    The runtime half is PROCESS-level: the clock belongs to the runtime, so
    one session's activity refreshes it for every session on it. An idle
    co-tenant is therefore never idle while a neighbour talks, which is the
    SAFE direction for anything that reaps on idleness (it defers, never
    signals early) and the wrong one for anything that reports idle time
    as a fact about a session.

    The handle half is per-session: the tool-stall watchdog ignores the
    runtime clock, so without it a blocking tool's keepalive pings
    (``wait``, ``spawn_sub_agents``) would not keep its own turn alive.
    """
    self._runtime._last_activity = time.monotonic()
    self._handle.note_keepalive()


def note_tool_heartbeat(
    self: AcpSessionProvider, tool_call_id: str = "", *, activity_at: float | None = None
) -> int:
    """A Kiro Crew tool's heartbeat for one of this session's calls.

    Carried by ``/api/session-keepalive`` beside the touch above: it restarts
    the per-call silence timer of the call the tool runs under
    (``AcpSessionHandle.note_tool_heartbeat``). Returns how many calls it
    stamped.
    """
    return self._handle.note_tool_heartbeat(tool_call_id, activity_at=activity_at)


def mark_keepalive_routed(self: AcpSessionProvider) -> None:
    """The keepalive route's registry holds this session: its handle times every call."""
    self._handle.keepalive_routed = True
