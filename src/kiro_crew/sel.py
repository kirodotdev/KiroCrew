"""Security Event Log — immutable, tamper-evident audit trail for tool invocations.

Records structured JSON events for every tool/MCP action with:
- Timestamp (ISO 8601 UTC)
- Caller identity (session key, agent, source interface)
- Operation type (tool_call, tool_approved, tool_rejected, tool_denied, mcp_call)
- Resources affected (tool name, tool kind, arguments summary)
- Outcome (approved, rejected, denied, completed, failed)
- Downstream service (MCP server name if applicable)
- HMAC-SHA256 integrity chain (each entry signs over previous hash)

Storage: ``<config_dir>/security_events.jsonl`` (append-only JSONL)
Retention: configurable, default 365 days per Amazon Security Event Logging Standard.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import logging
import os
import queue
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kiro_crew import platform_compat
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
        if self._initialized:
            return
        # sync=True writes each event inline (no background thread). Used by
        # tests that read the raw log file immediately after logging; production
        # uses the async writer for off-hot-path appends.
        self._sync = sync
        self._dir = base_dir or _default_dir()
        self._path = self._dir / _SEL_FILE
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
        with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                if raise_on_error:
                    raise
                logger.warning("SEL dir create failed for %d events", len(events), exc_info=True)
                return
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
        if callback:
            for event in events:
                self._forward_event(callback, event)

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
        key_path = self._dir / _HMAC_KEY_FILE
        self._dir.mkdir(parents=True, exist_ok=True)
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

    def _read_last_hash(self) -> str:
        """Return the entry_hash of the last COMPLETE record, or "" if none.

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
        if not self._path.exists():
            return ""
        try:
            with open(self._path, "rb") as f:
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
                while pos > 0:
                    read_start = max(pos - 4096, 0)
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
                        except (json.JSONDecodeError, ValueError):
                            # Truncated/corrupt line — skip backward to the last
                            # complete record rather than resetting the chain to
                            # genesis. Flag it so the corruption is not hidden.
                            skipped_corrupt = True
                            logger.warning(
                                "SEL: skipping unparseable audit-log line while "
                                "resolving chain tip in %s; chaining from the last "
                                "complete record instead of resetting to genesis",
                                self._path,
                            )
                            continue
                        if not isinstance(data, dict):
                            # Parseable JSON but not a record object — treat as
                            # corrupt and keep scanning backward.
                            skipped_corrupt = True
                            logger.warning(
                                "SEL: skipping non-object audit-log line while "
                                "resolving chain tip in %s",
                                self._path,
                            )
                            continue
                        if skipped_corrupt:
                            logger.warning(
                                "SEL: recovered chain tip from an earlier complete "
                                "record after a corrupt/truncated tail in %s",
                                self._path,
                            )
                        return data.get("entry_hash", "")
            # No parseable record anywhere in the file — nothing to chain from.
            return ""
        except OSError:
            logger.warning(
                "SEL: failed to read chain tip from %s", self._path, exc_info=True
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
        """Verify HMAC chain. Returns (total_entries, valid_entries)."""
        self.flush()  # ensure all queued events are on disk before verifying
        if not self._path.exists():
            return 0, 0
        total = 0
        valid = 0
        prev_hash = ""
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                data = json.loads(line)
                stored_hash = data.pop("entry_hash", "")
                if data.get("prev_hash", "") != prev_hash:
                    logger.warning("SEL chain break at entry %d", total)
                    prev_hash = stored_hash
                    continue
                payload = json.dumps(data, sort_keys=True).encode()
                expected = hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
                if hmac.compare_digest(stored_hash, expected):
                    valid += 1
                else:
                    logger.warning("SEL HMAC mismatch at entry %d", total)
                prev_hash = stored_hash
            except (json.JSONDecodeError, Exception):
                logger.warning("SEL parse error at entry %d", total)
        return total, valid

    def recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent events."""
        self.flush()  # surface any queued-but-unwritten events
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        result = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(result) >= limit:
                break
        return result

    def prune(self, keep_days: int = _RETENTION_DAYS) -> int:
        """Remove entries older than keep_days. Returns count removed.

        Streams the log line-by-line to bound memory usage, writes survivors
        to a temp file, then atomically replaces the original. The append lock
        is held across the whole read+replace critical section so a concurrent
        append cannot land in the old file after the read pass and be lost by
        the replace: appends either complete before the read (and are copied)
        or block until after the replace (and land in the new file). Appends
        run on the background writer thread, so blocking them for the prune
        duration never touches the event loop.
        """
        self.flush()  # don't rewrite the file out from under queued appends
        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)
        cutoff_str = cutoff_dt.isoformat()

        removed = 0
        with self._lock:
            if not self._path.exists():
                return 0
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
                                if data.get("timestamp", "") < cutoff_str:
                                    removed += 1
                                    continue
                            except json.JSONDecodeError:
                                removed += 1
                                continue
                            tmp_f.write(line)
                            tmp_f.write("\n")

                if removed:
                    os.replace(tmp_path, self._path)
                    self._last_hash = self._read_last_hash()
                    logger.info(
                        "SEL pruned %d entries older than %d days", removed, keep_days
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
    directory when one is initialized (tests and embedded deployments pass a
    ``base_dir``); otherwise falls back to the same default the singleton
    would use. Dependent protocols must resolve the key through this accessor
    rather than re-deriving the path (e.g. via ``config_dir()``; ``_default_dir()``
    honors ``KIROCREW_HOME`` the same way, so resolving through the shared
    accessor keeps the trust root single under isolated-home deployments).
    """
    inst = SecurityEventLog._instance
    if inst is not None and getattr(inst, "_initialized", False):
        return inst._dir / _HMAC_KEY_FILE
    return _default_dir() / _HMAC_KEY_FILE
