"""Serialize SEL chain writes across processes: the chain lock and its hold registry.

The read-tip, chain and append sequence runs under an advisory lock on
``trust/security_events.lock``; every instance in one process joins that process's one
hold, and on the asyncio event-loop thread the acquire is a single nonblocking attempt
that fails closed. The sidecar's directory is pinned by descriptor before the sidecar is
opened inside it.

Each function here whose first parameter is ``self`` is a ``SecurityEventLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller. Module
names this code reads are patched through ``kiro_crew.sel``, which reaches every member
of the family that binds them (``kiro_crew._facade_mirror``).

Instance fields read: ``_dir``, ``_hmac_key_file``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.sel_reader import _PIN_BY_FD_SUPPORTED
from kiro_crew.sel_trust_root import _HMAC_KEY_FILE, _TRUST_SUBDIR

if TYPE_CHECKING:
    from kiro_crew.sel import SecurityEventLog


def _on_event_loop() -> bool:
    """True when called on a thread running an asyncio event loop.

    Lets a caller refuse a contended lock acquire, and bound an otherwise
    unbounded tail scan, so neither stalls that loop.
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


# Sidecar whose advisory lock serializes chain writes ACROSS PROCESSES. It lives
# in _TRUST_SUBDIR, not beside the log: that directory is owner-only and inside
# the sensitive-path floor, so the audited agent can neither unlink the sidecar
# mid-hold nor hold it out from under the writers. Its name is a CONSTANT under
# that directory, never a path read off the resolved HMAC key: the key's location
# varies with each process's own migration state, so two writers reading
# different states would lock different inodes and fork the chain. It is also
# deliberately not the log file itself — msvcrt.locking() locks a byte range at
# offset 0 and so needs an fd whose offset the caller may move freely, which the
# O_APPEND log fd is not.
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


# Named by the path callers import it from: a repr, a pickle or a traceback names a
# class as ``module.qualname``, and that text names ``kiro_crew.sel``.
_ChainHold.__module__ = "kiro_crew.sel"


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


@contextlib.contextmanager
def _chain_lock(self: SecurityEventLog, *, kind: str = "append") -> Iterator[None]:
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
    could wedge every writer. Its path is a constant under that directory
    and never read off the resolved HMAC key, whose location varies per
    process; when the directory is a link or cannot be created at all,
    :meth:`_chain_lock_target` falls back to the legacy key file, which is
    a filesystem fact every writer on the install reads alike.

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
    # Keyed on the sidecar's CONSTANT path, not on the file finally locked:
    # the registry only has to name "the hold in this process on this log",
    # and a stable name is what makes two instances join one hold even when
    # one of them resolved the fallback below. A key that tracked the
    # fallback would hand them different keys, and two real flocks on two
    # inodes is the fork this lock exists to prevent.
    registry_path = self._chain_lock_path()
    key = str(registry_path)
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
    # Resolved only now, after the same-process join above has had its
    # chance: the join path must stay free of filesystem work, and this
    # call does a mkdir.
    lock_path, lock_may_create, lock_dir_fd = self._chain_lock_target()
    try:
        # A sidecar that is a LINK is not a lock. If the path resolves elsewhere,
        # replacing its target hands two writers locks on different inodes — the
        # exact fork this serialization exists to prevent — so a link planted
        # before this code ran must be refused, not followed. O_NOFOLLOW makes
        # that atomic on POSIX (no TOCTOU window between checking and opening);
        # the explicit probe carries Windows junctions, which O_NOFOLLOW does not
        # exist for. This is the same defense _load_or_create_hmac_key already
        # applies to this directory.
        if platform_compat.is_link_or_junction(lock_path):
            raise OSError(f"SEL chain-lock sidecar {lock_path} is a link; refusing to lock it")
        # Windows' CRT text mode strips a trailing 0x1A while opening a file
        # for update. The fallback lock path can be the raw HMAC key, so this
        # descriptor must be binary even though the lock code never writes it.
        # O_CREAT only on the sidecar: creating the fallback would leave a
        # 0-byte file at the legacy KEY path, which the migration block would
        # then promote over the real key and destroy it.
        #
        # Opened RELATIVE to the pinned directory where the platform has
        # directory descriptors, so the directory screened in
        # ``_chain_lock_target`` is the directory this sidecar is opened
        # inside: a ``trust`` replaced after that screen cannot redirect this
        # open into another tree, where both writers' screens would pass while
        # they locked different inodes.
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = _open_lock_sidecar(lock_path, flags, create=lock_may_create, dir_fd=lock_dir_fd)
    finally:
        if lock_dir_fd is not None:
            # The descriptor's whole job was to anchor the open above; the
            # locked fd carries the identity from here on.
            os.close(lock_dir_fd)
    joined: _ChainHold | None = None
    unlock: Callable[[], None] | None = None
    try:
        # A hard link makes the same inode reachable under a second name the
        # deny-list does not cover; O_NOFOLLOW says nothing about that.
        if os.fstat(fd).st_nlink > 1:
            raise OSError(f"SEL chain-lock sidecar {lock_path} is hard-linked; refusing to lock it")
        # Nothing is written through this descriptor, including to prime a
        # fresh sidecar. A Windows byte-range lock covers byte 0 of a
        # zero-length file and still excludes every other descriptor and
        # process, and it makes byte 0 unwritable while it is held: a write
        # here races a sibling's acquire and fails with EACCES, which
        # ``_flush_batch`` reports as an unavailable chain lock and drops the
        # batch for. The legacy-key fallback path must not be written
        # through either -- its bytes are the HMAC key.
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


