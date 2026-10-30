"""One file by descriptor.

Copying it with its metadata (:func:`copy_file_pinned`), writing, writing new, reading
and unlinking it, the ``stat``-by-name helpers those share, and the identity-checked
unlink (:func:`unlink_verified`, which the tree removal in :mod:`kiro_crew.pinned_fs`
reuses, and its by-name form :func:`unlink_verified_by_name`).
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import stat as _stat
from contextlib import suppress
from pathlib import Path
from typing import Callable

from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.pinned_fs_dirs import create_and_open_dir_pinned
from kiro_crew.pinned_fs_primitives import (
    SKIP_IDENTITY_CHANGED,
    SKIP_NOT_REGULAR,
    SKIP_SYMLINK,
    SKIP_TOO_LARGE,
    SKIP_UNREADABLE_ENTRY,
    SkipReporter,
    _noop_skip,
    pin_parent,
    supports_pinned_walk,
)
from kiro_crew.platform_compat import pin_directory


def copy_file_pinned(
    by_name: str,
    dst: str | None = None,
    *,
    dir_fd: int | None = None,
    name: str | None = None,
    src_fd: int | None = None,
    dst_dir_fd: int | None = None,
    dst_name: str | None = None,
    skip_existing: bool = False,
    skip_unreadable: bool = False,
    force_mode: int | None = None,
    max_bytes: int | None = None,
    expected_src_ident: "tuple[int, int] | None" = None,
    on_skip: SkipReporter = _noop_skip,
    on_created: "Callable[[os.stat_result], None] | None" = None,
    on_opened: "Callable[[os.stat_result], None] | None" = None,
) -> bool:
    """Copy one file's bytes from a descriptor pinned to a validated inode.

    Returns True when bytes were copied, False when the source was skipped.

    ``skip_unreadable`` tolerates ONE failure: the SOURCE open being refused for
    permission, which is reported as ``SKIP_UNREADABLE_ENTRY`` and returns False.
    It is deliberately not a tolerance for the copy as a whole. A caller that
    wrapped this call instead could not tell the two ends apart, so a DESTINATION
    refusal was recorded as an unreadable source -- an omission attributed to the
    operator's data rather than to the failure to write it, in a bundle that then
    reported success and let retention prune a complete one. Only the end that was
    actually refused is knowable here, so the decision belongs here.

    ``expected_src_ident`` (when given) is the ``(st_dev, st_ino)`` a caller's
    own validation observed: the copy proceeds only when the pinned source
    descriptor fstats to exactly that inode, and reports
    ``SKIP_IDENTITY_CHANGED`` otherwise. This closes the validate->copy window
    the descriptor pin alone cannot: the pin proves the copied inode is the
    OPENED inode, not that it is the inode the caller judged — a hardlink
    swapped in at the name between validation and this open would be a regular
    single-link file the other gates accept.

    ``on_created`` (when given) receives the DESTINATION descriptor's ``fstat``
    at publish time — the identity witness for a caller that must re-open the
    copy by name later: matching ``(st_dev, st_ino)`` on that reopen proves it
    reached the inode this copy created, not a replacement swapped in at the
    same name between the copy and the reopen.

    ``on_opened`` receives the exclusively created destination descriptor's
    ``fstat`` BEFORE the first write, including when the copy later fails. A
    caller rolling back a multi-file install can record this identity without
    re-reading a mutable path. It does not change ``on_created``'s success-only
    semantics; cleanup remains the caller's responsibility.

    ``max_bytes`` is a size ceiling enforced INSIDE the copy, not after it: the
    ``fstat`` size is checked before the destination is even created (a sparse
    file's logical size is what ``fstat`` reports, so a swapped-in sparse giant
    is refused before a byte lands), and the write loop aborts after the first
    excess byte for a source that grows between the ``fstat`` and the read.
    Both refusals report ``SKIP_TOO_LARGE`` and truncate whatever was written
    through the destination descriptor -- a ceiling checked only after the copy
    would let the copy itself exhaust the destination volume on the way to the
    rejection.

    ``shutil.copy2`` cannot be used on a user-writable tree: it dereferences a
    hardlink into innocent-looking regular bytes, and a later tar-level hardlink
    screen never sees a link to reject -- so a hardlink to a credential planted
    inside an otherwise allowlisted directory would ride along as plain content.
    The order here is open first, judge the DESCRIPTOR second: ``O_NOFOLLOW`` where
    the platform has it, then ``fstat`` on the fd, so the inode that is validated is
    exactly the inode whose bytes are copied and no check-to-use window remains.
    Mode and timestamps are applied from that same ``fstat`` result rather than from
    a fresh by-name stat.

    BOTH ends can be pinned, and on a destination the caller does not own they MUST
    be. Pass *dir_fd* + *name* for a pinned source and *dst_dir_fd* + *dst_name* for
    a pinned destination; each side falls back to the by-name form when its pair is
    absent, which is only appropriate for a path this process just created. A caller
    that already holds a validated source descriptor (an ``os.open`` followed by a
    ``fd_real_path`` witness check) passes it as *src_fd* instead — ownership
    transfers to this function, which closes it on every path — so the source is
    never re-opened by name; on Windows, which has no ``dir_fd`` support, that is
    the only pinned source form available. A
    destination reached by name is an ancestor swap away from landing the bytes
    somewhere else entirely -- a real gap in a by-name-only design, and it is why
    the by-name destination is now the
    exception rather than the only form.

    The destination is created with ``O_EXCL``, so anything already at that name is a
    planted link or alias rather than a file to overwrite and creation refuses it
    without a separate check. With *skip_existing* an occupied name is reported as
    skipped instead, which is what a merge that must not overwrite needs: exclusive
    creation makes "it did not exist" and "this call created it" one statement rather
    than two with a window between them.

    ``FileNotFoundError`` propagates so a caller can tolerate a source that vanished
    mid-walk; every other ``OSError`` propagates so real failures still abort.
    """
    if dst is None and dst_name is None:  # pragma: no cover - caller bug
        raise ValueError("copy_file_pinned needs either dst or dst_name")
    # O_NONBLOCK is not about performance. Opening a FIFO for reading BLOCKS until a
    # writer appears, so without it a single named pipe -- in an extracted archive, or
    # planted in a staged tree -- hangs the whole snapshot or restore forever with no
    # timeout and no message. Found when a mutation probe removed a caller's own
    # `is_file()` guard and the test run stalled until a watchdog killed it, which is
    # exactly how an operator would experience it. The fstat below still rejects the FIFO;
    # this only guarantees we reach that check. On a regular file the flag has no effect.
    src_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if src_fd is not None:
        # Caller-opened source: ownership TRANSFERS here — the descriptor is
        # closed on every path by the ``finally`` below, exactly like one this
        # function opened itself. This is the form for a caller that has
        # already validated its descriptor (``fd_real_path`` witness after an
        # ``os.open``): the by-name and ``dir_fd`` forms RE-OPEN the source,
        # which re-introduces the check-to-open window that validation closed —
        # and on Windows, which has no ``dir_fd`` support, this is the only
        # pinned source form available at all. The fstat gates below still run
        # against this descriptor, so a caller cannot use it to skip them.
        fd = src_fd
    else:
        try:
            if dir_fd is not None and name is not None:
                fd = os.open(name, src_flags, dir_fd=dir_fd)
            else:
                fd = os.open(by_name, src_flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                # A symlink at the final component. Which reason code depends on whether
                # the caller already LOOKED: with `expected_src_ident` the caller stat-ed
                # a regular file at this name and a link now sits there, which is the
                # same-UID swap this module is built to refuse, and a bundle missing that
                # file lacks something it was asked to carry. Without it this open is the
                # first look, and a link on first look is the ordinary screen. The two
                # reasons sit on opposite sides of the retention split, so reporting the
                # swap as a screen let it prune the last complete backup.
                on_skip(
                    SKIP_IDENTITY_CHANGED if expected_src_ident is not None else SKIP_SYMLINK,
                    by_name,
                )
                return False
            if skip_unreadable and isinstance(exc, PermissionError):
                # The SOURCE, and only here: this is the one open in this function
                # that reads the operator's file. Everything below writes.
                on_skip(SKIP_UNREADABLE_ENTRY, by_name)
                return False
            raise
    try:
        st = os.fstat(fd)
        if expected_src_ident is not None and (st.st_dev, st.st_ino) != expected_src_ident:
            # The descriptor is pinned, but it is not the inode the caller's
            # validation judged -- something was swapped in at the name between
            # the two. Refuse rather than copy bytes nobody vetted. Asked BEFORE the
            # type check: a swap to a FIFO or a directory is still a swap, and asking
            # the type first reported it as `not_regular`, a screen code, so the bundle
            # that omitted a wanted file read as complete and retention pruned on it.
            on_skip(SKIP_IDENTITY_CHANGED, by_name)
            return False
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            # Reached with a matching identity, or with none supplied: this is the same
            # inode the caller saw, or the first look at it, so a non-regular type or a
            # hardlink alias here is the design screen and not a swap.
            on_skip(SKIP_NOT_REGULAR, by_name)
            return False
        if max_bytes is not None and st.st_size > max_bytes:
            # BEFORE the destination is created: ``fstat`` reports a sparse
            # file's LOGICAL size, so the swapped-in sparse giant is refused
            # here with zero bytes written. The in-loop bound below covers the
            # one case this cannot -- a source that grows after this fstat.
            on_skip(SKIP_TOO_LARGE, by_name)
            return False
        # The bytes are written to the FINAL name, opened O_CREAT|O_EXCL, and no name is
        # resolved again afterwards. Three designs have now been tried on these lines and
        # this is the only one that satisfies pinned_fs's own central rule -- once a
        # descriptor exists, no write may be derived from a path name:
        #
        #   * exclusive create at the final name, cleanup unlinks that name after an
        #     identity check -- rejected because the name can change between the check and
        #     the unlink (POSIX has no unlink-by-inode), so cleanup could delete a file
        #     another writer had published;
        #   * private temporary published with `os.link` -- rejected because the LINK
        #     re-resolves the temporary BY NAME. Proven exploitable: swapping the `.part`
        #     entry after the copy makes the publish install attacker bytes and report the
        #     core file as restored. A descriptor-based publish would fix it, and there is
        #     no portable one -- `linkat(AT_EMPTY_PATH)` is not exposed by Python and the
        #     `/proc/self/fd` form is Linux-only and privileged;
        #   * this one: the descriptor IS the destination from the first byte. There is no
        #     publish step to attack, and no hard link, which is also why the FAT/exFAT
        #     fallback and its overwrite hazard are simply gone rather than guarded.
        #
        # The hazard is in the CLEANUP, not the create, so no name is ever
        # unlinked: on failure the file is emptied through the descriptor we
        # hold and the failure is reported. `O_EXCL` proves we created this entry, so
        # truncating it cannot touch anyone else's file, and a reported empty file with the
        # previous version still in the backup is a far smaller harm than either publishing
        # attacker bytes or deleting a concurrent writer's file.
        #
        # EEXIST keeps its two answers: with `skip_existing` an occupied name is a skip,
        # otherwise it is raised for the caller to translate.
        dst_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        published = False
        try:
            if dst_dir_fd is not None and dst_name is not None:
                dst_fd = os.open(dst_name, dst_flags, 0o600, dir_fd=dst_dir_fd)
            else:
                dst_fd = os.open(str(dst), dst_flags, 0o600)
        except FileExistsError:
            if skip_existing:
                return False
            raise
        # The destination is finished through its OWN descriptor and never by name again:
        # `os.chmod(name, dir_fd=...)` re-resolves the final component, so a name swapped
        # between the write and the chmod would have the mode applied to the replacement,
        # while `fchmod`/`futimes` on the open descriptor cannot be redirected.
        #
        # On failure only the TEMPORARY is removed. That is the whole point of writing to
        # one: the final name is never touched by the failure path, so no cleanup of ours
        # can delete a file another writer published there. The descriptor is closed before
        # the unlink because Windows refuses to remove a file with an open handle -- the
        # earlier form unlinked first and left the fragment exactly where cleanup was meant
        # to remove it, which my own Windows shard caught.
        try:
            if on_opened is not None:
                on_opened(os.fstat(dst_fd))
            exceeded = False
            with os.fdopen(fd, "rb") as fsrc:
                fd = -1  # ownership passed to the file object
                # fdopen takes ownership and closes what it is given, so it gets a
                # duplicate: dst_fd itself has to outlive the write for the two
                # descriptor-based metadata calls below.
                with os.fdopen(os.dup(dst_fd), "wb") as fdst:
                    if max_bytes is None:
                        shutil.copyfileobj(fsrc, fdst)
                    else:
                        # Enforced WHILE copying: the fstat pre-check above cannot
                        # see a source that grows after it, and only aborting on
                        # the first excess byte keeps the copy itself from
                        # exhausting the destination volume on the way to a
                        # rejection.
                        remaining = max_bytes
                        while True:
                            chunk = fsrc.read(min(1024 * 1024, remaining + 1))
                            if not chunk:
                                break
                            if len(chunk) > remaining:
                                exceeded = True
                                break
                            fdst.write(chunk)
                            remaining -= len(chunk)
            if exceeded:
                # AFTER the dup'd writer has closed (its buffer flushes on close,
                # so truncating first would let the flush write stale bytes
                # back). Same rule as the failure path below: the partial
                # content is emptied through the descriptor we hold (O_EXCL
                # proves the entry is ours), never by name.
                try:
                    os.ftruncate(dst_fd, 0)
                except OSError:
                    pass
                on_skip(SKIP_TOO_LARGE, by_name)
                return False
            _apply_metadata(
                dst_fd,
                st,
                dst=dst,
                dst_dir_fd=dst_dir_fd,
                dst_name=dst_name,
                mode=force_mode,
            )
            if on_created is not None:
                # Through the descriptor we still hold, so the witness is the
                # published inode itself — never a name re-resolution.
                on_created(os.fstat(dst_fd))
            published = True
        except BaseException:
            # No name is unlinked here. `O_EXCL` above proves this entry is ours, so the
            # partial content is emptied through the descriptor -- the one operation that
            # cannot be redirected at another writer's file. The caller's reporter is what
            # makes it visible; on a restore that reporter is fatal, so the operation stops
            # with the previous version still in the backup rather than continuing over a
            # truncated core file.
            if dst_fd >= 0:
                try:
                    os.ftruncate(dst_fd, 0)
                except OSError:
                    pass  # nothing further is safe to try, and the report still fires
                os.close(dst_fd)
                dst_fd = -1
            raise
        finally:
            if dst_fd >= 0:
                os.close(dst_fd)
        return published
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def write_file_pinned(
    target: Path | str,
    content: str,
    *,
    what: str,
    mode: int = 0o600,
    refusal: type[Exception] = OSError,
) -> None:
    """Publish *content* at *target* without ever following a planted link.

    THE single no-follow file-publish path. Every by-name ``write_text`` /
    ``open(..., "w")`` on an agent-influenced path is a truncation primitive
    pointed at whatever the name currently resolves to: replace the target with a
    symlink to a host file and the next publish truncates that file instead. The
    same hole was closed once for a pod's SSO seeding and once for the systemd
    unit files, in two separate hand-rolled copies -- so this exists to make the
    third copy impossible rather than to add a fourth.

    Three properties, and the middle one is the portable half:

    * The PARENT is pinned through :func:`create_and_open_dir_pinned`, so no
      ancestor is re-resolved by name between the check and the write, and the
      write lands relative to that descriptor.
    * The target is ``lstat``-ed THROUGH that descriptor and refused when it is a
      symlink or not a regular file. ``os.stat(..., follow_symlinks=False)`` exists
      on every platform Python supports, so this check holds even where
      ``O_NOFOLLOW`` does not (see :func:`supports_pinned_walk`) -- which is what
      keeps the guarantee real on Windows rather than silently platform-gated.
    * The publish itself is ``atomic_write_at`` on the pinned descriptor, so the
      mode is applied to the temp before the payload is reachable under the final
      name and a reader never sees a partial file.

    Residual, stated rather than implied: a same-UID process can still swap the
    target between the ``lstat`` and the rename. Per the recorded pod threat model
    a pod is operational isolation, not protection from arbitrary same-UID
    processes; what this closes is the case where the ATTACKER-PLANTED name is
    followed, which needs no race at all.

    **What pinning means on Windows, decided rather than left to a crash.** The
    pinned walk needs ``O_DIRECTORY``, ``O_NOFOLLOW`` and ``os.open`` in
    ``os.supports_dir_fd`` -- exactly what :func:`supports_pinned_walk` probes, and
    none of which Windows has (``atomic_write_at``'s own docstring records that a
    pinned caller is on POSIX). So there the publish degrades to BY NAME, and the
    degradation is bounded and named:

    * KEPT everywhere: the ``lstat`` refusal of a symlink or non-regular target --
      the leg that stops a PLANTED name from being followed, which is the attack
      needing no race, and the everywhere-floor this function promises.
    * LOST on Windows: ancestor pinning, so an ancestor swapped between the check
      and the write is not caught. No supported configuration relies on it (pods
      are systemd/launchd only), and the caller that publishes a pod's own
      credential tree gates on ``supports_pinned_walk()`` and REFUSES rather than
      degrades (``pod.runtime._seed_pod_os_home``).
    """
    target = Path(target)

    def _refuse_if_unsafe(existing: os.stat_result | None) -> None:
        if existing is None:
            return
        if stat.S_ISLNK(existing.st_mode):
            raise refusal(f"refusing to write {what} {target}: it is a symbolic link")
        if not stat.S_ISREG(existing.st_mode):
            raise refusal(f"refusing to write {what} {target}: it is not a regular file")

    if not supports_pinned_walk():
        _refuse_if_unsafe(lstat_by_name(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, content, restrict_to_owner=True)
        return
    dir_fd = create_and_open_dir_pinned(target.parent, what=f"{what} directory", refusal=refusal)
    try:
        _refuse_if_unsafe(stat_at(dir_fd, target.name))
        atomic_write_at(dir_fd, target.name, content, fsync=True, mode=mode)
    finally:
        os.close(dir_fd)


def write_new_file_pinned(
    dir_fd: int,
    name: str,
    data: bytes,
    *,
    mode: int,
    fsync: bool = False,
) -> None:
    """Create *name* under *dir_fd* with ``O_EXCL`` and write *data*, or leave nothing.

    THE single pinned create-if-absent transaction. The steering create, the skill
    create and the prompt create's by-name fallback each spelled this ~40-line
    sequence by hand, and each copy's pinned branch is exercised on one platform
    only -- so a step wrong in one copy stayed invisible on the platform that ran
    the others. One review found one step wrong per round: no rollback on a
    partial body, the close outside the guard, the rollback not identity-verified,
    the identity probe outside the guard. This function owns the sequence so a
    fix lands once.

    What it owns, in order, and why each line sits where it does:

    1. ``os.open(name, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_BINARY, mode, dir_fd=)``.
       ``O_EXCL`` makes create-if-absent one syscall and refuses a pre-planted
       leaf, a symlink included; ``O_NOFOLLOW`` refuses a link where the platform
       distinguishes it; ``O_BINARY`` is Windows-only and unreachable (no
       ``dir_fd`` there) but belongs on any ``os.open`` this module owns, so the
       bytes written are the bytes handed in. A ``FileExistsError`` propagates
       UNCHANGED: the error taxonomy stays at the call site (steering's
       ``exists``, the skill create's ``False``, the prompt create's 409), because
       that taxonomy is what differs between callers and a primitive that owned it
       would flatten three deliberate contracts into one.
    2. ``os.fstat`` on the descriptor, INSIDE the guarded region and BEFORE the
       first ``os.write``. The rollback below addresses a NAME, and a rival can
       unlink ours and create its own inside the failure window, so the inode this
       call created is what makes the cleanup "remove this object, or nothing".
       A probe that fails (EIO, ESTALE on a network filesystem) is rolled back like
       any other failure rather than escaping with the ``O_EXCL`` name on disk.
    3. The short-write loop, then ``os.fsync(fd)`` when *fsync* is set.
    4. ``os.close`` INSIDE the guarded region, with ``fd`` cleared first. A
       deferred write error is reported by ``close()`` (ENOSPC on the last flush,
       EIO on NFS), so a close in a ``finally`` beside the rollback would raise
       after the rollback was skipped and strand the partial body under an
       ``O_EXCL`` name -- the permanent ``exists`` the rollback exists to prevent.
       Clearing ``fd`` first keeps the rollback's own close from double-closing.
    5. ``os.fsync(dir_fd)`` when *fsync* is set, still inside the guard: the
       create made the ENTRY, and a caller whose success claims the file is on
       disk needs the entry flushed too; a failure here withdraws the publication
       through the same rollback.

    The rollback: the identity is re-asked THROUGH the descriptor when the first
    probe failed and the descriptor is still open (a transient EIO usually answers
    the second time, and a descriptor can never answer with another object), then
    the descriptor is closed, then :func:`unlink_verified` removes the name only
    while it still holds that inode. **No unlink runs without an identity.** With
    both probes failed the name STAYS: removing it would be removing whatever
    answers to the name, which is a file this code has never read. The cost is
    bounded, because the probe precedes the first write: a rollback with no
    identity is one where nothing was written, so what is left is an EMPTY file,
    never a truncated one. The original exception is re-raised either way.

    The residual is :func:`unlink_verified`'s and is stated there: POSIX has no
    unlink-by-inode, so the verify and the unlink are two syscalls on one name.
    """
    fd = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        mode,
        dir_fd=dir_fd,
    )
    leaf: os.stat_result | None = None
    try:
        leaf = os.fstat(fd)
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        if fsync:
            os.fsync(fd)
        fd, open_fd = -1, fd
        os.close(open_fd)
        if fsync:
            os.fsync(dir_fd)
    except BaseException:
        identity = leaf
        if fd >= 0:
            if identity is None:
                with suppress(OSError):
                    identity = os.fstat(fd)
            with suppress(OSError):
                os.close(fd)
        if identity is not None:
            unlink_verified(dir_fd, name, (identity.st_dev, identity.st_ino))
        raise


def read_file_pinned(
    target: Path | str,
    *,
    what: str,
    max_bytes: int = 64 * 1024,
    refusal: type[Exception] = OSError,
) -> str:
    """Read *target* without ever following a planted link. The READ counterpart
    to :func:`write_file_pinned`, with the identical chokepoint discipline.

    A by-name ``read_text`` on an agent-influenced path is a disclosure primitive
    pointed at whatever the name currently resolves to: replace the file with a
    symlink to a credential and the next read prints that credential under the
    original name's label. The write side of this exact path was already pinned;
    leaving the read by-name meant the same planted link still worked, just in the
    other direction (found in review on the pod refusal note, which
    ``kirocrew pod ls`` prints).

    Same two properties, same everywhere-floor as the write:

    * The PARENT is pinned via :func:`pin_parent`, so no ancestor is re-resolved
      by name between the check and the read.
    * The target is ``lstat``-ed THROUGH that descriptor and refused when it is a
      symlink or not a regular file. That check is portable, so it holds where
      ``O_NOFOLLOW`` does not.

    Windows degrades exactly as the write does: the ``lstat`` refusal of a
    non-regular target is KEPT (the leg that stops a PLANTED name from being
    followed, which needs no race), ancestor pinning is LOST, and no supported
    configuration relies on it because pods are systemd/launchd only.

    *max_bytes* bounds the read: a note is a line of prose, and a name pointed at
    something enormous should not be able to make a status command allocate it.
    """
    target = Path(target)
    # The ANCESTOR chain is resolved once, here, exactly as ``open_dir_pinned``
    # does it: ``pin_parent`` requires a caller-resolved parent, and resolving it
    # inside would re-follow whatever an ancestor points at by now. The FINAL
    # component is never resolved -- it is opened by name under the pinned parent
    # with ``O_NOFOLLOW`` and lstat-checked through that descriptor, which is what
    # refuses a planted link at the note's own name. (A real home is often reached
    # through a symlink -- ``/home/<user>`` -> ``/local/home/<user>`` on this class
    # of host -- so pinning an UNRESOLVED parent refuses every legitimate read.)
    parent = os.path.realpath(target.parent)
    name = target.name
    if not supports_pinned_walk():
        # Degraded but not defeated: keep the floor the docstring promises.
        st = lstat_by_name(target)
        if st is None:
            raise FileNotFoundError(f"{what} is not there: {target}")
        if not _stat.S_ISREG(st.st_mode):
            raise refusal(f"refusing to read {what}: {target} is not a regular file")
        with open(target, "rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    dir_fd = pin_parent(parent, what=what, refusal=refusal)
    try:
        st = stat_at(dir_fd, name)
        if st is None:
            raise FileNotFoundError(f"{what} is not there: {target}")
        if not _stat.S_ISREG(st.st_mode):
            raise refusal(f"refusing to read {what}: {target} is not a regular file")
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        try:
            return os.read(fd, max_bytes).decode("utf-8", errors="replace")
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def lstat_by_name(target: Path | str) -> os.stat_result | None:
    """``lstat`` *target* by name, or ``None`` when it is not there.

    The by-name counterpart to :func:`stat_at`, for the platforms with no
    descriptor-relative stat. Never follows the final component, which is what
    makes it the portable half of :func:`write_file_pinned`'s guarantee.
    """
    try:
        return os.stat(target, follow_symlinks=False)
    except OSError:
        return None


def unlink_pinned(target: Path | str, *, what: str) -> None:
    """Remove *target* through a pinned parent descriptor. Missing is success.

    The delete half of :func:`write_file_pinned`: an ``unlink`` by name resolves
    its ancestors afresh, so a link planted at a parent aims the removal somewhere
    else. Refuses nothing about the target's own type -- removing a link IS the
    correct outcome here, and unlink never follows the final component anyway.
    """
    target = Path(target)
    if not supports_pinned_walk() or os.unlink not in os.supports_dir_fd:
        # No pinned walk here (Windows). Fall back to the by-name removal: unlink
        # does not follow its FINAL component anywhere, so the residual is an
        # ancestor swap -- and a pod cannot boot on this platform at all
        # (systemd/launchd only), so no supported configuration relies on the
        # pinned form. See write_file_pinned for the same decision stated in full.
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        dir_fd = create_and_open_dir_pinned(
            target.parent, what=f"{what} directory", refusal=OSError
        )
    except (OSError, ValueError):
        return
    try:
        os.unlink(target.name, dir_fd=dir_fd)
    except (FileNotFoundError, NotADirectoryError):
        pass
    finally:
        os.close(dir_fd)


def stat_at(dir_fd: int, name: str) -> os.stat_result | None:
    """`lstat` *name* relative to *dir_fd*, or ``None`` if it is not there.

    The descriptor-relative answer to "what is this, and is it there?". A plain
    ``Path.is_file()`` re-resolves the whole path, so between the question and the use of
    the answer the object can be replaced -- and a guard that inspects the replacement
    while the code acts on the original is worse than no guard, because it reports success.
    Review found three such guards in code that was already holding the right descriptor.

    Never follows a link: the caller wants to know what the NAME is, and a link is one of
    the answers it needs to be able to see.
    """
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None


def is_regular_at(dir_fd: int, name: str) -> bool:
    """True when *name* under *dir_fd* is a plain file -- not a link, FIFO, or directory."""
    st = stat_at(dir_fd, name)
    return st is not None and _stat.S_ISREG(st.st_mode)


def _apply_metadata(
    dst_fd: int,
    st: os.stat_result,
    *,
    dst: str | Path | None,
    dst_dir_fd: int | None,
    dst_name: str | None,
    mode: int | None = None,
) -> None:
    """Copy mode and timestamps onto the destination, by descriptor where possible.

    The fallbacks name the DESTINATION, which is now the only thing they could name: the
    bytes are written straight to the final name under ``O_EXCL``. An earlier revision wrote
    to a private temporary and published it by link, so the fallbacks had to name that
    temporary instead -- that design is gone (the publish re-resolved a name, which is the
    one thing this module exists to avoid), and with it the parameter that carried the
    distinction.

    ``os.fchmod`` does not exist on Windows and ``os.utime`` only accepts a descriptor
    where ``os.utime in os.supports_fd``, so the fd form cannot be unconditional: an
    earlier revision called it always and crashed a Windows snapshot with
    ``AttributeError`` the moment it reached a core file. Caught in review.

    The fd form is preferred wherever it exists, because a name re-resolves: a final
    component swapped between the write and the chmod would have the mode applied to the
    replacement. Where it does not exist the by-name form is the only option available,
    and it is the same platform that already cannot pin a directory at all -- so this
    adds no exposure that the declared by-name traversal does not already carry.

    *mode* overrides the source's mode. Used for a restored security file, which must end
    up owner-only regardless of what the archive recorded.
    """
    want = _stat.S_IMODE(st.st_mode) if mode is None else mode
    fallback_name = dst_name
    fallback_path = str(dst) if dst else None
    if hasattr(os, "fchmod"):
        os.fchmod(dst_fd, want)
    elif dst_dir_fd is not None and fallback_name is not None:  # pragma: no cover - Windows
        os.chmod(fallback_name, want, dir_fd=dst_dir_fd)
    elif fallback_path is not None:  # pragma: no cover - Windows
        os.chmod(fallback_path, want)

    times = (st.st_atime_ns, st.st_mtime_ns)
    if os.utime in os.supports_fd:
        os.utime(dst_fd, ns=times)
    elif dst_dir_fd is not None and fallback_name is not None:  # pragma: no cover - Windows
        os.utime(fallback_name, ns=times, dir_fd=dst_dir_fd)
    elif fallback_path is not None:  # pragma: no cover - Windows
        os.utime(fallback_path, ns=times)


def unlink_verified(
    holder_fd: int,
    name: str,
    expect: tuple[int, int],
    *,
    on_error: Callable[[OSError], None] | None = None,
) -> bool:
    """Unlink *name* under *holder_fd*, only if it is still ``(st_dev, st_ino)`` *expect*.

    The residual is irreducible and better stated than implied: POSIX has no
    unlink-by-inode, so the stat and the unlink are two syscalls addressing the same NAME.
    What CAN be done is refuse when the name holds anything but what the scan saw, which is
    what turns "delete whatever answers to this name" into "delete this object, or nothing".
    The remaining window needs a swap landing between two adjacent syscalls, and the
    directory holding the name was itself reached only through verified descriptors.

    *on_error* receives the exception when the UNLINK itself is refused -- a permission or
    read-only mount, never an identity mismatch, which is a deliberate "no" and stays
    silent. Both still answer ``False``; the callback is how a caller that must report
    the first kind tells it from the second.
    """
    try:
        info = os.stat(name, dir_fd=holder_fd, follow_symlinks=False)
    except OSError:
        return False
    if (info.st_dev, info.st_ino) != expect:
        return False
    try:
        os.unlink(name, dir_fd=holder_fd)
    except OSError as exc:
        if on_error is not None:
            on_error(exc)
        return False
    return True


def unlink_verified_by_name(
    parent: Path,
    name: str,
    expect: tuple[int, int],
    *,
    on_error: Callable[[OSError], None] | None = None,
) -> bool:
    """Unlink *parent/name* only while it holds ``(st_dev, st_ino)`` *expect*.

    This is the path-only sibling of :func:`unlink_verified` for the Windows
    branch and client-side asides that have no directory descriptor. It pins
    the parent, delegates to :func:`unlink_verified` on POSIX, and checks the
    regular-file identity under the pin before unlinking on Windows. An absent
    or mismatched name is a deliberate refusal and returns ``False`` without
    deleting anything.
    """
    pin = pin_directory(parent)
    try:
        if os.name != "nt":
            return unlink_verified(pin, name, expect, on_error=on_error)
        target = parent / name
        try:
            info = os.stat(target, follow_symlinks=False)
        except OSError:
            return False
        if not _stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != expect:
            return False
        try:
            os.unlink(target)
        except OSError as exc:
            if on_error is not None:
                on_error(exc)
            return False
        return True
    finally:
        os.close(pin)
