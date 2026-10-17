"""The transcript lock and the permanent-delete marks.

One process-wide reentrant lock and one cross-process ``flock`` sidecar per physical
stem, every alias stem of a session taken in one sorted order, the deferred off-loop
release, the proof-of-hold witness the unlocked cache fill publishes by, and the
in-process marks a permanent delete holds.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_dir``.

Process-wide registries read through ``type(self)``, class attributes of
``ConversationLog``: ``_deletes_in_flight``, ``_deletes_in_flight_guard``,
``_file_locks``, ``_file_locks_guard``, ``_flock_epochs``, ``_flock_guard``,
``_flock_state``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time as _time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.history_files import transcript_lock_stems
from kiro_crew.history_offload import _check_on_loop_persist_discipline
from kiro_crew.owner_only_files import mkdirs_owner_only

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: Moved code logs under the facade's own logger name.
logger = logging.getLogger("kiro_crew.history")


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


def _file_lock(self: ConversationLog, key: str) -> threading.RLock:
    """Return the process-wide reentrant lock guarding *key*'s session file.

    Reentrant so a locked method (e.g. ``append``) can call another locked
    helper (``_maybe_rotate``) without deadlocking.
    """
    lock_key = str(self._path(key))
    with type(self)._file_locks_guard:
        lock = type(self)._file_locks.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            type(self)._file_locks[lock_key] = lock
        return lock


def _lock_path(self: ConversationLog, key: str) -> Path:
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
def locked_stems(self: ConversationLog, stems: Iterable[str]) -> Iterator[None]:
    """Hold exact physical transcript stems in deterministic order."""
    with contextlib.ExitStack() as locks:
        for stem in sorted(set(stems)):
            locks.enter_context(self._locked_stem(stem))
        yield


@contextlib.contextmanager
def _locked(self: ConversationLog, key: str) -> Iterator[None]:
    """Hold every physical lock that can represent one transcript.

    Slack's canonical, sanitized, and pre-migration bare spellings all map
    to one sorted lock set. Target-path resolution must happen inside this
    context so a waiter cannot publish a filename choice made before a
    concurrent restore.
    """
    with self.locked_stems(transcript_lock_stems(key)):
        yield


@contextlib.contextmanager
def _locked_stem(self: ConversationLog, key: str) -> Iterator[None]:
    """Hold the in-process and cross-process locks for one physical stem.

    Callers use :meth:`_locked`, which acquires every stable alias stem in
    deterministic order. This primitive stays separate so that alias locking
    never resolves a target path before all sidecars are held.
    """
    # Fail loud (strict) or diagnose (production) if a mutation reached the
    # lock ON the event loop — the un-offloaded-call-site guard (see
    # OnLoopPersistError). No-op off the loop, which is the sanctioned path.
    _check_on_loop_persist_discipline(key)
    with self._file_lock(key):
        lock_key = str(self._path(key))
        with type(self)._flock_guard:
            state = type(self)._flock_state.get(lock_key)
            if state is None:
                lock_path = self._lock_path(key)
                mkdirs_owner_only(lock_path.parent)
                fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                state = [fd, 0, 0]  # fd, depth, held
                type(self)._flock_state[lock_key] = state
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
                with type(self)._flock_guard:
                    state[2] = 1  # flock now held on state[0]
            except BaseException:
                with type(self)._flock_guard:
                    state[1] -= 1
                    # Only tear the fd down if we are the last frame AND the
                    # flock was never taken; a concurrent reuse (depth > 0)
                    # or a held flock must keep the fd alive.
                    drop = state[1] == 0 and state[2] == 0
                    if drop:
                        type(self)._flock_state.pop(lock_key, None)
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
            with type(self)._flock_guard:
                state[1] -= 1
                done = state[1] == 0
            if done:
                # Depth hit 0. ``platform_compat.release_lock`` (flock
                # LOCK_UN) and ``os.close`` are both ``blocking: true``
                # syscalls, so run them off the event loop — a wedged
                # descriptor must never freeze chat/WS/heartbeat. We DO NOT
                # pop the state here:
                # the entry stays alive with ``held``=1 so a sequential
                # same-key re-acquire before the release runs reuses the
                # still-held flock instead of ``flock``-ing a fresh fd, which
                # would spuriously raise HistoryLockTimeout under executor
                # load. The deferred release re-checks depth and
                # its own fd under the guard, so a reuse cancels it.
                self._schedule_flock_release(key, lock_key, state[0])


@contextlib.contextmanager
def atomic_appends(self: ConversationLog, key: str) -> Iterator[None]:
    """Group several appends to one session into ONE indivisible write.

    :meth:`append` locks per ROW, so two callers each writing a
    user+assistant PAIR can interleave into ``user_A, user_B, assistant_A,
    assistant_B`` -- a transcript whose turns do not pair up, and which no
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


