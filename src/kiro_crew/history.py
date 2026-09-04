"""Persistent conversation history — JSONL per session + LLM consolidation.

Session files: ~/.kiro/crew/sessions/{safe_key}.jsonl
Each entry tracks provenance (source_thread, source_user) for citation.
Appends through ``ConversationLog.append`` auto-rotate at 10MB, keeping up to 200
lines within that byte cap. The dashboard whole-file save does not rotate, so a
transcript written only through it is bounded by its message window instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import math
import os
import re
import threading
import time as _time
import uuid
from collections.abc import Callable, Container, Iterator, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, overload

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.executors import run_in_embed_pool  # noqa: F401 - facade re-export
from kiro_crew.frontmatter import (  # noqa: F401 - facade re-exports
    SKILL_UPDATE,
    frontmatter_value,
)
from kiro_crew.history_cache import (
    _METADATA_CACHE_MAX,
    _TRANSCRIPT_CACHE_MAX,
    HistoryCacheCoordinator,
    _FileChangeCacheEntry,
    _LRUCache,
    _SearchTextCache,
)
from kiro_crew.history_consolidation import (  # noqa: F401 - facade re-exports
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_BACKOFF_MAX_SECS,
    _CONSOLIDATION_MAX_ATTEMPTS,
    _CONSOLIDATION_META_KEYS,
    _CONSOLIDATION_REFUSED,
    _CONSOLIDATION_THRESHOLD,
    _PLACEHOLDER_BODIES,
    _SENSITIVE_TOOL_PATTERNS,
    _SKILL_DETECTION_WINDOW,
    _TOOL_ROLES,
    AttemptedSpan,
    HistoryConsolidator,
    _ConsolidationNotDispatched,
    _ConsolidationRefusedSentinel,
    _count_tool_call_messages,
    _fmt_message,
    _frontmatter_value,
    _is_plausible_memory_file,
    _merge_trigger_lists,
    _session_touched_sensitive,
    _strip_code_fence,
    _strip_skill_frontmatter,
)
from kiro_crew.history_projection import (
    SessionMetadataProjection,
    TranscriptReadProjection,
)
from kiro_crew.history_rewrite import HistoryRewriteCoordinator
from kiro_crew.history_search import (  # noqa: F401 - facade re-exports
    _CJK_CHAR_WEIGHT,
    _FORGE_CHAIN_ONLY_WORDS,
    _FORGE_LEAD_PUNCT,
    _FORGE_MR_WORDS,
    _FORGE_REF_WEIGHT,
    _FORGE_REF_WORDS,
    _FORGE_REQUEST_WORDS,
    _FORGE_SIGIL_RE,
    _FORGE_TRAIL_PUNCT,
    _FORGE_TYPE_WORDS,
    _FORGE_URL_RE,
    _FORGE_URL_REPO_RE,
    _FORGE_WORD_NUM_RE,
    _PHRASE_BOOST,
    _RECENCY_HALF_WEIGHT_DAYS,
    _RECENCY_MAX_BOOST,
    _SEARCH_FOLD_BUDGET_BYTES,
    _SEARCH_MAX_FORGE_REFS,
    _SEARCH_MAX_SCORING_EXTRAS,
    _SEARCH_SCAN_WINDOW,
    _SEARCH_SNIPPET_BUDGET_BYTES,
    _TITLE_BOOST,
    SEARCH_MAX_TOKENS,
    SEARCH_MIN_CHARS,
    SearchNeedle,
    SessionCatalogProjection,
    _forge_lead_in,
    _forge_spellings,
    _forge_type_suffix,
    _ForgeRef,
    _is_cjk_char,
    _lead_names_a_type,
    _lead_names_merge_request,
    _parse_forge_ref,
    _script_runs,
    count_needle,
    needles_match_text,
    parse_search_query,
    snippet_needles,
)
from kiro_crew.llm_helpers import (  # noqa: F401 - facade re-exports
    ToolApprovalPolicy,
    background_turn,
    stream_and_collect,
    stream_and_collect_json,
)
from kiro_crew.messaging.link import canonical_key, legacy_key
from kiro_crew.preview_text import strip_markdown_preview  # noqa: F401 - facade re-export
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel  # noqa: F401 - facade re-export
from kiro_crew.skills import (  # noqa: F401 - facade re-export
    AUTO_SKILL_MAX_PROCEDURE_CHARS,
    AutoSkillProvenance,
)
from kiro_crew.skills_dedupe import (  # noqa: F401 - facade re-exports
    VERDICT_DUP,
    VERDICT_NEW,
    VERDICT_UPDATE,
    metadata_dedupe_verdict,
)
from kiro_crew.skills_script_validator import validate_skill_script  # noqa: F401 - facade re-export
from kiro_crew.vector_memory_constants import (  # noqa: F401 - facade re-exports
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

logger = logging.getLogger(__name__)

HistoryConsolidator.__module__ = __name__
AttemptedSpan.__module__ = __name__

SESSIONS_DIR_NAME = "sessions"
ARCHIVE_DIR_NAME = "archive"
ARCHIVE_RETENTION_DAYS = 7

# Separates a transcript's stem from an archive segment's timestamp. NOT a dot,
# because session keys legitimately contain dots (a Slack thread_ts), which would
# make a right-most-dot parse attribute a segment to the wrong session.
ARCHIVE_SEGMENT_DELIMITER = "__"

# The keys :meth:`ConversationLog.compact` is authoritative for; every other field
# on the metadata line is another layer's and is carried through.
_COMPACT_OWNED_META_KEYS: frozenset[str] = frozenset(
    {"_type", "created_at", "last_consolidated", "compacted_at"}
)

# The keys the dashboard's slot save is authoritative for. It reconstructs the
# line from the slot's in-memory state, so for THESE absence is meaningful (a
# cleared title, an un-pinned slot, a reopened tab) — hence they are named here
# and not preserved, while everything else survives the save.
SLOT_OWNED_META_KEYS: frozenset[str] = frozenset(
    {
        "_type",
        "created_at",
        "last_consolidated",
        "closed",
        "closed_at",
        "memory_mode",
        "title",
        "agent",
        "model",
        "reasoning_effort",
        "autocompact_pct",
        "mode",
        "workspace",
        "project",
        "folder_id",
        "app",
        "artifact",
        "pinned",
        "color_index",
        "color_hex",
        "color_theme",
        "tags",
        "forked_from",
        "linked_session_key",
        "tab_id",
    }
)


def carry_unowned_metadata(
    rebuilt: dict,
    existing: dict,
    owned: Container[str],
) -> dict:
    """Carry every pre-existing metadata field the rebuilding writer does not own.

    A writer that reconstructs a transcript's metadata line from its own state
    (the dashboard slot save, :meth:`ConversationLog.compact`) is authoritative
    ONLY for the keys it writes: for those, absence is meaningful and must erase
    (clearing a title, un-pinning, reopening a closed tab). For every OTHER key
    absence means "not mine to know about", so reconstructing the subset silently
    deletes another layer's durable state.

    Enumerating the foreign keys to preserve instead is the failure mode this
    replaces: each new field has to be added to every rebuilder, and the one that
    is missed loses data with no error — the rotation generation and the
    consolidation retry accounting were both erased that way. So *owned* names the
    writer's OWN keys and everything else is carried through verbatim, making
    preservation the default and erasure the deliberate act.

    Preservation is unconditional, INCLUDING the consolidation retry accounting
    (:data:`_CONSOLIDATION_META_KEYS`), because a rebuild is not evidence about
    content: a slot flush re-serializes the same window, and a compaction archives
    turns the budget has already measured without introducing any the LLM has not
    seen — erasing the accounting there resets a live backoff and resumes billed
    retries.

    A save that genuinely EDITS the conversation is distinguished by the content
    identity it writes, not by what this helper drops: the dashboard rewrite path
    advances ``rotation_generation``, which releases the budget through the span
    identity the accounting is stamped with (see :class:`AttemptedSpan` and
    :meth:`ConversationLog._attempts_describe_current_span`) and invalidates any
    in-flight attempt's marker write. Keeping that in ONE counter is what makes an
    edit and a rotation behave identically; a second, parallel drop-the-keys valve
    here would additionally discard the armed backoff deadline, handing a session
    whose consolidation keeps failing a free billed turn on every user edit.

    Returns *rebuilt*, mutated in place.
    """
    for meta_key, value in existing.items():
        if meta_key in owned or meta_key in rebuilt:
            continue
        rebuilt[meta_key] = value
    return rebuilt


_SESSION_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_SESSION_KEEP_LINES = 200
# Bounded cross-process lock acquisition. The per-session sidecar ``flock`` is
# acquired on the hot ``append`` path, which some transports (Telegram/WeCom/
# Webex dispatch, workflow/cron injection) may still call synchronously from the
# gateway event loop. An UNBOUNDED blocking acquire there would let one wedged
# writer in another process freeze the whole gateway indefinitely, and simply
# *proceeding* after a timeout would write WITHOUT the cross-process lock — a
# stale rewrite in the holder can then clobber that write (silent transcript
# loss). So we do neither: we poll ``try_acquire_lock`` up to a bounded deadline
# and, on timeout, RAISE ``HistoryLockTimeout`` (fail the mutation) instead of
# writing unlocked. The critical sections under the lock are all short (append a
# line / rewrite one metadata line / rotate) so a real contention window clears
# in milliseconds; a timeout means a genuinely wedged holder.
#
# The acquire strategy is chosen by execution context. Off the event loop
# (worker threads, subagents, crons, CLI) a caller can afford to poll patiently
# up to ``_FLOCK_ACQUIRE_TIMEOUT_S``. ON a running asyncio loop we must NEVER
# block or sleep — even a sub-second poll stalls every chat/heartbeat/websocket
# on the gateway's sole event loop — so the loop path makes exactly ONE
# non-blocking acquire attempt and fails fast on contention. Callers on the loop
# should offload persistence via ``asyncio.to_thread`` (see the transport
# dispatchers' ``_persist_turn``) or via :func:`append_off_loop` (see the
# dashboard ``inject_cron_result_to_dashboard`` / ``inject_workflow_result``
# injectors); the single non-blocking attempt is the safety net for any that
# don't.
_FLOCK_ACQUIRE_TIMEOUT_S = 10.0
_FLOCK_POLL_INTERVAL_S = 0.05


class HistoryLockTimeout(TimeoutError):
    """Raised when the cross-process session lock cannot be acquired in time.

    The mutation is abandoned (never applied without the lock) so a concurrent
    rewrite in another process cannot silently clobber an unlocked write. A
    timeout indicates a wedged lock holder, not ordinary contention (critical
    sections are sub-millisecond).

    On-loop callers must NOT let this raise into an async handler (it would turn
    a transient, retryable lock contention into a user-visible 500). The two
    supported disciplines are: (1) offload the mutation off the loop so it takes
    the patient acquire path — the transport dispatchers' ``_persist_turn`` use
    ``asyncio.to_thread`` and the dashboard injectors use
    :func:`append_off_loop`; or (2) wrap the mutation in ``try/except`` and skip
    persistence on this error. Call-sites that do neither will surface it as the
    mutation's failure — by design, fail rather than write unlocked.
    """


class OnLoopPersistError(AssertionError):
    """A session-file mutation entered :meth:`ConversationLog._locked` directly
    on the event loop, violating the off-loop persistence discipline.

    The offload invariant (see the ``_locked`` contract and
    ``docs/system-specs/modules/history.md``) is that NO session-JSONL mutator
    runs on the gateway event loop: on-loop callers route through
    ``append_off_loop`` / ``append_if_absent_off_loop`` / ``update_metadata_off_loop``
    / ``save_slot_off_loop`` (or ``asyncio.to_thread``), all of which dispatch
    the mutation to a worker thread so ``_locked`` runs OFF the loop and takes
    the patient acquire path. A raw on-loop mutator call works in every low-
    traffic test and use (the flock is uncontended) and only loses data under
    real concurrency — where the on-loop path makes a single non-blocking
    acquire, raises :class:`HistoryLockTimeout`, and the caller's best-effort
    ``try/except`` swallows it as silent transcript loss.

    Because that failure is invisible in CI, ``_locked`` fails LOUD instead:
    when strict enforcement is active (:func:`_on_loop_persist_strict` — on under
    ``KIROCREW_STRICT_ON_LOOP_PERSIST`` or ``KIROCREW_DEV_MODE``) any on-loop
    entry raises this error so an un-offloaded call-site fails tests rather than
    losing data in production. In production (strict off) the same on-loop entry
    is instead logged loudly (throttled) and proceeds via the single non-blocking
    safety-net acquire. Tests that deliberately exercise the low-level on-loop
    primitive wrap the call in :func:`allow_on_loop_persist`.
    """


# When True (default in production), on-loop entry into ``_locked`` is a logged
# warning + single non-blocking acquire. When strict enforcement is active it
# raises :class:`OnLoopPersistError` instead. A ``ContextVar`` (async-safe, no
# cross-task bleed) lets the low-level primitive tests opt a single call out.
_allow_on_loop_persist: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kirocrew_allow_on_loop_persist", default=False
)

# Throttle the production on-loop warning so a mis-wired hot path cannot flood
# the log; the diagnostic still fires once per window per process.
_ON_LOOP_WARN_INTERVAL_S = 60.0
_on_loop_warn_last: float = 0.0


@contextlib.contextmanager
def allow_on_loop_persist() -> Iterator[None]:
    """Suppress the strict on-loop persistence assertion for the current context.

    Reserved for tests (and any genuinely-vetted call-site) that intentionally
    drive a session mutator directly on the event loop to exercise the
    low-level ``_locked`` primitive — e.g. the HistoryLockTimeout fail-fast
    path. Production code must NEVER use this: it must offload instead.
    """
    token = _allow_on_loop_persist.set(True)
    try:
        yield
    finally:
        _allow_on_loop_persist.reset(token)


def _on_loop_persist_strict() -> bool:
    """Whether an on-loop ``_locked`` entry should RAISE (vs. warn-and-proceed).

    Strict enforcement turns the convention-only offload discipline into a hard,
    test-catchable failure. It is on when EITHER:

    - ``KIROCREW_STRICT_ON_LOOP_PERSIST`` is truthy (explicit opt-in — the knob
      the discipline-enforcement tests flip, and a CI/dev harness can export to
      make an un-offloaded call-site fail instead of ship), or
    - ``KIROCREW_DEV_MODE`` is truthy (developer gateway runs — where all
      persistence is already offloaded, so a raise means a real newly-added
      un-offloaded path, surfaced immediately rather than lost under contention).

    A truthy ``KIROCREW_STRICT_ON_LOOP_PERSIST`` wins; an explicit falsy value
    force-disables even under dev-mode (so the production-fallback path stays
    testable). It is deliberately NOT auto-on under bare pytest: the suite's own
    async harness calls several mutators directly on the loop as a convenience
    (not a production path), so auto-strict would flag harness code, not drift.
    The default (no flag) is the production behavior — a loud throttled warning
    plus the non-blocking safety-net acquire — so shipping never introduces a new
    hard failure in the field.
    """
    explicit = os.environ.get("KIROCREW_STRICT_ON_LOOP_PERSIST", "").strip().lower()
    if explicit in _ON_LOOP_TRUTHY:
        return True
    if explicit in _ON_LOOP_FALSY:
        return False
    return os.environ.get("KIROCREW_DEV_MODE", "").strip().lower() in _ON_LOOP_TRUTHY


_ON_LOOP_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ON_LOOP_FALSY = frozenset({"0", "false", "no", "off"})


def on_loop_persist_strict() -> bool:
    """Public alias of :func:`_on_loop_persist_strict` for other modules.

    The strictness knob (``KIROCREW_STRICT_ON_LOOP_PERSIST`` /
    ``KIROCREW_DEV_MODE``) governs the on-loop persistence discipline for every
    store, not just this module's conversation log; consumers that enforce the
    same offload rule on their own SQLite databases (e.g. the auto_research
    campaigns DB) read the shared setting through this alias instead of
    importing a private name.
    """
    return _on_loop_persist_strict()


def _check_on_loop_persist_discipline(key: str) -> None:
    """Enforce (strict) or diagnose (production) an on-loop ``_locked`` entry.

    Called at the top of :meth:`ConversationLog._locked`. If there is no running
    event loop the caller is already off-loop (worker thread / CLI / subagent /
    cron) and this is a no-op — the common, correct case. On the loop it either
    raises :class:`OnLoopPersistError` (strict) or logs a throttled warning
    (production) so the un-offloaded call-site is never silent.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # off-loop: the sanctioned path — nothing to flag
    if _allow_on_loop_persist.get():
        return  # explicitly-vetted low-level primitive exercise (tests)
    if _on_loop_persist_strict():
        raise OnLoopPersistError(
            f"session mutation for {key!r} entered _locked on the event loop; "
            f"on-loop callers MUST offload (append_off_loop / "
            f"append_if_absent_off_loop / update_metadata_off_loop / "
            f"save_slot_off_loop / asyncio.to_thread) so the write takes the "
            f"patient off-loop acquire path — a raw on-loop mutation loses data "
            f"under real contention (HistoryLockTimeout swallowed as silent "
            f"transcript loss). Wrap in history.allow_on_loop_persist() only to "
            f"test the low-level primitive itself."
        )
    global _on_loop_warn_last
    now = _time.monotonic()
    if now - _on_loop_warn_last >= _ON_LOOP_WARN_INTERVAL_S:
        _on_loop_warn_last = now
        logger.warning(
            "history: session mutation for %s ran _locked ON the event loop "
            "without offloading; this drops the write under real contention "
            "(HistoryLockTimeout swallowed by best-effort callers). Route it "
            "through append_off_loop/save_slot_off_loop/asyncio.to_thread.",
            key,
            stack_info=True,
        )