def _chain_lock_path(self: SecurityEventLog) -> Path:
    """The sidecar the cross-process chain lock is normally taken on.

    A pure function of the log directory, and nothing derived from where
    the HMAC key resolved to. Those two have no reason to be coupled, and
    coupling them forks the chain: the key's location differs BETWEEN
    PROCESSES on the same directory -- an install whose migration fails
    keeps signing from the legacy location while a sibling that completes
    it reads the relocated one -- so a lock path read off the key hands
    those two writers locks on DIFFERENT inodes, and both then append to
    one log unserialized, which is what the hash chain's tamper-evidence
    rests on not happening.

    The trust subdirectory is owner-only and inside the sensitive-path
    floor, so the audited agent can neither unlink this sidecar mid-hold
    nor hold it out from under the writers. That holds only for a REAL
    directory, so :meth:`_chain_lock_target` decides what to lock when this
    one is a link or cannot be created at all.
    """
    return self._dir / _TRUST_SUBDIR / _SEL_LOCK_FILE


def _chain_lock_target(self: SecurityEventLog) -> tuple[Path, bool, int | None]:
    """The file to lock, whether this call may CREATE it, and its pinned dir.

    On the healthy path this is the sidecar under ``trust``, resolved from
    facts about that directory -- whether it can be created, and whether it
    is a real directory rather than a link -- and never from where the HMAC
    key happens to live. A directory's permissions are a property of the
    filesystem, so every writer on the install reads the same answer and
    locks the same inode.

    One condition may send it elsewhere, and the rule every line below
    serves is that such a condition must be one no actor can flip BETWEEN
    two writers' acquires. A target that can differ across two concurrent
    writers is two flocks on two inodes, which is the forked chain this
    serialization exists to prevent.

    The fallback is the legacy key file, and ONLY when THIS process already
    resolved its HMAC key there -- ``self._hmac_key_file``, decided once in
    ``_load_or_create_hmac_key`` and never re-read. That is the install-wide
    fact the fallback has always been argued from: a process signing at the
    legacy location is one whose trust dir that method could not create or
    could not migrate out of, so the legacy key is the file every writer
    there is signing from and the one the deny list has protected all along.

    The file's mere EXISTENCE cannot be that gate, which is the trap this
    signature closes. ``sel_hmac.key`` is a sandbox read-write leaf
    (``sandbox_leaves.py``'s ``_CREW_SANDBOX_VISIBLE_LEAVES``, alongside ``trust``),
    so the audited agent can create one on a perfectly healthy install --
    and the lock path never reads its bytes, so any content will do. Gating
    on existence therefore let that agent arm the fallback at will, then
    refuse one writer's acquire below and leave it locking the legacy key
    while its sibling locked the sidecar: two flocks on two inodes, which
    forks the chain permanently and makes ``verify_integrity`` report honest
    later records as tampered. Keying on the resolved path instead means a
    planted file buys nothing, because it does not move where this process
    signs.

    Locking is advisory and this fd is never written, so its bytes are safe -- but
    the open must not pass ``O_CREAT``: a created-on-demand key file is a
    0-byte one, and the migration block would promote that over a real key
    and destroy it. Retrying the mkdir on every append is deliberate and
    cheap next to the alternative of dropping every best-effort audit and
    denying every critical action on an install that is otherwise signing
    fine.

    The third element is a PINNED descriptor for the sidecar's directory
    (``None`` for the legacy fallback, whose parent is the operator's log
    directory -- a directory the sandbox grants no entry for, unlike the
    ``trust`` leaf below it -- and ``None`` where the platform has no
    directory descriptors). The caller opens the sidecar RELATIVE to it, so
    the directory screened below is the directory the sidecar is opened
    inside -- see :func:`_pin_lock_dir` for why the by-name screen alone
    cannot give that. Ownership passes to the caller, which closes it once
    the sidecar is open.

    The legacy FILE is itself a read-write leaf, so on an install signing
    there the agent can replace it between two acquires and reach the same
    fork. That is deliberately not defended here: an agent that can rewrite
    the key signing the chain can forge any MAC on it directly, which is
    strictly more than forking it, so a lock-side screen would buy nothing
    that the key's own protection has not already lost.
    """
    lock_path = self._chain_lock_path()
    try:
        # A lock directory that IS a link is not ours, and the mkdir below
        # cannot tell: its ``exist_ok`` check is ``Path.is_dir()``, which
        # FOLLOWS the link and reports the target as a perfectly good
        # directory, so the ``FileExistsError`` is swallowed and the sidecar
        # is created inside whatever the link points at. On the install this
        # matters for that is storage the audited agent controls --
        # ``_load_or_create_hmac_key`` met the same planted ``trust`` link,
        # could not remove it, and fell back to the legacy key precisely so
        # it would never use the destination -- and there the agent can
        # unlink and recreate the sidecar between two writers' acquires,
        # giving them flocks on DIFFERENT inodes and forking the very hash
        # chain this serialization exists to protect. ``mode=0o700`` would
        # not save it either: mkdir's mode is ignored for a directory that
        # already exists, so the guarantee asserted just below would be void.
        #
        # The screen is the ``trust`` leaf and nothing above it, which is
        # both the whole reachable threat and the parity
        # ``_load_or_create_hmac_key`` screens this same directory by.
        # ``trust`` is the one component below the log directory the audited
        # agent can write; every ancestor is the operator's own layout -- a
        # home reached through a symlink, a crew dir moved to another volume
        # -- which the agent cannot plant, so refusing on one buys no
        # protection and costs the whole log: the refusal below routes into
        # the legacy-key fallback, and an install NOT signing at the legacy
        # location gets every best-effort audit dropped and every critical
        # operation denied while nothing is actually wrong.
        #
        # Raising here routes into the same fallback an uncreatable
        # directory takes: the legacy key when this process already SIGNS
        # there, and otherwise the failure, which is the same
        # fail-soft/fail-closed split as the key path's. Both of these are
        # filesystem facts every writer reads alike, which is why they may
        # select a target at all -- see the pin below for the refusal that
        # may not.
        if platform_compat.is_link_or_junction(lock_path.parent):
            raise OSError(
                f"SEL chain-lock directory {lock_path.parent} is a link; "
                "refusing to create the sidecar under it"
            )
        # 0o700 to match how the trust dir is created for the HMAC key:
        # the sidecar's protection is the directory's, so a laxer mode
        # here would quietly undo it.
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        # Only an install already SIGNING at the legacy location may switch
        # targets, and that is decided once at init rather than per acquire,
        # so every writer on the install reads one answer for its whole life.
        if self._hmac_key_file == self._dir / _HMAC_KEY_FILE:
            return self._hmac_key_file, False, None
        # Otherwise the chain cannot be serialized, and ``_flush_batch``
        # turns that into a rollback or a denied critical audit. Inventing a
        # lock elsewhere would be worse than failing, because an
        # unserialized append forks the chain.
        raise
    # Pinned AFTER the fallback decision, and deliberately OUTSIDE it. The
    # two refusals above are properties of the filesystem that every writer
    # on the install reads alike; this one is the opposite by construction --
    # ``_pin_lock_dir`` raises exactly when it catches ``trust`` being
    # REPLACED mid-acquire, which is per-acquire and agent-triggerable, so it
    # can be true for one writer and false for the next. Routing it into the
    # fallback would hand those two writers different inodes on an install
    # whose trust dir is perfectly healthy, which is the fork this whole
    # serialization exists to prevent. A detected swap has exactly one safe
    # answer: fail the acquire, and let the audit-or-deny contract decide.
    #
    # Pinning here is still before anything opens the sidecar, so the
    # descriptor's guarantee is unchanged: everything after resolves through
    # it, and a later replacement of the name cannot move the sidecar into
    # another directory between one writer's acquire and the next.
    return lock_path, True, _pin_lock_dir(lock_path.parent)


