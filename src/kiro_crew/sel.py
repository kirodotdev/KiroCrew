"""Security Event Log — immutable, tamper-evident audit trail for tool invocations.

Records structured JSON events for every tool/MCP action with:
- Timestamp (ISO 8601 UTC)
- Caller identity (session key, agent, source interface)
- Operation type (tool_call, tool_approved, tool_rejected, tool_denied, mcp_call)
- Resources affected (tool name, tool kind, arguments summary)
- Outcome (approved, rejected, denied, completed, failed)
- Downstream service (MCP server name if applicable)
- HMAC-SHA256 integrity chain (each entry signs over previous hash)

Storage: ``<config_dir>/security_events.jsonl`` (append-only JSONL); the HMAC
signing key lives OUTSIDE the log directory in ``<config_dir>/trust/`` so an
actor who can rewrite the log dir cannot also read the key and re-sign a
clean-looking chain.
Rotation: the live log is closed at ``_SEGMENT_MAX_BYTES`` and renamed into
``<config_dir>/security_events.d/``, keeping ``_SEGMENT_KEEP`` closed segments.
Each segment is an INDEPENDENT HMAC chain (it starts from genesis), and the
first record of every new live log is a ``sel_rotation`` event naming the
segment just closed and its size — deliberately NOT that segment's final
``entry_hash``, which a sibling process still holding a writable fd to the
renamed inode could invalidate, making verification report an untampered log as
compromised. The segment NAME is what lets an investigator walk the sequence, so
the boundary is auditable evidence rather than a chain break, and retention
deleting an old segment leaves every surviving segment verifiable on its own.
Retention: configurable, default 365 days per Amazon Security Event Logging Standard.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib  # noqa: F401
import errno
import functools  # noqa: F401
import hashlib  # noqa: F401
import hmac
import importlib as _importlib
import json  # noqa: F401
import logging
import os  # noqa: F401
import queue
import re
import stat  # noqa: F401
import sys as _sys
import tempfile  # noqa: F401
import threading
import uuid
from collections.abc import Callable, Iterator  # noqa: F401
from contextlib import contextmanager  # noqa: F401
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone  # noqa: F401
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal, NamedTuple, overload  # noqa: F401

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import platform_compat  # noqa: F401
from kiro_crew import sel_append as _sel_append
from kiro_crew import sel_chain_lock as _sel_chain_lock
from kiro_crew import sel_reader as _sel_reader
from kiro_crew import sel_rotation as _sel_rotation
from kiro_crew import sel_trust_root as _sel_trust_root
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.config.paths import config_dir
from kiro_crew.credential_patterns import AWS_KEY_ID_PREFIXES
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel_chain_lock import _on_event_loop
from kiro_crew.sel_request_provenance import current_request_forwarded
from kiro_crew.sel_rotation import _SEGMENT_SUBDIR, _no_rotation
from kiro_crew.sel_trust_root import (
    _HMAC_KEY_FILE,
    _HMAC_KEY_MIN_BYTES,
    _TRUST_SUBDIR,
    _trust_root_key_loads,
)

logger = logging.getLogger(__name__)


def _default_dir() -> Path:
    """Resolve the SEL default dir lazily.

    Deferred (not a module-level ``config_dir()`` capture) so importing
    :mod:`kiro_crew.sel` never triggers the one-time data-home migration as an
    import side effect — the migration must fire only at the single chosen point
    (``ensure_data_home()`` in the CLI prologue), not whenever a transitive
    import first loads this module. Resolving on each call is cheap: the first
    ``config_dir()`` of the process caches the resolved home.
    """
    return config_dir()


_SEL_FILE = "security_events.jsonl"


# Best-effort writer retries, with capped backoff; these errnos never heal.
_WRITE_RETRIES = 3
_WRITE_BACKOFF_SECS = 0.05
_WRITE_BACKOFF_CAP_SECS = 1.0
_PERMANENT_WRITE_ERRNOS = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.EPERM, errno.EACCES, errno.EROFS}
)
# Writer thread only; an Event wait, as sel.py must not import time (on-loop guard).
_backoff_wait = threading.Event().wait


class _MaybeWritten(OSError):
    """A best-effort append that may have left bytes on disk (or cannot tell): never replayed."""


_MAX_ARG_LEN = 500
# Background-writer tuning. The queue is unbounded so callers never block; a
# crash/kill can lose at most the events still queued (audit log is
# eventually-durable, not synchronously-durable). flush() drains it before any
# read so read-after-write stays consistent.
_QUEUE_DRAIN_BATCH = 256  # max events appended per open() in the writer loop
_FLUSH_TIMEOUT_SECS = 5.0  # bound on flush() so a stuck writer can't hang reads
_HARD_EXIT_FLUSH_TIMEOUT_SECS = 3.0  # seconds allowed for the audit tail at hard exit


def _redact_deep(obj: object, redactor: Callable[[str], str]) -> object:
    """Apply *redactor* to every string reachable in *obj*, copying containers.

    Shared by the on-disk write path (:func:`_redacted_metadata_copy`) and the
    forward-callback path (``_forward_event``) so the two surfaces can never
    silently diverge in which shapes they cover. Sequences are rebuilt as plain
    ``list``/``tuple`` (mirroring json's own array degradation) rather than via
    ``type(obj)(...)``: a namedtuple or a subclass with a different constructor
    signature is legal metadata today (json serializes it fine), and a
    ``TypeError`` here would cost a whole writer batch. Keys and non-string
    leaves pass through unchanged.
    """
    if isinstance(obj, str):
        return redactor(obj)
    if isinstance(obj, dict):
        return {k: _redact_deep(v, redactor) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return tuple(_redact_deep(i, redactor) for i in obj)
    if isinstance(obj, list):
        return [_redact_deep(i, redactor) for i in obj]
    return obj


# AWS access-key ids case-insensitively. The shared redactor's pattern is
# case-SENSITIVE (a real key is upper-case, and loosening it there would
# false-positive on ordinary prose across every egress surface). SEL callers,
# however, log NORMALIZED text — e.g. the dashboard file-search handler
# lowercases the query before logging it in ``resources`` — and a lowercased
# key is trivially reversible, so the audit trail needs the case-insensitive
# net that ordinary egress does not. Bounded on both sides so a longer
# alphanumeric run (where extra chars change the case-restore candidates)
# stays out of scope.
_AWS_KEY_ANYCASE_RE = re.compile(
    # Prefixes come from the shared home; the BODY deliberately does not. This net
    # is mixed-case and boundary-bounded, so it is a different pattern from the
    # scrubber's uppercase-only one rather than another spelling of it.
    f"(?<![A-Za-z0-9])(?:{AWS_KEY_ID_PREFIXES})"
    r"[A-Za-z0-9]{16}(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _redact_text(text: str) -> str:
    """Apply the baseline credential/exfiltration passes to one string.

    Adds a case-insensitive AWS access-key pass on top of ``security.redact``:
    SEL callers may normalize case before logging (see ``_AWS_KEY_ANYCASE_RE``),
    which would slip a lowercased key past the shared case-sensitive pattern.
    """
    # circular import: kiro_crew.security imports SecurityEvent/SecurityEventLog
    # from this module at top level, so the redactor can only be imported
    # lazily here (same pattern as _forward_event / log_governance_decision).
    from kiro_crew.security import redact

    return _AWS_KEY_ANYCASE_RE.sub("[REDACTED: credential]", redact(text))


# Free-form string fields on SecurityEvent that carry caller-influenced text
# (a tool/operation name, a resources summary, an exception message) and are
# therefore redacted by the writer alongside metadata. The identity-shaped
# fields (caller_identity, agent, source, downstream_service, request_id) are
# constrained vocabularies, not caller free-text, and stay verbatim.
_REDACTED_TEXT_FIELDS = ("operation", "resources", "error")


def _redact_and_clip(text: str) -> str:
    """Redact *text*, THEN clip it to ``_MAX_ARG_LEN``.

    Order is the whole point. Clipping first can cut a credential in half, and
    the surviving prefix no longer matches any credential grammar (they are all
    anchored on a full-length token), so the writer's own pass cannot recover
    it: a long ``resources`` line with a key straddling byte 500 would persist
    most of that key. Redacting the FULL string first replaces the token
    outright, and only then is the (now secret-free) text clipped.

    Used by the ``log_*`` helpers, which are where the clip happens. The
    writer's pass in ``_flush_batch`` stays as the backstop for events built by
    constructing :class:`SecurityEvent` directly.
    """
    return _redact_text(text)[:_MAX_ARG_LEN]


def _redacted_metadata_copy(metadata: dict) -> dict:
    """Return a copy of *metadata* with credential/exfiltration text redacted.

    ``metadata`` is a free-form dict whose string values often carry
    caller-supplied text verbatim (search queries, document titles). The SEL
    file is persisted and dashboard-readable, so a secret pasted into such a
    field must never land on disk. String VALUES are redacted at any nesting
    depth (dicts, lists, tuples); keys and non-string values pass through
    unchanged.

    Always builds a NEW structure: callers may reuse their metadata dict after
    logging, so redaction must never be observable through the object they
    passed in. May raise on a pathological value; the writer contains that
    per event by persisting a placeholder — the raw text is never written.
    """
    return {k: _redact_deep(v, _redact_text) for k, v in metadata.items()}


@dataclass
class SecurityEvent:
    """A single auditable security event."""

    event_id: str
    timestamp: str  # ISO 8601 UTC
    event_type: str  # tool_invocation, tool_approval, tool_denial, mcp_call, api_access
    caller_identity: str  # session key or user identifier
    agent: str  # agent name (kirocrew, custom, etc.)
    source: str  # slack, dashboard, cli, cron, subagent, taskrunner, background
    operation: str  # tool name or API operation
    tool_kind: str = ""  # execute_bash, fs_write, mcp, etc.
    outcome: str = ""  # approved, rejected, denied, completed, failed
    resources: str = ""  # affected resources summary (truncated)
    downstream_service: str = ""  # MCP server name if applicable
    request_id: str = ""  # ACP permission request ID
    error: str = ""
    prev_hash: str = ""  # HMAC chain — hash of previous entry
    entry_hash: str = ""  # HMAC of this entry (computed on write)
    metadata: dict = field(default_factory=dict)


class SecurityEventLog:
    """Append-only, HMAC-chained security event log.

    Safe against concurrent writers both across threads (one singleton per
    interpreter) and across processes (an advisory lock on a sidecar file
    orders the chain writes of every process sharing the log).
    """

    _instance: SecurityEventLog | None = None
    _init_lock = threading.Lock()
    _initialized: bool = False
    # Fields first set from a member an owner module defines (``_init_locked`` stores
    # what ``_load_or_create_hmac_key`` and ``_read_last_hash`` return, and the key load
    # sets ``_hmac_key_file``), declared so type checkers know them. An annotation
    # creates no attribute.
    _hmac_key: bytes
    _hmac_key_file: Path
    _last_hash: str

    def __new__(cls, base_dir: Path | None = None, sync: bool = False) -> SecurityEventLog:
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialized = False
                    cls._instance = inst
        return cls._instance

    def __init__(self, base_dir: Path | None = None, sync: bool = False) -> None:
        # Double-checked locking, and the lock is NOT optional: ``__new__``
        # publishes the instance before ``__init__`` runs, so a second thread
        # that arrives in between gets the same object with ``_initialized``
        # still False and would run this body concurrently. Both would then call
        # ``_load_or_create_hmac_key`` and each could mint a fresh key — one
        # wins on disk while the other keeps different bytes in memory, which
        # silently splits the audit chain from the file that every other process
        # (and ``session_pid_sig``) resolves. Callers reaching this from worker
        # threads rather than the event loop make that interleaving real.
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._init_locked(base_dir, sync)

    def _init_locked(self, base_dir: Path | None, sync: bool) -> None:
        """One-time construction body; runs under ``_init_lock`` exactly once."""
        # sync=True writes each event inline (no background thread). Used by
        # tests that read the raw log file immediately after logging; production
        # uses the async writer for off-hot-path appends.
        self._sync = sync
        self._dir = base_dir or _default_dir()
        self._path = self._dir / _SEL_FILE
        self._segment_dir = self._dir / _SEGMENT_SUBDIR
        # _lock guards _last_hash + the file append (held only inside the writer
        # thread and by synchronous fallbacks / prune, never by enqueuing callers).
        # Ordering across processes is _chain_lock's job, not this lock's.
        # (The process's ONE cross-process chain-lock hold lives in the
        # MODULE-level _CHAIN_HOLDS registry, keyed by lock path, so sibling
        # instances in this process share it -- see _ChainHold.)
        self._lock = threading.Lock()
        self._hmac_key = self._load_or_create_hmac_key()
        self._last_hash = self._read_last_hash()
        # Identity of the live log as of our last observation, so a replacement
        # by another process (its rotation) is detected before we chain off a tip
        # that has moved into the closed segment. Seeded here because
        # _read_last_hash above just anchored us to THIS file.
        self._live_seen: tuple[int, int, int] | None = self._live_identity()
        self._forward_callback: Callable[[dict], None] | None = None
        # Background writer: callers enqueue (non-blocking) and one daemon thread
        # maintains the HMAC chain + batches appends off the hot path. Lazily
        # started on first log() so importing/constructing SEL stays side-effect
        # free (tests that never log don't spawn a thread).
        self._queue: queue.Queue[SecurityEvent | None] = queue.Queue()
        self._writer: threading.Thread | None = None
        self._writer_lock = threading.Lock()
        # Pending-event counter guarded by a Condition: log() increments BEFORE
        # enqueuing, the writer decrements AFTER each event is written, and
        # flush() waits for it to reach 0. This is race-free (unlike a bare
        # "queue empty" flag, which a writer could set between a logger's
        # clear and its put).
        self._pending = 0
        self._pending_cond = threading.Condition()
        self.dropped_events = 0
        self._drop_streak = False
        self._initialized = True

    def set_forward_callback(self, callback: Callable[[dict], None] | None) -> None:
        """Register an optional callback to forward events to a centralized log system."""
        with self._lock:
            self._forward_callback = callback

    def _ensure_writer(self) -> None:
        """Start the background writer thread once, on first use."""
        if self._writer is not None and self._writer.is_alive():
            return
        with self._writer_lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(
                target=self._writer_loop, name="sel-writer", daemon=True
            )
            self._writer.start()
            # Flush queued events on interpreter exit (best-effort; daemon thread
            # would otherwise be killed mid-queue).
            atexit.register(self.flush)

    def _writer_loop(self) -> None:
        """Drain the queue, maintaining the HMAC chain and batching appends.

        Blocks on the queue when idle (no busy-wait). Wakes per event, then
        opportunistically batches any already-queued events into a single
        open()+write so a per-message burst is one file operation, not N.
        """
        while True:
            event = self._queue.get()
            if event is None:  # shutdown sentinel — no _pending credit to drop
                return
            batch = [event]
            stop = False
            while len(batch) < _QUEUE_DRAIN_BATCH:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:  # sentinel mid-batch: write batch, then stop
                    stop = True
                    break
                batch.append(nxt)
            # Always decrement _pending, even if _flush_batch raises (e.g. mkdir
            # PermissionError outside its OSError guard, or a json.dumps failure):
            # otherwise the writer thread would die with _pending > 0 and every
            # later flush() would block until timeout. The except keeps the
            # thread alive so subsequent events still drain.
            try:
                self._flush_with_retry(batch)
            except Exception:
                logger.warning("SEL writer batch failed for %d events", len(batch), exc_info=True)
            finally:
                self._decr_pending(len(batch))
            if stop:
                return

    def _flush_with_retry(self, batch: list[SecurityEvent]) -> None:
        """Best-effort flush, retried with backoff; count a batch that is lost."""
        for attempt in range(_WRITE_RETRIES + 1):
            exc = self._flush_batch(batch)
            if exc is None:
                self._drop_streak = False
                return
            permanent = isinstance(exc, _MaybeWritten) or exc.errno in _PERMANENT_WRITE_ERRNOS
            if permanent or attempt == _WRITE_RETRIES:
                break
            _backoff_wait(min(_WRITE_BACKOFF_SECS * 2**attempt, _WRITE_BACKOFF_CAP_SECS))
        self.dropped_events += len(batch)
        if not self._drop_streak:
            self._drop_streak = True
            logger.error(
                "SEL dropped %d events after write failures (%s); total dropped %d",
                len(batch),
                exc,
                self.dropped_events,
            )

    def _decr_pending(self, n: int) -> None:
        """Drop *n* from the pending counter and wake any flush() waiters."""
        with self._pending_cond:
            self._pending = max(0, self._pending - n)
            if self._pending == 0:
                self._pending_cond.notify_all()

    def _flush_batch(
        self,
        events: list[SecurityEvent],
        *,
        raise_on_error: bool = False,
    ) -> OSError | None:
        """Append a batch of events under the chain lock, then forward them.

        When ``raise_on_error=True`` a filesystem failure (unwritable SEL file,
        full disk, un-creatable dir) is re-raised after rolling the chain tip
        back, so a fail-closed caller (critical audit) can refuse the action it
        was about to audit. The default (best-effort) logs the error and RETURNS
        it, unforwarded, so the writer can retry; ``None`` means the batch landed.

        Whether this batch may ROTATE is decided by :meth:`_may_rotate`, not by the
        call site. Rotation does filesystem work -- a directory scan, a rename, a
        retention sweep -- and several paths reach this method INLINE on their
        caller's thread, which for an async handler is the event loop
        (``no-blocking-call-on-event-loop``): a critical audit, and the fallback
        taken when the writer thread cannot be started. Deriving the permission
        from the running thread means a future inline caller cannot reintroduce
        that stall by forgetting a flag.
        """
        # Redact caller-supplied text before the chain hash is computed, so the
        # persisted bytes and the HMAC signature agree on the redacted form.
        # This is the single convergence point for every CALLER-originated event
        # (async writer, sync mode, and the critical fail-closed path all land
        # here), so a credential pasted into a free-form field — a metadata
        # value, an exception message in ``error``, a resources summary — never
        # reaches disk regardless of which log_* helper recorded it. The one
        # write that bypasses this method, the ``sel_rotation`` boundary record,
        # carries no caller text (a generated segment name and a byte count), so
        # it needs no pass. Covered surfaces: ``metadata`` values (any nesting
        # depth) and the free-form top-level strings in
        # ``_REDACTED_TEXT_FIELDS``; identity-shaped fields stay verbatim.
        # Caller-side passes (the governance helpers'
        # ``redact_via_context``) remain as a broader first layer. Runs outside
        # the chain lock: regex cost must not extend the critical section that
        # synchronous fallbacks and prune contend on. Fail-closed per event: a
        # redaction failure replaces the offending text with a placeholder —
        # the raw text is never persisted, and one bad value never costs the
        # rest of the batch.
        for event in events:
            for field_name in _REDACTED_TEXT_FIELDS:
                value = getattr(event, field_name)
                if not value:
                    continue
                try:
                    cleaned = _redact_text(value)
                except Exception as exc:
                    logger.warning(
                        "SEL: %s redaction failed for a %s event; persisting a "
                        "placeholder instead of the raw text",
                        field_name,
                        event.event_type,
                        exc_info=True,
                    )
                    cleaned = f"[redaction_error: {type(exc).__name__}]"
                if cleaned != value:
                    logger.info(
                        "SEL: redacted %s text in a %s event",
                        field_name,
                        event.event_type,
                    )
                    setattr(event, field_name, cleaned)
            if not event.metadata:
                continue
            try:
                redacted = _redacted_metadata_copy(event.metadata)
            except Exception as exc:
                logger.warning(
                    "SEL: metadata redaction failed for a %s event; persisting "
                    "a placeholder instead of the raw metadata",
                    event.event_type,
                    exc_info=True,
                )
                redacted = {"redaction_error": type(exc).__name__}
            if redacted != event.metadata:
                # Observability only — a secret reaching audit metadata signals
                # an upstream handling defect. Keys only, never values.
                logger.info(
                    "SEL: redacted metadata value(s) in a %s event (keys: %s)",
                    event.event_type,
                    list(event.metadata),
                )
            event.metadata = redacted
        callback: Callable[[dict], None] | None = None
        # Chain lock FIRST, thread lock second — and in that order everywhere.
        # The reverse order lets a cross-process wait stall the event loop
        # indirectly: the background writer takes _lock, then blocks waiting for
        # another process's chain lock, and a loop-side critical audit then
        # blocks on _lock itself, which is an ordinary blocking threading.Lock.
        # Taking the chain lock first means the loop-side path reaches its
        # single-shot fail-closed gate (see _chain_lock) BEFORE it can wait on
        # anything, so it raises instead of stalling. Serializing the whole
        # read-tip → chain → append sequence across processes is what keeps two
        # writers from chaining off the same prev_hash; the FD-identity guard in
        # _append_lines_locked stays as the second line of defense — a sibling's
        # ROTATION holds the rotation lock, not this one, and this instance's
        # cached tip can be stale when a sibling appended before we took the
        # lock, both of which the guard turns into a re-anchor + re-chain.
        try:
            with self._chain_lock():
                with self._lock:
                    try:
                        self._dir.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        if raise_on_error:
                            raise
                        logger.warning("SEL dir create failed for %d events", len(events), exc_info=True)
                        return exc
                    # Close the live log first when it is already at the size cap, so
                    # this batch lands in a fresh segment, and keep the cross-process
                    # rotation lock held across our own chain + append (see
                    # _rotation_window). Rotation is best-effort and never raises: a
                    # rotation that cannot happen must not stop the audit record it
                    # precedes from being written.
                    with self._rotation_window() if self._may_rotate() else _no_rotation():
                        # Remember the chain tip so we can roll back if the append
                        # fails: we advance _last_hash per event below, but nothing is
                        # persisted until the write() succeeds. Without the rollback, a
                        # failed write would leave _last_hash pointing at a phantom hash
                        # never on disk, and the next batch would chain off it —
                        # silently corrupting the HMAC chain (verify_integrity would
                        # then report a break). Read INSIDE the window: rotation resets
                        # the tip to genesis, and rolling back to a pre-rotation tip
                        # would chain this batch off a record in a different segment.
                        before = None if raise_on_error else self._probe_live()
                        try:
                            self._append_chained_locked(events)
                        except OSError as exc:
                            if raise_on_error:
                                raise
                            logger.warning(
                                "SEL append failed for %d events", len(events), exc_info=True
                            )
                            if before is None or self._probe_live() != before:
                                return _MaybeWritten(errno.EIO, str(exc))
                            return exc
                    callback = self._forward_callback
        except OSError as exc:
            # The chain lock itself was unavailable: held by another writer while
            # this thread is the event loop (single-shot fail-closed acquire), or
            # its sidecar was unusable. Audit-or-deny for a critical caller; a
            # best-effort caller drops the batch with a warning.
            if raise_on_error:
                raise
            logger.warning(
                "SEL chain lock unavailable for %d events", len(events), exc_info=True
            )
            return exc
        if callback:
            for event in events:
                self._forward_event(callback, event)
        return None

    def _forward_event(self, callback: Callable[[dict], None], event: SecurityEvent) -> None:
        """Redact and forward a single event to the centralized sink."""
        try:
            # circular import: kiro_crew.security imports SecurityEvent/
            # SecurityEventLog from this module at top level, so redact() can
            # only be imported lazily here.
            from kiro_crew.security import redact

            callback(_redact_deep(asdict(event), redact))  # type: ignore[arg-type]
        except Exception:
            logger.warning("forward_callback failed", exc_info=True)

    # The cross-process chain lock and its per-process hold registry: ``kiro_crew.sel_chain_lock``.
    _chain_lock = _sel_chain_lock._chain_lock
    _chain_lock_path = _sel_chain_lock._chain_lock_path
    _chain_lock_target = _sel_chain_lock._chain_lock_target
    _chain_hold_relabel = _sel_chain_lock._chain_hold_relabel
    # Chain a batch onto the live log and keep the tip anchored: ``kiro_crew.sel_append``.
    _append_chained_locked = _sel_append._append_chained_locked
    _chain_locked = _sel_append._chain_locked
    _append_lines_locked = _sel_append._append_lines_locked
    _live_size = _sel_append._live_size
    _live_identity = _sel_append._live_identity
    _probe_live = _sel_append._probe_live
    _reanchor_if_replaced = _sel_append._reanchor_if_replaced
    _reanchor_now = _sel_append._reanchor_now
    _ends_without_newline = _sel_append._ends_without_newline
    _read_last_hash = _sel_append._read_last_hash
    # The HMAC trust root and the per-record MAC: ``kiro_crew.sel_trust_root``.
    _load_or_create_hmac_key = _sel_trust_root._load_or_create_hmac_key
    _compute_hash = _sel_trust_root._compute_hash
    _record_signature_matches = _sel_trust_root._record_signature_matches
    # Size rotation, segment retention and the age prune: ``kiro_crew.sel_rotation``.
    _rotation_window = _sel_rotation._rotation_window
    _open_rotation_lock = _sel_rotation._open_rotation_lock
    _ensure_segment_dir = _sel_rotation._ensure_segment_dir
    _rotate_under_lock = _sel_rotation._rotate_under_lock
    _next_segment_path = _sel_rotation._next_segment_path
    _segments_oldest_first = _sel_rotation._segments_oldest_first
    _enforce_segment_retention_locked = _sel_rotation._enforce_segment_retention_locked
    prune = _sel_rotation.prune
    _prune_live_locked = _sel_rotation._prune_live_locked
    _prune_segments_locked = _sel_rotation._prune_segments_locked
    _newest_timestamp = _sel_rotation._newest_timestamp
    # Verify the chains and read events back: ``kiro_crew.sel_reader``.
    verify_integrity = _sel_reader.verify_integrity
    _reader_handle = _sel_reader._reader_handle
    _verify_file = _sel_reader._verify_file
    recent = _sel_reader.recent
    _read_sources_newest_first = _sel_reader._read_sources_newest_first
    _segment_is_signed_by_us = _sel_reader._segment_is_signed_by_us
    _iter_lines_backward = _sel_reader._iter_lines_backward

    def flush(self, timeout: float = _FLUSH_TIMEOUT_SECS) -> None:
        """Block until all enqueued events are written. Bounded by *timeout*.

        Called before every read path (recent/verify_integrity/prune) and on
        shutdown so the on-disk log reflects all enqueued events. Waits on the
        pending-event counter (race-free vs a bare queue-empty check) with a
        timeout so a wedged writer can't hang a read forever.
        """
        with self._pending_cond:
            if self._pending == 0:
                return
            self._pending_cond.wait_for(lambda: self._pending == 0, timeout=timeout)

    def _may_rotate(self) -> bool:
        """Whether the CURRENT thread is allowed to do rotation's filesystem work.

        Only the dedicated writer thread, or ``sync=True`` (a test mode that has no
        writer thread and whose caller is the test itself).

        Every other route into ``_flush_batch`` runs inline on a caller's thread
        that may be the asyncio event loop -- the ``critical=True`` audit-or-deny
        write, and the fallback taken when ``_ensure_writer`` cannot start the
        thread. A directory scan plus a rename plus a retention sweep there stalls
        every gateway task. Rotation is deferrable and an audit write is not, so
        those paths write the record and leave rotation to the writer's next batch.

        Checked here rather than at each call site on purpose: the previous
        revision passed a flag from the critical path only, and the writer-start
        fallback -- added for a different reason and easy to overlook -- kept
        rotating on whatever thread it landed on.
        """
        if self._sync:
            return True
        writer = self._writer
        return writer is not None and threading.current_thread() is writer

    def _rotation_event(self, segment: Path, closed_bytes: int) -> SecurityEvent:
        """Build the ``sel_rotation`` record that opens a freshly rotated log.

        It names the segment just closed and its size, and DELIBERATELY does not
        claim that segment's final ``entry_hash``.

        An earlier revision did. The claim looked valuable -- it would let
        verification notice a predecessor that had been swapped -- but it cannot be
        made true without serializing every append: a segment is not immutable the
        instant it is renamed, because another process may still hold a writable fd
        to that inode and land a record after the tip is read. Any hash captured
        here can therefore be stale by the time the record is written, and the
        result is the WORST failure mode available: verification reporting an
        untampered log as compromised, from an entirely benign cause.

        Nothing is lost by dropping it, because forgery detection never rested on
        it. A planted or rewritten segment cannot produce valid per-record HMACs at
        all (the key lives outside the log directory), so ``verify_integrity``
        already reports its entries as invalid and the read path already refuses to
        present them. The segment NAME is what an investigator actually needs to
        walk the sequence, and a name is not a claim that can go stale.
        """
        return SecurityEvent(
            event_id=uuid.uuid4().hex[:16],
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            event_type="sel_rotation",
            caller_identity="_host",
            agent="kirocrew",
            source="host",
            operation="sel.rotate",
            outcome="completed",
            # Only the file NAME, never the absolute path: the record is
            # forwarded to the centralized sink and read back by operators, and
            # the segment always lives in this log's own segment dir.
            resources=segment.name,
            metadata={
                "previous_segment": segment.name,
                "previous_bytes": closed_bytes,
            },
        )

    def log(self, event: SecurityEvent, *, critical: bool = False) -> None:
        """Enqueue an event for the background writer (non-blocking).

        The HMAC chain (prev_hash/entry_hash) is computed in the writer thread
        in enqueue order, so callers never pay the hash + file-append cost on
        the hot path. If the writer can't be started (unexpected), fall back to
        a synchronous write off the event loop; ON the loop the non-critical
        event is dropped with a warning instead, because the inline write's
        filesystem work would freeze every task the loop serves
        (no-blocking-call-on-event-loop) — best-effort audit loss is the
        survivable direction.

        When ``critical=True`` the event is written SYNCHRONOUSLY and a
        filesystem failure is re-raised, so a fail-closed caller (e.g. safety
        override activation, unattended tool auto-approval) can refuse the
        action it was about to audit rather than proceed unaudited. Any events
        already queued are drained first so the on-disk HMAC chain keeps
        enqueue order. This is the crux of the "audit-or-deny" invariant: the
        async queue's swallow-and-warn behaviour must NOT apply to a critical
        audit, or the caller's fail-closed branch becomes unreachable.

        A critical write also does not rotate, and neither does the
        writer-unavailable fallback below: both run inline on their caller's
        thread, which may be the asyncio event loop. ``_may_rotate`` enforces that
        from the running thread rather than from a flag here, so the rule cannot be
        lost at a call site. The background writer rotates on its next batch.
        """
        if self._sync:
            self._flush_batch([event], raise_on_error=critical)
            return
        if critical:
            # Preserve chain order: drain the async backlog, then write this
            # event inline so PermissionError/OSError propagates to the caller.
            # NEVER wait for the drain on the event-loop thread: the background
            # writer may be parked on another process's chain lock (prune holds
            # it across a whole streaming rewrite), so waiting on ``_pending``
            # here re-imports the cross-process wait the single-shot acquire in
            # ``_chain_lock`` exists to keep off the loop — chat and the
            # heartbeat stall for every session it serves. The wait also cannot
            # help in exactly that case: the inline append below takes the same
            # chain lock with a single nonblocking attempt and fails closed
            # while the sibling holds it. Enqueue order on disk is already
            # best-effort — this same ``flush()`` gives up at its timeout and
            # the inline write proceeds regardless — so skipping the wait
            # changes no guarantee, it only removes the loop stall.
            if not _on_event_loop():
                self.flush()
            self._flush_batch([event], raise_on_error=True)
            return
        incremented = False
        try:
            self._ensure_writer()
            with self._pending_cond:
                self._pending += 1
            incremented = True
            self._queue.put(event)
        except Exception:
            # The event never reached the queue, so return its pending credit —
            # otherwise flush() waits its full timeout on a count nothing will
            # ever decrement (the writer only credits batches it dequeues).
            if incremented:
                self._decr_pending(1)
            # Writer unavailable. Off the loop, write synchronously so the
            # audit entry lands. ON the loop, drop it instead: `_flush_batch`
            # is redaction + chain lock + open/write/flush on the caller's
            # thread, and a non-critical audit is best-effort by contract —
            # losing one event is survivable, freezing every session's turn
            # and the liveness heartbeat is not (no-blocking-call-on-event-loop).
            # Critical writes never take this branch; they fail closed above.
            if _on_event_loop():
                logger.warning(
                    "SEL writer enqueue failed on the event loop; "
                    "dropping non-critical event",
                    exc_info=True,
                )
                return
            logger.warning("SEL writer enqueue failed; writing synchronously", exc_info=True)
            self._flush_batch([event])

    def log_tool_invocation(
        self,
        *,
        session_key: str,
        agent: str = "kirocrew",
        source: str = "",
        tool_name: str,
        tool_kind: str = "",
        outcome: str,
        request_id: str | int = "",
        downstream_service: str = "",
        resources: str = "",
        error: str = "",
        metadata: dict | None = None,
        critical: bool = False,
    ) -> None:
        """Convenience: log a tool invocation event.

        Pass ``critical=True`` when the caller enforces "audit-or-deny" (e.g.
        an unattended heartbeat auto-approve): the event is written
        synchronously and a filesystem failure is re-raised so the caller can
        deny the tool rather than run it unaudited.
        """
        self.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="tool_invocation",
                caller_identity=session_key,
                agent=agent,
                source=source or _infer_source(session_key),
                operation=tool_name,
                tool_kind=tool_kind,
                outcome=outcome,
                request_id=str(request_id),
                downstream_service=downstream_service,
                resources=_redact_and_clip(resources) if resources else "",
                error=_redact_and_clip(error) if error else "",
                metadata=metadata or {},
            ),
            critical=critical,
        )

    def log_governance_decision(
        self,
        *,
        session_key: str,
        agent: str = "kirocrew",
        tool_name: str,
        scope: str = "",
        item: str = "",
        outcome: str,
        rule: str = "",
        layer: str = "",
        reason: str = "",
        critical: bool = False,
    ) -> None:
        """Convenience: log a governance (Level 1 ∩ Level 2) decision.

        ``outcome`` is the existing permit/deny vocabulary — ``"allowed"`` /
        ``"denied"`` (NOT "approved"; matches the dominant token used across the
        codebase).  ``scope``/``item``/``rule``/``layer`` go in ``metadata`` for
        ``policy explain`` and forensic queries.

        The writer applies the baseline credential/exfiltration passes to
        ``operation``/``resources``/``error`` and metadata values before
        persisting, but ``redact_via_context`` is broader (a loaded
        companion's extra regexes apply) — so ``operation``/``item``/``reason``
        are ALSO redacted HERE (before ``log``): a command body or path that
        tripped governance must not leak anything the broader pass would have
        caught. The writer's narrower pass is a second layer, not a
        replacement.

        Pass ``critical=True`` when the caller enforces "audit-or-deny" for a
        GOVERNED decision (e.g. a governed transport-start allow): the event is
        written SYNCHRONOUSLY and a persistence failure (unwritable SEL file,
        full disk) is re-raised, so the caller can refuse the action rather than
        proceed unaudited. Without it the write is enqueued to the background
        writer, which swallows persistence failures — fine for best-effort audits
        (e.g. an ungoverned allow) but NOT for audit-or-deny.
        """
        # Lazy import: sel.py is imported very early (security.py imports it), and
        # platform.context imports security indirectly — keep this off the
        # module-load path to avoid a cycle (documented pattern).
        from kiro_crew.platform.context import redact_via_context

        safe_operation = redact_via_context(tool_name)
        safe_item = redact_via_context(item) if item else ""
        safe_reason = redact_via_context(reason) if reason else ""
        self.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="governance_decision",
                caller_identity=session_key,
                agent=agent,
                source=_infer_source(session_key),
                operation=_redact_and_clip(safe_operation),
                outcome=outcome,
                resources=_redact_and_clip(safe_item),
                metadata={
                    "scope": scope,
                    "rule": rule,
                    "layer": layer,
                    "reason": _redact_and_clip(safe_reason),
                },
            ),
            critical=critical,
        )

    def log_governance_degraded(
        self,
        *,
        session_key: str,
        chokepoint: str,
        scope: str = "",
        app: str = "",
        reason: str = "",
        failed_closed: bool = False,
    ) -> None:
        """Record that a governance chokepoint FAILED OPEN (degraded to permit).

        A governance evaluation raised an unexpected (non-PlatformCompositionError)
        error, so the chokepoint degraded to "no opinion" / permit and the
        operator's narrowing for that surface is silently NOT applied. This is a
        security-relevant event — without it a fail-open is invisible until an
        incident reconstructs it — so it is logged at WARNING by the caller AND
        persisted to the file-backed SEL here (safe even from a stdio MCP server,
        which must not write to stdout). ``app`` (when the degraded chokepoint
        resolved a per-app profile) is recorded so an investigator can tell WHICH
        app's narrowing was bypassed. ``reason`` is redacted before persistence.

        ``failed_closed=True`` inverts the disposition: the chokepoint
        DENIED the action rather than degrading to permit.  The event is written
        with ``critical=True`` (synchronously, raising on a filesystem failure)
        and its ``outcome`` is ``"blocked"``, matching the severity of other
        security-critical SEL audits so the fail-closed trip is durably recorded.
        """
        from kiro_crew.platform.context import redact_via_context

        safe_reason = redact_via_context(reason) if reason else ""
        self.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="governance_degraded",
                caller_identity=session_key,
                agent="kirocrew",
                source=_infer_source(session_key),
                operation=_redact_and_clip(chokepoint),
                outcome="blocked" if failed_closed else "degraded",
                resources="",
                metadata={
                    "scope": scope,
                    "app": app,
                    "reason": _redact_and_clip(safe_reason),
                    "disposition": "failed_closed" if failed_closed else "failed_open",
                },
            ),
            critical=failed_closed,
        )

    def log_api_access(
        self,
        *,
        caller: str,
        operation: str,
        outcome: str,
        source: str = "dashboard",
        resources: str = "",
        error: str = "",
        critical: bool = False,
        forwarded: bool | None = None,
    ) -> None:
        """Convenience: log a dashboard/API access event.

        Pass ``critical=True`` for fail-closed audits (e.g. safety-override
        activation): the event is written synchronously and a filesystem
        failure is re-raised so the caller can refuse the audited action.

        ``forwarded`` (``origin.is_proxied_request``) lands as ``metadata["forwarded"]``.
        ``None`` takes the verdict the deny-audit boundary bound for the live request
        (``sel_request_provenance``); outside one no key is written, which means
        "not recorded", never "made directly". ``caller_identity`` stays as passed.

        ``outcome`` is redacted and clipped like ``resources`` and ``error``,
        even though it reads as a constrained vocabulary. It is not one at this
        boundary: an installed app reaches this helper through ``ctx.audit``, so
        the value can be caller text rather than an in-tree constant, and this
        log is append-only and served over ``/api/sel/events`` -- a secret that
        lands here has no recovery path. The pass is the identity function on
        every spelling in-tree code writes (``ok``, ``denied``, ``completed``,
        ``rejected``, ``allowed``), so no existing row changes; it is applied
        here rather than in each caller so a new filler cannot miss it. The
        writer's own pass is not the backstop: ``_REDACTED_TEXT_FIELDS`` omits
        ``outcome`` because identity-shaped fields stay verbatim there.
        """
        if forwarded is None:
            forwarded = current_request_forwarded()
        self.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="api_access",
                caller_identity=caller,
                agent="",
                source=source,
                operation=operation,
                outcome=_redact_and_clip(outcome) if outcome else "",
                resources=_redact_and_clip(resources) if resources else "",
                error=_redact_and_clip(error) if error else "",
                metadata={} if forwarded is None else {"forwarded": bool(forwarded)},
            ),
            critical=critical,
        )


def _infer_source(session_key: str) -> str:
    """Infer the source interface from a session key.

    An EMPTY key carries no surface signal — a real Slack key is always a
    non-empty channel/thread timestamp — so it maps to ``"unknown"`` rather than
    being misattributed to ``"slack"`` (e.g. an app-activation governance degrade
    that passes no session_key; see governance ``audit_governance_degraded``).

    The ``_host`` sentinel is the explicit HOST-process surface: an in-process
    governance check that is not driven by any user-facing surface (app
    activation, Slack workspace admission).  It gives operators a stable,
    honest bind target (``bind: {type: surface, id: host}``) instead of the
    accidental ``slack`` an empty key would otherwise classify to.
    """
    if not session_key:
        return "unknown"
    if session_key == "_host":
        return "host"
    if session_key.startswith("dashboard:"):
        return "dashboard"
    # The side chat (``dashboard/handlers/side.py``) runs its isolated LLM
    # session under ``side:<slot>``. That IS a dashboard surface — the slot's
    # own side panel — keyed apart from ``dashboard:<slot>`` only so the ACP
    # session and its SEL rows stay separate from the parent slot's. Classifying
    # it here keeps every consumer in step: a dashboard-bound governance profile
    # (``governance_profiles.resolve_active_scope``) binds a side turn exactly as
    # it binds the parent slot, and the ``slack`` fallback below never claims it.
    if session_key.startswith("side:"):
        return "dashboard"
    # A reply thread on a crewmate chat message (``dashboard/chat_threads.py``)
    # runs its isolated turn under ``thread:<slot>:<mid>`` -- the same dashboard
    # surface as the side chat, keyed apart from the parent slot for the same
    # reason, and classified here for the same reason.
    if session_key.startswith("thread:"):
        return "dashboard"
    if session_key.startswith("cron:"):
        return "cron"
    if session_key.startswith("subagent:"):
        return "subagent"
    if session_key.startswith("taskrunner"):
        return "taskrunner"
    if session_key == "_bg":
        return "background"
    if session_key == "_consolidate":
        return "background"
    if session_key == "_hb":
        return "heartbeat"
    if session_key == "cli_chat":
        return "cli"
    # Namespaced messaging channels carry their transport as the first key
    # segment (``{channel}:{agent}:...`` per messaging/link.build_dm_session_key,
    # or a ``{channel}_`` prefix). Match the SAME set context._runtime_display_name
    # uses so SEL attribution and the display name stay in lockstep.
    # Bare/legacy Slack keys (thread timestamps like ``C08...:thread``) have no
    # namespace prefix and correctly retain the legacy ``slack`` fallback.
    lowered_key = session_key.lower()
    for namespace in (
        "discord",
        "telegram",
        "wecom",
        "weixin",
        "whatsapp",
        "feishu",
        "webex",
        "teams",
        "imessage",
        "slack",
    ):
        if lowered_key.startswith((f"{namespace}:", f"{namespace}_")):
            return namespace
    return "slack"


_AUDIT_SOURCES: tuple[str, ...] = (
    "unknown",
    "host",
    "dashboard",
    "cron",
    "subagent",
    "taskrunner",
    "background",
    "heartbeat",
    "cli",
    "discord",
    "telegram",
    "wecom",
    "weixin",
    "whatsapp",
    "feishu",
    "webex",
    "teams",
    "imessage",
    "slack",
)


def audit_sources() -> tuple[str, ...]:
    """Every ``source`` value :func:`_infer_source` can stamp on an event.

    This is the authoritative set of audited surfaces, consumed by the
    security-posture view (``security_posture._audit_surface_items``) so that
    surface count is derived rather than a hand-copied number that goes stale.
    A drift guard in ``test_security_posture`` pins this tuple against
    ``_infer_source``'s actual branches, so adding a surface there without adding
    it here fails CI.
    """
    return _AUDIT_SOURCES


def sel() -> SecurityEventLog:
    """Module-level accessor for the singleton SEL instance."""
    return SecurityEventLog()


async def warm_sel_singleton() -> None:
    """Prime the SecurityEventLog singleton OFF the event loop at startup.

    The first ``sel()`` of a process runs ``_init_locked`` — blocking file I/O
    (trust-dir creation, HMAC key load/create, a tail read of the live log) —
    on whatever thread touches it first. Without this warm, every
    handler that could plausibly be a fresh gateway's first SEL touch needs
    its own ``asyncio.to_thread`` wrapper, and the 250+ other
    ``log_api_access`` call sites stay candidate first-touch stalls.
    Warming once here, before the server accepts traffic, closes the
    class: a post-init ``log_api_access`` only enqueues to the writer thread
    (after the writer's one-time daemon-thread start on first ``log()``), so
    call sites need no thread hop.

    Both async startup paths (``start_dashboard`` / ``start_api_server``)
    ``await`` this before building the middleware chain — the same pattern as
    ``token_auth.warm_auth_singletons``. The cost does not scale with user
    data: one key-file read (or create), one backward tail read of the live
    log (a single 4 KiB chunk on a healthy log; worst case a full backward
    scan of the live log, bounded by rotation's ``_SEGMENT_MAX_BYTES``, when
    its tail holds no parseable record), and a ``stat``.

    Best-effort by design: construction can raise (e.g. a trust root too
    short to sign the chain), and an SEL init failure must not keep the
    gateway from becoming ready — audit degradation is survivable, a boot
    loop is not. On a failed warm the first later touch retries init on its
    caller's thread, as every call site did before the per-site hops existed;
    every ``critical=True`` audit still fails closed at its own site.
    """
    try:
        await asyncio.to_thread(sel)
    except Exception:
        logger.warning(
            "SEL startup warm failed; the first audit write will retry init",
            exc_info=True,
        )


def sel_is_warm() -> bool:
    """Is the singleton constructed, so that ``sel()`` is a plain attribute read?

    The complement to :func:`warm_sel_singleton`'s best-effort contract. A
    failed warm leaves ``_instance`` allocated but ``_initialized`` False, and
    the next ``sel()`` retries ``_init_locked`` -- blocking file I/O -- on the
    caller's thread. A call site that must never block the event loop (a
    middleware deny path is the one every request can hit) asks this first and
    takes a thread hop ONLY when the answer is no; on the healthy path (the
    warm succeeded, which is every normal start) it keeps the direct enqueue.
    Cheap and lock-free: two attribute reads.
    """
    inst = SecurityEventLog._instance
    return inst is not None and bool(getattr(inst, "_initialized", False))


def flush_audit_queue(timeout: float = _HARD_EXIT_FLUSH_TIMEOUT_SECS) -> None:
    """Drain an existing singleton without creating a trust root during exit."""
    inst = SecurityEventLog._instance
    if inst is None:
        return
    try:
        inst.flush(timeout=timeout)
    except Exception:
        logger.debug("SEL flush before hard exit failed", exc_info=True)


async def flush_audit_queue_before_hard_exit(
    timeout: float = _HARD_EXIT_FLUSH_TIMEOUT_SECS,
) -> None:
    """Offload the bounded drain to the teardown pool with an outer deadline."""
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), flush_audit_queue, timeout
            ),
            timeout=timeout + 1.0,
        )
    except Exception:
        logger.debug("SEL flush before hard exit failed", exc_info=True)


def sel_hmac_key_path() -> Path:
    """Canonical on-disk location of the SEL trust-root key (``sel_hmac.key``).

    Single source of truth shared by :class:`SecurityEventLog` (the key's
    creator/owner) and dependent protocols (``session_pid_sig``) so they can
    never diverge on which file anchors trust. Tracks the LIVE singleton's
    RESOLVED key path when one is initialized (tests and embedded deployments
    pass a ``base_dir``; a failed legacy migration keeps the legacy location —
    see ``_load_or_create_hmac_key``); otherwise falls back to the same
    ``trust/`` default the singleton would use. Dependent protocols must
    resolve the key through this accessor rather than re-deriving the path
    (e.g. via ``config_dir()``; ``_default_dir()`` honors ``KIROCREW_HOME`` the
    same way, so resolving through the shared accessor keeps the trust root
    single under isolated-home deployments).

    RE-RESOLVES per call. ``_hmac_key_file`` is decided once inside
    ``_load_or_create_hmac_key`` and a legacy install can leave it on the legacy
    location after a failed migration; a sibling process that later completes
    that migration deletes the file this process is still naming. Without
    re-resolution every dependent protocol inherits that dead path and has to
    grow its own recovery, which is one fallback per caller instead of the class
    being closed (the shape ``session_pid_sig`` would be left in).

    What re-resolution does NOT touch is the audit chain. The chain is signed
    and verified with ``self._hmac_key``, the BYTES read once at init, and no
    record carries a key id or generation marker — so re-reading the key file
    into the signing key mid-process would orphan every record already chained
    (which is why ``_load_or_create_hmac_key`` raises rather than regenerating,
    and why migration moves bytes with ``os.replace``). This accessor returns a
    PATH that the signing and verification code never reads. The anchor stays
    pinned to the bytes cached at init; only the path handed to dependent
    protocols follows the file.

    A relocated candidate is adopted ONLY when its bytes equal the key this
    process already validated at init. Adopting an unverified file would be a
    downgrade rather than a fix: the resolved path vanishing is exactly the
    moment an actor who can write the trust directory would plant a key of their
    own, and handing dependents a path is handing them signing material. When
    nothing verifies, the resolved path is returned unchanged so the operator
    report keeps naming the file that actually broke, and
    ``session_pid_sig._load_hmac_key`` still recovers from the in-memory copy.
    """
    inst = SecurityEventLog._instance
    if inst is None or not getattr(inst, "_initialized", False):
        # No singleton in this process (the verifying MCP process, typically):
        # this branch already recomputes on every call, so it was never frozen.
        return _default_dir() / _TRUST_SUBDIR / _HMAC_KEY_FILE
    resolved: Path = inst._hmac_key_file
    if _trust_root_key_loads(resolved):
        return resolved
    anchor: bytes | None = getattr(inst, "_hmac_key", None)
    if not anchor:
        return resolved
    base: Path = inst._dir
    # Same precedence as _load_or_create_hmac_key: the trust/ location first,
    # the legacy sibling-of-the-log location second.
    for cand in (base / _TRUST_SUBDIR / _HMAC_KEY_FILE, base / _HMAC_KEY_FILE):
        if cand == resolved or not _trust_root_key_loads(cand):
            continue
        try:
            found = cand.read_bytes()
        except OSError:
            continue
        if hmac.compare_digest(found, anchor):
            logger.debug(
                "SEL trust root re-resolved %s -> %s (same key bytes)",
                resolved,
                cand,
            )
            return cand
    return resolved


def _sel_hmac_key_bytes() -> bytes | None:
    """Return the live singleton's trust-root key BYTES, or ``None``.

    Module-private with exactly ONE intended caller
    (``session_pid_sig._load_hmac_key``), because the safety of handing out raw
    trust-root material rests on an ordering rule the CALLER enforces, not the
    accessor: the FILE must be preferred and this used only as a fallback. A
    readable file is the anchor every OTHER process resolves independently, so a
    second caller that reached for memory first would sign MACs a separate
    verifier rejects. Keeping one caller keeps that rule enforceable.

    ``SecurityEventLog`` reads the key once at init and signs every subsequent
    record from that in-memory copy, so the audit chain is immune to the key
    file moving, being deleted, losing read permission, or being truncated
    afterwards. The dependent protocol that re-reads the file on every use is
    not. ``sel_hmac_key_path`` re-resolves a relocation whose bytes match this
    anchor, so what reaches here is the residue it cannot resolve — a
    key deleted, unreadable, truncated, or replaced by bytes that are not the
    anchor — which is how a gateway would otherwise end up publishing unsigned
    identities forever while its audit chain still looks healthy. These are the
    same bytes, already validated at init (``>= _HMAC_KEY_MIN_BYTES``, see
    ``_load_or_create_hmac_key``).

    Returns ``None`` when no initialized singleton exists in this process (the
    verifying MCP process, typically) or the cached key is unusable.
    """
    inst = SecurityEventLog._instance
    if inst is None or not getattr(inst, "_initialized", False):
        return None
    key = getattr(inst, "_hmac_key", None)
    if isinstance(key, bytes) and len(key) >= _HMAC_KEY_MIN_BYTES:
        return key
    return None


# --------------------------------------------------------------------------- #
# Compatibility facade. ``SecurityEventLog``'s collaborators live in the owner
# modules in ``_OWNERS``: each function there whose first parameter is ``self`` is
# bound into the class above by name, so every member, field and patch target stays
# on the one class. Every module-level name that moved stays readable as
# ``kiro_crew.sel.<name>``: a name this module's own code reads is imported above,
# and every other one (``_SERVED_BY_OWNER``) is read from its owner by the mirror's
# module class, resolved from :data:`sys.modules` on each use. A write or delete
# through this module reaches this module (when it binds the name) and every owner
# that binds the same object (``kiro_crew._facade_mirror``), so a patch of
# ``kiro_crew.sel.<name>`` reaches every reader it reached when this was one module.
# A served name is absent from this module's namespace on purpose: a binding here
# would shadow the owner for every later read.
#
# The imports at the top stay bound even where this module's own code does not read
# them (``# noqa: F401``): they are its surface, which callers read and tests patch.
#
# No owner imports this module at run time: the owners import each other lowest layer
# first. ``test/test_sel_bindings.py`` pins the bindings, the mirror and that
# direction.
# --------------------------------------------------------------------------- #
#: The owner modules, lowest layer first: the first one that binds a name answers a
#: read of it. Imported above, before the class binds them, so no reader can find one
#: half-built.
_OWNERS: tuple[str, ...] = (
    "kiro_crew.sel_trust_root",
    "kiro_crew.sel_reader",
    "kiro_crew.sel_chain_lock",
    "kiro_crew.sel_append",
    "kiro_crew.sel_rotation",
)


def _import_owners() -> None:
    for dotted in _OWNERS:
        _importlib.import_module(dotted)


_import_owners()

#: Owner -> the names that moved there from this module and are served here unbound.
_SERVED_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.sel_reader": (
        "SelVerification",
        "_MAX_LINE_BYTES",
        "_PIN_BY_FD_SUPPORTED",
        "_SegmentDirPin",
        "_TAIL_CHUNK_BYTES",
        "_open_segment",
        "_open_segment_dir",
        "_parse_timestamp",
    ),
    "kiro_crew.sel_chain_lock": (
        "_CHAIN_HOLDS",
        "_CHAIN_HOLDS_MUTEX",
        "_ChainHold",
        "_LOOP_GATE_POLL_SECS",
        "_SEL_LOCK_FILE",
        "_acquire_chain_lock_on_loop",
        "_acquire_gate_loop_side",
        "_chain_hold_release",
        "_open_lock_sidecar",
        "_pin_lock_dir",
        "_try_join_chain_hold",
    ),
    "kiro_crew.sel_append": (
        "SelChainContention",
        "_APPEND_RETRIES",
        "_ChainTipBeyondBound",
        "_LiveLogReplaced",
        "_identity_changed",
        "_live_log_moved_on",
    ),
    "kiro_crew.sel_rotation": (
        "_RETENTION_DAYS",
        "_ROTATE_LOCK_FILE",
        "_SEGMENT_KEEP",
        "_SEGMENT_MAX_BYTES",
        "_SEGMENT_NAME_PROBES",
        "_SEGMENT_NAME_RE",
        "_SEGMENT_SCAN_CAP",
        "_segment_seq",
    ),
}

#: Served name -> the dotted NAME of its owner, never the module object: the owner is
#: read from :data:`sys.modules` on each use, so a module purged and imported again is
#: seen at once. Bound empty before the install below, which fills it.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__],
    _OWNERS,
    excluded=(),
    surface=frozenset(globals()).union(*_SERVED_BY_OWNER.values()),
)
_EXPORTS.update(_MIRROR.served)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# ``from kiro_crew.sel import *`` binds only the names this list holds: nothing can
# enumerate what the mirror serves, so a served name reaches a star importer only by
# being listed here. The list is DERIVED from what this module binds plus the served
# names, so it is not a third list of names to keep in step. ``TYPE_CHECKING`` exists
# only for the manifest below, so a star import binds what it bound before the owners.
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name != "TYPE_CHECKING"
)

if TYPE_CHECKING:  # the served names, visible to type checkers and IDEs
    from kiro_crew.sel_append import (  # noqa: F401
        _APPEND_RETRIES,
        SelChainContention,
        _ChainTipBeyondBound,
        _identity_changed,
        _live_log_moved_on,
        _LiveLogReplaced,
    )
    from kiro_crew.sel_chain_lock import (  # noqa: F401
        _CHAIN_HOLDS,
        _CHAIN_HOLDS_MUTEX,
        _LOOP_GATE_POLL_SECS,
        _SEL_LOCK_FILE,
        _acquire_chain_lock_on_loop,
        _acquire_gate_loop_side,
        _chain_hold_release,
        _ChainHold,
        _open_lock_sidecar,
        _pin_lock_dir,
        _try_join_chain_hold,
    )
    from kiro_crew.sel_reader import (  # noqa: F401
        _MAX_LINE_BYTES,
        _PIN_BY_FD_SUPPORTED,
        _TAIL_CHUNK_BYTES,
        SelVerification,
        _open_segment,
        _open_segment_dir,
        _parse_timestamp,
        _SegmentDirPin,
    )
    from kiro_crew.sel_rotation import (  # noqa: F401
        _RETENTION_DAYS,
        _ROTATE_LOCK_FILE,
        _SEGMENT_KEEP,
        _SEGMENT_MAX_BYTES,
        _SEGMENT_NAME_PROBES,
        _SEGMENT_NAME_RE,
        _SEGMENT_SCAN_CAP,
        _segment_seq,
    )
