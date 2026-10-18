"""Pruning stale identity mappings from the data home.

``session_pid_<pid>.txt`` files (and their ``.sig`` sidecars) whose pid is gone, or is a
recycled pid the recorded start token disproves -- retracted through a private name so a
concurrent republish is never lost -- and ``session_token_*.sig`` files past their age
limit.

Split out of ``kiro_crew.session_pid``, which stays the import path and the patch target:
every name here is reached as ``kiro_crew.session_pid.<name>``, and a patch through that
module reaches this one (``kiro_crew._facade_mirror``). Nothing here imports it.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from stat import S_ISREG

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir

#: The family logs as ``kiro_crew.session_pid``, the name callers, log filters and
#: ``caplog`` assertions use.
logger = logging.getLogger("kiro_crew.session_pid")


def _prune_stale_session_pid_files(*, narrow_with_leaders: bool = True) -> int:
    """Remove ``session_pid_<pid>.txt`` mappings whose pid is not that session.

    ``narrow_with_leaders`` decides whether the thread-group-leaders snapshot is
    taken. It costs one ``/proc`` directory read for the whole pass and is what
    catches a pid recycled as a THREAD of a live process, but it is work the
    gateway boot path may not carry: ``no-new-work-on-gateway-boot-path`` names
    orphan sweeps specifically, so the boot caller passes ``False``. The
    narrowing is asked for on the graceful shutdown path instead.

    A live session's pid is both signalable and a thread-group leader, so it is
    retained under either setting, and this pass never touches the shared
    ``kiro_session_pids.txt`` that pass 1 rewrites.

    The same setting turns on the one prune of a LIVE pid: a mapping whose
    recorded start token disagrees with the pid's live one, see
    :func:`_retract_recycled_mapping`. The boot setting skips it for the same
    reason it skips the snapshot.

    Returns the number of mapping files removed.
    """
    # The live-pid arm runs in one pass per process at a time, so a private name
    # this process made is never in flight while another pass of it settles
    # leftovers. A pass that finds the lock taken skips the arm for this tick.
    retract = narrow_with_leaders and _RETRACT_LOCK.acquire(blocking=False)
    try:
        return _prune_session_pid_files_pass(narrow_with_leaders, retract)
    finally:
        if retract:
            _RETRACT_LOCK.release()


#: Serializes the live-pid retraction arm within this process.
_RETRACT_LOCK = threading.Lock()


def _prune_session_pid_files_pass(narrow_with_leaders: bool, retract: bool) -> int:
    """One pass of :func:`_prune_stale_session_pid_files`; *retract* enables the live-pid arm."""
    stale_pid_files = 0
    cfg = config_dir()
    if retract:
        # A retraction that stopped part-way leaves its private name behind, and
        # that file may be a CURRENT mapping moved aside by mistake. Only a
        # leftover no running pass owns is settled, and it is settled before the
        # glob so a restored mapping is judged below.
        # Mappings before sidecars: whether a sidecar is still needed depends on
        # whether its mapping was put back.
        for suffix in (".txt", ".sig"):
            for leftover in cfg.glob(f".session_pid_*{suffix}{_RETRACT_SUFFIX}*"):
                if _retraction_leftover_is_orphaned(leftover.name):
                    _settle_retraction_leftover(leftover)
    pid_files = list(cfg.glob("session_pid_*.txt"))
    # Snapshot the host's thread-group leaders ONCE for the whole sweep — one
    # directory read instead of a synchronous /proc read per mapping.
    #
    # Ordering matters: snapshot AFTER globbing. A pid that starts in the window
    # between the two lands IN the set and is retained; one that exits in that
    # window is absent and is pruned, which is correct. Snapshotting first would
    # invert both.
    leaders = platform_compat.live_thread_group_leaders() if narrow_with_leaders else None
    for pid_file in pid_files:
        try:
            pid = int(pid_file.stem.removeprefix("session_pid_"))
        except ValueError:
            # Malformed filename (e.g. MagicMock leak) -- safe to delete
            logger.debug("Removing malformed pid file: %s", pid_file.name)
            try:
                pid_file.unlink(missing_ok=True)
                stale_pid_files += 1
            except OSError:
                logger.debug("Could not remove malformed pid file: %s", pid_file.name)
            continue
        # os.kill(pid, 0) would terminate the process on Windows — probe instead.
        #
        # The leaders set narrows the liveness test: a dead session's pid can be
        # recycled as a THREAD of an unrelated live process, and a tid satisfies
        # ``pid_exists``, so that probe alone would keep the mapping forever.
        #
        # Resolution of a TOKEN-BEARING mapping is already safe without this:
        # ``session_pid_sig._pid_recycled`` compares the live start token and
        # refuses on a mismatch on both the strict and the lenient path, and a
        # tid's live start token cannot match the dead process's. What this
        # sweep adds is (a) pruning LEGACY token-less mappings, where that
        # guard has no recorded token to compare and callers keep resolving,
        # and (b) bounding accumulation — observed on a host whose pid counter
        # had wrapped: 233 mappings, 1 still naming a 6-day-dead session via a
        # thread of an unrelated process.
        #
        # ``leaders is None`` means the question was unanswerable (non-Linux,
        # unreadable /proc), so it never contributes to a prune.
        #
        # Absence from the snapshot selects a CANDIDATE, never the outcome. The
        # snapshot was read before this loop, so a pid recycled since -- whose
        # mapping the new owner has already republished at this same path -- is
        # missing from it while naming a LIVE session. Unlinking that mapping
        # would lose a live session's identity, so the decision needs a reading
        # for this pid taken now. Retain on anything but a definite "not a
        # process", and pay the per-pid read only for the few candidates rather
        # than for every mapping.
        if platform_compat.pid_exists(pid):
            if (
                leaders is None
                or pid in leaders
                or platform_compat.is_thread_group_leader(pid) is not False
            ):
                if retract and _retract_recycled_mapping(pid_file, pid):
                    stale_pid_files += 1
                continue
        pid_file.unlink(missing_ok=True)
        # Remove the HMAC sidecar (session_pid_<pid>.sig) alongside its
        # .txt — a dangling sidecar is harmless (verification requires
        # both) but would accumulate forever.
        pid_file.with_suffix(".sig").unlink(missing_ok=True)
        stale_pid_files += 1
    if stale_pid_files:
        logger.info("Cleaned up %d stale session PID files", stale_pid_files)
    return stale_pid_files


#: Infix of the private name a mapping is moved to while it is being retracted.
_RETRACT_SUFFIX = ".retract-"


def _retract_recycled_mapping(pid_file: Path, pid: int) -> bool:
    """Remove *pid_file* when its recorded start token differs from the live pid's.

    Such a mapping names the pid's PREVIOUS owner. Both resolvers already refuse
    it (``session_pid_sig._pid_recycled``), but the lenient client resolver
    cannot tell it from a planted file -- it reads without a signature check --
    so it counts the record as unusable and a caller with no other identity
    source ends its walk refused. The publisher's own sweep is the party that can
    retire it, so it does, and the walk then sees the absence it would have seen
    had the old owner's mapping been pruned when that owner exited.

    Only POSITIVE evidence retracts: an absent recorded token (legacy file), an
    unreadable live token, a malformed body or a co-tenant record all stay, as
    does anything that is not a regular file.

    The live owner of the pid may republish this exact path at any moment, so the
    file is first moved to a private name and the retraction stands only if what
    moved is still the inode that was read AND its body still proves the recycle.
    Anything else is put back without replacing a file published after it, and a
    file that cannot be put back is KEPT under its private name for the next
    sweep to settle rather than deleted. The ``.sig`` sidecar is handled the same
    way, see :func:`_retract_sidecar`.

    Returns True when the mapping was retracted. Never raises.
    """
    try:
        before = os.lstat(pid_file)
    except OSError:
        return False
    if not S_ISREG(before.st_mode) or not _is_recycled_record(pid, pid_file, before):
        return False
    private = _private_name(pid_file)
    try:
        os.rename(pid_file, private)
    except OSError:
        return False
    if not _is_recycled_record(pid, private, before):
        if _restore_no_replace(private, pid_file):
            _discard(private)
        return False
    _discard(private)
    _retract_sidecar(pid_file)
    logger.debug("Retracted session_pid_%d: the pid now names a different process", pid)
    return True


def _private_name(path: Path) -> Path:
    """The name *path* is moved to while it is retracted, unique to this call."""
    return path.with_name(f".{path.name}{_RETRACT_SUFFIX}{os.getpid()}-{secrets.token_hex(4)}")


def _is_recycled_record(pid: int, path: Path, expected: os.stat_result) -> bool:
    """True when *path* is still the inode *expected* and its body proves a recycle.

    Read through the mapping family's own hardened reader, which never blocks on
    a FIFO and never follows a link: the directory is same-uid agent-writable, so
    anything swapped in after the caller's ``lstat`` is either refused by type or
    by the identity check against *expected*.
    """
    from kiro_crew import session_pid_sig

    body = session_pid_sig._read_regular_nofollow(path, expected=expected)
    if body is None:
        return False
    return session_pid_sig._mapping_from_body(pid, body).refusal == session_pid_sig.REFUSAL_RECYCLED


def _retract_sidecar(pid_file: Path) -> None:
    """Remove the ``.sig`` beside a retracted *pid_file*, never a republished one.

    The publisher writes ``.txt`` then ``.sig``. The sidecar is moved aside first
    and the ``.txt`` checked after the move: if a ``.txt`` is back, or another
    pass holds one privately and may put it back, whatever was moved goes back
    too, without replacing a newer sidecar; if not, any sidecar published from
    here on lands after the move and is untouched.
    """
    sig = pid_file.with_suffix(".sig")
    private = _private_name(sig)
    try:
        os.rename(sig, private)
    except OSError:
        return
    if not _mapping_may_return(pid_file) or _restore_no_replace(private, sig):
        _discard(private)


def _mapping_may_return(pid_file: Path) -> bool:
    """True while *pid_file* exists, or any pass holds a ``.txt`` of it privately.

    Another process's pass can have moved a CURRENT mapping aside and be about
    to put it back, so an empty public path alone does not mean the sidecar is
    unused.
    """
    # A pass moves a mapping aside with one rename and puts it back by linking
    # the public name BEFORE dropping the private one, so at every instant at
    # least one name exists. Public, private, public again: a move that lands
    # between the first two checks shows in the second, a restore that lands
    # between them shows in the third.
    if os.path.lexists(pid_file):
        return True
    if any(pid_file.parent.glob(f".{pid_file.name}{_RETRACT_SUFFIX}*")):
        return True
    return os.path.lexists(pid_file)


def _retraction_leftover_is_orphaned(name: str) -> bool:
    """True when no running pass owns private name *name*.

    The name ends ``.retract-<pid>-<hex>``. Called with :data:`_RETRACT_LOCK`
    held, so this process has no retraction in flight and its own leftovers are
    settled too; another live process's may be in flight and are left alone.

    One live owner has no pass in flight: the supervisor an in-app restart
    through a supervising launcher leaves behind. The restart ``os.execv``s the
    launcher, so the old pid stays alive as this gateway's ancestor while the
    image that ran the retraction is gone, and a leftover it had in flight at
    the exec would otherwise stay unsettled for as long as the supervisor lives.
    A live gateway ancestor still runs Python
    (:func:`platform_compat.is_exec_supervisor_of_this_process`) and keeps its
    leftovers.
    """
    try:
        owner = int(name.rsplit(_RETRACT_SUFFIX, 1)[1].split("-", 1)[0])
    except (IndexError, ValueError):
        return False
    return (
        owner == os.getpid()
        or not platform_compat.pid_exists(owner)
        or platform_compat.is_exec_supervisor_of_this_process(owner)
    )


def _settle_retraction_leftover(leftover: Path) -> None:
    """Finish an unowned retraction: delete what is proven stale, restore the rest.

    A ``.txt`` leftover is deleted only when its body still proves the recycle;
    anything else may be a current mapping moved aside by mistake, so it goes
    back to its name without replacing a newer one. A ``.sig`` leftover goes back
    only beside a ``.txt``; with no ``.txt`` and no private ``.txt`` of the same
    pid still waiting to go back, nothing reads it. A leftover that cannot be put
    back stays for the next sweep.
    """
    original = leftover.with_name(leftover.name[1:].rsplit(_RETRACT_SUFFIX, 1)[0])
    if original.suffix == ".txt":
        try:
            pid = int(original.stem.removeprefix("session_pid_"))
            st = os.lstat(leftover)
        except (ValueError, OSError):
            return
        if S_ISREG(st.st_mode) and _is_recycled_record(pid, leftover, st):
            _discard(leftover)
        elif _restore_no_replace(leftover, original):
            _discard(leftover)
    elif original.suffix == ".sig":
        txt = original.with_suffix(".txt")
        if os.path.lexists(txt):
            if _restore_no_replace(leftover, original):
                _discard(leftover)
        elif not _mapping_may_return(txt):
            _discard(leftover)


def _restore_no_replace(private: Path, target: Path) -> bool:
    """Link *private* back at *target* only if *target* is free.

    True when *private* can be discarded: it was linked back, or *target*
    already holds a newer file. False when the link could not be made (a
    filesystem without hard links, an I/O error); the caller then keeps
    *private*, because a rename here could replace a newer file.

    Only a regular file is ever linked, and never through a symlink: the
    directory is same-uid agent-writable, so a planted link at a private name
    must not become a public name for whatever it points at. Anything else is
    reported as discardable, so the caller unlinks the name itself.
    """
    try:
        if not S_ISREG(os.lstat(private).st_mode):
            return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        if _LINK_NOFOLLOW:
            os.link(private, target, follow_symlinks=False)
        else:
            os.link(private, target)
    except FileExistsError:
        return True
    except OSError:
        logger.debug("Could not restore %s; keeping %s", target.name, private.name)
        return False
    return True


#: ``os.link`` must link the name itself, never a symlink's target, where the
#: platform lets it choose; the ``S_ISREG`` check covers the rest.
_LINK_NOFOLLOW = os.link in os.supports_follow_symlinks


def _discard(path: Path) -> None:
    """Unlink a private retraction name, tolerating an I/O error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove retraction leftover: %s", path.name)


