"""Cross-process advisory file locks, one contract on POSIX and Windows.

POSIX takes ``fcntl.flock``. Windows takes a byte-range ``msvcrt.locking`` lock on
byte 0, acquired by spinning on the non-blocking code, because msvcrt's own blocking
code gives up with ``EDEADLOCK``. Both acquires are bounded by a ceiling and fail
closed past it, and both are single-shot on the asyncio event-loop thread.
:func:`open_create_or_existing` creates or opens a lock sidecar race-safely, and
:func:`probe_file_persistence` checks that a directory supports every primitive the
locked stores rely on.

``kiro_crew.platform_compat`` re-exports every name here, and a patch through it
lands here.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import tempfile
from pathlib import Path
from typing import Iterator

# The helpers read the platform flag (``IS_POSIX``), the lock modules (``fcntl``,
# ``msvcrt``) and the clock (``time``) from ``kiro_crew.platform_compat``, imported
# inside the function that uses them: a test that rebinds one of those there -- to
# force the Windows branch, or to fake the clock a ceiling is measured on -- reaches
# the helper at call time. The facade binds only the lock module this platform has, so
# each of the two is imported at the point where its branch first uses it. Circular
# import: ``kiro_crew.platform_compat`` imports this module while it loads.

# msvcrt's blocking lock codes (LK_LOCK / LK_RLCK) are NOT the equivalent of
# fcntl.flock(LOCK_EX): rather than waiting until the lock is free, they retry
# ~10 times at 1s intervals and then RAISE EDEADLOCK (errno 36). Swallowing
# that as "acquired" lets a caller run its read-modify-write with no exclusion
# and silently lose writes. So the Windows "blocking" acquire spins on the
# non-blocking code (LK_NBLCK) instead — the same idiom cron._file_lock uses.
# It is bounded rather than truly unbounded because a contended fd and a
# non-writable fd are indistinguishable on Windows (both surface as errno 13
# EACCES), so an unbounded spin would turn a permission error into a hang.
#
# Two ceilings, because on-loop and off-loop have opposite needs:
#  - OFF the loop (cron, app backends — threads/subprocesses): the wait must
#    cover a legitimately long holder that can hold the lock across a
#    multi-second operation, and a waiter there must NOT give up and race it. So
#    use a generous ceiling that no real hold approaches.
#  - ON the loop (e.g. bridges._mcp_lock during app enable): a spin-sleep would
#    freeze chat/heartbeat, so that path never sleeps at all (single-shot).
_LOCK_POLL_SECS = 0.01
# Backoff cap for the POSIX poll. A kernel-blocking acquire sleeps at zero cost,
# so a flat 10ms poll would add ~30k pointless wakeups across the full ceiling;
# doubling up to this bound keeps the first polls tight (a holder finishing a
# sub-second critical section is still seen at once) and makes a long wait cheap.
_LOCK_POLL_MAX_SECS = 0.25
# Generous off-loop ceiling: longer than any legitimate hold, short enough that
# a truly stuck/permission-denied fd still fails.
_LOCK_TIMEOUT_SECS = 300.0

# The ceiling is not Windows-only. ``fcntl.flock`` has no timeout argument, so an
# unbounded POSIX acquire waits on a stuck holder without limit -- and on the boot
# path that is a gateway which never binds its port and logs nothing, which reads
# as a slow start rather than a failure. Both platforms refuse past this same
# ceiling, so the failure is reportable. ``_WIN_*`` are aliases of these names.
_WIN_LOCK_POLL_SECS = _LOCK_POLL_SECS
_WIN_LOCK_TIMEOUT_SECS = _LOCK_TIMEOUT_SECS


def _on_event_loop() -> bool:
    """True when called on a thread that is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _lock_timeout_message(
    waited: float,
    *,
    exclusive: bool = True,
    ceiling: float | None = None,
    on_loop: bool = False,
) -> str:
    """The one refusal string both platforms raise when an acquire gives up.

    Names how long the acquire REALLY waited and what was observed: the lock was
    still held. It deliberately names no cause. The acquire cannot tell a hung
    holder from a busy one or from a queue of waiters, and naming one of them
    sends an operator after the wrong fix. On the event-loop thread the acquire
    makes one attempt and never waits, so the message says that instead of
    naming a ceiling it never applied. A caller that catches this as best-effort
    work has nothing else to report, so this message is the only evidence of WHY
    the critical section was declined.
    """
    kind = "exclusive" if exclusive else "shared"
    if on_loop:
        detail = (
            f"still held after waiting {waited:.2f}s (one attempt: an acquire on "
            "the event-loop thread never waits; take it from a worker thread to wait)"
        )
    else:
        limit = waited if ceiling is None else ceiling
        detail = f"still held after waiting {waited:.2f}s (limit {limit:g}s)"
    return f"could not acquire {kind} file lock: {detail}; refusing to proceed unserialized"


