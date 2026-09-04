"""The asynchronous backup sidecar: the long-running copier.

One public seam, ``run_sidecar(settings)`` (see ``container/CONTRACT.md``). The
real work is factored into ``run_backup_cycle`` so a single pass can be tested
directly against a fake store and real temporary files.

Three on-disk facts shape the copier, each proven wrong-if-ignored by a test:

1. The transcript is atomically replaced, sometimes shorter. So we upload whole
   objects (``store.put`` has no offset/append). An incremental splice would be
   incorrect, not merely slow.
2. mtime is restored after every rewrite. So change detection is size PLUS a
   content hash, never mtime.
3. Writes hold a per-session advisory ``flock`` on ``<transcript>.lock``. So a
   read of a live transcript takes a shared ``flock`` on the same sidecar file
   and waits, rather than reading a half-replaced file.

Artifacts are write-once and heavy: an object already recorded in the state is
skipped without being re-hashed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..common import Settings
from . import layout
from .state import BackupState, ObjMeta, backup_status, state_path
from .store import ObjectStore, S3ObjectStore

logger = logging.getLogger("smc.backup.sidecar")

# How long a single file read will wait for the writer's lock before giving up
# for this cycle. Bounded so one wedged writer cannot stall the whole loop; the
# file is simply retried next cycle. The design accepts lag.
LOCK_WAIT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

__all__ = ["run_sidecar", "run_backup_cycle", "CycleResult", "backup_status"]


@dataclass
class CycleResult:
    scanned: int = 0
    uploaded: int = 0
    skipped_unchanged: int = 0
    skipped_artifact: int = 0
    deferred_locked: int = 0
    uploaded_bytes: int = 0


#: Guarded because NEITHER constant exists on every platform; see the twin in
#: ``packaging/build.py``. They are separate on purpose -- this tree is the source
#: of a container image and cannot import that module -- and
#: ``test_backup_swap_race.py`` pins the two to the same value.
_NOFOLLOW_READ_FLAGS: int = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class _Contended(Exception):
    """The writer's lock could not be taken within the wait budget."""


class _NotARegularFile(Exception):
    """What was opened is not a regular file, so it is not a backup candidate."""


