"""The durable task store: one SQLite file, write-before-ack, atomic claims.

``$KIROCREW_HOME/tasks/tasks.db`` is the scheduling source of truth for every
accepted task. The entry point that accepts work gets an id back only after the
row is committed (:meth:`TaskStore.accept`); a write that fails raises
:class:`TaskStoreUnavailable` so the caller has nothing to acknowledge.
Dispatch takes a row with one ``UPDATE ... WHERE state IN (claimable)`` that
also stamps a lease and bumps the generation, so two dispatchers cannot both
own a row and a worker from an older generation cannot overwrite a newer one's
outcome. Every state write goes through the transition table in ``model``.

The store keeps no dispatch state in memory: the bounded dispatch window is the
CALLER's list (``TaskStore.window`` is the size it should be bounded to), and
:meth:`fetch_dispatchable` refills it from the rows on disk in FIFO order.

WAL is the default journal. A data home on a network filesystem gets
``journal_mode=DELETE`` instead -- WAL relies on shared memory the NFS/SMB
locking model does not provide -- and the fact is recorded in
:attr:`TaskStore.warnings` for ``kirocrew doctor``; the store never refuses to
open over it.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from kiro_crew import platform_compat
from kiro_crew.metrics.events import TASKQ_COMPLETIONS, emit_counter
from kiro_crew.on_loop_db import OnLoopDBGuard

from . import lanes as _lanes
from . import migrate
from .model import (
    ACTIVE,
    ADMITTED,
    CANCELLED,
    CLAIMABLE,
    LEASE_SECS,
    QUEUED,
    RECOVERING,
    RETRY_WAIT,
    RUNNING,
    TERMINAL,
    WAITING,
    WAITING_INFRA,
    InvalidTransition,
    TaskEvent,
    TaskRecord,
    check_transition,
    is_terminal,
    steps_to,
)

logger = logging.getLogger(__name__)

#: Default bound on the in-memory dispatch window (``agent.task_dispatch_window``).
DEFAULT_DISPATCH_WINDOW = 64

#: This surface's own strictness switch, per ``on_loop_db``'s "one switch per
#: surface" rule. Deliberately NOT ``on_loop_db.STORE_STRICT_ENV``: that one is
#: the knowledge store's, and arming a test over the task queue with it would
#: also arm the knowledge store's tracked backlog.
STRICT_ON_LOOP_ENV = "KIROCREW_STRICT_ON_LOOP_TASK_STORE"

# MODULE-LEVEL by requirement, not by style: each guard owns a ``ContextVar``
# and CPython never collects one a ``Context`` has seen, so a per-store guard
# would leak (``on_loop_db``'s adoption note).
#
# ``dev_mode_arms_strict=False`` for the reason ``knowledge/store.py`` gives:
# two classes of on-loop take are tracked here, and an un-offloaded READ is not
# one of them -- a read reached from a coroutine is a DEFECT, and the way to find
# one is :attr:`TaskStore.loop_thread_calls` under an armed pin, never this
# comment. Both tracked classes are ORDERING problems; a write whose RETURN VALUE
# is load-bearing is NOT one, because the value can be awaited (the drained
# spawn's defer is: ``admission.gate``'s ``_deferred`` parks a ``DeferPoint``
# with both answers and ``finish_parked_defer`` awaits ``store.run(store.defer)``
# for the boolean that picks between them):
#
# 1. The terminal write on an unwinding arm is synchronous BY DESIGN --
#    ``taskq.adapters.runner``'s ``waiting_dependency`` cancel and its
#    ``_end_row_on_cancel`` (``admit``'s ``CancelledError`` arm, for a row the
#    caller never received a handle for), ``taskrunner``'s and
#    ``workflows.agent_pool``'s ``CancelledError`` / ``BaseException`` arms --
#    because an ``await`` there can be interrupted before the write is submitted
#    and a dropped terminal write leaves the row active for the next boot's
#    reconciler (see ``Admitted.settle_async``).
# 2. ``subagent_manager.admission.taskq_cancel_queued``, reached from
#    ``_unqueue`` on the Stop-all path: the row has to be cancelled before a
#    drain can claim it AND out of the in-memory window before a stagger timer
#    can start it, and an await between those two is a race in either order.
#
# Those takes are on-loop and cannot simply be offloaded, so arming this from
# ``KIROCREW_DEV_MODE`` would raise on a cancel or a Stop-all and the developer's
# rational response -- unsetting that variable -- silences every OTHER surface's
# guard too. Flip it back to True once both classes are either restructured or
# inside a vetted ``allow_on_loop()`` block.
_ON_LOOP_DB_GUARD = OnLoopDBGuard(
    label="task store",
    remedy=(
        "Offload it: await store.run(store.<method>, ...) so the busy wait runs on the "
        "taskq-writer thread."
    ),
    strict_env=STRICT_ON_LOOP_ENV,
    dev_mode_arms_strict=False,
)

#: How long a writer waits on a locked database before the write is reported
#: as a failure. Two seconds is the RFC's "locked > 2s" line: past it the
#: caller must refuse rather than hold the event loop.
BUSY_TIMEOUT_SECS = 2.0

#: Filesystem type names that mean "not this host's disk". WAL mode needs the
#: ``-shm`` file to be coherently mmap-shared, which these do not guarantee.
NETWORK_FS_TYPES: frozenset[str] = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb",
        "smb3",
        "smbfs",
        "afpfs",
        "webdav",
        "fuse.sshfs",
        "sshfs",
        "9p",
        "afs",
        "ceph",
        "glusterfs",
        "lustre",
        "ncpfs",
        "coda",
        "fuse.rclone",
        "davfs",
    }
)

_DARWIN_MNT_LOCAL = 0x00001000

_SQL_CLAIMABLE = "(" + ",".join(f"'{s}'" for s in sorted(CLAIMABLE)) + ")"
_SQL_TERMINAL = "(" + ",".join(f"'{s}'" for s in sorted(TERMINAL)) + ")"
_SQL_ACTIVE = "(" + ",".join(f"'{s}'" for s in sorted(ACTIVE)) + ")"
_SQL_WAITING = "(" + ",".join(f"'{s}'" for s in sorted(WAITING)) + ")"


class _CorruptStore(Exception):
    """Internal: the file is not a usable SQLite database (see ``TaskStore.open``)."""


#: SQLite's own verdicts that the FILE is damaged. Exact phrases: a bare
#: substring such as ``corrupt`` could match a path or a wrapped message.
#: The ``integrity_check`` verdict is the fourth trigger (``_open_connection``).
_CORRUPTION_MARKERS = (
    "file is not a database",
    "database disk image is malformed",
    "malformed database schema",
)
#: Messages that mean another writer or the host, never the file: keep refusing.
_TRANSIENT_MARKERS = ("locked", "busy", "disk is full", "database or disk is full", "readonly")


def _is_corruption(exc: BaseException) -> bool:
    """Only SQLite's own damage verdicts count. Anything else that is not a
    lock / full-disk / read-only condition -- a schema version newer than this
    build (``migrate.apply_schema``), a permission error -- stays a refusal:
    quarantining a VALID newer file would be a downgrade destroying data."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    text = str(exc).lower()
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return False
    return any(marker in text for marker in _CORRUPTION_MARKERS)


