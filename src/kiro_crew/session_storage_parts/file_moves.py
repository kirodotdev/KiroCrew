"""Moving one session's files into a batch and back, never through a link.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

The mutation lock every move and delete holds, resolving a batch id to its directory,
the no-follow exclusive move and the durable cross-filesystem copy, rolling a session
back, where a staged file came from and may go back to, and the files a batch holds
that its manifest does not list.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import IO, Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import fsync_dir, replace_with_retry
from kiro_crew.chat_attachments import ATTACHMENTS_DIR_SUFFIX, attachments_dir
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.history import (
    ARCHIVE_DIR_NAME,
    THREADS_DIR_NAME,
    THREADS_SIDECAR_SUFFIX,
    threads_sidecar_for_stem,
)
from kiro_crew.session_storage_parts.batch_manifest import _manifest_rels
from kiro_crew.session_storage_parts.store_layout import (
    _O_NOFOLLOW,
    _UNIT_ID_RE,
    INCOMING_PREFIX,
    MANIFEST_NAME,
    MUTATION_LOCK_NAME,
    STAGE_CLI_LEAF,
    STAGE_CREW_LEAF,
    SessionStorageError,
    _crew_archive_dir,
    _crew_sessions_dir,
    logger,
    trash_root,
)


@contextmanager
def _mutation_lock() -> Iterator[None]:
    """Serialize every operation that moves or deletes staged files.

    Two concurrent reclaims can select the same session and interleave their
    moves, landing one half in each batch — after which neither batch can restore
    it, and emptying either destroys half a session. A file lock rather than a
    thread lock because instances share the kiro-cli store: a pod and the live
    gateway both read ``~/.kiro/sessions/cli``, so in-process exclusion excludes
    nothing.

    :func:`platform_compat.file_lock` fails closed, so a lock that cannot be taken
    raises instead of entering the section unserialized.
    """
    lock_path = trash_root().parent / MUTATION_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(fd, exclusive=True, required=True):
            yield
    finally:
        os.close(fd)


def _batch_dir(batch_id: str) -> Path:
    """Resolve a batch id to its directory, refusing anything that is not one.

    The id itself cannot traverse — it is pattern-checked — but a **link** planted
    under the trash root can, and its name would pass that check. Following one
    would let a crafted manifest drive moves into or out of paths this module does
    not own, in either direction. A batch is a real directory inside the trash root
    or it is not a batch, so both the link check and the containment check happen
    here, at the one place every caller resolves an id through.

    The link test goes through :func:`platform_compat.is_link_or_junction` because
    ``is_symlink()`` reports False for an NTFS **junction**: on Windows a junction
    named as a valid batch id would read as a real directory, and the delete would
    resolve through it to whatever batch it points at.

    Containment is anchored to the DATA HOME, not to the trash root alone. The trash
    root lives under a directory the agent can write, so it could itself be replaced
    with a link: resolving relative to it would then accept batches under whatever it
    points at, and "empty everything" would recurse into directories this module
    does not own.

    The root must also not BE a link. Anchoring alone is not enough, because a link
    can point at another directory *inside* the data home — the live sessions or
    archive tree — which satisfies both the anchor and containment while making
    `empty` delete real session data.
    """
    if not _UNIT_ID_RE.match(batch_id):
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}")
    root = trash_root()
    if platform_compat.is_link_or_junction(root):
        raise SessionStorageError(f"the trash root is a link: {root}")
    path = root / batch_id
    if platform_compat.is_link_or_junction(path):
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}")
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
        home = data_home().resolve()
    except OSError as exc:  # pragma: no cover - defensive
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}") from exc
    if home not in resolved_root.parents:
        raise SessionStorageError(f"the trash root does not live under the data home: {root}")
    if resolved_root not in resolved.parents:
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}")
    return path


def _open_source_no_follow(src: Path) -> IO[bytes]:
    """Open *src* for reading, refusing to traverse a symlink in its final component.

    Both movers' COPY fallbacks read through this; their fast paths neither need it nor
    have the hole. ``os.rename`` moves a symlink rather than what it points at, and
    ``os.link`` links to the link itself — only a copy dereferences. Without this the
    same mover would mean two different things depending on whether source and
    destination happen to share a filesystem, and the cross-device meaning is the
    dangerous one in both directions: an indexed session file swapped for a link to a
    credential store would have the SECRET's bytes copied into a trash the agent can
    read, and a link planted in the trash would have arbitrary bytes copied back out
    over a restored origin. This module's threat model already assumes planted files,
    so it has to assume planted links too.

    Refused rather than followed, and not recreated as a link either: a symlink in the
    session store is anomalous, the staging loop already knows how to leave a session
    alone when one of its files will not move, and failing closed is the direction that
    cannot leak.

    The guard covers the FINAL component, which is what the swap it defends against
    replaces. A symlinked parent directory is still traversed; closing that needs an
    ``openat`` walk down the chain and is its own change.
    """
    # O_NOFOLLOW is POSIX and absent on Windows, where creating a symlink needs
    # privilege this model does not hand out. Degrade rather than fail to import.
    return open(src, "rb", opener=lambda path, flags: os.open(path, flags | _O_NOFOLLOW))


def _publish_then_drop(src: Path, dst: Path, *, dst_dir_fd: int | None = None) -> None:
    """Make a just-created *dst* durable, then remove *src*. Both movers end here.

    One code path for both of :func:`_move_file_exclusive`'s branches, because they have
    the same shape and the same hazard: a hard link and a copy both leave TWO directory
    entries for one file, and removing the source is a SECOND metadata operation. Unlike
    a same-filesystem rename — one atomic operation, where forcing the destination side
    forces the removal with it — nothing orders these two for us. If the unlink reaches
    disk and the new name does not, the inode has no name left at all and the session is
    gone, which is why the destination is synced BEFORE the source is dropped rather than
    alongside it.

    A failing destination sync ATTEMPTS to withdraw *dst* — the unlink is suppressed so
    that it cannot displace the sync error, which is the one that says what went wrong.
    That withdrawal is safe because the SOURCE has not been touched at that point, so
    removing *dst* provably restores the state the call started in. The unlink below is
    different: once it has been attempted, no observation available here proves whether
    the original survived, so nothing is withdrawn there. This function's callers read an
    exception as "the origin was not taken", which the sync case honours exactly. The
    unlink case can leave *dst* behind, and that leftover is deliberate: it is a file no
    manifest entry names, so :func:`_unlisted_files` refuses to delete it and the batch
    cannot be emptied until an operator clears it. That is a worse report than the truth
    but a recoverable one, and it is chosen over withdrawing a copy that may be the last
    one. Only the calling move created *dst*, so keeping it costs nothing that predates
    the call either.

    The source-side sync is ``best_effort`` and runs after the unlink, where nothing may
    raise: the move is complete by then, so an exception could only mislabel finished work
    as failed, and a source entry that is not yet durable can at worst resurrect a
    duplicate — never lose the copy now at *dst*.
    """
    try:
        if dst_dir_fd is not None:
            # The destination directory is the one the leaf was created in --
            # the pinned descriptor -- not whatever the path resolves to now:
            # a directory swapped under the name after the open would otherwise
            # be the one synced (and the source then dropped) while the leaf
            # sits in the old one, unreachable.
            os.fsync(dst_dir_fd)
        else:
            fsync_dir(dst.parent)
    except OSError:
        with suppress(OSError):
            if dst_dir_fd is not None:
                os.unlink(dst.name, dir_fd=dst_dir_fd)
            else:
                dst.unlink()
        raise
    # Propagates untouched on failure, leaving *dst* where it is. Withdrawing it would
    # be guessing: a filesystem can report a failure for an unlink that DID commit (a
    # network filesystem losing the reply to a completed operation), and the source name
    # existing afterwards does not prove the original still does — a concurrent append
    # recreates that name with a NEW, empty file, at which point *dst* holds the only
    # copy of the history and deleting it destroys exactly what this module exists to
    # keep. Nothing available here separates those cases, so the leftover is accepted:
    # it stays in the batch as a file no manifest entry names, which _unlisted_files()
    # refuses to delete, and it blocks emptying that batch until an operator clears it.
    # A wedge an operator can undo beats bytes nobody can get back.
    src.unlink()
    fsync_dir(src.parent, best_effort=True)


def _move_file_exclusive(src: Path, dst: Path, *, dst_dir_fd: int | None = None) -> bool:
    """Move *src* to *dst*, never replacing an existing *dst*. False if occupied.

    With *dst_dir_fd* (POSIX), the destination is created relative to that
    already-pinned directory descriptor -- ``dst.name`` under it -- so a parent
    swapped for a link between the caller's check and this move cannot carry the
    file elsewhere, and every later step (metadata, the directory sync, a
    withdrawal) addresses that same descriptor, never the path, so a swap after
    the open cannot make a sync of the swapped-in directory count for the one
    the leaf landed in.

    Restore's preflight checks that the origin is free, but the session can be
    recreated in the interval before the move — and ``os.rename`` replaces the
    destination silently, so undoing a deletion would destroy the newer data it was
    meant to protect. Creating the destination exclusively makes the check and the
    write one atomic step, so a lost race is reported rather than acted on.

    Both branches finish through :func:`_publish_then_drop`, which is where the
    durability ordering lives.
    """
    # The plain ``link()`` call never dereferences *src*, but the descriptor-relative
    # form is ``linkat`` and CPython hands that ``AT_SYMLINK_FOLLOW`` unless told
    # otherwise -- a planted link in the staged trash would then be resolved and a
    # hard link to its TARGET published under ``.threads``. Say so on both branches
    # where the platform lets us, so the two halves mean the same thing.
    link_kwargs: dict[str, Any] = {}
    if os.link in os.supports_follow_symlinks:
        link_kwargs["follow_symlinks"] = False
    try:
        if dst_dir_fd is not None:
            os.link(src, dst.name, dst_dir_fd=dst_dir_fd, **link_kwargs)
        else:
            os.link(src, dst, **link_kwargs)
    except FileExistsError:
        return False
    except OSError:
        # A different filesystem, or one without hard links: copy into a file that
        # must not already exist, which keeps the no-clobber guarantee.
        try:
            if dst_dir_fd is not None:
                fd = os.open(
                    dst.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_dir_fd
                )
            else:
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as out, _open_source_no_follow(src) as handle:
                shutil.copyfileobj(handle, out)
                # Before the unlink, not after: this is the moment the copy stops
                # being a second copy. Buffered bytes live in this process and then
                # in the page cache, so without forcing them out a power-off between
                # the last write and the unlink reaching disk comes back to an
                # empty-or-partial destination and no source — the one outcome a MOVE
                # of the only copy must never produce.
                #
                # copystat first so the fsync forces the metadata with the bytes.
                out.flush()
                if dst_dir_fd is not None:
                    # Metadata onto the descriptor we wrote, not a path lookup.
                    st = os.stat(src)
                    os.utime(out.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
                else:
                    shutil.copystat(src, dst)
                os.fsync(out.fileno())
        except OSError:
            with suppress(OSError):
                if dst_dir_fd is not None:
                    os.unlink(dst.name, dir_fd=dst_dir_fd)
                else:
                    dst.unlink()
            raise
        _publish_then_drop(src, dst, dst_dir_fd=dst_dir_fd)
        return True
    _publish_then_drop(src, dst, dst_dir_fd=dst_dir_fd)
    return True


def _move_sidecar_pinned(src: Path, dst: Path) -> bool:
    """:func:`_move_file_exclusive` into a `.threads` directory pinned by descriptor.

    Opens the parent ``O_DIRECTORY|O_NOFOLLOW`` (a link at that name is an
    ``OSError`` the caller treats as "not taken") and publishes the leaf
    relative to it. Windows has no descriptor-relative link; there the by-name
    move runs after the preflight's link check, as the store's writer degrades.
    """
    if not platform_compat.IS_POSIX:
        if platform_compat.is_link_or_junction(dst.parent):
            raise NotADirectoryError(
                errno.ENOTDIR, "thread sidecar directory is a link", str(dst.parent)
            )
        return _move_file_exclusive(src, dst)
    dir_fd = os.open(
        dst.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return _move_file_exclusive(src, dst, dst_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _move_file(src: Path, dst: Path) -> None:
    """Move one file, preferring a rename and falling back to a copy.

    A rename is atomic and instant, which is what makes a trash usable at tens of
    gigabytes. It only works within one filesystem, so a data home mounted apart
    from the kiro-cli store falls back to a copy — slow, and it needs the space
    twice while it runs.

    That fallback is NOT ``shutil.move``. ``shutil.move`` copies straight to the
    final name and then unlinks the source, so it spends the whole copy with a
    partial file sitting under the name the manifest will record: an exit there
    leaves a staged file that looks complete and silently is not. Copy into a
    private incoming name instead, force the bytes out, and publish with a rename —
    then the destination name only ever exists whole, and the source is not
    unlinked until it is durable.
    """
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    _copy_across_filesystems(src, dst)


def _copy_across_filesystems(src: Path, dst: Path) -> None:
    """Copy *src* to *dst* durably and atomically, then remove *src*.

    The incoming file is created in ``dst.parent`` because the publishing step is a
    rename, and a rename is only atomic within one filesystem — the destination's
    own directory is the only place that is guaranteed to be. It carries
    :data:`INCOMING_PREFIX` so an operator who finds one after a crash can see what it
    is. That is ALL the prefix does: :func:`_unlisted_files` deliberately exempts
    nothing by name, so an interrupted copy stays protected as unlisted data rather
    than being deleted as scratch. Erring that way is the safe direction — a name says
    nothing about who wrote it, and this module already assumes planted files — at the
    documented cost that such a leftover blocks its batch from being emptied until an
    operator removes it.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(dst.parent), prefix=INCOMING_PREFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, _open_source_no_follow(src) as handle:
            shutil.copyfileobj(handle, out)
            out.flush()
            # On the temp, not on dst: dst does not exist yet, and the point is that
            # the name appears already carrying the source's mode and timestamps. The
            # mtime in particular is what the rest of this module ages sessions by.
            #
            # BEFORE the fsync, because copystat writes the inode and the fsync is
            # what forces the inode out. After it, a power loss could publish the
            # bytes under the temp's own 0600 and its creation time instead.
            shutil.copystat(src, tmp)
            os.fsync(out.fileno())
        replace_with_retry(tmp, dst)
    except BaseException:
        # BaseException, matching atomic_write: a Ctrl-C mid-copy must not leave the
        # incoming file behind either. The exception itself propagates untouched.
        with suppress(OSError):
            tmp.unlink()
        raise
    # Publish, then make the publication durable, and only then drop the source.
    # Reversing the last two would be the same lost-only-copy window the exclusive
    # mover guards against.
    #
    # A failing sync takes the published name back out. The caller reads an exception
    # as "this file did not move", and it is only entitled to read it that way if
    # nothing of the move survives: leaving dst behind would put a file in the batch
    # that no manifest entry names, which blocks emptying that batch for good. The
    # source has not been touched yet, so unlinking dst restores the state this call
    # started in exactly.
    try:
        fsync_dir(dst.parent)
    except OSError:
        with suppress(OSError):
            dst.unlink()
        raise
    # Propagates untouched on failure, leaving *dst* in the batch: see
    # :func:`_publish_then_drop` for why withdrawing it would be a guess that can
    # destroy the only remaining copy.
    src.unlink()
    # Nothing raising may follow the unlink. The move is COMPLETE here, and this
    # function's caller reads an exception as "this file did not move" — it rolls the
    # session back and omits the file from the manifest, which for a file that HAS
    # moved is a half-staged session that nothing records: the exact split the loop's
    # rollback exists to prevent. The source directory still wants syncing (see
    # :func:`_sync_batch`, which does it once per batch), so the caller collects it
    # rather than paying a per-file sync here.