def _posix_acquire_blocking(
    fd: int,
    mode: int,
    *,
    timeout: float | None = None,
) -> bool:
    """POSIX bounded lock acquire: poll ``LOCK_NB`` until free or *timeout*.

    Returns True if the lock was taken, False if the ceiling was reached.

    ``fcntl.flock`` takes no timeout, so polling the non-blocking code is the only
    way to bound the wait -- the same shape :func:`_win_acquire_blocking` uses, for
    the same reason: a stuck holder must not be waited on without limit.

    Retries cover contention ONLY. ``EAGAIN``/``EACCES``/``EWOULDBLOCK`` mean
    another holder has it; any other errno is about this fd and propagates at
    once, so a real defect is not reported as a stuck holder at the ceiling.

    NEVER polls on the asyncio event-loop thread, matching
    :func:`_win_acquire_blocking`: ``time.sleep`` there would freeze chat and
    heartbeat for the whole wait, and a freeze long enough to miss a heartbeat is
    a supervisor kill. On the loop the acquire is single-shot -- take it if free,
    else refuse at once -- so a caller fails closed instead of stalling every
    other session. Off the loop, which is where the lock is normally taken, it
    polls to the ceiling as a real wait.

    The sleep BACKS OFF from ``_LOCK_POLL_SECS`` to ``_LOCK_POLL_MAX_SECS``: a
    flat 10ms poll would wake ~30k times across the full ceiling for no benefit,
    while a kernel-blocking acquire wakes not at all. The first polls stay tight,
    so a holder finishing its sub-second critical section is picked up promptly,
    and a long wait settles into a cheap idle.
    """
    from kiro_crew.platform_compat import fcntl, time

    nb_mode = mode | fcntl.LOCK_NB

    def _try_once() -> bool:
        try:
            fcntl.flock(fd, nb_mode)
            return True
        except OSError as exc:
            # Only "someone holds it" is retryable. EBADF/EINVAL and friends are
            # real errors about THIS fd and must surface now, not at the ceiling.
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            return False

    if _on_event_loop():
        # Single attempt only -- a poll-sleep here blocks the event loop.
        return _try_once()

    ceiling = _LOCK_TIMEOUT_SECS if timeout is None else timeout
    deadline = time.monotonic() + ceiling
    delay = _LOCK_POLL_SECS
    while True:
        if _try_once():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Never sleep past the deadline, so the refusal lands at the ceiling
        # rather than up to one backed-off interval after it.
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _LOCK_POLL_MAX_SECS)


