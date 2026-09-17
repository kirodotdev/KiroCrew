"""Write ownership of one unit's ledger, arbitrated by the kernel.

The arbiter is an advisory lock on a ``.lease`` file beside the log, taken
NON-BLOCKING, so contention is an answer rather than a wait: a process that
cannot own the log is told so and writes nothing. The kernel drops the lock when
the holder's descriptor closes, including on any process death, so a crashed
writer never blocks its successor. There is deliberately NO expiry -- a live but
wedged writer keeps ownership until its process exits, because expropriating it
and then having its appends resume is how the log ends up stating two outcomes
for one turn, which is the exact damage this exists to prevent.

The lock is REFCOUNTED PER PROCESS, keyed by the lease file's path, and that is
not an optimization. A POSIX lock belongs to an open file description, not to a
process, so a second ``open()`` of the same path in the same process contends
with the first exactly as another process would -- and one process legitimately
holds several handles for one unit: the emitter's cached handle and the one a
session claim opens overlap while the cache entry is replaced. Counting
references means the first writer in this process takes the kernel lock, every
later handle shares it, and only the last one to be dropped gives it up.

The path is the key rather than ``(kind, unit_id)`` because the data home is
repointable: the same unit id under two homes is two files, and the kernel locks
a file. Both the acquire and the release run under one module lock, for the same
reason the count exists -- two threads reaching for one unit must share a
descriptor rather than race two of them and have one refuse the other. That lock
is REENTRANT, and has to be: a release is bound to a handle's lifetime through
``weakref.finalize``, and the cyclic collector runs finalizers on whatever thread
happens to allocate when its threshold trips -- including a thread that is inside
``acquire`` with this lock held. A plain lock there is a self-deadlock the caller
can never see coming, since nothing in its own code took the lock twice; it
surfaced as a whole test worker hanging in ``_take`` until the timeout killed it.
Reentry is safe because the two paths never touch one key at the same time: a
key with a live reference is found in the table before ``_take`` runs, so a
finalizer firing during ``_take`` always releases a DIFFERENT key, and one firing
during ``release`` finds its own key already gone.

After locking, the held inode is compared with the file now at the lease path.
A POSIX lock names an inode, so a lock on a path that was unlinked and recreated
proves nothing about the file a writer is about to append to. Nothing in this
tree removes a LIVE unit's lease file: the one path that removes one
(``store.remove_unit``) first takes ownership no other handle shares, so by then
the unit has no writer, and it unlinks the lease last, after every other file is
already gone. The check is what makes that a fact about the code rather than an
assumption.
"""

from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.ledger.errors import CODE_ALREADY_OWNED, LedgerError
from kiro_crew.platform_compat import file_lock

#: The lease file's name inside a unit's ledger directory. Distinct from the
#: per-append lock: that one is taken and released around every write, so a
#: lease on the same path would either deadlock against it or be released by it.
LEASE_FILE = ".lease"

#: How many times a lock on a stale inode is retried before the acquire is
#: reported as contended. Steady state needs one pass; a second is only reached
#: when the lease file is replaced under us, which nothing here does.
_MAX_ATTEMPTS = 3

#: Reentrant -- see the module docstring: a ``weakref.finalize`` release can run
#: on the thread that holds this lock, from inside ``acquire``.
_lock = threading.RLock()


@dataclass
class _Held:
    """One kernel lock and how many handles in this process share it.

    ``removing`` marks the one holder that must NOT be shared: a caller deleting
    the unit's files. See :func:`acquire`'s ``sole`` argument.
    """

    stack: contextlib.ExitStack
    refs: int
    removing: bool = False


#: lease path -> the lock this process holds on it. Guarded by :data:`_lock`.
_held: "dict[str, _Held]" = {}