@contextlib.contextmanager
def _chain_hold_relabel(self: SecurityEventLog, kind: str) -> Iterator[None]:
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


def _open_lock_sidecar(path: Path, flags: int, *, create: bool, dir_fd: int | None) -> int:
    """Open (creating when *create*) the chain-lock sidecar, race-safe on Darwin.

    The first two writers on a fresh log directory (the background writer's first
    flush and a ``prune``) race to create this sidecar, and a nonexclusive
    ``O_CREAT`` can hand one of them a bare ``ENOENT`` -- the prune is then
    skipped. :func:`platform_compat.open_create_or_existing` is the shared
    answer (the decision log and the app-deps lock hit the same race);
    descriptor-relative when *dir_fd* is given, so the pin taken in
    ``_chain_lock_target`` still anchors the open.
    """
    if dir_fd is None:
        # By-name open, spelled without ``dir_fd=``: the link-screen ratchet reads
        # a ``dir_fd=`` keyword as "anchored to a descriptor", so this branch --
        # the one that really does resolve the screened name by name -- must
        # not carry one, or the ratchet would stop counting it as a resolve.
        if not create:
            return os.open(path, flags, 0o600)
        return platform_compat.open_create_or_existing(path, flags, 0o600)
    if not create:
        return os.open(path.name, flags, 0o600, dir_fd=dir_fd)
    return platform_compat.open_create_or_existing(path.name, flags, 0o600, dir_fd=dir_fd)


