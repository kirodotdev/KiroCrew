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


def emit_histogram(
    name: str,
    value: float,
    attrs: dict[str, str | int | bool | float],
    *,
    unit: str = "1",
) -> None:
    """Record one *value* observation on histogram *name*; never raises.

    The sampled series below (queue depth, oldest wait, effective caps, host
    peaks, loop lag, recovery duration) are observations taken by a sampler on
    its own cadence, so a histogram -- whose MAX/last reading the aggregator
    can take per attribute set -- is the right instrument: a gauge would need
    an observable callback registered on the provider's live path, which the
    facade cannot reach without the import cycle documented above.
    """
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().histogram(name, value, unit=unit, attrs=attrs)
    except Exception:  # telemetry must never break the caller
        logger.debug("histogram emit failed for %s", name, exc_info=True)


# ---------------------------------------------------------------------------
# Hang-resilience series (see docs in the emitting call sites)
# ---------------------------------------------------------------------------

#: Every fast-fail denial of a backend-child permission request — the path
#: that prevents a silent 2-hour hang. ``reason`` is
#: the closed SEL reason enum; ``surface`` names the choke point.
CHILD_PERMISSION_DENIED = "kirocrew.acp.child_permission.denied"

#: Every backend-child permission request successfully ROUTED into the
#: mode-parity pipeline (owner queue → policy gates / interactive card).
#: This is the impact numerator: each increment is a request that would
#: otherwise be silently dropped and wedge its crew until the 2h ceiling.
#: ``routed + denied`` ≈ total child permission requests handled.
CHILD_PERMISSION_ROUTED = "kirocrew.acp.child_permission.routed"

#: Unroutable ACP frames per method class. ``method_class=permission`` is the
#: hang signature and MUST stay ~0 — any nonzero
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

#: One per adaptive-concurrency decision that CHANGED a cap or the paused flag
#: (``adaptive.controller``). ``action`` is the policy's closed action enum:
#: ``decrease`` (a corroborated-pressure halving), ``increase`` (a +1 earned by
#: a clean window), ``pause`` (dispatch stopped under severe pressure),
#: ``probe`` (one task admitted to test the recovery) and ``resume`` (the probe
#: completed). Holds and fixed-mode ticks are not counted: the series measures
#: how often the host made the controller act, not how often it looked. The
#: cap values themselves grow with the host and belong in the state snapshot,
#: never in a series key.
ADAPTIVE_DECISIONS = "kirocrew.adaptive.decisions"


# ---------------------------------------------------------------------------
# Overload-resilience series (RFC overload-resilience §10) — every attribute
# value is a member of a closed set: a task STATE, a LAYER name, a LANE KIND,
# a PRESSURE REASON, a PROCESS role. Never a task id, a session key, a slot
# key, a backend key, a hostname.
# ---------------------------------------------------------------------------

#: Sampled depth of the durable task queue per state (``state`` is one of the
#: 13 ``taskq.model`` states). Emitted by the structured health sampler
#: (``dashboard/session_health.py``) on each health computation.
TASKQ_DEPTH = "kirocrew.taskq.depth"

#: Sampled age (seconds) of the oldest row still waiting for dispatch.
TASKQ_OLDEST_WAIT_SECS = "kirocrew.taskq.oldest_wait_secs"

#: One per task reaching a terminal state; ``outcome`` is the terminal state
#: name. The aggregator's rate over this IS the completion rate the RFC names
#: (``taskq_completion_rate``); a pre-computed rate would need its own window.
TASKQ_COMPLETIONS = "kirocrew.taskq.completions"

#: Sampled effective concurrency cap per ``lane_kind`` (``subagents``,
#: ``spawn_gate``): the value the controller is currently enforcing, which is
#: the user's maximum only when nothing is degraded.
TASKQ_EFFECTIVE_CAP = "kirocrew.taskq.effective_cap"

#: One per health sample taken while a pressure reason is active; ``reason``
#: is the controller's closed reason enum (``memory``, ``loop_lag``,
#: ``provider_throttle``, ``start_latency``, ``fd``, ``procs``, ``manual``).
TASKQ_PRESSURE_REASON = "kirocrew.taskq.pressure_reason"

#: Sampled peaks the host budget observed since the previous sample.
HOST_PROCS_PEAK = "kirocrew.host.procs_peak"
HOST_FDS_PEAK = "kirocrew.host.fds_peak"
HOST_RSS_PEAK_MB = "kirocrew.host.rss_peak_mb"

#: Sampled event-loop lag per ``process`` (``gateway``, ``gatewayd``).
LOOP_LAG_MS = "kirocrew.loop.lag_ms"

#: One sample of the emitting process's resident set, per ``process``, taken on
#: the adaptive controller's cadence. Distinct from the
#: ``kirocrew.process.memory.rss_bytes`` GAUGE of the same quantity, and the
#: name says ``sampled`` because that is the difference that matters: a gauge
#: merged across instances keeps only min, max and mean, so a fleet-wide p90 is
#: not recoverable from it at any storage layer. Histograms sharing bucket
#: boundaries merge element-wise, so the distribution survives the merge. Both
#: are kept: the gauge still answers "what is this one box doing now".
PROCESS_RSS_SAMPLED = "kirocrew.process.memory.rss_sampled"

#: The same sample as a share of the whole machine: CPU seconds burned since the
#: previous sample, divided by the seconds elapsed times the logical core count.
#: Computed here rather than downstream because ``kirocrew.process.cpu.seconds``
#: is a lifetime total, so a consumer must difference consecutive samples per
#: process lifetime and detect restarts to recover a rate at all — and once a
#: gauge has been merged across instances there is nothing left to difference.
PROCESS_CPU_UTILIZATION = "kirocrew.process.cpu.utilization"

#: Sampled histograms here whose values are NOT milliseconds, mapped to the unit
#: their ``emit_histogram`` call passes.
#:
#: The dashboard's generic histogram branch reports every statistic it does not
#: recognise under ``*_ms`` keys, so a byte count or a ratio arriving there is
#: rendered as a duration. It resolves the exceptions from this mapping, the same
#: way it resolves lifetime-total gauge names from the gauge modules: the module
#: that declares an instrument is the one that knows what its reading means, so
#: the unit is stated once, here, rather than re-spelled by the reader.
#:
#: Adding a non-millisecond sampled histogram means adding it here too.
#: ``test_provider_bucket_views.py`` fails when an entry in the provider's
#: non-duration bucket map is missing from this mapping.
NON_MS_HISTOGRAM_UNITS: dict[str, str] = {
    PROCESS_RSS_SAMPLED: "By",
    PROCESS_CPU_UTILIZATION: "1",
}

#: One observation per recovered unit: seconds from its first failure at
#: ``layer`` to the success that closed the run (``recovery.ladder``).
RECOVERY_DURATION_SECS = "kirocrew.recovery.duration_secs"

#: One per recovery decision; ``layer`` + ``action`` (``retry`` /
#: ``escalate`` / ``notify`` / ``give_up``).
RECOVERY_ATTEMPTS = "kirocrew.recovery.attempts"

#: One per hand-up the ladder took; ``from_layer`` / ``to_layer``.
RECOVERY_ESCALATIONS = "kirocrew.recovery.escalations"

#: One per rebuild a layer performed (a backend respawn, a runtime rebuild, a
#: daemon respawn); ``layer``.
RESTARTS_TOTAL = "kirocrew.recovery.restarts"
