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
:meth:`fetch_dispatchable` refills it from the rows on disk in FIFO order. The
one in-memory fact it does keep is an index, not a decision: the ids of the
rows that are accepted and not yet started (:meth:`is_unstarted`), written
through after every committed write that moves a row into or out of that set
and loaded at :meth:`open`, so an
event-loop caller can ask "is this id still pending?" without the SQLite read
the loop must never take.

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
from kiro_crew.owner_only_files import prepare_owner_only_sqlite
from kiro_crew.sqlite_quarantine import is_damaged_database_error, quarantine_sqlite_file

from . import lanes as _lanes
from . import migrate
from . import store_events as _store_events
from . import store_reads as _store_reads
from . import store_waits as _store_waits
from . import store_work as _store_work
from .model import ADOPTED_RESUME_EVENT  # noqa: F401
from .model import RUNNING  # noqa: F401
from .model import TaskEvent  # noqa: F401
from .model import (
    ACTIVE,
    ADMITTED,
    CANCELLED,
    CLAIMABLE,
    LEASE_SECS,
    QUEUED,
    RECOVERING,
    RETRY_WAIT,
    TERMINAL,
    WAITING,
    WAITING_INFRA,
    InvalidTransition,
    TaskRecord,
    check_transition,
    is_terminal,
    steps_to,
)
from .store_reads import _SQL_CHILD_ONLY  # noqa: F401
from .store_vocab import (
    _SQL_ACTIVE,
    _SQL_CLAIMABLE,
    _SQL_TERMINAL,
    _SQL_UNSTARTED,
    _UNSTARTED,
    TaskStoreUnavailable,
    _typed_read,
)
from .store_waits import _SQL_WAITING  # noqa: F401
from .store_work import WorkRow  # noqa: F401

logger = logging.getLogger(__name__)

#: The modules ``TaskStore`` is composed from, lowest layer first. ``store_vocab`` holds
#: the SQL fragments several owners share and ``TaskStoreUnavailable``; each other owner
#: holds methods the class binds by name (``events = _store_events.events``), so the class
#: attribute IS the owner's function and every field stays on the instance. An owner
#: imports only modules listed before it, never this one at run time, and a name a test
#: patches through this module is read only by code this module keeps.
_STORE_OWNERS = (
    "kiro_crew.taskq.store_vocab",
    "kiro_crew.taskq.store_events",
    "kiro_crew.taskq.store_waits",
    "kiro_crew.taskq.store_reads",
    "kiro_crew.taskq.store_work",
)

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
#    a single ``cancel`` (``cancel_impl``): the row has to be cancelled before a
#    drain can claim it AND out of the in-memory window before a stagger timer
#    can start it, and an await between those two is a race in either order.
#    Stop all is restructured out of this class: it queues one writer-thread job
#    for every row's cancel and only then drops the window entries, both before
#    its first await (``taskq_post_cancel_queued``).
#
# Those takes are on-loop and cannot simply be offloaded, so arming this from
# ``KIROCREW_DEV_MODE`` would raise on a cancel -- an unqueue that raises leaves
# that row waiting and fails the request -- and the developer's rational
# response, unsetting that variable, silences every OTHER surface's guard too.
# Flip it back to True once both classes are either restructured or inside a
# vetted ``allow_on_loop()`` block.
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

#: Ids per ``IN (...)`` list in a multi-row event read: under the 999
#: host-parameter ceiling of an older SQLite build.
_EVENT_ID_CHUNK = 500
#: The key a terminal ``transition`` event carries when its writer still owes
#: the row a report (``finish(report_owed=True)``), and the event that clears it.
_REPORT_OWED_BY = "report_owed_by"
_EVENT_REPORTED = "reported"