def _move_without_overwrite(src: Path, dst: Path) -> None:
    """Rename that FAILS on an existing destination on every platform.

    POSIX ``rename`` replaces silently and Windows refuses, so neither is the
    portable form. A hard link never replaces anywhere: link, then unlink the
    source. A filesystem without links gets a rename behind an existence
    check, which is the best that filesystem offers.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if dst.exists():
            raise FileExistsError(str(dst))
        src.rename(dst)
        return
    src.unlink()


class TaskStoreUnavailable(RuntimeError):
    """The store could not answer; a write did not commit.

    Raised for a locked or unwritable database, a schema the build cannot
    write, or a full disk. Never raised for a row the transition table
    refuses -- that is a ``False`` return, because the store is fine.

    It is the ONE error type every public method of :class:`TaskStore` reports a
    database failure as, reads included (:func:`_typed_read`). A read that let
    ``sqlite3.Error`` through would be indistinguishable from a bug in this
    module at every call site, and ``journal_mode=delete`` -- what a data home
    on a network filesystem gets -- blocks readers behind a competing writer,
    so a bare ``SELECT`` is exactly as failure-prone there as a transaction.
    """


def _typed_read(fn: "Callable[..., Any]") -> "Callable[..., Any]":
    """Report a read's database failure as :class:`TaskStoreUnavailable`.

    Every WRITE on this class already converts ``sqlite3.Error`` at its own
    boundary; a read must too, because a caller cannot act on a distinction it
    did not choose. That the gap was an omission and not a design is visible in
    the callers: ``except TaskStoreUnavailable`` already guards ``state_of`` /
    ``get`` / ``events`` / ``count_pending`` / ``list_rows`` / ``active_rows``
    reads across the runner adapter, the dependency coordinator and the
    admission bridge -- arms that cannot fire for a read raising
    ``sqlite3.OperationalError`` instead. Wrapping HERE makes those arms live
    once; an ``except sqlite3.Error`` per call site would be the same rule
    restated at every read in those three subsystems, each copy free to drift.
    """

    @functools.wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            raise TaskStoreUnavailable(f"task store read failed: {exc}") from exc

    return _wrapper


@dataclass(frozen=True)
class ClaimResult:
    """What a successful claim hands the dispatcher: the row and its new generation."""

    record: TaskRecord

    @property
    def generation(self) -> int:
        return self.record.generation


# ── network filesystem detection (Q6) ─────────────────────────────────────────


def _nearest_existing(path: Path) -> Path:
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return probe


def _linux_mount_type(path: Path) -> str | None:
    try:
        target = os.path.realpath(_nearest_existing(path))
        lines = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        if target == mount or target.startswith(mount.rstrip("/") + "/") or mount == "/":
            if best is None or len(mount) > best[0]:
                best = (len(mount), parts[2])
    return best[1] if best else None


class _DarwinStatfs(ctypes.Structure):
    """``struct statfs`` with 64-bit inodes, as ``statfs64`` fills it."""

    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_mount_facts(path: Path) -> tuple[str, bool] | None:
    """``(fstypename, is_local)`` for the mount holding *path*, or None."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = None
        for symbol in ("statfs64", "statfs$INODE64"):
            try:
                fn = getattr(libc, symbol)
                break
            except AttributeError:
                continue
        if fn is None:
            return None
        fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(_DarwinStatfs)]
        fn.restype = ctypes.c_int
        buf = _DarwinStatfs()
        target = os.fsencode(str(_nearest_existing(path)))
        if fn(target, ctypes.byref(buf)) != 0:
            return None
        name = buf.f_fstypename.decode("ascii", errors="replace").strip("\x00").lower()
        return name, bool(buf.f_flags & _DARWIN_MNT_LOCAL)
    except (OSError, ValueError, AttributeError):
        return None


def detect_network_filesystem(path: Path) -> bool | None:
    """True when *path* lives on a network mount, False when local, None when unknown.

    Linux reads ``/proc/mounts`` and matches the longest mount point prefix;
    macOS asks ``statfs`` for the type name and the ``MNT_LOCAL`` flag. Windows
    has no mount table, so it asks the volume ROOT's drive type
    (``platform_compat.path_volume_is_remote``), which reports a UNC path and a
    mapped network drive alike as remote. Any failure answers None: the caller
    keeps WAL and records nothing, because a detector that cannot see the mount
    table has no evidence either way.
    """
    try:
        if sys.platform.startswith("linux"):
            fstype = _linux_mount_type(path)
            if fstype is None:
                return None
            return fstype.lower() in NETWORK_FS_TYPES
        if sys.platform == "darwin":
            facts = _darwin_mount_facts(path)
            if facts is None:
                return None
            name, is_local = facts
            return (not is_local) or name in NETWORK_FS_TYPES
        if sys.platform == "win32":
            return platform_compat.path_volume_is_remote(path)
    except Exception:  # noqa: BLE001 - detection is advisory
        logger.debug("network filesystem detection failed for %s", path, exc_info=True)
    return None


# ── the store ─────────────────────────────────────────────────────────────────


