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
Rotation: the active file rolls to numbered sealed segments
(``security_events.jsonl.1`` … ``.N``) once it exceeds ``max_bytes``, keeping at
most ``backup_count`` of them, so the log is size-bounded instead of growing
without limit on a long-lived host. The HMAC chain is NOT re-anchored on a roll:
verify_integrity() walks every segment oldest→newest as one continuous chain.
Retention: configurable, default 365 days per Amazon Security Event Logging Standard.
Aged whole segments are dropped first (never severing a chain mid-segment), then
the active file's own aged entries are rewritten out.
"""

from __future__ import annotations

import atexit
import errno
import hashlib
import hmac
import json
import logging
import os
import queue
import stat
import sys
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

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
_RETENTION_DAYS = 365
_HMAC_KEY_FILE = "sel_hmac.key"
# Sticky marker: written the first time a sealed segment is actually deleted
# (backup_count overflow or age-prune), i.e. the genesis prefix has been evicted.
# Non-digit suffix, so it is never mistaken for a numbered segment. Its CONTENTS
# are a MAC under the SEL key (see _marker_token), so it is authenticated state
# rather than a bare touch-file. Lets verify tell
# "genesis legitimately evicted" (relax the baseline) from "never rotated"
# (enforce genesis) even after ALL sealed segments have later been age-pruned —
# a state bool(_sealed_segments()) alone can't distinguish. Cleared only on a
# backup_count=0 genesis re-anchor (which restores prev_hash="").
_EVICTED_MARKER_FILE = "evicted"
# Companion to the sticky marker above, and deliberately NOT the same file.
# A backup_count=0 roll TRUNCATES the active file rather than unlinking it (see
# _discard_leased for why), so a writer in another process holding an O_APPEND fd
# lands its already-computed record at the new EOF. That record's prev_hash names
# the tip this install just destroyed, so verify met a first entry with a
# non-empty prev_hash on a host carrying no eviction marker and reported
# "SEL chain break at entry 1" -- measured (total=1 valid=0) -- which is
# byte-for-byte the verdict a head truncation produces. This file records THAT
# ONE tip, MAC'd under the SEL key, so the relaxation is scoped to a single hash
# value instead of relaxing the genesis anchor wholesale the way the sticky
# marker does. Head-truncating to any other point still breaks, because after the
# discard the only record that can carry the discarded tip as its prev_hash is
# the concurrent append itself -- an attacker cannot move prev_hash to a
# different value without invalidating that entry's own HMAC. Non-digit name, so
# it is never mistaken for a numbered segment.
_DISCARDED_TIP_FILE = "discarded-tip"
# Sealed segments live in their OWN subdirectory rather than as dot-suffixed
# siblings of the active file. That is what lets the sensitive-path floor cover
# the whole family with a single registered leaf (subtree matching), instead of a
# prefix-family regex replicated across every matcher. The active file stays put,
# so its existing exact-name protection is untouched and there is nothing to
# migrate.
_SEL_SEGMENT_DIR = "sel"
# Read cap for the eviction marker. Its payload is a 64-char hex MAC, so this is
# ~4x what a genuine marker needs. The cap is a DoS bound, not a parsing limit:
# the marker path is agent-writable before this feature's sensitive-path family
# lands, so a symlink pointing at an endless source (/dev/zero) would otherwise
# make an unbounded read hang verify_integrity() instead of failing closed.
_MARKER_READ_CAP = 256
# Backward-scan cap for chain-tip discovery. _tip_hash_of walks a segment from
# the end looking for the last complete JSON record, holding back the bytes it
# has not yet split into whole lines. A segment containing NO newline never
# yields a complete line, so that held-back buffer grows to the size of the
# whole file -- an unbounded read reached from the constructor. _open_segment
# already refuses symlinks, fifos and devices; a large REGULAR file passes it,
# which is why this bound is separate from that guard. 1 MiB is far more than
# any genuine tail needs (a record is a few hundred bytes).
_TIP_SCAN_MAX_BYTES = 1024 * 1024
# Per-LINE cap for the verify walk. verify must read a whole segment to count its
# entries, so it cannot be bounded in total without breaking a legitimately large
# one. A single line is different: the writer emits one JSON record per line, a few
# hundred bytes, so a line this long means the file is not a segment. Without the cap
# a planted newline-free file is one allocation the size of the whole file -- the
# same plant _open_segment's other guards do not stop, since a large REGULAR file
# passes them.
_SEGMENT_LINE_CAP = 1024 * 1024
# Lock file serialising the SEAL across writer processes. Scoped to exactly
# "claim a number and move the active file onto it" -- NOT to segment renames,
# which monotonic numbering removed. See _seal_lease for why the atomic claim
# alone is not sufficient.
_SEAL_LOCK_FILE = "seal.lock"

# How many times verify re-takes its segment snapshot when a rival process seals
# mid-snapshot. Bounded so a busy rotator cannot spin verify: on the last attempt
# the segments that appeared are counted UNVERIFIED (loud) instead of dropped.
_VERIFY_SNAPSHOT_ATTEMPTS = 3


def _open_segment(path: Path) -> IO[bytes]:
    """Open an audit segment for reading, refusing anything but a regular file.

    EVERY segment read goes through here so a later read site cannot silently miss
    the check. Segments are named predictably (``security_events.jsonl.1``..N), and
    before this feature's sensitive-path family lands those names are agent-writable
    — so an agent can plant one as a symlink to an endless source and turn a verify
    or an events fetch into an unbounded read that exhausts memory.

    The two guards are complementary, not redundant. ``O_NOFOLLOW`` refuses a
    symlinked final component, which is the plant described above; ``S_ISREG`` on the
    opened descriptor refuses a fifo, device or directory, which ``O_NOFOLLOW``
    permits. Checked by ``fstat`` on the descriptor rather than by a prior ``lstat``
    so there is no window between the check and the open. Windows has no
    ``O_NOFOLLOW``, and ``S_ISREG`` does NOT stand in for it there: ``fstat`` reports
    the TARGET of a link the open already followed, so a symlink aimed at a large
    regular file passes that check. Where the flag is unavailable the link is
    therefore refused explicitly before the open -- an ``lstat`` pre-check, which
    trades a guaranteed follow for a lose-the-race window and is the strongest
    refusal a platform without ``O_NOFOLLOW`` offers.

    ``O_NONBLOCK`` is what makes the ``S_ISREG`` half reachable at all: opening a
    fifo read-only BLOCKS until a writer appears, so without it a planted fifo hangs
    inside ``os.open`` and the check below never runs — the same denial of service by
    another route. It is a no-op for regular files, which is every legitimate segment.

    Raises ``OSError`` so callers keep their existing ``except OSError`` behaviour:
    an unreadable segment already means "skip it", and refusing to read a planted one
    is the same outcome.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        # Read at call time, not module scope, so the unavailable branch stays
        # exercisable on a host that HAS the flag.
        raise OSError(errno.ELOOP, "SEL audit segment is a symlink", str(path))
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(
                errno.EINVAL, "SEL audit segment is not a regular file", str(path)
            )
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _fd_is_unlinked(fd: int) -> bool:
    """True iff *fd*'s inode has no remaining directory entry.

    The append path holds no lease -- taking one per append would put a
    cross-process file lock on the hot path -- so a rotation in ANOTHER process can
    land between this fd's ``open`` and its flush. Two outcomes, and only one loses
    data. A seal alone is a ``rename``: link count stays 1, the fd still names that
    inode under its new segment number, and the record lands at the end of that
    sealed segment correctly chained and readable, so nothing is owed. A seal
    FOLLOWED by an age-prune ``unlink`` drops the count to 0, and the bytes are then
    reachable by nobody -- that is the loss this detects.

    ``st_nlink`` is the check because it asks the question directly, rather than
    comparing a stat of the path (which a concurrent roll has already replaced).
    Any ``OSError`` returns False: unprovable loss must not trigger a re-append,
    because a spurious one DUPLICATES an audit record. Windows reports 1 here and
    also refuses to unlink an open file, so the race it detects cannot arise there.
    """
    try:
        return os.fstat(fd).st_nlink == 0
    except OSError:
        return False


def _segment_lines(fh: IO[bytes]) -> Iterator[str]:
    """Yield a segment's lines, refusing any line longer than ``_SEGMENT_LINE_CAP``.

    A GENERATOR, not a list, and that is the load-bearing half of the bound. Capping
    each LINE leaves the aggregate unbounded when every line is appended to a list:
    a segment of many ordinary short lines trips no cap and still materialises whole,
    so a max_bytes-sized segment cost hundreds of MB of live ``str`` objects on the
    ``/api/sel/verify`` and ``/api/sel/events`` paths (measured: a 3.2 MB segment
    peaked at 22.9 MB, because a decoded line is several times its bytes). Yielding
    holds one line at a time, which makes every call site O(1) in the file's size.

    ``_entry_count_of`` documents the same trade from the other side -- it hand-rolled
    a streaming loop precisely BECAUSE this helper accumulated. That divergence is
    what this shape removes: counting no longer needs its own reader for memory
    reasons.

    Raises ``OSError`` rather than a bespoke exception so the verify walk's existing
    ``except OSError`` treats an over-cap line exactly as it treats an unreadable
    segment: logged, folded into ``total``, never into ``valid``. Fail-loud, not a
    silent skip. Note the raise now surfaces MID-ITERATION rather than before the
    caller sees any line, so each call site keeps its ``except OSError`` around the
    loop, not merely around the call.

    ``readline`` splits on ``\n`` only, which is what the writer emits. A bare
    ``read().splitlines()`` also split on ``\v``, ``\f`` and the Unicode line
    separators, so an embedded control character used to inflate ``total`` with
    fragments of one record.

    EVERY segment read goes through here, the same way every segment OPEN goes
    through :func:`_open_segment`, and for the same reason: three separate call
    sites each slurped a whole segment, and bounding one of them left the other two
    open. Decoding with ``errors="replace"`` also removes a second hazard the
    ``recent()`` site carried -- a bare ``decode("utf-8")`` raises
    ``UnicodeDecodeError``, which its ``except OSError`` does not catch, so one
    non-UTF-8 byte in any segment took down the events endpoint.
    """
    while True:
        raw = fh.readline(_SEGMENT_LINE_CAP + 1)
        if not raw:
            return
        if len(raw) > _SEGMENT_LINE_CAP:
            raise OSError(
                errno.EFBIG,
                f"SEL audit segment line exceeds {_SEGMENT_LINE_CAP} bytes",
            )
        yield raw.decode("utf-8", errors="replace")


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
# Rotation defaults. The active file rolls into a sealed segment under the
# ``sel/`` subdirectory (``sel/1``, then ``sel/2`` …; numbers are allocated once
# and NEVER renamed -- see _segment_path) once it would exceed
# _DEFAULT_MAX_BYTES; at most _DEFAULT_BACKUP_COUNT sealed segments are kept.
# max_bytes=0 disables rotation entirely (legacy unbounded append-only). The
# HMAC chain is NOT reset on rotation: the new active file's first entry chains
# off the sealed segment's last entry, so verify_integrity() walks every
# segment oldest->newest as one continuous chain and stays correct across the
# rotation seam.
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB active-file cap
_DEFAULT_BACKUP_COUNT = 5  # sealed segments retained (~600 MB total at defaults)

# A rejected override is echoed back only when it is short AND spelled entirely
# from this set. No credential can be written in digits and integer punctuation,
# while the typo an operator needs to see -- "1,000", "1 000", "1.5" -- always can.
_SAFE_ECHO_CHARS = frozenset("0123456789+-_., \t")
_SAFE_ECHO_MAX_LEN = 16


def _macs_equal(stored: object, expected: str) -> bool:
    """Constant-time MAC compare that cannot raise on a hostile *stored* value.

    ``hmac.compare_digest`` REJECTS a non-ASCII ``str`` -- measured:
    ``TypeError: comparing strings with non-ASCII characters is not supported`` --
    and also rejects a non-``str`` operand. Both values are reachable FROM DISK,
    which is why this guard exists rather than a docstring promising they cannot be:

    * the eviction marker is decoded with ``errors="replace"``, so ANY invalid byte
      in it becomes U+FFFD and the decoded ``str`` is non-ASCII (measured:
      ``b"\\xff\\xfeXX".decode("utf-8", "replace")`` -> ``'\\ufffd\\ufffdXX'``);
    * a segment line's ``entry_hash`` is whatever the JSON held, and
      ``{"entry_hash": "\\u00e1..."}`` is perfectly valid JSON.

    Every MAC this install writes is a ``hexdigest()``, so it is ASCII by
    construction. A non-ASCII or non-``str`` value therefore CANNOT be a match, and
    returning False is the same not-authentic outcome each caller already has for a
    mismatch -- the guard rejects input, it does not invent a new verdict.

    Deliberately NOT ``except TypeError`` around the compare: that would also
    swallow a genuine type error introduced at a call site, hiding the class
    instead of validating the input. Ordering matters too -- ``isinstance`` is
    checked BEFORE ``.isascii()``, because a non-``str`` has no ``.isascii``.
    """
    if not isinstance(stored, str) or not stored.isascii():
        return False
    return hmac.compare_digest(stored, expected)