class _CorruptStore(Exception):
    """Internal: the file is not a usable SQLite database (see ``TaskStore.open``)."""


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
            # ``>=`` so that, among mounts sharing the longest matching point,
            # the LAST wins: /proc/mounts lists an over-mount after the mount it
            # covers, so the last same-length entry is the one visible at that
            # path (a tmpfs over an ext4 /tmp reads as tmpfs).
            if best is None or len(mount) >= best[0]:
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
        #: Ids of the rows in an :data:`_UNSTARTED` state as of the last commit
        #: on this connection: :meth:`is_unstarted`'s answer. Written through by
        #: :meth:`_note_state` after every committed write that can move a row
        #: into or out of that set (all of them run in this class) and reloaded
        #: by :meth:`open`. Guarded by its own
        #: lock, never ``_lock``, for the reason ``_executor_lock`` gives: the
        #: reader is the event loop, and ``_lock`` is held across the busy wait.
        self._unstarted_ids: set[str] = set()
        self._unstarted_lock = threading.Lock()

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
            self._load_unstarted_ids(self._conn)
            return self

    def _load_unstarted_ids(self, conn: sqlite3.Connection) -> None:
        """Seed :meth:`is_unstarted` from the rows already on disk.

        A row accepted by an earlier incarnation, or imported at boot, is
        pending work too; the write-through in :meth:`_note_state` only sees
        this connection's own writes from here on. A read that fails leaves the
        index empty rather than refusing the open: the index is an answer for a
        probe, and an open that refused over it would refuse every spawn.
        """
        try:
            rows = conn.execute(f"SELECT id FROM tasks WHERE state IN {_SQL_UNSTARTED}").fetchall()
        except sqlite3.Error:
            logger.warning("taskq: could not load the unstarted-row index", exc_info=True)
            rows = []
        with self._unstarted_lock:
            self._unstarted_ids = {str(row["id"]) for row in rows}

    def _note_state(self, task_id: str, state: str) -> None:
        """Record *task_id*'s committed *state* in the unstarted-row index.

        Called after the ``COMMIT`` of every write that can move a row into or
        out of :data:`_UNSTARTED`, and only then, so a rolled-back write never
        reaches the index. :meth:`claim` is the one state write that skips it:
        claimable to ``admitted`` stays inside the set.
        """
        with self._unstarted_lock:
            if state in _UNSTARTED:
                self._unstarted_ids.add(task_id)
            else:
                self._unstarted_ids.discard(task_id)

    def is_unstarted(self, task_id: str) -> bool:
        """Whether *task_id* is accepted and not yet started (claimable or admitted).

        Safe ON the event loop: an in-memory set read under a lock no I/O is
        ever done under -- it never takes ``_lock`` or reaches :meth:`_c`, so
        it neither waits out ``busy_timeout`` nor trips the on-loop guard. The
        answer is this process's committed view: a write by another connection
        on the same file is not seen until the next :meth:`open`.
        """
        with self._unstarted_lock:
            return task_id in self._unstarted_ids

    def _open_connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            platform_compat.restrict_dir_to_owner(self._path.parent)
            prepare_owner_only_sqlite(self._path)
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
            if is_damaged_database_error(exc):
                raise _CorruptStore(str(exc)) from exc
            raise TaskStoreUnavailable(f"cannot open task store {self._path}: {exc}") from exc
        return conn

    def _quarantine_corrupt_file(self, reason: str) -> Path:
        """Move the corrupt file and its sidecars aside (``sqlite_quarantine``).

        Never overwrites, and a sidecar left behind is logged and reported,
        never a reason to keep the corrupt database in place.
        """
        try:
            moved = quarantine_sqlite_file(self._path)
        except OSError as exc:
            raise TaskStoreUnavailable(
                f"task store {self._path} is corrupt ({reason}) and could not be "
                f"quarantined: {exc}"
            ) from exc
        message = (
            f"task store {self._path} was corrupt ({reason}); quarantined as {moved.target.name} "
            f"({', '.join(moved.moved)}) and recreated empty. Accepted work that "
            "only lived in the old file is NOT recovered: inspect the quarantined copy."
        )
        if moved.left:
            message += f" Sidecar(s) still beside the recreated file: {', '.join(moved.left)}."
        logger.warning("taskq: %s", message)
        self.warnings.append(message)
        return moved.target

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
        return await self.post(fn, *args, **kwargs)

    def post(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> "asyncio.Future[Any]":
        """Queue ``fn(*args, **kwargs)`` on the writer thread NOW; return its future.

        :meth:`run` without the wait, for a caller that must not await before
        the job is queued: the single worker runs jobs in submission order, so
        a write posted here lands ahead of any job queued after this call
        returns -- whichever task queues it, and however soon.
        """
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._writer_executor(), functools.partial(fn, *args, **kwargs))

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

    @_typed_read
    def deferred_longer_than(
        self,
        kind: str,
        bound: float,
        *,
        exclude_ids: Sequence[str] = (),
        limit: int = 64,
    ) -> list[TaskRecord]:
        """Waiting rows of *kind* parked by :meth:`defer` NOW that have spent *bound* seconds parked.

        Waiting means any state :meth:`defer` parks (``CLAIMABLE``: ``queued``,
        ``retry_wait``, ``recovering``), so a restart survivor or a retried run
        the memory gates keep deferring is bounded too. Oldest first, at most *limit*. Parked now means ``next_run_at`` is still
        in the future: a row whose deferral has lapsed is eligible again and waits
        for whatever picks it, which is not a deferral.

        The time counted is the time the row was actually PARKED in its current
        wait: each ``deferred`` event after the row's last ``claimed`` or
        ``transition`` event (the closers the queued listing applies too,
        ``_defer_details`` in the admission bridge) parks it from its ``ts`` to
        its ``until``, cut short by the next deferral or by now. A gap between a
        lapsed deferral and the next one is NOT counted: the row was eligible
        then and waited for a slot to be re-checked in, not for memory. So a row
        that was held back once and then queued behind a full cap is measured by
        its parked time alone, while a row the host keeps short (re-checked every
        admit wait) accrues almost all of its wall-clock time. Every re-check
        appends one more event, so the clock is never restarted by one; a claim
        or a re-queue ends the wait and the next deferral starts a new one.
        """
        excl_sql, excl_args = self._exclusion(exclude_ids)
        now = self.now()
        cutoff = now - float(bound)
        with self._lock:
            conn = self._c()
            # The SQL keeps only rows whose current wait BEGAN at or before the
            # cutoff -- necessary, since parked time never exceeds the time since
            # the first deferral -- so the event walk below reads only rows that
            # could be past the bound.
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE kind=? AND state IN {_SQL_CLAIMABLE} AND next_run_at>?"
                f"{excl_sql} AND (SELECT MIN(d.ts) FROM task_events d "
                "WHERE d.task_id=tasks.id AND d.kind='deferred' AND d.seq > "
                "COALESCE((SELECT MAX(c.seq) FROM task_events c WHERE c.task_id=tasks.id "
                "AND c.kind IN ('claimed', 'transition')), 0)) <= ? "
                "ORDER BY created_at, rowid",
                [kind, now, *excl_args, cutoff],
            ).fetchall()
            records = [TaskRecord.from_row(r) for r in rows]
            parked: dict[str, list[tuple[float, float]]] = {rec.id: [] for rec in records}
            ids = list(parked)
            for i in range(0, len(ids), _EVENT_ID_CHUNK):
                chunk = ids[i : i + _EVENT_ID_CHUNK]
                marks = ", ".join("?" for _ in chunk)
                for ev in conn.execute(
                    "SELECT e.task_id, e.ts, e.data_json FROM task_events e "
                    f"WHERE e.task_id IN ({marks}) AND e.kind='deferred' AND e.seq > "
                    "COALESCE((SELECT MAX(c.seq) FROM task_events c WHERE c.task_id=e.task_id "
                    "AND c.kind IN ('claimed', 'transition')), 0) ORDER BY e.task_id, e.seq",
                    chunk,
                ).fetchall():
                    try:
                        data = json.loads(ev["data_json"])
                        until = float(data["until"])
                    except (TypeError, ValueError, KeyError):
                        until = float(ev["ts"])
                    parked[str(ev["task_id"])].append((float(ev["ts"]), until))
        out: list[TaskRecord] = []
        for rec in records:
            spans = parked[rec.id]
            total = 0.0
            for n, (ts, until) in enumerate(spans):
                end = spans[n + 1][0] if n + 1 < len(spans) else now
                total += max(0.0, min(until, end, now) - ts)
            if total >= float(bound):
                out.append(rec)
                if len(out) >= int(limit):
                    break
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
                    if rec.state not in CLAIMABLE:
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
        for rec in batch:
            self._note_state(rec.id, rec.state)
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
        if inserted:
            self._note_state(record.id, record.state)
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
        fenced against a later re-dispatch. The earlier attempt's progress
        marker is cleared in the same statement, so the new attempt starts
        with none.
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
                    "progress_json=NULL, updated_at=? "
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
        # No index write: a claim moves a claimable row to ``admitted``, and
        # both are unstarted.
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
        self._note_state(task_id, new_state)
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
        report_owed: bool = False,
    ) -> bool:
        """Write a terminal state; a stale generation or an already-terminal row is a no-op.

        *report_owed* records, in the same transaction, that the writer still
        owes this terminal a report it has not made yet: the row is named by
        :meth:`owed_reports` until :meth:`mark_reported` clears it, so a process
        lost between this commit and the report leaves the next one the work.
        """
        if not is_terminal(state):
            raise InvalidTransition("?", state)
        detail: dict[str, Any] = {"error": error[:500]} if error else {}
        if report_owed:
            detail[_REPORT_OWED_BY] = self.incarnation
        return self.transition(
            task_id, state, generation=generation, result_ref=result_ref, detail=detail or None
        )

    def mark_reported(self, task_id: str) -> None:
        """Clear the report a :meth:`finish` with ``report_owed`` left owing."""
        self.append_event(task_id, _EVENT_REPORTED)

    @_typed_read
    def owed_reports(
        self, kind: str, *, limit: int = 256, after: tuple[float, str] | None = None
    ) -> list[TaskRecord]:
        """Terminal rows of *kind* an EARLIER incarnation owed a report it never made.

        A row whose terminal ``transition`` carries the owing incarnation, with no
        ``reported`` event after it, oldest terminal first (``updated_at``, then
        ``id``). This incarnation's own rows are left out: their reports are in
        flight in this process. *after* is the ``(updated_at, id)`` of the last
        row of the previous page; only rows ordered after it are named, so a
        caller pages through every owed row without re-reading one whose clear
        has not landed yet.
        """
        cursor_sql = ""
        args: list[Any] = [self.incarnation, kind, _EVENT_REPORTED]
        if after is not None:
            cursor_sql = "AND (t.updated_at>? OR (t.updated_at=? AND t.id>?)) "
            args += [float(after[0]), float(after[0]), str(after[1])]
        with self._lock:
            rows = (
                self._c()
                .execute(
                    "SELECT t.* FROM tasks t JOIN task_events e ON e.task_id=t.id "
                    "AND e.kind='transition' "
                    f"AND json_extract(e.data_json, '$.{_REPORT_OWED_BY}') IS NOT NULL "
                    f"AND json_extract(e.data_json, '$.{_REPORT_OWED_BY}')<>? "
                    f"WHERE t.kind=? AND t.state IN {_SQL_TERMINAL} AND NOT EXISTS ("
                    "SELECT 1 FROM task_events r WHERE r.task_id=t.id AND r.kind=? "
                    f"AND r.seq>e.seq) {cursor_sql}ORDER BY t.updated_at, t.id LIMIT ?",
                    [*args, int(limit)],
                )
                .fetchall()
            )
        return [TaskRecord.from_row(r) for r in rows]

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
        self._note_state(task_id, CANCELLED)
        emit_counter(TASKQ_COMPLETIONS, {"outcome": CANCELLED})
        return old

    def defer(self, task_id: str, *, wait: float, reason: str) -> bool:
        """Keep a waiting row waiting, but not eligible for *wait* more seconds.

        The spawn gate's memory floor uses this instead of refusing: the row stays
        accepted, holds nothing, and the dispatcher skips it until the clock
        passes ``next_run_at``.

        ``next_run_at`` is the event's own ``ts`` plus *wait*, both from one
        clock read, so the park spans exactly *wait* in
        :meth:`deferred_longer_than`, for whole-second waits on an epoch clock.
        A caller-computed ``now() + wait`` could not promise that: the clock
        moves between the caller's read and this one.
        """
        ts = self.now()
        until = ts + float(wait)
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
                        conn,
                        task_id,
                        "deferred",
                        {"until": float(until), "reason": reason},
                        ts=ts,
                    )
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task defer failed: {exc}") from exc
        return cur.rowcount == 1

    def expedite(self, task_ids: Sequence[str]) -> int:
        """Make the deferred rows among *task_ids* eligible now; how many were.

        The early end of a :meth:`defer`: a waiting row whose ``next_run_at`` is
        still ahead is brought to now, so the next dispatch read may take it.
        It appends NO event. The deferral's own ``deferred`` event stays the
        record of the park, and :meth:`deferred_longer_than` counts only a row
        parked now, so a row brought forward and then claimed is never counted.
        One brought forward and parked AGAIN before its old ``until`` is
        charged the gap between the two: that span is cut at the next
        deferral, not at this call, so at most what was left of the deferral
        counts. The memory wake brings forward only rows that now fit, so that
        gap is a re-check that lost a race. A row that is not waiting, or not
        deferred, is left as it is.
        """
        ids = [str(i) for i in task_ids if i]
        if not ids:
            return 0
        ts = self.now()
        changed = 0
        with self._lock:
            conn = self._c()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for i in range(0, len(ids), _EVENT_ID_CHUNK):
                    chunk = ids[i : i + _EVENT_ID_CHUNK]
                    marks = ", ".join("?" for _ in chunk)
                    cur = conn.execute(
                        "UPDATE tasks SET next_run_at=?, updated_at=? "
                        f"WHERE id IN ({marks}) AND state IN {_SQL_CLAIMABLE} "
                        "AND next_run_at>?",
                        (ts, ts, *chunk, ts),
                    )
                    changed += max(0, cur.rowcount)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback(conn)
                raise TaskStoreUnavailable(f"task expedite failed: {exc}") from exc
        return changed

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

    # The members below are bound from the ``store_*`` owners (``_STORE_OWNERS``). A
    # ``_typed_read`` member's binding declares the type its decorator gives it, so a module
    # mypy checks before this class body reads that type instead of waiting on the inference.

    # The task event log: ``kiro_crew.taskq.store_events``.
    _append_event_in_tx = _store_events._append_event_in_tx
    append_event = _store_events.append_event
    lost_owner_recoveries: Callable[..., Any] = _store_events.lost_owner_recoveries
    events: Callable[..., Any] = _store_events.events
    latest_events: Callable[..., Any] = _store_events.latest_events

    # Wait rows: ``kiro_crew.taskq.store_waits``.
    enter_wait = _store_waits.enter_wait
    update_wait = _store_waits.update_wait
    wake_wait = _store_waits.wake_wait
    children_of: Callable[..., Any] = _store_waits.children_of
    waiting_rows: Callable[..., Any] = _store_waits.waiting_rows
    orphaned_children: Callable[..., Any] = _store_waits.orphaned_children

    # Accepted-work admission rows: ``kiro_crew.taskq.store_work``.
    work_insert_or_get = _store_work.work_insert_or_get
    work_compare_and_set = _store_work.work_compare_and_set
    work_get: Callable[..., Any] = _store_work.work_get
    work_by_scope: Callable[..., Any] = _store_work.work_by_scope
    work_rows: Callable[..., Any] = _store_work.work_rows
    work_purge = _store_work.work_purge
    storage_charge = _store_work.storage_charge
    storage_bytes: Callable[..., Any] = _store_work.storage_bytes

    # Row reads, dispatch reads and counts: ``kiro_crew.taskq.store_reads``.
    get: Callable[..., Any] = _store_reads.get
    state_of: Callable[..., Any] = _store_reads.state_of
    _admitted_exclusion = staticmethod(_store_reads._admitted_exclusion)
    _exclusion = staticmethod(_store_reads._exclusion)
    pending_lanes: Callable[..., Any] = _store_reads.pending_lanes
    fetch_dispatchable_fair: Callable[..., Any] = _store_reads.fetch_dispatchable_fair
    fetch_dispatchable: Callable[..., Any] = _store_reads.fetch_dispatchable
    list_pending: Callable[..., Any] = _store_reads.list_pending
    fetch_pending_by_batch: Callable[..., Any] = _store_reads.fetch_pending_by_batch
    next_eligible_at: Callable[..., Any] = _store_reads.next_eligible_at
    count_pending: Callable[..., Any] = _store_reads.count_pending
    count: Callable[..., Any] = _store_reads.count
    count_by_state: Callable[..., Any] = _store_reads.count_by_state
    list_rows: Callable[..., Any] = _store_reads.list_rows
    active_rows: Callable[..., Any] = _store_reads.active_rows
    oldest_wait_secs: Callable[..., Any] = _store_reads.oldest_wait_secs
    overdue_unleased_rows: Callable[..., Any] = _store_reads.overdue_unleased_rows