class TaskStore:
    """SQLite-backed task rows with atomic claim, lease and generation fencing.

    One instance per gateway process. The synchronous methods are for callers
    that are ALREADY off the event loop -- the writer thread, a worker thread,
    the CLI, cron -- and are serialized by an internal lock; an event-loop
    caller MUST go through :meth:`run` instead, because ``_lock`` is held
    across ``BEGIN IMMEDIATE``'s busy wait and a wait taken on the loop stalls
    every session with it. ``clock`` is injectable so tests drive
    ``next_run_at`` and lease expiry deterministically.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        window: int = DEFAULT_DISPATCH_WINDOW,
        clock: Callable[[], float] = time.time,
        network_fs: bool | None = None,
        lease_secs: float = LEASE_SECS,
        busy_timeout_secs: float = BUSY_TIMEOUT_SECS,
        diagnostic: bool = False,
    ) -> None:
        self._path = Path(path)
        self._window = max(1, int(window))
        self._clock = clock
        self._network_fs_override = network_fs
        self._lease_secs = float(lease_secs)
        self._busy_timeout_secs = float(busy_timeout_secs)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._incarnation = uuid.uuid4().hex
        self._previous_incarnation: str | None = None
        self._journal_mode = "wal"
        #: A diagnostic reader (``kirocrew doctor``) never records itself as the
        #: store's incarnation, so the gateway's own reconcile bookkeeping is
        #: untouched by someone merely looking.
        self._diagnostic = bool(diagnostic)
        self.warnings: list[str] = []
        #: ONE dedicated writer thread for callers that live on an event loop.
        #: ``BEGIN IMMEDIATE`` waits up to ``busy_timeout`` for another
        #: connection's writer lock (a second gateway on the same home, a
        #: ``sqlite3`` shell, a network filesystem), and a wait taken on the
        #: gateway loop freezes every chat, stream and heartbeat with it; the
        #: async entry points (``/api/spawn`` -> ``spawn_async``, the tasks API)
        #: await :meth:`run` instead. Lazily created; single-threaded so the
        #: connection's RLock never contends between two executor workers.
        self._executor: ThreadPoolExecutor | None = None
        #: Guards the executor SLOT above, and nothing else. Deliberately not
        #: ``_lock``: :meth:`run` is called FROM the event loop, the writer
        #: thread holds ``_lock`` across ``BEGIN IMMEDIATE``'s busy wait, and a
        #: ``run()`` that took ``_lock`` to reach its executor would hand the
        #: loop the very wait it exists to move off it -- invisibly, since that
        #: take is not a :meth:`_c` and no counter would show it. Held only for
        #: the microseconds of a ``ThreadPoolExecutor`` construction.
        self._executor_lock = threading.Lock()
        #: How many store calls executed ON a thread with a running event loop
        #: -- the blocking shape ``check_sync_io_in_async`` hunts. Counted here
        #: rather than left to :data:`_ON_LOOP_DB_GUARD` because the guard has
        #: no counter and a test asserting "this path took NO on-loop call"
        #: needs a number, not the absence of a log line.
        self.loop_thread_calls = 0

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def default_path(cls, home: Path) -> Path:
        return home / "tasks" / "tasks.db"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def window(self) -> int:
        return self._window

    @property
    def incarnation(self) -> str:
        return self._incarnation

    @property
    def previous_incarnation(self) -> str | None:
        return self._previous_incarnation

    @property
    def journal_mode(self) -> str:
        return self._journal_mode

    @property
    def lease_secs(self) -> float:
        return self._lease_secs

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    #: Set when :meth:`open` found the file corrupt and moved it aside:
    #: the quarantine path (``tasks.db.corrupt-<utc>``). ``None`` otherwise.
    quarantined_to: Path | None = None

    def open(self) -> "TaskStore":
        """Open (creating if needed), apply the schema, record this incarnation.

        Raises :class:`TaskStoreUnavailable` when the file cannot be opened or
        the schema cannot be written; the caller decides whether to run
        without a store (legacy in-memory queue) or to refuse work.

        A CORRUPT file is not a refusal that waits for a human. Corruption --
        ``file is not a database``, ``database disk image is malformed``, a
        failed ``PRAGMA integrity_check`` -- is what a crash mid-write can
        leave behind, and with the durable queue enabled every spawn would be
        refused until someone flipped the flag. So the file is QUARANTINED
        (renamed to ``tasks.db.corrupt-<utc>`` beside its ``-wal``/``-shm``),
        the schema is recreated in a fresh file, the fact is logged once at
        warning level and recorded on :attr:`quarantined_to` and
        :attr:`warnings` (``kirocrew doctor`` reads both). A locked / busy /
        disk-full / read-only error is NOT corruption: another writer, or a
        host condition, and the refusal stands. In ``diagnostic`` mode nothing
        is moved: doctor reports the corruption instead.
        """
        with self._lock:
            if self._conn is not None:
                return self
            try:
                self._conn = self._open_connection()
            except _CorruptStore as exc:
                if self._diagnostic or self.quarantined_to is not None:
                    raise TaskStoreUnavailable(
                        f"task store {self._path} is corrupt ({exc}); the gateway quarantines "
                        "it at its next boot"
                    ) from exc
                self.quarantined_to = self._quarantine_corrupt_file(str(exc))
                try:
                    self._conn = self._open_connection()
                except _CorruptStore as again:
                    raise TaskStoreUnavailable(
                        f"task store {self._path} is corrupt after quarantine: {again}"
                    ) from again
            return self

    def _open_connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            platform_compat.restrict_dir_to_owner(self._path.parent)
            conn = sqlite3.connect(
                str(self._path),
                timeout=self._busy_timeout_secs,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_secs * 1000)}")
            verdict = conn.execute("PRAGMA integrity_check").fetchone()
            if verdict is not None and str(verdict[0]).lower() != "ok":
                raise _CorruptStore(f"integrity_check: {verdict[0]}")
            self._apply_journal_mode(conn)
            conn.execute("PRAGMA synchronous=NORMAL")
            migrate.apply_schema(conn)
            if not self._diagnostic:
                self._record_incarnation(conn)
            platform_compat.restrict_to_owner(self._path)
        except _CorruptStore:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                conn.close()
            if _is_corruption(exc):
                raise _CorruptStore(str(exc)) from exc
            raise TaskStoreUnavailable(f"cannot open task store {self._path}: {exc}") from exc
        return conn

    #: Sidecars SQLite may leave beside the file: the DELETE-mode rollback
    #: journal, and the WAL pair. A hot ``-journal`` left beside a recreated
    #: database would be rolled into it at the next open, so it moves too.
    _SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

    def _quarantine_corrupt_file(self, reason: str) -> Path:
        """Move the corrupt file and its sidecars aside under a name nothing else holds.

        Never overwrites: a crash loop quarantines several times a second, and
        each copy is recovery evidence. The base name (UTC stamp to the
        microsecond, pid, then a counter) is RESERVED by exclusive creation
        before anything moves, so two boots -- or two processes -- cannot pick
        the same one. The moves are not atomic as a group; they run sidecars
        first and the database LAST, because the database's absence is what
        the reopen keys on: a sidecar left behind is logged and reported,
        never a reason to keep the corrupt database in place or to raise into
        the reopen path.
        """
        target = self._reserve_quarantine_name()
        moved: list[str] = []
        left: list[str] = []
        for suffix in self._SIDECAR_SUFFIXES:
            src = self._path.with_name(self._path.name + suffix)
            if not src.exists():
                continue
            dst = target.with_name(target.name + suffix)
            try:
                _move_without_overwrite(src, dst)
                moved.append(dst.name)
            except OSError as exc:
                left.append(f"{src.name} ({exc})")
        try:
            # Onto the placeholder this call created: the one replace that is
            # ours to make.
            os.replace(self._path, target)
            moved.append(target.name)
        except OSError as exc:
            try:
                target.unlink()
            except OSError:
                pass
            raise TaskStoreUnavailable(
                f"task store {self._path} is corrupt ({reason}) and could not be "
                f"quarantined: {exc}"
            ) from exc
        message = (
            f"task store {self._path} was corrupt ({reason}); quarantined as {target.name} "
            f"({', '.join(moved)}) and recreated empty. Accepted work that "
            "only lived in the old file is NOT recovered: inspect the quarantined copy."
        )
        if left:
            message += f" Sidecar(s) still beside the recreated file: {', '.join(left)}."
        logger.warning("taskq: %s", message)
        self.warnings.append(message)
        return target

    def _reserve_quarantine_name(self) -> Path:
        """Create the quarantine's base path exclusively and return it.

        ``O_EXCL`` is the collision check: an existing name -- an earlier boot
        in the same microsecond, an operator's copy -- fails the create, and
        the counter suffix moves on to the next name.
        """
        now = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
        base = f"{self._path.name}.corrupt-{stamp}.{int((now % 1) * 1_000_000):06d}Z-{os.getpid()}"
        for n in range(10_000):
            candidate = self._path.with_name(base if n == 0 else f"{base}-{n}")
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
        raise TaskStoreUnavailable(f"task store {self._path} is corrupt; no free quarantine name")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    # -- off-loop execution -------------------------------------------------

    def _writer_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="taskq-writer"
                )
            return self._executor

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` -- normally one of this store's own
        methods -- on the dedicated writer thread and await its result.

        This is the ONLY way an event-loop caller should touch the store: the
        SQLite call, its lock waits and its fsync all happen off-loop, and the
        single worker keeps writes serialized without the loop thread ever
        holding the connection lock.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._writer_executor(), functools.partial(fn, *args, **kwargs)
        )

    @staticmethod
    def _on_running_loop_thread() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def _apply_journal_mode(self, conn: sqlite3.Connection) -> None:
        """Pick the journal mode; WAL needs POSITIVE evidence of a local disk.

        The three answers are not two. ``detect_network_filesystem`` returns True,
        False or None, and None is what every failure answers -- an unreadable
        ``/proc/mounts``, a ``statfs`` that raised, a Windows volume root that
        would not report its drive type. Reading None as "local" is a fail-OPEN on
        a durability control: WAL's shared-memory assumption does not hold on
        SMB/NFS, so the one environment the special case exists for is also the
        one where a detector is most likely to answer None, and the cost of being
        wrong there is a corrupt store -- accepted work quarantined and lost.
        DELETE is slower and correct everywhere, so unknown takes it and says so.

        An operator whose disk IS local and whose mount table is unreadable is not
        stuck with the slow mode: ``agent.task_store_journal_mode=wal`` forces WAL
        and skips detection entirely, and the warning names that key.
        """
        on_network = self._network_fs_override
        detected: bool | None = None
        if on_network is None:
            detected = detect_network_filesystem(self._path)
            if detected:
                self.warnings.append(
                    f"{self._path} is on a network filesystem: task store journal_mode=DELETE "
                    "(WAL needs local shared memory). Throughput is lower; move KIROCREW_HOME "
                    "to a local disk for the default."
                )
            elif detected is None:
                self.warnings.append(
                    f"could not tell whether {self._path} is on a local or a network "
                    "filesystem: task store journal_mode=DELETE, because WAL's shared-memory "
                    "assumption does not hold on SMB/NFS and an unverified guess there risks "
                    "the store. Throughput is lower; set agent.task_store_journal_mode=wal to "
                    "force WAL on a disk you know is local."
                )
        elif on_network:
            self.warnings.append(
                f"{self._path} treated as a network filesystem by configuration "
                "(agent.task_store_journal_mode=delete): task store journal_mode=DELETE; "
                "throughput is lower than WAL."
            )
        # WAL only on a POSITIVE local answer: the configured override saying
        # False, or the detector saying False. None is neither.
        local = on_network is False or detected is False
        wanted = "wal" if local else "delete"
        row = conn.execute(f"PRAGMA journal_mode={wanted}").fetchone()
        got = str(row[0]).lower() if row else wanted
        if got != wanted:
            self.warnings.append(
                f"task store asked for journal_mode={wanted} but SQLite kept {got}"
            )
        self._journal_mode = got

    def _record_incarnation(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key='incarnation'").fetchone()
        self._previous_incarnation = str(row[0]) if row else None
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('incarnation', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (self._incarnation,),
        )

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise TaskStoreUnavailable(f"task store {self._path} is not open")
        if self._on_running_loop_thread():
            self.loop_thread_calls += 1
        _ON_LOOP_DB_GUARD.check()
        return self._conn

    def now(self) -> float:
        return float(self._clock())

    # -- events --------------------------------------------------------------

    def _append_event_in_tx(
        self, conn: sqlite3.Connection, task_id: str, kind: str, data: dict[str, Any]
    ) -> None:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()
        seq = int(seq_row[0]) if seq_row else 1
        conn.execute(
            "INSERT INTO task_events(task_id, seq, ts, kind, data_json) VALUES(?,?,?,?,?)",
            (task_id, seq, self.now(), kind, json.dumps(data, sort_keys=True, default=str)),
        )

    def append_event(self, task_id: str, kind: str, data: dict[str, Any] | None = None) -> None:
        """Append one event outside any state change (progress, ``deliver``)."""
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._append_event_in_tx(conn, task_id, kind, data or {})
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task event write failed: {exc}") from exc

    @_typed_read
    def events(self, task_id: str, *, limit: int = 200) -> list[TaskEvent]:
        with self._lock:
            rows = (
                self._c()
                .execute(
                    "SELECT task_id, seq, ts, kind, data_json FROM task_events "
                    "WHERE task_id=? ORDER BY seq DESC LIMIT ?",
                    (task_id, int(limit)),
                )
                .fetchall()
            )
        out: list[TaskEvent] = []
        for r in reversed(rows):
            try:
                data = json.loads(r["data_json"])
            except (TypeError, ValueError):
                data = {}
            out.append(
                TaskEvent(
                    task_id=r["task_id"],
                    seq=int(r["seq"]),
                    ts=float(r["ts"]),
                    kind=str(r["kind"]),
                    data=data if isinstance(data, dict) else {},
                )
            )
        return out

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -- accept (write-before-ack) -------------------------------------------

    def accept(self, records: Iterable[TaskRecord]) -> list[str]:
        """Commit every record in one transaction and return their ids.

        The ids come back only after ``COMMIT``. Any failure rolls the whole
        batch back and raises :class:`TaskStoreUnavailable`: a caller holding
        that exception has accepted nothing and must say so. A duplicate id or
        idempotency key is an ``IntegrityError`` and is reported the same way,
        because handing back an id for a row that was NOT written is the exact
        promise this method exists to keep.
        """
        batch = list(records)
        if not batch:
            return []
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for rec in batch:
                    if rec.state != QUEUED and rec.state not in CLAIMABLE:
                        raise ValueError(f"accept() only takes claimable rows, got {rec.state!r}")
                    if not rec.created_at:
                        rec.created_at = ts
                    rec.updated_at = ts
                    if not rec.lane:
                        rec.lane = self._lane_in_tx(conn, rec)
                    conn.execute(
                        f"INSERT INTO tasks({', '.join(TaskRecord.COLUMNS)}) "
                        f"VALUES({', '.join('?' for _ in TaskRecord.COLUMNS)})",
                        rec.to_row(),
                    )
                    self._append_event_in_tx(conn, rec.id, "accepted", {"kind": rec.kind})
                conn.execute("COMMIT")
            except (sqlite3.Error, OSError, ValueError) as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task store write failed: {exc}") from exc
        return [rec.id for rec in batch]

    def accept_one(self, record: TaskRecord) -> str:
        return self.accept([record])[0]

    def insert_if_absent(self, record: TaskRecord) -> bool:
        """Keyed insert for imports: True when the row was written, False if present."""
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not record.created_at:
                    record.created_at = ts
                record.updated_at = ts
                if not record.lane:
                    record.lane = self._lane_in_tx(conn, record)
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO tasks({', '.join(TaskRecord.COLUMNS)}) "
                    f"VALUES({', '.join('?' for _ in TaskRecord.COLUMNS)})",
                    record.to_row(),
                )
                inserted = cur.rowcount == 1
                if inserted:
                    self._append_event_in_tx(
                        conn, record.id, "imported", {"kind": record.kind, "state": record.state}
                    )
                conn.execute("COMMIT")
            except (sqlite3.Error, OSError) as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task store write failed: {exc}") from exc
        return inserted

    # -- claim / lease -------------------------------------------------------

    def claim(
        self, task_id: str, *, owner: str | None = None, lease_secs: float | None = None
    ) -> ClaimResult | None:
        """Atomically take *task_id* for dispatch; None when someone else has it.

        One ``UPDATE`` does the whole check-and-set: the row must be in a
        claimable state, eligible now (``next_run_at``), and either unleased
        or past its lease. The generation is bumped and ``attempts`` counts
        this dispatch, so every callback the run makes afterwards can be
        fenced against a later re-dispatch.
        """
        ts = self.now()
        lease = self._lease_secs if lease_secs is None else float(lease_secs)
        who = owner or self._incarnation
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE tasks SET state=?, lease_owner=?, lease_expires_at=?, "
                    "generation=generation+1, attempts=attempts+1, next_run_at=NULL, "
                    "updated_at=? "
                    f"WHERE id=? AND state IN {_SQL_CLAIMABLE} "
                    "AND (next_run_at IS NULL OR next_run_at<=?) "
                    "AND (lease_expires_at IS NULL OR lease_expires_at<?)",
                    (ADMITTED, who, ts + lease, ts, task_id, ts, ts),
                )
                if cur.rowcount != 1:
                    conn.execute("COMMIT")
                    return None
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                rec = TaskRecord.from_row(row)
                self._append_event_in_tx(
                    conn, task_id, "claimed", {"owner": who, "generation": rec.generation}
                )
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task claim failed: {exc}") from exc
        return ClaimResult(record=rec)

    def claim_next(
        self,
        kind: str,
        *,
        exclude_ids: Sequence[str] = (),
        owner: str | None = None,
        session_key: str | None = None,
    ) -> ClaimResult | None:
        """Claim the oldest eligible row of *kind*; None when nothing is eligible.

        Retries a handful of times when a concurrent dispatcher wins the row
        picked, so the caller sees one answer, not a spurious empty queue.
        """
        for _ in range(8):
            candidates = self.fetch_dispatchable(
                kind, limit=1, exclude_ids=exclude_ids, session_key=session_key
            )
            if not candidates:
                return None
            got = self.claim(candidates[0].id, owner=owner)
            if got is not None:
                return got
        return None

    def renew_lease(
        self, task_id: str, generation: int, *, lease_secs: float | None = None
    ) -> bool:
        """Extend the lease the run loop holds; False when the generation is stale."""
        ts = self.now()
        lease = self._lease_secs if lease_secs is None else float(lease_secs)
        with self._lock:
            conn = self._c()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET lease_expires_at=?, updated_at=? "
                    f"WHERE id=? AND generation=? AND state IN {_SQL_ACTIVE}",
                    (ts + lease, ts, task_id, int(generation)),
                )
            except sqlite3.Error as exc:
                raise TaskStoreUnavailable(f"lease renewal failed: {exc}") from exc
        return cur.rowcount == 1

    def release_lease(self, task_id: str) -> bool:
        """Drop whatever lease a row carries without changing its state.

        Used by the reconciler for a row whose owner is gone and whose state
        is already claimable, so the next dispatcher can take it at once.
        """
        with self._lock:
            conn = self._c()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                    "WHERE id=?",
                    (self.now(), task_id),
                )
            except sqlite3.Error as exc:
                raise TaskStoreUnavailable(f"lease release failed: {exc}") from exc
        return cur.rowcount == 1

    # -- transitions ---------------------------------------------------------

    def transition(
        self,
        task_id: str,
        new_state: str,
        *,
        generation: int | None = None,
        result_ref: str | None = None,
        next_run_at: float | None = None,
        progress: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        wait: dict[str, Any] | None = None,
    ) -> bool:
        """Move *task_id* to *new_state* if the table allows it and the generation matches.

        Returns False -- and records why in ``task_events`` -- when the row is
        missing, the caller's generation is stale (``stale_result``), or the
        edge is forbidden (``rejected_transition``, which is how a terminal
        state stays terminal). Raises only when the database itself fails.

        ``wait`` is the ``WaitRecord`` dict for a move INTO a waiting state
        (required there; :meth:`enter_wait` is the checked front door). Any
        move OUT of a waiting state clears the column, so a row never carries
        a stale reason.
        """
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT state, generation FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return False
                old, gen = str(row["state"]), int(row["generation"])
                if generation is not None and int(generation) != gen:
                    self._append_event_in_tx(
                        conn,
                        task_id,
                        "stale_result",
                        {"from_generation": int(generation), "current": gen, "wanted": new_state},
                    )
                    conn.execute("COMMIT")
                    return False
                try:
                    check_transition(old, new_state)
                except InvalidTransition:
                    self._append_event_in_tx(
                        conn, task_id, "rejected_transition", {"from": old, "to": new_state}
                    )
                    conn.execute("COMMIT")
                    return False
                sets = ["state=?", "updated_at=?"]
                args: list[Any] = [new_state, ts]
                if new_state in TERMINAL or new_state in CLAIMABLE:
                    sets.append("lease_owner=NULL")
                    sets.append("lease_expires_at=NULL")
                if new_state in (RETRY_WAIT, RECOVERING, WAITING_INFRA, QUEUED):
                    sets.append("next_run_at=?")
                    args.append(next_run_at)
                if new_state in WAITING:
                    sets.append("wait_json=?")
                    args.append(json.dumps(wait, sort_keys=True, default=str) if wait else None)
                elif old in WAITING:
                    sets.append("wait_json=NULL")
                if result_ref is not None:
                    sets.append("result_ref=?")
                    args.append(result_ref)
                if progress is not None:
                    sets.append("progress_json=?")
                    args.append(json.dumps(progress, sort_keys=True, default=str))
                args.extend([task_id, old, gen])
                cur = conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND state=? AND generation=?",
                    args,
                )
                if cur.rowcount != 1:
                    conn.execute("COMMIT")
                    return False
                event = {"from": old, "to": new_state}
                if detail:
                    event.update(detail)
                self._append_event_in_tx(conn, task_id, "transition", event)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task transition failed: {exc}") from exc
        if new_state in TERMINAL:
            # The completion-rate series: the one funnel every terminal write
            # crosses (finish/cancel route here). ``outcome`` is a state name
            # -- a closed set.
            emit_counter(TASKQ_COMPLETIONS, {"outcome": new_state})
        return True

    def advance(self, task_id: str, new_state: str, *, generation: int | None = None) -> bool:
        """:meth:`transition` for a caller whose PREDECESSOR write may have been
        lost: the ROW's own state picks the path.

        ``transition`` refuses ``admitted -> running`` for good, so one locked
        database between the claim and the ``starting`` write would keep a LIVE
        row in ``admitted`` -- the one state a boot reconciler requeues without
        asking the side-effect class -- for the whole run, because every LATER
        write asks for an edge the table forbids from there and is refused too.
        ``model.steps_to`` supplies the table's OWN path from where the row
        actually is, so nothing is widened: each step is an edge
        ``check_transition`` accepts, and each carries *generation*, so a row
        another owner ended stays ended.

        False when the row is gone or the table joins the two states by no path
        this helper will replay; True when the row is already *new_state*.

        The read and the writes share ``_lock``, so no writer on this
        connection lands between the state the path was computed from and the
        first step of it. Never called ON the event loop, like every other
        synchronous method here: post it through :meth:`run`.
        """
        with self._lock:
            current = self.state_of(task_id)
            if current is None:
                return False
            if current == new_state:
                return True
            steps = steps_to(current, new_state)
            if not steps:
                logger.debug("taskq: %s cannot move %s -> %s", task_id, current, new_state)
                return False
            return all(self.transition(task_id, step, generation=generation) for step in steps)

    def finish(
        self,
        task_id: str,
        state: str,
        *,
        generation: int | None = None,
        result_ref: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Write a terminal state; a stale generation or an already-terminal row is a no-op."""
        if not is_terminal(state):
            raise InvalidTransition("?", state)
        detail = {"error": error[:500]} if error else None
        return self.transition(
            task_id, state, generation=generation, result_ref=result_ref, detail=detail
        )

    def cancel(
        self,
        task_id: str,
        *,
        reason: str = "",
        only_from: frozenset[str] | None = None,
        generation: int | None = None,
    ) -> str | None:
        """Cancel from ANY non-terminal state; returns the state it left, or None.

        Bumps the generation so a worker still holding the old one is fenced
        out of every later write, and clears the lease. A terminal row -- a
        completed run, an earlier cancel -- is untouched and answers None.

        ``only_from`` and ``generation`` make that cancel CONDITIONAL, for a
        caller whose decision to cancel came from a row it read in an earlier
        call: the test and the write share this transaction, so a row that moved
        in between answers None instead of being cancelled under a live executor
        -- whose own settlement the generation bump then fences out, leaving work
        running that nothing will ever settle. The refusals are the ones
        :meth:`transition` already records for the same two reasons
        (``rejected_transition`` for the state, ``stale_result`` for the
        generation), and the predicate rides in the ``UPDATE`` exactly as
        :meth:`claim`'s and :meth:`update_wait`'s do, so a caller reading
        ``task_events`` sees one refusal vocabulary and not a second one.

        Cancel-from-anywhere stays the default because the Stop-all cascade
        (``WaitLedger.cancel_tree``) and the boot sweeps mean it: their subject
        is a row whose live owner is gone or is being reaped by the same caller.
        """
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT state, generation FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None or is_terminal(str(row["state"])):
                    conn.execute("COMMIT")
                    return None
                old, gen = str(row["state"]), int(row["generation"])
                if only_from is not None and old not in only_from:
                    self._append_event_in_tx(
                        conn, task_id, "rejected_transition", {"from": old, "to": CANCELLED}
                    )
                    conn.execute("COMMIT")
                    return None
                if generation is not None and int(generation) != gen:
                    self._append_event_in_tx(
                        conn,
                        task_id,
                        "stale_result",
                        {"from_generation": int(generation), "current": gen, "wanted": CANCELLED},
                    )
                    conn.execute("COMMIT")
                    return None
                cur = conn.execute(
                    "UPDATE tasks SET state=?, generation=generation+1, lease_owner=NULL, "
                    "lease_expires_at=NULL, next_run_at=NULL, wait_json=NULL, updated_at=? "
                    "WHERE id=? AND state=? AND generation=?",
                    (CANCELLED, ts, task_id, old, gen),
                )
                if cur.rowcount != 1:
                    conn.execute("COMMIT")
                    return None
                self._append_event_in_tx(
                    conn, task_id, "transition", {"from": old, "to": CANCELLED, "reason": reason}
                )
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task cancel failed: {exc}") from exc
        emit_counter(TASKQ_COMPLETIONS, {"outcome": CANCELLED})
        return old

    def defer(self, task_id: str, until: float, *, reason: str) -> bool:
        """Keep a waiting row waiting, but not eligible before *until*.

        The memory-posture gate uses this instead of refusing: the row stays
        accepted, holds nothing, and the dispatcher skips it until the clock
        passes ``next_run_at``.
        """
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE tasks SET next_run_at=?, updated_at=? "
                    f"WHERE id=? AND state IN {_SQL_CLAIMABLE}",
                    (float(until), ts, task_id),
                )
                if cur.rowcount == 1:
                    self._append_event_in_tx(
                        conn, task_id, "deferred", {"until": float(until), "reason": reason}
                    )
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task defer failed: {exc}") from exc
        return cur.rowcount == 1

    # -- waits (taskq.waits) ------------------------------------------------

    def enter_wait(
        self, task_id: str, record: dict[str, Any], *, generation: int | None = None
    ) -> bool:
        """``running -> <record["state"]>`` with the wait record on the row.

        One write: the state, the record and a ``wait`` event land together or
        not at all. The lane slot is the caller's to release (admission owns
        it); the generation is NOT bumped, because the same run continues.
        """
        state = str(record.get("state") or "")
        if state not in WAITING:
            raise InvalidTransition("?", state)
        return self.transition(
            task_id,
            state,
            generation=generation,
            wait=record,
            detail={
                "wait": True,
                "reason": str(record.get("reason") or "")[:200],
                "resume": (record.get("resume_condition") or {}).get("kind"),
                "slot_released": bool(record.get("slot_released", True)),
                "residency_charged": bool(record.get("residency_charged", True)),
            },
        )

    def update_wait(
        self, task_id: str, record: dict[str, Any], *, generation: int | None = None
    ) -> bool:
        """Rewrite the wait record of a row already in ``record["state"]``.

        For a children wait that gains an awaited child after entry. Same
        state, same generation: no transition, one ``wait_updated`` event.
        """
        state = str(record.get("state") or "")
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                args: list[Any] = [
                    json.dumps(record, sort_keys=True, default=str),
                    ts,
                    task_id,
                    state,
                ]
                sql = "UPDATE tasks SET wait_json=?, updated_at=? WHERE id=? AND state=?"
                if generation is not None:
                    sql += " AND generation=?"
                    args.append(int(generation))
                cur = conn.execute(sql, args)
                if cur.rowcount == 1:
                    self._append_event_in_tx(
                        conn,
                        task_id,
                        "wait_updated",
                        {"ids": list((record.get("resume_condition") or {}).get("ids") or [])},
                    )
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"wait update failed: {exc}") from exc
        return cur.rowcount == 1

    def wake_wait(
        self,
        task_id: str,
        *,
        reason: str,
        generation: int | None = None,
        to: str = RETRY_WAIT,
        only_from: frozenset[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int | None:
        """End a wait under generation+1; returns the new generation.

        ``to`` is where the row goes: ``running`` ONLY when the caller holds
        the lane slot and the runtime is resident (admission's ``resume_grant``,
        called after the pump granted the slot); every other wake -- a runner
        coroutine that still has to reacquire its lane, a parked row whose run
        is gone, an answer delivered to a ``waiting_input`` row -- lands in
        ``retry_wait`` with ``next_run_at = now`` and no lease: claimable at
        once, so the dispatcher / the runner's re-admission claims it and writes
        ``running`` only when capacity was actually granted. A crash in that
        gap leaves a claimable row, not a ``running`` row with a dead owner
        that reconciles to ``unknown_side_effect``.

        ``only_from`` narrows the default fence -- any state in
        :data:`model.WAITING` -- to the wait the caller's wake ANSWERS, exactly
        as :meth:`cancel`'s does for a cancel: a wake carries the answer to ONE
        question, and ``waiting_input`` / ``waiting_dependency`` /
        ``waiting_children`` are different questions, so a caller that read one
        of them names it here and a row that moved to another between the read
        and this call is refused instead of having the wrong wait ended. The
        refusal is the ``rejected_transition`` the default fence already
        records, and the predicate is tested in THIS transaction.

        The bump is the re-admission fence (RFC §14.2): callbacks issued during
        the wait carry the old generation and are rejected as ``stale_result``.
        None when the row is missing, not waiting (or not in ``only_from``), or
        the generation is stale.
        """
        if to not in (RUNNING, RETRY_WAIT):
            raise ValueError(f"wake target must be running or retry_wait, not {to!r}")
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT state, generation FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                old, gen = str(row["state"]), int(row["generation"])
                if generation is not None and int(generation) != gen:
                    self._append_event_in_tx(
                        conn,
                        task_id,
                        "stale_result",
                        {"from_generation": int(generation), "current": gen, "wanted": to},
                    )
                    conn.execute("COMMIT")
                    return None
                fence = WAITING if only_from is None else (WAITING & only_from)
                if old not in fence:
                    self._append_event_in_tx(
                        conn, task_id, "rejected_transition", {"from": old, "to": to}
                    )
                    conn.execute("COMMIT")
                    return None
                if to == RUNNING:
                    conn.execute(
                        "UPDATE tasks SET state=?, generation=generation+1, wait_json=NULL, "
                        "lease_owner=?, lease_expires_at=?, updated_at=? "
                        "WHERE id=? AND state=? AND generation=?",
                        (RUNNING, self._incarnation, ts + self._lease_secs, ts, task_id, old, gen),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET state=?, generation=generation+1, wait_json=NULL, "
                        "lease_owner=NULL, lease_expires_at=NULL, next_run_at=?, updated_at=? "
                        "WHERE id=? AND state=? AND generation=?",
                        (RETRY_WAIT, ts, ts, task_id, old, gen),
                    )
                data: dict[str, Any] = {
                    "from": old,
                    "to": to,
                    "reason": reason[:200],
                    "generation": gen + 1,
                }
                if detail:
                    data.update(detail)
                self._append_event_in_tx(conn, task_id, "wake", data)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task wake failed: {exc}") from exc
        return gen + 1

    @_typed_read
    def children_of(self, parent_id: str) -> list[TaskRecord]:
        with self._lock:
            rows = (
                self._c()
                .execute(
                    "SELECT * FROM tasks WHERE parent_id=? ORDER BY created_at, rowid",
                    (parent_id,),
                )
                .fetchall()
            )
        return [TaskRecord.from_row(r) for r in rows]

    @_typed_read
    def waiting_rows(self, *, state: str | None = None) -> list[TaskRecord]:
        """Rows in a ``WAITING`` state (one state when given), oldest first."""
        if state is not None:
            if state not in WAITING:
                return []
            sql, args = "SELECT * FROM tasks WHERE state=? ORDER BY created_at, rowid", [state]
        else:
            sql, args = (
                f"SELECT * FROM tasks WHERE state IN {_SQL_WAITING} ORDER BY created_at, rowid",
                [],
            )
        with self._lock:
            rows = self._c().execute(sql, args).fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    @_typed_read
    def orphaned_children(self) -> list[TaskRecord]:
        """Non-terminal rows whose parent row is terminal: nothing will collect them."""
        with self._lock:
            rows = (
                self._c()
                .execute(
                    "SELECT c.* FROM tasks c JOIN tasks p ON p.id=c.parent_id "
                    f"WHERE c.state NOT IN {_SQL_TERMINAL} AND p.state IN {_SQL_TERMINAL} "
                    "ORDER BY c.created_at, c.rowid"
                )
                .fetchall()
            )
        return [TaskRecord.from_row(r) for r in rows]

    def record_progress(self, task_id: str, generation: int, progress: dict[str, Any]) -> bool:
        """Store the latest progress marker; stale generations are ignored."""
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET progress_json=?, updated_at=? WHERE id=? AND generation=?",
                    (
                        json.dumps(progress, sort_keys=True, default=str),
                        ts,
                        task_id,
                        int(generation),
                    ),
                )
            except sqlite3.Error as exc:
                raise TaskStoreUnavailable(f"progress write failed: {exc}") from exc
        return cur.rowcount == 1

    def restamp_accepted_by(self, task_id: str, incarnation: str) -> bool:
        """Name *incarnation* as the row's accepter, leaving state and generation put.

        ``params.accepted_by`` is the owner test for a row that has never been
        claimed and so carries no lease (``adapters.runner._orphaned_queued_rows``),
        and the row a caller re-adopts is handed back with the params it was
        first inserted with. Without this the sweep still reads the incarnation
        that accepted it originally.
        """
        ts = self.now()
        with self._lock:
            conn = self._c()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET params_json=json_set(params_json, '$.accepted_by', ?), "
                    "updated_at=? WHERE id=?",
                    (str(incarnation), ts, task_id),
                )
            except sqlite3.Error as exc:
                raise TaskStoreUnavailable(f"accepted_by restamp failed: {exc}") from exc
        return cur.rowcount == 1

    # -- reads ---------------------------------------------------------------

    @_typed_read
    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._c().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return TaskRecord.from_row(row) if row is not None else None

    @_typed_read
    def state_of(self, task_id: str) -> str | None:
        with self._lock:
            row = self._c().execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        return str(row["state"]) if row is not None else None

    @staticmethod
    def _exclusion(exclude_ids: Sequence[str]) -> tuple[str, list[Any]]:
        ids = [str(i) for i in exclude_ids if i]
        if not ids:
            return "", []
        return f" AND id NOT IN ({', '.join('?' for _ in ids)})", ids

    # -- fairness lanes (``taskq.lanes``) ---------------------------------------

    @staticmethod
    def _lane_in_tx(conn: sqlite3.Connection, rec: TaskRecord) -> str:
        """The lane a new row is filed under, resolved inside the accept txn.

        An explicit ``params["lane"]`` (the runner adapters name the lane from
        the run's source) wins. Otherwise a nested row takes its parent's lane
        (which is the root's, by induction); a root row is keyed by its
        session (automation keys fold into ``system``). A parent row that is
        gone leaves the child under its own ``subagent:<id>`` key rather than
        guessed into another lane.
        """
        explicit = str(rec.params.get("lane") or "") if isinstance(rec.params, dict) else ""
        if explicit:
            return explicit
        if rec.parent_id:
            row = conn.execute("SELECT lane FROM tasks WHERE id=?", (rec.parent_id,)).fetchone()
            if row is not None and row[0]:
                return str(row[0])
        return _lanes.lane_key_for(rec.session_key)

    @_typed_read
    def pending_lanes(
        self,
        kind: str,
        *,
        exclude_ids: Sequence[str] = (),
        children_only: bool = False,
    ) -> dict[str, int]:
        """Eligible (claimable now) row count per lane, for the fair dispatcher."""
        ts = self.now()
        excl_sql, excl_args = self._exclusion(exclude_ids)
        child_sql = " AND parent_id IS NOT NULL AND parent_id <> ''" if children_only else ""
        with self._lock:
            rows = (
                self._c()
                .execute(
                    f"SELECT lane, COUNT(*) FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                    "AND (next_run_at IS NULL OR next_run_at<=?) "
                    "AND (lease_expires_at IS NULL OR lease_expires_at<?)"
                    f"{child_sql}{excl_sql} GROUP BY lane",
                    [kind, ts, ts, *excl_args],
                )
                .fetchall()
            )
        return {str(r[0] or ""): int(r[1]) for r in rows}

    @_typed_read
    def fetch_dispatchable_fair(
        self,
        kind: str,
        *,
        limit: int,
        scheduler: "_lanes.LaneScheduler",
        exclude_ids: Sequence[str] = (),
        children_only: bool = False,
        lanes: Sequence[str] | None = None,
        per_lane_limit: int | None = None,
    ) -> list[TaskRecord]:
        """Claimable-now rows of *kind* in weighted round-robin order across lanes.

        Each lane contributes its oldest eligible rows (FIFO by ``created_at``
        inside the lane, at most *per_lane_limit* of them); *scheduler*
        interleaves the lanes, carrying its credit across calls. ``lanes``
        restricts the read to those lanes; ``children_only`` to nested rows
        (``parent_id`` set) -- the rows allowed to take the reserved child
        slot. With one lane pending this is exactly the FIFO order of
        :meth:`fetch_dispatchable`.
        """
        if limit <= 0:
            return []
        if lanes is not None and not lanes:
            return []
        ts = self.now()
        excl_sql, excl_args = self._exclusion(exclude_ids)
        child_sql = " AND parent_id IS NOT NULL AND parent_id <> ''" if children_only else ""
        lane_sql, lane_args = ("", [])
        if lanes is not None:
            lane_sql = f" AND lane IN ({', '.join('?' for _ in lanes)})"
            lane_args = [str(x) for x in lanes]
        rank = int(limit if per_lane_limit is None else max(1, per_lane_limit))
        with self._lock:
            rows = (
                self._c()
                .execute(
                    "SELECT * FROM ("
                    "SELECT *, ROW_NUMBER() OVER (PARTITION BY lane ORDER BY created_at, rowid) "
                    f"AS lane_rank FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                    "AND (next_run_at IS NULL OR next_run_at<=?) "
                    "AND (lease_expires_at IS NULL OR lease_expires_at<?)"
                    f"{child_sql}{lane_sql}{excl_sql}) WHERE lane_rank<=? ORDER BY lane, lane_rank",
                    [kind, ts, ts, *lane_args, *excl_args, rank],
                )
                .fetchall()
            )
        by_lane: dict[str, list[TaskRecord]] = {}
        for r in rows:
            rec = TaskRecord.from_row(r)
            by_lane.setdefault(rec.lane, []).append(rec)
        return scheduler.interleave(by_lane, limit=limit, age_of=lambda rec: rec.created_at)

    @_typed_read
    def fetch_dispatchable(
        self,
        kind: str,
        *,
        limit: int,
        exclude_ids: Sequence[str] = (),
        session_key: str | None = None,
    ) -> list[TaskRecord]:
        """Oldest-first rows of *kind* that a claim would succeed on right now."""
        ts = self.now()
        excl_sql, excl_args = self._exclusion(exclude_ids)
        sess_sql, sess_args = ("", [])
        if session_key is not None:
            sess_sql, sess_args = " AND session_key=?", [session_key]
        with self._lock:
            rows = (
                self._c()
                .execute(
                    f"SELECT * FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                    "AND (next_run_at IS NULL OR next_run_at<=?) "
                    "AND (lease_expires_at IS NULL OR lease_expires_at<?)"
                    f"{excl_sql}{sess_sql} ORDER BY created_at, rowid LIMIT ?",
                    [kind, ts, ts, *excl_args, *sess_args, int(limit)],
                )
                .fetchall()
            )
        return [TaskRecord.from_row(r) for r in rows]

    @_typed_read
    def list_pending(
        self,
        kind: str,
        *,
        session_key: str | None = None,
        exclude_ids: Sequence[str] = (),
        limit: int | None = None,
    ) -> list[TaskRecord]:
        """Every waiting row of *kind* (deferred ones included), oldest first.

        Unbounded by default, like :meth:`fetch_pending_by_batch` and
        :meth:`active_rows`: the caller is the Stop-all cascade
        (``cancellation.cancel_for_parent_impl`` through
        ``taskq_pending_ids_for``), which stops the rows this answer names and
        nothing else -- so a row past a cap is one the user stopped that stays
        queued and dispatchable. A caller that wants a page (a listing, a probe)
        passes *limit* and gets the oldest that many.
        """
        excl_sql, excl_args = self._exclusion(exclude_ids)
        sess_sql, sess_args = ("", [])
        if session_key is not None:
            sess_sql, sess_args = " AND session_key=?", [session_key]
        lim_sql, lim_args = ("", [])
        if limit is not None:
            lim_sql, lim_args = " LIMIT ?", [int(limit)]
        with self._lock:
            rows = (
                self._c()
                .execute(
                    f"SELECT * FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE}"
                    f"{sess_sql}{excl_sql} ORDER BY created_at, rowid{lim_sql}",
                    [kind, *sess_args, *excl_args, *lim_args],
                )
                .fetchall()
            )
        return [TaskRecord.from_row(r) for r in rows]

    @_typed_read
    def fetch_pending_by_batch(
        self, kind: str, batch_id: str, *, exclude_ids: Sequence[str] = ()
    ) -> list[TaskRecord]:
        """Waiting rows of *kind* whose params name *batch_id* (wave accounting)."""
        excl_sql, excl_args = self._exclusion(exclude_ids)
        with self._lock:
            rows = (
                self._c()
                .execute(
                    f"SELECT * FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} "
                    f"AND json_extract(params_json, '$.batch_id')=?{excl_sql} "
                    "ORDER BY created_at, rowid",
                    [kind, batch_id, *excl_args],
                )
                .fetchall()
            )
        return [TaskRecord.from_row(r) for r in rows]

    @_typed_read
    def next_eligible_at(self, kind: str) -> float | None:
        """Earliest ``next_run_at`` among waiting rows of *kind*; None when none wait."""
        with self._lock:
            row = (
                self._c()
                .execute(
                    f"SELECT MIN(COALESCE(next_run_at, 0)) FROM tasks WHERE kind=? "
                    f"AND state IN {_SQL_CLAIMABLE}",
                    (kind,),
                )
                .fetchone()
            )
        if not row or row[0] is None:
            return None
        return float(row[0])

    @_typed_read
    def count_pending(
        self,
        kind: str | None = None,
        *,
        exclude_ids: Sequence[str] = (),
        session_key: str | None = None,
        eligible_only: bool = False,
    ) -> int:
        """Rows waiting for dispatch (claimable states), optionally per session.

        ``eligible_only`` drops rows deferred past now; the default counts a
        deferred row too, because it is still accepted work the parent is owed.
        """
        excl_sql, excl_args = self._exclusion(exclude_ids)
        where = [f"state IN {_SQL_CLAIMABLE}"]
        args: list[Any] = []
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        if session_key is not None:
            where.append("session_key=?")
            args.append(session_key)
        if eligible_only:
            where.append("(next_run_at IS NULL OR next_run_at<=?)")
            args.append(self.now())
        with self._lock:
            row = (
                self._c()
                .execute(
                    f"SELECT COUNT(*) FROM tasks WHERE {' AND '.join(where)}{excl_sql}",
                    [*args, *excl_args],
                )
                .fetchone()
            )
        return int(row[0]) if row else 0

    @_typed_read
    def count(self, *, state: str | None = None, kind: str | None = None) -> int:
        where, args = [], []
        if state is not None:
            where.append("state=?")
            args.append(state)
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        sql = "SELECT COUNT(*) FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            row = self._c().execute(sql, args).fetchone()
        return int(row[0]) if row else 0

    @_typed_read
    def count_by_state(self, *, kind: str | None = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) AS n FROM tasks"
        args: list[Any] = []
        if kind is not None:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " GROUP BY state"
        with self._lock:
            rows = self._c().execute(sql, args).fetchall()
        return {str(r["state"]): int(r["n"]) for r in rows}

    @_typed_read
    def list_rows(
        self,
        *,
        state: str | None = None,
        kind: str | None = None,
        session_key: str | None = None,
        lane: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskRecord]:
        """Rows oldest-first, optionally filtered; ``lane`` is the stored
        fairness lane (every row carries one: derived at accept, backfilled by
        the v3 migration), so the filter is a SQL predicate on ``tasks_lane``."""
        where, args = [], []
        if state is not None:
            where.append("state=?")
            args.append(state)
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        if session_key is not None:
            where.append("session_key=?")
            args.append(session_key)
        if lane is not None:
            where.append("lane=?")
            args.append(lane)
        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, rowid LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._c().execute(sql, [*args, int(limit), int(offset)]).fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    @_typed_read
    def active_rows(self, *, exclude_owner: str | None = None) -> list[TaskRecord]:
        """Rows some incarnation owns; optionally only those NOT owned by *exclude_owner*."""
        sql = f"SELECT * FROM tasks WHERE state IN {_SQL_ACTIVE}"
        args: list[Any] = []
        if exclude_owner is not None:
            sql += " AND (lease_owner IS NULL OR lease_owner<>?)"
            args.append(exclude_owner)
        sql += " ORDER BY created_at, rowid"
        with self._lock:
            rows = self._c().execute(sql, args).fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    @_typed_read
    def oldest_wait_secs(self, *, kind: str | None = None) -> float:
        """Age of the oldest row still waiting for dispatch; 0 when none."""
        sql = f"SELECT MIN(created_at) FROM tasks WHERE state IN {_SQL_CLAIMABLE}"
        args: list[Any] = []
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        with self._lock:
            row = self._c().execute(sql, args).fetchone()
        if not row or row[0] is None:
            return 0.0
        return max(0.0, self.now() - float(row[0]))

    def doctor_lines(self) -> list[str]:
        """Human-readable status for ``kirocrew doctor``: depth, oldest wait, warnings."""
        by_state = self.count_by_state()
        pending = sum(by_state.get(s, 0) for s in CLAIMABLE)
        active = sum(by_state.get(s, 0) for s in ACTIVE)
        lines = [
            f"task store: {self._path} (journal_mode={self._journal_mode})",
            f"  pending={pending} active={active} oldest_wait={self.oldest_wait_secs():.0f}s",
        ]
        lines.extend(f"  warning: {w}" for w in self.warnings)
        return lines
