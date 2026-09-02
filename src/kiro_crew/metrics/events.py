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


# ---------------------------------------------------------------------------
# Business-event series — "did this subsystem do its job, and how often"
# ---------------------------------------------------------------------------
# Each of these is emitted at its own subsystem's call site rather than from a
# central observer, because there is no frame every one of them crosses. What
# they share is the attribute rule above: every value is a constant from a
# closed set the emitting site owns, never an id, a name, or a count that grows
# with input.

#: One per subagent whose spawn actually started. ``concurrency`` is the live
#: running count INCLUDING this one (the counter is emitted after the admission
#: increment), so it reads 1 for a lone subagent -- an integer bounded by the
#: spawn cap, which keeps the series small and makes the aggregator's MAX over it
#: the concurrency high-water mark. A separate high-water instrument would need
#: its own reset semantics and could not be read per-attribute like this.
SUBAGENTS_SPAWNED = "kirocrew.subagent.spawned"

#: One per cron job execution. ``kind`` separates the three dispatch shapes
#: (``agent`` runs an LLM turn, ``script`` and ``command`` bypass the model
#: entirely), which is the difference between a job that costs tokens and one
#: that costs none. ``trigger`` says whether the schedule or a human fired it.
CRON_FIRES = "kirocrew.cron.fires"

#: One per artifact created. ``kind`` is the artifact's validated type and
#: ``source`` the validated origin — both already closed sets in ``artifacts``.
ARTIFACTS_CREATED = "kirocrew.artifact.created"

#: One per dynamic-workflow run started, foreground or background (the
#: background entry point drives the same method). ``authored`` marks a run that
#: writes its own script from an intent; ``replay`` marks a restart-subtree that
#: reuses cached agent results instead of re-calling the model.
WORKFLOW_RUNS = "kirocrew.workflow.runs"

#: One per context compaction that reached a verdict, so the sum is compaction
#: ATTEMPTS. ``success`` reports whether the compaction itself completed, NOT how
#: much context it reclaimed -- no before/after reading is taken -- and it is
#: False when an in-place compaction failed and the session was recycled instead.
CONTEXT_COMPACTIONS = "kirocrew.context.compactions"

#: One per MCP stub reconnect to a restarted daemon. Each increment is a
#: bridge that died and was rebuilt under a live session, so a rising rate is
#: daemon instability that sessions are absorbing silently.
MCP_RECONNECTS = "kirocrew.mcp.reconnects"

#: One per tool-approval decision from the per-surface gate
#: (``hooks.HookManager.on_tool_call``), which every surface consults before a
#: tool runs. ``decision`` is the gate's own bounded action: ``auto_approve``
#: skipped the human, ``deny`` refused without asking, and ``allow`` is the
#: branch that falls through TO an interactive prompt — so the ``allow`` slice
#: is approvals shown and the ``deny`` slice is approvals denied.
#: ``security_deny`` separates a hard security refusal from a policy-state one.
#:
#: This generalises the backend-child pair above (:data:`CHILD_PERMISSION_DENIED`
#: / :data:`CHILD_PERMISSION_ROUTED`) to every approval prompt. Those two stay
#: exactly as they are: they measure a specific hang-resilience fix on the
#: child-permission path, and their population is not this one's.
APPROVAL_DECISIONS = "kirocrew.approval.decisions"
