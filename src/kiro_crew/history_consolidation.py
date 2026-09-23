"""History consolidation and auto-skill extraction.

The transcript facade owns persistence and locking.  This module owns the
asynchronous consolidation workflow and resolves the few facade-level seams
that tests and embedding applications intentionally replace at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import hashlib
import json
import logging
import math
import re
import time as _time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

from kiro_crew import execution_context
from kiro_crew.config import live
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.frontmatter import SKILL_UPDATE, frontmatter_value
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_INSTRUCTION,
    extracted_lesson_applies,
)
from kiro_crew.llm_helpers import (
    ToolApprovalPolicy,
    background_turn,
)
from kiro_crew.messaging import privacy_mode
from kiro_crew.messaging.link import is_channel_session_key, is_legacy_slack_key
from kiro_crew.project_scope import scope_is_admissible
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skills import AUTO_SKILL_MAX_PROCEDURE_CHARS, AutoSkillProvenance
from kiro_crew.skills_dedupe import (
    VERDICT_DUP,
    VERDICT_NEW,
    VERDICT_UPDATE,
)
from kiro_crew.skills_script_validator import validate_skill_script
from kiro_crew.vector_memory_constants import (
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory import MemoryStore
    from kiro_crew.memory_schema import MemoryFacets
    from kiro_crew.session import SessionManager
    from kiro_crew.skills import SkillsLoader
    from kiro_crew.vector_memory import VectorMemoryStore


_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")

_T = TypeVar("_T")

_CONSOLIDATION_THRESHOLD = 30
_CONSOLIDATION_MAX_ATTEMPTS = 5
_CONSOLIDATION_BACKOFF_BASE_SECS = 900.0
_CONSOLIDATION_BACKOFF_MAX_SECS = 86400.0
_SKILL_DETECTION_WINDOW = 200

#: Wall-clock ceiling on the memory writes of ONE consolidation pass that embed
#: inline. A pass writes up to ``_MAX_SEMANTIC_PER_CONSOLIDATION`` +
#: ``_MAX_EPISODIC_PER_CONSOLIDATION`` rows and each one embeds its text through a
#: blocking inference call, so a degraded embedder makes the pass cost N times one
#: call's latency — all of it on an embed-pool worker, which is shared with every
#: other embed consumer. Past the ceiling the rest of the pass is written with its
#: embedding deferred, so it stops queueing inference it has measured to be slow.
#: Deferred rows are filled in by the standing repair sweep
#: (``backfill_missing_embeddings``), which is what makes deferral lossless.
_EMBED_BUDGET_SECS_PER_PASS = 60.0


class _EmbedBudget:
    """One consolidation pass's embed-time ceiling, and the latch it arms.

    The ceiling is measured over the store WRITES, not over the embed calls
    themselves: the consolidation layer has no seam onto an individual embed, and
    write time is the quantity that actually has to be bounded. Inference is the
    only unbounded part of a write — the rest is local SQLite work — so a slow
    embedder is what normally spends this budget, though lock contention or a
    stalled disk can spend it too. The remedy for either is the same, and it is
    self-healing: the repair sweep embeds what this pass deferred.

    Once tripped the latch stays tripped for the rest of the pass — that is the
    whole point. Without it, an embedder that is slow for the first row is slow for
    every row, and the pass pays that latency once per item before finishing with
    exactly the same rows it would have written anyway (a failed embed already
    stores a NULL vector for the repair sweep to fill).
    """

    __slots__ = ("_budget", "_logger", "_spent", "tripped")

    def __init__(self, budget_secs: float, logger: logging.Logger) -> None:
        self._budget = budget_secs
        self._logger = logger
        self._spent = 0.0
        self.tripped = False

    @contextlib.contextmanager
    def measured(self):
        """Time one store write and charge it to the pass, arming the latch once."""
        started = _time.monotonic()
        try:
            yield
        finally:
            self._spent += _time.monotonic() - started
            if not self.tripped and self._spent >= self._budget:
                self.tripped = True
                # Once per pass, not once per row: a degraded embedder would
                # otherwise repeat this line for every remaining item.
                self._logger.warning(
                    "Consolidation spent %.1fs on memory writes (budget %.1fs); "
                    "embedding is deferred to the repair sweep for the rest of this pass",
                    self._spent,
                    self._budget,
                )


#: Default for the two write helpers' store arguments, meaning "argument not
#: supplied — use the global handle off ``self``". It cannot be ``None``, because
#: ``ContextBuilder.ensure_store`` may answer ``None`` for an unavailable legacy
#: V1 silo (private V2 raises), and that answer must skip the tier rather than
#: inherit the global store:
#: inheriting files one crew's rows into the operator's own memory, which is the
#: misfiling the resolved handles exist to prevent. Typed ``Any`` so each parameter
#: keeps its real annotation.


class _InheritGlobal:
    """Sentinel type for "argument omitted, inherit the consolidator's handle".

    A CLASS rather than a bare ``object()`` so the parameters it defaults can keep
    their real annotations. Typed ``Any``, the sentinel erased mypy's view of both
    store parameters — and ``_save_lessons`` is called positionally, so a swapped
    ``(vector_store, lesson_store)`` pair would have written one crew's lessons
    through the other's handle with nothing to catch it.
    """

    __slots__ = ()


_INHERIT_GLOBAL = _InheritGlobal()

_CONSOLIDATION_META_KEYS: frozenset[str] = frozenset(
    {
        "consolidation_attempts",
        "consolidation_retry_at",
        "consolidation_env_failures",
        "consolidation_attempts_generation",
        "consolidation_attempts_offset",
        "consolidation_attempts_count",
    }
)


class _ConsolidationRefusedSentinel:
    """A retry gate or changed source declined a pass without accepting its writes."""

    __slots__ = ()


_CONSOLIDATION_REFUSED = _ConsolidationRefusedSentinel()


class _ModeTightened(Exception):
    """The target's memory mode tightened since the pass began; raised by :class:`_WriteGate`.

    Raised on whatever thread was about to write, so it unwinds the batch or
    transaction in flight -- a member transaction's own ``except BaseException``
    rolls it back, nothing partial commits -- up to ``_consolidate``, the ONE
    place it is caught, which ends the pass as a refusal (``_refuse_restricted``:
    the same SEL denial and memo entry as a verdict before the snapshot), not as
    a failure that charges the retry budget.
    """

    def __init__(self, mode: str, source: str, site: str) -> None:
        super().__init__(f"{site}: memory mode tightened to {mode} ({source})")
        self.mode = mode
        self.source = source
        self.site = site


# The three durable sources of a consolidation target's memory mode, named the
# way ``_refuse_restricted`` and the dashboard route report them.
TARGET_SOURCE_EXECUTION = "execution record"
TARGET_SOURCE_HEADER = "transcript header"
TARGET_SOURCE_SESSION_MAP = "session map"

# Bound on ``HistoryConsolidator._restricted_refused``, the memo of keys the
# consolidator refused as temporary or incognito so the automatic entry points
# skip them. Insertion-ordered; past the bound the OLDEST key is evicted (one
# debug line). Eviction is cheap and safe: the evicted key is simply eligible
# for the idle sweep again, so its cost is one extra refused attempt and one
# extra SEL denial row for that key, after which it is memoed again. A few
# thousand keys covers every private thread a gateway refuses in one process
# lifetime many times over; the memo is process-local and starts empty.
#
# The two sibling populations this memo is often read beside are bounded
# differently, on purpose. The ``privacy_mode`` trackers are LRUs capped at
# ``privacy_mode.PRIVACY_LRU_MAX`` and hydrate from the session-map rows, so
# their population is the rows'. The privacy-flagged session-map rows share
# that bound (``SessionMap.PRIVACY_ROW_CAP``) but hold it the other way round:
# a row is the record the channel's inbound gate hydrates from, so evicting one
# re-opens the leak this module's refusals close -- so past the cap the map
# REFUSES the next new flag, fail-closed (the modifier tells the user the
# message was not processed), and never evicts a row it holds. No startup step
# removes one either: the startup path only copies a map-only mode into the
# thread's existing transcript header (``SessionMap.stamp_privacy_headers``);
# retiring the rows needs the channel gate to read the header, a separate
# change.
_RESTRICTED_REFUSAL_MEMO_MAX = 4_096


class RestrictedTarget(NamedTuple):
    """A consolidation target whose durable record says temporary or incognito."""

    mode: str
    """``temporary`` or ``incognito``."""
    source: str
    """Which record said so: one of the ``TARGET_SOURCE_*`` names."""


class ConsolidationTarget(NamedTuple):
    """What :func:`resolve_consolidation_target` read about a target, and its verdict."""

    execution: Any
    """The ``ExecutionContext`` bound to the key, or ``None``."""
    metadata: dict
    """The transcript header (``{}`` when there is none, or no log to read it from)."""
    restricted: RestrictedTarget | None
    """``None`` when no durable record calls the target temporary or incognito."""


def _live_channel_key(key: str, sessions: Any) -> str | None:
    """Channel thread *key* as the session map spells it, or ``None``.

    ``None`` for a non-channel key, or when there is no session manager to ask.
    A caller naming the thread by its transcript stem (``slack_<ts>``) is
    unfolded to the live key (``slack:<ts>``) through the map, the only
    authority for the ``:``-to-``_`` fold; a key the map does not know is kept
    as given. ``channel_key_for_stem`` holds the map lock, so this is safe from
    a worker thread as well as the loop.

    The unfold is a walk over the whole map under its lock, so it runs only for
    a key that CAN be a stem. A stem is ``history._safe_key``'s product, which
    folds every ``:`` to ``_``: a key still carrying a ``:`` is the live form
    already, and a legacy bare Slack ``thread_ts`` is neither form (the fold of
    ``slack:<ts>`` is ``slack_<ts>``, never the bare number) -- for both the walk
    could never match, and the write gate's per-mutation tier would otherwise
    pay it for every row it admits.
    """
    if sessions is None:
        return None
    if not (is_channel_session_key(key) or is_legacy_slack_key(key)):
        return None
    if ":" in key or is_legacy_slack_key(key):
        return key
    live_key = key
    unfold = getattr(sessions, "channel_key_for_stem", None)
    if callable(unfold):
        unfolded = unfold(key)
        if isinstance(unfolded, str) and is_channel_session_key(unfolded):
            live_key = unfolded
    return live_key


def channel_thread_mode(key: str, sessions: Any) -> str | None:
    """The ``temporary`` / ``incognito`` mode channel thread *key* holds in the session map.

    A channel thread marked ``!temporary`` or ``!incognito`` records that mode in
    two places (``privacy_mode.apply_mode``): the transcript header's
    ``memory_mode``, which :func:`resolve_restricted_target` reads ahead of this,
    and the session map's per-conversation flag, keyed by the thread's LIVE key
    (``slack:<ts>``). This read covers what the header cannot: a thread flagged
    before the header carried the mode -- the entry is then the only record, and
    the consolidator copies the mode into the header when this read refuses --
    and a mark whose best-effort header write failed. Without it such a thread
    reads as persistent and the idle sweep or the channel's session-end hook
    consolidates its pre-flag transcript into durable memory.

    *sessions* is the ``SessionManager`` whose map the modifier wrote through
    (``None`` keeps the other two sources). The flag is read the way the
    dashboard's ``live_session_memory_mode`` reads it: ``privacy_mode.hydrate``
    restores the durable flag into the process-local trackers, and the trackers
    answer -- which also honours a mark whose disk write failed, held for this
    process only. A caller that names the thread by its transcript stem
    (``slack_<ts>``: the dashboard trigger and the CLI) is unfolded to the live
    key through the map first, the only authority for the ``:``-to-``_`` fold
    (:func:`_live_channel_key`). Cheap and allocation-free for an unflagged
    key: dict lookups plus one pass over the map for a stem, no disk read.
    ``None`` for a non-channel key or an unflagged thread. Event-loop form:
    ``hydrate`` MARKS the trackers; the any-thread form a worker may call is
    :func:`restricted_in_memory`.
    """
    live_key = _live_channel_key(key, sessions)
    if live_key is None:
        return None
    privacy_mode.hydrate(sessions, live_key)
    # Strictest first, the ranking ``privacy_mode.strictest`` uses: a thread
    # carrying both flags reads as temporary.
    if privacy_mode.is_temporary(live_key):
        return privacy_mode.MODE_TEMPORARY
    if privacy_mode.is_incognito(live_key):
        return privacy_mode.MODE_INCOGNITO
    return None


def restricted_in_memory(key: str, sessions: Any) -> RestrictedTarget | None:
    """The mode the IN-MEMORY records call *key* restricted, or ``None``.

    Synchronous and safe on any thread: the per-mutation tier of
    :class:`_WriteGate`, asked immediately before every store mutation from
    whatever thread performs it. Two records, both dict lookups and neither a
    file read: the live execution registry -- a dashboard or API session's
    mode, ``execution_context.read_live_session_execution`` -- and a channel
    thread's process-local trackers beside its session-map flag
    (``privacy_mode.recorded_mode``: the read-only form of
    :func:`channel_thread_mode`, marking nothing, because the trackers are the
    loop's to mutate). The channel modifier writes both of its records before
    its awaited header write, so a ``!temporary`` / ``!incognito`` landing
    between two mutations of one batch is visible to the next mutation's
    admission. What this does NOT see is a mode recorded only in a transcript
    header: that read is a file read and belongs to the boundary tier
    (:func:`resolve_consolidation_target`), asked before each awaited batch and
    transaction is dispatched.
    """
    live = execution_context.read_live_session_execution(key)
    if live is not None and live.memory_mode != "persistent":
        return RestrictedTarget(live.memory_mode, TARGET_SOURCE_EXECUTION)
    live_key = _live_channel_key(key, sessions)
    if live_key is None:
        return None
    mode = privacy_mode.recorded_mode(sessions, live_key)
    if mode is None:
        return None
    return RestrictedTarget(mode, TARGET_SOURCE_SESSION_MAP)


async def resolve_consolidation_target(
    key: str, *, log: ConversationLog | None, sessions: Any
) -> ConsolidationTarget:
    """The ONE read of a consolidation target's durable memory-mode records.

    Both the dashboard route (``api_memory_consolidate``) and
    ``HistoryConsolidator._consolidate`` ask this, so a source known to one is
    known to the other by construction -- a source added here refuses at the
    route and in the background sweep alike, and a source added anywhere else
    is the bug class this exists to close (a thread the route passed and the
    consolidator refused, or the reverse). Three sources, and the ORDER is a
    privacy contract: a mode already KNOWN without opening the transcript
    refuses first -- the execution record (live registry or persisted carrier),
    then a channel thread's session-map flag through the process-local trackers
    (:func:`channel_thread_mode`) -- and only a target neither knows falls
    through to the transcript header's ``memory_mode`` (which the channel
    modifier stamps too, so a transcript read never depends on the session
    map) -- and the flag is read ONCE MORE after that awaited header read,
    before an unrestricted verdict: a modifier landing while the read is in
    flight has marked the tracker and flagged the map before its header write
    lands, so the second dict lookup is what catches it, and its restricted
    answer wins. A restricted session whose mode is known is therefore refused without
    this resolver, or the pass behind it, opening its transcript, not even for
    the header (``test_restricted_consolidation_never_reads_transcript_or_opens_memory``
    pins this); an entry point's own pre-checks ahead of the pass
    (``consolidate_session``, ``consolidate_now``) are outside that scope.
    ``restricted is None`` means no durable record calls the target
    restricted; it says nothing about LIVE state (a dashboard slot's mode, an
    inherited subagent mode), which the route reads separately and the
    background paths cannot see. The header read on the fall-through rides
    along, so the consolidator resolves its memory identity from the same
    execution record and header without a second read. The file reads run off
    the loop.
    """
    # Call-time import, and the one that has to be: ``kiro_crew.history`` imports
    # THIS module at module scope (its facade re-exports the consolidator), so a
    # module-scope import here is the cycle ``history -> history_consolidation
    # -> history`` and fails on whichever side loads second.
    from kiro_crew.history import transcript_privacy_mode

    execution = await asyncio.to_thread(execution_context.read_session_execution, key)
    if execution is not None and execution.memory_mode != "persistent":
        return ConsolidationTarget(
            execution, {}, RestrictedTarget(execution.memory_mode, TARGET_SOURCE_EXECUTION)
        )
    mode = channel_thread_mode(key, sessions)
    if mode is not None:
        return ConsolidationTarget(execution, {}, RestrictedTarget(mode, TARGET_SOURCE_SESSION_MAP))
    metadata: dict = {}
    if log is not None:
        read = await asyncio.to_thread(log.get_metadata, key)
        if isinstance(read, dict):
            metadata = read
    restricted: RestrictedTarget | None = None
    # Normalized (``transcript_privacy_mode``: the shared predicate's rule), so
    # a header spelled ``Temporary`` refuses AS temporary -- the mode the SEL
    # record, the memo, the route's 403 body and the tab's tally then carry --
    # rather than as the string it happens to hold.
    if header_mode := transcript_privacy_mode(metadata.get("memory_mode")):
        restricted = RestrictedTarget(header_mode, TARGET_SOURCE_HEADER)
    elif (mode := channel_thread_mode(key, sessions)) is not None:
        # The header read above is an await, and the channel modifier lands
        # its records in an order that opens a window across it: the tracker
        # mark and the map flag are synchronous, the header write is awaited
        # after them. A modifier landing while the header read is in flight
        # therefore leaves a header that predates the mode beside a flag that
        # already records it -- so the flag is read AGAIN after the await, and
        # a restricted answer here wins over the header just read. Dict
        # lookups only (``channel_thread_mode``), no second file read. The
        # execution record is not re-read: a slot's mode is fixed when the
        # slot is created and no path tightens it live, so the first read is
        # the last word, and it costs a file read.
        restricted = RestrictedTarget(mode, TARGET_SOURCE_SESSION_MAP)
    return ConsolidationTarget(execution, metadata, restricted)


class _WriteGate:
    """The one path every durable write of a consolidation pass takes.

    A pass resolves its target's mode once, before the snapshot, then spends
    minutes in model calls and offloaded writes. A ``!temporary`` /
    ``!incognito`` landing anywhere in that time must stop every write that has
    not happened yet -- wherever the next one sits: the next awaited batch, the
    next mutation inside a batch, the commit of a member transaction. A check
    placed at each site guards that site and no other; the four heads before
    this one each moved the same window one call further. So the check lives
    IN the write path instead: ``_consolidate`` and its helpers hold no store
    they write through directly. Every durable write is a verb of this gate,
    and every verb re-reads the mode at the moment of writing. Two tiers, by
    what they cost:

    * :meth:`admit` -- before EVERY mutation, on whatever thread performs it:
      the in-memory records only (:func:`restricted_in_memory` -- the live
      execution registry, a channel thread's trackers and map flag), dict
      lookups, no file read. Every write verb below calls it first; a member
      transaction calls it before each of its mutations and once more before
      its commit.
    * :meth:`boundary` -- before every awaited write batch or transaction is
      DISPATCHED (:meth:`run`, :meth:`run_in_thread`): the full resolution the
      pass started with, transcript header included
      (:func:`resolve_consolidation_target`), its file reads off the loop. Also
      asked once directly, ahead of the skill pass: that pass hands the FULL
      transcript to a model, and a transcript that just turned private must not
      be disclosed to it either, durable write or not.

    Either tier raises :class:`_ModeTightened`, which unwinds the batch or
    transaction in flight -- a member transaction rolls back, nothing partial
    commits -- up to ``_consolidate``, the one place it is caught: the pass
    ends with the same SEL denial and memo entry as a refusal before the
    snapshot (``_refuse_restricted``). A write that completed before the
    tightening stands; nothing purges, by the existing design line.

    The verbs below ARE the inventory of durable-write entry points
    consolidation uses, each named as the store names it, one admission in
    front. ``test/test_consolidation_write_gate.py`` reads that inventory from
    this class and asserts over the module's syntax tree that no verb is
    called or handed to an executor anywhere else, that every verb admits
    before it writes, that both dispatchers resolve before they dispatch, that
    the write batches are dispatched only through them, and that outside this
    class a store is only ever read. Stateless: a gate is (consolidator, key),
    and ``_consolidate`` hands its one instance to every helper that writes.
    """

    def __init__(self, consolidator: "HistoryConsolidator", key: str) -> None:
        self._consolidator = consolidator
        self._key = key

    @property
    def key(self) -> str:
        """The session key every write of this pass is for."""
        return self._key

    # -- the two tiers -----------------------------------------------------

    def admit(self, site: str) -> None:
        """Per-mutation tier: the in-memory records, any thread, no file read."""
        restricted = restricted_in_memory(self._key, self._consolidator._sessions)
        if restricted is not None:
            raise _ModeTightened(restricted.mode, restricted.source, site)

    async def boundary(self, site: str) -> None:
        """Per-batch tier: the full resolution, header included, before a dispatch."""
        target = await resolve_consolidation_target(
            self._key, log=self._consolidator._log, sessions=self._consolidator._sessions
        )
        if target.restricted is not None:
            raise _ModeTightened(target.restricted.mode, target.restricted.source, site)

    async def run(self, site: str, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Resolve at the boundary, then run write batch *fn* on the embed pool.

        For a batch that embeds (structured memory, lessons) or takes the
        cross-process history lock: the bounded pool, not a bare thread.
        """
        await self.boundary(site)
        return await run_in_embed_pool(fn, *args, **kwargs)

    async def run_in_thread(
        self, site: str, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> _T:
        """Resolve at the boundary, then run write *fn* on a worker thread.

        For a write that neither embeds nor contends on the history lock: the
        offset advance (an fsync-backed transcript rewrite) and the skill pass.
        """
        await self.boundary(site)
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- the write verbs: the store's own, the admission INSIDE the store -------
    # Positional-only store, then the store method's own arguments untouched,
    # so a verb cannot drift from the signature it fronts.
    #
    # A verb admits NOTHING itself. It hands ``admit`` into the store as the
    # method's ``admit`` hook, and the store asks it as the LAST step inside its
    # own lock -- after any embedding or read, immediately before the mutation,
    # and again before the commit where it holds a transaction -- so there is
    # exactly ONE admission site per mutation, and it sits where the mutation
    # is decided. An admission taken here, ahead of the call, cannot hold: the
    # store then waits on its own lock (the history file's ``.append.lock``, the
    # database's ``_db_lock``) or embeds for seconds, and a mode that lands in
    # that wait would let the write follow the restriction. A raise from the hook
    # unwinds through the store's own rollback or leaves its file untouched --
    # the store is byte-identical. ``test_consolidation_write_gate.py`` pins the
    # shape on both sides: every verb passes the hook and admits nothing, every
    # store method asks it under its lock ahead of its first mutation.

    def set_semantic(self, store: Any, /, *args: Any, **kwargs: Any) -> Any:
        return store.set_semantic(
            *args, **kwargs, admit=functools.partial(self.admit, "the semantic write")
        )

    def propose_semantic_delete(self, store: Any, /, *args: Any, **kwargs: Any) -> Any:
        return store.propose_semantic_delete(
            *args, **kwargs, admit=functools.partial(self.admit, "the semantic delete proposal")
        )

    def delete_semantic(self, store: Any, /, *args: Any, **kwargs: Any) -> Any:
        return store.delete_semantic(
            *args, **kwargs, admit=functools.partial(self.admit, "the semantic delete")
        )

    def write_episodic(self, store: Any, /, *args: Any, **kwargs: Any) -> Any:
        return store.write_episodic(
            *args, **kwargs, admit=functools.partial(self.admit, "the episodic write")
        )

    def write_lesson(self, store: Any, /, *args: Any, **kwargs: Any) -> Any:
        return store.write_lesson(
            *args, **kwargs, admit=functools.partial(self.admit, "the lesson write")
        )

    def save(self, lesson_store: Any, /, *args: Any, **kwargs: Any) -> Any:
        return lesson_store.save(
            *args, **kwargs, admit=functools.partial(self.admit, "the lesson file write")
        )

    def append_history(self, memory: Any, /, *args: Any, **kwargs: Any) -> Any:
        return memory.append_history(
            *args, **kwargs, admit=functools.partial(self.admit, "the history write")
        )

    def write_preferences(self, memory: Any, /, *args: Any, **kwargs: Any) -> Any:
        return memory.write_preferences(
            *args, **kwargs, admit=functools.partial(self.admit, "the preferences write")
        )

    def write_projects(self, memory: Any, /, *args: Any, **kwargs: Any) -> Any:
        return memory.write_projects(
            *args, **kwargs, admit=functools.partial(self.admit, "the projects write")
        )

    def stage_skill_candidate(self, loader: Any, /, *args: Any, **kwargs: Any) -> Any:
        return loader.stage_skill_candidate(
            *args, **kwargs, admit=functools.partial(self.admit, "the skill candidate write")
        )

    def create_auto_skill(self, loader: Any, /, *args: Any, **kwargs: Any) -> Any:
        return loader.create_auto_skill(
            *args, **kwargs, admit=functools.partial(self.admit, "the skill write")
        )

    def update_auto_skill(self, loader: Any, /, *args: Any, **kwargs: Any) -> Any:
        return loader.update_auto_skill(
            *args, **kwargs, admit=functools.partial(self.admit, "the skill refinement write")
        )

    def mark_consolidated(self, log: Any, /, *args: Any, **kwargs: Any) -> Any:
        return log.mark_consolidated(
            *args, **kwargs, admit=functools.partial(self.admit, "the offset advance")
        )

    def apply_consolidation(self, store: Any, /, *args: Any, **kwargs: Any) -> Any:
        """The member transaction: admitted before each mutation and before its commit.

        The store performs every mutation and the commit inside one
        ``BEGIN IMMEDIATE`` it owns, so the per-mutation tier is handed IN as
        the store's ``admit`` hook rather than wrapped around the call; a raise
        from it reaches the store's own ``except BaseException`` and the
        transaction rolls back whole. Like every verb, this one admits nothing
        itself: the hook is the one admission site.
        """
        return store.apply_consolidation(
            *args, admit=functools.partial(self.admit, "the member transaction"), **kwargs
        )


class AttemptedSpan(NamedTuple):
    """Identity of the transcript span a billed consolidation turn covered."""

    total: int
    generation: int
    offset: int


class _ConsolidationNotDispatched(Exception):
    """A consolidation prompt never reached the provider."""


def _persistence_disabled() -> bool:
    """True when the operator turned persistent memory off.

    ``memory.persistence_enabled`` is the global persistence switch: consolidation
    is the largest automatic writer (lessons, semantic, episodic, preferences,
    projects, history, auto-skills all flow from one pass), so a disabled
    system must not schedule it — pausing entirely rather than run-and-discard,
    so no LLM turn is ever billed for output that would be thrown away.
    Read through ``KiroCrewConfig.load()`` (fingerprint-cached, so per-turn
    checks cost a stat) rather than a constructor flag, so flipping the key
    takes effect without a gateway restart. Imported lazily to keep this
    module's import graph light (same rationale as the facade seams above).
    """
    from kiro_crew.config.loader import KiroCrewConfig

    return not KiroCrewConfig.load().memory.persistence_enabled


def _fmt_message(message: dict) -> str:
    """Render one transcript message for a consolidation prompt."""
    tools = f" [tools: {', '.join(message['tools'])}]" if message.get("tools") else ""
    return (
        f"[{message.get('ts', '?')[:16]}] {message['role'].upper()}"
        f"{tools}: {message['content']}"
    )


_PLACEHOLDER_BODIES = frozenset(
    {
        "unchanged",
        "no change",
        "no changes",
        "no change needed",
        "no changes needed",
        "no changes required",
        "no update",
        "no updates",
        "no update needed",
        "no updates needed",
        "nothing changed",
        "nothing to update",
        "nothing to change",
        "none",
        "n/a",
        "na",
        "empty",
        "same",
        "same as before",
        "as before",
        "see above",
        "content unchanged",
        "file unchanged",
    }
)


def _is_plausible_memory_file(content: str, header: str) -> bool:
    """Refuse placeholder text before it overwrites a complete memory file."""
    first_line, _, body = content.strip().partition("\n")
    if first_line.strip() != header:
        return False
    normalized = body.strip().lower().strip(" \t\"'`*_~.,!()[]")
    return normalized not in _PLACEHOLDER_BODIES


def _session_facets(meta: dict, key: str) -> "MemoryFacets":
    """The carve axes for everything this session's consolidation writes.

    The consolidator is the writer worth threading first: it is the one that
    produces most rows and it already holds every axis one line away. The others
    have no identity in scope and would have to invent one.

    ``crew`` comes from the session's ``agent`` metadata, which is the CREW alias
    (``cfg.agents`` key) rather than the kiro-cli agent template. Confusing the two
    is a named bug -- resolving a store from the template answers ``default`` for
    exactly the crew that configured otherwise -- so this reads ``meta["agent"]`` and
    never ``kiro_agent``. Note that is a DIFFERENT key from the one
    :func:`kiro_crew.context.store_of_session` reads (``meta["memory_store"]``): a crew
    alias and the store it binds to are separate facts. Each named member now owns a
    unique private store; recording the alias separately preserves its provenance.

    ``surface`` uses ``telemetry_channel_of``, whose output is a BOUNDED label and
    never the key itself, so the column cannot acquire one value per conversation.
    Not ``sel._infer_source``, which fails OPEN to ``"slack"`` for an unrecognised
    key and would file dashboard rows inside a slack carve.

    Never raises: a facet is an index projection, and no carve axis is worth
    failing a consolidation over.
    """
    from kiro_crew.memory_schema import MemoryFacets

    try:
        from kiro_crew.messaging.link import telemetry_channel_of

        surface = telemetry_channel_of(key)
    except Exception:
        surface = ""
    crew = meta.get("agent")
    return MemoryFacets(
        surface=surface,
        crew=crew if isinstance(crew, str) else "",
        session_key=key,
    )


def _facade_sel() -> Any:
    from kiro_crew import history as history_facade

    return history_facade.sel()


def _facade_stream_and_collect(*args: Any, **kwargs: Any) -> Awaitable[str | None]:
    from kiro_crew import history as history_facade

    return history_facade.stream_and_collect(*args, **kwargs)


def _facade_stream_and_collect_json(*args: Any, **kwargs: Any) -> Awaitable[dict | None]:
    from kiro_crew import history as history_facade

    return history_facade.stream_and_collect_json(*args, **kwargs)


def _facade_metadata_dedupe_verdict(
    candidate: dict,
    existing: list[dict],
    judge: Callable[[str], str],
) -> tuple[str, str | None]:
    from kiro_crew import history as history_facade

    return history_facade.metadata_dedupe_verdict(candidate, existing, judge)


# ── Module-level helpers for auto skill eligibility ──
#
# Kept at module level so they're trivially unit-testable without
# instantiating HistoryConsolidator.

# Canonical tool titles that indicate a read targeting a sensitive path.
# Supplements is_sensitive_path() and is_sensitive_bash_command() which
# handle the actual runtime blocking — this is a second-layer defense
# that refuses to extract a skill if the session tried to access a
# sensitive path, even when the attempt was denied at hook time.
_SENSITIVE_TOOL_PATTERNS: tuple[str, ...] = (
    ".aws/",
    ".ssh/",
    ".gnupg/",
    ".gpg/",
    ".docker/config",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # Kiro Crew's own credential file. The data home moved to ~/.kiro/crew, so the
    # LIVE secret is ~/.kiro/crew/.env; cover the pre-move legacy home too
    # (substring match, so bare "/.env"-suffixed forms).
    ".kiro/crew/.env",
    ".kirocrew/.env",
    "169.254.169.254",  # IMDS
)


_TOOL_ROLES: frozenset[str] = frozenset({"tool", "tool_call", "tool_result"})


def _frontmatter_value(text: str | None, key: str) -> str:
    """Return *key*'s frontmatter value from a SKILL.md body, or "".

    Values resolve the way ``SkillsLoader._parse_frontmatter`` resolves them:
    only a column-0 key is a field, and a bare block-scalar indicator
    (``>``/``|``, optionally chomped) folds the indented lines that follow.
    The auto-skill update path carries the live skill's ``description`` and
    ``triggers`` through this reader into a staged candidate that overwrites
    the live skill on approval — reading the indicator verbatim would collapse
    a block-scalar description to ``""`` and inject a bogus ``>`` trigger on
    that round-trip. The grammar (plus the leading-whitespace opener
    tolerance, verbatim plain values, and first-duplicate-wins lookup) is
    pinned as ``frontmatter.SKILL_UPDATE``.
    """
    if not text:
        return ""
    return frontmatter_value(text, key, SKILL_UPDATE)


def _merge_trigger_lists(live: str, candidate: str, *, cap: int = 12) -> str:
    """Union two comma-separated trigger lists, live first, case-insensitively
    deduped and capped.

    Triggers are the skill's ACTIVATION surface. An update proposes triggers for
    the new requirement only, so replacing the live list would stop the skill
    firing on every phrasing it already answered — a silent regression the diff
    shows but nobody reads as a behavior change. Union instead, and cap so
    repeated updates cannot grow the list without bound.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for raw in (live or "").split(",") + (candidate or "").split(","):
        t = re.sub(r"\s+", " ", raw).strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        merged.append(t)
        if len(merged) >= cap:
            break
    return ", ".join(merged)


def _strip_skill_frontmatter(text: str | None) -> str:
    """Return *text* with a leading ``---`` frontmatter block removed.

    A skill body read off disk carries its frontmatter header; only the prose
    below it may be fed to (or accepted from) the update-merge turn, because
    ``stage_skill_candidate`` re-emits frontmatter of its own. Text without a
    leading block is returned unchanged (stripped). A fence LOCATOR, not a
    field parser — deliberately outside ``kiro_crew.frontmatter``; editing
    its grammar means revisiting ``_frontmatter_value``'s dialect too. Like
    that dialect's fence, an optional carriage return before each fence
    newline is tolerated, so the locator strips exactly the block the field
    parser reads.
    """
    if not text:
        return ""
    m = re.match(r"^\s*---\r?\n.*?\r?\n---\r?\n?(.*)$", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _strip_code_fence(text: str) -> str:
    """Unwrap a single outer ```/```markdown fence, if the model emitted one."""
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    lines = s.split("\n")
    if len(lines) < 2:
        return s
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _count_tool_call_messages(messages: list[dict]) -> int:
    """Count messages that represent tool invocations under either schema.

    Two recording formats exist:
    - Legacy (Slack pipeline): assistant messages carry a ``tools`` list field.
    - Dashboard pipeline: separate messages with ``role`` in {"tool", "tool_call",
      "tool_result"} and the tool name embedded in ``content``.

    A message matching EITHER condition counts once (no double-counting).
    """
    count = 0
    for msg in messages:
        tools = msg.get("tools")
        if isinstance(tools, list) and tools:
            count += 1
        elif msg.get("role") in _TOOL_ROLES:
            count += 1
    return count


def _session_touched_sensitive(messages: list[dict]) -> bool:
    """Return True if any tool call in the session referenced a sensitive path.

    Checks both recording schemas:
    - Legacy: substring match over each entry in ``msg["tools"]`` list.
    - Dashboard: substring match over ``content`` when ``role`` indicates a tool event.

    Designed to be conservative — a false positive just means we skip
    auto-creation for this session.
    """
    for msg in messages:
        # Legacy schema: tools list on assistant messages
        tools = msg.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, str):
                    continue
                lower = tool.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
        # Dashboard schema: role="tool" with tool info in content
        if msg.get("role") in _TOOL_ROLES:
            content = msg.get("content", "")
            if isinstance(content, str):
                lower = content.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
    return False


class HistoryConsolidator:
    """Summarize old messages into structured memory via LLM.

    Two consolidation paths:
    - Preferences/projects: triggered by message count (30 messages)
    - Daily history: triggered by idle time (3h default) or end of day
    """

    def __init__(
        self,
        log: ConversationLog,
        memory: MemoryStore,
        sessions: SessionManager | None = None,
        lesson_store: LessonStore | None = None,
        history_idle_secs: float = 3 * 3600,
        vector_store: "VectorMemoryStore | None" = None,
        migrated: bool = False,
        # ── Auto skill creation ──
        # All-default so callers unaware of this feature continue to work.
        skills_loader: "SkillsLoader | None" = None,
        auto_skills_enabled: bool = False,
        auto_refine_enabled: bool = False,
        auto_min_tool_calls: int = 5,
        auto_similarity_threshold: float = 0.85,
        # ── Staged approval + lifecycle (v2) ──
        approval_required: bool = True,
        max_auto_skills: int = 100,
        stale_after_days: int = 30,
        archive_after_days: int = 90,
        generate_scripts: bool = True,
        judge_model: str = "",
    ) -> None:
        self._log = log
        self._memory = memory
        self._sessions = sessions
        self._lesson_store = lesson_store
        self._history_idle_secs = history_idle_secs
        self._vector_store = vector_store
        self._migrated = migrated
        self._skills_loader = skills_loader
        self._auto_skills_enabled = auto_skills_enabled
        self._auto_refine_enabled = auto_refine_enabled
        self._auto_min_tool_calls = auto_min_tool_calls
        self._auto_similarity_threshold = auto_similarity_threshold
        self._approval_required = approval_required
        self._max_auto_skills = max_auto_skills
        self._stale_after_days = stale_after_days
        self._archive_after_days = archive_after_days
        self._generate_scripts = generate_scripts
        self._judge_model = judge_model
        # Captured on the first _consolidate (the gateway loop) so the sync,
        # thread-offloaded _process_auto_skills can bridge the async dedupe
        # judge back onto the loop. Throttle guards the autonomous lifecycle.
        self._event_loop: "asyncio.AbstractEventLoop | None" = None
        self._last_lifecycle: float = 0.0
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # Keys _refuse_restricted refused as temporary or incognito, with the
        # mode: the memo the two AUTOMATIC entry points (check_idle_sessions,
        # maybe_consolidate) consult before scheduling, so neither re-attempts
        # a session already known to be refused. The explicit triggers
        # (consolidate_session, consolidate_now, the dashboard route) do not
        # read it: an explicit attempt is attempted, refused and audited every
        # time. Only a COMMITTED mode is memoed (see _refuse_restricted): a
        # committed mode only ever tightens, so its memo cannot go stale; a
        # provisional one -- a reservation a failed steer may release -- is
        # refused without being remembered. Process-local, so a restart costs
        # one refusal per thread. Bounded by _RESTRICTED_REFUSAL_MEMO_MAX,
        # oldest evicted (see _refuse_restricted).
        self._restricted_refused: dict[str, str] = {}
        # Track last activity per session for idle-based history consolidation
        self._last_activity: dict[str, float] = {}
        self._history_consolidated: dict[str, float] = {}  # key → last history consolidation time
        # Separate offset for prefs-only consolidation (doesn't advance main offset)
        self._prefs_offset: dict[str, int] = {}
        # Session length at the last skill-detection pass, so an unchanged
        # (rotation_generation, message_count) at the last skill-detection
        # pass, so an unchanged session isn't re-judged on every history
        # consolidation — while a rotation (which bumps the generation and
        # swaps the window's content) still forces a fresh pass.
        self._last_skillgen_marker: dict[str, tuple[int, int]] = {}
        # Every tunable above is a copy of skills.* / memory.* config, so a write to
        # config.json reaches them only through reconfigure(). Held on self because
        # the watcher holds the owner weakly.
        self._config_sub = live.watch_object(
            self,
            "skills",
            "memory.history_idle_hours",
            "memory.migrated",
            name="HistoryConsolidator",
        )

    def reconfigure(self, cfg: object) -> None:
        """Push the live ``skills.*`` and consolidation settings onto this instance.

        These only ever gate the NEXT consolidation pass or the next auto-skill
        judgement, so swapping them mid-flight cannot corrupt work already running:
        a pass that has already read a threshold finishes on the old value and the
        next one uses the new one.
        """
        skills = getattr(cfg, "skills")
        memory = getattr(cfg, "memory")
        self._history_idle_secs = float(getattr(memory, "history_idle_hours")) * 3600
        self._migrated = bool(getattr(memory, "migrated"))
        self._auto_skills_enabled = bool(getattr(skills, "auto_create_from_sessions"))
        self._auto_refine_enabled = bool(getattr(skills, "auto_refine_on_deviation"))
        self._auto_min_tool_calls = int(getattr(skills, "auto_min_tool_calls"))
        self._auto_similarity_threshold = float(getattr(skills, "auto_similarity_threshold"))
        self._approval_required = bool(getattr(skills, "approval_required"))
        self._max_auto_skills = int(getattr(skills, "max_auto_skills"))
        self._stale_after_days = int(getattr(skills, "stale_after_days"))
        self._archive_after_days = int(getattr(skills, "archive_after_days"))
        self._generate_scripts = bool(getattr(skills, "generate_scripts"))
        self._judge_model = str(getattr(skills, "judge_model"))

    @property
    def _logger(self) -> logging.Logger:
        """Keep the pre-extraction ``kiro_crew.history`` logger category."""
        return _HISTORY_LOGGER

    def retry_eligible(
        self, key: str, now: float | None = None, message_count: int | None = None
    ) -> bool:
        """True when *key* may spend a billed consolidation turn right now.

        Every automatic entry point consults this so a span whose consolidation
        keeps failing backs off instead of re-billing an LLM turn on each sweep,
        and _consolidate() itself enforces it as the final gate, so an entry
        point without a pre-check of its own still cannot bypass the backoff.
        A span at :data:`_CONSOLIDATION_MAX_ATTEMPTS` is refused: the abandon path
        normally writes the marker (which also clears the accounting), so reaching
        here at the cap means even that write failed, and refusing keeps a broken
        span from spending forever.

        That refusal covers the SPAN, not the session. The cap is scoped to the
        content it measured, so a rotation or new messages release it with a fresh
        bounded budget (see
        :meth:`ConversationLog._attempts_describe_current_span`) — otherwise one
        transient marker-write failure would stop this session from ever
        consolidating again.

        Costs one metadata-line read and NO transcript read: this runs on the
        gateway event loop (heartbeat sweep, expiry, dashboard trigger), where a
        synchronous full-file read would stall every other gateway task on a large
        transcript. *message_count* is the transcript's current total, which every
        automatic caller already holds from its own
        :meth:`ConversationLog.consolidation_counts` call; omitting it skips the
        extent test and keeps the cap.
        """
        attempts, retry_at = self._log.consolidation_retry_state(key, message_count)
        if attempts >= _CONSOLIDATION_MAX_ATTEMPTS:
            return False
        return (_time.time() if now is None else now) >= retry_at

    async def _note_failed_attempt(self, key: str, span: AttemptedSpan, reason: str) -> None:
        """Charge one attempt for a billed turn that never reached the marker.

        Called only once the prompt has actually reached the provider, so a
        pre-dispatch failure (no session manager, kiro-cli failing to start) and a
        cheap pre-call failure (snapshot, metadata read) both keep their free
        retry. At the attempt cap the durable marker is written anyway and the span
        is abandoned with a warning: the alternative is re-billing this failure
        indefinitely.

        *span* is the pre-turn snapshot identity (see :class:`AttemptedSpan`), used
        both to stamp the charge and to place the abandon marker — the same values
        for both, so the marker cannot be written for a span other than the one the
        cap was reached on.
        """
        try:
            attempts, retry_at = await asyncio.to_thread(
                self._log.record_consolidation_failure,
                key,
                _CONSOLIDATION_BACKOFF_BASE_SECS,
                _CONSOLIDATION_BACKOFF_MAX_SECS,
                span,
            )
        except Exception:
            # Without a persisted count the sweep cannot back off, so say so
            # loudly — but never let bookkeeping mask the original failure.
            self._logger.warning(
                "Could not persist consolidation retry state for %s", key, exc_info=True
            )
            return
        if attempts < 1:
            # The session was deleted mid-consolidation, so nothing was recorded
            # and there is no span left to abandon.
            return
        if attempts < _CONSOLIDATION_MAX_ATTEMPTS:
            self._logger.warning(
                "Consolidation attempt %d/%d failed for %s (%s); " "next attempt in %.0fs",
                attempts,
                _CONSOLIDATION_MAX_ATTEMPTS,
                key,
                reason,
                max(0.0, retry_at - _time.time()),
            )
            return
        self._logger.warning(
            "Abandoning consolidation for %s after %d failed attempts (%s): "
            "marking %d messages consolidated WITHOUT a memory pass, so this "
            "span's history/preferences/lessons are not extracted",
            key,
            attempts,
            reason,
            span.total,
        )
        try:
            await asyncio.to_thread(self._log.mark_consolidated, key, span.total, span.generation)
        except Exception:
            # The count stays at the cap, so retry_eligible() keeps refusing —
            # the span stops spending even though the marker is missing.
            self._logger.warning(
                "Could not mark abandoned consolidation for %s", key, exc_info=True
            )

    async def _note_environment_failure(self, key: str, reason: str) -> None:
        """Arm the backoff for a consolidation that never reached the provider.

        Deliberately does NOT touch the attempt cap. A pre-dispatch failure spends
        nothing, so abandoning the span over one would write the durable marker
        over messages no LLM has ever read — losing a memory pass to a broken
        kiro-cli install rather than to a genuinely unprocessable span. The
        environment counter only widens the retry interval, so a permanently broken
        host settles at the backoff ceiling instead of re-attempting every tick.
        """
        try:
            failures, retry_at = await asyncio.to_thread(
                self._log.record_consolidation_environment_failure,
                key,
                _CONSOLIDATION_BACKOFF_BASE_SECS,
                _CONSOLIDATION_BACKOFF_MAX_SECS,
            )
        except Exception:
            self._logger.warning(
                "Could not persist consolidation environment backoff for %s",
                key,
                exc_info=True,
            )
            return
        if failures < 1:
            return
        self._logger.warning(
            "Consolidation for %s could not reach the LLM (%s; environment "
            "failure #%d, nothing billed); retrying in %.0fs without consuming "
            "the attempt budget",
            key,
            reason,
            failures,
            max(0.0, retry_at - _time.time()),
        )

    def maybe_consolidate(self, key: str) -> None:
        """Fire preferences/projects consolidation if message threshold exceeded."""
        self._last_activity[key] = _time.time()
        if _persistence_disabled():
            return
        if key in self._running:
            return
        # Same memo as the idle sweep: past the threshold a refused private
        # thread would otherwise schedule (and audit) a refusal on EVERY turn,
        # since a refusal advances no offset.
        if key in self._restricted_refused:
            return
        total = len(self._log._read_messages(key))
        prefs_off = self._prefs_offset.get(key, 0)
        if total - prefs_off < _CONSOLIDATION_THRESHOLD:
            return
        # Cheap pre-check mirroring the other automatic entry points. This
        # runs on every user turn, so during a backoff window every message
        # past the threshold would otherwise schedule a task whose snapshot
        # takes the per-file lock (the same one appends contend on) and reads
        # the transcript, only to be refused by the gate inside _consolidate().
        # retry_eligible costs one metadata-line read and no transcript read;
        # the inner gate remains the enforcement backstop.
        if not self.retry_eligible(key, message_count=total):
            return
        self._running.add(key)
        t = asyncio.create_task(self._consolidate(key, include_history=False))
        self._tasks.add(t)

        def _on_done(fut: asyncio.Task, k: str = key, off: int = total) -> None:  # type: ignore[type-arg]
            self._tasks.discard(fut)
            if (
                not fut.cancelled()
                and fut.exception() is None
                # A refusal ran no pass over the window. Advancing the offset
                # anyway would mark the window consolidated, so once the
                # backoff expires the threshold test skips it until a whole new
                # threshold of messages accumulates — silently dropping its
                # preference/project extraction.
                and fut.result() is not _CONSOLIDATION_REFUSED
            ):
                self._prefs_offset[k] = off

        t.add_done_callback(_on_done)

    def check_idle_sessions(self) -> None:
        """Check all tracked sessions for idle-based history consolidation."""
        if _persistence_disabled():
            return
        now = _time.time()
        for key, last in list(self._last_activity.items()):
            if now - last < self._history_idle_secs:
                continue
            # Already refused as temporary or incognito by a pass this process
            # ran: a refusal sets no throttle (it is not a completed pass), so
            # without this the sweep would re-schedule the key every tick and
            # write one audit denial a minute for the life of the process.
            # The memo is checked first, ahead of the metadata reads below.
            if key in self._restricted_refused:
                continue
            total, unconsolidated = self._log.consolidation_counts(key)
            if (
                unconsolidated < 1
                or now - self._history_consolidated.get(key, 0) < self._history_idle_secs
                or key in self._running
                # Durable backoff, checked last so it only costs a metadata read
                # once the cheap conditions pass. The in-memory throttle above is
                # set only when the task ends without an exception and is lost on
                # restart, so it alone cannot stop a repeatedly failing span from
                # re-billing an LLM turn every tick. *total* comes from the read
                # above, so the check adds no transcript read on the loop.
                or not self.retry_eligible(key, now, message_count=total)
            ):
                continue
            self._running.add(key)
            captured_now = now
            t = asyncio.create_task(self._consolidate(key, include_history=True))
            self._tasks.add(t)

            def _on_idle_done(
                fut: asyncio.Task,  # type: ignore[type-arg]
                k: str = key,
                ts: float = captured_now,
            ) -> None:
                self._tasks.discard(fut)
                if (
                    not fut.cancelled()
                    and fut.exception() is None
                    # A refusal is not a completed pass; setting the throttle
                    # for it would delay the retry past the backoff deadline.
                    and fut.result() is not _CONSOLIDATION_REFUSED
                ):
                    self._history_consolidated[k] = ts

            t.add_done_callback(_on_idle_done)

    def consolidate_session(self, key: str) -> None:
        """Trigger history consolidation for *key* (fire-and-forget).

        Used by session-end hooks (dashboard close, Slack end, idle expiry)
        and the ``kirocrew consolidate`` CLI command.  Skips if the session
        is already being consolidated, has no unconsolidated messages, or is
        inside the durable consolidation retry backoff.

        Safety: skill detection (_run_skill_detection) re-checks
        _session_touched_sensitive() over its window before proposing anything,
        so sensitive sessions never produce skills regardless of entry point.
        """
        if key in self._running:
            return
        if _persistence_disabled():
            return
        total, unconsolidated = self._log.consolidation_counts(key)
        if unconsolidated < 1:
            return
        # This path consults no time-based throttle at all — every session expiry
        # for the same key fires a fresh consolidation — so the durable backoff
        # stands between a repeatedly failing span and one billed LLM turn per
        # expiry. Checked here (as well as inside _consolidate()) so the skip is
        # logged before a task is ever scheduled.
        if not self.retry_eligible(key, message_count=total):
            self._logger.info(
                "consolidate_session skipped for %s: consolidation retry backoff", key
            )
            return
        # Short-circuit sensitive sessions before scheduling a task
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            self._logger.info("consolidate_session skipped for %s: sensitive session", key)
            return
        self._running.add(key)
        t = asyncio.create_task(self._consolidate(key, include_history=True))
        self._tasks.add(t)

        def _on_done(
            fut: asyncio.Task,  # type: ignore[type-arg]
            k: str = key,
        ) -> None:
            self._tasks.discard(fut)
            self._running.discard(k)
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc is None:
                # A refusal is not a completed pass; leave the throttle unset.
                if fut.result() is not _CONSOLIDATION_REFUSED:
                    self._history_consolidated[k] = _time.time()
            else:
                self._logger.warning("consolidate_session failed for %s: %s", k, exc)

        t.add_done_callback(_on_done)

    async def consolidate_now(self, key: str) -> bool:
        """Consolidate a session synchronously (blocking).

        Unlike consolidate_session() which is fire-and-forget, this awaits
        completion. Used by the CLI command.

        Returns ``False`` when the consolidation retry backoff refused the
        span — so the CLI can report the skip instead of a false success —
        and ``True`` for every other completion (including the nothing-to-do
        and sensitive-session skips, which were already reported as done).

        Safety: defense-in-depth — the consolidation retry backoff is also
        checked inside _consolidate(), and _run_skill_detection() re-checks
        the sensitive-session guard over its own window.
        """
        if self._log.unconsolidated_count(key) < 1:
            return True
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            self._logger.info("consolidate_now skipped for %s: sensitive session", key)
            return True
        outcome = await self._consolidate(key, include_history=True)
        return outcome is not _CONSOLIDATION_REFUSED

    def _refuse_restricted(self, key: str, mode: str, source: str) -> _ConsolidationRefusedSentinel:
        """Refuse *key* as a *mode* session learned from *source*.

        Two traces. The debug line names the source, so a skip is distinguishable
        from a pass and a header that predates the modifier's stamp is visible
        as such. The SEL record is the denial the dashboard route writes for the
        same target (``memory.consolidate`` / ``denied`` /
        ``restricted_target_session:<mode>``), with the target key appended and
        ``source="background"`` because no request carried it here -- so an
        audit reader sees a background sweep's refusal beside the route's.

        EVERY refusal writes its own record; nothing is windowed, counted or
        folded. An audit event that is sometimes not written is a gap the reader
        cannot see, whatever a later record claims to account for. The volume
        that a windowed record was meant to tame is solved where it arises
        instead: the key goes into ``_restricted_refused``, and the two
        automatic entry points skip a memoed key before scheduling anything --
        a restricted session is attempted once per process by the idle sweep,
        not once per 60 s tick. An explicit trigger ignores the memo, so a
        Summarize-now aimed at the session is attempted and audited every time.
        The memo is bounded by :data:`_RESTRICTED_REFUSAL_MEMO_MAX`: a refused
        key is (re)inserted newest-last, and past the bound the oldest key is
        evicted -- it becomes eligible for the sweep again, which costs that key
        one more refused attempt and one more SEL row before it is memoed anew.

        Only a COMMITTED restriction is memoed. A mode that stands provisionally
        (``privacy_mode.is_provisional``: a Telegram reservation whose steer has
        not landed, a commit whose write is in flight, a release in flight) is
        refused and audited like any other, but not remembered: a memo taken
        from a reservation the steer's failure then released would skip the
        now-persistent session on every automatic sweep for the life of the
        process. The next sweep re-asks, and memoes the mode once it is
        committed. The live channel key is resolved for the check because the
        registry is keyed by it, not by the stem a dashboard route posts.
        """
        self._logger.debug("consolidation skipped for %s: %s session (%s)", key, mode, source)
        live_key = _live_channel_key(key, self._sessions) or key
        if privacy_mode.is_provisional(mode, live_key):
            self._logger.debug(
                "consolidation refusal of %s not memoed: its %s mode is provisional", key, mode
            )
        else:
            self._restricted_refused.pop(key, None)
            self._restricted_refused[key] = mode
            while len(self._restricted_refused) > _RESTRICTED_REFUSAL_MEMO_MAX:
                evicted = next(iter(self._restricted_refused))
                del self._restricted_refused[evicted]
                self._logger.debug(
                    "restricted-refusal memo full (%d): %s evicted; the sweep may attempt it once more",
                    _RESTRICTED_REFUSAL_MEMO_MAX,
                    evicted,
                )
        try:
            _facade_sel().log_api_access(
                caller="history_consolidator",
                operation="memory.consolidate",
                outcome="denied",
                source="background",
                resources=f"restricted_target_session:{mode}:{key}",
            )
        except Exception:  # noqa: BLE001 - the refusal must hold even if audit fails
            self._logger.debug("SEL denial record failed for %s", key, exc_info=True)
        return _CONSOLIDATION_REFUSED

    def _write_gate(self, key: str) -> _WriteGate:
        """The write gate for a pass over *key* (see :class:`_WriteGate`)."""
        return _WriteGate(self, key)

    async def _consolidate(
        self, key: str, include_history: bool = True
    ) -> _ConsolidationRefusedSentinel | None:
        """Run LLM consolidation for a session.

        Returns :data:`_CONSOLIDATION_REFUSED` when the retry-eligibility gate
        refuses the span or its transcript changes during extraction; every
        other completion returns ``None``. A changed source remains pending
        rather than consuming the failure/abandon budget of a different span.
        """
        # Capture the gateway loop so the thread-offloaded _process_auto_skills
        # can schedule the async dedupe judge back onto it.
        self._event_loop = asyncio.get_running_loop()
        # Flipped once the prompt actually reaches the provider, which is what
        # makes a failure expensive: everything before that point is free to
        # retry, everything after costs a turn that produced nothing durable.
        billed = False
        total = 0
        generation_at_snapshot = 0
        # The span identity any failure charge is stamped with. Rebuilt from the
        # snapshot below; the zero value only ever reaches a charge if the snapshot
        # itself raised, and that path is not billed.
        attempted = AttemptedSpan(0, 0, 0)
        try:
            # Persistence global switch, checked here as well as in the automatic
            # entry points so the manual triggers (POST /api/memory/consolidate,
            # ``kirocrew consolidate``) are covered too. The REFUSED sentinel
            # gives the entry-point done-callbacks the right semantics for free:
            # no pass ran, so offsets must not advance and throttles must not be
            # set.
            #
            # INSIDE the try, so the finally below clears self._running. The
            # entry points add the key before scheduling this task and their
            # done-callbacks never discard it, so returning ahead of the try
            # would strand the key and refuse every later consolidation for that
            # session — reachable when the switch is flipped off in the gap
            # between create_task and the task's first line.
            if _persistence_disabled():
                self._logger.info(
                    "consolidation skipped for %s: memory.persistence_enabled is false", key
                )
                return _CONSOLIDATION_REFUSED

            # Memory-mode choke point. Every entry point -- the idle sweep,
            # maybe_consolidate, the expiry sweep, the dashboard trigger and
            # the CLI -- funnels through here, so a temporary or incognito
            # session is refused before THIS PASS reads its transcript (the
            # snapshot below) even when a caller carries no target-side check
            # of its own. The scope is the pass, not the entry point: the
            # fire-and-forget consolidate_session and the CLI's consolidate_now
            # read the transcript for their own pre-checks (the unconsolidated
            # count, the sensitive-session scan) before scheduling this, so a
            # restricted session's transcript IS read there -- and nothing of
            # it is written anywhere. The durable records are read by
            # resolve_consolidation_target, the ONE resolver the dashboard
            # route asks as well, so the two cannot disagree on what refuses; a
            # mode already known (execution record, session-map flag) refuses
            # without the resolver opening the transcript, and only an unknown
            # one reads the header. Each refusal goes through
            # _refuse_restricted: a debug trace naming the source, and the same
            # SEL denial the dashboard route records. A map-only mode is copied
            # into the transcript header by the startup path, off the loop and
            # without a consolidation ever opening the transcript
            # (SessionMap.stamp_privacy_headers); the map row itself stays.
            target = await resolve_consolidation_target(key, log=self._log, sessions=self._sessions)
            execution, metadata, restricted = target
            if restricted is not None:
                return self._refuse_restricted(key, restricted.mode, restricted.source)
            # From here on the mode is re-read by the write path itself: every
            # durable write below is a verb of this gate, and every batch of
            # them is dispatched through it (_WriteGate). A tightening landing
            # anywhere in the minutes this pass takes raises _ModeTightened
            # out of the write it stopped; the except clause below turns it
            # into the same refusal as the verdict just above.
            gate = self._write_gate(key)
            # Atomically snapshot the unconsolidated tail, the total message
            # count (the absolute offset handed to mark_consolidated below), and
            # the rotation generation under ONE lock hold. Reading them as
            # separate calls let an append trigger a rotation between them,
            # pairing a pre-rotation offset with a post-rotation generation —
            # mark_consolidated would then see matching generations and apply
            # the stale offset (retained-count fallback misses it too), silently
            # dropping messages from extraction. Offloaded to a worker thread:
            # _consolidate runs on the gateway event loop and _locked/file IO is
            # blocking (same rationale as the mark_consolidated offload below).
            (
                unconsolidated,
                total,
                generation_at_snapshot,
            ) = await asyncio.to_thread(self._log.snapshot_for_consolidation, key)
            # Transcript caches may share nested message dictionaries with an
            # editor. Freeze the submitted evidence before awaiting the model.
            unconsolidated = copy.deepcopy(unconsolidated)
            if not unconsolidated:
                return None
            # Retry-eligibility choke point: every entry point funnels through
            # this function, so a span inside its durable backoff is refused
            # here — before anything that can bill a provider turn — even if a
            # caller carries no pre-check of its own (a future entry point, or
            # a pre-check that raced the backoff being recorded). Callers keep
            # their cheaper pre-checks as scheduling short-circuits and UX (the
            # idle sweep's per-tick skip, maybe_consolidate's per-turn skip,
            # the dashboard trigger's 429); this gate is the enforcement that
            # holds when a new entry point forgets one. The count comes
            # from the atomic snapshot above — the same consistent read the
            # rest of this function uses — and retry_eligible costs one
            # metadata-line read, so no second transcript read lands on the
            # event loop. The refusal returns a sentinel rather than raising:
            # the finally block still releases self._running and the callers'
            # done-callbacks run normally (so the key is never stranded), while
            # the sentinel lets those callbacks tell a refusal from a completed
            # pass and leave their bookkeeping untouched.
            if not self.retry_eligible(key, message_count=total):
                self._logger.info("_consolidate refused for %s: consolidation retry backoff", key)
                return _CONSOLIDATION_REFUSED
            # Freeze the whole span identity from that one snapshot. The offset is
            # derived rather than returned because the snapshot slices at it
            # (``messages[offset:]``), so the subtraction is exact and comes from
            # the same lock hold — no second read that a concurrent rotation could
            # land between. A failure charge stamped with these values describes
            # what the turn attempted even if the file changed underneath it.
            attempted = AttemptedSpan(
                total=total,
                generation=generation_at_snapshot,
                offset=total - len(unconsolidated),
            )

            # Resolve the owning execution once. V2 learning requires its exact
            # database; only V1 retains Markdown and JSONL learning handles.
            from kiro_crew.context import store_of_session
            from kiro_crew.memory_stores import memory_store_version

            # Resolve through the same strict metadata reader as interactive
            # turns before any consolidation provider or memory write starts.
            def _resolve_memory_identity() -> tuple[str, bool]:
                store_name = (
                    execution.store.legacy_name
                    if execution is not None
                    else store_of_session(self._log, key)
                )
                return store_name, bool(store_name and memory_store_version(store_name) == 2)

            store_name, member_memory = await asyncio.to_thread(_resolve_memory_identity)
            # V2 anchors are owner-managed essentials. Extract proposed facts
            # through the revision-aware structured path; never let a legacy
            # whole-file rewrite remove their rules or age out project guides.
            allow_markdown_updates = not self._migrated and not member_memory
            meta = metadata
            facets = _session_facets(meta, key)
            ws_name = meta.get("workspace")
            lessons_store = self._lesson_store
            if store_name:
                from kiro_crew.context import ContextBuilder

                vector_store = await ContextBuilder.ensure_store(store_name)
                memory = await asyncio.to_thread(
                    ContextBuilder.get_memory_for, memory_store=store_name
                )
                lessons_store = (
                    None
                    if member_memory
                    else await asyncio.to_thread(
                        ContextBuilder.get_lessons_for, memory_store=store_name
                    )
                )
                # May be None when the store could not be stood up; the writes
                # below then skip the vector tier rather than falling back to the
                # global store. Losing a semantic row is recoverable, writing it
                # into another crew's memory is not.
            elif ws_name:
                from kiro_crew.context import ContextBuilder

                memory = await asyncio.to_thread(ContextBuilder.get_memory_for, ws_name)
                vector_store = self._vector_store
            else:
                memory = self._memory
                vector_store = self._vector_store

            source_id = ""
            if member_memory:
                if vector_store is None:
                    raise RuntimeError("Member memory database is unavailable")
                source_id = hashlib.sha256(
                    json.dumps(
                        [
                            key,
                            generation_at_snapshot,
                            attempted.offset,
                            include_history,
                            None if include_history else total,
                        ]
                    ).encode("utf-8")
                ).hexdigest()
                committed = await asyncio.to_thread(vector_store.consolidation_receipt, source_id)
                if committed is not None:
                    from kiro_crew.vector_memory import consolidation_source_digest

                    count = committed["source_count"]
                    if (
                        len(unconsolidated) < count
                        or await asyncio.to_thread(
                            consolidation_source_digest, unconsolidated[:count]
                        )
                        != committed["source_digest"]
                    ):
                        raise ValueError(
                            "Committed consolidation source changed before acknowledgement"
                        )
                    if include_history:
                        # An offset advance is a durable write: through the gate,
                        # like the one at the end of a full pass.
                        await gate.run_in_thread(
                            "the offset advance",
                            gate.mark_consolidated,
                            self._log,
                            key,
                            committed["source_total"],
                            generation_at_snapshot,
                        )
                    return None

            conversation = "\n".join(_fmt_message(m) for m in unconsolidated)

            current_prefs, current_projects = await asyncio.to_thread(
                lambda: (memory.read_preferences(), memory.read_projects())
            )

            # Build prompt keys dynamically based on consolidation type
            keys: list[str] = []
            if include_history:
                keys.append(
                    '"history_entry": A concise paragraph (2-5 sentences) summarizing '
                    "what happened. Use local time [YYYY-MM-DD HH:MM]. Focus on "
                    "decisions, outcomes, facts. Use user's real name if known."
                )

            # Structured memory extraction (when vector store is available).
            # Reads the RESOLVED store, never ``self._vector_store``: the rows
            # fetched here go into the prompt, and the prompt instructs the model
            # to update and delete them. Fetching globally would show crew B the
            # operator's semantic table and let its consolidation turn delete it.
            has_vector = vector_store is not None
            if has_vector and vector_store is not None:
                private_policy = getattr(vector_store, "algorithm_version", "v1") == "v2"
                # Offload: the fetch serializes on the store's _db_lock,
                # and this coroutine runs on the gateway event loop — a worker
                # holding the lock (backfill's FAISS rebuild, reconcile's bulk
                # UPDATEs) would otherwise block the whole loop here.
                current_semantic = await asyncio.to_thread(vector_store.get_all_semantic)

                def _prompt_value(e: dict) -> object:
                    # A lesson row stores a mapping; the consolidation model
                    # should read the rule prose, not a JSON envelope whose
                    # field names dilute the instruction it is weighing.
                    if str(e.get("key", "")).startswith("lesson."):
                        from kiro_crew.vector_memory import _lesson_display_text

                        try:
                            decoded = json.loads(e["value_json"])
                        except Exception:
                            return e["value_json"]
                        return _lesson_display_text(decoded) or e["value_json"]
                    return e["value_json"]

                if private_policy:
                    current_semantic = await run_in_embed_pool(
                        vector_store.with_record_metadata, current_semantic
                    )
                semantic_json = (
                    json.dumps(
                        [
                            {
                                "key": e["key"],
                                "value_json": _prompt_value(e),
                                "confidence": e["confidence"],
                                **(
                                    {
                                        "record_revision": e.get("record_revision", 0),
                                        "metadata": e.get("record_metadata", {}),
                                    }
                                    if private_policy
                                    else {}
                                ),
                            }
                            for e in current_semantic
                        ],
                        indent=1,
                    )
                    if current_semantic
                    else "[]"
                )
                semantic_fields = (
                    '"delete": false, "metadata": {"category": "contact", "subject": "user", '
                    '"predicate": "work_email", "scope": "", "source_ref": "brief evidence", '
                    '"valid_from": "ISO date only if stated", "valid_until": "ISO date only if stated"}}. '
                    "Metadata is optional: omit unknown dates/identity; do not guess them. "
                    "For an explicit user replacement, add correction_quote containing an exact user quote "
                    "with both old and new values and replacement language. Never fabricate a quote. "
                    "Keep one atomic fact per key. The subject/predicate/scope tuple is exact identity, "
                    "so retain it for updates and do not reuse it for a different person or project. "
                    if private_policy
                    else '"delete": false}. '
                )
                deletion_policy = (
                    'To propose removal of a stale/invalidated key, set "delete": true. '
                    "Changes to existing values and deletions require owner review; confidence does not authorize overwrites. "
                    if private_policy
                    else 'To DELETE a stale/invalidated key, set "delete": true (e.g. pet died → delete '
                    "user.pet.name; project cancelled → delete project.x.status). "
                )
                keys.append(
                    '"semantic": Array of structured facts to remember long-term. '
                    'Each: {"key": "<dotted.key>", "value": <json_value>, "confidence": 0.0-1.0, '
                    + semantic_fields
                    + "Rules: keys must start with pref.*, project.*, or user.* "
                    "(e.g. pref.color, user.favorite_language, project.name). "
                    "confidence 1.0 = user stated, 0.8-0.9 = clearly implied, <0.8 = uncertain (rejected). "
                    "value must be a JSON primitive (string, number, boolean) — NOT objects or arrays. "
                    "IMPORTANT: Check existing semantic memory above. If a key already covers "
                    "the same topic, UPDATE that key instead of creating a new one. "
                    "Do NOT create near-duplicate keys (e.g. project.x.approach AND project.x.refined). "
                    + deletion_policy
                    + f"Max {_MAX_SEMANTIC_PER_CONSOLIDATION} items."
                )
                keys.append(
                    '"episodic": Array of conversation fragments worth remembering. '
                    'Each: {"text": "...", "tags": ["tag1"], "importance": 0.0-1.0}. '
                    "Rules: text 10-2000 chars, factual. importance 0.9+ = critical, "
                    "0.7-0.9 = useful, 0.5-0.7 = minor. Skip greetings/small talk. "
                    f"Max {_MAX_EPISODIC_PER_CONSOLIDATION} items. "
                    "IMPORTANT: Do NOT write simple key-value facts here that belong in semantic "
                    "(e.g. 'Favorite color: blue'). Episodic is for events, decisions, and context "
                    "— not for duplicating semantic facts."
                )

            # Markdown memory (backward compat when not migrated)
            if allow_markdown_updates:
                keys.append(
                    '"preferences_update": The COMPLETE updated preferences file, '
                    "included ONLY if the file needs changes. Merge duplicates, keep "
                    "only newest if contradicted, remove stale one-off observations. "
                    "Keep '# User Preferences' header. If nothing changed, OMIT this "
                    "key entirely — never echo the file back and never answer with a "
                    "placeholder word like 'unchanged': the value overwrites the file, "
                    "so when present it must be the full file body."
                )
                keys.append(
                    '"projects_update": The COMPLETE updated projects file, included '
                    "ONLY if the file needs changes. Only active projects, remove "
                    "stale entries, update facts. Keep '# Active Projects' header. "
                    "If nothing changed, OMIT this key entirely — never echo the file "
                    "back and never answer with a placeholder word like 'unchanged': "
                    "the value overwrites the file, so when present it must be the "
                    "full file body."
                )

            if include_history:
                keys.append(
                    '"lessons": Array of corrections the user taught '
                    '(e.g. "no, do X", "always Y", "never Z"). '
                    'Each: {"rule": "...", "negative": "...", "category": "tool|preference|knowledge", '
                    '"repo_scope": "...", "applies": "always|on_topic"}. '
                    # The same instruction learn_add's schema carries, from one
                    # constant, so both writers ask the model the same question.
                    f'"applies": {LESSON_APPLIES_INSTRUCTION} '
                    '"repo_scope" is OPTIONAL: include it ONLY when the correction is '
                    "genuinely specific to one codebase worked on in the chat. Give a "
                    "RELATIVE directory path inside that repository that is distinctive "
                    'of it (e.g. "src/kiro_crew") -- never an absolute path, no leading '
                    'slash or drive letter, no "." or ".." segments; a malformed scope '
                    "drops the whole lesson. OMIT it when unsure and for anything that "
                    "applies everywhere -- a scoped lesson is withheld outside its "
                    "repository. "
                    "Empty [] if no corrections. Skip general preferences. "
                    f"Max {_MAX_LESSONS_PER_CONSOLIDATION} items. "
                    "IMPORTANT: Only extract lessons that the user did NOT explicitly ask "
                    "to remember (those are already saved via learn_add). Only extract "
                    "implicit corrections the user made without saying 'remember'."
                )

            # ── Auto skill detection ──
            # Skill detection runs as its OWN pass (below, after the memory
            # writes) over a wider last-N window of the full session — not the
            # incremental history tail — so a reusable procedure that spans the
            # whole session is judged as a unit. It is therefore intentionally
            # absent from this consolidation prompt's keys.

            numbered = "\n\n".join(f"{i + 1}. {k}" for i, k in enumerate(keys))
            prompt_parts = [
                "You are a memory consolidation agent. Process this conversation "
                f"and return a JSON object with these keys:\n\n{numbered}",
            ]
            if has_vector:
                prompt_parts.append(f"\n\n## Current Semantic Memory\n{semantic_json}")
            if allow_markdown_updates or member_memory:
                if member_memory:
                    prompt_parts.append(
                        "\n\nThe following member anchors are read-only. Do not return "
                        "preferences_update or projects_update; preserve the owner's core "
                        "guides. Extract new facts or proposed corrections into semantic "
                        "memory with their evidence instead."
                    )
                prompt_parts.append(f"\n\n## Current Preferences\n{current_prefs or '(empty)'}")
                prompt_parts.append(f"\n\n## Current Projects\n{current_projects or '(empty)'}")
            prompt_parts.append(f"\n\n## Conversation to Process\n{conversation}")
            prompt_parts.append("\n\nRespond with ONLY valid JSON, no markdown fences.")
            prompt = "".join(prompt_parts)

            try:
                result = (
                    await self._call_llm(prompt, memory_store=store_name, session_key=key)
                    if member_memory
                    else await self._call_llm(prompt, session_key=key)
                )
            except _ConsolidationNotDispatched as exc:
                # Nothing was sent, so nothing was billed. Charging this to the
                # attempt cap would let a handful of environment failures abandon
                # the span — writing the durable marker over messages no LLM has
                # ever read, which is the exact false-abandonment this accounting
                # exists to prevent. Arm the backoff only, so a broken host retries
                # on a widening interval instead of on every 60s tick.
                if include_history:
                    await self._note_environment_failure(key, str(exc))
                return None
            billed = True
            if not result:
                # The turn reached the provider and produced nothing usable, so it
                # was spent while the marker below stays unwritten. Returning
                # silently would look like success to the done-callbacks, setting
                # the in-memory throttle while the durable count still says
                # unconsolidated: the span re-bills a full turn every idle window,
                # and immediately after every restart. Charge the attempt.
                if include_history:
                    await self._note_failed_attempt(key, attempted, "empty LLM result")
                return None

            # A pending model result cannot authorize writes from a deleted or
            # edited transcript, or outrank a newer user turn. Appended assistant
            # replies may remain unconsolidated without invalidating the original
            # span. Recheck at the write boundary, before history or fact changes.
            latest, latest_total, latest_generation = await asyncio.to_thread(
                self._log.snapshot_for_consolidation, key
            )
            if (
                latest_generation != generation_at_snapshot
                or latest_total < total
                or latest[: len(unconsolidated)] != unconsolidated
                or any(row.get("role") == "user" for row in latest[len(unconsolidated) :])
            ):
                self._logger.info(
                    "Discarding consolidation result for %s: conversation changed during extraction",
                    key,
                )
                return _CONSOLIDATION_REFUSED

            # Every durable write from here is a _WriteGate verb, dispatched
            # through the gate: the full resolution (header included) at each
            # dispatch, the in-memory records before each mutation inside it.
            # No await sits between a check and the write it guards, and no
            # write batch is dispatched any other way (pinned over the module's
            # syntax tree by test_consolidation_write_gate.py). A modifier
            # landing while this pass runs -- during the model call, inside an
            # earlier write of this pass, between two mutations of one batch,
            # inside the member transaction -- stops the next write.
            if member_memory:
                if vector_store is None:
                    raise RuntimeError("Member memory database is unavailable")
                await gate.run(
                    "the member transaction",
                    gate.apply_consolidation,
                    vector_store,
                    source_id=source_id,
                    session_key=key,
                    source_total=total,
                    result=result,
                    snapshot={row["key"]: row for row in current_semantic},
                    messages=unconsolidated,
                    facets=facets,
                )

            if not member_memory and (entry := result.get("history_entry")):
                # Offloaded to a worker thread: append_history takes a blocking
                # advisory file lock (cross-process) and does synchronous file
                # IO, and _consolidate runs on the event loop thread (fired via
                # asyncio.create_task). Running it inline would let cross-process
                # lock contention stall the whole gateway loop.
                await gate.run("the history write", gate.append_history, memory, entry)
                self._logger.info("Consolidated %d messages for %s", len(unconsolidated), key)

            # Structured memory writes (Phase 2/3). Offloaded to a worker thread:
            # _write_structured_memory embeds each item via a blocking urllib call
            # to the in-process embedder, and _consolidate runs on the event loop thread (fired via
            # asyncio.create_task). Running it inline stalls the whole gateway loop
            # if the embedding endpoint is slow/hung (heartbeats, Slack, dashboard).
            if vector_store and not member_memory:
                await gate.run(
                    "the structured-memory write",
                    self._write_structured_memory,
                    result,
                    key,
                    vector_store,
                    facets=facets,
                    snapshot={row["key"]: row for row in current_semantic},
                    messages=unconsolidated,
                    gate=gate,
                )

            # Legacy V1 Markdown writes (skip if migrated or private V2). Each value
            # replaces the whole file, so a non-file answer (e.g. the literal
            # word "unchanged") must be discarded, not written: once written it
            # re-enters the next prompt as the file's current content and primes
            # every later pass to repeat it (see _is_plausible_memory_file).
            if allow_markdown_updates:
                if prefs := result.get("preferences_update"):
                    if not _is_plausible_memory_file(prefs, "# User Preferences"):
                        self._logger.warning(
                            "Discarding implausible preferences_update from "
                            "consolidation (missing '# User Preferences' header "
                            "or placeholder body; %d chars)",
                            len(prefs),
                        )
                    elif prefs.strip() != current_prefs.strip():
                        # Offloaded like append_history above (blocking file
                        # I/O on the event loop thread). expected_baseline is
                        # the compare-and-swap guard: this whole-file result
                        # was merged from current_prefs, read BEFORE the
                        # minutes-long LLM call — if a dashboard Save landed
                        # in that window, writing would silently revert it,
                        # so the store skips the stale write instead.
                        wrote = await gate.run(
                            "the preferences write",
                            gate.write_preferences,
                            memory,
                            prefs,
                            expected_baseline=current_prefs,
                        )
                        if not wrote:
                            self._logger.info(
                                "Consolidated preferences for %s discarded: file "
                                "changed during consolidation",
                                key,
                            )

                if projects := result.get("projects_update"):
                    if not _is_plausible_memory_file(projects, "# Active Projects"):
                        self._logger.warning(
                            "Discarding implausible projects_update from "
                            "consolidation (missing '# Active Projects' header "
                            "or placeholder body; %d chars)",
                            len(projects),
                        )
                    elif projects.strip() != current_projects.strip():
                        wrote = await gate.run(
                            "the projects write",
                            gate.write_projects,
                            memory,
                            projects,
                            expected_baseline=current_projects,
                        )
                        if not wrote:
                            self._logger.info(
                                "Consolidated projects for %s discarded: file "
                                "changed during consolidation",
                                key,
                            )

            # Lesson extraction: _save_lessons calls write_lesson which embeds
            # each rule (+ up to 5 lazy backfills) via blocking urllib to Ollama.
            # Same rationale as _write_structured_memory above — must offload.
            if (
                not member_memory
                and (lessons_store or vector_store)
                and (raw_lessons := result.get("lessons"))
            ):
                await gate.run(
                    "the lesson writes",
                    self._save_lessons,
                    raw_lessons,
                    vector_store,
                    lessons_store,
                    facets=facets,
                    gate=gate,
                )

            # Auto skill detection — a SEPARATE LLM pass over the full-session
            # window (see _run_skill_detection), not the incremental tail. Runs
            # only on history consolidation, guarded by flag + loader; failures
            # are logged, never fatal -- a refusal at its write is not a
            # failure and ends the pass like any other.
            # Auto-skills are shared install-wide. Private member experience
            # must not be published or contribute to another member's skills.
            if (
                not member_memory
                and include_history
                and self._auto_skills_enabled
                and self._skills_loader is not None
            ):
                # The pass reads the FULL transcript and hands it to the model
                # for a generation turn. Not a durable write, but a transcript
                # that just turned private must not be disclosed to the model
                # either -- so the full resolution runs ahead of the read; the
                # skill write is then dispatched through the gate inside
                # _run_skill_detection, after the generation's await.
                await gate.boundary("the skill pass")
                try:
                    await self._run_skill_detection(key, gate)
                except _ModeTightened:
                    raise
                except Exception:
                    self._logger.warning("Auto-skill detection failed for %s", key, exc_info=True)

            # Autonomous lifecycle: age-based archival must run even when this
            # pass created/approved no skill, otherwise skills never age out on
            # their own (create/approve were the only triggers). Consolidation is
            # the existing idle/periodic path; throttle to at most once/hour
            # across all sessions so frequent consolidations don't rescan the set.
            if (
                not member_memory
                and self._skills_loader is not None
                and (_time.time() - self._last_lifecycle) > 3600
            ):
                self._last_lifecycle = _time.time()
                try:
                    await asyncio.to_thread(
                        self._skills_loader.run_skill_lifecycle,
                        max_auto_skills=self._max_auto_skills,
                        stale_after_days=self._stale_after_days,
                        archive_after_days=self._archive_after_days,
                    )
                except Exception:
                    self._logger.debug("Periodic skill lifecycle pass failed", exc_info=True)

            # Only advance the consolidated offset for history consolidation.
            # Prefs-only consolidation uses a separate in-memory offset.
            # mark_consolidated does a synchronous, fsync-backed rewrite of the
            # whole transcript (up to a couple of MB) behind the per-file lock.
            # _consolidate runs on the gateway event loop (fired via
            # asyncio.create_task), so offload the blocking rewrite to a worker
            # thread — otherwise a slow filesystem freezes the loop (heartbeats,
            # Slack, dashboard). Same rationale as the offloads above.
            if include_history:
                # The offset advance is a durable write too: the window it marks
                # consolidated is gone from every later pass.
                await gate.run_in_thread(
                    "the offset advance",
                    gate.mark_consolidated,
                    self._log,
                    key,
                    total,
                    generation_at_snapshot,
                )

        except _ModeTightened as tightened:
            # The write gate stopped a write: the pass ends as a REFUSAL --
            # the SEL denial and memo entry a pre-snapshot verdict produces,
            # no attempt charged (nothing about the span failed; its thread
            # went private) -- and whatever the gate had already let through
            # stands, by the existing design line. Ahead of the failure clause
            # below, which would charge the retry budget and re-raise.
            self._logger.info(
                "consolidation for %s aborted before %s: mode tightened to %s (%s) during the pass",
                key,
                tightened.site,
                tightened.mode,
                tightened.source,
            )
            return self._refuse_restricted(key, tightened.mode, tightened.source)
        except Exception:
            self._logger.exception("Consolidation failed for %s", key)
            # Anything raised between the LLM call and mark_consolidated (memory
            # writes, lesson writes, the marker write itself) re-raises, so the
            # idle sweep's done-callback never sets its throttle and all of its
            # skip conditions are false again on the next 60s tick. Charging the
            # attempt here is what converts that tight loop into backoff.
            if billed and include_history:
                await self._note_failed_attempt(key, attempted, "exception after the LLM call")
            elif include_history and attempted.total > 0:
                # Setup has a durable retry budget too, but consumes no model
                # attempt and must never abandon a span the model did not read.
                await self._note_environment_failure(key, "memory setup failed before the LLM call")
            raise
        finally:
            self._running.discard(key)
        return None

    async def _run_skill_detection(self, key: str, gate: _WriteGate) -> None:
        """Detect a reusable skill from the FULL session (bounded window).

        The skill is written through *gate* (``_process_auto_skills``,
        dispatched by ``gate.run_in_thread`` after the generation await): a
        thread whose mode tightened during the generation raises
        :class:`_ModeTightened` out of that dispatch and the caller's pass ends
        on it.

        Unlike history/semantic/lesson extraction — which correctly runs on the
        incremental unconsolidated tail — skill detection judges the last
        ``_SKILL_DETECTION_WINDOW`` messages of the WHOLE session, decoupled
        from the consolidation offset. A reusable procedure usually spans a
        session rather than the slice since the last consolidation, so a
        tail-only view systematically misses skills in any session consolidated
        more than once. The skill need only be demonstrated by PART of the
        window; the pass does not have to cover the whole session.

        Runs as its own LLM call so the consolidation prompt stays tail-scoped
        (widening THAT prompt would re-summarize already-consolidated messages
        into duplicate history/semantic entries). A per-session
        (rotation_generation, count) guard skips re-running when nothing new has
        been appended since the last pass, yet still forces a fresh pass after a
        transcript rotation (which swaps the window's content); genuine repeats
        are still caught by the dedupe verdict in ``_process_auto_skills``.

        The prompt gates on RECURRENCE, not effort. A session can be long,
        difficult, and rich in tool calls while still being one-off — a single
        bug's fix, a one-time audit of one component, a probe answering a
        question that is now answered — and the tool-call floor
        (``auto_min_tool_calls``) cannot tell those apart from a repeatable
        method. So the prompt makes the model name the future session and the
        DIFFERENT target that would reuse the procedure, and return null when
        the only honest answer reuses this session's own artifact. It also
        prefers null under uncertainty: an unreusable candidate is not free,
        because it spends the human's review attention on every later proposal.
        """
        if self._skills_loader is None:
            return None
        all_messages = await asyncio.to_thread(self._log._read_messages, key)
        if not all_messages:
            return None
        # Key the guard on (rotation generation, message count), NOT count
        # alone. The transcript rotates at _SESSION_MAX_BYTES / _SESSION_KEEP_LINES:
        # a rotation bumps rotation_generation and replaces the window with fresh
        # messages even when the resulting count matches a prior value, so a
        # count-only guard would wrongly treat a rotated session as unchanged and
        # never propose its skill. Comparing the pair re-detects after any
        # rotation while still skipping a genuinely unchanged session.
        generation = await asyncio.to_thread(
            lambda: int(self._log._read_metadata(key).get("rotation_generation", 0) or 0)
        )
        marker = (generation, len(all_messages))
        if self._last_skillgen_marker.get(key) == marker:
            return None
        window = all_messages[-_SKILL_DETECTION_WINDOW:]
        if _count_tool_call_messages(window) < self._auto_min_tool_calls:
            return None
        if _session_touched_sensitive(window):
            return None

        scripts_field = ""
        if self._generate_scripts:
            scripts_field = (
                ', "scripts": (optional array, part of THIS new_skill '
                "object) ONLY when the procedure includes a "
                "DETERMINISTIC, always-identical step sequence worth "
                "running verbatim (a fixed command chain, a set API "
                "sequence, a predictable file transform). Each item: "
                '{"filename": "<name>.py", "language": "python", '
                '"content": "<self-contained Python, no network to '
                "unknown hosts, no credential access, no destructive "
                'commands, <=4KB>"}. Python ONLY (must run on Windows). '
                "Omit for judgment-based / context-dependent procedures. "
                "Scripts always require human approval"
            )
        skill_keys = [
            '"new_skill": Object or null. Return an object ONLY if this '
            "session demonstrated a procedure that will RECUR — one a future "
            "session, working on a DIFFERENT target, would run again "
            "substantially unchanged (e.g. a repeatable debugging method for a "
            "class of error, a fixed command/API sequence, a verification "
            "technique). The procedure may be demonstrated by only PART of the "
            "excerpt below — you do NOT need to cover the whole session. "
            "Shape: "
            '{"slug": "<kebab-case-4-to-60-chars>", '
            '"description": "<=150 chars, starts with verb>", '
            '"triggers": "<3-8 comma-separated keywords/phrases>", '
            '"procedure_md": "<concise markdown body with '
            "## When to use / ## Steps / ## Gotchas sections, "
            '<=8000 chars>"' + scripts_field + "}. "
            "## The recurrence test (apply BEFORE returning an object)\n"
            "Name the future session that would load this skill and the "
            "DIFFERENT target it would run against. If the only honest answer "
            "reuses this session's specific artifact — this bug, this file, "
            "this component, this one question — the procedure does not recur "
            "and you MUST return null. Effort is not evidence of recurrence: a "
            "long, many-step, genuinely difficult session is still one-off if "
            "its steps were chosen for one target.\n"
            "Return null for: a task done once and now finished (a specific "
            "bug's fix, a one-time audit/trace of one component, a migration, "
            "a probe run to answer a question that is now answered); a design "
            "or planning discussion; a narrative of what happened in this "
            "session; a procedure whose steps only make sense against the "
            "exact artifact at hand; a trivial or single-shot answer; a "
            "one-off failure with no reusable takeaway; anything touching "
            "sensitive paths. Prefer null when uncertain — an unreusable "
            "candidate costs the user review effort on every future proposal, "
            "so silence is cheaper than a plausible-looking one-off. "
            "Do NOT include absolute paths, credentials, tokens, or user PII "
            "in the procedure body."
        ]
        if self._auto_refine_enabled:
            skill_keys.append(
                '"refined_skill": Object or null. If an existing '
                '"auto/..." skill was loaded during this session AND '
                "the agent found a better procedure than the one "
                "documented in that skill, return: "
                '{"name": "auto/<existing-slug>", '
                '"description": "<updated>", "triggers": "<updated>", '
                '"procedure_md": "<refined markdown>"}. Return null '
                "if nothing was refined. Do not fabricate refinements."
            )
        numbered = "\n\n".join(f"{i + 1}. {k}" for i, k in enumerate(skill_keys))
        conversation = "\n".join(_fmt_message(m) for m in window)
        prompt = (
            "You are a skill-extraction agent. Review this session excerpt and "
            "return a JSON object with these keys:\n\n"
            + numbered
            + "\n\n## Session excerpt\n"
            + conversation
            + "\n\nRespond with ONLY valid JSON, no markdown fences."
        )
        try:
            result = await self._call_llm(prompt)
        except _ConsolidationNotDispatched:
            # Skill detection is best-effort and owns no retry accounting, so an
            # unreachable provider is simply no detection this pass. The marker
            # below is still recorded, matching the existing failed-turn path.
            result = None
        if result:
            # Log the verdict, not just the proposals. The prompt's default is
            # null, so silence is the common outcome, and the staging log in
            # ``_process_auto_skills`` only fires when a candidate is produced --
            # which would leave the queue showing the false-POSITIVE rate while
            # the false-negative rate had no signal at all.
            self._logger.debug(
                "Skill detection verdict for %s: %s",
                key,
                "candidate proposed" if result.get("new_skill") else "no recurring procedure",
            )
            # The generation above is an await of its own, minutes long: a
            # modifier landing during it must not have the skill -- distilled
            # from this thread's transcript -- written into the shared skill set.
            # Dispatched through the gate: the full resolution here, and each
            # skill write inside admits again on the worker. _event_loop was
            # captured by our caller (_consolidate) so the thread-offloaded
            # dedupe judge can marshal back onto the gateway loop.
            await gate.run_in_thread(
                "the skill write", self._process_auto_skills, result, key, gate
            )
        # Record the (generation, count) marker only NOW, once the window's
        # verdict has been acted on -- nothing to write, or the write in -- so an
        # unchanged session is not re-evaluated on every later consolidation
        # while a rotation still forces a fresh pass. Not ahead of the write: a
        # consume-once marker is consumed by the committed mutation, never by the
        # attempt. A refusal at the write raises out of the dispatch above and
        # past this line -- a refusal is not a commit -- because the mode that
        # refused it may stand only provisionally (a reservation whose steer then
        # fails is released), after which the next pass runs over an UNCHANGED
        # transcript: a marker recorded ahead of the write would skip detection
        # at the pair check above and lose the candidate for the process's life.
        # ``TestAMarkerIsConsumedByTheCommittedPass`` pins the order.
        self._last_skillgen_marker[key] = marker

    def _gated_lesson_scope(self, item: dict) -> tuple[str | None, bool]:
        """The lesson's ``repo_scope`` to forward, plus whether to DROP the lesson.

        Returns ``(scope, False)`` for no scope (absent, ``None``, or a
        whitespace-only string -- the unchanged global path) or an admissible
        one, and ``(None, True)`` (with the reason logged) when a PRESENT scope
        is malformed. A malformed scope refuses the WHOLE lesson rather than
        stripping the scope: storing it globally would be the fail-open a
        scoped lesson must never take (``write_lesson`` draws the same line for
        inadmissible strings). The value is untrusted model output, so this
        seam also refuses the shapes the stores' own guards cannot see -- a
        non-string would slip past ``write_lesson``'s string-only admissibility
        check and be canonicalised to a GLOBAL write, and ``LessonStore.save``
        never checks admissibility at all.
        """
        raw = item.get("repo_scope")
        refusal: str | None = None
        if raw is None:
            return None, False
        elif not isinstance(raw, str):
            refusal = "scope_not_a_string"
        elif not raw.strip():
            return None, False
        elif not scope_is_admissible(raw):
            refusal = "scope_inadmissible"
        if refusal:
            # Only the closed-set reason code is interpolated, never the
            # untrusted value itself.
            self._logger.warning(
                "Dropping consolidation lesson with malformed repo_scope (%s)",
                refusal,
            )
            return None, True
        return raw, False

    def _lesson_tier(self, item: dict) -> str | None:
        """The lesson's authored ``applies`` tier to forward, or ``None`` for unstated.

        One policy for every consolidation write path, so the member-store path
        in ``VectorMemoryStore.apply_consolidation`` and this one cannot drift:
        see ``extracted_lesson_applies``.
        """
        return extracted_lesson_applies(item.get("applies"), self._logger)

    def _save_lessons(
        self,
        raw: object,
        vector_store: "VectorMemoryStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
        lesson_store: "LessonStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
        *,
        facets: "MemoryFacets | None" = None,
        gate: _WriteGate,
    ) -> None:
        """Save extracted lessons from consolidation result.

        Both stores are passed in rather than read off ``self`` so a crew's
        corrections land in its own silo. Omitting them keeps the historical
        behaviour (the global handles), which is what the workspace and default
        arms of the caller want. Every lesson is written through *gate*, the
        pass's :class:`_WriteGate`: the mode is re-read before each one.

        An explicitly passed ``None`` is NOT omission (:data:`_INHERIT_GLOBAL`):
        it means the silo has no store of that tier, so the tier is skipped. The
        caller reaches this method whenever EITHER store is live, so a silo whose
        vector store could not be stood up arrives here with
        ``vector_store=None`` and a real ``lesson_store`` — and inheriting the
        global vector store there would take the dedup-aware branch below and
        file the crew's corrections into the operator's own table, never touching
        the silo's ``lessons.jsonl`` at all.
        """
        if isinstance(vector_store, _InheritGlobal):
            vector_store = self._vector_store
        if isinstance(lesson_store, _InheritGlobal):
            lesson_store = self._lesson_store
        if not isinstance(raw, list):
            return

        # Cap like semantic/episodic: each write_lesson can perform up to 6
        # blocking embeds, so an uncapped LLM lessons array would occupy a
        # worker thread for minutes.
        max_lessons = _MAX_LESSONS_PER_CONSOLIDATION
        if len(raw) > max_lessons:
            self._logger.warning(
                "Consolidation returned %d lessons; capping to %d",
                len(raw),
                max_lessons,
            )
            raw = raw[:max_lessons]

        # Prefer vector store (dedup-aware) over JSONL
        if vector_store:
            count = 0
            for item in raw:
                if isinstance(item, dict) and item.get("rule"):
                    scope, drop = self._gated_lesson_scope(item)
                    if drop:
                        continue
                    ok = gate.write_lesson(
                        vector_store,
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        source="consolidation",
                        # Gated by _gated_lesson_scope above; write_lesson
                        # canonicalises and re-checks admissibility itself.
                        repo_scope=scope,
                        # Already normalized by _lesson_tier, so write_lesson's
                        # own raising check cannot fire on it.
                        applies=self._lesson_tier(item),
                        facets=facets,
                    )
                    if ok:
                        count += 1
            if count:
                self._logger.info("Extracted %d lesson(s) from chat (vector store)", count)
            return

        if not lesson_store:
            return
        from datetime import timezone as _tz

        from kiro_crew.learn import Lesson

        count = 0
        for item in raw:
            if isinstance(item, dict) and item.get("rule"):
                scope, drop = self._gated_lesson_scope(item)
                if drop:
                    continue
                outcome = gate.save(
                    lesson_store,
                    Lesson(
                        ts=datetime.now(tz=_tz.utc).isoformat(),
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        # Gated by _gated_lesson_scope above (LessonStore.save
                        # canonicalises but never checks admissibility itself).
                        repo_scope=scope,
                        # None is dropped by _serializable, so an unstated row
                        # is byte-identical to one written before the field.
                        applies=self._lesson_tier(item),
                    ),
                )
                if outcome != "refused":
                    count += 1
        if count:
            self._logger.info("Extracted %d lesson(s) from chat", count)

    def _write_structured_memory(
        self,
        result: dict,
        key: str,
        vector_store: "VectorMemoryStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
        *,
        facets: "MemoryFacets | None" = None,
        snapshot: dict | None = None,
        messages: list[dict] | None = None,
        gate: _WriteGate,
    ) -> None:
        """Write semantic + episodic entries from consolidation result.

        *vector_store* is the session's RESOLVED store. Omitting it keeps the
        global handle, which is what the workspace and default arms want; an
        explicit ``None`` means the silo has no vector store and the tier is
        skipped, the same distinction :meth:`_save_lessons` draws. Every row is
        written through *gate*, the pass's :class:`_WriteGate`: the mode is
        re-read before each one, so a modifier landing between two rows of the
        batch stops the second.
        """
        if isinstance(vector_store, _InheritGlobal):
            vector_store = self._vector_store
        if not vector_store:
            return
        source = f"consolidation:{key}"
        private_policy = getattr(vector_store, "algorithm_version", "v1") == "v2"
        # Shared by both tiers below: each embeds inline, so both charge the same
        # pass and either can arm the latch for the other.
        budget = _EmbedBudget(_EMBED_BUDGET_SECS_PER_PASS, self._logger)

        # Semantic entries
        semantic_items = result.get("semantic")
        if isinstance(semantic_items, list):
            written = 0
            deleted = 0
            skipped = 0
            refused = 0
            for item in semantic_items[:_MAX_SEMANTIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                    continue
                # Handle deletion of stale keys
                if item.get("delete"):
                    if private_policy:
                        if gate.propose_semantic_delete(vector_store, item["key"], source):
                            refused += 1
                    elif gate.delete_semantic(vector_store, item["key"], source):
                        deleted += 1
                    continue
                if "value" not in item or item["value"] is None:
                    # Counted and logged here because this path returns before set_semantic, so
                    # the VALUE_EMPTY reject event never fires for the omission that motivated it.
                    skipped += 1
                    self._logger.warning(
                        "Semantic consolidation skipped %r: item carries no value", item["key"]
                    )
                    continue
                try:
                    conf = float(item.get("confidence", 0.5))
                except (ValueError, TypeError):
                    skipped += 1
                    continue
                if not math.isfinite(conf) or not 0 <= conf <= 1:
                    skipped += 1
                    continue
                # Always our own source, never "user_explicit": a confidence of 1.0 is the
                # LLM's claim that the user stated the fact, not proof of it. Under
                # `consolidation:<key>` the conflict resolution in _write_semantic protects
                # a genuine user-stated row from a re-summarization (conflict_skip) while
                # still letting consolidation create new keys and update its own.
                extra = {}
                if private_policy and item.get("metadata") is not None:
                    extra["metadata"] = item["metadata"]
                if private_policy and snapshot and messages and item["key"] in snapshot:
                    from kiro_crew.memory_record_metadata import verified_correction

                    evidence = verified_correction(
                        key=item["key"],
                        before=snapshot[item["key"]],
                        value=item["value"],
                        quote=item.get("correction_quote"),
                        messages=messages,
                        session_key=key,
                    )
                    if evidence:
                        extra["correction"] = evidence
                        extra["expected_revision"] = evidence.revision
                with budget.measured():
                    err = gate.set_semantic(
                        vector_store,
                        key=item["key"],
                        value=item["value"],
                        confidence=conf,
                        source=source,
                        facets=facets,
                        defer_embedding=budget.tripped,
                        **extra,
                    )
                if err is None:
                    written += 1
                else:
                    # Counted apart from `skipped`: several reject causes reach here and only
                    # VALUE_EMPTY is a missing value, so a shared label names the wrong cause.
                    reject_code, reason = err
                    refused += 1
                    # The reason names the specific cause a bare code cannot (which
                    # confidence lost, which proposal holds the value). Causes the store
                    # audits also carry both values in memory_events under the cause as
                    # the event type; VALUE_SIZE and VALUE_ENCODING audit nothing, which
                    # is why the pointer is scoped rather than a promise for every code.
                    self._logger.warning(
                        "Semantic consolidation refused %r: %s: %s"
                        " (audited causes carry both values in memory_events)",
                        item["key"],
                        reject_code.value,
                        reason,
                    )
            if written or deleted or skipped or refused:
                self._logger.info(
                    "Semantic consolidation: %d written, %d deleted, %d skipped (no value), "
                    "%d refused",
                    written,
                    deleted,
                    skipped,
                    refused,
                )

        # Episodic entries
        episodic_items = result.get("episodic")
        if isinstance(episodic_items, list):
            written = 0
            deferred = 0
            for item in episodic_items[:_MAX_EPISODIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                tags = item.get("tags", [])
                if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                    continue
                try:
                    importance = float(item.get("importance", 0.5))
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(importance) or not 0 <= importance <= 1:
                    continue
                # `defer_embedding` stores the row with a NULL vector instead of
                # embedding it here. The text is keyword-searchable at once and the
                # repair sweep fills the vector in.
                #
                # `preserve_existing` comes with it, and is not optional: without a
                # vector the similarity dedup cannot run, and on a legacy V1 store
                # at its episodic cap the insert would then tombstone the
                # lowest-importance row to make room for a paraphrase it never
                # compared against. A write that cannot arbitrate a conflict has no
                # standing to evict, so at the cap the deferred row is refused
                # instead — the transcript it came from is still on disk, and the
                # row it would have displaced is not recoverable.
                #
                # Read before the write, so the row that SPENDS the budget is the
                # last one to pay for an embed rather than the first to skip one.
                defer = budget.tripped
                with budget.measured():
                    ep_ok = gate.write_episodic(
                        vector_store,
                        text=item["text"],
                        conversation_id=key,
                        tags=tags,
                        importance=importance,
                        source=source,
                        facets=facets,
                        defer_embedding=defer,
                        preserve_existing=defer,
                    )
                if ep_ok:
                    written += 1
                    if defer:
                        deferred += 1
            if written:
                self._logger.info(
                    "Wrote %d episodic entries from consolidation (%d with embedding deferred)",
                    written,
                    deferred,
                )

    def _dedupe_candidate(
        self, slug: str, description: str, triggers: str
    ) -> "tuple[str, str | None]":
        """Classify a candidate against existing auto-skills.

        Returns ``(verdict, key)`` where ``verdict`` is one of ``VERDICT_NEW``
        (stage as a new candidate), ``VERDICT_DUP`` (drop — pure re-detection),
        or ``VERDICT_UPDATE`` (stage a pending update to ``key``). ``key`` is the
        matched/target existing-skill key for DUP/UPDATE, else ``None``.

        Primary: a single tri-state metadata-judge call comparing the candidate
        against ALL existing auto-skills at once (bounded set, no embeddings).
        Lexical ``find_similar`` runs as a fallback when the judge is unavailable
        (no ``judge_model``, no captured event loop, or no existing skills) AND
        as a safety net when the judge returns ``VERDICT_NEW`` — so a judge
        *failure* (which fails open to "new") can't silently skip dedup and let
        a near-identical skill through. A lexical hit is treated as a DUP.
        """
        loader = self._skills_loader
        if loader is None:
            return (VERDICT_NEW, None)
        existing = list(loader.list_auto_skills())
        # Include already-staged (pending) candidates so repeated sessions don't
        # queue a duplicate of something still awaiting review (list_auto_skills
        # only enumerates LIVE skills — .pending is pruned from discovery).
        try:
            for p in loader.list_pending_skills():
                existing.append(
                    {
                        "key": f"auto/{p.get('slug', '')}",
                        "description": p.get("description", ""),
                        "triggers": p.get("triggers", ""),
                    }
                )
        except Exception:
            pass
        loop = self._event_loop

        def _lexical() -> "tuple[str, str | None]":
            hit = loader.find_similar(description, threshold=self._auto_similarity_threshold)
            return (VERDICT_DUP, hit) if hit else (VERDICT_NEW, None)

        if self._judge_model and existing and loop is not None:

            def _judge_fn(prompt: str) -> str:
                try:
                    fut = asyncio.run_coroutine_threadsafe(self._dedupe_judge(prompt), loop)
                    return fut.result(timeout=60) or ""
                except Exception:
                    return ""

            candidate = {
                "key": f"auto/{slug}",
                "description": description,
                "triggers": triggers,
            }
            verdict, key = _facade_metadata_dedupe_verdict(candidate, existing, _judge_fn)
            # VERDICT_NEW means "new" OR a judge error (the verdict API fails open
            # to new). Either way, confirm with the cheap lexical check before
            # concluding the candidate is unique.
            if verdict == VERDICT_NEW:
                return _lexical()
            return (verdict, key)
        return _lexical()

    async def _dedupe_judge(self, prompt: str) -> str:
        """One cheap metadata-dedupe judge turn on the shared background session.
        Runs on that session's existing (lite / haiku-class) model — no per-turn
        ``set_model`` switch, because the ``BACKGROUND_KEY`` session is shared
        with consolidation and a switch would leak the judge model into later
        turns when recycling doesn't fire. Fail-open (returns "" on any error)."""
        if not self._sessions:
            return ""
        try:
            async with background_turn(
                self._sessions, task="skill_dedupe", agent="kirocrew-lite"
            ) as client:
                text = await _facade_stream_and_collect(
                    client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
                )
            return text or ""
        except Exception:
            self._logger.debug("Skill dedupe judge failed", exc_info=True)
            return ""

    async def _merge_skill_update(
        self, live_body: str, description: str, triggers: str, procedure_md: str
    ) -> "str | None":
        """Merge an existing live skill body with a new candidate into ONE
        updated markdown body — a single text turn on the shared background
        session. Mirrors ``_dedupe_judge`` exactly. Fail-open (returns ``None`` on
        any error) so the caller can fall back to a plain replacement proposal."""
        if not self._sessions:
            return None
        prompt = (
            "You are updating an existing auto-generated agent skill with a newly "
            "learned requirement. Merge the EXISTING skill body and the NEW "
            "requirement into ONE updated markdown skill body — fold the new "
            "requirement in, do NOT blindly replace the existing content. Keep "
            "the '## When to use', '## Steps', and '## Gotchas' sections. Keep "
            "the result under 8000 characters. Output ONLY the updated markdown "
            "body — no preamble, no explanation, no code fences.\n\n"
            f"EXISTING skill body:\n{live_body}\n\n"
            f"NEW requirement — description: {description}\n"
            f"NEW requirement — triggers: {triggers}\n"
            f"NEW requirement — procedure:\n{procedure_md}\n"
        )
        try:
            async with background_turn(
                self._sessions, task="skill_merge", agent="kirocrew-lite"
            ) as client:
                text = await _facade_stream_and_collect(
                    client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
                )
            return text or None
        except Exception:
            self._logger.debug("Skill update merge failed", exc_info=True)
            return None

    def _stage_skill_update(
        self,
        *,
        key: str,
        target_key: str,
        description: str,
        triggers: str,
        procedure_md: str,
        scripts: "list[dict] | None" = None,
        gate: _WriteGate,
    ) -> None:
        """Stage a pending UPDATE candidate for an existing auto-skill.

        (a) read the target's current live body; (b) LLM-merge it with the new
        requirement (bridged from this worker thread onto the captured loop,
        90s, fail-open); (c) use the redacted merge as the proposed body, else
        fall back to the candidate's own procedure (also on oversize); (d) stage
        under ``<target-slug>-update`` with ``kind='update'`` metadata, through
        *gate* (the pass's :class:`_WriteGate`: the mode is re-read at the
        write, after the merge turn); (e) SEL audit with outcome
        ``staged_update``."""
        loader = self._skills_loader
        if loader is None:
            return

        def _redact(text: object) -> str:
            if not isinstance(text, str):
                return ""
            safe, _ = redact_exfiltration_urls(text)
            safe, _ = redact_credentials(safe)
            return safe

        target_slug = target_key.split("/", 1)[-1]
        # Capture the base version BEFORE reading the body it describes. The merge
        # turn below can take up to 90s, and an approval landing in that window
        # advances live — sampling the version afterwards would record the NEW
        # version against a body merged from the OLD one, and
        # ``approve_pending_update``'s staleness guard would then see base ==
        # current and let the stale body overwrite the intervening update. Reading
        # it first fails safe in the other direction: if live advances after this
        # point the recorded base is behind, the guard fires, and the candidate is
        # refused rather than silently applied.
        try:
            base_version = loader.get_auto_skill_version(target_key)
        except Exception:
            base_version = 1
        try:
            live_body = loader.read_auto_skill_body(target_key)
        except Exception:
            live_body = None
        if not live_body:
            # ``_dedupe_candidate`` deliberately includes already-PENDING
            # candidates in the judge's ``existing`` set (so repeated sessions
            # don't queue duplicates), which means the judge can answer
            # ``UPDATE auto/<pending-slug>`` — a target that is not live.
            # ``approve_pending_update`` requires a live target, so staging that
            # would queue a candidate the user can never approve. Drop it
            # instead, audited so the loss is visible.
            self._logger.info(
                "Skill update skipped: target '%s' is not a live auto skill",
                target_key,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="rejected",
                metadata={"target": target_key, "reason": "target_not_live"},
            )
            return
        # ``read_auto_skill_body`` returns the FULL SKILL.md (frontmatter
        # included). Only the prose body may be merged: ``stage_skill_candidate``
        # re-wraps the result in its own frontmatter, so feeding the header in
        # invites the merge to echo it back and nest a second ``---`` block
        # inside the procedure.
        # Redact before the merge prompt. The read path already refuses symlinks
        # into credential storage, but a credential can also be typed straight
        # INTO a skill body via the dashboard editor — that file legitimately
        # lives in the skills tree, so no path guard catches it. The candidate's
        # own description/triggers/procedure are redacted upstream; this was the
        # one input reaching the model raw. (Redaction also runs on the merge
        # OUTPUT, which is too late to protect the prompt.)
        live_prose = _redact(_strip_skill_frontmatter(live_body))

        merged: "str | None" = None
        if live_prose and self._event_loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._merge_skill_update(live_prose, description, triggers, procedure_md),
                    self._event_loop,
                )
                merged = fut.result(timeout=90)
            except Exception:
                merged = None

        used_merge = False
        body = procedure_md
        if merged:
            # Defensive sanitize: the prompt forbids fences/frontmatter, but a
            # model may still emit them — strip both so the staged candidate's
            # procedure is pure markdown prose.
            red = _redact(_strip_skill_frontmatter(_strip_code_fence(merged)))
            if red and len(red) <= AUTO_SKILL_MAX_PROCEDURE_CHARS:
                body = red
                used_merge = True

        provenance = AutoSkillProvenance(session_key=key, created_at=AutoSkillProvenance.now_iso())
        # The slug pattern caps at 64 chars, and our own generation prompt permits
        # up to 60, so `<target>-update` can overflow and be REJECTED by staging —
        # silently dropping the learning, because consolidation advances its
        # message offset regardless of candidate outcome. Reserve room for
        # "-update" (7) plus the "-2".."-50" collision suffix (3).
        _update_slug = f"{target_slug[:54].rstrip('-')}-update"
        # Approval writes the candidate's frontmatter over the live skill, so the
        # candidate must carry the MERGED metadata, not just its own. The body is
        # merged by the LLM turn above; description/triggers were not, and the
        # candidate only proposes triggers for the NEW requirement — replacing the
        # live list would stop the skill activating on everything it already
        # answered. Union the triggers and keep the live description when the
        # candidate did not supply one.
        _live_triggers = _frontmatter_value(live_body, "triggers")
        _live_description = _frontmatter_value(live_body, "description")
        _staged_triggers = _merge_trigger_lists(_live_triggers, triggers)
        _staged_description = description or _live_description
        name = gate.stage_skill_candidate(
            loader,
            _update_slug,
            description=_staged_description,
            triggers=_staged_triggers,
            procedure_md=body,
            provenance=provenance,
            scripts=scripts or None,
            kind="update",
            target=target_key,
            base_version=base_version,
        )
        if name:
            self._logger.info(
                "Staged skill update %s (target %s) from session %s",
                name,
                target_key,
                key,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="staged_update",
                metadata={
                    "name": name,
                    "target": target_key,
                    "base_version": base_version,
                    "merged": used_merge,
                },
            )
        else:
            self._logger.info("Skill update staging rejected for target '%s'", target_key)
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="rejected",
                metadata={"slug": _update_slug, "reason": "creation_failed"},
            )

    def _process_auto_skills(self, result: dict, key: str, gate: _WriteGate) -> None:
        """Extract + write auto-generated skills from the consolidation result.

        Handles both ``new_skill`` and ``refined_skill`` result keys.  Each
        is validated, redacted via ``security.redact_*``, then deduped
        against existing skills (for new creation) before being written
        through ``SkillsLoader`` -- every write through *gate*, the pass's
        :class:`_WriteGate`, so the mode is re-read at each one (the dedupe
        judge and the update merge are model turns of their own between the
        generation and these writes).  Every successful write emits a SEL audit
        event via ``_facade_sel().log_tool_invocation``.
        """
        if self._skills_loader is None:
            return

        def _redact(text: object) -> str:
            """Run the same two-pass redaction used for Slack/dashboard output."""
            if not isinstance(text, str):
                return ""
            safe, _ = redact_exfiltration_urls(text)
            safe, _ = redact_credentials(safe)
            return safe

        # Create path
        new_skill = result.get("new_skill")
        if isinstance(new_skill, dict):
            slug = str(new_skill.get("slug", "")).strip()
            description = _redact(new_skill.get("description", ""))
            triggers = _redact(new_skill.get("triggers", ""))
            procedure_md = _redact(new_skill.get("procedure_md", ""))
            # Extract + statically validate any generated scripts. Scripts are
            # redacted, then each is checked by the always-on static validator;
            # only individually-clean scripts survive. A script-bearing
            # candidate ALWAYS routes to approval (never auto-published).
            valid_scripts: list[dict] = []
            scripts_supplied = False
            if self._generate_scripts:
                raw_scripts = new_skill.get("scripts")
                if isinstance(raw_scripts, list) and raw_scripts:
                    scripts_supplied = True
                    for s in raw_scripts:
                        if not isinstance(s, dict):
                            continue
                        fn = _redact(s.get("filename", "")).strip()
                        body = _redact(s.get("content", ""))
                        ok, _findings = validate_skill_script(fn, body)
                        if ok:
                            valid_scripts.append({"filename": fn, "content": body})
                        else:
                            self._logger.info(
                                "Auto-skill script %r rejected by validator: %s",
                                fn,
                                "; ".join(_findings),
                            )
            if not (slug and description and procedure_md):
                # Required fields missing (or stripped empty by redaction).
                # Audit the rejection so operators can see that a create
                # attempt happened but lacked the minimum inputs.
                self._logger.info(
                    "Auto-skill create skipped: empty slug/description/procedure "
                    "after redaction (slug=%r)",
                    slug,
                )
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_create",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={
                        "slug": slug or "(empty)",
                        "reason": "empty_after_redaction",
                    },
                )
            else:
                verdict, target = self._dedupe_candidate(slug, description, triggers)
                # ``_dedupe_candidate`` deliberately shows the judge already-PENDING
                # candidates too (so repeat sessions don't queue duplicates), which
                # means an UPDATE verdict can name a target that is not LIVE. Such a
                # target cannot be updated — but the requirement is genuinely new
                # relative to the live skill set, and consolidation advances its
                # message offset regardless, so dropping it would lose the learning
                # for good. Downgrade to a NEW candidate instead: it only overlaps
                # another *proposal*, which the human reviews side by side anyway.
                if verdict == VERDICT_UPDATE and target:
                    try:
                        _target_is_live = (
                            self._skills_loader.read_auto_skill_body(target) is not None
                        )
                    except Exception:
                        _target_is_live = False
                    if not _target_is_live:
                        self._logger.info(
                            "Auto-skill UPDATE target '%s' is not live (pending candidate); "
                            "staging '%s' as a new candidate instead of dropping it",
                            target,
                            slug,
                        )
                        verdict = VERDICT_NEW
                if verdict == VERDICT_DUP:
                    self._logger.info(
                        "Auto-skill synthesis skipped: '%s' overlaps existing skill '%s'",
                        slug,
                        target,
                    )
                    _facade_sel().log_tool_invocation(
                        session_key=key,
                        tool_name="auto_skill_create",
                        tool_kind="skills",
                        outcome="rejected",
                        metadata={
                            "slug": slug,
                            "reason": "similar_exists",
                            "existing": target,
                        },
                    )
                elif verdict == VERDICT_UPDATE and target:
                    # Same skill, new requirements worth folding in — stage a
                    # pending UPDATE candidate rather than dropping the learning.
                    self._stage_skill_update(
                        key=key,
                        target_key=target,
                        description=description,
                        triggers=triggers,
                        procedure_md=procedure_md,
                        scripts=valid_scripts or None,
                        gate=gate,
                    )
                else:
                    provenance = AutoSkillProvenance(
                        session_key=key,
                        created_at=AutoSkillProvenance.now_iso(),
                    )
                    if scripts_supplied and not valid_scripts and not self._approval_required:
                        # The user opted out of prose review, but this candidate
                        # attempted to add executable content and every script
                        # failed validation. Do not disguise it as a prose-only
                        # skill, and do not create an approval request the user
                        # explicitly disabled: reject the candidate as a whole.
                        self._logger.info(
                            "Auto-skill candidate %s rejected: all supplied scripts failed validation",
                            slug,
                        )
                        _facade_sel().log_tool_invocation(
                            session_key=key,
                            tool_name="auto_skill_create",
                            tool_kind="skills",
                            outcome="rejected",
                            metadata={"slug": slug, "reason": "all_scripts_rejected"},
                        )
                    elif self._approval_required or valid_scripts:
                        # Stage when review is enabled or the candidate retained
                        # a validator-passed script. A mixed candidate keeps only
                        # the scripts that passed validation; with review enabled,
                        # an all-rejected candidate can still be inspected as
                        # prose. (An all-invalid candidate with approval disabled
                        # is consumed by the reject branch above, so a bare
                        # scripts_supplied never decides this branch.)
                        name = gate.stage_skill_candidate(
                            self._skills_loader,
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                            scripts=valid_scripts or None,
                        )
                        if name:
                            self._logger.info(
                                "Staged skill candidate %s from session %s", name, key
                            )
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="staged",
                                metadata={"name": name, "scripts": len(valid_scripts)},
                            )
                        else:
                            self._logger.info("Skill staging rejected for slug '%s'", slug)
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="rejected",
                                metadata={"slug": slug, "reason": "creation_failed"},
                            )
                    else:
                        name = gate.create_auto_skill(
                            self._skills_loader,
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                        )
                        if name:
                            self._logger.info("Auto-created skill %s from session %s", name, key)
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="invoked",
                                metadata={"name": name},
                            )
                            # Bound the live auto-skill set after a live create
                            # (auto-approve path). Best-effort; never break
                            # consolidation on a lifecycle hiccup.
                            try:
                                self._skills_loader.run_skill_lifecycle(
                                    max_auto_skills=self._max_auto_skills,
                                    stale_after_days=self._stale_after_days,
                                    archive_after_days=self._archive_after_days,
                                )
                            except Exception:  # pragma: no cover - defensive
                                self._logger.debug("Skill lifecycle pass failed", exc_info=True)
                        else:
                            # create_auto_skill returned None: invalid slug,
                            # oversized procedure, or directory already exists.
                            # Audit the rejection so operators can see why.
                            self._logger.info(
                                "Auto-skill creation rejected for slug '%s' (creation_failed)",
                                slug,
                            )
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="rejected",
                                metadata={
                                    "slug": slug,
                                    "reason": "creation_failed",
                                },
                            )
        else:
            # Eligible session ran the skill-gen prompt, but the model returned
            # no new-skill candidate. Emit a lightweight audit trail so
            # operators can distinguish "asked, model declined" from "never
            # asked" — with no event or log line here the audit log cannot show
            # whether skill generation was attempted during a consolidation.
            self._logger.info(
                "Auto-skill: model proposed no skill candidate for session %s",
                key,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="skipped",
                metadata={"reason": "no_candidate_proposed"},
            )

        # Refine path (only if explicitly enabled)
        if not self._auto_refine_enabled:
            return
        refined = result.get("refined_skill")
        if isinstance(refined, dict):
            name = str(refined.get("name", "")).strip()
            if not self._skills_loader.is_auto_generated(name):
                self._logger.info("Auto-skill refine rejected for %s: not in auto namespace", name)
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "not_auto_namespace"},
                )
                return
            description = _redact(refined.get("description", ""))
            triggers = _redact(refined.get("triggers", ""))
            procedure_md = _redact(refined.get("procedure_md", ""))
            if not description or not procedure_md:
                self._logger.info(
                    "Auto-skill refine skipped for %s: empty description/procedure "
                    "after redaction",
                    name,
                )
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "empty_after_redaction"},
                )
                return
            provenance = AutoSkillProvenance(
                session_key=key,
                created_at=AutoSkillProvenance.now_iso(),
                refined_at=AutoSkillProvenance.now_iso(),
            )
            ok = gate.update_auto_skill(
                self._skills_loader,
                name,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )
            if ok:
                self._logger.info("Auto-refined skill %s from session %s", name, key)
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="invoked",
                    metadata={"name": name},
                )
            else:
                # update_auto_skill returned False: oversized procedure,
                # file missing, or other internal rejection.  Audit it so
                # operators can trace why a refine was proposed but not
                # applied.
                self._logger.info("Auto-skill refine rejected for %s (update_failed)", name)
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "update_failed"},
                )

    async def _call_llm(
        self, prompt: str, *, memory_store: str = "", session_key: str = ""
    ) -> dict | None:
        """Call LLM for consolidation via the persistent background session.

        Uses the shared background kiro-cli process (no spawn/teardown cost).
        Returns the parsed JSON dict, or ``None`` when the turn reached the
        provider but produced nothing usable (a failed or unparsable answer).

        Raises :class:`_ConsolidationNotDispatched` when the prompt never reached
        the provider at all — no session manager, or the background session could
        not be acquired because kiro-cli is missing, not logged in, or failing to
        start. That case is signalled separately rather than folded into ``None``
        because the two cost different things: a spent turn costs money and must
        consume the caller's retry budget, while a prompt that was never sent costs
        nothing and must not, or a broken host would abandon spans it never read.
        An exception (rather than a flag beside the result) is used so a caller
        cannot silently drop the distinction.

        Once ``stream_and_collect_json`` is entered the prompt counts as sent: a
        failure inside it may still have been billed, so it returns ``None`` and is
        charged rather than risk an unbounded retry loop over real spend.
        """
        if not self._sessions:
            self._logger.warning("LLM consolidation skipped — no session manager")
            raise _ConsolidationNotDispatched("no session manager")

        # Timing instrumentation: measure both the wait to acquire the shared
        # `_bg` session (queue contention behind other `_bg` consumers like
        # chat_nav link-preview) and the LLM turn itself. Logged at DEBUG:
        # silent in normal operation, surfaced only when log_level is raised
        # to investigate a consolidation stall.
        t_start = _time.monotonic()
        async with contextlib.AsyncExitStack() as stack:
            try:
                client = await stack.enter_async_context(
                    background_turn(
                        self._sessions,
                        task="consolidation",
                        agent="kirocrew-lite",
                        # This turn is spent on ONE session's transcript, so its
                        # cost belongs in that session's log even though the user
                        # never asked for it. Callers that pass no key -- skill
                        # detection, the dedupe and merge judges -- are not charged
                        # to a single session and record nothing.
                        crew_log_kind="memory_consolidation",
                        crew_log_session_key=session_key,
                        **({"memory_store": memory_store} if memory_store else {}),
                    )
                )
            except Exception as exc:
                self._logger.warning(
                    "Consolidation could not acquire the background session "
                    "after %.1fs — nothing was sent",
                    _time.monotonic() - t_start,
                    exc_info=True,
                )
                raise _ConsolidationNotDispatched("background session unavailable") from exc
            t_acquired = _time.monotonic()
            wait_s = t_acquired - t_start
            # Reject all tools: this is a text/JSON-only generation turn. kiro
            # scopes the kirocrew-lite session to tools:[] via set_mode, but the
            # Claude Code backend skips set_mode and injects the full
            # kirocrew-core/cron toolset — without REJECT_ALL a background
            # consolidation turn could fire side-effecting tools (send_message,
            # learn_add, spawn_run). REJECT_ALL keeps both providers tool-free.
            try:
                result = await _facade_stream_and_collect_json(
                    client,
                    prompt,
                    approval_policy=ToolApprovalPolicy.REJECT_ALL,
                    model_fallback=True,
                )
            except Exception:
                self._logger.warning(
                    "LLM consolidation turn failed after %.1fs",
                    _time.monotonic() - t_start,
                    exc_info=True,
                )
                return None
            turn_s = _time.monotonic() - t_acquired
            self._logger.debug(
                "Consolidation LLM turn: wait=%.1fs turn=%.1fs total=%.1fs ok=%s",
                wait_s,
                turn_s,
                _time.monotonic() - t_start,
                result is not None,
            )
            return result
        # Reached only if the exit stack suppresses an exception. The prompt was
        # already sent by then, so the turn may have been billed: report it as a
        # spent-but-unusable result rather than a non-dispatch, which would hand
        # the caller a free retry it has not earned.
        return None