def _file_stamp(path: Path) -> tuple[int, float]:
    """The size and last-modified time of a file about to be staged.

    Raises ``OSError`` when unreadable. A named operation rather than an inline
    ``stat`` because both readings are load-bearing and both come from ONE call:
    a file whose size cannot be read cannot be recorded, and an unrecorded file
    is one restore cannot put back; the mtime is what tells the move loop the
    file has been touched since this batch was validated (see
    :func:`_move_to_trash_locked`). Reading them separately would be two stats
    describing two instants, which is the class of bug the caller is guarding
    against.
    """
    info = path.stat()
    return info.st_size, info.st_mtime


def _rollback(moved: list[tuple[Path, Path]]) -> bool:
    """Undo a partial session move, best effort. True if everything went back.

    Each pair is (where it landed, where it came from). A rollback that itself
    fails is logged and nothing more: the alternative is raising over a caller
    already handling a failure, which would abandon the rest of the batch too.
    The return value exists for the one caller that makes a POSITIVE claim about
    the outcome — "this session was left in place" is only true if every file
    actually went back — and callers that merely omit a session can ignore it.

    The move is EXCLUSIVE. A rollback runs after something already went wrong, so
    the origin may have been recreated in the meantime — and a plain rename would
    replace that newer session data with the copy being put back, turning a handled
    failure into data loss. An occupied origin leaves the file where it is staged,
    which keeps it recoverable, and is reported here as an incomplete rollback
    because the staged copy is then in the batch without being in its manifest.
    """
    complete = True
    for landed, origin in reversed(moved):
        try:
            if not _move_file_exclusive(landed, origin):
                logger.warning(
                    "not rolling %s back: %s was recreated and is newer",
                    landed,
                    origin,
                )
                complete = False
        except OSError:
            logger.warning("could not roll %s back to %s", landed, origin, exc_info=True)
            complete = False
    return complete


