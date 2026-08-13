"""Subagent orchestration — spawn isolated background agents.

Each subagent gets its own LLM session (via SessionManager) with a
focused system prompt.  Results are announced back to the caller via
a callback.  Max concurrent limit prevents resource exhaustion.

No spawn recursion: subagents cannot spawn other subagents.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol

from kiro_crew.acp.liveness import (
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
    consult_offloaded,
)
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import PROVIDER_LABEL_CLAUDE, PROVIDER_LABEL_DEFAULT
from kiro_crew.executors import run_in_embed_pool

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.providers.base import LLMProvider

from kiro_crew import platform_compat
from kiro_crew.agent_discovery import cached_project_agent_names, list_agents
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import SUBAGENT_COMPLETION_PREFIX
from kiro_crew.context import (
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    ContextBuilder,
    window_for_provider_client,
)
from kiro_crew.context_management import (
    COMPLETION_KEEP_DEFAULT_CHARS,
    apply_completion_keep,
    cap_result_file,
    evict_completed_agents,
)
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    fire_tool_hooks,
    safe_read_file,
)
from kiro_crew.llm_helpers import (
    TRANSIENT_RETRIES,
    acp_error_is_transient,
    transient_retry_delay,
)
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED, emit_counter
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
)
from kiro_crew.resource_status import cached_admission_check
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import SessionManager
from kiro_crew.session_surface import has_dashboard_surface
from kiro_crew.session_workspace import result_path as _ws_result_path
from kiro_crew.slack.format import extract_options
from kiro_crew.stats import Stats
from kiro_crew.subagent_completion_meta import (
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    single_completion_meta,
)
from kiro_crew.subagent_cost import (
    append_cost_sample,
    compact_cost_log,
    read_learned_cost,
)
from kiro_crew.subagent_persistence import (
    _agent_dir,
    _cleanup_session_files_sync,
    _subagents_dir,
    agent_dir_for_display,
    clear_tombstone,
    create_agent_folder,
    list_orphans,
    mark_delivered,
    prune_stale_tombstones,
    read_state,
    record_slow_command,
    update_state,
    write_result_chunk,
    write_tombstone,
)
from kiro_crew.validation import _AGENT_NAME_RE

# Standalone ClaudeCodeProvider removed (KiroACP-only). Name kept as None so the
# legacy isinstance guards short-circuit; the claude-agent-acp seam lives in
# providers.acp.is_claude_backend.
ClaudeCodeProvider = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


_background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks


def _safe_fire(coro: Awaitable[None]) -> None:
    """Schedule a coroutine, preventing GC and logging failures."""

    async def _wrap() -> None:
        try:
            await coro
        except Exception:
            logger.warning("Subagent callback failed", exc_info=True)

    task = asyncio.ensure_future(_wrap())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


_MAX_CONCURRENT = 3


def _validate_agent(requested: str, project_dir: str = "") -> tuple[str, str]:
    """Validate that an agent name is one kiro-cli can actually load.

    Runs ON the event loop (``spawn`` is synchronous), so it must not add
    filesystem work. The user-level ``list_agents()`` scan here is pre-existing —
    callers that can validate off-loop skip it via ``_agent_prevalidated`` — and
    this deliberately does NOT widen it: the project scope is read from
    ``cached_project_agent_names()``, which performs no syscalls at all.

    Consequence, stated plainly: a project agent is accepted only once that
    project's cache is warm (any session that has already resolved bindings for it
    has warmed it). A cold cache means the name is reported unknown, which is
    fail-closed and matches this function's existing rule — refusing an unknown
    name rather than silently running the default agent, which would be a
    privilege escalation. Widening the on-loop scan to a second directory instead
    would stall the gateway on a slow or network checkout.

    *project_dir* must be the cwd the subagent will actually run in, because that
    is what kiro-cli resolves ``--agent`` against.

    Returns (agent_name, error). If agent found, error is empty.
    If not found, agent_name is empty and error explains what happened.
    """
    if not requested:
        return "", ""
    known = {a.name for a in list_agents()}
    if project_dir:
        known |= set(cached_project_agent_names(project_dir) or frozenset())
    if requested in known:
        return requested, ""
    available = sorted(known - {"kirocrew", "kirocrew-conductor"})
    # REFUSE a named-but-unknown agent rather than silently falling back to the
    # host default: that fallback runs the full default agent (frequently at
    # approval_mode="auto"), so a typo'd — or malicious — agent name was a silent
    # privilege escalation at the manager primitive. An EMPTY request still means
    # "use the default" (handled above); only a named agent that does not exist
    # is rejected, so a future caller cannot reintroduce the escalation.
    logger.warning("Agent %r not found; refusing spawn. Available: %s", requested, available)
    return "", f"agent {requested!r} not found"


def _vet_spawn_governance(parent_session_key: str, agent: str, app: str = "") -> str | None:
    """Return a denial reason if governance forbids spawning, else None.

    ``app`` binds the calling app's OWN profile (precedence #1 in
    ``resolve_active_scope``): an app spawning through the SpawnSDK must be
    contained by a profile written for that app, which is skipped entirely when
    the app identity is not threaded here — the Level-2 (PROFILE) half of the
    check would then never run and only the policy ceiling would apply.

    Two checks against the parent surface's ceiling ∩ profile:
    1. ``capabilities.spawn`` must be enabled.
    2. if enabled with an ``agents`` scope, the target *agent* must be permitted.

    Best-effort beyond the always-on guards: a ``PlatformCompositionError``
    propagates (fail-closed CPP); any other error returns a denial reason
    (fail-closed) rather than None/no-opinion.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # Gate enabled?  (item ignored when no inner scope — checks ``enabled``.)
        gate = governance_permits("capabilities.spawn", "", session_key=parent_session_key, app=app)
        if not getattr(gate, "permitted", True):
            return getattr(gate, "reason", "spawn capability disabled")
        # Agent-scope check (capabilities.spawn.scopes.agents).
        if agent:
            scoped = governance_permits(
                "capabilities.spawn",
                f"agents:{agent}",
                session_key=parent_session_key,
                app=app,
            )
            if not getattr(scoped, "permitted", True):
                return f"agent {agent!r} not permitted by spawn policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED: a governance evaluation error must DENY the
        # spawn, not silently permit it (previously returned None = no opinion =
        # allow).  PlatformCompositionError already propagates above; every other
        # error lands here and is audited before denial.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "subagent_spawn",
                session_key=parent_session_key,
                scope="capabilities.spawn",
                failed_closed=True,
            )
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return "subagent spawn denied: governance evaluation failed (fail-closed)"


def _redact(text: str) -> str:
    """Redact credentials and exfiltration URLs from text."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


# Bounds for a rendered exception chain. The rendering reaches a WS frame, a
# tombstone and the Subagents panel, so it is capped rather than trusted.
_MAX_ERROR_DETAIL_LEN = 2_000
_MAX_ERROR_CHAIN = 4


def _describe_exception(exc: BaseException) -> str:
    """Render *exc* as ``Type: message``, following its cause chain.

    A bare ``str(exc)`` drops the class, and for a whole family of failures the
    message alone cannot be attributed to a subsystem: ``bad parameter or other
    API misuse`` is unreadable prose until ``sqlite3.InterfaceError`` names
    what raised it. The module is included for anything outside ``builtins``,
    because the bare class name is frequently just as ambiguous as the message.

    The chain is followed because the outermost exception is often a generic
    wrapper whose ``__cause__`` holds the real fault. ``__context__`` is
    followed only when it was not suppressed, matching how a traceback decides
    the same question, so an unrelated exception that merely happened to be in
    flight is not reported as this one's cause.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(parts) < _MAX_ERROR_CHAIN:
        if id(current) in seen:
            break
        seen.add(id(current))
        cls = type(current)
        name = cls.__qualname__
        module = getattr(cls, "__module__", "")
        if module and module != "builtins":
            name = f"{module}.{name}"
        message = str(current).strip()
        parts.append(f"{name}: {message}" if message else name)
        nxt = current.__cause__
        if nxt is None and not current.__suppress_context__:
            nxt = current.__context__
        current = nxt
    return " <- caused by ".join(parts)[:_MAX_ERROR_DETAIL_LEN]


_MAX_DONE_RESULT_LEN = 50_000  # cap subagent_done payload to avoid bloating WS frames


def _done_result(text: str) -> str:
    """Redact + cap result for inclusion in subagent_done event."""
    if not text:
        return ""
    redacted = _redact(text)
    if len(redacted) <= _MAX_DONE_RESULT_LEN:
        return redacted
    return "…(truncated)\n" + redacted[-_MAX_DONE_RESULT_LEN:]


_TIMEOUT_SECS = 1800  # 30 minutes
_TURN_LIMIT = 100
_REAPER_INTERVAL = 60  # seconds between reaper sweeps
# Idle TTL for continuable conversations (keep=True): a conversation with no
# run for this long has its session files + map entry deleted by the reaper.
# Hibernated conversations cost a JSON file, not RSS, so this is generous.
_CONVERSATION_TTL_SECS = 6 * 3600
# Startup grace for spawn_steer (#1113): how long a steer on a live run
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
_RECOVERY_SLOT_WAIT_SECS = 60.0
_REPORT_DRAIN_TIMEOUT = (
    30.0  # max seconds cancel_all() waits for shielded terminal reports to drain
)
_STARTUP_TIMEOUT_SECS = 120  # max seconds a subagent may sit pre-first-turn with no runtime before the startup watchdog reaps it
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
# (slack/gateway.py `_subagent_done`) injects a continuation turn wrapped in
# ``asyncio.wait_for(..., timeout=INJECTION_TIMEOUT)``. spawn_run-heavy crons
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


def _subagent_default_model() -> str:
    """Explicit sub-agent model pin (``agent.role_models['subagent']``), or ``""``.

    Returns ``""`` when the sub-agent role is unpinned so the caller OMITS the
    model kwarg and keeps deferring to the provider's configured default —
    rather than forcing the chat default on as an explicit override (which also
    breaks callers/mocks that don't expect the kwarg). Only a deliberate pin
    overrides. Never raises.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig, normalize_agent_model

        return normalize_agent_model(KiroCrewConfig.load().agent.role_models.get("subagent", ""))
    except Exception:
        return ""


def _subagent_default_effort() -> str:
    """Explicit sub-agent effort pin (``agent.role_efforts['subagent']``), or ``""``.

    Returns ``""`` when unpinned so the caller omits ``reasoning_effort_override``
    and the factory's default effort applies. Only a deliberate pin overrides.
    Never raises.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        val = KiroCrewConfig.load().agent.role_efforts.get("subagent", "")
        return val if isinstance(val, str) else ""
    except Exception:
        return ""


_STALL_IDLE_SECS = (
    120  # seconds with no stream activity before a running subagent is surfaced as "stalled"
)

# SUPPRESSION CEILING: the multiple of the idle threshold past which a WORKING
# liveness verdict may no longer hold the "stalled" badge back.
#
# Attribution is not infallible. Under ``agent.session_sharing`` (default true)
# siblings share a runtime pid, so two subagents running similar commands can
# cmdline-match the SAME child process; a genuinely wedged agent can then read
# WORKING for as long as its sibling's child lives. Unbounded, that converts a
# case the old idle-time-only path DID badge into a permanent false negative --
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
# withholds them for the full ``_TIMEOUT_SECS`` reap (30 min), which is
# indistinguishable from a dead session (issue #2215).
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


def _timeout_context(info: "SubagentInfo", *, include_elapsed: bool = True, turn_limit: int = 0) -> str:
    """Build a human-readable context string for timeout errors.

    ``turn_limit`` is the resolved effective turn cap (per-spawn override →
    manager default → hardcoded). ``info.max_turns`` alone is only the raw
    per-spawn override, which is 0 when unset and would render a misleading
    ``turn N/0``. When no positive cap is known, the cap is omitted entirely.
    """
    limit = turn_limit or info.max_turns
    parts = [f"turn {info.turns}/{limit}" if limit > 0 else f"turn {info.turns}"]
    if info.last_tool:
        parts.append(f"last tool: {_redact(info.last_tool)}")
    if include_elapsed:
        elapsed = info.elapsed if info.elapsed > 0 else (time.time() - info.started)
        parts.append(f"elapsed: {int(elapsed)}s")
    return " | ".join(parts)


def check_memory_available(min_gb: float = 4.0, path: str = "/proc/meminfo") -> tuple[bool, float]:
    """Check if enough memory is available to spawn a subagent.

    Reads /proc/meminfo MemAvailable via ``safe_read_file`` (hooks.py)
    and compares against *min_gb*.
    Returns (ok, available_gb).  On read failure returns (True, -1.0)
    to avoid blocking spawns on non-Linux systems.
    """
    try:
        text = safe_read_file(path)
    except PermissionError:
        logger.warning("Memory check blocked: sensitive path %s", path)
        return (True, -1.0)
    except OSError:
        return (True, -1.0)
    try:
        for line in text.splitlines():
            if line.startswith("MemAvailable:"):
                kb = int(line.split()[1])
                avail = kb / (1024 * 1024)
                return (avail >= min_gb, round(avail, 2))
    except (ValueError, IndexError):
        return (True, -1.0)
    return (True, -1.0)


# Process-subtree RSS readers (relocated from the upstream mcp_gateway pool,
# which is absent in this fork). Pure-stdlib /proc walkers: on non-Linux hosts
# every /proc access raises OSError and these degrade to -1 / [] gracefully.
_RSS_SUBTREE_MAX_PROCS = 256


def _single_proc_rss_kb(pid: int) -> int:
    """RSS (KiB) of a single ``pid`` from /proc/<pid>/status, or -1."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _proc_children(pid: int) -> list[int]:
    """Direct child PIDs of ``pid`` via /proc/<pid>/task/<tid>/children.

    Uses the kernel-provided children list (CONFIG_PROC_CHILDREN), so no
    ``pgrep``/full-table scan. Returns ``[]`` if the file is unavailable.
    """
    kids: list[int] = []
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return kids
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children", encoding="ascii") as fh:
                kids.extend(int(tok) for tok in fh.read().split())
        except (OSError, ValueError):
            continue
    return kids


def _proc_rss_kb(pid: Optional[int]) -> int:
    """Resident set size (KiB) for ``pid`` **and all its descendants**.

    A subagent's kiro-cli process is frequently a thin launcher whose real
    memory lives in a child process. Counting only ``pid``'s own ``VmRSS``
    under-reports the true footprint, so we sum the whole subtree.

    Returns -1 if ``pid`` is falsy or its own status cannot be read; otherwise
    the summed KiB (descendants that vanish mid-walk are simply skipped, so the
    result degrades gracefully to parent-only when ``children`` is unreadable).
    """
    if not pid:
        return -1
    own = _single_proc_rss_kb(pid)
    if own < 0:
        return -1
    total = own
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _RSS_SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                kb = _single_proc_rss_kb(child)
                if kb > 0:
                    total += kb
                nxt.append(child)
        frontier = nxt
    return total


def _proc_subtree_counts(pid: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    """``(processes, mcp_stubs)`` in ``pid``'s subtree, or ``(None, None)``.

    The companion to :func:`_proc_rss_kb` for the two count columns of the
    Sessions surface: how many processes a runtime carries, and how many of them
    are MCP stubs — matched on ``STUB_MODULE``, the module path the rewriter
    itself puts on the stub launch line.

    ``None`` means UNMEASURABLE, never zero. A host without ``/proc`` cannot walk
    a subtree at all, and rendering that as "0 processes" for a live runtime
    would be a lie — the surface renders ``None`` as an em dash instead.

    Walk order, depth and the ``_RSS_SUBTREE_MAX_PROCS`` ceiling mirror
    ``_proc_rss_kb`` so both readings describe the same set of processes.

    Blocking: reads one ``/proc`` entry per process in the subtree, so it belongs
    on an executor thread, never on the event loop (see ``_reaper_loop``).
    """
    if not pid or not platform_compat.IS_LINUX:
        return (None, None)
    if _single_proc_rss_kb(pid) < 0:
        return (None, None)  # pid gone or /proc unreadable: nothing to attribute
    needles = (STUB_MODULE,)
    procs = 1
    stubs = 1 if platform_compat.process_matches(pid, needles) else 0
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _RSS_SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                procs += 1
                if platform_compat.process_matches(child, needles):
                    stubs += 1
                nxt.append(child)
        frontier = nxt
    return (procs, stubs)


def _attributed_count(
    total: Optional[int], sharers: int, previous: Optional[int]
) -> Optional[int]:
    """One co-tenant's share of a subtree *total*, or *previous* if unmeasured.

    Counts follow the same per-sharer split as the RSS/CPU attribution (see
    ``SubagentManager._live_shared_count``) so every attributed column on a row
    describes the same fraction of the runtime. Two differences follow from a
    count being a whole number:

    * The quotient is rounded to the nearest whole process.
    * A nonzero total never rounds down to zero. "This runtime carries stubs,
      your share is 0" is the reading that would reproduce the original bug in a
      new form, so the floor is 1 whenever anything was counted.

    Passing *previous* through on an unmeasured sweep (``total is None`` — the
    pid died, or the platform has no ``/proc``) keeps the last good reading
    rather than blanking a column mid-run, matching how RSS only writes when its
    own read succeeded.

    NOTE on the divisor: ``_live_shared_count`` counts SUBAGENT tenants. A
    subagent shares its parent session's runtime whenever one is available
    (``_create_shared_session``), and the parent session is not a subagent, so
    with a parent co-tenant the divisor is a lower bound and a task's share is an
    upper bound. That is the divisor RSS and CPU have always used; unifying it is
    a separate change to numbers users already read, not a side effect of adding
    two columns.
    """
    if total is None:
        return previous
    if sharers <= 1 or total <= 0:
        return total
    return max(1, round(total / sharers))


# Legacy hard-coded concurrent cap; also the lower clamp bound for auto-sizing
# so dynamic sizing never regresses below today's behavior.
_LEGACY_DEFAULT_MAX = 3


def _available_memory_gb() -> float:
    """Effective available memory (GB), dispatched per operating system.

    Each OS reports "available" memory through a different, non-portable
    interface, so the probe is a small per-platform branch. Every branch
    returns a best-effort available-GB figure, or ``-1.0`` when this platform
    has no probe yet / the read failed — in which case the caller
    (``compute_max_subagents``) fails open to the legacy default cap.

        • Linux  — ``/proc/meminfo`` ``MemAvailable`` (via ``check_memory_available``),
                   then clamped by cgroup headroom so a container's limit binds.
        • macOS  — reclaimable memory via Mach ``host_statistics64`` (ctypes,
                   in-process, no subprocess); see ``_macos_available_memory_gb``.
                   No cgroups.
        • other  — no probe yet → ``-1.0`` (fail open).

    NOTE (adding a new OS): implement a ``_<os>_available_memory_gb()`` helper
    returning GB or -1.0, add an ``IS_<OS>`` flag to ``platform_compat``, and
    wire one branch below. Keep the -1.0 fail-open contract so an unmeasurable
    host degrades to the safe legacy default rather than over-spawning.
    """
    if platform_compat.IS_LINUX:
        _ok, host_gb = check_memory_available(min_gb=0.0)
        if host_gb <= 0:
            return host_gb  # unreadable → caller fails open
        cg_gb = _cgroup_available_gb()
        if cg_gb < 0:
            return host_gb  # no cgroup cap (unconstrained)
        return min(host_gb, cg_gb)
    if platform_compat.IS_MACOS:
        return _macos_available_memory_gb()
    # Unsupported platform (e.g. Windows): no probe yet → fail open.
    return -1.0


def _macos_vm_reclaimable_pages() -> Optional[int]:
    """Reclaimable memory in **pages** via Mach ``host_statistics64``, or ``None``.

    macOS-only; validated live against ``vm_stat`` on Apple silicon (matches
    within live-fluctuation noise). The Mach call itself lives in
    ``platform_compat.macos_vm_statistics``, so the kernel struct is declared in
    one place; what stays here is this caller's own composition of it.

    Reclaimable ≈ ``free + inactive + speculative + purgeable`` page classes:
    memory that can back a new allocation without swapping (the closest analogue
    to Linux ``MemAvailable``). Wired/active/compressed pages are excluded.
    Returns ``None`` on any failure (non-macOS, ``libSystem`` absent, non-zero
    ``kern_return_t``) so the caller falls back to the legacy default.

    This sum is knowingly looser than ``platform_compat.host_available_mib``,
    which bounds ``inactive`` by ``external_page_count`` and does not re-add
    ``speculative`` (``free_count`` already contains it). The two are not
    interchangeable: tightening this one moves ``compute_max_subagents``, a
    number that is documented and that operators tune against.
    """
    probe = platform_compat.macos_vm_statistics()
    if probe is None:
        return None
    stats, _filled = probe
    return stats.free_count + stats.inactive_count + stats.speculative_count + stats.purgeable_count


def _macos_available_memory_gb() -> float:
    """macOS available-memory probe (GB), or ``-1.0`` on failure.

    Combines the in-process Mach reclaimable-page count
    (``_macos_vm_reclaimable_pages``) with the page size from ``os.sysconf``.
    macOS has no ``/proc/meminfo`` and ``os.sysconf`` exposes only *total*
    physical pages (no ``SC_AVPHYS_PAGES``), so the Mach VM statistics are the
    only cheap, non-blocking source of *available* memory — which the sizing
    formula needs so a memory-pressured Mac is not handed an inflated cap. Any
    read failure returns -1.0 so the caller falls back to the legacy default.
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return -1.0
    if page_size <= 0:
        return -1.0
    pages = _macos_vm_reclaimable_pages()
    if pages is None or pages <= 0:
        return -1.0
    avail_gb = pages * page_size / (1024**3)
    return round(avail_gb, 2) if avail_gb > 0 else -1.0


# Values at/above this are the kernel's "no limit" sentinel (PAGE_COUNTER_MAX).
_CGROUP_UNLIMITED = 1 << 62


def _read_int_file(path: str) -> int | None:
    """Read a single integer from *path*; None on absence/garbage. 'max' → None."""
    try:
        with open(path, encoding="ascii") as fh:
            txt = fh.read().strip()
    except OSError:
        return None
    if txt == "max":  # cgroup v2 unlimited sentinel
        return None
    try:
        return int(txt)
    except ValueError:
        return None


def _cgroup_available_gb() -> float:
    """Container memory headroom (GB) = limit − current, or -1.0 if unlimited/unknown.

    Reads cgroup v2 (``memory.max``/``memory.current``) then v1
    (``memory.limit_in_bytes``/``memory.usage_in_bytes``). A sentinel-large
    limit means unlimited. Returns -1.0 on unconstrained / non-Linux hosts so
    the caller ignores the clamp (``dynamic-subagent-sizing.md`` §9).
    """
    # cgroup v2
    limit = _read_int_file("/sys/fs/cgroup/memory.max")
    if limit is not None:
        if limit >= _CGROUP_UNLIMITED:
            return -1.0
        current = _read_int_file("/sys/fs/cgroup/memory.current") or 0
        return max(0.0, (limit - current) / (1024**3))
    # cgroup v1
    limit = _read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if limit is not None:
        if limit >= _CGROUP_UNLIMITED:
            return -1.0
        current = _read_int_file("/sys/fs/cgroup/memory/memory.usage_in_bytes") or 0
        return max(0.0, (limit - current) / (1024**3))
    return -1.0  # no cgroup memory controller


def compute_max_subagents(cfg: KiroCrewConfig) -> int:
    """Compute the concurrent sub-agent cap from host memory and CPU.

    Memory- and CPU-symmetric: each resource yields a candidate count from a
    buffered budget divided by a per-agent cost, and the tighter one binds.
    The result is clamped to ``[3, hard_cap]`` — never below the legacy
    default (the per-spawn ``spawn_min_memory_gb`` gate is the real-time
    memory guard), never above the absolute ``subagent_auto_max`` (which
    stands in for the unmodeled LLM-provider concurrency limit).

    Per-agent costs come from the learned cost store (``read_learned_cost``);
    when no learned value exists yet, the configured first-boot fallbacks
    (``subagent_cost_gb`` / ``subagent_cpu_cost_cores``) are used. Fails open to
    the legacy default when memory can't be read (e.g. non-Linux hosts).

    See ``dynamic-subagent-sizing.md`` §3.
    """
    agent = cfg.agent
    # Hard floor of 3 (``_LEGACY_DEFAULT_MAX``): the auto-sized cap never drops
    # below today's behavior even if ``subagent_auto_max`` is somehow < 3 (the
    # config loader clamps it up to 3, but defend here too so the runtime cap is
    # guaranteed >= 3). ``subagent_auto_max`` is the upper ceiling.
    hard_cap = max(_LEGACY_DEFAULT_MAX, agent.subagent_auto_max)
    lo = _LEGACY_DEFAULT_MAX

    avail_gb = _available_memory_gb()
    if avail_gb <= 0:
        # Memory unreadable (non-Linux / read error) — fail open.
        logger.info(
            "dynamic subagent cap = %d (memory unreadable; fail-open to legacy default)",
            lo,
        )
        return lo

    buf = 1.0 - agent.subagent_mem_buffer_pct / 100.0
    mem_cost = read_learned_cost("mem_gb") or agent.subagent_cost_gb or 0.5
    cpu_cost = read_learned_cost("cpu_cores") or agent.subagent_cpu_cost_cores or 1.0
    pool_size = cfg.session.pool_size

    mem_term = math.floor((avail_gb * buf - pool_size * mem_cost) / mem_cost)
    cpu_count = os.cpu_count() or 1
    cpu_term = math.floor((cpu_count * buf) / cpu_cost)

    candidate = min(mem_term, cpu_term)
    result = max(lo, min(candidate, hard_cap))

    # Name the active bound for an explainable startup log (§5.2).
    if candidate >= hard_cap:
        reason = "hard_cap"
    elif candidate <= lo:
        reason = "floor"
    elif mem_term <= cpu_term:
        reason = "mem_term"
    else:
        reason = "cpu_term"
    logger.info(
        "dynamic subagent cap = %d (%s; mem_term=%d, cpu_term=%d, floor=%d, hard_cap=%d)",
        result,
        reason,
        mem_term,
        cpu_term,
        lo,
        hard_cap,
    )
    return result


def resolve_max_subagents(cfg: KiroCrewConfig) -> int:
    """Resolve the effective cap: explicit value when > 0, else auto-compute.

    ``agent.max_subagents == 0`` is the "auto" sentinel that triggers
    :func:`compute_max_subagents`. See ``dynamic-subagent-sizing.md`` §5.1.
    """
    try:
        configured = int(cfg.agent.max_subagents)
    except (AttributeError, TypeError, ValueError):
        configured = _LEGACY_DEFAULT_MAX
    if configured > 0:
        # An explicit pin below the legacy floor (1 or 2) would silently disable
        # auto-sizing AND run below today's default; floor it to 3. 0 stays the
        # auto sentinel. The config loader and dashboard API also enforce this;
        # defend here so a directly-constructed config can't drop the runtime cap
        # below the floor.
        return max(configured, _LEGACY_DEFAULT_MAX)
    return compute_max_subagents(cfg)


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_CPU_SUBTREE_MAX_PROCS = 256


def _parse_cpu_jiffies(stat: bytes) -> int:
    """Sum utime+stime (clock ticks) from raw ``/proc/<pid>/stat`` bytes.

    Splits after the final ``)`` so a ``comm`` containing spaces/parens is
    handled. utime/stime are fields 14/15 (1-indexed) → indices 11/12 of the
    post-comm tokens. Returns 0 on any parse error.
    """
    try:
        rparen = stat.rindex(b")")
        fields = stat[rparen + 2 :].split()
        return int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        return 0


