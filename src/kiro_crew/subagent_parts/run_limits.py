"""The timeouts, limits and wire markers a sub-agent run is held to.

Plain values, read by the manager, the owners and the coordinators through
``kiro_crew.subagent`` (the patch surface: tests set ``kiro_crew.subagent._RESET_TIMEOUT``
and friends there). Two of them are resolved from the environment once, at import.
"""

from __future__ import annotations

import math
import os

from kiro_crew.constants import DEFAULT_SUBAGENT_MAX_TURNS, SUBAGENT_TIMEOUT_SECS
from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_RECHECK_SECS

_MAX_CONCURRENT = 3

# Deepest sub-agent a spawn may create: a root session's child is depth 1, its
# child depth 2, and so on. A spawn whose child would sit deeper is refused at
# admission. Without a cap a recursive chain keeps spawning, and the child
# reserve hands nested rows the last slots, so one runaway tree can hold them.
_MAX_SPAWN_DEPTH = 4


# Width of a run id, in hex characters. 16 characters is 8 bytes, and every one
# of those 64 bits is random, which is what makes a uniqueness CHECK
# unnecessary: 2000 draws on one host collide with probability about
# 2000**2 / (2 * 2**64), roughly 1 in 10**13, against 1 in 2,100 at the 8
# characters this replaces. Nothing in the product pins the width -- the only
# consumers print or pass the id through -- so widening is cheaper than any
# mechanism that would have to remember which ids are taken, and a durable row
# outlives the process that wrote it, so remembering means reading the store.
_RUN_ID_HEX_CHARS = 16


# Wall-clock deadline for one subagent run: the fallback when config is
# unavailable or ``agent.subagent_timeout_secs`` is 0. One owner in
# ``constants`` because the MCP gateway's hard-wedge ceiling has to sit above
# it (see ``mcp_gateway/backend_liveness.py``).
_TIMEOUT_SECS = SUBAGENT_TIMEOUT_SECS


_TURN_LIMIT = DEFAULT_SUBAGENT_MAX_TURNS


# Successor-claim markers: a retry start in flight, and a start that raised
# after it may have accepted its durable row (the successor's id is unknown).
SUCCESSOR_PENDING = "(starting)"


SUCCESSOR_UNKNOWN = "(start outcome unknown)"


#: What the model is told when an unattended subagent run refuses a tool call
#: nothing positively authorizes. The SURFACE refuses the call -- nothing about
#: the call itself was judged -- so the reason names every tier that could
#: still have authorized it, and the surface-policy notice tells the model to
#: read exactly that. Resolved by ``subagent_manager/run.py`` through
#: ``bind_component_globals``.
_HEADLESS_DENY_REASON = (
    "this subagent run is unattended: no approval handler is attached, the "
    "parent did not set parent_policy=auto, and nothing positively authorizes "
    "this call, so only calls this run authorizes on its own can run here -- "
    "tools listed in hooks.auto_approve_tools, and calls the hook gate "
    "classifies as read-only"
)


#: The unattended run's fail-closed answer to a backend child's request whose
#: security context is absent (``AcpEvent.child_low_fidelity``): only the
#: agent-authored title describes it, so no gate here can judge it and no
#: approver is attached to be asked.
_LOW_FIDELITY_DENY_REASON = (
    "this subagent run is unattended and the child request carried no "
    "verifiable security context -- its structured parameters are missing, so "
    "only the agent-authored title describes it -- and nothing here can judge or "
    "approve such a call"
)


_REAPER_INTERVAL = 60  # seconds between reaper sweeps


# Idle TTL for continuable conversations (keep=True): a conversation with no
# run for this long has its session files + map entry deleted by the reaper.
# Hibernated conversations cost a JSON file, not RSS, so this is generous.
_CONVERSATION_TTL_SECS = 6 * 3600


# Startup grace for spawn_steer: how long a steer on a live run
# waits for its session to register before returning the typed
# ``session_starting`` refusal, and the poll cadence within that window.
_STEER_STARTUP_WAIT_SECS = 15.0


_STEER_STARTUP_POLL_SECS = 0.5