def _take(path: Path) -> "contextlib.ExitStack | None":
    """Lock *path* and return the holder, or None when the inode kept moving.

    Raises :class:`BlockingIOError` when another descriptor holds the lock, which
    is what ``file_lock(wait=False)`` reports on every platform, and lets any
    other ``OSError`` through: failing to open the lease file is not evidence
    that someone else owns the log.
    """
    for _ in range(_MAX_ATTEMPTS):
        # ``"r+"`` -- writable but NOT truncating -- for the reason the per-append
        # lock documents: ``msvcrt.locking`` needs a writable handle, while a
        # truncating open of a file another process holds locked raises a sharing
        # violation on Windows instead of reporting contention.
        path.touch(exist_ok=True)
        stack = contextlib.ExitStack()
        try:
            handle = stack.enter_context(path.open("r+"))
            stack.enter_context(
                file_lock(handle.fileno(), exclusive=True, required=True, wait=False)
            )
            locked = os.fstat(handle.fileno())
            try:
                current: "os.stat_result | None" = path.stat()
            except FileNotFoundError:
                # The file we locked is gone from the path, so the lock guards an
                # orphan. Start over against whatever stands there now.
                current = None
            same = current is not None and (current.st_ino, current.st_dev) == (
                locked.st_ino,
                locked.st_dev,
            )
        except BaseException:
            stack.close()
            raise
        if same:
            return stack
        stack.close()
    return None


def acquire(path: Path, *, kind: str, unit_id: str, sole: bool = False) -> str:
    """Take this process's write ownership of the unit whose lease file is *path*.

    Returns the key to :func:`release`. Idempotent per process only in the
    counting sense: every call that returns has added a reference, and each one
    has to be released.

    Raises a :class:`~kiro_crew.ledger.LedgerError` with code ``already_owned``
    when another process holds the log. That is a REFUSAL, not a failure: this
    process has written nothing, and it will not own the log by asking again in a
    moment, so a caller reports the loss rather than retrying it.

    ``sole`` asks for ownership NO OTHER HANDLE IN THIS PROCESS SHARES, and it is
    what a caller removing the unit's files must ask for. The reference count
    above exists because cooperating writers in one process legitimately share
    one kernel lock -- so a plain ``acquire`` against a unit this process is
    already writing SUCCEEDS by incrementing that count, proving nothing about
    who owns the log. A deleter that accepted the shared lock would be permitted
    to unlink the segments the emitter's cached handle is appending to. Refusing
    when the count is already non-zero is the same answer as another process
    holding it -- the caller does not own the log alone, so it removes nothing --
    and it has to be decided HERE, under the module lock, because a check the
    caller makes first is a snapshot a claim can land inside.

    A holder taken with ``sole`` also blocks every later acquire, shared or not,
    until it is released. Without that the refusal is one-directional: the
    deleter would refuse a writer that arrived first, then a writer arriving
    second would JOIN the deleter's own lock through the count and append into a
    unit whose files are being unlinked. A writer told ``already_owned`` writes
    nothing, which is the outcome that keeps the removal whole.
    """
    key = str(path)
    with _lock:
        held = _held.get(key)
        if held is not None:
            if held.removing:
                raise _refused(f"the {kind} ledger {unit_id!r} is being removed by this process")
            if sole:
                raise _refused(f"this process already owns writes to {kind} ledger {unit_id!r}")
            held.refs += 1
            return key
        try:
            stack = _take(path)
        except BlockingIOError as exc:
            raise _refused(f"another process owns writes to {kind} ledger {unit_id!r}") from exc
        if stack is None:
            # A lock was taken every time and the file moved out from under it
            # every time. Same answer as contention -- ownership could not be
            # established, so nothing is written -- and the message says which.
            raise _refused(
                f"the lease file of {kind} ledger {unit_id!r} keeps being replaced, so no "
                "lock on it can be verified"
            )
        _held[key] = _Held(stack=stack, refs=1, removing=sole)
    return key


def release(key: str) -> None:
    """Drop one reference, releasing the kernel lock when it was the last.

    Closing the descriptor is what releases the lock, and it happens under the
    module lock: a successor acquire in this process must not find the old
    descriptor still open and read its own predecessor as another owner.

    Also the body of a ``weakref.finalize`` callback, so it may run on ANY thread
    at ANY allocation -- including one that is already inside this module with
    the lock held. The lock is reentrant for exactly that; see the module
    docstring for why the reentry cannot observe a half-updated key.
    """
    with _lock:
        held = _held.get(key)
        if held is None:
            return
        held.refs -= 1
        if held.refs > 0:
            return
        del _held[key]
        held.stack.close()


def _refused(what: str) -> LedgerError:
    """The refusal raised when this process cannot own the log.

    One code for both causes -- contention, and a lease file that keeps moving --
    because the caller's answer is the same: it does not own the log, so it writes
    nothing. The message says which, since only one of them names another process.
    """
    return LedgerError(
        f"{what}; this process appended nothing and did not repair the file",
        code=CODE_ALREADY_OWNED,
    )
