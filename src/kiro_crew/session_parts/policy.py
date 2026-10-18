"""Fixed policy ``SessionManager`` hands its collaborators: limits, key shapes, thresholds.

The pool ceiling and shutdown bounds, the session-key vocabulary (stateless and
persistent keys, the background and heartbeat keys), the compaction wait budget and
its damping thresholds, the background recycle thresholds, the automatic-mirroring
opt-out bucket, and the model a new session falls back to.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Protocol

from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_SESSION_EVICTION,
    selectable_backends,
)
from kiro_crew.config.loader import POOL_SIZE_MAX
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.messaging.link import canonical_key
from kiro_crew.session_compaction import COMPACT_OUTCOME_COMPACTED

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig

logger = logging.getLogger("kiro_crew.session")


def _resolve_allocation_crew_identity(
    cfg: KiroCrewConfig,
    agent: str | None,
    crew_agent: str | None,
) -> str:
    from kiro_crew.config.loader import resolve_crew_identity

    return resolve_crew_identity(cfg, agent, crew_agent)


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
_THREAD_PREFIX = "thread:"

#: Every value the ``kirocrew.session.pool.decision`` counter can report. A
#: warm-pool claim either happens or is refused for exactly one reason; keeping
#: the set closed keeps the metric's cardinality bounded. Every reason the
#: allocator assigns must be listed: an unlisted one is counted as ``other``,
#: so the hit rate it costs cannot be told apart from any other reason.
POOL_DECISIONS: frozenset[str] = frozenset(
    {
        "hit",
        "miss_empty",
        "bypass_member_capabilities",
        "bypass_resume",
        "bypass_stateless",
        "bypass_member",
        "bypass_app_launch",
        "bypass_restricted_context",
        "bypass_member_context",
        "bypass_cwd",
        "bypass_env",
        "bypass_shared_scratch",
        "bypass_backend_override",
        "bypass_effort",
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
    # A reply thread on a crewmate chat message re-seeds its whole envelope on
    # every cold start (``chat_threads.build_thread_message``), so a resumed
    # kiro-cli transcript would only duplicate it.
    _THREAD_PREFIX,
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

# Heartbeat and history extraction have separate keys so their longer turns
# cannot hold the semaphore used by lightweight background jobs.
BACKGROUND_KEY = "_bg"
CONSOLIDATE_KEY = "_consolidate"
# Singleton stateless keys, matched exactly: the extraction session mints no
# suffixed keys, so a prefix would only claim names it never uses.
_STATELESS_KEYS = frozenset({CONSOLIDATE_KEY})

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
#
# Intersected with ``ACP_BACKENDS_SESSION_EVICTION`` as well, and that term is the
# one carrying weight. Background handles are the high-churn ones — title
# generation, suggestions, folders and nav each take their own ephemeral
# sessionId, many per conversation, at a rate a user never controls. A harness
# whose teardown verb does not dispose a session accumulates every one of them in
# the adapter's own map, so a shared process serving this path grows without
# bound. Foreground sessions accumulate the same way but at the rate a person
# opens chats, and each one is disposed by the teardown of the conversation that
# owns it, which is why the eviction term is required HERE and not there.
#
# The age/RSS recycle is NOT what bounds a foreground runtime: ``_is_stale`` is
# consulted from one place, the reuse path in
# ``session_background.get_bg_session``, so its 6 h / 500 MB ceilings govern the
# ``_bg`` runtime this set gates and nothing else. A foreground runtime and a
# per-parent companion runtime are bounded by their own teardown and by
# ``_expire_idle``, and a harness added here on the belief that a ceiling will
# catch what its teardown verb does not would have no such backstop.
#
# The two terms are separate memberships, earned separately. codex holds both:
# it runs on the shared runtime, and its teardown is a ``session/close`` request,
# which the adapter answers by dropping the session from its map -- measured,
# and re-measured by the gated live test on every install that has the adapter.
# ``session/cancel`` would NOT have earned the second membership: it leaves the
# session addressable with its context resident. A harness reaches this path by
# demonstrating BOTH properties in ``agent_sdk/backends_sessions.py``, never by an edit here.
def _bg_runtime_backends() -> frozenset[str]:
    return ACP_BACKENDS_ACP_RUNTIME & ACP_BACKENDS_SESSION_EVICTION & selectable_backends()


# Heartbeat session key — used by HeartbeatService.  Spawned with the full
# ``kirocrew`` agent so polled tasks can call read-only MCP tools (CR/ticket
# status, etc.).  Tool approval at runtime is gated by the
# heartbeat allowlist (``_heartbeat_approval``, deciding through
# ``slack/gateway_runtime/tool_policy.py``).
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


def _compact_result_wait_secs(elapsed: float, budget: float = COMPACT_WAIT_TIMEOUT_SECS) -> float:
    """Inner deadline for the async compaction-status wait.

    The FULL remainder of the shared compaction ``budget`` after ``elapsed``
    seconds — never less, so a compaction completing in the final seconds of
    the budget is not abandoned early. The outer ``asyncio.wait_for`` carries
    ``_COMPACT_RESULT_WAIT_MARGIN_SECS`` of headroom on top, keeping this
    wait's graceful "no result" diagnostic reachable. Clamped to a floor so
    the wait can never be zero or negative.

    ``budget`` defaults to ``COMPACT_WAIT_TIMEOUT_SECS``; the automatic path
    passes the effective budget (``_resolve_compact_wait_secs``), so a config
    key raising the budget raises this inner wait with it rather than leaving
    it clamped at the built-in default minus elapsed.
    """
    return max(
        _COMPACT_RESULT_WAIT_FLOOR_SECS,
        budget - elapsed,
    )


def _resolve_compact_wait_secs(configured: float) -> float:
    """The effective compaction wait budget.

    ``configured`` is ``cfg.session.compact_wait_secs``: a positive value is
    the operator's chosen budget, and 0 (the default) or any non-positive
    value falls back to the built-in ``COMPACT_WAIT_TIMEOUT_SECS``. Resolved
    per compaction so a live config change takes effect on the next one.
    """
    return configured if configured > 0 else COMPACT_WAIT_TIMEOUT_SECS


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
# itself may not fit. Such a session is reset — with its native resume
# sid cleared, so the overflowed conversation is not reloaded — instead of
# limping through compact/cooldown cycles. Every compaction caller gets this,
# not just the task runner's post-compaction verification.
# The escalation rides the verdict settle (not a raw re-read after compact())
# because only a settled reading has passed the measurability rules — a raw
# re-read can be unknown (kiro zeroes + flags stats) or stale (a backend that
# never reset them), and resetting on either would destroy a healthy session.
_POST_COMPACT_RESET_PCT = 95.0


class _CompactCallback(Protocol):
    # Mirrors ``session_compaction.CompactCallback`` exactly, including ``outcome``:
    # this facade re-declares the shape so callers need not import the coordinator,
    # and a narrower copy here makes the two disagree about what a registration must
    # accept. ``outcome`` says WHICH arm ran -- compacted or recycled -- because a
    # recycle is equally "successful" to a caller that only needs headroom, and a
    # surface told only ``success`` announced a summary that never happened.
    async def __call__(  # noqa: E704
        self,
        key: str,
        pct: float,
        *,
        success: bool,
        outcome: str = COMPACT_OUTCOME_COMPACTED,
    ) -> None: ...


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
_BG_BLIND_RECYCLE_PROMPTS = 40  # unconditional backstop: recycle after 40 prompts

# TTL (seconds) for the per-agent model resolution cache. Bounds how long a
# stale resolution — especially the "auto" miss for an agent whose JSON is
# created/edited after first lookup — can survive an in-place file edit that
# does not bump the agents-dir mtime.
_AGENT_MODEL_CACHE_TTL = 30.0

# Persistent session keys — never expired by idle cleanup
_PERSISTENT_KEYS = frozenset({BACKGROUND_KEY, HEARTBEAT_KEY})


def _session_model(
    cfg: "KiroCrewConfig",
    agent: str | None,
    *,
    crew_agent: str | None = None,
    app_name: str = "",
    session_key: str | None = None,
    backend_override: str | None = None,
) -> "str | None":
    """The model a new session on *agent* starts on, for EVERY surface.

    The one execution-path owner of the default-model ladder. It answers
    through :func:`resolve_effective_model`, the resolver the dashboard's model
    chip reads, so a session and its chip cannot disagree. A caller's own model
    (a slot pin) outranks this and is handled before it is reached; below it,
    highest first: the crew's pin, the bound template's pin, the app's
    ``apps.<name>.model`` (*app_name*), the global ``agent.model``, the
    installed agent file. ``""`` and ``"auto"`` inherit at every tier.

    ``crew_agent`` carries the allocation's explicit member identity, including
    "" for a literal template. The empty claim excludes a same-named member's
    pin while preserving the template/global fallback. Without an explicit
    claim, the shared identity resolver retains legacy crew-name inference for
    callers such as Slack and cron.

    Every tier is scoped to the backend that will run the session, chosen by the
    same per-session selection the provider factory makes from *session_key* and
    *backend_override*: a member DM routed to another harness keeps a pin that
    harness serves.

    Returns ``None`` when every tier defers, leaving the backend to pick. A
    resolver error also returns ``None``, with a warning: the provider factory
    then resolves the template pin / global itself, so a cold start never fails
    on the ladder.

    Blocking I/O (globs + reads ``~/.kiro/agents/*.json``): call in an executor.
    """
    from kiro_crew.config.loader import resolve_effective_model
    from kiro_crew.members import is_member_session_key, select_provider_backend

    try:
        backend: str | None = None
        if backend_override is not None or is_member_session_key(session_key):
            backend = select_provider_backend(
                session_key,
                cfg.agent.member_acp_backend,
                cfg.agent.acp_backend,
                backend_override=backend_override,
            )
        crew_name = _resolve_allocation_crew_identity(cfg, agent, crew_agent)
        if crew_name and crew_name in cfg.agents:
            model = resolve_effective_model(cfg, crew_name, app_name=app_name, backend=backend)
        else:
            # No crew: the template tier is the agent this session launches,
            # never a crew that happens to share its name.
            model = resolve_effective_model(
                cfg, None, template=agent or "", app_name=app_name, backend=backend
            )
    except Exception:  # noqa: BLE001 — includes StopIteration from a malformed config
        logger.warning("Failed to resolve the session model for agent %r", agent, exc_info=True)
        return None
    return model or None


# Named by the path callers import them from: a class renders as
# ``module.qualname`` in reprs, pickles and tracebacks, so this module's name
# must not leak into them.
_CompactCallback.__module__ = "kiro_crew.session"
_RecycleCallback.__module__ = "kiro_crew.session"