def _proc_cpu_jiffies(pid: int) -> int:
    """utime+stime (clock ticks) for a single pid, 0 on error."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return _parse_cpu_jiffies(fh.read())
    except OSError:
        return 0


def _subtree_cpu_jiffies(pid: int) -> int:
    """Sum utime+stime across ``pid`` and its descendants (clock ticks).

    Walks the same kernel children list as ``pool._proc_rss_kb`` so the CPU
    subtree matches the RSS subtree.
    """
    total = _proc_cpu_jiffies(pid)
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _CPU_SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                total += _proc_cpu_jiffies(child)
                nxt.append(child)
        frontier = nxt
    return total


def validate_cwd(cwd: str, allowed_roots: list[str]) -> tuple[str, str]:
    """Validate a caller-supplied ``cwd`` for ``spawn_run``.

    Resolves symlinks and verifies the path is an existing directory under at
    least one entry in ``allowed_roots``. Empty ``allowed_roots`` disables the
    feature — any non-empty ``cwd`` is rejected.

    Args:
        cwd: Caller-supplied absolute path (may contain ``~``).
        allowed_roots: Permitted root paths from config (may contain ``~``).

    Returns:
        ``(resolved_cwd, error)``. On success ``error`` is empty and
        ``resolved_cwd`` is the canonical absolute path (realpath-resolved).
        On failure ``error`` is a reason string and ``resolved_cwd`` is empty.
    """
    if not cwd:
        return ("", "")
    if not allowed_roots:
        return ("", "cwd override is disabled (subagent_cwd_allowed_roots is empty)")
    try:
        expanded = os.path.expanduser(cwd)
        if not os.path.isabs(expanded):
            return ("", "cwd must be an absolute path")
        resolved = os.path.realpath(expanded)
    except (OSError, ValueError) as exc:
        return ("", f"cwd resolution failed: {exc}")
    if not os.path.isdir(resolved):
        return ("", "cwd does not exist or is not a directory")
    resolved_roots = [os.path.realpath(os.path.expanduser(r)) for r in allowed_roots]
    for root in resolved_roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return (resolved, "")
    return ("", f"cwd is not under any allowed root: {allowed_roots}")


_SYSTEM_PREFIX = (
    "You are a focused sub-agent. Complete the following task concisely. "
    "Do NOT create other agents. Report your result directly.\n"
    "IMPORTANT: Do NOT narrate your own process, failures, retries, or "
    "orchestration decisions. The user does not care how you got the answer. "
    "Do NOT include [OPTIONS: ...] tags. Do NOT use the AskUserQuestion tool. "
    "Only output meaningful, actionable results. Never output greetings or filler.\n\n"
)


@dataclass
class SubagentInfo:
    """Metadata for a running subagent."""

    id: str
    task: str
    started: float = field(default_factory=time.time)
    done: bool = False
    # True for the record returned by a spawn that hit the stagger/concurrency
    # gate and was QUEUED instead of started. Such a record is not registered in
    # ``_agents`` and carries no live state — but its ``id`` IS the id the agent
    # will run under once ``_drain_queue`` starts it (pre-assigned at accept
    # time), so callers may print it or hold on to it.
    queued: bool = False
    result: str = ""
    result_path: str = ""
    result_truncated: bool = False  # completion-event copy dropped content → summary+path
    error: str = ""
    parent_session_key: str = ""
    agent: str = ""
    # The app that spawned this child (empty for a non-app spawn). Persisted so
    # the child's per-tool-call gate can resolve the app's Level-2 profile, not
    # just the spawn-time decision — without it the profile constrains only
    # whether the spawn was allowed, and the child's ongoing tool calls run
    # unconstrained by the app scope.
    app: str = ""
    approval_mode: str = ""  # "auto" to skip tool approvals in the subagent session
    silent: bool = False  # suppress completion notification (dashboard + Slack)
    turns: int = 0
    last_tool: str = ""
    tool_count: int = (
        0  # count of observed tool calls (incl. auto-approved); drives running-card progress
    )
    last_activity: float = field(
        default_factory=time.time
    )  # time.time() of last stream event; drives idle-stall detection
    stalled: bool = (
        False  # True while the reaper has flagged this subagent as idle/stalled (UI signal)
    )
    # follow_up delivery mode (spawn_steer mode="follow_up"): messages queued
    # here are NOT injected into the running turn — they are dispatched as ONE
    # continuation on this run's conversation after the run completes, so a
    # correction can wait for the current turn instead of interrupting it
    # mid-execution. Drained by the per-run watcher (_deliver_followups).
    pending_followups: list = field(default_factory=list)
    # True once a followup watcher task is armed for this run (one per run).
    _followup_watcher: bool = False
    _stall_suspect_at: float = (
        0.0  # first reaper sweep that saw the idle threshold exceeded; 2-sweep confirmation (scale dampening)
    )
    _awaiting_approval: bool = (
        False  # True while blocked on a human tool-approval prompt; exempt from idle-stall
    )
    # Attribution snapshot of the tool currently in flight, mirroring what
    # ``AcpSessionHandle`` keeps for the main agent. This is what lets the
    # liveness oracle key evidence to THIS subagent's own child process (by
    # cmdline match) instead of to the whole runtime subtree — which, on a
    # session-shared runtime, is dominated by kiro-cli's own background I/O.
    _inflight_tool: Any = None
    # Per-agent liveness oracle. One instance PER AGENT is required, not one per
    # manager: the oracle keys its counter samples by kind ("io"/"cpu"), not by
    # pid, so a shared instance would let one agent's sample become another's
    # baseline and read as movement. Retired (not cleared) on every new tool
    # dispatch so a walk still running against the previous tool cannot write
    # into the next tool's baseline.
    _stall_oracle: Any = None
    # The in-flight offloaded consult for this agent, if any. Tracked so at most
    # ONE /proc walk per agent is outstanding: a permanently wedged read would
    # otherwise leave a blocked worker behind on every reaper sweep and starve
    # the shared subprocess pool that teardown also draws from. Deliberately NOT
    # cleared when the oracle is retired on a new tool dispatch — dropping the
    # handle would un-bound exactly that growth.
    _consult_future: Any = None
    # Monotonic generation of the attribution snapshot above. Bumped on EVERY
    # retirement (new dispatch, final tool result, fresh stream activity) so an
    # offloaded consult that outlived the tool it was submitted for can be
    # recognised as stale and discarded instead of applied to whatever is in
    # flight now. Without it the ``/proc`` walk's own latency is enough to flag a
    # subagent that resumed work while the walk was still running.
    _stall_gen: int = 0
    # Batch/wave identity: set when this spawn is part of a multi-task wave
    # (spawn_run tasks=[...]) so scale plumbing can digest completions and
    # emit batch lifecycle events. Empty for standalone spawns.
    batch_id: str = ""
    batch_total: int = 0
    # True when this member's per-agent injection was HELD for the wave digest
    # (gateway _subagent_done). The run loop must then SKIP mark_delivered():
    # the result is not yet in the parent's context, and a "delivered"
    # tombstone would exclude it from orphan reconciliation — a gateway
    # restart mid-wave would silently lose every held completion. The gateway
    # marks held members delivered when the digest fires.
    _digest_held: bool = False
    # ``time.time()`` when the gateway HELD this member's delivery for the wave
    # digest; 0.0 once that hold has been flushed (or never held). Separate from
    # ``_digest_held`` on purpose: that flag is the restart-safety contract read
    # by the run loop, and must not be mutated by the hold-deadline sweep. This
    # timestamp is the sweep's only input — see ``_sweep_digest_holds``.
    _digest_held_at: float = 0.0
    # True ONLY for the synthetic record that :meth:`force_digest_flush`
    # announces to release held results whose hold deadline expired. It is NOT a
    # wave member: it carries the wave's ``batch_id`` so the gateway can find
    # the wave's digest buffer, but the gateway must skip every per-member side
    # effect for it (WS terminal event, orchestration tracker accounting,
    # done/ok/err counters, digest lines) and only force the flush.
    _digest_flush_only: bool = False
    # A reap/stop has STARTED but may still be in its (awaiting) teardown. Split
    # out of `reaped` because that flag carries two incompatible meanings: the
    # cancel-recovery scheduler needs it set BEFORE the teardown awaits (or it
    # respawns the run being killed), while `_run`'s error synthesis needs it
    # still False until the reaper actually owns the record (or a run woken by
    # the reaper's session reset skips its own error and reports a FALSE
    # SUCCESS). One flag cannot be both early and late; this one is the early
    # half — "do not respawn, a reap is in flight".
    _reap_started: bool = False
    # Set by the gateway on the wave's FINAL member only: the held OK member
    # ids whose delivery tombstones must be settled once the digest has been
    # successfully handed off (i.e. after _on_done returns without raising —
    # the same contract as the per-agent mark_delivered). Settling these at
    # digest COMPOSITION would re-open the restart-loss window between
    # composing and routing.
    _digest_settle_ids: list[str] = field(default_factory=list)
    max_turns: int = 0
    reaped: bool = False
    streaming_text: str = ""
    elapsed: float = 0.0
    _raw_task: str = ""  # unredacted task for kiro-cli execution prompt
    # CC-specific overrides (ignored for ACP)
    model: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    bare: bool = False
    # Continuable conversations (spawn_run keep=True / spawn_continue):
    # keep=True forces a dedicated (non-shared) session, persists the sid via
    # SessionManager.mark_continuable, and skips session-file deletion at
    # teardown so the conversation can be resumed by a later run.
    keep: bool = False
    # Which switchable context groups this sub-agent inherits from the injected
    # session context. All True ⇒ byte-identical to a non-sub-agent session. A
    # parent opts one out when it can name why this task cannot need it; the
    # sub-agent is told by name what was withheld so it reports the gap instead
    # of guessing. Resolved once at spawn and carried through the queue and
    # retry paths, so a drained or retried run sees the same scope as the run
    # its caller asked for.
    include_memory: bool = True
    include_lessons: bool = True
    include_project: bool = True
    # Session key override for continuation runs: a spawn_continue run reuses
    # the ORIGINAL run's session key (``subagent:<conv-id>``) so get_or_create
    # finds the persisted sid and arms session/load. Empty ⇒ the default
    # ``subagent:{id}``.
    conversation_key: str = ""
    # Optional subprocess cwd override. When set, the subagent kiro-cli/claude-code
    # process launches here instead of the default ``subagent_<id>`` sandbox, so
    # cwd-relative resource globs (``.kiro/steering/**/*.md``, ``AGENTS.md``,
    # ``CLAUDE.md``) resolve against this directory. Validated on spawn against
    # ``AgentConfig.subagent_cwd_allowed_roots``.
    cwd: str = ""
    _pid: int | None = None  # PID of kiro-cli child process, for tombstone diagnostics
    # Wall-clock (time.time) when _run_inner actually began executing. Distinct
    # from ``started`` (set at registration): a subagent may sit in ``_agents``
    # awaiting spawn approval for an arbitrary time before execution begins. The
    # startup watchdog measures from THIS timestamp so it never reaps an agent
    # that is merely waiting for approval. None until execution starts.
    _exec_started: float | None = None
    # Learned-cost high-water marks (dynamic-subagent-sizing.md §4.1), sampled
    # periodically by the reaper loop and folded into the cost store at exit.
    peak_rss_gb: float = 0.0
    peak_cpu_cores: float = 0.0
    # Most-recent sample of the same two signals. The peaks answer "how big can
    # this agent get" (what sizing needs); a live task-manager surface needs "how
    # big is it right now", which a high-water mark cannot express — it never
    # comes back down. Both are written by the same sweep, so exposing the last
    # sample costs no extra syscalls.
    last_rss_gb: float = 0.0
    last_cpu_cores: float = 0.0
    # Live process/MCP-stub counts of this run's subtree, from the same sweep.
    # ``None`` = not measured yet (or unmeasurable on this platform), which the
    # surface must render as an em dash: a live runtime with "0 processes" is a
    # lie, and it is exactly the reading that made subagent rows look like they
    # carried no MCP stubs at all.
    last_procs: int | None = None
    last_stubs: int | None = None
    _cpu_jiffies_prev: int = 0  # last subtree utime+stime sample (clock ticks)
    _cpu_sample_ts: float = 0.0  # monotonic time of the last CPU sample
    # Session sharing — when True, this subagent runs as a session on the
    # parent's shared AcpRuntime instead of its own process. Cleanup skips
    # release/reset (no entry in SessionManager) and instead calls shutdown()
    # on the _shared_provider directly.
    _session_sharing: bool = False
    _shared_provider: Any = None  # AcpSessionProvider when _session_sharing=True
    # ── Turn-resilience state (subagent parity with the main-agent guards) ──
    # True when the user explicitly stopped this agent (DELETE /api/spawn/{id}).
    # Renders as a neutral "stopped" terminal state (not an error) and
    # preserves whatever partial output was streamed.
    user_stopped: bool = False
    # One-shot budget for auto-continue after an UNEXPECTED (non-user, non-
    # shutdown) asyncio cancellation — mirrors the main path's cancel recovery.
    _cancel_retry_used: bool = False
    # True between an unexpected cancellation and the recovery respawn; the
    # _run finally block skips terminal finalization (subagent_done, on_done)
    # while set so the agent is not reported done mid-recovery.
    _recovering: bool = False
    # Ownership token for the one-time TERMINAL REPORT (`subagent_done` +
    # `_on_done`), claimed via `SubagentManager._claim_finalize`.
    _finalized: bool = False
    # Ownership token for the one-time SLOT RELEASE (`_running_count` decrement
    # + queue drain), claimed via `SubagentManager._release_slot`. Separate from
    # both `reaped` and `done`: whichever terminal path arrives first frees the
    # slot exactly once, so neither a reap that loses the report claim nor a
    # `_run` that sees `reaped` can leave `_running_count` inflated. At
    # 60-100 concurrent agents, a leaked slot starves the queue.
    _slot_released: bool = False
    # True once the terminal report's `_on_done` injection has RETURNED, i.e.
    # the outcome actually reached the parent. Distinct from `_finalized` (the
    # claim, taken before delivery is attempted) and from the "delivered"
    # tombstone (written later, after teardown). Read by `cancel_all()` to tell
    # a report cancelled BEFORE delivery — which must be made recoverable on the
    # next start — from one cancelled AFTER it, which must not be re-delivered.
    _reported_to_parent: bool = False

    @property
    def outcome(self) -> str:
        """Canonical three-way terminal outcome: 'stopped' | 'failed' | 'completed'.

        THE single source of truth for terminal-state classification. Consumers
        MUST use this (or the ``outcome`` field carried on every subagent_done
        emission) instead of re-deriving from ``error``-nullability — the
        legacy ``error ? failed : completed`` idiom silently misreports a
        user-stopped agent as completed. ``stopped``/``error`` remain on the
        wire for compatibility.
        """
        if self.user_stopped:
            return "stopped"
        if self.error:
            return "failed"
        return "completed"


# Callback: (subagent_info) -> None
AnnounceCallback = Callable[[SubagentInfo], Awaitable[None]]

# Event callback: (event_type, info, extra_data) -> None
SubagentEventCallback = Callable[[str, "SubagentInfo", dict], Awaitable[None]]


def _context_groups_of(info: "SubagentInfo") -> frozenset[str]:
    """The switchable context groups this run KEEPS.

    One source of truth for the run's scope, shared by the ``build_message``
    call that applies it and the ``state.json`` record a continuation reads it
    back from, so the two cannot drift.
    """
    return frozenset(
        group
        for group, on in (
            (CONTEXT_GROUP_MEMORY, info.include_memory),
            (CONTEXT_GROUP_LESSONS, info.include_lessons),
            (CONTEXT_GROUP_PROJECT, info.include_project),
        )
        if on
    )


def _context_groups_field(info: "SubagentInfo") -> str:
    """``state.json`` encoding of the run's scope: comma-joined, sorted."""
    return ",".join(sorted(_context_groups_of(info)))


class ToolApprovalCallback(Protocol):
    async def __call__(self, event: LLMEvent, parent_session_key: str = "") -> bool:
        pass


