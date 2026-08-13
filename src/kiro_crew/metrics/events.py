"""Best-effort counter emits for hang-resilience telemetry.

One tiny facade so low-level modules (acp runtime/handle, session sweep,
subagent manager) can emit ``kirocrew.*`` counters without importing
``metrics.provider`` at module top — that import chain reads KiroCrewConfig
and would form a cycle (config.loader -> ... -> metrics.provider ->
config.loader; same reason every existing emit site does a lazy import).

Telemetry must never break the instrumented path: every failure is swallowed
after a debug log. Attribute VALUES must be low-cardinality constants per
``metrics/schema.py`` — callers pass closed enums only, never ids or
free-form strings.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def emit_counter(name: str, attrs: dict[str, str | int | bool | float]) -> None:
    """Add 1 to counter *name* with *attrs*; never raises."""
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().counter(name, attrs=attrs)
    except Exception:  # telemetry must never break the caller
        logger.debug("counter emit failed for %s", name, exc_info=True)


# ---------------------------------------------------------------------------
# Hang-resilience series (see docs in the emitting call sites)
# ---------------------------------------------------------------------------

#: Every fast-fail denial of a backend-child permission request — the paths
#: that replaced the pre-fix silent 2-hour hangs (issue #3785). ``reason`` is
#: the closed SEL reason enum; ``surface`` names the choke point.
CHILD_PERMISSION_DENIED = "kirocrew.acp.child_permission.denied"

#: Every backend-child permission request successfully ROUTED into the
#: mode-parity pipeline (owner queue → policy gates / interactive card).
#: This is the impact numerator: each increment is a request that, before
#: #3786, was silently dropped and wedged its crew until the 2h ceiling.
#: ``routed + denied`` ≈ total child permission requests handled.
CHILD_PERMISSION_ROUTED = "kirocrew.acp.child_permission.routed"

#: Unroutable ACP frames per method class. ``method_class=permission`` was the
#: pre-fix hang signature and MUST stay ~0 after #3786/#3889 — any nonzero
#: value is a routing regression alarm.
DROPPED_FRAMES = "kirocrew.acp.dropped_frames"

#: Cause attribution for turn timeouts (the 2h-ceiling hangs): whether the
#: session was parked on a permission prompt and whether backend children
#: were live when the ceiling fired.
TURN_TIMEOUT_CAUSE = "kirocrew.turn.timeout.cause"

#: Idle-sweep expiries — ``turn_active=True`` means the sweep killed a
#: runtime mid-turn, the teardown signature of the original incidents.
SESSION_IDLE_EXPIRED = "kirocrew.session.idle_expired"