def _win_acquire_blocking(fd: int, *, timeout: float = _LOCK_TIMEOUT_SECS) -> bool:
    """Windows blocking lock acquire: spin on LK_NBLCK until free or timeout.

    Returns True if the lock was taken, False if it could not be.

    NEVER spins on the asyncio event-loop thread: ``time.sleep`` there would
    freeze chat/heartbeat for the whole wait. A few callers still take the lock
    on the loop (e.g. bridges._mcp_lock during app enable), so when a running
    loop is detected the acquire is single-shot — take it if free, else return
    False at once — and the caller fails closed rather than stalling the loop.
    Off the loop (the common case) it polls up to ``timeout`` as a real
    blocking wait, so a legitimately long holder is waited out rather than
    raced.
    """
    from kiro_crew.platform_compat import time

    def _try_once() -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            from kiro_crew.platform_compat import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    if _on_event_loop():
        # Single attempt only — a spin-sleep here blocks the event loop.
        return _try_once()

    deadline = time.monotonic() + timeout
    while True:
        if _try_once():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LOCK_POLL_SECS)


@contextlib.contextmanager
def file_lock(
    fd: int,
    *,
    exclusive: bool = True,
    required: bool = False,
    wait: bool = True,
    timeout: float | None = None,
) -> Iterator[None]:
    """Acquire an advisory lock on ``fd`` for the duration of the block.

    POSIX: ``fcntl.flock(LOCK_EX|LOCK_SH)`` with ``LOCK_UN`` release, acquired by
    polling the non-blocking code up to ``_LOCK_TIMEOUT_SECS``. ``flock`` itself
    takes no timeout, and an unbounded wait on a stuck holder is not a wait but a
    hang: on the boot path it leaves a gateway that binds no port and logs
    nothing, which no supervisor or health check can act on.
    Windows: ``msvcrt.locking`` on the first byte, acquired by spinning on the
    non-blocking code up to ``_LOCK_TIMEOUT_SECS`` — because msvcrt's own
    "blocking" code gives up after ~10s with EDEADLOCK, which cannot be treated
    as a wait. ``msvcrt`` has no shared mode, so a shared request is satisfied
    with an exclusive lock (correctness over concurrency — readers genuinely
    serialize with the holder, but never see torn writes).

    On BOTH platforms, if the lock cannot be taken within the ceiling
    ``file_lock`` FAILS CLOSED — it raises rather than entering the critical
    section unserialized, since proceeding lock-less is the exact fail-open that
    loses writes. On BOTH platforms the acquire is additionally single-shot when
    called on the asyncio event-loop thread: a poll-sleep there would freeze chat
    and heartbeat for the whole wait, and a freeze long enough to miss a heartbeat
    is a supervisor kill, so a contended on-loop caller is refused at once and
    fails closed rather than stalling every other session. ``flock`` counts a
    second descriptor in this same process as a competing holder, so even a
    sibling thread's brief critical section refuses an on-loop caller: a caller
    that must wait for the lock calls this from a worker thread
    (``asyncio.to_thread``), where the wait is a real one. The timeout is a safety
    ceiling against a stuck holder, not a normal wait. ``required`` is kept for
    call-site intent and does not change the outcome (both paths refuse to proceed
    without the lock).

    *timeout* overrides that ceiling for a caller whose critical section is
    legitimately long, and must be set by any caller that can hold the lock past
    ``_LOCK_TIMEOUT_SECS``. The default suits the common case — a sub-second read
    plus an atomic rename — but it is NOT an upper bound on every in-tree holder:
    ``frontend._staging_lock`` spans an ``npm run build`` plus an install, so a
    default ceiling would refuse a contender while the holder is still working
    rather than because it is stuck. A ceiling shorter than the holder's real
    work turns a wait into a spurious refusal, which is the failure this
    parameter exists to prevent.

    *wait* is for a caller whose work is OPTIONAL and retried later, and which
    may run on the event-loop thread: with ``wait=False`` the acquire is
    single-shot on every platform and raises :class:`BlockingIOError` at once
    when the lock is held, instead of blocking the loop for as long as the holder
    keeps it. POSIX gets that from ``LOCK_NB``; Windows already behaves this way
    on the loop thread, and a zero timeout makes it uniform off the loop too.
    ``BlockingIOError`` is an ``OSError``, so it is a NARROWING of what a caller
    already had to handle, and it separates "someone else is writing right now"
    from the stuck-holder ceiling above. It changes only how long we are willing
    to wait, never whether the critical section is serialized -- a contended
    ``wait=False`` acquire raises rather than proceeding.

    Note: on Windows, ``msvcrt.locking`` requires seeking to byte 0, so the
    ``fd`` must be a dedicated lock file; callers must not rely on the file
    offset being preserved across the context manager boundary.
    """
    from kiro_crew.platform_compat import IS_POSIX, time

    if IS_POSIX:
        from kiro_crew.platform_compat import fcntl

        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not wait:
            # BlockingIOError (an OSError) when held: same fail-closed contract
            # as the Windows branch, reported by the platform rather than by us.
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        else:
            started = time.monotonic()
            if not _posix_acquire_blocking(fd, mode, timeout=timeout):
                # Refuse LOUDLY rather than wait without limit: an unbounded wait
                # here leaves a boot with no port bound and no log line, while a
                # raise is something the caller can report and recover from --
                # the gateway boot path logs it at ERROR, prints the repair
                # command, and still binds its port.
                raise OSError(
                    _lock_timeout_message(
                        time.monotonic() - started,
                        exclusive=exclusive,
                        ceiling=_LOCK_TIMEOUT_SECS if timeout is None else timeout,
                        on_loop=_on_event_loop(),
                    )
                )
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    else:
        # Fail CLOSED, not open: if the lock cannot be taken within the ceiling
        # (a stuck/crashed holder — never a normal sub-second hold), raise rather
        # than enter the critical section unserialized. Entering anyway is the
        # exact fail-open that loses writes; a loud error in that rare case is
        # strictly safer, and callers already run under `with`, so the fd is
        # cleaned up. `required` is kept for call-site intent and does not
        # change the outcome — both paths refuse to proceed lock-less.
        # The waiting path with no explicit ceiling is called with no keyword, so
        # the default-argument call shape existing tests stub out is preserved.
        started = time.monotonic()
        if not wait:
            ceiling = 0.0
            acquired = _win_acquire_blocking(fd, timeout=0.0)
        elif timeout is None:
            ceiling = _LOCK_TIMEOUT_SECS
            acquired = _win_acquire_blocking(fd)
        else:
            ceiling = timeout
            acquired = _win_acquire_blocking(fd, timeout=timeout)
        if not acquired:
            if not wait:
                # Held right now. BlockingIOError so the caller can tell this
                # from the timed-out wait below, matching POSIX LOCK_NB.
                raise BlockingIOError("file lock is held; not waiting for it")
            raise OSError(
                _lock_timeout_message(
                    time.monotonic() - started,
                    exclusive=exclusive,
                    ceiling=ceiling,
                    on_loop=_on_event_loop(),
                )
            )
        try:
            yield
        finally:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                from kiro_crew.platform_compat import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass


@contextlib.contextmanager
def flock_exclusive(fd: int) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``fd`` (see :func:`file_lock`)."""
    with file_lock(fd, exclusive=True):
        yield


@contextlib.contextmanager
def open_lock_file(path: "str | os.PathLike[str]") -> Iterator[int]:
    """Open *path* for locking WITHOUT truncating it (GH-9248).

    ``open(path, "w")`` truncates the file before any lock is held. On POSIX
    that is survivable; on Windows the subsequent acquire routes to
    ``msvcrt.locking`` on the already-truncated file, so a contending process
    can observe or produce an empty lock file and crash out of the critical
    section — the loss lands only on a specific interleaving, which is why it
    read as shard flake rather than a deterministic failure.
    The create-or-open is :func:`open_create_or_existing`, which never
    truncates and is race-safe against a sibling creating the same name.

    Yields the raw integer fd, ready for :func:`file_lock` /
    :func:`flock_exclusive`. The lock file's CONTENT is never meaningful to
    the lock itself; this exists so contenders cannot watch it flicker empty.
    """
    # 0o600: a lock file is Kiro Crew's own and nobody else's to open; inside
    # the data home every file is owner-only (kiro_crew.owner_only_files).
    fd = open_create_or_existing(path, os.O_RDWR, 0o600)
    try:
        yield fd
    finally:
        os.close(fd)


def open_create_or_existing(
    path: "str | os.PathLike[str]",
    flags: int,
    mode: int = 0o600,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open *path*, creating it when absent, race-safe against a sibling creator.

    A nonexclusive ``O_CREAT`` open of an absent name can come back ``ENOENT``
    on Darwin when two callers race to create it -- the create is not the atomic
    "make or find" the flag reads as. So the name is created EXCLUSIVELY first
    and, when a sibling already made it, opened again WITHOUT ``O_CREAT`` so the
    sibling's inode is the one both hold. A leaf that vanishes between those two
    calls is a genuine ``ENOENT``, left to the caller: recreating it here would
    hand two writers two different inodes under one lock name.

    *flags* carries everything but the create bits (``O_RDWR``, ``O_NOFOLLOW``,
    ``O_APPEND``, ...). *dir_fd* makes the open descriptor-relative, so a caller
    that pinned the directory keeps its pin anchoring the open. Returns the raw
    integer fd; the caller owns it. Shared by the SEL chain lock, the decision
    log and the app-deps provisioning lock, which all hit the same race.
    """
    name = os.fspath(path)
    try:
        return os.open(name, flags | os.O_CREAT | os.O_EXCL, mode, dir_fd=dir_fd)
    except FileExistsError:
        return os.open(name, flags, mode, dir_fd=dir_fd)