class SpawnApprovalCallback(Protocol):
    async def __call__(
        self, request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        pass


class SubagentManager:
    """Spawn and track isolated background agents."""

    def __init__(
        self,
        sessions: SessionManager,
        ctx_builder: ContextBuilder,
        on_done: AnnounceCallback | None = None,
        max_concurrent: int = _MAX_CONCURRENT,
        default_turn_limit: int = _TURN_LIMIT,
        default_timeout: int = _TIMEOUT_SECS,
        startup_timeout: int = _STARTUP_TIMEOUT_SECS,
        stall_idle_secs: int = _STALL_IDLE_SECS,
        on_tool_approval: ToolApprovalCallback | None = None,
        on_tool_approval_factory: (
            Callable[["SubagentInfo"], Callable[[LLMEvent], Awaitable[bool]]] | None
        ) = None,
        on_spawn_approval: SpawnApprovalCallback | None = None,
        is_yolo: Callable[[], bool] | None = None,
        on_event: SubagentEventCallback | None = None,
        on_orphan_notify: Callable[..., Awaitable[bool]] | None = None,
        on_orphan_dm: Callable[[str], Awaitable[bool]] | None = None,
        completion_keep: str = "head",
        completion_keep_chars: int = COMPLETION_KEEP_DEFAULT_CHARS,
    ):
        self._sessions = sessions
        self._ctx_builder = ctx_builder
        self._on_done = on_done
        self._max_concurrent = max_concurrent
        self._default_turn_limit = default_turn_limit
        self._default_timeout = default_timeout if default_timeout > 0 else _TIMEOUT_SECS
        self._startup_deadline = startup_timeout if startup_timeout > 0 else _STARTUP_TIMEOUT_SECS
        self._stall_idle_secs = stall_idle_secs if stall_idle_secs > 0 else _STALL_IDLE_SECS
        self._on_tool_approval = on_tool_approval  # fallback for non-auto sessions
        self._on_tool_approval_factory = on_tool_approval_factory
        self._on_spawn_approval = on_spawn_approval
        self._is_yolo = is_yolo
        self._on_event = on_event
        # Orphan-notification delivery (gateway-wired). ``on_orphan_notify``
        # injects a message into the parent dashboard slot (returns True on
        # success); ``on_orphan_dm`` is the owner-DM / notification fallback.
        self._on_orphan_notify = on_orphan_notify
        self._on_orphan_dm = on_orphan_dm
        # Set by cancel_all() so shutdown-driven task cancellations never
        # trigger the one-shot unexpected-cancel auto-continue.
        self._shutting_down = False
        self._completion_keep = completion_keep
        self._completion_keep_chars = completion_keep_chars
        self._running_count = 0
        # Strong refs to in-flight shielded terminal reports (see
        # `_spawn_terminal_report`); drained in `cancel_all`.
        self._report_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # follow_up watchers (spawn_steer mode="follow_up"), keyed by run id.
        # Manager-OWNED on purpose: these tasks can spawn a brand-new run
        # (continue_conversation), so per this module's containment contract
        # (see _schedule_cancel_recovery) they must be reachable by
        # cancel_all() — a watcher parked in the global _safe_fire set would
        # survive shutdown and dispatch against a closing SessionManager.
        self._followup_watchers: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # task -> the agent whose terminal report it is delivering
        self._report_owners: dict[asyncio.Task, SubagentInfo] = {}  # type: ignore[type-arg]
        self._last_spawn_ts: float = 0.0  # monotonic time of the last actual start (stagger gate)
        self.hook_store: Any = None  # Optional ScriptHookStore, set by server.py
        self._agents: dict[str, SubagentInfo] = {}
        # Continuable conversations: session_key ("subagent:<conv-id>") →
        # last-used unix ts. Drives the reaper's idle-TTL sweep. Rebuilt from
        # state.json (keep=True runs) on the reaper's first pass after a
        # gateway restart (#1114), so promoted conversations stay owned by
        # the TTL sweep across restarts; a spawn_continue on an unknown key
        # also re-registers it on demand.
        self._conversations: dict[str, float] = {}
        self._conv_registry_rebuilt = False
        # state.json is the source of truth for retention (#1115): give the
        # SessionManager's in-memory continuable cache a disk fallback so a
        # cache miss (restart window) cannot demote a promoted conversation.
        try:
            self._sessions.set_continuable_fallback(self._keep_recorded_on_disk)
        except AttributeError:
            pass  # test doubles without the setter
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # Queued spawns store the FULL spawn() kwarg set (not just a 5-tuple), so a
        # drained spawn preserves approval_mode / silent / model / allowed_tools / bare —
        # dropping them made a queued headless/auto spawn hit the deny-by-default gate and
        # a queued silent spawn emit output. See _drain_queue.
        self._queue: list[dict[str, Any]] = []
        # Batch ids whose spawn_batch_started event has already fired.
        self._seen_batches: set[str] = set()
        # Submission accounting per wave: batch_id -> (submitted, expected).
        # Guards the wave digest against firing before every member's POST has
        # arrived — a fast-failing first member must not let the completion
        # fallback see "no pending members" while later submissions are still
        # in flight. Pruned by finalize_batch().
        self._batch_submitted: dict[str, list[int]] = {}
        # Wave liveness: last submission-progress time.time() per batch_id.
        # Drives the reaper's stuck-wave backstop (a wave with lost
        # submissions and no progress is force-reconciled). Pruned by
        # finalize_batch alongside _batch_submitted.
        self._batch_progress_ts: dict[str, float] = {}
        self._reaper_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Cache global approval_mode at init to avoid disk I/O on every
        # parentless spawn (cron, webhooks).
        try:
            self._global_approval_mode = KiroCrewConfig.load().agent.approval_mode
        except Exception:
            logger.warning(
                "Failed to load KiroCrewConfig for approval_mode; defaulting to interactive",
                exc_info=True,
            )
            self._global_approval_mode = ""
        # Retention window (seconds) for a delivered subagent's result.txt before
        # the reaper prunes it — the parent's grace window to read the full
        # transcript (spawn_status / read / grep) after the completion event.
        try:
            self._result_ttl_secs = int(KiroCrewConfig.load().agent.subagent_result_ttl_secs)
        except Exception:
            self._result_ttl_secs = 3600
        # Spawn stagger interval — bounds the cold-start ramp rate so a high cap
        # never bursts (dynamic-subagent-sizing.md §5.3).
        try:
            self._spawn_stagger_secs = max(
                0.0, float(KiroCrewConfig.load().agent.subagent_spawn_stagger_secs)
            )
        except Exception:
            self._spawn_stagger_secs = 2.0

    def _effective_turn_limit(self, info: SubagentInfo) -> int:
        """Resolved turn cap for a run: per-spawn ``max_turns`` → config
        default (``agent.subagent_max_turns``) → hardcoded ``_TURN_LIMIT``.

        ``0`` at any level means "not set" and falls through to the next.
        """
        return info.max_turns or self._default_turn_limit or _TURN_LIMIT

    def update_completion_keep(self, mode: str, max_chars: int) -> None:
        """Update the live completion-keep mode and char budget.

        Called from ``api_kirocrew_config_patch`` after the user changes
        ``agent.completion_keep`` or ``agent.completion_keep_chars`` from
        the Settings UI. The values are read once per subagent at
        completion time (``apply_completion_keep`` call site), so swapping
        them here takes effect for the next subagent to finish — including
        ones already running. No torn-read possible under asyncio: both
        reads happen in the same synchronous block.

        ``mode`` is validated by ``_validated_completion_keep`` at config
        load; this setter is intentionally permissive about ``max_chars``
        so the loader / handler stays the validation choke-point.
        """
        self._completion_keep = mode
        self._completion_keep_chars = max_chars

    @staticmethod
    async def _approve_and_log(
        client,
        request_id: str | int,
        session_key: str,
        event: LLMEvent,
        *,
        metadata: dict | None = None,
        info: "SubagentInfo | None" = None,
    ) -> None:
        await client.approve_tool(request_id)
        # An APPROVED child-origin escalation is side-effect activity: count
        # it in tool_count so the transient-retry / cancel-respawn replay
        # gates see it (an approved child mutation must never be replayed by
        # a bare original prompt). Counted here — on the approval outcome —
        # not at receipt: a purely rejected escalation executed nothing and
        # must not permanently disable the run's replay budget.
        if info is not None and event.sub_session_id:
            info.tool_count += 1
        sel().log_tool_invocation(
            session_key=session_key,
            source="subagent",
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome="auto_approved" if metadata and metadata.get("reason") else "approved",
            request_id=request_id,
            metadata=metadata,
        )

    @staticmethod
    async def _reject_and_log(
        client,
        request_id: str | int,
        session_key: str,
        event: LLMEvent,
        *,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        await client.reject_tool(request_id)
        # getattr: production LLMEvents always carry sub_session_id, but this
        # static helper is also driven with lightweight test doubles.
        if getattr(event, "sub_session_id", ""):
            # Hang-resilience series: backend-child denials on the headless
            # subagent surface (low-fidelity fail-close, escalation/turn-limit
            # bails, interactive rejections). ``reason`` is a closed enum.
            emit_counter(
                CHILD_PERMISSION_DENIED,
                {"surface": "subagent", "reason": error or "rejected"},
            )
        sel().log_tool_invocation(
            session_key=session_key,
            source="subagent",
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome="denied" if error else "rejected",
            request_id=request_id,
            error=error or "",
            metadata=metadata,
        )

    def start_reaper(self) -> None:
        """Start the periodic reaper loop.  Call once after the event loop is running."""
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            # One-shot orphan reconciliation on startup
            self._reconcile_task = asyncio.create_task(self._reconcile_orphans())

    async def _reconcile_orphans(self) -> None:
        """Scan for orphaned agent folders from a prior gateway run.

        For each orphan (folder with state.json but no tombstone.json
        and not tracked in ``_agents``):
        - PID alive → SIGKILL, tombstone (gateway_restart)
        - PID dead + result → tombstone (gateway_restart, delivered)
        - PID dead + no result → tombstone (gateway_restart, notification_pending)
        """
        try:

            orphans = list_orphans()
            if not orphans:
                return
            logger.info("Reconciling %d orphaned subagent(s)", len(orphans))
            processed = 0
            # DM-fallback messages are DIGESTED: collected across the whole
            # scan and delivered as ONE message at the end — a restart with N
            # in-flight agents must never produce N pings. (The session-
            # injection path batches naturally via the parent slot's pending-
            # failures drain.)
            dm_pending: list[str] = []
            for state in orphans:
                agent_id = state.get("id", "")
                if not agent_id or agent_id in self._agents:
                    continue  # tracked in current run, skip
                try:
                    pid = state.get("pid")
                    has_result = False
                    try:

                        rp = _agent_dir(agent_id) / "result.txt"
                        has_result = rp.exists() and rp.stat().st_size > 0
                    except OSError:
                        pass

                    recovery = "undeliverable"
                    if pid and self._is_pid_alive(pid):
                        # Use pid_recorded_at (when PID was actually written) instead of
                        # started (folder creation time) to avoid false negatives under load
                        pid_recorded_at = state.get("pid_recorded_at", state.get("started", 0))
                        if self._is_orphan_process(pid, pid_recorded_at):
                            self._kill_orphan_pid(pid)
                            try:
                                sel().log_tool_invocation(
                                    session_key=f"subagent:{agent_id}",
                                    source="subagent",
                                    tool_name="orphan_reconcile_kill",
                                    outcome="killed",
                                    metadata={"subagent_id": agent_id, "pid": pid},
                                )
                            except Exception:
                                logger.debug("SEL audit failed for orphan %s", agent_id)
                        recovery = "result_available" if has_result else "notification_pending"
                    elif has_result:
                        recovery = "result_available"
                    else:
                        recovery = "notification_pending"

                    try:
                        write_tombstone(
                            agent_id,
                            cause="gateway_restart",
                            recovery_action=recovery,
                            pid=pid,
                            turns=state.get("turns", 0),
                            last_tool=state.get("last_tool", ""),
                        )
                    except Exception:
                        logger.debug("Failed to tombstone orphan %s", agent_id, exc_info=True)

                    # Retain-by-default: session files are deliberately NOT
                    # deleted here — an orphaned run's transcript is still
                    # spawn_continue resume material after the restart. The
                    # tombstone pruner owns deletion (with the keep guard for
                    # promoted conversations).

                    logger.info(
                        "Reconciled orphan %s: recovery=%s, pid=%s, has_result=%s",
                        agent_id,
                        recovery,
                        pid,
                        has_result,
                    )
                    # Notify user about the orphaned agent. Injection happens
                    # per-orphan (it rides the parent slot's batched pending-
                    # failures queue); DM fallback is deferred to the digest.
                    try:
                        undelivered = await self._notify_orphan(
                            agent_id, state, recovery, has_result
                        )
                        if undelivered:
                            dm_pending.append(undelivered)
                    except Exception:
                        logger.debug("Notification failed for orphan %s", agent_id, exc_info=True)
                except Exception:
                    logger.warning("Failed to reconcile orphan %s", agent_id, exc_info=True)

                # Rate limit: yield to event loop every 50 entries
                processed += 1
                if processed % 50 == 0:
                    await asyncio.sleep(0)

            # Single digest for everything the injection path couldn't deliver.
            if dm_pending:
                if len(dm_pending) == 1:
                    digest = dm_pending[0]
                else:
                    digest = (
                        f"[Subagent restart digest] {len(dm_pending)} subagent(s) "
                        f"orphaned by a gateway restart:\n\n" + "\n\n".join(dm_pending)
                    )
                try:
                    await self._send_orphan_slack_dm(digest)
                except Exception:
                    logger.debug("Orphan digest DM failed", exc_info=True)
        except Exception:
            logger.warning("Orphan reconciliation failed", exc_info=True)

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a PID is still running."""
        # os.kill(pid, 0) would terminate the process on Windows — probe instead.
        return platform_compat.pid_exists(pid)

    @staticmethod
    def _is_orphan_process(pid: int, spawned_at: float) -> bool:
        """Check if PID belongs to the original subagent (not a recycled PID).

        Compares /proc/{pid} creation time against the recorded spawn time.
        Returns False if the process was created after the agent was spawned
        (indicating PID reuse).
        """
        try:
            proc_stat = os.stat(f"/proc/{pid}")
            # Process was created before or around the time we spawned the agent
            return proc_stat.st_ctime <= spawned_at + 2.0
        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    def _kill_orphan_pid(pid: int) -> None:
        """Best-effort SIGKILL of an orphaned process."""
        try:
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    async def _notify_orphan(
        self, agent_id: str, state: dict, recovery: str, has_result: bool
    ) -> str | None:
        """Notify user about an orphaned subagent.

        1. Try session injection if parent session still exists (delivered
           messages return ``None``).
        2. Otherwise return the redacted message so the caller can batch all
           undelivered notifications into a SINGLE digest DM — never N pings.
        """
        task_preview = (state.get("task", "") or "")[:100]
        parent_session = state.get("parent_session", "")
        result_path = str(agent_dir_for_display(agent_id) / "result.txt")

        if has_result:
            msg = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{agent_id}` ⚠️ orphaned by gateway restart\n"
                f"Task: {task_preview}\n"
                f"Result saved at: `{result_path}`\n"
                f"Use the read tool to retrieve it."
            )
            # A restart orphan whose result survived on disk: interrupted, not a
            # plain failure. The note is the only explanation the header carries.
            row_meta = single_completion_meta(
                agent_id=agent_id,
                outcome=OUTCOME_INTERRUPTED,
                task=task_preview,
                note="orphaned by gateway restart",
            )
        else:
            msg = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{agent_id}` ❌ lost to gateway restart\n"
                f"Task: {task_preview}\n"
                f"No result was captured before the restart."
            )
            row_meta = single_completion_meta(
                agent_id=agent_id,
                outcome=OUTCOME_FAILED,
                task=task_preview,
                note="lost to gateway restart",
            )

        # Redact before any delivery path (injection or Slack DM)
        msg = _redact(msg)

        # Try session injection first. The question is whether a tab is OPEN to
        # receive the notice, not where the conversation started — a channel-born
        # parent keeps its channel session key while its tab is open, and with no
        # tab the digest DM below is the only surface.
        if has_dashboard_surface(parent_session):
            try:
                injected = await self._try_inject_orphan_notification(parent_session, msg, row_meta)
                if injected:
                    # Update tombstone recovery_action
                    try:
                        write_tombstone(
                            agent_id,
                            cause="gateway_restart",
                            recovery_action="delivered",
                            pid=state.get("pid"),
                            turns=state.get("turns", 0),
                            last_tool=state.get("last_tool", ""),
                        )
                    except Exception:
                        pass
                    return None
            except Exception:
                logger.debug("Injection failed for orphan %s", agent_id, exc_info=True)

        # Undelivered: hand back for the caller's single digest DM.
        return msg

    async def _try_inject_orphan_notification(
        self, parent_session: str, msg: str, meta: dict | None = None
    ) -> bool:
        """Try to inject a message into the parent dashboard session.

        Delegates to the gateway-wired ``on_orphan_notify`` callback, which
        appends the (already-redacted) message to the parent slot's transcript
        and queues it into ``slot._pending_subagent_failures`` so the LLM
        learns about the orphan on its next turn. Returns True if delivered.

        ``meta`` carries the structured completion facts for the dashboard card
        (#1792) so the orphan row renders without re-parsing its prose header.
        """
        if self._on_orphan_notify is None:
            return False
        try:
            delivered = bool(await self._on_orphan_notify(parent_session, msg, meta))
        except Exception:
            logger.debug("on_orphan_notify raised for %s", parent_session, exc_info=True)
            return False
        if delivered:
            try:
                sel().log_api_access(
                    caller=parent_session,
                    operation="subagent.orphan_notification_injected",
                    outcome="ok",
                    source="subagent",
                )
            except Exception:
                logger.debug("SEL audit for orphan injection failed", exc_info=True)
        return delivered

    async def _send_orphan_slack_dm(self, msg: str) -> None:
        """Deliver an orphan notification via the owner DM / notification path.

        Delegates to the gateway-wired ``on_orphan_dm`` callback (Slack DM +
        dashboard notification). Falls back to a log line when no callback is
        wired (e.g. slack-only setups constructed without the gateway hooks).
        """
        if self._on_orphan_dm is not None:
            try:
                delivered = bool(await self._on_orphan_dm(msg))
                if delivered:
                    return
            except Exception:
                logger.debug("on_orphan_dm raised", exc_info=True)
        logger.warning("Orphan notification (no delivery channel wired): %s", msg[:200])

    def _live_shared_count(
        self, pid: int | None, agents: "list[SubagentInfo] | None" = None
    ) -> int:
        """Count live session-shared subagents sharing runtime *pid* (>= 1).

        Used to average the shared AcpRuntime's measured RSS/CPU across the
        sessions currently running inside it, so each shared subagent is charged
        an empirical per-session share rather than the whole process.

        *agents* lets an off-loop caller pass the snapshot it already took, so the
        count never iterates the live registry from a worker thread (see
        ``_sample_live_costs``). Omitted, it reads the registry directly, which is
        correct on the event loop.
        """
        if not pid:
            return 1
        pool = agents if agents is not None else list(self._agents.values())
        n = sum(1 for a in pool if not a.done and a._session_sharing and a._pid == pid)
        return n if n > 0 else 1

    def _sample_live_costs(self) -> None:
        """Sample high-water RSS/CPU for each live agent (reaper-loop piggyback).

        Updates per-run peaks on ``SubagentInfo`` (dynamic-subagent-sizing.md
        §4.1). RSS is the subtree VmRSS in GB; CPU is cores used since the last
        sample = Δ(utime+stime jiffies) / (CLK_TCK × Δt). The first sample only
        seeds the CPU baseline (no delta yet). Best-effort: a dead/unreadable
        pid is simply skipped.

        BLOCKING, and therefore off-loop: every live agent costs several ``/proc``
        walks (RSS subtree, CPU jiffies, process+stub counts), so the caller
        hands this to :func:`maintenance_executor` and the body must stay
        thread-safe. Concretely that means it takes ONE snapshot of the agent
        registry up front and derives everything, sharer counts included, from
        that list: iterating the live dict from a worker thread would raise
        ``RuntimeError`` the moment the event loop registered or evicted an
        agent mid-sweep. Writes are plain float/int field assignments on
        ``SubagentInfo``, which the surface only ever reads.
        """
        now = time.monotonic()
        agents = list(self._agents.values())
        for info in agents:
            if info.done or not info._pid:
                continue
            # Session-shared subagents run inside the parent's AcpRuntime process;
            # every sharing subagent reports the SAME runtime PID, so naive
            # per-PID sampling would attribute the whole shared process to each
            # of them. Instead attribute the runtime's measured RSS/CPU divided
            # by the number of concurrently-live shared sessions on that PID — an
            # empirical per-session average, not a guessed constant
            # (dynamic-subagent-sizing.md §session-sharing cost model).
            if info._session_sharing:
                shared_n = self._live_shared_count(pid_owner := info._pid, agents=agents)
                rss_kb = _proc_rss_kb(pid_owner)
                if rss_kb > 0 and shared_n > 0:
                    gb = (rss_kb / (1024 * 1024)) / shared_n
                    info.last_rss_gb = gb
                    if gb > info.peak_rss_gb:
                        info.peak_rss_gb = gb
                procs, stubs = _proc_subtree_counts(pid_owner)
                info.last_procs = _attributed_count(procs, shared_n, info.last_procs)
                info.last_stubs = _attributed_count(stubs, shared_n, info.last_stubs)
                jiffies = _subtree_cpu_jiffies(pid_owner)
                if info._cpu_sample_ts > 0.0 and jiffies >= info._cpu_jiffies_prev and shared_n > 0:
                    dt = now - info._cpu_sample_ts
                    if dt > 0:
                        cores = ((jiffies - info._cpu_jiffies_prev) / (_CLK_TCK * dt)) / shared_n
                        info.last_cpu_cores = cores
                        if cores > info.peak_cpu_cores:
                            info.peak_cpu_cores = cores
                info._cpu_jiffies_prev = jiffies
                info._cpu_sample_ts = now
                continue
            pid = info._pid
            rss_kb = _proc_rss_kb(pid)
            if rss_kb > 0:
                gb = rss_kb / (1024 * 1024)
                info.last_rss_gb = gb
                if gb > info.peak_rss_gb:
                    info.peak_rss_gb = gb
            procs, stubs = _proc_subtree_counts(pid)
            # Sole tenant of its own process: the subtree reading IS this run's.
            info.last_procs = _attributed_count(procs, 1, info.last_procs)
            info.last_stubs = _attributed_count(stubs, 1, info.last_stubs)
            jiffies = _subtree_cpu_jiffies(pid)
            if info._cpu_sample_ts > 0.0 and jiffies >= info._cpu_jiffies_prev:
                dt = now - info._cpu_sample_ts
                if dt > 0:
                    cores = (jiffies - info._cpu_jiffies_prev) / (_CLK_TCK * dt)
                    info.last_cpu_cores = cores
                    if cores > info.peak_cpu_cores:
                        info.peak_cpu_cores = cores
            info._cpu_jiffies_prev = jiffies
            info._cpu_sample_ts = now

    def _record_cost(self, info: SubagentInfo) -> None:
        """Persist this run's high-water RSS/CPU to the learned-cost store."""
        if info.peak_rss_gb <= 0 and info.peak_cpu_cores <= 0:
            return  # never sampled (e.g. finished before the first reaper sweep)
        try:
            append_cost_sample(info.agent, info.peak_rss_gb, info.peak_cpu_cores)
        except Exception:
            logger.debug("Failed to record subagent cost for %s", info.id, exc_info=True)

    async def _reaper_loop(self) -> None:
        """Periodically force-kill subagents that exceed the timeout.

        Defense-in-depth: catches cases where ``asyncio.wait_for`` in
        ``_run()`` fails to fire (event-loop saturation, orphaned tasks,
        or ``reset()`` hanging in the finally block).
        """
        try:
            compact_cost_log()  # startup FIFO trim (§4.2)
        except Exception:
            logger.debug("Reaper: startup cost-log compaction failed", exc_info=True)
        while True:
            await asyncio.sleep(_REAPER_INTERVAL)
            now = time.time()
            if not self._conv_registry_rebuilt:
                # First pass after (re)start: re-seed the conversation TTL
                # registry from state.json so promoted conversations survive
                # a gateway restart under sweep ownership (#1114). The flag
                # is set only on SUCCESS — a failed rebuild retries on the
                # next sweep instead of silently restoring the pre-#1114
                # orphaning until the next restart (Arbiter, PR #1246).
                try:
                    await self._rebuild_conversation_registry()
                    self._conv_registry_rebuilt = True
                except Exception:
                    logger.warning(
                        "Reaper: conversation registry rebuild failed — retrying next sweep",
                        exc_info=True,
                    )
            # Off the event loop: the sweep is several /proc walks per live agent,
            # and the reaper shares the loop with every chat turn and heartbeat.
            try:
                await asyncio.get_running_loop().run_in_executor(
                    maintenance_executor(), self._sample_live_costs
                )
            except Exception:
                logger.debug("Reaper: live-cost sample failed", exc_info=True)
            # Wave liveness backstop: reconcile waves wedged by submissions
            # lost before the process boundary (see _sweep_stuck_waves).
            try:
                self._sweep_stuck_waves(now)
            except Exception:
                logger.debug("Reaper: stuck-wave sweep failed", exc_info=True)
            # Digest hold deadline: release completed wave results that a
            # straggler (or a hung member) has been withholding.
            try:
                self._sweep_digest_holds(now)
            except Exception:
                logger.debug("Reaper: digest-hold sweep failed", exc_info=True)
            try:
                self._sweep_conversations(now)
            except Exception:
                logger.debug("Reaper: conversation sweep failed", exc_info=True)
            try:
                compact_cost_log()  # periodic FIFO trim (also bounds a long-running gateway)
            except Exception:
                logger.debug("Reaper: cost-log compaction failed", exc_info=True)
            for agent_id, info in list(self._agents.items()):
                if info.done:
                    continue
                elapsed = now - info.started
                # Startup watchdog: a subagent that entered execution but is
                # still on turn 0 with no runtime PID after the startup window
                # is wedged in startup (e.g. a hung provider/ACP handshake that
                # never launches the child process). Reap it fast with a clear
                # "failed to start" error instead of burning the full deadline
                # and surfacing a misleading 30-minute turn-0 timeout.
                if self._is_startup_stalled(info, now):
                    logger.warning(
                        "Reaper: subagent %s failed to start within %ds "
                        "(turn 0, no runtime launched), force-killing",
                        agent_id,
                        self._startup_deadline,
                    )
                    try:
                        await self._force_reap(
                            agent_id,
                            info,
                            now - (info._exec_started or now),
                            reason="startup_timeout",
                        )
                    except Exception:
                        logger.exception("Reaper: failed to reap %s", agent_id)
                    continue
                # Idle-stall detection (see _maybe_flag_stall). The main-agent
                # watchdog stack does not govern subagents; this is their
                # equivalent — surface a "stalled" UI signal well before the
                # 30-min ceiling. Surface-only: it never terminates the agent
                # (users close it from the UX), so we always fall through to
                # the wall-clock check below.
                await self._maybe_flag_stall(agent_id, info, now)
                if elapsed <= self._default_timeout:
                    continue
                logger.warning(
                    "Reaper: subagent %s exceeded %ds (ran %.0fs), force-killing",
                    agent_id,
                    self._default_timeout,
                    elapsed,
                )
                try:
                    await self._force_reap(agent_id, info, elapsed)
                except Exception:
                    logger.exception("Reaper: failed to reap %s", agent_id)

            # Prune stale tombstoned folders (>7 days old)
            try:
                pruned = await asyncio.get_running_loop().run_in_executor(
                    maintenance_executor(),
                    prune_stale_tombstones,
                    7,
                    self._result_ttl_secs,
                )
                if pruned:
                    logger.info("Reaper: pruned %d stale tombstone(s)", pruned)
            except Exception:
                logger.debug("Reaper: tombstone pruning failed", exc_info=True)

    def _is_startup_stalled(self, info: SubagentInfo, now: float) -> bool:
        """True if a subagent is wedged in startup and should be reaped early.

        A subagent qualifies only once it has actually entered execution
        (``_exec_started`` set by ``_run_inner``) yet has launched no runtime
        (``_pid is None``) and produced no turn (``turns == 0``) within
        ``_startup_deadline`` seconds. Keying on ``_exec_started`` — not the
        registration timestamp ``started`` — means an agent merely awaiting
        spawn approval (never entered ``_run_inner``) is never caught here.
        """
        exec_started = info._exec_started
        if exec_started is None:
            return False
        return (
            info.turns == 0 and info._pid is None and (now - exec_started) > self._startup_deadline
        )

    @staticmethod
    def _note_tool_dispatch(info: SubagentInfo, event: Any) -> None:
        """Record the in-flight tool for liveness attribution.

        Mirrors ``AcpSessionHandle``'s ``_inflight_tool`` snapshot: title, the
        already-redacted input, the dispatch instant, and the TRUSTED
        ``is_shell`` / ``tool_name`` fields from ``_meta.kiro`` (never the
        LLM-authored title). The subagent event loop already receives the same
        ``AcpEvent``; it previously kept only ``title`` and dropped the rest,
        which is why stall detection had nothing to attribute evidence with.

        Retiring the oracle here (rather than clearing it) is load-bearing: a
        movement walk still running against the PREVIOUS tool's command holds a
        reference to the old instance, and clearing in place would let its late
        write land on the new tool's baseline and read as movement.
        """
        info._inflight_tool = ToolCallState(
            title=event.title or "",
            command=event.tool_input or "",
            dispatch_ts=time.monotonic(),
            is_shell=bool(getattr(event, "is_shell", False)),
            tool_name=getattr(event, "tool_name", "") or "",
        )
        oracle = info._stall_oracle
        info._stall_oracle = oracle.fresh() if oracle is not None else None
        info._stall_gen += 1

    @staticmethod
    def _note_tool_result(info: SubagentInfo, event: Any) -> None:
        """Retire the attribution snapshot when a tool's FINAL result arrives.

        The gate lives here rather than at the call site so the invariant is
        directly testable. ``EVENT_TOOL_RESULT`` is also emitted for
        non-completed progress updates (``_dispatch`` sets
        ``tool_final = status == "completed"``), and treating one of those as the
        end of the tool would drop attribution while the command is still
        running — degrading liveness to idle-time-only for exactly the long
        silent command this detection exists to judge, and so raising the badge
        on a healthy agent. ``acp.client`` gates on the same field.
        """
        if event.tool_final:
            SubagentManager._clear_tool_dispatch(info)

    @staticmethod
    def _clear_tool_dispatch(info: SubagentInfo) -> None:
        """Drop the in-flight tool snapshot and retire the oracle with it."""
        info._inflight_tool = None
        oracle = info._stall_oracle
        info._stall_oracle = oracle.fresh() if oracle is not None else None
        info._stall_gen += 1

    async def _stall_verdict(self, info: SubagentInfo) -> tuple[str, str]:
        """Liveness verdict for an idle subagent: working, wedged, or unknown.

        Idle time alone cannot separate a hung tool call from a slow silent one,
        so this consults the same ``LivenessOracle`` the main agent's watchdog
        uses (:mod:`kiro_crew.acp.liveness`) for ``/proc`` evidence.

        The attribution is what makes it sound. With the in-flight tool's real
        ``is_shell`` + command, the consult takes the oracle's shell-child
        branch, which matches a live descendant by CMDLINE and then tracks that
        pid — so the evidence belongs to THIS subagent's own child even when the
        runtime is shared with sibling subagents. That is the distinction an
        earlier whole-subtree attempt could not make: a subtree aggregate is
        dominated by kiro-cli's own background socket/keepalive traffic, so a
        ``sleep``-only subagent read as "working" and was never flagged.

        Returns ``(verdict, evidence)``; any failure degrades to
        ``(VERDICT_UNKNOWN, ...)`` so the caller falls back to idle time.
        """
        if not info._pid:
            return VERDICT_UNKNOWN, "no runtime pid"
        tool = info._inflight_tool
        if tool is None:
            # Idle with no tool in flight is a model-wait, not a hung command.
            # The model-wait branch reads the whole runtime subtree, which is not
            # attributable on a shared runtime — so decline rather than guess.
            return VERDICT_UNKNOWN, "no tool in flight"
        if not tool.is_shell:
            # A non-shell MCP tool has no child process to match, so the oracle
            # can only offer the same unattributable subtree aggregate. Decline.
            return VERDICT_UNKNOWN, "non-shell tool — not attributable"
        if info._stall_oracle is None:
            info._stall_oracle = LivenessOracle()
        # The consult is a SYNCHRONOUS /proc filesystem walk (``iter_descendants``
        # over the runtime's descendant subtree, plus ``os.readlink`` on
        # ``/proc/<pid>/fd/*``, which can block on the very wedged fd being
        # investigated) — and this runs on the reaper's event loop, the same loop
        # that serves every chat turn and the liveness heartbeat, sweeping agents
        # serially. Inline, one wedged read freezes the gateway until the
        # loop-stall watchdog kills it. Offload it exactly as the main-agent path
        # does (``AcpSessionHandle._consult_oracle_offloaded``): bounded await,
        # and at most ONE outstanding walk per agent so a permanently wedged read
        # cannot leave a new blocked worker behind on every sweep.
        # ``consult_offloaded`` owns that whole sequence -- one outstanding walk per
        # holder, submission inside the guard, exception retrieval attached at
        # submission, every failure degrading to UNKNOWN -- for the two watchdog
        # paths that already depend on it, so a fix there lands here too.
        # ``SubagentInfo`` satisfies its ``ConsultFutureHolder`` protocol via
        # ``_consult_future``. That handle deliberately OUTLIVES snapshot
        # retirement: ``_clear_tool_dispatch`` bumps ``_stall_gen`` (which
        # invalidates a stale verdict, below) but leaves the future in place, so a
        # walk still wedged on a stuck fd keeps suppressing resubmission instead
        # of letting each later sweep strand another blocked worker.
        submitted_gen = info._stall_gen
        verdict = await consult_offloaded(
            info,
            info._stall_oracle.check_tool,
            (info._pid, tool),
            executor_factory=subprocess_executor,
            log_label=f"stall consult for {info.id}",
        )
        # The consult awaits, so fresh activity, a final tool result, or the next
        # dispatch can retire this snapshot while the walk is still running. A
        # verdict about a tool that is no longer in flight must not be applied to
        # whatever replaced it: DEAD/STUCK_INPUT skips the two-sweep confirmation,
        # so a stale one would flag an agent that has demonstrably resumed working.
        if info._stall_gen != submitted_gen:
            return VERDICT_UNKNOWN, "superseded mid-consult"
        return verdict

    async def _maybe_flag_stall(self, agent_id: str, info: SubagentInfo, now: float) -> None:
        """Idle-stall detection for a running subagent (surface-only).

        A subagent that has started (>=1 turn or a live runtime PID) but has
        emitted no stream activity for ``_stall_idle_secs`` may be wedged in a
        hung tool call — or simply running one slow, silent command. Idle time
        cannot tell those apart, so the flag is gated on a ``LivenessOracle``
        consult (:meth:`_stall_verdict`) that attributes evidence to the
        subagent's OWN child process by cmdline match:

        * ``WORKING`` — a live matched child, so it is progressing: not flagged.
        * ``DEAD`` / ``STUCK_INPUT`` — the child exited with no result frame, or
          its subtree is flat and blocked on a tty/stdin read. That is positive
          evidence of a wedge, so it flags IMMEDIATELY, skipping the two-sweep
          confirmation the idle-time path needs.
        * ``UNKNOWN`` — no attributable evidence (no shell child to match, no
          tool in flight, unreadable ``/proc``). Falls back to idle time with the
          two-sweep confirmation, i.e. exactly the previous behaviour.

        Still deliberately *surface-only*: it emits a ``subagent_stalled`` UI
        signal and records the slow command, but NEVER terminates the agent, so
        a slow-but-healthy command can only ever produce a self-clearing badge.
        Escalating a ``DEAD`` verdict to an early reap would be a change to kill
        semantics and is intentionally NOT part of this; the wall-clock reaper at
        ``_TIMEOUT_SECS`` remains the only automatic terminator.
        """
        if not (info.turns > 0 or info._pid is not None):
            return
        # A subagent blocked on a human tool-approval prompt is healthy, not
        # stalled — the permission request bumps `turns` before the approval
        # wait, so without this a slow approval would be mislabelled idle.
        if info._awaiting_approval:
            return
        idle = now - info.last_activity
        if not info.stalled and idle > self._stall_idle_secs:
            verdict, evidence = await self._stall_verdict(info)
            if verdict == VERDICT_WORKING and idle < self._stall_idle_secs * _SUPPRESS_CEILING:
                # Attributable progress in this subagent's own child: silent, not
                # stalled. Leave the suspicion open (do not reset
                # _stall_suspect_at) so the badge appears as soon as that child
                # stops moving or exits.
                #
                # The ceiling above bounds how long a WORKING reading may hold the
                # badge back, because attribution is not infallible: under
                # ``session_sharing`` two siblings running similar commands can
                # cmdline-match the SAME child, so a wedged agent can read WORKING
                # for as long as its sibling's child lives. Without a bound that
                # turns an old true positive into a permanent false negative —
                # strictly worse than the idle-time-only path it replaces. With
                # it, misattribution costs latency, not the signal.
                logger.debug(
                    "Reaper: subagent %s idle %.0fs but working (%s) — not flagging",
                    agent_id,
                    idle,
                    evidence,
                )
                return
            # A wedged verdict normally skips it: DEAD/STUCK_INPUT is positive
            # evidence about this agent's child rather than a guess from elapsed
            # silence, so dampening it would only delay a signal already earned.
            #
            # BUT that trust is only warranted when the cmdline match cannot have
            # landed on someone else's child. ``_SUPPRESS_CEILING`` exists
            # precisely because the match is fallible under a shared runtime, and
            # a DEAD derived from a fallible match is exactly as wrong as the
            # WORKING the ceiling bounds — a sibling's matched child exiting would
            # otherwise raise an immediate badge on a healthy agent, skipping the
            # very dampening added to keep the badge trustworthy at scale. So the
            # skip is withdrawn whenever another session could be the one being
            # measured, and the wedged verdict then earns its badge the same way
            # an idle-time guess does: by holding across two sweeps.
            #
            # The gate keys on ``session_sharing`` itself, not on a count of live
            # siblings, because the confusable co-tenant is not only a sibling:
            # ``_create_shared_session`` puts the subagent on the PARENT's
            # AcpRuntime ("one process hosts everything"), so ``info._pid`` is the
            # parent's process and the parent's own tool children are descendants
            # of it too. ``_live_shared_count`` iterates the subagent registry and
            # therefore cannot see the parent, so a LONE subagent counted 1 and
            # kept the fast path while still able to cmdline-match the parent's
            # child — and flag instantly when that child exited. Since a shared
            # runtime always has the parent in it, "could this match belong to
            # someone else?" is true for every session-sharing agent.
            wedged = verdict in (VERDICT_DEAD, VERDICT_STUCK_INPUT)
            if wedged and info._session_sharing:
                wedged = False
            # Two-sweep confirmation (scale dampening): at 60-100 concurrent
            # agents a single-window trip ambers several healthy-slow agents at
            # any moment, training users to ignore the badge. Require the idle
            # threshold to hold across TWO consecutive reaper sweeps before
            # flagging — a stream event between sweeps resets the suspicion
            # (_touch_activity clears both flags). Adds at most one sweep
            # interval (~60s) of latency to a genuine stall.
            if not wedged and info._stall_suspect_at <= 0.0:
                info._stall_suspect_at = now
                return
            info.stalled = True
            logger.warning(
                "Reaper: subagent %s idle %.0fs (verdict=%s; %s) — marking stalled",
                agent_id,
                idle,
                verdict,
                evidence,
            )
            # Persist the slow command for future analysis. Best-effort; must
            # not disturb the still-running agent (NOT a tombstone — the agent
            # is alive, not dead).
            self._record_slow_command(info, idle)
            try:
                await self._fire_event(
                    "subagent_stalled",
                    info,
                    # The verdict and its evidence are deliberately NOT on the
                    # wire: no consumer reads them (the frontend narrows this
                    # payload to {slot, id, stalled, idle_secs} on arrival, and
                    # the coalesced batch update forwards only `stalled`), and the
                    # event is app-sdk-forwarded, so shipping unread keys would
                    # create semi-permanent surface. The log line above records
                    # both for diagnosis; add them here when something renders it.
                    {"stalled": True, "idle_secs": int(idle)},
                )
            except Exception:
                logger.debug(
                    "Reaper: failed to emit subagent_stalled for %s", agent_id, exc_info=True
                )

    @staticmethod
    def _record_slow_command(info: SubagentInfo, idle: float) -> None:
        """Best-effort append of a stalled subagent's slow command for analysis.

        Writes to ``~/.kiro/crew/subagents/slow_commands.jsonl`` (append-only,
        survives per-agent folder cleanup). Deliberately separate from the
        tombstone path, which marks an agent dead — a stalled agent is still
        running.
        """
        try:
            record_slow_command(
                info.id,
                last_tool=_redact(info.last_tool or ""),
                tool_count=info.tool_count,
                turns=info.turns,
                idle_secs=int(idle),
                elapsed_secs=int(time.time() - info.started),
                parent_session=info.parent_session_key or "",
                session_sharing=info._session_sharing,
            )
        except Exception:
            logger.debug("Failed to record slow command for %s", info.id, exc_info=True)

    def _claim_finalize(self, info: SubagentInfo, *, supersede_recovery: bool = False) -> bool:
        """Claim the exclusive right to report ``info``'s terminal outcome.

        Returns True for exactly one caller. Both the reap path and ``_run``'s
        ``finally`` call this and report only if it returns True, so the parent
        is notified exactly once no matter which wins the race or whether the
        loser is cancelled part-way through its teardown.

        Contains no ``await``, so on a single-threaded event loop the
        check-and-set is atomic with respect to other tasks.

        Returns False while ``_recovering`` — a cancel-recovery respawn is
        pending and the agent must not be reported done yet — leaving the claim
        OPEN so the respawned run can take it later.

        ``supersede_recovery=True`` overrides that withholding and is used ONLY
        by definitively-terminal callers (`_force_reap`, which also serves user
        Stop). Without it a reap landing inside the recovery window stranded the
        outcome: the reap was refused the claim, performed teardown and set
        ``reaped``, reported nothing — and `_resume`'s ``reaped`` abort path
        bare-returns, so no path ever reported and the agent sat unfinished
        until the reaper's wall-clock deadline. Superseding also clears
        ``_recovering``, because a killed agent has nothing left to respawn.

        Scope note: this token governs REPORTING only, and it deliberately does
        NOT consult ``info.done``. Gating it on ``done`` is wrong:
        if ``_run_inner`` set ``done`` while the reap awaited its session reset,
        the reaper refused the claim, still marked ``reaped``, and ``_run``'s
        finally then skipped its own claim — nobody reported. The terminal RECORD
        (tombstone/stat) keeps its own ``not info.done`` guard and slot accounting
        has its own one-shot token (:meth:`_release_slot`); three concerns, three
        guards. Session teardown stays keyed on ``reaped``.
        """
        if info._recovering and not supersede_recovery:
            return False
        if info._finalized:
            return False
        if info._recovering:
            # A terminal reap/stop SUPERSEDES a pending cancel-recovery respawn:
            # the agent is being killed, so there is nothing left to respawn.
            # Clearing the flag here is what keeps `False` from meaning two
            # different things to this caller ("someone else already reported"
            # vs "withheld for a respawn that will report later") — the exact
            # conflation this token exists to remove.
            info._recovering = False
        info._finalized = True
        return True

    async def _report_terminal(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> None:
        """Deliver ``info``'s one-shot terminal report as a single unit.

        This is the exact work the finalize claim guards: fire the
        ``subagent_done`` WS event, then inject the completion into the parent
        (``_on_done``) with its ``_ON_DONE_TIMEOUT`` cap, timeout handling, and
        (for the ``_run`` path) the result.txt TTL / workspace-cleanup
        bookkeeping.

        Why this is a separate coroutine run under ``asyncio.shield`` (see
        ``_run_terminal_report``): the claim makes reporting EXCLUSIVE but not
        ATOMIC. A claimer cancelled mid-report (``_force_reap`` /
        ``cancel_all()`` cancelling the task while it awaits ``_fire_event`` or
        ``_on_done``) would exit without delivering, and the other path — seeing
        the claim already taken — stays silent, so the completed outcome never
        reaches the parent. Running the report on a shielded, strongly-held task
        makes it complete independently of caller cancellation.

        The two call sites (reap vs. ``_run``'s ``finally``) differ only in the
        injection-timeout reason string, the log ``source`` prefix, and whether
        a successful delivery marks the result delivered — those are passed as
        arguments rather than unified away. The WS payload is identical (both
        set ``info.elapsed`` before calling), so it is built from ``info`` here.
        """
        await self._fire_event(
            "subagent_done",
            info,
            {
                "elapsed": info.elapsed,
                "error": _redact(info.error) if info.error else None,
                "stopped": info.user_stopped,
                "outcome": info.outcome,
                "task": _redact(info.task),
                "agent": _redact(info.agent),
                "result": _done_result(info.result),
            },
        )
        if not self._on_done:
            return
        try:
            await asyncio.wait_for(self._on_done(info), timeout=_ON_DONE_TIMEOUT)
            # The outcome has REACHED the parent. Recorded before any further
            # await so a shutdown cancellation landing in the teardown wait or
            # the tombstone write below is not mistaken for a lost delivery by
            # `cancel_all()` (which would re-deliver it on the next start).
            info._reported_to_parent = True
            if settle_digest:
                # _on_done returned without raising, so the wave digest (if this
                # was the final member) has been handed off. Only NOW settle the
                # held members' delivery tombstones.
                self._settle_digest_holds(info)
            # Digest-held wave members are NOT marked delivered here: their
            # result has not reached the parent yet (the gateway marks them when
            # the digest fires), so a restart mid-wave leaves them visible to
            # orphan reconciliation.
            if mark_delivered_on_success and not info.error and not info._digest_held:
                # Wait for the caller's session teardown before writing the
                # "delivered" tombstone. This report is deliberately SPAWNED
                # ahead of teardown (so a cancellation cannot strand it), which
                # opens a window the older post-teardown ordering did not have:
                # a crash after the tombstone but before teardown finished would
                # leave a surviving child process EXCLUDED from orphan
                # reconciliation — invisible and never reaped. The wait is
                # bounded because the caller's teardown is itself bounded
                # (_RESET_TIMEOUT then SIGKILL) and runs in a `finally`, so the
                # event is set even when the caller is cancelled.
                if teardown_done is not None and not teardown_done.is_set():
                    try:
                        await asyncio.wait_for(teardown_done.wait(), timeout=_RESET_TIMEOUT + 30)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Subagent %s: teardown did not complete before the "
                            "delivered tombstone; writing it anyway",
                            info.id,
                        )
                # Retain result.txt for a TTL grace window instead of deleting
                # it now, so the parent can read the full transcript
                # (spawn_status / read / grep) after the completion event. A
                # "delivered" tombstone excludes it from orphan reconciliation;
                # the reaper prunes it after agent.subagent_result_ttl_secs.
                try:
                    mark_delivered(info.id)
                except Exception:
                    logger.debug("Failed to mark subagent %s delivered", info.id, exc_info=True)
                # Clean up workspace result file (agent-{id}.md in parent dir).
                # The directory is named after the parent's SLOT, which a
                # channel-born parent has while its session key stays the
                # channel's own; without a tab there is no directory to clean.
                try:
                    # Lazy: the dashboard layer must not be imported by a core
                    # module at import time.
                    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

                    slot_key = dashboard_slot_key(info.parent_session_key)
                    if slot_key:
                        _ws_result_path(slot_key, info.id).unlink(missing_ok=True)
                except Exception:
                    logger.debug("Failed to clean workspace result for %s", info.id, exc_info=True)
        except asyncio.TimeoutError:
            logger.error(
                "%s: completion injection timed out for %s after %.0fs",
                source,
                info.id,
                _ON_DONE_TIMEOUT,
            )
            # Kill the parent session's kiro-cli process so the next agent's
            # injection gets a clean provider instead of hitting "Prompt already
            # in progress" on the stuck one.
            try:
                await self._sessions.reset(info.parent_session_key)
            except Exception:
                logger.debug(
                    "Failed to reset parent session %s after injection timeout",
                    info.parent_session_key,
                    exc_info=True,
                )
            self.notify_injection_failed(info, reason=injection_timeout_reason)
        except Exception:
            logger.exception("%s: announce failed for %s", source, info.id)

    async def _run_terminal_report(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> None:
        """Spawn the shielded terminal report and block until it completes.

        Convenience for callers that have no cancellable ``await`` between
        taking the claim and reporting (``_force_reap``): there is no window in
        which a cancellation could strand the outcome before the report task
        exists, so spawning and awaiting can be adjacent. Callers that DO have a
        teardown ``await`` between the claim and the report (``_run``'s
        ``finally``) must instead :meth:`_spawn_terminal_report` BEFORE that
        await and :meth:`_await_report` after, so the report task is already
        live (and shielded) no matter where the cancellation lands.
        """
        await self._await_report(
            self._spawn_terminal_report(
                info,
                source=source,
                injection_timeout_reason=injection_timeout_reason,
                mark_delivered_on_success=mark_delivered_on_success,
                settle_digest=settle_digest,
                teardown_done=teardown_done,
            )
        )

    def _spawn_terminal_report(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> "asyncio.Task":  # type: ignore[type-arg]
        """Launch :meth:`_report_terminal` on a strongly-referenced task.

        Returns immediately (no ``await``) so the caller can start the report
        BEFORE its own teardown awaits, guaranteeing the report exists and is
        held alive independently of the caller's fate. The task is retained in
        ``self._report_tasks`` (so it cannot be garbage-collected while its
        awaiter is cancelled, and so ``cancel_all()`` can drain it) and
        self-removes on completion.
        """
        task = asyncio.create_task(
            self._report_terminal(
                info,
                source=source,
                injection_timeout_reason=injection_timeout_reason,
                mark_delivered_on_success=mark_delivered_on_success,
                settle_digest=settle_digest,
                teardown_done=teardown_done,
            )
        )
        self._report_tasks.add(task)
        # Owner map so `cancel_all()` can identify WHOSE outcome it is about to
        # abandon (and re-admit it to orphan recovery). Kept alongside the set
        # rather than replacing it: `_report_tasks` is the strong reference that
        # keeps the task alive, and both are cleared by the one done callback.
        self._report_owners[task] = info

        def _forget(t: "asyncio.Task") -> None:  # type: ignore[type-arg]
            self._report_tasks.discard(t)
            self._report_owners.pop(t, None)

        task.add_done_callback(_forget)
        return task

    @staticmethod
    async def _await_report(task: "asyncio.Task") -> None:  # type: ignore[type-arg]
        """Block until a spawned terminal report completes, shielded.

        On normal completion this blocks until the report is delivered
        (sequencing unchanged). If the awaiting caller is cancelled, the shield
        keeps the report running to completion on its own task while the caller
        still receives ``CancelledError`` — teardown semantics are unchanged and
        the outcome is never stranded.
        """
        await asyncio.shield(task)

    def _release_slot(self, info: SubagentInfo) -> bool:
        """Claim the exclusive right to free ``info``'s concurrency slot.

        Returns True for exactly one caller; that caller decrements
        ``_running_count`` once and drains the queue. Contains no ``await``, so
        the check-and-set is atomic with respect to other tasks on the loop.

        Why this is its OWN token rather than a side effect of ``done`` or
        ``reaped``: both terminal paths (`_force_reap` and `_run`'s ``finally``)
        can run for the same agent, and previous revisions inferred slot
        ownership from whichever flag happened to be set. That produced a double
        decrement in one interleaving and — after the flag order was changed to
        fix a delivery bug — no decrement at all in another, inflating
        ``_running_count`` and permanently starving the spawn queue. An explicit
        one-shot token makes the count independent of report and record ordering.

        Note the recovery respawn's own ``_running_count += 1`` re-admit is
        unaffected: it runs after the interrupted run's ``finally`` has already
        released, and this token is per-``SubagentInfo``.
        """
        if info._slot_released:
            return False
        info._slot_released = True
        return True

    async def _force_reap(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        """Kill a subagent's session process and mark it done."""
        session_key = f"subagent:{agent_id}"

        # Reap-in-flight marker + recovery cancel BEFORE ANY await in this
        # method. Both used to sit after the session teardown below, which yields
        # (bounded by _RESET_TIMEOUT, longer still on the SIGKILL path). A
        # cancel-recovery task whose bounded handshake expired inside that window
        # respawned the very run being killed — tools executing after a user
        # Stop, strictly worse than a duplicate report. Note this sets
        # `_reap_started`, NOT `reaped`: setting `reaped` this early makes a run
        # woken by our own session reset skip its error synthesis and report a
        # false SUCCESS before we own the record. See `_reap_started`.
        info._reap_started = True
        # A pending cancel-recovery respawn is moot — this agent is being killed.
        # Cancel it rather than letting it sit in its bounded handshake wait
        # (_RESET_TIMEOUT + 60s) only to discover `reaped` and bare-return.
        # The reap owns the terminal report from here (see the claim below).
        recovery_task = self._tasks.pop(f"{agent_id}:recovery", None)
        if recovery_task and not recovery_task.done():
            recovery_task.cancel()

        if info._session_sharing:
            # Session-sharing subagent: NEVER SIGKILL the shared runtime —
            # the parent session owns it and other co-tenants may be active.
            # Conservative approach: shut down only this subagent's provider
            # handle, leaving the shared runtime intact.
            runtime_pid = info._pid
            logger.info(
                "Reaper: conservative shutdown for session-sharing %s — "
                "runtime pid=%s kept alive (shared runtime, never SIGKILL)",
                agent_id,
                runtime_pid,
            )
            try:
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="subagent",
                    tool_name="smart_hard_kill",
                    outcome="conservative-shutdown",
                    resources=f"runtime_pid={runtime_pid}",
                    metadata={
                        "subagent_id": agent_id,
                        "runtime_pid": runtime_pid,
                        "decision": "session-sharing-never-kill",
                    },
                )
            except Exception:
                logger.debug("SEL audit for conservative shutdown failed", exc_info=True)
            # Shutdown the shared provider handle only
            try:
                if info._shared_provider:
                    await info._shared_provider.shutdown()
            except Exception:
                logger.debug(
                    "Reaper: shared session shutdown failed for %s", agent_id, exc_info=True
                )
        else:
            # Kill the process FIRST so the pipe unblocks, then cancel the task.
            try:
                await asyncio.wait_for(self._sessions.reset(session_key), timeout=_RESET_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Reaper: reset hung for %s, attempting SIGKILL", agent_id)
                await self._sigkill_session(session_key)
            except Exception:
                logger.exception("Reaper: reset failed for %s", agent_id)

        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            # `reaped` is set HERE — late, immediately before the intentional
            # cancel — not at the top of the method. Late enough that a run woken
            # by the session reset above still synthesizes its own error (a run
            # that sees `reaped` skips error synthesis, and reporting with no
            # error set delivers a false success). Early enough to satisfy the
            # intentional-cancel contract: visible when the task's
            # CancelledError arm runs. The recovery scheduler reads the earlier
            # `_reap_started` instead, so it is not affected by this placement.
            info.reaped = True
            self._cancel_task_intentionally(task, info, reason=reason or "reaped")

        # No live task to cancel above (already exited) — the reap still owns
        # teardown bookkeeping from here, so mark it now.
        info.reaped = True
        # Guard 1 of 3 — the terminal RECORD (done/error/stat/tombstone/cost) is
        # first-arrival-wins on `info.done`, so it is never written twice.
        if not info.done:
            info.done = True
            if not info.error and not info.user_stopped:
                # A user stop is neutral — never synthesize a reap error for it.
                if reason == "startup_timeout":
                    info.error = f"Failed to start within {self._startup_deadline}s (no runtime launched, no turn produced) [{_timeout_context(info, include_elapsed=False, turn_limit=self._effective_turn_limit(info))}]"
                else:
                    info.error = f"Reaped after {int(elapsed)}s (exceeded {self._default_timeout}s deadline) [{_timeout_context(info, include_elapsed=False, turn_limit=self._effective_turn_limit(info))}]"
            if not info.user_stopped:
                # A user-initiated stop is a neutral outcome, not a failure.
                Stats().inc_subagent_failed()
            self._write_tombstone(info, reason or "reaped")
            self._record_cost(info)
        # Guard 2 of 3 — SLOT accounting, on its own one-shot token and therefore
        # independent of both `done` (above) and `reaped`. A reap/cancel frees a
        # slot but — unlike normal completion — does NOT otherwise pump the queue,
        # so queued spawns would sit stranded until an unrelated agent finished.
        # Drain here so the freed slot is used immediately.
        if self._release_slot(info):
            self._running_count = max(0, self._running_count - 1)
            self._drain_queue()

        try:
            sel().log_tool_invocation(
                session_key=session_key,
                source="subagent",
                tool_name="reaper_force_kill",
                outcome="reaped",
                metadata={
                    "subagent_id": agent_id,
                    "session_key": session_key,
                    "elapsed": int(elapsed),
                },
            )
        except Exception:
            logger.exception("Reaper: SEL audit failed for %s", agent_id)

        try:
            # Retain-by-default: the reaped run's session files stay on disk
            # (spawn_continue resume material); the tombstone pruner owns
            # their deletion. A force-reaped long run is exactly the case
            # retention exists for.
            self._sessions.release(session_key, cleanup=False)
        except Exception:
            logger.warning("Reaper: release failed for %s", agent_id, exc_info=True)

        # Guard 3 of 3 — the terminal REPORT (subagent_done + _on_done), owned by
        # the finalize claim. The claim deliberately does NOT consult `info.done`:
        # `_run_inner` may set `done` while the teardown above is suspended, and
        # gating on it made the reaper decline while `_run`'s finally also declined
        # (it sees `reaped`) — so nobody reported. The report runs SHIELDED, so a
        # cancellation landing mid-report (cancel_all during shutdown) still
        # delivers rather than stranding the outcome with the claim consumed.
        info.elapsed = elapsed
        if self._claim_finalize(info, supersede_recovery=True):
            await self._run_terminal_report(
                info,
                source="Reaper",
                injection_timeout_reason=(
                    f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s (reaper)"
                ),
                mark_delivered_on_success=False,
                # This member's own result is NOT marked delivered (it was
                # reaped, not completed) — but if it was the wave member whose
                # `_on_done` flushed the batch digest, its SIBLINGS' successful
                # results HAVE now reached the parent. Settling is about their
                # holds, not this member's outcome, so it must happen on this
                # path too or held siblings stay visible to orphan
                # reconciliation and get spuriously "recovered" after a restart.
                settle_digest=True,
            )

        # Truncate retained text AFTER _on_done to preserve full output for result injection
        if len(info.streaming_text) > 10_000:
            info.streaming_text = info.streaming_text[:10_000] + "\n…(truncated)"

    async def _sigkill_session(self, session_key: str) -> None:
        """Best-effort SIGKILL when graceful reset hangs.

        Uses killpg to kill the entire process group, then sweeps
        escaped children in different PGIDs (MCP servers).

        Async so the Windows ``taskkill`` spawn offloads to
        :func:`kiro_crew.executors.subprocess_executor` via
        :func:`platform_compat.kill_process_tree_async` / ``kill_pid_async``
        instead of blocking the reaper loop's event loop for the duration of
        ``taskkill.exe``.
        """
        try:
            # circular import: subagent → acp.client → session → subagent
            from kiro_crew.acp.client import (
                _capture_child_records,
                _get_child_pids,
                _is_our_child,
                _kill_escaped_children,
            )

            session = self._sessions._sessions.get(session_key)
            if not session:
                return
            client = getattr(session.provider, "_client", None)
            raw_pid = getattr(client, "_pid", None) if client else None
            pid = raw_pid if isinstance(raw_pid, int) else None
            if not pid:
                return
            # Snapshot child tree before killing — children in different
            # PGIDs survive killpg. macOS pgrep/ps spawns are offloaded to
            # subprocess_executor to keep the reaper loop responsive
            loop = asyncio.get_running_loop()
            raw_children = getattr(client, "_child_pids", None)
            child_pids: dict = dict(raw_children) if isinstance(raw_children, dict) else {}
            fresh = await loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
            new_pids = [p for p in fresh if p not in child_pids]
            if new_pids:
                child_pids.update(
                    await loop.run_in_executor(
                        subprocess_executor(), _capture_child_records, new_pids
                    )
                )
            # Validate PID hasn't been recycled before killing.
            original_start = getattr(client, "_start_time", None)
            if original_start is None:
                logger.debug("Reaper: PID %d already dead for %s", pid, session_key)
                await loop.run_in_executor(
                    subprocess_executor(), _kill_escaped_children, child_pids
                )
                return
            if not await loop.run_in_executor(
                subprocess_executor(), _is_our_child, pid, original_start
            ):
                logger.warning("Reaper: PID %d recycled for %s, skipping killpg", pid, session_key)
                stored = dict(raw_children) if isinstance(raw_children, dict) else {}
                await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, stored)
                return
            # Kill the entire process group first
            logger.warning(
                "Reaper: killpg for PID %d (%d children) for %s",
                pid,
                len(child_pids),
                session_key,
            )
            try:
                # Async variants offload Windows taskkill to
                # subprocess_executor so the reaper loop never blocks the
                # event loop on taskkill.exe.
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
            except ValueError:
                # Guard refused the pid outright (non-int/reserved) — nothing
                # safe to signal. Mirrors CronService._sigkill_session so a
                # broadcast-guard refusal is a clean log line, not the noisy
                # generic `except Exception` traceback below.
                logger.error("Reaper: kill guard refused pid %r for %s", pid, session_key)
            except (ProcessLookupError, OSError):
                try:
                    await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            # Sweep children that escaped to different PGIDs
            await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, child_pids)
        except Exception:
            logger.exception("Reaper: SIGKILL failed for %s", session_key)

    def notify_injection_failed(
        self, info: SubagentInfo, reason: str = "delivery timed out"
    ) -> None:
        """Notify UI and queue failure for LLM when injection times out.

        Appends a synthetic error to the dashboard slot (UI) and queues a
        failure message into ``slot._pending_subagent_failures`` so the LLM
        learns about the failure on the next ``_run_chat`` turn and can read
        the result from disk if needed.
        """
        try:
            # Lazy: the dashboard layer must not be imported by a core module at
            # import time.
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            # The failure is queued into a SLOT, so the gate is whether the
            # parent has a tab — true for a channel-born parent whose session
            # key is the channel's own. Without one there is nothing to append
            # to and nothing to drain on the next turn.
            slot_name = dashboard_slot_key(info.parent_session_key)
            if not slot_name:
                return

            # Build failure message the LLM will see on next turn
            task_preview = _redact((info.task or "")[:100])
            result_hint = ""
            if info.result_path:
                try:
                    size = os.path.getsize(info.result_path)
                    size_str = f"{size:,} bytes"
                except OSError:
                    size_str = ""
                result_hint = (
                    f"\nResult saved at: {info.result_path}"
                    + (f" ({size_str})" if size_str else "")
                    + "\nUse the read tool to retrieve it if needed."
                )
            failure_msg = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{info.id}` ❌ {reason}\n"
                f"Task: {task_preview}\n"
                f"The agent finished but result delivery timed out.{result_hint}"
            )

            # Queue for LLM context drain on next _run_chat
            if self._on_event:
                _task = asyncio.ensure_future(
                    self._fire_event(
                        "subagent_injection_failed",
                        info,
                        {
                            "error": reason,
                            "slot": slot_name,
                            "failure_msg": failure_msg,
                        },
                    )
                )
                _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except Exception:
            logger.debug("notify_injection_failed failed for %s", info.id, exc_info=True)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        return self._running_count

    def running_agents_for(self, parent_key: str) -> list[dict]:
        """Return summary dicts for agents belonging to *parent_key*."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        def _r(s: str) -> str:
            s, _ = redact_exfiltration_urls(s)
            s, _ = redact_credentials(s)
            return s

        return [
            {
                "id": a.id,
                "task": _r(a.task[:80]),
                "agent": _r(a.agent),
                "turns": a.turns,
                "last_tool": _r(a.last_tool),
                "tool_count": a.tool_count,
                "stalled": a.stalled,
                "startedAt": a.started,
            }
            for a in self._agents.values()
            if not a.done and a.parent_session_key == parent_key
        ]

    def task_memory_rows(self) -> list[dict[str, object]]:
        """Per-running-task memory/CPU rows for the session-memory surface.

        Reads the samples the reaper sweep already takes (``_sample_live_costs``,
        every ``_REAPER_INTERVAL`` seconds) — this method itself does no ``/proc``
        work, so it is safe on the event loop. ``rss_mb`` is 0.0 until the first
        sweep observes the agent, which is why ``sampled`` is reported separately:
        a fresh task genuinely has no measurement yet, and rendering that as
        "0 MB" would be a lie.

        ``shared`` mirrors ``_session_sharing``: the value is that runtime's
        measurement divided by the number of concurrently-live sharing sessions
        on the same pid, i.e. an average share, not an exclusive figure. The same
        split applies to ``procs``/``mcp`` (see ``_attributed_count``), which are
        null until a sweep has counted them — a task row that reported no MCP
        stubs because the field was simply absent read as "subagents do not use
        the MCP pool", which is the opposite of what they do.
        """
        return [
            {
                "id": a.id,
                "task": _redact(a.task[:80]),
                "agent": _redact(a.agent),
                "parent": a.parent_session_key,
                "rss_mb": round(a.last_rss_gb * 1024, 1),
                "peak_rss_mb": round(a.peak_rss_gb * 1024, 1),
                "cpu_cores": round(a.last_cpu_cores, 2),
                "procs": a.last_procs,
                "mcp": a.last_stubs,
                "started_at": a.started,
                "shared": a._session_sharing,
                "pid": a._pid,
                "sampled": a.last_rss_gb > 0.0 or a.peak_rss_gb > 0.0,
            }
            for a in self._agents.values()
            if not a.done and not a.queued
        ]

    def spawn(
        self,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        max_turns: int = 0,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        bare: bool = False,
        cwd: str = "",
        approval_mode: str | None = None,
        silent: bool = False,
        batch_id: str = "",
        batch_total: int = 0,
        keep: bool = False,
        conversation_key: str = "",
        app: str = "",
        include_memory: bool = True,
        include_lessons: bool = True,
        include_project: bool = True,
        _agent_prevalidated: bool = False,
        _from_queue: bool = False,
        _preassigned_id: str = "",
    ) -> SubagentInfo | None:
        """Spawn a subagent for *task*.

        Approval priority (first match wins):

        1. YOLO mode → immediate execution
        2. ``approval_mode="auto"`` from caller → immediate execution
        3. ``auto_approve_subagent_spawn`` config → auto-approved execution
        4. ``on_spawn_approval`` callback → interactive approval
        5. Otherwise → rejected

        When ``approval_mode="auto"`` is set, it has two effects:
        - Skips the spawn approval gate (this method)
        - Sets the subagent's session-level tool approval policy to
          "auto" in ``_run_inner()``, meaning all tool calls within
          the subagent are auto-approved for its entire lifetime.

        This dual behavior is intentional for headless callers (e.g.
        Mochi bg agent) that have no UI to respond to approval prompts.
        The parameter is only accepted via the internal ``POST /api/spawn``
        endpoint (requires X-Internal-Secret), not from LLM tool calls.

        Args:
            task (str): The prompt/task description for the subagent.
            parent_session_key (str): Session key of the caller.
            agent (str): Agent name override (default: "kirocrew").
            model (str): Model override for CC provider (ignored for ACP).
            allowed_tools (list): Tool allowlist for CC provider (ignored for ACP).
            bare (bool): Launch CC in bare mode (ignored for ACP).
            cwd (str): Optional absolute path where the subagent subprocess
                launches instead of the default ``subagent_<id>`` sandbox.
                Validated against ``AgentConfig.subagent_cwd_allowed_roots``;
                rejected spawns return a done ``SubagentInfo`` with ``error``
                set. Enables cwd-relative resource globs (``AGENTS.md``,
                ``.kiro/steering``, ``CLAUDE.md``) to resolve correctly.
            approval_mode (str | None): "auto" to skip spawn gate and
                set session-level auto-approve.  Only honored from
                authenticated internal callers (X-Internal-Secret).
            silent (bool): Suppress completion notifications.

        Returns:
            SubagentInfo | None: Agent metadata, or None if at capacity.
        """
        # Identity is assigned ONCE, here, and used by every exit path — the
        # queued return, each rejection, and the started record. That is what
        # makes the id the caller is handed the id it will actually see again:
        # ``spawn_run`` prints this id into its wave roster, and the dashboard
        # resolves a wave by matching those printed ids against live per-agent
        # events. A drained spawn passes the id it was queued under back in via
        # ``_preassigned_id``, so a member that waits behind the stagger /
        # concurrency gate keeps its identity across the round-trip instead of
        # being announced under one id and starting under another.
        agent_id: str = _preassigned_id or uuid.uuid4().hex[:8]
        # Submission accounting: count this member as
        # submitted BEFORE any rejection or queue/registration branching. A
        # member refused below (empty task, low memory, bad cwd, governance)
        # never registers and never completes — if it weren't counted here,
        # batch_members_pending() would see submitted < expected FOREVER and
        # the wave digest would never fire, permanently stranding every
        # sibling's held result. A queued member re-enters
        # spawn() via _drain_queue — never double-count it.
        if batch_id and not _from_queue:
            _bs = self._batch_submitted.setdefault(batch_id, [0, max(0, int(batch_total))])
            _bs[0] += 1
            self._batch_progress_ts[batch_id] = time.time()
        # --- Task guard: refuse empty/whitespace-only tasks (defense in depth).
        # The HTTP handler (api_spawn) and MCP tool schemas validate too, but
        # direct Python callers reach this choke point unvalidated. An empty
        # task produces a useless subagent and a blank Activity card. Must run
        # BEFORE the redaction below, which would raise on a None task. ---
        if not task or not task.strip():
            logger.warning("Subagent spawn refused: empty task (parent=%s)", parent_session_key)
            # Audit is best-effort: the rejection must be returned even if
            # SEL is unavailable (a graceful refusal must not become an
            # unhandled exception in api_spawn / MCP tool callers).
            try:
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_empty_task",
                    metadata={"agent": agent},
                )
            except Exception:
                logger.debug("SEL audit failed for empty-task rejection", exc_info=True)
            return self._announce_rejection(
                SubagentInfo(
                    id=agent_id,
                    task="",
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error="spawn refused: task must be a non-empty string",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        # --- Redact task once for all SubagentInfo storage (raw task kept for kiro-cli prompt) ---
        _redacted_task = redact_credentials(redact_exfiltration_urls(task)[0])[0]

        # --- Memory guard: refuse to spawn if system memory is critically low ---
        try:
            min_mem = KiroCrewConfig.load().agent.spawn_min_memory_gb
        except Exception:
            min_mem = 4.0
        mem_ok, avail_gb = check_memory_available(min_gb=min_mem)
        if not mem_ok:
            logger.warning(
                "Subagent spawn refused: only %.2f GB available (min %.1f GB required)",
                avail_gb,
                min_mem,
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="refused_low_memory",
                metadata={
                    "available_gb": avail_gb,
                    "min_gb": min_mem,
                    "task": _redacted_task[:120],
                },
            )
            info = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                parent_session_key=parent_session_key,
                done=True,
                error=f"spawn refused: only {avail_gb:.1f} GB memory available (need {min_mem:.0f} GB)",
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )
            return self._announce_rejection(info)

        # --- Admission gate: refuse NEW spawns while host memory posture is
        # critical. Complements the absolute spawn_min_memory_gb floor above
        # with the posture tier (resource_critical_gb) and shares its
        # off-switch (agent.admission_gate) with the cron scheduler's deferral
        # gate. This method is sync and runs on the gateway event loop, so it
        # reads the CACHED off-thread verdict — never inline config/procfs
        # I/O; bounded staleness is acceptable for pressure-shedding.
        # In-flight subagents are untouched; direct user chat turns are
        # not gated; fails open on an unknown posture. ---
        admission = cached_admission_check()
        if not admission.admitted:
            logger.warning("Subagent spawn refused: %s", admission.reason)
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="refused_memory_critical",
                metadata={
                    "available_gb": admission.available_gb,
                    "posture": admission.posture,
                    "task": _redacted_task[:120],
                },
            )
            info = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                parent_session_key=parent_session_key,
                done=True,
                error=f"spawn refused: {admission.reason}",
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )
            return self._announce_rejection(info)

        # --- CWD validation: reject bad paths before consuming a slot ---
        resolved_cwd = ""
        if cwd:
            try:
                allowed_roots = KiroCrewConfig.load().agent.subagent_cwd_allowed_roots
            except Exception:
                # Fail closed: if config is unavailable, treat cwd override as
                # disabled. Defaulting to the permissive default here would
                # silently re-enable the feature for admins who set
                # subagent_cwd_allowed_roots=[] to disable it.
                allowed_roots = []
            resolved_cwd, cwd_err = validate_cwd(cwd, allowed_roots)
            if cwd_err:
                logger.warning("Subagent spawn refused: invalid cwd %r: %s", cwd, cwd_err)
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_invalid_cwd",
                    metadata={"cwd": cwd[:200], "reason": cwd_err, "task": _redacted_task[:120]},
                )
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"spawn refused: {cwd_err}",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                return self._announce_rejection(info)

        # --- Governance: spawn capability gate (blast-radius containment) ---
        # A policy/profile may disable sub-agent spawning entirely, or bound it
        # to named agents (capabilities.spawn.scopes.agents).  Resolved against
        # the PARENT surface so a per-app/per-surface profile contains what it
        # can spawn — even if the kiro side would allow it.
        gov_spawn_err = _vet_spawn_governance(parent_session_key, agent, app=app)
        if gov_spawn_err:
            logger.warning("Subagent spawn refused by governance: %s", gov_spawn_err)
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error=gov_spawn_err,
                metadata={"agent": agent, "task": _redacted_task[:120]},
            )
            return self._announce_rejection(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"spawn refused by governance: {gov_spawn_err}",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        now = time.monotonic()
        should_queue, slot_free = self._should_stagger_queue(now)
        if should_queue:
            # A prevalidated app spawn must NOT sit in the queue. _agent_prevalidated
            # skips the agent-directory ownership scan on drain (it was validated
            # off the loop at request time); if it waited in the queue, the app
            # could be disabled and its agent file removed meanwhile, and the drain
            # would then run a same-named FOREIGN agent under the app's auto-approval
            # without re-checking ownership. Fail closed: reject so the caller
            # re-requests and re-validates ownership fresh. Only the app SpawnSDK
            # sets _agent_prevalidated, and because such a spawn never enters the
            # queue, a drain re-entry (_from_queue) never carries the flag.
            if _agent_prevalidated:
                logger.warning(
                    "Rejecting prevalidated app spawn that would queue "
                    "(agent=%s, app=%s): retry to revalidate ownership",
                    agent,
                    app,
                )
                return self._announce_rejection(
                    SubagentInfo(
                        id=agent_id,
                        task=_redacted_task,
                        agent=agent,
                        parent_session_key=parent_session_key,
                        done=True,
                        error=(
                            "spawn queue is at capacity; the app spawn was not queued "
                            "to avoid a stale ownership check — retry to revalidate and "
                            "spawn"
                        ),
                        batch_id=batch_id,
                        batch_total=max(0, int(batch_total)),
                    )
                )
            # Carry this spawn's id (assigned at the top) in the queue entry so
            # the drained spawn runs under it. The identity must survive the
            # round-trip because it is the only handle the caller gets: spawn_run
            # prints the id this call returns, and the inline SubagentRunCard
            # resolves a wave by matching those printed ids against live
            # per-agent events. Returning a throwaway sentinel (the old
            # ``q<n>``) and minting a fresh uuid on drain meant every wave member
            # after the first was announced under an id no agent ever had — with
            # the default 2s stagger that is EVERY member after the first, so a
            # 2-agent wave permanently rendered "1 agent running" while the
            # sidebar and Subagents panel correctly showed 2.
            self._queue.append(
                {
                    "task": task,
                    "parent_session_key": parent_session_key,
                    "agent": agent,
                    "max_turns": max_turns,
                    "model": model,
                    "allowed_tools": allowed_tools,
                    "bare": bare,
                    "cwd": resolved_cwd,
                    "approval_mode": approval_mode,
                    "silent": silent,
                    "batch_id": batch_id,
                    "batch_total": batch_total,
                    "keep": keep,
                    "conversation_key": conversation_key,
                    "app": app,
                    "include_memory": include_memory,
                    "include_lessons": include_lessons,
                    "include_project": include_project,
                    "_agent_prevalidated": _agent_prevalidated,
                    "_preassigned_id": agent_id,
                }
            )
            logger.info(
                "Subagent queued (%d running, %d queued, slot_free=%s)",
                self._running_count,
                len(self._queue),
                slot_free,
            )
            # Advisory UI signal: tell the chip how many agents are now waiting
            # to start for this parent so it can appear immediately and show a
            # "waiting" count instead of only running/completed ones.
            self._emit_queue_depth(parent_session_key, batch_id)
            # If a slot is free, no running agent will trigger the drain on
            # completion — schedule the staggered pump at the interval boundary
            # so the queued spawn still launches.
            if slot_free:
                delay = max(0.0, self._spawn_stagger_secs - (now - self._last_spawn_ts))
                try:
                    asyncio.get_event_loop().call_later(delay, self._drain_queue)
                except RuntimeError:
                    pass  # no running loop (sync/test context)
            info = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                app=app,
                queued=True,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                include_memory=include_memory,
                include_lessons=include_lessons,
                include_project=include_project,
            )
            return info

        # `_agent_prevalidated` skips the on-loop agent-directory scan: a caller
        # that already confirmed the agent exists OFF the loop (the app SpawnSDK
        # validates via `list_agents()` in a thread) would otherwise make
        # `_validate_agent` re-scan/stat every agent file synchronously here,
        # stalling chat and the heartbeat on a populated agents directory. Only
        # the app path sets it; every other caller still validates inline.
        if agent and not _agent_prevalidated:
            # Validate against the cwd the subagent will ACTUALLY run in. When no
            # explicit cwd was given the runtime falls back to the session pool's
            # cwd, so validating only the explicit value refused a project agent
            # kiro-cli would have loaded — the same interface asymmetry the project
            # scope exists to remove, just one layer down.
            effective_cwd = resolved_cwd or str(getattr(self._sessions, "_pool_cwd", "") or "")
            agent, err = _validate_agent(agent, effective_cwd)
            if err:
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent="",
                    parent_session_key=parent_session_key,
                    done=True,
                    error=err,
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                return self._announce_rejection(info)

        info = SubagentInfo(
            id=agent_id,
            task=_redacted_task,
            parent_session_key=parent_session_key,
            agent=agent,
            app=app,
            approval_mode=approval_mode or "",
            silent=silent,
            max_turns=max_turns,
            model=model or "",
            allowed_tools=list(allowed_tools) if allowed_tools else [],
            bare=bare,
            cwd=resolved_cwd,
            batch_id=batch_id,
            batch_total=max(0, int(batch_total)),
            keep=keep,
            conversation_key=conversation_key,
            include_memory=include_memory,
            include_lessons=include_lessons,
            include_project=include_project,
        )
        info._raw_task = task  # unredacted prompt for kiro-cli execution
        self._agents[agent_id] = info
        self._running_count += 1
        self._last_spawn_ts = time.monotonic()  # stagger gate: one start per interval
        # Batch lifecycle: announce the wave ONCE, on its first member to
        # actually start (queued members haven't started yet — the event marks
        # execution begin, and the UI uses it to key batch progress).
        if batch_id and batch_id not in self._seen_batches:
            self._seen_batches.add(batch_id)
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(
                    self._fire_event(
                        "spawn_batch_started",
                        info,
                        {"batch_id": batch_id, "count": info.batch_total},
                    )
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)

        # Check parent session trust (approval_policy="auto") set by dashboard trust toggle.
        parent_trusted = (
            parent_session_key and self._sessions.get_approval_policy(parent_session_key) == "auto"
        )

        if self._is_yolo and self._is_yolo():
            self._tasks[agent_id] = asyncio.create_task(self._run(info))
            self._log_spawned(info)
        elif approval_mode == "auto":
            self._tasks[agent_id] = asyncio.create_task(self._run(info))
            self._log_spawned(info)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="auto_approved_spawn",
                metadata={"subagent_id": agent_id, "reason": "approval_mode_auto"},
            )
        elif parent_trusted:
            self._tasks[agent_id] = asyncio.create_task(self._run(info))
            self._log_spawned(info)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="auto_approved_spawn",
                metadata={"subagent_id": agent_id, "reason": "parent_trusted"},
            )
        elif self._ctx_builder and self._ctx_builder.hooks:
            if self._ctx_builder.hooks.auto_approve_subagent_spawn is True:
                self._tasks[agent_id] = asyncio.create_task(self._run(info))
                self._log_spawned(info)
                sel().log_tool_invocation(
                    session_key=info.parent_session_key,
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="auto_approved_spawn",
                    metadata={"subagent_id": agent_id, "reason": "tool_calls_gated"},
                )
            elif self._on_spawn_approval:
                self._tasks[agent_id] = asyncio.create_task(self._spawn_with_approval(info))
            else:
                info.done = True
                info.error = "spawn rejected: no approval mechanism configured"
                self._running_count -= 1
                self._drain_queue()
                sel().log_tool_invocation(
                    session_key=info.parent_session_key,
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_spawn",
                    metadata={"subagent_id": agent_id, "reason": "no_approval_mechanism"},
                )
                # Batch members must still reach the gateway's completion
                # consumer: this is a REGISTERED rejection
                # (done=True in _agents), so batch_members_pending() already
                # counts it as complete — without an announce, a wave whose
                # final member lands here closes with no event and every held
                # sibling digest strands forever.
                return self._announce_rejection(info)
        elif self._on_spawn_approval:
            self._tasks[agent_id] = asyncio.create_task(self._spawn_with_approval(info))
        else:
            info.done = True
            info.error = "spawn rejected: no approval mechanism configured"
            self._running_count -= 1
            self._drain_queue()
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata={"subagent_id": agent_id, "reason": "no approval mechanism"},
            )
            logger.warning("Subagent %s rejected: no approval callback", agent_id)
            if self._on_done:
                self._tasks[agent_id] = asyncio.ensure_future(self._safe_announce(info))

        return info

    async def _safe_announce(self, info: SubagentInfo) -> None:
        """Notify completion callback with error handling.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._on_done is not None
        try:
            await self._on_done(info)
        except Exception:
            logger.exception("Subagent announce failed for %s", info.id)

    def _announce_rejection(self, info: SubagentInfo) -> SubagentInfo:
        """Route a terminal spawn rejection through the done callback.

        A rejected batch member is counted as submitted (top of ``spawn``)
        but never registers and never reaches ``_run``'s completion path.
        Without an announce, the gateway's wave accounting never sees its
        terminal state — and when the rejection is the wave's FINAL
        submission, no later completion event re-evaluates the wave, so
        every sibling result already held for the digest strands forever.
        Announcing lets ``_subagent_done`` count the member
        as failed and release the digest when it closes the wave.

        Non-batch rejections skip the announce: the caller already receives
        the error synchronously in the returned info, and injecting a
        completion turn for them would double-report. That holds for
        queue-drained non-batch rejections too — ``_drain_queue`` announces
        those itself off the returned info, so announcing here as well would
        inject the completion twice.
        """
        if info.batch_id and self._on_done:
            try:
                self._tasks[f"reject-{info.id}"] = asyncio.ensure_future(self._safe_announce(info))
            except RuntimeError:
                pass  # no running loop (sync/test context)
        return info

    def _should_stagger_queue(self, now: float) -> tuple[bool, bool]:
        """Decide whether a spawn arriving at *now* must be queued.

        Returns ``(should_queue, slot_free)``. A spawn is queued when either no
        slot is free (at capacity) OR a spawn started within the stagger window
        (``subagent_spawn_stagger_secs``) — so the initial fill never bursts and
        no two agents start within the interval (dynamic-subagent-sizing.md §5.3).
        """
        slot_free = self._running_count < self._max_concurrent
        too_soon = (now - self._last_spawn_ts) < self._spawn_stagger_secs
        return (not slot_free or too_soon, slot_free)

    # ── Continuable conversations (keep=True) ─────────────────────────────

    def _conversation_busy(self, conv_key: str) -> SubagentInfo | None:
        """Return the live or QUEUED run on *conv_key*, or None.

        Queued members matter: a continuation waiting
        in the spawn queue is not in ``_agents`` yet — missing it would let
        ``spawn_release`` delete the session files it needs (the accepted run
        would then die with ``resume_failed``), or let a second continue race
        the same conversation.
        """
        for a in self._agents.values():
            if not a.done and (a.conversation_key or f"subagent:{a.id}") == conv_key:
                return a
        for p in self._queue:
            pkey = str(p.get("conversation_key") or "") or (
                f"subagent:{p.get('_preassigned_id', '')}"
            )
            if pkey == conv_key:
                return SubagentInfo(
                    id=str(p.get("_preassigned_id") or "queued"),
                    task="",
                    queued=True,
                )
        return None

    def _keep_recorded_on_disk(self, key: str) -> bool:
        """Disk-truth continuable check for SessionManager's cache (#1115).

        True iff *key* is a subagent conversation whose run's ``state.json``
        records ``keep`` — the single persisted source of retention intent.
        Non-subagent keys short-circuit without touching disk.
        """
        if not key.startswith("subagent:"):
            return False
        conv_id = key[len("subagent:"):]
        try:
            state = read_state(conv_id) or {}
        except Exception:
            return False
        return bool(state.get("keep"))

    def _promote_conversation(self, conv_id: str, conv_key: str, last_used: float | None = None) -> None:
        """Single choke point for promoting a conversation's retention (#1115).

        Writes all three retention surfaces together so they cannot drift:
        ``keep=True`` in state.json (the persisted source of truth), the
        SessionManager continuable cache (file-deletion exemption), and the
        TTL registry entry (sweep ownership).
        """
        try:
            update_state(conv_id, keep=True)
        except Exception:
            logger.debug("promote: failed to persist keep for %s", conv_id, exc_info=True)
        self._sessions.mark_continuable(conv_key)
        self._conversations[conv_key] = last_used if last_used is not None else time.time()

    def _scan_keep_states(self) -> list[tuple[str, str, str, str, str, float]]:
        """Blocking scan for keep runs (#1114): read every ``state.json``
        under the subagents dir and collect the promoted conversations.

        Returns ``(conv_id, conv_key, sid, provider, cwd, last_used)`` tuples.
        Runs in an executor — no event-loop work here.
        """
        out: list[tuple[str, str, str, str, str, float]] = []
        try:
            base = _subagents_dir()
            entries = list(base.iterdir()) if base.is_dir() else []
        except Exception:
            return out
        for d in entries:
            try:
                if not d.is_dir():
                    continue
                state = read_state(d.name) or {}
                if not state.get("keep"):
                    continue
                conv_key = str(state.get("conversation_key") or "") or f"subagent:{d.name}"
                conv_id = conv_key[len("subagent:"):]
                sid = str(state.get("session_id") or "")
                last_used = float(state.get("updated_at") or state.get("started") or 0.0)
                out.append((
                    conv_id, conv_key, sid,
                    str(state.get("provider") or PROVIDER_LABEL_DEFAULT),
                    str(state.get("cwd") or ""),
                    last_used,
                ))
            except Exception:
                logger.debug("registry rebuild: skipping %s", d, exc_info=True)
        return out

    async def _rebuild_conversation_registry(self) -> None:
        """Re-seed the conversation TTL registry from disk after a restart (#1114).

        The registry (``_conversations`` + the SessionManager continuable
        cache + session map) is in-memory; without this, a gateway restart
        orphans promoted conversations — the TTL sweep no longer knows them,
        and nothing else deletes their session files (the tombstone pruner
        skips keep runs by design). Runs on the reaper's first pass (retried
        until it succeeds); entries already past TTL are released by the
        very next sweep.

        Threading contract (Arbiter, PR #1246): ONLY the pure-read
        ``_scan_keep_states`` runs in the executor. All ``SessionMap``
        access (``resumable_sid`` self-prune, ``seed_conversation`` writes)
        stays on the event loop — the map is an unlocked dict with
        whole-file saves, concurrently mutated by ``get_or_create`` /
        ``close_all``, so touching it from a worker thread races restart
        cold-starts (lost mappings / dict-changed-size errors). Per-entry
        work is small and bounded by the keep-run count, and the loop
        yields between entries so a large batch cannot stall chat turns
        (the round-2 event-loop concern).
        """
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, self._scan_keep_states)
        # Newest record wins: a conversation appears once per run that
        # touched it (original + each continuation). The first record kept
        # for a conv_key wins the `in self._conversations` guard below, so
        # iterate newest-first — an oldest-first order would seed a stale
        # last_used and let the SAME pass's sweep expire (and delete) a
        # conversation whose real last-use is recent.
        found.sort(key=lambda t: t[5], reverse=True)
        seeded = 0
        for conv_id, conv_key, sid, provider, cwd, last_used in found:
            if conv_key in self._conversations:
                continue  # live registration wins over the disk snapshot
            # Same on-demand seeding as continue_conversation (also on-loop):
            # the map entry is what makes release_conversation able to find
            # and delete files.
            if sid and not self._sessions.resumable_sid(conv_key):
                self._sessions.seed_conversation(conv_key, sid, provider=provider, cwd=cwd)
            # Resumability gate: SessionMap.get self-prunes entries whose
            # session files are missing, so this also rejects RELEASED
            # conversations whose continuation runs still carry a stale
            # keep=True in their own state.json — their files are gone, and
            # re-owning them would resurrect a released conversation.
            if not self._sessions.resumable_sid(conv_key):
                continue
            self._sessions.mark_continuable(conv_key)
            self._conversations[conv_key] = last_used or time.time()
            seeded += 1
            # Cooperative yield: keep restart cold-start turns responsive
            # while a large keep batch seeds (one small file write each).
            await asyncio.sleep(0)
        if seeded:
            logger.info("Rebuilt conversation TTL registry from disk: %d conversation(s)", seeded)

    def continue_conversation(
        self,
        conv_id: str,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        model: str | None = None,
        max_turns: int = 0,
        cwd: str = "",
        _preassigned_id: str = "",
    ) -> SubagentInfo | None:
        """Dispatch a follow-up *task* into conversation *conv_id*.

        ``_preassigned_id`` mirrors ``spawn``: a caller that must persist the
        dispatch identity BEFORE the side effect (so a crash in between is
        recoverable rather than ambiguous) supplies the id it already wrote
        down, instead of discovering the minted one only on return.

        Retain-by-default: works on ANY completed run whose session files are
        still on disk — no keep flag needed at spawn time. Every run's sid /
        provider / cwd are already recorded in its ``state.json``; this seeds
        the session map on demand, so ``get_or_create`` finds the sid and arms
        ``session/load``. Continuing a run PROMOTES it: retention extends from
        the tombstone-prune window (~1h) to the conversation TTL, until
        ``spawn_release``.

        Mints a NEW run (new id, own state.json / result.txt / completion
        event) on the SAME session key, so the follow-up executes with the
        conversation's accumulated context.

        Typed failures (returned as a done SubagentInfo with ``error``):
        - ``conversation_busy`` — a run is in flight; use spawn_steer.
        - ``conversation_gone`` — no resumable session files remain.
        """
        conv_key = f"subagent:{conv_id}"
        busy = self._conversation_busy(conv_key)
        if busy is not None:
            info = SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task=_redact(task),
                done=True,
                parent_session_key=parent_session_key,
                error=(
                    f"conversation_busy: run {busy.id} is in flight on this "
                    "conversation — use spawn_steer to inject into it, or wait "
                    "for its completion event"
                ),
            )
            return info
        # Seed the session map from the run's state.json when no mapping
        # exists yet (default runs never write one at spawn; the map is also
        # in-memory-lost across gateway restarts while state.json persists).
        if not self._sessions.resumable_sid(conv_key):
            state = read_state(conv_id) or {}
            sid = str(state.get("session_id") or "")
            if sid:
                self._sessions.seed_conversation(
                    conv_key,
                    sid,
                    provider=str(state.get("provider") or PROVIDER_LABEL_DEFAULT),
                    cwd=str(state.get("cwd") or ""),
                )
        # Re-check: SessionMap.get self-prunes entries whose session files
        # are missing, so a surviving mapping == resumable files on disk.
        if not self._sessions.resumable_sid(conv_key):
            # Point the caller at the prior result if the run folder survives
            # (result.txt outlives the session under the tombstone TTL).
            result_hint = ""
            try:
                _rp = agent_dir_for_display(conv_id) / "result.txt"
                if _rp.exists():
                    result_hint = f" Prior result still readable at: {_rp}"
            except Exception:
                pass
            info = SubagentInfo(
                id=uuid.uuid4().hex[:8],
                task=_redact(task),
                done=True,
                parent_session_key=parent_session_key,
                error=(
                    "conversation_gone: no resumable session remains for "
                    f"{conv_id} (expired, released, or files pruned)."
                    + result_hint
                    + " Re-spawn with a fresh task carrying a summary."
                ),
            )
            return info
        # Promote the run's retention through the single choke point
        # (#1115): state.json keep=True (tombstone pruner skips deletion),
        # the SessionManager continuable cache, and the TTL registry entry.
        # The conversation TTL sweep / spawn_release owns deletion from here.
        self._promote_conversation(conv_id, conv_key)
        inc_memory, inc_lessons, inc_project = self._inherited_context_groups(conv_id)
        # A continuation has to run WHERE THE RUN RAN. `spawn` resolves an empty
        # cwd to the pool project before it validates the agent name, so a run
        # spawned against a project-local agent (defined under that project's
        # .kiro/agents/) came back "unknown agent" here — and the caller reads any
        # non-busy error as unresumable and respawns from the digest alone,
        # silently dropping the conversation this call exists to preserve.
        #
        # The cwd must come from the CALLER, not be discovered here. This method is
        # synchronous and runs on the gateway's event loop, so probing the recorded
        # path (`is_dir()`) would freeze the gateway for as long as a stalled
        # network mount takes to answer. Async callers resolve it off-loop instead:
        # crew passes its slot project, and `recorded_cwd()` gives the others the
        # run's own recorded path to hand back in.
        return self.spawn(
            task,
            _preassigned_id=_preassigned_id,
            parent_session_key=parent_session_key,
            agent=agent,
            model=model,
            max_turns=max_turns,
            keep=True,
            cwd=cwd,
            conversation_key=conv_key,
            include_memory=inc_memory,
            include_lessons=inc_lessons,
            include_project=inc_project,
        )

    def recorded_cwd(self, conv_id: str) -> str:
        """The cwd run *conv_id* executed in, or "" if it never had one.

        `continue_conversation` deliberately does NOT discover this itself: it is
        synchronous and runs on the gateway's event loop, where the state read would
        block for as long as a stalled network mount takes to answer. This helper
        does the blocking work in one place so an async caller can hand it to
        `asyncio.to_thread` and pass the result in.

        A path that no longer exists is returned ANYWAY, so `spawn` refuses it.
        Round 35 filtered it to "" to keep such a continuation working, which was
        the wrong trade: an empty cwd resolves to the POOL project, so a follow-up
        whose task names relative files would have edited an unrelated project's
        working tree. A loud refusal is recoverable; a silent write to the wrong
        repository is not. Only a run that never recorded a cwd returns "" — for it
        the pool default is correct, because there is no project to miss.
        """
        return str((read_state(conv_id) or {}).get("cwd") or "")

    def _inherited_context_groups(self, conv_id: str) -> tuple[bool, bool, bool]:
        """Recover the context scope of the run being continued.

        A continuation DOES rebuild session context: ``get_or_create`` reports
        ``is_new=True`` even when it restores the session via ``session/load``
        (``resumed`` is the separate flag, and it gates only thread history), so
        ``build_message`` runs the full session-context path for the follow-up
        turn. Without inheriting the scope here, a run the parent deliberately
        spawned without memory would silently regain it on continuation.

        Prefers the live record; falls back to the scope persisted in the run's
        ``state.json``. A run that predates the field records no scope at all,
        which is distinguishable from "every group withheld" (an empty string)
        and defaults to all-on.
        """
        live = self._agents.get(conv_id)
        if live is not None:
            return live.include_memory, live.include_lessons, live.include_project
        raw = (read_state(conv_id) or {}).get("context_groups")
        if raw is None:
            return True, True, True
        groups = {g for g in str(raw).split(",") if g}
        return (
            CONTEXT_GROUP_MEMORY in groups,
            CONTEXT_GROUP_LESSONS in groups,
            CONTEXT_GROUP_PROJECT in groups,
        )

    async def steer_run(self, agent_id: str, message: str) -> tuple[bool, str]:
        """Inject *message* into the RUNNING turn of run *agent_id*.

        Returns ``(ok, detail)``. Typed detail values on refusal:
        ``not_found`` (unknown id), ``not_running`` (run finished — use
        spawn_continue), ``session_starting`` (run alive but its session has
        not registered yet — retry shortly), ``no_session`` (session not
        reachable), or the provider's failure reason.

        Startup grace (#1113): the window between spawn-return and session
        registration is precisely when a parent most wants to steer (it just
        realized the task text was wrong), so a missing provider on a live
        run polls for up to ``_STEER_STARTUP_WAIT_SECS`` instead of failing
        immediately with a bare ``no_session``.
        """
        info = self._agents.get(agent_id)
        if info is None:
            return False, "not_found"
        if info.done:
            return False, "not_running: run finished — use spawn_continue"

        def _resolve_provider() -> Any:
            if info._session_sharing and info._shared_provider is not None:
                return info._shared_provider
            session_key = info.conversation_key or f"subagent:{info.id}"
            return self._sessions.get_provider(session_key)

        provider: Any = _resolve_provider()
        if provider is None or not hasattr(provider, "steer"):
            # Bounded wait for session registration on a run that is still
            # alive. Re-checks done-ness each tick: a run finishing while we
            # wait flips the answer to not_running, never a stale inject.
            deadline = time.monotonic() + _STEER_STARTUP_WAIT_SECS
            while time.monotonic() < deadline:
                await asyncio.sleep(_STEER_STARTUP_POLL_SECS)
                if info.done:
                    return False, "not_running: run finished — use spawn_continue"
                provider = _resolve_provider()
                if provider is not None and hasattr(provider, "steer"):
                    break
            else:
                return False, (
                    "session_starting: the run is alive but its session has "
                    f"not registered within {_STEER_STARTUP_WAIT_SECS}s — "
                    "retry in a few seconds"
                )
        try:
            ok = await provider.steer(message)
        except Exception as exc:  # pragma: no cover - provider-specific
            logger.warning("steer_run %s failed", agent_id, exc_info=True)
            return False, f"steer failed: {exc}"
        if ok:
            try:
                sel().log_tool_invocation(
                    session_key=info.parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_steer",
                    outcome="ok",
                    metadata={"subagent_id": agent_id},
                )
            except Exception:
                logger.debug("steer_run: SEL audit failed", exc_info=True)
        return ok, "ok" if ok else "steer rejected by provider"

    # Bounds for the follow_up watcher: poll cadence, post-done busy retries
    # (finalization may briefly hold the conversation), and a hard deadline so
    # a wedged run can never leave an immortal watcher behind.
    _FOLLOWUP_POLL_SECS = 2.0
    _FOLLOWUP_BUSY_RETRIES = 10
    _FOLLOWUP_BUSY_RETRY_SECS = 3.0

    async def follow_up_run(self, agent_id: str, message: str) -> tuple[bool, str]:
        """Queue *message* for delivery AFTER run *agent_id*'s turn completes.

        The non-interrupting sibling of :meth:`steer_run` (spawn_steer
        ``mode="follow_up"``): instead of injecting into the running turn —
        which can derail critical work mid-execution — the message waits for
        the run to finish and is then dispatched as a CONTINUATION on the
        run's own conversation (``continue_conversation``), executing with its
        accumulated context. The continuation is a new run whose result
        arrives as a normal completion event on the same parent session.

        Multiple queued follow-ups drain as ONE continuation (joined in
        arrival order), so three corrections cost one run, not three.

        Returns ``(ok, detail)``. Typed refusals mirror ``steer_run``:
        ``not_found`` (unknown id) and ``not_running`` (already finished —
        ``spawn_continue`` is the direct tool for that case). Queued
        follow-ups are best-effort by design: if the conversation is gone by
        the time the run ends, the failure is logged and audited, not raised.
        """
        info = self._agents.get(agent_id)
        if info is None:
            return False, "not_found"
        if info.done:
            return False, "not_running: run finished — use spawn_continue"
        if self._shutting_down:
            # Refuse rather than accept-and-drop: an accepted follow-up
            # promises a completion event, and a shutting-down gateway can
            # keep neither the watcher nor the continuation alive.
            return False, "shutting_down: the gateway is stopping — re-send after restart"
        info.pending_followups.append(message)
        if not info._followup_watcher:
            self._arm_followup_watcher(info)
        try:
            sel().log_tool_invocation(
                session_key=info.parent_session_key or "",
                source="subagent",
                tool_name="spawn_steer",
                outcome="followup_queued",
                metadata={"subagent_id": agent_id, "queued": len(info.pending_followups)},
            )
        except Exception:
            logger.debug("follow_up_run: SEL audit failed", exc_info=True)
        return True, "queued"

    def _arm_followup_watcher(self, info: SubagentInfo) -> None:
        """Arm the (single) follow-up watcher for *info*'s run.

        The done-callback resets the one-watcher latch AND re-arms when
        messages are still pending: a follow-up can be accepted while the
        previous watcher is inside its final awaits (announcing an expiry) —
        it sees the latch still true and arms nothing, so without the re-arm
        that accepted message would be stranded with no dispatch and no event
        (GPT review). Not re-armed during shutdown or once the run record is
        gone (removal drops any leftovers deliberately).
        """
        info._followup_watcher = True
        run_id = info.id
        run_info = info  # narrowed local: mypy loses the None-narrow in closure defaults
        task = asyncio.create_task(self._deliver_followups(info))
        self._followup_watchers[run_id] = task

        def _done(
            t: "asyncio.Task", _id: str = run_id, _info: SubagentInfo = run_info
        ) -> None:
            self._followup_watchers.pop(_id, None)
            _info._followup_watcher = False
            if not t.cancelled() and t.exception() is not None:
                logger.warning(
                    "follow_up watcher for %s failed", _id, exc_info=t.exception()
                )
                return
            if (
                not t.cancelled()
                and _info.pending_followups
                and not self._shutting_down
                and _id in self._agents
            ):
                self._arm_followup_watcher(_info)

        task.add_done_callback(_done)

    async def _deliver_followups(self, info: SubagentInfo) -> None:
        """Watch run *info* until its turn completes, then dispatch the queue.

        DELIBERATELY a per-run poller rather than a hook in ``_run``'s
        finalization: completion is reached from many terminal paths (normal,
        error, timeout, cancel-recovery, reaper), all guarded by a carefully
        ordered 3-guard finally — a watcher observes the outcome without
        adding a new obligation to any of them. Waits for the run's task to be
        popped from ``self._tasks`` too, so teardown (session release) has
        finished before the continuation tries to reuse the conversation; any
        residual ``conversation_busy`` gets a bounded retry.

        OUTCOME-AWARE: a run the user explicitly STOPPED does not get its
        follow-ups dispatched — resurrecting work the user killed is the
        opposite of "the correction can wait" (``followup_suppressed`` audit).
        Other non-success terminals (error, timeout) still dispatch: the
        continuation runs with the conversation's context, so "fix what just
        broke" is a legitimate follow-up.

        NEVER SILENT: the spawn_steer reply promised the parent a completion
        event, so every path that cannot deliver one from a real continuation
        (suppressed, expired, dispatch failure) announces a SYNTHETIC failure
        completion event through the normal ``_on_done`` path — the parent
        must not wait forever on an event that is not coming.

        Hard-bounded and manager-owned: gives up at the manager's run timeout
        plus a margin, and the task is registered in ``_followup_watchers`` so
        ``cancel_all()`` cancels it — a watcher must never dispatch a fresh
        run into a shutting-down gateway.
        """
        deadline = time.monotonic() + self._default_timeout + 300
        while time.monotonic() < deadline:
            if info.done and info.id not in self._tasks:
                break
            await asyncio.sleep(self._FOLLOWUP_POLL_SECS)
        else:
            dropped = list(info.pending_followups)
            logger.warning(
                "follow_up watcher for %s timed out before the run completed — "
                "%d queued message(s) dropped",
                info.id,
                len(dropped),
            )
            self._audit_followup(info, "followup_expired")
            await self._announce_followup_failure(
                info,
                "follow_up expired: the run never completed within its timeout "
                "window; the queued follow-up message(s) were dropped",
                messages=dropped,
            )
            # Drop ONLY what was just reported dropped, and only AFTER the
            # announce settled — clearing first meant a shutdown cancelling
            # this task mid-announce left the queue empty for cancel_all()'s
            # sweep, so the messages vanished with no event. The slice keeps
            # anything queued while we were announcing (the done-callback
            # re-arms for it).
            info.pending_followups = info.pending_followups[len(dropped):]
            return
        # SNAPSHOT, do not drain: messages stay in ``pending_followups`` until
        # their outcome is SETTLED (dispatched, or their failure announced).
        # An eager drain lost messages when shutdown landed mid-await — e.g.
        # during a conversation_busy retry sleep — because cancel_all() saw an
        # empty queue, cancelled this task, and nothing was ever announced.
        # Appends only ever happen at the tail, so removing the first
        # ``len(messages)`` entries at settlement drops exactly this snapshot
        # and preserves anything queued while we were dispatching.
        messages = list(info.pending_followups)
        if not messages:
            return

        def _settle() -> None:
            info.pending_followups = info.pending_followups[len(messages):]

        if info.user_stopped:
            logger.info(
                "follow_up for %s suppressed — the user stopped the run", info.id
            )
            self._audit_followup(info, "followup_suppressed")
            _settle()
            await self._announce_followup_failure(
                info,
                "follow_up suppressed: the user stopped this run, so its queued "
                "follow-up message(s) were NOT dispatched",
                messages=messages,
            )
            return
        if self._shutting_down:
            # Leave the queue intact: cancel_all()'s shutdown sweep owns the
            # announce-and-drop for pending messages.
            return
        task = "\n\n---\n\n".join(messages)
        # Finalization may hold the conversation for a beat after the task is
        # popped (shielded report); retry a bounded number of times.
        for _attempt in range(self._FOLLOWUP_BUSY_RETRIES):
            child = self.continue_conversation(
                info.id,
                task,
                parent_session_key=info.parent_session_key,
                agent=info.agent,
            )
            err = "spawn_failed" if child is None else str(getattr(child, "error", "") or "")
            if not err.startswith("conversation_busy"):
                break
            await asyncio.sleep(self._FOLLOWUP_BUSY_RETRY_SECS)
        if err:
            logger.warning(
                "follow_up delivery for %s failed: %s", info.id, err.split(":", 1)[0]
            )
            self._audit_followup(info, "followup_failed")
            _settle()
            # continue_conversation's typed failures are already done
            # SubagentInfo records — announce the real one when we have it.
            if child is not None:
                await self._announce_followup_failure(info, "", failure_info=child)
            else:
                await self._announce_followup_failure(
                    info, f"follow_up dispatch failed: {err}"
                )
        else:
            self._audit_followup(info, "followup_dispatched")
            _settle()

    async def _announce_followup_failure(
        self,
        info: SubagentInfo,
        reason: str,
        failure_info: SubagentInfo | None = None,
        messages: list | None = None,
    ) -> None:
        """Deliver a SYNTHETIC failure completion event for an undeliverable
        follow-up, through the same ``_on_done`` path as real completions.

        Best-effort by design (a notification about a failure must not itself
        take anything down), but never silent-by-default: without this the
        parent — told by spawn_steer that a completion event would arrive —
        blocks its plan on an event that only ever existed in SEL logs.
        ``messages`` labels the synthetic event when the queue was already
        drained by the caller (the expiry path clears before announcing so a
        later watcher cannot resurrect messages reported dead).
        """
        if self._on_done is None:
            return
        label_msgs = messages if messages is not None else info.pending_followups
        synthetic = failure_info or SubagentInfo(
            id=uuid.uuid4().hex[:8],
            task=f"[follow_up of run {info.id}] " + _redact(
                "; ".join(m[:120] for m in label_msgs) or "queued follow-up"
            ),
            done=True,
            parent_session_key=info.parent_session_key,
            error=reason,
        )
        try:
            await self._on_done(synthetic)
        except Exception:
            logger.warning(
                "follow_up failure announce for %s failed", info.id, exc_info=True
            )

    def _audit_followup(self, info: SubagentInfo, outcome: str) -> None:
        try:
            sel().log_tool_invocation(
                session_key=info.parent_session_key or "",
                source="subagent",
                tool_name="spawn_steer",
                outcome=outcome,
                metadata={"subagent_id": info.id},
            )
        except Exception:
            logger.debug("follow_up audit failed", exc_info=True)

    def release_conversation(self, conv_id: str) -> tuple[bool, str]:
        """Release conversation *conv_id*: forget the sid and delete files.

        Refuses (``conversation_busy``) while a run is in flight. Returns
        ``(ok, detail)``.
        """
        conv_key = f"subagent:{conv_id}"
        busy = self._conversation_busy(conv_key)
        if busy is not None:
            return False, f"conversation_busy: run {busy.id} is in flight"
        provider_label = (
            self._sessions.conversation_provider(conv_key) or PROVIDER_LABEL_DEFAULT
        )
        sid = self._sessions.forget_conversation(conv_key)
        self._conversations.pop(conv_key, None)
        # Demote the persisted source of truth too (#1115): with the disk
        # fallback in place, a stale keep=True would re-warm the continuable
        # cache after release and resurrect the conversation on the next
        # restart's registry rebuild.
        try:
            update_state(conv_id, keep=False)
        except Exception:
            logger.debug("release: failed to demote state for %s", conv_id, exc_info=True)
        if not sid:
            return False, "conversation_gone: nothing to release"
        try:
            _cleanup_session_files_sync(sid, provider_label)
        except Exception:
            logger.debug("release_conversation: file cleanup failed", exc_info=True)
        return True, "released"

    def _sweep_conversations(self, now: float) -> None:
        """Reaper hook: expire continuable conversations idle past TTL."""
        for conv_key, last_used in list(self._conversations.items()):
            if now - last_used < _CONVERSATION_TTL_SECS:
                continue
            if self._conversation_busy(conv_key) is not None:
                self._conversations[conv_key] = now  # active — refresh
                continue
            conv_id = conv_key[len("subagent:") :]
            ok, detail = self.release_conversation(conv_id)
            logger.info(
                "Conversation %s expired after %ds idle: %s",
                conv_id,
                _CONVERSATION_TTL_SECS,
                detail,
            )

    def _drain_queue(self) -> None:
        """Spawn the next queued task if a slot is available and the stagger
        interval has elapsed.

        This is the single staggered pump: at most one start per
        ``subagent_spawn_stagger_secs`` (dynamic-subagent-sizing.md §5.3). If a
        slot is free but a spawn started too recently, it reschedules itself at
        the interval boundary rather than bursting.
        """
        if not self._queue or self._running_count >= self._max_concurrent:
            return
        elapsed = time.monotonic() - self._last_spawn_ts
        if elapsed < self._spawn_stagger_secs:
            # Too soon since the last start — reschedule at the boundary.
            try:
                asyncio.get_event_loop().call_later(
                    self._spawn_stagger_secs - elapsed, self._drain_queue
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
            return
        params = self._queue.pop(0)
        # A run can be cancelled WHILE it waits here — a user stop, or a session
        # deleted out from under it. Starting it anyway would execute tools for
        # work already reported as stopped, so skip it and drain the next one
        # instead: `cancel()` marks the info terminal but cannot unqueue this.
        queued_id = str(params.get("_preassigned_id") or "")
        if queued_id:
            waiting = self._agents.get(queued_id)
            if waiting is not None and (waiting.done or waiting.user_stopped or waiting.reaped):
                logger.info("Skipping queued spawn %s: cancelled while waiting", queued_id)
                self._emit_queue_depth(
                    str(params.get("parent_session_key", "")), str(params.get("batch_id", ""))
                )
                if self._queue:
                    self._drain_queue()
                return
        logger.info(
            "Draining queue: spawning '%s' (%d left)",
            str(params.get("task", ""))[:40],
            len(self._queue),
        )
        # The popped item's parent just lost one waiting agent — re-emit its
        # queued depth (0 when this was its last) so the chip's "waiting" count
        # tracks the drain. Done before spawn() so an immediate re-queue there
        # (still too soon since last start) re-bumps it correctly afterwards.
        self._emit_queue_depth(
            str(params.get("parent_session_key", "")), str(params.get("batch_id", ""))
        )
        # spawn() re-checks the gate; since elapsed >= stagger and a slot is
        # free, it starts immediately and updates _last_spawn_ts. Forward the FULL
        # kwarg set so approval_mode / silent / model / allowed_tools / bare survive
        # the queue round-trip — including `_preassigned_id`, which makes the agent
        # start under the id its caller was already told (and, if the gate re-queues
        # it, keeps that id across the second round-trip too).
        drained = self.spawn(**params, _from_queue=True)
        # A drained spawn has NO synchronous reader: this call site is a timer
        # callback, and the original caller was handed a queued info long ago. So a
        # terminal rejection here — the cwd was deleted while the run waited, the
        # agent stopped resolving — was dropped on the floor: no completion event,
        # and the caller's own bookkeeping showed the run as still going. Crew left
        # such a topic `running` forever.
        #
        # Only for NON-batch runs, which is exactly the set `_announce_rejection`
        # skips (it announces batch members itself, from inside `spawn`). Announcing
        # regardless double-counted a queued batch rejection: the wave's own
        # accounting closed early and emitted a duplicate or incomplete digest.
        if (
            drained is not None
            and drained.done
            and drained.error
            and not drained.batch_id
            and self._on_done
        ):
            try:
                self._tasks[f"reject-{drained.id}"] = asyncio.ensure_future(
                    self._safe_announce(drained)
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
        if self._queue and self._running_count < self._max_concurrent:
            try:
                asyncio.get_event_loop().call_later(self._spawn_stagger_secs, self._drain_queue)
            except RuntimeError:
                pass

    async def _spawn_with_approval(self, info: SubagentInfo) -> None:
        """Request approval before starting the subagent.

        If approval is denied the subagent is marked as done with an
        error and the running count is decremented without executing.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._on_spawn_approval is not None
        request_id: str = f"spawn:{info.id}"
        try:
            from kiro_crew.security import (
                redact_credentials,
                redact_exfiltration_urls,
            )

            task_safe, _ = redact_exfiltration_urls(info.task)
            task_safe, _ = redact_credentials(task_safe)
            task_preview: str = task_safe[:80]
            approved: bool = await self._on_spawn_approval(
                request_id, f"spawn_run({task_preview})", info.parent_session_key
            )
        except Exception:
            logger.exception("Spawn approval failed for %s", info.id)
            approved = False

        if not approved:
            info.done = True
            info.error = "spawn rejected"
            # Slot accounting through the one-shot token, NOT a bare decrement.
            # A user Stop funnels into `_force_reap` and can land while this
            # approval is still pending (a human prompt has no deadline), and
            # `_force_reap` releases the slot and reports. A bare decrement here
            # would double-release — driving `_running_count` negative — and the
            # announce below would double-report the completion.
            if self._release_slot(info):
                self._running_count -= 1
                self._drain_queue()
            self._tasks.pop(info.id, None)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata={"subagent_id": info.id},
            )
            logger.info("Subagent %s spawn rejected", info.id)
            # Report ownership through the same claim every other terminal path
            # uses, so a concurrent reap/stop cannot also announce.
            if self._on_done and self._claim_finalize(info):
                await self._safe_announce(info)
            return

        self._log_spawned(info)
        await self._run(info)

    def _log_spawned(self, info: SubagentInfo) -> None:
        """Record spawn metrics and audit log entry.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        # Persist agent folder to disk for orphan recovery
        try:

            create_agent_folder(
                info.id,
                task=info.task,
                agent=info.agent,
                parent_session=info.parent_session_key,
                max_turns=info.max_turns,
                context_groups=_context_groups_field(info),
            )
        except Exception:
            logger.warning("Failed to create agent folder for %s", info.id, exc_info=True)

        Stats().inc_subagent_spawned()
        sel().log_tool_invocation(
            session_key=info.parent_session_key,
            source="subagent",
            tool_name="spawn_run",
            outcome="spawned",
            metadata={
                "subagent_id": info.id,
                "agent": info.agent or "kirocrew",
                "cwd": info.cwd,
            },
        )
        logger.info("Subagent %s spawned: %s", info.id, info.task[:80])

    @property
    def running(self) -> list[SubagentInfo]:
        """Return currently running (not done) subagents."""
        return [a for a in self._agents.values() if not a.done]

    @property
    def all_agents(self) -> list[SubagentInfo]:
        """Return all tracked subagents (running and done)."""
        return list(self._agents.values())

    def batch_members_pending(self, batch_id: str) -> bool:
        """True while ANY member of *batch_id* is still outstanding — running
        (registered, not done), queued behind the stagger gate (not yet
        registered), OR not yet submitted (sibling POSTs still in flight —
        a fast-failing first member must not finalize the
        wave and emit a partial digest before the rest of the batch even
        arrives). The wave digest must also not be held hostage by unrelated
        agents under the same parent."""
        if not batch_id:
            return False
        _bs = self._batch_submitted.get(batch_id)
        if _bs is not None and _bs[1] > 0 and _bs[0] < _bs[1]:
            return True  # submissions still in flight
        if any(a.batch_id == batch_id and not a.done for a in self._agents.values()):
            return True
        return any(p.get("batch_id") == batch_id for p in self._queue)

    def finalize_batch(self, batch_id: str) -> None:
        """Prune per-wave bookkeeping once the wave digest has fired.

        Bounds `_seen_batches` / `_batch_submitted` growth: without
        this, long-lived gateways accrete an entry per wave forever.
        """
        if not batch_id:
            return
        self._seen_batches.discard(batch_id)
        self._batch_submitted.pop(batch_id, None)
        self._batch_progress_ts.pop(batch_id, None)

    def record_lost_submission(
        self,
        batch_id: str,
        batch_total: int,
        reason: str,
        parent_session_key: str = "",
    ) -> None:
        """Reconcile a wave member whose spawn submission was LOST before it
        reached :meth:`spawn` (transport error / timeout / pre-spawn HTTP
        rejection in ``api_spawn``).

        Every sibling POST carried ``batch_total`` counting the lost member,
        but ``spawn()`` never ran for it — so ``submitted < expected``
        forever, ``batch_members_pending()`` stays True, the digest chunk
        never fires, and held sibling results strand until a gateway restart.
        This helper counts the lost
        member as submitted AND announces a synthetic terminal member through
        the single completion consumer, so the wave's accounting sees a
        failure line and can close.

        Idempotent-ish by construction: each call reconciles exactly one lost
        member; callers invoke it once per lost submission.
        """
        if not batch_id:
            return
        _bs = self._batch_submitted.setdefault(batch_id, [0, max(0, int(batch_total))])
        _bs[0] += 1
        self._batch_progress_ts[batch_id] = time.time()
        try:
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="submission_lost",
                metadata={"batch_id": batch_id, "reason": reason[:200]},
            )
        except Exception:
            logger.debug("SEL audit failed for lost submission", exc_info=True)
        info = SubagentInfo(
            id=uuid.uuid4().hex[:8],
            task="(submission lost before spawn)",
            agent="",
            parent_session_key=parent_session_key,
            done=True,
            error=f"spawn submission lost: {reason[:300]}",
            batch_id=batch_id,
            batch_total=max(0, int(batch_total)),
        )
        if self._on_done:
            try:
                self._tasks[f"lost-{info.id}"] = asyncio.ensure_future(self._safe_announce(info))
            except RuntimeError:
                pass  # no running loop (sync/test context)

    def _sweep_stuck_waves(self, now: float) -> None:
        """Reaper backstop: force-reconcile waves wedged by lost submissions.

        A wave is STUCK when ``submitted < expected``, every registered
        member is terminal, nothing of the wave sits in the spawn queue, and
        there has been no submission progress for ``_WAVE_STUCK_SECS``. The
        count-driven ``batch_members_pending()`` can never close such a wave
        on its own — no future completion event will arrive. Reconciling via
        :meth:`record_lost_submission` (once per sweep per wave — waves with
        multiple losses converge across sweeps) re-enters the completion
        consumer so held sibling results deliver instead of stranding until
        restart. Also bounds the ``_batch_submitted``/``_batch_progress_ts``
        leak in the stuck case.
        """
        for batch_id, _bs in list(self._batch_submitted.items()):
            if _bs[1] <= 0 or _bs[0] >= _bs[1]:
                continue  # complete or unbounded — not wedged by lost POSTs
            last = self._batch_progress_ts.get(batch_id, 0.0)
            if now - last < _WAVE_STUCK_SECS:
                continue  # still within the grace window
            members = [a for a in self._agents.values() if a.batch_id == batch_id]
            if any(not a.done for a in members):
                continue  # live members will re-evaluate the wave on completion
            if any(p.get("batch_id") == batch_id for p in self._queue):
                continue  # queued members still pending — not stuck
            parent = members[0].parent_session_key if members else ""
            logger.warning(
                "Reaper: wave %s stuck (%d/%d submitted, no progress for %.0fs)"
                " — reconciling one lost submission",
                batch_id,
                _bs[0],
                _bs[1],
                now - last,
            )
            self.record_lost_submission(
                batch_id,
                _bs[1],
                f"submission never arrived (wave stuck > {_WAVE_STUCK_SECS}s"
                " — reconciled by reaper liveness backstop)",
                parent_session_key=parent,
            )

    def _sweep_digest_holds(self, now: float) -> None:
        """Reaper backstop: release wave results whose HOLD DEADLINE expired.

        The gateway holds a completed member's per-agent injection until the
        wave's digest chunk fires. Both of the chunk's triggers are event-driven
        — a COUNT trigger (``SUBAGENT_DIGEST_CHUNK_SIZE`` completions pending)
        and wave close — so neither can fire while a straggler is simply *not
        finishing*. With the default count (10) above any realistic wave size,
        the only flush that ever fires is the wave-close one, and a member that
        HANGS rather than fails withholds every sibling's finished result for
        the full ``_TIMEOUT_SECS`` reap window (issue #2215).

        This sweep is the timer the event-driven triggers lack: when the OLDEST
        outstanding hold in a wave has aged past :data:`DIGEST_HOLD_SECS` and
        the wave is still live, it announces a synthetic *flush-only* record
        through the single completion consumer (the same re-entry mechanism
        :meth:`record_lost_submission` uses), which forces the partial digest
        out. Ordinary fast waves never reach the deadline, so the deliberate
        "small wave = one consolidated digest" behavior is untouched.
        """
        if DIGEST_HOLD_SECS <= 0 or self._on_done is None:
            return  # deadline disabled — count-trigger-only
        oldest: dict[str, float] = {}
        parents: dict[str, str] = {}
        totals: dict[str, int] = {}
        for info in list(self._agents.values()):
            _bid = info.batch_id
            if not _bid or info._digest_held_at <= 0.0:
                continue
            _prev = oldest.get(_bid)
            if _prev is None or info._digest_held_at < _prev:
                oldest[_bid] = info._digest_held_at
            parents.setdefault(_bid, info.parent_session_key)
            totals.setdefault(_bid, info.batch_total)
        for batch_id, held_at in oldest.items():
            age = now - held_at
            if age < DIGEST_HOLD_SECS:
                continue  # still inside the grace window
            if not self.batch_members_pending(batch_id):
                # The wave is closing on its own — the real wave-close flush is
                # already in flight (or the held flags are stale bookkeeping).
                # Forcing a partial digest here would race it and could emit a
                # duplicate chunk for the same members.
                continue
            logger.warning(
                "Reaper: wave %s held results for %.0fs (deadline %.0fs) —"
                " forcing partial digest flush",
                batch_id,
                age,
                DIGEST_HOLD_SECS,
            )
            self.force_digest_flush(
                batch_id,
                parents.get(batch_id, ""),
                totals.get(batch_id, 0),
                age,
            )

    def force_digest_flush(
        self,
        batch_id: str,
        parent_session_key: str,
        batch_total: int,
        held_secs: float,
    ) -> None:
        """Announce a synthetic *flush-only* record to release a wave's held
        results without waiting for another member to complete.

        The record is deliberately NOT a wave member: ``_digest_flush_only``
        tells the gateway to skip every per-member side effect (terminal WS
        event, orchestration accounting, done/ok/err counters, digest lines) and
        only force the pending chunk out. Announcing it through ``_on_done`` —
        rather than reaching into the gateway's digest buffers — reuses the one
        completion consumer that owns digest composition, routing, and the
        held-tombstone settle contract.
        """
        if not batch_id or self._on_done is None:
            return
        info = SubagentInfo(
            id=uuid.uuid4().hex[:8],
            task=f"(wave digest flush — results held {int(held_secs)}s)",
            parent_session_key=parent_session_key,
            done=True,
            batch_id=batch_id,
            batch_total=max(0, int(batch_total)),
        )
        info._digest_flush_only = True
        try:
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="digest_hold_expired",
                metadata={"batch_id": batch_id, "held_secs": int(held_secs)},
            )
        except Exception:
            logger.debug("SEL audit failed for digest hold expiry", exc_info=True)
        try:
            self._tasks[f"flush-{info.id}"] = asyncio.ensure_future(
                self._announce_digest_flush(info)
            )
        except RuntimeError:
            pass  # no running loop (sync/test context)

    async def _announce_digest_flush(self, info: SubagentInfo) -> None:
        """Run the flush-only announce with the SAME settle contract as ``_run``.

        ``_settle_digest_holds`` must run only after ``_on_done`` returns
        cleanly: a routing failure has to leave the held members undelivered so
        orphan reconciliation can still recover them after a restart. This path
        has no run loop to enforce that ordering, so it enforces it here.
        """
        assert self._on_done is not None
        try:
            await self._on_done(info)
        except Exception:
            logger.warning(
                "Digest hold flush announce failed for wave %s", info.batch_id, exc_info=True
            )
            return
        self._settle_digest_holds(info)

    def _settle_digest_holds(self, info: SubagentInfo) -> None:
        """Settle delivery tombstones for wave members whose injection was
        held for this member's digest. Called ONLY after ``_on_done`` returned
        without raising — the digest has been handed off, so marking the held
        members delivered no longer risks the restart-loss window
        (settling at digest composition, before routing, would).
        """
        for _hid in info._digest_settle_ids:
            try:
                mark_delivered(_hid)
            except Exception:
                logger.debug("Failed to settle held subagent %s", _hid, exc_info=True)
        info._digest_settle_ids = []

    def get(self, agent_id: str) -> SubagentInfo | None:
        """Get agent info by ID."""
        return self._agents.get(agent_id)

    @property
    def count(self) -> int:
        return len(self.running)

    async def _teardown_run_session(self, info: SubagentInfo, session_key: str) -> None:
        """Release and reset the run's own session (skipped when reaped).

        Split out of ``_run``'s ``finally`` so the caller can wrap it in a nested
        ``try/finally``: every statement here AWAITS, and a cancellation arriving
        at one of those awaits propagates straight out of the enclosing
        ``finally`` suite (the ``except Exception`` arms do not catch
        ``CancelledError``). That skipped the slot release, the task-registry pop
        and the teardown gate — leaking a concurrency slot, which is the very
        class of bug this module's guard split exists to prevent.
        """
        try:
            if info._session_sharing:
                # Session-sharing subagents: destroy the session handle
                # (unregister from shared runtime). Don't kill the runtime.
                # Skip when the reaper already tore it down (info.reaped).
                # Retain-by-default: keep the transcript files — they are
                # spawn_continue's resume material. The tombstone pruner
                # deletes them with the run folder (~1h after delivery)
                # unless the conversation is promoted (continued / keep).
                if info._shared_provider and not info.reaped:
                    try:
                        info._shared_provider.set_keep_transcript(True)
                    except Exception:
                        logger.debug("set_keep_transcript failed", exc_info=True)
                    await info._shared_provider.shutdown()
            else:
                # Retain-by-default: never delete session files at teardown.
                # The reset() below still expires the process, so an idle
                # conversation costs a JSON file, not RSS. Deletion is owned
                # by the tombstone pruner (default runs, ~1h) or the
                # conversation TTL sweep / spawn_release (promoted runs).
                self._sessions.release(session_key, cleanup=False)
        except Exception:
            logger.warning("Subagent %s: release failed", info.id, exc_info=True)
        if not info._session_sharing:
            try:
                await asyncio.wait_for(self._sessions.reset(session_key), timeout=_RESET_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Subagent %s: reset timed out, force-killing", info.id)
                await self._sigkill_session(session_key)
                try:
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="subagent",
                        tool_name="run_finally_force_kill",
                        outcome="sigkill",
                        metadata={"subagent_id": info.id},
                    )
                except Exception:
                    logger.exception("Subagent %s: SEL audit failed", info.id)
            except Exception:
                logger.exception("Subagent %s: reset failed", info.id)

    async def _run(self, info: SubagentInfo) -> None:
        """Execute a subagent task in its own session."""
        session_key = info.conversation_key or f"subagent:{info.id}"
        try:
            await asyncio.wait_for(
                self._run_inner(info, session_key), timeout=self._default_timeout
            )
        except asyncio.TimeoutError:
            if not info.reaped:
                info.error = f"Timed out after {self._default_timeout // 60} minutes [{_timeout_context(info, turn_limit=self._effective_turn_limit(info))}]"
                info.done = True
                Stats().inc_subagent_failed()
                self._write_tombstone(info, "timeout")
            logger.warning("Subagent %s timed out", info.id)
        except asyncio.CancelledError:
            if not info.reaped:
                if (
                    not info.user_stopped
                    and not self._shutting_down
                    and not info._cancel_retry_used
                    and info.tool_count == 0
                ):
                    # UNEXPECTED cancellation (not user Stop, not shutdown):
                    # one-shot auto-continue, mirroring the main path's
                    # unexpected-cancel recovery. Skip terminal
                    # finalization (via _recovering) and respawn on a fresh
                    # task — this task is being cancelled and cannot continue.
                    #
                    # SIDE-EFFECT GATE (tool_count == 0): the respawn runs on a
                    # FRESH session with no ledger of prior tool calls, so the
                    # model cannot verify which side effects (files written,
                    # messages sent, commands run) already happened — a
                    # preamble alone cannot make re-running safe. Once any tool
                    # has executed, we finalize with the partial preserved
                    # instead of respawning. Text-only activity is safe to
                    # resume (the partial is preserved and re-presented).
                    info._cancel_retry_used = True
                    info._recovering = True
                    logger.warning(
                        "Subagent %s unexpectedly cancelled — scheduling one-shot auto-continue",
                        info.id,
                    )
                    try:
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="subagent",
                            tool_name="cancel_auto_continue",
                            outcome="scheduled",
                            metadata={"subagent_id": info.id},
                        )
                    except Exception:
                        logger.debug("SEL audit for cancel recovery failed", exc_info=True)
                    self._schedule_cancel_recovery(info)
                else:
                    info.done = True
                    if info.tool_count > 0 and not info.user_stopped and not self._shutting_down:
                        # Auto-continue deliberately suppressed (side-effect
                        # gate above): be explicit so the parent/user knows the
                        # run was interrupted and NOT resumed, and why.
                        info.error = (
                            "cancelled (auto-continue suppressed: tools already "
                            "executed — resuming on a fresh session could repeat "
                            "side effects)"
                        )
                    else:
                        info.error = "cancelled"
                    # Preserve whatever streamed before the cancel as a partial
                    # result (delivered with the failure).
                    if not info.result and info.streaming_text:
                        info.result = info.streaming_text
                    Stats().inc_subagent_failed()
                    self._write_tombstone(info, "cancelled")
            logger.info("Subagent %s cancelled", info.id)
        except Exception as exc:
            if not info.reaped:
                info.error = _describe_exception(exc)
                info.done = True
                Stats().inc_subagent_failed()
                self._write_tombstone(info, "error")
            logger.exception("Subagent %s failed", info.id)
        finally:
            # Guard 3 of 3 — the terminal REPORT, owned by the finalize claim.
            # Taken (and the report task SPAWNED) before the teardown awaits
            # below, so a cancellation landing anywhere in teardown cannot
            # strand the outcome: the shielded task is already live. The claim
            # returns False while _recovering without consuming itself, so a
            # pending cancel-recovery respawn is not reported done and its
            # respawned run can claim later.
            report_task = None
            # Set once this finally's session teardown has finished, so the
            # already-spawned report holds its "delivered" tombstone until the
            # child is provably gone (see `_report_terminal`).
            teardown_done = asyncio.Event()
            if self._claim_finalize(info):
                info.elapsed = time.time() - info.started
                self._record_cost(info)
                report_task = self._spawn_terminal_report(
                    info,
                    source="Subagent",
                    injection_timeout_reason=(
                        f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s" " (queue + injection)"
                    ),
                    mark_delivered_on_success=True,
                    settle_digest=True,
                    teardown_done=teardown_done,
                )
            # Nested try/finally: the teardown awaits must never be able to skip
            # the bookkeeping below (see `_teardown_run_session`).
            try:
                if not info.reaped:
                    await self._teardown_run_session(info, session_key)
            finally:
                # Guard 2 of 3 — SLOT accounting on its own one-shot token, so
                # the count is released exactly once whichever terminal path
                # arrives first (and is NOT skipped just because the reaper set
                # `reaped`, which is how an earlier revision leaked slots).
                if self._release_slot(info):
                    self._running_count -= 1
                    self._drain_queue()
                self._tasks.pop(info.id, None)
                # Teardown is done (or was skipped because the reaper did it) —
                # release the report's delivered-tombstone gate. Unconditional,
                # so the report can never wedge on a cancelled teardown.
                teardown_done.set()

        # The report itself already ran (or is running) on the shielded task
        # spawned in the finally above; block until it completes so sequencing is
        # unchanged for callers.
        #
        # NOT during shutdown. `_run`'s CancelledError arm deliberately does not
        # re-raise, so by the time we reach this await the cancellation has been
        # consumed and `shield` would simply wait out the full _ON_DONE_TIMEOUT
        # injection cap — holding `cancel_all()`'s gather for up to 20 minutes.
        # The report is registered in `self._report_tasks`, so `cancel_all()`'s
        # bounded drain owns it from here.
        if report_task is not None and not self._shutting_down:
            await self._await_report(report_task)

    def _schedule_cancel_recovery(self, info: SubagentInfo) -> None:
        """Respawn *info*'s run on a fresh task after an unexpected cancellation.

        Called from ``_run``'s CancelledError handler — the current task is
        being cancelled and cannot continue itself, so the continuation runs on
        a new task. One-shot: gated by ``info._cancel_retry_used`` at the call
        site. The original run's finally block still performs session cleanup
        (release/reset) but skips terminal finalization while ``_recovering``.

        **Cancellation-source contract.** This branch exists for cancellations
        that arrive from OUTSIDE the manager's own lifecycle — in practice the
        parent task tree being torn down around a live subagent (e.g. a
        dashboard slot reset/removal cancelling background tasks, or an event
        during gateway component re-init) — mirroring the main path's
        unexpected-cancel recovery. Every INTENTIONAL cancel site in
        this module sets a terminal marker before cancelling, and the recovery
        branch defers to all of them: ``cancel()`` sets ``user_stopped``,
        ``cancel_all()`` sets ``_shutting_down``, and ``_force_reap`` sets
        ``reaped`` (checked before the recovery branch). Any NEW code path that
        cancels a subagent task on purpose MUST set one of those markers first,
        or the cancel will be treated as unexpected and recovered once.

        Coordination is explicit, not timed: ``_resume`` awaits the ORIGINAL
        task object to fully complete (its finally does session release/reset,
        slot decrement, and pops the task registry) before respawning. This
        guarantees the old finally can neither pop the new task out of
        ``self._tasks`` nor emit a duplicate completion, and the respawn never
        starts against a session whose reset is still in flight. The respawn
        then re-acquires a slot by waiting for capacity (the old finally's
        ``_drain_queue`` may have admitted a queued spawn into the freed slot),
        so the concurrency ceiling is never exceeded.

        The pending ``_resume`` task itself is registered in ``self._tasks``
        (under ``"<id>:recovery"``) so ``cancel_all()`` reaches it during
        shutdown — a recovery can never outlive or escape manager teardown.
        """
        orig_task = asyncio.current_task()
        recovery_key = f"{info.id}:recovery"

        async def _resume() -> None:
            try:
                # Explicit handshake: wait for the original task's finally
                # (session release/reset, slot decrement, task-registry pop)
                # to fully complete before respawning. The finally is bounded
                # (_RESET_TIMEOUT-capped reset), so add slack on top of it.
                if orig_task is not None:
                    await asyncio.wait({orig_task}, timeout=_RESET_TIMEOUT + 60)
                    if not orig_task.done():
                        logger.error(
                            "Subagent %s cancel-recovery: original task did not "
                            "finish teardown in time — aborting recovery",
                            info.id,
                        )
                        raise RuntimeError("original task teardown timed out")
                if info.done or info._reap_started or info.reaped or self._shutting_down:
                    info._recovering = False
                    return
                # Re-acquire a slot through capacity, not blind increment:
                # the old finally freed our slot and may have drained a queued
                # spawn into it. Wait (bounded) for a free slot so recovery
                # never pushes the pool past max_concurrent.
                deadline = time.time() + _RECOVERY_SLOT_WAIT_SECS
                while self._running_count >= self._max_concurrent:
                    if time.time() >= deadline or self._shutting_down:
                        raise RuntimeError("no free slot for recovery respawn")
                    await asyncio.sleep(0.25)
                if info.done or info._reap_started or info.reaped or self._shutting_down:
                    info._recovering = False
                    return
                info._recovering = False
                # Claim the slot and launch the respawn ATOMICALLY (no await
                # between capacity check, increment, and create_task). An await
                # in that window would let a finishing subagent's _drain_queue
                # admit a queued spawn into the same slot and push the pool
                # past max_concurrent. The respawned _run owns the slot from
                # here (its finally decrements). The informational
                # subagent_recovering emit happens after, where a cancellation
                # can no longer leak the counter.
                self._running_count += 1
                # The interrupted run's finally already consumed this info's
                # slot token to free its slot. The respawn occupies a FRESH slot,
                # so re-arm the token or the respawned run's finally would no-op
                # and leave `_running_count` permanently inflated.
                info._slot_released = False
                self._tasks[info.id] = asyncio.create_task(self._run(info))
                try:
                    await self._fire_event("subagent_recovering", info, {"attempt": 1})
                except Exception:
                    logger.debug("subagent_recovering emit failed for %s", info.id, exc_info=True)
            except Exception:
                logger.exception("Subagent %s cancel-recovery respawn failed", info.id)
                info._recovering = False
                # The RECORD keeps its own first-arrival-wins `done` guard...
                if not info.done and not info._reap_started and not info.reaped:
                    # Full terminal finalization — the UI must never be left on
                    # a running card and the parent must still hear about the
                    # failure (with any partial result) even when the respawn
                    # itself could not happen.
                    info.done = True
                    info.error = "cancelled (recovery failed)"
                    info.elapsed = time.time() - info.started
                    Stats().inc_subagent_failed()
                    self._write_tombstone(info, "cancelled")
                    self._record_cost(info)
                if not info.elapsed:
                    # Report needs an elapsed even when the record above was
                    # skipped because another path had already set `done`.
                    info.elapsed = time.time() - info.started
                # ...and the REPORT goes through the one-shot claim, exactly like
                # the reap and `_run`'s finally. Routing through the claim (not a
                # direct `subagent_done`/`_on_done` fire) keeps this from being a
                # fourth reporter outside the very claim this
                # class uses to guarantee exactly-once delivery, so a reaper
                # racing a failed respawn cannot deliver the outcome twice.
                # Reporting via `_run_terminal_report` also shields the delivery,
                # which matters here because `_force_reap` cancels this task.
                if self._claim_finalize(info):
                    await self._run_terminal_report(
                        info,
                        source="Recovery",
                        injection_timeout_reason=(
                            f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s "
                            "(recovery failure)"
                        ),
                        mark_delivered_on_success=False,
                        # Same reasoning as the reap path: settle siblings' holds.
                        settle_digest=True,
                    )
            finally:
                # Whether respawned, aborted, or cancelled: this pending
                # recovery is no longer outstanding.
                _reg = self._tasks.get(recovery_key)
                if _reg is asyncio.current_task():
                    self._tasks.pop(recovery_key, None)

        async def _resume_guarded() -> None:
            try:
                await _resume()
            except asyncio.CancelledError:
                # The pending recovery itself was cancelled (cancel_all during
                # shutdown, or manager teardown). Terminal by default — a
                # cancelled recovery NEVER re-recovers; just make sure the
                # record isn't left in limbo.
                info._recovering = False
                _live = self._tasks.get(info.id)
                if _live is not None and not _live.done():
                    # Respawn already launched — the live run owns the record
                    # (its own CancelledError arm is terminal: one-shot flag is
                    # spent). Don't finalize over it.
                    raise
                # `_reap_started`, not just `reaped`: `_force_reap` cancels this
                # task BEFORE it sets `reaped` (which must stay false until the
                # reaper owns the record — see `_reap_started`). Consulting only
                # `reaped` made this arm win the race and persist a neutral user
                # Stop as a FAILURE, with a failure stat and a "cancelled"
                # tombstone the reaper could no longer correct.
                if not info.done and not info._reap_started and not info.reaped:
                    info.done = True
                    info.error = "cancelled"
                    info.elapsed = time.time() - info.started
                    if not info.user_stopped:
                        # A user-initiated stop is a neutral outcome, not a
                        # failure — matching the reap path's own record guard.
                        Stats().inc_subagent_failed()
                    self._write_tombstone(info, "cancelled")
                raise

        _t = asyncio.create_task(_resume_guarded())
        self._tasks[recovery_key] = _t
        _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _touch_activity(self, info: SubagentInfo) -> None:
        """Record stream activity for idle-stall detection.

        Updates ``last_activity`` and, if the subagent was previously flagged
        stalled by the reaper, clears the flag and notifies the UI so the
        running-card drops the "stalled" warning the moment work resumes.
        """
        info.last_activity = time.time()
        info._stall_suspect_at = 0.0  # activity resets the 2-sweep confirmation
        # Retire the oracle so the next suspicion samples a fresh baseline rather
        # than differencing against counters from before this activity, and bump
        # the generation so a consult submitted before this moment cannot land a
        # stalled verdict on an agent that has just proven it is working.
        if info._stall_oracle is not None:
            info._stall_oracle = info._stall_oracle.fresh()
        info._stall_gen += 1
        if info.stalled:
            info.stalled = False
            await self._fire_event("subagent_stalled", info, {"stalled": False})

    async def _fire_event(self, etype: str, info: SubagentInfo, extra: dict | None = None) -> None:
        if self._on_event:
            try:
                await self._on_event(etype, info, extra or {})
            except Exception:
                logger.warning("on_event failed for %s/%s", etype, info.id, exc_info=True)

    def _queued_depth(self, parent_session_key: str) -> int:
        """Number of spawns currently queued for *parent_session_key* (waiting
        behind the concurrency cap / stagger gate, not yet started)."""
        return sum(1 for q in self._queue if q.get("parent_session_key", "") == parent_session_key)

    def queued_count_for(self, parent_session_key: str) -> int:
        """Public queued-spawn count for *parent_session_key*.

        Spawns accepted behind the concurrency cap / stagger gate sit in
        ``_queue`` with no ``SubagentInfo`` yet, so ``running``-based checks
        read "no pending work" during exactly the window a wave is ramping.
        Reset-deferral guards must consult this alongside ``running``.
        """
        return self._queued_depth(parent_session_key)

    def has_pending_work_for(self, parent_session_key: str) -> bool:
        """True while *parent_session_key* has sub-agents RUNNING or QUEUED.

        The reset-deferral guards must consult this, not ``running`` alone —
        see :meth:`queued_count_for` for why. A parent session reset while a
        spawn is still queued strands that agent's completion on a
        cold-started, context-free replacement session.
        """
        if self._queued_depth(parent_session_key) > 0:
            return True
        return any(a.parent_session_key == parent_session_key for a in self.running)

    def _emit_queue_depth(self, parent_session_key: str, batch_id: str = "") -> None:
        """Emit the current queued depth for *parent_session_key* as a
        ``subagent_queued`` lifecycle event.

        The chip is otherwise driven only by agents that have actually started
        (``subagent_spawn``), so agents sitting behind the concurrency cap /
        stagger gate are invisible and the chip can appear late or flicker.
        This advisory count lets the UI show "N waiting to start" the moment a
        wave is accepted, and stay mounted across the staggered ramp.

        Fire-and-forget: scheduled on the running loop; a no-op in sync/test
        contexts without a loop (the count is advisory UI signal, not state).
        """
        depth = self._queued_depth(parent_session_key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (sync/test context) — advisory event skipped
        info = SubagentInfo(
            id="_queue",
            task="",
            parent_session_key=parent_session_key,
            batch_id=batch_id,
        )
        loop.create_task(self._fire_event("subagent_queued", info, {"queued": depth}))

    @staticmethod
    def _write_tombstone(info: SubagentInfo, cause: str) -> None:
        """Best-effort tombstone write for abnormal exits."""
        try:

            write_tombstone(
                info.id,
                cause=cause,
                recovery_action="pending",
                pid=info._pid,
                turns=info.turns,
                last_tool=info.last_tool,
                outcome=info.outcome,
                # ``cause`` is a coarse bucket ("error", "timeout"), which is
                # not enough to act on. ``info.error`` is in-memory only and
                # dies with the gateway, so without this the specific reason is
                # recoverable from nothing but the log.
                detail=(_redact(info.error)[:_MAX_ERROR_DETAIL_LEN] if info.error else ""),
            )
        except Exception:
            logger.debug("Failed to write tombstone for %s", info.id, exc_info=True)

    async def _run_inner(self, info: SubagentInfo, session_key: str) -> None:
        """Inner execution — called within timeout wrapper."""
        # Mark the real start of execution BEFORE any await so the startup
        # watchdog measures from here, not from registration (which may include
        # an arbitrary spawn-approval wait). Must be the first statement.
        info._exec_started = time.time()
        # Reset the activity clock to execution start too: last_activity is set
        # at registration (like ``started``), which can include a long spawn-
        # approval / queue wait. Without this, _maybe_flag_stall would treat
        # that pre-execution delay as idle time and prematurely surface a
        # healthy, just-started subagent as "stalled".
        info.last_activity = info._exec_started
        # Inherit approval policy from parent session; yolo/trust overrides
        parent_policy = self._sessions.get_approval_policy(info.parent_session_key)
        # Explicit approval_mode from spawn caller (e.g. Mochi bg agent)
        if not parent_policy and info.approval_mode == "auto":
            parent_policy = "auto"
            sel().log_api_access(
                caller=info.parent_session_key or f"subagent:{info.id}",
                operation="subagent.approval_mode_auto_policy",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id}",
            )
        if not parent_policy and self._is_yolo and self._is_yolo():
            parent_policy = "auto"
            sel().log_api_access(
                caller=info.parent_session_key,
                operation="subagent.yolo_policy_fallback",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id}",
            )
        if not parent_policy and self._global_approval_mode == "auto":
            # Apply global config as fallback only when parent is absent or
            # confirmed garbage-collected (no longer in session store).
            # If parent session still exists but returned no policy, deny by
            # default — the session is alive and intentionally non-auto.
            if not info.parent_session_key:
                _parent_gone = True  # no_parent
            elif self._sessions.has_session(info.parent_session_key) is False:
                _parent_gone = True  # parent_gc
            else:
                _parent_gone = False  # parent alive or store error → deny
                sel().log_api_access(
                    caller=f"subagent:{info.id}",
                    operation="subagent.config_policy_fallback",
                    outcome="denied",
                    source="subagent",
                    resources=f"subagent_id={info.id},reason=parent_alive_or_store_error",
                )
            if _parent_gone:
                parent_policy = "auto"
                _reason = "parent_gc" if info.parent_session_key else "no_parent"
                sel().log_api_access(
                    caller=f"subagent:{info.id}",
                    operation="subagent.config_policy_fallback",
                    outcome="ok",
                    source="subagent",
                    resources=f"subagent_id={info.id},reason={_reason}",
                )
        # auto_approve_subagent_tools auto-approves tool calls inside
        # subagents (separate from the spawn gate, deny-by-default).
        if not parent_policy and self._ctx_builder and self._ctx_builder.hooks:
            if self._ctx_builder.hooks.auto_approve_subagent_tools is True:
                parent_policy = "auto"
                sel().log_api_access(
                    caller=info.parent_session_key or f"subagent:{info.id}",
                    operation="subagent.auto_approve_subagent_tools_policy",
                    outcome="ok",
                    source="subagent",
                    resources=f"subagent_id={info.id}",
                )
        # Inherit agent from parent session when not explicitly specified
        agent = info.agent or self._sessions.get_agent(info.parent_session_key)
        if not info.agent and agent:
            sel().log_api_access(
                caller=f"subagent:{info.id}",
                operation="subagent.agent_inheritance",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id},inherited_agent={agent}",
            )
        extra_kwargs: dict[str, Any] = {}
        # An explicit per-spawn model wins; otherwise fall back to the
        # configured sub-agent role model (agent.role_models['subagent']). When
        # that role is unpinned the helper returns "" so we omit the kwarg and
        # keep deferring to the provider's configured default, exactly as before.
        eff_model = info.model or _subagent_default_model()
        if eff_model:
            extra_kwargs["model"] = eff_model
        # Sub-agent reasoning effort (role_efforts['subagent'] -> chat default).
        # Passed as an override so it wins over the factory's agent-derived
        # default; "" leaves it to that default.
        eff_effort = _subagent_default_effort()
        if eff_effort:
            extra_kwargs["reasoning_effort_override"] = eff_effort
        if info.bare:
            extra_kwargs["bare"] = True
        if info.allowed_tools:
            extra_kwargs["allowed_tools"] = info.allowed_tools
        if info.cwd:
            extra_kwargs["cwd"] = info.cwd

        # ── Session sharing: reuse parent's shared AcpRuntime ──
        # When enabled and eligible, subagents get a session on the parent's
        # companion AcpRuntime (~200ms startup, ~0 memory) instead of spawning
        # a fresh kiro-cli process (~3-5s, ~400MB).
        #
        # Retain-by-default: EVERY run keeps its session files (teardown skips
        # deletion on both arms), so any completed run is continuable while
        # its files survive. keep=True / continuation runs additionally take
        # the dedicated arm: their resume path is the proven dashboard
        # expire-and-session/load lifecycle, which owns its process. Whether a
        # SHARED-runtime sid is loadable is the open Phase 0 question — until
        # proven, a continue on a shared-arm run relies on the fail-closed
        # resume guard below rather than a spawn-time guarantee.
        if info.keep:
            self._sessions.mark_continuable(session_key)
            self._conversations[session_key] = time.time()
        use_session_sharing = (not info.keep) and self._should_use_session_sharing(info)
        # A per-spawn or per-role model / reasoning-effort override cannot be
        # applied to the parent's already-started shared runtime (it was spawned
        # with the parent's model and cannot switch model per session). Force the
        # dedicated process path so the override in extra_kwargs actually reaches
        # get_or_create -> the provider factory; otherwise a configured sub-agent
        # model/effort would silently no-op on the default (session-sharing) path.
        if eff_model or eff_effort:
            use_session_sharing = False
        if use_session_sharing:
            try:
                client = await self._create_shared_session(info, session_key, agent)
            except Exception as exc:
                # Fallback: shared runtime unavailable (dead, spawn failed, etc.)
                # Revert to legacy per-process path transparently.
                logger.warning(
                    "Subagent %s: session sharing failed (%s), falling back to dedicated process",
                    info.id,
                    exc,
                )
                info._session_sharing = False
                info._shared_provider = None
                use_session_sharing = False
                client, is_new, _resumed = await self._sessions.get_or_create(
                    session_key,
                    agent=agent or None,
                    approval_policy=parent_policy,
                    **extra_kwargs,
                )
                is_cc = self._is_cc_provider(client)
            else:
                is_new = True
                _resumed = False
                is_cc = False
        else:
            client, is_new, _resumed = await self._sessions.get_or_create(
                session_key,
                agent=agent or None,
                approval_policy=parent_policy,
                **extra_kwargs,
            )
            # Fail CLOSED on a continuation that did not actually resume:
            # get_or_create silently falls back to a FRESH session when
            # session/load fails (lock held, corrupt files, backend refusal).
            # Executing the follow-up on that fresh session would silently run
            # it context-free — worse than an honest error the parent can react
            # to (re-spawn with a summary). conversation_key is only set by
            # continue_conversation, so first spawns are unaffected.
            if info.conversation_key and not _resumed:
                raise RuntimeError(
                    "resume_failed: session/load did not restore conversation "
                    f"{info.conversation_key} — refusing to execute the "
                    "follow-up without its prior context. The conversation "
                    "may be locked by a live process or its files corrupt; "
                    "re-spawn with a fresh task carrying a summary."
                )
            # Detect CC provider to skip permission event loop
            is_cc = self._is_cc_provider(client)
        # Intentionally check info.agent (not resolved `agent`) so only
        # explicitly requested agents skip _SYSTEM_PREFIX (defense-in-depth).
        named_agent = bool(info.agent and _AGENT_NAME_RE.fullmatch(info.agent))
        raw_task = info._raw_task or info.task
        message = raw_task if named_agent else (_SYSTEM_PREFIX + raw_task)
        if info._cancel_retry_used and (info.streaming_text or info.tool_count > 0):
            # One-shot auto-continue after an unexpected cancellation: tell the
            # model the prior attempt was interrupted so it completes the task
            # instead of assuming a fresh start. Same activity predicate as
            # the transient-retry path: a mutating tool may
            # have executed BEFORE the first text chunk, so tool_count must
            # trigger the preamble too — a bare original prompt after tool
            # activity invites duplicate side effects. (info.tool_count and
            # streaming_text persist across the respawn; _run_inner never
            # resets them.)
            message = _CANCEL_RESUME_PREFIX + message
        # Scale the injected-context budget to this subagent's model window (a
        # subagent can be pinned to a smaller model). Resolved from the live
        # client; None ⇒ 1M reference.
        _sub_window = window_for_provider_client(client)
        # Context scope this run was spawned with. Passed even when every group
        # is on, so build_message applies one code path for sub-agents.
        _groups = _context_groups_of(info)
        # Off-loop: build_message embeds the episodic query (blocking urllib).
        # A run's explicitly-given cwd IS its project for skill scoping: a
        # dashboard spawn inherits the parent slot's project as its cwd, so the
        # hands-off surface keeps the repo-scoped skills that surface exists for.
        # The pool default is deliberately NOT substituted -- it is the
        # workspace directory, not a checkout, so it can only ever mean "this
        # run named no project", which is exactly the fail-closed case. Keeping
        # one meaning for that makes the rule the same on every surface.
        full_message, _ = await run_in_embed_pool(
            self._ctx_builder.build_message,
            message,
            is_new,
            session_key,
            project=info.cwd or None,
            provider_type=self._provider_label_of(client),
            model_window=_sub_window,
            context_groups=_groups,
        )
        # The one place the resolved scope and its cost are both known — without
        # this, "the sub-agent didn't know X" is undebuggable after the fact.
        logger.info(
            "Subagent %s context: groups=%s, %d chars",
            info.id,
            ",".join(sorted(_groups)) or "conduct-only",
            len(full_message),
        )

        result_text = ""
        turns = 0
        turn_limit = self._effective_turn_limit(info)
        # Separate volume bound for child-origin permission escalations —
        # they are exempt from the parent's turn budget (see the
        # EVENT_PERMISSION_REQUEST branch) but must not be unbounded.
        child_escalations = 0
        child_escalation_limit = max(turn_limit * 3, 60)
        # Reports inherited agent (not just info.agent) so telemetry shows
        # the actual agent used for this subagent session.
        await self._fire_event(
            "subagent_spawn", info, {"task": _redact(info.task), "agent": agent or ""}
        )
        # Stream results to disk for orchestrated chat.

        # Record PID for orphan recovery
        try:

            pid = self._sessions.get_pid(session_key)
            if pid:
                info._pid = pid  # make available for _write_tombstone
                update_state(info.id, pid=pid, pid_recorded_at=time.time())
        except Exception:
            logger.debug("Failed to record PID for %s", info.id, exc_info=True)

        # Record session_id and provider type for session file cleanup
        try:
            session_id = client.session_id if hasattr(client, "session_id") else ""
            provider_type = self._provider_label_of(client)
            state_update: dict[str, object] = {
                "session_id": session_id,
                "provider": provider_type,
                # keep marks this run's session files as resume material: the
                # orphan reconciler and tombstone pruner skip file deletion
                # for keep runs (restart-safe — read from disk, not memory).
                "keep": info.keep,
                "conversation_key": session_key if info.keep else "",
            }
            # Store CWD for CC cleanup (needed to derive project-key path).
            # info.cwd is only set when a caller passes an explicit cwd
            # override (disabled by default), so for the common case derive
            # the project dir from the provider's own work dir — that is the
            # same path sent as ACP `cwd`, hence the encoded project key under
            # ~/.claude/projects. Without this, CC cleanup is skipped (no cwd)
            # and the transcript leaks.
            if is_cc:
                cc_cwd = info.cwd
                if not cc_cwd:
                    inner = getattr(client, "client", None)
                    work_dir = getattr(inner, "_work_dir", None)
                    if work_dir:
                        cc_cwd = str(work_dir)
                if cc_cwd:
                    state_update["cwd"] = cc_cwd
            update_state(info.id, **state_update)
        except Exception:
            logger.debug("Failed to record session_id for %s", info.id, exc_info=True)

        _rp = agent_dir_for_display(info.id) / "result.txt"
        info.result_path = str(_rp)
        # Cache tool names by tool_call_id so PostToolUse can recover the tool name
        # when EVENT_TOOL_RESULT arrives (which only carries tool_call_id and output).
        # Mirrors kiro_crew.dashboard.chat_runner._pending_tools.
        _pending_tools: dict[str, str] = {}

        async def _stream_with_transient_retry():
            """Yield stream events, retrying transient backend errors.

            Parity with the main path's retry ladder (chat_runner B1/B2):
            - Pre-token (no text streamed yet): re-send the SAME prompt on the
              same live session, up to TRANSIENT_RETRIES with exp backoff.
            - Post-token (partial already streamed): send a CONTINUE prompt so
              the preserved partial is finished, not duplicated.
            Non-transient errors and exhausted budgets propagate unchanged
            (handled by _run's generic exception arm → error tombstone).
            """
            attempts = 0
            # One-shot post-activity allowance, mirroring the main path's
            # ``_posttoken_retry_used`` rule (dashboard/chat_runner.py ~L4324):
            # each continuation turn issued AFTER observed activity is an
            # independent opportunity for the model to repeat a side-effecting
            # tool, so post-activity recovery gets exactly ONE attempt.
            # TRANSIENT_RETRIES applies only while zero activity was observed
            # (replaying the bare prompt is side-effect-free by definition).
            # PARITY NOTE: this ladder and chat_runner's are two copies with
            # intentionally identical semantics — a fix to either's activity
            # predicate or budget rules must be mirrored in the other.
            post_activity_attempts = 0
            msg = full_message
            while True:
                try:
                    async for _ev in client.stream(msg):
                        yield _ev
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not acp_error_is_transient(exc):
                        raise
                    # Post-activity: continue instead of re-running. "Activity"
                    # is ANY text chunk, approved tool turn, or auto-allowed
                    # tool call — a mutating tool may have executed before the
                    # first text chunk, and replaying the full prompt would
                    # re-run it (duplicate writes/messages). Only a turn with
                    # zero observed activity resends the original prompt.
                    _had_activity = bool(result_text) or turns > 0 or info.tool_count > 0
                    if _had_activity:
                        if post_activity_attempts >= 1:
                            raise
                        post_activity_attempts += 1
                    elif attempts >= TRANSIENT_RETRIES:
                        raise
                    attempts += 1
                    delay = transient_retry_delay(attempts)
                    logger.warning(
                        "Subagent %s: transient backend error (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        info.id,
                        attempts,
                        TRANSIENT_RETRIES,
                        delay,
                        exc,
                    )
                    await self._fire_event(
                        "subagent_retrying",
                        info,
                        {"attempt": attempts, "max": TRANSIENT_RETRIES},
                    )
                    try:
                        sel().log_api_access(
                            caller=info.parent_session_key or f"subagent:{info.id}",
                            operation="subagent.transient_retry",
                            outcome="retrying",
                            source="subagent",
                            resources=f"subagent_id={info.id},attempt={attempts}",
                        )
                    except Exception:
                        logger.debug("SEL audit for transient retry failed", exc_info=True)
                    await asyncio.sleep(delay)
                    msg = _TRANSIENT_CONTINUE_MSG if _had_activity else full_message

        _complete_event: LLMEvent | None = None
        # Wall clock for THIS subagent's own turn. Deliberately started here,
        # at the subagent's own stream, not on the parent side: under session
        # sharing this subagent reuses the parent's runtime, so a parent-side
        # clock would charge the child for the parent's elapsed time. acp
        # leaves TurnUsage.duration_ms at 0, so the row needs this.
        # Includes transient-retry backoff, which is real wall time the caller
        # waited for this turn.
        _turn_t0 = time.monotonic()
        async for event in _stream_with_transient_retry():
            # Refresh the activity clock for EVERY event kind (thinking chunks,
            # tool-call updates, etc.) before dispatch, so idle-stall detection
            # only trips on a genuine no-event hang — not on an event kind this
            # switch does not special-case. Approval waits stay exempt via
            # _awaiting_approval.
            await self._touch_activity(info)
            if event.kind == EVENT_TEXT_CHUNK:
                result_text += event.text
                write_result_chunk(info.id, event.text)
                redacted = _redact(event.text)
                info.streaming_text += redacted
                if len(info.streaming_text) > 50_000:
                    info.streaming_text = "…(truncated)\n" + info.streaming_text[-40_000:]
                await self._fire_event("subagent_chunk", info, {"text": redacted})
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Both kiro-cli and claude-agent-acp surface tool calls via
                # session/request_permission. Run them through the same hook
                # → parent_policy → interactive callback pipeline so the
                # approve / reads / trust / yolo protocol applies uniformly.
                #
                # Child-origin escalations (runtime-routed backend subagents)
                # do NOT consume the parent's turn budget: a child asking for
                # enough permissions would otherwise trip the parent's
                # turn_limit and kill the whole subagent run on activity that
                # is not the parent's own turns. They get their OWN bound
                # instead — without one, a chatty or adversarial backend
                # child could generate unbounded approval prompts until the
                # wall-clock reaper fires (the turn budget used to bound
                # exactly this traffic). Generous multiple of the parent's
                # limit: legitimate crews fan many small child tool calls.
                if not event.sub_session_id:
                    turns += 1
                    info.turns = turns
                else:
                    # Child escalation: counted toward its own volume bound
                    # here; side-effect activity (tool_count) is counted at
                    # APPROVAL in _approve_and_log — a purely rejected
                    # escalation executed nothing and must not consume the
                    # run's replay budget (tool_count gates prompt replay
                    # and cancel-respawn).
                    child_escalations += 1
                    if child_escalations > child_escalation_limit:
                        # Answer the triggering request BEFORE bailing: this
                        # event is already dequeued, so returning without a
                        # response would strand the child's oneshot — under
                        # session sharing the runtime outlives this subagent
                        # and nothing else tears the connection down. The
                        # "requests are answered on every queue path"
                        # contract this PR establishes applies to limit
                        # bails too.
                        try:
                            await self._reject_and_log(
                                client,
                                event.request_id,
                                session_key,
                                event,
                                error="child_escalation_limit",
                            )
                        except Exception:
                            logger.exception(
                                "failed to reject escalation-limit trigger request"
                            )
                        info.result = result_text or "_Partial output._"
                        info.error = f"child_escalation_limit:{child_escalation_limit}"
                        info.done = True
                        Stats().inc_subagent_failed()
                        logger.warning(
                            "Subagent %s hit child escalation limit (%d)",
                            info.id,
                            child_escalation_limit,
                        )
                        self._write_tombstone(info, "child_escalation_limit")
                        return
                # Diagnostic pointer is written for BOTH origins — orphan
                # recovery must see child activity too; only the turn
                # increment is parent-scoped.
                info.last_tool = event.title or ""
                self._note_tool_dispatch(info, event)
                # Persist turn state for orphan recovery diagnostics
                try:
                    update_state(info.id, turns=turns, last_tool=event.title or "")
                except Exception:
                    pass
                await self._fire_event(
                    "subagent_tool",
                    info,
                    {
                        "tool": _redact(event.title or ""),
                        "tool_kind": event.tool_kind,
                        "turns": info.turns,
                        "tool_count": info.tool_count,
                    },
                )
                if turns > turn_limit:
                    # Same contract as the child_escalation_limit bail: the
                    # triggering request is already dequeued and must be
                    # answered before this loop exits, or its oneshot strands.
                    try:
                        await self._reject_and_log(
                            client, event.request_id, session_key, event, error="turn_limit"
                        )
                    except Exception:
                        logger.exception("failed to reject turn-limit trigger request")
                    info.result = result_text or "_Partial output._"
                    info.error = f"turn_limit:{turn_limit}"
                    info.done = True
                    Stats().inc_subagent_failed()
                    logger.warning("Subagent %s hit turn limit (%d)", info.id, turn_limit)
                    self._write_tombstone(info, "turn_limit")
                    return
                tool_result = self._ctx_builder.hooks.on_tool_call(
                    event.title,
                    session_key=session_key,
                    agent=info.agent or "",
                    app=info.app or "",
                    tool_kind=event.tool_kind,
                    raw_params=event.raw_tool_params,
                    command=event.shell_command,
                    is_shell=event.is_shell,
                    mcp_server_name=event.mcp_server_name,
                    mcp_tool_name=event.tool_name,
                )
                if tool_result.action == TOOL_DENY:
                    await self._reject_and_log(
                        client, event.request_id, session_key, event, error="hook_deny"
                    )
                    continue
                if event.child_low_fidelity:
                    # Backend-internal child origin whose SECURITY context is
                    # absent (structured params missing, unresolved shell
                    # classification, or shell without a recoverable command —
                    # AcpEvent.child_low_fidelity): any AUTO-approve would
                    # rest on the LLM-authored title alone, so skip the hook
                    # auto-approve and parent_policy=auto branches. When an
                    # interactive approver IS configured — the per-subagent
                    # factory, or the gateway-level _on_tool_approval
                    # fallback the non-child path below also uses — hand the
                    # decision to it: that is a human/host judgment, the same
                    # downgrade the dashboard's card provides. Only a truly
                    # headless consumer fails closed.
                    _child_fallback = self._on_tool_approval
                    if self._on_tool_approval_factory or _child_fallback is not None:
                        # The human must know the title is ALL there is: the
                        # structured params the policy gates would verify are
                        # absent, so the displayed text is agent-authored and
                        # unverifiable. Annotate the prompt so the approval
                        # is an informed judgment, not a title-only rubber
                        # stamp.
                        event.title = (
                            "⚠️ UNVERIFIED child request (security context "
                            f"missing — title is agent-authored): {event.title or '<unknown tool>'}"
                        )
                        approved = False
                        # Same human-wait lifecycle as the ordinary callback
                        # branches below: without _awaiting_approval the
                        # reaper reads a healthy approval wait as a stalled
                        # subagent after the idle threshold.
                        info._awaiting_approval = True
                        try:
                            if self._on_tool_approval_factory:
                                approve_cb = self._on_tool_approval_factory(info)
                                approved = bool(await approve_cb(event))
                            elif _child_fallback is not None:
                                approved = bool(
                                    await _child_fallback(
                                        event, info.parent_session_key
                                    )
                                )
                        except Exception:
                            logger.exception("child approval callback failed")
                        finally:
                            info._awaiting_approval = False
                            info.last_activity = time.time()
                        if approved:
                            await self._approve_and_log(
                                client,
                                event.request_id,
                                session_key,
                                event,
                                metadata={
                                    "subagent_id": info.id,
                                    "reason": "child_interactive_approved",
                                },
                                info=info,
                            )
                        else:
                            await self._reject_and_log(
                                client,
                                event.request_id,
                                session_key,
                                event,
                                error="child_interactive_rejected",
                            )
                        continue
                    await self._reject_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        error="child_origin_no_command_context",
                    )
                    continue
                if tool_result.action == TOOL_AUTO_APPROVE:
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "hook_auto_approve"},
                        info=info,
                    )
                    continue
                if parent_policy == "auto":
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "parent_policy_auto"},
                        info=info,
                    )
                    continue
                if self._on_tool_approval_factory:
                    approve_cb = self._on_tool_approval_factory(info)
                    info._awaiting_approval = True
                    try:
                        approved = await approve_cb(event)
                    finally:
                        info._awaiting_approval = False
                        info.last_activity = time.time()
                    if not approved:
                        await self._reject_and_log(
                            client,
                            event.request_id,
                            session_key,
                            event,
                            metadata={"subagent_id": info.id, "reason": "factory_rejected"},
                        )
                        continue
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id},
                        info=info,
                    )
                elif self._on_tool_approval:
                    info._awaiting_approval = True
                    try:
                        approved = await self._on_tool_approval(event, info.parent_session_key)
                    finally:
                        info._awaiting_approval = False
                        info.last_activity = time.time()
                    if not approved:
                        await self._reject_and_log(client, event.request_id, session_key, event)
                        continue
                    await self._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id},
                        info=info,
                    )
                else:
                    # No callback, no auto policy — deny by default
                    await self._reject_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "no_policy_deny_default"},
                    )
                    continue
            elif event.kind == EVENT_TOOL_CALL:
                # Auto-allowed (kiro-internal) tools surface here as informational
                # tool_call updates and NEVER as EVENT_PERMISSION_REQUEST, so this
                # is the only progress signal a simple/read-only subagent task emits.
                # Count it, record it, and broadcast the same subagent_tool event
                # the permission path uses so the running-card shows live activity.
                info.tool_count += 1
                info.last_tool = event.title or info.last_tool
                self._note_tool_dispatch(info, event)
                await self._fire_event(
                    "subagent_tool",
                    info,
                    {
                        "tool": _redact(event.title or ""),
                        "tool_kind": event.tool_kind,
                        "turns": info.turns,
                        "tool_count": info.tool_count,
                    },
                )
                # Fire PreToolUse hooks for auto-approved tools (informational only)
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="subagent",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="auto_approved",
                    metadata={"subagent_id": info.id},
                )
                # Cache tool name so PostToolUse can recover it on EVENT_TOOL_RESULT.
                # Strip "Running: " prefix to match the name passed to PreToolUse hooks.
                _raw = event.title or ""
                if _raw.startswith("Running: "):
                    _raw = _raw[9:]
                if event.tool_call_id:
                    _pending_tools[event.tool_call_id] = _raw
                await fire_tool_hooks(
                    self.hook_store,
                    event.title,
                    event.tool_input,
                    subagent_id=info.id,
                    parent_session_key=info.parent_session_key or None,
                    agent_role=info.agent or None,
                )
            elif event.kind == EVENT_TOOL_RESULT:
                # A FINAL result means the tool is done: drop the attribution
                # snapshot so a later idle stretch is not judged against a
                # command that has already returned. A non-final progress frame
                # is not the end of the tool — the gate is in _note_tool_result.
                self._note_tool_result(info, event)
                # Fire PostToolUse hooks (parity with chat_runner). Until this
                # branch existed, hooks registered for subagent-spawned tools
                # received PreToolUse but never PostToolUse — losing the
                # tool_response payload.
                if self.hook_store is not None:
                    try:
                        _tool_name = _pending_tools.pop(event.tool_call_id, "")
                        _out = _redact((event.tool_output or "")[:2000])
                        await self.hook_store.fire(
                            HOOK_EVENT_POST_TOOL_USE,
                            tool_name=_tool_name,
                            tool_response={"output": _out},
                            subagent_id=info.id,
                            parent_session_key=info.parent_session_key or None,
                            agent_role=info.agent or None,
                        )
                    except Exception:
                        logger.debug(
                            "PostToolUse hook error in subagent",
                            exc_info=True,
                        )
            elif event.kind == EVENT_COMPLETE:
                _complete_event = event
                break

        # Strip [OPTIONS: ...] tags and redact sensitive content
        cleaned, _ = extract_options(result_text) if result_text else (result_text, [])
        if cleaned:
            from kiro_crew.security import (
                redact_credentials,
                redact_exfiltration_urls,
            )

            cleaned, _ = redact_exfiltration_urls(cleaned)
            cleaned, _ = redact_credentials(cleaned)
        info.result = cleaned or "_No response._"
        # Cap disk file and trim memory — gateway decides how much to show based on mode.
        if info.result_path:
            cap_result_file(Path(info.result_path))
        # Flag whether the completion-event copy will drop content, so the gateway
        # emits a summary + result_path pointer (read on demand) instead of a lossy
        # blob. The full transcript stays in result.txt for the TTL grace window.
        info.result_truncated = (
            self._completion_keep_chars > 0 and len(info.result) > self._completion_keep_chars
        )
        info.result = apply_completion_keep(
            info.result,
            self._completion_keep,
            self._completion_keep_chars,
        )
        evict_completed_agents(self._agents)

        # ── Per-turn usage row: attribute subagent spend. ──
        # Deliberately BEFORE `info.done`: the caller's cleanup (which awaits
        # provider.shutdown() -> handle.destroy()) runs after this function
        # returns, so an await placed after `done` sits inside the
        # done-to-teardown window. Waiters that poll for `done` would then
        # observe completion while this file write is still in flight — which
        # widens that window on slow filesystems and lets teardown-observing
        # callers race it. Writing first also means `done` never becomes
        # visible with the usage row still missing.
        try:
            # circular import: reached while kiro_crew.slack.handler is still
            # initialising (dashboard/handlers/files.py imports is_tracked_channel
            # from it), so a module-scope import raises ImportError under the
            # suite's import order.
            from kiro_crew.dashboard.handlers.usage import (
                persist_token_record_async,
                read_context_tokens,
                read_effective_agent,
            )

            _used, _window = read_context_tokens(client)
            await persist_token_record_async(
                session_key,
                info.model or "",
                _complete_event,
                provider="claude_code" if is_cc else "acp",
                surface="subagent",
                # Explicit/inherited `agent` FIRST here — unlike every other
                # surface. Under session sharing this subagent reuses the
                # PARENT's runtime, so read_effective_agent() would report the
                # parent's agent and misattribute a `spawn_run(agent="…")` turn.
                # `agent` is already the resolved value (it inherits the parent
                # session's agent when the spawn did not name one), and the
                # helper stays as the fallback for when it is empty.
                agent=agent or read_effective_agent(client) or "",
                context_used=_used,
                context_window=_window,
                elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                model_source=client,
            )
        except Exception:
            logger.debug("usage row (subagent) persist failed", exc_info=True)

        info.done = True
        self._sessions.record_success(session_key)

        Stats().inc_subagent_completed()
        logger.info("Subagent %s completed", info.id)

    def _should_use_session_sharing(self, info: SubagentInfo) -> bool:
        """Decide whether a subagent should use the shared-runtime path.

        All must hold: session_sharing config True; parent session exists and
        is ACP/kiro-backed (not CC); not a CC-specific spawn (model/allowed_tools/bare).
        """
        try:
            cfg = KiroCrewConfig.load()
            if not cfg.agent.session_sharing:
                return False
        except Exception:
            return False
        if info.model or info.allowed_tools or info.bare:
            return False
        if not info.parent_session_key:
            return False
        return self._sessions.is_session_sharing_eligible(info.parent_session_key)

    async def _create_shared_session(
        self, info: SubagentInfo, session_key: str, agent: str
    ) -> "LLMProvider":
        """Create a subagent session on the parent's AcpRuntime.

        The parent session (provider=kiro) runs on an AcpRuntime via
        AcpSessionProvider. Subagents create additional sessions on that SAME
        runtime — one process hosts everything. Falls back to
        get_subagent_runtime() (companion runtime) if the parent doesn't use
        AcpSessionProvider. Marks info._session_sharing=True so cleanup calls
        provider.shutdown() instead of SessionManager.release/reset.
        """
        runtime = self._get_parent_runtime(info.parent_session_key)
        if runtime is None:
            runtime = await self._sessions.get_subagent_runtime(info.parent_session_key)

        cwd = info.cwd or str(getattr(self._sessions, "_pool_cwd", ""))
        handle = await runtime.create_session(
            cwd=cwd or None,
            agent=agent or None,
        )
        provider = AcpSessionProvider(handle, runtime)
        # This consumer implements the low-fidelity child downgrade (interactive
        # approver when configured, reject when headless) — opt in so the
        # handle-level fail-close gate yields those events instead of rejecting.
        provider.child_fidelity_aware = True
        info._session_sharing = True
        info._shared_provider = provider
        if runtime.pid:
            info._pid = runtime.pid
            update_state(info.id, pid=runtime.pid, pid_recorded_at=time.time())
        logger.info(
            "Subagent %s using session sharing on runtime PID %s (session %s, key %s)",
            info.id,
            runtime.pid,
            handle.session_id,
            session_key,
        )
        return provider

    def _get_parent_runtime(self, parent_session_key: str) -> "AcpRuntime | None":
        """Extract the AcpRuntime from the parent session's provider.

        Returns the runtime if the parent uses AcpSessionProvider (kiro unified
        path), or None if the parent uses AcpClient (CC or legacy).
        """
        provider = self._sessions.get_provider(parent_session_key)
        if provider is None:
            return None
        inner = getattr(provider, "client", None) or getattr(provider, "_client", None)
        if isinstance(inner, AcpSessionProvider):
            return inner._runtime
        return None

    @staticmethod
    def _is_cc_provider(provider: object) -> bool:
        """Check if a provider routes to Claude Code.

        Matches both the (dead) standalone ``ClaudeCodeProvider`` and the
        real default backend ``AcpProvider(acp_backend="claude")``.  The
        latter is what ``_sessions.get_or_create`` actually returns for the
        ``claude_code`` provider, so detecting it here is what makes the
        session-file cleanup target ``~/.claude`` instead of ``~/.kiro``.
        """
        if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
            return True
        # circular import: providers.acp participates in a providers -> session
        # cycle (see session.py), so keep this off the module top.
        from kiro_crew.providers.acp import is_claude_backend

        return is_claude_backend(provider)

    @staticmethod
    def _provider_label_of(provider: object) -> str:
        """Backend identity key for *provider*, persisted with the run's state.

        Mirrors ``_is_cc_provider`` in also matching the (dead) standalone
        ``ClaudeCodeProvider``, which the shared ``provider_label`` helper does
        not know about.
        """
        if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
            return PROVIDER_LABEL_CLAUDE
        # circular import: see _is_cc_provider.
        from kiro_crew.providers.acp import provider_label

        return provider_label(provider)

    def _cancel_task_intentionally(
        self,
        task: "asyncio.Task",  # type: ignore[type-arg]
        info: "SubagentInfo | None" = None,
        *,
        reason: str,
    ) -> None:
        """The single sanctioned chokepoint for INTENTIONALLY cancelling a
        manager-owned subagent task.

        Enforces the intentional-cancel contract mechanically instead of by
        docstring: a terminal marker MUST already be visible before the cancel
        is issued (``info.user_stopped`` / ``info.reaped`` / ``info.done`` /
        ``self._shutting_down``), otherwise ``_run``'s CancelledError arm
        classifies the cancel as unexpected and auto-respawns the run — a
        zombie respawn of work this call site meant to kill. A source-scan
        test asserts every raw ``.cancel()`` on a managed run task in this
        module routes through here.

        Missing marker → loud error + the recovery budget is consumed
        defensively (``_cancel_retry_used``) so a mis-marked intentional
        cancel can never zombie-respawn; the cancel still proceeds.
        """
        marked = self._shutting_down or (
            info is not None and (info.user_stopped or info.reaped or info.done)
        )
        if not marked:
            logger.error(
                "Intentional cancel (reason=%s) issued WITHOUT a terminal "
                "marker — consuming the recovery budget defensively to "
                "prevent a zombie auto-respawn. Fix the call site: set "
                "user_stopped/reaped/done or _shutting_down BEFORE cancelling.",
                reason,
            )
            if info is not None:
                info._cancel_retry_used = True
        task.cancel()

    def _unqueue(self, agent_id: str) -> bool:
        """Drop a not-yet-started spawn from the stagger queue. True if removed.

        The queue is the only record of a waiting run — `spawn` returns its queued
        SubagentInfo without registering it in ``_agents`` — so removing the entry
        is what makes a cancel take effect before the work exists. Also re-emits the
        parent's queued depth, or the chip keeps counting an agent that will never
        run.
        """
        keep = [p for p in self._queue if str(p.get("_preassigned_id") or "") != agent_id]
        if len(keep) == len(self._queue):
            return False
        dropped = [p for p in self._queue if str(p.get("_preassigned_id") or "") == agent_id]
        self._queue = keep
        for p in dropped:
            try:
                self._emit_queue_depth(
                    str(p.get("parent_session_key", "")), str(p.get("batch_id", ""))
                )
            except Exception:
                logger.debug("queue-depth re-emit failed after unqueue", exc_info=True)
        return True

    async def cancel(self, agent_id: str) -> bool:
        """Cancel a single running subagent. Returns True if found and cancelled.

        User-initiated stop is a neutral terminal state, not an error: partial
        output is preserved on the info record (and remains in result.txt), the
        tombstone is written as ``user_stop``, and the ``subagent_done`` event
        carries ``stopped: true`` so the UI renders a neutral "stopped" card.
        """
        info = self._agents.get(agent_id)
        if not info or info.done:
            # A run still WAITING behind the stagger has no `_agents` record at
            # all: `spawn` builds its queued SubagentInfo and returns it without
            # registering. So this used to answer False and leave the entry in the
            # queue, which the drain later started — the stop was reported as
            # ineffective while the work ran anyway, and a purge on a deleted
            # session could not reach it. Unqueueing IS the cancel for that state.
            if self._unqueue(agent_id):
                logger.info("Cancelled queued subagent %s before it started", agent_id)
                return True
            return False
        info.user_stopped = True
        # Neutral semantics live in the RECORD, not just the live event: a user
        # stop leaves ``error`` unset so every consumer (reconnect snapshots,
        # tombstones, /api/spawn listing, orphan reconciliation) derives the
        # same neutral "stopped" status without having to cross-check
        # ``user_stopped``. _force_reap is also user_stopped-aware and will not
        # synthesize a reap error for this path.
        # Preserve whatever streamed before the stop as a partial result.
        if not info.result and info.streaming_text:
            info.result = info.streaming_text
        # _force_reap emits the (single) stopped-aware ``subagent_done`` event
        # and drives _on_done delivery — no second event here.
        await self._force_reap(agent_id, info, time.time() - info.started, reason="user_stop")
        return True

    async def cancel_all(self) -> None:
        """Cancel all running subagents and wait for cleanup."""
        # Shutdown-driven cancellations must never trigger the one-shot
        # unexpected-cancel auto-continue (the loop is going away).
        self._shutting_down = True
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            self._reaper_task = None
        # follow_up watchers: CANCEL AND GATHER FIRST, announce after (GPT
        # review). The announce awaits — _on_done injection can be slow — and
        # a busy-retry watcher waking during that await could dispatch a
        # continuation into the shutting-down gateway, so every watcher task
        # must be DEAD before anything here yields. Announcing afterwards is
        # safe: the settle-after-outcome protocol leaves undelivered messages
        # in their queues, so each is still present to be reported. An
        # ACCEPTED follow-up must not die silently: the spawn_steer reply
        # promised the parent a completion event, so each non-empty queue is
        # announced as a synthetic failure — the parent learns the message was
        # dropped instead of waiting forever.
        # Snapshot ids BEFORE cancelling: each watcher's done-callback pops it
        # from the dict as the gather completes it, so a post-gather snapshot
        # is already empty.
        watcher_ids = list(self._followup_watchers)
        followup_watchers = [t for t in self._followup_watchers.values() if not t.done()]
        for followup_watcher in followup_watchers:
            followup_watcher.cancel()
        if followup_watchers:
            await asyncio.gather(*followup_watchers, return_exceptions=True)
        self._followup_watchers.clear()
        for agent_id in watcher_ids:
            watcher_info = self._agents.get(agent_id)
            if watcher_info is not None and watcher_info.pending_followups:
                dropped = list(watcher_info.pending_followups)
                watcher_info.pending_followups = []
                self._audit_followup(watcher_info, "followup_expired")
                try:
                    await self._announce_followup_failure(
                        watcher_info,
                        "follow_up dropped: the gateway is shutting down before "
                        "the run completed; the queued message(s) were not "
                        "dispatched",
                        messages=dropped,
                    )
                except Exception:  # noqa: BLE001 - shutdown must not wedge here
                    logger.debug(
                        "shutdown follow_up announce failed for %s", agent_id, exc_info=True
                    )
        tasks_to_await: list[asyncio.Task] = []  # type: ignore[type-arg]
        for agent_id, task in list(self._tasks.items()):
            if not task.done():
                # _shutting_down (set above) is the terminal marker for this
                # site; the chokepoint enforces the contract mechanically.
                self._cancel_task_intentionally(task, self._agents.get(agent_id), reason="shutdown")
                tasks_to_await.append(task)
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        self._tasks.clear()
        # Shielded terminal reports keep running after their awaiter is
        # cancelled (that is the point). Drain them with a BOUNDED wait so a
        # report is not orphaned by a closing event loop, without letting a
        # wedged injection block shutdown indefinitely.
        pending_reports = [t for t in self._report_tasks if not t.done()]
        if pending_reports:
            try:
                await asyncio.wait(pending_reports, timeout=_REPORT_DRAIN_TIMEOUT)
            except Exception:
                logger.debug("cancel_all: report drain wait failed", exc_info=True)
            # `asyncio.wait` RETURNS on timeout without touching the stragglers.
            # Leaving them pending is worse than not shielding at all: shutdown
            # would proceed while they keep invoking `_on_done` against
            # tearing-down state, and they would then die when the loop closes —
            # losing the very report the shield exists to guarantee. So cancel
            # them explicitly and gather to completion, which also surfaces any
            # exception into the log instead of an "exception was never
            # retrieved" warning at interpreter exit.
            stragglers = [t for t in pending_reports if not t.done()]
            if stragglers:
                logger.warning(
                    "cancel_all: %d terminal report(s) did not drain in %.0fs — "
                    "cancelling; their completions may not have been delivered",
                    len(stragglers),
                    _REPORT_DRAIN_TIMEOUT,
                )
                abandoned = [self._report_owners.get(t) for t in stragglers]
                for report_task in stragglers:
                    report_task.cancel()
                try:
                    await asyncio.gather(*stragglers, return_exceptions=True)
                except Exception:
                    logger.debug("cancel_all: straggler gather failed", exc_info=True)
                # A cancelled report is a LOST delivery, and the terminal record
                # for it was already written — including a tombstone, which is
                # exactly what `list_orphans()` uses to exclude a folder from the
                # next start's reconciliation. Left alone, the outcome is
                # unrecoverable: never injected, and invisible to the one path
                # that could still inject it.
                #
                # Extending the drain to `_ON_DONE_TIMEOUT` instead was rejected:
                # it would hold gateway shutdown for up to 20 minutes on a single
                # wedged injection, which is what the bounded drain exists to
                # prevent. Bounded shutdown plus recoverable state is strictly
                # better than unbounded shutdown.
                #
                # Only reports cancelled BEFORE `_on_done` returned are re-admitted
                # — `_reported_to_parent` marks the ones that already reached the
                # parent, so a cancellation in the later teardown/tombstone waits
                # does not cause a duplicate delivery on restart.
                for task, owner in zip(stragglers, abandoned):
                    if owner is None or not task.cancelled():
                        continue
                    if owner._reported_to_parent:
                        continue
                    try:
                        if clear_tombstone(owner.id):
                            logger.warning(
                                "cancel_all: %s's completion was not delivered — "
                                "re-admitted to orphan recovery for the next start",
                                owner.id,
                            )
                    except Exception:
                        logger.debug(
                            "cancel_all: failed to re-admit %s to orphan recovery",
                            owner.id,
                            exc_info=True,
                        )