# Wave liveness backstop: a wave with lost submissions (submitted < expected,
# all registered members terminal, nothing queued) is force-reconciled after
# this many seconds without submission progress, so held digest results can
# never strand indefinitely.
# Deliberately generous — 30 min, symmetric with the per-agent hard ceiling:
# nothing else waits on this timer (it only fires when zero members run, zero
# are queued, and submissions stopped arriving), and layers 1+2 (the counted
# marker + the /api/spawn/lost reconcile) catch nearly every loss immediately;
# this sweep exists solely for the double-transport-failure tail, where extra
# latency is irrelevant next to permanent wedging.
_WAVE_STUCK_SECS = 1800


_RESET_TIMEOUT = 30.0  # max seconds for session reset in finally block


# How long past ``_RESET_TIMEOUT`` the run's terminal report waits for its
# teardown to decide the kill before publishing. The reset half of the
# teardown is bounded by ``_RESET_TIMEOUT``; the kill half (the fallback's
# executor hops, sequential over every handle) is not, so this is a grace,
# not a bound on the teardown -- and a report whose wait runs out publishes
# the record with the kill named UNDECIDED, never clean (``_report_terminal``).
_TEARDOWN_REPORT_GRACE = 30.0


_RECOVERY_SLOT_WAIT_SECS = 60.0


# A start admitted at the shared price that turns dedicated re-checks the
# memory floor before launching its process; below it, it waits at most this
# long (re-reading every ``_DEDICATED_TOPUP_POLL_SECS``) and then starts anyway
# with a warning: an admitted run is never failed on capacity.
_DEDICATED_TOPUP_WAIT_SECS = 60.0


_DEDICATED_TOPUP_POLL_SECS = 2.0


# The most one root start waits on the macOS kernel memory-pressure hold with no
# root start in its lane beginning, clocked from the later of its first hold and
# its lane's last root start, is ``agent.subagent_queue_max_wait_secs`` (read
# live off the manager, 0 for no bound); past it the start is ended, never
# started.
# A held row's clock older than this many times that bound (the key's default
# when the bound is lower or off) belongs to a row that left without a
# registration or a refusal (cancelled in the store by another process); dropped.
_PRESSURE_HOLD_PRUNE_FACTOR = 4


# The longest gap between two pressure-hold reads still taken as one continuous
# episode: a few recheck intervals, the cadence its timer keeps while it applies.
_PRESSURE_EPISODE_MAX_GAP_SECS = 4.0 * MEMORY_PRESSURE_RECHECK_SECS


#: SEL outcome of a root start ended, never started, because the pressure hold
#: kept it past its bound (or its episode had already outlived the bound).
SEL_MEMORY_PRESSURE_NEVER_STARTED = "never_started_memory_pressure"


# How long one off-loop host-memory read may wait for the shared executor. A read
# that misses it comes back unanswered (``MEMORY_CAUSE_READ_UNANSWERED``) and is
# never retaken on the loop.
_HOST_READ_OFF_LOOP_SECS = 2.0


_REPORT_DRAIN_TIMEOUT = (
    30.0  # max seconds cancel_all() waits for shielded terminal reports to drain
)


# Max seconds a cancelled run holds cancellation open for an in-flight off-loop
# state.json write worker -- every off-loop writer: long enough for any healthy
# fsync, short enough that a wedged FS
# cannot hold cancel_all()'s untimed gather — bounded shutdown plus recoverable
# state beats unbounded shutdown.
_STATE_DRAIN_TIMEOUT = 5.0


# The startup watchdog's window (``SubagentManager._startup_deadline``) covers one
# start clock, which pauses only while the start is QUEUED (see
# ``RunEventCoordinator._gate_exit_reset_impl``) and therefore spans every phase
# that does work: ``_STARTUP_HANDSHAKES`` rounds of process spawn plus
# ``initialize`` and ``session/new`` (or ``session/load``), then the late-start
# collector's wait (timeout plus ``_await_late_start``'s grace), plus a launch
# margin; floor 120.
_STARTUP_TIMEOUT_SECS = 120


# Handshake rounds one start may legitimately run: a retried start runs two (the
# companion spawn's dead-runtime retry, the spawn's one re-derive retry, a resume
# whose runtime died respawning before ``session/new``, a re-projected claim).
_STARTUP_HANDSHAKES = 2


