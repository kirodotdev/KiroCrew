"""Session manager — maps conversation session keys to LLM provider sessions.

Each conversation (channel thread, dashboard slot, CLI) gets its own
LLMProvider instance. Sessions are
cleaned up after idle timeout (default 60 min).

Warm session pool: ``start_pool()`` pre-spawns kiro-cli processes so
``get_or_create()`` returns instantly.  After handing out a warm session,
a replacement is created in the background to maintain the target count.

Background session: ``BACKGROUND_KEY`` is a persistent shared session for
lightweight background work (heartbeat, lesson extraction).  It
stays alive between uses, serialized by the per-session semaphore.

At >= ``cfg.session.autocompact_pct`` context usage, fires a background
compaction task. Both backends compact **in place** so the session — and
any queued or agentic work on it — continues without a user nudge:

* **kiro-cli:** run ``/compact`` in place under the session semaphore
  (native command execute + ``_kiro.dev/compaction/status`` wait). The
  process and session ID survive; the conversation is summarized in
  place. If the in-place compact fails or times out, fall back to the
  legacy **recycle**: kill the session and let the next user message
  re-seed context via ``build_session_context()`` (the stale resume SID is
  cleared while the session-map entry and channel linkage remain). A recycle is never
  forced through a live turn — if the turn semaphore cannot be acquired,
  the attempt is skipped and re-triggered at the next turn end.
* **claude-agent-acp:** run ``/compact`` in place under the session
  semaphore. The SDK preserves the same session ID across the
  compact_boundary; the session keeps its summary and continues without
  a recycle.

A failed compact records a per-key cooldown so a broken /compact does
not fire on every subsequent turn. The compact callback fires on both
success and failure; the dashboard uses ``success`` to choose the
banner copy. The user's response is never blocked — compaction is
fire-and-forget.

Circuit breaker: after 5 consecutive failures on a session, the session
is force-reset instead of retrying forever.

Per-session semaphore: serializes prompts on the same session key so
concurrent messages on the same conversation don't interleave.

Process Sweep Architecture
--------------------------
Four mechanisms clean up processes. They are complementary — not redundant.

1. ``cleanup_orphaned_sessions()`` — **startup + shutdown only**.
   Reads ``kiro_session_pids.txt`` (bare sandbox root PIDs from the previous
   gateway run). Validates each with ``_is_managed_agent_process``, kills descendants
   bottom-up, then kills the root. Truncates the file afterward.
   *Cannot be replaced by the periodic sweep* because sandbox roots are
   independent processes with no idle timeout — they survive indefinitely
   unless explicitly killed.

2. ``_cleanup_orphaned_mcp_servers()`` — **periodic** (every ~5 min).
   Reads ``kiro_pids.txt`` (child:parent pairs). Kills children whose parent
   is confirmed dead. PPid-based reuse guard prevents killing recycled PIDs.
   Also prunes dead bare PIDs. *Depends on (1)* — children are only orphaned
   after their sandbox root is killed.

3. ``_expire_idle()`` — **periodic** (every ~5 min).
   Kills sessions idle for >``timeout_secs`` (default 60 min) via
   ``reset()`` → ``provider.shutdown()`` → SIGKILL process tree.
   Protected keys: ``_PERSISTENT_KEYS`` (``_bg`` and ``_hb``).
   **Known limitation**: ``last_used`` is only bumped on ``get_or_create()``,
   not on every LLM round-trip. A task runner step doing continuous work for
   >60 min without a new ``get_or_create()`` call could be swept. This is
   accepted for now to prevent runaway tasks, but may need a heartbeat or
   persistent-key mechanism if longer steps become common.

"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle

from kiro_crew import model_registry, platform_compat, shutdown_event
from kiro_crew.acp.client import advertised_model_ids, model_is_unusable
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_DEFAULT,
)
from kiro_crew.acp_backends import selectable_backends
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import _read_agent_spec, spec_model
from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CONTEXT_WARN_MARGIN_PCT,
    POOL_SIZE_MAX,
    build_provider_factory,
    default_project_dir,
    normalize_agent_model,
    published_autocompact_pct,
)
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.mcp_gateway.abort import schedule_abort
from kiro_crew.messaging.link import (
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    ChannelLink,
    canonical_key,
    legacy_key,
    telemetry_channel_of,
)
from kiro_crew.metrics.events import SESSION_IDLE_EXPIRED, emit_counter
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.providers.base import CancelOutcome, LLMProvider
from kiro_crew.pycache_gc import PYCACHE_GC_INTERVAL_SECS, prune_pycache
from kiro_crew.sandbox import cleanup_stale_sandbox_profiles
from kiro_crew.sel import sel
from kiro_crew.session_allocation import (
    AllocationConstants,
    AllocationDeps,
    SessionAllocationService,
)
from kiro_crew.session_allocation import SessionBusyError as SessionBusyError  # noqa: F401
from kiro_crew.session_allocation import (
    SessionClosingError,
    SessionRegistryState,
)
from kiro_crew.session_allocation import (  # noqa: F401
    SpeculativeResumeRefused as SpeculativeResumeRefused,
)
from kiro_crew.session_allocation import (
    _collect_parent_runtime_kwargs,
)
from kiro_crew.session_background import (
    BackgroundRuntimeDeps,
    BackgroundSessionRuntime,
    _ProviderBgSession,
)
from kiro_crew.session_cleanup import CleanupDeps, CleanupState, SessionCleanup
from kiro_crew.session_compaction import (
    CompactionCoordinator,
    CompactionDeps,
    CompactionState,
)
from kiro_crew.session_lifecycle import (
    SessionLifecycleConstants,
    SessionLifecycleDeps,
    SessionLifecycleService,
    SessionLifecycleState,
)
from kiro_crew.session_map import _kiro_sessions_dir  # noqa: F401
from kiro_crew.session_map import MIRROR_OPT_OUT_FLAG
from kiro_crew.session_map import SessionMap as SessionMap  # noqa: F401
from kiro_crew.session_map import UnbindListener, set_unbind_listener
from kiro_crew.session_pid import (
    _build_child_map,
    _cleanup_orphaned_mcp_servers,
    _collect_active_pids,
    _kill_confirmed_and_writeback,
    _periodic_pid_sweep,
    _rss_mb_from_tree,
    _sync_kill_provider,
)
from kiro_crew.session_pid import _track_child_pids as _track_child_pids  # noqa: F401
from kiro_crew.session_pid import _track_pid as _track_pid  # noqa: F401
from kiro_crew.session_pid import _track_session_pid as _track_session_pid  # noqa: F401
from kiro_crew.session_pid import _untrack_child_pids as _untrack_child_pids  # noqa: F401
from kiro_crew.session_pid import _untrack_pid as _untrack_pid  # noqa: F401
from kiro_crew.session_pid import _untrack_session_pid as _untrack_session_pid  # noqa: F401
from kiro_crew.session_pid import (
    cleanup_orphaned_session_roots,
)
from kiro_crew.session_pid import (  # noqa: F401
    cleanup_orphaned_sessions as cleanup_orphaned_sessions,
)
from kiro_crew.session_pid import (
    find_orphan_mcp_candidates,
    get_session_rss_mb,
    kill_orphan_mcps,
)
from kiro_crew.session_pool import WarmPoolDeps, WarmSessionPool
from kiro_crew.stats import Stats
from kiro_crew.watchdog import CleanupHook, SessionWatchdog

# The standalone ClaudeCodeProvider was removed in the KiroACP-only refactor;
# Claude Code is now driven through the ACP adapter instead. The name is kept
# (always None) so the legacy ``ClaudeCodeProvider is not None and
# isinstance(...)`` guards below short-circuit cleanly. The live path is
# ``_is_claude_backend``, on a harness the public build ships as selectable.
ClaudeCodeProvider = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def _is_claude_backend(provider: Any) -> bool:
    """Check if a provider drives claude-agent-acp via the ACP adapter.

    Returns True when an AcpProvider wraps claude-agent-acp (backend="claude"),
    which the public build offers: ``BASELINE_SELECTABLE_BACKENDS`` includes it, so
    the factory selects it whenever an operator has chosen it and the adapter is
    installed.

    The ``isinstance`` gate and the backend-string read stay here -- they are
    what makes this predicate mock-safe, and the shape they read is the
    provider's own business. Only the comparison is delegated to
    ``agent_sdk.backend_identity``, so this and ``AcpProvider.is_claude_backend``
    cannot disagree about which string counts as the claude backend.
    """
    from kiro_crew.providers.acp import AcpProvider  # circular import: providers -> session

    if not isinstance(provider, AcpProvider):
        return False
    backend = getattr(provider.client, "backend", "")
    return is_claude_backend_name(backend)


def _provider_label(provider: Any) -> str:
    """Backend identity key for *provider* — see ``providers.acp.provider_label``.

    Deferred import for the same reason ``_is_claude_backend`` defers it.
    """
    from kiro_crew.providers.acp import provider_label  # circular: providers -> session

    return provider_label(provider)


def _is_acp_provider(provider: Any) -> bool:
    """Runtime type check kept lazy across the providers/session cycle."""
    from kiro_crew.providers.acp import AcpProvider

    return isinstance(provider, AcpProvider)


def _is_claude_provider(provider: Any) -> bool:
    """Honor the dynamically registered legacy Claude provider seam."""
    return bool(ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider))


def _load_acp_session_provider_type() -> Callable[..., LLMProvider]:
    """Load the shared-runtime provider adapter without an eager ACP edge."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    return AcpSessionProvider


def _load_acp_provider_type() -> type[LLMProvider]:
    """Load the registered-session ACP provider without an eager cycle."""
    from kiro_crew.providers.acp import AcpProvider

    return AcpProvider


def _load_child_process_helpers() -> tuple[
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
]:
    """Resolve reset's process-tree helpers at call time for patch compatibility."""
    from kiro_crew.acp.client import (
        _capture_child_records,
        _get_child_pids,
        _kill_escaped_children,
    )

    return _capture_child_records, _get_child_pids, _kill_escaped_children


def _resolve_allocation_crew_identity(
    cfg: KiroCrewConfig,
    agent: str | None,
    crew_agent: str | None,
) -> str:
    from kiro_crew.config.loader import resolve_crew_identity

    return resolve_crew_identity(cfg, agent, crew_agent)


def _load_allocation_watchdog_settings(crew: str) -> object:
    from kiro_crew.acp.session_handle import _load_watchdog_settings

    return _load_watchdog_settings(crew)


def _get_agent_model_cache() -> dict[str, tuple[str, float, float]]:
    if not hasattr(SessionManager, "_agent_model_cache"):
        SessionManager._agent_model_cache = {}
    return SessionManager._agent_model_cache


def _provider_effectively_alive(provider: Any) -> bool:
    """Whether a session's provider should be treated as live (NOT stale).

    Uses the process-level check (is_process_alive), not is_alive() which has a
    600s stale-activity threshold that falsely kills idle sessions. A CC
    per_session provider whose process has exited is still effectively alive:
    its session state is on disk and reconnects lazily on the next stream(), so
    it must not be evicted as stale.

    Used by the two post-acquire re-checks (the post-semaphore re-validate and
    the won-race re-validate). The in-lock live fast path keeps its own inline
    copy of this decision because it also evicts the stale entry and emits
    path-specific logging; that copy must stay in sync with this helper.

    Read straight off the declared ABC: ``LLMProvider.is_process_alive``
    defaults to ``is_alive()``, so every provider answers the call and no
    capability probe is needed (harness-parity H14).
    """
    alive = provider.is_process_alive()
    if (
        not alive
        and ClaudeCodeProvider is not None
        and isinstance(provider, ClaudeCodeProvider)
        and provider.connection_mode == "per_session"
    ):
        alive = True
    return alive


def _provider_uses_kiro_identity_store(provider: Any) -> bool:
    """Whether *provider*'s child authenticates from kiro-cli's identity store.

    Reads the capability the object DECLARES (harness-parity H14) rather than
    probing private attributes: ``LLMProvider`` declares it with a safe default of
    False, ``AcpProvider`` / ``AcpSessionProvider`` grant it by membership in
    ``ACP_BACKENDS_KIRO_IDENTITY_STORE``, and ``AcpRuntime`` declares the same
    property under the same name because the sweep reaches shared runtimes too.

    Fails CLOSED on anything that does not declare it -- a test double or a future
    holder is left running rather than recycled on a store it may never read.
    """

    declared = getattr(provider, "uses_kiro_identity_store", False)
    return declared is True