def acquire_lock(fd: int, *, exclusive: bool = True) -> None:
    """Low-level lock acquire for the acquire-now / release-later fd-handoff
    pattern (where a context manager does not fit).

    POSIX and Windows both wait up to ``_LOCK_TIMEOUT_SECS`` off the asyncio loop
    thread and are both single-shot on it: POSIX polls ``fcntl.flock`` with
    ``LOCK_NB`` (:func:`_posix_acquire_blocking`), Windows polls ``msvcrt.locking``
    (:func:`_win_acquire_blocking`). If the lock cannot be taken it FAILS
    CLOSED — raises rather than letting the caller proceed unserialized — since
    a stuck holder past the ceiling is an error, not a routine wait, and
    proceeding lock-less is the fail-open that loses writes. Pair every call
    with :func:`release_lock` on the same ``fd``.
    """
    from kiro_crew.platform_compat import IS_POSIX, time

    started = time.monotonic()
    if IS_POSIX:
        from kiro_crew.platform_compat import fcntl

        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not _posix_acquire_blocking(fd, mode):
            raise OSError(
                _lock_timeout_message(
                    time.monotonic() - started,
                    exclusive=exclusive,
                    ceiling=_LOCK_TIMEOUT_SECS,
                    on_loop=_on_event_loop(),
                )
            )
        return
    if not _win_acquire_blocking(fd):
        raise OSError(
            _lock_timeout_message(
                time.monotonic() - started,
                ceiling=_LOCK_TIMEOUT_SECS,
                on_loop=_on_event_loop(),
            )
        )


def release_lock(fd: int) -> None:
    """Release a lock acquired via :func:`acquire_lock` / :func:`try_acquire_lock`."""
    from kiro_crew.platform_compat import IS_POSIX

    if IS_POSIX:
        from kiro_crew.platform_compat import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        from kiro_crew.platform_compat import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    except OSError:
        pass


def try_acquire_lock(fd: int, *, exclusive: bool = False) -> bool:
    """Attempt a non-blocking lock acquire. Returns True iff the lock was taken.

    POSIX: ``fcntl.flock(... | LOCK_NB)``. Windows: ``msvcrt.locking`` with the
    non-blocking codes. On success, the caller must :func:`release_lock` the fd.
    Every failure, including a filesystem that cannot lock, reads as ``False``;
    :func:`try_acquire_lock_or_raise` tells the two apart.
    """
    try:
        return try_acquire_lock_or_raise(fd, exclusive=exclusive)
    except OSError:
        return False


