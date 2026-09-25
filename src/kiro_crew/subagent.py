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
from collections.abc import Awaitable, Callable, Container, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol

from kiro_crew.acp.liveness import (
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
    boottime_now,
    consult_offloaded,
)
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import PROVIDER_LABEL_CLAUDE, PROVIDER_LABEL_DEFAULT
from kiro_crew.agent_sdk.drivers.acp_vocab import (  # noqa: F401 - STOP_* resolved by run.py via bind_component_globals
    STOP_CLASS_CANCELLED,
    STOP_CLASS_FAILED,
    STOP_CLASS_SUCCEEDED,
    STOP_RECOVERY_MAX_RETRIES,
    classify_stop_reason,
    is_runtime_death,
)
from kiro_crew.executors import run_in_embed_pool

if TYPE_CHECKING:
    from kiro_crew.execution_context import ExecutionContext
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.providers.base import LLMProvider

from kiro_crew import name_grant, platform_compat
from kiro_crew.agent_discovery import cached_project_agent_names, list_agents
from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.agent_sdk.provider_identity import PROVIDER_CLAUDE_CODE
from kiro_crew.config import live
from kiro_crew.config.loader import DEFAULT_MODEL, KiroCrewConfig
from kiro_crew.config.paths import data_home
from kiro_crew.constants import (
    DEFAULT_SUBAGENT_MAX_TURNS,
    SUBAGENT_COMPLETION_PREFIX,
    SUBAGENT_TIMEOUT_SECS,
)
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
from kiro_crew.effort import effort_settings_key, model_supports_effort
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    fire_tool_hooks,
    hook_gate_kwargs,
    identity_grant_covers_child,
)
from kiro_crew.llm_helpers import (
    FALLBACK_CANDIDATE_ATTEMPTS,
    FALLBACK_STORY_ATTR,
    TRANSIENT_RETRIES,
    FallbackState,
    acp_error_is_transient,
    advance_fallback_candidate,
    annotate_model_fallback,
    append_fallback_story,
    configured_fallback_chain,
    provider_fallback_active,
    transient_retry_delay,
)
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED, emit_counter
from kiro_crew.platform.context import redact_via_context
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
)
from kiro_crew.resource_status import cached_admission_check
from kiro_crew.sandbox import _agents_slice_cgroup_dir
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
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
    _SAMPLE_MAX_AGE_SECS,
    append_cost_sample,
    cap_buckets,
    compact_cost_log,
    cost_log_identity,
    learned_cost_for,
    read_learned_cost,
    read_learned_costs,
    read_learned_costs_checked,
)
from kiro_crew.subagent_manager import (
    CancellationCoordinator,
    ClaimPoint,
    ContinuationCoordinator,
    OrphanStallMonitor,
    PreparedSpawn,
    RunEventCoordinator,
    SpawnAdmissionCoordinator,
    TerminalCoordinator,
    WaveDigestCoordinator,
    bind_component_globals,
    copy_component_docs,
)
from kiro_crew.subagent_manager.monitoring import (  # noqa: F401 - resolved by monitoring.py via bind_component_globals
    orphan_resume_hint,
    tombstone_recovery_action,
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
from kiro_crew.subagent_wait_reasons import (  # noqa: F401 - re-exported: the gate and handlers read them from this namespace
    DEFERRED_QUEUED_REASONS,
    QUEUED_REASON_ADAPTIVE_CAP_ZERO,
    QUEUED_REASON_CONCURRENCY_LIMIT,
    QUEUED_REASON_LOW_MEMORY,
    QUEUED_REASON_POSTURE_CRITICAL,
)
from kiro_crew.validation import _AGENT_NAME_RE

# Standalone ClaudeCodeProvider removed (KiroACP-only). Name kept as None so the
# legacy isinstance guards short-circuit; which seam serves a session is answered
# by ``SessionCapabilities.provider_seam``.
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

#: Agent names a roster never suggests: the host default and the conductors are
#: reached by OMITTING ``agent``, not by naming one. Every roster inherits this
#: as :func:`visible_agent_names`' default ``exclude``, so no other module names
#: the set and it cannot drift when a reserved name appears.
UNADVERTISED_AGENTS = frozenset(
    {
        "kirocrew",
        "kirocrew-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
    }
)

#: Wire code for the unknown-agent refusal ``_validate_agent`` returns. It rides
#: ``SubagentInfo.error_code`` to ``POST /api/spawn``, which forwards the FIELD as
#: the response's ``code`` without naming the value -- so the gateway handler never
#: spells this identifier and stays agnostic about which code it is carrying. The
#: refusal's PROSE is advisory and free to be reworded (RFC 9457 3.1.3); this is
#: the contract ``spawn_run`` switches on, and here is the only place the literal
#: appears: ``mcp_tools.spawn`` imports it, under the same single-definition rule
#: as the reserved pair above, because a respelled literal is exactly the drift a
#: code exists to remove.
AGENT_NOT_FOUND_CODE = "agent_not_found"

# Why an accepted spawn is WAITING rather than running -- the label the admission
# gate puts on the wait it decided on, carried on ``SubagentInfo.queued_reason``,
# on the ``subagent_queued`` lifecycle event (``reason``) and, for the deferred
# kinds, on ``POST /api/spawn``'s ``status: "queued"`` answer. Defined in the leaf
# module :mod:`kiro_crew.subagent_wait_reasons` so a surface that must not import
# this module at runtime (the channel command layer) can still read them; re-exported
# here (see the import block above) because the gate and the handlers read them
# from this namespace.


def visible_agent_names(
    names: Iterable[str],
    *,
    exclude: Container[str] = UNADVERTISED_AGENTS,
    limit: int | None = None,
) -> tuple[list[str], int]:
    """Make a roster of agent names safe to render, and bound it.

    Returns ``(shown, withheld)``: the names that may be rendered, and how many
    the *limit* dropped (``0`` when nothing was dropped, so a caller appends its
    "+N more" only when there is a remainder to report).

    Three surfaces render this roster -- an unknown-agent refusal, the spawn
    tools' parameter descriptions, and ``spawn_list``'s output -- and all three
    come through the pipeline below rather than re-implementing it. Duplicated
    copies drift, so the SAFETY half lives here where a fourth surface cannot
    omit it:

    * **Grammar.** Every name must match ``_AGENT_NAME_RE`` before it is
      rendered. This is the load-bearing filter, not a tidiness check: an agent
      spec's ``name`` field is taken verbatim by
      ``agent_discovery._global_agent_info`` with no validation, so a spec can
      declare a name containing a newline plus instruction-shaped text -- which
      is pure ASCII, so an ``isascii`` check passes it -- and it would ride this
      string into a model's context. ``SPAWN_RUN_SCHEMA`` gates the ``agent``
      parameter on the same grammar, so a name that fails it could never have
      been dispatched anyway: offering it would advertise an unusable name.
    * **Redaction.** A grammar-valid name can still be credential-shaped (an
      AWS access key is pure alphanumerics), so each name goes through the
      canonical context-aware shim rather than being trusted.
    * **Bound.** Every rendered roster that reaches always-on context is capped,
      and the remainder is returned as a count instead of being silently lost.

    Order is the CALLER's: this returns names in the order it received them, so
    each surface keeps the presentation its own copy promises. *exclude* defaults
    to the reserved pair (reached by omitting ``agent``, never by naming one);
    ``spawn_list`` passes an empty set on purpose, because the two bounded
    rosters point at it as the surface that lists everything.
    """
    kept = [
        redact_via_context(n)
        for n in names
        if n and n not in exclude and _AGENT_NAME_RE.fullmatch(n)
    ]
    if limit is None or len(kept) <= limit:
        return kept, 0
    return kept[:limit], len(kept) - limit


# How many valid names an unknown-agent refusal carries. The string reaches a WS
# frame, a tombstone and the caller's transcript, so it is bounded like every
# other rendered detail in this module; the remainder is reported as a count with
# a pointer to spawn_list, which lists them all.
_MAX_AVAILABLE_IN_ERROR = 12


def _available_agents_hint(available: list[str]) -> str:
    """Render the valid-name roster for an unknown-agent refusal.

    The names are computed anyway, to log the refusal. Withholding them from the
    RETURNED error leaves the caller unable to self-correct: it retries other
    invented names while every log line already holds the answer, and the
    log is not a surface the caller can read.

    Filtering, redaction and the bound are :func:`visible_agent_names`; the
    caller already sorted *available*, and that order is preserved.
    """
    shown, withheld = visible_agent_names(available, limit=_MAX_AVAILABLE_IN_ERROR)
    if not shown:
        # An empty roster is a different instruction than a truncated one: there
        # is no name to correct to, so the only valid move is to stop naming an
        # agent at all.
        return "; no other agents are installed - omit 'agent' to use the default"
    hint = "; available: " + ", ".join(shown)
    if withheld:
        hint += f" (+{withheld} more, call spawn_list)"
    return hint


def _validate_app_agent_ownership(agent: str, app: str) -> str:
    """The app-ownership proof the SpawnSDK runs at request time, repeated for
    a spawn that waited in the queue: *agent* must be one of *app*'s own
    materialized agents (``<app>--<agent>.json``). Returns the refusal reason,
    or ``""`` when the agent is the app's own."""
    prefix = f"{app}--"
    try:
        known = {a.name for a in list_agents() if a.filename.startswith(prefix)}
    except Exception as exc:  # noqa: BLE001 - cannot confirm -> refuse
        return f"cannot verify agent {agent!r} for app {app!r}: {exc}"
    if agent not in known:
        return (
            f"app {app!r} may only spawn its OWN agents ({prefix}*); {agent!r} is not one "
            "(refusing to run the host default or another app's agent)"
        )
    return ""


def _validate_agent(requested: str, project_dir: str = "") -> tuple[str, str, str]:
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

    Returns (agent_name, error, code). If the agent is found, error and code are
    both empty. If not, agent_name is empty, error explains what happened in prose
    and code is the machine-readable identifier for that decision. The code is
    returned rather than inferred by the caller so that a SECOND refusal kind
    added here has to choose its own identifier instead of silently inheriting
    this one.
    """
    if not requested:
        return "", "", ""
    known = {a.name for a in list_agents()}
    if project_dir:
        known |= set(cached_project_agent_names(project_dir) or frozenset())
    if requested in known:
        return requested, "", ""
    available = sorted(known - UNADVERTISED_AGENTS)
    # REFUSE a named-but-unknown agent rather than silently falling back to the
    # host default: that fallback runs the full default agent (frequently at
    # approval_mode="auto"), so a typo'd — or malicious — agent name was a silent
    # privilege escalation at the manager primitive. An EMPTY request still means
    # "use the default" (handled above); only a named agent that does not exist
    # is rejected, so a future caller cannot reintroduce the escalation.
    logger.warning("Agent %r not found; refusing spawn. Available: %s", requested, available)
    # The roster travels WITH the refusal, not only to the log: the caller acts on
    # the returned string, and a bare "not found" gives it nothing to correct to.
    return (
        "",
        f"agent {requested!r} not found{_available_agents_hint(available)}",
        AGENT_NOT_FOUND_CODE,
    )


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
        # Fail CLOSED: a governance evaluation error must DENY the spawn, not
        # silently permit it. PlatformCompositionError already propagates above;
        # every other error lands here and is audited before denial.
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


def _redact_and_truncate(text: str, max_chars: int) -> str:
    """Redact over the FULL text, then truncate (never ``_redact(x[:n])``).

    Truncating first can cut a credential in half at the boundary, leaving a
    fragment the redaction regexes do not match — the raw remainder would
    then leak into the surface this feeds. Delegates to the canonical helper.
    """
    return redact_and_truncate(text, max_chars)


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

# Width of a run id, in hex characters. 16 characters is 8 bytes, and every one
# of those 64 bits is random, which is what makes a uniqueness CHECK
# unnecessary: 2000 draws on one host collide with probability about
# 2000**2 / (2 * 2**64), roughly 1 in 10**13, against 1 in 2,100 at the 8
# characters this replaces. Nothing in the product pins the width -- the only
# consumers print or pass the id through -- so widening is cheaper than any
# mechanism that would have to remember which ids are taken, and a durable row
# outlives the process that wrote it, so remembering means reading the store.
_RUN_ID_HEX_CHARS = 16


def _done_result(text: str) -> str:
    """Redact + cap result for inclusion in subagent_done event."""
    if not text:
        return ""
    redacted = _redact(text)
    if len(redacted) <= _MAX_DONE_RESULT_LEN:
        return redacted
    return "…(truncated)\n" + redacted[-_MAX_DONE_RESULT_LEN:]


# Wall-clock deadline for one subagent run: the fallback when config is
# unavailable or ``agent.subagent_timeout_secs`` is 0. One owner in
# ``constants`` because the MCP gateway's hard-wedge ceiling has to sit above
# it (see ``mcp_gateway/backend.py``).
_TIMEOUT_SECS = SUBAGENT_TIMEOUT_SECS
_TURN_LIMIT = DEFAULT_SUBAGENT_MAX_TURNS
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
_RECOVERY_SLOT_WAIT_SECS = 60.0
_REPORT_DRAIN_TIMEOUT = (
    30.0  # max seconds cancel_all() waits for shielded terminal reports to drain
)
# Bound retained failures inside each boundary scope after terminal tasks disappear.
_REPORT_FAILURE_BYTE_BUDGET = 64 * 1024 * 1024
_REPORT_FAILURES_PER_PARENT_CAP = 64
_REPORT_FAILURE_PAYLOAD_MAX_BYTES = 64 * 1024
_REPORT_RETENTION_REFUSED_BYTE_BUDGET = "byte_budget"
_REPORT_RETENTION_REFUSED_ROW_CAP = "row_cap"
# Match the dashboard's process-wide live-slot ceiling. A stage can carry more
# than one routed parent, so aliases may exhaust this bound earlier; that stage
# then stays closed instead of expanding the manager's retained-scope map.
_PENDING_BOUNDARY_CANCELLATION_SCOPE_CAP = 500
# This fixes the retained diagnostic-text budget independently of exception size.
_PENDING_BOUNDARY_CANCELLATION_FAILURE_MAX_CHARS = 2_000
_BOUNDARY_CANCELLATION_SCOPE_CAP_REASON = "pending_scope_cap"
# Max seconds a cancelled run holds cancellation open for an in-flight off-loop
# state.json write worker -- every off-loop writer: long enough for any healthy
# fsync, short enough that a wedged FS
# cannot hold cancel_all()'s untimed gather — bounded shutdown plus recoverable
# state beats unbounded shutdown.
_STATE_DRAIN_TIMEOUT = 5.0
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


def _resolved_model_of(client: object) -> str:
    """The model id *client*'s live session actually resolved to serve, or ``""``.

    Reads the provider's PUBLIC ``served_model`` accessor (never private
    ``_client`` internals, which are free to move) — the same contract the
    poisoned-conversation canary and ``AcpProvider.served_model`` use. Both
    provider shapes are covered: ``AcpSessionProvider.served_model`` prefers the
    explicit ``set_model`` and falls back to the ``session/new|load`` response's
    ``currentModelId`` (so a session on the backend-selected DEFAULT is still
    readable at spawn), while the raw ``AcpClient`` reports ``_resolved_model_id``
    once the backend has answered (known after the first turn on the CC path).

    The ``DEFAULT_MODEL`` (``"auto"``) sentinel — "let the backend pick", not yet
    resolved — is filtered to ``""`` (unknown/inconclusive) so a caller never
    renders it as if it were a real model, and callers must treat ``""`` as
    "don't show", never as a wildcard. Never raises — an unreadable or
    duck-typed client (test doubles) yields ``""``.
    """
    try:
        model = str(getattr(client, "served_model", "") or "").strip()
    except Exception:
        return ""
    return "" if model == DEFAULT_MODEL else model


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


def _spawn_effective_model(model: str, agent: str, *, crew_agent: str | None = None) -> str | None:
    """Resolve the factory's model; ``""`` means auto, ``None`` means unavailable.

    Not a re-encoding of the factory's precedence — the selection itself is
    :meth:`KiroCrewConfig.acp_effective_model`, the same function the factory
    calls, so this verdict cannot drift from the gate it reports on. What this
    wrapper reproduces is only the CALLER side of the chain, exactly as the
    spawn path drives ``get_or_create``: the kwarg the spawn passes (explicit
    per-spawn *model*, else the subagent role pin — see ``_run_inner``, which
    forwards raw ``info.model`` including an explicit ``"auto"``), and, when no
    kwarg is passed, ``session._session_model`` for *agent* (a crew's own pin,
    else non-sentinel global; the factory resolves a named template's JSON pin).
    An explicit empty ``crew_agent`` retains the template namespace; a member
    claim retains that member's pin and bound template. Omitted claims keep the
    helper's crew-name inference. Reporting never prepares a capability runtime.
    """
    try:
        # circular imports (config.loader / session import sibling modules at
        # load time, matching the lazy-import convention of _subagent_default_*)
        from kiro_crew.config.loader import KiroCrewConfig, resolve_crew_identity
        from kiro_crew.session import _session_model

        # The kwarg the spawn path actually passes (see _run_inner): raw
        # info.model — an explicit "auto" flows through VERBATIM and the
        # factory treats it as a truthy override — else the role pin.
        override: str | None = model or _subagent_default_model() or None
        cfg = KiroCrewConfig.load()
        claim = resolve_crew_identity(cfg, agent or None, crew_agent)
        if claim:
            member = cfg.agents.get(claim)
            if member is None or not isinstance(member.kiro_agent, str):
                return None
            # The factory receives the bound provider template, never the alias.
            # This matters when the member defers to the template's own model.
            agent = member.kiro_agent
        if override is None:
            # No kwarg: get_or_create resolves the session chain and passes
            # its result (possibly None) as model_override.
            override = _session_model(cfg, agent or None, crew_agent=claim)
        return cfg.acp_effective_model(agent or None, override) or ""
    except Exception:
        return None


def effort_drop_reason(
    model: str, reasoning_effort: str, agent: str = "", *, crew_agent: str | None = None
) -> str:
    """Why a requested per-spawn effort will not take effect, or ``""``.

    Mirrors the model resolution the provider factory's effort gate actually
    sees (explicit per-spawn model, else the subagent role pin, else the selected
    member's pin, template pin and global fallback). A resolved ``auto`` cannot
    carry an effort level through the overlay. Returns a human-readable reason when
    *reasoning_effort* is set
    but the resolved model is not effort-capable; ``""`` means the effort will
    be delivered, none was requested, or the selection could not be resolved.
    Reporting-only: never raises and never influences whether or how a spawn
    proceeds. ``crew_agent`` has the same namespace semantics as allocation.
    """
    if not reasoning_effort:
        return ""
    resolved = _spawn_effective_model(model, agent, crew_agent=crew_agent)
    if resolved is None:
        return ""
    if not resolved:
        return (
            "no concrete model is pinned — the model resolves to 'auto', which "
            "does not support effort configuration; pass an effort-capable "
            "model= to apply the level"
        )
    if not model_supports_effort(resolved):
        return f"model '{resolved}' does not support effort configuration"
    return ""


def effort_applied_note(
    model: str, reasoning_effort: str, agent: str = "", *, crew_agent: str | None = None
) -> str:
    """The delivery mirror of :func:`effort_drop_reason`, or ``""``.

    Names the resolved model and the family-specific cli.json settings key the
    level is delivered under (``reasoning`` for GPT, ``output_config`` for
    Claude) when a requested per-spawn effort WILL take effect. The key matters
    because kiro-cli silently ignores a level written under the wrong family
    key, so a bare "applied" would leave that failure mode unobservable.
    Complementary with the drop reason when the selection can be resolved:
    exactly one is non-empty for a requested effort. An unavailable selection
    leaves both empty. Reporting-only, same totality contract.
    """
    if not reasoning_effort:
        return ""
    resolved = _spawn_effective_model(model, agent, crew_agent=crew_agent)
    if not resolved or not model_supports_effort(resolved):
        return ""
    return f"{resolved} → {effort_settings_key(resolved)}.effort"


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


def _timeout_context(
    info: "SubagentInfo", *, include_elapsed: bool = True, turn_limit: int = 0
) -> str:
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


def check_memory_available(
    min_gb: float = 4.0, *, path: str = "/proc/meminfo"
) -> tuple[bool, float]:
    """Check if enough memory is available to spawn a subagent.

    Reads /proc/meminfo MemAvailable with a plain ``open`` and compares
    against *min_gb*. The read deliberately does NOT go through
    ``hooks.safe_read_file``: that gate polices agent-supplied paths, and
    this path is a fixed module constant that no caller overrides in
    production, so the gate adds no protection here — while a gate refusal
    under load would silently disable spawn back-pressure exactly when it
    matters (the gate's refusal modes correlate with CPU contention).
    ``platform_compat._linux_available_mib`` reads the same file the same
    way. The ``path`` keyword is keyword-only and exists for tests only;
    production callers always take the constant.
    Native macOS/Windows readers handle the production path on those hosts.
    Linux production reads also respect cgroup headroom. An explicit test path
    always exercises the file reader. With no readable host memory or finite
    cgroup limit, returns (True, -1.0).
    """
    if path == "/proc/meminfo" and not platform_compat.IS_LINUX:
        if platform_compat.IS_MACOS:
            avail = _macos_available_memory_gb()
        elif platform_compat.IS_WINDOWS:
            avail = _windows_available_memory_gb()
        else:
            avail = -1.0
        return (True, -1.0) if avail < 0 else (avail >= min_gb, round(avail, 2))
    avail = -1.0
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for line in text.splitlines():
            if line.startswith("MemAvailable:"):
                kb = int(line.split()[1])
                avail = kb / (1024 * 1024)
                break
    except (OSError, ValueError, IndexError):
        # A failed host read cannot discard a known container constraint.
        pass
    if path == "/proc/meminfo":
        cgroup_gb = _cgroup_available_gb()
        if cgroup_gb >= 0:
            avail = cgroup_gb if avail < 0 else min(avail, cgroup_gb)
    return (True, -1.0) if avail < 0 else (avail >= min_gb, round(avail, 2))


# Process-subtree readings come from ONE shared walker,
# :func:`platform_compat.proc_subtree_sample`. RSS, CPU and the two counts all
# come from that single walk, which lives above both this module and
# ``mcp_gateway.pool``, so the 256 ceiling and the sentinels cannot drift
# between separate copies.


def _proc_subtree_sample(pid: Optional[int]) -> platform_compat.SubtreeSample:
    """One walk of *pid*'s subtree, carrying all four readings the sweep needs.

    Thin adapter over :func:`platform_compat.proc_subtree_sample` that supplies
    the needle this module counts by: ``STUB_MODULE``, the module path the
    rewriter itself puts on the stub launch line. So ``sample.matched`` is the
    stub count here, and the shared walker stays free of gateway vocabulary
    while this module stays free of a second walk.

    Blocking: reads a handful of ``/proc`` entries per process in the subtree, so
    it belongs on an executor thread, never on the event loop (see
    ``_reaper_loop`` -> ``_sample_live_costs``).
    """
    return platform_compat.proc_subtree_sample(pid, counts=True, needles=(STUB_MODULE,))


def _subtree_cpu_jiffies(pid: int, *, pids: Optional[list[int]] = None) -> int:
    """Sum utime+stime across ``pid`` and its descendants (clock ticks).

    Two routes, and the caller picks one by argument. WITHOUT ``pids`` it asks
    the shared walker for the CPU reading alone, so the subtree is the one the
    task rows describe and no ``status`` read is paid for an RSS figure the
    caller does not use.

    ``pids`` is that subtree when the caller has ALREADY walked it, so the tree
    is not enumerated a second time to total the same processes -- the same
    hand-over ``_get_rss_tree_mb`` takes, and for the same reason: nearly all of
    the cost is the enumeration, not the per-process read. It also makes the CPU
    figure describe exactly the set the caller's other figures describe, where
    two enumerations could disagree (the walker stops at ``_SUBTREE_MAX_PROCS``
    and a caller's own walk need not). This is the route the Sessions session
    rows take, so their CPU figure spans their whole uncapped tree -- the same
    set every other figure on those rows spans -- rather than the walker's first
    ``_SUBTREE_MAX_PROCS`` processes.
    """
    if pids is not None:
        return platform_compat.proc_cpu_jiffies_for_pids(pids)
    return platform_compat.proc_subtree_sample(pid, rss=False, counts=False).jiffies


def _attributed_count(total: Optional[int], sharers: int, previous: Optional[int]) -> Optional[int]:
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
                   then clamped by cgroup headroom so the tighter of a
                   container's limit and the agents slice's ceiling binds.
        • macOS  — reclaimable memory via Mach ``host_statistics64`` (ctypes,
                   in-process, no subprocess); see ``_macos_available_memory_gb``.
                   No cgroups.
        • Windows — ``GlobalMemoryStatusEx`` through
                   ``platform_compat.host_available_mib``. No cgroups.
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
    if platform_compat.IS_WINDOWS:
        return _windows_available_memory_gb()
    # Unsupported platform: no probe yet → fail open.
    return -1.0


def _windows_available_memory_gb() -> float:
    """Available memory (GB) on Windows, or ``-1.0`` when it cannot be read.

    Delegates to ``platform_compat.host_available_mib`` instead of calling
    ``GlobalMemoryStatusEx`` here. That shim is the single place the MiB unit
    and the "0 means unreadable, never zero memory" contract are defined, and a
    second reader would have to restate both to stay correct.

    Without this branch the cap loses its memory term on Windows entirely and
    falls open to ``_LEGACY_DEFAULT_MAX``, so a host with tens of GB free is
    held to the same three concurrent sub-agents as an unmeasurable one.
    """
    available_mib = platform_compat.host_available_mib()
    if available_mib <= 0:
        return -1.0  # unreadable → caller fails open
    return available_mib / 1024.0


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
    """Read a single integer from *path*; None on absence/garbage. 'max' → None.

    Deliberately reads through this module's ``open`` so the sizing tests can
    fabricate every kernel input (membership, mounts, limits) by patching one
    name; the slice probe below shares it for the same reason.
    """
    try:
        with open(path, encoding="ascii") as fh:
            txt = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if txt == "max":  # cgroup v2 unlimited sentinel
        return None
    try:
        return int(txt)
    except ValueError:
        return None


def _cgroup_memory_roots() -> list[tuple[PurePosixPath, PurePosixPath, bool]]:
    """Return (process directory, mount boundary, v2) for visible memory mounts."""
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            memberships = handle.read().splitlines()
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            mounts = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        memberships, mounts = [], []

    groups: dict[bool, PurePosixPath] = {}
    for line in memberships:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        if fields[0] == "0" and not fields[1]:
            v2 = True
        elif "memory" in fields[1].split(","):
            v2 = False
        else:
            continue
        membership = PurePosixPath(fields[2])
        if membership.is_absolute() and ".." not in membership.parts:
            groups[v2] = membership

    roots = []
    for line in mounts:
        before, separator, after = line.partition(" - ")
        fields, fs = before.split(), after.split()
        if not separator or len(fields) < 6 or len(fs) < 3:
            continue
        v2 = fs[0] == "cgroup2"
        if not v2 and not (fs[0] == "cgroup" and "memory" in fs[2].split(",")):
            continue
        group = groups.get(v2)
        if group is None:
            continue
        # mountinfo escapes whitespace and backslashes in its path fields.
        paths = [
            PurePosixPath(
                field.replace(r"\040", " ")
                .replace(r"\011", "\t")
                .replace(r"\012", "\n")
                .replace(r"\134", "\\")
            )
            for field in fields[3:5]
        ]
        root, mount = paths
        if not root.is_absolute() or not mount.is_absolute():
            continue
        try:
            relative = group.relative_to(root)
        except ValueError:
            continue  # This bind mount does not expose our cgroup.
        roots.append((mount / relative, mount, v2))

    if not roots:
        # Preserve the root-only probe on hosts without readable proc metadata.
        for directory, v2 in (("/sys/fs/cgroup", True), ("/sys/fs/cgroup/memory", False)):
            path = PurePosixPath(directory)
            roots.append((path, path, v2))
    return roots


def _cgroup_available_gb() -> float:
    """Cgroup memory headroom (GB) from whichever ceiling binds, or -1.0 if none.

    Two cgroups can bound the agents this host runs, and either may be the
    binding one:

    * the **process's own cgroup ancestry** (the container's limit when the
      gateway runs inside a memory-limited container) -- see
      :func:`_container_cgroup_available_gb`;
    * the **agents slice** (``kirocrew-agents.slice``), the aggregate ceiling
      the sandbox itself places on every agent process on a bare Linux host --
      see :func:`_agents_slice_available_gb`.

    The slice is a sibling of the gateway's own cgroup, not an ancestor, so the
    ancestry walk never sees it; and the walk reads hard limits only, never
    ``memory.high``, which is the ceiling the kernel throttles at. Without the
    slice term a bare host with tens of GB free reads as "ample" while the
    kernel is already throttling the whole agent subtree, so admission keeps
    admitting into the throttle. The tighter of the two readings is returned;
    -1.0 only when neither constrains (``dynamic-subagent-sizing.md`` §9).
    """
    readings = [
        gb for gb in (_container_cgroup_available_gb(), _agents_slice_available_gb()) if gb >= 0
    ]
    return min(readings) if readings else -1.0


def _container_cgroup_available_gb() -> float:
    """Tightest visible cgroup headroom (GB), or -1.0 if unlimited/unknown.

    Reads cgroup v2 (``memory.max``/``memory.current``) then v1
    (``memory.limit_in_bytes``/``memory.usage_in_bytes``) at the process's
    cgroup and its visible ancestors. Each limit is paired with usage at the
    SAME level, including siblings charged to a parent. A finite limit with
    unknown usage contributes zero headroom, never zero usage. Ancestors
    hidden above a mount cannot be measured.
    """
    available = -1.0
    for leaf, mount, v2 in _cgroup_memory_roots():
        limit_name = "memory.max" if v2 else "memory.limit_in_bytes"
        usage_name = "memory.current" if v2 else "memory.usage_in_bytes"
        directory = leaf
        while True:
            # Older v1 kernels can disable descendant accounting per group.
            if (
                v2
                or directory == leaf
                or _read_int_file(str(directory / "memory.use_hierarchy")) == 1
            ):
                limit = _read_int_file(str(directory / limit_name))
                current = _read_int_file(str(directory / usage_name))
                if limit is not None and 0 <= limit < _CGROUP_UNLIMITED:
                    # No spare capacity is established when usage is unknown.
                    headroom = (
                        max(0.0, (limit - current) / (1024**3))
                        if current is not None and current >= 0
                        else 0.0
                    )
                    available = headroom if available < 0 else min(available, headroom)
            if directory == mount:
                break
            directory = directory.parent
    return available


def _agents_slice_available_gb() -> float:
    """Headroom (GB) under the agents slice's own ceiling, or -1.0 if none applies.

    The slice carries two ceilings: ``memory.high`` (past it the kernel
    throttles-and-reclaims the whole subtree) and ``memory.max`` (past it the
    kernel OOM-kills a scope). The lower one binds, so headroom is
    ``min(high, max) - current``, floored at zero: usage can sit ABOVE
    ``memory.high`` while the kernel reclaims, and a negative figure would
    mislead every threshold comparison downstream.

    The slice directory comes from ``sandbox._agents_slice_cgroup_dir`` (which
    knows systemd's dash-hierarchy); the files are read through the same
    ``_read_int_file`` as the container probe so ``max`` and an absent file
    both mean "does not constrain". -1.0 when not Linux, the slice is not
    materialized, or neither ceiling is set.
    """
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return -1.0
    ceilings = [
        limit
        for limit in (
            _read_int_file(str(slice_dir / "memory.high")),
            _read_int_file(str(slice_dir / "memory.max")),
        )
        if limit is not None and limit < _CGROUP_UNLIMITED
    ]
    if not ceilings:
        return -1.0
    current = _read_int_file(str(slice_dir / "memory.current")) or 0
    return max(0.0, (min(ceilings) - current) / (1024**3))


def compute_max_subagents(cfg: KiroCrewConfig) -> int:
    """Compute the concurrent sub-agent cap from host memory.

    Memory is the ONLY host resource that sizes the cap: a buffered memory
    budget divided by a per-agent memory cost. CPU is deliberately not a term.
    Over-committing memory ends in the OOM killer, an unrecoverable hard
    failure, so it must be sized up front; over-committing CPU only slows work
    down, and the adaptive controller already backs off on the pressure signals
    that slowness produces (timeouts, slow starts). A static CPU estimate on top
    of that closed loop only ever closed the door early: peak-of-one-minute
    CPU readings from build/test-heavy runs priced every slot at the busiest
    agent's burst and pinned the cap to its starting value on 32-core hosts
    with tens of GB free.

    The result is clamped to ``[3, hard_cap]`` — never below the legacy
    default (the per-spawn ``spawn_min_memory_gb`` gate is the real-time
    memory guard), never above the absolute ``subagent_auto_max`` (which
    stands in for the unmodeled LLM-provider concurrency limit).

    The per-agent memory cost comes from the learned cost store
    (``read_learned_cost``); when no learned value exists yet, the configured
    first-boot fallback (``subagent_cost_gb``) is used. Fails open to the legacy
    default when memory can't be read (e.g. non-Linux hosts).

    See ``dynamic-subagent-sizing.md`` §3.
    """
    agent = cfg.agent
    # Hard floor of 3 (``_LEGACY_DEFAULT_MAX``): the auto-sized cap never drops
    # below today's behavior even if ``subagent_auto_max`` is somehow < 3 (the
    # config loader clamps it up to 3, but defend here too so the runtime cap is
    # guaranteed >= 3). ``subagent_auto_max`` is the upper ceiling.
    hard_cap = max(_LEGACY_DEFAULT_MAX, agent.subagent_auto_max)
    lo = _LEGACY_DEFAULT_MAX

    mem_term = _host_mem_term(cfg)
    if mem_term is None:
        # Memory unreadable (non-Linux / read error) — fail open.
        logger.info(
            "dynamic subagent cap = %d (memory unreadable; fail-open to legacy default)",
            lo,
        )
        return lo

    result = max(lo, min(mem_term, hard_cap))

    # Name the active bound for an explainable startup log (§5.2).
    if mem_term >= hard_cap:
        reason = "hard_cap"
    elif mem_term <= lo:
        reason = "floor"
    else:
        reason = "mem_term"
    logger.info(
        "dynamic subagent cap = %d (%s; mem_term=%d, floor=%d, hard_cap=%d)",
        result,
        reason,
        mem_term,
        lo,
        hard_cap,
    )
    return result


def _host_mem_term(cfg: KiroCrewConfig) -> int | None:
    """How many agents fit in this host's available memory, or None when unreadable.

    THE one place the sizing arithmetic lives, so the auto-sized cap
    (:func:`compute_max_subagents`) and its startup log line can never drift
    apart. It sizes the AUTO ceiling only (``max_subagents=0``); an explicit
    ``max_subagents`` is the ceiling as written, and the adaptive controller
    climbs toward whichever applies on live pressure signals, not on this
    prediction.
    """
    agent = cfg.agent
    avail_gb = _available_memory_gb()
    if avail_gb <= 0:
        return None
    buf = 1.0 - agent.subagent_mem_buffer_pct / 100.0
    mem_cost = read_learned_cost("mem_gb") or agent.subagent_cost_gb or 0.5
    pool_size = cfg.session.pool_size
    return math.floor((avail_gb * buf - pool_size * mem_cost) / mem_cost)


# Sweeps that must have measured a dedicated worker before the guard trusts its
# own reading over the learned per-start price. One reading can land mid-growth
# (a runtime started just before a sweep reads at a fraction of its size); two
# readings an interval apart bound that exposure to one ``_REAPER_INTERVAL``.
_RSS_SAMPLES_TO_SETTLE = 2


def _live_dedicated(agents: list[SubagentInfo]) -> tuple[list[SubagentInfo], list[SubagentInfo]]:
    live = [info for info in agents if not info.done and not info.queued]
    return live, [info for info in live if not info._session_sharing]


def _effective_next_start_gb(
    agents: list[SubagentInfo], *, cost_gb: float, next_start_gb: float | None
) -> float:
    """What the NEXT dedicated start is actually priced at.

    The larger of the configured cost, the caller's learned figure and every
    live dedicated peak: a worker observed above the learned p90 is evidence
    that starts on this host can cost that much. Shared by the reserve and by
    the gate's deferral record, so the price an operator is shown is the price
    the arithmetic used.
    """
    _live, dedicated = _live_dedicated(agents)
    expected = max([0.0, cost_gb, *(info.peak_rss_gb for info in dedicated)])
    return max([expected, next_start_gb if next_start_gb is not None else 0.0])


def _startup_memory_reserve_gb(
    agents: list[SubagentInfo],
    *,
    running_count: int,
    cost_gb: float,
    next_start_gb: float | None = None,
) -> float:
    """Memory promised to cold dedicated starts but not observed in RSS yet.

    Include the next start and claims awaiting registration. Queued and terminal
    rows promise nothing; a yielded parent still owns its process. Confirmed
    shared sessions do not launch another process and incur no dedicated-start
    reservation. Until sharing is known, a row is priced as a warming
    dedicated start, since it may yet become one.

    Two prices, because two kinds of worker are live at once:

    * A start that is not SETTLED yet -- the next one, a claim awaiting
      registration, and a dedicated worker fewer than ``_RSS_SAMPLES_TO_SETTLE``
      sweeps have measured -- owes ``next_start_gb`` (default ``cost_gb``; see
      :func:`_effective_next_start_gb`) less whatever RSS it already holds. A
      single reading can land mid-growth, so one sample does not yet say what
      the worker will weigh; what it holds is subtracted so nothing is counted
      twice, but the remainder stays reserved at the learned figure.
    * A SETTLED dedicated worker owes only the gap between the larger of
      ``cost_gb`` and its OWN peak and what it holds now. Its reservation retires
      as its RSS is observed: a learned p90 far above what this particular
      worker turned out to need must not become a phantom reserve that no later
      sample can close -- and the phantom a warming worker can hold is bounded
      to the sweeps before it settles.
    """
    live, dedicated = _live_dedicated(agents)
    next_start = _effective_next_start_gb(agents, cost_gb=cost_gb, next_start_gb=next_start_gb)
    unregistered = max(0, running_count - sum(not info._slot_released for info in live))
    gaps = 0.0
    for info in dedicated:
        if info._rss_samples < _RSS_SAMPLES_TO_SETTLE:
            gaps += max(0.0, next_start - info.last_rss_gb)
        else:
            gaps += max(0.0, max(cost_gb, info.peak_rss_gb) - info.last_rss_gb)
    return next_start * (1 + unregistered) + gaps


def _cost_bucket(agent: str, execution: Any) -> str:
    """The cost-store key one run's samples are written under and priced from.

    The explicit ``agent`` when the spawn named one, else the template the run
    actually executes (``execution.template_id`` -- an agent-less spawn inherits
    its parent's), so an inherited heavy template builds and reads its OWN
    bucket instead of mixing into the default one. ONE function for the write
    (``_record_cost``) and the read (the spawn guard), because a key that
    differs between the two never converges. Empty when neither is known; the
    store normalizes that to its default agent.
    """
    if agent:
        return agent
    return str(getattr(execution, "template_id", "") or "")


def _startup_cost_gb(agent: Any, learned_gb: float | None) -> float:
    """What one unmeasured dedicated start is priced at by the spawn guard.

    The larger of the configured first-boot fallback (``subagent_cost_gb``) and
    *learned_gb*, this run's own bucket's dedicated p90
    (:func:`~kiro_crew.subagent_cost.read_learned_costs` with ``dedicated_only``,
    refreshed off-loop by the reaper sweep and held on the manager, so this is
    arithmetic only; ``None`` when the bucket has no such history). The
    reserve is the ONLY thing that prices a start between admission and the
    reaper's first RSS sample (60 s), and a dedicated runtime takes tens of
    seconds to reach its resident size, so a burst of starts inside that window
    is bounded by this number alone. Pricing it at the 0.5 GB fallback while the
    store already knew a ~6 GB p90 let four starts each clear a raw free-memory
    check and then grow into the same headroom together.

    ``max`` rather than the cap's learned-over-configured
    (:func:`_host_mem_term`), deliberately: the cap is a COUNT, and a learned
    cost below the configured one should raise it -- that is what learning is
    for -- while the reserve is a safety floor against an unrecoverable OOM, so
    an operator's higher pin must never be lowered by a learned figure.
    Over-reserving here only defers a start until the next sample; under-reserving
    is the failure being guarded against.
    """
    try:
        configured = float(agent.subagent_cost_gb)
    except (AttributeError, TypeError, ValueError):
        configured = 0.5
    try:
        learned = float(learned_gb) if learned_gb is not None else 0.0
    except (TypeError, ValueError):
        learned = 0.0
    return max(configured, learned)


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


def stage_boundary_owner_for_run(info: object) -> str:
    """Return a run's captured owner token; a missing token is unowned."""
    owner = getattr(info, "_stage_boundary_owner", "")
    return owner if isinstance(owner, str) else ""


#: Reap reasons that end a run on purpose. A tombstone written with one of these
#: is a neutral "stopped", never a failure, and the same set decides whether the
#: reap-echo arm in ``_run`` and ``_force_reap``'s own record synthesize an error.
_NEUTRAL_REAP_REASONS = frozenset({"user_stop", "parent_end", "stage_cancel"})


@dataclass
class SubagentInfo:
    """Metadata for a running subagent."""

    id: str
    task: str
    started: float = field(default_factory=time.time)
    done: bool = False
    # True for work accepted behind the stagger/concurrency gate but never
    # started. The record returned by ``spawn`` is normally not registered in
    # ``_agents``; a queued stop registers a synthetic copy temporarily so batch
    # settlement can observe it, and keeps this discriminator True so running
    # cancellation and live-resource monitoring do not treat it as executing.
    queued: bool = False
    # Why a ``queued`` record waits: one of the ``QUEUED_REASON_*`` kinds, or ""
    # for a wait the gate did not label (a claim retained across a store outage).
    # ``queued_reason_detail`` is the gate's own sentence for it -- the same text
    # the task store's ``deferred`` event records -- so ``POST /api/spawn`` can
    # relay it verbatim. Both stay "" on a running or terminal record.
    queued_reason: str = ""
    queued_reason_detail: str = ""
    result: str = ""
    result_path: str = ""
    result_truncated: bool = False  # completion-event copy dropped content → summary+path
    error: str = ""
    # Machine-readable identifier for ``error``, forwarded by ``POST /api/spawn``
    # as the response's ``code`` so a client can switch on the decision instead of
    # parsing the prose. Set today only by the unknown-agent refusal
    # (``AGENT_NOT_FOUND_CODE``), which is the one rejection a client acts on
    # differently: ``spawn_run`` stops re-posting a name the gateway already
    # refused. The other kinds that DO carry an error (bad task, low memory, a cwd
    # refusal, governance) stay un-coded and the handler answers them under a
    # generic code — a code with no consumer would be contract surface bought for
    # nothing. A capacity refusal never reaches this field at all: it returns no
    # record, and the handler answers 429 from that absence.
    error_code: str = ""
    parent_session_key: str = ""
    # Boundary generation captured at admission; paired with
    # ``parent_session_key`` it identifies the exact owning stage boundary.
    # Empty selects legacy-compatible or explicitly unowned routing.
    _stage_boundary_owner: str = field(default="", repr=False)
    # Set synchronously when the owning stage is cancelled. Terminal accounting
    # remains visible, but its completion can never route back into the parent.
    _stage_boundary_cancelled: bool = field(default=False, repr=False)
    memory_mode: str = field(default="persistent", kw_only=True)
    _memory_mode_ready: bool = field(default=True, init=False, repr=False)
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
    # True while blocked on a human approval prompt — either the pre-execution
    # spawn gate or a mid-run tool prompt; exempt from idle-stall. Paired with
    # ``_exec_started is None`` it also tells the reaper which of the two a
    # parked run is sitting on.
    _awaiting_approval: bool = False
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
    delegation: dict[str, str] = field(default_factory=dict)
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
    # WHY the reap in flight is happening, written next to ``_reap_started`` and
    # read by the run loop when its stream dies UNDER that reap. ``_force_reap``
    # tears the run's session down before it cancels the task, so the in-flight
    # turn observes its own runtime being killed first and raises
    # ``AcpProcessDied`` -- "killed (provider shutdown)". Without these two fields
    # that echo was recorded as the run's failure: a ``cause="error"`` tombstone
    # carrying the death text and an ERROR log, for a run a user had just pressed
    # Stop on (or a parent end had cancelled). ``_reap_reason`` is the tombstone
    # cause the reap itself would write (``user_stop`` / ``parent_end`` /
    # ``reaped`` / ``startup_timeout``); ``_stop_origin`` is the one-line WHO/WHY
    # for the record and the log ("stopped by user", "parent conversation ended
    # (retire_kiro_identity_sessions)", "reaped after 900s (reaped)").
    _reap_reason: str = ""
    _stop_origin: str = ""

    @property
    def stop_is_neutral(self) -> bool:
        """Whether the reap that owns this run is a deliberate stop, not a failure.

        Decided by the FIRST stopper (``_reap_reason``), never by
        ``user_stopped`` alone: a Stop that lands while a deadline reap is already
        tearing the run down sets ``user_stopped`` too, and reading that flag would
        let the late Stop convert a claimed deadline failure into a neutral stop.
        A user Stop, a parent end and a stage cancel are neutral; a deadline or
        startup reap is the run's own failure.
        """
        return self._reap_reason in _NEUTRAL_REAP_REASONS

    # Set by the gateway on the wave's FINAL member only: the held OK member
    # ids whose delivery tombstones must be settled once the digest has been
    # successfully handed off (i.e. after _on_done returns without raising —
    # the same contract as the per-agent mark_delivered). Settling these at
    # digest COMPOSITION would re-open the restart-loss window between
    # composing and routing.
    _digest_settle_ids: list[str] = field(default_factory=list)
    # True when the gateway QUEUED this completion's injection because the
    # parent's slot was busy. Delivery is not consumption: the announce sits in
    # the slot queue until a turn drains it, and that wait is bounded only by the
    # turn ceiling — far longer than agent.subagent_result_ttl_secs. The run loop
    # must therefore SKIP mark_delivered() (a "delivered" tombstone starts the
    # retention clock, so the reaper would prune result.txt while the promise of
    # it is still queued, and the parent would be handed a dead path). The drain
    # settles the tombstone instead — see
    # ``_ChatSlot.take_pending_subagent_deliveries``.
    _delivery_queued: bool = False
    max_turns: int = 0
    reaped: bool = False
    streaming_text: str = ""
    elapsed: float = 0.0
    _raw_task: str = ""  # unredacted task for kiro-cli execution prompt
    # CC-specific overrides (ignored for ACP)
    model: str = ""
    # The model id the live session ACTUALLY resolved to serve, read back from
    # the provider's public ``served_model`` accessor. Distinct from ``model``,
    # which is only the REQUESTED pin (often "" ⇒ provider default): the ACP
    # backend reports the served id even on the default, and a routing/config/
    # availability downgrade makes the two differ. "" means unknown/inconclusive
    # (never a wildcard) — the ACP session/new response fills it at spawn, while
    # the CC/raw path only knows it after the first turn, so it is refreshed at
    # completion too. Surfaced on the subagent WS frames and completion meta so a
    # model-pinned review's actual model is auditable.
    resolved_model: str = ""
    # The EFFECTIVE requested model — the per-spawn pin (``model``) OR, when that
    # is empty, the ``agent.role_models['subagent']`` config pin
    # (docs/system-specs/common/model-selection.md names
    # the config pin as *the* way to pin a subagent model). This is the side the
    # downgrade comparison must use: a config-pinned run served a different model
    # is exactly the "unverifiable pin" this feature exists to catch, and keying
    # off the bare per-spawn ``model`` would miss it.
    # ``"auto"`` ⇒ unpinned (no per-spawn pin, no role pin — the provider picks
    # the model). Resolved once at spawn.
    requested_model: str = ""
    # Per-call reasoning-effort override (spawn_run ``reasoning_effort``).
    # Wins over the ``role_efforts['subagent']`` pin; ``""`` defers to it.
    # Like ``model``, a non-empty value forces the dedicated-process path.
    reasoning_effort: str = ""
    # Per-spawn ACP backend override (spawn_run ``backend``). ``None`` means NO
    # override: the child runs on the configured default backend exactly as it
    # did before per-spawn selection existed. ``""`` is kiro-cli's own id and is
    # a REAL override -- it pins the child to Kiro even under a non-Kiro default.
    # Neither inherits the parent CHAT's per-chat pin -- a parent that wants its
    # child on a specific provider names it here. Validated selectable at
    # admission (gate.py). Like ``model``/``reasoning_effort`` an override
    # (any string) forces the dedicated-process path: the parent's shared
    # runtime runs on its own backend and cannot switch per session, so the
    # override reaches the provider factory only on a fresh process. Delivered
    # to the factory as ``backend_override`` in ``_run_inner``'s ``extra_kwargs``.
    acp_backend: str | None = None
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
    # The memory silo this child reads and writes, or "" for the global store.
    # Carried rather than derived: `agent` above holds a kiro-cli modeId, a
    # namespace disjoint from cfg.agents, so resolving a store from it answers
    # `default` for exactly the crew that configured otherwise — silently, and
    # toward the operator's own memory. The caller passes a name it already
    # resolved (ResolvedBindings.memory_store_name) or nothing at all.
    #
    # Empty is the correct default for a plain spawn: a child that inherits no
    # crew reads the global store, which is what every spawn did before crews
    # had silos.
    memory_store: str = ""
    execution_context: ExecutionContext | None = field(default=None, kw_only=True)
    # A named member remains the conversation owner during a template override.
    crew: str = field(default="", kw_only=True)
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
    _first_stream_started: float | None = None
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
    # How many reaper sweeps have measured a non-zero RSS for this run. The
    # spawn guard treats a dedicated worker as still WARMING until it has been
    # seen by two sweeps (an interval apart), so a single reading taken
    # mid-growth is not mistaken for the worker's size (see
    # _startup_memory_reserve_gb).
    _rss_samples: int = 0
    # Bumped when the run gets a NEW process (the cancel-recovery respawn), so
    # an off-loop sweep that read the old process cannot land its reading on
    # the new one: the sweep snapshots this before reading and writes only if
    # it is unchanged.
    _rss_generation: int = 0
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
    # True while a cancelled run is draining an in-flight off-loop state.json
    # write worker (every off-loop writer). _run's
    # unexpected-cancel recovery gate reads it: on Python 3.10 a second outer
    # cancel can deliver that gate BEFORE the drain finishes (wait_for's
    # _cancel_and_wait awaits an interruptible bare future), and scheduling a
    # recovery writer while the worker is live re-opens the stale-overwrite race
    # the drain exists to close.
    _state_drain_active: bool = False
    # Set only on the synthetic marker `_conversation_busy` returns for a
    # conversation held by an abandoned state writer, so the two
    # retention callers can say "still settling a state write" instead of
    # promising a completion event that has already fired. The authoritative
    # record is `SubagentManager._abandoned_state_writers`, which survives
    # `evict_completed_agents` pruning a completed run out of `_agents`.
    _state_writer_abandoned: bool = False
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
    # Generation the durable task row was claimed under (``kiro_crew.taskq``).
    # Every store write the run makes carries it, so a late write from a
    # superseded dispatch of the same id is fenced out. 0 = no store row.
    _taskq_generation: int = 0
    # Scheduler-core fields (session-start gate, lane-slot waits; see
    # docs/system-specs/modules/subagent.md § Lane-slot waits).
    # Queue wait behind the session-start gate, ms; 0 when the gate was free.
    _start_queue_wait_ms: float = 0.0
    # True once the durable row was written ``running`` -- at the FIRST stream
    # event of the run's own turn, not at execution start, so a row is never
    # ``running`` while the session is still being created (RFC §4.4).
    _taskq_running_marked: bool = False
    # Provider built by a StartCollector's late adoption, handed to the run
    # that was waiting for it; None otherwise.
    _late_start_provider: Any = None
    # The live WaitRecord (as a dict) while this run has yielded its lane
    # slot for a wait; None while it holds a slot or has none to hold.
    _wait_record: Any = None
    # Set when the run's lane slot was yielded for a wait and a resume entry
    # is queued in admission; cleared when the slot is granted back.
    _resume_pending: bool = False
    # The run loop's wake-up for a yielded slot: set by ``resume_grant`` when
    # the pump hands the slot back, or by the dependency coordinator's
    # ``on_fail`` when the wait ended in failure (``_wait_failed`` names why).
    # None while the run holds its slot.
    _resume_event: Any = None
    _wait_failed: str = ""
    # True once the terminal report's `_on_done` injection has RETURNED, i.e.
    # the outcome actually reached the parent. Distinct from `_finalized` (the
    # claim, taken before delivery is attempted) and from the "delivered"
    # tombstone (written later, after teardown). Read by `cancel_all()` to tell
    # a report cancelled BEFORE delivery — which must be made recoverable on the
    # next start — from one cancelled AFTER it, which must not be re-delivered.
    _reported_to_parent: bool = False
    # One bounded-latch debt for this completion; cleared only after redelivery.
    _report_failure_latched: bool = field(default=False, init=False, repr=False)
    # The run's final ACP ``stop_reason`` and its ``classify_stop_reason``
    # class (a ``STOP_CLASS_*`` value), recorded by ``_run_inner`` on the
    # completion that ended the run
    # and carried on the ``subagent_done`` event so the parent sees WHY the run
    # ended, not only whether ``error`` is set. Empty until the run completes.
    stop_reason: str = ""
    stop_class: str = ""
    # True when the delivered ``result`` is a PARTIAL: text streamed before a
    # non-success completion (stall, cancel, error). The parent must not read
    # it as a finished answer.
    partial: bool = False
    # Continue-nudges already spent recovering a ``stalled`` / ``recovering``
    # completion in place (``STOP_RECOVERY_MAX_RETRIES`` budget, shared with
    # the main chat's ``slot._tool_stall_retries``).
    _stop_recovery_used: int = 0

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


def _injection_notice_outcome(info: "SubagentInfo") -> str:
    """One-sentence outcome line for the injection-failure fallback notice.

    ``notify_injection_failed`` fires whenever a terminal report could not be
    injected into the parent — for EVERY terminal state, not just successful
    completion. Asserting "finished" for a run that was stopped or rejected
    before it executed misdescribes the outcome, so the line branches on the
    record's canonical :attr:`SubagentInfo.outcome` with one before-start
    refinement per branch: ``_exec_started`` — the marker ``_run_inner`` sets
    when execution actually begins — is ``None`` exactly when the run never
    executed, which covers every spawn-rejection site (all of them construct
    their record without it) with no wording contract between ``error``
    strings and this notice. The "no result to deliver" phrasings are guarded
    on the absence of any output so they can never contradict the result-path
    recovery hint. Pure function of the record, unit-tested per branch.
    """
    never_ran = info._exec_started is None and not info.result and not info.result_path
    outcome = info.outcome
    if outcome == "stopped":
        if never_ran:
            return "The run was stopped before it started, so there is no result to deliver."
        return "The run was stopped before it completed."
    if outcome == "failed":
        if never_ran:
            return "The run failed before it started, so there is no result to deliver."
        return "The agent failed before a result could be delivered."
    return "The agent finished but result delivery timed out."


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


def _truncate_report_failure_text(text: str) -> str:
    """Fit retained report text within its UTF-8 byte budget, marker included."""
    encoded = text.encode("utf-8")
    if len(encoded) <= _REPORT_FAILURE_PAYLOAD_MAX_BYTES:
        return text
    omitted = len(encoded)
    prefix = ""
    marker = ""
    for _ in range(8):
        marker = f"\n[truncated {omitted} bytes]"
        budget = max(0, _REPORT_FAILURE_PAYLOAD_MAX_BYTES - len(marker.encode("utf-8")))
        prefix = encoded[:budget].decode("utf-8", errors="ignore")
        next_omitted = len(encoded) - len(prefix.encode("utf-8"))
        if next_omitted == omitted:
            break
        omitted = next_omitted
    return prefix + marker


@dataclass(frozen=True, slots=True)
class _ReportFailureSnapshot:
    """Compact boundary-owned data sufficient to retry one terminal report."""

    id: str
    parent_session_key: str
    _stage_boundary_owner: str
    _stage_boundary_cancelled: bool
    task: str
    started: float
    result: str
    result_path: str
    result_truncated: bool
    error: str
    elapsed: float
    user_stopped: bool
    _stop_origin: str
    outcome: str
    partial: bool
    agent: str
    silent: bool
    conversation_key: str
    model: str
    requested_model: str
    resolved_model: str
    stop_reason: str
    stop_class: str
    batch_id: str
    batch_total: int
    _digest_held: bool
    _digest_flush_only: bool
    _digest_settle_ids: tuple[str, ...]
    _delivery_queued: bool

    @classmethod
    def capture(cls, info: SubagentInfo) -> "_ReportFailureSnapshot":
        bounded = _truncate_report_failure_text
        return cls(
            id=info.id,
            parent_session_key=info.parent_session_key,
            _stage_boundary_owner=stage_boundary_owner_for_run(info),
            _stage_boundary_cancelled=bool(info._stage_boundary_cancelled),
            task=bounded(info.task),
            started=float(info.started),
            result=bounded(info.result),
            result_path=info.result_path,
            result_truncated=bool(info.result_truncated),
            error=bounded(info.error),
            elapsed=float(info.elapsed),
            user_stopped=bool(info.user_stopped),
            _stop_origin=bounded(info._stop_origin),
            outcome=info.outcome,
            partial=bool(info.partial),
            agent=bounded(info.agent),
            silent=bool(info.silent),
            conversation_key=bounded(info.conversation_key),
            model=bounded(info.model),
            requested_model=bounded(info.requested_model),
            resolved_model=bounded(info.resolved_model),
            stop_reason=bounded(info.stop_reason),
            stop_class=bounded(info.stop_class),
            batch_id=info.batch_id,
            batch_total=int(info.batch_total),
            _digest_held=bool(info._digest_held),
            _digest_flush_only=bool(info._digest_flush_only),
            _digest_settle_ids=tuple(str(agent_id) for agent_id in info._digest_settle_ids),
            _delivery_queued=bool(info._delivery_queued),
        )

    @property
    def retained_bytes(self) -> int:
        """UTF-8 payload bytes this compact snapshot retains."""
        text = (
            self.id,
            self.parent_session_key,
            self._stage_boundary_owner,
            self.task,
            self.result,
            self.result_path,
            self.error,
            self._stop_origin,
            self.outcome,
            self.agent,
            self.conversation_key,
            self.model,
            self.requested_model,
            self.resolved_model,
            self.stop_reason,
            self.stop_class,
            self.batch_id,
            *self._digest_settle_ids,
        )
        return sum(len(value.encode("utf-8")) for value in text)

    def delivery_info(self) -> SubagentInfo:
        info = SubagentInfo(
            id=self.id,
            task=self.task,
            started=self.started,
            done=True,
            result=self.result,
            result_path=self.result_path,
            result_truncated=self.result_truncated,
            error=self.error,
            parent_session_key=self.parent_session_key,
            _stage_boundary_owner=self._stage_boundary_owner,
            _stage_boundary_cancelled=self._stage_boundary_cancelled,
            agent=self.agent,
            silent=self.silent,
            batch_id=self.batch_id,
            batch_total=self.batch_total,
            _digest_held=self._digest_held,
            _digest_flush_only=self._digest_flush_only,
            _digest_settle_ids=list(self._digest_settle_ids),
            _delivery_queued=self._delivery_queued,
            elapsed=self.elapsed,
            model=self.model,
            resolved_model=self.resolved_model,
            requested_model=self.requested_model,
            conversation_key=self.conversation_key,
            user_stopped=self.user_stopped,
            _stop_origin=self._stop_origin,
            stop_reason=self.stop_reason,
            stop_class=self.stop_class,
            partial=self.partial,
        )
        info._report_failure_latched = True
        return info


class SubagentReportDeliveryError(RuntimeError):
    """One or more registered terminal reports failed before delivery."""


class ToolApprovalCallback(Protocol):
    async def __call__(self, event: LLMEvent, parent_session_key: str = "") -> bool:
        pass


class SpawnApprovalUnreachable(Exception):
    """A spawn-approval prompt has no surface that could ever answer it.

    Raised BY a :class:`SpawnApprovalCallback`, at the point it would otherwise
    park, and handled by the spawn gate in ``subagent_manager/admission/gate.py``.

    Why an exception rather than a ``False`` return, and why the callback rather
    than the gate decides:

    * ``False`` already means "a human refused", and the two must not collapse:
      a refusal is a decision, this is the absence of anyone who could decide.
      They want different prose, and only this one is a misconfiguration.
    * The gate cannot compute the answer. Every non-human auto-approve shortcut
      the callback owns -- ``hooks.auto_approve_sources``, the CLI ``--approval``
      mode, the YOLO override, slot trust -- is evaluated inside the callback and
      never reaches the gate's cascade, so a gate-side probe would have to
      re-derive all four and would reject spawns those rungs mean to allow (the
      ``auto_approve_sources`` opt-in is the documented workaround). Raising from
      the callback puts the check where "we are about
      to park with nobody attached" is the only remaining possibility.

    The message SHOULD name the surface that was missing ("no dashboard client is
    connected"), because the raiser is the only party that knows what the
    surfaces are. The gate quotes it and adds the config rungs, which are the
    gate's own; that split is what keeps the gate's prose from going stale when
    channel-side delivery lands.

    A callback that never raises it keeps today's behaviour unchanged.
    """


class SpawnApprovalCallback(Protocol):
    async def __call__(
        self, request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        pass


#: Hard ceiling on :attr:`SubagentManager._completion_waiters`. Entries are
#: created only by an explicit :meth:`SubagentManager.completion_event` call and
#: removed by its release, so this is a leak fuse rather than a working limit.
_MAX_COMPLETION_WAITERS = 64


# ── Delivery routing state: enumerated from the PRODUCING side ────────────────
#
# Every ``SubagentInfo`` attribute that the four modules owning terminal-outcome
# routing WRITE -- ``subagent_manager/terminal.py``, ``subagent_manager/waves.py``,
# ``subagent_manager/cancellation.py`` and ``slack/gateway.py`` -- classified by what it
# says about whether the outcome has reached the parent.
#
# The list exists because "has this run's outcome reached its parent" has more than one
# representation, and reading only the obvious one was wrong four separate times. A
# parent-end teardown has to suppress the delivery of a run whose parent is gone, so a
# representation it does not know about is a delivery that lands in a conversation that
# ended -- and the injector CREATES a session when none is live, so that delivery rebuilds
# the conversation the teardown just took down.
#
# ``PARKS_WHEN_SET``  -- truthy means the outcome is parked somewhere and has not landed.
# ``PARKS_WHEN_UNSET`` -- falsy means it has not landed; truthy means it has.
# ``NOT_DELIVERY_STATE`` -- written by those modules but says nothing about delivery.
#
# ``test_the_delivery_parked_states_are_enumerated_from_the_producers`` recomputes the
# write set from those modules' AST and fails when it stops matching this table, so a new
# field written by any of them cannot be added without being classified here.
PARKS_WHEN_SET = "parks-when-set"
PARKS_WHEN_UNSET = "parks-when-unset"
NOT_DELIVERY_STATE = "not-delivery-state"

DELIVERY_ROUTING_FIELDS: "dict[str, str]" = {
    # The gateway parked this member's per-agent injection for the wave digest. Two
    # fields on purpose: the flag is the restart-safety contract the run loop reads, the
    # timestamp is the hold-deadline sweep's only input, and the sweep must not mutate the
    # flag. Either being set means the result is not in the parent's context.
    "_digest_held": PARKS_WHEN_SET,
    "_digest_held_at": PARKS_WHEN_SET,
    # The held SIBLINGS whose delivery tombstones this member owes once its digest is
    # handed off. Non-empty means other runs' deliveries are parked ON this record.
    "_digest_settle_ids": PARKS_WHEN_SET,
    # The announce sits in the parent's slot queue because the slot was busy. Delivery is
    # not consumption: a turn has to drain it.
    "_delivery_queued": PARKS_WHEN_SET,
    # Boundary cancellation revokes this owner's authority before durable settlement.
    # Truthy therefore means its outcome must not reach the parent, which is the same
    # parked answer the parent-end delivery gate needs.
    "_stage_boundary_cancelled": PARKS_WHEN_SET,
    # Set the moment ``_on_done`` RETURNS. Its truth is the only positive evidence the
    # outcome reached the parent -- which is why it reads the other way round, and why
    # reading it ALONE was wrong: two routes above return having merely parked the work.
    "_reported_to_parent": PARKS_WHEN_UNSET,
    # A synthetic record ``force_digest_flush`` builds to release an expired hold. It is a
    # CARRIER of a future injection rather than a member with a parked outcome, and it
    # carries a fresh id, so an id-keyed gate can never recognise it -- which is why the
    # wave hold is disarmed at its source (``_expired_digest_holds``) instead.
    "_digest_flush_only": NOT_DELIVERY_STATE,
    # Run bookkeeping these modules also write. None of them says where an outcome is.
    "_finalized": NOT_DELIVERY_STATE,
    "_reap_reason": NOT_DELIVERY_STATE,
    "_reap_started": NOT_DELIVERY_STATE,
    "_recovering": NOT_DELIVERY_STATE,
    # The recovery respawn resets the dead process's RSS readings so the spawn
    # guard prices the fresh process as warming; memory sizing, not delivery.
    "_rss_generation": NOT_DELIVERY_STATE,
    "_rss_samples": NOT_DELIVERY_STATE,
    "_slot_released": NOT_DELIVERY_STATE,
    "_stop_origin": NOT_DELIVERY_STATE,
    "done": NOT_DELIVERY_STATE,
    "elapsed": NOT_DELIVERY_STATE,
    "error": NOT_DELIVERY_STATE,
    "last_rss_gb": NOT_DELIVERY_STATE,
    # Owned continuation work is cancelled separately when a parent ends; its presence
    # says nothing about whether this run's terminal outcome reached that parent.
    "pending_followups": NOT_DELIVERY_STATE,
    "reaped": NOT_DELIVERY_STATE,
    "result": NOT_DELIVERY_STATE,
    "streaming_text": NOT_DELIVERY_STATE,
    "user_stopped": NOT_DELIVERY_STATE,
}

# The modules the table is derived from. Named here so the test and the table cannot
# disagree about which producers were read.
DELIVERY_ROUTING_MODULES: tuple[str, ...] = (
    "subagent_manager/terminal.py",
    "subagent_manager/waves.py",
    "subagent_manager/cancellation.py",
    "slack/gateway.py",
)


def delivery_is_parked(info: "SubagentInfo") -> bool:
    """True when this run's outcome has not reached its parent.

    Reads :data:`DELIVERY_ROUTING_FIELDS` rather than naming fields inline, so the
    predicate and the classification cannot drift -- the drift is what let a parked
    representation through on four separate rounds.

    The union is deliberately conservative. Answering True for a run whose delivery has in
    fact landed costs nothing: the gate only SKIPS an injection, and a run that already
    delivered does not inject again. Answering False for a parked one rebuilds a retired
    conversation.
    """
    for field_name, rule in DELIVERY_ROUTING_FIELDS.items():
        value = getattr(info, field_name, None)
        if rule == PARKS_WHEN_SET and value:
            return True
        if rule == PARKS_WHEN_UNSET and not value:
            return True
    return False


def _audit_ids(ids: "Iterable[str]", cap: int = 20) -> str:
    """Render run ids for the parent-end audit line, bounded.

    Declared on the facade rather than in the component that logs, because a component
    method's module-level names resolve against THIS module's globals at runtime -- a
    helper defined beside its caller raises ``NameError`` there.

    A wave can carry more ids than one log line should hold, and a silently truncated list
    is worse than a count: it reads as the whole set.
    """
    listed = list(ids)
    if not listed:
        return "none"
    if len(listed) <= cap:
        return ",".join(listed)
    return ",".join(listed[:cap]) + f",+{len(listed) - cap}-more"


# How long the delivery gate remembers a teardown-cancelled run id when nothing has
# explicitly discarded it.
#
# A BACKSTOP, not the primary rule. The primary rule is that the gate keeps an id until
# that run's delivery has actually been suppressed, which is what ``_report_terminal_impl``
# discards on -- so the ordinary case never depends on this number. It exists for a marked
# run that never reaches a terminal at all.
#
# A day rather than an hour, because an approval-parked run is deliberately NOT cancelled
# (the approval is a person's decision to make) and a person can take far longer than an
# hour to answer. An hour let a later teardown prune the mark while such a run was still
# waiting, and its completion then injected into whatever the key served by then.
_TEARDOWN_GATE_TTL_SECS = 86400.0


class _AgingIdSet:
    """A membership set of run ids that forgets an entry once it is OLD, never when full.

    Age since MARKING is the only eviction rule, and the reason is that the alternative
    is unsafe. A CAPACITY rule evicts by arrival order regardless of whether the run
    could still announce, so a single parent with more queued children than the capacity
    would evict its own earliest ids while its reports were still being spawned -- and
    those reports then walk through the gate and rebuild the conversation the teardown
    took down. An age rule cannot do that: the TTL is chosen to exceed every window in
    which a marked run has an announce left.

    Age is also why the lifetime is not tied to the run's ``_agents`` record. That record
    is popped while a run is still tearing down (a dashboard "clear completed" does it),
    so discarding on the pop would disarm the gate while the run can still announce --
    the same reason ``_teardown_gates`` outlives those records.

    Not an LRU: a read must not extend an entry's life, or a hot gate check on one id
    would keep others alive past the point the TTL is reasoned about.
    """

    __slots__ = ("_marked_at", "_ttl")

    def __init__(self, ttl_secs: float) -> None:
        self._marked_at: dict[str, float] = {}
        self._ttl = max(1.0, float(ttl_secs))

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        if not self._marked_at:
            return
        # Insertion-ordered, and marking times are monotonic, so the expired entries are
        # a PREFIX: stop at the first live one instead of scanning the whole dict.
        for agent_id, marked_at in list(self._marked_at.items()):
            if marked_at > cutoff:
                break
            del self._marked_at[agent_id]

    def add(self, agent_id: str) -> None:
        if not agent_id:
            return
        now = time.monotonic()
        self._prune(now)
        self._marked_at.pop(agent_id, None)
        self._marked_at[agent_id] = now

    def update(self, agent_ids: "Iterable[str]") -> None:
        for agent_id in agent_ids:
            self.add(agent_id)

    def discard(self, agent_id: str) -> None:
        self._marked_at.pop(agent_id, None)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._marked_at

    def __iter__(self) -> "Iterator[str]":
        return iter(tuple(self._marked_at))

    def __len__(self) -> int:
        return len(self._marked_at)


class SubagentManager:
    """Spawn and track isolated background agents."""

    _COMPONENT_TYPES = {
        "_monitor": OrphanStallMonitor,
        "_terminal": TerminalCoordinator,
        "_admission": SpawnAdmissionCoordinator,
        "_continuation": ContinuationCoordinator,
        "_waves": WaveDigestCoordinator,
        "_run_events": RunEventCoordinator,
        "_cancellation": CancellationCoordinator,
    }
    _reconcile_task: asyncio.Task | None  # type: ignore[type-arg]

    def __getattr__(self, name: str) -> Any:
        """Lazily compose a missing coordinator for minimal facade construction."""
        component_type = self._COMPONENT_TYPES.get(name)
        if component_type is None:
            raise AttributeError(name)
        component = component_type(self)
        object.__setattr__(self, name, component)
        return component

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
        memory_mode_for_session: Callable[[str], str] | None = None,
        defer_queue_dispatch: bool = False,
        stage_boundary_for_scope: Callable[[str, str], object | None] | None = None,
    ):
        self._sessions = sessions
        # Run ids a parent-end teardown stopped. Keyed by ID rather than carried
        # only on the run record because a QUEUED run has no ``_agents`` row at
        # all: ``_report_queued_stop`` builds a fresh ``SubagentInfo`` for its
        # synthetic terminal, which would default the flag to False and walk
        # straight through the delivery gate. The gate reads this set, so live
        # runs, queued runs and follow-up synthetics are all covered by the one
        # place the teardown writes.
        self._teardown_cancelled_ids = _AgingIdSet(_TEARDOWN_GATE_TTL_SECS)
        self._memory_mode_for_session = memory_mode_for_session
        self._stage_boundary_for_scope = stage_boundary_for_scope
        self._ctx_builder = ctx_builder
        self._on_done = on_done
        #: While True the staggered pump admits nothing: the durable rows that
        #: survived a restart wait for :meth:`release_queue_dispatch`. The
        #: gateway sets it so the boot drain (``start_reaper`` /
        #: ``_initialize_taskq``) cannot claim a row during the memory barrier;
        #: a manager built without it (tests, tools) pumps as soon as it can.
        self._queue_dispatch_held = bool(defer_queue_dispatch)
        #: Set by the pump the first time it refuses a pass under the hold, so
        #: a hold that is never released leaves one debug line behind instead
        #: of the silent "accepted, never claimed" queue this fix diagnoses.
        self._queue_dispatch_hold_logged = False
        # ``_max_concurrent`` is the EFFECTIVE cap every admission read site
        # consults: ``min(user cap, adaptive cap)``. The user's resolved cap
        # (``agent.max_subagents`` / auto-size) is the ceiling in
        # ``_user_max_concurrent``; the adaptive controller lowers the runtime
        # value through :meth:`set_effective_cap` and never writes the ceiling.
        self._user_max_concurrent = max_concurrent
        self._adaptive_cap: int | None = None
        self._max_concurrent = max_concurrent
        #: Gates OUTSIDE this manager that are bounded by ``_max_concurrent``
        #: and cannot see it change (the runner lane -- TaskRunner steps and
        #: workflow ``ctx.agent()`` calls). Registered by the gateway through
        #: :meth:`set_cap_raise_listener`; None everywhere else.
        self._cap_raise_listener: Callable[[], object] | None = None
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
        # The learned per-run memory p90s (``read_learned_costs("mem_gb")``,
        # keyed by cost bucket) the spawn guard prices a warming start from
        # (``learned_cost_for`` → ``_startup_cost_gb``; a bucket with no
        # dedicated history answers None and the configured cost plus live
        # peaks prices it). Empty until the first off-loop
        # refresh: the reaper sweep reads the cost log on the maintenance
        # executor and publishes here, so the gate -- which runs on the event
        # loop -- never opens the file itself. Stale by at most one sweep, far
        # below the rate a 50-sample p90 can move at.
        self._learned_costs_gb: dict[str, float] = {}
        # The log identity the map was last merged from (``cost_log_identity``),
        # so a replaced log -- new inode or shrunk -- is read fresh, not merged.
        self._learned_costs_source: tuple[object, ...] | None = None
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
        # Run id -> parent session for each live watcher. Kept separately from
        # `_agents` because completed-record eviction can remove the run while
        # its watcher still owns a follow-up dispatch.
        self._followup_watcher_parents: dict[str, str] = {}
        # Run id -> the exact record captured by each watcher. Completed-record
        # eviction may remove it from ``_agents`` while queued follow-ups still
        # own a continuation, so stage cancellation needs this independent index.
        self._followup_watcher_infos: dict[str, SubagentInfo] = {}
        # task -> the agent whose terminal report it is delivering
        self._report_owners: dict[asyncio.Task, SubagentInfo] = {}  # type: ignore[type-arg]
        # Boundary-scoped failed terminal payloads outlive completed report
        # tasks. Compact snapshot bytes share one process-wide budget; refusal
        # state lives on the exact live StageBoundary resolved by the callback.
        self._boundary_report_payloads: dict[
            tuple[str, str],
            dict[str, _ReportFailureSnapshot],
        ] = {}
        self._retained_report_failure_bytes = 0
        # Exact stage scopes whose durable queued rows are being cancelled.
        # Membership is cancellation authority: the pump refuses matching rows
        # until the store confirms every queued cancel, including across a
        # transient store outage. Values carry a bounded latest failure for the
        # dashboard halt notice; an empty value means the first write is live.
        # A full map refuses the extra live boundary instead of retaining it.
        self._pending_boundary_cancellations: dict[tuple[str, str], str] = {}
        self._boundary_cancellation_overflow_count = 0
        self._boundary_cancel_retry_handle: asyncio.TimerHandle | None = None
        # A post-claim store outage cannot return an ADMITTED row to the ordinary
        # refill, which reads only claimable rows. Keep that generation, its
        # reserved slot, and its re-entry callback until a later pump pass can
        # revalidate it. One entry requires one already-reserved slot, so this map
        # is bounded by the effective concurrency cap for the process lifetime.
        self._retained_claims: dict[
            str,
            tuple[
                ClaimPoint,
                int,
                Callable[[tuple[int, bool, str]], Any],
                dict[str, Any],
            ],
        ] = {}
        self._retained_claim_retry_handle: asyncio.TimerHandle | None = None
        self._last_spawn_ts: float = 0.0  # monotonic time of the last actual start (stagger gate)
        self.hook_store: Any = None  # Optional ScriptHookStore, set by server.py
        self._agents: dict[str, SubagentInfo] = {}
        # Continuable conversations: session_key ("subagent:<conv-id>") →
        # last-used unix ts. Drives the reaper's idle-TTL sweep. Rebuilt from
        # state.json (keep=True runs) on the reaper's first pass after a
        # gateway restart, so promoted conversations stay owned by
        # the TTL sweep across restarts; a spawn_continue on an unknown key
        # also re-registers it on demand.
        self._conversations: dict[str, float] = {}
        self._conv_registry_rebuilt = False
        # Run ids whose bounded state-write drain EXPIRED, so a pool worker is
        # still live and its stale whole-file rewrite would roll back the
        # retention `keep` a promote / release writes on the loop.
        # `_conversation_busy` reports these as held, which defers both retention
        # writes past the worker; each worker's own done-callback discards its id,
        # so the set holds at most one entry per live zombie. It lives on the
        # MANAGER, not on the run's SubagentInfo, because `evict_completed_agents`
        # prunes completed runs out of `_agents` and an eviction must not silently
        # release the hold.
        self._abandoned_state_writers: set[str] = set()
        # state.json is the source of truth for retention: give the
        # SessionManager's in-memory continuable cache a disk fallback so a
        # cache miss (restart window) cannot demote a promoted conversation.
        try:
            self._sessions.set_continuable_fallback(self._keep_recorded_on_disk)
        except AttributeError:
            pass  # test doubles without the setter
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # Teardown gates for runs whose terminal report has started, keyed by id and
        # OUTLIVING both dicts above. A "delivered" tombstone excludes a folder from
        # restart orphan reconciliation, so it must never be written while the run's
        # child is still being killed -- and the settlement that writes it can happen
        # outside the run (the parent's queue drain), long after a
        # dashboard "clear completed" / "cancel" has popped BOTH ``_agents`` and
        # ``_tasks`` for a done-but-still-tearing-down run. Reading the gate from
        # here, rather than inferring "record gone means teardown finished", is what
        # makes that inference unnecessary. Removed by the same ``finally`` that sets
        # the event, so a missing entry always means "nothing left to wait for".
        self._teardown_gates: dict[str, asyncio.Event] = {}
        #: parent session key -> event pulsed whenever one of its runs reaches a
        #: terminal report. Created on demand by :meth:`completion_event` and
        #: dropped by :meth:`release_completion_event`, so the only entries are
        #: the ones a waiter asked for (today: the autopilot stage loop).
        self._completion_waiters: dict[str, asyncio.Event] = {}
        # Queued spawns store the FULL spawn() kwarg set (not just a 5-tuple), so a
        # drained spawn preserves approval_mode / silent / model / allowed_tools / bare —
        # dropping them made a queued headless/auto spawn hit the deny-by-default gate and
        # a queued silent spawn emit output. See _drain_queue.
        self._queue: list[dict[str, Any]] = []
        # parent_session_key -> the last wait the gate labelled for that parent
        # (``{"reason": <QUEUED_REASON_*>, "available_gb"?, "required_gb"?}``).
        # ``_emit_queue_depth`` attaches it to every ``subagent_queued`` it sends
        # while the parent still has rows waiting, and forgets it at depth 0: the
        # drain and the cancel paths re-emit the depth without a verdict of their
        # own, and without this memory each re-emit would flip a memory-deferred
        # wave back to the default (concurrency) text.
        self._queue_wait: dict[str, dict[str, Any]] = {}
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
        # Spawn stagger interval — serializes cold starts so a high cap fills as
        # a ramp rather than a burst (dynamic-subagent-sizing.md §5.3). It is a
        # smoothing interval, not the memory guard: every spawn still clears
        # ``spawn_min_memory_gb`` and the host budget, and the adaptive
        # controller cuts the cap on real pressure.
        try:
            self._spawn_stagger_secs = max(
                0.0, float(KiroCrewConfig.load().agent.subagent_spawn_stagger_secs)
            )
        except Exception:
            self._spawn_stagger_secs = 0.25

        # Every limit captured above is a copy of config.json. The live watcher
        # pushes a rewrite at this object through ``reconfigure`` so a write from
        # the dashboard, ``kirocrew config set`` or ``$EDITOR`` lands without a
        # gateway restart. The subscription holds ``self`` weakly, so a discarded
        # manager (tests, provider reloads) falls out of the registry by itself.
        # The prefixes ARE the watched-path list, so the dispatcher filters an
        # unrelated ``agent.*`` write rather than the applier re-deriving on it.
        # ``None`` until the first ``reconfigure`` runs, so that first reload
        # always resolves the cap rather than comparing against a snapshot that
        # was never taken.
        self._last_sizing_fields: tuple[object, ...] | None = None
        self._config_sub: live.Subscription | None = None
        try:
            self._config_sub = live.watch_object(
                self, *self.LIVE_CONFIG_PATHS, name="SubagentManager"
            )
        except Exception:
            logger.warning("SubagentManager could not subscribe to live config", exc_info=True)

        # The facade is the single owner of mutable registries and slot tokens.
        # Coordinators own transition logic and route every cross-boundary call
        # back through this object, preserving overrides and monkeypatch seams.
        self._monitor = OrphanStallMonitor(self)
        self._terminal = TerminalCoordinator(self)
        self._admission = SpawnAdmissionCoordinator(self)
        # Durable task queue (``kiro_crew.taskq``): ``_queue`` above is a bounded
        # window over this store's rows. Schema, import and reconcile must
        # finish before attachment. Loop callers open in a worker; synchronous
        # callers open inline. Pending or failed opens refuse typed; only
        # agent.task_queue_enabled=false selects the in-memory queue.
        # ``admitted -> starting`` is written at claim; ``running`` at the
        # run's first stream event; waits and wakes through admission.
        self._taskq: Any = None
        self._taskq_admit_wait_secs: float = 30.0
        #: Set when ``agent.task_queue_enabled`` is on but the store could not
        #: be opened: every spawn is then REFUSED (typed, ``task_store_unavailable``)
        #: instead of accepted into an in-memory queue that a restart forgets.
        self._taskq_unavailable: str | None = None
        #: The re-open schedule for that refusal (``taskq_reopen_if_due``, driven
        #: by the reaper sweep). The count is how many opens have failed, which is
        #: the exponent of the shared recovery backoff; the deadline is monotonic,
        #: and 0.0 means the next sweep may attempt one.
        self._taskq_reopen_attempts: int = 0
        self._taskq_reopen_at: float = 0.0
        self._continuation = ContinuationCoordinator(self)
        self._waves = WaveDigestCoordinator(self)
        self._run_events = RunEventCoordinator(self)
        self._cancellation = CancellationCoordinator(self)
        self._taskq_init_task: asyncio.Task[None] | None = None
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not SpawnAdmissionCoordinator.open_store_off_loop:
            self._taskq = self._open_taskq()
        else:
            self._taskq_unavailable = "durable task queue is initializing"
            self._taskq_init_task = loop.create_task(self._initialize_taskq())
            self._admission.track_store_task(self._taskq_init_task)

    def _open_taskq(self) -> Any:
        """Open, migrate and reconcile on a worker, or in a synchronous caller."""
        try:
            cfg = KiroCrewConfig.load()
            self._taskq_admit_wait_secs = float(cfg.agent.admit_wait_secs)
            if not cfg.agent.task_queue_enabled:
                self._taskq_unavailable = None
                return None
            store = self._admission.taskq_open(cfg, home=data_home())
            if store is None and self._taskq_unavailable is None:
                self._taskq_unavailable = "durable task queue could not be opened"
                self._admission.taskq_arm_reopen(cfg)
            return store
        except Exception as exc:
            logger.warning("durable task queue could not be opened", exc_info=True)
            self._taskq_unavailable = f"durable task queue could not be opened: {exc}"
            # Whoever records the refusal arms its retry, so no refusal path can
            # leave the store waiting for a restart. No cfg here: the load itself
            # is one of the things that may have raised.
            self._admission.taskq_arm_reopen()
            return None

    async def _initialize_taskq(self) -> None:
        opening = asyncio.create_task(asyncio.to_thread(self._open_taskq))
        try:
            store = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The thread still owns the open; retrieve and close its result.
            store = await opening
            if store is not None:
                await asyncio.to_thread(store.close)
            raise
        if getattr(self, "_shutting_down", False):
            if store is not None:
                await asyncio.to_thread(store.close)
            return
        if store is None and self._taskq is not None:
            # A re-open never UN-attaches: this task can be one the reaper armed
            # while the refusal stood, and an attach that happened meanwhile is
            # the newer fact.
            return
        # Attach only after schema, integrity check, imports and reconcile finish.
        # Before this assignment every entry point returns task_store_unavailable.
        self._taskq = store
        if store is not None:
            self._taskq_unavailable = None
            self._taskq_reopen_attempts = 0
            if self._reaper_task is not None and not self._reaper_task.done():
                self._drain_queue()

    async def wait_taskq_ready(self) -> None:
        """Wait for startup recovery without cancelling it if this caller leaves."""
        if self._taskq_init_task is not None:
            await asyncio.shield(self._taskq_init_task)

    def close(self) -> None:
        """Release the process-lifetime durable task store this manager opened.

        ``__init__`` opens the durable task queue (a SQLite connection plus its
        dedicated writer thread, see :class:`~kiro_crew.taskq.store.TaskStore`);
        ``cancel_all`` cancels in-flight runs but never touches that store, so
        without this every manager leaks the connection's descriptors and its
        writer executor for the life of the process. Idempotent and safe to call
        from a synchronous teardown: it cancels a still-pending async open, then
        closes the store if one was attached.
        """
        taskq_open_task = self._taskq_init_task
        if taskq_open_task is not None and not taskq_open_task.done():
            taskq_open_task.cancel()
        self._taskq_init_task = None
        store, self._taskq = self._taskq, None
        if store is not None:
            store.close()

    def _effective_turn_limit(self, info: SubagentInfo) -> int:
        return self._run_events._effective_turn_limit_impl(info)

    def update_completion_keep(self, mode: str, max_chars: int) -> None:
        return self._run_events.update_completion_keep_impl(mode, max_chars)

    #: Dotted config paths whose value this manager copies at construction. They
    #: are the subscription's prefixes, so a reload that touches none of them
    #: never reaches this object; one that touches any of them re-derives EVERY
    #: copy from the new config (cheaper and safer than a per-field diff, and the
    #: derivations are all O(1)).
    LIVE_CONFIG_PATHS: tuple[str, ...] = (
        "agent.max_subagents",
        "agent.subagent_auto_max",
        "agent.subagent_mem_buffer_pct",
        "agent.subagent_cost_gb",
        "session.pool_size",
        "agent.subagent_max_turns",
        "agent.subagent_timeout_secs",
        "agent.subagent_stall_idle_secs",
        "agent.subagent_spawn_stagger_secs",
        "agent.subagent_result_ttl_secs",
        "agent.completion_keep",
        "agent.completion_keep_chars",
    )

    #: The subset of ``LIVE_CONFIG_PATHS`` that actually feeds
    #: :func:`resolve_max_subagents` / :func:`compute_max_subagents` (the
    #: explicit pin, the auto-sizing inputs, and the pool-size term). Every
    #: other watched path only affects a plain field copy in
    #: :meth:`apply_limits`, so a reload that touches none of these has no way
    #: to change the resolved cap and must not pay for
    #: :func:`resolve_max_subagents`'s host memory / cgroup probe.
    SIZING_CONFIG_PATHS: tuple[str, ...] = (
        "agent.max_subagents",
        "agent.subagent_auto_max",
        "agent.subagent_mem_buffer_pct",
        "agent.subagent_cost_gb",
        "session.pool_size",
    )

    @staticmethod
    def _sizing_fields(cfg: KiroCrewConfig) -> tuple[object, ...]:
        """The values ``resolve_max_subagents`` reads from *cfg*, as a tuple.

        Comparing this tuple across reloads is how :meth:`reconfigure` tells
        whether a change could possibly move the resolved cap -- ``reconfigure``
        receives only the reloaded ``cfg``, not the ``ConfigChange`` that
        produced it (:func:`kiro_crew.config.live.watch_object` calls
        ``owner.reconfigure(cfg)``), so this diffs values rather than paths.
        """
        agent = cfg.agent
        return (
            agent.max_subagents,
            agent.subagent_auto_max,
            agent.subagent_mem_buffer_pct,
            agent.subagent_cost_gb,
            cfg.session.pool_size,
        )

    async def reconfigure(self, cfg: KiroCrewConfig) -> None:
        """Live-config applier: re-derive the captured limits from *cfg*.

        The concurrent cap may auto-size from host memory (``/proc/meminfo``,
        cgroup files), which is filesystem I/O -- resolved off the loop and
        handed to :meth:`apply_limits` ready-made, but ONLY when a sizing input
        actually moved. ``reconfigure`` is invoked on every reload that touches
        any of ``LIVE_CONFIG_PATHS`` (e.g. ``agent.completion_keep``), most of
        which cannot change the resolved cap at all; re-probing host memory on
        every one of those is a needless thread hop. When nothing in
        :attr:`SIZING_CONFIG_PATHS` moved since the last reconfigure, the
        current cap is kept and only the other limits are re-applied.
        """
        sizing_now = self._sizing_fields(cfg)
        if self._last_sizing_fields is not None and sizing_now == self._last_sizing_fields:
            cap = self._user_max_concurrent
        else:
            try:
                cap = await asyncio.to_thread(resolve_max_subagents, cfg)
            except Exception:
                logger.warning("resolve_max_subagents failed on reload; keeping the current cap")
                cap = self._user_max_concurrent
        self._last_sizing_fields = sizing_now
        self.apply_limits(cfg, max_concurrent=cap)

    def apply_limits(self, cfg: KiroCrewConfig, *, max_concurrent: int | None = None) -> None:
        """Adopt every constructor-captured limit from *cfg*.

        Applies the same normalization the constructor does: ``0`` for a
        timeout / stall interval keeps the built-in default (the sentinel the
        gateway passes when the field is unset), the stagger interval is floored
        at ``0.0``, and the cap goes through :func:`resolve_max_subagents` (the
        explicit ``max_subagents`` pin, or the host-sized auto value) unless the
        caller already resolved it and passes *max_concurrent*.

        Raising the cap admits queued spawns through the staggered pump;
        lowering it only stops new admissions -- an in-flight run is never
        cancelled to fit a smaller cap, the count simply drains below it as runs
        finish. Every read site (admission gate, reaper, stall detector, run
        timeout, parentless approval policy, completion-keep) reads the attribute
        at use, so the assignment is the whole apply.
        """
        agent = cfg.agent
        old_cap = self._max_concurrent
        if max_concurrent is None:
            try:
                max_concurrent = resolve_max_subagents(cfg)
            except Exception:
                logger.warning("resolve_max_subagents failed; keeping max_concurrent=%d", old_cap)
                max_concurrent = self._user_max_concurrent
        self._user_max_concurrent = max(1, int(max_concurrent))
        self._max_concurrent = self._clamp_effective_cap()
        try:
            self._default_turn_limit = int(agent.subagent_max_turns)
        except (TypeError, ValueError):
            pass
        try:
            timeout = int(agent.subagent_timeout_secs)
            self._default_timeout = timeout if timeout > 0 else _TIMEOUT_SECS
        except (TypeError, ValueError):
            pass
        try:
            stall = int(agent.subagent_stall_idle_secs)
            self._stall_idle_secs = stall if stall > 0 else _STALL_IDLE_SECS
        except (TypeError, ValueError):
            pass
        try:
            self._spawn_stagger_secs = max(0.0, float(agent.subagent_spawn_stagger_secs))
        except (TypeError, ValueError):
            pass
        try:
            self._result_ttl_secs = int(agent.subagent_result_ttl_secs)
        except (TypeError, ValueError):
            pass
        # ``agent.approval_mode`` is deliberately NOT adopted here: it is
        # boot-only (schema ``restart=True``) because every channel dispatcher
        # resolves it once at start, and one consumer taking it live while the
        # others keep the boot value would make the UI's "restart required"
        # honest for some tool calls and false for others.
        self.update_completion_keep(agent.completion_keep, int(agent.completion_keep_chars))
        logger.info(
            "SubagentManager reconfigured: max_concurrent=%d (was %d), turn_limit=%d, "
            "timeout=%ds, stall_idle=%ds, stagger=%.1fs, result_ttl=%ds",
            self._max_concurrent,
            old_cap,
            self._default_turn_limit,
            self._default_timeout,
            self._stall_idle_secs,
            self._spawn_stagger_secs,
            self._result_ttl_secs,
        )
        if self._max_concurrent > old_cap:
            self._notify_cap_raised()

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
        return self._monitor.start_reaper_impl()

    def release_queue_dispatch(self) -> None:
        """Open the pump held by ``defer_queue_dispatch`` and drain once.

        Called by the gateway after the memory barrier. Every drain request
        that landed while the hold stood (the boot dispatch's ``call_later``,
        the store attach, a dependency wake) returned without a pass, so this
        one pass is what picks up the rows they would have. Idempotent: a
        manager that was never held, or was already released, drains nothing
        extra here.
        """
        if not self._queue_dispatch_held:
            return
        self._queue_dispatch_held = False
        self._drain_queue()

    async def _reconcile_orphans(self) -> None:
        return await self._monitor._reconcile_orphans_impl()

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
        return await self._monitor._notify_orphan_impl(agent_id, state, recovery, has_result)

    async def _try_inject_orphan_notification(
        self, parent_session: str, msg: str, meta: dict | None = None
    ) -> bool:
        return await self._monitor._try_inject_orphan_notification_impl(parent_session, msg, meta)

    async def _send_orphan_slack_dm(self, msg: str) -> None:
        return await self._monitor._send_orphan_slack_dm_impl(msg)

    def _live_shared_count(self, pid: int | None, agents: "list[SubagentInfo]") -> int:
        return self._monitor._live_shared_count_impl(pid, agents)

    def _sample_live_costs(self) -> None:
        return self._monitor._sample_live_costs_impl()

    def _refresh_learned_cost(self) -> None:
        return self._monitor._refresh_learned_cost_impl()

    def _record_cost(self, info: SubagentInfo) -> None:
        return self._monitor._record_cost_impl(info)

    async def _reaper_loop(self) -> None:
        return await self._monitor._reaper_loop_impl()

    def _is_startup_stalled(self, info: SubagentInfo, now: float) -> bool:
        return self._monitor._is_startup_stalled_impl(info, now)

    @staticmethod
    def _note_tool_dispatch(info: SubagentInfo, event: Any) -> None:
        """Record the in-flight tool for liveness attribution.

        Mirrors ``AcpSessionHandle``'s ``_inflight_tool`` snapshot: title, the
        already-redacted input, the dispatch instant, and the TRUSTED
        ``is_shell`` / ``tool_name`` fields from ``_meta.kiro`` (never the
        LLM-authored title). The subagent event loop already receives the same
        ``AcpEvent``, and keeping only ``title`` would leave stall detection
        with nothing to attribute evidence with.

        Retiring the oracle here (rather than clearing it) is load-bearing: a
        movement walk still running against the PREVIOUS tool's command holds a
        reference to the old instance, and clearing in place would let its late
        write land on the new tool's baseline and read as movement.
        """
        info._inflight_tool = ToolCallState(
            title=event.title or "",
            command=event.tool_input or "",
            dispatch_ts=time.monotonic(),
            dispatch_boot_ts=boottime_now(),
            # No consumer parking on this path: a subagent's events are consumed
            # by the run loop itself, with no approval / IM send / hook holding a
            # frame, so this stamp cannot lag the runtime's spawn the way the
            # dashboard dispatch loop's can.
            dispatch_parked_secs=0.0,
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
        return await self._monitor._stall_verdict_impl(info)

    async def _maybe_flag_stall(self, agent_id: str, info: SubagentInfo, now: float) -> None:
        return await self._monitor._maybe_flag_stall_impl(agent_id, info, now)

    @staticmethod
    def _record_slow_command(info: SubagentInfo, idle: float) -> None:
        """Best-effort append of a stalled subagent's slow command for analysis.

        Writes to ``~/.kiro/crew/subagents/slow_commands.jsonl`` (rotated at
        1 MiB keeping one previous generation, survives per-agent folder
        cleanup). Deliberately separate from the
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
        claimed = self._terminal._claim_finalize_impl(info, supersede_recovery=supersede_recovery)
        if claimed:
            # The one reporter of the outcome also writes it to the task store,
            # fenced by the generation the run was dispatched under.
            self._admission.taskq_settle(info)
        return claimed

    async def _report_terminal(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> bool:
        return await self._terminal._report_terminal_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
        )

    async def _run_terminal_report(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> bool:
        return await self._terminal._run_terminal_report_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
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
    ) -> "asyncio.Task[bool]":
        return self._terminal._spawn_terminal_report_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
        )

    @staticmethod
    async def _await_report(task: "asyncio.Task[bool]") -> bool:
        """Block until a spawned terminal report completes, shielded.

        On normal completion this blocks until the report is delivered
        (sequencing unchanged). If the awaiting caller is cancelled, the shield
        keeps the report running to completion on its own task while the caller
        still receives ``CancelledError`` — teardown semantics are unchanged and
        the outcome is never stranded.
        """
        return await asyncio.shield(task)

    def _report_failure_boundary(self, parent: str, owner: str) -> object | None:
        """Resolve the exact live stage boundary without retaining it here."""
        resolver = self._stage_boundary_for_scope
        if resolver is None or not parent or not owner:
            return None
        try:
            boundary = resolver(parent, owner)
        except Exception:
            logger.debug("Failed to resolve report-failure boundary", exc_info=True)
            return None
        return boundary if getattr(boundary, "owner", None) == owner else None

    def _report_retention_refusal(self, parent: str, owner: str) -> str | None:
        """Read one exact boundary's fail-closed retention reason."""
        boundary = self._report_failure_boundary(parent, owner)
        reason = getattr(boundary, "report_retention_refused", None)
        return reason if isinstance(reason, str) and reason else None

    def _set_report_retention_refusal(self, parent: str, owner: str, reason: str) -> None:
        """Fail one exact live boundary closed when its payload cannot be retained."""
        boundary = self._report_failure_boundary(parent, owner)
        if boundary is not None:
            setattr(boundary, "report_retention_refused", reason)

    def _clear_report_retention_refusal(self, parent: str, owner: str) -> None:
        """Release only the exact discarded boundary's refusal state."""
        boundary = self._report_failure_boundary(parent, owner)
        if boundary is not None:
            setattr(boundary, "report_retention_refused", None)

    def _boundary_cancellation_refusal(self, parent: str, owner: str) -> str:
        """Read one exact live boundary's fail-closed hold refusal."""
        boundary = self._report_failure_boundary(parent, owner)
        reason = getattr(boundary, "cancellation_hold_refused", None)
        return reason if isinstance(reason, str) and reason else ""

    def _set_boundary_cancellation_refusal(
        self,
        parent: str,
        owner: str,
        reason: str,
    ) -> None:
        """Keep an unretained cancellation scope closed on its live boundary."""
        boundary = self._report_failure_boundary(parent, owner)
        if boundary is not None:
            setattr(boundary, "cancellation_hold_refused", reason)

    def _clear_boundary_cancellation_refusal(self, parent: str, owner: str) -> None:
        """Release a scope-cap refusal once the manager can retain that scope."""
        boundary = self._report_failure_boundary(parent, owner)
        if boundary is not None:
            setattr(boundary, "cancellation_hold_refused", None)

    def boundary_cancellation_refused(self, parent: str, owner: str) -> bool:
        """Whether the pending-scope cap keeps this live boundary closed."""
        return bool(self._boundary_cancellation_refusal(parent, owner))

    def reserve_boundary_cancellation_scopes(
        self,
        parent_session_keys: Sequence[str],
        boundary_owner: str,
    ) -> str:
        """Atomically retain one stage's parent scopes, or refuse them all."""
        scopes = tuple(
            dict.fromkeys(
                (parent, boundary_owner)
                for parent in parent_session_keys
                if parent and boundary_owner
            )
        )
        if not scopes:
            return ""
        pending = self._pending_boundary_cancellations
        needed = tuple(scope for scope in scopes if scope not in pending)
        cap = max(0, _PENDING_BOUNDARY_CANCELLATION_SCOPE_CAP)
        if len(pending) + len(needed) > cap:
            existing = next(
                (
                    reason
                    for parent, owner in scopes
                    if (reason := self._boundary_cancellation_refusal(parent, owner))
                ),
                "",
            )
            if existing:
                return existing
            self._boundary_cancellation_overflow_count += 1
            reason = (
                f"{_BOUNDARY_CANCELLATION_SCOPE_CAP_REASON}: retained {len(pending)}, "
                f"requested {len(needed)}, cap {cap}, "
                f"overflow count {self._boundary_cancellation_overflow_count}"
            )
            for parent, owner in scopes:
                self._set_boundary_cancellation_refusal(parent, owner, reason)
            return reason
        for parent, owner in scopes:
            self._clear_boundary_cancellation_refusal(parent, owner)
            pending.setdefault((parent, owner), "")
        return ""

    def _hold_boundary_cancellation(self, parent: str, owner: str) -> str:
        """Retain one cancellation scope, or return its bounded cap refusal."""
        return self.reserve_boundary_cancellation_scopes((parent,), owner)

    def _bounded_boundary_cancellation_failure(self, failure: object) -> str:
        """Redact and cap one retained durable-cancellation failure reason."""
        text = str(failure).strip() or "task store cancellation failed"
        return _redact_and_truncate(
            text,
            max(1, _PENDING_BOUNDARY_CANCELLATION_FAILURE_MAX_CHARS),
        )

    def _admit_report_failure(self, snapshot: _ReportFailureSnapshot) -> bool:
        """Retain one snapshot, or fail its exact live boundary closed."""
        key = (snapshot.parent_session_key, snapshot._stage_boundary_owner)
        bucket = self._boundary_report_payloads.get(key)
        if bucket is not None and snapshot.id in bucket:
            return False
        if self._report_retention_refusal(*key):
            return False
        if (len(bucket) if bucket is not None else 0) >= max(0, _REPORT_FAILURES_PER_PARENT_CAP):
            self._set_report_retention_refusal(
                *key,
                _REPORT_RETENTION_REFUSED_ROW_CAP,
            )
            return False
        retained_bytes = snapshot.retained_bytes
        if self._retained_report_failure_bytes + retained_bytes > max(
            0, _REPORT_FAILURE_BYTE_BUDGET
        ):
            self._set_report_retention_refusal(
                *key,
                _REPORT_RETENTION_REFUSED_BYTE_BUDGET,
            )
            return False
        self._boundary_report_payloads.setdefault(key, {})[snapshot.id] = snapshot
        self._retained_report_failure_bytes += retained_bytes
        return True

    def _latch_report_failure(self, info: SubagentInfo) -> None:
        """Retain one boundary-owned failure in a bounded payload bucket."""
        if info._report_failure_latched:
            return
        owner = stage_boundary_owner_for_run(info)
        parent = info.parent_session_key
        if not owner or not parent:
            return
        snapshot = _ReportFailureSnapshot.capture(info)
        if not self._admit_report_failure(snapshot):
            return
        info._report_failure_latched = True

    def _clear_report_failure(self, info: object) -> None:
        """Settle one latched failure after that report is redelivered."""
        is_snapshot = isinstance(info, _ReportFailureSnapshot)
        if not is_snapshot and not getattr(info, "_report_failure_latched", False):
            return
        owner = stage_boundary_owner_for_run(info)
        parent = getattr(info, "parent_session_key", "")
        payload_id = getattr(info, "id", "")
        if owner and isinstance(parent, str) and parent and isinstance(payload_id, str):
            key = (parent, owner)
            bucket = self._boundary_report_payloads.get(key)
            if bucket is not None and payload_id in bucket:
                removed = bucket.pop(payload_id)
                if isinstance(removed, _ReportFailureSnapshot):
                    self._retained_report_failure_bytes = max(
                        0,
                        self._retained_report_failure_bytes - removed.retained_bytes,
                    )
                if not bucket:
                    self._boundary_report_payloads.pop(key, None)
            live = self._agents.get(payload_id)
            if (
                live is not None
                and live.parent_session_key == parent
                and stage_boundary_owner_for_run(live) == owner
            ):
                live._report_failure_latched = False
        if isinstance(info, SubagentInfo):
            info._report_failure_latched = False

    def _report_failure_payloads_for_boundary(
        self,
        parent: str,
        owner: str,
    ) -> tuple[_ReportFailureSnapshot, ...]:
        """Return compact snapshots retained for one exact boundary."""
        return tuple(
            row
            for row in self._boundary_report_payloads.get((parent, owner), {}).values()
            if isinstance(row, _ReportFailureSnapshot)
        )

    def discard_report_failures(self, parent: str, owner: str) -> None:
        """Drop report debt and retained payloads when a boundary is discarded."""
        if not parent or not owner:
            return
        key = (parent, owner)
        retained = self._report_failure_payloads_for_boundary(parent, owner)
        for snapshot in retained:
            self._clear_report_failure(snapshot)
        for info in self._agents.values():
            if info.parent_session_key == parent and stage_boundary_owner_for_run(info) == owner:
                info._report_failure_latched = False
        self._boundary_report_payloads.pop(key, None)
        self._clear_report_retention_refusal(parent, owner)

    def discard_report_failure_scopes(
        self,
        scopes: Iterable[tuple[str, str]],
    ) -> int:
        """Drop only the exact failure scopes captured by slot teardown."""
        captured = tuple(dict.fromkeys(scopes))
        removed = 0
        for parent, owner in captured:
            key = (parent, owner)
            if (
                key not in self._boundary_report_payloads
                and self._report_retention_refusal(parent, owner) is None
            ):
                continue
            self.discard_report_failures(parent, owner)
            removed += 1
        return removed

    async def settle_before_delete(
        self,
        agent_id: str,
        active_boundary_owner: str,
    ) -> Literal["delivered", "pending"]:
        """Settle completion debt and remove a finished run atomically."""
        info = self._agents.get(agent_id)
        if info is None:
            return "delivered"
        active_reports = tuple(
            task
            for task, report_info in self._report_owners.items()
            if report_info is info or report_info.id == agent_id
        )
        if active_reports:
            outcomes = await asyncio.gather(
                *(asyncio.shield(task) for task in active_reports),
                return_exceptions=True,
            )
            if any(isinstance(outcome, BaseException) or outcome is False for outcome in outcomes):
                self._latch_report_failure(info)
            else:
                self._clear_report_failure(info)
        if info._report_failure_latched:
            owner = stage_boundary_owner_for_run(info)
            if owner and active_boundary_owner == owner:
                snapshot = next(
                    (
                        retained
                        for retained in self._report_failure_payloads_for_boundary(
                            info.parent_session_key,
                            owner,
                        )
                        if retained.id == info.id
                    ),
                    None,
                )
                if snapshot is None:
                    return "pending"
                delivered = await self._run_terminal_report(
                    snapshot.delivery_info(),
                    source="Completed run deletion",
                    injection_timeout_reason=("delivery timed out while deleting completed run"),
                    mark_delivered_on_success=False,
                )
                if not delivered:
                    return "pending"
                self._clear_report_failure(snapshot)
            elif owner:
                self.discard_report_failures(info.parent_session_key, owner)
        self._agents.pop(agent_id, None)
        self._tasks.pop(agent_id, None)
        return "delivered"

    def _mint_agent_id(self) -> str:
        """Draw a run id: :data:`_RUN_ID_HEX_CHARS` hex characters of random bytes.

        Every spawn identity comes from here, which is the point -- one draw site
        is what lets the width be a single number.

        The width carries the uniqueness on its own, with nothing to remember and
        nothing to read. At 8 characters the id was 32 bits, so 2000 spawns on
        one host collided about once in 2,100 times, and the collision did not
        read as one: identity is assigned before registration, so the caller was
        handed the id and the accept then failed on the duplicate primary key,
        reaching the user as ``task store write failed`` -- naming a subsystem
        that was working correctly. 64 bits puts that at about 1 in 10**13.

        Drawn from ``os.urandom`` rather than a ``uuid4`` prefix. A v4 UUID spends
        its 13th hex character on the fixed version digit ``4``, so the first 16
        characters of one carry 60 random bits, not 64 -- a 16-fold worse bound
        than the width advertises, from a detail no reader of the slice can see.

        A checked narrow draw would need to know which ids are taken, and a
        durable task row outlives the process that wrote it, so it would have to
        ask the store -- which the spawn path cannot do, because taking a
        task-store connection on the event loop stalls every session's turn
        (:mod:`kiro_crew.on_loop_db` refuses it). Widening removes the question.
        """
        return os.urandom(_RUN_ID_HEX_CHARS // 2).hex()

    async def _redeliver_boundary_report_payloads(self, parent: str, owner: str) -> bool:
        """Retry retained terminal payloads for one live stage boundary."""
        retained = self._report_failure_payloads_for_boundary(parent, owner)
        for snapshot in retained:
            delivered = await self._run_terminal_report(
                snapshot.delivery_info(),
                source="Stage boundary report retry",
                injection_timeout_reason="delivery timed out while retrying stage boundary",
                mark_delivered_on_success=False,
            )
            if delivered:
                self._clear_report_failure(snapshot)
        return bool(retained)

    def _peek_report_failures(self, parent: str, owner: str) -> int:
        """Derive this boundary's failure count from retained or refused rows."""
        if not owner:
            return 0
        limit = _REPORT_FAILURES_PER_PARENT_CAP + 1
        if self._report_retention_refusal(parent, owner):
            return limit
        return min(len(self._boundary_report_payloads.get((parent, owner), {})), limit)

    def _report_failure_error(
        self,
        parent: str,
        owner: str,
        failed: int,
    ) -> SubagentReportDeliveryError:
        refusal = self._report_retention_refusal(parent, owner)
        if refusal == _REPORT_RETENTION_REFUSED_BYTE_BUDGET:
            budget = max(0, _REPORT_FAILURE_BYTE_BUDGET)
            mib = 1024 * 1024
            label = (
                f"{budget // mib} MiB" if budget >= mib and budget % mib == 0 else f"{budget} bytes"
            )
            return SubagentReportDeliveryError(
                f"Report-failure byte budget ({label}) was hit for this boundary"
            )
        if refusal == _REPORT_RETENTION_REFUSED_ROW_CAP:
            return SubagentReportDeliveryError(
                f"Report-failure row cap ({max(0, _REPORT_FAILURES_PER_PARENT_CAP)}) "
                "was hit for this boundary"
            )
        return SubagentReportDeliveryError(f"{failed} registered terminal report task(s) failed")

    async def wait_for_parent_reports(
        self,
        parent_session_key: str,
        boundary_owner: str = "",
    ) -> bool:
        """Wait until this boundary's registered terminal reports finish.

        Active tasks live in ``_report_owners``; completed failures remain in
        ``_boundary_report_payloads`` until their report is redelivered or their
        boundary is discarded.
        """
        observed = False
        while True:
            if await self._redeliver_boundary_report_payloads(
                parent_session_key,
                boundary_owner,
            ):
                observed = True
            failed = self._peek_report_failures(parent_session_key, boundary_owner)
            if failed:
                raise self._report_failure_error(
                    parent_session_key,
                    boundary_owner,
                    failed,
                )
            reports = tuple(
                task
                for task, owner in self._report_owners.items()
                if owner.parent_session_key == parent_session_key
                and stage_boundary_owner_for_run(owner) == boundary_owner
            )
            if not reports:
                return observed
            observed = True
            outcomes = await asyncio.gather(
                *(asyncio.shield(task) for task in reports),
                return_exceptions=True,
            )
            # The normal done callback removes every owner and latches failures.
            # Focused tests and shutdown races may leave a completed entry here;
            # consume it explicitly so the barrier cannot observe it twice.
            for task in reports:
                if task.done():
                    self._report_owners.pop(task, None)
            outcome_failures = sum(
                isinstance(outcome, BaseException) or outcome is False for outcome in outcomes
            )
            latched_failures = self._peek_report_failures(
                parent_session_key,
                boundary_owner,
            )
            failed = max(outcome_failures, latched_failures)
            if failed:
                raise self._report_failure_error(
                    parent_session_key,
                    boundary_owner,
                    failed,
                )

    def _release_slot(self, info: SubagentInfo) -> bool:
        return self._terminal._release_slot_impl(info)

    async def _force_reap(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        return await self._terminal._force_reap_impl(agent_id, info, elapsed, reason=reason)

    async def _sigkill_session(self, session_key: str) -> None:
        return await self._terminal._sigkill_session_impl(session_key)

    def notify_injection_failed(
        self, info: SubagentInfo, reason: str = "delivery timed out"
    ) -> None:
        return self._terminal.notify_injection_failed_impl(info, reason)

    def _clamp_effective_cap(self) -> int:
        if self._adaptive_cap is None:
            return self._user_max_concurrent
        return max(0, min(self._user_max_concurrent, int(self._adaptive_cap)))

    def set_cap_raise_listener(self, listener: Callable[[], object] | None) -> None:
        """Register the ONE hook a cap raise rings, or ``None`` to drop it.

        For a gate that is bounded by ``max_concurrent`` but lives outside this
        manager: it can read the new cap whenever it likes, but it has no edge
        to react to, and its own occupancy may never produce one (see
        :meth:`_notify_cap_raised`). Set, never appended: the gateway owns the
        single runner lane and re-registration replaces the stale handle.
        """
        self._cap_raise_listener = listener

    def _notify_cap_raised(self) -> None:
        """Fan freed capacity out to every gate the live cap bounds.

        MUST be called on the event loop, not from a worker thread: the runner
        lane resolves its parked waiters' futures, which is loop-affine.

        The subagent queue drains through the staggered pump. The runner lane
        (`taskq/adapters/runner.py`) reads this cap as its ceiling but parks its
        waiters on a bare future, so a waiter parked while the cap was ``0``
        holds no slot and has no running holder whose release would wake it:
        this raise is its only edge. The lane keeps its own occupancy count, so
        it grants exactly the FIFO prefix the new cap allows -- and nothing at
        all while the effective cap is still ``0``.
        """
        if self._queue:
            # Freed capacity: the pump re-checks the gate itself and honours the
            # stagger interval, so this never bursts.
            self._drain_queue()
        listener = self._cap_raise_listener
        if listener is None:
            return
        try:
            listener()
        except Exception:  # noqa: BLE001 - a broken hook must not block a raise
            logger.debug("cap-raise listener failed", exc_info=True)

    def set_effective_cap(self, cap: int | None) -> int:
        """Adaptive-controller seam: bound the live cap beneath the user's.

        ``None`` removes the bound. ``0`` pauses new grants (in-flight runs
        finish; nothing is cancelled). A raise notifies every gate the cap
        bounds exactly as a config raise does. Returns the cap now in force.
        """
        old_cap = self._max_concurrent
        self._adaptive_cap = None if cap is None else max(0, int(cap))
        self._max_concurrent = self._clamp_effective_cap()
        if self._max_concurrent != old_cap:
            logger.info(
                "SubagentManager effective cap %d -> %d (user ceiling %d)",
                old_cap,
                self._max_concurrent,
                self._user_max_concurrent,
            )
        if self._max_concurrent > old_cap:
            self._notify_cap_raised()
        return self._max_concurrent

    @property
    def user_max_concurrent(self) -> int:
        """The user's resolved cap -- the ceiling the adaptive cap sits under."""
        return self._user_max_concurrent

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        return self._running_count

    @property
    def pending_work_count(self) -> int:
        """Return accepted subagent work that a process restart would interrupt.

        ``running_count`` alone stops representing work before shielded terminal
        delivery finishes, and an unexpected-cancel recovery can be live while
        holding no concurrency slot. Count the finite manager-owned registries
        instead, while retaining ``running_count`` as a fail-closed floor for a
        slot published just before its task registration. The perpetual reaper
        is maintenance and is deliberately excluded; its one-shot orphan
        reconciliation is finite delivery work and is included.
        """
        live_primary: set[int] = set()
        for task in self._tasks.values():
            if not task.done():
                live_primary.add(id(task))

        pending = len(self._queue) + max(max(0, int(self._running_count)), len(live_primary))
        seen = set(live_primary)
        extra_tasks = [*self._report_tasks, *self._followup_watchers.values()]
        reconcile = getattr(self, "_reconcile_task", None)
        if reconcile is not None:
            extra_tasks.append(reconcile)
        for task in extra_tasks:
            marker = id(task)
            if marker in seen:
                continue
            seen.add(marker)
            if not task.done():
                pending += 1

        # A cancelled to_thread state write can outlive its run task. Its done
        # callback removes this hold, so every entry is finite restart-sensitive
        # work even though the worker Future has no retained awaitable here.
        pending += len(self._abandoned_state_writers)
        return pending

    def running_agents_for(self, parent_key: str) -> list[dict]:
        return self._run_events.running_agents_for_impl(parent_key)

    def completion_event(self, parent_key: str) -> "asyncio.Event":
        """Event pulsed each time a run belonging to *parent_key* finishes.

        For a caller that would otherwise poll :meth:`running_agents_for` — an
        O(n) scan over every retained agent — on a timer. The event is a PULSE,
        not a state: a waiter clears it, re-reads the running set, and waits
        again, so a completion landing between the clear and the read is still
        observed on the next wait rather than lost.

        It is not a guarantee. A run can reach a terminal state on a path that
        never announces (``cancel_all`` at shutdown), so every waiter must keep
        a timeout of its own; that is why this returns a bare event rather than
        a helper that waits. Release it with
        :meth:`release_completion_event` when the wait is over.
        """
        evt = self._completion_waiters.get(parent_key)
        if evt is not None:
            return evt
        if len(self._completion_waiters) >= _MAX_COMPLETION_WAITERS:
            # A detached event nothing ever sets: the caller degrades to its own
            # fallback timeout instead of this growing without bound. Reachable
            # only if callers leak registrations, which is why it is logged.
            logger.warning(
                "Subagent completion-waiter table is full (%d); %s gets no pulse",
                _MAX_COMPLETION_WAITERS,
                parent_key,
            )
            return asyncio.Event()
        evt = asyncio.Event()
        self._completion_waiters[parent_key] = evt
        return evt

    def release_completion_event(self, parent_key: str) -> None:
        """Drop *parent_key*'s completion event. Idempotent."""
        self._completion_waiters.pop(parent_key, None)

    def signal_completion(self, parent_key: str) -> None:
        """Pulse *parent_key*'s completion event, if anything is waiting on it.

        Deliberately creates nothing: a parent with no waiter must not leave an
        entry behind, so the announce path stays free of bookkeeping.
        """
        evt = self._completion_waiters.get(parent_key)
        if evt is not None:
            evt.set()

    def task_memory_rows(self) -> list[dict[str, object]]:
        return self._monitor.task_memory_rows_impl()

    def spawn(
        self,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        max_turns: int = 0,
        model: str | None = None,
        reasoning_effort: str = "",
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
        memory_store: str = "",
        _agent_prevalidated: bool = False,
        _from_queue: bool = False,
        _preassigned_id: str = "",
        _crew_log_asked: "tuple[str, int] | None" = None,
        _memory_mode: str | None = None,
        _store_accepted: bool = False,
        _stop_before_claim: bool = False,
        _claimed: "tuple[int, bool, str] | None" = None,
        _window_hint: "bool | None" = None,
        _child_registration: bool = True,
        *,
        crew: str = "",
        target_member: str | None = None,
        delegation: dict[str, str] | None = None,
        _execution_context: dict | None = None,
        _stage_boundary_owner: str = "",
        acp_backend: str | None = None,
    ) -> SubagentInfo | None:
        result = self._admission.spawn_impl(
            task,
            parent_session_key,
            agent,
            max_turns,
            model,
            reasoning_effort,
            allowed_tools,
            bare,
            cwd,
            approval_mode,
            silent,
            batch_id,
            batch_total,
            keep,
            conversation_key,
            app,
            include_memory,
            include_lessons,
            include_project,
            memory_store,
            _agent_prevalidated,
            _from_queue,
            _preassigned_id,
            _crew_log_asked=_crew_log_asked,
            _memory_mode=_memory_mode,
            _store_accepted=_store_accepted,
            _stop_before_claim=_stop_before_claim,
            _claimed=_claimed,
            _window_hint=_window_hint,
            _child_registration=_child_registration,
            crew=crew,
            target_member=target_member,
            delegation=delegation,
            _execution_context=_execution_context,
            _stage_boundary_owner=_stage_boundary_owner,
            acp_backend=acp_backend,
        )
        assert not isinstance(result, PreparedSpawn)
        # Every synchronous gate return (started, queued, or refused) receives
        # the same admission snapshot before a scheduled announce can run.
        if isinstance(result, SubagentInfo):
            result._stage_boundary_owner = _stage_boundary_owner
        # ``ClaimPoint`` comes back ONLY for ``_stop_before_claim=True``, whose
        # sole caller is the coroutine pump's ``_dispatch_async``; every other
        # caller receives a ``SubagentInfo`` or None as declared.
        return result  # type: ignore[return-value]

    def prepare_spawn(self, task: str, **kwargs: Any) -> "SubagentInfo | PreparedSpawn | None":
        """Run every policy gate of :meth:`spawn` and return the row to persist
        instead of starting anything. A refusal comes back as the same done
        ``SubagentInfo`` :meth:`spawn` would return; ``None`` is the legacy
        at-capacity answer."""
        kwargs.pop("_from_queue", None)
        kwargs.pop("_store_accepted", None)
        prepared = self._admission.spawn_impl(task, _prepare_only=True, **kwargs)
        assert not isinstance(prepared, ClaimPoint)  # never requested here
        if isinstance(prepared, SubagentInfo):
            prepared._stage_boundary_owner = str(kwargs.get("_stage_boundary_owner") or "")
        return prepared

    async def spawn_async(self, task: str, **kwargs: Any) -> SubagentInfo | None:
        """:meth:`spawn` for event-loop callers (``/api/spawn``).

        Write-before-ack with the write OFF the loop: the policy gates run
        first (``prepare_spawn``), the row is written on the store's dedicated
        writer thread (``TaskStore.run``), and only then does the sync
        ``spawn`` start the run with ``_store_accepted=True`` -- the SQLite
        lock wait never blocks the loop, and the caller is still acked only
        once the row exists. Without a durable store this is plain ``spawn``.
        """
        # Snapshot loop-owned policy inputs before reading a missing durable
        # carrier. The admission gate below still runs on-loop after the await.
        if (
            not isinstance(task, str)
            or not task.strip()
            or getattr(self._sessions, "admission_closed", False) is True
        ):
            return self.spawn(task, **kwargs)
        if kwargs.get("_execution_context") is None:
            from kiro_crew.execution_context import read_session_execution
            from kiro_crew.subagent_persistence import read_run_execution

            parent = str(kwargs.get("parent_session_key") or "")
            conversation = str(kwargs.get("conversation_key") or "")
            mode = kwargs.get("_memory_mode")
            inherited = None
            try:
                if mode is None:
                    resolver = self._memory_mode_for_session
                    mode = resolver(parent) if resolver is not None else "persistent"
                if not isinstance(mode, str) or mode not in {
                    "persistent",
                    "incognito",
                    "temporary",
                }:
                    raise ValueError("unknown memory mode")
                if parent and not kwargs.get("agent") and not conversation:
                    inherited = self._sessions.get_agent_selection(parent)
                record_id = (conversation or parent).removeprefix("subagent:")
                live = (
                    self._agents.get(record_id)
                    if (conversation or parent).startswith("subagent:")
                    else None
                )
                record = live.execution_context if live is not None else None
                if record is None:
                    record = (
                        await asyncio.to_thread(read_run_execution, record_id)
                        if conversation
                        else await asyncio.to_thread(read_session_execution, parent)
                    )
                execution = self._admission.resolve_spawn_execution(
                    parent_session_key=parent,
                    conversation_key=conversation,
                    agent=kwargs.get("agent", ""),
                    memory_store=kwargs.get("memory_store", ""),
                    app=kwargs.get("app", ""),
                    crew=kwargs.get("crew", ""),
                    target_member=kwargs.get("target_member"),
                    _memory_mode=mode,
                    _record=record,
                    _inherited_selection=inherited,
                )
                kwargs["_execution_context"] = execution.to_record()
                kwargs["_memory_mode"] = execution.memory_mode
            except (OSError, ValueError) as exc:
                batch_id = str(kwargs.get("batch_id") or "")
                batch_total = max(0, int(kwargs.get("batch_total") or 0))
                if batch_id and not kwargs.get("_from_queue") and not kwargs.get("_store_accepted"):
                    submitted = self._batch_submitted.setdefault(batch_id, [0, batch_total])
                    submitted[0] += 1
                    self._batch_progress_ts[batch_id] = time.time()
                return self._announce_rejection(
                    SubagentInfo(
                        id=kwargs.get("_preassigned_id") or self._mint_agent_id(),
                        task=_redact(task),
                        parent_session_key=parent,
                        agent=str(kwargs.get("agent") or ""),
                        memory_mode=mode if isinstance(mode, str) else "persistent",
                        done=True,
                        error=f"memory_unavailable: {exc}",
                        batch_id=batch_id,
                        batch_total=batch_total,
                    )
                )
        store = self._admission.taskq_store()
        if store is None:
            return self.spawn(task, **kwargs)
        prepared = self.prepare_spawn(task, **kwargs)
        if not isinstance(prepared, PreparedSpawn):
            return prepared
        # From the moment the row exists until this call has claimed or
        # windowed it, the pump's refill must not pick it up: the awaits below
        # are where a concurrent drain could otherwise start it twice.
        admitting: set[str] = self.__dict__.setdefault("_admitting_ids", set())
        admitting.add(prepared.agent_id)
        try:
            return await self._spawn_async_accepted(task, prepared, **kwargs)
        finally:
            # A pressure defer posted on the way out must be ON the row before
            # the pump may refill it, or the next pass re-runs the gate on a
            # row whose ``next_run_at`` is not set yet.
            await self._admission.await_pending_defer(prepared.agent_id)
            admitting.discard(prepared.agent_id)

    async def _spawn_async_accepted(
        self, task: str, prepared: PreparedSpawn, **kwargs: Any
    ) -> SubagentInfo | None:
        store = self._admission.taskq_store()
        assert store is not None
        store_err = await store.run(self._admission.taskq_accept_record, prepared.record)
        if store_err:
            sel().log_tool_invocation(
                session_key=str(kwargs.get("parent_session_key") or ""),
                source="subagent",
                tool_name="spawn_run",
                outcome="refused_task_store",
                metadata={"error": str(store_err)[:200], "subagent_id": prepared.agent_id},
            )
            return self._announce_rejection(
                SubagentInfo(
                    id=prepared.agent_id,
                    task=redact_credentials(redact_exfiltration_urls(task)[0])[0],
                    agent=str(kwargs.get("agent") or ""),
                    parent_session_key=str(kwargs.get("parent_session_key") or ""),
                    _stage_boundary_owner=str(kwargs.get("_stage_boundary_owner") or ""),
                    done=True,
                    error=f"spawn refused: task store unavailable ({store_err})",
                    error_code=self._admission.TASK_STORE_UNAVAILABLE_CODE,
                    batch_id=str(kwargs.get("batch_id") or ""),
                    batch_total=max(0, int(kwargs.get("batch_total") or 0)),
                )
            )
        params = dict(prepared.params)
        params.pop("_preassigned_id", None)
        # No store I/O on the loop from here on: the window decision and the
        # claim run on the writer thread; the sync re-entry only registers.
        await self._admission.ensure_coordinator_async()
        window_hint = await self._admission.taskq_should_window_async(prepared.agent_id)
        common: dict[str, Any] = dict(
            _preassigned_id=prepared.agent_id,
            _store_accepted=True,
            _window_hint=window_hint,
            _child_registration=False,  # the W3 branch runs awaited, below
        )
        first: Any = self.spawn(**params, **common, _stop_before_claim=True)
        if not isinstance(first, ClaimPoint):
            if first is not None and first.queued and not first.done:
                await self._admission.taskq_child_registered_async(first)
            return first
        # The slot is reserved (ClaimPoint); the claim is awaited off-loop and
        # the re-entry consumes the reservation or releases it.
        result = await self._admission.claim_and_start(
            first,
            lambda claimed: self.spawn(**params, **common, _claimed=claimed),
            stop_params={**params, **common},
        )
        if result is not None and not result.done and result.id in self._agents:
            # Nested child of a parent blocked in spawn_sub_agents: the parent
            # yields its slot (taskq.waits, W3) with the store I/O off-loop.
            await self._admission.taskq_child_registered_async(result)
        return result

    async def _safe_announce(self, info: SubagentInfo) -> None:
        return await self._admission._safe_announce_impl(info)

    def _announce_rejection(self, info: SubagentInfo) -> SubagentInfo:
        return self._admission._announce_rejection_impl(info)

    def _should_stagger_queue(self, now: float) -> tuple[bool, bool]:
        return self._admission._should_stagger_queue_impl(now)

    # ── Continuable conversations (keep=True) ─────────────────────────────

    def _conversation_busy(self, conv_key: str) -> SubagentInfo | None:
        return self._continuation._conversation_busy_impl(conv_key)

    def _keep_recorded_on_disk(self, key: str) -> bool:
        return self._continuation._keep_recorded_on_disk_impl(key)

    def _promote_conversation(
        self, conv_id: str, conv_key: str, last_used: float | None = None
    ) -> None:
        return self._continuation._promote_conversation_impl(conv_id, conv_key, last_used)

    def _scan_keep_states(self) -> list[tuple[str, str, str, str, str, float]]:
        return self._continuation._scan_keep_states_impl()

    async def _rebuild_conversation_registry(self) -> None:
        return await self._continuation._rebuild_conversation_registry_impl()

    def native_child_resume_refusal(self, conversation_id: str) -> str | None:
        """Typed refusal when *conversation_id* is a harness-native child of a
        live session (no conversation of its own; the parent is the lever)."""
        return self._continuation.native_child_resume_refusal(conversation_id)

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
        _memory_mode: str | None = None,
        _crew_log_asked: "tuple[str, int] | None" = None,
        _stage_boundary_owner: str = "",
    ) -> SubagentInfo | None:
        return self._continuation.continue_conversation_impl(
            conv_id,
            task,
            parent_session_key,
            agent,
            model,
            max_turns,
            cwd,
            _preassigned_id,
            _memory_mode=_memory_mode,
            _crew_log_asked=_crew_log_asked,
            _stage_boundary_owner=_stage_boundary_owner,
        )

    async def continue_conversation_async(
        self,
        conv_id: str,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        model: str | None = None,
        max_turns: int = 0,
        cwd: str = "",
        _preassigned_id: str = "",
        _memory_mode: str | None = None,
        _crew_log_asked: "tuple[str, int] | None" = None,
        _stage_boundary_owner: str = "",
    ) -> SubagentInfo | None:
        return await self._continuation.continue_conversation_async_impl(
            conv_id,
            task,
            parent_session_key,
            agent,
            model,
            max_turns,
            cwd,
            _preassigned_id,
            _memory_mode,
            _crew_log_asked,
            _stage_boundary_owner,
        )

    def _continue_prelude(
        self,
        conv_id: str,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        model: str | None = None,
        max_turns: int = 0,
        cwd: str = "",
        _preassigned_id: str = "",
        _memory_mode: str | None = None,
        _crew_log_asked: "tuple[str, int] | None" = None,
        *,
        _execution_context=None,
        _captured_state=...,
        _stage_boundary_owner: str = "",
    ) -> "SubagentInfo | dict[str, Any] | None":
        return self._continuation._continue_prelude_impl(
            conv_id,
            task,
            parent_session_key,
            agent,
            model,
            max_turns,
            cwd,
            _preassigned_id,
            _memory_mode,
            _crew_log_asked,
            _execution_context=_execution_context,
            _captured_state=_captured_state,
            _stage_boundary_owner=_stage_boundary_owner,
        )

    def recorded_cwd(self, conv_id: str) -> str:
        return self._continuation.recorded_cwd_impl(conv_id)

    def _inherited_context_groups(self, conv_id: str) -> tuple[bool, bool, bool]:
        return self._continuation._inherited_context_groups_impl(conv_id)

    def _inherited_memory_store(self, conv_id: str) -> str:
        return self._continuation._inherited_memory_store_impl(conv_id)

    async def steer_run(self, agent_id: str, message: str) -> tuple[bool, str]:
        return await self._continuation.steer_run_impl(agent_id, message)

    # Bounds for the follow_up watcher: poll cadence, post-done busy retries
    # (finalization may briefly hold the conversation), and a hard deadline so
    # a wedged run can never leave an immortal watcher behind.
    _FOLLOWUP_POLL_SECS = 2.0
    _FOLLOWUP_BUSY_RETRIES = 10
    _FOLLOWUP_BUSY_RETRY_SECS = 3.0

    async def follow_up_run(self, agent_id: str, message: str) -> tuple[bool, str]:
        return await self._continuation.follow_up_run_impl(agent_id, message)

    def _arm_followup_watcher(self, info: SubagentInfo) -> None:
        return self._continuation._arm_followup_watcher_impl(info)

    async def _deliver_followups(self, info: SubagentInfo) -> None:
        return await self._continuation._deliver_followups_impl(info)

    async def _announce_followup_failure(
        self,
        info: SubagentInfo,
        reason: str,
        failure_info: SubagentInfo | None = None,
        messages: list | None = None,
    ) -> None:
        return await self._continuation._announce_followup_failure_impl(
            info, reason, failure_info, messages
        )

    def _audit_followup(self, info: SubagentInfo, outcome: str) -> None:
        return self._continuation._audit_followup_impl(info, outcome)

    def release_conversation(self, conv_id: str) -> tuple[bool, str]:
        return self._continuation.release_conversation_impl(conv_id)

    def _sweep_conversations(self, now: float) -> None:
        return self._continuation._sweep_conversations_impl(now)

    def _drain_queue(self) -> None:
        return self._admission._drain_queue_impl()

    async def _drain_queue_async(self) -> None:
        return await self._admission._drain_queue_async_impl()

    async def _drain_queue_pass(self) -> None:
        return await self._admission._drain_queue_pass_impl()

    def _drain_queue_sync(
        self,
        *,
        refill: Callable[..., int],
        dispatch: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        return self._admission._drain_queue_sync_impl(refill=refill, dispatch=dispatch)

    async def _dispatch_async(self, params: dict[str, Any]) -> SubagentInfo | None:
        return await self._admission._dispatch_async_impl(params)

    def _after_dispatch(
        self, params: dict[str, Any], drained: SubagentInfo | None, *, refill: Callable[..., int]
    ) -> None:
        return self._admission._after_dispatch_impl(params, drained, refill=refill)

    async def _spawn_with_approval(self, info: SubagentInfo) -> None:
        return await self._admission._spawn_with_approval_impl(info)

    def _log_spawned(self, info: SubagentInfo) -> None:
        return self._admission._log_spawned_impl(info)

    @property
    def running(self) -> list[SubagentInfo]:
        """Return currently running (not done) subagents."""
        return [a for a in self._agents.values() if not a.done]

    def has_live_shared_session(self, session_key: str) -> bool:
        """Recognize a shared child only while its exact runtime handle is live.

        Run records survive completion/restart for display and continuation;
        they are not session authority. The runtime's queue registry is what
        destroy() unregisters, even when the parent process keeps running.
        """
        for info in self._agents.values():
            if info.done or info.reaped or not info._session_sharing:
                continue
            if (info.conversation_key or f"subagent:{info.id}") != session_key:
                continue
            provider = info._shared_provider
            if not isinstance(provider, AcpSessionProvider):
                continue
            runtime, handle = provider._runtime, provider._handle
            if (
                runtime.is_alive()
                and runtime._session_queues.get(handle.session_id) is handle._queue
            ):
                return True
        return False

    @property
    def all_agents(self) -> list[SubagentInfo]:
        """Return all tracked subagents (running and done)."""
        return list(self._agents.values())

    def batch_members_pending(self, batch_id: str) -> bool:
        return self._waves.batch_members_pending_impl(batch_id)

    async def batch_members_pending_async(self, batch_id: str) -> bool:
        return await self._waves.batch_members_pending_async_impl(batch_id)

    def wave_has_live_nested_spawns(self, batch_id: str) -> bool:
        return self._waves.wave_has_live_nested_spawns_impl(batch_id)

    def finalize_batch(self, batch_id: str) -> None:
        return self._waves.finalize_batch_impl(batch_id)

    def record_lost_submission(
        self,
        batch_id: str,
        batch_total: int,
        reason: str,
        parent_session_key: str = "",
    ) -> None:
        return self._waves.record_lost_submission_impl(
            batch_id, batch_total, reason, parent_session_key
        )

    def _sweep_stuck_waves(self, now: float) -> None:
        return self._waves._sweep_stuck_waves_impl(now)

    async def _sweep_stuck_waves_async(self, now: float) -> None:
        return await self._waves._sweep_stuck_waves_async_impl(now)

    def _sweep_digest_holds(self, now: float) -> None:
        return self._waves._sweep_digest_holds_impl(now)

    async def _sweep_digest_holds_async(self, now: float) -> None:
        return await self._waves._sweep_digest_holds_async_impl(now)

    def force_digest_flush(
        self,
        batch_id: str,
        parent_session_key: str,
        batch_total: int,
        held_secs: float,
    ) -> None:
        return self._waves.force_digest_flush_impl(
            batch_id, parent_session_key, batch_total, held_secs
        )

    async def _announce_digest_flush(self, info: SubagentInfo) -> None:
        return await self._waves._announce_digest_flush_impl(info)

    async def settle_queued_delivery(self, agent_ids: list[str]) -> None:
        return await self._waves.settle_queued_delivery_impl(agent_ids)

    def _settle_digest_holds(self, info: SubagentInfo) -> None:
        return self._waves._settle_digest_holds_impl(info)

    def get(self, agent_id: str) -> SubagentInfo | None:
        return self._run_events.get_impl(agent_id)

    @property
    def count(self) -> int:
        return len(self.running)

    async def _teardown_run_session(self, info: SubagentInfo, session_key: str) -> None:
        return await self._run_events._teardown_run_session_impl(info, session_key)

    async def _run(self, info: SubagentInfo) -> None:
        return await self._run_events._run_impl(info)

    def _schedule_cancel_recovery(self, info: SubagentInfo) -> None:
        return self._cancellation._schedule_cancel_recovery_impl(info)

    async def _touch_activity(self, info: SubagentInfo) -> None:
        return await self._run_events._touch_activity_impl(info)

    async def _fire_event(self, etype: str, info: SubagentInfo, extra: dict | None = None) -> None:
        return await self._run_events._fire_event_impl(etype, info, extra)

    def _queued_depth(self, parent_session_key: str) -> int:
        return self._run_events._queued_depth_impl(parent_session_key)

    async def _queued_depth_async(self, parent_session_key: str) -> int:
        return await self._run_events._queued_depth_async_impl(parent_session_key)

    @property
    def queued_count(self) -> int:
        """Return all not-yet-registered spawns in the stagger queue."""
        return len(self._queue)

    def queued_count_for(self, parent_session_key: str) -> int:
        return self._run_events.queued_count_for_impl(parent_session_key)

    async def queued_count_for_async(self, parent_session_key: str) -> int:
        return await self._run_events.queued_count_for_async_impl(parent_session_key)

    def has_pending_work_for(self, parent_session_key: str) -> bool:
        return self._run_events.has_pending_work_for_impl(parent_session_key)

    async def has_pending_work_for_async(self, parent_session_key: str) -> bool:
        return await self._run_events.has_pending_work_for_async_impl(parent_session_key)

    def _emit_queue_depth(
        self,
        parent_session_key: str,
        batch_id: str = "",
        *,
        wait: dict[str, Any] | None = None,
    ) -> None:
        return self._run_events._emit_queue_depth_impl(parent_session_key, batch_id, wait=wait)

    @staticmethod
    def _write_tombstone(info: SubagentInfo, cause: str) -> None:
        """Best-effort tombstone write for abnormal exits."""
        try:

            write_tombstone(
                info.id,
                cause=cause,
                recovery_action=tombstone_recovery_action(info.id, read_state(info.id) or {}),
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

    async def _write_state_off_loop(self, info: SubagentInfo, what: str, **fields: object) -> bool:
        return await self._run_events._write_state_off_loop_impl(info, what, **fields)

    async def _run_inner(self, info: SubagentInfo, session_key: str) -> None:
        return await self._run_events._run_inner_impl(info, session_key)

    # Facades for the completion / stop-reason handling and the lane-slot
    # waits that live in subagent_manager/run.py.
    def _stop_recovery_wanted(self, info: SubagentInfo, stop: Any) -> bool:
        return self._run_events._stop_recovery_wanted_impl(info, stop)

    async def _yield_for_stop_recovery(self, info: SubagentInfo, event: Any) -> str | None:
        return await self._run_events._yield_for_stop_recovery_impl(info, event)

    async def _await_lane_resume(
        self, info: SubagentInfo, *, reason: str, timeout: float, request: bool = True
    ) -> bool:
        return await self._run_events._await_lane_resume_impl(
            info, reason=reason, timeout=timeout, request=request
        )

    async def _yield_for_dependency(self, info: SubagentInfo, signal: Any) -> bool:
        return await self._run_events._yield_for_dependency_impl(info, signal)

    async def _yield_for_infra_retry(self, info: SubagentInfo, infra: Any) -> str | None:
        return await self._run_events._yield_for_infra_retry_impl(info, infra)

    def _dependency_coordinator(self) -> Any:
        return self._monitor._dependency_coordinator_impl()

    def dependency_coordinator(self) -> Any:
        """The manager's ONE ``DependencyCoordinator`` (or None without a store).

        Public seam for the gateway: it registers this process-wide so the
        main chat and the monitors read the shared ``retry_at`` per scope, and
        subscribes the runner adapters' waiters to the same schedule.
        """
        return self._dependency_coordinator()

    async def dependency_coordinator_async(self) -> Any:
        """:meth:`dependency_coordinator` for an event-loop caller.

        The FIRST build runs ``rebuild()`` over every waiting row, so a loop
        caller that may be the first one takes it on the store's writer thread.
        """
        await self._admission.ensure_coordinator_async()
        return self._dependency_coordinator()

    def _taskq_pump(self) -> None:
        self._monitor._taskq_pump_impl()

    def _stop_error_text(self, info: SubagentInfo, stop: Any, event: Any) -> str:
        return self._run_events._stop_error_text_impl(info, stop, event)

    def _taskq_note_stop_recovery(self, info: SubagentInfo, data: dict[str, Any]) -> None:
        self._run_events._taskq_note_stop_recovery_impl(info, data)

    def _should_use_session_sharing(self, info: SubagentInfo) -> bool:
        return self._run_events._should_use_session_sharing_impl(info)

    async def _create_shared_session(
        self, info: SubagentInfo, session_key: str, agent: str
    ) -> "LLMProvider":
        return await self._run_events._create_shared_session_impl(info, session_key, agent)

    # Facades for the session-start gate's late-adoption path;
    # implementations live in run.py.
    async def _await_late_start(
        self, info: SubagentInfo, session_key: str, exc: Exception
    ) -> "LLMProvider":
        return await self._run_events._await_late_start_impl(info, session_key, exc)

    async def _bind_shared_handle(
        self, info: SubagentInfo, session_key: str, runtime: "AcpRuntime", handle: Any
    ) -> "LLMProvider":
        return await self._run_events._bind_shared_handle_impl(info, session_key, runtime, handle)

    def _get_parent_runtime(self, parent_session_key: str) -> "AcpRuntime | None":
        return self._run_events._get_parent_runtime_impl(parent_session_key)

    @staticmethod
    def _is_cc_provider(provider: object) -> bool:
        """Check if a provider routes to Claude Code.

        Matches both the (dead) standalone ``ClaudeCodeProvider`` and the
        real default backend ``AcpProvider(acp_backend="claude")``.  The
        latter is what ``_sessions.get_or_create`` actually returns for the
        ``claude_code`` provider, so detecting it here is what makes the
        session-file cleanup target ``~/.claude`` instead of ``~/.kiro``.

        Asks ``SessionCapabilities.provider_seam`` through
        :func:`~kiro_crew.agent_sdk.capabilities.capabilities_of`, which replaced a
        lazy ``from kiro_crew.providers.acp import is_claude_backend``. The import
        was lazy because ``providers.acp`` sits in a providers -> session cycle;
        the SDK is in no cycle, so this one can live at module scope. The calling
        convention is unchanged: a shape that is not a provider answers False,
        which is what the old predicate's ``isinstance`` gate bought.
        """
        if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
            return True
        return capabilities_of(provider).provider_seam == PROVIDER_CLAUDE_CODE

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
        task: "asyncio.Task | asyncio.TimerHandle",  # type: ignore[type-arg]
        info: "SubagentInfo | None" = None,
        *,
        reason: str,
    ) -> None:
        """The single sanctioned chokepoint for INTENTIONALLY cancelling a
        manager-owned subagent task or admission-retry timer.

        Enforces the intentional-cancel contract mechanically instead of by
        docstring: a managed run's terminal marker MUST already be visible
        before the cancel is issued (``info.user_stopped`` / ``info.reaped`` /
        ``info.done`` / ``self._shutting_down``), otherwise ``_run``'s
        CancelledError arm classifies the cancel as unexpected and auto-respawns
        the run — a zombie respawn of work this call site meant to kill. The
        manager-owned retry timers and follow-up watchers have no run recovery
        arm; identity with their manager fields is their marker. A source-scan
        test asserts every raw ``.cancel()`` on these objects routes through here.

        Missing marker → loud error + the recovery budget is consumed
        defensively (``_cancel_retry_used``) so a mis-marked intentional
        cancel can never zombie-respawn; the cancel still proceeds.
        """
        retry_timer = any(
            task is timer
            for timer in (
                getattr(self, "_boundary_cancel_retry_handle", None),
                getattr(self, "_retained_claim_retry_handle", None),
            )
        )
        followup_watcher = any(
            task is watcher for watcher in getattr(self, "_followup_watchers", {}).values()
        )
        marked = (
            retry_timer
            or followup_watcher
            or self._shutting_down
            or (info is not None and (info.user_stopped or info.reaped or info.done))
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

    def _unqueue(self, agent_id: str, **kwargs: Any) -> dict | None:
        return self._cancellation._unqueue_impl(agent_id, **kwargs)

    def _report_queued_stop(self, params: dict) -> None:
        return self._cancellation._report_queued_stop_impl(params)

    async def cancel(self, agent_id: str) -> bool:
        return await self._cancellation.cancel_impl(agent_id)

    async def cancel_for_parent(self, parent_session_key: str) -> tuple[int, int]:
        return await self._cancellation.cancel_for_parent_impl(parent_session_key)

    def _revoke_boundary_owners(
        self,
        parent_session_key: str,
        boundary_owner: str,
    ) -> tuple[SubagentInfo, ...]:
        return self._cancellation._revoke_boundary_owners_impl(
            parent_session_key,
            boundary_owner,
        )

    async def cancel_for_boundary(
        self,
        parent_session_key: str,
        boundary_owner: str,
        *,
        retain_scope: bool = True,
    ) -> tuple[int, int]:
        return await self._cancellation.cancel_for_boundary_impl(
            parent_session_key,
            boundary_owner,
            retain_scope=retain_scope,
        )

    def _boundary_scope_matches(
        self,
        params: Mapping[str, Any],
        parent_session_key: str,
        boundary_owner: str,
    ) -> bool:
        return self._cancellation._boundary_scope_matches_impl(
            params,
            parent_session_key,
            boundary_owner,
        )

    def _boundary_cancellation_pending(self, params: Mapping[str, Any]) -> bool:
        return self._cancellation._boundary_cancellation_pending_impl(params)

    def boundary_cancellation_pending_reason(
        self,
        parent_session_key: str,
        boundary_owner: str,
    ) -> str:
        return self._cancellation.boundary_cancellation_pending_reason_impl(
            parent_session_key,
            boundary_owner,
        )

    def _schedule_boundary_cancel_retry(self) -> None:
        return self._cancellation._schedule_boundary_cancel_retry_impl()

    def _apply_boundary_cancelled_rows(
        self,
        parent_session_key: str,
        boundary_owner: str,
        cancelled: list[dict],
        *,
        settled: bool,
    ) -> int:
        return self._cancellation._apply_boundary_cancelled_rows_impl(
            parent_session_key,
            boundary_owner,
            cancelled,
            settled=settled,
        )

    async def _settle_boundary_queue(
        self,
        parent_session_key: str,
        boundary_owner: str,
    ) -> int:
        return await self._cancellation._settle_boundary_queue_impl(
            parent_session_key,
            boundary_owner,
        )

    async def retry_pending_boundary_cancellations(self) -> None:
        return await self._cancellation.retry_pending_boundary_cancellations_impl()

    def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
        """Run ids under *parent_session_key*, taken with no await. Parent-end use.

        Marks them as teardown-cancelled in the same synchronous step. The mark is what
        stops a terminal report from injecting into the retired parent, and a run can
        finish on its own during the provider-teardown awaits that follow — so marking
        later, when the cancel actually runs, is too late for exactly the runs whose
        report is already on its way.
        """
        return self._cancellation.snapshot_teardown_children_impl(parent_session_key)

    async def cancel_for_teardown(
        self,
        agent_ids: "Sequence[str]",
        *,
        parent_session_key: str,
        verb: str = "",
    ) -> int:
        """Stop the snapshotted runs without reporting them to a retired parent.

        ``parent_session_key`` is carried so the teardown's one audit line can name the
        conversation whose runs these were; the ids themselves come from the snapshot,
        which is the only reading of them that cannot drift.
        """
        return await self._cancellation.cancel_for_teardown_impl(
            agent_ids,
            parent_session_key=parent_session_key,
            verb=verb,
        )

    async def cancel_all(self) -> None:
        return await self._cancellation.cancel_all_impl()


# Component implementations deliberately resolve globals through this module:
# existing integrations patch ``kiro_crew.subagent.*`` after manager creation.
_COMPONENT_GLOBAL_BINDINGS = (
    AcpSessionProvider,
    Any,
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    FALLBACK_CANDIDATE_ATTEMPTS,
    FALLBACK_STORY_ATTR,
    FallbackState,
    HOOK_EVENT_POST_TOOL_USE,
    KiroCrewConfig,
    LLMEvent,
    LivenessOracle,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    PROVIDER_LABEL_DEFAULT,
    Path,
    SUBAGENT_COMPLETION_PREFIX,
    Stats,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    TRANSIENT_RETRIES,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    _AGENT_NAME_RE,
    _SAMPLE_MAX_AGE_SECS,
    _agent_dir,
    _cleanup_session_files_sync,
    _subagents_dir,
    _ws_result_path,
    acp_error_is_transient,
    advance_fallback_candidate,
    agent_dir_for_display,
    annotate_model_fallback,
    append_cost_sample,
    append_fallback_story,
    cap_buckets,
    apply_completion_keep,
    asyncio,
    cached_admission_check,
    cap_result_file,
    clear_tombstone,
    compact_cost_log,
    configured_fallback_chain,
    _cost_bucket,
    consult_offloaded,
    cost_log_identity,
    create_agent_folder,
    learned_cost_for,
    read_learned_costs,
    read_learned_costs_checked,
    evict_completed_agents,
    extract_options,
    fire_tool_hooks,
    hook_gate_kwargs,
    identity_grant_covers_child,
    has_dashboard_surface,
    list_orphans,
    maintenance_executor,
    mark_delivered,
    name_grant,
    os,
    platform_compat,
    provider_fallback_active,
    prune_stale_tombstones,
    read_state,
    redact_credentials,
    redact_exfiltration_urls,
    run_in_embed_pool,
    sel,
    single_completion_meta,
    stage_boundary_owner_for_run,
    subprocess_executor,
    time,
    transient_retry_delay,
    update_state,
    window_for_provider_client,
    write_result_chunk,
    write_tombstone,
)
_MANAGER_COMPONENTS = (
    OrphanStallMonitor,
    TerminalCoordinator,
    SpawnAdmissionCoordinator,
    ContinuationCoordinator,
    WaveDigestCoordinator,
    RunEventCoordinator,
    CancellationCoordinator,
)
bind_component_globals(_MANAGER_COMPONENTS, globals())
copy_component_docs(SubagentManager, _MANAGER_COMPONENTS)