def detect_provider_switch(session_map: "SessionMap", session_key: str, new_provider: str) -> bool:
    """Detect if the provider for a session differs from the stored one.

    Returns True if the stored provider is set AND differs from *new_provider*
    AND a stored session ID exists. As a side effect, emits a SEL audit event
    when a switch is detected.

    This guards against attempting to resume incompatible session IDs across
    providers (kiro session IDs vs Claude Code UUIDs). Cross-provider continuity
    is achieved via KiroCrew's own history replay (build_session_replay), never
    via session_id translation.
    """
    stored_provider = session_map.get_provider(session_key) or PROVIDER_LABEL_DEFAULT
    if stored_provider == new_provider:
        return False
    # Only counts as a switch if there's actually a stored SID to discard
    stored_sid = session_map.get(session_key)
    if not stored_sid:
        return False
    sel().log_tool_invocation(
        session_key=session_key,
        agent="kirocrew",
        source="session",
        tool_name="provider_switch_detected",
        tool_kind="lifecycle",
        outcome="switch",
        metadata={
            "stored_provider": stored_provider,
            "new_provider": new_provider,
        },
    )
    logger.info(
        "Provider switch detected for %s: %s -> %s",
        session_key,
        stored_provider,
        new_provider,
    )
    return True


# Pre-warmed session pool ceiling. Aliased to the loader's POOL_SIZE_MAX (the
# single source of truth shared with the config API + load-time clamp) so the
# runtime pool cap, the API-write gate, and the loader clamp cannot drift apart.
_MAX_POOL = POOL_SIZE_MAX

# Cap on how many provider.shutdown() calls close_all runs concurrently. Each
# shutdown fans out 2-3 (potentially wedged) subprocess_executor tasks; matching
# this to the subprocess pool size (executors._MAX_SUBPROCESS_WORKERS) keeps a
# mass shutdown from enqueueing dozens of uncancellable teardown tasks at once.
_CLOSE_ALL_CONCURRENCY = 8

# Bounded window to bring in-flight prompts to a safe boundary before a gateway
# restart / Make-Live cutover tears down the kiro-cli processes (see
# SessionManager.drain_active_turns). Kept small: a restart must stay snappy, and
# a turn that will not reach a safe boundary in this window is unlikely to in any
# reasonable one — on timeout we fall through to the (SIGTERM-first) kill path.
# This is the co-operative drain the empty-response-after-Make-Live incident
# needed; the subsequent SIGTERM grace (AcpRuntime 5s / AcpClient 3s) is what
# then lets kiro-cli release its native-session lock.
_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS = 5.0

# Bound on the won-race stale-retry recursion in get_or_create. Each retry
# requires the winning session to have been recycled/reaped in the narrow
# window between our semaphore acquire and re-validate, so >1 is already
# adversarial; the cap is a safety backstop against pathological churn, never
# expected to be hit in practice.
_WON_RACE_MAX_RETRIES = 8

#: How long a turn's consumer may hold one event before the stuck_turn hook
#: reports it. Not configurable: the hook only reports, so an operator has no
#: behaviour to tune. Deliberately BELOW the cleanup loop's own tick so a park
#: worth reporting is caught on the first pass that sees it rather than the
#: second; re-reporting is prevented by latching on the park's identity, not by
#: sizing this above the tick (which is derived from `session.timeout_secs` and
#: so is not a fixed number to sit above).
_STUCK_TURN_REPORT_SECS = 300.0

_SUBAGENT_PREFIX = "subagent:"
_CHANNEL_PREFIX = "channel:"
_SIDE_PREFIX = "side:"

#: Every value the ``kirocrew.session.pool.decision`` counter can report. A
#: warm-pool claim either happens or is refused for exactly one reason; keeping
#: the set closed keeps the metric's cardinality bounded.
POOL_DECISIONS: frozenset[str] = frozenset(
    {
        "hit",
        "miss_empty",
        "bypass_resume",
        "bypass_stateless",
        "bypass_cwd",
        "bypass_effort",
        "bypass_env",
        "disabled",
        "other",
    }
)

# Stateless session-key prefixes — skip resume across restarts.
_WORKFLOW_AUTHOR_PREFIX = "wf-author:"
_WORKFLOW_POOL_PREFIX = "wf-pool:"
_STATELESS_PREFIXES = (
    "cron:",
    _SUBAGENT_PREFIX,
    "taskrunner:",
    _CHANNEL_PREFIX,
    "secretary:",
    _SIDE_PREFIX,
    # Workflow authoring sessions are one-request scratch contexts. Explicit
    # destruction reaps the provider; stateless classification additionally
    # prevents a resume lookup or map write before that teardown completes.
    _WORKFLOW_AUTHOR_PREFIX,
    # Warm workflow-pool workers (workflows/agent_pool.py) are per-run ephemeral
    # sessions reset between tasks via provider.new_conversation(); they must
    # NEVER persist a session_map entry or resume a prior transcript. Without
    # this, the pool's hard-reset fallback (new_conversation failed -> reset +
    # re-acquire) would resume the prior task's conversation via session/load,
    # leaking cross-task context — violating the pool's isolation guarantee.
    _WORKFLOW_POOL_PREFIX,
)

# Background session key — cron and lessons share this session.
# Heartbeat uses a separate key (HEARTBEAT_KEY) so it can run a tooled
# agent without forcing other background callers (chat-title, consolidator,
# taskkeeper) to load the same MCP servers.
BACKGROUND_KEY = "_bg"
# Concurrent cold starts allowed by ``_start_sem``. Named rather than inline so the
# identity sweep can ask how many starts are in flight (see
# ``_cold_starts_in_flight``): a provider inside ``start()`` has not published a PID
# yet, so the semaphore is the only evidence it exists.
_MAX_CONCURRENT_COLD_STARTS = 4
# Kiro agent the background session runs as. Named once because it is needed in
# TWO places — the provider factory call AND the ``_Session`` record — and when
# only the factory got it, ``_Session.agent`` stayed at its "" default, so every
# consumer reading ``sess.agent`` (e.g. ``runtime_pids``) saw the background
# session as agent-less.
BACKGROUND_AGENT = "kirocrew-lite"


# Backends the _bg runtime path may spawn under: runtime-capable AND
# operator-selectable. Identical sets today, so the intersection is pure
# defense-in-depth — a future runtime-capable preview harness that is not yet
# selectable must not be spawnable by the background path from a config object
# that skipped the loader's _normalize_acp_backend.
#
# Computed per call, not frozen at import: selectability lives in the
# ``acp_backends`` registry, which an edition extends during boot via
# ``register_selectable_backend`` — strictly after this module is imported. A
# module-level intersection would snapshot the baseline and permanently exclude
# a backend the operator did register.
def _bg_runtime_backends() -> frozenset[str]:
    return ACP_BACKENDS_ACP_RUNTIME & selectable_backends()


def _load_bg_runtime_types() -> tuple[Any, type[BaseException]]:
    """Load runtime types lazily across the session/acp import cycle."""
    from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead

    return AcpRuntime, AcpRuntimeDead


# Heartbeat session key — used by HeartbeatService.  Spawned with the full
# ``kirocrew`` agent so polled tasks can call read-only MCP tools (CR/ticket
# status, etc.).  Tool approval at runtime is gated by the
# ``HEARTBEAT_SAFE_TOOLS`` allowlist in ``slack/gateway.py``.
HEARTBEAT_KEY = "_hb"


# Context usage thresholds.
#
# The compaction threshold itself is NOT here — it is per-install config
# (``cfg.session.autocompact_pct``, default ``DEFAULT_AUTOCOMPACT_PCT``), read
# at ``check_context_usage``. The warning fires one
# ``CONTEXT_WARN_MARGIN_PCT`` below whatever that threshold is; see that
# constant in ``config.loader`` for why it is relative rather than absolute.
#
# Cost is why the compaction default sits below the 90.0 validation ceiling.
# Measured on a 7-day sample (808 turns), credits scale ~linearly with context
# at ~7 per 100k tokens up to about 90% of the window and then roughly double,
# so turns taken near the ceiling are the most expensive ones a session ever
# runs, and firing compaction there means paying that rate repeatedly first.

# Headroom ADDED to the outer ``asyncio.wait_for`` cap around the kiro-cli
# in-place compact, so the inner status wait can spend the FULL remaining
# ``COMPACT_WAIT_TIMEOUT_SECS`` budget and its graceful "no result"
# diagnostic still lands before the outer cap fires. Subtracting it from the
# inner wait instead would cut short a compaction completing in the final
# seconds of the shared budget.
_COMPACT_RESULT_WAIT_MARGIN_SECS = 5.0

# Minimum inner status wait even when the /compact prompt turn has consumed
# nearly the whole budget — never zero or negative, and long enough to drain
# a status notification that is already sitting in the queue.
_COMPACT_RESULT_WAIT_FLOOR_SECS = 5.0


def _compact_result_wait_secs(elapsed: float) -> float:
    """Inner deadline for the async compaction-status wait.

    The FULL remainder of the shared ``COMPACT_WAIT_TIMEOUT_SECS`` budget
    after ``elapsed`` seconds — never less, so a compaction completing in the
    final seconds of the budget is not abandoned early. The outer
    ``asyncio.wait_for`` carries ``_COMPACT_RESULT_WAIT_MARGIN_SECS`` of
    headroom on top, keeping this wait's graceful "no result" diagnostic
    reachable. Clamped to a floor so the wait can never be zero or negative.
    """
    return max(
        _COMPACT_RESULT_WAIT_FLOOR_SECS,
        COMPACT_WAIT_TIMEOUT_SECS - elapsed,
    )


# After a failed compact, suppress auto-compaction for this many seconds so a
# broken /compact does not fire on every subsequent turn.
_COMPACT_FAILURE_COOLDOWN_SECS = 60.0

# A compaction that completes but frees less than this many percentage points
# of the context window made no meaningful progress: the next turn-end check
# would re-trigger immediately and each attempt costs a real model-generated
# summarization. Such an INEFFECTIVE compaction keeps (rather than clears) the
# failure cooldown above, damping the retry loop. Measured as a drop in
# ``context_usage_pct()`` across the attempt — a drop, not "still above the
# threshold", because a legitimately good compaction of a very long turn can
# land above ``autocompact_pct`` while still having freed real headroom.
_COMPACT_MIN_EFFECT_PCT_POINTS = 5.0

# A compaction whose SETTLED verdict is ineffective (see
# _COMPACT_MIN_EFFECT_PCT_POINTS) while the confirmed reading is still AT OR
# ABOVE this percentage has not restored usable headroom: the very next turn
# re-crosses the trigger threshold, and on the task runner the next prompt
# itself may no longer fit. Such a session is reset — with its native resume
# sid cleared, so the overflowed conversation is not reloaded — instead of
# limping through compact/cooldown cycles. Promoted from the task runner's
# post-compaction verification so every compaction caller gets it (#4686).
# The escalation rides the verdict settle (not a raw re-read after compact())
# because only a settled reading has passed the measurability rules — a raw
# re-read can be unknown (kiro zeroes + flags stats) or stale (a backend that
# never reset them), and resetting on either would destroy a healthy session.
_POST_COMPACT_RESET_PCT = 95.0


class _CompactCallback(Protocol):
    async def __call__(self, key: str, pct: float, *, success: bool) -> None: ...  # noqa: E704


class _RecycleCallback(Protocol):
    async def __call__(self, key: str, *, reason: str) -> None: ...  # noqa: E704


# Circuit breaker: force-reset after this many consecutive failures
_CIRCUIT_BREAKER_THRESHOLD = 5

# Cap on remembered per-session channel notice targets. reset()/remove() evict
# their own entries, so this only bounds sessions dropped by some other path.
_MAX_ORIGIN_LINKS = 512


# Trailing ``:gen{N}`` on a session key. Matched here rather than reused from
# messaging.link because that module's copy is private to its own parser.
_GEN_SUFFIX_RE = re.compile(r"^gen\d+$")


def _opt_out_key(key: str) -> str:
    """The key an automatic-mirroring refusal is stored under.

    The durable BUCKET, never the generation-suffixed session key. The refusal is
    a preference about the CONVERSATION, not about one session — the same reason
    the per-route model choice is not keyed by session — and generations rotate
    on ``/new`` and on the configured idle/daily reset. Keyed per generation, an
    idle rotation would silently undo the user's "off" with no action on their
    part, and every rotated generation would strand its own row that pruning is
    forbidden to collect. Bucket-keyed, one conversation holds one such row.

    The suffix is stripped textually rather than through the canonical parser,
    because the shapes that most need it are the ones the parser rejects: a
    ``dm_scope="unified"`` bucket is ``unified:{agent}``, which is too short for
    the §9 grammar, so a parser-only rule would leave unified conversations keyed
    per generation — exactly the bug this function exists to prevent.
    """
    canon = canonical_key(key)
    head, sep, tail = canon.rpartition(":")
    return head if sep and _GEN_SUFFIX_RE.match(tail) else canon


# Background session recycle thresholds (more aggressive than chat compaction)
_BG_RECYCLE_PCT = 70.0  # recycle at 70% — well before overflow
_BG_BLIND_RECYCLE_PROMPTS = 40  # recycle after 40 prompts if no metadata

# TTL (seconds) for the per-agent model resolution cache. Bounds how long a
# stale resolution — especially the "auto" miss for an agent whose JSON is
# created/edited after first lookup — can survive an in-place file edit that
# does not bump the agents-dir mtime.
_AGENT_MODEL_CACHE_TTL = 30.0

# Persistent session keys — never expired by idle cleanup
_PERSISTENT_KEYS = frozenset({BACKGROUND_KEY, HEARTBEAT_KEY})