def try_acquire_lock_or_raise(fd: int, *, exclusive: bool = False) -> bool:
    """Like :func:`try_acquire_lock`, but only "held by someone else" is ``False``.

    :func:`try_acquire_lock` folds every failure into ``False``, which reads a
    filesystem that cannot lock at all (``ENOLCK``, ``EINVAL`` on some network
    mounts) as permanently busy. A caller that retries on busy would retry
    forever; this one raises that ``OSError`` instead. POSIX busy is
    ``EWOULDBLOCK``/``EAGAIN``; Windows busy is ``EACCES``/``EDEADLOCK``.
    """
    from kiro_crew.platform_compat import IS_POSIX

    if IS_POSIX:
        from kiro_crew.platform_compat import fcntl

        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, mode)
            return True
        except BlockingIOError:
            return False
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            raise
    # msvcrt has no shared lock; LK_NBLCK is a non-blocking exclusive lock.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        from kiro_crew.platform_compat import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
            return False
        raise


def probe_file_persistence(directory: Path) -> str | None:
    """Verify that *directory* supports every primitive the Kiro Crew
    persistence paths depend on: creating a new file (``tempfile.mkstemp``),
    writing bytes to it, taking an advisory lock (:func:`file_lock`),
    atomically replacing it (``os.replace``), and removing it — the exact
    operations ``atomic_write`` and the ``.lock``-file helpers perform.

    Returns ``None`` when all of them work, otherwise a human-readable
    description of the first failure. A process whose environment breaks any
    of these primitives cannot save chat history, cron history, or session
    state — but it CAN still serve traffic and append to already-open log fds,
    so without this probe it limps along losing writes silently. The known way
    to get into that state is inheriting a seccomp syscall filter from a
    sandboxed parent (seccomp survives fork/exec, ``nohup`` included):
    filtered syscalls fail with ``ENOSYS`` while everything else looks
    healthy. The returned message names that cause when ``errno`` says so.

    Probe files carry a ``.persistence-probe-`` prefix, and their removal is
    part of the probed contract: an environment that allows creating files but
    denies deleting them (delete-scoped ACLs) breaks the atomic
    rename/replace paths just the same, so a failed cleanup is reported as a
    preflight failure rather than suppressed. On the failure path probe files
    are best-effort removed; one may remain only when removal itself is what
    is broken.
    """
    fd: int | None = None
    path: str | None = None
    replaced: str | None = None
    step = "create files in"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=directory, prefix=".persistence-probe-")
        step = "write files in"
        os.write(fd, b"probe")
        step = "flush files in"
        os.fsync(fd)
        step = "lock files in"
        with file_lock(fd, exclusive=True):
            pass
        os.close(fd)
        fd = None
        step = "atomically replace files in"
        replaced = f"{path}.target"
        # The same replace primitive atomic_write commits with: plain
        # os.replace on POSIX, bounded retry over the Windows AV/indexer
        # sharing-violation window — a healthy Windows data home must not fail
        # the preflight over that transient. Imported lazily because
        # atomic_write imports this module at top level.
        from kiro_crew.atomic_write import replace_with_retry

        replace_with_retry(path, replaced)
        path = None
        step = "remove files from"
        os.unlink(replaced)
        replaced = None
    except OSError as exc:
        hint = ""
        if exc.errno == errno.ENOSYS:
            hint = (
                " (ENOSYS from a basic file syscall usually means this process"
                " inherited a seccomp filter from a sandboxed parent — e.g. a"
                " gateway spawned from inside an agent session; start it from a"
                " regular shell or the system service instead)"
            )
        return f"cannot {step} {directory}: {exc}{hint}"
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        for leftover in (path, replaced):
            if leftover is not None:
                with contextlib.suppress(OSError):
                    os.unlink(leftover)
    return None