#: How long an unrefreshed ``session_token_<sha256>.sig`` mapping is kept.
#:
#: Age is the ONLY signal available: the filename is a token hash, so unlike a
#: ``session_pid_<pid>`` mapping there is no process to probe. It is nonetheless a
#: safe signal, and for a specific reason rather than because the window is
#: generous: the mapping is republished at the START of every turn
#: (``messaging.identity.publish_turn_identity``), BEFORE anything in that turn can
#: call a tool. So pruning a long-idle session's mapping cannot cost it identity —
#: its next turn rewrites the file before the first resolution — and the window
#: only decides how much disk an abandoned session holds in the meantime.
_SESSION_TOKEN_TTL_SECS = 7 * 24 * 60 * 60


def _prune_stale_session_token_files(ttl_secs: float = _SESSION_TOKEN_TTL_SECS) -> int:
    """Remove ``session_token_*.sig`` mappings unrefreshed for *ttl_secs*.

    This pass is the ONLY retraction path, deliberately: a teardown hook cannot
    reach the case that actually accumulates files — a gateway that dies without
    running one — so an age-based pass is what bounds the directory.

    It runs where its caller runs: :func:`cleanup_orphaned_sessions` is startup and
    graceful-shutdown only, NOT periodic. A long-lived gateway therefore holds one
    mapping per session started since its last boot or clean stop. Stated because
    the TTL below reads like a continuous expiry and is not one.

    A mapping outliving its session is not a forgery risk: it names a session that
    does not exist, and any holder of its token is inside the trust boundary. So
    this is hygiene rather than a control, and it fails soft on every file it
    cannot read or unlink.

    Returns the number of mappings removed.
    """
    removed = 0
    now = time.time()
    try:
        candidates = list(config_dir().glob("session_token_*.sig"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if now - path.stat().st_mtime <= ttl_secs:
                continue
            path.unlink(missing_ok=True)
        except OSError:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - path.name is the sha256 DIGEST of a token, never a token  # noqa: E501
            logger.debug("could not prune identity mapping %s", path.name, exc_info=True)
            continue
        removed += 1
    if removed:
        logger.info("Cleaned up %d stale session-token mappings", removed)
    return removed
