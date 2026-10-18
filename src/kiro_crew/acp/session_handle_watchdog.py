"""The session's stale/stall watchdog.

``WatchdogSettings`` and its loader (the ``watchdog.*`` snapshot a handle takes), the turn
park clocks, the liveness-oracle consult, the WORKING deferral log and metric, the
stalled-tool end and the input-wait status.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_awaiting_permission``, ``_consult_future``, ``_crew_agent``,
``_keepalive_parked``, ``_keepalive_ts``, ``_oracle``, ``_parked_since``,
``_parked_total``, ``_watchdog``, ``_working_logged_ts``.

Instance fields read: ``_runtime``, ``_session_id``, ``_tool_output_seen``,
``_turn_done``, ``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.client import prompt_timeout_for_ceiling
from kiro_crew.acp.liveness import (
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_MCP_SUBTREE_ACTIVE,
    EVIDENCE_PLATFORM_LIMITED,
    EVIDENCE_REMOTE_FLAT,
    EVIDENCE_SHELL_CHILD_ABSENT,
    INTERACTIVE_NARROWING_RISKS,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    _consume_future_exception,
    consult_offloaded,
)
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    PROGRESS_SOURCE_PROCESS_EVIDENCE,
    STATUS_ORIGIN_LIVENESS_ORACLE,
    STATUS_PHASE_WAITING,
    STOP_REASON_TOOL_STALL,
    WAIT_REASON_INPUT,
    AcpEvent,
    StructuredStatus,
)
from kiro_crew.executors import subprocess_executor

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# ``WatchdogSettings.interactive_command_policy`` values (RFC §14.6).
INTERACTIVE_POLICY_CANCEL = "cancel"
INTERACTIVE_POLICY_WAIT = "wait"


@dataclass(frozen=True)
class WatchdogSettings:
    """Resolved ``watchdog.*`` config values, read ONCE at handle construction
    (never inside the dispatch loop). Defaults mirror ``WatchdogConfig`` in
    ``config/service_sections.py`` so a config-less context (tests, early bootstrap)
    behaves identically to a default config.

    Every idle window must stay strictly inside the turn's own wall-clock
    ceiling — see :func:`_clamp_to_prompt_ceiling` for why and
    :data:`_TURN_CEILING_WINDOW_FRACTION` for the enforced headroom."""

    check_after_secs: float = 60.0
    stale_window_secs: float = 600.0
    tool_stall_suspect_secs: float = 5400.0
    tool_stall_hard_cap_secs: float = 7200.0
    model_silent_probe_secs: float = 1800.0
    remote_flat_probe_secs: float = 0.0
    wellness_sample_secs: float = 3.0
    # Whether a per-agent watchdog_tool_stall_* override was applied to this
    # snapshot. Telemetry-only (the kirocrew.watchdog.action attr): a BOOLEAN,
    # never the agent name — free-form agent names are a cardinality bomb on
    # OTel attrs (metrics/schema.py); per-agent joins happen via the always-on
    # token row store instead.
    agent_override: bool = False
    # What the tool branch does when a stall is classified ``waiting_input``
    # (RFC §14.6): ``"cancel"`` — today's non-lethal ``session/cancel`` +
    # ``STOP_REASON_TOOL_STALL``, preceded by a ``waiting_input``
    # ``StructuredStatus`` so a scheduler can release the lane slot; ``"wait"``
    # — emit that status once and keep the turn open for real input, bounded by
    # the turn's own ceiling (the hard cap does NOT cancel a declared input
    # wait: the caller owns the task's ``deadline_at``). Never auto-answers.
    # Sourced from ``agent.interactive_command_policy`` by
    # ``_load_watchdog_settings``; an unknown value falls back to cancel.
    interactive_command_policy: str = INTERACTIVE_POLICY_CANCEL


# Fraction of a turn's deadline that a watchdog idle window may occupy. A window
# at or past the deadline is unreachable: the turn's own timeout fires first, so
# the UNKNOWN-verdict branch never runs and the user gets the generic "turn hit
# the limit" card instead of tool-stall recovery (which cancels non-lethally and
# re-drives with a continue-nudge naming the tool and any redirect log). The
# headroom covers the cancel + ack grace so recovery lands inside the same turn.
_TURN_CEILING_WINDOW_FRACTION = 0.9
# watchdog.* keys bounded by the prompt timeout: each is an idle-seconds window
# the dispatch loop compares elapsed idle against. wellness_sample_secs is a
# sampling interval, not a window, so it is not bounded here.
_TURN_BOUNDED_WINDOWS = (
    "check_after_secs",
    "stale_window_secs",
    "tool_stall_suspect_secs",
    "tool_stall_hard_cap_secs",
    "model_silent_probe_secs",
    "remote_flat_probe_secs",
)


def _clamp_to_prompt_ceiling(key: str, value: float, chat_ceiling: float) -> float:
    """Bound one watchdog window to the transport's per-prompt timeout.

    Resolved via :func:`~kiro_crew.acp.client.prompt_timeout_for_ceiling` on the
    caller's ALREADY-LOADED ``chat_turn_timeout_secs`` (no second config read;
    the transport's dispatch loop stops the turn at the same deadline), so it
    is the only safe bound for a snapshot that is taken once per handle and
    reused across prompts. It follows a raised ``agent.chat_turn_timeout_secs``
    and never sits below the 2h default, so a proportionately raised watchdog
    window is honoured instead of being cut to the default's fraction.

    Mirrors the shape of ``turn_dispatch.chat_turn_timeout_secs``'s clamp
    against the same timeout: an out-of-range value is honoured as far as the
    system can honour it, and the clamp is logged at warning level so the
    misconfiguration is visible instead of silently ignored.
    """
    ceiling = prompt_timeout_for_ceiling(chat_ceiling)
    budget = ceiling * _TURN_CEILING_WINDOW_FRACTION
    if value <= budget:
        return value
    logger.warning(
        "watchdog.%s=%.0fs leaves no room inside the %.0fs prompt timeout; "
        "clamping to %.0fs. The turn's own timeout would fire first, so the "
        "larger window cannot take effect.",
        key,
        value,
        ceiling,
        budget,
    )
    return budget


def _warn_if_above_chat_ceiling(key: str, value: float, chat_ceiling: float) -> None:
    """Advisory: a DASHBOARD turn ends at ``agent.chat_turn_timeout_secs``, so a
    window above that can never act there.

    Deliberately not clamped. The same handle also serves callers that pass
    their own, larger prompt timeout (a review run, a cron turn), and shrinking
    every window to the dashboard's ceiling would cancel their live work. So the
    mismatch is reported and left to the operator.
    """
    if 0 < chat_ceiling < value:
        logger.warning(
            "watchdog.%s=%.0fs exceeds agent.chat_turn_timeout_secs=%.0fs — a "
            "dashboard turn ends before this window can act, so a stall there "
            "surfaces as the turn-limit card instead of stall recovery.",
            key,
            value,
            chat_ceiling,
        )


def _load_watchdog_settings(crew_agent: str = "", cfg: Any = None) -> WatchdogSettings:
    """Snapshot ``watchdog.*`` from config. Function-level import (mirrors
    ``_sync_effort_levels``) avoids the config -> dashboard -> acp import
    cycle; any failure falls back to defaults rather than breaking a handle.

    ``crew_agent`` is the CANONICAL Kiro Crew agent name — a ``cfg.agents``
    key resolved by the surface that owns the identity (the dashboard slot,
    or a crew-name-passing surface like Slack/cron) and plumbed here through
    provider -> runtime -> handle. Resolution is a direct dict lookup: no
    cross-namespace matching happens here, so a bound kiro agent name (or any
    non-crew name) simply inherits the globals. That crew's
    ``watchdog_tool_stall_*`` overrides overlay the globals (> 0 means
    override; 0 inherits — the same empty-inherits convention as the agent's
    ``model``).

    ``cfg`` is an already-loaded ``KiroCrewConfig``. The config watcher's
    hot-apply hands in the config it just loaded so the re-clamp runs on the
    event loop without a filesystem read; ``None`` loads (a fingerprint-cache
    hit in practice) for the per-session paths that resolve off-loop.
    """
    try:
        # circular import: config.loader -> dashboard -> session -> acp
        from kiro_crew.config.loader import KiroCrewConfig

        if cfg is None:
            cfg = KiroCrewConfig.load()
        w = cfg.watchdog
        raw = {key: float(getattr(w, key)) for key in _TURN_BOUNDED_WINDOWS}
        overridden = False
        crew = cfg.agents.get(crew_agent) if crew_agent else None
        if crew is not None:
            if crew.watchdog_tool_stall_suspect_secs > 0:
                raw["tool_stall_suspect_secs"] = float(crew.watchdog_tool_stall_suspect_secs)
                overridden = True
            if crew.watchdog_tool_stall_hard_cap_secs > 0:
                raw["tool_stall_hard_cap_secs"] = float(crew.watchdog_tool_stall_hard_cap_secs)
                overridden = True
        # Overrides are applied BEFORE the ceiling pass so a per-agent window is
        # bounded exactly like a global one — an over-ceiling override is clamped
        # with the same warning instead of smuggling past the prompt timeout.
        chat_ceiling = float(cfg.agent.chat_turn_timeout_secs)
        bounded: dict[str, Any] = {}
        for key in _TURN_BOUNDED_WINDOWS:
            value = _clamp_to_prompt_ceiling(key, raw[key], chat_ceiling)
            _warn_if_above_chat_ceiling(key, value, chat_ceiling)
            bounded[key] = value
        policy = str(getattr(cfg.agent, "interactive_command_policy", "") or "")
        if policy not in (INTERACTIVE_POLICY_CANCEL, INTERACTIVE_POLICY_WAIT):
            policy = INTERACTIVE_POLICY_CANCEL
        return WatchdogSettings(
            wellness_sample_secs=float(w.wellness_sample_secs),
            agent_override=overridden,
            interactive_command_policy=policy,
            **bounded,
        )
    except Exception:
        logger.debug("watchdog settings load failed — using defaults", exc_info=True)
        return WatchdogSettings()


# How often a WORKING-verdict deferral is logged (evidence trail without spam).
_WORKING_LOG_INTERVAL_SECS = 600.0
# Idle ceiling past which a WORKING deferral stops being routine. Below it, a
# deferral is the expected shape of a long build and logs at INFO. Past it the
# deferral is on course to consume the whole turn budget, so it logs at WARNING
# — the default ``agent.log_level``, without which the one decision that can
# hold a turn silent until its ceiling leaves no trace in production logs. The
# rate limit above still applies, so escalation does not become a spam source.
_WORKING_WARN_AFTER_SECS = 1800.0
# The same mark as a fraction of the turn's own deadline, so escalation still
# happens with room to spare on a turn shorter than the default: the effective
# threshold is whichever of the two is lower.
_WORKING_WARN_DEADLINE_FRACTION = 0.25
# "No deferral logged yet" marker for the rate-limit clock. It cannot be 0.0:
# ``time.monotonic()`` counts from boot on Linux, so on a host up for less than
# the interval above, 0.0 reads as "logged moments ago" and swallows the very
# first deferral line — exactly the evidence a freshly restarted gateway needs.
_WORKING_NEVER_LOGGED = float("-inf")


def _watchdog_evidence_class(evidence: str) -> str:
    """Bucket a free-form oracle evidence string into a closed enum.

    OTel attribute values MUST be low-cardinality (metrics/schema.py): the raw
    evidence carries pids, byte deltas, and command fragments, so only its
    SHAPE is emitted. Buckets: ``established_flat`` (LLM-shaped — runtime-held
    backend socket, flat subtree), ``mcp_flat`` (opaque MCP tool, moving or
    flat), ``shell_absent`` (shell tool in flight with nothing this dispatch
    could have started still running), ``shell`` (other shell-child evidence),
    ``remote_flat`` (opaque MCP tool, flat subtree, the tool's own MCP
    server holding an established TCP connection — a tool blocked on its own
    remote call), ``wait`` (the declared-duration wait tool), ``platform_limited`` (the
    oracle had no platform evidence to sharpen the verdict — a live-but-flat
    shell child on macOS, any tree probe on Windows), ``degraded`` (everything
    else: sampling baseline, unreadable /proc, no pid, oracle error — the
    oracle could not attest either way).
    """
    e = evidence or ""
    if e.startswith(EVIDENCE_ESTABLISHED_FLAT):
        return "established_flat"
    if e.startswith(EVIDENCE_SHELL_CHILD_ABSENT):
        # Checked before the "shell child" substring below, which its evidence
        # text also contains.
        return "shell_absent"
    if e.startswith(EVIDENCE_PLATFORM_LIMITED):
        # Same ordering reason: its text names the shell child / mcp subtree.
        return "platform_limited"
    if e.startswith(EVIDENCE_REMOTE_FLAT):
        # Same ordering reason: its text names the mcp subtree.
        return "remote_flat"
    if "mcp subtree" in e:
        return "mcp_flat"
    if "shell child" in e:
        return "shell"
    if e.startswith("wait tool"):
        return "wait"
    return "degraded"


# ── Turn park state (readable from OUTSIDE the turn) ──


def parked_for_secs(self: AcpSessionHandle) -> float:
    """Seconds the consumer has been holding the current event; 0.0 if not parked.

    This is the one signal the in-band watchdog structurally cannot report on
    itself. That watchdog is the ``except asyncio.TimeoutError`` arm of
    :meth:`_dispatch_events`, an async generator, so it only advances when a
    consumer pulls it — a consumer that awaits inside its own ``async for``
    body freezes the generator at the yield and the arm never executes again
    for the rest of the turn. It is not slow or mis-configured there; it is
    not called. An observer with its own timer reads this instead.
    """
    since = self._parked_since
    if since is None:
        return 0.0
    # Clamped: a monotonic clock cannot go backwards, but a negative duration
    # leaking into a caller's threshold comparison would read as "not parked".
    return max(0.0, time.monotonic() - since)


def _end_human_wait(self: AcpSessionHandle) -> None:
    """Close the human-wait segment of the current park.

    The consumer is still parked when a permission is answered — it resolves
    the approval and then finishes its own branch (an IM send, a hook, a
    transcript write) before coming back for the next event. Banking the wait
    into ``_parked_total`` and restarting ``_parked_since`` keeps the in-band
    correction exact (it wants the WHOLE park, all of which was consumer time
    from the runtime's point of view) while making ``parked_for_secs()``
    measure only what the consumer itself has spent since the answer.

    Without this the observer reports a park whose duration is almost
    entirely the human's thinking time — the same misattribution the in-band
    clocks were fixed to avoid, reappearing one layer out.
    """
    self._awaiting_permission = False
    if self._parked_since is not None:
        now = time.monotonic()
        self._parked_total += max(0.0, now - self._parked_since)
        self._parked_since = now


def note_keepalive(self: AcpSessionHandle) -> None:
    """Record a ``/api/session-keepalive`` touch addressed to this session.

    A blocking MCP tool on this session (``wait``, ``spawn_sub_agents``)
    sends no frame while it runs, so this is the only sign it is alive.
    The runtime's ``_last_activity`` is process-wide and the tool-idle clock
    deliberately ignores it (a co-tenant's traffic must not defer this
    session's stall), so the touch is stamped here, where the clock reads it.
    """
    now = time.monotonic()
    since = self._parked_since
    self._keepalive_ts = now
    # The park baseline at the touch, including a park still in progress:
    # a touch can land while the consumer holds an event, and
    # `_parked_total` banks a park only when it ends.
    self._keepalive_parked = self._parked_total + (
        max(0.0, now - since) if since is not None else 0.0
    )


# ── Responsiveness ──


def _runtime_idle_secs(self: AcpSessionHandle) -> float:
    """Seconds since the runtime last moved a frame on ANY of its sessions.

    The only place the runtime's activity clock is read outside the dispatch
    loop's own per-session bookkeeping. A caller must NOT read the result as
    this session's idleness: on a shared process a co-tenant's traffic keeps
    it near zero while this session has produced nothing.
    """
    return time.monotonic() - self._runtime._last_activity


def is_responsive(self: AcpSessionHandle, stale_threshold: float = 600.0) -> bool:
    """True if runtime is alive AND has had activity within threshold seconds."""
    if not self._runtime.is_alive():
        return False
    return self._runtime_idle_secs() < stale_threshold


def rebind_watchdog(
    self: AcpSessionHandle, crew_agent: str, settings: WatchdogSettings | None = None
) -> None:
    """Re-snapshot the watchdog windows for a new canonical crew identity.

    Called on warm-pool rekey: the pooled runtime was spawned before any
    crew claimed it, so the construction-time snapshot cannot know the
    claiming crew's ``watchdog_tool_stall_*`` overrides — the identity
    travels with the SESSION, not the pool key. The dispatch loop reads
    ``self._watchdog`` on every tick, so the swap takes effect at the next
    watchdog check; an empty ``crew_agent`` (a claim with no crew) rebinds
    to the globals so a recycled runtime never carries a previous crew's
    windows. The oracle keeps its per-session evidence state — only its
    sampling floor follows the new snapshot.

    ``settings`` is the pre-resolved snapshot: an ASYNC caller (the
    warm-pool claim) resolves it off-loop and hands it in, making the
    no-event-loop-I/O property an explicit data dependency rather than a
    cache-timing contract; None loads synchronously (config-cache hit in
    practice) for callers without an off-loop path.
    """
    self._crew_agent = crew_agent
    self._watchdog = settings if settings is not None else _load_watchdog_settings(crew_agent)
    self._oracle._sample_min_secs = self._watchdog.wellness_sample_secs


def _retire_liveness_state(self: AcpSessionHandle) -> None:
    """Release the tracked consult and swap in a fresh, configured oracle.

    Both boundaries that drop the evidence baseline — turn start in
    ``prompt()`` and each new tool dispatch — retire rather than
    ``reset()``, because ``_consult_oracle_offloaded`` submits a BOUND oracle
    method to ``subprocess_executor()``: a walk whose await already timed out
    keeps running and keeps a reference to the instance it was handed. Samples
    are keyed ``"io"``/``"cpu"`` with no PID, so clearing in place lets that
    late writer repopulate the baseline the next generation reads, and since
    any nonzero delta counts as movement a flat tick then reads WORKING.

    The tool path has a second, sharper version of the same hazard that the
    capture path does not: ``_check_shell_child`` matches a descendant against
    the *dispatched* command and stores it as ``_tracked_child`` for exact
    exit detection on later ticks. A walk carrying the PREVIOUS tool's
    ``ToolCallState`` therefore writes a child of the previous command into
    the live oracle, and the new tool's next tick reports
    ``WORKING "shell child N alive"`` on a process that has nothing to do with
    it. Retiring confines both writes to an instance nobody reads.

    Retirement is not a semantic change for the cross-tick tracked-child
    contract itself: ``fresh()`` starts with exactly the state ``reset()``
    produced (no tracked child, no grace timestamp, no samples), and the
    consult resolves ``self._oracle`` at submission, so every tick after the
    boundary binds and accumulates on the new instance the way it did before.

    The future must be retired TOGETHER with the oracle. Replacing only the
    oracle would leave a walk wedged in the previous generation answering
    every later tick "prior consult still in flight", so the new generation
    would never sample its own process — the tool branch would then run on
    UNKNOWN and end a healthy tool call at the suspect window.

    Releasing the future costs at most one abandoned worker per boundary
    instead of one per tick. ``fresh()`` rather than a default construction so
    the per-session ``wellness_sample_secs`` (and an injected /proc root or
    clock in tests) survives the swap.
    """
    prior_consult = self._consult_future
    self._consult_future = None
    if prior_consult is not None:
        if prior_consult.done():
            _consume_future_exception(prior_consult)
        else:
            prior_consult.add_done_callback(_consume_future_exception)
    self._oracle = self._oracle.fresh()


async def _consult_oracle_offloaded(self: AcpSessionHandle, *, model_wait: bool) -> tuple[str, str]:
    """Oracle verdict, offloaded off the event loop.

    The oracle's evidence gathering is a synchronous /proc filesystem walk
    (``iter_descendants`` + per-descendant reads + ``os.readlink`` on
    ``/proc/<pid>/fd/*``, which can block on a wedged fd), so it runs on
    ``subprocess_executor()`` — same treatment as the runtime's RSS probe —
    bounded so a hung /proc read can't wedge the watchdog itself. Any
    failure degrades to UNKNOWN, never to a kill.

    A timed-out await does not stop its executor thread. The one-outstanding-
    walk bound (otherwise a permanently wedged /proc read grows a new blocked
    worker every ``check_after_secs`` and starves the shared pool that
    teardown's ``_get_child_pids`` also draws from), the refused-submission-
    reads-UNKNOWN contract, and exception retrieval all live in the shared
    :func:`consult_offloaded` guard; only which oracle check runs, and
    against which pid and tool state, is decided here.
    """
    pid = getattr(self._runtime, "pid", None)
    call: Callable[..., tuple[str, str]]
    if model_wait:
        call = self._oracle.check_model_wait
        args: tuple[Any, ...] = (pid,)
    else:
        tool = self._inflight_tool
        if tool is None:
            # Resolved before the in-flight guard on purpose: this answer is
            # pure handle state and needs no worker, so a wedged walk must
            # not mask why the tool branch has nothing to check.
            return VERDICT_UNKNOWN, "no in-flight tool state"
        call = self._oracle.check_tool
        args = (pid, tool)

    return await consult_offloaded(
        self,
        call,
        args,
        executor_factory=subprocess_executor,
        log_label="oracle consultation",
    )


def _working_tool_bound_reached(self: AcpSessionHandle, evidence: str, tool_idle: float) -> bool:
    """Whether a tool-branch WORKING reading has run out of forbearance.

    Only the opaque-MCP reading is bounded. It says some process in the
    runtime's tree moved, which a warm sibling MCP server does too, so a
    call whose result frame was lost would otherwise hold the turn to its
    deadline. The bound is ``watchdog.tool_stall_hard_cap_secs``, the same
    ceiling an UNKNOWN reading gets, measured on the session's own frame
    clock. A matched shell child, the declared-duration ``wait`` verdict
    and a kirocrew-core tool that pings the session keepalive
    (:meth:`ToolCallState.is_trusted_keepalive_tool`) keep deferring.
    """
    if not evidence.startswith(EVIDENCE_MCP_SUBTREE_ACTIVE):
        return False
    tool = self._inflight_tool
    if tool is not None and tool.is_trusted_keepalive_tool():
        return False
    return tool_idle > self._watchdog.tool_stall_hard_cap_secs


def _log_working_deferral(
    self: AcpSessionHandle, idle: float, evidence: str, turn_timeout: float
) -> None:
    """Evidence trail for a WORKING deferral, rate-limited to one line per
    interval so a 40-minute build doesn't spam the journal.

    Escalates to WARNING once idle passes the lower of
    :data:`_WORKING_WARN_AFTER_SECS` and
    :data:`_WORKING_WARN_DEADLINE_FRACTION` of this turn's own deadline, so a
    deferral long enough to matter is visible at the default log level on a
    short turn as well as a default-length one.
    """
    now = time.monotonic()
    if now - self._working_logged_ts < _WORKING_LOG_INTERVAL_SECS:
        return
    self._working_logged_ts = now
    warn_after = min(_WORKING_WARN_AFTER_SECS, turn_timeout * _WORKING_WARN_DEADLINE_FRACTION)
    logger.log(
        logging.WARNING if idle >= warn_after else logging.INFO,
        "Watchdog deferral on session %s: idle %.0fs but verdict WORKING (%s)",
        self._session_id,
        idle,
        evidence,
    )
    # Telemetry rides the same rate limit as the log line: one deferral
    # point per interval per session, so an hours-long WORKING build contributes a
    # bounded handful of points instead of one per 5s dispatch tick.
    self._emit_watchdog_metric("deferral", VERDICT_WORKING, evidence, idle)


def _emit_watchdog_metric(
    self: AcpSessionHandle,
    action: str,
    verdict: str,
    evidence: str,
    idle: float,
    *,
    window: str = "standard",
) -> None:
    """Emit kirocrew.watchdog.action + kirocrew.watchdog.idle.duration (best-effort).

    One counter point + one histogram point per watchdog DECISION —
    ``deferral`` (WORKING, rate-limited via _log_working_deferral),
    ``probe`` (the non-lethal session/cancel stale probe), and ``cancel``
    (tool-stall recovery via _end_stalled_tool). Attrs are all closed
    enums (metrics/schema.py cardinality rule): the free-form evidence is
    bucketed by :func:`_watchdog_evidence_class`; ``window`` is one of:
    "standard" (default), "narrowed" (a tool-branch tag reduces the
    build-scale suspect window — established_flat to the model-silent budget,
    shell_child_absent and a prompt-shaped command's platform_limited to the
    ordinary silence window, remote_flat to the opt-in remote-call budget),
    "extended" (model-wait established_flat extends the 600s stale window to the
    model-silent probe window for a non-streamed server-side think), or
    "working_cap" (an opaque-MCP WORKING reading ran past the hard cap).
    ``agent_override`` is the per-agent-override BOOLEAN from the settings
    snapshot — deliberately NOT the agent name (per-agent joins happen via
    the always-on token row store, not OTel attrs). Failures never reach
    the dispatch loop.
    """
    try:
        # circular import: importing get_recorder at module top would form
        # config.loader -> ... -> acp.client -> metrics.provider ->
        # config.loader (provider reads KiroCrewConfig). Keep it lazy so
        # provider is never loaded during config.loader's import chain
        # (mirrors AcpClient.ensure_ready's emit).
        from kiro_crew.metrics.provider import get_recorder

        attrs: dict[str, str | int | bool | float] = {
            "action": action,
            "verdict": verdict,
            "evidence_class": _watchdog_evidence_class(evidence),
            "window": window,
            "agent_override": bool(self._watchdog.agent_override),
        }
        rec = get_recorder()
        rec.counter("kirocrew.watchdog.action", attrs=attrs)
        # ms, like every other kirocrew duration histogram: the dashboard's
        # generic aggregation reports a histogram under *_ms keys unless its
        # emitting module declares a non-millisecond unit for it, and this
        # one declares none, so a seconds-unit value would render 1000x off.
        rec.histogram(
            "kirocrew.watchdog.idle.duration",
            float(idle) * 1000.0,
            unit="ms",
            attrs={"action": action, "evidence_class": attrs["evidence_class"]},
        )
    except Exception:  # telemetry must never break the watchdog
        logger.debug("watchdog metric emit failed", exc_info=True)


async def _end_stalled_tool(
    self: AcpSessionHandle,
    verdict: str,
    evidence: str,
    idle: float,
    *,
    status: StructuredStatus | None = None,
) -> AsyncIterator[AcpEvent]:
    """Cancel THIS session and end the turn with the tool-stall stop reason.

    Session-scoped recovery on a SHARED runtime: never kill the process
    (co-tenant sessions would die) — session/cancel drops only this
    session's in-flight prompt. Bounded (5s) so an unresponsive runtime
    can't turn stall recovery into a second stall. The terminal event
    carries the tool title / redacted command / evidence so chat_runner's
    dedicated recovery can build a targeted continue-nudge (with log-file
    hint and, for STUCK_INPUT, the re-run-non-interactively advice)
    instead of blindly re-running the original user message. ``status`` is
    the ``waiting_input`` classification when the stall was one (RFC
    §14.6); it rides the terminal as ``AcpEvent.status`` so a consumer
    reads ``wait_reason`` / ``safe_retry`` from a typed field instead of
    parsing the evidence text.
    """
    tool = self._inflight_tool
    logger.warning(
        "Tool stall on session %s (idle %.0fs, verdict=%s: %s) — cancelling "
        "session (runtime kept alive for co-tenants)",
        self._session_id,
        idle,
        verdict,
        evidence,
    )
    try:
        await asyncio.wait_for(self.cancel(), timeout=5.0)
    except Exception:
        logger.debug(
            "session/cancel after tool stall failed for %s",
            self._session_id,
            exc_info=True,
        )
    self._turn_done.set()
    yield AcpEvent(
        kind=EVENT_COMPLETE,
        stop_reason=STOP_REASON_TOOL_STALL,
        title=(tool.title if tool else ""),
        tool_input=(tool.command if tool else ""),
        text=f"verdict={verdict}; idle_secs={int(idle)}; {evidence}",
        usage=self.last_prompt_stats.to_turn_usage(),
        status=status,
    )


def _input_wait_status(
    self: AcpSessionHandle, verdict: str, evidence: str
) -> StructuredStatus | None:
    """The ``waiting_input`` status for the current stall, or None.

    A stall is classified as waiting for input on either of two grounds,
    and on nothing else: the oracle's own STUCK_INPUT evidence (Linux: a
    flat subtree with a process blocked reading a tty / stdin pipe), or a
    ``platform_limited`` no-progress verdict on a command the tool layer
    classified as prompt-shaped (``INTERACTIVE_NARROWING_RISKS``) — the
    one case the missing stdin evidence would have answered. A flat
    build with no interactive classification stays an opaque tool stall.

    ``safe_retry`` is True only when the classifier's read-only verdict
    holds AND the call streamed no output: a command that has printed may
    have acted, and a non-interactive retry must never replay a side
    effect (SPEC-ADDENDUM §6). ``cancellable`` is always True (the call
    can be cancelled via ``session/cancel``); ``resumable`` is False (a
    blocked process cannot be resumed with input from here).
    """
    interactive = self._inflight_interactive
    if verdict == VERDICT_STUCK_INPUT:
        grounds = "stuck_input"
    elif (
        evidence.startswith(EVIDENCE_PLATFORM_LIMITED)
        and interactive is not None
        and interactive.risk in INTERACTIVE_NARROWING_RISKS
    ):
        grounds = "platform_limited+interactive_risk"
    else:
        return None
    tool_call_id = self._inflight_tool_call_id
    output_seen = bool(tool_call_id) and tool_call_id in self._tool_output_seen
    safe_retry = bool(interactive and interactive.replay_safe and not output_seen)
    return StructuredStatus(
        session_id=self._session_id,
        tool_call_id=tool_call_id,
        phase=STATUS_PHASE_WAITING,
        wait_reason=WAIT_REASON_INPUT,
        progress_source=PROGRESS_SOURCE_PROCESS_EVIDENCE,
        cancellable=True,
        resumable=False,
        safe_retry=safe_retry,
        origin=STATUS_ORIGIN_LIVENESS_ORACLE,
        evidence=f"{grounds}: {evidence}",
    )