def append_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    role: str,
    content: str,
    *,
    agent: str | None = None,
) -> None:
    """Persist a message without blocking (or fail-fast-dropping on) the loop.

    The lock-backed :meth:`ConversationLog.append` acquires a cross-process
    flock and writes to disk. On the gateway event loop that primitive makes a
    single non-blocking acquire attempt and raises :class:`HistoryLockTimeout`
    on *any* concurrent holder (see the ``_locked`` contract), so calling it
    directly from an async context both risks a disk write on the loop and drops
    the write under benign contention.

    This helper routes around both problems: on a running asyncio loop the
    append is dispatched to a worker thread (``run_in_executor``) so it takes the
    patient off-loop acquire path and never stalls the loop; off the loop it
    appends inline. Persistence is best-effort — the caller has already reflected
    the message in the in-memory slot, so a lock timeout or I/O error only skips
    the durable replay copy and is logged rather than raised.
    """

    def _do() -> None:
        conversation_log.append(key, role, content, agent=agent)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort durable copy
            logger.warning("append_off_loop: inline append failed key=%s", key, exc_info=True)
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning("append_off_loop: offloaded append failed key=%s: %r", key, exc)

    loop.run_in_executor(None, _do).add_done_callback(_report)


def append_rows_if_absent_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    rows: "Sequence[tuple[str, str, str, str | None]]",
    *,
    agent: str | None = None,
) -> Any:
    """Persist SEVERAL rows of one turn as one indivisible off-loop write.

    :func:`append_if_absent_off_loop` dispatches each row as its own executor
    task, so a caller writing a prompt+result PAIR hands two worker threads two
    independent writes: they can land out of order, and one can fail while the
    other succeeds. The transcript then replays a run whose rows are reversed or
    half-present, and no timestamp ordering repairs it because each row's ``ts``
    is correct on its own.

    This routes the whole group through ONE task holding
    :meth:`ConversationLog.atomic_appends`, whose contract names this hazard as
    the companion a multi-append caller needs precisely BECAUSE it moved the
    write off the loop. ``_locked`` is reentrant per key per thread, so the
    per-row locks inside ``append_if_absent`` reuse the hold rather than
    deadlocking on it.

    *rows* is an ordered sequence of ``(role, content, cls, mid)``; they are
    appended in that order. Each row keeps ``append_if_absent``'s idempotence,
    so a row the periodic slot save already serialized is skipped individually
    without dropping its siblings.

    Returns the executor future, or None when the write already happened inline
    (no running loop). Best-effort like its siblings: a lock timeout or I/O
    error only skips the durable replay copy the slot already carries.
    """

    def _do() -> None:
        with conversation_log.atomic_appends(key):
            for role, content, cls, mid in rows:
                conversation_log.append_if_absent(key, role, content, agent=agent, cls=cls, mid=mid)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort durable copy
            logger.warning(
                "append_rows_if_absent_off_loop: inline append failed key=%s",
                key,
                exc_info=True,
            )
        return None

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "append_rows_if_absent_off_loop: offloaded append failed key=%s: %r",
                key,
                exc,
            )

    fut = loop.run_in_executor(None, _do)
    fut.add_done_callback(_report)
    return fut


def append_if_absent_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    role: str,
    content: str,
    *,
    agent: str | None = None,
    cls: str = "",
    mid: str | None = None,
) -> Any:
    """Idempotent, loop-safe variant of :func:`append_off_loop`.

    Returns the executor future for the scheduled write, or None when the write
    already happened inline (no running loop). A caller holding the ONLY durable
    copy of something must await that future: scheduling is not durability.

    Routes :meth:`ConversationLog.append_if_absent` — which atomically skips a
    message already persisted under the same session lock — off the event loop
    exactly like :func:`append_off_loop`. Used by the workflow-result and
    cron-result injectors, which ALSO reflect the message in the in-memory slot
    (``slot.append``); the periodic slot save can therefore serialize the same
    message before this durable copy runs. The plain ``append`` would then
    double-write it; ``append_if_absent`` performs the disk check under the
    lock so the second write is a no-op. Best-effort — a lock timeout / I/O
    error only skips the durable replay copy (the slot already carries it) and
    is logged rather than raised.
    """

    def _do() -> None:
        conversation_log.append_if_absent(key, role, content, agent=agent, cls=cls, mid=mid)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort durable copy
            logger.warning(
                "append_if_absent_off_loop: inline append failed key=%s",
                key,
                exc_info=True,
            )
        return None

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "append_if_absent_off_loop: offloaded append failed key=%s: %r",
                key,
                exc,
            )

    fut = loop.run_in_executor(None, _do)
    fut.add_done_callback(_report)
    # Hand the future BACK: a caller holding the only durable copy awaits this
    # to turn "scheduled" into "on disk". Dropping it here made the barrier a
    # no-op on every running-loop path, i.e. every real gateway path.
    return fut


def update_metadata_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    fields: dict,
) -> None:
    """Merge session metadata without running the flock/fd ops on the loop.

    Mirrors :func:`append_off_loop`, but for the lock-backed
    :meth:`ConversationLog.update_metadata`. That method enters ``_locked``,
    which ``os.open``\\ s the sidecar, takes a cross-process ``flock`` and
    ``os.close``\\ s the fd. Those primitives are ``blocking: true`` under the
    ``no-blocking-call-on-event-loop`` rule: a wedged cross-process peer can
    stall the acquire (or, mid-release, the close) and freeze chat, WebSockets,
    and heartbeat until the watchdog restarts the gateway. This helper keeps
    them off the loop.

    Use it from *synchronous* helpers that may run on the event-loop thread
    (e.g. slot rehydration / startup restore) where ``await`` is not available.
    Async handlers that can ``await`` should prefer
    ``await asyncio.to_thread(conversation_log.update_metadata, ...)`` so the
    write is ordered against the response.

    On a running loop the update is dispatched to a worker thread (which takes
    ``_locked``'s patient off-loop acquire path); off the loop it runs inline.
    Persistence is best-effort — the mutation is metadata backfill the caller
    has already reflected in memory, so a lock timeout or I/O error is logged
    rather than raised.
    """

    def _do() -> None:
        conversation_log.update_metadata(key, fields)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _do()
        except Exception:  # noqa: BLE001 - best-effort metadata backfill
            logger.warning(
                "update_metadata_off_loop: inline update failed key=%s",
                key,
                exc_info=True,
            )
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "update_metadata_off_loop: offloaded update failed key=%s: %r",
                key,
                exc,
            )

    loop.run_in_executor(None, _do).add_done_callback(_report)


# Canonical set of memory_mode values that mark a session private — never
# searchable/listable/summarizable. Single source of truth shared by the MCP
# history tools (mcp_core) and the dashboard session handlers so the exclusion
# can't silently diverge between surfaces.
INCOGNITO_MEMORY_MODES = frozenset({"incognito", "temporary"})


def is_incognito_transcript(memory_mode: object) -> bool:
    """True when *memory_mode* marks a transcript private (incognito/temporary).

    The single shared predicate for :data:`INCOGNITO_MEMORY_MODES` membership,
    so the normalization cannot drift between the surfaces that must agree on
    what "private" means (history scans, MCP history tools, dashboard session
    handlers, Discord resume, summary/folder/channel-slot derivations).

    Normalization is ``str()`` + ``lower()`` — exactly what the call sites
    apply: ``None``/absent reads as persistent (not private), and comparison is
    case-insensitive because the set holds lowercase members while a
    hand-edited transcript header is not bound by the API's validation.
    Whitespace is deliberately NOT stripped and unrecognized values read as
    not-private: callers that must fail closed on an unreadable or junk header
    (e.g. the restricted-session write gate) resolve the mode through an
    allowlist first and deny on ``None`` before this membership test applies.
    """
    return str(memory_mode or "").lower() in INCOGNITO_MEMORY_MODES


# The fields that record where a message came from: the session key it arrived
# on (``source_thread``, e.g. ``slack:1785861252.833429``) and the platform user
# who sent it (``source_user``). Written by :meth:`ConversationLog.append`, read
# by :meth:`ConversationLog.get_source_threads` for cross-session citation and
# by SEL attribution.
PROVENANCE_FIELDS = ("source_thread", "source_user")


def carry_provenance(dest: dict, src: dict) -> None:
    """Copy *src*'s recorded provenance onto *dest*, leaving absent fields out.

    A message's origin is a property of the message, not of the file it happens
    to be stored in, so any path that copies or re-serializes a persisted line
    must carry these fields across or the copy claims a different origin than
    the original.

    Absent stays absent, and an empty or non-string value is treated as absent:
    :meth:`ConversationLog.append` writes each field only when it is truthy and
    :meth:`get_source_threads` filters on the same truthiness, so writing
    ``""`` would create a third state that reads as present-but-unusable.
    """
    for field in PROVENANCE_FIELDS:
        value = src.get(field)
        if isinstance(value, str) and value:
            dest[field] = value


