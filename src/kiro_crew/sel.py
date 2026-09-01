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
import contextlib
import errno
import functools
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import stat
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Literal, NamedTuple, overload

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.credential_patterns import AWS_KEY_ID_PREFIXES

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


def _on_event_loop() -> bool:
    """True when called on a thread running an asyncio event loop.

    Used to refuse a contended lock acquire, and to bound an otherwise unbounded
    tail scan, so neither stalls that loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _acquire_chain_lock_on_loop(fd: int) -> bool:
    """Try the chain lock ONCE, without blocking. False if it is contended.

    Single-shot by design: there is no retry and no sleep on this path. A poll
    spin here — however short each nap — sleeps the GATEWAY EVENT LOOP, so the
    stall is paid by every session the loop serves, not just the caller being
    audited. Refusing immediately keeps the loop responsive and leaves the
    append path fail-closed: the loop-side caller raises and the action it was
    about to audit is refused rather than proceeding unaudited. Off-loop callers
    (the background writer, the sync path) still take the blocking cross-process
    lock, which is where routine overlap is absorbed at no cost to the loop.
    """
    return platform_compat.try_acquire_lock(fd, exclusive=True)


class _ChainTipBeyondBound(OSError):
    """The chain tip lies past a bounded tail read.

    An OSError subclass so the append path's existing fail-closed handling
    covers it, but a distinct type so _read_last_hash's fail-soft ``except
    OSError`` (which exists for genuine read errors and returns "") cannot
    swallow it and hand back a genesis tip.
    """


_SEL_FILE = "security_events.jsonl"
# Sidecar whose advisory lock serializes chain writes ACROSS PROCESSES. It lives
# in _TRUST_SUBDIR, not beside the log: that directory is owner-only and inside
# the sensitive-path floor, so the audited agent cannot unlink or hold the lock
# out from under the writers. It is also deliberately not the log file itself —
# msvcrt.locking() locks a byte range at offset 0 and so needs an fd whose offset
# the caller may move freely, which the O_APPEND log fd is not.
_SEL_LOCK_FILE = "security_events.lock"


class _ChainHold:
    """One process-wide cross-process chain-lock hold, keyed by lock path.

    MODULE-level (not per-instance) because flock is per open file
    description: a second ``SecurityEventLog`` instance in the same process
    (test suites churn the singleton; embedded deployments may hold several)
    opening its own fd reads its own process as a FOREIGN writer, and the
    loop-side single-shot then denies critical audits spuriously -- measured
    as the issue-radar trust tests failing whenever a sibling instance's
    writer was mid-append. ``gate`` serializes the critical sections of every
    holder and joiner within the process, which is what makes a cross-instance
    join chain-safe: instances do not share ``_lock``, so without the gate two
    of them could chain and append concurrently.
    """

    __slots__ = ("fd", "unlock", "count", "kind", "gate")

    def __init__(self, fd: int, unlock: Callable[[], None], kind: str) -> None:
        self.fd = fd
        self.unlock = unlock
        self.count = 1
        self.kind = kind
        self.gate = threading.Lock()


_CHAIN_HOLDS: dict[str, _ChainHold] = {}
_CHAIN_HOLDS_MUTEX = threading.Lock()


def _try_join_chain_hold(key: str, kind: str) -> _ChainHold | None:
    """Join this process's existing hold on *key*, or ``None`` when none exists.

    Raises on the event-loop thread when the hold's label is a heavy step
    (prune, rotation) -- joining those is the stall the label exists to
    prevent. A non-"append" joiner promotes the label: the promotion is sticky
    until the hold fully drains, so if the promoting joiner leaves first the
    label may briefly over-refuse loop-side joins while only an append remains
    -- the fail-closed direction, bounded by that append's own short work.
    """
    with _CHAIN_HOLDS_MUTEX:
        hold = _CHAIN_HOLDS.get(key)
        if hold is None:
            return None
        if _on_event_loop() and hold.kind != "append":
            raise OSError(
                f"SEL chain lock is held by this process's {hold.kind}; "
                "refusing to wait for it on the event-loop thread"
            )
        hold.count += 1
        if kind != "append":
            hold.kind = kind
        return hold


_LOOP_GATE_POLL_SECS = 0.05


def _acquire_gate_loop_side(hold: _ChainHold) -> None:
    """Acquire *hold.gate* on the event-loop thread, label-aware.

    The label check in :func:`_try_join_chain_hold` runs BEFORE the gate wait,
    so a joiner admitted while the hold reads "append" can then wait through a
    promotion that lands after the check -- rotation relabels the hold for its
    heavy step (:meth:`SecurityEventLog._chain_hold_relabel`), and a prune
    joiner promotes the label sticky. Waiting through either from the event
    loop is the stall the label exists to prevent. Poll the gate in short
    slices and re-read the label between attempts: the moment the hold is
    promoted to a heavy step, raise instead of continuing to wait. Off-loop
    joiners keep the plain blocking acquire -- their wait stalls one worker
    thread, not the gateway.

    The caller has already joined (count incremented), so on raise it must
    release its membership; the poll slice bounds how long a loop-side joiner
    can overstay into a promoted step.
    """
    while True:
        if hold.gate.acquire(timeout=_LOOP_GATE_POLL_SECS):
            return
        if hold.kind != "append":
            raise OSError(
                f"SEL chain lock was promoted to {hold.kind} while waiting; "
                "refusing to keep waiting for it on the event-loop thread"
            )


def _chain_hold_release(key: str) -> None:
    """Leave the chain-lock hold; the last thread out releases the flock.

    The unlock and close run UNDER the registry mutex: releasing the flock
    after deleting the record would open a window where the lock is still
    held but the registry is empty, and a loop-side single shot in that
    window would read its own process as a foreign writer and refuse a
    critical audit. Both syscalls are fast and local.
    """
    with _CHAIN_HOLDS_MUTEX:
        hold = _CHAIN_HOLDS[key]
        hold.count -= 1
        if hold.count > 0:
            return
        try:
            hold.unlock()
        finally:
            os.close(hold.fd)
        del _CHAIN_HOLDS[key]


_RETENTION_DAYS = 365
# ── Size rotation ──
# The live log is closed (renamed into _SEGMENT_SUBDIR) once an append would
# push it past _SEGMENT_MAX_BYTES, and at most _SEGMENT_KEEP closed segments are
# retained, oldest deleted first. Without this the log grew without bound: a
# long-running install reached 4.09 GB, at which point the sanctioned reader was
# impractical and every append/read paid the size (issue #4843). The ceiling is
# _SEGMENT_MAX_BYTES * (_SEGMENT_KEEP + 1) -- ~256 MiB, roughly 500k events at
# the ~513 bytes/event measured on a real log. Age-based retention
# (_RETENTION_DAYS, swept by prune()) still applies on top and is unchanged;
# size rotation is what bounds the log BETWEEN those daily sweeps, which is the
# window the 4.09 GB was accumulated in.
# Closed segment: security_events-<6-digit sequence>-<UTC stamp>.jsonl. The
# SEQUENCE, not the timestamp, orders segments: it is derived from the highest
# one still on disk plus one, so it keeps increasing across retention deletions
# and is immune to a clock step (an NTP correction that moved the wall clock
# backwards would otherwise mint a name sorting as the OLDEST segment, and
# retention would delete the newest log). The stamp is carried for humans
# reading the directory. Retention must never delete a merely prefix-matching
# file an operator parked here, so the pattern is matched in full.
_SEGMENT_SUBDIR = "security_events.d"
_SEGMENT_MAX_BYTES = 32 * 1024 * 1024
_SEGMENT_KEEP = 7
_SEGMENT_NAME_RE = re.compile(
    r"security_events-(?P<seq>\d{6,})-(?P<stamp>\d{8}T\d{6}Z)\.jsonl"
)
# Cross-process rotation mutex. It lives INSIDE the segment dir so it inherits
# that directory's sensitive-path fence: a lock file the agent could hold would
# let it suppress rotation and bring back the unbounded growth. Not a segment
# name, so _segments_oldest_first() never sees it and retention never deletes it.
_ROTATE_LOCK_FILE = ".rotate.lock"
# Ceiling on entries examined when enumerating segments. Rotation itself keeps at
# most _SEGMENT_KEEP + 1 files there, so a larger directory is someone else's
# doing; the walk runs on the append path (where a critical audit is written
# inline, sometimes on the event loop) and must not scale with a directory an
# agent filled before this release.
_SEGMENT_SCAN_CAP = 4096
# Re-chain attempts when a concurrent rotation replaces the live log between
# chaining and appending. Each collision is a lost microsecond race; after this
# many the record is written unguarded, because a missing audit record is worse
# than one suspect chain link.
_APPEND_RETRIES = 3
# Ceiling on collision probes when minting a segment name. Each probe is a
# filesystem stat on the append path (where a critical audit is written inline,
# sometimes on the event loop), so a directory pre-filled with consecutive
# segment names must not be able to make rotation stat its way through it.
_SEGMENT_NAME_PROBES = 64


class _LiveLogReplaced(Exception):
    """The live log was swapped by another process between chaining and appending.

    Internal control flow only: raised by ``_append_lines_locked`` BEFORE anything
    is written and handled by ``_append_chained_locked``, which re-anchors and
    chains the batch again. Never escapes the module.
    """


class SelChainContention(OSError):
    """Repeated foreign writes prevented a correctly-chained append.

    Deliberately an ``OSError``: this class already has one answer for an audit
    that cannot be written, and it is not "write it wrong". A ``critical=True``
    caller re-raises and refuses the action it was about to audit (audit-or-deny),
    and the background writer drops the batch with a warning exactly as it does
    for a full disk. Raising the same TYPE those paths already handle keeps that
    behaviour without a new branch at either site.

    Reaching this needs a foreign write to land between our chaining and our open
    on every one of ``_APPEND_RETRIES + 1`` attempts.
    """


def _live_log_moved_on(
    previous: tuple[int, int, int], current: tuple[int, int, int]
) -> bool:
    """Whether the live log is no longer the file+position *previous* describes.

    Both arguments are ``(st_dev, st_ino, st_size)``. ONE predicate serves both the
    pre-append check and the post-open guard, because they are asking the same
    question and answering it differently is how a stale tip survives a collision:
    a detector that fires on growth paired with a re-anchor that only reacts to a
    shrink leaves the retry writing the very ``prev_hash`` the collision proved
    wrong.

    Two independent signals:

    * a DIFFERENT inode -- the file was replaced (a rotation). Skipped when either
      inode is 0, which some Windows filesystems report instead of a usable index.
    * a DIFFERENT size -- somebody appended, so the file no longer ends where we
      chained from. This also covers the case no inode comparison can see: a
      rotator anchored on an ABSENT replacement has no inode, and only the size
      reveals that another process already put a record there.

    Our OWN writes never trip this. The anchor is refreshed from the fd's
    post-write ``fstat`` on every append, so a difference means somebody else.
    """
    prev_dev, prev_ino, prev_size = previous
    cur_dev, cur_ino, cur_size = current
    if prev_ino and cur_ino and (prev_ino, prev_dev) != (cur_ino, cur_dev):
        return True
    return prev_size != cur_size


def _identity_changed(expect: tuple[int, int, int], actual: os.stat_result) -> bool:
    """Whether an opened fd is no longer the file+position *expect* describes.

    Thin adapter over :func:`_live_log_moved_on` so the guard and the pre-append
    check cannot drift apart.
    """
    return _live_log_moved_on(expect, (actual.st_dev, actual.st_ino, actual.st_size))


# Tail-read chunk for recent(): the log is append-only, so the newest records
# live at the END. Reading backward in chunks keeps a bounded read bounded
# instead of loading the whole segment (the old recent() did
# read_text().splitlines(), i.e. a 4.09 GB allocation to print 20 lines).
_TAIL_CHUNK_BYTES = 256 * 1024
# Ceiling on bytes held while looking for a line boundary in the backward reader.
# A real SEL record is a few hundred bytes; anything past this has no newline in
# it, which means a truncated write or a file someone else placed there. Bounds
# the memory an attacker-influenced segment can make a reader allocate.
_MAX_LINE_BYTES = 4 * 1024 * 1024
_HMAC_KEY_FILE = "sel_hmac.key"
# Dedicated trust-root subdirectory (owner-only, 0o700) holding the HMAC key.
# The key must not live NEXT TO the log it signs: an actor with write access to
# the log directory could otherwise read the key, rewrite security_events.jsonl,
# and re-sign a clean-looking chain that verify_integrity() accepts. A legacy
# key at ``<config_dir>/sel_hmac.key`` is migrated in atomically (same bytes, so
# every existing chain still verifies) — see _load_or_create_hmac_key.
_TRUST_SUBDIR = "trust"
# Minimum accepted HMAC key length. os.urandom(32) is always written, so a
# shorter key on disk means truncation/corruption/tampering — signing the
# audit chain with an empty or short key yields a predictable, forgeable MAC
# and silently disables the chain's tamper-evidence. Mirrors the >= 32-byte
# requirement enforced in dashboard/token_secret.py.
_HMAC_KEY_MIN_BYTES = 32
_MAX_ARG_LEN = 500
# Background-writer tuning. The queue is unbounded so callers never block; a
# crash/kill can lose at most the events still queued (audit log is
# eventually-durable, not synchronously-durable). flush() drains it before any
# read so read-after-write stays consistent.
_QUEUE_DRAIN_BATCH = 256  # max events appended per open() in the writer loop
_FLUSH_TIMEOUT_SECS = 5.0  # bound on flush() so a stuck writer can't hang reads


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
                self._flush_batch(batch)
            except Exception:
                logger.warning("SEL writer batch failed for %d events", len(batch), exc_info=True)
            finally:
                self._decr_pending(len(batch))
            if stop:
                return

    def _decr_pending(self, n: int) -> None:
        """Drop *n* from the pending counter and wake any flush() waiters."""
        with self._pending_cond:
            self._pending = max(0, self._pending - n)
            if self._pending == 0:
                self._pending_cond.notify_all()

    @contextlib.contextmanager
    def _chain_lock(self, *, kind: str = "append") -> Iterator[None]:
        """Serialize the read-tip → chain → append sequence across PROCESSES.

        ``_lock`` is a ``threading.Lock``, so it only orders writers inside one
        interpreter. The gateway and each managed MCP server are separate
        processes sharing one log file, and each holds its own singleton with
        its own cached tip — so two of them chain off the same ``prev_hash``,
        and because each only ever advances its OWN cache the two lineages fork
        permanently, making verify_integrity() report every later entry from the
        losing process as tampered. ``O_APPEND`` guarantees no torn or
        overwritten bytes; it guarantees nothing about the read-compute-append
        sequence, which is what the chain depends on.

        The sidecar lives in the trust subdirectory, not beside the log: that
        directory is owner-only and inside the sensitive-path floor, whereas a
        sibling of ``security_events.jsonl`` is not covered by its exact-leaf
        deny-list entry. An audited agent able to unlink the sidecar mid-hold
        would leave two writers holding locks on different inodes — the very
        fork this serialization exists to prevent — and one able to hold it
        could wedge every writer.

        On the asyncio event-loop thread the acquire is a SINGLE nonblocking
        attempt that then fails closed, because waiting there — even a short
        poll spin — stalls chat and the heartbeat for every session the loop
        serves, and ``prune`` holds this lock across a whole streaming rewrite.
        A refused acquire raises, which ``_flush_batch`` already turns into a
        rollback plus warning — or into a propagated error for a critical audit,
        which is the audit-or-deny contract working rather than a stalled
        gateway. Off the loop (the background writer thread, and ``prune`` in an
        executor) the acquire waits normally, which is where routine overlap
        between processes is absorbed.

        Callers must take this lock BEFORE ``_lock``, in both write paths. The
        reverse order lets a cross-process wait stall the loop indirectly: a
        writer thread holding ``_lock`` while it waits here leaves a loop-side
        critical audit blocking on ``_lock``, which has no fail-closed gate of
        its own.

        SAME-PROCESS callers JOIN the one hold instead of contending. flock is
        per open file description, so a second fd opened in this process --
        another thread of this instance, or a SIBLING instance on the same
        directory -- reads its own process as a foreign writer, and the
        loop-side single shot then fails a critical audit closed against our
        own background writer mid-batch (measured twice: the fingerprint
        read's terminal audit racing the writer flushing the same call's
        earlier best-effort event, and the issue-radar trust tests failing
        whenever a sibling instance's writer held the lock). The hold registry
        is therefore MODULE-level, keyed by lock path, and each hold carries a
        gate that serializes the critical sections of holders and joiners --
        instances do not share ``_lock``, so the gate is what keeps a
        cross-instance join chain-safe. What a loop-side joiner waits on is
        one append's bounded local disk work; the heavy holds are refused by
        label instead: ``prune`` (``kind="prune"``) spans a whole streaming
        rewrite, and rotation relabels the hold for its step
        (:meth:`_chain_hold_relabel`).
        """
        lock_path = self._chain_lock_path()
        key = str(lock_path)
        hold = _try_join_chain_hold(key, kind)
        if hold is not None:
            # The gate serializes the critical sections of holders and joiners
            # ACROSS instances (they do not share ``_lock``); a loop-side
            # joiner's wait here is bounded by one append's local disk work --
            # the heavy holds (prune, rotation) were already refused by their
            # label inside _try_join_chain_hold. The label can still be
            # PROMOTED after that check (rotation's relabel, a prune joining),
            # so the loop-side wait re-reads it between poll slices and fails
            # closed on promotion instead of waiting through the heavy step.
            try:
                if _on_event_loop():
                    _acquire_gate_loop_side(hold)
                else:
                    hold.gate.acquire()
                try:
                    yield
                finally:
                    hold.gate.release()
            finally:
                _chain_hold_release(key)
            return
        if lock_path != self._hmac_key_file:
            # 0o700 to match how the trust dir is created for the HMAC key:
            # the sidecar's protection is the directory's, so a laxer mode
            # here would quietly undo it.
            lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A sidecar that is a LINK is not a lock. If the path resolves elsewhere,
        # replacing its target hands two writers locks on different inodes — the
        # exact fork this serialization exists to prevent — so a link planted
        # before this code ran must be refused, not followed. O_NOFOLLOW makes
        # that atomic on POSIX (no TOCTOU window between checking and opening);
        # the explicit probe carries Windows junctions, which O_NOFOLLOW does not
        # exist for. This is the same defense _load_or_create_hmac_key already
        # applies to this directory.
        if platform_compat.is_link_or_junction(lock_path):
            raise OSError(
                f"SEL chain-lock sidecar {lock_path} is a link; refusing to lock it"
            )
        # Windows' CRT text mode strips a trailing 0x1A while opening a file
        # for update. The fallback lock path can be the raw HMAC key, so this
        # descriptor must be binary even though the lock code never writes it.
        fd = os.open(
            lock_path,
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        joined: _ChainHold | None = None
        unlock: Callable[[], None] | None = None
        try:
            # A hard link makes the same inode reachable under a second name the
            # deny-list does not cover; O_NOFOLLOW says nothing about that.
            if os.fstat(fd).st_nlink > 1:
                raise OSError(
                    f"SEL chain-lock sidecar {lock_path} is hard-linked; refusing to lock it"
                )
            # Windows locks a byte RANGE (msvcrt.locking on byte 0), so a fresh
            # empty sidecar has nothing to lock — same shape as the rotation
            # lock, which primes itself with one NUL byte. Prime only the
            # sidecar we created: the legacy-key fallback path never writes
            # through this fd (the key file is never empty — init rejects a
            # short key — and its bytes must not be touched).
            if lock_path != self._hmac_key_file and os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            if _on_event_loop():
                if _acquire_chain_lock_on_loop(fd):
                    unlock = functools.partial(platform_compat.release_lock, fd)
                else:
                    # The flock is held -- but by WHOM? A sibling thread or
                    # instance of THIS process may have won the acquire between
                    # our registry check above and this attempt (its record is
                    # published right after its flock succeeds). Re-check and
                    # JOIN it; only a genuinely foreign process is refused.
                    joined = _try_join_chain_hold(key, kind)
                    if joined is None:
                        raise OSError(
                            "SEL chain lock is held by another writer; refusing "
                            "to wait for it on the event-loop thread"
                        )
            else:
                # Blocking acquire through the same context manager the rest of
                # the codebase uses, held open in an ExitStack so the LAST
                # thread out of the hold can run its platform-correct release
                # (a plain `with` would release at this frame's exit, pulling
                # the lock out from under a joiner still inside).
                lock_scope = contextlib.ExitStack()
                lock_scope.enter_context(platform_compat.file_lock(fd, exclusive=True))
                unlock = lock_scope.close
        except BaseException:
            os.close(fd)
            raise
        if joined is not None:
            os.close(fd)  # our probe fd; the hold keeps its own
            # This path is reached only on the event-loop thread (the off-loop
            # branch blocks on the flock instead), so the wait must stay
            # label-aware for the same promotion race as the first join site.
            try:
                _acquire_gate_loop_side(joined)
                try:
                    yield
                finally:
                    joined.gate.release()
            finally:
                _chain_hold_release(key)
            return
        assert unlock is not None
        # The flock is ours. Publish the hold so same-process callers join it;
        # the LAST one out releases (see _chain_hold_release) — releasing in
        # this frame's finally would pull the lock out from under a joiner
        # still inside its critical section.
        hold = _ChainHold(fd, unlock, kind)
        with _CHAIN_HOLDS_MUTEX:
            _CHAIN_HOLDS[key] = hold
        hold.gate.acquire()
        try:
            yield
        finally:
            hold.gate.release()
            _chain_hold_release(key)

    def _chain_lock_path(self) -> Path:
        """The file the cross-process chain lock is taken on.

        Normally the sidecar in the trust subdirectory. When
        ``_load_or_create_hmac_key`` fell back to the legacy key location
        (uncreatable trust dir, or a planted link on it that could not be
        removed), the LEGACY KEY FILE itself: retrying the mkdir on every
        append would fail the same way — dropping every best-effort audit and
        denying every critical action on an install that is otherwise signing
        fine — and the legacy key is the one sibling of the log the
        sensitive-path deny list has protected all along. The lock is advisory
        and the fd is never written, so locking the key file cannot disturb
        its bytes.
        """
        lock_dir = self._dir / _TRUST_SUBDIR
        if self._hmac_key_file.parent != lock_dir:
            return self._hmac_key_file
        return lock_dir / _SEL_LOCK_FILE

    @contextlib.contextmanager
    def _chain_hold_relabel(self, kind: str) -> Iterator[None]:
        """Temporarily relabel this process's chain hold for a heavy step.

        Loop-side joins are refused for any non-"append" label, so elevating
        the label around a slow step that runs INSIDE an append hold (rotation:
        a directory scan, a rename, a retention sweep) makes a loop-side
        critical audit arriving during that step fail closed -- audit-or-deny --
        instead of joining and then blocking for the step's whole duration.
        Plain appends before and after stay joinable.

        The restore is conditional: if another thread promoted the label while
        the step ran (a prune joining mid-step), that stricter label is kept --
        blindly restoring would re-admit loop-side joins into the prune's
        streaming rewrite. No-op when this process holds no chain lock (the
        label then has no reader).
        """
        key = str(self._chain_lock_path())
        with _CHAIN_HOLDS_MUTEX:
            hold = _CHAIN_HOLDS.get(key)
            previous = hold.kind if hold is not None else ""
            if hold is not None:
                hold.kind = kind
        try:
            yield
        finally:
            if hold is not None:
                with _CHAIN_HOLDS_MUTEX:
                    if _CHAIN_HOLDS.get(key) is hold and hold.kind == kind:
                        hold.kind = previous

    def _flush_batch(
        self,
        events: list[SecurityEvent],
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Append a batch of events under the chain lock, then forward them.

        When ``raise_on_error=True`` a filesystem failure (unwritable SEL file,
        full disk, un-creatable dir) is re-raised after rolling the chain tip
        back, so a fail-closed caller (critical audit) can refuse the action it
        was about to audit. The default (async writer / best-effort) swallows
        the error and keeps the writer thread alive.

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
                    except OSError:
                        if raise_on_error:
                            raise
                        logger.warning("SEL dir create failed for %d events", len(events), exc_info=True)
                        return
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
                        try:
                            self._append_chained_locked(events)
                        except OSError:
                            if raise_on_error:
                                raise
                            logger.warning(
                                "SEL append failed for %d events", len(events), exc_info=True
                            )
                    callback = self._forward_callback
        except OSError:
            # The chain lock itself was unavailable: held by another writer while
            # this thread is the event loop (single-shot fail-closed acquire), or
            # its sidecar was unusable. Audit-or-deny for a critical caller; a
            # best-effort caller drops the batch with a warning.
            if raise_on_error:
                raise
            logger.warning(
                "SEL chain lock unavailable for %d events", len(events), exc_info=True
            )
        if callback:
            for event in events:
                self._forward_event(callback, event)

    def _append_chained_locked(self, events: list[SecurityEvent]) -> None:
        """Chain *events* onto the live log, re-chaining if it moves under us.

        Caller holds the cross-process chain lock AND ``_lock``. The tip is
        rolled back on any failure so a record never chains off a hash that was
        not persisted.

        The retry survives under the chain lock because the lock orders sibling
        APPENDS, not everything that can move the file: this instance's cached
        tip is stale when a sibling appended before we took the lock, and a
        sibling's ROTATION — serialized by the rotation lock, not this one —
        can rename the file between our chaining and our open. In both cases
        the append VALIDATES the file it opened and re-chains — see
        :meth:`_append_lines_locked`.

        EVERY attempt is validated, including the last. When the retries are
        exhausted the batch is NOT written; :class:`SelChainContention` is raised.

        An earlier revision wrote the final attempt unguarded, reasoning that a
        missing audit record is worse than one suspect chain link. That was wrong
        twice over. A knowingly-broken link does not read as "one imperfect
        record" -- it makes ``security verify`` report the log as COMPROMISED, and
        an investigator cannot tell that false signal apart from real tampering.
        This PR already deleted the rotation record's predecessor-hash claim on
        exactly that principle (a check that can fire on a benign cause is worse
        than no check); writing a bad link is the same mistake with the sign
        flipped. And it invented a third behaviour: this class already answers an
        unwritable audit by DENYING the action (``critical=True``, audit-or-deny)
        or by dropping the batch with a warning (best-effort) -- never by writing
        something it knows to be corrupt.

        Raising an ``OSError`` subclass is what routes it correctly with no new
        plumbing: a critical caller re-raises and refuses the action it was about
        to audit, and the background writer treats it exactly as it already treats
        a full disk.
        """
        for attempt in range(_APPEND_RETRIES + 1):
            prev_last_hash = self._last_hash
            lines = self._chain_locked(events)
            try:
                self._append_lines_locked(lines, expect=self._live_seen)
                return
            except _LiveLogReplaced:
                # The guard proved the file moved on between our chaining and our
                # open, and the records were never written. Roll the tip back, then
                # re-anchor UNCONDITIONALLY -- the question the predicate would ask
                # has already been answered by the collision itself.
                self._last_hash = prev_last_hash
                logger.info(
                    "SEL live log moved on mid-append; re-chaining this batch "
                    "(attempt %d of %d)",
                    attempt + 1,
                    _APPEND_RETRIES + 1,
                )
                self._reanchor_now()
            except OSError:
                self._last_hash = prev_last_hash  # nothing persisted — roll back
                raise
        raise SelChainContention(
            f"could not append {len(events)} SEL event(s) with a valid chain link "
            f"after {_APPEND_RETRIES + 1} attempts: another process kept writing to "
            "the live log between chaining and appending"
        )

    def _chain_locked(self, events: list[SecurityEvent]) -> list[str]:
        """Stamp the HMAC chain onto *events* and return their JSONL lines.

        Advances ``_last_hash`` per event. Caller holds ``_lock`` and is
        responsible for rolling the tip back if the write does not land.
        """
        lines: list[str] = []
        for event in events:
            event.prev_hash = self._last_hash
            event.entry_hash = self._compute_hash(event)
            lines.append(json.dumps(asdict(event)) + "\n")
            self._last_hash = event.entry_hash
        return lines

    def _append_lines_locked(
        self, lines: list[str], *, expect: tuple[int, int, int] | None = None
    ) -> None:
        """Append already-chained JSONL *lines* to the live log. Caller holds ``_lock``.

        ``expect`` is the identity of the file whose tip *lines* were chained from.
        When given, the file actually opened is validated BY FD before anything is
        written, and :class:`_LiveLogReplaced` is raised if it is a different file.

        This is what makes a lock-free append safe against a concurrent rotation,
        and why the check is on the FD rather than on the path. Two outcomes, both
        correct:

        * the rename has NOT happened yet -- we hold the old inode, the identity
          matches, we write, and the winner's rename carries our record into the
          segment still correctly chained off the tip we used;
        * the rename HAS happened -- ``O_CREAT`` gave us a brand-new file with a
          different inode, we notice before writing, and the caller re-anchors and
          re-chains instead of leaving a record whose ``prev_hash`` lives in
          another file.

        A path ``stat`` cannot do this: whatever it observed may be replaced before
        the ``open`` that follows it.
        """
        # Use os.open with explicit 0o600 mode to prevent other users
        # from reading the security audit log.
        fd = os.open(
            self._path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            actual = os.fstat(f.fileno())
            if expect is not None and _identity_changed(expect, actual):
                raise _LiveLogReplaced(
                    "live log was replaced between chaining and appending"
                )
            # If a crash mid-append left a truncated tail line WITHOUT a
            # trailing newline, writing directly (O_APPEND) would glue
            # this record onto the corrupt fragment, forming a single
            # unparseable line. _read_last_hash() recovers the correct
            # prev_hash from the last complete record, but that glued
            # line stays unreadable by verify_integrity — so the new
            # event, though correctly chained, is orphaned from every
            # parseable record. Insert a newline boundary first so the
            # new record starts on a fresh, parseable line. We do NOT
            # truncate the corrupt fragment: the SEL log is append-only
            # forensic evidence, and the fragment is preserved as its
            # own (skipped) line.
            if self._ends_without_newline():
                f.write("\n")
            f.write("".join(lines))
            f.flush()
            # Record what we wrote to from the FD, not the path: if a rotation
            # renames the path immediately after this write, the file we actually
            # appended to is the one we must remember, so the NEXT append's guard
            # can see that it has moved on.
            written = os.fstat(f.fileno())
        # Ensure permissions are correct even if file pre-existed with
        # wrong mode (e.g. created by an older version). POSIX repair only,
        # deliberately still NOT ``platform_compat.restrict_to_owner``: on POSIX
        # that helper IS this exact call, so a swap would add only the Windows
        # owner-only DACL — and this append path can run inline on a caller's
        # thread that may be the asyncio event loop (the ``critical=True``
        # audit-or-deny write, and the fallback taken when the writer thread
        # cannot start, see ``_may_rotate``), where a DACL write to a UNC or
        # mapped-drive path costs an unbounded SMB round-trip. Adopting the
        # helper here therefore means first deciding what a non-local volume
        # gets, the way ``write_config_atomically`` gates its own lockdown on
        # ``windows_acl.volume_is_local``; until that is settled the log keeps
        # whatever DACL it inherits on Windows. Tracked in #6359.
        try:
            os.chmod(self._path, 0o600)  # lockdown-ok: #5228 -- unbounded SMB round-trip on the loop
        except OSError:
            logger.warning("Failed to enforce 0o600 permissions on SEL audit log %s", self._path, exc_info=True)
        self._live_seen = (written.st_dev, written.st_ino, written.st_size)

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

    def _load_or_create_hmac_key(self) -> bytes:
        trust_dir = self._dir / _TRUST_SUBDIR
        key_path = trust_dir / _HMAC_KEY_FILE
        legacy_path = self._dir / _HMAC_KEY_FILE
        self._dir.mkdir(parents=True, exist_ok=True)
        # The upgrade boundary is hostile ground: BEFORE this release, ``trust``
        # was not on the sensitive-path deny list, so an agent could have
        # pre-planted a ``trust`` symlink/junction (pointing the key write
        # somewhere it can read) or a ``trust/sel_hmac.key`` with bytes it
        # knows — letting it forge SEL chain and session-identity MACs after
        # the upgrade. Two defenses, both BEFORE anything trusts the
        # destination:
        #   1. a linked ``trust`` entry is removed (link only, never its
        #      target) so the real directory is created in its place;
        #   2. when a genuine legacy key exists, it WINS over any
        #      pre-existing destination file (see the migration block below) —
        #      the legacy key is the only one that was deny-list-protected all
        #      along, so it is the only trustworthy chain anchor here.
        if platform_compat.is_link_or_junction(trust_dir):
            logger.warning(
                "SEL trust dir %s is a symlink/junction — removing the link "
                "(planted before upgrade?) and creating a real directory",
                trust_dir,
            )
            try:
                platform_compat.unlink_link_or_junction(trust_dir)
            except OSError:
                # Read-only config dir: the link cannot be removed. NEVER use
                # the linked destination — fall back to the legacy key when one
                # exists (same fail-soft as an uncreatable trust dir below);
                # a fresh install with an unremovable planted link cannot
                # proceed safely, so surface the failure.
                if legacy_path.exists():
                    logger.warning(
                        "cannot remove linked SEL trust dir %s; continuing with "
                        "the legacy key location",
                        trust_dir,
                        exc_info=True,
                    )
                    key_path = legacy_path
                else:
                    raise
        # Owner-only trust dir. mkdir's mode is umask-filtered and ignored when
        # the dir already exists, so chmod_safe re-asserts 0o700 every init
        # (fail-soft: a read-only FS must not take down SecurityEventLog init;
        # Windows relies on the key FILE's owner-only DACL below). Creation
        # failure itself is fail-soft too: a legacy install on a read-only
        # config dir must keep signing with its existing key at the legacy
        # location, not crash before the migration fallback can run.
        if key_path != legacy_path:
            try:
                trust_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                if legacy_path.exists():
                    logger.warning(
                        "cannot create SEL trust dir %s; continuing with the legacy "
                        "key location",
                        trust_dir,
                        exc_info=True,
                    )
                    key_path = legacy_path
                else:
                    # Fresh install on an unwritable config dir: key creation
                    # below would fail anyway — surface the real cause.
                    raise
            else:
                platform_compat.chmod_safe(trust_dir, 0o700)
        # A linked destination KEY file is removed for the same reason as a
        # linked dir above (the link is removed, never its target): a fresh
        # key must never be written THROUGH a planted link, and a read must
        # never follow one.
        if key_path != legacy_path and platform_compat.is_link_or_junction(key_path):
            logger.warning(
                "SEL HMAC key path %s is a symlink/junction — removing the link "
                "(planted before upgrade?)",
                key_path,
            )
            try:
                platform_compat.unlink_link_or_junction(key_path)
            except OSError:
                # Same fail-soft as the linked dir above: never use the linked
                # destination; prefer the legacy key, else surface the failure.
                if legacy_path.exists():
                    logger.warning(
                        "cannot remove linked SEL HMAC key %s; continuing with "
                        "the legacy key location",
                        key_path,
                        exc_info=True,
                    )
                    key_path = legacy_path
                else:
                    raise
        # ── Migration: relocate a legacy key sitting next to the log ──
        # os.replace is atomic on the same filesystem and preserves the key
        # BYTES, so every HMAC chain already written to security_events.jsonl
        # still verifies — no re-signing. The >= length validation and
        # restrict_to_owner below apply to the migrated file exactly as to a
        # pre-existing one at the new path. Skipped when trust-dir creation
        # above already fell back to the legacy location.
        if key_path != legacy_path and legacy_path.exists():
            # The legacy key WINS over any pre-existing destination file.
            # ``trust/`` was not deny-listed before this release, so a file
            # already at the destination on a legacy install is untrustworthy
            # (an agent could have planted bytes it knows and then forged SEL
            # and session-identity MACs); the legacy key is the only anchor
            # that was deny-list-protected all along. os.replace overwrites
            # the destination atomically. Benign overlap (a backup restore
            # resurrecting the legacy file after a real migration) is
            # unaffected: the key never rotates, so the bytes are identical.
            if key_path.exists():
                logger.warning(
                    "pre-existing file at %s is being replaced by the legacy SEL "
                    "HMAC key %s (the legacy key is the deny-list-protected "
                    "trust anchor)",
                    key_path,
                    legacy_path,
                )
            try:
                os.replace(legacy_path, key_path)
                logger.info("migrated SEL HMAC key %s -> %s", legacy_path, key_path)
            except OSError:
                # Ordering is security-relevant: while the legacy source STILL
                # EXISTS it stays the only deny-list-protected trust anchor, so
                # a failed replace must fall back to it — never to a
                # destination file that could have been pre-planted (an
                # attacker able to make os.replace fail must not get their
                # planted key adopted). The destination is trusted only after
                # the legacy source is gone, which on a failed replace can only
                # mean a sibling process completed the same migration (its
                # os.replace moved the SAME legacy bytes there).
                if legacy_path.exists():
                    # Chain continuity beats relocation: if the move fails
                    # (read-only FS, permissions), keep signing with the
                    # legacy file rather than minting a fresh key that would
                    # orphan every already-chained record. Path stays legacy
                    # for this process so sel_hmac_key_path() reports the
                    # file in use.
                    logger.warning(
                        "failed to migrate SEL HMAC key %s -> %s; continuing with "
                        "the legacy location",
                        legacy_path,
                        key_path,
                        exc_info=True,
                    )
                    key_path = legacy_path
                elif key_path.exists():
                    # Lost the migration race to a sibling process: the key
                    # is already at the new path, and its bytes are the same
                    # legacy bytes — proceed with it.
                    logger.debug(
                        "SEL HMAC key migration raced; using already-migrated %s",
                        key_path,
                    )
                # else: both paths vanished mid-init (external deletion) —
                # fall through to fresh-key creation at the NEW path.
        # Single source of truth for dependent protocols: sel_hmac_key_path()
        # reports THIS resolved path (normally trust/sel_hmac.key; the legacy
        # path only on a failed migration above).
        self._hmac_key_file = key_path
        if key_path.exists():
            existing = key_path.read_bytes()
            # Validate the key BEFORE it is ever used to sign the chain. A
            # 0-byte or too-short key is accepted silently by hmac.new(),
            # producing a predictable, forgeable MAC that disables the audit
            # chain's tamper-evidence. Fail HARD (mirroring token_secret.py's
            # >= 32-byte requirement) rather than silently falling back to a
            # weak key. We RAISE instead of regenerating (unlike
            # token_secret.py's fall-through) because a fresh key would orphan
            # every already-chained record — an operator must consciously
            # rotate/restore the key.
            if len(existing) < _HMAC_KEY_MIN_BYTES:
                raise RuntimeError(
                    f"SEL HMAC key {key_path} is too short ({len(existing)} bytes; "
                    f"require >= {_HMAC_KEY_MIN_BYTES}). Refusing to sign the audit "
                    "chain with a weak/forgeable key. Restore the correct key from "
                    "backup, or remove the file to start a fresh chain with a new key."
                )
            # Re-enforce owner-only perms at LOAD time, not just at creation:
            # the mode may have been relaxed since (backup restore, manual edit,
            # migration) and this key signs the entire audit chain — a
            # group/world-readable key lets any local user forge valid MACs.
            # Mirrors token_secret.py's load-time restrict_to_owner. Fail-SOFT
            # at the call site (warn, don't crash): a read-only FS / chmod
            # failure must not take down SecurityEventLog init.
            try:
                platform_compat.restrict_to_owner(key_path)  # lockdown-ok: load-time re-assert; the only preceding write to key_path is the legacy-key migration rename in a mutually exclusive branch
            except OSError:
                # Logs the key file PATH, never the key bytes.
                logger.warning(  # nosemgrep: python-logger-credential-disclosure
                    "failed to enforce owner-only permissions on SEL HMAC key %s; "
                    "file may be readable by other users",
                    key_path,
                    exc_info=True,
                )
            return existing
        key = os.urandom(32)
        # Create the key ATOMICALLY (temp file + rename via ``atomic_write``,
        # whose short-write loop was modelled on the hand-rolled one this call
        # replaces): a plain os.open()+os.write() is NOT atomic — a crash or
        # full-disk partial write leaves a 0-byte/short key on disk, which the
        # load-time length check above would then reject with a hard
        # RuntimeError on the NEXT boot, bricking every SecurityEventLog()
        # init until an operator manually removes the file. os.replace() makes
        # the key file visible only once it is complete.
        #
        # ``restrict_to_owner=True`` locks the temp file down BEFORE the key
        # bytes reach it — the previous post-rename lockdown left a brand-new
        # key readable under the inherited DACL on Windows for the write
        # window (issue #5285) — and implies 0o600 on POSIX.
        # ``restrict_on_error="warn"`` keeps this site's fail-SOFT policy: a
        # read-only FS / chmod failure must not crash SecurityEventLog init
        # (see test_chmod_failure_is_swallowed). The linked-parent refusal
        # implied by ``restrict_to_owner=True`` raises unconditionally, which
        # is the right behavior for the key that signs the audit chain: a
        # pre-planted link under the trust dir is hostile (#4381).
        atomic_write(key_path, key, restrict_to_owner=True, restrict_on_error="warn")
        return key

    @contextmanager
    def _rotation_window(self) -> Iterator[None]:
        """Rotate if the live log is at the cap, holding the lock over the body.

        Caller holds ``_lock``. The body is the caller's chain + append.

        Below the cap -- the overwhelmingly common case -- this is a single
        ``stat`` and no lock at all, so a normal append pays nothing.

        At the cap it takes a CROSS-PROCESS lock, because ``_lock`` is a thread
        lock and SEVERAL PROCESSES share one data home (the gateway, the CLI,
        cron), all appending to this same file. They therefore reach the cap at
        the same moment, and two things go wrong unserialized:

        * both pick the same target name, and the second ``os.replace`` moves the
          freshly recreated live log onto the segment the first just closed,
          destroying it;
        * the loser keeps its pre-rotation chain tip and appends from it, so its
          first record in the new log names a ``prev_hash`` that is not in that
          file and ``security verify`` reports an untampered log as compromised.

        The acquire is NON-BLOCKING and the whole window is best-effort. A
        critical (``audit-or-deny``) event is written INLINE on its caller's
        thread, and some of those callers are event-loop coroutines, so a
        blocking acquire here could park the loop on another process's rotation —
        the ``no-blocking-call-on-event-loop`` hazard. Rotation is never worth
        that: it is deferrable (the next batch retries) while an audit write is
        not.

        Skipping on contention does NOT leave the loser appending from a stale
        tip, which is the reason the lock spans the append at all: the contended
        path re-checks the live log's identity and re-anchors the tip immediately
        before the caller chains (see :meth:`_reanchor_if_replaced`). A rotation
        that lands between that check and the append is the residual, and it is
        the pre-existing cross-process interleaving race rather than an
        escalation of it -- closing THAT means holding a cross-process lock
        across every audit write, which is both the event-loop hazard above and
        the hot-path cost #4247 is about.

        Every failure -- an uncreatable/planted segment dir, a planted or
        unopenable lock file -- yields WITHOUT rotating so the audit record still
        lands.
        """
        if self._reanchor_if_replaced() < _SEGMENT_MAX_BYTES or not self._ensure_segment_dir():
            yield
            return
        lock_fh = self._open_rotation_lock()
        if lock_fh is None:
            yield
            return
        try:
            if not platform_compat.try_acquire_lock(lock_fh.fileno(), exclusive=True):
                # Another process is rotating right now. Do not wait (see above)
                # and do not trust the tip we anchored before it started.
                logger.debug("SEL rotation lock busy; deferring rotation to the next batch")
                self._reanchor_if_replaced()
                yield
                return
            try:
                # Elevate the chain-hold label for the duration of the heavy
                # step: a loop-side critical audit arriving now must fail
                # closed rather than join and block on ``_lock`` behind the
                # scan + rename + retention sweep.
                with self._chain_hold_relabel("rotation"):
                    self._rotate_under_lock()
                yield
            finally:
                platform_compat.release_lock(lock_fh.fileno())
        finally:
            lock_fh.close()

    def _open_rotation_lock(self) -> IO[bytes] | None:
        """Open the rotation mutex, refusing a planted link. ``None`` if unusable.

        The path must never be followed through a symlink/junction: opening it
        CREATES the file and ``chmod``s it, so a link planted before this release
        (when ``security_events.d`` was not on the sensitive-path floor) would
        have rotation write a byte into, and relax the mode of, whatever the agent
        pointed it at. Same defense and same helpers as the segment dir and the
        SEL trust dir -- the link is removed, never its target -- and if the link
        cannot be removed we decline to rotate rather than open through it.
        """
        lock_path = self._segment_dir / _ROTATE_LOCK_FILE
        if platform_compat.is_link_or_junction(lock_path):
            logger.warning(
                "SEL rotation lock %s is a symlink/junction — removing the link "
                "(planted before upgrade?)",
                lock_path,
            )
            try:
                platform_compat.unlink_link_or_junction(lock_path)
            except OSError:
                logger.warning(
                    "cannot remove linked SEL rotation lock %s; refusing to rotate "
                    "rather than open through the link",
                    lock_path,
                    exc_info=True,
                )
                return None
        try:
            # "a+b": msvcrt.locking needs a writable fd and locks a byte range,
            # so the file must be non-empty (same shape as metrics retention).
            lock_fh = open(lock_path, "a+b")
        except OSError:
            logger.warning("SEL rotation lock %s could not be opened", lock_path, exc_info=True)
            return None
        try:
            lock_fh.seek(0, os.SEEK_END)
            if lock_fh.tell() == 0:
                lock_fh.write(b"\0")
                lock_fh.flush()
            try:
                os.chmod(lock_path, 0o600)  # lockdown-ok: the rotation lock holds no data (a single NUL byte), so there is no payload to expose
            except OSError:
                pass  # perms are hygiene here; the file holds no data
        except OSError:
            lock_fh.close()
            logger.warning("SEL rotation lock %s could not be primed", lock_path, exc_info=True)
            return None
        return lock_fh

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

    def _live_size(self) -> int:
        """Size of the live log on disk, or 0 when it is absent/unreadable."""
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def _live_identity(self) -> tuple[int, int, int]:
        """``(st_dev, st_ino, st_size)`` of the live log, zeros when absent."""
        try:
            st = self._path.stat()
        except OSError:
            return (0, 0, 0)
        return (st.st_dev, st.st_ino, st.st_size)

    def _reanchor_if_replaced(self) -> int:
        """Refresh the cached chain tip if the live log was replaced. Returns its size.

        Caller holds ``_lock``. This is the ONE stat the rotation check already
        needed, so detecting a foreign rotation is free.

        Why it is needed: several processes share one data home and append to this
        file, each caching its own tip in ``_last_hash`` and never re-reading it.
        When ANOTHER process rotates, this process's next append usually does not
        see the cap at all (the live log is small and fresh), so it never enters
        the rotation window — and it would chain its record off a tip that now
        lives inside the closed segment. Unlike the pre-existing interleaving
        race, which needs two appends to actually collide, that break is
        GUARANTEED for every process after every foreign rotation, so rotation
        must not leave it standing.

        Detection needs no lock and no extra syscall. The live log is append-only,
        so it can never shrink: a smaller size than we last wrote means the file
        was replaced. ``st_ino`` catches the same event where the filesystem
        reports it (a fresh file gets a new inode), and is skipped when either
        side reports 0 — some Windows filesystems do not supply a file index.

        The residual after this is the pre-existing one: a rotation landing
        between this stat and our append. Closing THAT means holding a
        cross-process lock across every audit write, which is the hot-path cost
        #4247 is about, so it stays measured rather than paid for here.
        """
        identity = self._live_identity()
        previous = self._live_seen
        self._live_seen = identity
        if previous is not None and _live_log_moved_on(previous, identity):
            logger.info(
                "SEL live log moved on since our last write (another process "
                "rotated or appended); re-reading the chain tip before appending"
            )
            # Bounded on the event-loop thread for the same reason as
            # _reanchor_now: only an already-corrupt multi-kilobyte tail needs
            # more than one tail chunk, and scanning it under a loop-side
            # critical audit would stall every session the loop serves.
            # Exhausting the bound raises (an OSError), failing the audit
            # closed instead of chaining from a guessed tip.
            self._last_hash = self._read_last_hash(
                max_chunks=1 if _on_event_loop() else None
            )
        return identity[2]

    def _reanchor_now(self) -> None:
        """Re-read the chain tip and the live log's identity, unconditionally.

        For the collision path, where the append guard has ALREADY proved the file
        moved on. There is nothing left to decide there, so this must not route
        through :meth:`_reanchor_if_replaced`'s predicate: an earlier revision did,
        and because that predicate reacted only to a shrink while the guard fired
        on growth too, a foreign APPEND was detected and then not corrected -- the
        retry re-chained from the same tip the collision had just invalidated and
        wrote it. Both now share one predicate, and this path skips it entirely.
        """
        self._live_seen = self._live_identity()
        # Bounded on the event-loop thread: a normal log yields the tip from a
        # single tail read, so only an already-corrupt multi-kilobyte tail could
        # walk further, and an unbounded backward scan there would stall chat and
        # the heartbeat for every session the loop serves. Exhausting the bound
        # raises (_ChainTipBeyondBound is an OSError), so a critical audit fails
        # closed instead of chaining from a guessed tip; callers off the loop
        # recover as far back as needed.
        self._last_hash = self._read_last_hash(
            max_chunks=1 if _on_event_loop() else None
        )

    def _ensure_segment_dir(self) -> bool:
        """Create the segment dir (owner-only), refusing a planted link.

        Returns whether the real directory is now usable. Rotation must NEVER
        write through a symlink/junction at this path: before this release
        ``security_events.d`` was not on the sensitive-path floor, so an agent
        could have pre-planted a link pointing somewhere it can read, and
        ``mkdir(exist_ok=True)`` succeeds on a link-to-directory. The segments
        would then land outside the fence, readable and rewritable by the audited
        agent — rotation itself becoming the way around it. Same defense, and the
        same helpers, as the SEL trust dir in
        :meth:`_load_or_create_hmac_key`; the link is removed, never its target.

        When the link cannot be removed (read-only dir) we REFUSE to rotate. The
        live log then keeps growing, which is the failure this release exists to
        bound — but an oversized log inside the fence beats a bounded one outside
        it.
        """
        if platform_compat.is_link_or_junction(self._segment_dir):
            logger.warning(
                "SEL segment dir %s is a symlink/junction — removing the link "
                "(planted before upgrade?) and creating a real directory",
                self._segment_dir,
            )
            try:
                platform_compat.unlink_link_or_junction(self._segment_dir)
            except OSError:
                logger.warning(
                    "cannot remove linked SEL segment dir %s; refusing to rotate "
                    "rather than write audit segments through the link",
                    self._segment_dir,
                    exc_info=True,
                )
                return False
        try:
            self._segment_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            logger.warning("SEL segment dir %s could not be created", self._segment_dir, exc_info=True)
            return False
        platform_compat.chmod_safe(self._segment_dir, 0o700)
        return True

    def _rotate_under_lock(self) -> None:
        """Perform one rotation. Caller holds ``_lock`` AND the rotation lock.

        The size is re-read here, not trusted from the check that opened the
        window: a sibling process may have rotated while we waited for the lock.

        The check reads the size ALREADY ON DISK, so a segment can overshoot the
        cap by at most the batch being appended (``_QUEUE_DRAIN_BATCH`` events,
        ~128 KiB against a 32 MiB cap). That keeps rotation one ``stat`` per
        batch instead of serializing every batch twice to measure it first.

        Chain semantics: the closed segment keeps its own complete chain and the
        new live log starts from genesis, so BOTH verify independently and
        retention deleting an old segment can never break a surviving one. The
        boundary is not lost — it is recorded as the new log's first entry, a
        ``sel_rotation`` event naming the closed segment and its size. It
        deliberately does NOT claim that segment's final ``entry_hash``: a hash
        captured here can be stale by the time the record is written (see
        :meth:`_rotation_event`), which would make verification report an
        untampered log as compromised. An investigator can therefore still walk
        segment to segment, and a segment that was deleted or swapped is visible
        as a rotation record whose named predecessor is absent or whose entries
        fail their own per-record HMACs.
        """
        size = self._live_size()
        if size < _SEGMENT_MAX_BYTES:
            # A sibling process rotated while we waited for the lock. Our cached
            # chain tip now lives in a closed segment, so appending from it would
            # chain this process's next batch off a record in a different file.
            # Re-anchor on the live log we are about to append to — safe to trust
            # because the caller holds the lock across that append.
            self._last_hash = self._read_last_hash()
            return
        target = self._next_segment_path()
        if target is None:
            return  # no free name; rotation deferred (see _next_segment_path)
        try:
            os.replace(self._path, target)
        except OSError:
            logger.warning(
                "SEL rotation failed; continuing to append to %s", self._path, exc_info=True
            )
            return
        logger.info("SEL rotated %s -> %s (%d bytes)", self._path, target, size)
        # Anchor on the REPLACEMENT log -- normally absent, so genesis -- and write
        # the rotation record through the guarded append rather than assuming it
        # lands first. Another process can create and append to the replacement in
        # the gap after the rename (it sees a small file, so it never enters the
        # rotation window and never takes this lock). Writing a genesis record
        # blindly would then put TWO records claiming an empty prev_hash in the new
        # log and break its chain from the second line. The guard notices the file
        # already has content and the record chains off it instead, so the rotation
        # record is the first one we could place rather than necessarily line 1.
        self._reanchor_if_replaced()
        try:
            self._append_chained_locked([self._rotation_event(target, size)])
        except OSError:
            # The boundary record is lost but nothing is corrupt: the new log simply
            # starts without one. _append_chained_locked already rolled the tip back
            # to what it was, so the next batch chains off a record that is really
            # on disk.
            logger.warning("SEL rotation record could not be written", exc_info=True)
        self._enforce_segment_retention_locked()

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

    def _next_segment_path(self) -> Path | None:
        """An unused segment path for the log being closed now, or ``None``.

        The sequence continues from the highest segment still on disk, so it
        keeps rising even after retention has deleted earlier ones — a reused
        number would sort as the OLDEST segment and make the next retention
        sweep delete the log that was just closed.

        Collision probing is CAPPED. An existing name must never be overwritten
        (audit history is not silently replaced), but the probe is a filesystem
        ``stat`` per attempt and this runs on the append path, where a critical
        audit is written inline and sometimes on the event loop. A directory
        pre-filled with consecutive segment names could otherwise make rotation
        stat its way through all of them
        (``no-blocking-call-on-event-loop``). Returning ``None`` defers rotation,
        which leaves the log over budget rather than stalling a write.
        """
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        existing = self._segments_oldest_first()
        seq = (_segment_seq(existing[-1]) + 1) if existing else 1
        for _probe in range(_SEGMENT_NAME_PROBES):
            candidate = self._segment_dir / f"security_events-{seq:06d}-{stamp}.jsonl"
            if not candidate.exists():
                return candidate
            seq += 1
        logger.warning(
            "SEL could not find a free segment name within %d probes from %06d in %s; "
            "deferring rotation (names pre-planted, or a restored backup?)",
            _SEGMENT_NAME_PROBES,
            seq - _SEGMENT_NAME_PROBES,
            self._segment_dir,
        )
        return None

    def _segments_oldest_first(self, *, pin: _SegmentDirPin | None = None) -> list[Path]:
        """Closed segments in rotation order (oldest first).

        Only files matching ``_SEGMENT_NAME_RE`` in full are returned, so an
        unrelated file an operator parked in the segment dir is never treated as
        rotation output (and so never deleted by retention). A candidate must
        ALSO be a real regular file: the name check is what protects an
        operator's file, the ``lstat`` check is the cheap first filter that
        stops a PLANTED link from being read as audit history. It judges the
        dirent at SCAN time only, so the readers re-validate at open time via
        :func:`_open_segment` — a link planted between scan and open is refused
        there rather than followed. A hardlink ALIAS is deliberately NOT
        deduplicated: every key such a decision could use is
        attacker-controlled here, so a dedupe lets a planted name DISPLACE the
        real segment (see the comment at the scan); an alias's worst case is
        repeating already-signed records, which admission gating bounds.

        The link case is reachable on an upgrade and matters on the read side,
        not the delete side: ``security_events.d`` was not on the sensitive-path
        floor before this release, so an agent could have pre-planted
        ``security_events-000001-<stamp>.jsonl`` as a symlink to any file it
        wanted surfaced -- and every reader here (``recent``, which backs the
        dashboard's SEL events endpoint, and ``verify_integrity``) would parse
        the LINKED file's lines and hand back whatever JSON objects it found as
        events. Retention unlinking such an entry would only remove the link,
        but the read is a real disclosure, so a non-regular entry is excluded.

        Ordering is by the name's numeric sequence — see ``_SEGMENT_NAME_RE``
        for why that rather than the timestamp or the mtime.

        Enumeration is BOUNDED at ``_SEGMENT_SCAN_CAP`` entries. Rotation keeps
        at most ``_SEGMENT_KEEP + 1`` files here, so any larger count is either an
        operator's dumping ground or entries planted before this release, and this
        walk runs on the append path — where a critical audit is written inline,
        sometimes on the event loop. An unbounded listing of a directory someone
        else filled would stall that caller
        (``no-blocking-call-on-event-loop``), so the scan stops at the cap and
        works with what it has: rotation then simply does not find the segments
        beyond it, which leaves the log over budget rather than blocking a write.

        *pin* is the read-side directory pin (#4999). The walk itself stays
        the same bounded, BY-NAME scan on every platform — the cap above is
        the memory bound, and materializing an unbounded listing first (as an
        fd-relative ``os.listdir`` would) would spend unbounded memory just to
        refuse it afterwards. The pin closes the redirect the name allows:
        after the walk, the path must still name the directory the read
        pinned (any swap — planted link, or a different real directory, whose
        identity also differs — fails closed to no segments), and on
        descriptor platforms the per-file opens that follow resolve RELATIVE
        to the pinned descriptor, so a swap after the revalidation still
        cannot redirect a read. Rotation calls this UNPINNED: it repairs the
        directory itself first (:meth:`_ensure_segment_dir`), and its callers
        are not read paths.
        """
        if pin is not None and pin.fd is not None:
            # fd-relative walk: a PATH swap can neither redirect nor empty
            # this scan. That closes the ABA shape — swap a decoy in DURING
            # the walk, restore the real directory before any recheck — in
            # which a by-name scan enumerates the decoy and then sees the
            # restored identity pass. os.scandir(fd) stays lazy, so the cap
            # below keeps bounding the walk on a legacy directory pre-filled
            # before the fence.
            try:
                scanner = os.scandir(pin.fd)
            except OSError:
                logger.warning(
                    "SEL could not enumerate the pinned segment dir", exc_info=True
                )
                return []
        else:
            try:
                scanner = os.scandir(self._segment_dir)
            except OSError:
                return []
        # A hardlink ALIAS is deliberately NOT handled here (or anywhere):
        # an alias IS the regular file its name points at, so telling it from
        # the original requires a cross-name decision — and every key
        # available for that decision (which name sorts first, which survives)
        # is attacker-controlled in this directory. A dedupe keyed on names
        # lets a planted alias DISPLACE the real segment and corrupt read
        # chronology, which is strictly worse than what an alias can do on its
        # own: duplicate already-signed records in bounded reads. That
        # duplication is admission-gated (_read_sources_newest_first requires
        # a record signed with the out-of-tree key) and cannot forge history.
        named: list[Path] = []
        examined = 0
        truncated = False
        with scanner:
            for entry in scanner:
                examined += 1
                if examined > _SEGMENT_SCAN_CAP:
                    truncated = True
                    break
                if not _SEGMENT_NAME_RE.fullmatch(entry.name):
                    continue
                try:
                    # follow_symlinks=False, so a symlink is judged as a symlink
                    # rather than as whatever it points at.
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    logger.warning(
                        "SEL ignoring non-regular entry %s in the segment dir "
                        "(planted link?); it is not audit history",
                        entry.name,
                    )
                    continue
                named.append(self._segment_dir / entry.name)
        if truncated:
            logger.warning(
                "SEL segment dir %s holds more than %d entries; scan stopped there. "
                "Rotation keeps at most %d, so the extra entries were not created by "
                "it — retention will not see past the cap until they are removed",
                self._segment_dir,
                _SEGMENT_SCAN_CAP,
                _SEGMENT_KEEP + 1,
            )
        if pin is not None and pin.fd is None and not pin.matches(self._segment_dir):
            # Identity pin (this platform has no directory descriptors): the
            # walk above was by NAME, so a directory swapped in mid-walk
            # would have been followed. The identity the read pinned and the
            # identity the path names NOW differ, so fail closed rather than
            # return entries from a tree the pin never covered.
            logger.warning(
                "SEL segment dir %s was replaced during enumeration (planted "
                "link?); its entries are not audit history",
                self._segment_dir,
            )
            return []
        return sorted(named, key=_segment_seq)

    def _enforce_segment_retention_locked(self) -> int:
        """Delete the oldest closed segments beyond ``_SEGMENT_KEEP``.

        Returns the number deleted. Caller holds ``_lock``.

        STOPS at the first segment it cannot unlink rather than skipping past it.
        Skipping would delete the NEXT-oldest instead — trading newer audit
        history for older history that is stuck occupying a retention slot, so a
        permission problem on one file would quietly eat the evidence behind it.
        Stopping leaves the count over budget until the obstruction clears, which
        is the safe direction, and the next rotation retries. Matches
        :meth:`_prune_segments_locked`, which already stops for the same reason.
        """
        segments = self._segments_oldest_first()
        excess = len(segments) - _SEGMENT_KEEP
        if excess <= 0:
            return 0
        deleted = 0
        for path in segments[:excess]:
            try:
                path.unlink()
            except OSError:
                logger.warning(
                    "SEL segment retention could not delete %s; stopping this sweep "
                    "rather than deleting newer segments behind it",
                    path,
                    exc_info=True,
                )
                break
            deleted += 1
        if deleted:
            logger.info(
                "SEL retention deleted %d closed segment(s) beyond the %d kept",
                deleted,
                _SEGMENT_KEEP,
            )
        return deleted

    def _ends_without_newline(self) -> bool:
        """True if the log's final byte is not a newline.

        A trailing byte other than ``\\n`` means the previous append was
        truncated (crash / partial write) and left an unterminated fragment.
        The writer uses this to insert a newline boundary before the next
        record so the new line stays independently parseable (see
        _flush_batch). Fail-soft: on any read error assume no separator is
        needed rather than crashing the writer.
        """
        try:
            with open(self._path, "rb") as f:
                f.seek(0, 2)
                if f.tell() == 0:
                    return False
                f.seek(-1, 2)
                return f.read(1) != b"\n"
        except OSError:
            return False

    def _read_last_hash(
        self, path: Path | None = None, *, max_chunks: int | None = None
    ) -> str:
        """Return the entry_hash of the last COMPLETE record, or "" if none.

        Reads the live log by default; *path* names a closed segment instead
        (rotation reads the tip of the file it just renamed, which is immutable
        and therefore race-free).

        A crash mid-append can leave a truncated/partial final line. The old
        implementation wrapped the parse in a blanket ``except: return ""``, so
        one corrupt tail line silently restarted the HMAC chain from genesis —
        severing the tamper-evidence link and masking exactly the corruption
        the chain exists to detect. Instead we scan backward and skip an
        unparseable trailing line to chain from the last COMPLETE valid record;
        we only return "" when the log is genuinely empty/absent or contains no
        parseable record at all (nothing to chain from). Skipped corrupt tail
        lines are logged so the integrity concern is surfaced, not hidden.
        """
        target = path or self._path
        if not target.exists():
            return ""
        try:
            with open(target, "rb") as f:
                f.seek(0, 2)
                pos = f.tell()
                if pos == 0:
                    return ""
                # Scan backward in chunks. ``buf`` holds bytes not yet split
                # into complete lines; while pos > 0 its first split element is
                # a possibly-partial line whose start lies in an earlier chunk,
                # so we hold it back until more is read (or we reach the file
                # start). This lets us walk past a truncated tail line to find
                # the last complete record.
                buf = b""
                skipped_corrupt = False
                chunks_read = 0
                while pos > 0:
                    # ``max_chunks`` bounds how far back the scan may walk. The
                    # append path passes 1 on the event-loop thread: a normal log
                    # yields the tip from a single tail read, so only an already
                    # corrupt multi-kilobyte tail could walk further, and doing
                    # that on the loop would stall chat and the heartbeat.
                    # Exhausting the bound raises rather than returning a guessed
                    # tip, so the caller fails closed instead of chaining from the
                    # wrong record. Callers off the loop pass None and recover as
                    # far back as needed.
                    if max_chunks is not None and chunks_read >= max_chunks:
                        raise _ChainTipBeyondBound(
                            "SEL chain tip lies beyond the bounded tail read "
                            "(corrupt tail); refusing to scan further here"
                        )
                    read_start = max(pos - 4096, 0)
                    f.seek(read_start)
                    buf = f.read(pos - read_start) + buf
                    pos = read_start
                    chunks_read += 1
                    parts = buf.split(b"\n")
                    if pos > 0:
                        # First element may be incomplete — defer it.
                        buf = parts[0]
                        complete = parts[1:]
                    else:
                        # Reached the file start: every element is complete.
                        buf = b""
                        complete = parts
                    for line in reversed(complete):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except ValueError:
                            # Truncated/corrupt line — skip backward to the last
                            # complete record rather than resetting the chain to
                            # genesis. Flag it so the corruption is not hidden.
                            skipped_corrupt = True
                            logger.warning(
                                "SEL: skipping unparseable audit-log line while "
                                "resolving chain tip in %s; chaining from the last "
                                "complete record instead of resetting to genesis",
                                target,
                            )
                            continue
                        if not isinstance(data, dict):
                            # Parseable JSON but not a record object — treat as
                            # corrupt and keep scanning backward.
                            skipped_corrupt = True
                            logger.warning(
                                "SEL: skipping non-object audit-log line while "
                                "resolving chain tip in %s",
                                target,
                            )
                            continue
                        if skipped_corrupt:
                            logger.warning(
                                "SEL: recovered chain tip from an earlier complete "
                                "record after a corrupt/truncated tail in %s",
                                target,
                            )
                        return data.get("entry_hash", "")
            # No parseable record anywhere in the file — nothing to chain from.
            return ""
        except _ChainTipBeyondBound:
            # Not a read failure: the bound was deliberately hit. Propagate so
            # the caller fails closed rather than chaining from genesis.
            raise
        except OSError:
            logger.warning(
                "SEL: failed to read chain tip from %s", target, exc_info=True
            )
            return ""

    def _compute_hash(self, event: SecurityEvent) -> str:
        # Hash over all fields except entry_hash itself
        d = asdict(event)
        d.pop("entry_hash", None)
        payload = json.dumps(d, sort_keys=True).encode()
        return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()

    def log(self, event: SecurityEvent, *, critical: bool = False) -> None:
        """Enqueue an event for the background writer (non-blocking).

        The HMAC chain (prev_hash/entry_hash) is computed in the writer thread
        in enqueue order, so callers never pay the hash + file-append cost on
        the hot path. If the writer can't be started (unexpected), fall back to
        a synchronous write so an event is never silently dropped.

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
        try:
            self._ensure_writer()
            with self._pending_cond:
                self._pending += 1
            self._queue.put(event)
        except Exception:
            # Writer unavailable — write synchronously so the audit entry lands.
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
    ) -> None:
        """Convenience: log a dashboard/API access event.

        Pass ``critical=True`` for fail-closed audits (e.g. safety-override
        activation): the event is written synchronously and a filesystem
        failure is re-raised so the caller can refuse the audited action.
        """
        self.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="api_access",
                caller_identity=caller,
                agent="",
                source=source,
                operation=operation,
                outcome=outcome,
                resources=_redact_and_clip(resources) if resources else "",
                error=_redact_and_clip(error) if error else "",
            ),
            critical=critical,
        )

    @overload
    def verify_integrity(self) -> tuple[int, int]: ...

    @overload
    def verify_integrity(self, *, detailed: Literal[True]) -> SelVerification: ...

    def verify_integrity(self, *, detailed: bool = False) -> tuple[int, int] | SelVerification:
        """Verify the HMAC chains. Returns (total_entries, valid_entries).

        Every rotated segment (oldest first) is verified BEFORE the live log.
        Each file is an independent chain that starts from genesis, so a segment
        deleted by retention cannot make a surviving one look tampered — which is
        the property rotation had to preserve.

        There is deliberately no cross-file assertion. Forgery is caught per
        RECORD: a planted or rewritten segment cannot produce a valid HMAC because
        the key lives outside the log directory, so its entries are counted invalid
        here regardless of what any boundary record says. An earlier revision also
        checked a predecessor-hash claim carried in the rotation record; see
        :meth:`_rotation_event` for why that claim cannot be made true without
        serializing every append, and why a check that can fire on a benign cause
        is worse than no check.

        Segments are enumerated under a read-side directory pin (#4999): the
        segment dir that refused to pin (a planted link, or not a directory)
        contributes NOTHING here rather than being walked by name — which is
        what makes a swapped ``security_events.d`` fail closed instead of
        inflating ``total`` with another tree's files (a false tamper alarm).

        ``detailed=True`` adds the third outcome that refusal needs (#5051
        review): ``history_verifiable=False`` with a ``reason`` when the
        directory refused to pin or was replaced mid-verification, because
        the rotated segments were not checked and "intact over the live log
        alone" must not be able to hide that. A directory that simply does
        not exist yet stays verifiable — a fresh install has no history to
        vouch for.
        """
        self.flush()  # ensure all queued events are on disk before verifying
        total = 0
        valid = 0
        pin, absent = _open_segment_dir(self._segment_dir)
        try:
            # Enumerate INSIDE the try: a mid-readdir I/O error must still
            # unwind through the finally that releases the pin, not strand
            # the descriptor on every failed request.
            segments = self._segments_oldest_first(pin=pin) if pin is not None else []
            for path in segments + [self._path]:
                if pin is None or pin.fd is None:
                    # Pathname walks may see a segment retention-deleted
                    # between enumeration and here; that is benign. An
                    # fd-pinned walk must not re-touch the PATH at all — a
                    # swap racing the recheck would skip real segments here
                    # and the fd-relative open below handles absence itself.
                    if not path.exists():
                        continue
                file_total, file_valid = self._verify_file(path, pin=pin)
                total += file_total
                valid += file_valid
        finally:
            if pin is not None:
                pin.close()
        if not detailed:
            return total, valid
        reason = ""
        if pin is None:
            if not absent:
                # absent is what the PIN itself observed, so a concurrent
                # repair removing a refused link cannot reclassify the
                # refusal as benign absence after the fact.
                reason = "segment directory refused to pin (planted link?)"
        elif not pin.matches(self._segment_dir):
            # matches() is lstat-based, so it still answers after close();
            # a mid-verification swap leaves the totals computed from the
            # pinned directory but the tree on disk no longer is it.
            reason = "segment directory was replaced during verification"
        return SelVerification(
            total=total, valid=valid, history_verifiable=not reason, reason=reason
        )

    @overload
    def _reader_handle(
        self, path: Path, *, binary: Literal[True], pin: _SegmentDirPin | None = ...
    ) -> IO[bytes] | None: ...

    @overload
    def _reader_handle(
        self, path: Path, *, binary: Literal[False], pin: _SegmentDirPin | None = ...
    ) -> IO[str] | None: ...

    def _reader_handle(
        self, path: Path, *, binary: bool, pin: _SegmentDirPin | None = None
    ) -> IO[str] | IO[bytes] | None:
        """Reader-side open policy, shared by both readers.

        The LIVE log opens with ordinary ``open()``: it is a fixed
        module-owned path, never an enumerated name, and its WRITER follows
        an operator's symlink (``O_CREAT | O_APPEND``), so a reader that
        refuses one turns the live log write-only — events keep landing
        while every read reports empty and ``verify_integrity`` counts a
        clean ``(0, 0)``. Rotated segments ARE enumerated, attacker-nameable
        entries, and take the descriptor-validating funnel
        (:func:`_open_segment`), which resolves them relative to *pin* when
        the read holds one (#4999) — a segment dir swapped after the pin
        cannot redirect the open.
        """
        if path == self._path:
            try:
                if binary:
                    return open(path, "rb")
                return open(path, encoding="utf-8")
            except OSError:
                logger.warning("SEL could not open the live log %s", path.name, exc_info=True)
                return None
        fd = _open_segment(path, pin=pin)
        if fd is None:
            return None
        if binary:
            return os.fdopen(fd, "rb")
        return os.fdopen(fd, encoding="utf-8")

    def _verify_file(self, path: Path, *, pin: _SegmentDirPin | None = None) -> tuple[int, int]:
        """Verify one log file's chain. Returns (total, valid).

        Streamed line by line so a capped-but-large segment is never loaded whole.
        The handle comes from :meth:`_reader_handle`, so a symlink or FIFO
        planted under a segment name is refused here rather than followed; a
        refused file is not audit history and counts as (0, 0).
        """
        total = 0
        valid = 0
        prev_hash = ""
        handle = self._reader_handle(path, binary=False, pin=pin)
        if handle is None:
            return 0, 0
        with handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                total += 1
                try:
                    data = json.loads(line)
                    stored_hash = data.pop("entry_hash", "")
                    if data.get("prev_hash", "") != prev_hash:
                        logger.warning("SEL chain break at entry %d of %s", total, path.name)
                        prev_hash = stored_hash
                        continue
                    # Re-attach the hash: _record_signature_matches owns the one
                    # payload/compare implementation both readers use.
                    if self._record_signature_matches({**data, "entry_hash": stored_hash}):
                        valid += 1
                    else:
                        logger.warning("SEL HMAC mismatch at entry %d of %s", total, path.name)
                    prev_hash = stored_hash
                except (json.JSONDecodeError, Exception):
                    logger.warning("SEL parse error at entry %d of %s", total, path.name)
        return total, valid

    def recent(
        self,
        limit: int = 100,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """Return the most recent events, newest first.

        ``since`` / ``until`` bound the window (inclusive ``since``, exclusive
        ``until``) so a caller can ask "what happened in the last two hours"
        instead of guessing a count large enough to reach back that far.

        The read is bounded on BOTH ends. Files are scanned backward from the
        tail in chunks — never loaded whole, which is what made a large log
        impractical to query — and the walk stops at the first record older than
        ``since`` because the log is append-ordered. Rotated segments are only
        opened when the live log has not already satisfied the request.

        Records whose timestamp cannot be parsed are returned when no window was
        asked for, and skipped when one was: a record that cannot be placed in
        time cannot be asserted to fall inside the window.
        """
        self.flush()  # surface any queued-but-unwritten events
        result: list[dict] = []
        if limit <= 0:
            return result
        windowed = since is not None or until is not None
        # Newest first: the live log, then rotated segments newest to oldest.
        # Segments are discovered LAZILY (only if the live log has not already
        # satisfied the request), so the common tail read touches one file.
        # The segment dir is PINNED for the whole walk (#4999) so a directory
        # swapped mid-read cannot redirect later opens; the early returns below
        # all unwind through the finally that releases the pin.
        pin, _absent = _open_segment_dir(self._segment_dir)
        try:
            for path in self._read_sources_newest_first(pin=pin):
                if pin is None or pin.fd is None:
                    if not path.exists():
                        continue
                for line in self._iter_lines_backward(path, pin=pin):
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if windowed:
                        ts = _parse_timestamp(data.get("timestamp", ""))
                        if ts is None:
                            continue
                        if until is not None and ts >= until:
                            continue
                        if since is not None and ts < since:
                            # Append-ordered log: everything from here back is older.
                            return result
                    result.append(data)
                    if len(result) >= limit:
                        return result
        finally:
            if pin is not None:
                pin.close()
        return result

    def _read_sources_newest_first(self, *, pin: _SegmentDirPin | None) -> Iterator[Path]:
        """The live log, then ADMISSIBLE segments newest to oldest.

        A segment is only handed to the read path once one of its records
        verifies under our HMAC key. ``security_events.d`` is created by this
        release, and it was not on the sensitive-path floor before it, so an agent
        could have pre-created the directory and left a segment-shaped JSONL of
        its own choosing in it; without this gate the upgrade would then present
        those forged records to every reader as audit events. Forging is what the
        chain key exists to prevent and the key lives outside the log directory,
        so a planted segment cannot produce a single valid signature.

        The check is ONE record per segment (the last, reached through the same
        backward reader), not the whole file: full-segment verification would undo
        the bounded read this release exists to provide, and one valid signature
        is already unforgeable. ``verify_integrity`` deliberately does NOT filter
        this way — it reports a rejected segment's entries as invalid instead of
        hiding them, because an audit tool must surface tampering rather than
        quietly drop the evidence.

        A generator so segments are neither listed nor opened when the live log
        already answered the caller.

        *pin* is the caller's read-side directory pin (#4999), owned and
        released by the caller. ``None`` means the directory refused to pin —
        a planted link, or not a directory — and the response is to offer NO
        segment sources rather than fall back to a by-name walk, which is
        exactly what a swapped directory exploits.
        """
        yield self._path
        if pin is None:
            # _open_segment_dir already logged the refusal at the severity
            # the cause deserves (a planted link warns; a directory that
            # simply does not exist yet — a fresh install — stays quiet).
            logger.debug("SEL offering no segment sources: the segment dir did not pin")
            return
        for path in reversed(self._segments_oldest_first(pin=pin)):
            if self._segment_is_signed_by_us(path, pin=pin):
                yield path
            else:
                logger.warning(
                    "SEL refusing to read %s as audit history: no record in it is "
                    "signed with this log's key (planted before upgrade?)",
                    path.name,
                )

    def _segment_is_signed_by_us(self, path: Path, *, pin: _SegmentDirPin | None = None) -> bool:
        """Whether *path* holds at least one record signed with our HMAC key."""
        for line in self._iter_lines_backward(path, pin=pin):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and self._record_signature_matches(data):
                return True
        return False

    def _record_signature_matches(self, record: dict) -> bool:
        """Whether *record*'s ``entry_hash`` is our HMAC over its other fields."""
        data = dict(record)
        stored = data.pop("entry_hash", "")
        if not isinstance(stored, str) or not stored:
            return False
        try:
            payload = json.dumps(data, sort_keys=True).encode()
        except (TypeError, ValueError):
            return False
        expected = hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(stored, expected)

    def _iter_lines_backward(
        self, path: Path, *, pin: _SegmentDirPin | None = None
    ) -> Iterator[str]:
        """Yield *path*'s non-empty lines from last to first, reading in chunks.

        Same backward scan as :meth:`_read_last_hash`: only the tail is touched
        unless the caller keeps consuming, so reading 20 entries out of a
        size-capped segment costs one chunk rather than the whole file.

        The pending buffer is CAPPED at ``_MAX_LINE_BYTES``. Held bytes are
        whatever has not yet been split into a complete line, so a file with no
        newline in it would otherwise accumulate to its full size in memory --
        and this reader is pointed at attacker-influenced input: a segment planted
        before this release (when the directory was not fenced) is read here both
        by the time-range read and by segment admission. A single planted
        gigabyte-long unterminated line would exhaust the process. Hitting the cap
        ends the scan, so a caller sees the lines it already got and admission
        simply fails to find a signed record -- which is the safe direction.

        The handle comes from :meth:`_reader_handle`, so a symlink or FIFO
        planted under a segment name yields nothing here instead of being
        followed (or, for a FIFO, blocking the reader inside ``open``); the
        live log itself opens ordinarily, matching its writer. A read holding
        a directory pin resolves segments relative to it (#4999).
        """
        handle = self._reader_handle(path, binary=True, pin=pin)
        if handle is None:
            return
        try:
            with handle as f:
                f.seek(0, 2)
                pos = f.tell()
                buf = b""
                while pos > 0:
                    read_start = max(pos - _TAIL_CHUNK_BYTES, 0)
                    f.seek(read_start)
                    buf = f.read(pos - read_start) + buf
                    pos = read_start
                    parts = buf.split(b"\n")
                    if pos > 0:
                        # First element may be incomplete — defer it until the
                        # next chunk supplies its start.
                        buf = parts[0]
                        complete = parts[1:]
                    else:
                        buf = b""
                        complete = parts
                    for raw in reversed(complete):
                        stripped = raw.strip()
                        if stripped:
                            yield stripped.decode("utf-8", errors="replace")
                    if len(buf) > _MAX_LINE_BYTES:
                        logger.warning(
                            "SEL stopping the backward scan of %s: over %d bytes with no "
                            "line boundary (truncated or planted file?)",
                            path.name,
                            _MAX_LINE_BYTES,
                        )
                        return
        except OSError:
            logger.warning("SEL could not read %s", path, exc_info=True)

    def prune(self, keep_days: int = _RETENTION_DAYS) -> int:
        """Remove entries older than keep_days. Returns count removed.

        Streams the log line-by-line to bound memory usage, writes survivors
        to a temp file, then atomically replaces the original. The append lock
        is held across the whole read+replace critical section so a concurrent
        append cannot land in the old file after the read pass and be lost by
        the replace: appends either complete before the read (and are copied)
        or block until after the replace (and land in the new file). Both the
        cross-process chain lock and the thread lock are held, in that order —
        a writer in ANOTHER process is ordered only by the former, and taking it
        first is what keeps a cross-process wait off the event loop. Appends
        run on the background writer thread, so blocking them for the prune
        duration never touches the event loop.

        The chain lock orders sibling APPENDS; rotation by a sibling is ordered
        by the cross-process ROTATION lock (the same one rotation takes), which
        the live sweep additionally holds via :meth:`_prune_live_locked`. The
        read-then-replace below is the one window where an unserialized sibling
        is destructive rather than merely untidy: an append or rotation landing
        in the old file after the read pass would be dropped by the replace —
        the only path in this class that can lose already-persisted audit
        events. Unlike rotation, prune WAITS for the rotation lock instead of
        skipping. Rotation is deferrable and runs on the audit hot path; prune
        is a once-a-day sweep on the maintenance executor, never on the event
        loop, and skipping it would postpone retention for a whole day. When
        the lock cannot be taken at all the live sweep is skipped and reported,
        because doing it unserialized is what loses events.

        Rotated segments are aged out WHOLE rather than rewritten: a segment
        whose newest record is past the cutoff is deleted outright, which keeps
        every surviving segment's chain intact (rewriting one would orphan its
        first record's ``prev_hash``). Their entries are included in the
        returned count.
        """
        self.flush()  # don't rewrite the file out from under queued appends
        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)

        removed = 0
        with self._chain_lock(kind="prune"), self._lock:
            removed += self._prune_segments_locked(cutoff_dt)
            if not self._path.exists():
                return removed
            lock_fh = self._open_rotation_lock() if self._ensure_segment_dir() else None
            if lock_fh is None:
                logger.warning(
                    "SEL prune could not take the rotation lock; skipping the live-log "
                    "sweep rather than risking a concurrent rotation's events"
                )
                return removed
            try:
                with platform_compat.file_lock(lock_fh.fileno(), exclusive=True):
                    removed += self._prune_live_locked(cutoff_dt, keep_days)
            except OSError:
                logger.warning(
                    "SEL prune could not serialize the live-log sweep; skipped",
                    exc_info=True,
                )
            finally:
                lock_fh.close()
        return removed

    def _prune_live_locked(self, cutoff_dt: datetime, keep_days: int) -> int:
        """Rewrite the live log without its aged-out entries. Returns count removed.

        Caller holds ``_lock`` AND the cross-process rotation lock, so no sibling
        can rotate the file out from under the read-then-replace below.
        """
        cutoff_str = cutoff_dt.isoformat()
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".sel_prune_", suffix=".tmp"
        )
        live_removed = 0
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
                with open(self._path, encoding="utf-8") as src_f:
                    for raw_line in src_f:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if data.get("timestamp", "") < cutoff_str:
                                live_removed += 1
                                continue
                        except json.JSONDecodeError:
                            live_removed += 1
                            continue
                        tmp_f.write(line)
                        tmp_f.write("\n")

            if live_removed:
                os.replace(tmp_path, self._path)
                # The replace gives the live log a NEW inode and a smaller size, so
                # re-anchor both the tip and the identity we compare against.
                # Leaving the identity stale would make the next append see a
                # foreign change and re-chain needlessly.
                self._reanchor_now()
                logger.info(
                    "SEL pruned %d entries older than %d days", live_removed, keep_days
                )
            else:
                os.unlink(tmp_path)
        except BaseException:
            # Clean up temp file on any failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return live_removed

    def _prune_segments_locked(self, cutoff: datetime) -> int:
        """Delete whole rotated segments whose NEWEST record predates *cutoff*.

        Returns the number of entries dropped with them. Caller holds ``_lock``.
        A segment is deleted only when its last record is past the cutoff, so a
        segment straddling the boundary is kept in full rather than rewritten:
        rewriting it would orphan its first record's ``prev_hash`` and turn a
        retention sweep into an apparent chain break. Best-effort — a segment
        that cannot be read or unlinked is logged and left alone.
        """
        removed = 0
        for path in self._segments_oldest_first():
            newest = self._newest_timestamp(path)
            if newest is None or newest >= cutoff:
                # Unreadable/undatable, or still within retention. Segments are
                # chronological, so the first keeper ends the sweep.
                break
            count = sum(1 for _ in self._iter_lines_backward(path))
            try:
                path.unlink()
            except OSError:
                logger.warning("SEL could not delete aged-out segment %s", path, exc_info=True)
                break
            removed += count
            logger.info("SEL deleted aged-out segment %s (%d entries)", path.name, count)
        return removed

    def _newest_timestamp(self, path: Path) -> datetime | None:
        """Timestamp of *path*'s newest parseable record, or ``None``."""
        for line in self._iter_lines_backward(path):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            parsed = _parse_timestamp(data.get("timestamp", ""))
            if parsed is not None:
                return parsed
        return None