_STARTUP_COLLECT_GRACE_SECS = 5


_STARTUP_LAUNCH_MARGIN_SECS = 30


# Derived in-startup bound, in rounds of the session-start gate: one round
# holding permits plus one round already admitted and waiting behind them. See
# ``SubagentManager._startup_cap`` for why the bound is tied to the gate's
# width and not to the running cap.
_STARTUP_CAP_GATE_ROUNDS = 2


# How often a start released from the spawn-approval prompt re-pumps while it
# waits for the in-startup bound (``_admit_released_start``). A backstop behind
# the edges that pump anyway (PID, first answer, terminal, stagger boundary), so
# it is slow.
_RELEASE_REPUMP_SECS = 1.0


_ON_DONE_TIMEOUT = 1200.0  # outer cap: max total seconds for semaphore wait + injection


# Continuation prompt sent when a transient backend error interrupted a turn
# AFTER output had already streamed. Mirrors the main path's post-token
# CONTINUE recovery: the partial is preserved (result_text keeps
# accumulating), and the model is asked to finish rather than restart.
_TRANSIENT_CONTINUE_MSG = (
    "[system] Your previous response was interrupted by a transient backend "
    "error. The output you already produced was preserved. Continue exactly "
    "where you stopped and finish the task — do not repeat completed work."
)


# Prefix injected on the one-shot auto-continue after an unexpected (non-user)
# cancellation, when the first attempt showed ANY activity (text chunk or tool
# call). Mirrors the main path's cancelled-turn preamble. The respawn
# runs on a FRESH session (the original was reset in the old task's finally),
# so this preamble is the only vehicle for the replay-safety warning: a
# mutating tool may have executed on the first attempt before any text
# streamed, and blindly re-running the bare prompt would re-execute it.
_CANCEL_RESUME_PREFIX = (
    "[system] Your previous attempt at this task was interrupted before "
    "completion (unexpected cancellation). Partial output may have been "
    "recorded, and tools may have ALREADY EXECUTED with side effects (files "
    "written, messages sent, commands run). Verify current state before "
    "repeating any side-effecting action — do not blindly redo work that "
    "already completed. Continue the task and produce a complete result.\n\n"
)


# Inner cap: max seconds for a single injected continuation turn
# (stream_and_collect). When the last spawn_run subagent completes, the gateway
# (`_subagent_done`, slack/gateway_runtime/subagent_wiring.py) injects a continuation
# turn wrapped in ``asyncio.wait_for(..., timeout=INJECTION_TIMEOUT)``. spawn_run-heavy crons
# doing their final synthesis / multi-file apply on that turn were cancelled at
# the old hard 300s cap and the finally block reset the session mid-action.
# Default raised to 900s and made tunable via ``KIROCREW_INJECTION_TIMEOUT``
# (float seconds). It never makes sense for the inner turn cap to exceed the
# outer semaphore-wait+injection cap, so the resolved value is clamped to
# ``_ON_DONE_TIMEOUT``; invalid / non-positive env values fall back to the
# default.
_DEFAULT_INJECTION_TIMEOUT = 900.0


def _env_float(name: str, default: float) -> float:
    """Parse a positive float env override, falling back to ``default``.

    Non-positive or unparseable values return ``default`` (mirrors the
    ``_env_int`` convention in mcp_playwright_proxy.py / pod/config.py).
    """
    try:
        val = float(os.environ.get(name, "") or default)
    except (ValueError, TypeError):
        return default
    return val if val > 0 else default


def _resolve_injection_timeout() -> float:
    """Resolve INJECTION_TIMEOUT from the env, clamped to ``_ON_DONE_TIMEOUT``."""
    val = _env_float("KIROCREW_INJECTION_TIMEOUT", _DEFAULT_INJECTION_TIMEOUT)
    return min(val, _ON_DONE_TIMEOUT)


INJECTION_TIMEOUT = _resolve_injection_timeout()


_STALL_IDLE_SECS = (
    120  # seconds with no stream activity before a running subagent is surfaced as "stalled"
)


