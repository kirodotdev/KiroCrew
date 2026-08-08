"""Persistent conversation history — JSONL per session + LLM consolidation.

Session files: ~/.kirocrew/sessions/{safe_key}.jsonl
Each entry tracks provenance (source_thread, source_user) for citation.
Files auto-rotate at 512KB, keeping last 200 lines.
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
from collections import OrderedDict
from collections.abc import Callable, Container, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Generic, NamedTuple, TypeVar

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect, stream_and_collect_json
from kiro_crew.messaging.link import legacy_key
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.skills import AUTO_SKILL_MAX_PROCEDURE_CHARS, AutoSkillProvenance
from kiro_crew.skills_dedupe import (
    VERDICT_DUP,
    VERDICT_NEW,
    VERDICT_UPDATE,
    metadata_dedupe_verdict,
)
from kiro_crew.skills_script_validator import validate_skill_script
from kiro_crew.vector_memory_constants import (
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory import MemoryStore
    from kiro_crew.session import SessionManager
    from kiro_crew.skills import SkillsLoader
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

SESSIONS_DIR_NAME = "sessions"
ARCHIVE_DIR_NAME = "archive"
ARCHIVE_RETENTION_DAYS = 7

# Separates a transcript's stem from an archive segment's timestamp. NOT a dot,
# because session keys legitimately contain dots (a Slack thread_ts), which would
# make a right-most-dot parse attribute a segment to the wrong session.
ARCHIVE_SEGMENT_DELIMITER = "__"
_CONSOLIDATION_THRESHOLD = 30  # preferences/projects update threshold (messages)
# Persistent retry accounting for history consolidation. A pass spends a billed
# LLM turn BEFORE it can write the durable "consolidated" marker, so any failure
# in between leaves the span unconsolidated — and every entry point (the 60s
# idle sweep, session-expiry sweeps, the dashboard trigger) then re-spends that
# turn, forever, because the only thing suppressing a repeat is an in-memory
# throttle that a failure skips and a restart forgets. Attempts are therefore
# counted in the transcript's metadata line (durable, per span) and gated behind
# exponential backoff up to a hard cap. At the cap the span is abandoned with
# the marker written anyway: losing one memory pass costs one summary, while
# retrying forever costs an LLM turn per heartbeat tick indefinitely.
_CONSOLIDATION_MAX_ATTEMPTS = 5
_CONSOLIDATION_BACKOFF_BASE_SECS = 900.0  # 15 min, doubling per failed attempt
_CONSOLIDATION_BACKOFF_MAX_SECS = 86400.0  # 24 h ceiling on a single wait
# Every metadata field the accounting owns, so the marker's success path can drop
# the whole set in one place and a rebuilding writer can name it as foreign.
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


class AttemptedSpan(NamedTuple):
    """The span identity a consolidation turn actually covered.

    All three fields come from ONE atomic
    :meth:`ConversationLog.snapshot_for_consolidation` taken BEFORE the turn:
    where the span starts (*generation*, *offset*) and how far it reaches
    (*total*). They travel together because the accounting's whole guarantee is
    that a charged attempt is stamped with the span it measured — mixing a
    snapshot value with one re-read after the turn stamps content that was never
    attempted, and the cap then either outlives its span (stranding messages
    forever) or releases early (re-billing indefinitely).

    Re-reading the metadata line at failure time is the specific mistake this
    type exists to prevent. A rotation or a marker-resetting rewrite DURING the
    turn moves all three, so a post-turn read stamps the counter with the NEW
    span's identity while the count it carries was charged against the old one.
    At the cap that leaves the post-rotation messages capped without ever having
    been attempted, and because their generation and offset already match the
    stamp no later append can release them.
    """

    #: Transcript message total the turn covered — the snapshot's total, not the
    #: file's size afterwards. Named ``total`` rather than ``count`` because a
    #: ``NamedTuple`` field called ``count`` would shadow ``tuple.count``.
    total: int
    #: ``rotation_generation`` at the snapshot.
    generation: int
    #: ``last_consolidated`` at the snapshot — where the attempted span begins.
    offset: int


# Skill detection judges a wider window than the incremental history tail: a
# reusable procedure usually spans the whole session, not just the slice since
# the last consolidation, so a tail-only view systematically misses skills in
# any session consolidated more than once. Bound the window so pathologically
# long sessions stay cost-safe.
_SKILL_DETECTION_WINDOW = 200

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
        "mode",
        "workspace",
        "project",
        "folder_id",
        "app",
        "artifact",
        "pinned",
        "color_index",
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


def _fmt_message(m: dict) -> str:
    """Render one transcript message for a consolidation / skill-detection prompt."""
    tools = f" [tools: {', '.join(m['tools'])}]" if m.get("tools") else ""
    return f"[{m.get('ts', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}"


_SESSION_MAX_BYTES = 2 * 1024 * 1024  # 2MB
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


class _ConsolidationNotDispatched(Exception):
    """A consolidation prompt never reached the provider.

    Distinguishes "we could not send the turn" (no session manager; kiro-cli
    missing, not logged in, or failing to start) from "the turn ran and returned
    nothing useful". Only the latter costs money, so only the latter may consume
    the attempt budget that abandons a span — charging an unsent turn would let a
    broken host mark messages consolidated that no LLM has ever read. Raised
    rather than reported as a flag beside the result so a caller cannot silently
    drop the distinction.
    """


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


def append_if_absent_off_loop(
    conversation_log: "ConversationLog",
    key: str,
    role: str,
    content: str,
    *,
    agent: str | None = None,
) -> None:
    """Idempotent, loop-safe variant of :func:`append_off_loop`.

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
        conversation_log.append_if_absent(key, role, content, agent=agent)

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
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "append_if_absent_off_loop: offloaded append failed key=%s: %r",
                key,
                exc,
            )

    loop.run_in_executor(None, _do).add_done_callback(_report)


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


SEARCH_MIN_CHARS = 2  # shortest query string that triggers backend search
_TITLE_BOOST = 10  # field-boost multiplier for title matches in search_sessions
_SEARCH_SCAN_WINDOW = 500  # cap files scanned per search to bound I/O

# Hard ceilings on the memory the two search memos may hold, in real retained
# bytes as reported by ``str.__sizeof__`` — NOT in characters.
#
# ``len()`` is the wrong unit and dangerously so: CPython stores a ``str`` in the
# narrowest width its contents allow, so one character is 1 byte for latin-1, 2
# for the BMP, and 4 for astral planes. A ceiling of 160 MB counted in characters
# retains 168 MB of ASCII, 336 MB of CJK, or 671 MB of emoji — and CJK is the
# ordinary case for a non-English corpus, not a pathological one. The sizers
# therefore call ``__sizeof__`` per string, and the snippet memo also charges for
# its list container.
#
# Why bytes and not an entry count: a session is read up to
# ``_SESSION_MAX_BYTES`` (2 MB), so ``_SEARCH_SCAN_WINDOW`` entries is anywhere
# from a few MB to ~1 GB depending on the corpus. An entry count therefore
# bounds nothing that matters; it only *looked* safe because real sessions are
# small (a 171 MB / 230-session corpus folds to ~8 MB).
#
# The two are separate rather than one shared pool so a corpus that blows the
# snippet budget cannot starve the fold, which is the one that keeps matching
# off the critical path. Their sum is the ceiling to reason about.
_SEARCH_FOLD_BUDGET_BYTES = 96 * 1024 * 1024
_SEARCH_SNIPPET_BUDGET_BYTES = 64 * 1024 * 1024

# Canonical set of memory_mode values that mark a session private — never
# searchable/listable/summarizable. Single source of truth shared by the MCP
# history tools (mcp_core) and the dashboard session handlers so the exclusion
# can't silently diverge between surfaces.
INCOGNITO_MEMORY_MODES = frozenset({"incognito", "temporary"})

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
    import time as _time

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
    # re-run the expensive KiroCrewConfig.load() — reintroducing the
    # regression for the disabled case.
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


#: Default upper bound on the number of distinct session keys held in the
#: in-memory transcript / metadata caches.  The previous implementation used
#: plain ``dict`` caches that grew one entry per session key touched and never
#: evicted — on a gateway serving thousands of sessions the parsed message
#: lists (each up to ~200 messages / 2 MB of source JSONL) accumulated in RAM
#: for the lifetime of the process.  A bounded LRU keeps hot sessions resident
#: while giving the working set a deterministic ceiling.
_TRANSCRIPT_CACHE_MAX = 256

# Metadata reads retry briefly before reporting "no metadata": on Windows a
# just-written session file can be transiently unopenable while an indexer or AV
# scanner holds it (ERROR_SHARING_VIOLATION -> PermissionError). Those holds are
# measured in milliseconds, and the caller that matters here (the open-tab
# restore) treats an empty result as "session never existed" and drops the tab.
_METADATA_READ_ATTEMPTS = 3
_METADATA_READ_RETRY_SECS = 0.02


_V = TypeVar("_V")