def _pin_lock_dir(path: Path) -> int | None:
    """Pin the directory the chain-lock sidecar is opened inside.

    The write-side analog of :func:`_open_segment_dir`. Screening the directory
    BY NAME and then creating/opening the sidecar through that same name are
    separate syscalls, and ``trust`` is a read-write leaf for the audited agent
    (it is the one component below the log directory the sandbox grants), so the
    agent can replace the directory in that window. Both writers' checks then
    pass while their opens land in DIFFERENT directories, giving them flocks on
    different inodes -- the forked chain this serialization exists to prevent,
    and one the by-name screen cannot see because each writer's own check was
    truthful when it ran.

    A descriptor closes the window: it names an inode, not a path, so the
    directory the sidecar is finally opened inside IS the one screened here, and
    a later swap of the name cannot redirect that open. Judged the same three
    ways as the read side, for the same reasons:

    - ``O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK`` refuses a link at the open
      itself, so a link swapped in after the caller's ``is_link_or_junction``
      probe fails here rather than being followed.
    - ``fstat`` on the DESCRIPTOR requires a directory, which nothing can swap
      afterwards.
    - The descriptor's identity must equal what the name's ``lstat`` reports
      right now, so a swap landing between the two fails closed.

    Returns ``None`` where the platform has no directory descriptors (Windows),
    which leaves the caller's junction probe as that platform's only screen --
    the same split :class:`_SegmentDirPin` already carries. Raises ``OSError``
    on a refusal, and the caller deliberately does NOT route it into the
    legacy-key fallback: a refusal here means ``trust`` was replaced mid-acquire,
    which can be true for one writer and false for the next, so it fails the
    acquire and the audit-or-deny contract decides (see
    :meth:`SecurityEventLog._chain_lock_target`).
    """
    if not _PIN_BY_FD_SUPPORTED:
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        named = os.lstat(path)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISDIR(opened.st_mode):
        os.close(fd)
        raise OSError(f"SEL chain-lock directory {path} is not a directory")
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        raise OSError(
            f"SEL chain-lock directory {path} is not the directory its name "
            "points at (replaced mid-acquire?); refusing to create the sidecar "
            "under it"
        )
    return fd