# Sentinel model values that mean "let kiro-cli resolve from agent JSON".
# When the global agent.model config is one of these, get_or_create() skips
# the model fallback so kiro-cli's own resolution path takes over.  Extend
# this set if more sentinel values are introduced (e.g. "default", "system").
_SENTINEL_MODELS = frozenset({"auto"})


def _model_fallback(per_agent_model: str, global_default: str) -> "str | None":
    """Choose the session model when the caller supplied none.

    Precedence (high → low): explicit caller model (resolved before this is
    reached) > per-agent pin > global default. When the agent pins its own
    model, return ``None`` so the provider factory defers to kiro's native
    agent-JSON resolution. Otherwise return the global default — unless it is a
    sentinel (e.g. ``"auto"``), in which case return ``None``.
    """
    if per_agent_model:
        return None
    return global_default if global_default and global_default not in _SENTINEL_MODELS else None


def _session_model(cfg: "KiroCrewConfig", agent: str | None) -> "str | None":
    """Resolve the model for a new session on *agent*, for EVERY surface.

    ``agent`` is whatever the caller passed, and callers are not consistent: the
    dashboard passes a resolved kiro template name, while Slack threads, cron
    jobs and spawned agents pass a KiroCrew agent (crew) name. Both are handled
    by trying the crew namespace first, so a crew's own ``model`` applies no
    matter which surface starts the turn. Without this, a crew pinned to one
    model in the Crews table still ran the template/global model from Slack or
    cron — the same per-surface drift this tier exists to remove.

    Returns ``None`` when nothing is pinned above the kiro layer, which leaves
    the provider factory to resolve the template pin / global itself. A crew pin
    is returned VERBATIM because the factory has no way to discover it: it never
    sees the crew name.

    Blocking I/O (globs + reads ``~/.kiro/agents/*.json``): call in an executor.
    """
    crew = cfg.agents.get(agent) if agent else None
    if crew is not None:
        crew_model = normalize_agent_model(crew.model)
        if crew_model:
            return crew_model
        # The crew defers, so continue down the chain on the template it binds.
        agent = crew.kiro_agent or agent

    per_agent_model = ""
    if agent and agent != "kirocrew":
        per_agent_model = cfg._resolve_named_agent_model(agent)
    return _model_fallback(per_agent_model, cfg.agent.model)


# Type alias for provider factory — accepts optional session key
ProviderFactory = Callable[..., LLMProvider]


def _provider_has_active_turn(provider: LLMProvider) -> bool:
    """True only if ``provider`` reports a real in-flight turn.

    Real providers implement ``has_active_turn()`` as a synchronous method that
    returns a plain ``bool``. This helper guards the shutdown-drain path against
    (a) providers that don't implement it (warm-pool doubles, minimal stubs) and
    (b) test doubles whose auto-generated attribute returns a coroutine
    (``AsyncMock``) — calling that would otherwise leak an un-awaited coroutine
    warning. Anything that is not exactly ``True`` is treated as "no active
    turn", so the drain is a strict opt-in that can never mis-fire on a double.
    """
    fn = getattr(provider, "has_active_turn", None)
    if not callable(fn):
        return False
    try:
        res = fn()
    except Exception:
        return False
    if inspect.isawaitable(res):
        # A double returned an awaitable instead of a bool — close it to avoid
        # a RuntimeWarning and treat as "no active turn".
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False
    return res is True


def _context_pct_is_unknown(provider: LLMProvider) -> bool:
    """True only if ``provider`` reports its 0% context reading as unknown.

    Mirrors :func:`_provider_has_active_turn`'s defensive shape: the probe is
    optional (stubs and warm-pool doubles need not implement it), and an
    ``AsyncMock``-style double that returns a coroutine is closed rather than
    left to raise a RuntimeWarning. Anything that is not exactly ``True`` reads
    as "the percentage is trustworthy", keeping the caller's recycle decision
    fail-quiet on a double.
    """
    fn = getattr(provider, "context_usage_unknown", None)
    if not callable(fn):
        return False
    try:
        res = fn()
    except Exception:
        return False
    if inspect.isawaitable(res):
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False
    return res is True


def _provider_has_unfinished_turn(provider: LLMProvider) -> bool:
    """True only if ``provider`` reports a native turn that has not reached its
    done boundary — INDEPENDENT of cancel state (unlike
    :func:`_provider_has_active_turn`).

    The shutdown drain filters on THIS, not ``has_active_turn``. A turn that has
    already been ``session/cancel``'d but whose native turn-done ack has not yet
    arrived reports ``has_active_turn() is False`` yet still holds kiro-cli's
    native-session lock open; killing the process now reproduces the
    empty-response-after-restart bug (#200). Reporting it as "unfinished" keeps
    it in the drain set so the ack is waited on before teardown.

    Same defensive guard as :func:`_provider_has_active_turn`: providers that
    don't implement the method (warm-pool doubles, minimal stubs) or doubles
    whose auto-generated attribute returns a coroutine (``AsyncMock``) are
    treated as "no unfinished turn", so the drain can never mis-fire on a
    double.
    """
    fn = getattr(provider, "has_unfinished_turn", None)
    if not callable(fn):
        return False
    try:
        res = fn()
    except Exception:
        return False
    if inspect.isawaitable(res):
        close = getattr(res, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False
    return res is True


StopOutcome = Literal["soft", "hard", "idle"]


class FirstTurnState(Enum):
    """One-shot first-turn observation on a ``_Session``.

    Records what the session's creator observed at provider start, for the
    first REAL claimant to consume atomically under the per-session semaphore
    (a speculative claimant reads without consuming). A single three-member
    field — not a pair of booleans — so the illegal fourth combination the
    old ``is_new``/``resumed_armed`` pair could represent by accident (a
    resume marker armed on an already-claimed session, which would silently
    skip history injection) has no spelling.

    Internal representation only: public return shapes stay
    ``(provider, is_new, resumed)``, derived via :attr:`is_new` /
    :attr:`resumed` at the return boundary.
    """

    # Session already claimed — no first-turn observation armed. The state
    # every session reaches once a real turn has consumed the observation
    # (and the state ``get_or_create``'s real creator registers, having
    # consumed its own; ``open_task_session``'s cold path instead registers
    # FRESH unconsumed, exactly as it left ``is_new`` armed before).
    NOTHING_ARMED = auto()
    # Fresh session: the first real turn injects Kiro Crew history.
    FRESH = auto()
    # Natively resumed — a speculative resume creator's ACP ``session/load``
    # restored the persisted transcript, so the first real turn must skip
    # history injection.
    RESUMED = auto()

    @property
    def is_new(self) -> bool:
        """The ``is_new`` boolean this state derives to at the return boundary."""
        return self is not FirstTurnState.NOTHING_ARMED

    @property
    def resumed(self) -> bool:
        """The ``resumed`` boolean this state derives to at the return boundary."""
        return self is FirstTurnState.RESUMED


@dataclass
class _Session:
    provider: LLMProvider
    last_used: float = field(default_factory=time.monotonic)
    # Wall-clock spawn time, for the uptime column on the session-memory surface.
    # ``last_used`` is monotonic (correct for idle math, but it has no epoch), so
    # it cannot answer "how long has this session been alive".
    created_at: float = field(default_factory=time.time)
    # One-shot first-turn observation, consumed by the first REAL claimant
    # under the per-session semaphore (a speculative claimant reads without
    # consuming). A single ``FirstTurnState`` field replacing the old
    # ``is_new``/``resumed_armed`` boolean pair, so arming a resume marker on
    # an already-claimed session is unrepresentable rather than forbidden by
    # convention. ``RESUMED`` is selected only by a SPECULATIVE creator whose
    # provider start restored the persisted transcript via ACP session/load —
    # the existing-session fast path and the won-race path otherwise report
    # ``resumed=False``, which would make the real first turn inject Kiro
    # Crew history on top of the natively-replayed transcript. Selected
    # atomically at registration; read and cleared in one consume.
    first_turn: FirstTurnState = FirstTurnState.FRESH
    # Set when an identity sweep found this session BUSY and therefore left its
    # in-flight turn alone. The child is authenticated as an account that is no
    # longer signed in, so the NEXT turn on this key must not reuse it: the
    # post-semaphore re-validate reads this and reports the session invalid, and
    # the caller's existing stale-provider path evicts it and cold starts. Default
    # False so every existing construction site is unaffected.
    retire_on_identity_change: bool = False
    prompt_count: int = 0
    consecutive_failures: int = 0
    # Bounded rather than plain: a release() call that lands on this object
    # after get_or_create() has already replaced it at the session key (see
    # SessionManager.release) must raise instead of silently pushing the
    # counter above 1, which would let a second turn acquire concurrently
    # with one still in flight.
    semaphore: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(1))
    approval_policy: str = ""  # "" (interactive) | "auto" (auto-approve all tools)
    agent: str = ""  # kiro agent name used for this session
    # Slack message queue: FIFO of (msg_ts, text, kwargs) waiting for the semaphore
    queue: deque[tuple[str, str, dict]] = field(default_factory=deque)
    # Set when this session's last turn was cancelled via soft-stop.
    # kiro-cli discards cancelled turns from its conversation log, so callers
    # must re-inject the cancelled turn (user prompt + partial assistant) as a
    # preamble on the next prompt. One-shot: consumers clear after use.
    prev_turn_cancelled: bool = False
    # Set when a provider switch is detected (e.g. kiro→CC or CC→kiro).
    # Consumed one-shot by the next prompt builder to inject history replay
    # from KiroCrew's conversation_log. Ensures replay fires exactly once
    # per switch, even if the session is reused across multiple prompts.
    provider_switch_replay: bool = False
    # Set of msg_ts values cancelled (message deleted while processing)
    cancelled: set[str] = field(default_factory=set)
    # Set after context compaction drops the session-start skill index.
    # Consumed one-shot by the next prompt builder to re-inject the skills
    # index so the model can still discover skills post-compaction.
    needs_context_reinjection: bool = False

    def adopt_provider(self, provider: LLMProvider) -> None:
        """Swap in a freshly-spawned *provider*, resetting conversation state.

        Recycling in place — rather than registering a new ``_Session`` — is what
        lets a caller already holding this session (or blocked on its semaphore)
        pick up the replacement instead of a torn-down provider: both the
        semaphore and the object identity the registry is keyed on survive.
        Everything reset below describes the OLD transcript, so carrying it onto
        a fresh provider would misreport its size or replay a preamble the new
        conversation never lost. ``agent`` and ``approval_policy`` describe the
        session's role, not its transcript, so they are kept.
        """
        self.provider = provider
        self.provider_switch_replay = False
        # The replacement provider is a fresh native session, not a resumed
        # one — a stale armed observation would make the next first turn skip
        # history injection it actually needs. Only the resume half of the
        # observation is stale: an armed fresh observation, or nothing armed,
        # describes the replacement just as well and carries over unchanged.
        if self.first_turn is FirstTurnState.RESUMED:
            self.first_turn = FirstTurnState.FRESH
        self.prompt_count = 0
        self.consecutive_failures = 0
        self.prev_turn_cancelled = False
        self.needs_context_reinjection = False
        self.created_at = time.time()
        self.last_used = time.monotonic()


def unlink_queued_temp_paths(kwargs: dict) -> None:
    """Unlink the temp files a queue entry tracks in ``image_temp_paths``.

    Queued Slack messages defer temp-image cleanup to whichever code path
    consumes the entry, so the queued turn's text can still resolve its image
    paths at dispatch time. Every path that consumes an entry — dispatch, or
    any discard (cancel, queue clear, cancelled-skip on dequeue) — must unlink
    here, or the files sit on disk until external cleanup. Already-missing
    files are ignored: a discard can benignly follow a dispatch that already
    cleaned up.
    """
    for p in kwargs.get("image_temp_paths") or []:
        try:
            os.unlink(p)
        except OSError:
            pass


def _unlink_session_queue(session: "_Session") -> None:
    """Unlink temp files for every entry still queued on a popped session.

    Every teardown path that pops a whole ``_Session`` out of ``_sessions``
    (stale-provider eviction, ``reset``, RSS recycle, ``remove``,
    ``remove_if_unclaimed``, ``destroy``, ``discard_conversation``,
    ``drain_all_providers``) discards ``session.queue`` along with it.
    Anything still sitting there never reaches ``_dispatch_queued``'s own
    cleanup — that only runs for an entry that actually gets dispatched —
    so this is the one place responsible for unlinking the images behind a
    whole-session teardown, the same way ``cancel_queued``/``clear_queue``/
    ``dequeue``'s cancelled-skip already do for a live session's own
    piecemeal discards.
    """
    for _, _, kwargs in session.queue:
        unlink_queued_temp_paths(kwargs)