class _LRUCache(Generic[_V]):
    """A tiny bounded LRU cache with a dict-compatible surface.

    Backed by an :class:`collections.OrderedDict`; the most recently
    accessed key is kept at the end and eviction pops from the front
    (least-recently-used), so eviction order is fully deterministic for a
    given access sequence. Supports the subset of the mapping protocol the
    caller relies on (``get`` / ``__getitem__`` / ``__setitem__`` / ``pop`` /
    ``__contains__`` / ``__len__`` / ``clear``). Both reads (``get`` /
    ``__getitem__``) and writes mark a key as recently used.

    ``maxsize <= 0`` disables bounding (behaves like an ordinary dict) so a
    caller can opt out without a code-path split.

    Thread safety: the same :class:`ConversationLog` instance is touched from
    the event loop *and* from worker threads (``chat_persistence`` flush /
    restore, ``chat_regenerate`` / ``chat_rewind`` via ``asyncio.to_thread``,
    ``handlers/cron`` and ``slack/gateway`` off-loop ``read_messages`` calls).
    Every method therefore takes ``self._lock`` so each is atomic and the
    compound read-modify-write sequences (``move_to_end`` + index in ``get`` /
    ``__getitem__``; the eviction ``len()`` + ``popitem`` loop in
    ``__setitem__``) cannot interleave with a concurrent ``pop`` / ``clear``.
    Without it a concurrent ``pop`` landing in the bytecode gap between a
    successful ``move_to_end`` and the following index raised ``KeyError``
    (crashing the request/background task) instead of returning the default.
    The operations are tiny in-memory dict ops, so lock contention is
    negligible. A plain :class:`threading.Lock` suffices — no method calls
    another locked method, so the lock is never re-entered.
    """

    def __init__(self, maxsize: int = _TRANSCRIPT_CACHE_MAX) -> None:
        self._maxsize = maxsize
        self._data: "OrderedDict[str, _V]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            try:
                self._data.move_to_end(key)
            except KeyError:
                return default
            return self._data[key]

    def __getitem__(self, key: str) -> _V:
        with self._lock:
            self._data.move_to_end(key)
            return self._data[key]

    def __setitem__(self, key: str, value: _V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            # Evict least-recently-used entries until within the bound.
            if self._maxsize > 0:
                while len(self._data) > self._maxsize:
                    self._data.popitem(last=False)

    def pop(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            return self._data.pop(key, default)

    def pop_prefix(self, prefix: str) -> None:
        """Remove every entry whose (string) key starts with *prefix*.

        Used to invalidate all cached ``recent()`` windows for one session key
        (composite keys are ``"<key>\\x00<max>\\x00<roles>"``) without touching
        other sessions' entries. Atomic under the lock.
        """
        with self._lock:
            doomed = [k for k in self._data if isinstance(k, str) and k.startswith(prefix)]
            for k in doomed:
                del self._data[k]

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class _SearchTextCache(Generic[_V]):
    """Byte-budgeted memo for the two derived corpora ``search_sessions`` needs.

    Distinct from :class:`_LRUCache` because the access pattern is different in
    the one way that decides an eviction policy. A search walks
    ``_SEARCH_SCAN_WINDOW`` sessions **in the same recency order every query**,
    so the working set is cyclic. Under LRU a cyclic scan larger than the cache
    evicts each entry exactly one step before its next read: the hit rate does
    not degrade, it collapses to zero, and it does so for the users with the
    most sessions — the ones the memo exists for. ``_folded_cache`` was sized
    ``max(cache_max, _SEARCH_SCAN_WINDOW)`` for precisely that reason.

    An entry *count* is the wrong bound to carry that guarantee, though: 500
    entries is 37 MB on one corpus and could be far more on another, because a
    session is read up to ``_SESSION_MAX_BYTES``. The bound here is therefore
    **bytes**, which is what actually has to fit in the gateway's RSS.

    A byte budget reintroduces the cliff, so eviction is replaced by
    **admission control**: when a new entry would exceed the budget it is simply
    not stored, and the entries already held are kept. For a cyclic scan that
    turns 0% into (whatever fraction fits)% — and because ``search_sessions``
    walks most-recent-first, the fraction that stays cached is the most recently
    active sessions, which is the half worth keeping. Existing entries are still
    replaced in place on a content change (same key, new value), so a session
    that is being written to never gets stuck on a stale value.

    ``max_bytes <= 0`` disables the bound (behaves like a plain dict).

    Thread safety mirrors :class:`_LRUCache`: one lock, every method atomic, no
    method calls another locked method.
    """

    def __init__(self, max_bytes: int, sizer: Callable[[_V], int], label: str = "") -> None:
        self._max_bytes = max_bytes
        self._sizer = sizer
        self._label = label
        self._data: dict[str, _V] = {}
        self._bytes = 0
        self._admitted = 0
        self._refused = 0
        self._refused_since_prune = 0
        self._warned = False
        self._lock = threading.Lock()

    def get(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            return self._data.get(key, default)

    def __setitem__(self, key: str, value: _V) -> None:
        cost = self._sizer(value)
        with self._lock:
            previous = self._data.get(key)
            if previous is not None:
                # Replacement, not growth: release the old cost first so a
                # session that keeps being appended to cannot inflate the
                # accounting or be refused admission for its own new value.
                self._bytes -= self._sizer(previous)
                del self._data[key]
            if self._max_bytes > 0 and self._bytes + cost > self._max_bytes:
                self._refused += 1
                self._refused_since_prune += 1
                warn = not self._warned
                self._warned = True
                if warn:
                    # Say it once, at the moment the ceiling starts binding.
                    # Without this the degradation is observable only under a
                    # debugger, which makes "diagnosable" an empty claim.
                    logger.warning(
                        "search memo %s hit its %d MB ceiling (%d entries, %d "
                        "admitted); sessions past it fall back to re-reading "
                        "their file, so warm search will be slower on this "
                        "corpus",
                        self._label or "cache",
                        self._max_bytes // (1024 * 1024),
                        len(self._data),
                        self._admitted,
                    )
                return
            self._data[key] = value
            self._bytes += cost
            self._admitted += 1

    def pop(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            if key in self._data:
                value = self._data.pop(key)
                self._bytes -= self._sizer(value)
                return value
            return default

    def retain(self, live_keys: Container[str]) -> int:
        """Drop every entry whose key is not in *live_keys*; return how many.

        The release valve that keeps admission control from becoming a one-way
        ratchet. Without it, a cache that fills freezes on whatever it happened
        to hold first: entries that have since aged out of the scan window keep
        their budget forever (only invalidation or a stat failure pops them),
        while every newly created session is refused — so the newest and most
        searched sessions become exactly the cold ones, and warm latency
        regresses over process lifetime until a restart.

        Safe against the LRU cliff this class exists to avoid, because it only
        drops entries a search can no longer reach: ``search_sessions`` walks the
        ``_SEARCH_SCAN_WINDOW`` most recent sessions, so anything outside that
        window is dead weight by construction rather than an entry one step from
        its next read.

        Called only under pressure (see ``refused_since_prune``) so an
        uncontended cache never pays for the scan.
        """
        with self._lock:
            doomed = [k for k in self._data if k not in live_keys]
            for k in doomed:
                self._bytes -= self._sizer(self._data.pop(k))
            self._refused_since_prune = 0
            return len(doomed)

    def refused_since_prune(self) -> int:
        """Admissions turned away since the last :meth:`retain`.

        Nonzero means the budget is binding right now, which is the signal to
        spend a prune. Distinct from the cumulative ``refused`` in
        :meth:`stats`, which never resets so it stays useful for diagnosis.
        """
        with self._lock:
            return self._refused_since_prune

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        """Observability for the budget: how full it is and what it turned away.

        ``refused`` rising while ``entries`` is flat is the signal that the
        budget is smaller than the working set, i.e. that searches on this
        corpus are paying the cold cost for the sessions that did not fit.
        """
        with self._lock:
            return {
                "entries": len(self._data),
                "bytes": self._bytes,
                "max_bytes": self._max_bytes,
                "admitted": self._admitted,
                "refused": self._refused,
            }


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
        self._msg_cache: _LRUCache[tuple[float, list[dict]]] = _LRUCache(cache_max)
        self._meta_cache: _LRUCache[tuple[float, dict]] = _LRUCache(cache_max)
        #: Bounded, mtime-keyed LRU of formatted ``recent()`` windows keyed by
        #: (key, max_messages, roles). The tail-read fast path intentionally
        #: never warms ``_msg_cache`` (it returns a partial view), so a session
        #: accessed *only* via ``recent()`` — the hot per-turn context path —
        #: would otherwise re-open and re-parse the file tail on every single
        #: call. This memoizes the formatted window; the stored mtime guards
        #: staleness (an append bumps the file mtime, so the entry is
        #: recomputed on the next call). Own ``_LRUCache`` → own internal lock.
        self._recent_cache: _LRUCache[tuple[float, list[dict]]] = _LRUCache(cache_max)
        #: Bounded, mtime-keyed memo of ``(mtime, doc_chars, casefolded_blob)``
        #: per session, consumed only by :meth:`search_sessions`.
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
        self._folded_cache: _SearchTextCache[tuple[float, int, str]] = _SearchTextCache(
            _SEARCH_FOLD_BUDGET_BYTES, lambda v: v[2].__sizeof__(), "fold"
        )
        #: session key → (mtime, raw message texts) for snippet extraction.
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
        #: displayed to the user; the fold cannot be reused for it.
        self._snippet_cache: _SearchTextCache[tuple[float, list[str]]] = _SearchTextCache(
            _SEARCH_SNIPPET_BUDGET_BYTES,
            lambda v: v[1].__sizeof__() + sum(t.__sizeof__() for t in v[1]),
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
        if sig is not None and data.get("sig") == sig and isinstance(summary, str):
            return summary
        return None

    def set_cached_summary(self, key: str, summary: str, sig: float) -> None:
        """Persist a derived one-line *summary* to the sidecar cache.

        Keyed by the session file's mtime *sig* so a later append invalidates
        it. Atomic and side-effect-free with respect to the session JSONL —
        no read-modify-write, hence no data-loss race with a concurrent
        :meth:`append`.
        """
        atomic_write(
            self._summary_cache_path(key),
            json.dumps({"sig": sig, "summary": summary}),
        )

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
    ) -> None:
        """Append a message with optional provenance to the session log.

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

            # Rotate if file exceeds size limit
            self._maybe_rotate(path)

    def append_if_absent(
        self,
        key: str,
        role: str,
        content: str,
        *,
        agent: str | None = None,
        tab_id: str | None = None,
    ) -> bool:
        """Append a message only if an identical one is not already persisted.

        Returns ``True`` if the message was written, ``False`` if a message
        with the same ``(role, content)`` already exists on disk.

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
        """
        with self._locked(key):
            if self._path(key).exists():
                # Compare against the form ``append`` actually stores: the
                # write boundary redacts non-user content, so matching on the
                # raw text would never recognise an already-persisted message
                # that contained a credential and would append it twice.
                persisted = _redact_at_write_boundary(role, content)
                for m in self._read_messages(key):
                    if m.get("role") == role and m.get("content") == persisted:
                        return False
            # Reentrant: ``append`` re-enters ``_locked`` for the same key on
            # this thread (RLock + refcounted flock), so the write stays inside
            # the critical section we already hold.
            self.append(key, role, content, agent=agent, tab_id=tab_id)
            return True

    def recent(
        self,
        key: str,
        max_messages: int = 20,
        roles: set[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        """Return last *max_messages* entries as ``[{role, content}]``.

        When *roles* is provided, only messages with matching roles are
        counted toward the limit.  This filters out low-signal entries
        (e.g. tool display titles) so the budget is spent on user and
        assistant content.

        When *exclude_last_n* > 0, drops that many trailing raw entries
        BEFORE role filtering. Used by the dashboard to avoid re-injecting
        the just-flushed current-turn user message as history when the
        background flush wins the race against kiro-cli spawn.
        """
        # Recent-only access with no trailing exclusion can
        # be served by reading just the TAIL of the file, skipping a full parse
        # of a potentially 2 MB session log. Only taken on a cache miss (a fresh
        # full-parse cache is already O(1) and is preferred). Returns None to
        # signal "fall through to the full read" (missing file, or a full cache
        # is fresh — in which case the full path is a cheap cache hit).
        if exclude_last_n == 0 and self._tail_reads:
            tail = self._recent_via_tail(key, max_messages, roles)
            if tail is not None:
                return tail
        messages = self._read_messages(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        if roles:
            messages = [m for m in messages if m["role"] in roles]
        return [{"role": m["role"], "content": m["content"]} for m in messages[-max_messages:]]

    def recent_chained(
        self,
        key: str,
        max_messages: int = 20,
        roles: set[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        """Like recent() but reads across all chained session files (same tab_id).

        For long-lived sessions that span multiple session files (linked by
        tab_id), this reads the full chain. Falls back to single-file read
        for legacy sessions without a tab_id.

        See :meth:`recent` for *exclude_last_n* semantics.
        """
        messages = self.read_messages_chained(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        if roles:
            messages = [m for m in messages if m["role"] in roles]
        return [{"role": m["role"], "content": m["content"]} for m in messages[-max_messages:]]

    def recent_with_provenance(
        self, key: str, max_messages: int = 3, *, exclude_last_n: int = 0
    ) -> list[dict]:
        """Return recent entries with source_thread provenance for cross-session citation.

        See :meth:`recent` for *exclude_last_n* semantics.
        """
        messages = self._read_messages(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        with_source = [m for m in messages if m.get("source_thread")]
        result: list[dict] = []
        for m in with_source[-max_messages:]:
            snippet = m["content"][:150] + "…" if len(m["content"]) > 150 else m["content"]
            result.append(
                {
                    "source_thread": m["source_thread"],
                    "ts": m.get("ts", "?"),
                    "snippet": snippet,
                }
            )
        return result

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

    def _attempts_describe_current_span(
        self, meta: dict, message_count: int | None
    ) -> bool:
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

    def load_transcript(self, key: str) -> str:
        """Load full session as formatted text for LLM summarization."""
        messages = self._read_messages(key)
        if not messages:
            return ""
        lines: list[str] = []
        for m in messages:
            role = m["role"].title()
            lines.append(f"{role}: {m['content']}")
        return "\n\n".join(lines)

    @staticmethod
    def _canonical_key(key: str) -> str:
        """Collapse stacked ``dashboard_`` prefixes to a single one.

        Files like ``dashboard_dashboard_chat-1-123`` are duplicates of
        ``dashboard_chat-1-123`` caused by resume round-trips.  Return
        the canonical (single-prefix) form so callers can deduplicate.
        """
        if not key.startswith("dashboard_"):
            return key
        stripped = key
        while stripped.startswith("dashboard_"):
            stripped = stripped[len("dashboard_") :]
        return f"dashboard_{stripped}" if stripped else key

    def list_sessions(self) -> list[dict]:
        """Return metadata for all session files, newest first.

        Deduplicates stacked ``dashboard_`` prefix files, keeping the
        most recently modified version.  Uses mtime-based metadata cache
        when available, falling back to reading only the first line for
        title extraction.
        """
        sessions: list[dict] = []
        if not self._dir.exists():
            return sessions
        # Deduplicate stacked dashboard_ prefixes by canonical key, keeping newer
        by_canon: dict[str, dict] = {}
        for path in self._dir.glob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            # Skip symlinks — these are handoff aliases pointing to the real session
            if path.is_symlink():
                continue
            key = path.stem
            meta: dict = {
                "key": key,
                "messages": max(1, int(stat.st_size / 200)),
                "modified": stat.st_mtime,
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            # Try metadata cache first (populated by _read_metadata calls)
            cached_meta = self._meta_cache.get(key)
            if cached_meta and cached_meta[0] == stat.st_mtime:
                d = cached_meta[1]
                if d.get("created_at"):
                    meta["created"] = d["created_at"]
                if d.get("title"):
                    meta["title"] = d["title"]
                if d.get("agent"):
                    meta["agent"] = d["agent"]
                meta["memory_mode"] = d.get("memory_mode", "persistent")
                if d.get("folder_id"):
                    meta["folder_id"] = d["folder_id"]
            else:
                # Read only the first line for metadata
                try:
                    with open(path, encoding="utf-8") as f:
                        first_line = f.readline().strip()
                    if first_line:
                        d = json.loads(first_line)
                        if d.get("_type") == "metadata":
                            if d.get("created_at"):
                                meta["created"] = d["created_at"]
                            if d.get("title"):
                                meta["title"] = d["title"]
                            if d.get("agent"):
                                meta["agent"] = d["agent"]
                            meta["memory_mode"] = d.get("memory_mode", "persistent")
                            if d.get("folder_id"):
                                meta["folder_id"] = d["folder_id"]
                            self._meta_cache[key] = (stat.st_mtime, d)
                except Exception:
                    pass
            # Ensure memory_mode is always present (old sessions lack it)
            meta.setdefault("memory_mode", "persistent")
            # Extract first user message as title fallback
            if "title" not in meta:
                msg_cached = self._msg_cache.get(key)
                if msg_cached and msg_cached[0] == stat.st_mtime:
                    for m in msg_cached[1]:
                        if m.get("role") == "user" and m.get("content"):
                            meta["title"] = m["content"][:80]
                            break
                else:
                    try:
                        with open(path, encoding="utf-8") as f:
                            for i, ln in enumerate(f):
                                if i > 20:
                                    break
                                ln = ln.strip()
                                if not ln:
                                    continue
                                try:
                                    d = json.loads(ln)
                                except json.JSONDecodeError:
                                    continue
                                if d.get("role") == "user" and d.get("content"):
                                    meta["title"] = d["content"][:80]
                                    break
                    except Exception:
                        pass
            if "title" not in meta:
                meta["title"] = key
            # Deduplicate: keep newer entry per canonical key
            canon = self._canonical_key(key)
            existing = by_canon.get(canon)
            if existing is None or stat.st_mtime >= existing["modified"]:
                by_canon[canon] = meta
        sessions = list(by_canon.values())
        sessions.sort(key=lambda s: s.get("modified", 0), reverse=True)
        return sessions

    def agent_usage(self) -> dict[str, tuple[int, float]]:
        """Return ``{agent_name: (session_count, last_used_mtime)}`` per agent.

        Built on top of :meth:`list_sessions` (not a fresh directory glob) so it
        inherits that method's canonical-session dedup and symlink-skip — counts
        are therefore per logical conversation, not per raw ``.jsonl`` file.
        Sessions whose metadata never recorded an agent are ignored.
        """
        usage: dict[str, tuple[int, float]] = {}
        for meta in self.list_sessions():
            agent = meta.get("agent")
            if not agent:
                continue
            count, last_used = usage.get(agent, (0, 0.0))
            usage[agent] = (count + 1, max(last_used, meta.get("modified", 0.0)))
        return usage

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        """Return session metadata for files whose message content matches *query*.

        Case-insensitive substring match over each message's ``content``
        field using full Unicode case folding via :meth:`str.casefold`
        (so e.g. German ``ß`` folds to ``ss``).  Matching only on parsed
        ``content`` avoids false positives from JSON structural elements
        (e.g. the word ``"user"`` matching every ``"role": "user"`` line).

        Ranking (higher is better)::

            score = (title_matches * _TITLE_BOOST)
                  + (content_matches / sqrt(1 + doc_chars / 1024))

        Title matches get a strong field boost - titles are short and
        intentional, so a hit there is strong evidence.  Content matches
        are normalized by a sqrt length factor so a long session with a
        casual mention doesn't outrank a short, focused one.  (Simpler
        than BM25's ``(1-b) + b*(dl/avgdl)`` because we avoid the
        two-pass scan needed for corpus stats.)  Sessions with zero
        matches are dropped.  Ties break by recency (existing
        ``list_sessions`` order - newest first).  Caps results at *limit*.
        Only the ``_SEARCH_SCAN_WINDOW`` most recent files are scored, so
        I/O stays bounded even with hundreds of sessions.
        """
        if not query or limit <= 0 or not self._dir.exists():
            return []
        needle = query.casefold()
        # (score, -rank, meta, needs_snippet)
        scored: list[tuple[float, int, dict, bool]] = []
        window = self.list_sessions()[:_SEARCH_SCAN_WINDOW]
        self._prune_search_memos({m["key"] for m in window})
        for rank, meta in enumerate(window):
            doc_chars, folded = self._folded_content(meta["key"])
            content_hits = folded.count(needle) if folded else 0
            title_hits = (meta.get("title") or "").casefold().count(needle)
            if not title_hits and not content_hits:
                continue
            length_norm = math.sqrt(1 + doc_chars / 1024)
            score = title_hits * _TITLE_BOOST + content_hits / length_norm
            # Negate rank so a smaller (newer) rank wins ties after score desc sort.
            scored.append((score, -rank, meta, content_hits > 0))
        scored.sort(reverse=True)

        # Snippets are attached AFTER the sort+slice, so the cost is proportional
        # to the rows actually returned rather than to every session that
        # matched. A snippet cannot come from the folded cache (it needs the
        # original text, so offsets line up), so building one per match put a
        # full re-read back on the hot path — dominating the query once the fold
        # itself was memoized.
        out: list[dict] = []
        for _score, _rank, meta, needs_snippet in scored[:limit]:
            snippet = self._content_snippet(meta["key"], query) if needs_snippet else ""
            out.append({**meta, "snippet": snippet} if snippet else meta)
        return out

    def _folded_content(self, key: str) -> tuple[int, str]:
        """Return ``(doc_chars, casefolded_content)`` for *key*, memoized by mtime.

        ``doc_chars`` counts the ORIGINAL (unfolded) characters, because it
        feeds the length normalizer in :meth:`search_sessions` and folding can
        change a string's length (``ß`` -> ``ss``).

        The folded blob joins the messages' string ``content`` fields in file
        order with ``\\x00``. That separator cannot appear in a user query, so
        it prevents a match spanning two messages while still allowing one
        ``count`` call over the whole session instead of one per message.

        Returns ``(0, "")`` for a missing/unreadable file or a session with no
        textual content. A read failure is deliberately NOT cached — see below.
        """
        path = self._path(key)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._folded_cache.pop(key, None)
            self._snippet_cache.pop(key, None)
            return (0, "")
        cached = self._folded_cache.get(key)
        if cached and cached[0] == mtime:
            return (cached[1], cached[2])
        # Cold: serialize against this key's writers for the whole
        # stat -> read -> store sequence.
        #
        # The mtime guard cannot protect this window, because the housekeeping
        # rewrites deliberately RESTORE the pre-write mtime (``_restore_mtime``,
        # so compaction does not reorder ``list_sessions``). A fold that started
        # before such a rewrite and stored after its ``_invalidate_cache`` would
        # sit in the cache holding pre-rewrite text under a mtime the file still
        # has — undetectable, so the newly saved messages would be missing from
        # every later search for the life of the process.
        #
        # ``_file_lock`` is the same in-process RLock every writer takes first in
        # ``_locked``, so holding it here orders this fold against append /
        # rewrite / metadata edits for this key. It is acquired ONLY on the miss
        # path: a warm search never contends, and two threads racing the same
        # cold key fold once (the re-check below).
        with self._file_lock(key):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                self._folded_cache.pop(key, None)
                self._snippet_cache.pop(key, None)
                return (0, "")
            cached = self._folded_cache.get(key)
            if cached and cached[0] == mtime:
                return (cached[1], cached[2])
            built = self._build_folded(key, mtime)
            if built is None:
                # The read failed rather than finding no content. Caching that
                # would be keyed by an mtime the file still has, so a session
                # made transiently unopenable (fd exhaustion, or a Windows
                # indexer / AV holding a just-written file — the same window
                # ``_METADATA_READ_ATTEMPTS`` exists for) would stay
                # unsearchable until something wrote to it again. Fail open:
                # report empty for this query and retry on the next one.
                return (0, "")
            self._folded_cache[key] = (mtime, built[0], built[1])
            return built

    def _prune_search_memos(self, live_keys: set[str]) -> None:
        """Free budget held by sessions that have left the scan window.

        Only runs for a memo that is currently refusing admissions, so an
        uncontended cache never pays the scan. Entries outside *live_keys* are
        unreachable by any future search (it walks only the
        ``_SEARCH_SCAN_WINDOW`` most recent sessions), so dropping them cannot
        cost a hit — which is what makes this safe where an LRU eviction would
        not be.

        Called before the walk, so a refusal raised *during* a walk is released
        on the NEXT query rather than that one: the prune needs the scan window,
        which the walk computes. The lag is one query (~20 ms for a keystroke
        caller) and self-clearing; what it prevents is budget staying pinned by
        aged-out sessions for the life of the process.
        """
        for cache in (self._folded_cache, self._snippet_cache):
            if cache.refused_since_prune():
                cache.retain(live_keys)

    def _build_folded(self, key: str, mtime: float) -> tuple[int, str] | None:
        """Parse *key* and fold its content — the cache-miss half of
        :meth:`_folded_content`.

        Returns ``None`` when the file could not be read, which the caller must
        distinguish from ``(0, "")`` (a session with no textual content): the
        former must not be cached.

        Reads the file via :meth:`_iter_message_texts` rather than going through
        :meth:`_read_messages`, for two reasons.

        Memory: that method memoizes the PARSED message dicts, and a search
        touches every session in the scan window, so routing the fold through it
        pins the whole corpus's parsed form in gateway RSS as a side effect of
        searching. On a 136 MB / 125-session corpus that is ~330 MB of parsed
        dicts versus ~37 MB for the folded strings this actually needs.

        Correctness: ``_msg_cache`` is filled by callers that do not hold this
        key's write lock, so an entry can be a pre-rewrite parse stored under a
        restored (unchanged) mtime. Folding from it would launder that staleness
        into the search cache, which the caller's lock cannot prevent. Reading
        the file makes the fold a function of the file alone.

        The caller holds ``_file_lock``, which orders this read against writers
        in THIS process. A writer in another process holds only the cross-process
        flock, so it can still interleave — but the caller stats BEFORE this read,
        so such a write leaves the cached mtime older than the file's and the next
        access re-folds. That case is self-healing, unlike the preserved-mtime
        rewrite the lock exists for.

        Separated from :meth:`_folded_content` so the memoization is observable:
        a caller (or a test) can count how often the expensive fold actually
        runs, independent of how many queries were served.
        """
        texts: list[str] = []
        try:
            texts = list(self._iter_message_texts(key))
        except OSError:
            return None
        if not texts:
            return (0, "")
        # Hand the same list to the snippet memo. The caller has already stat'ed
        # under ``_file_lock`` and passes that mtime, so both memos are keyed by
        # one observation of the file and cannot disagree about which revision
        # they hold. Storing here is why the second corpus costs no extra read.
        self._snippet_cache[key] = (mtime, texts)
        return (sum(len(t) for t in texts), "\x00".join(texts).casefold())

    def _iter_message_texts(self, key: str) -> Iterator[str]:
        """Yield each message's non-empty string ``content`` from *key*'s file.

        One definition of "what counts as searchable text in a session file",
        shared by the fold and the snippet so their skip rules cannot drift apart
        as the on-disk format evolves. Yields in file order; skips blank lines,
        unparseable lines, non-object lines, and the metadata header.

        A generator rather than a list so a caller can stop early — closing it
        closes the file — which is what lets :meth:`_content_snippet` read only as
        far as its first match. Propagates ``OSError``: callers distinguish "could
        not read" from "no text", and that distinction is load-bearing (a read
        failure must not be cached).
        """
        with open(self._path(key), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(data, dict) or data.get("_type") == "metadata":
                    continue
                content = data.get("content")
                if isinstance(content, str) and content:
                    yield content

    #: Characters of context kept before / after a snippet's match.
    _SNIPPET_LEAD = 40
    _SNIPPET_TRAIL = 100
    #: Hard cap on a returned snippet.
    _SNIPPET_MAX = 200

    def _snippet_texts(self, key: str) -> Iterator[str]:
        """Yield *key*'s message texts for snippet extraction, memo first.

        Prefers ``_snippet_cache`` — filled by :meth:`_build_folded` from the same
        read that produced the fold — and falls back to re-reading the file.

        The memo is validated against the file's current mtime, so it degrades to
        the file read rather than serving a stale snippet. That check is cheap
        relative to the parse it avoids, and unlike the fold this path does NOT
        need ``_file_lock``: a snippet is display-only, so the worst case for a
        preserved-mtime rewrite racing here is one stale preview line, not a
        session that stops matching. The fold — which decides whether a row
        appears at all — keeps the lock.

        Falls back for three reasons, all of which must stay non-fatal: the entry
        was refused admission by the byte budget, the fold cached ``(0, "")`` for
        a session with no text and so stored nothing, or the file changed since
        the fold. Propagates ``OSError`` from the fallback read, which
        :meth:`_content_snippet` already treats as "no snippet".
        """
        cached = self._snippet_cache.get(key)
        if cached is not None:
            try:
                if cached[0] == self._path(key).stat().st_mtime:
                    return iter(cached[1])
            except OSError:
                # Let the fallback read raise the OSError the caller handles,
                # rather than deciding here what a vanished file means.
                pass
        return self._iter_message_texts(key)

    def _content_snippet(self, key: str, query: str) -> str:
        """Return a match-centered window of *key*'s content around *query*.

        Streams the file and stops at the FIRST matching message, so a query that
        hits early reads only as far as the hit — rather than parsing the whole
        transcript, which on the largest sessions dominated the query. Sharing
        :meth:`_iter_message_texts` with the fold (instead of reading the file
        directly) also keeps search from pinning a parsed transcript in
        ``_msg_cache`` for every row it returns, and keeps the two halves of a
        query agreeing on what counts as searchable text.

        The window is confined to the matching message, which keeps a snippet from
        reading as one sentence when it actually spans two — consistent with
        :meth:`_folded_content`, where the ``\\x00`` join stops a match from
        bridging messages in the first place.

        Display-only and best effort: ``casefold`` is used for the search so it
        agrees with the hit detection in :meth:`search_sessions` (``.lower()``
        misses matches ``casefold`` finds, e.g. ``ß`` / ``İ``, which would yield
        an empty snippet despite a nonzero hit count), but folding can change a
        string's length, so the window may be off by a character or two.

        Returns ``''`` when the query is not locatable in any single message, or
        when the file cannot be read (display-only — never raises at the caller).
        """
        needle = query.casefold()
        if not needle:
            return ""
        try:
            for text in self._snippet_texts(key):
                pos = text.casefold().find(needle)
                if pos < 0:
                    continue
                start = max(0, pos - self._SNIPPET_LEAD)
                end = min(len(text), pos + len(query) + self._SNIPPET_TRAIL)
                frag = " ".join(text[start:end].split())
                prefix = "…" if start > 0 else ""
                suffix = "…" if end < len(text) else ""
                return (prefix + frag + suffix)[: self._SNIPPET_MAX]
        except OSError:
            return ""
        return ""

    def recent_from_source(
        self, source_prefix: str, exclude_key: str = "", max_messages: int = 20
    ) -> list[dict]:
        """Return recent messages from sessions matching *source_prefix*.

        Considers at most 50 matching files (newest mtime first) and stops after
        5 have contributed messages. Each contributing file costs a 5-line head
        read (to detect incognito/temporary sessions, which are skipped) plus a
        bounded tail read via :meth:`_read_tail_messages` — never a full-file
        read, which on large transcripts dominated the call.
        """
        if not self._dir.exists():
            return []
        safe_exclude = _safe_key(exclude_key) if exclude_key else ""
        safe_prefix = _safe_key(source_prefix)
        # Collect matching paths and sort by mtime (newest first)
        paths: list[Path] = []
        for path in self._dir.glob(f"{safe_prefix}*.jsonl"):
            if safe_exclude and path.stem == safe_exclude:
                continue
            paths.append(path)
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        candidates: list[dict] = []
        included = 0
        _max_scan = 50  # bound I/O even with many ephemeral sessions
        for path in paths[:_max_scan]:
            if included >= 5:
                break
            # The memory_mode marker lives in the metadata line at the head of the
            # file, so only the head is read here; the messages come from the tail.
            is_restricted = False
            try:
                with open(path, encoding="utf-8") as f:
                    for _, line in zip(range(5), f):
                        try:
                            d = json.loads(line.strip())
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if (
                            d.get("_type") == "metadata"
                            and str(d.get("memory_mode", "")).lower()
                            in INCOGNITO_MEMORY_MODES
                        ):
                            is_restricted = True
                            break
            except OSError:
                continue
            if is_restricted:
                continue
            included += 1
            candidates.extend(self._read_tail_messages(path, 50, None))
        # Sort by timestamp and return most recent
        candidates.sort(key=lambda m: m.get("ts", ""))
        return [{"role": m["role"], "content": m["content"]} for m in candidates[-max_messages:]]

    def read_messages(self, key: str) -> list[dict]:
        """Public access to session messages.

        The returned list may be the shared cached object (see
        :meth:`_read_messages`); treat it as read-only and copy before
        mutating.
        """
        return self._read_messages(key)

    def read_messages_chained(self, key: str) -> list[dict]:
        """Read messages from all session files sharing the same ``tab_id``.

        Returns messages from the current file only if no ``tab_id`` is set
        (legacy sessions).  Otherwise finds all sibling files with the same
        ``tab_id``, sorts chronologically, and concatenates their messages.

        Uses a ``_tab_id_index`` cache (built lazily, invalidated on save)
        to avoid scanning every file on each call.
        """
        meta = self.get_metadata(key)
        tid = meta.get("tab_id")
        if not tid:
            return self._read_messages(key)
        # Guard the lazy build/read of the shared _tab_id_index: this method is
        # reachable from worker threads (chat_persistence restore/save) while the
        # event loop may mark the index stale via invalidate_tab_id_cache().
        # Without the lock two threads could rebuild concurrently, or one could
        # read a half-built index. Rebuild only when it is missing/stale
        # (``None``); a freshly rebuilt index is AUTHORITATIVE, so a tid absent
        # from it genuinely has no sibling files right now. We deliberately do
        # NOT plant a permanent ``[]`` sentinel for a missing tid — the old
        # sentinel suppressed every future rebuild, so a second session opened
        # later under the same tab_id was never linked into the chain and its
        # messages silently vanished from recent_chained. Snapshot the key list
        # under the lock, then do the (potentially slow) per-file reads outside.
        with self._lock:
            if self._tab_id_index is None:
                self._rebuild_tab_id_index()
            index = self._tab_id_index or {}
            keys = list(index.get(tid, []))
        if not keys:
            return self._read_messages(key)
        all_msgs: list[dict] = []
        for k in keys:
            all_msgs.extend(self._read_messages(k))
        return all_msgs or self._read_messages(key)

    def _rebuild_tab_id_index(self) -> None:
        """Scan all dashboard session files and build tab_id → [keys] mapping.

        Caller MUST hold ``self._lock`` — this replaces the shared
        ``_tab_id_index`` mapping.
        """
        index: dict[str, list[str]] = {}
        for path in sorted(self._dir.glob("dashboard_chat-*.jsonl")):
            try:
                with path.open(encoding="utf-8") as f:
                    first_line = f.readline()
                m = json.loads(first_line)
                tid = m.get("tab_id")
                if tid:
                    index.setdefault(tid, []).append(path.stem.replace("_", ":", 1))
            except Exception:
                continue
        self._tab_id_index = index

    def invalidate_tab_id_cache(self) -> None:
        """Mark the tab_id index stale so it is rebuilt on the next chained read.

        Sets the index to ``None`` (rather than ``.clear()``-ing it) so
        read_messages_chained can distinguish "stale, must rebuild" from
        "freshly built, tid legitimately absent" — the distinction the removed
        ``[]`` sentinel used to get wrong. Guarded by ``self._lock`` so a
        concurrent rebuild/read on a worker thread can't observe a half-updated
        index.
        """
        with self._lock:
            self._tab_id_index = None

    def delete_session(self, key: str) -> bool:
        """Delete a session file. Returns True if a file was removed.

        The existence check and unlink run under ``_locked`` so a concurrent
        ``append`` / ``rewrite_session`` / ``update_metadata`` in this or any
        other process cannot race the delete — otherwise a writer holding the
        lock could complete its ``os.replace`` and resurrect the file we just
        removed, or we could unlink the file out from under an open descriptor
        and lose an acknowledged append. ``unlink(missing_ok=True)`` still
        tolerates a concurrent deletion of the same session (the TOCTOU race
        between our existence check and the unlink). On a wedged holder the lock
        acquire raises ``HistoryLockTimeout``; we report "not removed" rather
        than delete unlocked (the very clobber this lock prevents).
        """
        existed = False
        try:
            with self._locked(key):
                path = self._path(key)
                existed = path.exists()
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    return False
                # Also remove the derived summary sidecar (.summaries/<key>.json)
                # so a deleted session leaves no orphaned LLM-generated
                # description on disk — delete_session is contractually a
                # *permanent* removal. Best-effort: unlike the ``.lock`` sidecar
                # below, the summary cache is not a mutex inode, so reaping it is
                # safe, and a failure here must not fail the primary delete.
                try:
                    self._summary_cache_path(key).unlink(missing_ok=True)
                except OSError:
                    pass
        except HistoryLockTimeout:
            logger.warning("delete_session: lock timeout, not deleting key=%s", key)
            return False
        if existed:
            self._invalidate_cache(key)
            self.invalidate_tab_id_cache()
        # NB: we deliberately do NOT unlink the ``.lock`` sidecar here. The
        # sidecar's *inode* is the cross-process mutex: ``_locked`` opens the
        # path and ``flock``s the resulting fd. Reaping the file re-opens the
        # exact inode race the lock exists to prevent — after this delete
        # releases ``_locked``, a concurrent writer can recreate the session
        # and acquire the surviving sidecar; unlinking it then lets a later
        # acquirer create a *fresh* sidecar inode and lock that instead, so two
        # processes hold "the lock" on different inodes and can clobber each
        # other's writes (lost transcript update). A deleted session therefore
        # leaves a bounded, zero-byte ``.lock`` trace on purpose; cross-process
        # -safe reaping (under a separate directory-wide meta-lock that excludes
        # all session-lock acquisition) is deferred as future work.
        return existed

    def set_title(self, key: str, title: str) -> None:
        """Persist a title into the session's metadata line."""
        self.update_metadata(key, {"title": title})

    def update_metadata(self, key: str, fields: dict) -> None:
        """Merge *fields* into the session's metadata line and persist.

        Serialized behind the cross-process lock so a concurrent append or
        rewrite of the same session file — in this or any other process — can't
        clobber this metadata edit (and vice versa).
        """
        with self._locked(key):
            self._update_metadata_locked(key, fields)
        # A tab_id change re-links this session into (or out of) a chain, so the
        # cached tab_id→[keys] index is now stale. Invalidate OUTSIDE the lock
        # (it only touches in-process state) so the next chained read rebuilds.
        if "tab_id" in fields:
            self.invalidate_tab_id_cache()

    def _update_metadata_locked(self, key: str, fields: dict) -> None:
        """Merge *fields* into the session's metadata line and persist.

        Upsert semantics: if the session file does not exist yet (e.g. ``!ta
        <agent> --clean`` is issued before the first message is logged), the
        file is created with a fresh metadata line carrying *fields*.  Without
        this, the selection would live only in the in-memory caches and be lost
        on restart -- the session would silently resume under the default agent
        with the default toolset.
        """
        path = self._path(key)
        # A metadata-only edit (title/agent/folder/tab_id/pin) is not new
        # conversation activity — preserve the pre-write mtime so it doesn't
        # reorder list_sessions. None when the file is absent (upsert): a
        # genuinely new session should get a natural mtime. See _restore_mtime.
        prev_mtime = _safe_mtime(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        # Parse the existing metadata line, or synthesize a fresh one when the
        # file is absent/empty (the upsert case). A first line that exists but
        # isn't valid metadata is left untouched -- we never clobber it.
        if lines:
            try:
                meta = json.loads(lines[0])
            except json.JSONDecodeError:
                return
            if meta.get("_type") != "metadata":
                return
        else:
            self._dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "_type": "metadata",
                "created_at": metadata_now_iso(),
                "last_consolidated": 0,
            }
            lines = [""]  # placeholder; replaced below

        meta.update(fields)
        lines[0] = json.dumps(meta) + "\n"
        import os as _os
        import tempfile as _tf

        data = "".join(lines).encode("utf-8")
        fd, tmp = _tf.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            try:
                _os.write(fd, data)
                # Deliberately NO fsync here: this is a one-line metadata edit
                # (title/agent/folder/tab_id/pin) held under the cross-process
                # lock. os.replace() below is still crash-atomic (no torn file)
                # without a flush; fsync would only add power-loss durability,
                # which isn't worth blocking every other writer of this session
                # for a disk flush. Metadata is cheaply re-derivable/re-editable.
            finally:
                _os.close(fd)
            _os.replace(tmp, str(path))
        except Exception:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise
        _restore_mtime(path, prev_mtime)
        self._invalidate_cache(key)

    def mtime_of(self, key: str) -> float | None:
        """Return the session file's mtime for *key*, or ``None`` when absent.

        Cheap stat-only accessor for callers that need a file's last-write
        instant without reading it — e.g. the channel-slot reconciler, which
        uses it as the fallback close instant for a ``closed`` flag written
        before ``closed_at`` stamps existed.
        """
        try:
            return self._path(key).stat().st_mtime
        except OSError:
            return None

    def clear_closed(self, key: str, *, only_if_closed_before: float | None = None) -> None:
        """Remove the ``closed`` flag from a session's metadata line.

        Serialized behind the cross-process ``_locked`` so a concurrent append /
        rewrite / metadata edit — in this or any other process — can't clobber
        this edit (and vice versa). ``update_metadata`` only merges keys, so a
        dedicated remover is needed to drop ``closed`` when a session is resumed.
        No-op when the file is absent, unparsable, not flagged, or its first line
        isn't a metadata line. Housekeeping: the pre-write mtime is preserved so
        resuming doesn't reorder ``list_sessions``.

        *only_if_closed_before* makes the removal a compare-and-clear: the flag
        is dropped only when its close instant (``closed_at`` stamp, else the
        file's current mtime) is strictly older than the given epoch. Callers
        acting on a metadata snapshot (the channel-slot reconciler) use this so
        a ``closed`` written AFTER their snapshot — the user dismissing a tab
        mid-pass, or a racing reconcile — survives: the check runs under the
        same lock as the write, so there is no window between them.
        """
        path = self._path(key)
        with self._locked(key):
            if not path.exists():
                return
            prev_mtime = _safe_mtime(path)
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not lines:
                return
            try:
                meta = json.loads(lines[0])
            except json.JSONDecodeError:
                return
            if meta.get("_type") != "metadata" or "closed" not in meta:
                return
            if only_if_closed_before is not None:
                raw = meta.get("closed_at")
                close_time: float | None
                try:
                    close_time = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    close_time = None
                if close_time is None:
                    # Pre-stamp flag: the closing save is what last wrote the
                    # file, so its mtime approximates the close instant.
                    close_time = prev_mtime
                if close_time is not None and close_time >= only_if_closed_before:
                    return
            meta.pop("closed", None)
            meta.pop("closed_at", None)
            lines[0] = json.dumps(meta) + "\n"
            atomic_write(path, "".join(lines), fsync=False)
            _restore_mtime(path, prev_mtime)
        self._invalidate_cache(key)

    def _read_messages(self, key: str) -> list[dict]:
        """Read all non-metadata entries from a session JSONL file.

        Uses mtime-based caching to avoid re-parsing unchanged files.

        .. warning::
            On a cache hit this returns the **shared cached list object by
            identity** (see ``test_cache_hit_returns_same_object``) — the
            memoization that makes repeated reads O(1). Callers MUST treat the
            result as **immutable**: mutating it in place (append/pop/sort or
            editing a contained dict) silently corrupts the cache for every
            future reader, and — combined with cross-thread access — for other
            threads iterating the same list concurrently. Slice or ``list(...)``
            it before mutating. All current callers copy/slice; this contract
            keeps that invariant explicit.
        """
        path = self._path(key)
        if not path.exists():
            self._msg_cache.pop(key, None)
            return []
        # Retry transient read failures rather than reporting an empty session.
        # This method is on the open-tab restore path
        # (``read_messages_chained`` -> ``_rehydrate_slot_from_history``); on
        # Windows a just-written transcript can be briefly unopenable while an
        # indexer or AV scanner holds it (``ERROR_SHARING_VIOLATION`` ->
        # ``PermissionError``, an ``OSError`` subclass). An unhandled OSError
        # here propagates out of rehydrate and DROPS the tab, which is the
        # intermittent ``restored == N-1`` (``assert 7 == 8``) failure on the
        # Windows CI line. Mirror the retry ``_read_metadata`` already uses so
        # the tab keeps its full history instead of vanishing. The old bare
        # ``except OSError: return []`` on the stat silently yielded a
        # history-less tab on the same fault; retrying first recovers it.
        for attempt in range(_METADATA_READ_ATTEMPTS):
            try:
                mtime = path.stat().st_mtime
                cached = self._msg_cache.get(key)
                if cached and cached[0] == mtime:
                    return cached[1]
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
            except FileNotFoundError:
                # The transcript was deleted AFTER the exists() check above -- a
                # concurrent delete_session racing this read. That is NOT a
                # transient lock (retrying cannot bring the file back), and the
                # correct answer is an empty read: it matches the exists()-miss
                # branch above and the pre-PR stat() OSError branch. Letting it
                # fall through to the generic OSError arm below -- which now
                # re-raises on exhaustion -- would turn a benign race into an
                # HTTP 500 in a caller like api_session_detail that reaches
                # read_messages after its own exists() check (GPT review, PR
                # #2052). Return [] immediately, dropping any stale cache entry;
                # do not spend the retry budget on a file that is gone.
                self._msg_cache.pop(key, None)
                return []
            except OSError:
                if attempt + 1 < _METADATA_READ_ATTEMPTS:
                    self._pause_for_transient_retry()
                    continue
                # Out of retries. Do NOT swallow to [] here. A persistent read
                # failure is indistinguishable from a genuinely empty session,
                # and on the restore path _rehydrate_slot_from_history would then
                # register an EMPTY slot -- which restore_recent_sessions dedupes
                # by key and skips, stranding the tab history-less for the whole
                # session (GPT review, PR #2052). Re-raise instead so rehydrate
                # rolls back its partial slot and the tab is DROPPED rather than
                # registered empty -- recoverable on a later restore pass, most
                # reliably the next restart once the holder has released the file
                # (the same-startup restore_recent_sessions fallback only recovers
                # it if the file is readable by then). That matches the
                # "drop, don't register empty" outcome _read_metadata reaches by
                # returning {} on exhaustion. It is also the pre-retry behaviour:
                # the old unwrapped read_text() propagated this OSError with no
                # retry, so callers already tolerate it -- the loop above just
                # absorbs the transient case first.
                logger.warning(
                    "history: could not read messages for %s after %d attempts; "
                    "re-raising so restore can retry",
                    key,
                    _METADATA_READ_ATTEMPTS,
                    exc_info=True,
                )
                raise
            messages: list[dict] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("_type") == "metadata":
                    continue
                messages.append(data)
            self._msg_cache[key] = (mtime, messages)
            return messages
        return []

    #: Starting tail window (bytes) for :meth:`_read_tail_messages`. Sized to
    #: comfortably cover a few dozen JSONL message lines in one read; grown
    #: geometrically if it doesn't yield enough matching messages.
    _TAIL_MIN_BYTES = 8_192
    #: Rough average serialized-message size used to pick an initial window
    #: from ``max_messages`` so a large request opens a proportionally larger
    #: window on the first read.
    _TAIL_AVG_MSG_BYTES = 512
    #: Max window-doubling attempts before we accept whatever the window held
    #: (it is always correct: the newest messages are always inside the tail).
    _TAIL_MAX_GROWTHS = 6

    def _recent_via_tail(
        self, key: str, max_messages: int, roles: set[str] | None
    ) -> list[dict] | None:
        """Return the formatted recent window via a tail read, or None to defer.

        Returns None (caller falls through to the full-read path) when the file
        is missing OR a fresh full-parse cache already exists — in both cases
        the full path is at least as cheap and keeps the shared cache warm.
        Otherwise returns the last *max_messages* entries (role-filtered) as
        ``[{role, content}]``.

        The formatted window is memoized in ``_recent_cache`` keyed by
        (key, max_messages, roles) with the file mtime, so a session accessed
        only via ``recent()`` — which never warms ``_msg_cache`` — is served
        O(1) from memory on subsequent turns instead of re-opening and
        re-parsing the file tail on every call. The mtime guard makes the
        memo self-invalidating: an :meth:`append` bumps the file mtime, so the
        stale entry misses and is recomputed. A fresh list of fresh dicts is
        returned each call so callers can freely mutate the result without
        corrupting the shared entry.
        """
        path = self._path(key)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None  # missing/unreadable → let the full path return []
        cached = self._msg_cache.get(key)
        if cached and cached[0] == mtime:
            return None  # fresh full cache → full path is a cheap O(1) hit
        rc_key = self._recent_cache_key(key, max_messages, roles)
        rc = self._recent_cache.get(rc_key)
        if rc is not None and rc[0] == mtime:
            return [dict(m) for m in rc[1]]  # memo hit — no disk I/O
        tail = self._read_tail_messages(path, max_messages, roles)
        formatted = [{"role": m["role"], "content": m["content"]} for m in tail]
        self._recent_cache[rc_key] = (mtime, formatted)
        return [dict(m) for m in formatted]

    @staticmethod
    def _recent_cache_key(key: str, max_messages: int, roles: set[str] | None) -> str:
        """Build a stable ``_recent_cache`` key from the recent() parameters.

        ``\\x00`` cannot appear in a session key, so it is an unambiguous field
        separator; roles are sorted so ``{"user", "assistant"}`` and
        ``{"assistant", "user"}`` collapse to the same entry.
        """
        roles_part = ",".join(sorted(roles)) if roles else ""
        return f"{key}\x00{max_messages}\x00{roles_part}"

    def _read_tail_messages(
        self, path: Path, max_messages: int, roles: set[str] | None
    ) -> list[dict]:
        """Read the last *max_messages* messages by seeking to the file tail.

        Reads a bounded window from the END of the file and parses backwards,
        growing the window geometrically until it holds at least *max_messages*
        matching (role-filtered) messages or the window covers the whole file.
        The newest messages always sit at EOF, so the returned slice is always
        the true most-recent window — correctness is identical to a full parse
        followed by ``[-max_messages:]``; only older lines beyond the window are
        skipped, and those can never be part of the answer.

        Metadata lines and lines that don't parse as JSON are ignored, matching
        :meth:`_read_messages`. Never populates ``_msg_cache`` (the result is a
        partial view, not the authoritative full list).
        """
        if max_messages <= 0:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        window = max(self._TAIL_MIN_BYTES, max_messages * self._TAIL_AVG_MSG_BYTES * 2)
        messages: list[dict] = []
        for _ in range(self._TAIL_MAX_GROWTHS):
            covered = size <= window
            try:
                with open(path, "rb") as f:
                    if not covered:
                        f.seek(size - window)
                        f.readline()  # discard the (likely partial) first line
                    raw = f.read().decode("utf-8", errors="replace")
            except OSError:
                return []
            messages = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("_type") == "metadata":
                    continue
                if roles and data.get("role") not in roles:
                    continue
                messages.append(data)
            if covered or len(messages) >= max_messages:
                break
            window *= 4
        return messages[-max_messages:]

    def _last_row_ts(self, key: str) -> str | None:
        """The ``ts`` of the last message row on disk for *key*, or ``None``.

        Call this under :meth:`_locked`. The answer is only authoritative while
        no other writer can append, and the cross-process flock that ``_locked``
        takes is what makes that true for a subagent / cron / CLI writing the
        same session file from another process. ``append`` skips this entirely
        for a file it just created, which provably holds no rows yet.

        Reuses :meth:`_read_tail_messages` rather than parsing the file, so this
        stays a bounded read on a long transcript. That reader also grows its
        window when the last line is larger than it, so a single long reply
        cannot hide the row behind it.

        Measured on a 1.6 MB / 400-row transcript this is ~48 us, against ~38 us
        for the ``open`` + ``write`` the same critical section already performs
        and ~2.4 ms for the whole-file read ``_maybe_rotate`` does when it
        rotates -- 0.4% of an ``append`` call, which is ~11.8 ms end to end. A
        remembered-tail cache was tried here and removed: it would have needed a
        cheap stamp of the file's identity to know when to distrust itself, and
        the only cheap ones (size, mtime) are exactly the coarse-resolution
        signals this module now exists to stop trusting.
        """
        tail = self._read_tail_messages(self._path(key), 1, None)
        if not tail:
            return None
        ts = tail[-1].get("ts")
        return ts if isinstance(ts, str) and ts else None

    def _invalidate_cache(self, key: str) -> None:
        """Invalidate caches for a key after a write operation."""
        self._msg_cache.pop(key, None)
        self._meta_cache.pop(key, None)
        # The folded search blob is derived from the messages, so it goes stale
        # exactly when they do. Its own mtime guard is not enough here: the
        # housekeeping rewrites below restore the pre-write mtime.
        self._folded_cache.pop(key, None)
        # Same reasoning for the snippet source: it is the raw form of what the
        # fold is derived from, so it goes stale at exactly the same moment. Its
        # own mtime check would miss the preserved-mtime rewrites below, and a
        # missed invalidation here shows the user a preview line quoting text
        # that is no longer in the session.
        self._snippet_cache.pop(key, None)
        # Also drop any memoized recent() windows for this key. Necessary
        # because housekeeping rewrites (mark_consolidated/update_metadata/
        # rewrite_session/rotation) restore the pre-write mtime via
        # _restore_mtime, so the recent cache's mtime guard alone would let a
        # stale window survive a content change.
        self._recent_cache.pop_prefix(f"{key}\x00")

    #: Bytes read from the end of a session file for the last-message preview.
    #: One tail block comfortably covers several trailing JSONL lines without
    #: paying a full-file parse on large sessions.
    _PREVIEW_TAIL_BYTES = 16_384
    #: Max characters returned in a last-message preview.
    _PREVIEW_MAX_CHARS = 120

    def last_message_preview(self, key: str) -> str:
        """Return a short preview of the session's last message ('' if none).

        Reads only the tail of the JSONL file (bounded), scanning backwards
        for the newest parseable message line — cheap even on large sessions.
        Handles both plain-string ``content`` and structured list-form content
        blocks (text extracted from ``{"type": "text"}`` / ``text`` fields).
        If the initial tail window yields nothing parseable (a single trailing
        line larger than the window), retries once with a 16× window before
        giving up.
        """
        path = self._path(key)
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        for window in (self._PREVIEW_TAIL_BYTES, self._PREVIEW_TAIL_BYTES * 16):
            try:
                with open(path, "rb") as f:
                    if size > window:
                        f.seek(size - window)
                        f.readline()  # discard the (likely partial) first line
                    tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                return ""
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("_type") == "metadata":
                    continue
                text = self._content_text(data.get("content"))
                if not text:
                    continue
                preview = strip_markdown_preview(text)
                if not preview:
                    continue
                if len(preview) > self._PREVIEW_MAX_CHARS:
                    preview = preview[: self._PREVIEW_MAX_CHARS].rstrip() + "…"
                return preview
            if size <= window:
                break  # the window already covered the whole file — no retry
        return ""

    @staticmethod
    def _content_text(content: object) -> str:
        """Best-effort plain text from a message ``content`` field.

        Plain strings pass through; list-form content blocks (the structured
        shape newer turns use) contribute their ``text`` fields in order.
        Anything else yields ''.
        """
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t)
            return " ".join(p.strip() for p in parts if p.strip())
        return ""

    def get_metadata(self, key: str) -> dict:
        """Return session metadata for *key*."""
        return self._read_metadata(key)

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        """Return ``(metadata, readable)`` for *key*.

        ``readable`` is ``False`` only when the transcript exists but its
        metadata line could not be read after
        :data:`_METADATA_READ_ATTEMPTS` attempts. Every other empty result --
        no transcript at all, an empty first line, an undecodable one -- is a
        genuine answer and reports ``True``.

        :meth:`get_metadata` cannot express that difference: it returns ``{}``
        for both, and a caller that reads ``{}`` as "this session was never
        persisted" will discard live state. Use this instead of
        :meth:`get_metadata` wherever an empty result triggers something
        destructive.
        """
        return self._read_metadata_status(key)

    def _pause_for_transient_retry(self) -> None:
        """Pause briefly before retrying a transient read, but ONLY off the loop.

        Shared by :meth:`_read_metadata` and :meth:`_read_messages`. Both are
        reached ON the event loop by ``restore_open_slots_async`` (which keeps
        the whole restore on the loop deliberately: creating a slot broadcasts
        through ``asyncio.Queue.put_nowait`` / ``Event.set``, neither
        thread-safe). A kernel sleep there stops ``_loop_heartbeat`` from petting
        the LoopStallWatchdog -- whose ``exit_after`` timer then kills the
        gateway, the exact crash-loop the async restore exists to prevent. So
        sleep only when NOT on a running loop; on the loop the retry is
        immediate (a stat plus an open -- cheap enough to be worth taking).
        """
        on_loop = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop = False
        if not on_loop:
            _time.sleep(_METADATA_READ_RETRY_SECS)

    def _read_metadata(self, key: str) -> dict:
        """Read the metadata line (first line) from a session JSONL file.

        Thin wrapper over :meth:`_read_metadata_status` that drops the
        readability flag, so the many callers for whom "empty" and "unreadable"
        are equivalent keep a plain ``dict`` return.
        """
        return self._read_metadata_status(key)[0]

    def _read_metadata_status(self, key: str) -> tuple[dict, bool]:
        """Read the metadata line, reporting whether the read itself succeeded.

        Uses mtime-based caching to avoid re-reading unchanged files.

        Returns ``({}, True)`` for a session that genuinely has no metadata and
        ``({}, False)`` when the file exists but could not be read after
        retries. The retry absorbs the common transient case; the flag exists
        because absorbing it is not always possible, and at least one caller
        does something destructive with a confident empty answer: the open-tab
        restore treats ``{}`` as "never persisted" and drops the tab. Windows
        makes this more than theoretical: a freshly written file can be briefly
        unopenable while an indexer or AV scanner holds it
        (``ERROR_SHARING_VIOLATION``), which surfaces as ``PermissionError`` --
        an ``OSError`` subclass.
        """
        path = self._path(key)
        if not path.exists():
            self._meta_cache.pop(key, None)
            return {}, True
        for attempt in range(_METADATA_READ_ATTEMPTS):
            try:
                mtime = path.stat().st_mtime
                cached = self._meta_cache.get(key)
                if cached and cached[0] == mtime:
                    return cached[1], True
                # Read ONLY the first line. The previous form slurped the entire
                # file via read_text() and then threw all but the first line away
                # — on a 26 MB transcript that is ~10ms and ~26 MB of transient
                # allocation to obtain a few hundred bytes (measured ~32x slower
                # than readline()). Startup restore calls this once per tab, so
                # the waste scaled with both tab count and transcript size,
                # pushing the event loop toward the stall watchdog.
                with open(path, encoding="utf-8") as fh:
                    first = fh.readline().strip()
            except OSError:
                if attempt + 1 < _METADATA_READ_ATTEMPTS:
                    # Pause before retrying (off-loop only -- see the helper).
                    self._pause_for_transient_retry()
                    continue
                # Out of retries. Distinguish this from "no metadata" in the log
                # so a dropped tab is traceable to its cause instead of looking
                # like a session that was never written.
                logger.warning(
                    "history: could not read metadata for %s after %d attempts; "
                    "reporting no metadata",
                    key,
                    _METADATA_READ_ATTEMPTS,
                    exc_info=True,
                )
                return {}, False
            if not first:
                return {}, True
            try:
                data = json.loads(first)
                # ``isinstance`` guard, not just the _type check: a first line
                # that is valid JSON but not an OBJECT (``null``, a list, a bare
                # string, a number) would make ``.get`` raise AttributeError
                # rather than JSONDecodeError. That is a corrupt metadata line
                # exactly like an undecodable one, so report it the same way --
                # an empty dict that IS a genuine answer -- instead of throwing a
                # non-OSError out of a read that callers treat as total.
                meta = (
                    data
                    if isinstance(data, dict) and data.get("_type") == "metadata"
                    else {}
                )
            except json.JSONDecodeError:
                meta = {}
            self._meta_cache[key] = (mtime, meta)
            return meta, True
        return {}, True

    def sliding_window(self, key: str, keep_recent: int = 5) -> tuple[list[dict], list[dict]]:
        """Split messages into (older, recent) for compaction.

        *keep_recent* is the number of recent user/assistant pairs to retain.
        Returns ``(older_messages, recent_messages)``.
        """
        messages = self._read_messages(key)
        # keep_recent pairs = keep_recent * 2 individual messages
        split = max(0, len(messages) - keep_recent * 2)
        return messages[:split], messages[split:]

    def rewrite_session(self, key: str, messages: list[dict]) -> None:
        """Rewrite session JSONL with only the given messages."""
        # Serialize the full rewrite against concurrent appends — in this or
        # any other process — so a live session's writes aren't lost, and (via
        # atomic_write below) so a crash can't truncate the transcript.
        with self._locked(key):
            self._rewrite_session_locked(key, messages)

    def _rewrite_session_locked(self, key: str, messages: list[dict]) -> None:
        path = self._path(key)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Compaction is housekeeping, not new activity — preserve the pre-write
        # mtime so it doesn't reorder list_sessions (see _restore_mtime).
        prev_mtime = _safe_mtime(path)
        # Archive only messages being dropped (old content minus what's being kept).
        # Compare by normalized JSON (sort_keys) to be resilient to key ordering changes.
        if path.exists():
            old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if old_lines and '"_type"' in old_lines[0]:
                old_lines = old_lines[1:]
            kept_serialized = {json.dumps(m, sort_keys=True) for m in messages}
            dropped = []
            for ln in old_lines:
                if not ln.strip():
                    continue
                try:
                    normalized = json.dumps(json.loads(ln), sort_keys=True)
                except (json.JSONDecodeError, ValueError):
                    dropped.append(ln)  # corrupted line → archive it
                    continue
                if normalized not in kept_serialized:
                    dropped.append(ln)
            try:
                _archive_lines(key, dropped, reason="compact", base=self._dir)
            except Exception:
                logger.warning("Failed to archive dropped lines for %s", key, exc_info=True)
        # Rebuild only the fields compaction owns; everything else on the metadata
        # line belongs to another layer (the rotation generation an in-flight
        # consolidation checks, its retry accounting, the slot's title/agent/pin)
        # and is carried through verbatim. Enumerating what to keep is how those
        # fields got dropped in the first place.
        orig_meta = self.get_metadata(key) or {}
        meta = {
            "_type": "metadata",
            "created_at": orig_meta.get("created_at", metadata_now_iso()),
            "last_consolidated": orig_meta.get("last_consolidated", 0),
            "compacted_at": metadata_now_iso(),
        }
        carry_unowned_metadata(meta, orig_meta, _COMPACT_OWNED_META_KEYS)
        lines = [json.dumps(meta) + "\n"]
        for m in messages:
            lines.append(json.dumps(m) + "\n")
        atomic_write(path, "".join(lines))
        _restore_mtime(path, prev_mtime)
        self._invalidate_cache(key)

    def _maybe_rotate(self, path: Path) -> None:
        """Rotate a session file that exceeds the byte limit.

        Keeps the metadata line plus at most ``_SESSION_KEEP_LINES`` trailing
        messages. When a file is oversized because of a handful of very large
        messages — i.e. it has ``<= _SESSION_KEEP_LINES`` lines but still blows
        the byte budget — it now drops the OLDEST messages until it fits instead
        of returning early. Previously the ``len(lines) <= _SESSION_KEEP_LINES``
        guard let such a file grow without bound (a session of a few multi-MB
        messages would never rotate), defeating the size cap entirely.

        Callers hold the per-session lock (this is invoked from ``append`` under
        ``_locked``); it does not acquire the lock itself.
        """
        try:
            if path.stat().st_size <= _SESSION_MAX_BYTES:
                return
        except OSError:
            return
        # Rotation is triggered right after a genuine append; preserve that
        # append's mtime rather than re-stamping to "now" (see _restore_mtime).
        prev_mtime = _safe_mtime(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        meta_line = lines[0] if lines and '"_type"' in lines[0] else ""
        msg_lines = lines[1:] if meta_line else lines[:]
        if not msg_lines:
            return
        meta_bytes = len(meta_line.encode("utf-8"))

        def _kept_bytes(n: int) -> int:
            return meta_bytes + sum(len(ln.encode("utf-8")) for ln in msg_lines[-n:])

        # Start from the normal line cap, then shrink further while the retained
        # tail still exceeds the byte budget — this is what lets a file with
        # <= _SESSION_KEEP_LINES lines still rotate.
        keep_count = min(_SESSION_KEEP_LINES, len(msg_lines))
        while keep_count > 1 and _kept_bytes(keep_count) > _SESSION_MAX_BYTES:
            keep_count -= 1
        if keep_count >= len(msg_lines):
            # Keeping every message would drop nothing — the file is oversized
            # because of a single message larger than the whole budget, which we
            # can't split. Leave it rather than pointlessly rewriting.
            return

        kept = msg_lines[-keep_count:]
        dropped = msg_lines[:-keep_count]
        try:
            _archive_lines(path.stem, dropped, reason="rotate", base=self._dir)
        except Exception:
            logger.warning("Failed to archive rotated lines for %s", path.stem, exc_info=True)

        # Reset last_consolidated since offsets are now invalid, and bump the
        # rotation generation counter. Resetting the offset to 0 only protects
        # the case where the file shrank BELOW a stale consolidation snapshot;
        # a rotation that retains >= the snapshot offset leaves ``offset <=
        # msg_count`` true while every surviving index has shifted by the number
        # of dropped lines, so a stale offset written by a concurrent
        # consolidator would silently mark never-consolidated retained messages
        # as done. The monotonically-increasing generation lets
        # ``mark_consolidated`` detect ANY rotation between snapshot and write,
        # regardless of retained count (absent field == 0 for legacy files).
        if meta_line:
            try:
                meta = json.loads(meta_line)
                meta["last_consolidated"] = 0
                meta["rotated_at"] = metadata_now_iso()
                meta["rotation_generation"] = int(meta.get("rotation_generation", 0) or 0) + 1
                meta_line = json.dumps(meta) + "\n"
            except json.JSONDecodeError:
                pass

        content = meta_line + "".join(kept)
        atomic_write(path, content)
        _restore_mtime(path, prev_mtime)
        # Invalidate cache — offsets changed
        safe = path.stem
        self._invalidate_cache(safe)
        logger.info(
            "Rotated session file %s (%d → %d lines)",
            path.name,
            len(lines),
            len(kept) + (1 if meta_line else 0),
        )


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
    # KiroCrew's own credential file. The data home moved to ~/.kiro/crew, so the
    # LIVE secret is ~/.kiro/crew/.env; cover the pre-move legacy home too
    # (substring match, so bare "/.env"-suffixed forms).
    ".kiro/crew/.env",
    ".kirocrew/.env",
    "169.254.169.254",  # IMDS
)


_TOOL_ROLES: frozenset[str] = frozenset({"tool", "tool_call", "tool_result"})


def _frontmatter_value(text: str | None, key: str) -> str:
    """Return a single-line frontmatter value from a SKILL.md body, or ""."""
    if not text:
        return ""
    m = re.match(r"^\s*---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return ""
    for ln in m.group(1).split("\n"):
        if ":" in ln and ln.split(":", 1)[0].strip() == key:
            return ln.split(":", 1)[1].strip()
    return ""


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
    leading block is returned unchanged (stripped).
    """
    if not text:
        return ""
    m = re.match(r"^\s*---\n.*?\n---\n?(.*)$", text, re.DOTALL)
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

    def retry_eligible(
        self, key: str, now: float | None = None, message_count: int | None = None
    ) -> bool:
        """True when *key* may spend a billed consolidation turn right now.

        Every automatic entry point consults this so a span whose consolidation
        keeps failing backs off instead of re-billing an LLM turn on each sweep.
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

    async def _note_failed_attempt(
        self, key: str, span: AttemptedSpan, reason: str
    ) -> None:
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
            logger.warning(
                "Could not persist consolidation retry state for %s", key, exc_info=True
            )
            return
        if attempts < 1:
            # The session was deleted mid-consolidation, so nothing was recorded
            # and there is no span left to abandon.
            return
        if attempts < _CONSOLIDATION_MAX_ATTEMPTS:
            logger.warning(
                "Consolidation attempt %d/%d failed for %s (%s); "
                "next attempt in %.0fs",
                attempts,
                _CONSOLIDATION_MAX_ATTEMPTS,
                key,
                reason,
                max(0.0, retry_at - _time.time()),
            )
            return
        logger.warning(
            "Abandoning consolidation for %s after %d failed attempts (%s): "
            "marking %d messages consolidated WITHOUT a memory pass, so this "
            "span's history/preferences/lessons are not extracted",
            key,
            attempts,
            reason,
            span.total,
        )
        try:
            await asyncio.to_thread(
                self._log.mark_consolidated, key, span.total, span.generation
            )
        except Exception:
            # The count stays at the cap, so retry_eligible() keeps refusing —
            # the span stops spending even though the marker is missing.
            logger.warning(
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
            logger.warning(
                "Could not persist consolidation environment backoff for %s",
                key,
                exc_info=True,
            )
            return
        if failures < 1:
            return
        logger.warning(
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
        if key in self._running:
            return
        total = len(self._log._read_messages(key))
        prefs_off = self._prefs_offset.get(key, 0)
        if total - prefs_off < _CONSOLIDATION_THRESHOLD:
            return
        self._running.add(key)
        t = asyncio.create_task(self._consolidate(key, include_history=False))
        self._tasks.add(t)

        def _on_done(fut: asyncio.Task, k: str = key, off: int = total) -> None:  # type: ignore[type-arg]
            self._tasks.discard(fut)
            if not fut.cancelled() and fut.exception() is None:
                self._prefs_offset[k] = off

        t.add_done_callback(_on_done)

    def check_idle_sessions(self) -> None:
        """Check all tracked sessions for idle-based history consolidation."""
        now = _time.time()
        for key, last in list(self._last_activity.items()):
            if now - last < self._history_idle_secs:
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
                if not fut.cancelled() and fut.exception() is None:
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
        total, unconsolidated = self._log.consolidation_counts(key)
        if unconsolidated < 1:
            return
        # This path consults no time-based throttle at all — every session expiry
        # for the same key fires a fresh consolidation — so the durable backoff is
        # the only thing standing between a repeatedly failing span and one billed
        # LLM turn per expiry.
        if not self.retry_eligible(key, message_count=total):
            logger.info(
                "consolidate_session skipped for %s: consolidation retry backoff", key
            )
            return
        # Short-circuit sensitive sessions before scheduling a task
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            logger.info("consolidate_session skipped for %s: sensitive session", key)
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
                self._history_consolidated[k] = _time.time()
            else:
                logger.warning("consolidate_session failed for %s: %s", k, exc)

        t.add_done_callback(_on_done)

    async def consolidate_now(self, key: str) -> None:
        """Consolidate a session synchronously (blocking).

        Unlike consolidate_session() which is fire-and-forget, this awaits
        completion. Used by the CLI command.

        Safety: defense-in-depth — also checked inside _consolidate().
        """
        if self._log.unconsolidated_count(key) < 1:
            return
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            logger.info("consolidate_now skipped for %s: sensitive session", key)
            return
        await self._consolidate(key, include_history=True)

    async def _consolidate(self, key: str, include_history: bool = True) -> None:
        """Run LLM consolidation for a session."""
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
            if not unconsolidated:
                return
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

            # Resolve workspace-scoped memory from session metadata
            meta = self._log.get_metadata(key)
            ws_name = meta.get("workspace")
            if ws_name:
                from kiro_crew.context import ContextBuilder

                memory = ContextBuilder.get_memory_for(ws_name)
            else:
                memory = self._memory

            conversation = "\n".join(_fmt_message(m) for m in unconsolidated)

            current_prefs = memory.read_preferences()
            current_projects = memory.read_projects()

            # Build prompt keys dynamically based on consolidation type
            keys: list[str] = []
            if include_history:
                keys.append(
                    '"history_entry": A concise paragraph (2-5 sentences) summarizing '
                    "what happened. Use local time [YYYY-MM-DD HH:MM]. Focus on "
                    "decisions, outcomes, facts. Use user's real name if known."
                )

            # Structured memory extraction (when vector store is available)
            has_vector = self._vector_store is not None
            if has_vector and self._vector_store is not None:
                # Offload: the fetch serializes on the store's _db_lock (#1947),
                # and this coroutine runs on the gateway event loop — a worker
                # holding the lock (backfill's FAISS rebuild, reconcile's bulk
                # UPDATEs) would otherwise block the whole loop here.
                current_semantic = await asyncio.to_thread(self._vector_store.get_all_semantic)
                semantic_json = (
                    json.dumps(
                        [
                            {k: e[k] for k in ("key", "value_json", "confidence")}
                            for e in current_semantic
                        ],
                        indent=1,
                    )
                    if current_semantic
                    else "[]"
                )
                keys.append(
                    '"semantic": Array of structured facts to remember long-term. '
                    'Each: {"key": "<dotted.key>", "value": <json_value>, "confidence": 0.0-1.0, '
                    '"delete": false}. '
                    "Rules: keys must start with pref.*, project.*, or user.* "
                    "(e.g. pref.color, user.favorite_language, project.name). "
                    "confidence 1.0 = user stated, 0.8-0.9 = clearly implied, <0.8 = uncertain (rejected). "
                    "value must be a JSON primitive (string, number, boolean) — NOT objects or arrays. "
                    "IMPORTANT: Check existing semantic memory above. If a key already covers "
                    "the same topic, UPDATE that key instead of creating a new one. "
                    "Do NOT create near-duplicate keys (e.g. project.x.approach AND project.x.refined). "
                    'To DELETE a stale/invalidated key, set "delete": true (e.g. pet died → delete '
                    "user.pet.name; project cancelled → delete project.x.status). "
                    f"Max {_MAX_SEMANTIC_PER_CONSOLIDATION} items."
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
            if not self._migrated:
                keys.append(
                    '"preferences_update": The COMPLETE updated preferences file. '
                    "Merge duplicates, keep only newest if contradicted, remove stale "
                    "one-off observations. Keep '# User Preferences' header. "
                    "Return existing content exactly if nothing changed."
                )
                keys.append(
                    '"projects_update": The COMPLETE updated projects file. '
                    "Only active projects, remove stale entries, update facts. "
                    "Keep '# Active Projects' header. Return existing if unchanged."
                )

            if include_history:
                keys.append(
                    '"lessons": Array of corrections the user taught '
                    '(e.g. "no, do X", "always Y", "never Z"). '
                    'Each: {"rule": "...", "negative": "...", "category": "tool|preference|knowledge"}. '
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
            if not self._migrated:
                prompt_parts.append(f"\n\n## Current Preferences\n{current_prefs or '(empty)'}")
                prompt_parts.append(f"\n\n## Current Projects\n{current_projects or '(empty)'}")
            prompt_parts.append(f"\n\n## Conversation to Process\n{conversation}")
            prompt_parts.append("\n\nRespond with ONLY valid JSON, no markdown fences.")
            prompt = "".join(prompt_parts)

            try:
                result = await self._call_llm(prompt)
            except _ConsolidationNotDispatched as exc:
                # Nothing was sent, so nothing was billed. Charging this to the
                # attempt cap would let a handful of environment failures abandon
                # the span — writing the durable marker over messages no LLM has
                # ever read, which is the exact false-abandonment this accounting
                # exists to prevent. Arm the backoff only, so a broken host retries
                # on a widening interval instead of on every 60s tick.
                if include_history:
                    await self._note_environment_failure(key, str(exc))
                return
            billed = True
            if not result:
                # The turn reached the provider and produced nothing usable, so it
                # was spent while the marker below stays unwritten. Returning
                # silently would look like success to the done-callbacks, setting
                # the in-memory throttle while the durable count still says
                # unconsolidated: the span re-bills a full turn every idle window,
                # and immediately after every restart. Charge the attempt.
                if include_history:
                    await self._note_failed_attempt(
                        key, attempted, "empty LLM result"
                    )
                return

            if entry := result.get("history_entry"):
                # Offloaded to a worker thread: append_history takes a blocking
                # advisory file lock (cross-process) and does synchronous file
                # IO, and _consolidate runs on the event loop thread (fired via
                # asyncio.create_task). Running it inline would let cross-process
                # lock contention stall the whole gateway loop.
                await run_in_embed_pool(memory.append_history, entry)
                logger.info("Consolidated %d messages for %s", len(unconsolidated), key)

            # Structured memory writes (Phase 2/3). Offloaded to a worker thread:
            # _write_structured_memory embeds each item via a blocking urllib call
            # to the in-process embedder, and _consolidate runs on the event loop thread (fired via
            # asyncio.create_task). Running it inline stalls the whole gateway loop
            # if the embedding endpoint is slow/hung (heartbeats, Slack, dashboard).
            if self._vector_store:
                await run_in_embed_pool(self._write_structured_memory, result, key)

            # Markdown writes (backward compat — skip if migrated)
            if not self._migrated:
                if prefs := result.get("preferences_update"):
                    if prefs.strip() != current_prefs.strip():
                        memory.write_preferences(prefs)

                if projects := result.get("projects_update"):
                    if projects.strip() != current_projects.strip():
                        memory.write_projects(projects)

            # Lesson extraction: _save_lessons calls write_lesson which embeds
            # each rule (+ up to 5 lazy backfills) via blocking urllib to Ollama.
            # Same rationale as _write_structured_memory above — must offload.
            if (self._lesson_store or self._vector_store) and (
                raw_lessons := result.get("lessons")
            ):
                await run_in_embed_pool(self._save_lessons, raw_lessons)

            # Auto skill detection — a SEPARATE LLM pass over the full-session
            # window (see _run_skill_detection), not the incremental tail. Runs
            # only on history consolidation, guarded by flag + loader; failures
            # are logged, never fatal.
            if include_history and self._auto_skills_enabled and self._skills_loader is not None:
                try:
                    await self._run_skill_detection(key)
                except Exception:
                    logger.warning("Auto-skill detection failed for %s", key, exc_info=True)

            # Autonomous lifecycle: age-based archival must run even when this
            # pass created/approved no skill, otherwise skills never age out on
            # their own (create/approve were the only triggers). Consolidation is
            # the existing idle/periodic path; throttle to at most once/hour
            # across all sessions so frequent consolidations don't rescan the set.
            if self._skills_loader is not None and (_time.time() - self._last_lifecycle) > 3600:
                self._last_lifecycle = _time.time()
                try:
                    await asyncio.to_thread(
                        self._skills_loader.run_skill_lifecycle,
                        max_auto_skills=self._max_auto_skills,
                        stale_after_days=self._stale_after_days,
                        archive_after_days=self._archive_after_days,
                    )
                except Exception:
                    logger.debug("Periodic skill lifecycle pass failed", exc_info=True)

            # Only advance the consolidated offset for history consolidation.
            # Prefs-only consolidation uses a separate in-memory offset.
            # mark_consolidated does a synchronous, fsync-backed rewrite of the
            # whole transcript (up to a couple of MB) behind the per-file lock.
            # _consolidate runs on the gateway event loop (fired via
            # asyncio.create_task), so offload the blocking rewrite to a worker
            # thread — otherwise a slow filesystem freezes the loop (heartbeats,
            # Slack, dashboard). Same rationale as the offloads above.
            if include_history:
                await asyncio.to_thread(
                    self._log.mark_consolidated,
                    key,
                    total,
                    generation_at_snapshot,
                )

        except Exception:
            logger.exception("Consolidation failed for %s", key)
            # Anything raised between the LLM call and mark_consolidated (memory
            # writes, lesson writes, the marker write itself) re-raises, so the
            # idle sweep's done-callback never sets its throttle and all of its
            # skip conditions are false again on the next 60s tick. Charging the
            # attempt here is what converts that tight loop into backoff.
            if billed and include_history:
                await self._note_failed_attempt(
                    key, attempted, "exception after the LLM call"
                )
            raise
        finally:
            self._running.discard(key)

    async def _run_skill_detection(self, key: str) -> None:
        """Detect a reusable skill from the FULL session (bounded window).

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
        """
        if self._skills_loader is None:
            return
        all_messages = await asyncio.to_thread(self._log._read_messages, key)
        if not all_messages:
            return
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
            return
        window = all_messages[-_SKILL_DETECTION_WINDOW:]
        if _count_tool_call_messages(window) < self._auto_min_tool_calls:
            return
        if _session_touched_sensitive(window):
            return

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
            "session contained a non-trivial reusable multi-step procedure "
            "that future sessions would benefit from (e.g. debugging a "
            "specific class of error, running a multi-command sequence, "
            "a research synthesis flow). The procedure may be demonstrated by "
            "only PART of the excerpt below — you do NOT need to cover the whole "
            "session, just capture the one reusable procedure it contains. Shape: "
            '{"slug": "<kebab-case-4-to-60-chars>", '
            '"description": "<=150 chars, starts with verb>", '
            '"triggers": "<3-8 comma-separated keywords/phrases>", '
            '"procedure_md": "<concise markdown body with '
            "## When to use / ## Steps / ## Gotchas sections, "
            '<=8000 chars>"' + scripts_field + "}. "
            "Return null if the session was trivial, a single-shot answer, "
            "a one-off failure with no reusable takeaway, or involved "
            "sensitive paths. When a session plausibly contains a procedure "
            "a future session could reuse, lean toward returning it — every "
            "candidate is staged for human approval before it can activate, "
            "so a borderline proposal is cheap while a miss is lost for good. "
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
        # Record the (generation, count) marker regardless of outcome so an
        # unchanged session isn't re-evaluated on every subsequent
        # consolidation, but a rotation still forces a fresh pass.
        self._last_skillgen_marker[key] = marker
        if not result:
            return
        # _event_loop was captured by our caller (_consolidate) so the
        # thread-offloaded dedupe judge can marshal back onto the gateway loop.
        await asyncio.to_thread(self._process_auto_skills, result, key)

    def _save_lessons(self, raw: object) -> None:
        """Save extracted lessons from consolidation result."""
        if not isinstance(raw, list):
            return

        # Cap like semantic/episodic: each write_lesson can perform up to 6
        # blocking embeds, so an uncapped LLM lessons array would occupy a
        # worker thread for minutes.
        if len(raw) > _MAX_LESSONS_PER_CONSOLIDATION:
            logger.warning(
                "Consolidation returned %d lessons; capping to %d",
                len(raw),
                _MAX_LESSONS_PER_CONSOLIDATION,
            )
            raw = raw[:_MAX_LESSONS_PER_CONSOLIDATION]

        # Prefer vector store (dedup-aware) over JSONL
        if self._vector_store:
            count = 0
            for item in raw:
                if isinstance(item, dict) and item.get("rule"):
                    ok = self._vector_store.write_lesson(
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        source="consolidation",
                    )
                    if ok:
                        count += 1
            if count:
                logger.info("Extracted %d lesson(s) from chat (vector store)", count)
            return

        if not self._lesson_store:
            return
        from datetime import timezone as _tz

        from kiro_crew.learn import Lesson

        count = 0
        for item in raw:
            if isinstance(item, dict) and item.get("rule"):
                self._lesson_store.save(
                    Lesson(
                        ts=datetime.now(tz=_tz.utc).isoformat(),
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                    )
                )
                count += 1
        if count:
            logger.info("Extracted %d lesson(s) from chat", count)

    def _write_structured_memory(self, result: dict, key: str) -> None:
        """Write semantic + episodic entries from consolidation result."""
        if not self._vector_store:
            return
        source = f"consolidation:{key}"

        # Semantic entries
        semantic_items = result.get("semantic")
        if isinstance(semantic_items, list):
            written = 0
            deleted = 0
            for item in semantic_items[:_MAX_SEMANTIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or "key" not in item:
                    continue
                # Handle deletion of stale keys
                if item.get("delete"):
                    if self._vector_store.delete_semantic(item["key"], source):
                        deleted += 1
                    continue
                conf = float(item.get("confidence", 0.5))
                # Confidence 1.0 means user explicitly stated it — escalate source
                # so it can overwrite previous user_explicit entries
                item_source = "user_explicit" if conf >= 1.0 else source
                err = self._vector_store.set_semantic(
                    key=item["key"],
                    value=item.get("value"),
                    confidence=conf,
                    source=item_source,
                )
                if err is None:
                    written += 1
            if written or deleted:
                logger.info("Semantic consolidation: %d written, %d deleted", written, deleted)

        # Episodic entries
        episodic_items = result.get("episodic")
        if isinstance(episodic_items, list):
            written = 0
            for item in episodic_items[:_MAX_EPISODIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or "text" not in item:
                    continue
                ep_ok = self._vector_store.write_episodic(
                    text=item["text"],
                    conversation_id=key,
                    tags=item.get("tags", []),
                    importance=float(item.get("importance", 0.5)),
                    source=source,
                )
                if ep_ok:
                    written += 1
            if written:
                logger.info("Wrote %d episodic entries from consolidation", written)

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
            verdict, key = metadata_dedupe_verdict(candidate, existing, _judge_fn)
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
            client, _new, _resumed = await self._sessions.get_or_create(
                BACKGROUND_KEY, agent="kirocrew-lite"
            )
            text = await stream_and_collect(
                client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
            )
            return text or ""
        except Exception:
            logger.debug("Skill dedupe judge failed", exc_info=True)
            return ""
        finally:
            # get_or_create ACQUIRES the per-session semaphore — the caller MUST
            # release it (mirror _call_llm), else the shared _bg session is held
            # forever and the next consolidation turn deadlocks waiting for it.
            try:
                self._sessions.release(BACKGROUND_KEY)
                await self._sessions.recycle_background()
            except Exception:
                logger.debug("Skill dedupe judge session release failed", exc_info=True)

    async def _merge_skill_update(
        self, live_body: str, description: str, triggers: str, procedure_md: str
    ) -> "str | None":
        """Merge an existing live skill body with a new candidate into ONE
        updated markdown body — a single text turn on the shared background
        session. Mirrors ``_dedupe_judge`` exactly (get_or_create / REJECT_ALL /
        finally-release + recycle). Fail-open (returns ``None`` on any error) so
        the caller can fall back to a plain replacement proposal."""
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
            client, _new, _resumed = await self._sessions.get_or_create(
                BACKGROUND_KEY, agent="kirocrew-lite"
            )
            text = await stream_and_collect(
                client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
            )
            return text or None
        except Exception:
            logger.debug("Skill update merge failed", exc_info=True)
            return None
        finally:
            # get_or_create ACQUIRES the per-session semaphore — the caller MUST
            # release it (mirror _dedupe_judge), else the shared _bg session is
            # held forever and the next consolidation turn deadlocks.
            try:
                self._sessions.release(BACKGROUND_KEY)
                await self._sessions.recycle_background()
            except Exception:
                logger.debug("Skill update merge session release failed", exc_info=True)

    def _stage_skill_update(
        self,
        *,
        key: str,
        target_key: str,
        description: str,
        triggers: str,
        procedure_md: str,
        scripts: "list[dict] | None" = None,
    ) -> None:
        """Stage a pending UPDATE candidate for an existing auto-skill.

        (a) read the target's current live body; (b) LLM-merge it with the new
        requirement (bridged from this worker thread onto the captured loop,
        90s, fail-open); (c) use the redacted merge as the proposed body, else
        fall back to the candidate's own procedure (also on oversize); (d) stage
        under ``<target-slug>-update`` with ``kind='update'`` metadata; (e) SEL
        audit with outcome ``staged_update``."""
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
            logger.info("Skill update skipped: target '%s' is not a live auto skill", target_key)
            sel().log_tool_invocation(
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
        name = loader.stage_skill_candidate(
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
            logger.info("Staged skill update %s (target %s) from session %s", name, target_key, key)
            sel().log_tool_invocation(
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
            logger.info("Skill update staging rejected for target '%s'", target_key)
            sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="rejected",
                metadata={"slug": _update_slug, "reason": "creation_failed"},
            )

    def _process_auto_skills(self, result: dict, key: str) -> None:
        """Extract + write auto-generated skills from the consolidation result.

        Handles both ``new_skill`` and ``refined_skill`` result keys.  Each
        is validated, redacted via ``security.redact_*``, then deduped
        against existing skills (for new creation) before being written
        through ``SkillsLoader``.  Every successful write emits a SEL audit
        event via ``sel().log_tool_invocation``.
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
                            logger.info(
                                "Auto-skill script %r rejected by validator: %s",
                                fn,
                                "; ".join(_findings),
                            )
            if not (slug and description and procedure_md):
                # Required fields missing (or stripped empty by redaction).
                # Audit the rejection so operators can see that a create
                # attempt happened but lacked the minimum inputs.
                logger.info(
                    "Auto-skill create skipped: empty slug/description/procedure "
                    "after redaction (slug=%r)",
                    slug,
                )
                sel().log_tool_invocation(
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
                        logger.info(
                            "Auto-skill UPDATE target '%s' is not live (pending candidate); "
                            "staging '%s' as a new candidate instead of dropping it",
                            target,
                            slug,
                        )
                        verdict = VERDICT_NEW
                if verdict == VERDICT_DUP:
                    logger.info(
                        "Auto-skill synthesis skipped: '%s' overlaps existing skill '%s'",
                        slug,
                        target,
                    )
                    sel().log_tool_invocation(
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
                    )
                else:
                    provenance = AutoSkillProvenance(
                        session_key=key,
                        created_at=AutoSkillProvenance.now_iso(),
                    )
                    if self._approval_required or valid_scripts or scripts_supplied:
                        # Stage for human review — nothing goes live unattended,
                        # and any candidate that SUPPLIED scripts ALWAYS stages
                        # (even if every script was rejected by the validator, so
                        # a script-bearing candidate can never auto-publish as a
                        # prose-only skill).
                        name = self._skills_loader.stage_skill_candidate(
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                            scripts=valid_scripts or None,
                        )
                        if name:
                            logger.info("Staged skill candidate %s from session %s", name, key)
                            sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="staged",
                                metadata={"name": name, "scripts": len(valid_scripts)},
                            )
                        else:
                            logger.info("Skill staging rejected for slug '%s'", slug)
                            sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="rejected",
                                metadata={"slug": slug, "reason": "creation_failed"},
                            )
                    else:
                        name = self._skills_loader.create_auto_skill(
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                        )
                        if name:
                            logger.info("Auto-created skill %s from session %s", name, key)
                            sel().log_tool_invocation(
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
                                logger.debug("Skill lifecycle pass failed", exc_info=True)
                        else:
                            # create_auto_skill returned None: invalid slug,
                            # oversized procedure, or directory already exists.
                            # Audit the rejection so operators can see why.
                            logger.info(
                                "Auto-skill creation rejected for slug '%s' (creation_failed)",
                                slug,
                            )
                            sel().log_tool_invocation(
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
            # asked" — previously this branch left no SEL event or log line,
            # making it impossible to tell from the audit log whether skill
            # generation was ever attempted during a consolidation.
            logger.info(
                "Auto-skill: model proposed no skill candidate for session %s",
                key,
            )
            sel().log_tool_invocation(
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
                logger.info("Auto-skill refine rejected for %s: not in auto namespace", name)
                sel().log_tool_invocation(
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
                logger.info(
                    "Auto-skill refine skipped for %s: empty description/procedure "
                    "after redaction",
                    name,
                )
                sel().log_tool_invocation(
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
            ok = self._skills_loader.update_auto_skill(
                name,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )
            if ok:
                logger.info("Auto-refined skill %s from session %s", name, key)
                sel().log_tool_invocation(
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
                logger.info("Auto-skill refine rejected for %s (update_failed)", name)
                sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "update_failed"},
                )

    async def _call_llm(self, prompt: str) -> dict | None:
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
            logger.warning("LLM consolidation skipped — no session manager")
            raise _ConsolidationNotDispatched("no session manager")

        session_key = BACKGROUND_KEY
        # Timing instrumentation (_bg stall investigation): measure both the
        # wait to acquire the shared `_bg` session (queue contention behind
        # other `_bg` consumers like chat_nav link-preview) and the LLM turn
        # itself. No behavior change. Logged at DEBUG: silent in normal
        # operation, surfaced only when log_level is raised to investigate a
        # consolidation stall.
        t_start = _time.monotonic()
        try:
            try:
                client, _is_new, _resumed = await self._sessions.get_or_create(
                    session_key, agent="kirocrew-lite"
                )
            except Exception as exc:
                logger.warning(
                    "Consolidation could not acquire the background session "
                    "after %.1fs — nothing was sent",
                    _time.monotonic() - t_start,
                    exc_info=True,
                )
                raise _ConsolidationNotDispatched(
                    "background session unavailable"
                ) from exc
            t_acquired = _time.monotonic()
            wait_s = t_acquired - t_start
            # Reject all tools: this is a text/JSON-only generation turn. kiro
            # scopes the kirocrew-lite session to tools:[] via set_mode, but the
            # Claude Code backend skips set_mode and injects the full
            # kirocrew-core/cron toolset — without REJECT_ALL a background
            # consolidation turn could fire side-effecting tools (send_message,
            # learn_add, spawn_run). REJECT_ALL keeps both providers tool-free.
            try:
                result = await stream_and_collect_json(
                    client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
                )
            except Exception:
                logger.warning(
                    "LLM consolidation turn failed after %.1fs",
                    _time.monotonic() - t_start,
                    exc_info=True,
                )
                return None
            turn_s = _time.monotonic() - t_acquired
            logger.debug(
                "Consolidation LLM turn: wait=%.1fs turn=%.1fs total=%.1fs ok=%s",
                wait_s,
                turn_s,
                _time.monotonic() - t_start,
                result is not None,
            )
            return result
        finally:
            self._sessions.release(session_key)
            await self._sessions.recycle_background()