def _schedule_flock_release(self: ConversationLog, key: str, lock_key: str, fd: int) -> None:
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
            with type(self)._flock_guard:
                st = type(self)._flock_state.get(lock_key)
                if st is None or st[0] != fd or st[1] != 0:
                    return  # reused or replaced — leave the flock in place
                type(self)._flock_state.pop(lock_key, None)
                # The hold is over: advance the release epoch so an
                # unlocked fill's witness (:meth:`_flock_hold_witness`)
                # spanning this release cannot claim a continuous
                # hold, even if a re-acquire lands on a recycled fd number.
                type(self)._flock_epochs[lock_key] = type(self)._flock_epochs.get(lock_key, 0) + 1
            try:
                platform_compat.release_lock(fd)
            finally:
                os.close(fd)

    self._run_fd_cleanup_off_loop(_release_and_close)


@contextlib.contextmanager
def delete_in_flight_window(self: ConversationLog, key: str) -> Iterator[None]:
    """Mark *key*'s transcript as being permanently deleted for the block.

    Non-blocking bookkeeping only, so it is safe on the event loop. Held by
    ``delete_session`` for its whole transaction, and by callers whose
    delete spans more than that call (the session-delete handler captures
    which slot to remove before its first await, so the window has to open
    there for a slot published after that capture to be refused).
    """
    marks = self._delete_in_flight_marks(key)
    with type(self)._deletes_in_flight_guard:
        for mark in marks:
            type(self)._deletes_in_flight[mark] = type(self)._deletes_in_flight.get(mark, 0) + 1
    try:
        yield
    finally:
        with type(self)._deletes_in_flight_guard:
            for mark in marks:
                depth = type(self)._deletes_in_flight.get(mark, 0) - 1
                if depth > 0:
                    type(self)._deletes_in_flight[mark] = depth
                else:
                    type(self)._deletes_in_flight.pop(mark, None)


def delete_in_flight(self: ConversationLog, key: str) -> bool:
    """True while a permanent delete of *key* is in flight in this process."""
    marks = self._delete_in_flight_marks(key)
    with type(self)._deletes_in_flight_guard:
        return any(mark in type(self)._deletes_in_flight for mark in marks)


def _delete_in_flight_marks(self: ConversationLog, key: str) -> tuple[str, ...]:
    # The transcript lock stems, not ``_path``: ``_path`` stats the disk and
    # picks the legacy Slack file only while it exists, so a mark taken
    # before the unlink and a probe made after it would name different
    # files. The stems are pure string math and every spelling of one
    # session maps to the same set, exactly as the transcript lock does.
    base = str(self._dir)
    return tuple(f"{base}{os.sep}{stem}" for stem in transcript_lock_stems(key))


def _flock_hold_witness(self: ConversationLog, key: str) -> tuple[int, int] | None:
    """Proof-of-hold snapshot of OUR cross-process flock for *key*'s file.

    Returns ``(fd, release_epoch)`` when this process currently holds the
    sidecar flock (``_flock_state.held == 1``), else ``None``. An unlocked
    fill snapshots this before its stat and compares at publish time: an
    equal pair proves the flock was held by this process CONTINUOUSLY
    across the fill window, so no EXTERNAL process can have written the
    file in that window — external writers block on the flock, and they
    are the one writer class the in-process invalidation generation cannot
    witness (their ``_invalidate_cache`` runs in their process, not ours).
    Local writers write freely under our hold, and every local
    preserved-mtime rewrite bumps the generation, so the generation check
    covers them. ``None`` — including a local writer still WAITING on an
    external process's flock — means the window cannot be proven
    external-write-free and the fill must not publish.
    """
    lock_key = str(self._path(key))
    with type(self)._flock_guard:
        state = type(self)._flock_state.get(lock_key)
        if state is None or state[2] != 1:
            return None
        return (state[0], type(self)._flock_epochs.get(lock_key, 0))