@contextmanager
def _no_rotation() -> Iterator[None]:
    """A do-nothing stand-in for the rotation window (see ``_flush_batch``)."""
    yield


def _segment_seq(path: Path) -> int:
    """Rotation sequence encoded in a segment's name (0 if it has none).

    Sort key for :meth:`SecurityEventLog._segments_oldest_first`; callers filter
    non-segment names first, so the fallback only guards a caller that does not.
    """
    match = _SEGMENT_NAME_RE.fullmatch(path.name)
    return int(match.group("seq")) if match else 0


def _open_segment(path: Path, *, pin: _SegmentDirPin | None = None) -> int | None:
    """Open *path* read-only as a validated ROTATED SEGMENT, or ``None``.

    The dirent check in :meth:`SecurityEventLog._segments_oldest_first` judges
    what a directory entry WAS when scanned; this opener judges what the name
    IS at open time, closing the scan->open window in which a substitute can
    be planted (the read-side TOCTOU). Segments are the enumerated,
    attacker-nameable surface; the LIVE log deliberately does not come here —
    its writer follows an operator's symlink, so its readers must too
    (:meth:`SecurityEventLog._reader_handle` owns that split).

    With a *pin* (#4999) the final DIRECTORY hop is pinned too: the open (and
    the identity check below) resolve RELATIVE to the pinned descriptor where
    the platform has directory descriptors, so a ``security_events.d`` swapped
    after the pin cannot redirect the open into another tree; where it does
    not, the pin's directory identity is revalidated against the path first,
    refusing a swapped parent mid-read. The per-file funnel below is otherwise
    unchanged.

    Three layers, each covering what the previous one cannot:

    - ``O_NOFOLLOW`` fails a planted symlink at the open itself where the
      platform has it (``ELOOP`` on Linux/macOS; some BSDs say ``EMLINK`` or
      ``EFTYPE``). ``O_NONBLOCK`` is load-bearing for a planted FIFO: without
      it the read-side ``os.open`` blocks until a writer appears, before any
      descriptor-level check is reachable; it has no effect on regular files.
      Both degrade to 0 via ``getattr`` on Windows, and ``O_BINARY`` keeps the
      descriptor out of Windows text mode there.
    - ``fstat`` on the DESCRIPTOR — which nothing can swap afterwards —
      requires a regular file, refusing FIFOs, directories, and device nodes
      on every platform.
    - An identity check makes the flag degradation safe: the opened file must
      be exactly the file *path* names right now (``lstat`` dev/ino equal to
      the descriptor's). A symlink's own ``lstat`` identity never equals its
      target's, so a symlink swap is refused even where ``O_NOFOLLOW`` does
      not exist, and a mid-swap mismatch fails closed. (On a filesystem that
      reports ``st_ino == 0`` for everything, the comparison degrades to the
      flag and type checks; such filesystems do not support symlinks.)

    A HARDLINK is deliberately not judged here: a hardlink IS the regular
    file its name points at, indistinguishable at the per-file level, and a
    link count above one is routinely legitimate (``rsync --link-dest`` and
    ``cp -al`` backups). Nor is a planted second name deduplicated anywhere
    else — every key such a decision could use is attacker-controlled, so a
    dedupe would let a planted alias DISPLACE the real segment (see the scan
    comment in :meth:`SecurityEventLog._segments_oldest_first`). An alias's
    bounded worst case is repeating already-signed records.

    Returns a descriptor at position 0, ready for ``os.fdopen``; ownership
    passes to the caller. A refusal warns in the same "planted link?" style as
    the dirent filter, so the two layers read consistently in the log.
    """
    rel_fd = pin.fd if pin is not None else None
    if pin is not None and rel_fd is None and not pin.matches(path.parent):
        # Identity pin (no directory descriptors on this platform): the parent
        # no longer names the directory the read pinned, so the path may walk
        # through a swapped-in link. Fail closed.
        logger.warning(
            "SEL refusing audit file %s: the segment dir was replaced mid-read "
            "(planted link?); it is not audit history",
            path.name,
        )
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        if rel_fd is not None:
            fd = os.open(path.name, flags, dir_fd=rel_fd)
        else:
            fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (
            errno.ELOOP,
            getattr(errno, "EMLINK", -1),
            getattr(errno, "EFTYPE", -1),
        ):
            logger.warning(
                "SEL refusing audit file %s: it is a symlink (planted link?); "
                "it is not audit history",
                path.name,
            )
        elif exc.errno == errno.ENOENT:
            # Deleted by retention between the caller's exists() check and
            # this open — a normal race, not an incident.
            logger.debug("SEL audit file %s vanished before open", path.name)
        else:
            logger.warning("SEL could not open audit file %s", path.name, exc_info=True)
        return None
    try:
        opened = os.fstat(fd)
        if rel_fd is not None:
            named = os.stat(path.name, dir_fd=rel_fd, follow_symlinks=False)
        else:
            named = os.lstat(path)
    except OSError:
        os.close(fd)
        logger.warning("SEL could not stat opened audit file %s", path.name, exc_info=True)
        return None
    if not stat.S_ISREG(opened.st_mode):
        os.close(fd)
        logger.warning(
            "SEL ignoring non-regular audit file %s (planted link?); "
            "it is not audit history",
            path.name,
        )
        return None
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        logger.warning(
            "SEL refusing audit file %s: it is not the file its name points "
            "at (planted link?); it is not audit history",
            path.name,
        )
        return None
    return fd


