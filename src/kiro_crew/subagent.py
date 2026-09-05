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
from collections.abc import Awaitable, Callable, Container, Iterable
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
    boottime_now,
    consult_offloaded,
)
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import PROVIDER_LABEL_CLAUDE, PROVIDER_LABEL_DEFAULT
from kiro_crew.executors import run_in_embed_pool

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.providers.base import LLMProvider

from kiro_crew import name_grant, platform_compat
from kiro_crew.agent_discovery import cached_project_agent_names, list_agents
from kiro_crew.config.loader import DEFAULT_MODEL, KiroCrewConfig
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
from kiro_crew.effort import effort_settings_key, model_supports_effort
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    fire_tool_hooks,
    safe_read_file,
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
    append_cost_sample,
    compact_cost_log,
    read_learned_cost,
)
from kiro_crew.subagent_manager import (
    CancellationCoordinator,
    ContinuationCoordinator,
    OrphanStallMonitor,
    RunEventCoordinator,
    SpawnAdmissionCoordinator,
    TerminalCoordinator,
    WaveDigestCoordinator,
    bind_component_globals,
    copy_component_docs,
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

#: Agent names a roster never suggests: the host default and the conductors are
#: reached by OMITTING ``agent``, not by naming one. Every roster inherits this
#: as :func:`visible_agent_names`' default ``exclude``, so no other module names
#: the set and it cannot drift when a reserved name appears.
UNADVERTISED_AGENTS = frozenset({"kirocrew", "kirocrew-conductor", "kirocrew-pipeline-conductor"})

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
    tools' parameter descriptions, and ``spawn_list``'s output -- and each one
    used to re-implement the pipeline below. The duplication had already drifted
    once, so the SAFETY half lives here where a fourth surface cannot omit it:

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
    RETURNED error is what left the caller unable to self-correct: it retried
    other invented names while every log line already held the answer, and the
    log is not a surface the caller can read (#4842).

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


def _redact_and_truncate(text: str, max_chars: int) -> str:
    """Redact over the FULL text, then truncate (never ``_redact(x[:n])``).

    Truncating first can cut a credential in half at the boundary, leaving a
    fragment the redaction regexes no longer match — the raw remainder would
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
# Max seconds a cancelled run holds cancellation open for an in-flight off-loop
# state.json write worker (#6306 review; widened to every off-loop writer by
# #6308): long enough for any healthy fsync, short enough that a wedged FS
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


def _spawn_effective_model(model: str, agent: str) -> str:
    """The model the provider factory's effort gate will actually see, or ``""``.

    Not a re-encoding of the factory's precedence — the selection itself is
    :meth:`KiroCrewConfig.acp_effective_model`, the same function the factory
    calls, so this verdict cannot drift from the gate it reports on. What this
    wrapper reproduces is only the CALLER side of the chain, exactly as the
    spawn path drives ``get_or_create``: the kwarg the spawn passes (explicit
    per-spawn *model*, else the subagent role pin — see ``_run_inner``, which
    forwards raw ``info.model`` including an explicit ``"auto"``), and, when no
    kwarg is passed, ``session._session_model`` for *agent* (a crew's own pin,
    else non-sentinel global; ``None`` for a named kiro agent so the factory
    resolves the agent's own JSON pin — which ``acp_effective_model`` then
    does, identically). Never raises; ``""`` on any resolution failure.
    """
    try:
        # circular imports (config.loader / session import sibling modules at
        # load time, matching the lazy-import convention of _subagent_default_*)
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import _session_model

        # The kwarg the spawn path actually passes (see _run_inner): raw
        # info.model — an explicit "auto" flows through VERBATIM and the
        # factory treats it as a truthy override — else the role pin.
        override: str | None = model or _subagent_default_model() or None
        cfg = KiroCrewConfig.load()
        if override is None:
            # No kwarg: get_or_create resolves the session chain and passes
            # its result (possibly None) as model_override.
            override = _session_model(cfg, agent or None)
        return cfg.acp_effective_model(agent or None, override) or ""
    except Exception:
        return ""


def effort_drop_reason(model: str, reasoning_effort: str, agent: str = "") -> str:
    """Why a requested per-spawn effort will not take effect, or ``""``.

    Mirrors the model resolution the provider factory's effort gate actually
    sees (explicit per-spawn model, else the subagent role pin, else the
    session-level chain for *agent*: crew pin, else non-sentinel global) — an
    unresolved model (the provider picks a served one, or a named kiro agent's
    own pin resolves downstream) cannot carry an effort level through the
    overlay. Returns a human-readable reason when *reasoning_effort* is set
    but the resolved model is not effort-capable; ``""`` means the effort will
    be delivered (or none was requested). Reporting-only: never raises and
    never influences whether or how a spawn proceeds.
    """
    if not reasoning_effort:
        return ""
    resolved = _spawn_effective_model(model, agent)
    if not resolved:
        return (
            "no concrete model is pinned — the model resolves to 'auto', which "
            "does not support effort configuration; pass an effort-capable "
            "model= to apply the level"
        )
    if not model_supports_effort(resolved):
        return f"model '{resolved}' does not support effort configuration"
    return ""


def effort_applied_note(model: str, reasoning_effort: str, agent: str = "") -> str:
    """The delivery mirror of :func:`effort_drop_reason`, or ``""``.

    Names the resolved model and the family-specific cli.json settings key the
    level is delivered under (``reasoning`` for GPT, ``output_config`` for
    Claude) when a requested per-spawn effort WILL take effect. The key matters
    because kiro-cli silently ignores a level written under the wrong family
    key, so a bare "applied" would leave that failure mode unobservable.
    Complementary with the drop reason over a non-empty request: exactly one of
    the two is non-empty. Reporting-only, same totality contract.
    """
    if not reasoning_effort:
        return ""
    resolved = _spawn_effective_model(model, agent)
    if not resolved or not model_supports_effort(resolved):
        return ""
    return f"{resolved} → {effort_settings_key(resolved)}.effort"


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


# Process-subtree readings come from ONE shared walker,
# :func:`platform_compat.proc_subtree_sample`. RSS, CPU and the two counts used
# to be three walks here carrying two copies of one 256 ceiling, and a fourth
# copy of the same walk lived in ``mcp_gateway.pool``; the walk now has a single
# home above both callers (#6096), so a ceiling or a sentinel can no longer
# drift between them.


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


def _subtree_cpu_jiffies(pid: int) -> int:
    """Sum utime+stime across ``pid`` and its descendants (clock ticks).

    Asks the shared walker for the CPU reading alone, so the CPU subtree the
    Sessions session rows read is the same subtree the task rows describe, and
    the session rows pay no ``status`` read for an RSS figure they do not use.
    """
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
                   then clamped by cgroup headroom so a container's limit binds.
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
    # True for work accepted behind the stagger/concurrency gate but never
    # started. The record returned by ``spawn`` is normally not registered in
    # ``_agents``; a queued stop registers a synthetic copy temporarily so batch
    # settlement can observe it, and keeps this discriminator True so running
    # cancellation and live-resource monitoring do not treat it as executing.
    queued: bool = False
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
    # True when the gateway QUEUED this completion's injection because the
    # parent's slot was busy. Delivery is not consumption: the announce sits in
    # the slot queue until a turn drains it, and that wait is bounded only by the
    # turn ceiling — far longer than agent.subagent_result_ttl_secs. The run loop
    # must therefore SKIP mark_delivered() (a "delivered" tombstone starts the
    # retention clock, so the reaper would prune result.txt while the promise of
    # it is still queued, and the parent would be handed a dead path). The drain
    # settles the tombstone instead — see
    # ``_ChatSlot.take_pending_subagent_deliveries`` (issue #4839).
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
    # model-pinned review's actual model is auditable (issue #3582).
    resolved_model: str = ""
    # The EFFECTIVE requested model — the per-spawn pin (``model``) OR, when that
    # is empty, the ``agent.role_models['subagent']`` config pin (AGENTS.md names
    # the config pin as *the* way to pin a subagent model). This is the side the
    # downgrade comparison must use: a config-pinned run served a different model
    # is exactly the "unverifiable pin" this feature exists to catch, and keying
    # off the bare per-spawn ``model`` would miss it (Design review on #3582).
    # ``"auto"`` ⇒ unpinned (no per-spawn pin, no role pin — the provider picks
    # the model). Resolved once at spawn.
    requested_model: str = ""
    # Per-call reasoning-effort override (spawn_run ``reasoning_effort``).
    # Wins over the ``role_efforts['subagent']`` pin; ``""`` defers to it.
    # Like ``model``, a non-empty value forces the dedicated-process path.
    reasoning_effort: str = ""
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
    # True while a cancelled run is draining an in-flight off-loop state.json
    # write worker (#6306; every off-loop writer since #6308). _run's
    # unexpected-cancel recovery gate reads it: on Python 3.10 a second outer
    # cancel can deliver that gate BEFORE the drain finishes (wait_for's
    # _cancel_and_wait awaits an interruptible bare future), and scheduling a
    # recovery writer while the worker is live re-opens the stale-overwrite race
    # the drain exists to close.
    _state_drain_active: bool = False
    # Set only on the synthetic marker `_conversation_busy` returns for a
    # conversation held by an abandoned state writer (#6298 review), so the two
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
        # Run ids whose bounded state-write drain EXPIRED, so a pool worker is
        # still live and its stale whole-file rewrite would roll back the
        # retention `keep` a promote / release writes on the loop (#6298).
        # `_conversation_busy` reports these as held, which defers both retention
        # writes past the worker; each worker's own done-callback discards its id,
        # so the set holds at most one entry per live zombie. It lives on the
        # MANAGER, not on the run's SubagentInfo, because `evict_completed_agents`
        # prunes completed runs out of `_agents` and an eviction must not silently
        # release the hold.
        self._abandoned_state_writers: set[str] = set()
        # state.json is the source of truth for retention (#1115): give the
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
        # outside the run (the parent's queue drain; issue #4839), long after a
        # dashboard "clear completed" / "cancel" has popped BOTH ``_agents`` and
        # ``_tasks`` for a done-but-still-tearing-down run. Reading the gate from
        # here, rather than inferring "record gone means teardown finished", is what
        # makes that inference unnecessary. Removed by the same ``finally`` that sets
        # the event, so a missing entry always means "nothing left to wait for".
        self._teardown_gates: dict[str, asyncio.Event] = {}
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

        # The facade is the single owner of mutable registries and slot tokens.
        # Coordinators own transition logic and route every cross-boundary call
        # back through this object, preserving overrides and monkeypatch seams.
        self._monitor = OrphanStallMonitor(self)
        self._terminal = TerminalCoordinator(self)
        self._admission = SpawnAdmissionCoordinator(self)
        self._continuation = ContinuationCoordinator(self)
        self._waves = WaveDigestCoordinator(self)
        self._run_events = RunEventCoordinator(self)
        self._cancellation = CancellationCoordinator(self)

    def _effective_turn_limit(self, info: SubagentInfo) -> int:
        return self._run_events._effective_turn_limit_impl(info)

    def update_completion_keep(self, mode: str, max_chars: int) -> None:
        return self._run_events.update_completion_keep_impl(mode, max_chars)

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
        return self._terminal._claim_finalize_impl(info, supersede_recovery=supersede_recovery)

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
    ) -> None:
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
    ) -> "asyncio.Task":  # type: ignore[type-arg]
        return self._terminal._spawn_terminal_report_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
        )

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

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        return self._running_count

    def running_agents_for(self, parent_key: str) -> list[dict]:
        return self._run_events.running_agents_for_impl(parent_key)

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
        _agent_prevalidated: bool = False,
        _from_queue: bool = False,
        _preassigned_id: str = "",
    ) -> SubagentInfo | None:
        return self._admission.spawn_impl(
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
            _agent_prevalidated,
            _from_queue,
            _preassigned_id,
        )

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
        return self._continuation.continue_conversation_impl(
            conv_id, task, parent_session_key, agent, model, max_turns, cwd, _preassigned_id
        )

    def recorded_cwd(self, conv_id: str) -> str:
        return self._continuation.recorded_cwd_impl(conv_id)

    def _inherited_context_groups(self, conv_id: str) -> tuple[bool, bool, bool]:
        return self._continuation._inherited_context_groups_impl(conv_id)

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

    async def _spawn_with_approval(self, info: SubagentInfo) -> None:
        return await self._admission._spawn_with_approval_impl(info)

    def _log_spawned(self, info: SubagentInfo) -> None:
        return self._admission._log_spawned_impl(info)

    @property
    def running(self) -> list[SubagentInfo]:
        """Return currently running (not done) subagents."""
        return [a for a in self._agents.values() if not a.done]

    @property
    def all_agents(self) -> list[SubagentInfo]:
        """Return all tracked subagents (running and done)."""
        return list(self._agents.values())

    def batch_members_pending(self, batch_id: str) -> bool:
        return self._waves.batch_members_pending_impl(batch_id)

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

    def _sweep_digest_holds(self, now: float) -> None:
        return self._waves._sweep_digest_holds_impl(now)

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

    def queued_count_for(self, parent_session_key: str) -> int:
        return self._run_events.queued_count_for_impl(parent_session_key)

    def has_pending_work_for(self, parent_session_key: str) -> bool:
        return self._run_events.has_pending_work_for_impl(parent_session_key)

    def _emit_queue_depth(self, parent_session_key: str, batch_id: str = "") -> None:
        return self._run_events._emit_queue_depth_impl(parent_session_key, batch_id)

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

    async def _write_state_off_loop(self, info: SubagentInfo, what: str, **fields: object) -> bool:
        return await self._run_events._write_state_off_loop_impl(info, what, **fields)

    async def _run_inner(self, info: SubagentInfo, session_key: str) -> None:
        return await self._run_events._run_inner_impl(info, session_key)

    def _should_use_session_sharing(self, info: SubagentInfo) -> bool:
        return self._run_events._should_use_session_sharing_impl(info)

    async def _create_shared_session(
        self, info: SubagentInfo, session_key: str, agent: str
    ) -> "LLMProvider":
        return await self._run_events._create_shared_session_impl(info, session_key, agent)

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

    def _unqueue(self, agent_id: str) -> dict | None:
        return self._cancellation._unqueue_impl(agent_id)

    def _report_queued_stop(self, params: dict) -> None:
        return self._cancellation._report_queued_stop_impl(params)

    async def cancel(self, agent_id: str) -> bool:
        return await self._cancellation.cancel_impl(agent_id)

    async def cancel_for_parent(self, parent_session_key: str) -> tuple[int, int]:
        return await self._cancellation.cancel_for_parent_impl(parent_session_key)

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
    apply_completion_keep,
    asyncio,
    cached_admission_check,
    cap_result_file,
    clear_tombstone,
    compact_cost_log,
    configured_fallback_chain,
    consult_offloaded,
    create_agent_folder,
    evict_completed_agents,
    extract_options,
    fire_tool_hooks,
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
    subprocess_executor,
    time,
    transient_retry_delay,
    update_state,
    uuid,
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