# SUPPRESSION CEILING: the multiple of the idle threshold past which a WORKING
# liveness verdict stops holding the "stalled" badge back.
#
# Attribution is not infallible. Under ``agent.session_sharing`` (default true)
# siblings share a runtime pid, so two subagents running similar commands can
# cmdline-match the SAME child process; a genuinely wedged agent can then read
# WORKING for as long as its sibling's child lives. Unbounded, that converts a
# case idle time alone WOULD badge into a permanent false negative --
# suppressing the only user-facing signal is worse than badging a healthy agent,
# because the badge is self-clearing and a missing badge is not. With the ceiling
# a misattribution costs extra latency instead of the signal itself.
_SUPPRESS_CEILING = 4


# Wave-digest HOLD DEADLINE: the maximum time a COMPLETED wave member's result
# may sit undelivered while the gateway waits for the digest chunk to fill.
#
# The chunk-size trigger alone (``SUBAGENT_DIGEST_CHUNK_SIZE``, default 10) is
# a COUNT trigger, and the concurrency cap makes typical waves 2-5 members —
# so the count can never be reached and the only flush that ever fires is the
# wave-close one. Every sibling's result is then withheld for the SLOWEST
# member's entire remaining runtime; a member that HANGS rather than fails
# withholds them for the full ``_TIMEOUT_SECS`` reap, which is
# indistinguishable from a dead session.
#
# This deadline is the latency half of that one-knob-two-jobs split: the count
# trigger keeps bounding digest SIZE for large waves, while the deadline caps
# worst-case delivery LATENCY for every wave size. A wave whose members all
# finish within the deadline of each other still delivers ONE consolidated
# digest — the deliberate small-wave behavior is unchanged.
#
# Tunable via ``KIROCREW_SUBAGENT_DIGEST_HOLD_SECS``; 0/negative disables the
# deadline (count-trigger-only, i.e. pre-fix behavior). Guarded parse: a
# malformed value must never crash import.
_DEFAULT_DIGEST_HOLD_SECS = 120.0


def _digest_hold_secs() -> float:
    try:
        val = float(os.environ.get("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", ""))
    except (TypeError, ValueError):
        return _DEFAULT_DIGEST_HOLD_SECS
    if math.isnan(val):
        # NaN parses fine but loses every comparison, so it would be neither
        # disabled (``nan <= 0`` is False) nor bounded (``min(nan, x)`` is nan)
        # — the sweep's ``age < DIGEST_HOLD_SECS`` would also be False, forcing
        # a flush on the FIRST hold, and ``int(nan)`` then raises inside digest
        # composition AFTER the hold clocks were cleared and ``flushed`` was
        # advanced. That permanently withholds the very results this deadline
        # exists to release, so NaN is malformed input, not a deadline.
        return _DEFAULT_DIGEST_HOLD_SECS
    if val <= 0:
        return 0.0  # explicit opt-out
    return min(val, float(_TIMEOUT_SECS))


DIGEST_HOLD_SECS = _digest_hold_secs()


# Sweeps that must have measured a live row before it counts as settled (its
# memory already inside the free-memory reading) rather than warming (still
# owing its admitted start price). One reading can land mid-growth (a runtime
# started just before a sweep reads at a fraction of its size); two readings an
# interval apart bound that exposure to one ``_REAPER_INTERVAL``.
_RSS_SAMPLES_TO_SETTLE = 2


# A row whose own session answered this long ago counts as settled even when no
# sweep could measure it: macOS and Windows have no ``/proc`` subtree reading, so
# ``_rss_samples`` never advances there, and the native free-memory reading
# already holds the runtime by then. Two sweep intervals, the same exposure the
# sample count bounds where it can be read.
_SETTLE_AFTER_SECS = 2 * _REAPER_INTERVAL


_SYSTEM_PREFIX = (
    "You are a focused sub-agent. Complete the following task concisely. "
    "Do NOT create other agents. Report your result directly.\n"
    "IMPORTANT: Do NOT narrate your own process, failures, retries, or "
    "orchestration decisions. The user does not care how you got the answer. "
    "Do NOT include [OPTIONS: ...] tags. Do NOT use the AskUserQuestion tool. "
    "Only output meaningful, actionable results. Never output greetings or filler.\n\n"
)