#: Whether this platform can pin a directory BY DESCRIPTOR: ``O_DIRECTORY``
#: plus ``dir_fd``-relative open and no-follow stat. Where those hold,
#: fd-taking ``os.listdir`` holds too (same fdopendir capability), so it is
#: not probed separately. The POSIX branch of the read-side pin below is
#: built on this; Windows has none of it, so its pin carries identity
#: revalidation instead (see ``_SegmentDirPin``).
# supports_dir_fd holds FUNCTION OBJECTS — a string membership test is always
# False, which would silently strand every platform on the identity pin. And
# the no-follow lstat flavor is NOT a member even where it works: its
# dir_fd-relative spelling is stat(follow_symlinks=False), so THAT is what
# both the gate and the call sites use.
_PIN_BY_FD_SUPPORTED = (
    hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
)


@dataclass
class _SegmentDirPin:
    """A read-side pin on the segment directory (#4999).

    ``fd`` is the strong form — an open directory descriptor nothing can swap
    afterwards — and every per-file OPEN held by the read resolves RELATIVE
    to it, immune to a later swap of the path. On every platform the pin also
    carries the directory's ``identity`` (the fd's ``fstat`` where there is
    one, the ``lstat`` otherwise), revalidated after enumeration and before
    each child open, so a directory swapped for a link — or for a different
    real directory, whose identity also differs — is refused mid-read. The
    residual is the gap between one revalidation and the next resolution
    through the name, which on fd platforms the descriptor-relative opens
    close and elsewhere the rotation-time repair (``_ensure_segment_dir``)
    bounds; the same ``st_ino == 0`` degradation note as
    :func:`_open_segment` applies.
    """

    fd: int | None
    identity: tuple[int, int]

    def close(self) -> None:
        """Release the descriptor, if this pin holds one."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def matches(self, path: Path) -> bool:
        """Whether *path* still names the pinned directory (False on any error)."""
        try:
            current = os.lstat(path)
        except OSError:
            return False
        return (current.st_dev, current.st_ino) == self.identity


def _open_segment_dir(path: Path) -> tuple[_SegmentDirPin | None, bool]:
    """Pin the segment DIRECTORY a read is about to walk (#4999).

    The directory-level analog of :func:`_open_segment`: that function pins
    the final component, this one pins the hop above it. Without it, a
    ``security_events.d`` replaced with a link (planted before this release,
    or swapped while a read is in flight) redirected enumeration AND every
    per-file open into another tree — whose segment-shaped regular files pass
    every per-file check, because they are regular files.

    Returns ``(pin, absent)``. ``pin is None`` is a REFUSAL, not a fallback:
    callers treat it as "no segments are readable" and fail closed, because
    walking the path anyway is exactly what a swapped directory exploits.
    *absent* is CONFIRMED absence (ENOENT) as the pin itself observed it —
    the one benign shape, a fresh install, which yields the same empty
    outcome the unpinned scan always produced. A caller reporting on the
    read must keep THAT classification instead of re-stating the path: a
    concurrent repair can remove a refused link before anyone looks again,
    and the refusal would silently reclassify as absence (#5051 review).
    Judged, in the same three-layer spirit:

    - ``lstat`` + ``is_link_or_junction`` (junction-aware on Windows) refuses
      a linked directory outright, and ``S_ISDIR`` refuses a non-directory.
    - On a descriptor-capable platform the directory is then opened with
      ``O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK``, so a link swapped in after
      the ``lstat`` fails the open itself.
    - The descriptor's ``fstat`` identity must equal what the name's ``lstat``
      said — the same mid-swap mismatch refusal as the per-file funnel.
    """
    try:
        named = os.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            # No segment dir yet: nothing was ever rotated, which the callers
            # already treat as "no segments".
            logger.debug("SEL segment dir %s does not exist yet", path)
            return None, True
        logger.warning("SEL could not stat the segment dir %s", path, exc_info=True)
        return None, False
    if platform_compat.is_link_or_junction(path):
        logger.warning(
            "SEL refusing the segment dir %s: it is a link (planted link?); "
            "it is not audit history",
            path,
        )
        return None, False
    if not stat.S_ISDIR(named.st_mode):
        logger.warning("SEL refusing the segment dir %s: it is not a directory", path)
        return None, False
    if not _PIN_BY_FD_SUPPORTED:
        return _SegmentDirPin(fd=None, identity=(named.st_dev, named.st_ino)), False
    # O_DIRECTORY types as POSIX-only; the capability constant above already
    # gated this branch to platforms that have it.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1), getattr(errno, "EFTYPE", -1)):
            logger.warning(
                "SEL refusing the segment dir %s: it is a link (planted link?); "
                "it is not audit history",
                path,
            )
        elif exc.errno == errno.ENOENT:
            # Vanishing AFTER lstat already saw it is not fresh-install
            # absence — the directory WAS there, and its removal before the
            # pin landed is interference the refusal must surface.
            logger.warning(
                "SEL refusing the segment dir %s: it vanished between the lstat "
                "and the pin (planted link?)",
                path,
            )
            return None, False
        else:
            logger.warning("SEL could not pin the segment dir %s", path, exc_info=True)
        return None, False
    try:
        opened = os.fstat(fd)
    except OSError:
        os.close(fd)
        logger.warning("SEL could not stat the pinned segment dir %s", path, exc_info=True)
        return None, False
    if not stat.S_ISDIR(opened.st_mode):
        os.close(fd)
        logger.warning("SEL refusing the segment dir %s: it is not a directory", path)
        return None, False
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        logger.warning(
            "SEL refusing the segment dir %s: it is not the directory its name "
            "points at (planted link?); it is not audit history",
            path,
        )
        return None, False
    return _SegmentDirPin(fd=fd, identity=(opened.st_dev, opened.st_ino)), False


class SelVerification(NamedTuple):
    """``verify_integrity(detailed=True)``'s result (#5051 review).

    ``total``/``valid`` keep the plain two-number contract; the added pair
    states whether the audit HISTORY was verifiable at all. A segment
    directory that refused to pin (a planted link) or was replaced
    mid-verification leaves the rotated segments UNCHECKED, and a caller
    whose job is to surface tampering must not report that run as "intact"
    over the live log alone. A directory that simply does not exist yet
    stays verifiable — a fresh install has no history to vouch for.
    """

    total: int
    valid: int
    history_verifiable: bool
    reason: str


def _parse_timestamp(raw: object) -> datetime | None:
    """Parse a SEL record timestamp into an aware UTC datetime, or ``None``.

    Records are written with ``datetime.now(timezone.utc).isoformat()``, but a
    hand-edited or forwarded record may carry a ``Z`` suffix or no offset at all.
    ``fromisoformat`` only accepts ``Z`` from Python 3.11, and the package
    supports 3.10, so the suffix is normalized here. A naive timestamp is read as
    UTC — the only offset SEL ever writes — so it stays comparable with an aware
    window bound instead of raising.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    accidental ``slack`` an empty key used to classify to.
    """
    if not session_key:
        return "unknown"
    if session_key == "_host":
        return "host"
    if session_key.startswith("dashboard:"):
        return "dashboard"
    if session_key.startswith("cron:"):
        return "cron"
    if session_key.startswith("subagent:"):
        return "subagent"
    if session_key.startswith("taskrunner"):
        return "taskrunner"
    if session_key == "_bg":
        return "background"
    if session_key == "_hb":
        return "heartbeat"
    if session_key == "cli_chat":
        return "cli"
    # Namespaced messaging channels carry their transport as the first key
    # segment (``{channel}:{agent}:...`` per messaging/link.build_dm_session_key,
    # or a ``{channel}_`` prefix). Match the SAME set context._runtime_display_name
    # uses (#979) so SEL attribution and the display name stay in lockstep.
    # Bare/legacy Slack keys (thread timestamps like ``C08...:thread``) have no
    # namespace prefix and correctly retain the historical ``slack`` fallback.
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


def _trust_root_key_loads(path: Path) -> bool:
    """True when *path* is a regular file currently holding a usable key.

    ``stat`` only — the caller reads the bytes just after, and the point here is
    to answer "does the resolved path still resolve?" without paying a read on
    the healthy path. ``S_ISREG`` is load-bearing rather than tidiness: a
    candidate location an actor can create is a place they can put a FIFO, and
    opening one blocks in-kernel forever, which an ``asyncio`` timeout cannot
    reclaim because the thread is stuck in a syscall.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size >= _HMAC_KEY_MIN_BYTES


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
    being closed (the shape ``session_pid_sig`` was left in by #2574).

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
    anchor (#2588), so what reaches here is the residue it cannot resolve — a
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