def _staged_path(batch: Path, rel: str) -> Path | None:
    """Resolve a manifest ``rel`` inside *batch*, or ``None`` if it escapes.

    ``Path("/a/b") / "/etc/passwd"`` is ``/etc/passwd`` — joining an absolute
    string discards the base entirely. The manifest lives under the data home,
    which is agent-writable, so a tampered or corrupted ``rel`` would otherwise
    let restore pick up any file on the host and relocate it. Both the absolute
    form and ``..`` traversal are refused.
    """
    if not rel or Path(rel).is_absolute():
        return None
    candidate = batch / rel
    try:
        resolved = candidate.resolve()
        root = batch.resolve()
    except OSError:
        return None
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return candidate


def _canonical_origin(rel: str) -> Path | None:
    """Where a staged file must have come from, DERIVED from its staged path.

    The staged path already encodes the store and the filename, and the filename
    is the session's identity — a replay log is ``<sid>.jsonl`` and a transcript is
    ``<stem>.jsonl``. So the origin is not information the manifest needs to
    supply, and accepting it would let a tampered in-store origin relocate one
    session's file under another session's name: both paths pass a
    "inside a session store" test, and the result is a corrupted session with no
    error a user could connect to a restore.

    Deriving removes the choice. The recorded origin is then only checked for
    agreement, so a disagreement is refused instead of followed.
    """
    parts = PurePosixPath(rel).parts
    if not parts:
        return None
    name = parts[-1]
    if not name or name != Path(name).name or name in (os.curdir, os.pardir):
        return None
    if parts[0] == STAGE_CLI_LEAF and len(parts) == 2:
        return kiro_sessions_dir() / name
    if parts[0] == STAGE_CREW_LEAF:
        if len(parts) == 2:
            return _crew_sessions_dir() / name
        if len(parts) == 3 and parts[1] == ARCHIVE_DIR_NAME:
            return _crew_archive_dir() / name
        if len(parts) == 3 and parts[1].endswith(ATTACHMENTS_DIR_SUFFIX):
            # ``crew/<stem>.attachments/<image>``: the directory name IS the
            # session's identity, so it is checked as one -- a name that is not
            # a session id cannot have been staged from the attachments store.
            stem = parts[1][: -len(ATTACHMENTS_DIR_SUFFIX)]
            if not _UNIT_ID_RE.match(stem):
                return None
            return attachments_dir(_crew_sessions_dir(), stem) / name
        if len(parts) == 3 and parts[1] == THREADS_DIR_NAME:
            # ``crew/.threads/<stem>.json``: the file name IS the session's
            # identity, checked as one, the way the transcript's is.
            if not name.endswith(THREADS_SIDECAR_SUFFIX):
                return None
            stem = name[: -len(THREADS_SIDECAR_SUFFIX)]
            if not _UNIT_ID_RE.match(stem):
                return None
            return threads_sidecar_for_stem(_crew_sessions_dir(), stem)
    return None