def _open_nofollow_under(root: Path, path: Path) -> int:
    """Open ``path`` walking each component under ``root``, following no link at all.

    ``O_NOFOLLOW`` on a whole path constrains only the FINAL component, which this
    module said out loud it was not closing: an agent that controls a nested path can
    swap a PARENT directory for a link to ``/proc/self`` after validation, and the
    final-component check then happily opens ``environ``. The review asked for the
    real fix rather than the documented gap.

    So each component is opened relative to the previous descriptor with
    ``O_NOFOLLOW`` set, which makes a swapped directory fail at the component that
    was swapped instead of being traversed. ``root`` itself is opened normally: it is
    the container's own data home, fixed by the task definition, not something the
    agent names.

    Falls back to a single ``O_NOFOLLOW`` open where ``dir_fd`` is unsupported
    (Windows). That is a real narrowing and is why it is spelled as a branch rather
    than hidden: the sidecar only runs inside the Linux image, so the fallback exists
    to keep the module importable and testable off-platform, not to be relied on.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        return os.open(str(path), flags | (_NOFOLLOW_READ_FLAGS & ~getattr(os, "O_NOFOLLOW", 0)))

    rel = path.relative_to(root).parts
    dir_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        return os.open(rel[-1], flags | os.O_NONBLOCK, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _read_nofollow(path: Path, root: Path | None = None) -> bytes:
    """Read ``path``, refusing a symlink AT OPEN TIME rather than before it.

    ``layout.enumerate_*`` already rejects a symlink it can see, but that check and
    this read are two separate moments, and the agent writes into this tree. Between
    them it can replace an enumerated regular file with a link to
    ``/proc/self/environ``, and the read would follow it and upload the task's own
    credentials to the owner's bucket. Checking harder beforehand cannot close that:
    the fix is to make the check and the read the same operation.

    ``O_NOFOLLOW`` fails with ``ELOOP`` when the final component is a link, and the
    ``fstat`` confirms that what was actually opened is a regular file -- so the bytes
    returned are the bytes of the thing that passed the test.

    EVERY component is checked, not just the last. When a ``root`` is given the path
    is walked one component at a time under it, each opened with ``O_NOFOLLOW``, so a
    swapped PARENT directory fails at the component that was swapped. Without a
    ``root`` this falls back to a single whole-path open, which constrains the final
    component only -- callers inside the backup tree always pass one.
    ``O_NONBLOCK`` is on the open for a reason found by test: a FIFO left in the tree
    makes a plain ``os.open`` block forever waiting for a writer, so the ``fstat``
    below never runs and every later backup cycle queues behind it. Opening
    non-blocking gets the descriptor first and lets the regular-file check do its job.
    It is cleared afterwards so the reads themselves are ordinary blocking reads.
    """
    if root is not None:
        fd = _open_nofollow_under(root, path)
    else:
        fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW_READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NotARegularFile(str(path))
        # A regular file is confirmed, so restore blocking semantics before reading:
        # a non-blocking read on a regular file is fine, but clearing the flag keeps
        # the loop below identical to an ordinary read.
        # Only when O_NONBLOCK was actually applied. On Windows neither that flag
        # nor set_blocking() works on a regular-file descriptor -- it raises
        # WinError 87 -- and there is nothing to undo there anyway. Located by
        # reading the traceback: the previous attempt at this guessed os.read was
        # to blame and changed the wrong line.
        if getattr(os, "O_NONBLOCK", 0) and _NOFOLLOW_READ_FLAGS & os.O_NONBLOCK:
            os.set_blocking(fd, True)
        # Read through a file object rather than a raw os.read loop. A 1 MiB os.read
        # on Windows raises WinError 87 (invalid parameter), which reddened this on
        # the Windows shard; fdopen sizes its own buffers per platform. The
        # descriptor has already passed O_NOFOLLOW and the regular-file check, and
        # wrapping it changes neither -- closefd=False keeps the close in the
        # caller's finally, so there is exactly one close.
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read()
        return data
    finally:
        os.close(fd)


def _read_locked(path: Path, lock_path: Path, wait_secs: float, root: Path | None = None) -> bytes:
    """Read ``path`` while holding a shared ``flock`` on ``lock_path``.

    Polls ``LOCK_SH | LOCK_NB`` to a deadline instead of a bare blocking
    ``flock``, so the wait is bounded. The exclusive writer blocks us and we
    block no writer for longer than the read itself (a few milliseconds on a
    small transcript). Raises ``_Contended`` on timeout rather than reading
    through the lock.
    """
    fd = None
    try:
        # Imported HERE, not at module scope. ``fcntl`` is POSIX-only and this is
        # the one function that uses it, while the module around it is imported on
        # Windows by two things that have nothing to do with locking: this suite's
        # own collection, and an upstream repo-wide test that walks every module.
        # A module-level import made both fail with ModuleNotFoundError on Windows
        # even though the sidecar only ever runs inside the Linux image. Same
        # convention as this tree's boto3 imports: keep the package importable
        # where the capability is absent, and fail at the call instead.
        import fcntl

        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + wait_secs
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise _Contended(str(path))
                time.sleep(_LOCK_POLL_SECS)
        try:
            return _read_nofollow(path, root)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd is not None:
            os.close(fd)


def run_backup_cycle(
    settings: Settings,
    store: ObjectStore,
    state: BackupState,
    *,
    lock_wait_secs: float = LOCK_WAIT_SECS,
) -> CycleResult:
    """One backup pass over the whole unit. Mutates ``state`` in place.

    Whole-object upload; size+hash change detection; write-once artifact skip;
    lock-respecting reads of live transcripts.
    """
    result = CycleResult()
    art_prefix = layout.artifact_prefix(settings)

    for local_path, rel_key, root in layout.iter_backup_files(settings):
        result.scanned += 1
        prev = state.objects.get(rel_key)

        # Write-once artifacts: if we have already uploaded this object, do not
        # re-hash the file or re-upload it. (Presence is enough; artifacts are
        # never rewritten.)
        if rel_key.startswith(art_prefix) and prev is not None:
            result.skipped_artifact += 1
            continue

        try:
            if layout.needs_lock(settings, local_path):
                lock_path = local_path.parent / (local_path.name + ".lock")
                data = _read_locked(local_path, lock_path, lock_wait_secs, root)
            else:
                data = _read_nofollow(local_path, root)
        except _Contended:
            result.deferred_locked += 1
            logger.debug("backup: %s locked, deferring to next cycle", rel_key)
            continue
        except (_NotARegularFile, OSError) as exc:
            # OSError covers ELOOP from O_NOFOLLOW: the file that was enumerated as a
            # regular file is now a symlink. That is the race this reader exists to
            # lose safely, so skip the entry and say so -- FileNotFoundError keeps its
            # own quiet branch below because rotation is routine, while this is not.
            if isinstance(exc, FileNotFoundError):
                continue
            logger.warning(
                "backup: skipping %s, it is no longer the regular file it was "
                "enumerated as (%s). Nothing is uploaded for it this cycle.",
                rel_key,
                exc,
            )
            continue

        size = len(data)
        digest = hashlib.sha256(data).hexdigest()
        if prev is not None and prev.size == size and prev.hash == digest:
            result.skipped_unchanged += 1
            continue

        store.put(layout.full_key(settings, rel_key), data)
        state.objects[rel_key] = ObjMeta(size, digest)
        result.uploaded += 1
        result.uploaded_bytes += size

    return result


def _build_store(settings: Settings) -> ObjectStore | None:
    if not settings.backup_bucket:
        return None
    return S3ObjectStore(settings.backup_bucket)


def run_sidecar(
    settings: Settings,
    *,
    store: ObjectStore | None = None,
    stop: "threading.Event | None" = None,
    max_cycles: int | None = None,
) -> None:
    """Run the copier until ``stop`` is set (the container's normal case).

    ``store``/``stop``/``max_cycles`` exist for tests; the container calls this
    with only ``settings``. If no bucket is configured the sidecar logs and
    returns rather than crashing the task — backup is then disabled, which is a
    degraded state the owner can see, not a dead container.
    """
    if store is None:
        store = _build_store(settings)
    if store is None:
        logger.warning(
            "backup: SMC_BACKUP_BUCKET is not set; backup is DISABLED for this "
            "task. Conversations will not survive the container."
        )
        return

    stop = stop or threading.Event()
    spath = state_path(settings)
    state = BackupState.load(spath)

    # Seed the object index from what is already in the bucket so a task restart
    # does not re-upload every write-once artifact.
    try:
        existing = store.list(layout.object_prefix(settings))
        seed: dict[str, ObjMeta | int] = {}
        for full, size in existing.items():
            rel = layout.rel_from_full(settings, full)
            if rel is not None:
                seed[rel] = size
        state.seed_sizes(seed)
    except Exception:  # noqa: BLE001 - seeding is an optimisation, never fatal
        logger.warning("backup: could not seed state from bucket", exc_info=True)

    n = 0
    while not stop.is_set():
        started = time.time()
        try:
            res = run_backup_cycle(settings, store, state)
            state.cycles += 1
            state.last_cycle_ts = time.time()
            state.last_success_ts = state.last_cycle_ts
            state.save(spath)
            logger.info(
                "backup cycle=%d scanned=%d uploaded=%d (%d B) unchanged=%d "
                "artifacts_skipped=%d deferred_locked=%d lag=0.0s",
                state.cycles,
                res.scanned,
                res.uploaded,
                res.uploaded_bytes,
                res.skipped_unchanged,
                res.skipped_artifact,
                res.deferred_locked,
            )
        except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
            logger.exception("backup: cycle failed; will retry next interval")

        n += 1
        if max_cycles is not None and n >= max_cycles:
            break
        elapsed = time.time() - started
        stop.wait(max(0.0, settings.backup_interval_secs - elapsed))
