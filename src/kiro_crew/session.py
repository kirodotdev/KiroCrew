"""Session manager — maps Slack thread_ts to LLM provider sessions.

Each Slack thread gets its own LLMProvider instance. Sessions are
cleaned up after idle timeout (default 30 min).

Warm session pool: ``start_pool()`` pre-spawns kiro-cli processes so
``get_or_create()`` returns instantly.  After handing out a warm session,
a replacement is created in the background to maintain the target count.

Background session: ``BACKGROUND_KEY`` is a persistent shared session for
lightweight background work (cron, heartbeat, lesson extraction).  It
stays alive between uses, serialized by the per-session semaphore.

At >= ``cfg.session.autocompact_pct`` context usage, fires a background
compaction task. Both backends compact **in place** so the session — and
any queued or agentic work on it — continues without a user nudge:

* **kiro-cli:** run ``/compact`` in place under the session semaphore
  (native command execute + ``_kiro.dev/compaction/status`` wait). The
  process and session ID survive; the conversation is summarized in
  place. If the in-place compact fails or times out, fall back to the
  legacy **recycle**: kill the session and let the next user message
  re-seed context via ``build_session_context()`` (the session_map entry
  is dropped to avoid false-resume from stale state). A recycle is never
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
concurrent Slack messages on the same thread don't interleave.

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
   Kills sessions idle for >``timeout_secs`` (default 30 min) via
   ``reset()`` → ``provider.shutdown()`` → SIGKILL process tree.
   Protected keys: ``_PERSISTENT_KEYS`` (``_bg`` and ``_hb``).
   **Known limitation**: ``last_used`` is only bumped on ``get_or_create()``,
   not on every LLM round-trip. A task runner step doing continuous work for
   >30 min without a new ``get_or_create()`` call could be swept. This is
   accepted for now to prevent runaway tasks, but may need a heartbeat or
   persistent-key mechanism if longer steps become common.

"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
    from kiro_crew.acp.types import AcpEvent

from kiro_crew import model_registry, platform_compat, shutdown_event
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    POOL_SIZE_MAX,
    build_provider_factory,
    default_project_dir,
    normalize_agent_model,
)
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.mcp_gateway.abort import schedule_abort
from kiro_crew.messaging.link import ChannelLink, canonical_key, legacy_key
from kiro_crew.providers.base import CancelOutcome, LLMProvider
from kiro_crew.sandbox import cleanup_stale_sandbox_profiles
from kiro_crew.sel import sel
from kiro_crew.session_map import SessionMap as SessionMap  # noqa: F401
from kiro_crew.session_map import _kiro_sessions_dir  # noqa: F401
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
    kill_orphan_mcps,
)
from kiro_crew.stats import Stats
from kiro_crew.watchdog import CleanupHook, SessionWatchdog

# The standalone ClaudeCodeProvider was removed in the KiroACP-only refactor;
# the public core ships kiro-cli (ACP) only. The name is kept (always None) so
# the legacy ``ClaudeCodeProvider is not None and isinstance(...)`` guards below
# short-circuit cleanly. The claude-agent-acp seam survives via
# ``_is_claude_backend`` (the internal companion re-registers Claude Code).
ClaudeCodeProvider = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def _is_claude_backend(provider: Any) -> bool:
    """Check if a provider drives the claude-agent-acp seam via the ACP adapter.

    Returns True when an AcpProvider wraps claude-agent-acp (backend="claude").
    Dormant in the public core (the factory never selects it); the internal
    companion re-registers the Claude Code provider over this same seam.
    """
    from kiro_crew.providers.acp import AcpProvider  # circular import: providers -> session

    if not isinstance(provider, AcpProvider):
        return False
    backend = getattr(provider.client, "backend", "")
    return backend == "claude"


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
    """
    alive = (
        provider.is_process_alive()
        if hasattr(provider, "is_process_alive")
        else provider.is_alive()
    )
    if (
        not alive
        and ClaudeCodeProvider is not None
        and isinstance(provider, ClaudeCodeProvider)
        and provider.connection_mode == "per_session"
    ):
        alive = True
    return alive


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
    stored_provider = session_map.get_provider(session_key) or "acp"
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

_SUBAGENT_PREFIX = "subagent:"
_CHANNEL_PREFIX = "channel:"
_SIDE_PREFIX = "side:"