def _env_int(name: str, default: int) -> int:
    """Read an integer operator override, falling back to *default*.

    Fails SOFT on a malformed value: a typo in an env var must not stop the audit
    log from starting, and it must not silently pick a value the operator did not
    write either, so it logs and uses the default. The rejected value is echoed
    back only when it cannot carry a secret (see ``_SAFE_ECHO_CHARS``); otherwise
    only its length is reported, because an operator who pastes a credential into
    the wrong knob must not have it copied into the log. Negative values are passed
    through unchanged -- the knobs treat <=0 as "off" at the point of use.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        shown = (
            repr(raw)
            if len(raw) <= _SAFE_ECHO_MAX_LEN and set(raw) <= _SAFE_ECHO_CHARS
            else f"<{len(raw)} chars>"
        )
        logger.warning(
            "SEL ignoring malformed %s=%s; using default %d", name, shown, default
        )
        return default


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

    Thread-safe. Singleton pattern — all callers share one instance.
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
        # Rotation knobs: this module's constants, overridable per-host by an
        # operator through the environment. The override exists because these three
        # values govern DELETION on an audit surface, and a fixed compile-time cap
        # means a host whose event volume outruns the cap loses security events
        # inside the retention window with no lever to stop it. Amazon's audit-log
        # retention guidance requires the retention period to be operator-settable
        # and security events kept >= 365 days, so shipping the cap without a lever
        # would leave a policy-relevant behaviour unreachable. Env (not config) for
        # now: ``KiroCrewConfig`` carries no ``sel`` section, and reading one would be
        # a static type error plus an unreachable branch here. The config follow-up
        # slots in between env and these defaults, in this block alone.
        #
        # max_bytes<=0 disables rotation; retention_days<=0 disables age pruning.
        # Both off-switches are enforced at the point of use (_maybe_rotate and
        # _prune_sealed_by_age), so a test that assigns a nonsense value still gets
        # a defined behaviour rather than one that leaks into the comparisons. There
        # are deliberately NO constructor kwargs: nothing in production passed them,
        # so they were public surface serving only the tests, which set these
        # attributes directly instead.
        self._max_bytes = _env_int("KIROCREW_SEL_MAX_BYTES", _DEFAULT_MAX_BYTES)
        self._backup_count = _env_int("KIROCREW_SEL_BACKUP_COUNT", _DEFAULT_BACKUP_COUNT)
        self._retention_days = _env_int("KIROCREW_SEL_RETENTION_DAYS", _RETENTION_DAYS)
        # Cumulative count of segments the SIZE cap deleted while the RETENTION
        # window still wanted them. Read as a delta by _flush_batch to emit the
        # counter off _lock; monotonic for the process lifetime.
        self._early_evictions = 0
        # Single-flight guard for rotation handed off the event loop. See
        # _defer_rotation: a burst of critical audits must not spawn a thread each.
        self._rotation_deferred = False
        self._rotation_defer_lock = threading.Lock()
        # _lock guards _last_hash + the file append (held only inside the writer
        # thread and by synchronous fallbacks / prune, never by enqueuing callers).
        self._lock = threading.Lock()
        self._hmac_key = self._load_or_create_hmac_key()
        self._last_hash = self._read_last_hash()
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

    def _flush_batch(self, events: list[SecurityEvent], *, raise_on_error: bool = False) -> None:
        """Append a batch of events under the chain lock, then forward them.

        When ``raise_on_error=True`` a filesystem failure (unwritable SEL file,
        full disk, un-creatable dir) is re-raised after rolling the chain tip
        back, so a fail-closed caller (critical audit) can refuse the action it
        was about to audit. The default (async writer / best-effort) swallows
        the error and keeps the writer thread alive.
        """
        callback: Callable[[dict], None] | None
        rotation_failed = False  # emit the observability counter AFTER releasing _lock
        with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                if raise_on_error:
                    raise
                logger.warning("SEL dir create failed for %d events", len(events), exc_info=True)
                return
            # Roll the active file to a sealed segment if it hit the size cap.
            # Best-effort and orthogonal to durability: a rotation failure must
            # never block the audit append (or a fail-closed critical caller), so
            # it is swallowed here — the batch still appends to the un-rotated
            # active file. Rotation does NOT touch _last_hash, so the first entry
            # written below chains off the sealed segment's tip and the chain
            # stays continuous across the seam.
            early_before = self._early_evictions
            deferred = False
            try:
                # Rotation is bounded work but it is still filesystem work, and a
                # `critical=True` audit runs this inline on the CALLER's thread --
                # which for some callers is the asyncio loop. Hand it to a helper
                # thread there and keep the loop free; rotate inline everywhere
                # else (background writer, sync mode, CLI) where blocking is fine.
                if self._on_event_loop():
                    self._defer_rotation()
                    deferred = True
                else:
                    self._maybe_rotate()
            except Exception:
                logger.warning("SEL rotation failed; appending without rotating", exc_info=True)
                # Just flag it here; emit the observability counter AFTER releasing
                # _lock (below) so a slow metrics backend can't stall the writer
                # while it holds the chain lock. Rotation failure is the exact
                # condition this feature guards against (silent degrade back to
                # unbounded growth), so it must stay observable — but off the lock.
                rotation_failed = True
            # Remember the chain tip so we can roll back if the append fails:
            # we advance _last_hash per event below, but nothing is persisted
            # until the write() succeeds. Without the rollback, a failed write
            # would leave _last_hash pointing at a phantom hash never on disk,
            # and the next batch would chain off it — silently corrupting the
            # HMAC chain (verify_integrity would then report a break).
            prev_last_hash = self._last_hash
            lines: list[str] = []
            for event in events:
                event.prev_hash = self._last_hash
                event.entry_hash = self._compute_hash(event)
                lines.append(json.dumps(asdict(event)) + "\n")
                self._last_hash = event.entry_hash
            try:
                # Use os.open with explicit 0o600 mode to prevent other users
                # from reading the security audit log.
                fd = os.open(
                    self._path,
                    os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                    0o600,
                )
                stranded = False
                with os.fdopen(fd, "a", encoding="utf-8") as f:
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
                    stranded = _fd_is_unlinked(f.fileno())
                if stranded:
                    # Another PROCESS sealed the active file and then age-pruned
                    # the resulting segment while this fd was open, so the bytes
                    # just written have no name and no reader can reach them.
                    # Write them again, once, to whatever is now the active file.
                    # If that cannot place them either it raises, and the handler
                    # below treats it as any other append failure: roll the chain
                    # tip back, and re-raise only for a fail-closed caller.
                    self._reappend_stranded(lines)
                # Ensure permissions are correct even if file pre-existed with
                # wrong mode (e.g. created by an older version).
                try:
                    os.chmod(self._path, 0o600)
                except OSError:
                    logger.warning("Failed to enforce 0o600 permissions on SEL audit log %s", self._path, exc_info=True)
            except OSError:
                self._last_hash = prev_last_hash  # nothing persisted — roll back
                if raise_on_error:
                    raise
                logger.warning("SEL append failed for %d events", len(events), exc_info=True)
            callback = self._forward_callback
        # Inline path only: the deferred hand-off emits from its own body, where
        # the work has actually happened (see _emit_rotation_counters).
        if not deferred:
            self._emit_rotation_counters(
                rotation_failed=rotation_failed, early_before=early_before
            )
        if callback:
            for event in events:
                self._forward_event(callback, event)

    def _reappend_stranded(self, lines: list[str]) -> None:
        """Re-write *lines* once after they landed on an unlinked inode.

        Deliberately ONE attempt, not a loop: a second roll racing the retry is
        vanishingly rare next to the cost of spinning on the writer thread under
        ``_lock``, and a bounded miss stays observable through the warning below.
        ``_last_hash`` is untouched -- these are the same records with the same
        hashes, so re-writing them keeps the chain the caller already computed.

        The re-written records' ``prev_hash`` may now refer to a tip the prune
        deleted, which ``verify_integrity`` reports as a chain break. That is the
        same trade the zero-backup discard path takes: a break verify REPORTS beats
        evidence that silently is not there.

        Raises ``OSError`` when the records could not be placed anywhere a reader
        can reach -- either the re-open/write failed, or the retry was stranded in
        turn. Swallowing that would make a ``critical=True`` audit return normally
        with its evidence unreachable, so the caller's fail-closed branch never
        runs and it grants the permission unaudited. ``_flush_batch`` turns this
        back into the documented behaviour for each kind of caller: it rolls the
        chain tip off the unreachable records, then re-raises only when
        ``raise_on_error`` is set and warns otherwise.
        """
        logger.warning(
            "SEL append landed on an unlinked inode -- another process sealed and "
            "age-pruned the active log mid-append. Re-writing %d record(s) to the "
            "current active file; verify may report a chain break at this seam.",
            len(lines),
        )
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                # The SAME newline boundary the primary append already inserts (see
                # `_flush_batch`, which calls `_ends_without_newline()` for exactly
                # this reason). This path omitted it, and the omission is worse here
                # than there: the file being appended to is whatever OTHER process's
                # active file the retry lands on, so its tail was never written by
                # this call and can be a torn fragment from a crash mid-append.
                # Without the guard, O_APPEND glues the first re-written record onto
                # that fragment into ONE unparseable line -- measured: a
                # `CRITICAL-AUDIT` record re-appended after a torn tail was NOT
                # recoverable by `json.loads`, while the fragment and the record
                # together formed a single line no reader can parse.
                #
                # That is a DIFFERENT failure from the chain break this method's
                # docstring accepts. A reported break still leaves every record
                # readable; this destroys the record's readability outright, so a
                # `critical=True` audit would return normally with its evidence
                # unparseable and the caller's fail-closed branch would never run.
                #
                # The fragment is NOT truncated: the log is append-only forensic
                # evidence, so it is preserved as its own (skipped) line.
                if self._ends_without_newline():
                    f.write("\n")
                f.write("".join(lines))
                f.flush()
                restranded = _fd_is_unlinked(f.fileno())
        except OSError:
            logger.error(
                "SEL could not re-write %d stranded audit record(s)", len(lines), exc_info=True
            )
            raise
        if restranded:
            logger.error(
                "SEL re-append was stranded too; %d audit record(s) are unreachable. "
                "Concurrent rotation is racing every append -- run a single "
                "writer for this log.",
                len(lines),
            )
            # EIO, not ENOENT: the path usually DOES exist by now (the rival's roll
            # created a fresh active file), and ENOENT raises FileNotFoundError -- a
            # name that invites a caller to treat this as "not created yet, benign".
            raise OSError(
                errno.EIO,
                "SEL re-append also landed on an unlinked inode",
                str(self._path),
            )

    def _emit_rotation_counters(self, *, rotation_failed: bool, early_before: int) -> None:
        """Emit the rotation/eviction counters. Call with ``_lock`` RELEASED.

        Shared by both rotation paths because they observe the work at different
        times. The inline path rotates under ``_lock`` and so can compare against
        ``early_before`` the moment the lock drops; the DEFERRED path only hands
        the work to a thread, so at that moment nothing has rotated yet and both
        counters read as no-change. Emitting from the deferred body instead is
        what keeps ``early_eviction.count`` -- the signal that audit evidence was
        dropped before its retention window -- from under-reporting to ~never on
        the loop-driven path most gateway audits take.
        """
        # Outside _lock: a slow metrics backend must not stall the writer thread.
        # A counter (not a SEL event) — enqueuing an event would recurse into this
        # writer. The recorder self-guards (no-op if metrics off, never raises), and
        # the try/except keeps telemetry from turning a swallowed rotation failure
        # into a raised one.
        #
        # The import is function-local to keep the config layer off this module's
        # import graph, NOT because a top-level import would fail: metrics.provider
        # imports config.loader at module scope, and loader imports sel only inside
        # a function, so the cycle is not import-time-fatal (measured — a top-level
        # import here loads cleanly in all three orders). The reason is COST, and it
        # is the same trade AUTOSDE `top-level-imports` (blocking: false) has been
        # declined on before: importing sel pulls 190 modules, and hoisting this
        # would pull 81 more, because provider imports config.loader at module
        # scope (provider.py:42) — dragging config.loader, config.validation and
        # the OpenTelemetry probe onto every importer of the security event log,
        # which is constructed extremely early.
        # Same reason _default_dir() resolves config_dir() lazily rather than at
        # module scope. This block is also the only observability for a rotation
        # failure silently degrading the log back to unbounded growth, so it earns
        # the one local import.
        # ONE local import serves both counters below. It was two identical ones,
        # which made the AUTOSDE `top-level-imports` finding read as a one-line
        # hoist when it is not.
        #
        # circular import: hoisting this to module scope does not merely cost
        # boot time -- it BREAKS THE BUILD. `metrics.provider` imports
        # `config.loader` at module scope (provider.py:42), whose chain reaches
        # `kiro_crew.security`, and security.py:33 does
        # `from kiro_crew.sel import SecurityEvent, SecurityEventLog` at module
        # scope. So a module-scope import here re-enters this module while it is
        # still initialising. Measured, both shapes the rule would accept:
        #   from kiro_crew.metrics.provider import get_recorder   -> ImportError
        #   import kiro_crew.metrics.provider as _p                -> ImportError
        # both "cannot import name 'SecurityEvent' from partially initialized
        # module 'kiro_crew.sel'". This is the exemption the rule states for
        # genuine circular-import avoidance, and it is the same reason the
        # `security.redact` import below is lazy.
        #
        # The boot cost is a SECOND, independent reason and remains rnoack's call
        # (see the PR description): importing `sel` alone pulls 145 modules and
        # adding `metrics.provider` pulls 226, so the hoist would put +81 modules
        # on every importer of the security event log.
        if rotation_failed or self._early_evictions > early_before:
            try:
                from kiro_crew.metrics.provider import get_recorder

                recorder = get_recorder()
            except Exception:
                recorder = None
            if recorder is not None:
                if rotation_failed:
                    try:
                        recorder.counter("kirocrew.sel.rotation_failed.count")
                    except Exception:
                        pass
                if self._early_evictions > early_before:
                    try:
                        recorder.counter(
                            "kirocrew.sel.early_eviction.count",
                            self._early_evictions - early_before,
                        )
                    except Exception:
                        pass

    @staticmethod
    def _on_event_loop() -> bool:
        """True when the calling thread is running an asyncio event loop.

        Reads ``sys.modules`` rather than importing asyncio at module scope: a
        process that never imported asyncio cannot have a loop, and sel is
        constructed early enough that paying for that import is the same trade
        declined for the metrics provider below.
        """
        aio = sys.modules.get("asyncio")
        if aio is None:
            return False
        try:
            aio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def _defer_rotation(self) -> None:
        """Hand rotation to a background thread instead of running it inline.

        A ``critical=True`` audit writes synchronously on the CALLER's thread, and
        some callers are on the asyncio loop (a direct, un-offloaded
        ``log_tool_invocation(critical=True)`` inside an ``async def``). Sealing a
        segment plus evicting and age-pruning is bounded but not free, and none of
        it belongs on that thread, so the loop hands the work off and returns.

        Single-flight: while one hand-off is pending, further calls are no-ops, so
        a burst of critical audits cannot spawn a thread per event. The cost of
        deferring is that the active file may overshoot ``max_bytes`` by the appends
        that land before the helper takes ``_lock`` -- the same soft-cap overshoot
        :meth:`_maybe_rotate` already documents, not an unbounded one, because the
        hand-off is immediate rather than conditional on later traffic.
        """
        with self._rotation_defer_lock:
            if self._rotation_deferred:
                return
            self._rotation_deferred = True
        try:
            threading.Thread(
                target=self._run_deferred_rotation, name="sel-rotate", daemon=True
            ).start()
        except Exception:
            # Thread creation failed (fd/thread exhaustion): clear the flag so a
            # later append can retry, and let this roll be skipped. Rotation is
            # best-effort by construction -- see _flush_batch's swallow.
            with self._rotation_defer_lock:
                self._rotation_deferred = False
            raise

    def _run_deferred_rotation(self) -> None:
        """Body of the deferred hand-off: take ``_lock``, then rotate.

        Emits the rotation counters itself. ``_flush_batch`` cannot: it only
        SPAWNS this thread, so its own post-lock check runs before this body has
        re-acquired ``_lock`` and evicted anything, and both counters would read as
        no-change on every deferred roll.
        """
        rotation_failed = False
        early_before = self._early_evictions
        try:
            with self._lock:
                early_before = self._early_evictions
                self._maybe_rotate()
        except Exception:
            rotation_failed = True
            logger.warning("SEL deferred rotation failed", exc_info=True)
        finally:
            with self._rotation_defer_lock:
                self._rotation_deferred = False
        # Outside _lock, same reason as the inline path.
        self._emit_rotation_counters(
            rotation_failed=rotation_failed, early_before=early_before
        )

    def _maybe_rotate(self) -> None:
        """Roll the active file to a sealed segment when it exceeds max_bytes.

        Caller MUST hold ``self._lock``. Rotation seals the active file under the
        next MONOTONIC segment number -- ``security_events.jsonl`` becomes
        ``sel/security_events.jsonl.<N+1>`` -- and no existing segment is ever
        renamed. That retires the WIDE cross-process lease this path used to need,
        which had to span a shift sequence renaming every segment on every roll --
        an interleaving there could rename one segment onto another and destroy
        retained history for good. A much narrower lease remains and is still
        required: see :meth:`_seal_lease`, which spans only claim + replace,
        because the atomic number claim does not by itself ORDER the seal.

        The HMAC chain is deliberately NOT re-anchored: ``_last_hash`` is left
        untouched so the next append chains off the just-sealed segment's final
        entry. verify_integrity()/recent() read every segment oldest->newest, so
        the chain validates unbroken across the rotation seam. ``max_bytes<=0``
        disables rotation entirely (legacy unbounded append-only).

        Soft cap: this runs at the top of _flush_batch BEFORE the batch appends,
        so a sealed segment can overshoot max_bytes by up to one batch (<=256
        events).
        """
        if self._max_bytes <= 0:
            return
        try:
            size = self._path.stat().st_size
        except OSError:
            return  # no active file yet, or unstatable -- nothing to roll
        if size < self._max_bytes:
            return  # under threshold (also covers a 0-byte file: max_bytes>=1 here)
        self._rotate_now()

    def _rotate_now(self) -> None:
        """Seal the active file, then apply the size and age bounds.

        Caller MUST hold ``self._lock``, AND a cross-process lease is taken here
        (see :meth:`_seal_lease`). Monotonic numbering alone is not sufficient: the
        atomic claim in :meth:`_next_segment_index` stops two processes taking the
        same NUMBER, but it does not order the seal, and the chain depends on that
        order.

        Both terminal actions run inside the lease and re-stat the file first,
        because both act on a size measured by the unsynchronized pre-check in
        :meth:`_maybe_rotate`: :meth:`_seal_leased` moves the active file onto a
        fresh number, and :meth:`_discard_leased` unlinks it when no backups are
        kept. The re-stat is what makes a stale measurement harmless -- without it
        the discard path deletes an active file another process already rolled and
        appends have since recreated, which destroys persisted events.
        """
        # The dir guard runs BEFORE the lease so a planted link is refused rather
        # than locked inside -- and it must precede the lease in any case, because
        # the lease file itself lives inside that directory.
        self._ensure_segment_dir()
        with self._seal_lease() as leased:
            if not leased:
                return
            # Re-stat UNDER the lease: the pre-check in _maybe_rotate is
            # unsynchronized, so the lease holder may have just rolled this very
            # file. On the seal path acting on the stale size burns a retained slot
            # on a nearly empty segment; on the discard path below it UNLINKS an
            # active file that another process already rolled and appends have
            # recreated, which loses persisted events outright.
            try:
                if self._path.stat().st_size < self._max_bytes:
                    return
            except OSError:
                return
            if self._backup_count <= 0:
                self._discard_leased()
                return
            self._seal_leased()

    def _discard_leased(self) -> None:
        """Drop every segment and restart the chain at genesis.

        Holds ``_lock`` + the seal lease, and the caller has already re-stat'd the
        active file under that lease -- see :meth:`_rotate_now` for why an unlink on
        a stale size is destructive rather than merely wasteful.
        """
        # Rotation on but no backups kept: discard the sealed data entirely
        # and start a fresh active file (bounded to one file). Because the
        # prior tip is being deleted, re-anchor the chain to genesis -- leaving
        # _last_hash pointing at a hash no longer on disk would make the next
        # entry's prev_hash reference a vanished entry, which verify_integrity
        # reports as a break at the first post-rotation entry. A backup_count=0
        # operator has opted into losing history, so a clean genesis restart
        # (prev_hash="") is the correct, verifiable behavior.
        # ORDER IS LOAD-BEARING: sealed segments go FIRST, then the active file,
        # then the tip. Doing the active file first is what makes a partial failure
        # corrupting rather than merely wasteful. `missing_ok=True` suppresses only
        # FileNotFoundError, so a sealed unlink that fails for any OTHER reason --
        # a Windows sharing violation against a `recent()` call holding that segment
        # open, since recent() opens segments OUTSIDE `_lock` while this runs under
        # it -- propagates out of here. Under the old order that left the active
        # file already deleted, the sealed segments still on disk, and `_last_hash`
        # NOT yet reset, so `_flush_batch` caught the error, logged "appending
        # without rotating", and appended with prev_hash pointing at an entry that
        # no longer exists anywhere. That is a broken chain, and the operator note
        # above does NOT cover it: it promises the loss of recent EVENTS, not the
        # loss of the CHAIN. Sealed-first keeps the ACTIVE file and the TIP intact
        # on that failure, so the roll is skipped and retried later.
        #
        # IT IS NOT FILE-FOR-FILE ATOMIC, and this comment used to claim it was:
        # "the same failure aborts with every file and the tip untouched". Segments
        # are unlinked in ASCENDING order, so a raise on segment k+1 leaves 1..k
        # ALREADY GONE. That surviving-suffix state is a REAL prefix eviction, which
        # is why the marker is written from INSIDE the loop. Measured before the
        # fix, refusing the unlink of the newest of 7 sealed segments: 6 deleted, 1
        # surviving, NO marker written, and verify_integrity reported ``total=2
        # valid=1`` logging "SEL chain break at entry 1" -- a spurious break over
        # history this code had itself deleted.
        marked = False
        for idx in self._list_sealed_indices():
            self._segment_path(idx).unlink(missing_ok=True)
            # Mark on the FIRST deletion, mirroring :meth:`_evict_over_budget` for
            # the same reason: a raise on a later segment carries control out of
            # here BEFORE the marker clear and the genesis re-anchor below, and
            # `_flush_batch` swallows it, so a marker written after the loop
            # would never happen while the earlier deletions stand. On the success
            # path the clear below removes it again, so it is observable
            # only in the partial state it exists for.
            #
            # CONDITIONAL ON A DELETION HAVING HAPPENED, which is load-bearing in
            # the other direction: an empty sealed list never enters the body, and a
            # raise on the FIRST unlink leaves this False. Marking a host that
            # evicted nothing would relax the genesis anchor for free -- exactly the
            # head-truncation relaxation verify refuses to grant on mere segment
            # existence. Guarded, so at most one write per pass: no new cost.
            if not marked:
                self._mark_evicted()
                marked = True
        # OPERATOR NOTE: this unlinks the WHOLE active file at the rotation
        # boundary -- up to max_bytes (default 100 MB) of the MOST RECENT events,
        # not just long-tail history. backup_count=0 means "keep at most one
        # active file and drop everything else on roll"; use it only where that
        # recent-history loss is acceptable (keep backup_count>=1 to retain a
        # sealed tail).
        # TRUNCATE the active file rather than unlink it. Both discard its contents,
        # but they differ for a writer in another process that already has the fd
        # open: the append path opens with O_APPEND (see _flush_batch), so after an
        # UNLINK that writer's bytes go to an orphaned inode and vanish with no
        # error anywhere -- a silent audit loss. After a TRUNCATE the fd still names
        # this file, so the same write lands at the new EOF and survives. Its
        # prev_hash then refers to a tip that is gone, which verify reports as a
        # chain break -- fail-loud, and strictly better than fail-silent. This does
        # not make multi-writer append correct (the description names the
        # single-writer daemon for that); it removes the case where discarding
        # DESTROYS a concurrent write. Bounded-to-one-file still holds: the file is
        # the same one, now empty.
        # Captured BEFORE the truncate, while the tip is still the live one. This is
        # a pure read of an in-memory attribute -- nothing fallible is added between
        # the truncate and the re-anchor below, which the ordering note there
        # requires.
        discarded_tip = self._last_hash
        # Clear the evicted-prefix marker BEFORE anything destructive, and let a
        # failure ABORT the discard by propagating.
        #
        # This used to be the LAST statement, on the reasoning that "if THIS fails
        # the marker merely keeps the first-entry check relaxed, which is permissive
        # rather than corrupt." That reasoning was wrong about which failure is
        # worse, and the difference is measurable. Clearing last means an unlink
        # failure leaves an AUTHENTIC marker standing over a chain that has just been
        # re-anchored to genesis, so the relaxation is permanent -- measured: with the
        # stale marker, head-truncating 2 of 6 fresh entries verified
        # ``total=4 valid=4`` and logged NOTHING, while the identical truncation with
        # the marker cleared reported ``total=4 valid=3`` and "SEL chain break at
        # entry 1". "Permissive" is precisely the hole: it masks the head truncation
        # the marker gate exists to expose.
        #
        # Clearing first inverts both failure modes toward FAIL-LOUD, which is the
        # correct direction on an audit surface:
        #   * unlink fails -> nothing has been deleted or truncated yet, so the
        #     discard aborts in the state the marker legitimately describes (sealed
        #     prefix gone, active file and tip intact) and the roll is retried later;
        #   * the truncate below then fails -> the marker is already gone, so verify
        #     ENFORCES genesis against a chain whose prefix really was deleted and
        #     reports a break. A spurious break is loud and recoverable; a masked
        #     truncation is neither.
        self._marker_path().unlink(missing_ok=True)
        try:
            os.truncate(self._path, 0)
        except FileNotFoundError:
            pass  # already gone -- nothing to discard
        # Re-anchor the chain to genesis IMMEDIATELY after the active file goes,
        # with nothing fallible in between: a pure assignment cannot fail, so the
        # tip can never be left naming a deleted entry.
        self._last_hash = ""
        # Record the tip just destroyed so verify can tell a CONCURRENT APPEND that
        # linked to it from a head truncation. The truncate above deliberately keeps
        # such an append alive (see the note above it), and its prev_hash names this
        # hash, which verify would otherwise report as "chain break at entry 1" --
        # the same verdict tampering produces. Deliberately NOT _mark_evicted(): the
        # sticky marker relaxes the genesis anchor wholesale, whereas this
        # authenticates exactly one hash. Written LAST because it is the only step
        # whose failure is purely permissive of NOISE rather than of tampering: with
        # no record the concurrent append simply reports the break it reports today.
        self._record_discarded_tip(discarded_tip)

    def _seal_leased(self) -> None:
        """Claim a number and move the active file onto it. Holds ``_lock`` + lease."""
        index = self._next_segment_index()
        target = self._segment_path(index)
        try:
            os.replace(self._path, target)
        except FileNotFoundError:
            # The active file is gone: another process sealed it first. Benign, and
            # the ONLY error swallowed here -- drop the claimed placeholder, since an
            # empty segment left in the numbered run would read as a chain break.
            target.unlink(missing_ok=True)
            return
        except OSError:
            # A genuine failure (permissions, a Windows sharing violation against an
            # in-flight verify). Drop the placeholder, then RE-RAISE: _flush_batch
            # logs it and emits the rotation-failure counter, and silently degrading
            # back to unbounded growth is the exact condition this feature exists to
            # make observable.
            target.unlink(missing_ok=True)
            raise
        self._evict_over_budget()
        # count=False: this return is discarded, so skip the full-segment
        # _entry_count_of scan that would otherwise run under _lock on the writer
        # thread.
        self._prune_sealed_by_age(self._retention_days, count=False)

    def _drop_empty_claims(self, indices: list[int]) -> list[int]:
        """Exclude zero-byte segments from the budget WITHOUT unlinking them.

        Drops the NUMBER from the accounting, never the file. The budget hazard the
        exclusion fixes is real -- an uncounted zero-byte claim inflates
        ``len(indices)`` and evicts one additional VALID segment per roll -- but
        deleting the file to achieve it was a data-loss path, because a zero-byte
        segment has TWO possible provenances and this code cannot tell them apart:

        * a crash-left claim, created with ``O_EXCL`` by :meth:`_next_segment_index`
          before the ``os.replace`` that fills it, which never held history; or
        * a segment that WAS sealed with real history and was later truncated.

        Nothing on disk records which numbers were successfully sealed -- there is no
        sealed manifest -- and a zero-byte file has no content to authenticate, so
        the self-HMAC that guards :meth:`_prune_sealed_by_age` is unavailable here.
        With the two indistinguishable, the only safe direction is to keep the file:
        the surviving zero-byte segment is itself the evidence that something is
        wrong, and ``verify_integrity`` reports it as unverifiable (see the
        sealed-segment-holds-no-record branch of the walk), which forces
        ``valid < total`` rather than letting the log read clean.

        Caller MUST hold ``self._lock`` and the seal lease -- see
        :meth:`_evict_over_budget`. A stat failure leaves the number in the list:
        this is a budget input, and guessing that an unstattable segment is empty
        would evict real history.
        """
        kept: list[int] = []
        for idx in indices:
            seg = self._segment_path(idx)
            try:
                if seg.stat().st_size == 0:
                    logger.warning(
                        "SEL excluding empty sealed segment %s from the retention "
                        "budget but KEEPING the file: it is either a crash-left "
                        "number claim or a truncated segment, and nothing on disk "
                        "distinguishes them. verify_integrity() reports it as "
                        "unverifiable until an operator resolves it.",
                        seg.name,
                    )
                    continue
            except OSError:
                pass
            kept.append(idx)
        return kept

    def _evict_over_budget(self) -> None:
        """Delete the OLDEST sealed segments in excess of ``backup_count``.

        Caller MUST hold ``self._lock``. Segment numbers are monotonic, so the
        oldest segment is the LOWEST number and eviction removes a PREFIX of the
        numbered run. This is the direction that inverted with the layout: under
        shift-renames the oldest segment carried the HIGHEST index and eviction
        ran from the top, so applying that rule here would delete the NEWEST
        history and keep the oldest.

        Evicting by count (rather than by a fixed index threshold) also handles an
        operator LOWERING backup_count between runs: the whole excess prefix goes
        in one pass instead of leaving stale high segments over budget.

        A crash-left CLAIM is dropped from the BUDGET before it is computed. The
        number claim in :meth:`_next_segment_index` creates the target with ``O_EXCL``
        BEFORE the ``os.replace`` that fills it, so a process killed in that window
        leaves a zero-byte segment behind. Both in-process failure paths in
        :meth:`_seal_leased` already unlink it, but nothing cleans up after
        termination. It is no longer inert to the other mechanisms: the verify walk
        now reports a sealed segment holding no record as unverifiable, and
        age-pruning steps PAST it instead of stopping there. What it was never inert
        to is this budget: it inflates ``len(indices)`` and evicts one additional
        VALID segment, silently, on every roll for the life of the install. Measured
        at ``backup_count=3`` with segments 8-10 retained, a zero-byte claim at 11
        evicted segment 8 and its 439 bytes of real audit history.

        The claim is now EXCLUDED FROM THE BUDGET AND LEFT ON DISK, which is the
        part that changed. Deleting it looked lossless -- an empty segment carries no
        audit history by definition -- and the argument for deleting it ran: a
        truncated REAL segment is a different case and stays loud either way, because
        its successor's ``prev_hash`` still refers to the tip that was truncated away,
        so the chain breaks and ``valid < total``.

        THAT ARGUMENT IS FALSE AT THE POSITION THIS METHOD DELETES FROM, and the
        measurement is the reason the unlink is gone. Baseline relaxation applies to
        the OLDEST surviving entry, so it covers a mid-chain seam but not the head of
        the run -- and eviction removes a PREFIX, i.e. exactly the oldest. Measured on
        a 40-entry log with 39 sealed segments and an authenticated eviction marker
        present: truncating the OLDEST sealed segment and keeping the file reported
        ``total=40 valid=39`` (correctly not clean), and unlinking it reported
        ``total=39 valid=39`` -- ``integrity: ok`` over a record that was erased. The
        marker made the loss re-baselineable, so the erasure authenticated itself.
        """
        indices = self._drop_empty_claims(self._list_sealed_indices())
        excess = len(indices) - self._backup_count
        if excess <= 0:
            return
        cutoff = (
            datetime.now(tz=timezone.utc) - timedelta(days=self._retention_days)
            if self._retention_days > 0
            else None
        )
        evicted_early = 0
        marked = False
        for idx in indices[:excess]:  # lowest numbers == oldest segments
            seg = self._segment_path(idx)
            # Is the SIZE cap deleting evidence the RETENTION policy still wants?
            # These two bounds are independent, and the size cap wins because it
            # runs first -- so on a host whose event volume outruns max_bytes the
            # log silently stops meeting its own retention_days. Say so: an
            # operator who sees this is being told their cap is too small for their
            # volume. Best-effort by design: an unparseable or absent stamp just
            # means no warning, never a skipped eviction, because the size bound
            # must still hold.
            if cutoff is not None:
                newest = self._newest_timestamp_of(seg)
                parsed = self._parse_ts(newest) if newest is not None else None
                if parsed is not None and parsed >= cutoff:
                    evicted_early += 1
            seg.unlink(missing_ok=True)
            # Mark on the FIRST deletion, not after the loop. `missing_ok=True`
            # suppresses only FileNotFoundError, so a permission or other OS error
            # on a later segment carries control out of this method -- and
            # _flush_batch SWALLOWS that (see its _maybe_rotate guard), so a marker
            # written after the loop would simply never happen while the earlier
            # deletions stand. Verify would then report a genesis chain break
            # against history that is legitimately retained, with nothing logged.
            # Guarded, so this still writes at most once per pass: no new cost.
            if not marked:
                self._mark_evicted()
                marked = True
        if evicted_early:
            logger.warning(
                "SEL size cap evicted %d sealed segment(s) newer than the %d-day "
                "retention window: audit evidence is being dropped early. Raise "
                "KIROCREW_SEL_MAX_BYTES or KIROCREW_SEL_BACKUP_COUNT for this host.",
                evicted_early,
                self._retention_days,
            )
            self._early_evictions += evicted_early
        # The marker is written inside the loop on the first deletion (above), so
        # a partial failure cannot leave deletions applied with no marker.

    def _prune_sealed_by_age(self, keep_days: int, *, count: bool = True) -> int:
        """Delete whole sealed segments whose NEWEST entry predates the cutoff.

        Caller MUST hold ``self._lock``. Returns the number of entries removed
        (summed over dropped segments). Only sealed segments are eligible -- the
        active file is never touched here, so the live chain tail is preserved
        regardless of retention. A segment is dropped only when its most-recent
        entry is older than the cutoff (a segment straddling the boundary is kept
        intact, so its internal chain is never severed). ``keep_days<=0`` disables
        age pruning.

        The stamp must be AUTHENTIC as well as aged. The record carrying it is
        self-HMAC checked before it may authorise a delete, because the stamp is
        otherwise attacker-controlled data deciding whether audit history is erased
        -- and the erasure would then mark itself evicted, which verify_integrity()
        reads as licence to re-baseline. Failing that check keeps the segment.

        Survivors KEEP their numbers. Monotonic numbering means a deleted segment
        just leaves a gap, and a gap here means only "older history aged out" -- so
        there is no renumber pass, no temp-name staging, and no interrupted-renumber
        residue class. That apparatus existed solely because the shift-rename layout
        needed the numbered run to stay contiguous.

        Caller MUST also hold the cross-process ``_seal_lease``. Prune-vs-prune is
        harmless -- two processes delete the same aged prefix -- but prune-vs-SEAL is
        not: a rival that prunes and then seals REUSES the number, so a path proved
        aged here can name a new, fully-populated segment by the time it is unlinked.
        Both callers hold the lease: rotation is already inside it, and ``prune()``
        takes it around Stage 1 and skips that stage when it is unavailable.

        ``count=False`` skips the per-segment entry tally (``_entry_count_of``
        streams the whole ~100 MB segment). The rotation path discards the return,
        so it passes False to avoid that full-segment read on the writer thread
        under _lock; prune() uses the count and keeps the default.
        """
        if keep_days <= 0:
            return 0
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)
        removed = 0
        for seg in self._sealed_segments():  # oldest (lowest number) first
            try:
                empty = seg.stat().st_size == 0
            except OSError:
                empty = False
            if empty:
                # CHAIN-TRANSPARENT, so step past it rather than stopping here. This
                # is the one exception to the prefix rule below, and it is sound for
                # the same reason the rule exists: an empty segment contributes no
                # entries, so nothing chains THROUGH it -- the next segment's first
                # `prev_hash` already refers to the tip of the newest segment that
                # holds records. Skipping it therefore opens no mid-chain seam.
                #
                # Stopping here instead would defer retention FOREVER, because
                # `_drop_empty_claims` deliberately no longer deletes these (a
                # zero-byte segment may be a truncated real one, and destroying it
                # erases the only evidence). Measured: with a zero-byte segment at
                # the oldest position, this loop removed 0 of 2 genuinely aged and
                # authentic segments. That would trade a silent data-loss path for a
                # silent denial of the retention feature -- both bad, and neither
                # necessary.
                #
                # It is NOT unlinked here either. Retention deleting it would restore
                # exactly the erasure this skip exists alongside; it stays on disk as
                # the evidence, and verify_integrity() keeps reporting it until an
                # operator resolves it.
                continue
            newest_rec = self._newest_record_of(seg)
            newest_ts = newest_rec.get("timestamp") if newest_rec is not None else None
            parsed_ts = self._parse_ts(newest_ts) if isinstance(newest_ts, str) else None
            # Fail CLOSED on an unparseable/absent stamp: drop only when we can
            # prove the segment is older than the cutoff. Parsing (vs a raw string
            # compare) keeps a non-+00:00 offset correctly ordered; a None result
            # means "can't prove aged" -> keep, never delete real recent data.
            if newest_rec is None or parsed_ts is None or parsed_ts >= cutoff:
                # STOP at the first segment we cannot prove is aged: eviction has to
                # be a PREFIX operation. Segment timestamps are only weakly
                # monotonic (an NTP step backwards makes an older segment look
                # newer), so continuing could drop a MIDDLE segment -- and the
                # segment after the hole would then chain off a deleted entry.
                # Baseline relaxation covers only the OLDEST surviving entry, never
                # a mid-chain seam, so verify would report a permanent chain break.
                # Cost of stopping: one segment with an unparseable stamp defers
                # retention until it is gone, which is the right trade against
                # severing the chain.
                break
            if not self._record_is_authentic(newest_rec):
                # PROVING AGED IS NOT THE SAME AS TRUSTING THE PROOF. Everything
                # above establishes only that the stamp parses and reads older than
                # the cutoff -- and a stamp can be BOTH, while being forged. The
                # segment directory is agent-writable before the sensitive-path
                # floor lands, so a writer can edit the oldest segment's final
                # record to read older than any cutoff. The fail-closed guard above
                # cannot see it: nothing failed to parse, so deletion proceeds down
                # the CORRECT path. That erases history and then authenticates the
                # erasure, because the unlink is followed by `_mark_evicted()` and
                # verify_integrity() treats a genuine marker as licence to adopt the
                # next segment's own prev_hash as its baseline -- so the chain reads
                # `integrity: ok` over records that were deleted on a forged stamp.
                #
                # So the stamp only authorises a delete when the record carrying it
                # is this install's own. Self-HMAC via the same digest the verify
                # walk recomputes; linkage remains verify_integrity's job.
                #
                # Fail closed the same way, and for the same reason as the
                # unparseable case: a segment we cannot authenticate defers
                # retention until an operator deals with it. That IS a cost -- a
                # corrupt-but-genuine oldest segment now holds retention open
                # indefinitely rather than aging out -- and it is the right side to
                # err on, because the opposite error deletes audit history and
                # reports success. Logged at ERROR precisely because it needs a
                # human: it means tampering, or a key that no longer matches the
                # records it signed.
                logger.error(
                    "SEL refusing to age-prune sealed segment %s: its newest record "
                    "reads older than the cutoff but does NOT authenticate against "
                    "this install's key. Treating the stamp as untrustworthy and "
                    "keeping the segment. Investigate for tampering, then run "
                    "`kirocrew security verify`.",
                    seg.name,
                )
                break
            if count:
                removed += self._entry_count_of(seg)
            seg.unlink(missing_ok=True)
            # Dropping the oldest sealed segment evicts (part of) the genesis
            # prefix, so mark it -- this is exactly the case
            # bool(_sealed_segments()) misses once age-pruning removes ALL sealed
            # segments.
            self._mark_evicted()
            logger.info(
                "SEL dropped sealed segment %s (older than %d days)", seg.name, keep_days
            )
        return removed

    @staticmethod
    def _parse_ts(ts: str) -> datetime | None:
        """Parse a stored ISO timestamp to an aware UTC datetime for ORDERING.

        Returns None if the value is unparseable. The write path emits
        ``datetime.now(tz=timezone.utc).isoformat()`` (fixed ``+00:00``), so a
        lexicographic compare happens to sort right today. But log() takes a
        caller-built timestamp, so a future emitter using ``Z`` or a local offset
        would mis-order under a raw string compare — and that ordering decides which
        segment age-prune deletes. Parsing to datetime makes the compare
        offset-correct regardless of format: ``Z`` is normalized and a naive stamp
        is assumed UTC. On the destructive age-prune path an unparseable stamp must
        FAIL CLOSED (keep the segment) — never map it to a sentinel that reads as
        "ancient, delete", which would unlink a segment holding real recent data.
        Callers guard on None.
        """
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _count_entries_in(fh: IO[bytes]) -> int:
        """Non-blank line count read from an already-open handle.

        Handle-based so a caller that pinned a segment under ``_lock`` counts the
        inode it pinned, not whatever the path names by the time the count runs.
        Degrades to 0 on a read error; callers treat the count as a fail-loud floor
        rather than an exact figure, so 0 can only under-report, never fake clean.
        """
        try:
            fh.seek(0)
            return sum(1 for ln in _segment_lines(fh) if ln.strip())
        except OSError:
            return 0

    @staticmethod
    def _entry_count_of(path: Path) -> int:
        """Non-blank line count of *path* (entry count).

        Feeds prune's removed count, which is observational -- no caller gates on
        its exactness.

        Bounded on BOTH axes. It keeps its own read rather than calling
        :func:`_segment_lines` for a reason that is no longer about memory -- that
        helper now yields, so it is O(1) in aggregate too. What differs is the
        DEGRADATION: the helper raises on an over-cap line, whereas this returns the
        lines counted so far as a floor, which is what an observational count wants.
        The read streams (one line held at a time) AND caps each line at
        ``_SEGMENT_LINE_CAP``.

        The per-line cap is the part that was missing, and the reachability argument
        that previously excused it was wrong. It ran: `_prune_sealed_by_age` reads the
        newest record first (via `_newest_record_of`) and fails closed when no stamp
        parses, so a pathological segment never gets here. That only holds when the
        oversized
        line is the LAST one. A 6 MB line in the MIDDLE of a segment whose final
        line carries an ordinary aged stamp passes the gate cleanly (measured), and
        the old unbounded ``for ln in f`` then allocated it -- an audit segment is
        agent-writable before the sensitive-path floor lands, so that is a
        memory-exhaustion path into the gateway rather than a hypothetical.
        Opening through :func:`_open_segment` also picks up the ``O_NOFOLLOW`` and
        ``S_ISREG`` guards every other segment read already has.
        """
        try:
            with _open_segment(path) as fh:
                count = 0
                while True:
                    raw = fh.readline(_SEGMENT_LINE_CAP + 1)
                    if not raw:
                        return count
                    if len(raw) > _SEGMENT_LINE_CAP:
                        # Stop rather than allocate more. The count becomes a FLOOR,
                        # which is acceptable precisely because it is observational;
                        # the alternative is the exhaustion this cap exists to stop.
                        logger.error(
                            "SEL segment %s has a line over the %d-byte cap; "
                            "stopping the entry count at %d rather than reading it",
                            path.name,
                            _SEGMENT_LINE_CAP,
                            count,
                        )
                        return count
                    if raw.strip():
                        count += 1
        except OSError:
            return 0

    @staticmethod
    def _newest_record_of(path: Path) -> dict | None:
        """The last parseable entry in *path* as a dict, or None.

        Returns the whole RECORD rather than just its timestamp because a caller
        that deletes on the strength of the stamp must first be able to authenticate
        the record the stamp came from -- see ``_prune_sealed_by_age``. Callers that
        only want the stamp go through :meth:`_newest_timestamp_of`.

        Entries append in time order, so the newest is at the tail. Reads only
        the trailing chunk (backward 4 KB scan) rather than the whole segment, so
        age-pruning a 100 MB sealed segment doesn't load it fully into memory.
        That bound is enforced by ``_TIP_SCAN_MAX_BYTES``, exactly as in
        :meth:`_tip_hash_of`; on hitting the floor without a parseable record this
        returns None, which both callers treat as "cannot prove aged" -- so
        retention defers instead of deleting evidence it could not read.
        Callers order the returned value through ``_parse_ts`` (parsed to an aware
        UTC datetime), so a non-``+00:00`` offset a future writer might emit sorts
        correctly rather than mis-ordering under a raw string compare.
        """
        if not path.exists():
            return None
        try:
            with _open_segment(path) as f:
                f.seek(0, 2)
                pos = f.tell()
                if pos == 0:
                    return None
                # Floor the backward walk, and TRIM the buffer each step. Both are
                # required and they fix different faults. Without the floor a
                # segment containing no newline never yields a complete line, so
                # the held-back buffer grows to the whole file. Without the trim,
                # `buf` keeps every byte read so far and the split below re-splits
                # the entire accumulation on every 4 KB step -- O(n^2) CPU even
                # inside a bounded window. This runs on the writer thread under
                # `_lock`, so either one stalls all audit logging.
                scan_floor = max(pos - _TIP_SCAN_MAX_BYTES, 0)
                buf = b""
                while pos > scan_floor:
                    # Read exactly ONE new chunk per step (the bytes between the
                    # new and old positions), not pos->EOF: a bare f.read() here
                    # re-reads the whole tail every iteration -> O(n^2) I/O on a
                    # large segment. Prepending the fresh chunk keeps the same
                    # backward-growing window.
                    read_start = max(pos - 4096, scan_floor)
                    f.seek(read_start)
                    buf = f.read(pos - read_start) + buf
                    pos = read_start
                    parts = buf.split(b"\n")
                    if pos > 0:
                        # parts[0] may be truncated at the chunk boundary, so it
                        # isn't known-complete until we've read to BOF. Hold back
                        # ONLY that element -- keeping all of `buf` is the O(n^2).
                        buf = parts[0]
                        complete = parts[1:]
                    else:
                        buf = b""
                        complete = parts
                    for line in reversed(complete):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            if not isinstance(record, dict):
                                # Exactly the class the AttributeError catch below
                                # was already handling -- valid json that is not an
                                # object has no fields to read. Raised INTO that
                                # handler rather than returned, so the
                                # STOP-at-the-newest-unparseable-record reasoning
                                # and its log stay in one place.
                                raise AttributeError("segment record is not an object")
                            return record
                        except (json.JSONDecodeError, AttributeError, ValueError, RecursionError):
                            # ValueError/RecursionError widen this in the SAME
                            # direction the arm already takes -- return None, which both
                            # callers read as "cannot prove aged" and KEEP. A nesting
                            # bomb raises RecursionError (NOT a ValueError -- measured)
                            # and an over-4300-digit integer raises a plain ValueError
                            # (not a JSONDecodeError -- measured); both escaped here and
                            # out through _maybe_rotate. JSONDecodeError is listed
                            # explicitly even though it IS a ValueError, so the
                            # relationship stays readable rather than implied.
                            #
                            # STOP at the newest unparseable record; do NOT keep
                            # scanning backwards. Entries append in time order, so
                            # the next record back is OLDER -- returning its stamp
                            # reports a segment whose newest data we could not read
                            # as being as old as data we could, and age pruning then
                            # deletes recent forensic history. A crash truncated
                            # mid-write leaves exactly this shape. None is
                            # fail-closed: both callers read it as "cannot prove
                            # aged" and KEEP the segment.
                            #
                            # AttributeError is caught for the same reason as
                            # before: a line that is VALID json but not an object
                            # (`123`, `true`, `"s"`, `null`) parses fine and then has
                            # no .get, and uncaught it escapes to _maybe_rotate and
                            # trips the rotation_failed path on every flush,
                            # permanently disabling rotation. Returning still catches
                            # it, so that escape stays closed.
                            logger.error(
                                "SEL found an unparseable newest record in %s. "
                                "Treating its age as unknown, so retention will "
                                "keep it rather than delete data it could not read.",
                                path,
                            )
                            return None
            if pos > 0:
                # Hit the scan floor with nothing parseable. Report loudly and
                # return None: both callers read None as "cannot prove aged", so
                # age-pruning KEEPS this segment rather than deleting a file whose
                # contents it never read.
                logger.error(
                    "SEL gave up resolving a timestamp in %s: no parseable record "
                    "in the last %d bytes. Treating its age as unknown, so "
                    "retention will keep it.",
                    path,
                    _TIP_SCAN_MAX_BYTES,
                )
            return None
        except OSError:
            return None

    @classmethod
    def _newest_timestamp_of(cls, path: Path) -> str | None:
        """ISO timestamp of the last parseable entry in *path*, or None.

        A thin projection of :meth:`_newest_record_of`, so the backward scan and its
        fail-closed reasoning live in one place. Kept for the caller that only ORDERS
        by the stamp and never deletes because of it: ``_evict_over_budget`` uses it
        purely to warn that the size cap is outrunning the retention window, and
        evicts either way because the size bound must still hold. A caller that
        DELETES on the strength of the stamp takes the record instead, so it can
        authenticate the record the stamp came from.
        """
        record = cls._newest_record_of(path)
        if record is None:
            return None
        stamp = record.get("timestamp")
        return stamp if isinstance(stamp, str) else None

    def _forward_event(self, callback: Callable[[dict], None], event: SecurityEvent) -> None:
        """Redact and forward a single event to the centralized sink."""
        try:
            # circular import: kiro_crew.security imports SecurityEvent/
            # SecurityEventLog from this module at top level, so redact() can
            # only be imported lazily here.
            from kiro_crew.security import redact

            def _redact_deep(obj: object) -> object:
                if isinstance(obj, str):
                    return redact(obj)
                if isinstance(obj, dict):
                    return {k: _redact_deep(v) for k, v in obj.items()}
                if isinstance(obj, (list, tuple)):
                    return type(obj)(_redact_deep(i) for i in obj)
                return obj

            callback(_redact_deep(asdict(event)))  # type: ignore[arg-type]
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
                platform_compat.restrict_to_owner(key_path)
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
        # Create the key ATOMICALLY: write the full 32 bytes to a temp file in
        # the same dir (0o600 from birth, so never briefly world-readable) and
        # os.replace() it into place — the same pattern prune() uses. A plain
        # os.open()+os.write() is NOT atomic: a crash or full-disk partial
        # write between the two calls leaves a 0-byte/short key on disk, which
        # the load-time length check above would then reject with a hard
        # RuntimeError on the NEXT boot — bricking every SecurityEventLog()
        # init until an operator manually removes the file. os.replace() makes
        # the key file visible only once it is complete, so KiroCrew itself can
        # never manufacture the hard-fail state; it fires solely on genuine
        # external corruption/tampering (which is what the error message is
        # written for).
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(key_path.parent), prefix=".sel_hmac_", suffix=".tmp"
        )
        try:
            # os.write() may return a SHORT count (fewer bytes than len(key)),
            # notably when storage is nearly full. Ignoring it would publish a
            # truncated key while the running process keeps signing with the
            # full in-memory key — the next boot then hard-fails on the short
            # on-disk key and the existing records can't be verified. Loop
            # until every byte is written; treat a 0-byte write as an error.
            mv = memoryview(key)
            while mv:
                n = os.write(tmp_fd, mv)
                if n == 0:
                    raise OSError("short write persisting SEL HMAC key (wrote 0 bytes)")
                mv = mv[n:]
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_path, str(key_path))
        except BaseException:
            if tmp_fd != -1:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # Re-enforce owner-only perms (POSIX 0o600 / Windows owner-only DACL).
        # Fail-SOFT by design: a read-only FS must not crash SecurityEventLog
        # init (see test_chmod_failure_is_swallowed). Unlike token_secret.py,
        # which treats the same failure as merely a warning too, the SEL key
        # tolerates chmod failure at startup.
        try:
            platform_compat.restrict_to_owner(key_path)
        except OSError:
            # Logs the key file PATH, never the key bytes.
            logger.warning(  # nosemgrep: python-logger-credential-disclosure
                "failed to set owner-only permissions on SEL HMAC key %s; "
                "file may be readable by other users",
                key_path,
                exc_info=True,
            )
        return key

    def _segment_dir(self) -> Path:
        """Directory holding sealed segments and the eviction marker."""
        return self._dir / _SEL_SEGMENT_DIR

    def _segment_path(self, index: int) -> Path:
        """Path of a sealed segment (index>=1) or the active file (index 0).

        Segment numbers are MONOTONIC: allocated once, never renamed. A higher
        number is therefore a NEWER segment -- the opposite of the shift-rename
        layout this replaced, where rotation aged higher suffixes toward the tail.
        Eviction consequently deletes the LOWEST numbers.
        """
        if index <= 0:
            return self._path
        return self._segment_dir() / f"{_SEL_FILE}.{index}"

    def _marker_token(self) -> str:
        """The value a legitimate eviction marker must contain.

        A domain-separated MAC under the SEL key, so the marker is authenticated by
        the same secret that signs the chain — and by a secret that deliberately
        lives OUTSIDE the log directory. Without this the marker was an
        unauthenticated touch-file that gated an integrity relaxation: the very
        actor the module's threat model names (write access to the log dir, no
        access to the key) could create it and then head-truncate a never-evicted
        log, after which verify adopted the surviving first entry's prev_hash as
        baseline and read clean. Domain separation keeps this MAC valueless as a
        chain entry hash and vice versa.
        """
        return hmac.new(
            self._hmac_key, b"kirocrew.sel.evicted.v1", hashlib.sha256
        ).hexdigest()

    def _marker_path(self) -> Path:
        """Path of the sticky 'genesis prefix evicted' marker (see constant)."""
        return self._segment_dir() / _EVICTED_MARKER_FILE

    def _mark_evicted(self) -> None:
        """Record that a sealed segment was deleted (genesis prefix evicted).

        Written via temp-file + ``os.replace`` rather than ``write_text``, and the
        reason is a symlink hazard rather than atomicity. ``write_text`` opens the
        marker path for truncation and FOLLOWS a symlink, so an agent that
        pre-places ``security_events.jsonl.evicted`` as a link to any file it wants
        destroyed gets that file truncated and overwritten with the marker token the
        next time rotation evicts a segment. That pre-placement is reachable: the
        marker name is not covered by the sensitive-path floor until this feature
        lands. ``os.replace`` acts on the NAME, so it swaps the symlink itself out
        and never opens the target. Atomicity comes free with it, which also removes
        the torn-marker case where a crash mid-write leaves bytes that fail the MAC
        and read as tampering.

        Best-effort: a marker-write failure must never block rotation/prune, so
        the OSError is swallowed. Worst case the marker is missing and verify
        enforces the genesis anchor on a genuinely-evicted host, which reads as a
        spurious valid<total (fail-loud) until the next real eviction re-writes it.
        """
        try:
            atomic_write(
                self._marker_path(),
                self._marker_token(),
                restrict_to_owner=True,
                restrict_on_error="warn",
            )
        except OSError:
            logger.warning("SEL could not write eviction marker", exc_info=True)

    def _has_evicted(self) -> bool:
        """True if an AUTHENTIC eviction marker is present.

        Fails CLOSED: a missing, unreadable, empty, or non-matching marker returns
        False, which makes verify enforce the genesis anchor. That is the safe
        direction — on a forged marker the head-truncation it was meant to conceal
        surfaces as a chain break, and on a corrupt-but-genuine marker a
        legitimately-evicted host merely false-alarms (loud, recoverable) instead of
        going quiet. Constant-time compare so the token cannot be probed byte by
        byte through repeated verify calls.

        Opened with exactly the guards :func:`_open_segment` uses, and for the same
        reason :meth:`_mark_evicted` writes via replace: the marker path is
        agent-writable before this feature's sensitive-path family lands. Each guard
        covers a plant the others permit. ``O_NOFOLLOW`` refuses a symlinked final
        component. ``O_NONBLOCK`` is what makes the next check reachable at all,
        because opening a fifo read-only BLOCKS until a writer appears, so without it
        a planted fifo hangs inside ``os.open`` and the byte cap below never runs.
        ``S_ISREG`` on the descriptor then refuses the fifo, device or directory that
        ``O_NOFOLLOW`` allows. The cap bounds a large regular file, which passes all
        three. Windows has no ``O_NOFOLLOW`` or ``O_NONBLOCK``, so both degrade to 0
        there and ``S_ISREG`` plus the cap are what remain.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self._marker_path(), flags)
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                logger.error(
                    "SEL eviction marker %s is not a regular file — refusing to read "
                    "it, so verify_integrity() keeps enforcing the genesis anchor.",
                    self._marker_path().name,
                )
                return False
            raw = os.read(fd, _MARKER_READ_CAP)
        except OSError:
            return False
        finally:
            os.close(fd)
        found = raw.decode("utf-8", "replace").strip()
        if not found:
            return False
        # `_macs_equal`, not `hmac.compare_digest` directly: the decode above uses
        # errors="replace", so an invalid byte anywhere in the marker yields U+FFFD
        # and compare_digest raises TypeError on the non-ASCII str -- out of here,
        # through `eviction_plausible = self._has_evicted()` in verify_integrity,
        # and out of the events surface. A non-ASCII marker cannot be authentic
        # (the token is a hexdigest), so it takes the not-authentic path below.
        if not _macs_equal(found, self._marker_token()):
            logger.error(
                "SEL eviction marker %s is present but not authentic — refusing to "
                "relax the genesis anchor. Either it was not written by this "
                "install's key, or it was tampered with. verify_integrity() will "
                "enforce genesis, so a truncated chain reports valid<total.",
                self._marker_path().name,
            )
            return False
        return True

    def _discarded_tip_path(self) -> Path:
        """Path of the single-hash post-discard attribution record (see constant)."""
        return self._segment_dir() / _DISCARDED_TIP_FILE

    def _discarded_tip_token(self, tip: str) -> str:
        """The value a legitimate discarded-tip record must contain for *tip*.

        MACs the tip itself, not a fixed string: a token that did not bind the
        value would authenticate ANY tip once written, which is the blanket
        relaxation this record exists to avoid. Domain-separated from the eviction
        marker's token and from a chain entry hash, so no MAC this module writes is
        valuable in another's position.
        """
        return hmac.new(
            self._hmac_key,
            b"kirocrew.sel.discarded-tip.v1|" + tip.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _record_discarded_tip(self, tip: str) -> None:
        """Record the tip a backup_count=0 discard destroyed, or clear the record.

        Written through :func:`atomic_write` for exactly the symlink reason
        :meth:`_mark_evicted` documents: this name is agent-writable, and a
        truncating open would follow a planted link.

        Best-effort in the SAFE direction. A failure here leaves no record, so a
        later concurrent append reports the chain break it reports today --
        fail-loud, never fail-quiet. That asymmetry is why this is allowed to
        swallow the OSError while the deletions above are not.
        """
        if not tip:
            # Nothing was destroyed, so there is nothing to attribute. Clear any
            # record from an earlier discard rather than leaving a stale hash
            # authenticated: its tip can no longer be reached by a live append.
            try:
                self._discarded_tip_path().unlink(missing_ok=True)
            except OSError:
                logger.warning("SEL could not clear the discarded-tip record", exc_info=True)
            return
        try:
            atomic_write(
                self._discarded_tip_path(),
                f"{self._discarded_tip_token(tip)} {tip}",
                restrict_to_owner=True,
                restrict_on_error="warn",
            )
        except OSError:
            logger.warning("SEL could not write the discarded-tip record", exc_info=True)

    def _authentic_discarded_tip(self) -> str:
        """The AUTHENTIC discarded tip, or ``""``.

        Fails CLOSED at every step -- missing, unreadable, non-regular, empty,
        malformed, or failing the MAC all return ``""``, which leaves verify
        enforcing the genesis anchor. Opened with the same ``O_NOFOLLOW`` /
        ``O_NONBLOCK`` / ``S_ISREG`` / byte-cap guards as :meth:`_has_evicted`, and
        for the same reason: the path is agent-writable, and a planted fifo would
        otherwise hang verify inside ``os.open``.

        The MAC is checked through :func:`_macs_equal`, never
        ``hmac.compare_digest`` directly -- the decode below uses
        ``errors="replace"``, so any invalid byte yields U+FFFD and a direct
        compare would raise TypeError out of verify.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self._discarded_tip_path(), flags)
        except OSError:
            return ""
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                logger.error(
                    "SEL discarded-tip record %s is not a regular file — refusing to "
                    "read it, so verify_integrity() keeps enforcing the genesis anchor.",
                    self._discarded_tip_path().name,
                )
                return ""
            raw = os.read(fd, _MARKER_READ_CAP)
        except OSError:
            return ""
        finally:
            os.close(fd)
        found = raw.decode("utf-8", "replace").strip()
        parts = found.split(" ", 1)
        if len(parts) != 2 or not parts[1]:
            return ""
        token, tip = parts[0], parts[1].strip()
        if not _macs_equal(token, self._discarded_tip_token(tip)):
            logger.error(
                "SEL discarded-tip record %s is present but not authentic — refusing "
                "to attribute a chain break to a discard. verify_integrity() will "
                "enforce genesis, so a truncated chain reports valid<total.",
                self._discarded_tip_path().name,
            )
            return ""
        return tip

    def _sealed_segments(self) -> list[Path]:
        """Existing sealed segments, OLDEST first (lowest number first).

        Numbers are monotonic and never reused while segments exist, so ascending
        numeric order IS chain order. Sorted NUMERICALLY, never lexically: `.10`
        must not sort between `.1` and `.2`.

        Gaps are NORMAL here and carry no alarm. Eviction deletes the lowest
        numbers, so a surviving set starting above 1 simply means older history
        aged out -- unlike the shift-rename layout, where a hole meant a lost
        middle segment and needed separate orphan accounting.
        """
        return [self._segment_path(i) for i in self._list_sealed_indices()]

    def _list_sealed_indices(self) -> list[int]:
        """Every sealed-segment number present on disk, ascending.

        Refuses to list THROUGH a planted link, and does so by returning nothing
        rather than by calling :meth:`_ensure_segment_dir`. The read paths reach
        here -- ``verify_integrity`` (documented read-only), ``recent`` (polled by
        the dashboard) and ``prune`` Stage 1 -- and that helper MUTATES: it
        unlinks the link and ``mkdir``s a real directory, and raises ``OSError``
        when the result is still not a directory. Calling it from a read would
        make verification write to disk and would raise into callers that have no
        handler, so the guard here is the non-mutating half: same refusal, no side
        effect. Rotation still repairs the link, because ``_rotate_now`` and
        ``_next_segment_index`` call ``_ensure_segment_dir`` before they get here.

        Fails CLOSED, matching :meth:`_has_evicted`: a link reads as "no sealed
        segments" rather than as the link target's contents. Without it a ``sel``
        link planted before this feature shipped would have ``iterdir`` enumerate
        the TARGET, so any ``security_events.jsonl.<n>`` sitting there -- another
        install's segment dir being the realistic aim -- would be surfaced by
        ``recent()`` as this log's audit events and DELETED by eviction and
        age-pruning, both of which unlink whatever this returns.
        """
        seg_dir = self._segment_dir()
        if platform_compat.is_link_or_junction(seg_dir):
            logger.error(
                "SEL segment dir %s is a symlink/junction — refusing to list "
                "through it, so no sealed segment is read or deleted outside the "
                "SEL directory. Rotation removes the link and creates a real "
                "directory.",
                seg_dir,
            )
            return []
        prefix = f"{_SEL_FILE}."
        indices: list[int] = []
        try:
            entries = list(seg_dir.iterdir())
        except OSError:
            return []
        for entry in entries:
            name = entry.name
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix):]
            # isascii() as well as isdigit(): isdigit() accepts Unicode digits that
            # int() either rejects ("\u00b2" -> ValueError) or silently folds onto a
            # real segment number ("\u0663" -> 3). _segment_path only ever writes
            # ASCII, so ASCII is the whole legitimate set.
            if not (suffix.isascii() and suffix.isdigit()):
                continue
            n = int(suffix)
            # CANONICAL POSITIVE integers only, and the ".0" half of that is a
            # data-loss guard rather than tidiness: _segment_path maps index<=0 to
            # the ACTIVE file by design, so a "security_events.jsonl.0" sitting in
            # this directory resolves to the live log and makes _evict_over_budget
            # and _prune_sealed_by_age unlink it. Measured: a planted ".0" with an
            # aged stamp deleted the active file and its 5 entries outright.
            # Nothing here writes ".0" -- _next_segment_index starts at 1 -- but an
            # operator, a partial restore or a pre-upgrade layout can leave one, and
            # the cost of accepting it is the current audit log.
            # The zero-padded forms are the second half: ".01" is a distinct FILE
            # from ".1" but parses to the same index, so it both inflates the
            # eviction budget (deleting one more real segment per roll, the same
            # shape as an empty claim) and makes every path operation act on ".1"
            # while ".01" is what was listed. Requiring suffix == str(n) rejects
            # ".0", ".00", ".01" and any other non-canonical spelling; a legitimate
            # segment is always written as str(int), so none is ever refused.
            if n < 1 or suffix != str(n):
                logger.warning(
                    "SEL ignoring non-canonical segment file %s in %s: only "
                    "positive unpadded integer suffixes are real segments",
                    name,
                    seg_dir,
                )
                continue
            indices.append(n)
        return sorted(indices)

    def _ensure_segment_dir(self) -> Path:
        """Return the sealed-segment directory, refusing a planted link.

        ``mkdir(exist_ok=True)`` FOLLOWS an existing symlink or junction, so an
        agent that plants ``sel`` as a link before this feature ships would have
        every sealed segment written to the link target -- outside the
        sensitive-path floor and somewhere it can still read them. The floor
        protects the registered path, not wherever that path happens to point.

        Same posture, and the same helpers, as the ``trust`` dir above: remove the
        LINK (never its target) and create a real directory in its place. If the
        link cannot be removed, refuse to rotate rather than seal audit history to
        an attacker-chosen location -- an un-rolled oversized log is recoverable,
        history written outside the floor is not.
        """
        seg_dir = self._segment_dir()
        if platform_compat.is_link_or_junction(seg_dir):
            logger.warning(
                "SEL segment dir %s is a symlink/junction — removing the link "
                "(planted before upgrade?) and creating a real directory",
                seg_dir,
            )
            platform_compat.unlink_link_or_junction(seg_dir)
        seg_dir.mkdir(parents=True, exist_ok=True)
        if not seg_dir.is_dir():
            raise OSError(errno.ENOTDIR, "SEL segment dir is not a directory", str(seg_dir))
        return seg_dir

    @contextmanager
    def _seal_lease(self) -> Iterator[bool]:
        """Yield True iff this process holds the exclusive SEAL lease.

        The atomic number claim stops two processes taking the SAME number, but it
        does not order the seal, and the ordering is what the chain depends on.
        Without this lease: A claims N, B claims N+1, B moves the active file onto
        N+1 first, appends recreate the active file, and A then moves that NEWER
        data onto N. A lower number would hold newer events -- so the chain reads
        out of order and eviction, which deletes the lowest numbers, would drop the
        newer history first. The ``FileNotFoundError`` branch in the seal does not
        save it, because by then the active file exists again.

        This is narrower than the lease monotonic numbering retired: that one had
        to span a shift-rename sequence over every segment. This spans only claim +
        replace, so no existing segment is ever inside it.

        NON-BLOCKING on purpose. Rotation is best-effort, so losing the race means
        "skip this roll" -- never "wait on the writer thread" while holding
        ``_lock``. The oversized file rolls on a later flush.
        """
        lock_path = self._segment_dir() / _SEAL_LOCK_FILE
        fd: int | None = None
        try:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError:
                # Cannot even create the lease. Refuse to seal rather than seal
                # unserialized: an un-rolled file is recoverable, a mis-ordered
                # chain is not.
                logger.warning("SEL cannot open seal lease; skipping rotation")
                yield False
                return
            if not platform_compat.try_acquire_lock(fd, exclusive=True):
                logger.debug("SEL seal lease held by another process; skipping roll")
                yield False
                return
            try:
                yield True
            finally:
                platform_compat.release_lock(fd)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _next_segment_index(self) -> int:
        """Claim the next segment number ATOMICALLY and return it.

        Creates the target with ``O_CREAT|O_EXCL`` so the winner owns the number.
        ``max(existing)+1`` alone would be a read-modify-write: two processes
        rolling at once would compute the same number, and the second rename would
        destroy the segment the first had just sealed. Retrying on
        ``FileExistsError`` is what makes the allocation safe.

        The atomic claim is NOT on its own enough to order the roll -- see
        :meth:`_seal_lease`. Callers MUST hold that lease across claim + replace.

        The caller MUST either rename onto the returned path or unlink it -- the
        claim is an empty placeholder until then, and an empty segment would read
        as a chain break.
        """
        self._ensure_segment_dir()
        existing = self._list_sealed_indices()
        n = (existing[-1] + 1) if existing else 1
        while True:
            try:
                fd = os.open(self._segment_path(n), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                n += 1
                continue
            os.close(fd)
            return n

    def _chain_paths(self, sealed: list[int]) -> list[Path]:
        """All log segments in chain order: oldest sealed → … → active.

        Takes the sealed numbers rather than listing them, because the caller must
        keep the listing it can re-compare against to detect a rival seal landing
        mid-snapshot (see :meth:`_walk_chain`). Building the paths from a second,
        independent listing would defeat that check.
        """
        return [*(self._segment_path(i) for i in sealed), self._path]

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

    @staticmethod
    def _tip_hash_of(path: Path) -> str:
        """Return the entry_hash of the last COMPLETE record in *path*, or "".

        A crash mid-append can leave a truncated/partial final line. A blanket
        ``except: return ""`` would let one corrupt tail line silently restart the
        HMAC chain from genesis — severing the tamper-evidence link and masking
        exactly the corruption the chain exists to detect. Instead we scan backward
        and skip an unparseable trailing line to chain from the last COMPLETE valid
        record; we only return "" when the file is genuinely empty/absent or
        contains no parseable record at all (nothing to chain from). Skipped
        corrupt tail lines are logged so the integrity concern is surfaced, not
        hidden.

        Takes an explicit *path* rather than reading ``self._path`` so the same
        recovery logic serves every segment: after a rotation the active file can
        be empty while the real tip lives in ``.1`` (see :meth:`_read_last_hash`).
        """
        if not path.exists():
            return ""
        try:
            with _open_segment(path) as f:
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
                # Floor the backward walk. Without it a segment containing no
                # newline never yields a complete line, so `buf` below accumulates
                # the entire file into memory -- reached from the constructor, and
                # not covered by _open_segment, which refuses symlinks and fifos
                # but passes a large REGULAR file.
                scan_floor = max(pos - _TIP_SCAN_MAX_BYTES, 0)
                buf = b""
                skipped_corrupt = False
                while pos > scan_floor:
                    read_start = max(pos - 4096, scan_floor)
                    f.seek(read_start)
                    buf = f.read(pos - read_start) + buf
                    pos = read_start
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
                        except (json.JSONDecodeError, ValueError, RecursionError):
                            # RecursionError closes the gap this site's own sibling
                            # comment in `recent()` already named ("the RecursionError
                            # that sibling still lacks"). It is NOT a ValueError
                            # (measured False), so a nesting-bomb line escaped here --
                            # and it is reachable on the PRUNE path: prune() calls
                            # `_read_last_hash()` -> `_tip_hash_of` after rewriting the
                            # active file, so widening prune()'s own handler alone just
                            # moved the escape to this line rather than removing it.
                            # Same direction as the arm already takes: skip backward to
                            # the last complete record, never reset to genesis.
                            skipped_corrupt = True
                            logger.warning(
                                "SEL: skipping unparseable audit-log line while "
                                "resolving chain tip in %s; chaining from the last "
                                "complete record instead of resetting to genesis",
                                path,
                            )
                            continue
                        if not isinstance(data, dict):
                            # Parseable JSON but not a record object — treat as
                            # corrupt and keep scanning backward.
                            skipped_corrupt = True
                            logger.warning(
                                "SEL: skipping non-object audit-log line while "
                                "resolving chain tip in %s",
                                path,
                            )
                            continue
                        if skipped_corrupt:
                            logger.warning(
                                "SEL: recovered chain tip from an earlier complete "
                                "record after a corrupt/truncated tail in %s",
                                path,
                            )
                        return data.get("entry_hash", "")
            if pos > 0:
                # Hit the scan floor without finding one complete record. Report it
                # loudly and treat THIS segment as having no tip: _read_last_hash
                # then continues to older segments, so the chain is not silently
                # re-anchored to genesis, and verify_integrity independently reports
                # the segment as unverifiable.
                logger.error(
                    "SEL gave up resolving a chain tip in %s: no complete record in "
                    "the last %d bytes. Treating the segment as having no tip; it "
                    "will not verify.",
                    path,
                    _TIP_SCAN_MAX_BYTES,
                )
                return ""
            # No parseable record anywhere in the file — nothing to chain from.
            return ""
        except OSError:
            logger.warning(
                "SEL: failed to read chain tip from %s", path, exc_info=True
            )
            return ""

    def _read_last_hash(self) -> str:
        """Seed the chain tip from the newest segment that has one.

        Normally the active file holds the tip. Immediately after a rotation the
        active file can be empty (or absent) while the tip lives in ``.1`` — or,
        degenerately, in an older sealed segment — so walk newest→oldest and take
        the first tip found. Restarting the chain from genesis instead would make
        the next entry's prev_hash disagree with the sealed segment's final entry,
        which verify_integrity reports as a break at the rotation seam.

        The fallback is scoped by ROLE, because only the active file can legitimately
        have no tip. A SEALED segment with none was truncated after sealing, and
        falling through to an older tip would re-anchor the chain across the gap and
        leave the omission invisible. That case is logged here and counted as
        unverifiable by the verify walk, so integrity cannot read clean over it.
        """
        for path in (self._path, *reversed(self._sealed_segments())):
            tip = self._tip_hash_of(path)
            if tip:
                return tip
            if path != self._path and path.exists():
                logger.error(
                    "SEL sealed segment %s yields no chain tip; seeding the tip from "
                    "an older segment. Its history is not recoverable and "
                    "verify_integrity() will report it as unverifiable.",
                    path,
                )
        return ""

    def _compute_hash(self, event: SecurityEvent) -> str:
        # Hash over all fields except entry_hash itself
        d = asdict(event)
        d.pop("entry_hash", None)
        payload = json.dumps(d, sort_keys=True).encode()
        return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()

    def _record_is_authentic(self, event: dict) -> bool:
        """True iff *event*'s ``entry_hash`` is this install's MAC over its fields.

        The same digest the verify walk recomputes, over a dict read back from disk
        rather than a dataclass. Self-HMAC only: it proves the record was written by
        a holder of this key and has not been edited, and says nothing about the
        record's LINKAGE, which is verify_integrity's job. Compared in constant time.
        """
        candidate = dict(event)
        stored = candidate.pop("entry_hash", "")
        if not isinstance(stored, str) or not stored:
            return False
        payload = json.dumps(candidate, sort_keys=True).encode()
        expected = hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
        # `_macs_equal` also rejects a non-ASCII `stored`. The isinstance guard
        # above covers a non-str entry_hash but NOT `{"entry_hash": "\u00e1..."}`,
        # which is valid JSON on a disk line and made compare_digest raise
        # TypeError -- reaching here from `recent()`'s sealed-segment auth filter,
        # so a single planted line crashed the events listing.
        return _macs_equal(stored, expected)

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
        """
        if self._sync:
            self._flush_batch([event], raise_on_error=critical)
            return
        if critical:
            # Preserve chain order: drain the async backlog, then write this
            # event inline so PermissionError/OSError propagates to the caller.
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
                resources=resources[:_MAX_ARG_LEN] if resources else "",
                error=error[:_MAX_ARG_LEN] if error else "",
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

        On-disk SEL records are NOT redacted by the writer, and the persisted
        HMAC chain signs the bytes as-written, so the operation/resources/reason
        are redacted HERE (before ``log``) via ``redact_via_context`` — a command
        body or path that tripped governance must not leak a credential into the
        audit log.

        Pass ``critical=True`` when the caller enforces "audit-or-deny" for a
        GOVERNED decision (e.g. a governed transport-start allow): the event is
        written SYNCHRONOUSLY and a persistence failure (unwritable SEL file,
        full disk) is re-raised, so the caller can refuse the action rather than
        proceed unaudited. Without it the write is enqueued to the background
        writer, which swallows persistence failures — fine for best-effort audits
        (e.g. an ungoverned allow) but NOT for audit-or-deny.
        """
        # circular import: sel.py is imported very early -- security.py:33 does
        # `from kiro_crew.sel import SecurityEvent, SecurityEventLog` at module
        # scope -- and `platform.context` reaches security in turn, so a
        # module-scope import here re-enters this module while it is still
        # initialising. Measured: adding it at module scope raises "cannot import
        # name 'SecurityEvent' from partially initialized module 'kiro_crew.sel'".
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
                operation=safe_operation[:_MAX_ARG_LEN],
                outcome=outcome,
                resources=safe_item[:_MAX_ARG_LEN],
                metadata={
                    "scope": scope,
                    "rule": rule,
                    "layer": layer,
                    "reason": safe_reason[:_MAX_ARG_LEN],
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
        # circular import: same cycle as the sibling above -- security.py:33
        # imports SecurityEvent/SecurityEventLog from this module at top level and
        # `platform.context` reaches security, so this cannot be hoisted.
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
                operation=chokepoint[:_MAX_ARG_LEN],
                outcome="blocked" if failed_closed else "degraded",
                resources="",
                metadata={
                    "scope": scope,
                    "app": app,
                    "reason": safe_reason[:_MAX_ARG_LEN],
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
                resources=resources[:_MAX_ARG_LEN] if resources else "",
                error=error[:_MAX_ARG_LEN] if error else "",
            ),
            critical=critical,
        )

    def verify_integrity(self) -> tuple[int, int]:
        """Verify the HMAC chain across ALL segments. Returns (total, valid).

        Rotation seals old data into numbered segments but keeps ONE continuous
        HMAC chain (see _maybe_rotate). Verification therefore walks every
        segment oldest→newest (sealed .N … .1, then the active file) as a single
        stream. An *internal* segment seam (both sides retained) chains
        unbroken, so it validates exactly like an in-file transition.

        The one boundary that cannot be linkage-checked is the OLDEST surviving
        entry: rotation/retention may have evicted whole older segments, so its
        prev_hash can legitimately reference an entry no longer on disk. We
        therefore adopt that first entry's prev_hash as the chain baseline
        rather than forcing it to "" (which would be a false "chain break at
        entry 1" on every host that has rotated past backup_count). Its own
        self-HMAC is still verified, so content tampering at the boundary is
        caught (prev_hash is inside the HMAC payload); what a trimmed prefix
        cannot prove is the absence of malicious head-truncation — an inherent
        limitation of bounded retention without an external tip anchor.
        """
        self.flush()  # ensure all queued events are on disk before verifying
        # One walk, no retry loop. The walk pins each segment by an OPEN FILE
        # HANDLE taken under _lock (see _walk_chain), so a concurrent rotation
        # cannot rebind or remove what verify is reading — which is what the old
        # retry-on-OSError loop was working around, and which it could not
        # actually detect (a rename leaves every snapshotted PATH existing, just
        # pointing at a different inode, so no OSError was ever raised).
        return self._walk_chain()

    def _walk_chain(self) -> tuple[int, int]:
        """Pin the segment set under _lock by open handle, then walk the chain.

        Returns (total, valid).

        The handles are the correctness mechanism, not an optimization. Snapshotting
        PATHS and reading them after releasing the lock is unsound: a concurrent
        roll shifts ``.k`` → ``.k+1`` and reseals the active file as ``.1``, so every
        snapshotted path still EXISTS but now names a different inode. The walk then
        reads a set that is internally chain-adjacent while silently omitting the
        segment that was renamed out from under it — and because the sticky eviction
        marker suppresses the genesis-anchor check, the omission does not even
        surface as a break. Measured on 30 entries across 7 segments: the walk
        returned ``total=26 valid=26`` (``integrity: ok``) with all 30 entries still
        on disk. An open handle follows the inode, so a rename cannot rebind it and
        an unlink cannot pull it away mid-read.

        This is also why there is no retry loop any more: the failure it retried on
        (OSError from a vanished path) was never the failure that actually occurred.
        """
        # Open every segment under _lock so the set of INODES verify walks is fixed
        # atomically with respect to rotation IN THIS PROCESS, which takes the same
        # lock. Only the open() calls happen here — no file content is read under the
        # lock, so the heavy IO still happens outside it and verify never blocks the
        # writer. open() doubles as the existence check the old `p.exists()` filter
        # did, and unlike that filter it cannot be invalidated a moment later.
        #
        # `_lock` is a threading.Lock, so it orders nothing against ANOTHER writer
        # process, and the seal it would need to exclude is exactly what a rival
        # process does under the cross-process `_seal_lease`. Left unhandled that is
        # a SILENT hole rather than a loud one: a rival seal renames the active file
        # onto a fresh number and only recreates it on the next append, so verify
        # opens the active path in that window, gets ENOENT, cannot lstat it, and
        # takes the ordinary "no active file yet" branch below — while the entries
        # that were in it now live in a segment that was NOT in this listing. Every
        # path we did open then validates and `total == valid` reports
        # `integrity: ok` over a chain missing a whole segment, which is precisely
        # the failure this log exists to detect.
        #
        # Fixed by taking the snapshot until it is STABLE: re-list after opening and
        # redo if anything moved. Stability is judged by IDENTITY, not by the sealed
        # number set — numbering is only monotonic while a segment survives, so a
        # prune that empties the set lets the next seal REUSE a number and the set
        # can match while naming a different file (see _snapshot_drift).
        # Deliberately NOT by holding `_seal_lease` here — that lease is
        # non-blocking BY DESIGN so rotation can skip a roll rather than wait, and a
        # reader holding it would both block the writer and turn every concurrent
        # roll into a verify failure. Re-listing plus one fstat per segment costs no
        # more than a readdir and leaves rotation untouched. This is also not the
        # retry loop that was removed: that one retried on OSError from a vanished
        # path, a signal that never fired, whereas this keys on the snapshot MOVING.
        handles: list[tuple[Path, IO[bytes]]] = []
        unreadable: list[Path] = []
        drifted: list[Path] = []
        try:
            with self._lock:
                sealed_before: list[int] = []
                for _attempt in range(_VERIFY_SNAPSHOT_ATTEMPTS):
                    # Drop a torn attempt's handles before re-taking the snapshot.
                    for _p, fh in handles:
                        try:
                            fh.close()
                        except OSError:
                            pass
                    handles, unreadable = [], []
                    pinned: dict[Path, tuple[int, int]] = {}
                    sealed_before = self._list_sealed_indices()
                    chain = self._chain_paths(sealed_before)
                    for p in chain:
                        try:
                            handles.append((p, _open_segment(p)))
                        except OSError:
                            # Distinguish ABSENT from PRESENT-BUT-UNREADABLE. Absent is
                            # ordinary (no active file yet, or a segment evicted between
                            # listing and opening) and contributes nothing. Present but
                            # unopenable is audit history we cannot verify -- a
                            # permission change, an I/O error, or the non-regular-file
                            # refusal in _open_segment. Skipping that silently would let
                            # total == valid report `integrity: ok` while history is
                            # missing, which is the exact failure this log exists to
                            # detect. lstat, not exists(), so a dangling symlink counts
                            # as present rather than vanishing.
                            try:
                                os.lstat(p)
                            except OSError:
                                continue
                            logger.error(
                                "SEL cannot read segment %s; counting it as UNVERIFIED "
                                "so integrity cannot read clean",
                                p,
                                exc_info=True,
                            )
                            unreadable.append(p)
                            continue
                        st = os.fstat(handles[-1][1].fileno())
                        pinned[p] = (st.st_dev, st.st_ino)
                    drifted = self._snapshot_drift(sealed_before, pinned)
                    if not drifted:
                        break
                else:
                    # Still losing the race after the last attempt. Count every path the
                    # snapshot could not pin cleanly as UNVERIFIED so `total > valid`
                    # reports loud, rather than letting the omission read clean.
                    for p in drifted:
                        logger.error(
                            "SEL segment %s changed under verify while it was "
                            "snapshotting; counting it as UNVERIFIED so integrity "
                            "cannot read clean",
                            p.name,
                        )
                        unreadable.append(p)
                # Relax the genesis anchor ONLY when the sticky marker proves a
                # segment was actually deleted (evicting the genesis prefix). Gating
                # on the marker alone — not bool(_sealed_segments()) — is deliberate:
                # a rotated-but-never-evicted host (e.g. backup_count high enough
                # that nothing has dropped) still holds its genesis entry in the
                # OLDEST sealed segment, so the chain MUST anchor at genesis there;
                # relaxing on mere segment-existence would let an attacker
                # head-truncate that oldest segment's genesis prefix undetected. The
                # marker is set on EVERY real eviction path -- size-cap overflow in
                # _evict_over_budget, age-drop in _prune_sealed_by_age, and the
                # backup_count=0 prefix discard in _discard_leased -- and cleared
                # only on a backup_count=0 genesis re-anchor, so it is the precise
                # signal for "genesis legitimately gone." _discard_leased was the
                # gap: it deleted a prefix and marked nothing, so a partial discard
                # left this check enforcing genesis against a surviving segment
                # whose prev_hash named an entry that path had deleted. Its CONTENTS
                # are authenticated
                # with the SEL key (see _has_evicted), so an actor with write access
                # to the log directory but no key cannot forge the relaxation.
                # NOT conjoined with max_bytes>0: the marker already implies a real
                # eviction happened (all three _mark_evicted sites are
                # rotation-only, reached under _lock plus the seal lease), so
                # an operator who evicted under max_bytes>0 then set max_bytes=0
                # keeps the relaxed baseline (the physical chain still lacks its
                # genesis prefix); an added max_bytes>0 term would re-enforce genesis
                # and false-alarm.
                eviction_plausible = self._has_evicted()
            return self._walk_handles(handles, eviction_plausible, unreadable)
        finally:
            for _p, fh in handles:
                try:
                    fh.close()
                except OSError:
                    pass

    def _snapshot_drift(
        self, sealed_before: list[int], pinned: dict[Path, tuple[int, int]]
    ) -> list[Path]:
        """Paths whose snapshot is no longer trustworthy. Empty means STABLE.

        Comparing the sealed NUMBER set is not sufficient, because numbering is only
        monotonic while a sealed segment survives: ``_next_segment_index`` computes
        ``max(existing)+1`` and falls back to ``1`` on an empty set, so once the last
        segment is pruned the next seal REUSES a number. The set can therefore be
        identical across the snapshot while a number names a DIFFERENT file, and the
        handle we pinned still reads the unlinked inode -- so verify vouches for
        history that is no longer retained while never examining the history that is.
        Measured: an aged ``.1`` pruned and the active file resealed onto ``1`` gave
        ``total=6 valid=6`` (`integrity: ok`) over 6 evicted entries while the 3
        retained ones went unread.

        So identity, not the number, is the thing to compare. ``st_ino`` is what
        distinguishes them, and it is the same discriminator ``platform_compat`` uses
        for its bind-mount checks. Degrades safely where a filesystem reports no
        usable inode (every identity compares equal, leaving the number set as the
        only signal) rather than false-retrying.
        """
        drifted: list[Path] = []
        if self._list_sealed_indices() != sealed_before:
            # A number appeared or disappeared. Report the ones we never pinned; a
            # vanished one is already covered by its pinned handle.
            drifted.extend(
                self._segment_path(i)
                for i in self._list_sealed_indices()
                if i not in sealed_before
            )
        for p, ident in pinned.items():
            try:
                now = os.stat(p)
            except OSError:
                # Pinned but gone: an ordinary eviction. The handle still holds the
                # bytes, so this is not drift on its own.
                continue
            if (now.st_dev, now.st_ino) != ident:
                drifted.append(p)
        return drifted

    def _walk_handles(
        self,
        handles: list[tuple[Path, IO[bytes]]],
        eviction_plausible: bool,
        unreadable: list[Path] | None = None,
    ) -> tuple[int, int]:
        """Walk the HMAC chain over already-pinned handles. Runs OUTSIDE ``_lock``.

        Split out from :meth:`_walk_chain` so the lock scope is exactly the opening
        of the handles and nothing more — the reads and HMAC work below are the
        expensive part and must not hold the writer off.
        """
        unreadable = unreadable or []
        if not handles and not unreadable:
            return 0, 0
        # eviction_plausible (captured under the lock) gates the oldest-surviving-
        # entry baseline relaxation: it is legitimate only when the genesis prefix
        # was actually evicted (the authenticated sticky marker; see the gate at its
        # assignment). Do NOT re-add a max_bytes>0 conjunct: it regresses the
        # enable->evict->disable case. On a never-evicted log (non-rotated,
        # off-switch, or rotated-but-under-budget) the chain MUST anchor at genesis
        # (first entry's prev_hash==""); head-truncating the first entries then
        # surfaces as a chain break rather than being adopted as a new baseline.
        total = 0
        valid = 0
        prev_hash = ""
        first_entry = True
        # A backup_count=0 discard creates exactly ONE seam, and it can leave TWO
        # legitimately-anchored chains in the active file: the discarding process
        # re-anchors itself to genesis, while a concurrent O_APPEND writer's records
        # still link to the tip the truncate destroyed. Either can land first, so the
        # baseline adopted at entry 1 covers only one of them -- measured before this
        # fix, with the owner appending on both sides of the rival: owner-then-rival
        # gave ``total=2 valid=1`` and "SEL chain break at entry 2" with NO
        # attribution at all (the rival is not entry 1, so the branch below never ran
        # for it), and rival-then-owner attributed entry 1 and then broke at entry 2
        # on the owner's genesis anchor. Both are ordinary operation: the process that
        # rolled the log keeps logging afterwards.
        #
        # So each of the two anchor values a single discard can produce is adoptable
        # at most ONCE per walk. `discarded_tip` is None until read, so the record is
        # read at most once here; `seam_anchors_used` is what keeps this from becoming
        # a blanket relaxation -- one discard cannot manufacture a third anchor, and
        # with NO authenticated record nothing is adoptable and the break stands.
        discarded_tip = None
        seam_anchors_used = set()
        for path, fh in handles:
            # Walked in ONE pass, and `saw_record` is why. The empty-sealed-segment
            # test below used to be a pre-pass `any(ln.strip() for ln in lines)`, which
            # is safe only against a list: `_segment_lines` now YIELDS, so a pre-pass
            # would CONSUME the segment and leave this loop with nothing -- verify
            # would report 0 total / 0 valid for every sealed segment and call that
            # `integrity: ok`, which is the exact failure the test exists to catch.
            # Recording the observation during the pass cannot drift from what was
            # actually walked.
            saw_record = False
            try:
                fh.seek(0)
                # The iteration is INSIDE the try because the over-cap `OSError` now
                # surfaces mid-iteration rather than from the call: a generator body
                # does not run until it is advanced. Leaving the try around the call
                # alone would let EFBIG escape verify_integrity() entirely instead of
                # being folded into `unreadable`.
                for line in _segment_lines(fh):
                    line = line.strip()
                    if not line:
                        continue
                    saw_record = True
                    total += 1
                    try:
                        data = json.loads(line)
                        stored_hash = data.pop("entry_hash", "")
                        if first_entry:
                            # first_entry flips only on a PARSEABLE line, so a corrupt
                            # lead line (json.loads throws below) defers the baseline to
                            # the next parseable entry. That is correct, not a slip: an
                            # unparseable line can't supply a prev_hash to anchor on, and
                            # deferring avoids a manufactured "chain break at entry 2".
                            # The corrupt line still counts toward `total` (never `valid`).
                            if eviction_plausible:
                                # Oldest surviving entry after real eviction — its
                                # predecessor may be gone, so anchor the baseline to
                                # what it claims. Its own self-HMAC is still checked
                                # below (prev_hash is inside the HMAC payload).
                                prev_hash = data.get("prev_hash", "")
                            else:
                                # No eviction marker, so the chain MUST anchor at
                                # genesis -- EXCEPT for the one case a backup_count=0
                                # discard creates. That path truncates the active file
                                # instead of unlinking it precisely so a concurrent
                                # O_APPEND writer's record survives, and that record's
                                # prev_hash names the tip the discard destroyed. Adopt
                                # the baseline ONLY when the claim matches the
                                # authenticated record of that exact hash, so this is a
                                # single-value exception rather than the marker's
                                # blanket relaxation: any other truncation point yields
                                # a different prev_hash and still breaks.
                                #
                                # Read LAZILY, here, rather than captured under `_lock`
                                # alongside eviction_plausible: this branch is the only
                                # consumer, and a race with a concurrent discard fails
                                # in the safe direction -- a missing or superseded
                                # record simply leaves the break reported (fail-loud).
                                claimed = data.get("prev_hash", "")
                                if discarded_tip is None:
                                    discarded_tip = self._authentic_discarded_tip()
                                if claimed and claimed == discarded_tip:
                                    logger.warning(
                                        "SEL entry 1 links to the tip a backup_count=0 "
                                        "roll discarded; attributing the re-anchor to "
                                        "that discard rather than reporting a chain "
                                        "break. A concurrent writer appended after the "
                                        "discard and its predecessor is gone by design.",
                                    )
                                    prev_hash = claimed
                                # else: leave prev_hash == "" to enforce genesis.
                            # Whichever anchor the baseline ended up on is SPENT, so the
                            # seam below can only ever supply the OTHER one.
                            seam_anchors_used.add(prev_hash)
                            first_entry = False
                        if data.get("prev_hash", "") != prev_hash:
                            claimed = data.get("prev_hash", "")
                            if discarded_tip is None:
                                discarded_tip = self._authentic_discarded_tip()
                            # The seam, at ANY position rather than only at entry 1.
                            # Gated on an AUTHENTICATED discard record existing, which is
                            # what keeps a head truncation loud: without one, `""` and
                            # every other value alike stay unadoptable and the break is
                            # reported exactly as it is today. A truncation to any point
                            # OTHER than these two anchors yields some third prev_hash and
                            # still breaks even on a log that did discard.
                            if (
                                discarded_tip
                                and claimed in ("", discarded_tip)
                                and claimed not in seam_anchors_used
                            ):
                                logger.warning(
                                    "SEL entry %d re-anchors at the seam a "
                                    "backup_count=0 roll left; attributing it to that "
                                    "discard rather than reporting a chain break. The "
                                    "discarding process restarted at genesis while a "
                                    "concurrent writer's records still link to the "
                                    "destroyed tip, so both anchors are legitimate.",
                                    total,
                                )
                                seam_anchors_used.add(claimed)
                                prev_hash = claimed
                                # Fall through: the entry's own HMAC is still checked
                                # below, exactly as at the entry-1 adoption.
                            else:
                                logger.warning("SEL chain break at entry %d", total)
                                prev_hash = stored_hash
                                continue
                        payload = json.dumps(data, sort_keys=True).encode()
                        expected = hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
                        # `_macs_equal`, not `hmac.compare_digest` directly. This is
                        # the one compare site that still bypassed the wrapper, and
                        # THIS PR is what made it reachable: `_segment_lines` decodes
                        # with errors="replace", so any invalid byte in a segment
                        # becomes U+FFFD, `json.loads` accepts it inside a JSON string,
                        # and `stored_hash` arrives non-ASCII -- on which
                        # compare_digest raises TypeError (measured). It was caught by
                        # the `except Exception` below, so the observable was a
                        # MISCLASSIFICATION rather than a crash: a corrupted hash was
                        # logged as "SEL parse error" instead of an HMAC mismatch.
                        # Neither incremented `valid`, so the counts were already
                        # right; only the operator-facing reason was wrong.
                        if _macs_equal(stored_hash, expected):
                            valid += 1
                        else:
                            logger.warning("SEL HMAC mismatch at entry %d", total)
                        prev_hash = stored_hash
                    except Exception:
                        logger.warning("SEL parse error at entry %d", total)
            except OSError:
                # A genuine IO error on an already-open handle (bad disk, closed
                # descriptor), or an over-cap line — not a rotation race, which cannot
                # reach us here. Record it in `unreadable` so the fold-in below counts
                # it toward `total` and never toward `valid`. That append is
                # load-bearing: `total` is otherwise incremented only PER LINE, so
                # without it a segment we could not finish reading contributes 0 to
                # both counters and verify_integrity() reports `integrity: ok` while
                # history went unread. Lines already counted before the raise stay
                # counted, and the +1 here still forces valid < total.
                logger.warning("SEL could not read segment %s", path, exc_info=True)
                unreadable.append(path)
                continue
            if path != self._path and not saw_record:
                # A SEALED segment holding no record at all. Sealing only ever moves a
                # NON-EMPTY active file onto a number, so this is truncation, not the
                # benign post-rotation empty -- only the ACTIVE file can legitimately
                # be empty, which is why this is keyed on the path's role. Folded into
                # `unreadable` for exactly the reason given above: it contributes 0 to
                # both counters otherwise, so verify reports `integrity: ok` over
                # history that is gone.
                logger.error(
                    "SEL sealed segment %s holds no record; it was truncated or "
                    "emptied after sealing. Counting it as unverifiable.",
                    path,
                )
                unreadable.append(path)
        # A missing MIDDLE segment needs no separate accounting here. The walk
        # above covers every numbered segment on disk, so a hole makes the segment
        # after it chain off a deleted entry: that mismatch is counted into `total`
        # and not into `valid`, which is exactly the fail-loud valid<total a gap
        # must produce. Under the shift-rename layout the walk stopped at the first
        # gap, which is why stranded segments had to be found and folded in
        # separately or they would have vanished silently.
        if unreadable:
            # Fold into `total` and NEVER into `valid`, so an unverifiable segment
            # forces valid<total. One per segment is a fail-loud FLOOR, not an entry
            # count -- we could not read the file, so the true count is unknown. The
            # point is only that integrity must not read clean.
            logger.warning(
                "SEL integrity is UNVERIFIED for %d segment(s): %s",
                len(unreadable),
                ", ".join(p.name for p in unreadable),
            )
            total += len(unreadable)
        return total, valid

    def recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent events, newest first, across all segments.

        Reads the active file first, then sealed segments newest→oldest (.1, .2,
        …), so ``recent()`` still surfaces the latest events after a rotation
        boundary rather than only what happens to remain in the active file.
        """
        self.flush()  # surface any queued-but-unwritten events
        # Snapshot the segment list under _lock so a concurrent rotation/prune
        # re-number isn't observed mid-flight (same rationale as verify_integrity).
        # Best-effort newest-first display, not the integrity oracle: it reads the
        # segments in reverse numeric order and stops once it has `limit` entries.
        with self._lock:
            newest_first = [self._path, *self._sealed_segments()[::-1]]
        result: list[dict] = []
        forged = 0
        for path in newest_first:
            if not path.exists():
                continue
            sealed = path != self._path
            # Only the newest `remaining` lines of this segment can still be used:
            # `result` is filled newest-first and returns at `limit`. `deque(..., maxlen)`
            # consumes the generator holding at most that many lines, so a segment at
            # the 100 MB cap costs O(limit) instead of O(file) -- and it is built INSIDE
            # the `with`, because a generator left un-consumed until after the block
            # would be reading from a closed handle.
            #
            # The retained window is raw LINES, so a segment whose newest `remaining`
            # lines are blank, corrupt or forged yields fewer events here than the old
            # whole-file walk did, and the loop falls through to the next (older)
            # segment instead. That only arises on a segment that is already truncated
            # or planted, `recent()` is documented above as best-effort display rather
            # than the integrity oracle, and verify_integrity() is unaffected -- so it
            # is the right trade against an OOM on a healthy log.
            remaining = max(1, limit - len(result))
            try:
                with _open_segment(path) as fh:
                    tail: deque[str] = deque(_segment_lines(fh), maxlen=remaining)
            except OSError:
                # Segment renamed/unlinked by a concurrent _maybe_rotate between
                # the exists() check and the read, or an over-cap line. Skip it — its
                # content survives under a new index and is surfaced on the next poll.
                continue
            for line in reversed(tail):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    # A SUPERSET of the JSONDecodeError this used to catch, since
                    # JSONDecodeError IS a ValueError (measured True) -- so no line
                    # skipped before is treated differently now. It adds the two
                    # members a hostile line reaches and JSONDecodeError does not:
                    #   * deeply nested input raises RecursionError, which is NOT a
                    #     ValueError (measured False); and
                    #   * an integer literal longer than
                    #     sys.get_int_max_str_digits() (4300) raises a plain
                    #     ValueError, not a JSONDecodeError (measured).
                    # Both propagated out of recent() and crashed the events
                    # listing. Matches the sibling in `_tip_hash_of`, which already
                    # widened to (JSONDecodeError, ValueError); this adds the
                    # RecursionError that sibling still lacks. Kept as a NAMED tuple
                    # rather than `except Exception`: an OSError raised in THIS loop
                    # propagates, because the outer `except OSError` above closes at
                    # the tail read (where skipping the segment is the rotation-race
                    # behaviour that handler exists for) and does not cover here.
                    continue
                if not isinstance(event, dict):
                    # A line that is valid json but not an object (`123`, `true`,
                    # `null`) parses fine and then has no .get -- and this method is
                    # annotated `-> list[dict]`, so every consumer calls .get on the
                    # elements. `kirocrew security events` would raise AttributeError
                    # mid-listing rather than skip the corrupt line.
                    continue
                if sealed and not self._record_is_authentic(event):
                    # A numbered segment is the surface this feature adds, and one can
                    # be PLANTED: a regular file at `<segment_dir>/…jsonl.N` is read
                    # like any other, so without this an attacker-authored approval
                    # reaches `/api/sel/events` and `kirocrew security events` as an
                    # audit record. Dropped, never raised, for the same consumer-safety
                    # reason as the non-object case above. Counted and reported once
                    # below rather than per line, so a planted segment cannot also
                    # become a log-flooding channel.
                    forged += 1
                    continue
                result.append(event)
                if len(result) >= limit:
                    if forged:
                        self._warn_forged(forged)
                    return result
        if forged:
            self._warn_forged(forged)
        return result

    @staticmethod
    def _warn_forged(count: int) -> None:
        logger.error(
            "SEL dropped %d sealed audit record(s) that failed HMAC authentication; "
            "a segment may have been planted or edited. Run `kirocrew security "
            "verify` -- verify_integrity() reports the chain state.",
            count,
        )

    def prune(self, keep_days: int | None = None) -> int:
        """Remove entries older than keep_days. Returns count removed.

        keep_days defaults to this instance's ``retention_days`` knob, NOT the
        module constant read directly — so the daily heartbeat prune (which calls
        prune() with no arg) honors the same cutoff as the rotation path
        (_maybe_rotate -> _prune_sealed_by_age(self._retention_days)) rather than
        the two drifting apart. Both resolve to the same 365 days unless a caller
        overrode the knob at construction. An explicit keep_days still wins
        (tests). ``keep_days<=0`` disables retention entirely and BOTH stages
        no-op.

        Two-stage with rotation: first drop whole sealed segments whose newest
        entry predates the cutoff (clean — never severs a chain mid-segment), then
        rewrite the active file dropping its own aged entries (a no-op in the
        common rotated steady state where the active file only holds recent
        events). _read_last_hash() is re-seeded afterward and now falls back to
        the newest sealed segment, so the chain tip survives even if the active
        rewrite empties the file.

        Stage 2 streams the active file line-by-line into a temp file and
        atomically replaces the original, so memory stays bounded on a
        max_bytes-sized log. The append lock is held across Stage 2's whole
        read+replace critical section so a concurrent append cannot land in the
        old file after the read pass and be lost by the replace: appends either
        complete before the read (and are copied) or block until after the replace
        (and land in the new file). Appends run on the background writer thread,
        so blocking them for the prune duration never touches the event loop.

        The returned count is best-effort: the _lock is released between the two
        stages (so the writer isn't blocked across both), so a _maybe_rotate that
        seals the active file in the gap can move entries out of Stage 2's view,
        leaving `removed` under-reporting by up to one rotation's worth. The count
        is observational only; no caller gates on its exactness.
        """
        if keep_days is None:
            keep_days = self._retention_days
        self.flush()  # don't rewrite the file out from under queued appends
        removed = 0
        # Stage 1: drop aged sealed segments as whole units. The lock is released
        # between the two stages (not held across both) so the background writer
        # isn't blocked during the gap; each stage is independently safe (Stage 1
        # only touches sealed segments, Stage 2 only the active file), and no
        # invariant spans the two, so non-atomic is intentional and correct.
        #
        # Stage 1 needs the cross-process SEAL lease, not just `_lock`. It unlinks
        # only segments it proved aged, but numbering is reused: a rival that prunes
        # the same segment and then seals allocates that number again (see
        # _next_segment_index), so a path proved aged here can name a different,
        # newly-written segment by the time we unlink it. `count=True` widens the
        # window to a full ~100 MB read. Identity re-checking would only narrow it --
        # the Stage 2 note below says closing needs a lock the writer also honours,
        # and for seals that lock EXISTS, so take it. Skipping when a rival holds it
        # costs at most a day of over-retention, since prune runs daily.
        # The dir guard must precede the lease for the same reason it does on the
        # seal path: the lease file lives INSIDE the segment directory, so taking it
        # first would create it through a planted link -- outside the floor.
        self._ensure_segment_dir()
        with self._lock, self._seal_lease() as leased:
            if leased:
                removed += self._prune_sealed_by_age(keep_days)
            else:
                logger.debug(
                    "SEL prune skipping the aged-segment stage; seal lease held elsewhere"
                )

        # keep_days<=0 is the documented retention off-switch.
        # _prune_sealed_by_age already no-ops on it, but Stage 2 below derives
        # cutoff = now - 0 days = now, which would age out and delete EVERY
        # active entry. Honor the off-switch here too rather than wiping the live
        # audit log.
        if keep_days <= 0:
            return removed

        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)
        # Stage 2: rewrite the active file, dropping its aged entries. Unlike
        # verify_integrity()/recent() (read-only, tolerate a stale snapshot),
        # prune is a destructive read-modify-write, so the read+filter+rewrite
        # MUST be fully serialized with the background writer under _lock.
        with self._lock:
            if not self._path.exists():
                # Active file rotated away or never created — nothing to rewrite.
                # Stage 1 may still have removed segments, so return its count
                # rather than 0.
                return removed
            # Snapshot the size AND THE IDENTITY before filtering. `_lock` is
            # in-process only, so another SEL writer process appending between this
            # read pass and the replace below would have its events discarded by our
            # stale rewrite -- silently, since os.replace neither fails nor logs.
            # Re-comparing immediately before the replace converts that silent loss
            # into a SKIPPED cycle, and prune runs daily so the cost is at most a
            # day of over-retention. This narrows the window from the whole read
            # (seconds on a ~100 MB file) to one stat; closing it entirely needs a
            # lock the append path also honours, which is the append-path remainder.
            #
            # SIZE ALONE IS NOT IDENTITY. A rival process that rotates -- sealing the
            # active file away and letting appends recreate it -- produces a
            # DIFFERENT file, and byte-size equality between the old and the new one
            # is a coincidence the check would read as "nothing changed", so the
            # replace would then discard the recreated file's events. (st_dev,
            # st_ino) is the same discriminator `_snapshot_drift` uses against a
            # reused segment number, and for the same reason. Where a filesystem
            # reports no usable inode both sides compare equal and this degrades to
            # the size check rather than false-skipping.
            try:
                st_before = self._path.stat()
                size_before = st_before.st_size
                ident_before = (st_before.st_dev, st_before.st_ino)
            except OSError:
                return removed
            active_removed = 0
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".sel_prune_", suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
                    with open(self._path, encoding="utf-8") as src_f:
                        for raw_line in src_f:
                            line = raw_line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                                # Parsed-datetime compare (not raw string): a
                                # non-+00:00 offset would mis-order which entries
                                # age out. Fail CLOSED on an unparseable timestamp
                                # (parsed is None) -> keep the entry rather than
                                # drop audit data we can't prove is aged.
                                parsed = self._parse_ts(data.get("timestamp", ""))
                                if parsed is not None and parsed < cutoff_dt:
                                    active_removed += 1
                                    continue
                            except json.JSONDecodeError:
                                active_removed += 1
                                continue
                            except (ValueError, RecursionError):
                                # A SEPARATE arm, deliberately NOT added to the tuple
                                # above, and the difference is drop-vs-keep. The
                                # JSONDecodeError arm DELETES the line; widening that
                                # tuple would make a line self-erasing merely by being
                                # unparseable in a NEWER way -- a nesting bomb
                                # (RecursionError, not a ValueError -- measured) or an
                                # over-4300-digit integer (a plain ValueError, not a
                                # JSONDecodeError -- measured). An attacker who can
                                # append could then choose deletion. So this arm KEEPS
                                # the entry: falling through writes it to tmp_f and
                                # never touches `active_removed`.
                                #
                                # Ordering is load-bearing: JSONDecodeError IS a
                                # ValueError (measured True), so the narrow arm must
                                # stay ABOVE this one or it would never be reached and
                                # the existing drop behaviour would silently change.
                                #
                                # Without this arm both types escaped the handler, left
                                # the loop and the `with`, and hit `except BaseException`
                                # below, which unlinks tmp and RE-RAISES -- so no audit
                                # data was lost, but the age-prune aborted and
                                # `retention_days` stopped being enforced for as long as
                                # that one crafted line sat in the active file.
                                # Fail-closed on data, fail-STUCK on retention.
                                #
                                # Matches the fail-closed rule stated for the
                                # unparseable-timestamp case above: keep the entry
                                # rather than drop audit data we cannot prove is aged.
                                # No exc_info: a RecursionError is caught with the
                                # stack still nearly exhausted, and formatting a
                                # traceback there is itself deep work that can raise
                                # again out of the handler.
                                logger.warning(
                                    "SEL kept an active-file entry it could not parse "
                                    "during the age prune; retention cannot prove it is "
                                    "aged, so it is retained rather than deleted.",
                                )
                            tmp_f.write(line)
                            tmp_f.write("\n")

                if active_removed:
                    try:
                        st_now = self._path.stat()
                        size_now = st_now.st_size
                        ident_now = (st_now.st_dev, st_now.st_ino)
                    except OSError:
                        size_now = -1
                        ident_now = (-1, -1)
                    if size_now != size_before or ident_now != ident_before:
                        logger.info(
                            "SEL skipping the active-file prune: the log changed "
                            "while it was being filtered (%d -> %d bytes). Retrying "
                            "next cycle rather than discarding the events appended "
                            "in between.",
                            size_before,
                            size_now,
                        )
                        os.unlink(tmp_path)
                        return removed
                    # mkstemp creates the temp file 0o600, and os.replace carries
                    # that mode onto the destination — so the rewritten audit log
                    # keeps the explicit 0o600 the writer sets in _flush_batch
                    # rather than falling back to a umask default.
                    os.replace(tmp_path, self._path)
                    self._last_hash = self._read_last_hash()
                    removed += active_removed
                else:
                    os.unlink(tmp_path)
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        if removed:
            logger.info("SEL pruned %d entries older than %d days", removed, keep_days)
        return removed


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
        "webex",
        "teams",
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
    "webex",
    "teams",
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
    """
    inst = SecurityEventLog._instance
    if inst is not None and getattr(inst, "_initialized", False):
        return inst._hmac_key_file
    return _default_dir() / _TRUST_SUBDIR / _HMAC_KEY_FILE


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
    not, and its resolved path is never re-resolved — which is how a gateway
    ends up publishing unsigned identities forever while its audit chain still
    looks healthy. These are the same bytes, already validated at init
    (``>= _HMAC_KEY_MIN_BYTES``, see ``_load_or_create_hmac_key``).

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