def _safe_mtime(path: Path) -> float | None:
    """Return a file's mtime, or None if it can't be stat'd."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _restore_mtime(path: Path, prev_mtime: float | None) -> None:
    """Restore a session file's mtime after a *housekeeping* rewrite.

    ``list_sessions`` orders sessions by file mtime as a proxy for "last
    activity", and only a genuine message :meth:`ConversationLog.append`
    should advance that. Consolidation, rotation, and metadata updates
    (tab_id backfill on restore, title/agent/folder edits, last_consolidated
    bookkeeping) are background housekeeping — they rewrite the file but do
    NOT represent new conversation activity. Left unchecked they bump the
    mtime to "now", so every gateway restart (which consolidates + rehydrates
    open slots) floats long-closed sessions to the top of the session list and
    the "most recent session" a new dashboard/Slack session resolves to becomes
    a stale, unrelated thread. Restoring the pre-write mtime keeps ordering
    faithful to real activity. No-op when ``prev_mtime`` is None (fresh file).
    """
    if prev_mtime is None:
        return
    try:
        os.utime(path, (prev_mtime, prev_mtime))
    except OSError:
        pass


def _sessions_dir() -> Path:
    return config_dir() / SESSIONS_DIR_NAME


def _archive_dir(base: Path | None = None) -> Path:
    return (base or _sessions_dir()) / ARCHIVE_DIR_NAME


def _archive_lines(
    key: str, lines: list[str], reason: str, base: Path | None = None
) -> Path | None:
    """Append dropped message lines to archive/{key}.{YYYYMMDD-HHMMSS}.jsonl. Returns path or None."""
    if not lines:
        return None
    import itertools

    adir = _archive_dir(base)
    adir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    safekey = _safe_key(key)
    header = (
        json.dumps(
            {
                "_type": "archive",
                "reason": reason,
                "archived_at": now.isoformat(),
                "count": len(lines),
            }
        )
        + "\n"
    )
    payload = header + "".join(lines)
    # Atomic exclusive-create to avoid TOCTOU clobber when two archives land in the same second.
    for n in itertools.count():
        if n > 1000:
            raise RuntimeError(f"Failed to create archive file after {n} attempts")
        suffix = f"-{n}" if n else ""
        candidate = adir / f"{safekey}{ARCHIVE_SEGMENT_DELIMITER}{stamp}{suffix}.jsonl"
        try:
            with candidate.open("x", encoding="utf-8") as f:
                f.write(payload)
            break
        except FileExistsError:
            continue
    logger.info(
        "Archived %d lines from session %s to %s (reason=%s)",
        len(lines),
        key,
        candidate.name,
        reason,
    )
    _cleanup_old_archives(base=base)
    return candidate


_last_cleanup: float = 0.0


def _resolve_retention_days() -> int:
    """Read session.archive_retention_days from config.

    Returns the configured retention window in days, or ``-1`` when cleanup is
    disabled.  Falls back to the hardcoded default if config can't be loaded
    (e.g. during early init or in a stripped test environment).
    """
    try:
        return int(KiroCrewConfig.load().session.archive_retention_days)
    except Exception:
        return ARCHIVE_RETENTION_DAYS


def _cleanup_old_archives(retention_days: int | None = None, base: Path | None = None) -> int:
    """Delete archive files older than retention_days. Rate-limited to once per hour.

    When *retention_days* is None, the value is resolved from config
    (``session.archive_retention_days``).  A negative value disables cleanup
    entirely — the user manages archive deletion manually.
    """
    global _last_cleanup

    # Explicit negative disables cleanup immediately (no config read needed).
    if retention_days is not None and retention_days < 0:
        return 0  # cleanup disabled
    # Rate-limit guard runs BEFORE resolving retention from config so a
    # throttled call (the common case on hot archive paths) returns without
    # the expensive KiroCrewConfig.load() disk read + parse.
    now = _time.time()
    if now - _last_cleanup < 3600:
        return 0
    # Past the throttle window: stamp _last_cleanup NOW, before resolving
    # retention. Otherwise a config-resolved "disabled" (negative) would return
    # without updating the window, so every subsequent archive write would
    # re-run the expensive KiroCrewConfig.load().
    _last_cleanup = now
    # Resolve retention from config if not given, honoring a config-resolved
    # negative as the disable signal too.
    if retention_days is None:
        retention_days = _resolve_retention_days()
    if retention_days < 0:
        return 0  # cleanup disabled
    adir = _archive_dir(base)
    if not adir.exists():
        return 0
    cutoff = now - retention_days * 86400
    removed = 0
    for p in adir.glob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Cleaned %d expired archive files (>%dd)", removed, retention_days)
    return removed


def transcript_sort_key(ts: str) -> tuple[int, float]:
    """Sort key for a transcript timestamp: ``(bucket, epoch_seconds)``.

    Shared by every path that has to put two independently written streams of
    transcript lines into one chronological order.

    Timestamps in one transcript are not guaranteed to share one format. Both
    the dashboard and the channel path now write offset-aware values (message
    rows via :func:`monotonic_transcript_ts`, metadata via
    :func:`metadata_now_iso`), but transcripts written by older builds still
    hold naive ``datetime.now().isoformat()`` rows. Comparing those as STRINGS
    orders them by their text, so on any host that is not UTC a naive
    ``10:00:00`` sorts before an aware ``09:30:00+00:00`` that actually happened
    later — and this merge deletes the source file afterwards, so the wrong order
    is what survives.

    Naive values are interpreted as local time, matching the writer that
    produced them. ``bucket`` keeps unparseable values (bucket 1) after every
    real instant instead of letting a fallback epoch interleave them into the
    middle of the conversation.
    """
    parsed = _parse_transcript_ts(ts)
    if parsed is None:
        return (1, 0.0)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return (0, parsed.timestamp())


def _parse_transcript_ts(ts: str) -> datetime | None:
    """Parse a transcript ``ts`` in either stored format, or ``None``.

    Shared by :func:`transcript_sort_key` and :func:`monotonic_transcript_ts` so
    the two agree on what counts as a readable timestamp: whatever one of them
    can order, the other can stamp against.
    """
    try:
        return datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def metadata_now_iso() -> str:
    """Offset-aware ISO-8601 stamp for a transcript metadata timestamp.

    A transcript's metadata line carries absolute-instant fields --
    ``created_at``, ``updated_at``, ``compacted_at``, ``rotated_at`` -- that are
    read back as points in time, not wall clocks: the dashboard renders a
    session's ``created_at`` as a local-time timestamp, and
    :func:`transcript_sort_key` orders lines against it. A bare
    ``datetime.now().isoformat()`` records naive local wall-clock with no
    offset, so a reader (the browser, or a merge running on another host) has no
    way to know which timezone produced it -- the dashboard then renders it
    verbatim, showing a Slack/channel session's creation time in UTC instead of
    the viewer's local zone (issue #1948). Resolving to an absolute instant with
    ``astimezone()`` records the offset, matching the message-row convention in
    :func:`monotonic_transcript_ts` so both the metadata line and the rows below
    it speak the same, unambiguous format.
    """
    return datetime.now().astimezone().isoformat()


def mint_row_mid() -> str:
    """Mint a durable per-row delivery identity for a transcript row.

    The ONE place the ``meta.mid`` format is spelled. ``_ChatSlot.append`` mints
    the id for a row that enters a dashboard window, and the dashboard
    dual-writers (``cron_inject``, ``workflow_inject``, ``crew_chat``) read it back
    off that append to stamp their durable copy (``row_mid``). A writer with no
    slot to mint from -- a channel dispatcher persisting a turn it ran on its own
    session -- has to mint the id itself, and it must produce the SAME shape,
    because the readers match on the value, not on who wrote it.

    Why a channel row needs one AT WRITE TIME: the dashboard's merge keys on
    ``meta.mid`` and nothing else. ``isRedeliveredMessage`` drops a redelivered row
    by it, ``olderHeadAbovePage`` cuts the retained scrollback head at it, and
    ``rowIdentities``/``tailNotInPage`` decide by it which prior rows a page already
    carries -- and every one of those DECLINES rather than guesses when the id is
    absent or has changed. A row persisted without one is re-minted by each surface
    that materializes it (``channel_slots._rebuild_window`` /
    ``refresh_channel_window``), so one logical row carries a different identity on
    every pass, silently degrading all three at once.

    Random rather than a per-key counter, for the reason ``_ChatSlot.append``
    gives: a counter rebased after a restore can reissue an id a restored row
    already holds, and a colliding id makes a client DROP a real message. A
    random id has no such failure mode.
    """
    return f"m-{uuid.uuid4().hex[:16]}"


def monotonic_transcript_ts(previous: str | None, now: datetime) -> str:
    """Stamp a transcript row so it sorts strictly AFTER *previous*.

    The two rows of one turn -- the message a person sent and the reply to it --
    are written microseconds apart. A host whose clock ticks coarsely returns
    the SAME value for both reads (Windows advances the system clock in ~15.6 ms
    steps), so the pair lands on disk carrying an identical ``ts``. Every
    consumer that orders a transcript by ``ts`` -- the dashboard, and the merge
    built on :func:`transcript_sort_key` -- is then free to render the answer
    above the question that prompted it.

    Returning ``previous`` plus one microsecond when the clock has not advanced
    makes the order a property of the write sequence rather than of the clock's
    resolution. The correction only ever moves a row FORWARD, and only as far as
    the previous row already claims to be, so it cannot push a row past one that
    genuinely happened later. For a coarse clock that is one tick; it is larger
    only when *previous* is a legacy naive value whose lost fold (below) makes it
    read up to one local offset ahead.

    Everything here is compared and emitted as an ABSOLUTE INSTANT, never as a
    wall clock. A local wall clock repeats itself for an hour when daylight
    saving ends, and ``isoformat`` does not record which of the two passes a
    naive value belongs to -- so a naive stamp is not orderable against the
    offset-aware rows the dashboard writes into the same file, and one written
    during the repeat can read back an hour early. A naive *now* is therefore
    resolved to its offset before use, and the returned value always carries
    that offset. A naive *previous* is an older row from before this rule: it is
    read as local time, the same reading :func:`transcript_sort_key` gives it,
    which is the most its lost fold allows. An absent or unparseable *previous*
    yields *now*.
    """
    if now.tzinfo is None:
        now = now.astimezone()
    if previous:
        prior = _parse_transcript_ts(previous)
        if prior is not None:
            if prior.tzinfo is None:
                prior = prior.astimezone()
            if now <= prior:
                now = prior + timedelta(microseconds=1)
    return now.isoformat()


def _safe_key(key: str) -> str:
    """Convert a session key (e.g. Slack thread_ts) to a safe filename."""
    return re.sub(r"[^\w\-.]", "_", key)


def transcript_stem(key: str) -> str:
    """The canonical filename stem *key*'s transcript and archive segments share.

    Exported so callers that account for or reclaim a session's disk usage can
    pair a session key with its files without re-deriving the sanitization. A
    second copy of that rule would drift the moment this one changed, and the
    failure is silent and destructive: the pairing misses, and a caller deleting
    "the session" removes one half and leaves the other behind.

    Prefer :func:`transcript_stems` when the answer feeds a decision about which
    files belong to a session — a Slack thread predating the canonical
    ``slack:<ts>`` key still logs under its bare thread_ts stem, and this function
    alone would not find it.
    """
    return _safe_key(key)


_TAB_ID_INDEX_STEM_PREFIX = "dashboard_chat-"
_TAB_ID_INDEX_GLOB = f"{_TAB_ID_INDEX_STEM_PREFIX}*.jsonl"


def _index_key_for_stem(stem: str) -> str:
    """The key form :attr:`ConversationLog._tab_id_index` stores for *stem*.

    One derivation shared by the index builder (which starts from a filename)
    and the in-place updater (which starts from a session key), because a second
    copy would drift the moment either side changed and the failure is silent:
    the two spellings stop matching, so an updater's lookup misses an entry that
    is really there.
    """
    return stem.replace("_", ":", 1)


def can_hold_tab_id_index_entry(key: str) -> bool:
    """True when *key*'s transcript is one :meth:`_rebuild_tab_id_index` scans.

    The index is built by globbing :data:`_TAB_ID_INDEX_GLOB`, so a transcript
    whose stem does not match can never appear in it -- a channel-keyed session
    (``slack:<ts>`` and friends) writes ``slack_<ts>.jsonl``, which the glob
    never returns. Saving such a transcript therefore cannot add, remove or
    change any index entry, which is what makes a no-op the correct response to
    one rather than an invalidation.
    """
    return transcript_stem(key).startswith(_TAB_ID_INDEX_STEM_PREFIX)


def transcript_stems(key: str) -> tuple[str, ...]:
    """Every filename stem *key*'s transcript could occupy, canonical first.

    :meth:`ConversationLog._path` falls back to the pre-migration bare
    ``thread_ts`` filename for Slack threads that predate the canonical session
    key, so one session key can legitimately resolve to either name. A caller that
    only knew the canonical stem would treat the legacy transcript as belonging to
    no session — and therefore as reclaimable while the session is still
    resumable. Returning both keeps that decision correct without duplicating the
    fallback rule.
    """
    stems = [_safe_key(key)]
    bare = legacy_key(key)
    if bare is not None:
        legacy = _safe_key(bare)
        if legacy not in stems:
            stems.append(legacy)
    return tuple(stems)


def _redact_at_write_boundary(role: str, content: str) -> str:
    """Redact model-authored *content* on its way into a transcript.

    One transcript, one redaction rule. A conversation's dashboard tab and its
    channel thread persist to the same file through different code paths, so the
    rule has to live where the bytes are written rather than in either caller.

    The gate is ``role != "user"``, matching the dashboard's own write-back
    boundary: text the user typed is stored verbatim, and everything the model or
    the system produced is scrubbed of credentials and exfiltration URLs.
    Idempotent, so a caller that already redacted loses nothing by passing
    through here.
    """
    if role == "user":
        return content
    content, _ = redact_exfiltration_urls(content)
    content, _ = redact_credentials(content)
    return content


def latest_transcript_ts(*candidates: str | None) -> str | None:
    """The latest of several candidate predecessor timestamps, or ``None``.

    A writer can have more than one row to order itself against, and the two
    writers of a session transcript learn about predecessors differently:
    :meth:`ConversationLog.append` reads the authoritative file tail under the
    cross-process flock, while the dashboard's ``_ChatSlot.append`` runs on the
    event loop and may only consult in-process state (a ``stat`` plus a file read
    per append is what ``AUTOSDE.yaml``'s ``no-blocking-call-on-event-loop`` rule
    forbids). The slot therefore floors on the later of its in-memory window tail
    and the last on-disk tail it was told about at the previous save.

    Comparison goes through :func:`transcript_sort_key`, never string ordering:
    rows carry two stored formats (aware and naive isoformat), so ``"a" > "b"``
    on the raw strings compares different domains and can pick the earlier row.

    An UNPARSEABLE candidate is skipped rather than ranked. ``transcript_sort_key``
    deliberately buckets a value it cannot parse *after* every real instant, so
    that a corrupt line displays at the end of a transcript rather than in the
    middle of the conversation. That is right for sorting and wrong for a floor:
    ranked, one malformed ``ts`` would win here, and
    :func:`monotonic_transcript_ts` ignores a previous value it cannot parse — so
    a single corrupt row on disk would silently switch the whole ordering
    guarantee off and let the next row tie its predecessor again.

    ``None`` and empty candidates are ignored too, so a caller can pass a value it
    does not have yet without branching. Returns ``None`` when nothing usable was
    supplied — which correctly means "no floor", not "floor of zero".
    """
    best: str | None = None
    best_key: tuple[int, float] | None = None
    for candidate in candidates:
        if not candidate:
            continue
        key = transcript_sort_key(candidate)
        if key[0] != 0:
            # Unparseable: see the docstring. Not a usable floor.
            continue
        if best_key is None or key > best_key:
            best, best_key = candidate, key
    return best


# Metadata reads retry briefly before reporting "no metadata": on Windows a
# just-written session file can be transiently unopenable while an indexer or AV
# scanner holds it (ERROR_SHARING_VIOLATION -> PermissionError). Those holds are
# measured in milliseconds, and the caller that matters here (the open-tab
# restore) treats an empty result as "session never existed" and drops the tab.
_METADATA_READ_ATTEMPTS = 3
_METADATA_READ_RETRY_SECS = 0.02


class ConversationLog:
    """Append-only JSONL conversation store with provenance and rotation."""

    # Per-file reentrant locks shared across every instance in this process so
    # transcript mutations (append / rewrite / metadata edit) targeting the
    # same session file are serialized and never lose each other's writes.
    _file_locks: dict[str, threading.RLock] = {}
    _file_locks_guard = threading.Lock()

    # Cross-process advisory-lock state. The per-file ``threading.RLock`` above
    # only serializes writers *inside this process*; a subagent, cron, or CLI
    # invocation runs in a SEPARATE process and would otherwise interleave its
    # create/append/rotate/rewrite against ours and silently lose updates (or
    # let a reader observe a torn file mid-rewrite). ``_locked`` layers a POSIX
    # ``flock`` (advisory, cross-process) on a per-session ``.lock`` sidecar on
    # top of the in-process RLock. ``_flock_state`` maps lock_key →
    # ``[fd, depth, held]`` (``held`` = 1 while the ``flock`` is currently taken
    # on ``fd``) so re-entrant same-key acquisition (RLock is reentrant) reuses
    # the single held fd instead of ``flock``-ing a second fd of the same file —
    # which, on POSIX, blocks forever against ourselves. Crucially, the entry is
    # *kept alive with ``held``=1* across the brief window between a depth→0 exit
    # and the off-loop release actually running: a sequential same-key
    # re-acquire in that window REUSES the still-held flock (never re-``flock``s
    # a fresh fd, which would spuriously EWOULDBLOCK against our own not-yet-
    # released fd and fail the single non-blocking on-loop attempt). Guarded by
    # ``_flock_guard`` for the (fast, non-blocking) dict bookkeeping only; the
    # blocking ``flock`` call happens outside the guard so distinct keys never
    # serialize on it.
    _flock_state: dict[str, list[int]] = {}
    _flock_guard = threading.Lock()

    # Monotonic count of cross-process flock RELEASES per lock_key, bumped
    # under ``_flock_guard`` when a deferred release actually retires a held
    # flock. Part of the unlocked-fill publish witness
    # (:meth:`_flock_hold_witness`): "held now" at two instants does not prove
    # the hold was CONTINUOUS — the flock could have been released and
    # re-acquired between them with an external process's write in the gap,
    # and ``os.open`` can recycle the fd number, so the fd alone cannot prove
    # continuity either. An unchanged (fd, epoch) pair can: the epoch moves on
    # every release, so equal pairs mean the same unbroken hold. Same growth
    # class as ``_flock_state``.
    _flock_epochs: dict[str, int] = {}

    # Per-key invalidation generation, bumped by ``_invalidate_cache`` BEFORE
    # it drops entries. The mtime guard alone cannot protect a cache FILL:
    # housekeeping rewrites (compaction / rotation / metadata edits /
    # mark_consolidated) restore the pre-write mtime via ``_restore_mtime``,
    # so a fill that stats the file before such a rewrite and publishes after
    # its invalidation would park pre-rewrite data under an mtime the file
    # still has — undetectable for the life of the process. Fill paths
    # snapshot the generation before their stat and publish only while it is
    # unmoved (``_publish_if_current`` for the mtime-keyed memos; the unlocked
    # ``_msg_cache`` fallback in ``_read_messages`` checks it inline alongside
    # the flock-hold witness), discarding the fill otherwise. Class-level for
    # the same reason
    # ``_file_locks`` is: the writer whose lock hold forces a reader onto the
    # unlocked fill may live on a DIFFERENT ``ConversationLog`` instance over
    # the same directory, and its bump must be visible to that reader's
    # snapshot. Keyed by ``(transcript dir, sanitized filename stem)`` — pure
    # string math, so a snapshot costs no I/O, the dir component keeps
    # distinct ``base_dir``s from sharing counters, and the stem (see
    # ``_cache_gen``) makes the logical-key and ``path.stem`` spellings of one
    # session share one counter. ``_cache_gens_guard`` is always innermost:
    # taken under ``_file_lock`` (every writer invalidates while holding it),
    # never the reverse, and never across I/O — so no read path waits on a
    # writer's file operations. Grows one small int per (dir, spelling) ever
    # invalidated in this process — a session can occupy up to two buckets
    # (a legacy bare Slack stem plus its canonical spelling) — the same
    # growth class as ``_file_locks``, and entries are never evicted because
    # a missing entry must always mean "generation 0", not "forgotten bump".
    _cache_gens: dict[tuple[str, str], int] = {}
    _cache_gens_guard = threading.Lock()

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        cache_max: int = _TRANSCRIPT_CACHE_MAX,
    ):
        self._dir = base_dir or _sessions_dir()
        # Bounded, mtime-keyed LRU caches (key → (mtime, payload)). Bounded so
        # a long-lived gateway touching thousands of sessions cannot grow the
        # parsed-transcript working set without limit. Eviction is
        # least-recently-used and deterministic; writes invalidate per-key via
        # _invalidate_cache so a stale entry can never outlive a file change.
        self._msg_cache: _LRUCache[tuple[float, int, list[dict]]] = _LRUCache(cache_max)
        #: ``(mtime, gen, meta)`` — like the search memos, entries record the
        #: invalidation generation and a warm hit requires both fields to
        #: match, so a preserved-mtime metadata edit through another
        #: instance (whose pops cannot reach this cache) still unhits.
        #:
        #: Sized by ``_METADATA_CACHE_MAX``, NOT ``cache_max``: this memo holds one
        #: parsed first line per session rather than a transcript window, and
        #: ``list_sessions`` reads it in a whole-directory cyclic scan that an LRU
        #: smaller than the corpus cannot hit. Same reasoning the search budgets
        #: already use to decline that knob. Deliberately not overridable: a test
        #: that needs a small bound assigns ``_meta_cache`` directly rather than
        #: adding a constructor parameter no product caller uses.
        self._meta_cache: _LRUCache[tuple[float, int, dict]] = _LRUCache(_METADATA_CACHE_MAX)
        #: Bounded, mtime-keyed LRU of formatted ``recent()`` windows keyed by
        #: (key, max_messages, roles). The tail-read fast path intentionally
        #: never warms ``_msg_cache`` (it returns a partial view), so a session
        #: accessed *only* via ``recent()`` — the hot per-turn context path —
        #: would otherwise re-open and re-parse the file tail on every single
        #: call. This memoizes the formatted window; the stored mtime guards
        #: staleness (an append bumps the file mtime, so the entry is
        #: recomputed on the next call). Own ``_LRUCache`` → own internal lock.
        self._recent_cache: _LRUCache[tuple[float, list[dict]]] = _LRUCache(cache_max)
        #: Bounded memo of lightweight message projections containing only
        #: ``ts`` and ``meta.file_changes``. The Artifacts "All" view scans
        #: every session, so routing it through ``_msg_cache`` retains the full
        #: parsed transcript corpus. The file stamp includes inode and size in
        #: addition to nanosecond mtime so rotations and atomic rewrites miss.
        self._file_change_cache: _LRUCache[_FileChangeCacheEntry] = _LRUCache(cache_max)
        #: Bounded memo of ``(mtime, gen, doc_chars, casefolded_blob)`` per
        #: session, consumed only by :meth:`search_sessions`. ``gen`` is the
        #: invalidation generation (:meth:`_cache_gen`) the entry was folded
        #: under; a hit requires BOTH the mtime and the generation to match,
        #: because ``_invalidate_cache``'s pops reach only their own
        #: instance's caches while a preserved-mtime rewrite can be performed
        #: through a different ``ConversationLog`` instance over the same
        #: directory — the generation bump is what unhits such an entry where
        #: the instance-local pop cannot.
        #:
        #: Folding is the dominant cost of a search: the substring count itself
        #: is cheap, but ``str.casefold`` over a whole corpus is not, and it
        #: holds the GIL for its full duration — so re-folding per query stalls
        #: the event loop even when the search runs in a worker thread. The
        #: corpus does not change between the keystrokes of one search, so the
        #: fold is memoized here and each query pays only the count.
        #:
        #: Bounded by BYTES, not entries — see ``_SEARCH_FOLD_BUDGET_BYTES``. The
        #: previous ``max(cache_max, _SEARCH_SCAN_WINDOW)`` entry bound existed to
        #: stop an LRU from collapsing to a zero hit rate against the cyclic scan
        #: order; :class:`_SearchTextCache` keeps that guarantee by refusing
        #: admission instead of evicting, so the sessions that fit stay cached
        #: and the bound is now a real memory ceiling rather than a proxy for one.
        self._folded_cache: _SearchTextCache[tuple[float, int, int, str]] = _SearchTextCache(
            _SEARCH_FOLD_BUDGET_BYTES, lambda v: v[3].__sizeof__(), "fold"
        )
        #: session key → (mtime, gen, raw message texts) for snippet extraction.
        #:
        #: The fold above answers "does this session match"; this answers "show me
        #: the line". Without it every returned row re-opened its file and
        #: re-parsed JSONL until the first hit, which profiling showed to be 92%
        #: of a warm query (55% in ``json.raw_decode`` alone, ~7.2k parses per
        #: query on a 230-session corpus). The cost is not the match count but how
        #: deep the first hit sits, which is why a 21-hit query measured 189 ms
        #: while a 50-hit query measured 81 ms.
        #:
        #: Filled by :meth:`_build_folded`, which already materializes exactly
        #: this list to build the fold — so the second corpus costs one extra
        #: reference, never an extra read. Raw (not folded) because the snippet is
        #: displayed to the user; the fold cannot be reused for it. Carries the
        #: same generation field as ``_folded_cache`` above, for the same
        #: cross-instance reason: both memos are derived from the messages, so
        #: they go stale at exactly the same moment.
        self._snippet_cache: _SearchTextCache[tuple[float, int, list[str]]] = _SearchTextCache(
            _SEARCH_SNIPPET_BUDGET_BYTES,
            lambda v: v[2].__sizeof__() + sum(t.__sizeof__() for t in v[2]),
            "snippet",
        )
        #: tab_id → [session keys] chain index. ``None`` means "stale, rebuild
        #: on next chained read"; a dict is an authoritative snapshot. Rebuilt
        #: lazily by _rebuild_tab_id_index, invalidated by
        #: invalidate_tab_id_cache on every append / metadata edit / delete
        #: that can change the chain. Guarded by ``self._lock`` because
        #: ``read_messages_chained`` is reachable from worker threads while the
        #: event loop may mark it stale — an unsynchronized rebuild/read/clear
        #: produced a transient empty index or ``AttributeError``.
        self._tab_id_index: dict[str, list[str]] | None = None
        #: session key → (mtime, tab_id) memo feeding the rebuild above.
        #: Deliberately an unbounded plain dict, NOT an _LRUCache: the rebuild is
        #: a cyclic scan over every dashboard file, and a bounded cache under a
        #: cyclic scan larger than the bound has a 0% hit rate (see
        #: _SearchTextCache's docstring). Values are 12-char ids, so 1k sessions
        #: is tens of KB.
        #:
        #: TWO guards, and neither is sufficient alone. The explicit pop in
        #: _invalidate_cache covers writes THROUGH this class from THIS instance:
        #: those restore the pre-write mtime (_restore_mtime), so a stamp alone
        #: would not see them. The stamp covers rewrites that never reach that
        #: pop -- a hand-edited tab_id, or a write through ANOTHER instance,
        #: whose pop lands on its own memo and leaves ours intact.
        #:
        #: The stamp is (st_mtime_ns, st_size, st_ino), all from one stat. Size
        #: rides along because timestamp granularity is coarse (worse on
        #: Windows). ns rather than float seconds, and st_ino as well, because
        #: another instance's equal-length tab_id rewrite preserves mtime and
        #: size both -- see the cross-instance test.
        self._tab_id_by_key: dict[str, tuple[tuple[int, int, int], str]] = {}
        #: Bumped by _invalidate_cache. The rebuild samples it before reading a
        #: file's metadata and declines to memoize if it moved, so a store cannot
        #: land after a concurrent writer's pop and resurrect a stale id.
        #: _invalidate_cache deliberately does not take self._lock, so the
        #: rebuild cannot exclude it.
        self._tab_id_generation = 0
        #: Coarse instance lock protecting the lazily-built ``_tab_id_index``
        #: rebuild/read/clear. The message/metadata/recent LRUs are each
        #: internally locked; this guards the shared mutable state that lives
        #: directly on the instance.
        self._lock = threading.RLock()
        #: When True, ``recent``/``recent_chained`` may satisfy a cache miss by
        #: reading only the TAIL of the session file instead of parsing the
        #: whole thing. Correctness-neutral — see
        #: :meth:`_read_tail_messages`.
        self._tail_reads = True
        self._cache_coordinator = HistoryCacheCoordinator(
            self,
            safe_key=lambda key: _safe_key(key),
            registry_owner=ConversationLog,
        )
        self._catalog_projection = SessionCatalogProjection(self)
        self._read_projection = TranscriptReadProjection(self)
        self._metadata_projection = SessionMetadataProjection(self)
        self._rewrite_coordinator = HistoryRewriteCoordinator(self)

    def _file_lock(self, key: str) -> threading.RLock:
        """Return the process-wide reentrant lock guarding *key*'s session file.

        Reentrant so a locked method (e.g. ``append``) can call another locked
        helper (``_maybe_rotate``) without deadlocking.
        """
        lock_key = str(self._path(key))
        with ConversationLog._file_locks_guard:
            lock = ConversationLog._file_locks.get(lock_key)
            if lock is None:
                lock = threading.RLock()
                ConversationLog._file_locks[lock_key] = lock
            return lock

    def _lock_path(self, key: str) -> Path:
        """Sidecar lock-file path for *key* (``<session>.jsonl.lock``).

        A dedicated sidecar is used rather than locking the session file's own
        fd because writes go through ``atomic_write`` (temp file + ``os.replace``),
        which swaps the inode — a lock held on the pre-replace fd would guard a
        now-unlinked inode and protect nothing. The sidecar's inode is stable.
        The ``.lock`` suffix keeps it out of every ``*.jsonl`` glob (list/search/
        tab-id-index) so it is never mistaken for a session file.
        """
        p = self._path(key)
        return p.parent / (p.name + ".lock")

    @staticmethod
    def _run_fd_cleanup_off_loop(cleanup: Callable[[], None]) -> None:
        """Run a blocking lock-fd cleanup without ever stalling the event loop.

        ``platform_compat.release_lock`` (``flock(LOCK_UN)``) and ``os.close``
        are ``blocking: true`` under the ``no-blocking-call-on-event-loop``
        rule: on a wedged descriptor they can freeze chat, WebSockets, and the
        heartbeat until the watchdog restarts the gateway. ``_locked`` may run
        synchronously ON the loop thread — an on-loop caller that did not
        offload relies on the single non-blocking acquire as a safety net (see
        the module docstring and ``append_off_loop``) — so its descriptor
        release/close must never execute inline on the loop.

        Off the loop we run the cleanup inline. On the loop we dispatch it to
        the default executor (fire-and-forget: the caller awaits nothing). The
        release cleanup re-validates the ``_flock_state`` entry under the
        per-key RLock before touching the fd, so scheduling it while the entry
        is still live (kept alive with ``held``=1 across the depth→0 → release
        window) is safe — a same-key re-acquire simply cancels it. A failed
        unlock/close of an already-doomed fd is not actionable, so the future's
        exception is consumed to avoid a spurious "exception was never
        retrieved" warning.

        NOTE: this is only the *release* side. The first-entry acquire still
        ``os.open``s the local sidecar synchronously because the fd must exist
        before it can be ``flock``ed; that open is a fast local-FS syscall and,
        unlike the release, is on the critical path. On-loop callers that want
        to keep even the acquire off the loop must offload the whole mutation
        (``append_off_loop`` / ``update_metadata_off_loop`` / ``save_slot_off_loop``).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            cleanup()
            return
        fut = loop.run_in_executor(None, cleanup)
        fut.add_done_callback(lambda f: f.exception())

    @contextlib.contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        """Hold BOTH the in-process RLock and a cross-process advisory flock.

        Serializes create/append/rotate/rewrite/metadata mutations of a single
        session file against every other writer — threads in this process (via
        the RLock) *and* other processes such as subagents, crons, and the CLI
        (via the ``flock`` on the sidecar lock file). Reentrant: a nested
        ``_locked`` for the same key on the same thread reuses the held fd.
        """
        # Fail loud (strict) or diagnose (production) if a mutation reached the
        # lock ON the event loop — the un-offloaded-call-site guard (see
        # OnLoopPersistError). No-op off the loop, which is the sanctioned path.
        _check_on_loop_persist_discipline(key)
        with self._file_lock(key):
            lock_key = str(self._path(key))
            with ConversationLog._flock_guard:
                state = ConversationLog._flock_state.get(lock_key)
                if state is None:
                    lock_path = self._lock_path(key)
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                    state = [fd, 0, 0]  # fd, depth, held
                    ConversationLog._flock_state[lock_key] = state
                state[1] += 1
                # ``held`` == 1 means the ``flock`` is already taken on
                # ``state[0]`` — either by an outer reentrant frame OR because a
                # depth→0 exit's off-loop release has not run yet. In BOTH cases
                # we reuse the held flock rather than ``flock``-ing a fresh fd
                # (which would EWOULDBLOCK against our own fd and, on the loop,
                # fail the single non-blocking attempt). The re-bumped depth
                # also cancels any pending release (it re-checks depth == 0).
                need_acquire = state[2] == 0
            # Bounded cross-process acquire done OUTSIDE _flock_guard (only when
            # the flock is not already held) so unrelated keys never serialize
            # on the bookkeeping mutex. The RLock we hold guarantees no other
            # thread races this same key. On timeout/contention we RAISE
            # HistoryLockTimeout — we never proceed with the mutation unlocked,
            # because a concurrent rewrite could then clobber the unlocked write
            # (silent data loss). ON a running asyncio loop we make exactly ONE
            # non-blocking attempt and fail fast (never sleep/poll — that would
            # block the sole event loop). Off-loop we poll patiently to a
            # bounded deadline.
            if need_acquire:
                try:
                    on_loop = True
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        on_loop = False
                    if on_loop:
                        if not platform_compat.try_acquire_lock(state[0], exclusive=True):
                            logger.warning(
                                "history: cross-process lock for %s busy on the "
                                "event loop; abandoning mutation rather than "
                                "blocking the loop or writing unlocked (a "
                                "concurrent rewrite could clobber it)",
                                key,
                            )
                            raise HistoryLockTimeout(
                                f"could not acquire cross-process lock for "
                                f"{key!r} without blocking the event loop"
                            )
                    else:
                        deadline = _time.monotonic() + _FLOCK_ACQUIRE_TIMEOUT_S
                        while not platform_compat.try_acquire_lock(state[0], exclusive=True):
                            if _time.monotonic() >= deadline:
                                logger.warning(
                                    "history: cross-process lock for %s not "
                                    "acquired within %.1fs; abandoning mutation "
                                    "to avoid an unlocked write that a "
                                    "concurrent rewrite could clobber",
                                    key,
                                    _FLOCK_ACQUIRE_TIMEOUT_S,
                                )
                                raise HistoryLockTimeout(
                                    f"could not acquire cross-process lock for "
                                    f"{key!r} within "
                                    f"{_FLOCK_ACQUIRE_TIMEOUT_S:.1f}s"
                                )
                            _time.sleep(_FLOCK_POLL_INTERVAL_S)
                    with ConversationLog._flock_guard:
                        state[2] = 1  # flock now held on state[0]
                except BaseException:
                    with ConversationLog._flock_guard:
                        state[1] -= 1
                        # Only tear the fd down if we are the last frame AND the
                        # flock was never taken; a concurrent reuse (depth > 0)
                        # or a held flock must keep the fd alive.
                        drop = state[1] == 0 and state[2] == 0
                        if drop:
                            ConversationLog._flock_state.pop(lock_key, None)
                    if drop:
                        # Acquire failed → the flock was never taken, so only
                        # the fd needs releasing. ``os.close`` is ``blocking:
                        # true`` under the no-blocking-call-on-event-loop rule;
                        # defer it off the loop (see ``_run_fd_cleanup_off_loop``).
                        _fd = state[0]
                        self._run_fd_cleanup_off_loop(lambda: os.close(_fd))
                    raise
            try:
                yield
            finally:
                with ConversationLog._flock_guard:
                    state[1] -= 1
                    done = state[1] == 0
                if done:
                    # Depth hit 0. ``platform_compat.release_lock`` (flock
                    # LOCK_UN) and ``os.close`` are both ``blocking: true``
                    # syscalls, so run them off the event loop — a wedged
                    # descriptor must never freeze chat/WS/heartbeat (the
                    # finding this addresses). We DO NOT pop the state here:
                    # the entry stays alive with ``held``=1 so a sequential
                    # same-key re-acquire before the release runs reuses the
                    # still-held flock instead of ``flock``-ing a fresh fd (the
                    # regression that spuriously raised HistoryLockTimeout under
                    # executor load). The deferred release re-checks depth and
                    # its own fd under the guard, so a reuse cancels it.
                    self._schedule_flock_release(key, lock_key, state[0])

    @contextlib.contextmanager
    def atomic_appends(self, key: str) -> Iterator[None]:
        """Group several appends to one session into ONE indivisible write.

        :meth:`append` locks per ROW, so two callers each writing a
        user+assistant PAIR can interleave into ``user_A, user_B, assistant_A,
        assistant_B`` -- a transcript whose turns no longer pair up, and which no
        timestamp ordering can repair because every row's ``ts`` is correct.

        The hazard is specific to concurrent writers. A caller running ON the
        event loop could not hit it: ``save_conversation_turn`` never awaits
        between its two appends, so the single-threaded loop made the pair
        effectively atomic. It appears exactly when a caller does the right thing
        and moves the write OFF the loop, because two worker threads then run
        those pairs concurrently. So this is the companion any multi-append
        caller needs alongside the offload, not an optional extra.

        ``_locked`` is reentrant for the same key on the same thread, so the
        per-row locks inside ``append`` reuse the lock held here rather than
        deadlocking on it.

        Enter this OFF the event loop. It takes the same acquire path as
        ``append``, which fails fast with :class:`HistoryLockTimeout` on a
        running loop rather than blocking it.
        """
        with self._locked(key):
            yield

    def _schedule_flock_release(self, key: str, lock_key: str, fd: int) -> None:
        """Release+close *fd* off the loop iff the entry is still idle.

        Scheduled on a depth→0 exit of :meth:`_locked`. Runs the blocking
        ``flock(LOCK_UN)`` + ``os.close`` off the event loop (they can stall on
        a wedged descriptor). Re-validates under the per-key RLock and
        ``_flock_guard`` that the ``_flock_state`` entry still refers to *fd*
        and its depth is still 0 — if a same-key re-acquire bumped the depth in
        the meantime (reusing the still-held flock) or the fd was already
        replaced, the release is a no-op and ownership passes to that frame's
        own eventual depth→0 exit. This closes the window in which the entry was
        popped while the flock was still held, which made the next on-loop
        acquire EWOULDBLOCK against our own not-yet-released fd.
        """

        def _release_and_close() -> None:
            # Reentrant RLock: off-loop this runs on a worker thread and blocks
            # until no same-key frame holds it; inline (no loop) it re-enters on
            # the current thread. Either way, serializing with _locked bodies
            # guarantees the depth check below cannot race an acquire.
            with self._file_lock(key):
                with ConversationLog._flock_guard:
                    st = ConversationLog._flock_state.get(lock_key)
                    if st is None or st[0] != fd or st[1] != 0:
                        return  # reused or replaced — leave the flock in place
                    ConversationLog._flock_state.pop(lock_key, None)
                    # The hold is over: advance the release epoch so an
                    # unlocked fill's witness (:meth:`_flock_hold_witness`)
                    # spanning this release can no longer claim a continuous
                    # hold, even if a re-acquire lands on a recycled fd number.
                    ConversationLog._flock_epochs[lock_key] = (
                        ConversationLog._flock_epochs.get(lock_key, 0) + 1
                    )
                try:
                    platform_compat.release_lock(fd)
                finally:
                    os.close(fd)

        self._run_fd_cleanup_off_loop(_release_and_close)

    def init(self) -> None:
        """Create sessions directory if missing."""
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = self._dir / f"{_safe_key(key)}.jsonl"
        if not p.exists():
            # Back-compat: Slack threads created before the canonical
            # ``slack:<ts>`` session-key migration logged under the bare
            # thread_ts filename. Keep reading/appending the legacy file for
            # those threads so a thread active across the migration doesn't
            # split its log; brand-new threads create the canonical file.
            bare = legacy_key(key)
            if bare is not None:
                legacy = self._dir / f"{_safe_key(bare)}.jsonl"
                if legacy.exists():
                    return legacy
        return p

    def has_log(self, key: str) -> bool:
        """Return True if a conversation log file exists for *key*."""
        return self._path(key).exists()

    def session_mtime(self, key: str) -> float | None:
        """Return the session file's mtime, or None if it can't be stat'd.

        Advances on every real message :meth:`append` but is preserved across
        metadata-only writes (see :func:`_restore_mtime`), so it is a faithful
        "has this conversation changed?" signal — used as the cache-validity
        signature for derived artifacts like on-demand session summaries.
        """
        return _safe_mtime(self._path(key))

    def _summary_cache_path(self, key: str) -> Path:
        """Sidecar path for a session's cached one-line summary."""
        return self._dir / ".summaries" / f"{_safe_key(key)}.json"

    def get_cached_summary(self, key: str) -> str | None:
        """Return the cached one-line summary for *key* if still valid.

        Summaries are cached in a sidecar file — never in the session JSONL —
        so summarizing a session never rewrites (and therefore never risks
        clobbering, via a read-modify-write race with :meth:`append`) its
        conversation log. The cache is valid only while the session file's
        mtime matches the signature recorded when the summary was generated;
        any real append advances the mtime and invalidates it.
        """
        try:
            data = json.loads(self._summary_cache_path(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        summary = data.get("summary")
        sig = self.session_mtime(key)
        if (
            sig is not None
            and data.get("sig") == sig
            and data.get("gen", 0) == self.rotation_generation(key)
            and isinstance(summary, str)
        ):
            return summary
        return None

    def set_cached_summary(
        self, key: str, summary: str, sig: float, generation: int | None = None
    ) -> None:
        """Persist a derived one-line *summary* to the sidecar cache.

        Keyed by the session file's mtime *sig* so a later append invalidates
        it. Atomic and side-effect-free with respect to the session JSONL —
        no read-modify-write, hence no data-loss race with a concurrent
        :meth:`append`.

        *generation* is :meth:`rotation_generation` captured at the same moment
        as *sig*, and must come from the caller for the same reason *sig* does:
        summary generation holds no lock while the model call is in flight, and
        a rewrite landing in that window preserves the mtime while advancing the
        generation. Reading the generation HERE would stamp the new content's
        identity onto the old summary and bless it as fresh — the exact
        staleness the generation was added to catch. ``None`` reads it at write
        time, which is only safe when no snapshot preceded the call.
        """
        atomic_write(
            self._summary_cache_path(key),
            json.dumps(
                {
                    "sig": sig,
                    "gen": (self.rotation_generation(key) if generation is None else generation),
                    "summary": summary,
                }
            ),
        )

    def _intent_summary_cache_path(self, key: str) -> Path:
        """Sidecar path for a session's cached intent-structured summary.

        Deliberately a different file from :meth:`_summary_cache_path`: the
        one-line summary and the intent summary have independent writers and
        independent triggers, and sharing one file would reintroduce the
        read-modify-write race the sidecar design exists to avoid.
        """
        return self._dir / ".intents" / f"{_safe_key(key)}.json"

    def get_cached_intent_summary(self, key: str) -> dict | None:
        """Return the cached intent summary payload for *key* if still valid.

        Same mtime-signature contract as :meth:`get_cached_summary`: any real
        append advances the session file's mtime and invalidates the cache,
        while metadata-only rewrites preserve it. Returns the whole payload so
        the caller can read ``generated_at`` for display.
        """
        try:
            raw = self._intent_summary_cache_path(key).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("intents"), list):
            return None
        sig = self.session_mtime(key)
        if sig is None or data.get("sig") != sig:
            return None
        if data.get("gen", 0) != self.rotation_generation(key):
            return None
        return data

    def read_intent_summary(self, key: str) -> tuple[dict | None, bool]:
        """Return ``(payload, stale)`` for a session's intent summary.

        Unlike :meth:`get_cached_intent_summary`, this does not discard a
        payload whose signature no longer matches — it reports it as stale
        instead. The panel prefers showing the last known summary marked as
        out of date over showing nothing, because an empty panel reads as
        "this feature is broken" while a stale one reads as "not regenerated
        yet", which is the truth.
        """
        try:
            raw = self._intent_summary_cache_path(key).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None, False
        if not isinstance(data, dict) or not isinstance(data.get("intents"), list):
            return None, False
        sig = self.session_mtime(key)
        fresh = (
            sig is not None
            and data.get("sig") == sig
            and data.get("gen", 0) == self.rotation_generation(key)
        )
        return data, not fresh

    def set_cached_intent_summary(
        self, key: str, payload: dict, sig: float, generation: int | None = None
    ) -> bool:
        """Persist a derived intent summary *payload* to its sidecar cache.

        Writes only the sidecar, never the session JSONL, so generating a
        summary cannot clobber the transcript or advance its mtime (which would
        both invalidate every other derived cache and reorder ``list_sessions``).

        The write happens under ``_locked`` and only if the transcript still
        exists with the *same* signature the generation started from. Generation
        holds no lock while the model call is in flight (it can take tens of
        seconds), so a permanent :meth:`delete_session` can complete in that
        window -- removing the transcript AND this sidecar. An unconditional
        write here would then recreate the sidecar, resurrecting deleted
        conversation data after the user was told it was gone. The sig equality
        check also drops a summary that a mid-generation append has already made
        stale, rather than storing it as the latest word.

        Returns True when the payload was written, False when it was refused
        (transcript deleted or changed, or the lock could not be acquired).
        Callers run this off the event loop (``asyncio.to_thread``) because
        ``_locked`` blocks.
        """
        try:
            with self._locked(key):
                if _safe_mtime(self._path(key)) != sig:
                    return False
                current_generation = self.rotation_generation(key)
                if generation is not None and current_generation != generation:
                    # A rewrite landed while the model call was in flight. It
                    # PRESERVED the mtime, so the check above cannot see it —
                    # the generation is the only signal that the summary now
                    # describes replaced content. Refuse for the same reason a
                    # changed mtime is refused: storing it would record a known
                    # stale payload as the latest word.
                    return False
                atomic_write(
                    self._intent_summary_cache_path(key),
                    json.dumps(
                        {
                            **payload,
                            "sig": sig,
                            "gen": (current_generation if generation is None else generation),
                        }
                    ),
                )
                return True
        except HistoryLockTimeout:
            logger.warning("set_cached_intent_summary: lock timeout, not writing key=%s", key)
            return False

    def append(
        self,
        key: str,
        role: str,
        content: str,
        tools: list[str] | None = None,
        source_thread: str | None = None,
        source_user: str | None = None,
        agent: str | None = None,
        tab_id: str | None = None,
        cls: str = "",
        mid: str | None = None,
    ) -> None:
        """Append a message with optional provenance to the session log.

        *cls* persists the message's presentation class. The in-memory slot
        carries one (``_ChatSlot.append``) but this durable copy had nowhere to
        put it, so any class-borne distinction silently vanished the moment a
        session's rows had to be replayed from disk after a restart.

        *mid* persists the row's delivery identity as ``meta.mid`` — the SAME
        field shape the dashboard slot save writes
        (``chat_persistence._build_message_entry`` copies the window row's
        ``meta`` dict to disk). A dual-writer that reflects a message in the
        in-memory slot (``_ChatSlot.append``, which mints the id) AND persists
        it here must pass that minted id, so both copies carry one identity and
        the bounded-read reconciliation (``_append_unflushed_tail``'s
        ``meta.mid`` walk) recognises the durable copy instead of treating the
        window copy as still owed. Optional: a row appended without one carries
        no ``meta`` at all, which is what pre-id transcripts hold — readers keep
        their id-less fallback for exactly those rows.

        If the session file does not yet exist, it will be created with an
        initial metadata line.  When *agent* is supplied, the agent name is
        recorded in that metadata so the session can be resumed under the
        correct agent later.  (Has no effect if the file already exists;
        use :meth:`update_metadata` to change the agent after creation.)
        """
        path = self._path(key)
        # Serialize the create-if-missing + append + rotate against concurrent
        # rewrites (compaction / consolidation) so no write is lost and readers
        # never observe a torn file. ``_locked`` also takes a cross-process
        # advisory flock so a subagent / cron / CLI writing the SAME session
        # file in another process can't interleave and lose this append.
        with self._locked(key):
            created_with_tab_id = False
            created_now = False
            if not path.exists():
                created_now = True
                self._dir.mkdir(parents=True, exist_ok=True)
                meta: dict = {
                    "_type": "metadata",
                    "created_at": metadata_now_iso(),
                    "last_consolidated": 0,
                }
                if agent:
                    meta["agent"] = agent
                if tab_id:
                    meta["tab_id"] = tab_id
                    created_with_tab_id = True
                path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

            msg: dict = {
                "role": role,
                "content": _redact_at_write_boundary(role, content),
                **({"cls": cls} if cls else {}),
                # Strictly after the row already on disk, so the pair written by
                # one turn stays ordered on a host whose clock cannot separate
                # them (see monotonic_transcript_ts). Consulting the file here is
                # authoritative because ``_locked`` also holds the cross-process
                # flock: no writer, in this process or another, can append
                # between this look and the write below. A file this call just
                # created provably holds no rows yet, so it is not consulted.
                #
                # ``astimezone()`` resolves the clock to an absolute instant
                # before it is stored. This used to record a bare local wall
                # clock, which repeats for an hour when daylight saving ends and
                # cannot be ordered against the offset-aware rows the dashboard
                # writes into this same file.
                "ts": monotonic_transcript_ts(
                    None if created_now else self._last_row_ts(key),
                    datetime.now().astimezone(),
                ),
            }
            if tools:
                msg["tools"] = tools
            if source_thread:
                msg["source_thread"] = source_thread
            if source_user:
                msg["source_user"] = source_user
            if isinstance(mid, str) and mid:
                # ``meta`` holding ``mid`` is the identity shape every reader of
                # this file already matches on (the slot save writes it, the
                # bounded-read walk consumes it); a second spelling would be
                # invisible to both. Only a non-empty ``str`` counts, matching
                # the read side — persisting any other shape would store an id
                # the reader is structurally unable to honour.
                msg["meta"] = {"mid": mid}

            # Session transcripts are intentionally local plaintext JSONL (the
            # documented storage format), not a credential/secret store.
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")  # lgtm[py/clear-text-storage-sensitive-data]

            # Invalidate cache since file changed
            self._invalidate_cache(key)

            # A newly-created file carrying a tab_id adds a fresh key to that
            # tab_id's chain — drop the cached tab_id→[keys] index so the next
            # chained read rebuilds it and actually sees this session (a stale
            # index would silently omit it from the chain).
            if created_with_tab_id:
                self.invalidate_tab_id_cache()

            # Rotate if file exceeds size limit. Pass the logical key so the
            # rotation's invalidation reaches every cache-key spelling; the
            # file stem alone cannot recover it (sanitization is lossy).
            self._maybe_rotate(path, key)

    def append_if_absent(
        self,
        key: str,
        role: str,
        content: str,
        *,
        agent: str | None = None,
        tab_id: str | None = None,
        cls: str = "",
        mid: str | None = None,
    ) -> bool:
        """Append a message only if an identical one is not already persisted.

        Returns ``True`` if the message was written, ``False`` if it is already
        on disk — judged by ``(role, content)`` when the caller supplies no
        *mid*, and by ``(role, content)`` plus the SAME ``meta.mid`` when it
        does (see below).

        The disk check and the append run together under ``_locked`` so they
        are ATOMIC against a concurrent writer of the same session file — in
        particular the periodic dashboard slot save
        (``_save_slot_to_history``), which serializes the in-memory slot window
        (already carrying this message via ``slot.append``) and takes the SAME
        per-session cross-process lock. Without this, a fire-and-forget
        :func:`append_off_loop` scheduled after the slot save has already
        written the identical message would append it a SECOND time; the
        duplicate then survives a restart and is replayed twice to subsequent
        agent turns. This is the workflow-result / cron-result double-append
        race: the read-modify-write must be one locked critical section, not a
        separate unlocked existence check followed by a later append.

        What counts as "already persisted" depends on whether the caller holds
        an identity. Without *mid*, any row with the same ``(role, content)``
        does — body equality is all an id-less writer can check. WITH *mid*,
        only a body-equal row carrying the SAME ``meta.mid`` does: that row is
        this very message, landed by the slot save or an earlier attempt of
        this write. A body-equal row under another id (or none) is a DIFFERENT
        occurrence that happens to repeat the text — an id-carrying twin of an
        earlier injection, or a pre-id legacy row — and skipping on it would
        drop THIS occurrence's only durable copy: the in-memory window is lost
        on restart, so nothing would replay the newer message.
        """
        supplied_mid = mid if isinstance(mid, str) and mid else None
        with self._locked(key):
            if self._path(key).exists():
                # Compare against the form ``append`` actually stores: the
                # write boundary redacts non-user content, so matching on the
                # raw text would never recognise an already-persisted message
                # that contained a credential and would append it twice.
                persisted = _redact_at_write_boundary(role, content)
                for m in self._read_messages(key):
                    if m.get("role") != role or m.get("content") != persisted:
                        continue
                    if supplied_mid is None:
                        return False
                    m_meta = m.get("meta")
                    if isinstance(m_meta, dict) and m_meta.get("mid") == supplied_mid:
                        return False
            # Reentrant: ``append`` re-enters ``_locked`` for the same key on
            # this thread (RLock + refcounted flock), so the write stays inside
            # the critical section we already hold. The skip paths above leave
            # the persisted rows untouched — an id is never retrofitted onto a
            # row already on disk.
            self.append(key, role, content, agent=agent, tab_id=tab_id, cls=cls, mid=mid)
            return True

    def recent(
        self,
        key: str,
        max_messages: int = 20,
        roles: AbstractSet[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        return self._read_projection.recent(key, max_messages, roles, exclude_last_n=exclude_last_n)

    def recent_chained(
        self,
        key: str,
        max_messages: int = 20,
        roles: AbstractSet[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        return self._read_projection.recent_chained(
            key, max_messages, roles, exclude_last_n=exclude_last_n
        )

    def recent_with_provenance(
        self,
        key: str,
        max_messages: int = 3,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        return self._read_projection.recent_with_provenance(
            key, max_messages, exclude_last_n=exclude_last_n
        )

    def get_unconsolidated(self, key: str) -> tuple[list[dict], int]:
        """Return (messages_after_last_consolidated, total_message_count)."""
        messages = self._read_messages(key)
        offset = self._read_metadata(key).get("last_consolidated", 0)
        return messages[offset:], len(messages)

    def rotation_generation(self, key: str) -> int:
        """Return the session's content-identity counter for *key*.

        Advanced by every write that makes the transcript's messages a DIFFERENT
        body of content than a consolidation may have snapshotted: a rotation
        (:meth:`_maybe_rotate`), a dashboard rewrite save (regenerate / rewind /
        fork), and a channel transcript merge. A consolidator snapshots it
        alongside the message offset before its (slow) LLM call and passes it back
        to :meth:`mark_consolidated`, which refuses to apply the offset whenever
        the generation changed — closing the change-during-await race for ANY
        retained count, including an edit that leaves the count untouched. It is
        also the span identity the retry accounting is stamped against
        (:meth:`_attempts_describe_current_span`), so the same bump releases a
        budget charged against the superseded content.

        Named for the rotation that first needed it; the field is now the general
        content-identity counter. Absent field (legacy metadata / never advanced)
        reads as ``0``.
        """
        return int(self._read_metadata(key).get("rotation_generation", 0) or 0)

    def snapshot_for_consolidation(self, key: str) -> tuple[list[dict], int, int]:
        """Atomically snapshot ``(unconsolidated_messages, total, generation)``.

        The consolidator needs the unconsolidated tail, the total message count
        (the absolute offset it later passes to :meth:`mark_consolidated`), and
        the rotation generation counter to reflect the SAME point in time. Read
        as three separate calls (``get_unconsolidated`` + ``rotation_generation``)
        an append can trigger a rotation *between* them, pairing pre-rotation
        messages/offset with the post-rotation generation. ``mark_consolidated``
        then sees matching generations and applies the stale (shifted) offset —
        and when the retained count is >= that offset the count fallback misses
        it too — silently marking never-processed messages as consolidated and
        dropping them from memory/history extraction.

        Holding :meth:`_locked` across all three reads makes the snapshot
        atomic: no append/rotation can interleave, so the returned offset and
        generation are guaranteed consistent. Returns
        ``(messages[offset:], len(messages), generation)``. The returned list is
        a fresh slice (never the shared ``_read_messages`` cache object), so the
        caller may treat it as owned.
        """
        with self._locked(key):
            messages = self._read_messages(key)
            meta = self._read_metadata(key)
            offset = meta.get("last_consolidated", 0)
            generation = int(meta.get("rotation_generation", 0) or 0)
            return list(messages[offset:]), len(messages), generation

    def mark_consolidated(self, key: str, offset: int, generation: int | None = None) -> None:
        """Rewrite metadata line with updated ``last_consolidated`` offset.

        *offset* is an absolute message index captured by the caller BEFORE a
        (potentially slow) LLM consolidation call. *generation* is the rotation
        generation counter (:meth:`rotation_generation`) captured at the same
        moment. It advances on anything that changes the content under a
        consolidation in flight, and each case makes the caller's *offset*
        meaningless in a different way:

        * A **rotation** truncated the file to its newest messages and reset
          ``last_consolidated`` to 0, so every surviving index shifted by the
          number of dropped lines and applying the offset would mark
          never-consolidated retained messages as processed.
        * A **transcript edit** (the dashboard regenerate / rewind / fork save)
          replaced the live window's tail with content this turn never read. The
          message count, the marker and the extent can all be unchanged, so the
          offset still *looks* applicable — and applying it would mark the
          REPLACEMENT tail consolidated without ever extracting it.

        Both are silent memory loss, and the generation is what distinguishes
        them from a turn whose span is still intact.

        Detection uses two independent signals:

        1. **Generation change** (primary, when *generation* is supplied):
           anything that changes the content between snapshot and write bumps the
           counter, so a mismatch resets ``last_consolidated`` to 0. This closes
           both cases a pure offset-vs-count heuristic misses — a rotation that
           keeps >= *offset* messages, and an edit that keeps the count identical,
           each leave ``offset <= msg_count`` true.
        2. **Offset exceeds current count** (fallback, always): the file shrank
           below the captured offset (rotation truncated it). Retained if
           *generation* is unavailable (legacy callers) or as defense-in-depth.

        In either case ``last_consolidated`` is reset to 0 and the retained tail
        is reconsolidated rather than clamping the offset to EOF (which would
        silently mark post-rotation messages as already consolidated and drop
        them from memory/history extraction). When neither trips, the offset is
        applied as-is.
        """
        path = self._path(key)
        # Serialize behind the cross-process lock and re-read under it so a
        # concurrent append (in this or another process) is never lost.
        with self._locked(key):
            if not path.exists():
                return
            prev_mtime = _safe_mtime(path)
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not lines:
                return
            meta = json.loads(lines[0])
            # Reconcile the caller's absolute *offset* against the messages
            # actually present now. Line 0 is the metadata line; every other
            # non-blank line is a message.
            msg_count = sum(1 for ln in lines[1:] if ln.strip())
            current_generation = int(meta.get("rotation_generation", 0) or 0)
            if generation is not None and current_generation != generation:
                # PRIMARY signal: the content under this consolidation changed
                # between the caller's snapshot and now (the generation counter
                # advanced) — a rotation, or a dashboard rewrite that swapped the
                # live window's tail. Either way the offset cannot be applied.
                # After a rotation it is in the stale PRE-rotation numbering
                # (every surviving index shifted by the number of dropped lines);
                # after an edit the numbering still fits but the messages it would
                # mark are the REPLACEMENT tail, which no turn has read. Neither
                # is caught by the count heuristic below: a rotation that kept
                # >= *offset* messages and an edit that kept the count identical
                # both leave ``offset <= msg_count`` true, and marking either
                # would drop never-consolidated content from memory/history
                # extraction. Reset to 0 and reconsolidate the current tail
                # (harmless, idempotent) rather than risk that loss.
                logger.warning(
                    "mark_consolidated: rotation generation changed %s->%d for "
                    "%s (rotation or transcript edit during consolidation); "
                    "resetting last_consolidated to 0 to avoid marking content "
                    "no consolidation turn read",
                    generation,
                    current_generation,
                    key,
                )
                safe_offset = 0
            elif offset > msg_count:
                # The file shrank below the captured offset — a rotation fired
                # during the (slow) LLM await, truncating to the newest messages
                # and resetting ``last_consolidated`` to 0. The caller's offset
                # is in the PRE-rotation numbering and is now meaningless.
                # Clamping it to ``msg_count`` would mark the retained tail —
                # which now includes brand-new, never-consolidated messages that
                # arrived after the snapshot — as consolidated, permanently
                # skipping them (silent history/memory loss). Reset to 0 and let
                # the retained tail be reconsolidated instead: redoing a handful
                # of already-processed messages is harmless and idempotent,
                # whereas dropping new ones is a data-integrity failure.
                logger.warning(
                    "mark_consolidated: offset %d exceeds current message count "
                    "%d for %s (rotation during consolidation); resetting "
                    "last_consolidated to 0 to avoid skipping post-rotation "
                    "messages",
                    offset,
                    msg_count,
                    key,
                )
                safe_offset = 0
            else:
                safe_offset = offset
            meta["last_consolidated"] = safe_offset
            meta["updated_at"] = metadata_now_iso()
            # The marker is the success signal for the retry accounting written
            # by record_consolidation_failure: once a span is marked, its failed
            # attempts and backoff deadline describe a span that no longer
            # exists, and leaving them behind would charge the NEXT span for this
            # one's failures. Dropped in the same locked write so no window
            # exists where the marker is applied but the budget is not released.
            #
            # Only when the offset was actually APPLIED, though. Both branches
            # above reset to 0 without advancing anything, so the span is still
            # unconsolidated — and the abandon-at-cap path calls this method
            # precisely to stop spending on it. Clearing the accounting there
            # would hand a capped span a fresh budget every time a rotation
            # raced the marker write, so the cap would never actually hold.
            if safe_offset == offset:
                for _acct_key in _CONSOLIDATION_META_KEYS:
                    meta.pop(_acct_key, None)
            lines[0] = json.dumps(meta) + "\n"
            # Reduce lock hold for this one-line metadata rewrite: skip the
            # fsync (fsync=False). ``last_consolidated`` is recoverable
            # bookkeeping — if a crash loses it we simply re-consolidate a few
            # messages — so paying a disk flush while holding the cross-process
            # lock (blocking every other writer of this session) isn't worth it.
            atomic_write(path, "".join(lines), fsync=False)
            # Housekeeping bookkeeping — must not advance the session's mtime
            # (see _restore_mtime). Otherwise consolidation floats stale sessions
            # to the top of list_sessions on every gateway restart.
            _restore_mtime(path, prev_mtime)
            # Invalidate while still holding the lock. Outside it there is a
            # window where the file is already rewritten with its mtime
            # restored but the generation has not moved, so a concurrent fold /
            # snippet / metadata read passes both the mtime and the generation
            # guard and memoizes pre-rewrite data. Every other preserved-mtime
            # writer already invalidates inside its locked section;
            # _invalidate_cache is pure in-memory work, so this adds no I/O
            # under the cross-process flock.
            self._invalidate_cache(key)

    def unconsolidated_count(self, key: str) -> int:
        """Count messages not yet processed by the consolidator."""
        messages = self._read_messages(key)
        offset = self._read_metadata(key).get("last_consolidated", 0)
        return max(0, len(messages) - offset)

    def consolidation_counts(self, key: str) -> tuple[int, int]:
        """Return ``(total_messages, unconsolidated)`` from a SINGLE read.

        The consolidator's entry points need both: the unconsolidated count to
        decide there is anything to do, and the total as the extent
        :meth:`consolidation_retry_state` compares the charged span against. Both
        come from one ``_read_messages`` call so the eligibility check costs no
        additional transcript read on the event loop.
        """
        messages = self._read_messages(key)
        offset = self._read_metadata(key).get("last_consolidated", 0)
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError, OverflowError):
            offset = 0
        return len(messages), max(0, len(messages) - offset)

    def consolidation_retry_state(
        self, key: str, message_count: int | None = None
    ) -> tuple[int, float]:
        """Return ``(failed_attempts, next_eligible_at)`` for *key*.

        Both live on the metadata line beside ``last_consolidated``, so the
        accounting shares the marker's lifetime: it survives a gateway restart
        (the consolidator's own throttle is in-memory only) and is cleared by
        :meth:`mark_consolidated` when the span finally lands. ``(0, 0.0)`` means
        no failed attempt is on record — the common case — so callers can treat a
        missing entry as "eligible now".

        Read UNCACHED. The accounting is cross-process (a gateway sweep, the CLI,
        a subagent all record failures for the same session), and every writer of
        these fields restores the file's pre-write mtime so housekeeping does not
        reorder ``list_sessions``. The metadata cache is keyed on mtime, so a warm
        entry survives another process's write byte-for-byte and would serve a
        stale attempt count — bypassing the backoff on the read path and, on the
        read-increment-write path, overwriting the other process's durable count
        with a lower one. Dropping the entry first costs one first-line read.

        Metadata is caller-supplied JSON, so every conversion is defensive:
        ``1e309`` parses to ``inf`` and ``int(inf)`` raises ``OverflowError``
        (which would surface as a 500 from the manual trigger or break the idle
        sweep), while a ``NaN`` deadline makes every ``now >= retry_at``
        comparison false and disables consolidation for the session forever.
        Non-finite and unconvertible values therefore reset to the eligible zero
        state rather than propagating.
        """
        self._meta_cache.pop(key, None)
        meta = self._read_metadata(key)
        try:
            attempts = int(meta.get("consolidation_attempts", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            attempts = 0
        try:
            retry_at = float(meta.get("consolidation_retry_at", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            retry_at = 0.0
        if not math.isfinite(retry_at):
            retry_at = 0.0
        if attempts and not self._attempts_describe_current_span(meta, message_count):
            # The counter belongs to a span that no longer exists: a rotation
            # archived those messages (generation bumped), a rewrite reset the
            # marker they started from, or the transcript has grown past the
            # extent that was charged. The cap ABANDONS a span, so carrying a
            # capped count onto different content would silence consolidation for
            # this session permanently — every message written after that point
            # would stay ineligible forever. The new span gets a fresh budget.
            #
            # The deadline is deliberately kept: a fresh budget is not a free
            # immediate turn, so a session that keeps failing still waits out the
            # backoff already armed (4 h at the cap) rather than re-billing the
            # instant one more message lands.
            attempts = 0
        return max(0, attempts), retry_at

    def _attempts_describe_current_span(self, meta: dict, message_count: int | None) -> bool:
        """True when the recorded attempts belong to the span in front of us now.

        A span is identified by where it starts AND how far it reaches: the
        ``(rotation_generation, last_consolidated)`` pair it was charged against
        plus the message count that was actually attempted. While a span keeps
        failing none of the three move — the marker is only advanced on success —
        so the cap holds across attempts. Anything that changes the CONTENT under
        the counter moves one of them: a rotation and a dashboard rewrite
        (regenerate / rewind / fork) both advance the generation, a rewrite that
        cannot apply a stale offset resets the marker, and new messages push the
        transcript past the extent that was attempted.

        The generation is what covers the edit that moves nothing else. A
        regenerate replaces the assistant tail with a reply the failing turns
        never saw and lands at the same message count, the same marker and the
        same extent, so without the bump a capped budget would sit over brand-new
        content and refuse it forever.

        The extent matters because the cap is what ABANDONS a span, and it is only
        reachable from :meth:`HistoryConsolidator.retry_eligible` when the
        abandon-marker write itself failed. Without it, that one transient write
        failure would refuse the session forever: appended messages leave the
        generation and marker untouched, so every entry point would keep rejecting
        a transcript that is no longer the one that failed, and the session's
        history would never be consolidated again.

        *message_count* is the transcript's CURRENT total, supplied by the caller.
        It is never read from disk here: this predicate runs inside
        ``retry_eligible`` on the gateway event loop, and a synchronous full-file
        read there stalls every other gateway task on a large transcript. Callers
        that already hold a count pass it (see :meth:`consolidation_counts`);
        ``None`` means "no count available", which skips the extent test and keeps
        the cap — the conservative direction, since the alternative is spending a
        billed turn on an unverified premise.

        Growth is compared with ``>`` rather than ``!=`` on purpose. A count that
        SHRANK is a rotation or compaction, which already moves the generation or
        the marker; treating a shrink as new content on its own would hand a
        budget to a span with nothing added to it.

        Unstamped accounting (written before a given field existed) is treated as
        belonging to the current span, so an unknown provenance keeps the cap
        rather than granting an unbounded supply of billed retries.

        The stamp this compares against is the ATTEMPTED span, captured before the
        turn (see :class:`AttemptedSpan`), which is what makes all three tests
        meaningful: if the stamp were re-read after the turn it would describe the
        transcript in front of us by construction, and every test here would
        trivially match.
        """
        for meta_key, span_key in (
            ("rotation_generation", "consolidation_attempts_generation"),
            ("last_consolidated", "consolidation_attempts_offset"),
        ):
            if span_key not in meta:
                continue
            try:
                if int(meta.get(span_key, 0) or 0) != int(meta.get(meta_key, 0) or 0):
                    return False
            except (TypeError, ValueError, OverflowError):
                # Unreadable stamp — fall back to keeping the cap.
                continue
        if message_count is not None and "consolidation_attempts_count" in meta:
            try:
                attempted = int(meta.get("consolidation_attempts_count", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return True
            if message_count > attempted:
                return False
        return True

    def _current_span_fields(self, span: AttemptedSpan) -> dict:
        """The span-identity stamp to store beside a freshly charged attempt.

        Every field comes from *span* — the consolidation's own pre-turn snapshot
        (see :class:`AttemptedSpan`) — and NOTHING is re-read from the metadata
        line here. The stamp must describe the span the turn actually attempted,
        and the metadata line at failure time describes the transcript as it is
        *now*, which a rotation or a marker-resetting rewrite during the turn has
        already changed.
        The clamps are the only normalization: *span* is typed, and its values come
        from a snapshot's own ``len()``/generation reads, so there is nothing to
        coerce — but a negative offset or count would be nonsense to stamp.
        """
        return {
            "consolidation_attempts_generation": span.generation,
            "consolidation_attempts_offset": max(0, span.offset),
            "consolidation_attempts_count": max(0, span.total),
        }

    def record_consolidation_failure(
        self,
        key: str,
        base_secs: float,
        max_secs: float,
        span: AttemptedSpan,
        now: float | None = None,
    ) -> tuple[int, float]:
        """Durably count one failed consolidation attempt for *key*.

        *span* is the identity of what the failing turn actually attempted, taken
        from the pre-turn snapshot (see :class:`AttemptedSpan`). It serves twice:
        as the stamp written beside the counter, and as the span the existing
        counter is compared against — so a turn that attempted a DIFFERENT span
        starts a fresh budget while one attempting the same span increments toward
        the cap. Nothing about the span is re-read from the file here, so a
        rotation or rewrite that landed during the turn cannot relabel this charge
        as belonging to content it never measured.

        Returns the new ``(attempts, next_eligible_at)``. The wait doubles per
        attempt from *base_secs*, capped at *max_secs*, so a persistently broken
        span costs a geometrically shrinking number of billed LLM turns instead
        of one per heartbeat tick.

        The read-increment-write runs under a single :meth:`_locked` hold: two
        processes consolidating the same session (gateway sweep and CLI) would
        otherwise both read the same count and write the same value, letting the
        span consume unbounded attempts while the counter sits still. The read
        inside is uncached for the same reason (see
        :meth:`consolidation_retry_state`) — an mtime-preserving write by the
        other process is invisible to a warm cache.

        Returns ``(0, 0.0)`` without writing when the session's transcript is gone
        (deleted while the consolidation was in flight): ``_update_metadata_locked``
        upserts, so writing would resurrect the session as an empty metadata-only
        file. Blocking file IO — call it off the event loop.
        """
        stamp = _time.time() if now is None else now
        with self._locked(key):
            if not self._path(key).exists():
                # The session was deleted while this consolidation was in flight.
                # _update_metadata_locked upserts, so writing here would recreate
                # the transcript as a metadata-only file — resurrecting a deleted
                # session as empty history in list_sessions. Nothing to account
                # for: the span it described is gone.
                logger.info(
                    "Skipping consolidation retry accounting for %s: session deleted",
                    key,
                )
                return 0, 0.0
            attempts = self.consolidation_retry_state(key, span.total)[0] + 1
            # The exponent comes from caller-supplied metadata, so clamp it before
            # shifting: an absurd stored count would otherwise make ``2 ** n``
            # allocate a huge int (or raise) instead of returning a wait. The
            # backoff saturates at *max_secs* far below the clamp, so no reachable
            # attempt count is affected.
            retry_at = stamp + min(max_secs, base_secs * (2 ** min(attempts - 1, 64)))
            self._update_metadata_locked(
                key,
                {
                    "consolidation_attempts": attempts,
                    "consolidation_retry_at": retry_at,
                    # Bind the count to the span it was charged against — where it
                    # starts and how far it reaches — so a rotation, a rewrite, or
                    # a grown transcript cannot leave a capped counter sitting over
                    # content it never measured (see
                    # _attempts_describe_current_span). Every field comes from the
                    # pre-turn snapshot, never from the file as it stands now, and
                    # is written in the SAME locked update as the counter, so no
                    # window exists where the count is charged but its span is
                    # unidentified or misidentified.
                    **self._current_span_fields(span),
                },
            )
        return attempts, retry_at

    def record_consolidation_environment_failure(
        self,
        key: str,
        base_secs: float,
        max_secs: float,
        now: float | None = None,
    ) -> tuple[int, float]:
        """Arm the backoff for a consolidation that never reached the provider.

        Counted separately from :meth:`record_consolidation_failure` because the
        two failures have different costs. A spent turn costs money, so its
        counter feeds a hard abandon cap. A pre-dispatch failure — no session
        manager, or kiro-cli failing to start — costs nothing, so it must never
        abandon a span: doing so would write the durable marker over messages no
        LLM has ever read. It still needs a deadline, or a permanently broken host
        re-attempts on every 60s heartbeat tick forever, so the count drives the
        same widening wait up to *max_secs* and then holds there.

        Returns the new ``(environment_failures, next_eligible_at)``. Same single
        locked read-increment-write and same deleted-session guard as the billed
        path. Blocking file IO — call it off the event loop.
        """
        stamp = _time.time() if now is None else now
        with self._locked(key):
            if not self._path(key).exists():
                return 0, 0.0
            meta = self._read_metadata(key)
            try:
                failures = int(meta.get("consolidation_env_failures", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                failures = 0
            failures = max(0, failures) + 1
            retry_at = stamp + min(max_secs, base_secs * (2 ** min(failures - 1, 64)))
            self._update_metadata_locked(
                key,
                {
                    "consolidation_env_failures": failures,
                    "consolidation_retry_at": retry_at,
                },
            )
        return failures, retry_at

    @staticmethod
    def _canonical_key(key: str) -> str:
        return SessionCatalogProjection._canonical_key(key)

    def list_sessions(self) -> list[dict]:
        return self._catalog_projection.list_sessions()

    def agent_usage(self) -> dict[str, tuple[int, float]]:
        return self._catalog_projection.agent_usage()

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        return self._catalog_projection.search_sessions(query, limit)

    def _folded_content(self, key: str) -> tuple[int, str]:
        return self._catalog_projection._folded_content(key)

    def _prune_search_memos(self, live_keys: set[str]) -> None:
        self._catalog_projection._prune_search_memos(live_keys)

    def _build_folded(self, key: str, mtime: float, gen: int) -> tuple[int, str] | None:
        return self._catalog_projection._build_folded(key, mtime, gen)

    def _iter_message_texts(self, key: str) -> Iterator[str]:
        return self._catalog_projection._iter_message_texts(key)

    _SNIPPET_LEAD = SessionCatalogProjection._SNIPPET_LEAD
    _SNIPPET_TRAIL = SessionCatalogProjection._SNIPPET_TRAIL
    _SNIPPET_MAX = SessionCatalogProjection._SNIPPET_MAX

    def _snippet_texts(self, key: str) -> Iterator[str]:
        return self._catalog_projection._snippet_texts(key)

    def _content_snippet(self, key: str, query: str) -> str:
        return self._catalog_projection._content_snippet(key, query)

    def recent_from_source(
        self, source_prefix: str, exclude_key: str = "", max_messages: int = 20
    ) -> list[dict]:
        return self._read_projection.recent_from_source(source_prefix, exclude_key, max_messages)

    def read_messages(self, key: str) -> list[dict]:
        return self._read_projection.read_messages(key)

    def read_file_change_messages(self, key: str) -> list[dict]:
        return self._read_projection.read_file_change_messages(key)

    def read_messages_chained(self, key: str) -> list[dict]:
        return self._read_projection.read_messages_chained(key)

    def _rebuild_tab_id_index(self) -> None:
        self._read_projection._rebuild_tab_id_index()

    def invalidate_tab_id_cache(self) -> None:
        self._read_projection.invalidate_tab_id_cache()

    def note_tab_id(self, key: str, tab_id: str | None) -> None:
        self._read_projection.note_tab_id(key, tab_id)

    @overload
    def delete_session(self, key: str, *, skip_pinned: Literal[False] = ...) -> bool: ...

    @overload
    def delete_session(self, key: str, *, skip_pinned: Literal[True]) -> bool | None: ...

    def delete_session(self, key: str, *, skip_pinned: bool = False) -> bool | None:
        if skip_pinned:
            return self._metadata_projection.delete_session(key, skip_pinned=True)
        return self._metadata_projection.delete_session(key, skip_pinned=False)

    def set_title(self, key: str, title: str) -> None:
        self._metadata_projection.set_title(key, title)

    def update_metadata(self, key: str, fields: dict) -> None:
        self._metadata_projection.update_metadata(key, fields)

    def update_metadata_if(
        self,
        key: str,
        fields: dict,
        guard: Callable[[dict], bool],
    ) -> bool:
        return self._metadata_projection.update_metadata_if(key, fields, guard)

    def _update_metadata_locked(self, key: str, fields: dict) -> None:
        self._metadata_projection._update_metadata_locked(key, fields)

    def mtime_of(self, key: str) -> float | None:
        return self._metadata_projection.mtime_of(key)

    def clear_closed(self, key: str, *, only_if_closed_before: float | None = None) -> None:
        self._metadata_projection.clear_closed(key, only_if_closed_before=only_if_closed_before)

    def _read_messages(self, key: str) -> list[dict]:
        return self._read_projection._read_messages(key)

    @contextlib.contextmanager
    def _cache_fill_lock(self, key: str) -> Iterator[bool]:
        with self._read_projection._cache_fill_lock(key) as locked:
            yield locked

    def _read_messages_locked(
        self,
        key: str,
        *,
        gen: int | None,
        flock_witness: tuple[int, int] | None,
    ) -> list[dict]:
        return self._read_projection._read_messages_locked(
            key, gen=gen, flock_witness=flock_witness
        )

    _TAIL_MIN_BYTES = 8_192
    _TAIL_AVG_MSG_BYTES = 512
    _TAIL_MAX_GROWTHS = 6

    def _recent_via_tail(
        self,
        key: str,
        max_messages: int,
        roles: AbstractSet[str] | None,
    ) -> list[dict] | None:
        return self._read_projection._recent_via_tail(key, max_messages, roles)

    @staticmethod
    def _recent_cache_key(key: str, max_messages: int, roles: AbstractSet[str] | None) -> str:
        return TranscriptReadProjection._recent_cache_key(key, max_messages, roles)

    def _read_tail_messages(
        self,
        path: Path,
        max_messages: int,
        roles: AbstractSet[str] | None,
    ) -> list[dict]:
        return self._read_projection._read_tail_messages(path, max_messages, roles)

    def _last_row_ts(self, key: str) -> str | None:
        return self._read_projection._last_row_ts(key)

    def last_row_ts(self, key: str) -> str | None:
        return self._read_projection.last_row_ts(key)

    @staticmethod
    def _cache_key_identities(key: str) -> tuple[str, ...]:
        """Every cache-key spelling that can refer to *key*'s session.

        One session is addressable by its logical key, its sanitized filename
        stem, and — for Slack threads — the pre-migration bare ``thread_ts``
        in either role. The closure must be BIDIRECTIONAL: a writer told only
        the bare legacy spelling (e.g. rotation deriving it from the file
        name) must still reach the canonical spelling readers use, or its
        invalidation is invisible to them. Pure string math, no I/O.
        """
        idents = dict.fromkeys((key, *transcript_stems(key)))
        canon = canonical_key(key)
        if canon != key:
            idents.update(dict.fromkeys((canon, *transcript_stems(canon))))
        return tuple(idents)

    def _flock_hold_witness(self, key: str) -> tuple[int, int] | None:
        """Proof-of-hold snapshot of OUR cross-process flock for *key*'s file.

        Returns ``(fd, release_epoch)`` when this process currently holds the
        sidecar flock (``_flock_state.held == 1``), else ``None``. An unlocked
        fill snapshots this before its stat and compares at publish time: an
        equal pair proves the flock was held by this process CONTINUOUSLY
        across the fill window, so no EXTERNAL process can have written the
        file in that window — external writers block on the flock, and they
        are the one writer class the in-process invalidation generation cannot
        witness (their ``_invalidate_cache`` runs in their process, not ours).
        Local writers write freely under our hold, and every local
        preserved-mtime rewrite bumps the generation, so the generation check
        covers them. ``None`` — including a local writer still WAITING on an
        external process's flock — means the window cannot be proven
        external-write-free and the fill must not publish.
        """
        lock_key = str(self._path(key))
        with ConversationLog._flock_guard:
            state = ConversationLog._flock_state.get(lock_key)
            if state is None or state[2] != 1:
                return None
            return (state[0], ConversationLog._flock_epochs.get(lock_key, 0))

    def _cache_gen(self, key: str) -> int:
        return self._cache_coordinator._cache_gen(key)

    def _bump_cache_gen(self, key: str, idents: tuple[str, ...]) -> None:
        self._cache_coordinator._bump_cache_gen(key, idents)

    def _publish_if_current(
        self,
        cache: _LRUCache[Any] | _SearchTextCache[Any],
        entry_key: str,
        value: Any,
        *,
        key: str,
        gen: int,
    ) -> None:
        self._cache_coordinator._publish_if_current(cache, entry_key, value, key=key, gen=gen)

    def _invalidate_cache(self, key: str) -> None:
        self._cache_coordinator._invalidate_cache(key)

    #: Bytes read from the end of a session file for the last-message preview.
    #: One tail block comfortably covers several trailing JSONL lines without
    #: paying a full-file parse on large sessions.
    _PREVIEW_TAIL_BYTES = 16_384
    #: Max characters returned in a last-message preview.
    _PREVIEW_MAX_CHARS = 120

    def last_message_preview(self, key: str, sanitize=None) -> str:
        return self._read_projection.last_message_preview(key, sanitize=sanitize)

    def last_message_info(self, key: str, sanitize=None) -> tuple[str, float]:
        return self._read_projection.last_message_info(key, sanitize=sanitize)

    @staticmethod
    def _content_text(content: object) -> str:
        return TranscriptReadProjection._content_text(content)

    def get_metadata(self, key: str) -> dict:
        return self._read_projection.get_metadata(key)

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        return self._read_projection.get_metadata_status(key)

    def _pause_for_transient_retry(self) -> None:
        self._read_projection._pause_for_transient_retry()

    def _read_metadata(self, key: str) -> dict:
        return self._read_projection._read_metadata(key)

    def _read_metadata_status(self, key: str) -> tuple[dict, bool]:
        return self._read_projection._read_metadata_status(key)

    def sliding_window(self, key: str, keep_recent: int = 5) -> tuple[list[dict], list[dict]]:
        return self._read_projection.sliding_window(key, keep_recent)

    def rewrite_session(self, key: str, messages: list[dict]) -> None:
        self._rewrite_coordinator.rewrite_session(key, messages)

    def _rewrite_session_locked(self, key: str, messages: list[dict]) -> None:
        self._rewrite_coordinator._rewrite_session_locked(key, messages)

    def _maybe_rotate(self, path: Path, key: str) -> None:
        self._rewrite_coordinator._maybe_rotate(path, key)