class SessionManager:
    """Thread-keyed LLM provider pool with warm session pre-spawning."""

    _agent_model_cache: dict[str, tuple[str, float, float]] = {}

    def _registry_state(self) -> SessionRegistryState:
        """Return allocation state, lazily supporting focused ``__new__`` tests."""
        state = self.__dict__.get("_allocation_state")
        if state is None:
            state = SessionRegistryState()
            self.__dict__["_allocation_state"] = state
        return state

    def _allocation_deps(self) -> AllocationDeps:
        constants = AllocationConstants(
            max_concurrent_cold_starts=_MAX_CONCURRENT_COLD_STARTS,
            won_race_max_retries=_WON_RACE_MAX_RETRIES,
            circuit_breaker_threshold=_CIRCUIT_BREAKER_THRESHOLD,
            agent_model_cache_ttl=lambda: _AGENT_MODEL_CACHE_TTL,
            background_key=BACKGROUND_KEY,
            heartbeat_key=HEARTBEAT_KEY,
            background_agent=BACKGROUND_AGENT,
            subagent_prefix=_SUBAGENT_PREFIX,
            stateless_prefixes=_STATELESS_PREFIXES,
            provider_label_default=PROVIDER_LABEL_DEFAULT,
            provider_label_claude=PROVIDER_LABEL_CLAUDE,
        )
        return AllocationDeps(
            logger=logger,
            constants=constants,
            canonical_key=lambda key: canonical_key(key),
            legacy_key=lambda key: legacy_key(key),
            provider_has_active_turn=lambda provider: _provider_has_active_turn(provider),
            provider_effectively_alive=lambda provider: _provider_effectively_alive(provider),
            is_acp_provider=lambda provider: _is_acp_provider(provider),
            is_claude_provider=lambda provider: _is_claude_provider(provider),
            is_claude_backend=lambda provider: _is_claude_backend(provider),
            provider_label=lambda provider: _provider_label(provider),
            detect_provider_switch=lambda session_map, key, provider: detect_provider_switch(
                session_map, key, provider
            ),
            session_factory=lambda **kwargs: _Session(**kwargs),
            first_turn_nothing_armed=FirstTurnState.NOTHING_ARMED,
            first_turn_fresh=FirstTurnState.FRESH,
            first_turn_resumed=FirstTurnState.RESUMED,
            runtime_types=lambda: _load_bg_runtime_types(),
            session_provider_type=lambda: _load_acp_session_provider_type(),
            unlink_session_queue=lambda session: _unlink_session_queue(session),
            unlink_queued_temp_paths=lambda kwargs: unlink_queued_temp_paths(kwargs),
            session_model=lambda cfg, agent: _session_model(cfg, agent),
            load_config=lambda: KiroCrewConfig.load(),
            resolve_crew_identity=lambda cfg, agent, crew: _resolve_allocation_crew_identity(
                cfg, agent, crew
            ),
            load_watchdog_settings=lambda crew: _load_allocation_watchdog_settings(crew),
            advertised_model_ids=lambda models: advertised_model_ids(models),
            model_is_unusable=lambda model, advertised: model_is_unusable(model, advertised),
            to_provider_id=lambda model, provider: model_registry.to_provider_id(model, provider),
            to_acp_id=lambda model: model_registry.to_acp_id(model),
            inc_session_created=lambda: Stats().inc_session_created(),
            get_sel=lambda: sel(),
            get_subprocess_executor=lambda: subprocess_executor(),
            get_sync_kill_provider=lambda: _sync_kill_provider,
            agents_dir_path=lambda: kiro_agents_dir_path(),
            read_agent_spec=lambda path, *, operation, source: _read_agent_spec(
                path,
                operation=operation,
                source=source,
            ),
            spec_model=lambda spec: spec_model(spec),
            agent_model_cache=lambda: _get_agent_model_cache(),
        )

    def _allocation_boundary(self) -> SessionAllocationService:
        service = self.__dict__.get("_allocation")
        if service is None:
            service = SessionAllocationService(
                cast(Any, self),
                self._allocation_deps(),
                state=self._registry_state(),
            )
            self.__dict__["_allocation"] = service
        return service

    def _lifecycle_state_boundary(self) -> SessionLifecycleState:
        """Return lifecycle state, including for focused ``__new__`` tests."""
        state = self.__dict__.get("_lifecycle_state")
        if state is None:
            state = SessionLifecycleState()
            self.__dict__["_lifecycle_state"] = state
        return state

    def _lifecycle_deps(self) -> SessionLifecycleDeps:
        return SessionLifecycleDeps(
            logger=logger,
            load_config=lambda: KiroCrewConfig.load(),
            build_provider_factory=lambda cfg: build_provider_factory(cfg),
            default_project_dir=lambda: default_project_dir(),
            constants=lambda: SessionLifecycleConstants(
                max_pool=_MAX_POOL,
                max_concurrent_cold_starts=_MAX_CONCURRENT_COLD_STARTS,
                background_key=BACKGROUND_KEY,
                stateless_prefixes=_STATELESS_PREFIXES,
                close_all_concurrency=_CLOSE_ALL_CONCURRENCY,
                drain_active_turns_timeout_secs=_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS,
                unbind_reason_session_destroyed=UNBIND_REASON_SESSION_DESTROYED,
                first_turn_nothing_armed=FirstTurnState.NOTHING_ARMED,
                provider_label_claude=PROVIDER_LABEL_CLAUDE,
            ),
            get_unlink_session_queue=lambda: _unlink_session_queue,
            get_child_process_helpers=lambda: _load_child_process_helpers(),
            get_subprocess_executor=lambda: subprocess_executor(),
            get_platform_compat=lambda: platform_compat,
            get_acp_provider_type=lambda: _load_acp_provider_type(),
            get_claude_code_provider_type=lambda: ClaudeCodeProvider,
            provider_label=lambda provider: _provider_label(provider),
            provider_has_unfinished_turn=lambda provider: _provider_has_unfinished_turn(provider),
            provider_uses_kiro_identity_store=lambda provider: (
                _provider_uses_kiro_identity_store(provider)
            ),
            get_audit_logger=lambda: sel(),
            schedule_abort=lambda *args, **kwargs: schedule_abort(*args, **kwargs),
            monotonic=lambda: time.monotonic(),
        )

    def _lifecycle_boundary(self) -> SessionLifecycleService:
        service = self.__dict__.get("_lifecycle")
        if service is None:
            service = SessionLifecycleService(
                cast(Any, self),
                self._lifecycle_deps(),
                state=self._lifecycle_state_boundary(),
            )
            self.__dict__["_lifecycle"] = service
        return service

    def _cleanup_state_boundary(self) -> CleanupState:
        state = self.__dict__.get("_cleanup_state")
        if state is None:
            state = CleanupState()
            self.__dict__["_cleanup_state"] = state
        return state

    def _cleanup_deps(self) -> CleanupDeps:
        return CleanupDeps(
            logger=logger,
            get_shutdown_signal=lambda: shutdown_event,
            get_maintenance_executor=lambda: maintenance_executor(),
            get_subprocess_executor=lambda: subprocess_executor(),
            cleanup_orphaned_mcp_servers=lambda: _cleanup_orphaned_mcp_servers(),
            cleanup_orphaned_session_roots=lambda: cleanup_orphaned_session_roots(),
            cleanup_stale_sandbox_profiles=lambda: cleanup_stale_sandbox_profiles(),
            prune_pycache=lambda: prune_pycache(),
            collect_active_pids=lambda sessions: _collect_active_pids(
                cast(dict[Any, Any], sessions)
            ),
            periodic_pid_sweep=lambda gateway_pid, active_pids: _periodic_pid_sweep(
                gateway_pid,
                active_pids,
            ),
            kill_confirmed_and_writeback=lambda gateway_pid, candidates, dead: (
                _kill_confirmed_and_writeback(gateway_pid, candidates, dead)
            ),
            find_orphan_mcp_candidates=lambda active_pids: find_orphan_mcp_candidates(active_pids),
            kill_orphan_mcps=lambda candidates: kill_orphan_mcps(candidates),
            build_child_map=lambda: _build_child_map(),
            rss_mb_from_tree=lambda pid, child_map: _rss_mb_from_tree(pid, child_map),
            get_session_rss_mb=lambda pid: get_session_rss_mb(pid),
            is_windows=lambda: platform_compat.IS_WINDOWS,
            getpid=lambda: os.getpid(),
            monotonic=lambda: time.monotonic(),
            stats_factory=lambda: Stats(),
            sel_factory=lambda: sel(),
            provider_has_active_turn=lambda provider: _provider_has_active_turn(provider),
            emit_counter=lambda event, dimensions: emit_counter(event, dimensions),
            get_persistent_keys=lambda: _PERSISTENT_KEYS,
            get_channel_prefix=lambda: _CHANNEL_PREFIX,
            get_stuck_turn_report_secs=lambda: _STUCK_TURN_REPORT_SECS,
            get_pycache_gc_interval_secs=lambda: PYCACHE_GC_INTERVAL_SECS,
            get_session_idle_expired_event=lambda: SESSION_IDLE_EXPIRED,
        )

    def _cleanup_boundary(self) -> SessionCleanup:
        service = self.__dict__.get("_cleanup")
        if service is None:
            state = self._cleanup_state_boundary()
            if state.watchdog is None:
                # Construct through this module's patchable names and bind the
                # facade methods so instance monkeypatches remain observable.
                state.watchdog = SessionWatchdog(
                    [
                        CleanupHook("idle_expiry", self._expire_idle_hook),
                        CleanupHook("orphan_mcp", self._orphan_mcp_hook),
                        CleanupHook("rss_threshold", self._rss_threshold_check),
                        CleanupHook("stuck_turn", self._stuck_turn_check),
                        CleanupHook("bg_drain_reap", self._bg_drain_reap_hook),
                    ]
                )
            service = SessionCleanup(
                cast(Any, self),
                self._cleanup_deps(),
                state=state,
            )
            self.__dict__["_cleanup"] = service
        return service

    @property
    def _cleanup_task(self) -> asyncio.Task[Any] | None:
        return self._cleanup_state_boundary().cleanup_task

    @_cleanup_task.setter
    def _cleanup_task(self, value: asyncio.Task[Any] | None) -> None:
        self._cleanup_state_boundary().cleanup_task = value

    @property
    def _rss_max_mb(self) -> int:
        return self._cleanup_state_boundary().rss_max_mb

    @_rss_max_mb.setter
    def _rss_max_mb(self, value: int) -> None:
        self._cleanup_state_boundary().rss_max_mb = value

    @property
    def _idle_sweep_enabled(self) -> bool:
        return self._cleanup_state_boundary().idle_sweep_enabled

    @_idle_sweep_enabled.setter
    def _idle_sweep_enabled(self, value: bool) -> None:
        self._cleanup_state_boundary().idle_sweep_enabled = value

    @property
    def _idle_timeout(self) -> int:
        return self._cleanup_state_boundary().idle_timeout

    @_idle_timeout.setter
    def _idle_timeout(self, value: int) -> None:
        self._cleanup_state_boundary().idle_timeout = value

    @property
    def _stuck_reported(self) -> dict[str, float]:
        return self._cleanup_state_boundary().stuck_reported

    @_stuck_reported.setter
    def _stuck_reported(self, value: dict[str, float]) -> None:
        self._cleanup_state_boundary().stuck_reported = value

    @property
    def _last_pycache_gc(self) -> float | None:
        return self._cleanup_state_boundary().last_pycache_gc

    @_last_pycache_gc.setter
    def _last_pycache_gc(self, value: float | None) -> None:
        self._cleanup_state_boundary().last_pycache_gc = value

    @property
    def _active_dashboard_slots(self) -> set[str] | None:
        return self._cleanup_state_boundary().active_dashboard_slots

    @_active_dashboard_slots.setter
    def _active_dashboard_slots(self, value: set[str] | None) -> None:
        self._cleanup_state_boundary().active_dashboard_slots = value

    @property
    def _watchdog(self) -> SessionWatchdog:
        watchdog = self._cleanup_state_boundary().watchdog
        if watchdog is None:
            return self._cleanup_boundary()._watchdog
        return watchdog

    @_watchdog.setter
    def _watchdog(self, value: SessionWatchdog) -> None:
        self._cleanup_state_boundary().watchdog = value

    @property
    def _identity_sweep_lock(self) -> asyncio.Lock:
        return self._lifecycle_state_boundary().identity_sweep_lock

    @_identity_sweep_lock.setter
    def _identity_sweep_lock(self, value: asyncio.Lock) -> None:
        self._lifecycle_state_boundary().identity_sweep_lock = value

    @property
    def _recycling(self) -> dict[str, "_Session"]:
        return self._lifecycle_state_boundary().recycling  # type: ignore[return-value]

    @_recycling.setter
    def _recycling(self, value: dict[str, "_Session"]) -> None:
        self._lifecycle_state_boundary().recycling = cast(Any, value)

    @property
    def _suppress_replay(self) -> set[str]:
        return self._lifecycle_state_boundary().suppress_replay

    @_suppress_replay.setter
    def _suppress_replay(self, value: set[str]) -> None:
        self._lifecycle_state_boundary().suppress_replay = value

    @property
    def _origin_links(self) -> dict[str, ChannelLink]:
        return self._lifecycle_state_boundary().origin_links

    @_origin_links.setter
    def _origin_links(self, value: dict[str, ChannelLink]) -> None:
        self._lifecycle_state_boundary().origin_links = value

    @property
    def _on_recycled(self) -> _RecycleCallback | None:
        return self._lifecycle_state_boundary().on_recycled

    @_on_recycled.setter
    def _on_recycled(self, value: _RecycleCallback | None) -> None:
        self._lifecycle_state_boundary().on_recycled = value

    @property
    def _sessions(self) -> dict[str, "_Session"]:
        return self._registry_state().sessions

    @_sessions.setter
    def _sessions(self, value: dict[str, "_Session"]) -> None:
        self._registry_state().sessions = value

    @property
    def _lock(self) -> asyncio.Lock:
        return self._registry_state().lock

    @_lock.setter
    def _lock(self, value: asyncio.Lock) -> None:
        self._registry_state().lock = value

    @property
    def _closing(self) -> bool:
        return self._registry_state().closing

    @_closing.setter
    def _closing(self, value: bool) -> None:
        self._registry_state().closing = value

    @property
    def _start_sem(self) -> asyncio.Semaphore:
        return self._registry_state().start_sem

    @_start_sem.setter
    def _start_sem(self, value: asyncio.Semaphore) -> None:
        self._registry_state().start_sem = value

    @property
    def _starting_pids(self) -> set[int]:
        return self._registry_state().starting_pids

    @_starting_pids.setter
    def _starting_pids(self, value: set[int]) -> None:
        self._registry_state().starting_pids = value

    @property
    def _subagent_runtimes(self) -> dict[str, "AcpRuntime"]:
        return self._registry_state().subagent_runtimes

    @_subagent_runtimes.setter
    def _subagent_runtimes(self, value: dict[str, "AcpRuntime"]) -> None:
        self._registry_state().subagent_runtimes = value

    @property
    def _subagent_runtime_locks(self) -> dict[str, asyncio.Lock]:
        return self._registry_state().subagent_runtime_locks

    @_subagent_runtime_locks.setter
    def _subagent_runtime_locks(self, value: dict[str, asyncio.Lock]) -> None:
        self._registry_state().subagent_runtime_locks = value

    @property
    def _continuable_keys(self) -> set[str]:
        return self._registry_state().continuable_keys

    @_continuable_keys.setter
    def _continuable_keys(self, value: set[str]) -> None:
        self._registry_state().continuable_keys = value

    @property
    def _continuable_fallback(self) -> Callable[[str], bool] | None:
        return self._registry_state().continuable_fallback

    @_continuable_fallback.setter
    def _continuable_fallback(self, value: Callable[[str], bool] | None) -> None:
        self._registry_state().continuable_fallback = value

    # Keep the established state-inspection seams while giving the coordinator
    # a single authoritative state object.
    @property
    def _compacting(self) -> set[str]:
        return self._compaction_state.compacting

    @_compacting.setter
    def _compacting(self, value: set[str]) -> None:
        self._compaction_state.compacting = value

    @property
    def _compact_cooldown_until(self) -> dict[str, float]:
        return self._compaction_state.cooldown_until

    @_compact_cooldown_until.setter
    def _compact_cooldown_until(self, value: dict[str, float]) -> None:
        self._compaction_state.cooldown_until = value

    @property
    def _compact_pending_verdict(self) -> dict[str, float]:
        return self._compaction_state.pending_verdict

    @_compact_pending_verdict.setter
    def _compact_pending_verdict(self, value: dict[str, float]) -> None:
        self._compaction_state.pending_verdict = value

    @property
    def _on_compacted(self) -> _CompactCallback | None:
        return self._compaction_state.on_compacted

    @_on_compacted.setter
    def _on_compacted(self, value: _CompactCallback | None) -> None:
        self._compaction_state.on_compacted = value

    @property
    def _pool_started(self) -> bool:
        return self._pool._pool_started

    @_pool_started.setter
    def _pool_started(self, value: bool) -> None:
        self._pool._pool_started = value

    @property
    def _pool_size(self) -> int:
        return self._pool._pool_size

    @_pool_size.setter
    def _pool_size(self, value: int) -> None:
        self._pool._pool_size = value

    @property
    def _pool_agent(self) -> str:
        return self._pool._pool_agent

    @_pool_agent.setter
    def _pool_agent(self, value: str) -> None:
        self._pool._pool_agent = value

    @property
    def _pool_ttl_secs(self) -> int:
        return self._pool._pool_ttl_secs

    @_pool_ttl_secs.setter
    def _pool_ttl_secs(self, value: int) -> None:
        self._pool._pool_ttl_secs = value

    @property
    def _pool_cwd(self) -> str:
        return self._pool._pool_cwd

    @_pool_cwd.setter
    def _pool_cwd(self, value: str) -> None:
        self._pool._pool_cwd = value

    @property
    def _warm_pool(self) -> asyncio.Queue[tuple[LLMProvider, float]]:
        return self._pool._warm_pool

    @_warm_pool.setter
    def _warm_pool(self, value: asyncio.Queue[tuple[LLMProvider, float]]) -> None:
        self._pool._warm_pool = value

    @property
    def _pool_fill_lock(self) -> asyncio.Lock:
        return self._pool._pool_fill_lock

    @_pool_fill_lock.setter
    def _pool_fill_lock(self, value: asyncio.Lock) -> None:
        self._pool._pool_fill_lock = value

    @property
    def _pool_health_task(self) -> asyncio.Task[Any] | None:
        return self._pool._pool_health_task

    @_pool_health_task.setter
    def _pool_health_task(self, value: asyncio.Task[Any] | None) -> None:
        self._pool._pool_health_task = value

    @property
    def _pool_sweep_pids(self) -> set[int]:
        return self._pool._pool_sweep_pids

    @_pool_sweep_pids.setter
    def _pool_sweep_pids(self, value: set[int]) -> None:
        self._pool._pool_sweep_pids = value

    @property
    def _bg_runtime(self) -> "AcpRuntime | None":
        return cast("AcpRuntime | None", self._background_runtime._bg_runtime)

    @_bg_runtime.setter
    def _bg_runtime(self, value: "AcpRuntime | None") -> None:
        self._background_runtime._bg_runtime = cast(Any, value)

    @property
    def _bg_runtime_lock(self) -> asyncio.Lock:
        return self._background_runtime._bg_runtime_lock

    @_bg_runtime_lock.setter
    def _bg_runtime_lock(self, value: asyncio.Lock) -> None:
        self._background_runtime._bg_runtime_lock = value

    @property
    def _draining_bg_runtimes(self) -> list["AcpRuntime"]:
        return cast(list["AcpRuntime"], self._background_runtime._draining_bg_runtimes)

    @_draining_bg_runtimes.setter
    def _draining_bg_runtimes(self, value: list["AcpRuntime"]) -> None:
        self._background_runtime._draining_bg_runtimes = cast(Any, value)

    def _fold_key(self, key: str) -> str:
        """Resolve exact, canonical, then legacy aliases onto a live key."""
        return self._allocation_boundary()._fold_key(key)

    def has_session(self, key: str) -> bool:
        """Return whether a live session exists for the folded key."""
        return self._allocation_boundary().has_session(key)

    def get_provider(self, key: str) -> LLMProvider | None:
        """Return the live provider for a folded key."""
        return self._allocation_boundary().get_provider(key)

    async def try_acquire(self, key: str) -> bool:
        """Try to acquire an exact-key idle session."""
        return await self._allocation_boundary().try_acquire(key)

    def active_providers(self) -> list[LLMProvider]:
        """Return all currently registered providers."""
        return self._allocation_boundary().active_providers()

    def any_active_turn(self) -> bool:
        """Return whether any provider reports an active turn."""
        return self._allocation_boundary().any_active_turn()

    def get_pid(self, key: str) -> int | None:
        """Return the host PID for a folded session key."""
        return self._allocation_boundary().get_pid(key)

    def __init__(
        self,
        cfg: KiroCrewConfig,
        provider_factory: ProviderFactory | None = None,
    ):
        self._cfg = cfg
        # Baseline for adopt-on-change (see _sync_autocompact_pct). Captured here
        # so a caller that hands in a config with its own threshold -- a test, or
        # any embedder constructing a manager directly -- keeps that value until
        # a LOAD publishes a different one, rather than having it replaced by
        # whatever the last load in this process happened to publish.
        self._adopted_autocompact_pct = published_autocompact_pct()
        self._provider_factory = provider_factory
        self._allocation_state = SessionRegistryState(
            start_sem=asyncio.Semaphore(_MAX_CONCURRENT_COLD_STARTS)
        )
        self._allocation_boundary()
        self._lifecycle_state = SessionLifecycleState()
        self._compaction_state = CompactionState()
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._session_map = SessionMap()

        self._pool = WarmSessionPool(
            cast(Any, self),
            WarmPoolDeps(
                logger=logger,
                default_project_dir=lambda: default_project_dir(),
                get_sync_kill_provider=lambda: _sync_kill_provider,
                get_subprocess_executor=lambda: subprocess_executor(),
                get_pid_exists=lambda: platform_compat.pid_exists,
                get_identity_predicate=lambda: _provider_uses_kiro_identity_store,
                get_discard_timeout=lambda: self._POOL_DISCARD_TIMEOUT,
                get_health_interval=lambda: self._POOL_HEALTH_INTERVAL,
                get_recorder=lambda: get_recorder(),
                telemetry_channel_of=lambda key: telemetry_channel_of(key),
                max_pool=_MAX_POOL,
                pool_decisions=POOL_DECISIONS,
            ),
        )
        # Callback fired when a session expires (idle or orphaned).
        # Used by HistoryConsolidator to trigger skill extraction.
        self.on_session_expire: Callable[[str], None] | None = None
        # Fired by the stuck_turn hook for a turn whose consumer has stopped
        # pulling events. A seam, not a policy: this class only reports, and a
        # surface that can reach the user (a dashboard notification, a Slack DM)
        # decides what to do with it. Args: (session_key, parked_secs).
        self.on_stuck_turn: Callable[[str, float], None] | None = None

        self._compaction = CompactionCoordinator(
            self,
            CompactionDeps(
                logger=logger,
                is_claude_backend=lambda provider: _is_claude_backend(provider),
                is_cc_managed=lambda provider: bool(
                    ClaudeCodeProvider is not None
                    and isinstance(provider, ClaudeCodeProvider)
                    and provider.connection_mode == "per_session"
                ),
                get_recorder=lambda: get_recorder(),
                context_pct_is_unknown=lambda provider: _context_pct_is_unknown(provider),
                unlink_session_queue=lambda session: _unlink_session_queue(session),
                compact_wait_timeout_secs=lambda: COMPACT_WAIT_TIMEOUT_SECS,
                compact_result_wait_secs=lambda elapsed: _compact_result_wait_secs(elapsed),
                context_warn_margin_pct=CONTEXT_WARN_MARGIN_PCT,
                compact_result_wait_margin_secs=_COMPACT_RESULT_WAIT_MARGIN_SECS,
                compact_failure_cooldown_secs=_COMPACT_FAILURE_COOLDOWN_SECS,
                compact_min_effect_pct_points=_COMPACT_MIN_EFFECT_PCT_POINTS,
                post_compact_reset_pct=_POST_COMPACT_RESET_PCT,
            ),
            state=self._compaction_state,
        )

        self._background_runtime = BackgroundSessionRuntime(
            cast(Any, self),
            BackgroundRuntimeDeps(
                logger=logger,
                background_key=BACKGROUND_KEY,
                background_agent=BACKGROUND_AGENT,
                heartbeat_key=HEARTBEAT_KEY,
                runtime_agent=BACKGROUND_AGENT,
                acp_backend_kiro=ACP_BACKEND_KIRO,
                bg_recycle_pct=_BG_RECYCLE_PCT,
                bg_blind_recycle_prompts=_BG_BLIND_RECYCLE_PROMPTS,
                runtime_backends=lambda: _bg_runtime_backends(),
                context_pct_is_unknown=lambda provider: _context_pct_is_unknown(provider),
                runtime_types=lambda: _load_bg_runtime_types(),
                session_factory=lambda **kwargs: _Session(**kwargs),
                first_turn_nothing_armed=FirstTurnState.NOTHING_ARMED,
                provider_bg_session_factory=lambda session: _ProviderBgSession(session),
                session_closing_error=lambda message: SessionClosingError(message),
            ),
        )
        self._lifecycle_boundary()

        _rss_cfg = getattr(cfg.session, "watchdog_rss_max_mb", 0)
        self._cleanup_state = CleanupState(
            rss_max_mb=max(0, _rss_cfg) if isinstance(_rss_cfg, int) else 0,
        )
        self._cleanup_boundary()

    def _ensure_cleanup_task(self) -> None:
        """Start the one cleanup loop at the allocation registration point."""
        self._cleanup_boundary().start_cleanup()

    async def refresh_defaults(self) -> None:
        """Adopt changed defaults for new sessions without touching live sessions."""
        await self._lifecycle_boundary().refresh_defaults()

    def _sync_autocompact_pct(self) -> None:
        """Adopt a newly published compaction threshold, if one arrived.

        The threshold is captured on ``_cfg`` when the gateway starts, so a
        config write used to reach disk and stop there. Every successful
        ``KiroCrewConfig.load`` now publishes it, and prompt assembly loads
        config once per turn, so a write from ANY writer -- the dashboard PATCH
        handler or ``kirocrew config set`` -- is in force by the next context
        reading without a restart.

        Adopt-on-CHANGE rather than unconditional assignment: a manager
        constructed with a config that carries its own threshold must keep it, so
        only a value that differs from the last one this manager adopted wins.
        That also makes the sync idempotent, which matters because the gate calls
        it on every reading.

        Reads a module-level snapshot, never config.json -- this runs on the
        event loop, where a stat/read/validate is exactly what the publish idiom
        exists to avoid.
        """
        published = published_autocompact_pct()
        if published != self._adopted_autocompact_pct:
            self._adopted_autocompact_pct = published
            self._cfg.session.autocompact_pct = published

    async def reload_provider_factory(self) -> None:
        """Rebuild the provider factory and retire sessions created by the old one."""
        await self._lifecycle_boundary().reload_provider_factory()

    # ── Background Session ──

    async def start_pool(self, *, blocking: bool = True) -> None:
        """Delegate background and warm-pool startup."""
        await self._pool.start_pool(blocking=blocking)

    async def _ensure_background(self) -> None:
        """Delegate creation of the persistent background session."""
        await self._background_runtime._ensure_background()

    # ── Warm Pool ──

    def _configured_bg_backend_raw(self) -> str | None:
        """Return the configured background backend when readable."""
        return self._background_runtime._configured_bg_backend_raw()

    def _configured_bg_backend(self) -> str:
        """Return the backend background runtimes should use."""
        return self._background_runtime._configured_bg_backend()

    def _bg_backend_supports_runtime(self) -> bool:
        """Return whether the configured backend supports a shared runtime."""
        return self._background_runtime._bg_backend_supports_runtime()

    async def _reap_drained_bg_runtimes_locked(self) -> None:
        """Delegate reaping of drained displaced runtimes."""
        await self._background_runtime._reap_drained_bg_runtimes_locked()

    async def _displace_bg_runtime_locked(
        self, runtime: "AcpRuntime", cached_backend: str, configured_backend: str
    ) -> None:
        """Delegate backend-switch displacement under the runtime lock."""
        await self._background_runtime._displace_bg_runtime_locked(
            cast(Any, runtime), cached_backend, configured_backend
        )

    async def _retire_stale_backend_bg_runtime(self) -> None:
        """Delegate stale-backend runtime retirement."""
        await self._background_runtime._retire_stale_backend_bg_runtime()

    async def _provider_backed_bg_session(self) -> "_ProviderBgSession":
        """Return the serialized provider-backed background adapter."""
        return cast(
            "_ProviderBgSession",
            await self._background_runtime._provider_backed_bg_session(),
        )

    async def get_bg_session(self) -> "AcpSessionHandle | _ProviderBgSession":
        """Acquire a background handle from the configured runtime shape."""
        return cast(
            "AcpSessionHandle | _ProviderBgSession",
            await self._background_runtime.get_bg_session(),
        )

    async def get_subagent_runtime(
        self, parent_session_key: str, agent: str | None = None
    ) -> "AcpRuntime":
        """Get or spawn the shared companion runtime for a parent."""
        return await self._allocation_boundary().get_subagent_runtime(
            parent_session_key, agent=agent
        )

    async def release_subagent_runtime(self, parent_session_key: str) -> None:
        """Release the shared companion runtime for a parent."""
        await self._allocation_boundary().release_subagent_runtime(parent_session_key)

    async def _get_or_bootstrap_run_runtime(
        self, parent_session_key: str, *, agent: str | None = None, cwd: str | None = None
    ) -> "AcpRuntime":
        """Get or bootstrap a task-runner shared runtime."""
        return await self._allocation_boundary()._get_or_bootstrap_run_runtime(
            parent_session_key, agent=agent, cwd=cwd
        )

    async def _reacquire_and_validate(
        self,
        key: str,
        sess: "_Session",
        *,
        wait_if_busy: bool = True,
    ) -> bool:
        """Acquire outside the registry lock and revalidate identity."""
        return await self._allocation_boundary()._reacquire_and_validate(
            key,
            sess,
            wait_if_busy=wait_if_busy,
        )

    async def _evict_stale_session(self, key: str, sess: "_Session") -> None:
        """Evict and close the exact stale session."""
        await self._allocation_boundary()._evict_stale_session(key, sess)

    async def open_task_session(
        self,
        parent_session_key: str,
        session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
        approval_policy: str = "",
        _won_race_retries: int = 0,
    ) -> tuple[LLMProvider, bool, bool]:
        """Open a task session on its run-scoped shared runtime."""
        return await self._allocation_boundary().open_task_session(
            parent_session_key,
            session_key,
            agent=agent,
            cwd=cwd,
            approval_policy=approval_policy,
            _won_race_retries=_won_race_retries,
        )

    def _get_session_agent(self, session_key: str) -> str:
        """Return the agent recorded on an exact session key."""
        return self._allocation_boundary()._get_session_agent(session_key)

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict:
        """Return the parent runtime security and backend posture."""
        return _collect_parent_runtime_kwargs(cast(Any, self), parent_session_key)

    def is_session_sharing_eligible(self, parent_session_key: str) -> bool:
        """Return whether the exact parent can share a runtime."""
        return self._allocation_boundary().is_session_sharing_eligible(parent_session_key)

    async def _fill_warm_pool(self) -> None:
        """Delegate warm-provider creation."""
        await self._pool._fill_warm_pool()

    # Bound on the graceful shutdown attempt during a pool discard. The pool
    # health sweep is a single long-lived task: an unbounded await on a wedged
    # shutdown would freeze every future sweep, silently disabling TTL
    # enforcement for the whole pool.
    _POOL_DISCARD_TIMEOUT = 10.0

    @staticmethod
    def _dispatch_hard_kill(provider: LLMProvider) -> None:
        """Dispatch a blocking provider kill off the event loop."""
        WarmSessionPool.dispatch_hard_kill(
            provider,
            get_sync_kill_provider=lambda: _sync_kill_provider,
            get_subprocess_executor=lambda: subprocess_executor(),
        )

    async def _discard_pool_provider(self, provider: LLMProvider, context: str) -> None:
        """Delegate bounded shutdown and hard-kill fallback."""
        await self._pool._discard_pool_provider(provider, context)

    def _record_pool_decision(self, decision: str, key: str) -> None:
        """Delegate bounded-cardinality pool decision telemetry."""
        pool = self.__dict__.get("_pool")
        if pool is not None:
            pool._record_pool_decision(decision, key)
            return
        # Focused metric tests construct the facade with ``__new__``. Keep that
        # established seam without inventing a config-backed pool.
        try:
            get_recorder().counter(
                "kirocrew.session.pool.decision",
                1,
                attrs={
                    "outcome": decision if decision in POOL_DECISIONS else "other",
                    "channel": telemetry_channel_of(key),
                },
            )
        except Exception:
            logger.debug("pool decision metric emit failed", exc_info=True)

    def _claim_from_pool(self, agent: str | None) -> tuple[LLMProvider, float] | None:
        """Delegate exact-agent warm-pool claiming."""
        return self._pool._claim_from_pool(agent)

    async def _drain_and_claim(self, agent: str | None) -> LLMProvider | None:
        """Delegate stale/dead provider filtering during claim."""
        return await self._pool._drain_and_claim(agent)

    def _schedule_replenish(self) -> None:
        """Delegate owned refill-task scheduling."""
        self._pool._schedule_replenish()

    def _pool_pids(self) -> set[int]:
        """Return pooled and in-sweep provider PIDs."""
        return self._pool._pool_pids()

    def _in_flight_pids(self) -> set[int]:
        """Return start-to-registration PID shields."""
        return self._pool._in_flight_pids()

    def _companion_runtime_pids(self) -> set[int]:
        """PIDs of live AcpRuntimes NOT registered as ``self._sessions`` entries.

        Since the AcpRuntime unify (commit 0bf3b85a) every runtime records its
        PID in ``kiro_session_pids.txt`` at spawn, so the periodic orphan sweep
        treats any tracked PID it can't find in the active set as an orphan and
        SIGKILLs it (surfacing as ``process exited (rc=-9)`` mid-chat). Two
        runtime kinds live OUTSIDE ``self._sessions`` and are therefore invisible
        to ``_collect_active_pids``:

        - ``self._subagent_runtimes`` — companion runtimes multiplexing a parent
          session's subagents (alive for the parent's whole lifetime).
        - ``self._bg_runtime`` — the background runtime backing ``get_bg_session``
          (kirocrew-lite title-gen / memory consolidation), plus any
          ``_draining_bg_runtimes`` displaced by a backend switch while their
          handles finish — killing one mid-drain is exactly what parking avoids.

        All are shielded from the sweep by unioning their live PIDs into the
        active set here (mirrors ``_pool_pids``/``_in_flight_pids``). Only alive
        runtimes contribute — a dead entry SHOULD be reaped. Returns a copy.
        """
        pids: set[int] = set()
        for runtime in list(self._subagent_runtimes.values()):
            try:
                if runtime is not None and runtime.is_alive() and isinstance(runtime.pid, int):
                    pids.add(runtime.pid)
            except Exception:
                logger.debug("companion runtime pid probe failed", exc_info=True)
        for bg in [self._bg_runtime, *self._draining_bg_runtimes]:
            try:
                if bg is not None and bg.is_alive() and isinstance(bg.pid, int):
                    pids.add(bg.pid)
            except Exception:
                logger.debug("bg runtime pid probe failed", exc_info=True)
        return pids

    _POOL_HEALTH_INTERVAL = 30  # seconds between health sweeps

    async def _pool_health_loop(self) -> None:
        """Delegate the periodic warm-pool health loop."""
        await self._pool._pool_health_loop()

    async def _sweep_warm_pool_once(self) -> None:
        """Delegate one race-safe warm-pool sweep."""
        await self._pool._sweep_warm_pool_once()

    def runtime_pids(self) -> list[dict[str, object]]:
        """Return process-identity snapshots for session runtimes."""
        return self._allocation_boundary().runtime_pids()

    def _append_companion_runtime_rows(self, rows: list[dict[str, object]]) -> None:
        """Append background and subagent runtime process rows."""
        self._allocation_boundary()._append_companion_runtime_rows(rows)

    def context_info(self) -> list[dict[str, object]]:
        """Return the dashboard-facing live context snapshot."""
        return self._allocation_boundary().context_info()

    @staticmethod
    def _resolve_agent_model(agent: str) -> str:
        """Resolve model from agent config file. Cached at class level with
        mtime + TTL invalidation.

        The cache MUST NOT pin a resolution forever — in particular the
        ``"auto"`` miss (agent JSON absent, or present with no explicit model).
        A later create/edit of the agent's JSON has to be observed:

        - **mtime**: the agents-dir mtime is bumped by any add/remove/rename of
          a ``*.json`` file, so a newly-created agent config invalidates the
          whole cache immediately.
        - **TTL**: an in-place edit of an existing file does not change the dir
          mtime, so entries also expire after ``_AGENT_MODEL_CACHE_TTL`` seconds
          and are re-resolved.
        """
        agents_dir = kiro_agents_dir_path()
        try:
            dir_mtime = agents_dir.stat().st_mtime
        except OSError:
            dir_mtime = 0.0
        now = time.monotonic()

        if not hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache = {}  # type: ignore[attr-defined]
        cache = SessionManager._agent_model_cache  # type: ignore[attr-defined]

        entry = cache.get(agent)
        if entry is not None:
            cached_model, cached_mtime, cached_ts = entry
            if cached_mtime == dir_mtime and (now - cached_ts) < _AGENT_MODEL_CACHE_TTL:
                return cached_model

        model = "auto"
        try:
            # Use the SAME directory as the cache stamp and preserve the former
            # native-order, first-match scan.  This runs on the event-loop
            # thread, so a match must stop all later spec reads rather than
            # building a full map on every cache miss / TTL expiry.
            for agent_file in agents_dir.glob("*.json"):
                data = _read_agent_spec(
                    agent_file,
                    operation="resolve_agent_model",
                    source="unknown",
                )
                if data is None:
                    continue
                if data.get("name") == agent or agent_file.stem == agent:
                    model = spec_model(data)
                    break
        except Exception:
            pass
        cache[agent] = (model, dir_mtime, now)
        return model

    async def recycle_background(self) -> None:
        """Delegate context-driven background-provider recycling."""
        await self._background_runtime.recycle_background()

    async def recycle_heartbeat(self) -> None:
        """Delegate cycle-scoped heartbeat-provider recycling."""
        await self._background_runtime.recycle_heartbeat()

    async def get_or_create(
        self,
        key: str,
        agent: str | None = None,
        channel_id: str | None = None,
        approval_policy: str = "",
        model: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        speculative: bool = False,
        speculative_resume: bool = False,
        wait_if_busy: bool = True,
        _won_race_retries: int = 0,
        **extra_factory_kwargs: Any,
    ) -> tuple[LLMProvider, bool, bool]:
        """Claim or allocate a session and return its held lease."""
        return await self._allocation_boundary().get_or_create(
            key,
            agent=agent,
            channel_id=channel_id,
            approval_policy=approval_policy,
            model=model,
            cwd=cwd,
            extra_env=extra_env,
            speculative=speculative,
            speculative_resume=speculative_resume,
            wait_if_busy=wait_if_busy,
            _won_race_retries=_won_race_retries,
            **extra_factory_kwargs,
        )

    async def reset(
        self,
        key: str,
        *,
        expect_session: _Session | None = None,
        skip_if_busy: bool = False,
        clear_conversation: bool = False,
    ) -> bool:
        """Reset a live session while preserving its persistence entry."""
        return await self._lifecycle_boundary().reset(
            key,
            expect_session=cast(Any, expect_session),
            skip_if_busy=skip_if_busy,
            clear_conversation=clear_conversation,
        )

    def check_context_usage(self, key: str, provider: LLMProvider) -> float:
        """Delegate context accounting and compaction triggering."""
        return self._compaction.check_context_usage(key, provider)

    def set_autocompact_pct(self, key: str, pct: float | None) -> None:
        """Set or clear (``None``) *key*'s per-session compaction threshold.

        Values clamp into the documented ``AUTOCOMPACT_PCT_MIN``–``MAX`` range,
        the same guarantee the config loader gives the global knob: an
        out-of-range override (e.g. from a hand-edited persistence file) must
        degrade to the nearest firing value, never silently disable the
        backstop. The override is keyed independently of live-session
        membership, so it survives resets and recycles; callers that persist it
        (the dashboard slot) re-seed it after a gateway restart.
        """
        if pct is not None:
            if pct != pct:  # NaN: comparisons below would both be False
                return
            try:
                pct = min(max(float(pct), AUTOCOMPACT_PCT_MIN), AUTOCOMPACT_PCT_MAX)
            except OverflowError:
                # An int too large for a float; ignore like NaN.
                return
        self._compaction.set_autocompact_pct(key, pct)

    def drop_autocompact_overrides_matching(
        self, exact_keys: set[str], folded_keys: set[str], fold: Callable[[str], str]
    ) -> int:
        """Drop threshold overrides for permanently deleted, slotless sessions.

        The slotless complement of ``destroy()``'s override clear — see the
        coordinator method for the matching contract.
        """
        return self._compaction.drop_autocompact_overrides_matching(exact_keys, folded_keys, fold)

    async def compact_if_needed(self, key: str) -> str:
        """Delegate awaited between-turn compaction."""
        return await self._compaction.compact_if_needed(key)

    def set_compact_callback(self, cb: _CompactCallback | None) -> None:
        """Register the compaction completion callback."""
        self._compaction.set_compact_callback(cb)

    def mark_needs_reinjection(self, key: str) -> None:
        """Mark a live session for one-shot context reinjection."""
        self._compaction.mark_needs_reinjection(key)

    def consume_needs_reinjection(self, key: str) -> bool:
        """Consume a live session's reinjection marker."""
        return self._compaction.consume_needs_reinjection(key)

    def consume_replay_suppression(self, key: str) -> bool:
        """Read *and clear* whether *key*'s next cold start must skip replay.

        One-shot by construction, the same shape as
        :meth:`consume_needs_reinjection`: the flag is cleared as it is read, so
        exactly the FIRST cold start after ``discard_conversation(replay=False)``
        starts empty. Leaving it set would make every later cold start on that
        key — an idle-timeout expiry, a gateway restart — silently amnesiac,
        which nobody asked for.
        """
        if key in self._suppress_replay:
            self._suppress_replay.discard(key)
            return True
        folded = self._fold_key(key)
        if folded in self._suppress_replay:
            self._suppress_replay.discard(folded)
            return True
        return False

    def set_recycle_callback(self, cb: _RecycleCallback | None) -> None:
        """Register the lifecycle recycle callback."""
        self._lifecycle_boundary().set_recycle_callback(cb)

    def _compaction_gate_decision(self, key: str, provider: LLMProvider, pct: float) -> str | None:
        """Delegate the ordered compaction gate ladder."""
        # Adopt a newly published threshold before the ladder reads it: the
        # coordinator resolves it through ``owner._cfg``, so syncing here is what
        # makes a config write from any writer bind on the next reading.
        self._sync_autocompact_pct()
        return self._compaction._compaction_gate_decision(key, provider, pct)

    def _trigger_compaction(
        self, key: str, reason: str, pct: float, provider: LLMProvider
    ) -> str | None:
        """Delegate fire-and-forget compaction scheduling."""
        return self._compaction._trigger_compaction(key, reason, pct, provider)

    async def _compact_session(self, key: str, pct: float) -> str:
        """Delegate backend-specific compaction execution."""
        return await self._compaction._compact_session(key, pct)

    async def _recycle_held(self, key: str, session: "_Session", pct: float) -> None:
        """Delegate exact-session recycle while its semaphore is held."""
        await self._compaction._recycle_held(key, session, pct)

    async def _compact_in_place(self, key: str, session: "_Session", pct: float) -> str:
        """Delegate in-place compaction under turn exclusion."""
        return await self._compaction._compact_in_place(key, session, pct)

    def _settle_compact_cooldown(self, key: str, provider: LLMProvider, pct_before: float) -> bool:
        """Delegate measurable/deferred compaction verdict settlement."""
        return self._compaction._settle_compact_cooldown(key, provider, pct_before)

    def _judge_compact_effect(self, key: str, pct_before: float, pct_after: float) -> bool:
        """Delegate compaction effectiveness and damping policy."""
        return self._compaction._judge_compact_effect(key, pct_before, pct_after)

    async def _reset_still_critical(
        self, key: str, pct_before: float, pct_after: float, *, expect: "_Session | None"
    ) -> bool:
        """Delegate guarded reset after an ineffective compaction."""
        return await self._compaction._reset_still_critical(
            key, pct_before, pct_after, expect=expect
        )

    async def _fire_compact_callback(self, key: str, pct: float, *, success: bool) -> None:
        """Delegate compaction callback dispatch."""
        await self._compaction._fire_compact_callback(key, pct, success=success)

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None:
        """Dispatch a lifecycle recycle callback through the lifecycle boundary."""
        await self._lifecycle_boundary()._fire_recycle_callback(key, reason=reason)

    async def remove(self, key: str) -> None:
        """Retire a session while preserving its resumable mapping."""
        await self._lifecycle_boundary().remove(key)

    async def retire_kiro_identity_sessions(self) -> tuple[list[str], bool]:
        """Retire idle processes that loaded a superseded Kiro identity."""
        return await self._lifecycle_boundary().retire_kiro_identity_sessions()

    async def _retire_kiro_warm_pool(self) -> bool:
        """Delegate pooled-provider retirement after identity change."""
        return await self._pool._retire_kiro_warm_pool()

    async def _retire_kiro_subagent_runtimes(self) -> bool:
        """Retire idle companion runtimes that use Kiro's identity store."""
        return await self._lifecycle_boundary()._retire_kiro_subagent_runtimes()

    async def _retire_kiro_bg_runtime(self) -> bool:
        """Retire the idle shared background runtime after an identity change."""
        return await self._lifecycle_boundary()._retire_kiro_bg_runtime()

    async def remove_if_unclaimed(self, key: str) -> bool:
        """Remove a speculative session only before its first real claimant."""
        return await self._lifecycle_boundary().remove_if_unclaimed(key)

    async def destroy(self, key: str) -> None:
        """Permanently destroy a session and its persistence entry."""
        await self._lifecycle_boundary().destroy(key)

    async def discard_conversation(
        self, key: str, *, replay: bool = True, skip_if_busy: bool = False
    ) -> bool:
        """Drop native conversation state while retaining channel linkage.

        Returns whether a session was actually torn down; False means
        ``skip_if_busy`` refused because a turn was in flight. See
        :meth:`SessionLifecycleService.discard_conversation` for the guard's
        atomicity contract.
        """
        return await self._lifecycle_boundary().discard_conversation(
            key, replay=replay, skip_if_busy=skip_if_busy
        )

    async def drain_active_turns(self, timeout: float | None = None) -> int:
        """Bring unfinished native turns to a bounded safe boundary."""
        return await self._lifecycle_boundary().drain_active_turns(timeout=timeout)

    async def close_all(self, drain_timeout: float | None = None) -> None:
        """Drain turns, persist resumable state, and close every owned runtime."""
        await self._lifecycle_boundary().close_all(drain_timeout=drain_timeout)

    # ── Circuit breaker ──

    def record_success(self, key: str) -> None:
        """Clear the folded session failure count."""
        self._allocation_boundary().record_success(key)

    async def record_failure(self, key: str) -> bool:
        """Record a failure and apply the circuit breaker."""
        return await self._allocation_boundary().record_failure(key)

    def begin_turn(self, key: str) -> None:
        """Apply the yield-free pre-dispatch closing gate."""
        self._allocation_boundary().begin_turn(key)

    # ── Per-session semaphore ──

    def mark_continuable(self, key: str) -> None:
        """Mark a folded conversation as continuable."""
        self._allocation_boundary().mark_continuable(key)

    def unmark_continuable(self, key: str) -> None:
        """Remove a folded continuable mark."""
        self._allocation_boundary().unmark_continuable(key)

    def set_continuable_fallback(self, fn: Callable[[str], bool] | None) -> None:
        """Set the disk-truth continuable fallback."""
        self._allocation_boundary().set_continuable_fallback(fn)

    def _is_continuable_key(self, folded: str) -> bool:
        """Resolve a folded continuable key through cache and fallback."""
        return self._allocation_boundary()._is_continuable_key(folded)

    def is_continuable(self, key: str) -> bool:
        """Return whether a folded conversation is continuable."""
        return self._allocation_boundary().is_continuable(key)

    def resumable_sid(self, key: str) -> str | None:
        """Return the persisted resume SID for a folded key."""
        return self._allocation_boundary().resumable_sid(key)

    def resumable_hint(self, key: str) -> bool:
        """Return whether a folded key has resumable state."""
        return self._allocation_boundary().resumable_hint(key)

    def seed_conversation(self, key: str, sid: str, *, provider: str = "", cwd: str = "") -> None:
        """Seed a persisted conversation mapping."""
        self._allocation_boundary().seed_conversation(key, sid, provider=provider, cwd=cwd)

    def forget_conversation(self, key: str) -> str | None:
        """Delete a persisted conversation and continuable mark."""
        return self._allocation_boundary().forget_conversation(key)

    def conversation_provider(self, key: str) -> str:
        """Return the persisted provider label for a folded key."""
        return self._allocation_boundary().conversation_provider(key)

    def release(self, key: str, *, cleanup: bool = False) -> None:
        """Release the key-based session lease."""
        self._allocation_boundary().release(key, cleanup=cleanup)

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None:
        """Best-effort cleanup of provider session files."""
        await self._allocation_boundary()._safe_cleanup(provider, session_id)

    # ── Message queue (Slack thread serialization) ──

    def is_busy(self, key: str) -> bool:
        """Return whether a folded session lease is held."""
        return self._allocation_boundary().is_busy(key)

    def touch(self, key: str) -> bool:
        """Refresh a folded live session timestamp."""
        return self._allocation_boundary().touch(key)

    def enqueue(
        self, key: str, msg_ts: str, text: str, *, force: bool = False, **kwargs: object
    ) -> bool:
        """Queue a message behind a folded session turn."""
        return self._allocation_boundary().enqueue(key, msg_ts, text, force=force, **kwargs)

    def dequeue(self, key: str) -> tuple[str, str, dict] | None:
        """Pop the next non-cancelled queued message."""
        return self._allocation_boundary().dequeue(key)

    def cancel_queued(self, key: str, msg_ts: str) -> bool:
        """Remove or mark one queued message as cancelled."""
        return self._allocation_boundary().cancel_queued(key, msg_ts)

    def is_cancelled(self, key: str, msg_ts: str) -> bool:
        """Consume a queued-message cancellation marker."""
        return self._allocation_boundary().is_cancelled(key, msg_ts)

    def clear_queue(self, key: str) -> None:
        """Clear queued messages and their temporary paths."""
        self._allocation_boundary().clear_queue(key)

    async def is_provider_alive(self, key: str) -> bool | None:
        """Probe a folded session provider outside the registry lock."""
        return await self._allocation_boundary().is_provider_alive(key)

    def get_approval_policy(self, key: str) -> str:
        """Return a folded session approval policy."""
        return self._allocation_boundary().get_approval_policy(key)

    def get_agent(self, key: str) -> str:
        """Return a folded session agent name."""
        return self._allocation_boundary().get_agent(key)

    def set_approval_policy(self, key: str, policy: str) -> None:
        """Update a folded session approval policy with audit logging."""
        self._allocation_boundary().set_approval_policy(key, policy)

    # ── Slack thread linking (persisted via SessionMap) ──

    def set_slack_link(self, key: str, thread_ts: str, channel_id: str | None) -> None:
        """Link a session to a Slack thread. Persists to session map."""
        self._session_map.set_slack_link(key, thread_ts, channel_id)

    def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
        """Return (thread_ts, channel_id) for a session."""
        return self._session_map.get_slack_link(key)

    def clear_slack_link(self, key: str) -> bool:
        """Remove a session's Slack link (stop mirroring). Returns True if one was present."""
        return self._session_map.clear_slack_link(key)

    def set_slack_paused(self, key: str, paused: bool) -> bool:
        """Set whether turns reach the linked Slack thread; return the prior state.

        Disconnecting retains the thread binding and its reverse index, so a reply
        there still resolves to this session.
        """
        return self._session_map.set_slack_paused(key, paused)

    def is_slack_paused(self, key: str) -> bool:
        """True iff this session's Slack thread is disconnected but still bound."""
        return self._session_map.is_slack_paused(key)

    def get_session_for_thread(self, thread_ts: str) -> str | None:
        """Return the session key linked to a Slack thread, or None."""
        return self._session_map.get_session_for_thread(thread_ts)

    def channel_key_for_stem(self, stem: str) -> str:
        """The real channel session key behind a transcript filename *stem*.

        Lets the dashboard bind a surfaced channel tab to the session the
        channel itself runs, instead of deriving a key from the filename (the
        ``:``-to-``_`` fold is not reversible). ``""`` means unknown.
        """
        return self._session_map.channel_key_for_stem(stem)

    # ── Channel-neutral outbound mirror (generalizes Slack linking) ──

    def set_mirror_link(
        self,
        key: str,
        link: ChannelLink | None,
        *,
        accepts_inbound: bool = False,
        reason: str = UNBIND_REASON_UNSPECIFIED,
    ) -> None:
        """Bind (or clear) a session's channel-neutral mirror target.

        ``accepts_inbound`` upgrades a non-Slack outbound mirror into a
        persisted session-resume binding. Slack owns its dedicated reverse
        index; other channels use :meth:`find_mirror_sessions`. ``reason`` is
        recorded when this call ends an existing inbound binding.
        """
        self._session_map.set_mirror_link(
            key,
            link,
            accepts_inbound=accepts_inbound,
            reason=reason,
        )

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        """Return a session's outbound mirror target as a channel-neutral link,
        or None. Legacy Slack sessions surface as a Slack ``ChannelLink``."""
        return self._session_map.get_mirror_link(key)

    def mirror_accepts_inbound(self, key: str) -> bool:
        """True iff this session's mirror is a session-resume (two-way) binding."""
        return self._session_map.mirror_accepts_inbound(key)

    def set_mirror_opt_out(self, key: str, opted_out: bool) -> None:
        """Record (or withdraw) a refusal of AUTOMATIC origin mirroring.

        A channel that mirrors its own conversation by default needs an
        in-channel "off" that the NEXT inbound message does not silently undo,
        and clearing the binding cannot express that: an entry with no ``mirror``
        is indistinguishable from one that was never linked, so the automatic
        bind would fire again one message later. This flag is that difference.

        Persisted, because the bind it suppresses is itself re-asserted on every
        turn and survives a restart — an in-memory refusal would come back on
        its own. Only the automatic bind consults it; an explicit ``/link`` or
        dashboard link is a direct instruction and :meth:`set_mirror_link` never
        reads it.
        """
        with self._session_map.batched_save():
            self._session_map.set_flag(_opt_out_key(key), MIRROR_OPT_OUT_FLAG, opted_out)
            # Retire a refusal an earlier build stored under the generation key,
            # so it cannot outlive a withdrawal made through the bucket.
            legacy = canonical_key(key)
            if legacy != _opt_out_key(key):
                self._session_map.set_flag(legacy, MIRROR_OPT_OUT_FLAG, False)

    def mirror_opt_out(self, key: str) -> bool:
        """True iff this conversation declined automatic origin mirroring.

        Reads the bucket, then falls back to the generation key an earlier build
        wrote. Without the fallback, upgrading silently restores mirroring for
        every conversation that had already turned it off — the exact failure the
        flag exists to prevent, delivered by the fix for it.

        A legacy hit is PROMOTED to the bucket, which is why this read writes.
        Reading it without promoting would honour the refusal for the generation
        it was stored under and lose it at the next rotation, so an upgrading user
        would keep the expiring behaviour this change exists to remove. Retiring
        the old row in the same write also stops it holding an entry that pruning
        is forbidden to collect, one per generation.
        """
        bucket = _opt_out_key(key)
        if self._session_map.get_flag(bucket, MIRROR_OPT_OUT_FLAG):
            return True
        legacy = canonical_key(key)
        if legacy == bucket:
            return False
        if not self._session_map.get_flag(legacy, MIRROR_OPT_OUT_FLAG):
            return False
        with self._session_map.batched_save():
            self._session_map.set_flag(bucket, MIRROR_OPT_OUT_FLAG, True)
            self._session_map.set_flag(legacy, MIRROR_OPT_OUT_FLAG, False)
        return True

    def batched_save(self) -> AbstractContextManager[None]:
        """Collapse the session-map writes of a related mutation sequence into one.

        Each mutation rewrites the whole map, so a caller making several of them
        (a link, an unlink) pays that cost once per operation unless it says
        otherwise. Must not be held across an ``await`` — see
        :meth:`SessionMap.batched_save`.
        """
        return self._session_map.batched_save()

    def set_origin_link(self, key: str, link: ChannelLink) -> None:
        """Record the channel conversation this session was started from.

        Called by a transport's inbound path with the conversation's real send
        target, so unattended output about the session (the auto-compact notice)
        can reach the user.

        Held in memory, NOT persisted, and that is deliberate: the target is only
        ever needed to talk about a LIVE session, and sessions themselves are
        in-memory — a gateway restart takes the session with it, so there is
        nothing left to compact or explain. Keeping it here also keeps the
        recording free of disk I/O and of cross-thread mutation, both of which a
        session-map field would have put on the transport's turn path.
        """
        key = self._fold_key(key)
        self._origin_links[key] = link
        # Bound the map for the pathological case where sessions are dropped
        # without reset()/remove() (which evict their own entries). FIFO, since
        # dict preserves insertion order and the oldest key is the least likely
        # to still be live.
        while len(self._origin_links) > _MAX_ORIGIN_LINKS:
            self._origin_links.pop(next(iter(self._origin_links)), None)

    def get_origin_link(self, key: str) -> ChannelLink | None:
        """Return the channel conversation this session was started from, or None."""
        return self._origin_links.get(self._fold_key(key))

    def find_mirror_sessions(
        self,
        link: ChannelLink,
        *,
        inbound_only: bool = False,
    ) -> list[str]:
        """Return sessions bound to an exact non-Slack mirror location."""
        return self._session_map.find_mirror_sessions(
            link,
            inbound_only=inbound_only,
        )

    def mirror_claim_blockers(
        self,
        key: str,
        link: ChannelLink,
        *,
        accepts_inbound: bool = False,
    ) -> list[str]:
        """Sessions that must stop *key* from binding *link*, or [] if it is free."""
        return self._session_map.mirror_claim_blockers(key, link, accepts_inbound=accepts_inbound)

    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        """Remove a session's outbound mirror binding. Returns True iff present."""
        return self._session_map.clear_mirror_link(key, reason=reason)

    def clear_mirror_links_at(
        self, link: ChannelLink, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        """Clear every session mirroring to an exact location; return cleared keys."""
        return self._session_map.clear_mirror_links_at(link, reason=reason)

    @staticmethod
    def set_unbind_listener(callback: UnbindListener | None) -> None:
        """Register the sink notified when an inbound resume binding is removed.

        The registry it writes is the session map's, shared by every instance, so
        a removal performed through a throwaway map is announced too.
        """
        set_unbind_listener(callback)

    async def aflush(self) -> None:
        await self._session_map.aflush()

    def set_mirror_paused(self, key: str, paused: bool, *, origin: bool = False) -> bool:
        """Set whether turns reach one non-Slack delivery; return the prior state.

        ``origin`` selects the born-in conversation rather than the explicit
        mirror binding — a session can hold both, and they mute independently.
        """
        return self._session_map.set_mirror_paused(key, paused, origin=origin)

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        """True iff the named non-Slack delivery is disconnected (see the setter)."""
        return self._session_map.is_mirror_paused(key, origin=origin)

    # Backward-compat aliases used by callers not yet migrated
    async def set_channel(self, key: str, channel_id: str) -> None:
        """Set channel for a session. Prefer set_slack_link for new code."""
        thread_ts, _ = self.get_slack_link(key)
        self.set_slack_link(key, thread_ts or "", channel_id)

    def get_channel(self, key: str) -> str | None:
        """Return the Slack channel ID for a session key, or None."""
        _, channel_id = self.get_slack_link(key)
        return channel_id

    # ── Additional session map helpers ──

    def find_key_by_sid(self, sid: str) -> str | None:
        return self._session_map.find_key_by_sid(sid)

    def max_generation(self, bucket: str) -> int:
        """Highest persisted DM generation for a session bucket (see SessionMap)."""
        return self._session_map.max_generation(bucket)

    async def set_thread(self, key: str, thread_ts: str) -> None:
        """Set thread for a session. Prefer set_slack_link for new code."""
        _, channel_id = self.get_slack_link(key)
        self.set_slack_link(key, thread_ts, channel_id)

    def get_thread(self, key: str) -> str | None:
        """Return the Slack thread_ts for a session key, or None."""
        thread_ts, _ = self.get_slack_link(key)
        return thread_ts

    # ── Cancel ──

    async def cancel_current(
        self,
        key: str,
        *,
        wait_ack_timeout: float = 0.0,
    ) -> CancelOutcome:
        """Cancel the in-flight operation without destroying its session."""
        return await self._lifecycle_boundary().cancel_current(
            key,
            wait_ack_timeout=wait_ack_timeout,
        )

    async def stop_turn(
        self,
        key: str,
        *,
        force: bool = False,
        preserve_queue: bool = False,
        on_soft: Callable[[], Awaitable[None]] | None = None,
        on_hard: Callable[[], Awaitable[None]] | None = None,
    ) -> StopOutcome:
        """Cooperatively stop a turn, escalating to reset and eager respawn."""
        return await self._lifecycle_boundary().stop_turn(
            key,
            force=force,
            preserve_queue=preserve_queue,
            on_soft=on_soft,
            on_hard=on_hard,
        )

    async def _send_abort_for_session(self, key: str, session: Any) -> None:
        """Best-effort abort gateway work before hard session teardown."""
        await self._lifecycle_boundary()._send_abort_for_session(key, session)

    async def _eager_respawn(self, key: str) -> None:
        """Respawn after hard teardown and release the acquired lease."""
        await self._lifecycle_boundary()._eager_respawn(key)

    @property
    def count(self) -> int:
        return len(self._sessions)

    async def drain_all_providers(self) -> list:
        """Pop all registered sessions and return their providers."""
        return await self._lifecycle_boundary().drain_all_providers()

    async def drain_warm_pool(self) -> list:
        """Remove and return all queued warm providers."""
        return await self._pool.drain_warm_pool()

    # ── Idle cleanup ──

    # ── Watchdog hooks ──
    # Each hook is the execution half of a CleanupHook (see watchdog.py). Each
    # one reproduces the exact try/except of the inline cleanup-loop block it
    # was lifted from, so SessionWatchdog.tick() can stay a dumb dispatcher and
    # the move is behaviour-preserving (no severity promotion of swallowed
    # errors). The orphan-PID sweep is deliberately NOT a hook in CR 1.

    async def _expire_idle_hook(self) -> None:
        """Run the configured idle-expiry cleanup hook."""
        await self._cleanup_boundary()._expire_idle_hook()

    async def _bg_drain_reap_hook(self) -> None:
        """Reap drained background runtimes during a cleanup tick."""
        await self._cleanup_boundary()._bg_drain_reap_hook()

    async def _orphan_mcp_hook(self) -> None:
        """Run the legacy orphan-MCP cleanup hook."""
        await self._cleanup_boundary()._orphan_mcp_hook()

    async def _rss_threshold_check(self) -> None:
        """Recycle idle sessions whose process trees exceed the RSS policy."""
        await self._cleanup_boundary()._rss_threshold_check()

    async def _stuck_turn_check(self) -> None:
        """Report newly observed parked turn consumers."""
        await self._cleanup_boundary()._stuck_turn_check()

    async def _cleanup_loop(self) -> None:
        """Run periodic watchdog and process-cleanup policy until shutdown."""
        await self._cleanup_boundary()._cleanup_loop()

    def set_active_dashboard_slots(self, slot_keys: set[str]) -> None:
        """Publish the live dashboard slot set for orphan-session expiry."""
        self._cleanup_boundary().set_active_dashboard_slots(slot_keys)

    async def _expire_idle(self, timeout_secs: int) -> None:
        """Expire idle and orphaned sessions using the cleanup policy."""
        await self._cleanup_boundary()._expire_idle(timeout_secs)
