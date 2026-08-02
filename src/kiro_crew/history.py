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
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

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
_CONSOLIDATION_THRESHOLD = 30  # preferences/projects update threshold (messages)

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

# Canonical set of memory_mode values that mark a session private — never
# searchable/listable/summarizable. Single source of truth shared by the MCP
# history tools (mcp_core) and the dashboard session handlers so the exclusion
# can't silently diverge between surfaces.
INCOGNITO_MEMORY_MODES = frozenset({"incognito", "temporary"})


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
    # Use '__' delimiter so keys containing dots (e.g. Slack thread_ts) don't confuse rfind('.') parsing.
    for n in itertools.count():
        if n > 1000:
            raise RuntimeError(f"Failed to create archive file after {n} attempts")
        candidate = adir / f"{safekey}__{stamp}{f'-{n}' if n else ''}.jsonl"
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
    # the expensive KiroCrewConfig.load() disk read + parse (Bug #6).
    now = _time.time()
    if now - _last_cleanup < 3600:
        return 0
    # Past the throttle window: stamp _last_cleanup NOW, before resolving
    # retention. Otherwise a config-resolved "disabled" (negative) would return
    # without updating the window, so every subsequent archive write would
    # re-run the expensive KiroCrewConfig.load() — reintroducing the Bug #6
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


def _safe_key(key: str) -> str:
    """Convert a session key (e.g. Slack thread_ts) to a safe filename."""
    return re.sub(r"[^\w\-.]", "_", key)