# Stateless session-key prefixes — skip resume across restarts.
_WORKFLOW_POOL_PREFIX = "wf-pool:"
_STATELESS_PREFIXES = (
    "cron:",
    _SUBAGENT_PREFIX,
    "taskrunner:",
    _CHANNEL_PREFIX,
    "secretary:",
    _SIDE_PREFIX,
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

# Heartbeat session key — used by HeartbeatService.  Spawned with the full
# ``kirocrew`` agent so polled tasks can call read-only MCP tools (CR/ticket
# status, etc.).  Tool approval at runtime is gated by the
# ``HEARTBEAT_SAFE_TOOLS`` allowlist in ``slack/gateway.py``.
HEARTBEAT_KEY = "_hb"


# Context usage thresholds
_CONTEXT_WARN_PCT = 70.0
_CONTEXT_COMPACT_PCT = 80.0

# Hard timeout for an in-place /compact under the session semaphore. A stuck
# compact would otherwise block all concurrent gets on the same session.
_COMPACT_TIMEOUT_SECS = 300.0

# How long the kiro-cli in-place path waits for the async
# ``_kiro.dev/compaction/status`` result after issuing /compact. Matches the
# budget the dashboard's manual /compact and Slack's !compact already use.
_COMPACT_RESULT_WAIT_SECS = 120.0

# After a failed compact, suppress auto-compaction for this many seconds so a
# broken /compact does not fire on every subsequent turn.
_COMPACT_FAILURE_COOLDOWN_SECS = 60.0


class _CompactCallback(Protocol):
    async def __call__(self, key: str, pct: float, *, success: bool) -> None: ...  # noqa: E704


class _RecycleCallback(Protocol):
    async def __call__(self, key: str, *, reason: str) -> None: ...  # noqa: E704


# Circuit breaker: force-reset after this many consecutive failures
_CIRCUIT_BREAKER_THRESHOLD = 5

# Cap on remembered per-session channel notice targets. reset()/remove() evict
# their own entries, so this only bounds sessions dropped by some other path.
_MAX_ORIGIN_LINKS = 512

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


class SessionClosingError(RuntimeError):
    """Raised when a turn is requested while the manager is tearing down.

    A subclass of ``RuntimeError`` so existing broad handlers still catch it.
    Signalled both by the ``get_or_create`` entry gate (no new/resumed session
    once ``close_all`` has set ``_closing``) and by :meth:`SessionManager.begin_turn`
    (the pre-dispatch gate that stops a caller which already holds a lease from
    opening a turn during the shutdown drain window).
    """


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


@dataclass
class _Session:
    provider: LLMProvider
    last_used: float = field(default_factory=time.monotonic)
    is_new: bool = True
    prompt_count: int = 0
    consecutive_failures: int = 0
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
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


class _ProviderBgSession:
    """``AcpSessionHandle``-compatible handle over the shared ``BACKGROUND_KEY``
    ``_Session``, for non-kiro providers (claude_code / bedrock) that cannot use
    the multiplexed kiro-only ``AcpRuntime``.

    All ``_bg`` callers share this ONE provider session, so turns are serialized
    by the existing per-session ``Semaphore(1)`` — exactly the old pre-multiplex
    behavior. It yields the SAME ``AcpEvent`` type as ``AcpSessionHandle`` and
    both paths parse frames through the shared ``_dispatch.parse_session_update``
    — so the two ``_bg`` code paths cannot drift: this adapter is pure plumbing
    over ``provider.stream`` / ``provider.reject_tool``, adding no second parser.
    """

    def __init__(self, sess: "_Session") -> None:
        self._sess = sess
        self._sem_held = False

    @property
    def session_id(self) -> str:
        try:
            return self._sess.provider.session_id
        except Exception:
            return ""

    def _release(self) -> None:
        if self._sem_held:
            self._sem_held = False
            self._sess.semaphore.release()

    async def prompt(self, message: str, timeout: float | None = None) -> "AsyncIterator[AcpEvent]":
        # timeout is accepted for AcpSessionHandle signature parity; the
        # underlying provider/client manages its own stale-turn watchdog.
        await self._sess.semaphore.acquire()
        self._sem_held = True
        try:
            async for event in self._sess.provider.stream(message):
                yield event
        finally:
            self._release()

    async def reject_tool(self, request_id: str | int) -> None:
        await self._sess.provider.reject_tool(request_id)

    async def destroy(self) -> None:
        # The BACKGROUND_KEY _Session is persistent and shared — never tear it
        # down here. Just release the turn semaphore deterministically so the
        # next _bg caller isn't blocked on generator finalization.
        self._release()


class SessionManager:
    """Thread-keyed LLM provider pool with warm session pre-spawning."""

    def _fold_key(self, key: str) -> str:
        """Resolve bare/canonical Slack session-key aliases onto the live entry.

        Slack thread sessions have two historical key forms: the legacy bare
        ``thread_ts`` (``"1783733803.877979"``) and the namespaced canonical
        form (``"slack:1783733803.877979"``, see ``messaging.link``). The
        ``SessionMap`` thread index returns canonical keys while some callers
        still derive bare keys, so the registry must treat both forms as the
        SAME logical session — otherwise a lookup under one form misses a live
        session registered under the other, and the caller cold-starts a
        duplicate, context-free session (thread split).

        Resolution order: exact match, then the canonical alias, then the
        legacy bare alias. Unknown keys pass through unchanged so new
        registrations keep the caller's form and non-Slack namespaces
        (``dashboard:``, ``cron:``, ...) are never rewritten.
        """
        if key in self._sessions:
            return key
        canon = canonical_key(key)
        if canon != key and canon in self._sessions:
            return canon
        bare = legacy_key(key)
        if bare is not None and bare in self._sessions:
            return bare
        return key

    def has_session(self, key: str) -> bool:
        """Return ``True`` if an active session exists for *key*."""
        return self._fold_key(key) in self._sessions

    def get_provider(self, key: str) -> LLMProvider | None:
        """Return the LLM provider for *key*, or ``None``."""
        sess = self._sessions.get(self._fold_key(key))
        return sess.provider if sess else None

    async def try_acquire(self, key: str) -> bool:
        """Atomically take *key*'s turn semaphore iff a session exists and is idle.

        For out-of-band commands (e.g. ``/compact``) that must drive the SAME
        provider without interleaving JSON-RPC with a normal turn. Returns
        ``False`` if there is no session, or a turn already holds the semaphore.

        Atomic wrt other coroutines: the ``locked()`` check and ``acquire()``
        run with no intervening ``await`` suspension — ``acquire()`` on an idle
        ``Semaphore(1)`` decrements and returns synchronously (its ``while``
        loop never runs), so nothing else can slip in between. This closes the
        check-then-act race a bare ``locked()`` check + ``stream_command`` has.
        Pair every ``True`` return with ``release(key)``.
        """
        sess = self._sessions.get(key)
        if sess is None or sess.semaphore.locked():
            return False
        await sess.semaphore.acquire()
        return True

    def active_providers(self) -> list[LLMProvider]:
        """Return the providers of all currently-active sessions.

        Used by dashboard handlers that need to inspect a live backend (e.g.
        the model list or slash commands a claude-agent-acp session advertises)
        without reaching into the private session map.
        """
        return [sess.provider for sess in self._sessions.values()]

    def get_pid(self, key: str) -> int | None:
        """Return the kiro-cli PID for a session, or None."""
        sess = self._sessions.get(self._fold_key(key))
        if not sess:
            return None
        try:
            return sess.provider.client._pid  # type: ignore[attr-defined]
        except AttributeError:
            return None

    def __init__(
        self,
        cfg: KiroCrewConfig,
        provider_factory: ProviderFactory | None = None,
    ):
        self._cfg = cfg
        self._provider_factory = provider_factory
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        # Set True (under _lock) at the top of close_all() so the multi-second
        # pre-shutdown drain window cannot be raced by a new turn: get_or_create
        # refuses once this is set, so a prompt that began AFTER the drain
        # snapshot can't slip in and later get killed mid-turn with its native
        # session lock held (Codex HIGH: drain-window race).
        self._closing = False
        self._start_sem = asyncio.Semaphore(4)  # max 4 concurrent cold-starts
        self._cleanup_task: asyncio.Task | None = None
        self._compacting: set[str] = set()
        # key -> the EXACT _Session object being torn down by the recycle
        # FALLBACK (pop from _sessions + provider SIGKILL). Distinct from
        # _compacting: an in-place compact keeps the entry healthy and
        # reusable. get_or_create skips reuse only when the map still holds
        # THIS object — a healthy replacement registered under the same key
        # is reused normally (never overwritten/leaked by a duplicate
        # cold-start).
        self._recycling: dict[str, "_Session"] = {}
        self._compact_cooldown_until: dict[str, float] = {}
        # Per-session channel conversation to send unattended output to (the
        # auto-compact notice). In-memory on purpose: see set_origin_link.
        self._origin_links: dict[str, ChannelLink] = {}
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._on_compacted: _CompactCallback | None = None
        self._on_recycled: _RecycleCallback | None = None
        self._pool_started = False
        self._session_map = SessionMap()
        # Continuable subagent conversations: session keys registered here
        # opt OUT of the ``_STATELESS_PREFIXES`` treatment even though they
        # carry the ``subagent:`` prefix — their sid persists to the session
        # map, ``session/load`` is armed on the next get_or_create, and
        # ``release(cleanup=True)`` skips session-file deletion. Registered by
        # SubagentManager for ``keep=True`` spawns; unregistered on explicit
        # conversation release.
        self._continuable_keys: set[str] = set()
        self._active_dashboard_slots: set[str] | None = (
            None  # None = uninitialized; empty set = all tabs closed
        )

        # ── Warm Pool ──
        self._pool_size: int = min(_MAX_POOL, max(0, cfg.session.pool_size))
        if cfg.session.pool_size > _MAX_POOL:
            logger.warning(
                "pool_size %d exceeds max %d, clamping", cfg.session.pool_size, _MAX_POOL
            )
        self._pool_agent: str = cfg.session.pool_agent or getattr(cfg.agent, "default_agent", "")
        self._pool_ttl_secs: int = max(0, cfg.session.pool_ttl_secs)
        # Default cwd used by pool processes — matches the workspace-dir
        # fallback in chat_handlers so sessions that didn't pick an explicit
        # project can still claim from the pool.
        self._pool_cwd: str = default_project_dir()
        # Queue stores (provider, spawn_time) tuples for TTL tracking
        self._warm_pool: asyncio.Queue[tuple[LLMProvider, float]] = asyncio.Queue()
        self._pool_fill_lock = asyncio.Lock()
        self._pool_health_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._pool_sweep_pids: set[int] = set()  # PIDs temporarily out of queue during health sweep
        # PIDs of providers that have started (and written their PID to
        # kiro_session_pids.txt) but are not yet registered in self._sessions.
        # The orphan sweep must treat these as active, otherwise a slow ACP
        # cold-start can be SIGKILLed mid-init during the start()→register window.
        self._starting_pids: set[int] = set()
        # Callback fired when a session expires (idle or orphaned).
        # Used by HistoryConsolidator to trigger skill extraction.
        self.on_session_expire: Callable[[str], None] | None = None

        # Shared runtime for _bg callers (title gen, suggestions, folders, nav).
        # Each caller gets its own ephemeral AcpSessionHandle via get_bg_session().
        self._bg_runtime: "AcpRuntime | None" = None
        # Guards lazy creation of _bg_runtime so concurrent callers don't each
        # spawn a runtime and leak all but the last (orphaned subprocesses).
        self._bg_runtime_lock = asyncio.Lock()

        # ── Per-session subagent runtimes (session sharing) ──
        # Maps parent_session_key → shared AcpRuntime for that session's
        # subagents. Lazily created on first subagent spawn when
        # session_sharing=True. Killed when the parent session ends.
        self._subagent_runtimes: dict[str, "AcpRuntime"] = {}
        self._subagent_runtime_locks: dict[str, asyncio.Lock] = {}

        # ── Session Watchdog ──
        # RSS recycle threshold (MiB). 0 disables (default). A non-busy session
        # whose process tree exceeds this is reset on the next cleanup tick.
        # Tolerate a non-int (absent field, or a MagicMock cfg in unit tests) by
        # treating it as disabled — never raise from the constructor.
        _rss_cfg = getattr(cfg.session, "watchdog_rss_max_mb", 0)
        self._rss_max_mb: int = max(0, _rss_cfg) if isinstance(_rss_cfg, int) else 0
        # Idle-sweep gate + clamped timeout are computed once when the cleanup
        # loop starts (it owns the clamp logic) and read by _expire_idle_hook.
        self._idle_sweep_enabled: bool = False
        self._idle_timeout: int = 0
        # The watchdog holds the *execution* half of each cleanup behaviour as a
        # named CleanupHook. Each hook keeps the exact try/except of the inline
        # block it was lifted from, so the dispatcher stays dumb. The orphan-PID
        # sweep is intentionally NOT moved here (it is an inline ~35-line block
        # in _cleanup_loop); extracting it is a refactor deferred to CR 2.
        self._watchdog = SessionWatchdog(
            [
                CleanupHook("idle_expiry", self._expire_idle_hook),
                CleanupHook("orphan_mcp", self._orphan_mcp_hook),
                CleanupHook("rss_threshold", self._rss_threshold_check),
            ]
        )

    async def refresh_defaults(self) -> None:
        """Adopt config changes that only affect NEW sessions.

        For settings that are *defaults* — ``agent.model``,
        ``agent.reasoning_effort`` — the new value must reach the next session
        without a gateway restart, because the provider factory and ``_cfg``
        both capture them when they are built.

        Unlike :meth:`reload_provider_factory`, this deliberately does NOT touch
        ``_sessions``: a default is by definition not retroactive, and shutting
        down live providers to pick one up would kill in-flight turns and lose
        their responses. The warm pool IS drained, because a pre-warmed provider
        was constructed by the old factory and would hand the stale default to
        the very next session — and unlike a live session, a pooled provider has
        no conversation to lose.
        """
        cfg = KiroCrewConfig.load()
        async with self._pool_fill_lock:
            async with self._lock:
                self._cfg = cfg
                self._provider_factory = build_provider_factory(cfg)
                while not self._warm_pool.empty():
                    try:
                        provider, _ = self._warm_pool.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self._discard_pool_provider(provider, "Default changed")
        # Refill with the NEW factory. This is not optional bookkeeping: the
        # health sweep returns early on an empty pool (`if not qsize: return`),
        # so a drained pool would never refill on its own and a configured warm
        # pool would sit empty until the next gateway restart. Done outside both
        # locks — start_pool -> _fill_pool takes _pool_fill_lock itself.
        self._pool_started = False
        if self._pool_health_task and not self._pool_health_task.done():
            self._pool_health_task.cancel()
            self._pool_health_task = None
        await self.start_pool(blocking=False)
        logger.info(
            "Session defaults refreshed: model=%s effort=%r (live sessions untouched)",
            cfg.agent.model,
            cfg.agent.reasoning_effort,
        )

    async def reload_provider_factory(self) -> None:
        """Reload provider factory from current config (after provider switch)."""
        cfg = KiroCrewConfig.load()
        stale: list[tuple[str, Any]] = []
        async with self._pool_fill_lock:
            async with self._lock:
                self._cfg = cfg
                self._provider_factory = build_provider_factory(cfg)
                self._pool_size = min(_MAX_POOL, max(0, cfg.session.pool_size))
                self._pool_agent = cfg.session.pool_agent or getattr(cfg.agent, "default_agent", "")
                self._pool_cwd = default_project_dir()
                # Drain warm pool
                while not self._warm_pool.empty():
                    try:
                        provider, _ = self._warm_pool.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self._discard_pool_provider(provider, "Stale pool drain")
                # Clear all existing sessions (they use the old provider)
                stale = list(self._sessions.items())
                self._sessions.clear()
        # Shut down old sessions outside locks to avoid blocking
        for key, sess in stale:
            try:
                await sess.provider.shutdown()
            except Exception:
                logger.debug(
                    "Failed to shut down session %s on provider switch", key, exc_info=True
                )
        # Reset pool state so start_pool() actually refills
        self._pool_started = False
        if self._pool_health_task and not self._pool_health_task.done():
            self._pool_health_task.cancel()
            self._pool_health_task = None
        await self.start_pool(blocking=False)
        logger.info(
            "Provider factory reloaded: provider=%s, cleared %d sessions",
            cfg.agent.provider,
            len(stale),
        )

    # ── Background Session ──

    async def start_pool(self, *, blocking: bool = True) -> None:
        """Create the background session for cron/heartbeat.

        Chat sessions cold-start on first message via get_or_create().
        """
        if self._pool_started or not self._provider_factory:
            return

        # Prune stale session map entries on startup
        self._session_map.prune()

        self._pool_started = True

        if not blocking:

            async def _start_bg_and_pool() -> None:
                await self._ensure_background()
                await self._fill_warm_pool()
                if self._pool_size:
                    self._pool_health_task = asyncio.create_task(self._pool_health_loop())
                    self._background_tasks.add(self._pool_health_task)
                    self._pool_health_task.add_done_callback(self._background_tasks.discard)

            t = asyncio.create_task(_start_bg_and_pool())
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)
            logger.info("Background session starting (non-blocking)")
            return

        await self._ensure_background()
        logger.info("Background session ready")

        # Fill warm pool after background session is ready
        if self._pool_size:
            t = asyncio.create_task(self._fill_warm_pool())
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)
            self._pool_health_task = asyncio.create_task(self._pool_health_loop())
            self._background_tasks.add(self._pool_health_task)
            self._pool_health_task.add_done_callback(self._background_tasks.discard)

    async def _ensure_background(self) -> None:
        """Create the persistent background session if it doesn't exist."""
        async with self._lock:
            if BACKGROUND_KEY in self._sessions:
                return
        # Create outside lock
        if not self._provider_factory:
            return
        try:
            provider = self._provider_factory(BACKGROUND_KEY, agent="kirocrew-lite")
            async with self._start_sem:
                await provider.start()
        except Exception:
            logger.warning("Failed to create background session", exc_info=True)
            return
        async with self._lock:
            if BACKGROUND_KEY not in self._sessions:
                sess = _Session(provider=provider, is_new=False)
                self._sessions[BACKGROUND_KEY] = sess
                logger.info("Background session created")
            else:
                await provider.shutdown()

    # ── Warm Pool ──

    def _bg_provider_is_kiro(self) -> bool:
        """True when the ``kirocrew-lite`` ``_bg`` agent resolves to the kiro
        (``acp``) backend — the only backend the multiplexed ``AcpRuntime``
        supports. For non-kiro backends ``_bg`` falls back to the provider-backed
        ``_Session`` path serialized by ``Semaphore(1)``.
        """
        try:
            prov = getattr(self._cfg.agent, "provider", "acp") or "acp"
        except Exception:
            prov = "acp"
        return prov == "acp"

    async def get_bg_session(self) -> "AcpSessionHandle | _ProviderBgSession":
        """Acquire a ``_bg`` session handle, dispatching by provider backend.

        kiro (``acp``) → ephemeral ``AcpSessionHandle`` on the shared multiplexed
        ``AcpRuntime`` (each caller gets its own ``sessionId``; runtime creation
        guarded by ``_bg_runtime_lock``; respawn-once on death). non-kiro →
        ``_ProviderBgSession`` over the shared ``BACKGROUND_KEY`` ``_Session``
        serialized by its ``Semaphore(1)``. Caller MUST call ``session.destroy()``
        in a finally block when done.
        """
        if not self._bg_provider_is_kiro():
            await self._ensure_background()
            sess = self._sessions.get(BACKGROUND_KEY)
            if sess is None:
                raise RuntimeError("background session unavailable for non-kiro _bg provider")
            return _ProviderBgSession(sess)

        # circular import: session -> acp.runtime -> acp.client -> session
        from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead

        max_retries = 1
        for attempt in range(max_retries + 1):
            async with self._bg_runtime_lock:
                runtime = self._bg_runtime
                # Recycle a healthy-but-stale runtime (aged out or grown past
                # its RSS threshold — see AcpRuntime._is_stale()) before the
                # normal is_alive() respawn check. Only recycle when zero
                # sessions are registered so we never kill a runtime out from
                # under an in-flight co-tenant prompt.
                #
                # NOTE (race): create_session() registers its queue OUTSIDE this
                # lock (below), so a co-tenant whose session/new is in flight is
                # momentarily invisible to has_active_sessions(); a recycle here
                # could kill the runtime under it. That caller's create_session
                # then raises AcpRuntimeDead and is backstopped by the
                # max_retries respawn loop below (it costs one extra respawn, not
                # a dropped prompt — a mid-prompt session is always registered).
                if runtime is not None and runtime.is_alive():
                    if not runtime.has_active_sessions():
                        reason = await runtime._is_stale()
                        if reason:
                            logger.info(
                                "get_bg_session: recycling stale _bg runtime "
                                "(PID %s, reason=%s)",
                                runtime.pid,
                                reason,
                            )
                            await runtime.kill()
                            self._bg_runtime = None
                    elif runtime._stale_by_age():
                        # Stale but co-tenant sessions are active, so recycling
                        # is skipped this round. Surface it: if a runtime never
                        # reaches a zero-session window the age/RSS bound is
                        # never enforced, and this log makes that observable
                        # rather than silent.
                        logger.info(
                            "get_bg_session: _bg runtime (PID %s) stale by age "
                            "but has %d active session(s); deferring recycle",
                            runtime.pid,
                            len(runtime._session_queues),
                        )

                if self._bg_runtime is None or not self._bg_runtime.is_alive():
                    # Reap the dead runtime before replacing it — kill() releases
                    # its PID tracking + sweep-protection shield. Overwriting
                    # without kill would leak the process and its protected-PID.
                    if self._bg_runtime is not None:
                        try:
                            await self._bg_runtime.kill()
                        except Exception:
                            logger.debug(
                                "get_bg_session: dead _bg runtime kill failed", exc_info=True
                            )
                    runtime = AcpRuntime(agent="kirocrew-lite")
                    await runtime.spawn()
                    self._bg_runtime = runtime
            try:
                return await self._bg_runtime.create_session(agent="kirocrew-lite")
            except AcpRuntimeDead:
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "get_bg_session: _bg runtime died, respawning (attempt %d/%d)",
                    attempt + 1,
                    max_retries,
                )
                async with self._bg_runtime_lock:
                    if self._bg_runtime is not None and not self._bg_runtime.is_alive():
                        try:
                            await self._bg_runtime.kill()
                        except Exception:
                            logger.debug(
                                "get_bg_session: dead _bg runtime kill failed", exc_info=True
                            )
                        self._bg_runtime = None
        raise AcpRuntimeDead("get_bg_session exhausted retries")

    async def get_subagent_runtime(
        self, parent_session_key: str, agent: str | None = None
    ) -> "AcpRuntime":
        """Get or create a shared AcpRuntime for a parent session's subagents.

        Each parent session gets ONE shared runtime that all its subagents
        multiplex onto. Lazily spawned on first call; reused for subsequent
        subagent spawns within the same parent session. Killed when the parent
        session ends (via ``release_subagent_runtime``). Raises ``AcpRuntimeDead``
        if the runtime cannot be spawned/respawned.
        """
        # circular import: session -> acp.runtime -> acp.client -> session
        from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead

        max_retries = 1
        attempt = 0
        while True:
            # Acquire the CURRENT canonical lock for this key each iteration.
            # release_subagent_runtime pops the per-key lock inside its own
            # critical section; if it does so while we are awaiting that same
            # lock, the object we hold becomes stale (a later caller mints a
            # fresh lock for the key). Re-reading + the identity re-check below
            # guarantees we always serialize under the LIVE lock, so two locks
            # can never guard one key and a racing spawn can't orphan a
            # sweep-shielded companion runtime.
            lock = self._subagent_runtime_locks.setdefault(parent_session_key, asyncio.Lock())
            async with lock:
                if self._subagent_runtime_locks.get(parent_session_key) is not lock:
                    # Stale lock: a concurrent release popped it while we waited.
                    # Retry under the live lock (does not consume a spawn retry).
                    continue
                existing = self._subagent_runtimes.get(parent_session_key)
                if existing is not None and existing.is_alive():
                    return existing
                if existing is not None:
                    # Dead runtime being replaced — reap it (kill() releases its
                    # PID tracking + sweep-protection shield) before overwriting.
                    try:
                        await existing.kill()
                    except Exception:
                        logger.debug(
                            "get_subagent_runtime: dead runtime kill failed for %s",
                            parent_session_key,
                            exc_info=True,
                        )
                agent = agent or self._get_session_agent(parent_session_key) or "kirocrew"
                # Mirror the parent's security posture (sandbox + MCP gateway +
                # env) so companion-runtime subagents never run unsandboxed.
                rt_kwargs = self._parent_runtime_kwargs(parent_session_key)
                runtime = AcpRuntime(agent=agent, **rt_kwargs)
                try:
                    await runtime.spawn()
                except AcpRuntimeDead:
                    if attempt >= max_retries:
                        raise
                    attempt += 1
                    logger.warning(
                        "Subagent runtime spawn failed for %s (attempt %d/%d), retrying",
                        parent_session_key,
                        attempt,
                        max_retries + 1,
                        exc_info=True,
                    )
                    continue
                self._subagent_runtimes[parent_session_key] = runtime
                return runtime

    async def release_subagent_runtime(self, parent_session_key: str) -> None:
        """Kill and remove the subagent runtime for a parent session.

        Called when the parent session ends, is reset, removed, or destroyed.
        Safe to call even if no runtime exists for the key. Acquires the per-key
        spawn lock (when present) so a release racing an in-flight
        get_subagent_runtime spawn waits for it to finish, then reaps the
        just-spawned runtime instead of leaving it orphaned with no owner.
        """
        lock = self._subagent_runtime_locks.get(parent_session_key)
        if lock is not None:
            async with lock:
                runtime = self._subagent_runtimes.pop(parent_session_key, None)
                # Popping the lock inside its own critical section is safe:
                # get_subagent_runtime re-reads the canonical lock each iteration
                # and re-checks its identity after acquiring, so a spawn that was
                # waiting on THIS (now-removed) lock detects the staleness and
                # retries under the live lock instead of racing us.
                self._subagent_runtime_locks.pop(parent_session_key, None)
        else:
            runtime = self._subagent_runtimes.pop(parent_session_key, None)
        if runtime is not None:
            try:
                await runtime.kill()
            except Exception:
                logger.warning(
                    "Failed to kill subagent runtime for %s", parent_session_key, exc_info=True
                )

    async def _get_or_bootstrap_run_runtime(
        self, parent_session_key: str, *, agent: str | None = None, cwd: str | None = None
    ) -> "AcpRuntime":
        """Get or lazily bootstrap the task-runner's run-scoped shared runtime.

        Unlike ``get_subagent_runtime`` (a bare ``AcpRuntime`` mirrored from a
        live parent session), the task runner has no live parent session — so a
        bare runtime would miss the MCP-gateway/sandbox config the provider
        factory bakes in from ``KiroCrewConfig``. Instead, cold-start ONE
        fully-configured factory provider, adopt its ``AcpRuntime`` as the run's
        shared runtime, neutralize the factory provider's ownership, and
        terminate its bootstrap session (co-tenant-safe — the process stays
        alive). Every subsequent task/decompose/review session opens its own
        ``create_session`` on this runtime; it is killed exactly once via
        ``release_subagent_runtime(parent_session_key)`` at run end/cancel.
        """
        # No factory (e.g. unit tests) — fall back to a bare companion runtime.
        # Done BEFORE taking the per-parent lock: get_subagent_runtime acquires
        # the same lock and asyncio.Lock is not reentrant.
        if not self._provider_factory:
            return await self.get_subagent_runtime(parent_session_key, agent=agent)

        if parent_session_key not in self._subagent_runtime_locks:
            self._subagent_runtime_locks[parent_session_key] = asyncio.Lock()
        lock = self._subagent_runtime_locks[parent_session_key]
        async with lock:
            existing = self._subagent_runtimes.get(parent_session_key)
            if existing is not None and existing.is_alive():
                return existing
            provider = self._provider_factory(parent_session_key, agent=agent, cwd=cwd)
            await provider.start()
            session_provider = getattr(provider, "_client", None)
            runtime = getattr(session_provider, "_runtime", None)
            if session_provider is not None and runtime is not None:
                # Transfer ownership to _subagent_runtimes so the (soon-dropped)
                # factory provider's shutdown never kills the shared runtime.
                try:
                    session_provider._owns_runtime = False
                except Exception:
                    logger.debug("run runtime ownership transfer failed", exc_info=True)
                self._subagent_runtimes[parent_session_key] = runtime
                # Free the bootstrap session; the process stays alive for the
                # per-step sessions opened on it (co-tenant-safe).
                try:
                    _handle = getattr(session_provider, "_handle", None)
                    _sid = getattr(_handle, "session_id", None) or getattr(
                        _handle, "_session_id", None
                    )
                    if _sid:
                        await runtime.terminate_session(_sid)
                except Exception:
                    logger.debug("run runtime bootstrap-session terminate failed", exc_info=True)
                return runtime
            # Non-AcpRuntime backend (e.g. Claude Code) — can't share a runtime.
            try:
                await provider.shutdown()
            except Exception:
                logger.debug("run runtime bootstrap provider shutdown failed", exc_info=True)
        # Fall back OUTSIDE the lock (get_subagent_runtime takes the same lock).
        return await self.get_subagent_runtime(parent_session_key, agent=agent)

    async def _reacquire_and_validate(self, key: str, sess: "_Session") -> bool:
        """Acquire ``sess``'s per-session semaphore, then re-validate under lock.

        Shared post-semaphore re-check for all three multiplexing paths
        (``get_or_create`` fast path + won-race path, ``open_task_session``).
        Acquires the semaphore with ``self._lock`` RELEASED — it can be held for
        a whole turn, and pinning the global lock across that wait would freeze
        session creation for EVERY key — then re-takes ``self._lock`` and checks
        the session is still the registered one AND its provider is effectively
        alive (``_provider_effectively_alive`` treats a dead CC ``per_session``
        process as alive; it reconnects lazily on the next ``stream()``).

        Returns True with the semaphore STILL HELD (the caller MUST ``release``
        it), or False having ALREADY released it because the session was
        recycled/removed or its provider died during the wait.

        Keeping this acquire→relock→check→release-on-stale sequence in ONE place
        is deliberate: holding the semaphore across the ``_lock`` acquire would
        reintroduce the lock-ordering deadlock every copy of this dance exists to
        avoid, and a divergent copy is exactly how the stale-provider bug class
        this audit remediates gets reintroduced.
        """
        await sess.semaphore.acquire()
        try:
            async with self._lock:
                still_valid = self._sessions.get(key) is sess and _provider_effectively_alive(
                    sess.provider
                )
        except BaseException:
            # Cancelled (or errored) while awaiting self._lock AFTER acquiring the
            # semaphore. The caller never receives the held-semaphore contract, so
            # release it here — otherwise the key stays permanently locked and
            # every subsequent step for it deadlocks. Covers asyncio.CancelledError
            # (a BaseException on 3.8+), hence the broad catch + re-raise.
            sess.semaphore.release()
            raise
        if not still_valid:
            sess.semaphore.release()
        return still_valid

    async def _evict_stale_session(self, key: str, sess: "_Session") -> None:
        """Evict ``sess`` if still registered under ``key`` and shut it down.

        The post-stale cleanup shared by the paths that cold-start a replacement
        (``get_or_create`` fast path and ``open_task_session``). The caller has
        ALREADY released the semaphore (see :meth:`_reacquire_and_validate`);
        this re-takes ``self._lock`` only to remove-if-ours, then shuts the dead
        provider down OFF the lock. If the entry is no longer ours, another
        coroutine already recycled it and owns its teardown, so we leave it be.
        """
        dead: LLMProvider | None = None
        async with self._lock:
            if self._sessions.get(key) is sess:
                del self._sessions[key]
                dead = sess.provider
        if dead is not None:
            try:
                await dead.shutdown()
            except Exception:
                logger.warning("Failed to shut down stale provider for %s", key, exc_info=True)

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
        """Open a task-runner session multiplexed onto the run's shared runtime.

        Unlike ``get_or_create`` (which cold-starts a dedicated provider/process
        per key), every task/decompose/self-review session for one task-runner
        run shares ONE ``AcpRuntime`` keyed by ``parent_session_key`` (via
        ``get_subagent_runtime``). Each call opens its own kiro-cli session
        (``AcpSessionProvider``, ``owns_runtime=False``) on that runtime, so a
        run uses a single process instead of one per step.

        The session is registered in ``self._sessions`` under ``session_key`` so
        the existing key-based helpers keep working: ``check_context_usage`` and
        ``reset(session_key)`` operate on it, and because the provider does not
        own the runtime (and the key is not the runtime's parent key),
        ``reset`` terminates only this session — the shared runtime survives and
        is freed once via ``release_subagent_runtime(parent_session_key)`` at run
        end/cancel.

        Returns ``(provider, is_new, resumed)`` mirroring ``get_or_create``.
        Acquires the per-session semaphore; the caller MUST ``release`` it.
        """
        # circular import: session -> acp.session_provider -> acp.client -> session
        from kiro_crew.acp.session_provider import AcpSessionProvider

        key = self._fold_key(session_key)

        # Fast path: an existing live session for this key — reuse it.
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                existing.last_used = time.monotonic()
                if approval_policy:
                    existing.approval_policy = approval_policy
        if existing is not None:
            # Acquire the per-session semaphore with self._lock RELEASED — it can
            # be held for a whole turn, and pinning the global lock across that
            # wait would freeze open_task_session / get_or_create for EVERY key.
            # Re-validate after acquiring, mirroring get_or_create: while we
            # waited on the semaphore another coroutine may have recycled/removed
            # this session, or its provider's process may have died. Handing back
            # a stale/dead provider here would multiplex a task step onto a
            # terminated runtime; instead fall through to the cold path.
            if await self._reacquire_and_validate(key, existing):
                return existing.provider, False, False
            # Stale between fast-path claim and acquire: the semaphore has
            # already been released by the re-validate; if the dead entry is
            # still ours, evict it and shut the dead provider down before
            # cold-starting a replacement below.
            await self._evict_stale_session(key, existing)

        # Cold path: open a fresh session on the run's shared runtime. Runtime
        # I/O (get_subagent_runtime spawn + create_session) is kept OUTSIDE the
        # global lock to avoid pinning it across subprocess/RPC work.
        runtime = await self._get_or_bootstrap_run_runtime(parent_session_key, agent=agent, cwd=cwd)
        handle = await runtime.create_session(cwd=cwd or None, agent=agent or None)
        provider = AcpSessionProvider(handle, runtime)

        dup: LLMProvider | None = None
        won_race_sess: "_Session | None" = None
        async with self._lock:
            _existing = self._sessions.get(key)
            if _existing is not None:
                # Race: another coroutine registered this key first. Use theirs
                # and tear down our extra session (below, off the lock).
                sess = _existing
                sess.last_used = time.monotonic()
                if approval_policy:
                    sess.approval_policy = approval_policy
                dup = provider
            else:
                sess = _Session(
                    provider=provider,
                    is_new=True,
                    approval_policy=approval_policy,
                    agent=agent or "",
                )
                self._sessions[key] = sess
                won_race_sess = sess
        if dup is not None:
            try:
                await dup.shutdown()  # terminate the redundant session on the shared runtime
            except Exception:
                logger.debug("open_task_session: duplicate session teardown failed", exc_info=True)
            # Lost the same-key race: acquire the WINNER's semaphore through the
            # shared helper, NOT a bare acquire. While we wait a full turn on the
            # winner's semaphore it may be recycled or its runtime may die;
            # handing back sess.provider unchecked would multiplex this task step
            # onto a dead/recycled runtime — the exact stale-provider bug class
            # this consolidation exists to kill (see _reacquire_and_validate).
            if await self._reacquire_and_validate(key, sess):
                return sess.provider, False, False
            # Stale winner: the semaphore was already released by the re-validate.
            # Evict the dead entry if still ours, then retry the cold start from
            # the top (bounded, mirroring get_or_create's won-race retry).
            await self._evict_stale_session(key, sess)
            if _won_race_retries >= _WON_RACE_MAX_RETRIES:
                raise RuntimeError(
                    f"open_task_session({key!r}) exceeded {_WON_RACE_MAX_RETRIES} "
                    "won-race retries — session kept going stale between acquire "
                    "and re-validate"
                )
            return await self.open_task_session(
                parent_session_key,
                session_key,
                agent=agent,
                cwd=cwd,
                approval_policy=approval_policy,
                _won_race_retries=_won_race_retries + 1,
            )
        # We won the race: sess is the fresh session we just created and
        # registered above, so a bare acquire is correct here — there is no
        # window for another coroutine to recycle a session we only just made
        # (matches get_or_create's new-session acquire). The revalidate helper
        # is reserved for reused/won-by-another sessions that waited on a
        # possibly-recycled runtime.
        assert won_race_sess is sess
        await sess.semaphore.acquire()
        return sess.provider, True, False

    def _get_session_agent(self, session_key: str) -> str:
        """Return the agent name for an active session, or empty string."""
        sess = self._sessions.get(session_key)
        if sess is None:
            return ""
        return getattr(sess, "agent", "") or ""

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict:
        """Extract the parent provider's sandbox / MCP-gateway / env config so a
        companion subagent runtime spawns with the SAME security posture as the
        parent (sandboxed + MCP-gateway-routed), never a bare unsandboxed
        process. Returns {} when the parent/client can't be resolved.
        """
        provider = self.get_provider(parent_session_key)
        if provider is None:
            return {}
        client = getattr(provider, "client", None) or getattr(provider, "_client", None)
        if client is None:
            return {}
        kwargs: dict = {}
        for attr, key in (
            ("_sandbox_mode", "sandbox_mode"),
            ("_extra_env", "extra_env"),
            ("_mcp_gateway_overlay", "mcp_gateway_overlay"),
            ("_mcp_gateway_settings_mcp_json", "mcp_gateway_settings_mcp_json"),
            ("_mcp_gateway_socket", "mcp_gateway_socket"),
        ):
            val = getattr(client, attr, None)
            if val is not None:
                kwargs[key] = val
        return kwargs

    def is_session_sharing_eligible(self, parent_session_key: str) -> bool:
        """Check if a parent session can host multiplexed subagent sessions.

        True when the parent session exists and its provider is kiro-cli backed
        (ACP, not claude-agent-acp). Used by SubagentManager to choose the
        shared-runtime path vs the legacy per-subagent-process path.
        """
        sess = self._sessions.get(parent_session_key)
        if sess is None:
            return False
        provider = sess.provider
        return getattr(provider, "is_session_sharing_eligible", False)

    async def _fill_warm_pool(self) -> None:
        """
        Spawn providers up to ``_pool_size`` and enqueue them.
        Pool fill stops on first failure and does not retry until next claim.
        """
        if not self._pool_size or not self._provider_factory:
            return
        async with self._pool_fill_lock:
            while self._warm_pool.qsize() < self._pool_size:
                p = None
                try:
                    p = self._provider_factory(
                        "",
                        agent=self._pool_agent or None,
                        cwd=self._pool_cwd or None,
                    )
                    async with self._start_sem:
                        await p.start()
                    self._warm_pool.put_nowait((p, time.monotonic()))
                    p = None  # successfully enqueued — nothing to clean up
                    logger.info(
                        "Warm pool: spawned process (pool=%d/%d agent=%s)",
                        self._warm_pool.qsize(),
                        self._pool_size,
                        self._pool_agent or "default",
                    )
                except Exception:
                    logger.warning("Warm pool: failed to spawn process", exc_info=True)
                    break
                finally:
                    if p is not None:
                        await self._discard_pool_provider(p, "Warm pool fill cleanup")

    # Bound on the graceful shutdown attempt during a pool discard. The pool
    # health sweep is a single long-lived task: an unbounded await on a wedged
    # shutdown would freeze every future sweep, silently disabling TTL
    # enforcement for the whole pool.
    _POOL_DISCARD_TIMEOUT = 10.0

    @staticmethod
    def _dispatch_hard_kill(provider: LLMProvider) -> None:
        """Fire-and-forget ``_sync_kill_provider`` on the subprocess executor.

        For cancellation handlers, where neither alternative works: awaiting
        the offload re-raises ``CancelledError`` at the ``await`` and skips the
        kill, while calling ``_sync_kill_provider`` inline blocks the event
        loop (``os.waitpid`` on POSIX, a ``taskkill`` subprocess on Windows).
        Submission itself is synchronous and non-blocking, so the kill is
        guaranteed to be dispatched before the handler re-raises; it then
        completes on a worker thread. If the executor is already shut down
        (gateway teardown), a dedicated daemon thread carries the kill instead
        — the loop must never run ``_sync_kill_provider`` inline, because it
        blocks (``os.waitpid`` on POSIX, a ``taskkill`` subprocess on Windows)
        and a blocked loop can trip the watchdog; a daemon thread dying with
        the process is the acceptable trade.
        """
        try:
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                _sync_kill_provider,
                provider,
            )
        except RuntimeError:
            threading.Thread(
                target=_sync_kill_provider,
                args=(provider,),
                daemon=True,
            ).start()

    async def _discard_pool_provider(self, provider: LLMProvider, context: str) -> None:
        """Shut down a discarded pool provider and verify its process is gone.

        A discard removes the provider from all pool bookkeeping, so this is
        the LAST code path that will ever signal the process — a shutdown that
        fails (or silently fails to kill) leaks the full provider process tree
        until the next gateway restart. The orphan sweep cannot backstop this:
        it only reaps *reparented* processes, and a leaked pool child stays
        parented to its live launcher. Three guarantees:

        1. **Bounded** — the graceful shutdown is capped so a wedged provider
           can't stall the caller.
        2. **Loud** — shutdown failures are logged, never swallowed.
        3. **Verified against the OS** — liveness is re-checked after shutdown
           and any survivor is hard-killed via its tracked PID. The PID is
           captured BEFORE shutdown and probed with ``pid_exists`` because the
           provider's own bookkeeping (``is_process_alive``) self-reports dead
           once its kill path has *run* — even when signal delivery silently
           failed and the OS process is still alive.
        """
        # Resolve the OS handle before shutdown mutates provider state.
        # ``_client`` first (the attribute _sync_kill_provider kills through),
        # falling back to the public ``client`` accessor the pool sweep reads —
        # on real providers ``client`` is a passthrough property for
        # ``_client``, so probing both spellings guarantees this verification
        # can never resolve a different PID than either of those paths.
        _client = getattr(provider, "_client", None) or getattr(provider, "client", None)
        pid = getattr(_client, "_pid", None)
        try:
            await asyncio.wait_for(provider.shutdown(), timeout=self._POOL_DISCARD_TIMEOUT)
        except asyncio.CancelledError:
            # Cancellation handler: cannot await (the await would re-raise and
            # skip the kill) and must not block the loop — dispatch the kill
            # to a worker thread and re-raise immediately.
            self._dispatch_hard_kill(provider)
            raise
        except Exception:
            logger.warning(
                "%s: provider shutdown failed — falling back to hard kill",
                context,
                exc_info=True,
            )
        except BaseException:
            self._dispatch_hard_kill(provider)  # same rationale as the CancelledError arm
            raise
        # OS-truth probe on the pre-captured PID; fall back to the provider's
        # own view when no PID was resolvable (e.g. providers without a
        # tracked client PID).
        if isinstance(pid, int):
            still_alive = platform_compat.pid_exists(pid)
        else:
            try:
                still_alive = hasattr(provider, "is_process_alive") and provider.is_process_alive()
            except Exception:
                still_alive = False
        if still_alive:
            logger.warning(
                "%s: provider process (pid=%s) still alive after shutdown — hard-killing",
                context,
                pid,
            )
            # Normal (non-cancellation) path: offload — on Windows kill_pid
            # shells out to taskkill, a blocking call that must not run on the
            # event loop. Isolated so one provider's failure (e.g. an executor
            # shutdown race) can never abort a caller iterating a batch of
            # discards — the health sweep discards several providers in one
            # pass, and an escaping exception here would leak the rest.
            try:
                await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    _sync_kill_provider,
                    provider,
                )
            except Exception:
                logger.warning(
                    "%s: executor hard kill failed (pid=%s) — dispatching to a " "dedicated thread",
                    context,
                    pid,
                    exc_info=True,
                )
                self._dispatch_hard_kill(provider)

    def _claim_from_pool(self, agent: str | None) -> tuple[LLMProvider, float] | None:
        """Try to claim a pre-warmed provider if the agent matches.
        Deny-by-default: normalize both sides and positively compare.
        None/empty agent means "use default" → promoted to pool_agent.
        """
        if self._warm_pool.empty():
            return None
        requested = agent if agent else (self._pool_agent or "")
        pool = self._pool_agent or ""
        if requested != pool:
            return None
        try:
            return self._warm_pool.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _drain_and_claim(self, agent: str | None) -> LLMProvider | None:
        """Claim a live, non-stale provider from the warm pool."""
        discarded = False
        claimed = self._claim_from_pool(agent)
        while claimed is not None:
            provider, spawn_time = claimed
            # Check TTL (0 = disabled)
            age = time.monotonic() - spawn_time
            if self._pool_ttl_secs and age > self._pool_ttl_secs:
                logger.warning(
                    "Warm pool: %.0fs old provider exceeds TTL %ds, discarding",
                    age,
                    self._pool_ttl_secs,
                )
                discarded = True
                await self._discard_pool_provider(provider, "Warm pool discard")
                claimed = self._claim_from_pool(agent)
                continue
            # Check liveness — use process-level check, not is_alive/is_responsive
            # which has a 600s stale-activity threshold.  Pool processes are
            # expected to be idle (no I/O after init) so the stale check would
            # falsely discard healthy processes after ~10 min.
            alive = hasattr(provider, "is_process_alive") and provider.is_process_alive()
            if not alive:
                rc = provider.exit_code if hasattr(provider, "exit_code") else None
                logger.warning(
                    "Warm pool: claimed provider is dead (returncode=%s), discarding", rc
                )
                discarded = True
                await self._discard_pool_provider(provider, "Warm pool discard")
                claimed = self._claim_from_pool(agent)
                continue
            return provider
        # No healthy provider found — replenish if we discarded any
        if discarded:
            self._schedule_replenish()
        return None

    def _schedule_replenish(self) -> None:
        """Fire-and-forget task to refill the warm pool after a claim."""
        if not self._pool_size:
            return
        t = asyncio.create_task(self._fill_warm_pool())
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)

    def _pool_pids(self) -> set[int]:
        """Return PIDs of all providers currently in the warm pool (non-destructive peek)."""
        pids: set[int] = set()
        # Drain and re-enqueue to peek without losing entries
        items: list[tuple[LLMProvider, float]] = []
        while not self._warm_pool.empty():
            try:
                entry = self._warm_pool.get_nowait()
                items.append(entry)
            except asyncio.QueueEmpty:
                break
        for provider, spawn_time in items:
            pid = getattr(getattr(provider, "client", None), "_pid", None)
            if isinstance(pid, int):
                pids.add(pid)
            self._warm_pool.put_nowait((provider, spawn_time))
        pids.update(self._pool_sweep_pids)
        return pids

    def _in_flight_pids(self) -> set[int]:
        """PIDs of providers that have started but aren't registered yet.

        Unioned into the orphan-sweep active set so a slow cold-start isn't
        swept during the start()→register window. Returns a copy.
        """
        return set(self._starting_pids)

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
          (kirocrew-lite title-gen / memory consolidation).

        Both are shielded from the sweep by unioning their live PIDs into the
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
        bg = self._bg_runtime
        try:
            if bg is not None and bg.is_alive() and isinstance(bg.pid, int):
                pids.add(bg.pid)
        except Exception:
            logger.debug("bg runtime pid probe failed", exc_info=True)
        return pids

    _POOL_HEALTH_INTERVAL = 30  # seconds between health sweeps

    async def _pool_health_loop(self) -> None:
        """Periodically sweep the warm pool, discard dead/expired providers, and refill."""
        while True:
            await asyncio.sleep(self._POOL_HEALTH_INTERVAL)
            try:
                await self._sweep_warm_pool_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pool health sweep failed")

    async def _sweep_warm_pool_once(self) -> None:
        """One health sweep: drain the pool, keep healthy entries, reap the rest."""
        if not self._pool_size:
            return
        qsize = self._warm_pool.qsize()
        if not qsize:
            return
        logger.debug(
            "Pool health: sweeping %d providers (target=%d, ttl=%ds)",
            qsize,
            self._pool_size,
            self._pool_ttl_secs,
        )
        # Drain entire queue, keep healthy entries, discard the rest
        healthy: list[tuple[LLMProvider, float]] = []
        to_shutdown: list[LLMProvider] = []
        now = time.monotonic()
        try:
            for _ in range(qsize):
                try:
                    provider, spawn_time = self._warm_pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                age = now - spawn_time
                pid = getattr(getattr(provider, "client", None), "_pid", None)
                if isinstance(pid, int):
                    self._pool_sweep_pids.add(pid)
                if self._pool_ttl_secs and age > self._pool_ttl_secs:
                    logger.warning(
                        "Pool health: %.0fs old provider (pid=%s) exceeds TTL %ds, discarding",
                        age,
                        pid,
                        self._pool_ttl_secs,
                    )
                    to_shutdown.append(provider)
                    continue
                try:
                    alive = hasattr(provider, "is_process_alive") and provider.is_process_alive()
                except Exception:
                    alive = False
                if not alive:
                    rc = provider.exit_code if hasattr(provider, "exit_code") else None
                    logger.warning(
                        "Pool health: dead provider (pid=%s, returncode=%s, age=%.0fs), discarding",
                        pid,
                        rc,
                        age,
                    )
                    to_shutdown.append(provider)
                    continue
                logger.debug("Pool health: provider pid=%s alive (age=%.0fs)", pid, age)
                healthy.append((provider, spawn_time))
        finally:
            # Re-enqueue survivors first, then shut down dead providers.
            # This avoids an empty-queue window where _drain_and_claim()
            # would fall back to cold start.  CancelledError during
            # shutdown may skip remaining providers in to_shutdown —
            # acceptable because they're already dead/expired and their
            # PIDs are tracked in kiro_session_pids.txt for startup
            # cleanup.  Sweep PIDs are cleared in a nested finally so
            # they can't go stale regardless of how we exit.
            try:
                for entry in healthy:
                    self._warm_pool.put_nowait(entry)
                for p in to_shutdown:
                    await self._discard_pool_provider(p, "Pool health discard")
            finally:
                self._pool_sweep_pids.clear()
        removed = qsize - len(healthy)
        if removed:
            logger.info(
                "Pool health: removed %d dead/expired, %d healthy remain",
                removed,
                len(healthy),
            )
            self._schedule_replenish()
        else:
            logger.debug("Pool health: all %d providers healthy", len(healthy))

    def context_info(self) -> list[dict[str, object]]:
        """Return context usage for all active sessions."""
        from kiro_crew.providers.acp import AcpProvider  # circular import: providers -> session

        result: list[dict[str, object]] = []
        for key, sess in self._sessions.items():
            pct = sess.provider.context_usage_pct()
            model = "unknown"
            agent = ""
            if ClaudeCodeProvider is not None and isinstance(sess.provider, ClaudeCodeProvider):
                model = sess.provider._model or "auto"
                agent = sess.provider._agent or ""
            elif isinstance(sess.provider, AcpProvider):
                model = sess.provider.client._model or "auto"
                agent = sess.provider.client._agent or ""
                if model == "auto" and agent and agent != "kirocrew":
                    model = self._resolve_agent_model(agent)
                model = model or "auto"
            # Human-readable name
            if key == BACKGROUND_KEY:
                name = "Background (titles, cron, heartbeat)"
            elif key.startswith("dashboard:"):
                name = f"Chat ({key.split(':', 1)[1]})"
            else:
                name = key
            # Real served window (tokens), when the provider reports it. The
            # dashboard prefers this over re-deriving the window from the model
            # id, which can disagree with the window the adapter actually used
            # (e.g. a "[1m]" id served at 200k by Bedrock).
            window = 0
            if hasattr(sess.provider, "context_window_tokens"):
                window = sess.provider.context_window_tokens()
            result.append(
                {
                    "key": key,
                    "name": name,
                    "model": model,
                    "agent": agent,
                    "context_pct": round(pct, 1),
                    "context_window_tokens": window,
                    "prompts": sess.prompt_count,
                }
            )
        return result

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
        from kiro_crew.agent import kiro_agents_dir_path

        try:
            dir_mtime = kiro_agents_dir_path().stat().st_mtime
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
            import json as _json

            for af in kiro_agents_dir_path().glob("*.json"):
                try:
                    ad = _json.loads(af.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if ad.get("name") == agent or af.stem == agent:
                    model = ad.get("model", "auto")
                    break
        except Exception:
            pass
        cache[agent] = (model, dir_mtime, now)
        return model

    async def recycle_background(self) -> None:
        """Check background session context and recycle if too full.

        Background tasks are stateless (cron, heartbeat, lessons), so we
        don't need compaction — just kill the old session and create a fresh
        one.  Called after each background task completes.

        Thresholds are more aggressive than chat compaction:
        - At ≥ 70% context → recycle
        - After 40 prompts with no metadata → recycle (blind fallback)
        """
        session = self._sessions.get(BACKGROUND_KEY)
        if not session:
            return

        pct = session.provider.context_usage_pct()
        needs_recycle = pct >= _BG_RECYCLE_PCT
        if not needs_recycle and pct == 0.0:
            # Blind fallback: recycle after N prompts if metadata never reports %
            needs_recycle = session.prompt_count >= _BG_BLIND_RECYCLE_PROMPTS

        if not needs_recycle:
            return

        reason = f"context at {pct:.0f}%" if pct > 0 else f"blind ({session.prompt_count} prompts)"
        logger.info("Recycling background session — %s", reason)

        # Kill old session
        async with self._lock:
            old = self._sessions.pop(BACKGROUND_KEY, None)
        if old:
            await old.provider.shutdown()

        # Create fresh replacement
        await self._ensure_background()

    async def recycle_heartbeat(self) -> None:
        """Tear down the heartbeat session at the end of a cycle.

        Mirrors :meth:`recycle_background` but for ``HEARTBEAT_KEY``.  Called
        once per heartbeat cycle (after all tasks finish), NOT per task —
        per-task recycle would tear down the session under concurrent
        ``asyncio.gather``'d siblings sharing the same key.

        Unconditional, unlike :meth:`recycle_background`.  Heartbeat's
        published contract is "fresh context each cycle"
        (``config/prompt.md``), and every entry is re-read from
        ``HEARTBEAT.md`` each cycle, so a retained transcript supplies
        nothing the next cycle depends on while costing input tokens on
        every tick.  It is also actively wrong to keep: for a watch task the
        external system — not the prior transcript — is the source of truth,
        and unrelated queued tasks would otherwise inherit each other's
        reasoning.  Heartbeat runs in the background with nobody waiting on
        a tick, so the per-cycle cold-start it costs is unobserved.

        No-op if the heartbeat session was never created (cycle had no
        tasks) or already torn down by a per-task timeout reset.
        """
        session = self._sessions.get(HEARTBEAT_KEY)
        if not session:
            return

        pct = session.provider.context_usage_pct()
        logger.info("Recycling heartbeat session — cycle end (context at %.0f%%)", pct)

        # Kill old session — next get_or_create(HEARTBEAT_KEY) will create
        # a fresh one (no eager _ensure_heartbeat — heartbeat sessions are
        # only spawned on demand, unlike the persistent background session).
        async with self._lock:
            old = self._sessions.pop(HEARTBEAT_KEY, None)
        if old:
            await old.provider.shutdown()

    async def get_or_create(
        self,
        key: str,
        agent: str | None = None,
        channel_id: str | None = None,
        approval_policy: str = "",
        model: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        _won_race_retries: int = 0,
        **extra_factory_kwargs: Any,
    ) -> tuple[LLMProvider, bool, bool]:
        """Return ``(LLMProvider, is_new, resumed)`` for *key*, creating if needed.

        ``resumed`` is True when the session was restored via ACP session/load
        (kiro-cli has full native history — skip thread history injection).

        For new sessions, tries the warm pool first for instant startup.
        If the session is mid-compaction, creates a fresh one instead.
        Acquires the per-session semaphore before returning — caller MUST
        call ``release(key)`` when done.

        Args:
            agent: Optional agent name for ``session/set_mode``.  Non-default
                agents skip the warm pool (cold start only).
            model: Optional model override for the session.  When ``None``,
                falls back to the global ``agent.model`` config — but only when
                the named agent does not pin its own model (a per-agent pin
                outranks the global fallback) and the global is not a sentinel
                value like ``"auto"``, in which case it stays ``None`` to let
                the backend resolve from the agent's own JSON config.  Flows
                through to the provider factory as the ``model_override`` kwarg.
        """
        # Fast path: existing session — hold lock only briefly
        # Fold bare/canonical Slack key aliases FIRST: the SessionMap thread
        # index returns canonical ``slack:<ts>`` keys while first-message
        # derivation historically registered the bare ``thread_ts``. Without
        # the fold, the second in-thread message misses the live session and
        # cold-starts a context-free duplicate (thread split).
        key = self._fold_key(key)
        stale_provider = None
        _claimed: "tuple[_Session, bool] | None" = None
        try:
            async with self._lock:
                # Refuse to start OR resume any turn once teardown has begun.
                # close_all() sets _closing under this same lock BEFORE it
                # snapshots providers to drain; anything reaching here after
                # that would be absent from the drain set and get killed
                # mid-turn with its native lock held (Codex HIGH: drain-window
                # race). Reject fresh sessions and reuse alike — no new prompt
                # may begin during shutdown.
                if self._closing:
                    raise SessionClosingError(
                        "SessionManager is closing (gateway restart/shutdown in "
                        "progress); refusing to start or resume a turn"
                    )
                # Skip the live-session branch only while the recycle FALLBACK
                # is tearing down the EXACT session object still in the map.
                # In-place compaction — both the kiro-cli and claude paths —
                # keeps the entry healthy, so concurrent get_or_create must
                # reuse it (queueing on the session semaphore behind the
                # compact). A healthy REPLACEMENT registered under the same
                # key during a recycle is likewise reused — the object match
                # keeps the guard from exiling it and cold-starting a
                # duplicate provider that would overwrite _sessions[key] and
                # leak the replacement's process.
                _existing = self._sessions.get(key)
                _is_recycling = _existing is not None and self._recycling.get(key) is _existing
                if _existing is not None and not _is_recycling:
                    sess = _existing
                    # If the provider's process died (crash, SIGKILL, etc.),
                    # remove the stale entry so we fall through to cold-start
                    # with is_new=True — ensuring full context re-injection.
                    # Use process-level check, not is_alive() which has a 600s
                    # stale-activity threshold that falsely kills idle sessions.
                    if hasattr(sess.provider, "is_process_alive"):
                        _alive = sess.provider.is_process_alive()
                    else:
                        _alive = sess.provider.is_alive()
                    if not _alive:
                        # CC per_session: process died but session state is on
                        # disk — reconnect transparently instead of removing.
                        if (
                            ClaudeCodeProvider is not None
                            and isinstance(sess.provider, ClaudeCodeProvider)
                            and sess.provider.connection_mode == "per_session"
                        ):
                            logger.info(
                                "Session %s CC process dead — will reconnect on next stream()", key
                            )
                            _alive = True  # keep session, reconnect lazily
                        else:
                            logger.warning(
                                "Session %s has dead provider — removing stale entry", key
                            )
                            stale_provider = sess.provider
                            del self._sessions[key]
                            # Preserve session_map entry: the kiro-cli session
                            # files survive on disk, enabling lossless resume
                            # via session/load on the next get_or_create().
                    if _alive:
                        # agent is not updated: subagent session keys are unique
                        # per spawn so a key collision with a different agent
                        # cannot happen in practice.
                        sess.last_used = time.monotonic()
                        was_new = sess.is_new
                        sess.is_new = False
                        # Lazy-save CC session_id: init event fires after
                        # registration, so the first get_or_create that finds
                        # a live session with a populated session_id persists it.
                        if (
                            ClaudeCodeProvider is not None
                            and isinstance(sess.provider, ClaudeCodeProvider)
                            and sess.provider.session_id
                            and not self._session_map.get(key)
                        ):
                            self._session_map.set(
                                key,
                                sess.provider.session_id,
                                provider="claude_code",
                                cwd=sess.provider.cwd,
                            )
                        # Claim this session, but DON'T acquire its semaphore
                        # while holding self._lock. The semaphore can be held a
                        # long time (a whole turn); a wedged turn (e.g. a dead
                        # _bg ACP process) would otherwise pin self._lock and
                        # freeze get_or_create for EVERY session. Acquire below,
                        # after the lock is released, then re-validate.
                        _claimed = (sess, was_new)

                if _claimed is None:
                    if not self._provider_factory:
                        raise RuntimeError("No provider factory configured")
                    factory = self._provider_factory
        finally:
            # Kill orphaned child processes (MCP servers, kiro-cli-chat)
            # outside the lock — shutdown() may involve signals/waitpid.
            if stale_provider is not None:
                try:
                    await stale_provider.shutdown()
                except Exception:
                    logger.warning("Failed to shut down stale provider for %s", key, exc_info=True)

        # Existing session claimed above: acquire its semaphore HERE, with
        # self._lock released, so a long-held turn can't pin the global lock
        # and freeze every other session's get_or_create. Re-validate after
        # acquiring: another coroutine may have recycled/removed this session
        # while we waited on the semaphore — if so, fall through to cold-start.
        if _claimed is not None:
            sess, was_new = _claimed
            if await self._reacquire_and_validate(key, sess):
                return sess.provider, was_new, False
            # Stale between claim and acquire — the semaphore has already been
            # released by the re-validate. If the entry is still ours but the
            # provider died, evict it and shut the dead provider down (mirrors
            # the live-path stale handling above); otherwise another coroutine
            # already recycled it. Then set `factory` for the cold-start path,
            # which the `if _claimed is None` block skipped when we claimed a
            # then-live session.
            await self._evict_stale_session(key, sess)
            if not self._provider_factory:
                raise RuntimeError("No provider factory configured")
            factory = self._provider_factory

        # Resolve the session model here — on the cold-start path only.
        # Existing-session reuse returns above via the fast path without needing
        # it, so deferring past that short-circuit keeps per-agent resolution
        # (which globs + reads ``~/.kiro/agents/*.json``) off the hot path for
        # already-live sessions.
        if model is None:
            # KiroACP-only: the effective model is the kiro/ACP slot.
            #
            # Precedence: the KiroCrew agent's own model > the bound kiro
            # agent's pin > the global default (a *fallback*, which must not
            # override a tier that pins its own model). Resolved by
            # ``_session_model`` so every surface — dashboard, Slack, cron,
            # spawn — gets the same answer for the same agent.
            #
            # Offloaded to an executor: the resolver globs + read_text()s over
            # ~/.kiro/agents/*.json, so on a slow/large agents dir a cold start
            # would otherwise block the loop (and every concurrent task) for the
            # duration. No lock or session semaphore is held here, so awaiting
            # is safe.
            model = await asyncio.get_running_loop().run_in_executor(
                None, _session_model, self._cfg, agent
            )

        # Check session map for resume — only for long-lived sessions.
        # ``_hb`` is stateless alongside ``_bg``: heartbeat's published
        # contract is "fresh context each cycle" (config/prompt.md), and each
        # entry is re-read from HEARTBEAT.md every cycle, so resuming a prior
        # transcript supplies nothing while costing input tokens every tick.
        resume_sid: str | None = None
        is_stateless = (
            key in (BACKGROUND_KEY, HEARTBEAT_KEY)
            or any(key.startswith(p) for p in _STATELESS_PREFIXES)
        ) and key not in self._continuable_keys
        if not is_stateless:
            resume_sid = self._session_map.get(key)

        # Try warm pool first (no resume — pooled processes have no prior session)
        logger.info(
            "Pool decision: key=%s resume_sid=%s model=%s agent=%s pool_size=%d pool_qsize=%d cwd=%s pool_cwd=%s",
            key,
            resume_sid,
            model,
            agent,
            self._pool_size,
            self._warm_pool.qsize(),
            cwd,
            self._pool_cwd,
        )
        # Only bypass pool for cwd if it's a user-chosen project that differs
        # from the default workspace dir (which pool processes already use).
        _provider_switched = False
        cwd_blocks_pool = bool(cwd and cwd != self._pool_cwd)
        pooled = (
            None
            if resume_sid or is_stateless or not self._pool_size or cwd_blocks_pool or extra_env
            else await self._drain_and_claim(agent)
        )
        if pooled is not None:
            provider = pooled
            try:
                # Re-key pooled provider with actual session parameters
                from kiro_crew.providers.acp import (
                    AcpProvider,  # circular import: providers -> session
                )

                if isinstance(provider, AcpProvider):
                    provider.client.rekey(key, channel_id)
                    # Switch model post-claim if caller requested non-default.
                    if model:
                        _pool_model = (
                            self._resolve_agent_model(self._pool_agent)
                            if self._pool_agent
                            else None
                        )
                        # The requested `model` is a canonical/wire value while
                        # `_pool_model` is the pool agent's raw model slot — two
                        # namespaces. Normalize BOTH to the backend's provider ids
                        # before the equality check so an already-equivalent pooled
                        # process is not needlessly re-switched, and the value sent
                        # to set_model is a provider id the backend accepts. kiro
                        # (the "acp" provider) needs its bare dotted id (e.g.
                        # "claude-opus-4.8") via to_acp_id, which translates ONLY
                        # canonical keys and passes kiro's native ids/aliases
                        # (claude-haiku-4.5, …) through unchanged — DISTINCT real
                        # kiro models that must not be folded to Sonnet the way the
                        # claude backend's to_provider_id downgrades them. The
                        # claude backend needs the global.anthropic.* id.
                        if _is_claude_backend(provider):
                            _switch_model = model_registry.to_provider_id(model, "claude_code")
                            _cmp_pool = (
                                model_registry.to_provider_id(_pool_model, "claude_code")
                                if _pool_model
                                else _pool_model
                            )
                        else:
                            _switch_model = model_registry.to_acp_id(model)
                            _cmp_pool = (
                                model_registry.to_acp_id(_pool_model) if _pool_model else _pool_model
                            )
                        if _pool_model and _switch_model != _cmp_pool:
                            await provider.client.set_model(_switch_model)
                            logger.info("Pool post-claim: switched model to %s", _switch_model)
                logger.info(
                    "Claimed warm-pool process for %s (agent=%s)", key, agent or self._pool_agent
                )
                self._schedule_replenish()
            except (asyncio.CancelledError, Exception):
                _sync_kill_provider(provider)
                raise
        else:
            # Cold start: start provider OUTSIDE the lock so other sessions
            # can proceed in parallel.  Semaphore limits concurrent cold-starts
            # to avoid CPU saturation from multiple kiro-cli processes.
            # On resume, use the CWD stored in session_map so CC CLI finds
            # its conversation in the correct project directory.
            effective_cwd = cwd
            if not effective_cwd and resume_sid:
                stored_cwd = self._session_map.get_cwd(key)
                if stored_cwd and Path(stored_cwd).is_dir():
                    effective_cwd = stored_cwd
                    logger.info("Resume CWD override for %s: %s", key, stored_cwd)
            provider = factory(
                key,
                agent=agent,
                channel_id=channel_id,
                model_override=model,
                cwd=effective_cwd,
                extra_env=extra_env,
                **extra_factory_kwargs,
            )
            # Provider switch detection: if session was created by a different
            # provider (e.g. kiro->CC or CC->kiro), the resume_sid is from the
            # wrong runtime and unusable. Discard it and clear the stored SID.
            # KiroCrew's conversation_log will inject history via
            # build_session_replay on the first prompt (provider_switch_replay flag).
            _provider_switched = False
            if resume_sid:
                is_cc_now = (
                    ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider)
                ) or _is_claude_backend(provider)
                current_provider = "claude_code" if is_cc_now else "acp"
                if detect_provider_switch(self._session_map, key, current_provider):
                    resume_sid = None
                    _provider_switched = True
                    # Clear the incompatible SID from session_map so future
                    # lookups don't try to resume with a stale ID.
                    self._session_map.clear_sid(key)

            # Set resume ID before start() triggers _initialize_session
            if resume_sid:
                from kiro_crew.providers.acp import (
                    AcpProvider,  # circular import: providers -> session
                )

                if isinstance(provider, AcpProvider):
                    provider.client.set_resume_session_id(resume_sid)
                    logger.info("Attempting session/load for %s (sid=%s)", key, resume_sid)
                elif ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
                    provider.set_resume_session_id(resume_sid)
                    logger.info("CC resume for %s (sid=%s)", key, resume_sid)
            async with self._start_sem:
                try:
                    await provider.start()
                except (asyncio.CancelledError, Exception):
                    # Provider process may have spawned before the cancel/error —
                    # shut it down so it doesn't leak.  Use synchronous kill as
                    # a last resort since asyncio.shield is unreliable during
                    # cancellation (the awaited future raises CancelledError
                    # immediately, leaving shutdown fire-and-forget).
                    _sync_kill_provider(provider)
                    raise

        # start() has written the provider's PID to kiro_session_pids.txt, but
        # the session is not registered in self._sessions yet. Guard the PID so
        # the periodic orphan sweep doesn't kill it during this window. Removed
        # in the finally below once registration (or teardown) completes.
        _sp = getattr(getattr(provider, "client", None), "_pid", None)
        if not isinstance(_sp, int):
            _cc = getattr(provider, "_proc", None)
            _sp = _cc.pid if (_cc is not None and _cc.returncode is None) else None
        _starting_pid = _sp if isinstance(_sp, int) else None
        if _starting_pid is not None:
            self._starting_pids.add(_starting_pid)

        # Everything after start() must be wrapped so that a CancelledError
        # between start() and session registration doesn't orphan the process.
        _won_race_sess: "_Session | None" = None
        _dup_provider: "LLMProvider | None" = None
        try:
            # Check if session was resumed
            resumed = False
            from kiro_crew.providers.acp import AcpProvider  # circular import: providers -> session

            if isinstance(provider, AcpProvider):
                resumed = provider.client.resumed

            async with self._lock:
                # Re-check: another coroutine may have created this key while we
                # were starting the provider (race on same key). In-place
                # compaction (kiro-cli and claude) leaves the existing entry
                # healthy, so reuse it even when _compacting is set; only an
                # entry that IS the exact object the recycle FALLBACK is
                # tearing down should fall through to register us — a healthy
                # replacement under the same key must be reused, never
                # overwritten. The recycle path also pops by object identity,
                # so even if we do register over a being-recycled entry, only
                # the old session object is killed.
                _existing = self._sessions.get(key)
                _is_recycling = _existing is not None and self._recycling.get(key) is _existing
                if _existing is not None and not _is_recycling:
                    # Another task won the race — use theirs and shut down our
                    # duplicate provider below, after the lock is released
                    # (shutdown() involves subprocess teardown; no need to hold
                    # the global lock across it). Claim the winner here but DON'T
                    # acquire its semaphore under self._lock: _existing's
                    # semaphore may be held by a long-running turn, and blocking
                    # on it here would pin the global lock and freeze every other
                    # session (the same deadlock class fixed on the fast path).
                    sess = _existing
                    sess.last_used = time.monotonic()
                    if approval_policy:
                        sess.approval_policy = approval_policy
                    if agent:
                        sess.agent = agent
                    _won_race_sess = sess
                    _dup_provider = provider
                else:
                    sess = _Session(
                        provider=provider,
                        is_new=False,
                        approval_policy=approval_policy,
                        agent=agent or "",
                    )
                    if _provider_switched or (
                        getattr(provider, "_history_replay_needed", False) is True
                    ):
                        # provider_switch_replay OR F2 load-recovery fell back to
                        # a fresh native session (stale lock never cleared):
                        # replay KiroCrew's conversation_log into the new session
                        # on the first prompt so the slot isn't context-free.
                        sess.provider_switch_replay = True
                    self._sessions[key] = sess
                    logger.info(
                        "New session: %s agent=%s resumed=%s provider_switch=%s (total=%d)",
                        key,
                        agent or "kirocrew",
                        resumed,
                        _provider_switched,
                        len(self._sessions),
                    )

                    # Save session mapping for long-lived sessions
                    _cwd_str = provider.cwd
                    if not is_stateless and isinstance(provider, AcpProvider):
                        sid = provider.client._session_id
                        _prov_label = "claude_code" if _is_claude_backend(provider) else "acp"
                        if sid:
                            self._session_map.set(key, sid, provider=_prov_label, cwd=_cwd_str)
                    elif (
                        not is_stateless
                        and ClaudeCodeProvider is not None
                        and isinstance(provider, ClaudeCodeProvider)
                    ):
                        sid = provider.session_id
                        if sid:
                            self._session_map.set(key, sid, provider="claude_code", cwd=_cwd_str)

                    if self._cleanup_task is None or self._cleanup_task.done():
                        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

                    await sess.semaphore.acquire()
                    Stats().inc_session_created()

                    result = (provider, True, resumed)
        except BaseException:
            # CancelledError or any other exception after provider.start()
            # succeeded — provider is running but never registered.  Kill it.
            _sync_kill_provider(provider)
            raise
        finally:
            # Registration is complete (or the provider was killed) — the PID is
            # now either in self._sessions or dead, so drop the start-up guard.
            if _starting_pid is not None:
                self._starting_pids.discard(_starting_pid)

        # Lost the same-key race: shut down our duplicate provider (outside the
        # lock — subprocess teardown), then acquire the winner's semaphore HERE,
        # with self._lock released, so a long-held turn on the winning session
        # can't pin the global lock and freeze every other session's
        # get_or_create (the same deadlock class fixed on the fast path).
        if _won_race_sess is not None:
            if _dup_provider is not None:
                try:
                    await _dup_provider.shutdown()
                except Exception:
                    logger.warning(
                        "Failed to shut down duplicate provider for %s", key, exc_info=True
                    )
            if await self._reacquire_and_validate(key, _won_race_sess):
                return _won_race_sess.provider, False, False
            # Stale winner: the semaphore has already been released by the
            # re-validate; retry from the top (cold-starts cleanly). Bounded so
            # a pathological recycle race can't recurse without limit.
            if _won_race_retries >= _WON_RACE_MAX_RETRIES:
                raise RuntimeError(
                    f"get_or_create({key!r}) exceeded {_WON_RACE_MAX_RETRIES} "
                    "won-race retries — session kept going stale between acquire "
                    "and re-validate"
                )
            return await self.get_or_create(
                key,
                agent=agent,
                channel_id=channel_id,
                approval_policy=approval_policy,
                model=model,
                cwd=cwd,
                extra_env=extra_env,
                _won_race_retries=_won_race_retries + 1,
                **extra_factory_kwargs,
            )

        return result

    async def reset(
        self,
        key: str,
        *,
        expect_session: _Session | None = None,
        skip_if_busy: bool = False,
    ) -> bool:
        """Kill and recreate a session (context overflow recovery).

        Returns True if a session was actually torn down, False if an optional
        guard below made it a no-op.

        Optional guards, evaluated atomically under the lock together with the
        pop (so no turn can start and no session swap can slip into a
        released-lock window), used by the RSS-recycle watchdog:

          * ``expect_session`` — only reset if this exact session object still
            occupies ``key``. Guards against recycling a session that was
            reset+recreated under a reused key between an off-lock RSS
            measurement and this call (the victim would otherwise be killed on
            the prior occupant's stale reading).
          * ``skip_if_busy`` — skip if the current session has a turn in flight
            (semaphore held), so a live stream is never cut mid-turn. This is
            enforced here, atomically with the pop, rather than in a caller's
            separate lock acquisition (which reopens the window).
        """
        key = self._fold_key(key)
        async with self._lock:
            current = self._sessions.get(key)
            if expect_session is not None and current is not expect_session:
                return False
            if skip_if_busy and current is not None and current.semaphore.locked():
                return False
            session = self._sessions.pop(key, None)
            # The new process is a fresh start — drop any stale failure
            # cooldown so it isn't inherited.
            self._compact_cooldown_until.pop(key, None)
            self._origin_links.pop(key, None)
        if session:
            # Capture PID and child tree before shutdown clears them
            client = getattr(session.provider, "_client", None)
            raw_pid = getattr(client, "_pid", None) if client else None
            # CC provider: PID from long-lived _proc or ephemeral _active_proc
            if raw_pid is None:
                _cc_proc = getattr(session.provider, "_proc", None)
                if _cc_proc is not None and _cc_proc.returncode is None:
                    raw_pid = _cc_proc.pid
            if raw_pid is None:
                _cc_proc = getattr(session.provider, "_active_proc", None)
                if _cc_proc is not None and _cc_proc.returncode is None:
                    raw_pid = _cc_proc.pid
            pid = raw_pid if isinstance(raw_pid, int) else None
            raw_children = getattr(client, "_child_pids", None) if client else None
            child_pids: dict = dict(raw_children) if isinstance(raw_children, dict) else {}
            # Lazy import to avoid circular dependency with acp.client.
            # Imported unconditionally so _kill_escaped_children is always
            # defined for the post-shutdown sweep below.
            from kiro_crew.acp.client import (
                _capture_child_records,
                _get_child_pids,
                _kill_escaped_children,
            )

            if pid:
                # Snapshot child tree before shutdown. PIDs may be recycled
                # between snapshot and kill, but _kill_escaped_children uses
                # start-time comparison to skip recycled PIDs safely.
                # macOS pgrep/ps spawns are offloaded to subprocess_executor
                # to keep the reset path responsive.
                _loop = asyncio.get_running_loop()
                fresh = await _loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
                new_pids = [p for p in fresh if p not in child_pids]
                if new_pids:
                    child_pids.update(
                        await _loop.run_in_executor(
                            subprocess_executor(), _capture_child_records, new_pids
                        )
                    )
            await session.provider.shutdown()
            # Verify process is actually dead; force-kill entire tree if not.
            # os.kill(pid, 0) would *terminate* the process on Windows, so probe
            # via pid_exists() and force-kill the tree through platform_compat.
            if pid:
                if platform_compat.pid_exists(pid):
                    # Still alive after shutdown — force kill process group
                    logger.warning("Reset %s: PID %d survived shutdown, force-killing", key, pid)
                    try:
                        # Async variants offload Windows taskkill to
                        # subprocess_executor so this reset path never blocks
                        # the event loop on taskkill.exe.
                        await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
                    except (ProcessLookupError, OSError):
                        try:
                            await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
                        except (ProcessLookupError, OSError):
                            pass
                # Sweep children in different PGIDs (MCP servers) even when
                # root is dead — children in separate process groups may
                # outlive the root.
                if child_pids:
                    try:
                        # macOS `ps` spawns inside _is_our_child; offload
                        # to subprocess_executor.
                        _sweep_loop = asyncio.get_running_loop()
                        await _sweep_loop.run_in_executor(
                            subprocess_executor(), _kill_escaped_children, child_pids
                        )
                    except Exception:
                        logger.exception("Reset %s: child sweep failed", key)
            # Kill the shared subagent runtime associated with this session
            # (if any). Subagents on it are already dead since their queues
            # are poisoned when the runtime dies.
            if key in self._subagent_runtimes:
                try:
                    await self.release_subagent_runtime(key)
                except Exception:
                    logger.debug("Reset %s: subagent runtime cleanup failed", key, exc_info=True)
            logger.debug("Reset session: %s (pid=%s)", key, pid)
        return session is not None

    def check_context_usage(self, key: str, provider: LLMProvider) -> float:
        """Check context usage and fire background compaction at the
        configured ``autocompact_pct`` threshold.

        Falls back to prompt-count compaction if metadata never reports %.
        Returns context usage percentage immediately — never blocks.
        """
        key = self._fold_key(key)
        pct = provider.context_usage_pct()

        # Track prompts for background session recycle fallback
        session = self._sessions.get(key)
        if session:
            session.prompt_count += 1

        # CC per_session handles its own compaction natively — skip KiroCrew's
        _is_cc_persistent = (
            ClaudeCodeProvider is not None
            and isinstance(provider, ClaudeCodeProvider)
            and provider.connection_mode == "per_session"
        )
        if _is_cc_persistent:
            if pct > 0:
                logger.info("Session %s context at %.0f%% (CC-managed)", key, pct)
        elif pct >= self._cfg.session.autocompact_pct:
            self._trigger_compaction(key, f"context at {pct:.0f}%", pct)
        elif pct >= _CONTEXT_WARN_PCT:
            logger.warning("Session %s context at %.0f%%", key, pct)
        elif pct > 0:
            logger.info("Session %s context at %.0f%%", key, pct)
        return pct

    def set_compact_callback(self, cb: _CompactCallback | None) -> None:
        """Register a callback fired after a compact attempt.

        Signature: ``async def cb(key, pct, *, success)``.  Used by the
        dashboard to post the auto-compact notice and reset the context
        indicator after compaction.
        """
        if self._on_compacted is not None and cb is not None:
            logger.warning("Compact callback already registered; replacing existing handler")
        self._on_compacted = cb

    def set_recycle_callback(self, cb: _RecycleCallback | None) -> None:
        """Register a callback fired when the watchdog recycles a session.

        Signature: ``async def cb(key, *, reason)``.  Used by the dashboard to
        notify the user that their session was reset (e.g. by the RSS-threshold
        watchdog), since unlike idle/orphan expiry this can happen while the
        user is still around. Idle and orphan sweeps do NOT fire this — the
        user has already walked away in those cases.
        """
        if self._on_recycled is not None and cb is not None:
            logger.warning("Recycle callback already registered; replacing existing handler")
        self._on_recycled = cb

    def _trigger_compaction(self, key: str, reason: str, pct: float) -> None:
        """Schedule a background compact task for *key*, gated by two checks.

        - ``_compacting`` set: dedup concurrent triggers; an in-flight
          compaction blocks new triggers until it completes.
        - ``_compact_cooldown_until``: after a failed compact, suppress
          re-triggers until the cooldown elapses.

        The actual work runs in a fire-and-forget task so the caller's
        response path is never blocked.
        """
        if key in self._compacting:
            logger.info("Session %s compaction already in progress", key)
            return
        cooldown_until = self._compact_cooldown_until.get(key, 0.0)
        if cooldown_until > time.monotonic():
            # Last compact failed. Skip until the cooldown elapses so a broken
            # /compact does not fire on every subsequent turn. Log at INFO —
            # the original failure was already logged at exception level.
            logger.info(
                "Session %s compaction skipped — cooldown active for %.0fs more",
                key,
                cooldown_until - time.monotonic(),
            )
            return
        logger.warning("Session %s compacting — %s", key, reason)
        self._compacting.add(key)
        t = asyncio.create_task(self._compact_session(key, pct))
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)

    async def _compact_session(self, key: str, pct: float) -> None:
        """Compact a session that hit the context threshold.

        Both backends compact **in place** first, so the kiro-cli process (or
        claude SDK session) survives and any queued or agentic work continues
        automatically — the fix for "session stops after auto-compaction".

        kiro-cli only: if the in-place ``/compact`` fails or times out, fall
        back to the legacy recycle — kill the session and let the next user
        message re-seed context via build_session_context(). The session_map
        entry is dropped so we don't false-resume from stale state. A recycle
        is never forced through a live turn: if the turn semaphore cannot be
        acquired within the budget, the attempt is skipped and the next
        turn-end ``check_context_usage`` re-triggers it.
        """
        try:
            session = self._sessions.get(key)
            if session and _is_claude_backend(session.provider):
                # session_map entry stays — claude SDK preserves the same
                # session ID across the compact_boundary, no delete needed.
                # The timeout wraps both semaphore acquisition and compact()
                # itself: if a long-running prompt holds the semaphore, we
                # still bail out instead of waiting forever.
                claude_session = session

                async def _run_compact() -> None:
                    async with claude_session.semaphore:
                        await claude_session.provider.compact()

                try:
                    await asyncio.wait_for(_run_compact(), timeout=_COMPACT_TIMEOUT_SECS)
                except (Exception, asyncio.TimeoutError) as exc:
                    if isinstance(exc, asyncio.TimeoutError):
                        logger.error(
                            "Compact timed out after %.0fs for %s",
                            _COMPACT_TIMEOUT_SECS,
                            key,
                        )
                    else:
                        logger.exception("Compact failed for %s", key)
                    self._compact_cooldown_until[key] = (
                        time.monotonic() + _COMPACT_FAILURE_COOLDOWN_SECS
                    )
                    await self._fire_compact_callback(key, pct, success=False)
                    return
                # Success: clear any prior cooldown so subsequent triggers work.
                self._compact_cooldown_until.pop(key, None)
                logger.info("Compacted session %s (context overflow)", key)
                await self._fire_compact_callback(key, pct, success=True)
                return

            # ── kiro-cli: in-place /compact first ──
            if session is not None:
                outcome = await self._compact_in_place(key, session, pct)
                if outcome == "ok":
                    return
                if outcome == "busy":
                    # A turn is still running. NEVER kill a live turn for
                    # compaction — the old force-recycle here SIGKILLed
                    # kiro-cli mid-turn, losing all in-flight work. The next
                    # turn-end check_context_usage re-triggers compaction
                    # when the semaphore is free. No cooldown / no failure
                    # callback: this is a deferral, not a failure.
                    logger.warning(
                        "Session %s compaction deferred — turn still active after %.0fs",
                        key,
                        _COMPACT_TIMEOUT_SECS,
                    )
                    return
                # outcome == "failed" → fall through to the recycle fallback.

            # ── kiro-cli recycle fallback ──
            # SIGKILLs the provider; drain the in-flight turn via the session
            # semaphore first so we don't kill mid-turn. Operates strictly on
            # the *session object captured above*: pop-by-identity means a
            # fresh session registered by a racing cold-start is never popped
            # or killed by mistake.
            if session is None:
                return
            sem = session.semaphore
            try:
                await asyncio.wait_for(sem.acquire(), timeout=_COMPACT_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                logger.warning(
                    "Session %s recycle skipped — turn still active after %.0fs",
                    key,
                    _COMPACT_TIMEOUT_SECS,
                )
                return
            self._recycling[key] = session
            try:
                async with self._lock:
                    popped = None
                    if self._sessions.get(key) is session:
                        popped = self._sessions.pop(key, None)
                if popped is None:
                    # A racing cold-start already replaced the entry — the map
                    # now points at a fresh, healthy session. Reap OUR old
                    # provider (its process would otherwise leak) but leave the
                    # replacement and its session_map entry untouched.
                    await session.provider.shutdown()
                    logger.info(
                        "Recycled session %s (context overflow; entry already replaced)", key
                    )
                else:
                    self._session_map.delete(key)
                    await popped.provider.shutdown()
                    logger.info("Recycled session %s (context overflow)", key)
                await self._fire_compact_callback(key, pct, success=True)
            finally:
                if self._recycling.get(key) is session:
                    self._recycling.pop(key, None)
                sem.release()
        except Exception:
            logger.exception("Session recycle failed for %s", key)
        finally:
            self._compacting.discard(key)

    async def _compact_in_place(self, key: str, session: "_Session", pct: float) -> str:
        """Attempt a native in-place ``/compact`` on a kiro-cli session.

        Returns:
        - ``"ok"``: compaction completed; session (and its process) survives.
          The success callback has been fired and the cooldown cleared.
        - ``"busy"``: the turn semaphore could not be acquired within
          ``_COMPACT_TIMEOUT_SECS`` — a turn is still running. Nothing was
          attempted; the caller must NOT recycle (no mid-turn kill).
        - ``"failed"``: the compact was attempted but failed, timed out, or
          the provider does not support it (base ``wait_for_compaction``
          returns ``{"type": "timeout"}``). The caller falls back to recycle.

        Holds the session semaphore for the duration so a queued turn waits
        behind the compaction (and then continues on the compacted session)
        instead of interleaving with it.
        """
        try:
            await asyncio.wait_for(session.semaphore.acquire(), timeout=_COMPACT_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            return "busy"
        try:

            async def _run() -> None:
                # Lazy import: kiro_crew.acp.__init__ eagerly pulls client/
                # runtime; a module-level import here would recreate the
                # providers<->session cycle this file avoids everywhere else.
                from kiro_crew.acp.types import EVENT_COMPACTION_STATUS

                # Mirror the proven Slack !compact flow: send /compact as a
                # PROMPT and watch the stream for the compaction status
                # inline. kiro-cli may emit _kiro.dev/compaction/status
                # either mid-turn (before end_turn) or asynchronously after
                # it — watching the stream first means a mid-turn status is
                # never eaten by a blind drain (which would strand
                # wait_for_compaction until timeout and wrongly recycle a
                # just-compacted healthy session).
                status: str | None = None
                async for event in session.provider.stream_command("/compact"):
                    if event.kind == EVENT_COMPACTION_STATUS and event.text in (
                        "completed",
                        "failed",
                    ):
                        status = event.text
                if status is None:
                    # No terminal status mid-turn (a "started" may have
                    # streamed) — the result arrives async after end_turn.
                    result = await session.provider.wait_for_compaction(
                        timeout=_COMPACT_RESULT_WAIT_SECS
                    )
                    status = result.get("type") if isinstance(result, dict) else None
                if status != "completed":
                    raise RuntimeError(f"compaction reported {status or 'no result'}")

            await asyncio.wait_for(_run(), timeout=_COMPACT_TIMEOUT_SECS)
        except (Exception, asyncio.TimeoutError):
            logger.warning(
                "Session %s in-place /compact failed — falling back to recycle",
                key,
                exc_info=True,
            )
            return "failed"
        finally:
            session.semaphore.release()
        self._compact_cooldown_until.pop(key, None)
        logger.info("Compacted session %s in place (context overflow)", key)
        await self._fire_compact_callback(key, pct, success=True)
        return "ok"

    async def _fire_compact_callback(self, key: str, pct: float, *, success: bool) -> None:
        """Invoke ``_on_compacted`` if registered, swallowing exceptions."""
        if self._on_compacted is None:
            return
        try:
            await self._on_compacted(key, pct, success=success)
        except Exception:
            logger.exception("Compact callback failed for %s", key)

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None:
        """Invoke ``_on_recycled`` if registered, swallowing exceptions."""
        if self._on_recycled is None:
            return
        try:
            await self._on_recycled(key, reason=reason)
        except Exception:
            logger.exception("Recycle callback failed for %s", key)

    async def remove(self, key: str) -> None:
        """Shut down a session but preserve session_map for future resume.

        Use when the session can be revived later (tab close, agent switch,
        idle kill).  The kiro-cli session files remain on disk, so
        ``session/load`` can restore the full conversation losslessly.
        """
        key = self._fold_key(key)
        async with self._lock:
            session = self._sessions.pop(key, None)
            self._compact_cooldown_until.pop(key, None)
            self._origin_links.pop(key, None)
        if session:
            await session.provider.shutdown()
            # Reap any companion subagent runtime keyed by this parent (the
            # get_subagent_runtime fallback path). shutdown() covers the common
            # kiro path (subagents on the parent's own runtime), but a companion
            # runtime lives only in _subagent_runtimes and would otherwise leak.
            await self.release_subagent_runtime(key)
            logger.info("Removed session (map preserved): %s", key)

    async def destroy(self, key: str) -> None:
        """Permanently destroy a session — no resume possible.

        Use for irreversible actions: permanent history deletion, bulk
        clear, or error recovery where the session state is corrupt.
        """
        key = self._fold_key(key)
        async with self._lock:
            session = self._sessions.pop(key, None)
            self._compact_cooldown_until.pop(key, None)
        try:
            if session:
                await session.provider.shutdown()
            # Reap any companion subagent runtime keyed by this parent (see remove()).
            await self.release_subagent_runtime(key)
        finally:
            self._session_map.delete(key)
            logger.info("Destroyed session (map deleted): %s", key)

    async def drain_active_turns(self, timeout: float | None = None) -> int:
        """Bring in-flight prompts to a safe boundary before teardown.

        Every gateway restart and Dev-Fleet 'Make Live' cutover funnels through
        ``close_all()`` (the systemd cutover via the SIGTERM shutdown path, the
        in-process update-restart via ``_restart_gateway``). Without this step
        ``close_all()`` shuts each session's provider down immediately, so a slot
        that is mid-prompt has its kiro-cli killed with the native turn still
        open — leaving the native-session lock (``~/.kiro/sessions/cli/<uuid>.json``)
        held. When the new gateway resumes that slot via ``session/load`` kiro-cli
        rejects with "active in another process" and the slot returns EMPTY
        completions until the stale lock times out. This was the confirmed root
        cause of the Make-Live empty-response incident (slot chat-1, native
        session 31f36326, after the 04:26 cutover).

        For each registered session with an active turn, issue a graceful ACP
        ``session/cancel`` and wait (bounded) for the turn-done ack, so the
        native turn closes cleanly and kiro-cli can release the lock on the
        subsequent SIGTERM. The whole operation is bounded by ``timeout``; on
        timeout we log a warning and return so the caller falls through to the
        (SIGTERM-first) kill path — the drain never hangs teardown and never
        raises. ``timeout <= 0`` disables the drain entirely.

        Returns the number of sessions that had an active turn (for
        observability and tests). Only the registered user sessions are drained;
        the warm pool holds pre-spawned, never-prompted processes.
        """
        if timeout is None:
            timeout = _DRAIN_ACTIVE_TURNS_TIMEOUT_SECS
        if timeout <= 0:
            return 0

        async with self._lock:
            providers = [s.provider for s in self._sessions.values()]
        # Filter on has_UNFINISHED_turn, not has_active_turn: a turn already
        # session/cancel'd but whose native ack has not arrived reports
        # has_active_turn False yet still holds the native lock open. Draining
        # THAT (waiting for its ack) is exactly what prevents the killed-with-
        # lock-held empty-response bug (Codex HIGH, cancel/ack race).
        unfinished = [p for p in providers if _provider_has_unfinished_turn(p)]
        if not unfinished:
            return 0

        logger.info(
            "Draining %d unfinished turn(s) to a safe boundary before teardown (<= %.1fs)",
            len(unfinished),
            timeout,
        )

        async def _drain_one(provider: LLMProvider) -> None:
            cancel_fn = getattr(provider, "cancel", None)
            if not callable(cancel_fn):
                return
            try:
                # Graceful cancel: reach a safe turn boundary + wait for the ack
                # so kiro-cli closes the native turn (and can release its
                # session lock) before we kill the process. cancel() is itself
                # internally bounded by wait_ack_timeout; the outer wait_for
                # below is the hard cap.
                outcome = await cancel_fn(wait_ack_timeout=timeout)
            except Exception:
                logger.debug("drain_active_turns: cancel failed", exc_info=True)
                return
            # cancel() returns "no_turn" when has_active_turn() is already False
            # — precisely the already-cancelled-but-not-yet-acked turn that
            # has_unfinished_turn still flags. The native turn is still open, so
            # wait directly for its done-ack rather than skip it (that skip is
            # what left the lock held → empty-response bug).
            if outcome == "no_turn" and _provider_has_unfinished_turn(provider):
                waiter = getattr(provider, "wait_turn_done", None)
                if callable(waiter):
                    try:
                        await waiter(timeout=timeout)
                    except asyncio.TimeoutError:
                        logger.debug(
                            "drain_active_turns: post-cancel wait_turn_done timed out"
                        )
                    except Exception:
                        logger.debug(
                            "drain_active_turns: wait_turn_done failed", exc_info=True
                        )

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_drain_one(p) for p in unfinished], return_exceptions=True
                ),
                # A hair above the per-session budget so an internally-bounded
                # cancel resolves as its own timeout rather than the gather being
                # cancelled out from under it.
                timeout=timeout + 1.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "drain_active_turns: %d turn(s) did not reach a safe boundary within "
                "%.1fs — proceeding to kill (kiro-cli SIGTERM grace still applies)",
                len(unfinished),
                timeout,
            )
        return len(unfinished)

    async def close_all(self, drain_timeout: float | None = None) -> None:
        """Shut down every session (called on shutdown).

        ``drain_timeout`` bounds the pre-shutdown co-operative drain
        (:meth:`drain_active_turns`); ``None`` uses the full default budget.
        A caller that wraps ``close_all()`` in its own hard outer deadline —
        Slack's restart wraps it in ``wait_for(..., 5s)`` — MUST pass a
        ``drain_timeout`` small enough to leave room for the kill path inside
        that deadline. systemd cutover and in-process update-restart pass no
        budget and keep the full default. ``drain_timeout <= 0`` disables the
        drain.
        """
        # Bring any in-flight prompts to a safe boundary FIRST so kiro-cli can
        # release its native-session lock before we kill the processes below.
        # Bounded + best-effort: never blocks teardown, never raises. Both
        # restart paths (systemd Make-Live cutover, in-process update-restart)
        # funnel through here, so this is the single chokepoint that fixes the
        # empty-response-after-restart incident.
        # Enter the closing state under the lock BEFORE the drain snapshot so no
        # new turn can begin (or new session register) during the multi-second
        # drain window — a prompt that started after the snapshot would be absent
        # from the drain set and later get killed mid-turn with its native lock
        # held (Codex HIGH: drain-window race). Paired with the get_or_create
        # closing gate.
        async with self._lock:
            self._closing = True

        try:
            await self.drain_active_turns(timeout=drain_timeout)
        except Exception:
            # We deliberately do NOT catch asyncio.CancelledError here. Slack's
            # restart wraps close_all in wait_for(..., 5s), which enforces its
            # deadline by cancelling us. Swallowing that cancel would DEFEAT the
            # 5s hard cap — wait_for would then block until close_all finished on
            # its own, so a slow later teardown phase could overrun the deadline
            # and prevent os._exit(1) from being reached, wedging the restart
            # (Codex HIGH). Letting CancelledError propagate keeps the cap
            # honest; a drain cut short that way skips the in-line kill path, but
            # the next-startup orphan reaper reclaims any still-held
            # process/lock. drain_timeout is sized (e.g. 2.0 on the Slack path)
            # so the drain finishes well inside the cap and this cancellation is
            # not hit on the normal path.
            logger.debug("close_all: drain_active_turns failed", exc_info=True)

        if self._cleanup_task:
            self._cleanup_task.cancel()

        # Cancel background spawn tasks (may be blocked in _INIT_TIMEOUT waits)
        # _pool_health_task is included via _background_tasks registration.
        for t in list(self._background_tasks):
            t.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Kill the shared _bg runtime and any per-session subagent runtimes.
        # These are held only in instance attributes (never registered sessions
        # or warm-pool providers), so the session-shutdown loop below does not
        # cover them; without this they survive a graceful shutdown until the
        # next-startup orphan reaper.
        if self._bg_runtime is not None:
            try:
                await self._bg_runtime.kill()
            except Exception:
                logger.debug("close_all: _bg_runtime kill failed", exc_info=True)
            self._bg_runtime = None
        for _key in list(self._subagent_runtimes):
            try:
                await self.release_subagent_runtime(_key)
            except Exception:
                logger.debug(
                    "close_all: subagent runtime cleanup failed for %s", _key, exc_info=True
                )

        # Drain warm pool — shut down pre-spawned processes
        pool_providers: list[LLMProvider] = []
        while not self._warm_pool.empty():
            try:
                provider, _ = self._warm_pool.get_nowait()
                pool_providers.append(provider)
            except asyncio.QueueEmpty:
                break

        async with self._lock:
            # Save session mappings before killing processes
            from kiro_crew.providers.acp import AcpProvider  # circular import: providers -> session

            for key, sess in self._sessions.items():
                _cwd_str = sess.provider.cwd
                if isinstance(sess.provider, AcpProvider):
                    sid = sess.provider.client._session_id
                    if (
                        sid
                        and key != BACKGROUND_KEY
                        and (
                            not any(key.startswith(p) for p in _STATELESS_PREFIXES)
                            or key in self._continuable_keys
                        )
                    ):
                        # Persist the provider label so detect_provider_switch
                        # on next startup doesn't see a missing entry, default
                        # to "acp", and falsely fire a switch for users still
                        # on claude_code.
                        _prov_label = "claude_code" if _is_claude_backend(sess.provider) else "acp"
                        self._session_map.set(key, sid, provider=_prov_label, cwd=_cwd_str)
                elif ClaudeCodeProvider is not None and isinstance(
                    sess.provider, ClaudeCodeProvider
                ):
                    sid = sess.provider.session_id
                    if (
                        sid
                        and key != BACKGROUND_KEY
                        and (
                            not any(key.startswith(p) for p in _STATELESS_PREFIXES)
                            or key in self._continuable_keys
                        )
                    ):
                        self._session_map.set(key, sid, provider="claude_code", cwd=_cwd_str)

            sessions = dict(self._sessions)
            self._sessions.clear()
            self._compact_cooldown_until.clear()

        # Bound concurrent shutdowns: each provider.shutdown() -> _kill_process()
        # enqueues 2-3 subprocess_executor tasks (child scan, record capture,
        # escaped-child sweep), several of which can block on a wedged kernel
        # resource. Without a cap, a mass shutdown of the warm pool + active
        # sessions would flood the bounded subprocess pool with uncancellable
        # tasks at once; the semaphore lets them drain in pool-sized waves.
        _close_sem = asyncio.Semaphore(_CLOSE_ALL_CONCURRENCY)

        async def _close_one(provider: LLMProvider) -> None:
            async with _close_sem:
                try:
                    await provider.shutdown()
                except Exception:
                    pass

        all_providers = [s.provider for s in sessions.values()] + pool_providers
        if not all_providers:
            return

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_close_one(p) for p in all_providers], return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout closing %d sessions — orphan cleanup at next startup", len(all_providers)
            )
        logger.info("All sessions closed (active=%d)", len(sessions))

    # ── Circuit breaker ──

    def record_success(self, key: str) -> None:
        """Reset consecutive failure counter on success."""
        session = self._sessions.get(self._fold_key(key))
        if session:
            session.consecutive_failures = 0

    async def record_failure(self, key: str) -> bool:
        """Increment failure counter. Returns True if circuit tripped (session reset)."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        session.consecutive_failures += 1
        if session.consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            logger.error(
                "Circuit breaker tripped for %s (%d consecutive failures) — resetting",
                key,
                session.consecutive_failures,
            )
            await self.reset(key)
            return True
        return False

    def begin_turn(self, key: str) -> None:
        """Atomic pre-dispatch gate — raise if teardown has begun.

        Closes the lease-dispatch race (Codex HIGH). A caller obtains a session
        and its per-session semaphore *lease* from :meth:`get_or_create`, does
        async prep, then drives ``provider.stream(...)``. The ``_closing`` gate in
        ``get_or_create`` only blocks callers that have NOT yet acquired a lease;
        a caller that took its lease BEFORE ``close_all`` set ``_closing`` can
        still reach dispatch AFTER — and an already-issued lease cannot be
        revoked. Such a turn would first open *after* ``drain_active_turns``'s
        snapshot, be absent from the drain set, and get killed mid-turn with its
        native-session lock held (the empty-response-after-restart bug, #200).

        The caller MUST therefore call ``begin_turn`` **synchronously, with no
        await in between**, immediately before the stream drive. This method only
        *reads* ``_closing`` — atomic in the single-threaded event loop, so no
        lock is needed — and the stream's synchronous prefix clears the native
        turn-done event (registering the turn) before its first ``await`` (see
        ``AcpClient.stream_events``: ``_turn_done.clear()`` precedes
        ``await ensure_ready()``). So the ``_closing`` read here and that turn
        registration form ONE yield-free span; in the event loop that span is
        strictly ordered w.r.t. ``close_all``'s ``_closing`` set — the turn is
        EITHER registered before the drain snapshot (and thus drained) OR the
        caller aborts here. It can never straddle, so no un-drained turn opens
        during the drain window.

        NOTE: making this ``async`` / lock-guarded would REOPEN the race — the
        ``await`` would yield the loop between the check and the turn
        registration, letting ``close_all`` snapshot in between. ``key`` is
        accepted for API symmetry and possible future per-key gating; the
        current shutdown signal is manager-global.

        Raises:
            SessionClosingError: a gateway restart/shutdown is in progress; the
                caller aborts the turn (its ``finally`` releases the lease).
        """
        if self._closing:
            raise SessionClosingError(
                "SessionManager is closing (gateway restart/shutdown in "
                "progress); refusing to start a turn"
            )

    # ── Per-session semaphore ──

    def mark_continuable(self, key: str) -> None:
        """Register *key* as a continuable subagent conversation.

        The key keeps its ``subagent:`` prefix (so audit/identity behavior is
        unchanged) but opts out of the stateless treatment: its sid persists
        to the session map, ``session/load`` is armed on the next
        ``get_or_create``, and ``release(cleanup=True)`` keeps its session
        files on disk.
        """
        self._continuable_keys.add(self._fold_key(key))

    def unmark_continuable(self, key: str) -> None:
        """Remove *key* from the continuable set (conversation released)."""
        self._continuable_keys.discard(self._fold_key(key))

    def is_continuable(self, key: str) -> bool:
        """True iff *key* is registered as a continuable conversation."""
        return self._fold_key(key) in self._continuable_keys

    def resumable_sid(self, key: str) -> str | None:
        """Return the persisted sid for *key*, or None.

        Used by SubagentManager to decide whether a conversation can be
        continued (``session/load``-able files still on disk — the session
        map self-prunes entries whose files are gone).
        """
        return self._session_map.get(self._fold_key(key))

    def seed_conversation(self, key: str, sid: str, *, provider: str = "", cwd: str = "") -> None:
        """Write a session-map entry for *key* on demand (spawn_continue).

        Retain-by-default: default subagent runs never write a map entry at
        spawn (a per-spawn ``SessionMap.set`` is a full-file rewrite — O(n)
        churn at wave scale). Their sid/provider/cwd live in the run's
        ``state.json``; this seeds the map only when a continue actually
        happens. ``SessionMap.get`` self-prunes entries whose session files
        are missing, so a post-seed ``resumable_sid`` doubles as the
        files-still-on-disk check.
        """
        if not sid:
            return
        self._session_map.set(self._fold_key(key), sid, provider=provider, cwd=cwd)

    def forget_conversation(self, key: str) -> str | None:
        """Drop *key*'s session-map entry and continuable mark.

        Returns the sid that was mapped (for caller-side file cleanup), or
        None if no mapping existed.
        """
        folded = self._fold_key(key)
        sid = self._session_map.get(folded)
        self._session_map.delete(folded)
        self._continuable_keys.discard(folded)
        return sid

    def conversation_provider(self, key: str) -> str:
        """Provider label persisted for *key* ("acp"/"claude_code" or "")."""
        return self._session_map.get_provider(self._fold_key(key))

    def release(self, key: str, *, cleanup: bool = False) -> None:
        """Release the per-session semaphore acquired by ``get_or_create``.

        If *cleanup* is True and the key is a subagent session, schedule
        best-effort deletion of the provider's on-disk session files.
        Continuable conversations are exempt: their session files ARE the
        resume material for a later ``spawn_continue``.
        """
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if session:
            if (
                cleanup
                and key.startswith(_SUBAGENT_PREFIX)
                and key not in self._continuable_keys
            ):
                try:
                    session_id = session.provider.session_id
                    if session_id:
                        asyncio.ensure_future(self._safe_cleanup(session.provider, session_id))
                except Exception:
                    logger.debug("Failed to get session_id for cleanup", exc_info=True)
            session.semaphore.release()

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None:
        """Best-effort session file cleanup."""
        try:
            await provider.cleanup_session(session_id)
            logger.debug("Cleaned up session files for %s", session_id)
        except Exception:
            logger.warning("Failed to clean up session files for %s", session_id, exc_info=True)

    # ── Message queue (Slack thread serialization) ──

    def is_busy(self, key: str) -> bool:
        """True iff a turn is in flight for *key* (its semaphore is held)."""
        session = self._sessions.get(key)
        return bool(session and session.semaphore.locked())

    def enqueue(
        self, key: str, msg_ts: str, text: str, *, force: bool = False, **kwargs: object
    ) -> bool:
        """Append a message to the session queue. Returns True if queued (session busy).

        If *force* is True, queue even when the semaphore isn't locked yet
        (covers the startup race where a task exists but hasn't acquired the lock).
        """
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if force or session.semaphore.locked():
            session.queue.append((msg_ts, text, kwargs))
            return True
        return False

    def dequeue(self, key: str) -> tuple[str, str, dict] | None:
        """Pop the next queued message, skipping cancelled ones."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return None
        while session.queue:
            msg_ts, text, kwargs = session.queue.popleft()
            if msg_ts not in session.cancelled:
                return msg_ts, text, kwargs
            session.cancelled.discard(msg_ts)
        return None

    def cancel_queued(self, key: str, msg_ts: str) -> bool:
        """Remove a queued message or mark an in-flight message as cancelled.

        Returns True if the msg_ts was found in the queue and removed.
        Returns False if not queued (may be in-flight — added to cancelled set).
        """
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        for i, (ts, _, _) in enumerate(session.queue):
            if ts == msg_ts:
                del session.queue[i]
                return True
        # Not in queue — only mark cancelled if something is actually in-flight
        if session.semaphore.locked():
            session.cancelled.add(msg_ts)
        return False

    def is_cancelled(self, key: str, msg_ts: str) -> bool:
        """Check if a message was cancelled (deleted while processing)."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if msg_ts in session.cancelled:
            session.cancelled.discard(msg_ts)
            return True
        return False

    def clear_queue(self, key: str) -> None:
        """Clear all queued messages and cancelled set for a session."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if session:
            session.queue.clear()
            session.cancelled.clear()

    async def is_provider_alive(self, key: str) -> bool | None:
        """Return True/False for provider liveness, or None if no session exists."""
        key = self._fold_key(key)
        async with self._lock:
            sess = self._sessions.get(key)
        if sess is None:
            return None
        # Use process-level check, not is_alive() which has a 600s
        # stale-activity threshold that falsely kills idle sessions.
        if hasattr(sess.provider, "is_process_alive"):
            return sess.provider.is_process_alive()
        return sess.provider.is_alive()

    def get_approval_policy(self, key: str) -> str:
        """Return the approval policy for a session, or empty string."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        return session.approval_policy if session else ""

    def get_agent(self, key: str) -> str:
        """Return the agent name for a session, or empty string."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        return session.agent if session else ""

    def set_approval_policy(self, key: str, policy: str) -> None:
        """Set the approval policy for an existing session."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if session:
            old = session.approval_policy
            session.approval_policy = policy
            if old != policy:
                sel().log_tool_invocation(
                    session_key=key,
                    source="session",
                    tool_name="set_approval_policy",
                    outcome=policy or "default",
                    metadata={"old_policy": old, "new_policy": policy},
                )

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

    def get_session_for_thread(self, thread_ts: str) -> str | None:
        """Return the session key linked to a Slack thread, or None."""
        return self._session_map.get_session_for_thread(thread_ts)

    # ── Channel-neutral outbound mirror (generalizes Slack linking) ──

    def set_mirror_link(
        self,
        key: str,
        link: ChannelLink | None,
        *,
        accepts_inbound: bool = False,
    ) -> None:
        """Bind (or clear) a session's channel-neutral mirror target.

        ``accepts_inbound`` upgrades a non-Slack outbound mirror into a
        persisted session-resume binding. Slack owns its dedicated reverse
        index; other channels use :meth:`find_mirror_sessions`.
        """
        self._session_map.set_mirror_link(
            key,
            link,
            accepts_inbound=accepts_inbound,
        )

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        """Return a session's outbound mirror target as a channel-neutral link,
        or None. Legacy Slack sessions surface as a Slack ``ChannelLink``."""
        return self._session_map.get_mirror_link(key)

    def mirror_accepts_inbound(self, key: str) -> bool:
        """True iff this session's mirror is a session-resume (two-way) binding."""
        return self._session_map.mirror_accepts_inbound(key)

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

    def clear_mirror_link(self, key: str) -> bool:
        """Remove a session's outbound mirror binding. Returns True iff present."""
        return self._session_map.clear_mirror_link(key)

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

    def delete_session_map_entry(self, key: str) -> None:
        self._session_map.delete(key)

    async def set_thread(self, key: str, thread_ts: str) -> None:
        """Set thread for a session. Prefer set_slack_link for new code."""
        _, channel_id = self.get_slack_link(key)
        self.set_slack_link(key, thread_ts, channel_id)

    def get_thread(self, key: str) -> str | None:
        """Return the Slack thread_ts for a session key, or None."""
        thread_ts, _ = self.get_slack_link(key)
        return thread_ts

    # ── Cancel ──

    async def cancel_current(self, key: str, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """Cancel the in-flight operation for *key* without destroying the session."""
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return "no_turn"
        outcome = await session.provider.cancel(wait_ack_timeout=wait_ack_timeout)
        logger.info("Cancelled in-flight operation for %s: %s", key, outcome)
        return outcome

    async def stop_turn(
        self,
        key: str,
        *,
        force: bool = False,
        preserve_queue: bool = False,
        on_soft: Callable[[], Awaitable[None]] | None = None,
        on_hard: Callable[[], Awaitable[None]] | None = None,
    ) -> StopOutcome:
        """Cooperative stop with kill fallback + eager respawn.

        Sequence:
          1. clear_queue(key) — skipped when preserve_queue=True (interrupt flow)
          2. if force: go straight to hard kill
          3. else: send session/cancel, wait up to budget
             - acked → call on_soft hook → return "soft"
             - timeout/error → fall through to hard kill
             - no_turn → return "idle"
          4. hard kill: reset(key) → fire-and-forget respawn → on_hard → "hard"
        """
        key = self._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return "idle"

        if not preserve_queue:
            self.clear_queue(key)
        budget: float = self._cfg.agent.soft_stop_budget_secs
        t0 = time.monotonic()

        if not force:
            outcome = await session.provider.cancel(wait_ack_timeout=budget)
            logger.debug("stop_turn: provider.cancel outcome=%r for %s", outcome, key)
            if outcome == "acked":
                elapsed = time.monotonic() - t0
                logger.info(
                    "stop_turn outcome=soft-acked session=%s elapsed=%.2fs",
                    key,
                    elapsed,
                )
                # kiro-cli discards cancelled turns from its conversation log,
                # so the next prompt must re-inject the cancelled turn context.
                session.prev_turn_cancelled = True
                if on_soft:
                    try:
                        await on_soft()
                    except Exception:
                        logger.warning("on_soft hook failed for %s", key, exc_info=True)
                return "soft"
            if outcome == "no_turn":
                logger.info("stop_turn outcome=idle session=%s (no active turn)", key)
                return "idle"
            # timeout or error → escalate to hard kill
            logger.info(
                "stop_turn outcome=escalated-to-hard session=%s " "cancel_result=%r elapsed=%.2fs",
                key,
                outcome,
                time.monotonic() - t0,
            )

        # --- Hard kill path ---
        # Scope A gateway hook: send abort frame to gatewayd for this
        # session's runtime PIDs so in-flight tool work is cancelled in the
        # pooled backend processes.
        await self._send_abort_for_session(key, session)

        await self.reset(key)
        elapsed = time.monotonic() - t0
        logger.info(
            "stop_turn outcome=hard-done session=%s elapsed=%.2fs",
            key,
            elapsed,
        )
        # Keep a strong reference — the event loop holds only a weak ref,
        # and without this the task could be GC'd mid-respawn.
        t = asyncio.create_task(self._eager_respawn(key))
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)
        if on_hard:
            try:
                await on_hard()
            except Exception:
                logger.warning("on_hard hook failed for %s", key, exc_info=True)
        return "hard"

    async def _send_abort_for_session(self, key: str, session: Any) -> None:
        """Send an abort frame to gatewayd for the session's runtime PID(s).

        Best-effort: failures are logged but never block the hard-kill path.
        """
        try:
            # Prefer the stable runtime_info() API on the provider base class.
            pid, socket_path = session.provider.runtime_info()

            # Fallback for providers that haven't overridden runtime_info().
            if pid is None:
                client = getattr(session.provider, "_client", None)
                pid = getattr(client, "_pid", None) if client else None
            if socket_path is None:
                client = getattr(session.provider, "_client", None)
                socket_path = getattr(client, "_mcp_gateway_socket", None) if client else None

            if isinstance(pid, int) and pid > 1 and socket_path:
                # SEL audit at the point of decision: schedule_abort is
                # fire-and-forget and the downstream _audit_abort_applied in
                # gatewayd only fires on success — record the initiation here
                # so there is an audit trail even if gatewayd never acks.
                try:
                    sel().log_api_access(
                        caller="session",
                        operation="mcp-gateway.abort-initiated",
                        outcome="initiated",
                        source="session",
                        resources=f"pid={pid} session={key}",
                        error="reason=hard-stop",
                    )
                except Exception:  # pragma: no cover — audit must never block the kill path
                    logger.debug("SEL audit for abort initiation failed", exc_info=True)
                schedule_abort(socket_path, [pid], reason=f"hard-stop session={key}")
            else:
                # Visible-by-default: if provider internals get renamed, the
                # abort push silently stops firing and the stop/kill bug this
                # exists to fix would quietly return. Warn so regressions show.
                logger.warning(
                    "abort-push skipped for %s: no runtime pid/socket resolved "
                    "(pid=%r socket=%r) — in-flight tool calls will not be cancelled",
                    key,
                    pid,
                    socket_path,
                )
        except Exception:
            logger.debug("_send_abort_for_session failed for %s", key, exc_info=True)

    async def _eager_respawn(self, key: str) -> None:
        """Fire-and-forget respawn after hard kill.

        ``get_or_create`` acquires the per-session semaphore on every return
        path; release it here so the next real user message can run.
        """
        try:
            await self.get_or_create(key)
            self.release(key)
        except Exception:
            logger.debug("Eager respawn failed for %s", key, exc_info=True)

    @property
    def count(self) -> int:
        return len(self._sessions)

    async def drain_all_providers(self) -> list:
        """Pop all sessions and return their providers. Thread-safe."""
        providers = []
        async with self._lock:
            keys = list(self._sessions.keys())
            for key in keys:
                sess = self._sessions.pop(key, None)
                if sess:
                    providers.append(sess.provider)
        return providers

    async def drain_warm_pool(self) -> list:
        """Drain all pre-spawned providers from the warm pool.

        Returns providers for the caller to shut down. Must be called
        when MCP config changes so stale pool processes (which loaded
        the old config at spawn time) are discarded.
        """
        drained = []
        while not self._warm_pool.empty():
            try:
                provider, _ = self._warm_pool.get_nowait()
                drained.append(provider)
            except asyncio.QueueEmpty:
                break
        if drained:
            logger.info("Drained %d provider(s) from warm pool", len(drained))
        return drained

    # ── Idle cleanup ──

    # ── Watchdog hooks ──
    # Each hook is the execution half of a CleanupHook (see watchdog.py). Each
    # one reproduces the exact try/except of the inline cleanup-loop block it
    # was lifted from, so SessionWatchdog.tick() can stay a dumb dispatcher and
    # the move is behaviour-preserving (no severity promotion of swallowed
    # errors). The orphan-PID sweep is deliberately NOT a hook in CR 1.

    async def _expire_idle_hook(self) -> None:
        """Idle/orphan session expiry. Gate + timeout are published onto self by
        _cleanup_loop, which owns the <60 clamp. Preserves the original
        ``logger.exception`` on failure."""
        if not self._idle_sweep_enabled:
            return
        try:
            await self._expire_idle(self._idle_timeout)
        except Exception:
            logger.exception("Cleanup loop: _expire_idle crashed; continuing")

    async def _orphan_mcp_hook(self) -> None:
        """Sweep MCP servers orphaned by crashed/expired sessions. Preserves the
        original silent-swallow behaviour.

        Offloaded to the bounded maintenance pool (not the default executor) so
        the per-PID os.kill loop + file lock can't block the event loop or
        starve its DNS resolution."""
        try:
            mcp_killed = await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), _cleanup_orphaned_mcp_servers
            )
            if mcp_killed:
                logger.info("Periodic sweep: cleaned %d orphaned MCP servers", mcp_killed)
        except Exception:
            pass

    async def _rss_threshold_check(self) -> None:
        """Recycle non-busy sessions whose process tree exceeds the configured
        RSS ceiling. New in CR 1; disabled by default (``watchdog_rss_max_mb=0``).

        Mirrors _expire_idle's kill structure AND its protected-key set: collect
        candidates under the lock — skipping persistent and channel-prefixed
        sessions exactly as the idle sweep does — then reset() each victim AFTER
        releasing the lock (reset() re-acquires it, so holding it across the call
        would deadlock). A session whose turn is in flight (semaphore held) is
        skipped to avoid cutting a live stream.

        RSS measurement walks the process tree with synchronous /proc reads, so
        it is done OUTSIDE the lock on the bounded maintenance executor —
        mirroring _orphan_mcp_hook — to avoid blocking the event loop. Because the
        lock is released across that measurement, the victim's session OBJECT is
        captured at collection time and handed to reset(), which re-verifies
        identity + not-busy atomically under the lock before killing (see
        reset()); a session that was swapped or became busy in the measurement
        window is left untouched and generates no recycle notice.
        """
        if not self._rss_max_mb:
            return
        candidates: list[tuple[str, int, _Session]] = []
        async with self._lock:
            for key, sess in self._sessions.items():
                if key in _PERSISTENT_KEYS:
                    continue
                if key.startswith(_CHANNEL_PREFIX):
                    # Channel sessions are protected from idle expiry; keep RSS
                    # recycle aligned so a long-lived channel context isn't
                    # silently cut (and _on_recycled only notifies dashboard:
                    # keys, so a recycled channel session would have no notice).
                    continue
                if sess.semaphore.locked():  # turn in flight — don't cut it
                    continue
                pid = self.get_pid(key)
                if pid is not None:
                    candidates.append((key, pid, sess))
        victims: list[tuple[str, int, _Session]] = []
        if candidates:
            # Build the /proc parent->child map ONCE per tick, off-loop. It is
            # identical for every candidate this sweep, so measuring each tree
            # via get_session_rss_mb (which builds its own map) would rescan all
            # of /proc K times; scan once and share the read-only map instead.
            # Offloaded to the bounded maintenance pool (not the default
            # executor), matching the sibling hooks, so an unrelated default-
            # pool backlog can't starve this periodic /proc walk.
            loop = asyncio.get_running_loop()
            child_map = await loop.run_in_executor(maintenance_executor(), _build_child_map)
            for key, pid, sess in candidates:
                rss = await loop.run_in_executor(
                    maintenance_executor(), _rss_mb_from_tree, pid, child_map
                )
                if rss > self._rss_max_mb:
                    victims.append((key, rss, sess))
        for key, rss, sess in victims:
            # Per-victim guard so one failed reset/notify doesn't skip the rest
            # of the victims this tick (the watchdog backstop is debug-only).
            try:
                # reset() re-verifies UNDER ITS OWN LOCK, atomically with the
                # pop, that (a) this exact session object still occupies key —
                # guarding against a reset+recreate under a reused key in the
                # released-lock measurement window — and (b) it is not mid-turn.
                # It returns False (a no-op) if either guard fails, so we neither
                # kill the wrong/busy session nor emit a misleading recycle
                # notice for a session we did not actually recycle.
                recycled = await self.reset(key, expect_session=sess, skip_if_busy=True)
                if not recycled:
                    continue
                logger.warning(
                    "RSS recycle: session %s tree rss=%dMB exceeds %dMB", key, rss, self._rss_max_mb
                )
                Stats().inc_session_cleaned()
                # Unlike idle/orphan expiry, an RSS recycle can hit a session
                # whose user is still around, so notify them it was reset.
                await self._fire_recycle_callback(key, reason=f"memory limit ({rss}MB)")
            except Exception:
                logger.exception("RSS recycle failed for session %s", key)

    async def _cleanup_loop(self) -> None:
        timeout = self._cfg.session.timeout_secs
        # Defensive clamp: the dashboard validator now allows 0 (disable
        # sentinel) but still accepts 1–59 syntactically. Any positive
        # value below 60 would cause _expire_idle() to aggressively reap
        # active sessions, which is never the intent. Clamp such values
        # up to the historical minimum of 60.
        if 0 < timeout < 60:
            logger.warning(
                "session.timeout_secs=%d is below minimum 60; clamping to 60",
                timeout,
            )
            timeout = 60
        idle_sweep_enabled = timeout > 0
        if not idle_sweep_enabled:
            logger.info(
                "Idle session sweep disabled (session.timeout_secs=%d); "
                "MCP/PID sweeps still run at default cadence",
                timeout,
            )
        # Publish the clamped idle config for _expire_idle_hook (the watchdog
        # hook re-checks idle_sweep_enabled so the gate is preserved verbatim).
        self._idle_sweep_enabled = idle_sweep_enabled
        self._idle_timeout = timeout
        # When idle sweep is disabled we still run the maintenance sweeps
        # (orphaned MCP servers, leaked kiro-cli PIDs) on a fixed cadence so
        # operators who set timeout_secs=0 don't also lose process hygiene.
        interval = max(timeout // 6, 60) if idle_sweep_enabled else 300
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                return  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # normal wake-up

            # idle expiry + orphaned-MCP sweep, plus the new RSS-threshold
            # recycle, are dispatched by the watchdog.
            # Each hook carries the exact error handling of the block it was
            # lifted from (the orphan-MCP hook keeps the maintenance-
            # executor offload). The orphan-PID sweep below is intentionally left
            # inline (CR 2 extracts it into a hook).
            await self._watchdog.tick()

            # Sweep session root kiro-cli processes left behind by crashed
            # gateway instances. Offloaded to a thread to keep
            # blocking I/O (os.kill, file lock, /proc reads) off the event loop.
            try:
                roots_killed = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), cleanup_orphaned_session_roots
                )
                if roots_killed:
                    logger.info(
                        "Periodic sweep: cleaned %d orphaned session root processes",
                        roots_killed,
                    )
            except Exception:
                pass

            # Sweep orphaned sandbox launcher scripts and seatbelt profiles
            # from ~/.kiro/crew/run/.  PID-tagged filenames; the blocking
            # os.kill/os.remove loop is kept off the event loop.
            try:
                sandbox_removed = await asyncio.get_running_loop().run_in_executor(
                    maintenance_executor(), cleanup_stale_sandbox_profiles
                )
                if sandbox_removed:
                    logger.info(
                        "Periodic sweep: removed %d stale sandbox launchers",
                        sandbox_removed,
                    )
            except Exception as exc:
                logger.debug("sandbox launcher sweep failed: %s", type(exc).__name__)

            # Sweep kiro-cli processes tracked in kiro_session_pids.txt
            # but no longer in self._sessions or self._warm_pool (leaked by
            # failed reset/shutdown).  Warm pool PIDs are included in the
            # active set to prevent healthy pooled processes from being
            # killed as orphans.
            # Offloaded to a thread to avoid blocking the event loop with
            # os.kill, subprocess calls, and file I/O.
            try:
                active_pids, ok = _collect_active_pids(self._sessions)
                active_pids.update(self._pool_pids())
                active_pids.update(self._in_flight_pids())
                active_pids.update(self._companion_runtime_pids())
                if ok:
                    my_gw_pid = os.getpid()
                    # Phase 1 (thread): identify dead entries and orphan candidates.
                    # No killing happens here — keeps blocking I/O off the event loop.
                    killed_or_dead, candidates = await asyncio.to_thread(
                        _periodic_pid_sweep, my_gw_pid, active_pids
                    )
                    # Phase 2a (event loop): re-check candidates against
                    # live sessions and warm pool.  Deny-by-default: if any
                    # PID extraction fails, skip the kill phase (still prune
                    # dead entries).
                    confirmed: list[int] = []
                    if candidates:
                        current_pids, phase2_safe = _collect_active_pids(self._sessions)
                        current_pids.update(self._pool_pids())
                        current_pids.update(self._in_flight_pids())
                        current_pids.update(self._companion_runtime_pids())
                        if phase2_safe:
                            confirmed = [pid for pid in candidates if pid not in current_pids]
                    # Phase 2b (thread): kill confirmed orphans + writeback.
                    # Keeps blocking I/O (subprocess, fcntl.flock) off the
                    # event loop.
                    if confirmed or killed_or_dead:
                        orphan_killed = await asyncio.to_thread(
                            _kill_confirmed_and_writeback, my_gw_pid, confirmed, killed_or_dead
                        )
                        if orphan_killed:
                            logger.warning(
                                "Periodic sweep: killed %d orphaned kiro-cli processes",
                                orphan_killed,
                            )
            except Exception:
                logger.debug("Orphan PID sweep failed", exc_info=True)

            # Untracked orphan MCP sweep (defense-in-depth)
            try:
                sweep_pids, sweep_ok = _collect_active_pids(self._sessions)
                sweep_pids.update(self._pool_pids())
                sweep_pids.update(self._in_flight_pids())
                sweep_pids.update(self._companion_runtime_pids())
                if sweep_ok:
                    # Identify candidates in thread (blocking I/O)
                    candidates = await asyncio.get_running_loop().run_in_executor(
                        maintenance_executor(), find_orphan_mcp_candidates, sweep_pids
                    )
                    # Re-verify against fresh active PIDs before killing
                    if candidates:
                        fresh_pids, fresh_ok = _collect_active_pids(self._sessions)
                        fresh_pids.update(self._pool_pids())
                        fresh_pids.update(self._in_flight_pids())
                        fresh_pids.update(self._companion_runtime_pids())
                        if fresh_ok:
                            confirmed = [p for p in candidates if p not in fresh_pids]
                            if confirmed:
                                await asyncio.get_running_loop().run_in_executor(
                                    maintenance_executor(), kill_orphan_mcps, confirmed
                                )
                        else:
                            # Distinguish "reaper skipped" from "no orphans":
                            # fresh re-verification was unreliable, so we
                            # fail closed and kill nothing this cycle.
                            logger.warning(
                                "Orphan MCP sweep skipped kill phase: fresh "
                                "active-PID re-verification unreliable "
                                "(fresh_ok=False)"
                            )
                else:
                    # Fail closed: active-PID enumeration was unreliable, so
                    # the whole sweep no-ops. Log so on-call can tell this
                    # apart from a benign "ran, found no orphans" cycle.
                    logger.warning(
                        "Orphan MCP sweep skipped: active-PID enumeration "
                        "unreliable (sweep_ok=False)"
                    )
            except Exception:
                logger.warning("Orphan MCP sweep failed", exc_info=True)

    def set_active_dashboard_slots(self, slot_keys: set[str]) -> None:
        """Update the set of active dashboard slot keys.

        Called by the dashboard layer on slot create/delete/resume/restore
        so that ``_expire_idle`` can immediately reap orphaned sessions
        whose UI tab no longer exists.
        """
        self._active_dashboard_slots = set(slot_keys)

    async def _expire_idle(self, timeout_secs: int) -> None:
        now = time.monotonic()
        expired: list[tuple[str, bool]] = []  # (key, is_orphan)
        total_checked = 0
        async with self._lock:
            for key, sess in self._sessions.items():
                if key in _PERSISTENT_KEYS:
                    continue
                if key.startswith(_CHANNEL_PREFIX):
                    continue
                total_checked += 1
                idle = now - sess.last_used > timeout_secs
                orphaned = (
                    key.startswith("dashboard:")
                    and self._active_dashboard_slots is not None
                    and key not in self._active_dashboard_slots
                )
                if idle or orphaned:
                    expired.append((key, orphaned))
        if expired:
            logger.warning("Idle sweep: %d checked, %d expired", total_checked, len(expired))
        elif total_checked:
            logger.debug("Idle sweep: %d checked, 0 expired", total_checked)
        for key, is_orphan in expired:
            # NOTE: Small TOCTOU window — slot could be re-activated between
            # orphan check (under lock) and reset() here. Accepted as benign:
            # worst case is session re-created on next user interaction.
            if is_orphan:
                logger.warning("Expiring orphaned dashboard session (slot gone): %s", key)
            else:
                logger.warning("Expiring idle session: %s", key)
            Stats().inc_session_cleaned()
            # Notify consolidator before reset so it can extract skills.
            if self.on_session_expire:
                try:
                    sel().log_api_access(
                        caller="session_manager",
                        operation="consolidate_session_expire",
                        outcome="allowed",
                        source="idle_sweep",
                        resources=key,
                    )
                    self.on_session_expire(key)
                except Exception:
                    logger.debug("on_session_expire (or SEL) failed for %s", key, exc_info=True)
            # Use reset() instead of remove() to preserve session_map entry.
            # The kiro-cli session file persists on disk — next get_or_create
            # can try session/load to restore full conversation history.
            await self.reset(key)