def _origin_path(origin: str) -> Path | None:
    """Accept an *origin* only if it names a file inside a session store.

    Restore writes to this path, so an unconstrained origin from a tampered
    manifest is an arbitrary-write primitive. Only the two directories this module
    ever reclaims from are permitted; anything else — a credential file, a config,
    a path outside the homes entirely — is refused.
    """
    if not origin:
        return None
    candidate = Path(origin)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve()
        allowed = [
            kiro_sessions_dir().resolve(),
            _crew_sessions_dir().resolve(),
            _crew_archive_dir().resolve(),
        ]
    except OSError:
        return None
    if not any(resolved.is_relative_to(root) for root in allowed):
        return None
    return candidate


def _unlisted_files(batch: Path) -> list[Path]:
    """Staged files no manifest entry names.

    A process exit between moving a file into a batch and appending its manifest
    line leaves a file nothing points at. It is still the ONLY copy of that
    session's data, and the user was never shown it — the batch may even list zero
    sessions — so no cleanup path may remove it.

    An unreadable manifest yields every file, which is the safe direction: without
    the manifest nothing in the batch is accounted for.

    There is deliberately NO exemption for the incoming name
    :func:`_copy_across_filesystems` writes, even though a crash can leave one here and
    a caller then cannot empty the batch until an operator removes it. A name is not
    proof of who wrote it: everything else in this module's threat model assumes a
    batch can be planted in (a linked manifest, a file substituted at the manifest's
    name), so anything that may rename a file inside a batch could pick that prefix and
    have this scan pass over a staged file the manifest never approved for deletion.
    Blocking a deletion that should proceed is recoverable; letting one through is not.

    A scan that cannot be completed RAISES rather than returning a short list. This
    function exists to block deletions, so an empty result must mean "there is
    nothing unaccounted for", never "the walk gave up early" — ``rglob`` skips
    unreadable directories silently, which would turn a transient error into
    permission to delete a file that is the only copy of a session.
    """
    # An unreadable manifest yields no listed names, so every file below counts as
    # unlisted — the safe direction this docstring describes.
    listed: set[str] = set(_manifest_rels(batch))
    failures: list[OSError] = []
    unlisted = []
    # os.fwalk where the platform has it: this guard must complete on trees
    # whose component paths exceed the platform PATH_MAX (1024 on macOS, where
    # a name-based walk dies with ENAMETOOLONG and permanently wedges the batch
    # as ``unreadable_batch``). fwalk traverses and stats via directory
    # descriptors, so only the MANIFEST-RELATIVE strings below ever use the
    # textual path, and those are pure string operations with no length limit.
    # This also matches the descriptor discipline of the approval scan,
    # chain-open, and removal passes; the guard is otherwise the one name-based
    # walk.
    #
    # CPython defines fwalk only where ``{open, stat} <= os.supports_dir_fd``
    # — on native Windows it does not exist, mirroring pinned_delete's
    # ``_FD_SAFE_DELETE`` coarse path. There the ORIGINAL name-based walk is
    # used: Windows has no macOS 1024-byte wedge, and an unconditional fwalk
    # would crash every Empty-Trash with an AttributeError no caller catches.
    fwalk = getattr(os, "fwalk", None)
    if fwalk is not None:
        # Unlike os.walk, fwalk RAISES when the top itself cannot be statted or
        # opened rather than routing that first error through ``onerror`` — catch
        # it into the same failure list so an unopenable batch stays a refusal
        # with a reason, never an escaping OSError. RecursionError is belt only:
        # CPython's fwalk is iterative on every Python this package supports
        # (>=3.12; verified at depth 5000 under the default 1000-frame limit),
        # but the guard's contract is that NO traversal failure escapes as a
        # crash, so the impossible case still lands in the failure list.
        try:
            for root, _dirs, names, rootfd in fwalk(batch, onerror=failures.append):
                for name in names:
                    if name == MANIFEST_NAME:
                        continue
                    try:
                        # follow_symlinks=True mirrors the Path.is_file() this
                        # replaces: a symlink to a regular file counts, a broken
                        # link does not.
                        st = os.stat(name, dir_fd=rootfd)
                    except OSError as exc:
                        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP):
                            # The same cases Path.is_file() reports as False.
                            continue
                        failures.append(exc)
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    path = Path(root) / name
                    if path.relative_to(batch).as_posix() not in listed:
                        unlisted.append(path)
        except OSError as exc:
            failures.append(exc)
        except RecursionError as exc:
            failures.append(OSError(f"traversal exceeded the recursion limit: {exc}"))
    else:
        for root, _dirs, names in os.walk(batch, onerror=failures.append):
            for name in names:
                path = Path(root) / name
                if name == MANIFEST_NAME:
                    continue
                try:
                    if not path.is_file():
                        continue
                except OSError as exc:
                    failures.append(exc)
                    continue
                if path.relative_to(batch).as_posix() not in listed:
                    unlisted.append(path)
    if failures:
        raise SessionStorageError(
            f"could not read all of {batch.name!r}, so it is not known whether it "
            f"holds files nothing lists: {failures[0]}"
        )
    return unlisted


def _identity_of(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` for *path*, or None if it cannot be read.

    Links are not followed: the question is what this NAME holds, never what it points at.
    Taken so a later removal can be bound to the directory the caller was actually looking
    at - see :func:`_remove_emptied_batch` for why a pinned walk alone does not establish
    that.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)