#: Default upper bound on the number of distinct session keys held in the
#: in-memory transcript / metadata caches.  The previous implementation used
#: plain ``dict`` caches that grew one entry per session key touched and never
#: evicted — on a gateway serving thousands of sessions the parsed message
#: lists (each up to ~200 messages / 2 MB of source JSONL) accumulated in RAM
#: for the lifetime of the process.  A bounded LRU keeps hot sessions resident
#: while giving the working set a deterministic ceiling.
_TRANSCRIPT_CACHE_MAX = 256


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
        #: whole thing (recommendation #10). Correctness-neutral — see
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
            data = json.loads(
                self._summary_cache_path(key).read_text(encoding="utf-8")
            )
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
            if not path.exists():
                self._dir.mkdir(parents=True, exist_ok=True)
                meta: dict = {
                    "_type": "metadata",
                    "created_at": datetime.now().isoformat(),
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
                "content": content,
                "ts": datetime.now().isoformat(),
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
                for m in self._read_messages(key):
                    if m.get("role") == role and m.get("content") == content:
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
        # Recommendation #10: recent-only access with no trailing exclusion can
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
        """Return the session's rotation generation counter.

        Incremented by :meth:`_maybe_rotate` on every rotation. A consolidator
        snapshots this alongside the message offset before its (slow) LLM call
        and passes it back to :meth:`mark_consolidated`, which resets the offset
        whenever the generation changed — closing the rotation-during-await race
        for ANY retained count, not just files that shrank below the offset.
        Absent field (legacy metadata / never rotated) reads as ``0``.
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
        moment. If a rotation fired while the consolidator awaited the LLM, the
        file was truncated to its newest messages, ``last_consolidated`` reset
        to 0, and the generation bumped — so the caller's *offset* is in the
        stale PRE-rotation numbering: every surviving index shifted by the
        number of dropped lines, so applying it would silently mark
        never-consolidated retained messages as already processed.

        Detection uses two independent signals:

        1. **Generation change** (primary, when *generation* is supplied):
           any rotation between snapshot and write bumps the counter, so a
           mismatch resets ``last_consolidated`` to 0 regardless of how many
           messages the rotation retained. This closes the case a pure
           offset-vs-count heuristic misses — a rotation that keeps >= *offset*
           messages leaves ``offset <= msg_count`` true yet has still shifted
           every index.
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
                # PRIMARY signal: a rotation fired between the caller's snapshot
                # and now (the generation counter advanced). The offset is in
                # the stale PRE-rotation numbering — every surviving index has
                # shifted by the number of dropped lines — so it cannot be
                # applied regardless of how many messages the rotation retained.
                # A rotation that kept >= *offset* messages leaves
                # ``offset <= msg_count`` true and would sail past the count
                # heuristic below, silently marking never-consolidated retained
                # messages as done. Reset to 0 and reconsolidate the retained
                # tail (harmless, idempotent) rather than risk that loss.
                logger.warning(
                    "mark_consolidated: rotation generation changed %s->%d for "
                    "%s (rotation during consolidation); resetting "
                    "last_consolidated to 0 to avoid skipping retained messages",
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
            meta["updated_at"] = datetime.now().isoformat()
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
        scored: list[tuple[float, int, dict]] = []  # (score, -rank, meta)
        for rank, meta in enumerate(self.list_sessions()[:_SEARCH_SCAN_WINDOW]):
            content_hits = 0
            doc_chars = 0
            texts: list[str] = []
            # Pull parsed messages from the mtime-keyed cache (_read_messages)
            # rather than re-opening + re-parsing the file here. The snippet
            # path (search_chat_history) already reads each matched key via the
            # same cache, so this collapses the prior two-parses-per-query into
            # one. Content semantics are unchanged: only string ``content``
            # fields are counted, in file order, so the \x00-join hit count and
            # the doc_chars length normalizer stay identical to the previous
            # inline scan. _read_messages is OSError-safe and returns [] for a
            # missing/unreadable file, so it also subsumes the old try/except.
            for obj in self._read_messages(meta["key"]):
                raw = obj.get("content") if isinstance(obj, dict) else None
                text = raw if isinstance(raw, str) else ""
                if text:
                    doc_chars += len(text)
                    texts.append(text)
            # Casefold + count once per file instead of per line: a 200-line
            # session produces one temporary casefolded string instead of 200,
            # bounding GC pressure under rapid-fire search keystrokes.  The
            # ``\x00`` separator can't appear in user queries, so cross-line
            # false matches are impossible.
            if texts:
                content_hits = "\x00".join(texts).casefold().count(needle)
            title_hits = (meta.get("title") or "").casefold().count(needle)
            if not title_hits and not content_hits:
                continue
            length_norm = math.sqrt(1 + doc_chars / 1024)
            score = title_hits * _TITLE_BOOST + content_hits / length_norm
            # Match-centered content snippet (why the session surfaced when the
            # title doesn't contain the query). Best-effort, display-only.
            snippet = ""
            if content_hits and texts:
                joined = " ".join(texts)
                # casefold() to match the hit detection above — .lower() misses
                # matches casefold finds (ß→ss, İ), yielding an empty snippet
                # despite content_hits > 0. casefold can shift offsets slightly
                # (length changes); acceptable for a display-only best-effort
                # window.
                pos = joined.casefold().find(query.casefold())
                if pos >= 0:
                    start = max(0, pos - 40)
                    end = min(len(joined), pos + len(query) + 100)
                    frag = " ".join(joined[start:end].split())
                    snippet = (
                        ("…" if start > 0 else "") + frag + ("…" if end < len(joined) else "")
                    )[:200]
            out_meta = {**meta, "snippet": snippet} if snippet else meta
            # Negate rank so a smaller (newer) rank wins ties after score desc sort.
            scored.append((score, -rank, out_meta))
        scored.sort(reverse=True)
        return [meta for _, _, meta in scored[:limit]]

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
                        if d.get("_type") == "metadata" and d.get("memory_mode") in (
                            "incognito",
                            "temporary",
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
                "created_at": datetime.now().isoformat(),
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
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        cached = self._msg_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        messages: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
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

    def _invalidate_cache(self, key: str) -> None:
        """Invalidate caches for a key after a write operation."""
        self._msg_cache.pop(key, None)
        self._meta_cache.pop(key, None)
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

    def _read_metadata(self, key: str) -> dict:
        """Read the metadata line (first line) from a session JSONL file.

        Uses mtime-based caching to avoid re-reading unchanged files.
        """
        path = self._path(key)
        if not path.exists():
            self._meta_cache.pop(key, None)
            return {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._meta_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        # Read ONLY the first line. The previous form slurped the entire file via
        # read_text() and then threw all but the first line away — on a 26 MB
        # transcript that is ~10ms and ~26 MB of transient allocation to obtain a
        # few hundred bytes (measured ~32x slower than readline()). Startup
        # restore calls this once per tab, so the waste scaled with both tab count
        # and transcript size, pushing the event loop toward the stall watchdog.
        try:
            with open(path, encoding="utf-8") as fh:
                first = fh.readline().strip()
        except OSError:
            return {}
        if not first:
            return {}
        try:
            data = json.loads(first)
            meta = data if data.get("_type") == "metadata" else {}
        except json.JSONDecodeError:
            meta = {}
        self._meta_cache[key] = (mtime, meta)
        return meta

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
        # Preserve select fields from original metadata
        orig_meta = self.get_metadata(key) or {}
        meta = {
            "_type": "metadata",
            "created_at": orig_meta.get("created_at", datetime.now().isoformat()),
            "last_consolidated": orig_meta.get("last_consolidated", 0),
            "compacted_at": datetime.now().isoformat(),
        }
        # Carry the rotation generation forward so a compaction (which is NOT a
        # rotation) doesn't reset it to 0 and spuriously trip the generation
        # mismatch in mark_consolidated for an in-flight consolidation.
        if orig_meta.get("rotation_generation"):
            meta["rotation_generation"] = orig_meta["rotation_generation"]
        if orig_meta.get("memory_mode"):
            meta["memory_mode"] = orig_meta["memory_mode"]
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
                meta["rotated_at"] = datetime.now().isoformat()
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
            if (
                now - last < self._history_idle_secs
                or self._log.unconsolidated_count(key) < 1
                or now - self._history_consolidated.get(key, 0) < self._history_idle_secs
                or key in self._running
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
        is already being consolidated or has no unconsolidated messages.

        Safety: _consolidate() internally enforces _session_touched_sensitive()
        as part of the auto_skills_eligible gate — sensitive sessions never
        produce skills regardless of entry point.
        """
        if key in self._running:
            return
        if self._log.unconsolidated_count(key) < 1:
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

            # Resolve workspace-scoped memory from session metadata
            meta = self._log.get_metadata(key)
            ws_name = meta.get("workspace")
            if ws_name:
                from kiro_crew.context import ContextBuilder

                memory = ContextBuilder.get_memory_for(ws_name)
            else:
                memory = self._memory

            def _fmt(m: dict) -> str:
                tools = f" [tools: {', '.join(m['tools'])}]" if m.get("tools") else ""
                return f"[{m.get('ts', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}"

            conversation = "\n".join(_fmt(m) for m in unconsolidated)

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
                current_semantic = self._vector_store.get_all_semantic()
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

            # ── Auto skill creation ──
            # Only eligible when the feature is enabled, we have a loader to
            # write to, we're on the history path (so prefs-only doesn't retrigger
            # extraction), and the session has enough tool calls to be non-trivial.
            auto_skills_eligible = (
                include_history
                and self._auto_skills_enabled
                and self._skills_loader is not None
                and _count_tool_call_messages(unconsolidated) >= self._auto_min_tool_calls
                and not _session_touched_sensitive(unconsolidated)
            )
            if auto_skills_eligible:
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
                keys.append(
                    '"new_skill": Object or null. Return an object ONLY if this '
                    "session contained a non-trivial reusable multi-step procedure "
                    "that future sessions would benefit from (e.g. debugging a "
                    "specific class of error, running a multi-command sequence, "
                    "a research synthesis flow). Shape: "
                    '{"slug": "<kebab-case-4-to-60-chars>", '
                    '"description": "<=150 chars, starts with verb>", '
                    '"triggers": "<3-8 comma-separated keywords/phrases>", '
                    '"procedure_md": "<concise markdown body with '
                    "## When to use / ## Steps / ## Gotchas sections, "
                    '<=8000 chars>"' + scripts_field + "}. "
                    'Return null if the session was trivial, a single-shot answer, '
                    "a one-off failure, or involved sensitive paths. Do NOT "
                    "include absolute paths, credentials, tokens, or user PII in "
                    "the procedure body."
                )
                if self._auto_refine_enabled:
                    keys.append(
                        '"refined_skill": Object or null. If an existing '
                        '"auto/..." skill was loaded during this session AND '
                        "the agent found a better procedure than the one "
                        "documented in that skill, return: "
                        '{"name": "auto/<existing-slug>", '
                        '"description": "<updated>", "triggers": "<updated>", '
                        '"procedure_md": "<refined markdown>"}. Return null '
                        "if nothing was refined. Do not fabricate refinements."
                    )

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

            result = await self._call_llm(prompt)
            if not result:
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

            # Auto skill creation / refinement.
            # Guarded by flag + eligibility — failures are logged, never fatal.
            if auto_skills_eligible:
                try:
                    await asyncio.to_thread(self._process_auto_skills, result, key)
                except Exception:
                    logger.warning("Auto-skill processing failed for %s", key, exc_info=True)

            # Autonomous lifecycle: age-based archival must run even when this
            # pass created/approved no skill, otherwise skills never age out on
            # their own (create/approve were the only triggers). Consolidation is
            # the existing idle/periodic path; throttle to at most once/hour
            # across all sessions so frequent consolidations don't rescan the set.
            if (
                self._skills_loader is not None
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
            raise
        finally:
            self._running.discard(key)

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
                existing.append({
                    "key": f"auto/{p.get('slug', '')}",
                    "description": p.get("description", ""),
                    "triggers": p.get("triggers", ""),
                })
        except Exception:
            pass
        loop = self._event_loop

        def _lexical() -> "tuple[str, str | None]":
            hit = loader.find_similar(
                description, threshold=self._auto_similarity_threshold
            )
            return (VERDICT_DUP, hit) if hit else (VERDICT_NEW, None)

        if self._judge_model and existing and loop is not None:
            def _judge_fn(prompt: str) -> str:
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        self._dedupe_judge(prompt), loop
                    )
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
            logger.info(
                "Skill update skipped: target '%s' is not a live auto skill", target_key
            )
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
                    self._merge_skill_update(
                        live_prose, description, triggers, procedure_md
                    ),
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

        provenance = AutoSkillProvenance(
            session_key=key, created_at=AutoSkillProvenance.now_iso()
        )
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
            logger.info("Staged skill update %s (target %s) from session %s",
                        name, target_key, key)
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
                        _target_is_live = self._skills_loader.read_auto_skill_body(target) is not None
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
        Returns parsed JSON dict or None on failure.
        """
        if not self._sessions:
            logger.warning("LLM consolidation skipped — no session manager")
            return None

        session_key = BACKGROUND_KEY
        # Timing instrumentation (_bg stall investigation): measure both the
        # wait to acquire the shared `_bg` session (queue contention behind
        # other `_bg` consumers like chat_nav link-preview) and the LLM turn
        # itself. No behavior change. Logged at DEBUG: silent in normal
        # operation, surfaced only when log_level is raised to investigate a
        # consolidation stall.
        t_start = _time.monotonic()
        try:
            client, _is_new, _resumed = await self._sessions.get_or_create(
                session_key, agent="kirocrew-lite"
            )
            t_acquired = _time.monotonic()
            wait_s = t_acquired - t_start
            # Reject all tools: this is a text/JSON-only generation turn. kiro
            # scopes the kirocrew-lite session to tools:[] via set_mode, but the
            # Claude Code backend skips set_mode and injects the full
            # kirocrew-core/cron toolset — without REJECT_ALL a background
            # consolidation turn could fire side-effecting tools (send_message,
            # learn_add, spawn_run). REJECT_ALL keeps both providers tool-free.
            result = await stream_and_collect_json(
                client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
            )
            turn_s = _time.monotonic() - t_acquired
            logger.debug(
                "Consolidation LLM turn: wait=%.1fs turn=%.1fs total=%.1fs ok=%s",
                wait_s,
                turn_s,
                _time.monotonic() - t_start,
                result is not None,
            )
            return result
        except Exception:
            logger.warning(
                "LLM consolidation call failed after %.1fs",
                _time.monotonic() - t_start,
                exc_info=True,
            )
            return None
        finally:
            self._sessions.release(session_key)
            await self._sessions.recycle_background()
