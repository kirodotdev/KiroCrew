"""The descriptor-pinned replace: one no-follow open anchors validation, the
compare-and-swap hash, the metadata capture and the directory-fd-relative
staged rename.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import hashlib as _hashlib
import os
import re
import stat as _stat
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _XATTR_UNSUPPORTED_ERRNOS,
        _fd_real_path,
        _is_access_control_xattr,
        _opened_path_within_root,
        _should_carry_xattr,
        is_sensitive_path,
        logger,
        platform_compat,
        validate_file_path,
    )


def _pinned_replace(
    raw: str,
    content: str,
    within_root: str | None = None,
    base_hash: str | None = None,
    max_bytes: int | None = None,
) -> str:
    """Overwrite an EXISTING regular file, pinned to the descriptor opened.

    The engine behind :func:`safe_write_file_nolink` (no verification) and
    :func:`verified_replace_file_nolink` (compare-and-swap). Returns an
    outcome string: ``"ok"``, ``"refused"``, and — only when *base_hash* is
    given — ``"conflict"`` (the file's bytes or identity differ from the
    edit base) or ``"too_large"`` (the file outgrew *max_bytes* since the
    base was read, which by definition is not the state the edit was made
    against).

    When *base_hash* is given, the sha256 of the CURRENT bytes is computed by
    reading the SAME descriptor every other check runs against — one
    ``O_NOFOLLOW`` open, one name resolution, so no by-name re-read can be
    redirected between the verify and the replace. The residual is the data
    race between that read and the rename; it is narrowed twice below (the
    staged-rename identity re-check, and the mtime/size re-check in verify
    mode) and a detected change answers ``"conflict"`` — the newer file wins,
    never the stale edit.

    The write twin of :func:`safe_read_file_bytes_nolink`, and it exists for the
    same reason: validating a path by name and then opening it by name leaves a
    check-to-use window in which the final component -- or an ancestor
    directory -- can be swapped for a symlink, so the write lands on a file the
    caller never authorized. Here the open happens FIRST (``O_NOFOLLOW``), then
    every check runs against that descriptor: ``fstat`` rejects hardlinks and
    non-regular files, and when ``within_root`` is given the OPENED inode's real
    path must resolve inside it and must not be sensitive. Failing to determine
    the fd's real path fails closed.

    The target is opened WITHOUT ``O_CREAT``: a caller mirroring content back to
    a file it read earlier has no business creating one, and refusing turns
    "the file moved" into a no-op rather than a surprise new file. The bytes then
    land via an atomic replace (staged sibling + directory-fd-relative rename),
    so a write that fails partway leaves the original untouched instead of a
    truncated file.
    """
    path = validate_file_path(raw)
    if path is None:
        return "refused"
    encoded = content.encode("utf-8")
    try:
        # O_RDWR, not O_WRONLY: the no-dir-fd path below needs to READ the
        # original bytes before truncating so it can put them back if the write
        # fails. Same inode checks either way.
        #
        # O_BINARY is REQUIRED on Windows, where os.open defaults to TEXT mode
        # and os.write then expands every \n to \r\n — so a caller handing this
        # writer exact bytes got a longer file back, and a body just under a
        # size cap lands as a file over it. Absent on POSIX, where getattr
        # yields 0 and there is no text mode to opt out of. Same convention as
        # dashboard/token_secret.py and dashboard/handlers/files.py.
        fd = os.open(
            path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
    except OSError:
        return "refused"
    # The descriptor must survive validation (the no-dir-fd path below writes
    # THROUGH it), so it cannot be closed in a blanket `finally`. Validation
    # therefore runs in a nested function: every rejection is a single return
    # here, and the one caller closes the fd on any of them. Returning False
    # directly from inside the checks is what leaked descriptors -- one per
    # rejected update, until the gateway ran out.

    def _validate() -> tuple[int, tuple[int, int]] | None:
        st = os.fstat(fd)
        if st.st_nlink > 1 or not _stat.S_ISREG(st.st_mode):
            return None
        # Carried to the staged file below: a replace that dropped the original's
        # permissions would silently turn a 0644 shared doc or an 0755 script
        # into 0600 and break every other reader (or the execute bit).
        mode = _stat.S_IMODE(st.st_mode)
        if within_root is not None:
            fd_real = _fd_real_path(fd)
            if fd_real is None:
                return None  # cannot verify containment -> fail closed
            if not _opened_path_within_root(fd_real, within_root):
                return None  # opened inode escapes the approved tree
            if is_sensitive_path(fd_real):
                return None

        # (st_dev, st_ino): the staged rename re-resolves `base` against a
        # directory fd, and only this pair proves it lands on the checked file.
        # st_uid vs geteuid: a rename installs a NEW inode owned by THIS
        # process's user, so replacing a file owned by someone else (a
        # group-writable file in a shared project) would silently transfer
        # ownership away from its owner, and only root could chown it back. The
        # caller uses this to pick the write mechanism.
        # getattr, not os.geteuid() directly: it does not exist on Windows, and
        # AttributeError is NOT an OSError -- it would escape this function's
        # `except OSError`, escape the caller, and surface as a 500 with the
        # descriptor leaked and current.html already written. Same reason
        # O_DIRECTORY and fchmod are guarded below; this is the third
        # POSIX-only attribute in this one function.
        return mode, (st.st_dev, st.st_ino)

    try:
        validated = _validate()
    except OSError:
        validated = None
    if validated is None:
        try:
            os.close(fd)
        except OSError:
            pass
        return "refused"
    src_mode, src_ident = validated
    src_state: tuple[int, int] | None = None

    if base_hash is not None:
        # COMPARE half of the compare-and-swap, on the descriptor itself. The
        # bytes hashed here are the bytes of the inode every later check pins,
        # not a second by-name lookup that an ancestor or leaf swap could
        # redirect. Bounded: a file that outgrew the caller's cap since the
        # edit base was read cannot match that base.
        try:
            chunks: list[bytes] = []
            seen = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                seen += len(chunk)
                if max_bytes is not None and seen > max_bytes:
                    # Guarded close, then return: an unguarded close that
                    # raises would fall into the outer except, close the
                    # already-released fd number a second time, and rewrite
                    # this outcome as "refused".
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    return "too_large"
                chunks.append(chunk)
            if _hashlib.sha256(b"".join(chunks)).hexdigest() != base_hash:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return "conflict"
            # Freshness reference for the pre-rename re-check, captured
            # from the descriptor AFTER the bytes were read: the window
            # it guards is read-to-rename, so a touch landing before the
            # read (or a byte-identical rewrite, already covered by the
            # hash) cannot false-positive it.
            st_after = os.fstat(fd)
            src_state = (st_after.st_mtime_ns, st_after.st_size)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return "refused"

    # From here on the descriptor stays OPEN. It is the only handle proven to
    # point at the validated inode, and the no-dir-fd path below writes through
    # it rather than re-resolving the name.

    # ATOMIC REPLACE, never truncate-then-write. Truncating first means a write
    # that fails partway (ENOSPC, EIO, EDQUOT) leaves the user's file empty or
    # half-written with no way back. Stage the complete payload beside the target
    # and rename over it: the rename is atomic, so the file is either the old
    # bytes or all of the new ones.
    #
    # The staging + rename are DIRECTORY-FD RELATIVE. Doing them by name would
    # hand back the check-to-use window the O_NOFOLLOW open just closed -- the
    # parent could be swapped for a symlink between the checks above and the
    # rename. The directory fd is opened O_NOFOLLOW and re-verified, and both
    # halves of the rename resolve against it.
    parent, base = os.path.split(path)
    # A UNIQUE staging name, created with O_EXCL. A predictable sibling could
    # already exist as real user data, and O_CREAT|O_TRUNC would have destroyed
    # it and then renamed it away. O_EXCL also means we only ever clean up a file
    # this call created.
    tmp_name = f".{base}.kirocrew-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    # Directory-fd pinning is an ENHANCEMENT, not a precondition. Where the POSIX
    # APIs exist (Linux) the staging and rename resolve against an open handle on
    # the parent, so an ancestor swapped mid-save cannot redirect the write. Where
    # they do not (Windows), the same staged payload is renamed BY NAME instead --
    # which is exactly what every editor's atomic save does, and is what actually
    # protects the user's data: the file is either the old bytes or all of the new
    # ones, never a shredded half-write.
    #
    # Failing closed without the pinned variant would make the whole mirror-back
    # feature Linux-only in order to defend against someone renaming directories
    # inside your project during the milliseconds of a save, on your own machine,
    # to a file you explicitly asked us to link. Losing the feature on two
    # platforms is the larger harm.
    #
    # Use getattr for O_DIRECTORY: a bare os.O_DIRECTORY raises AttributeError,
    # which `except OSError` would NOT catch, surfacing as a 500.
    # NOTE: the capability probe names os.rename, not os.replace. CPython lists
    # only os.rename in supports_dir_fd even though os.replace accepts the same
    # arguments -- probing os.replace silently disables pinning on Linux.
    o_directory = getattr(os, "O_DIRECTORY", 0)
    use_dir_fd = bool(
        o_directory
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.rename in getattr(os, "supports_dir_fd", set())
    )

    # Extended attributes are read from the DESCRIPTOR, not the pathname, and read
    # HERE while it is still open. A by-name `listxattr(path)` re-resolves the
    # whole path, so an ancestor renamed mid-save makes the lookup fail while the
    # pinned rename below still succeeds -- installing a replacement stripped of
    # the owner's ACL. Everything else in this function is descriptor-pinned; this
    # was the one read that was not.
    #
    # A filesystem that does not support xattrs at all is NOT an error: there is
    # nothing on the source to lose. Any OTHER failure means we cannot know what
    # we would be dropping, so it refuses.
    #
    # `_should_carry_xattr` narrows this to the attributes an inode-replacing
    # write may reproduce, and it is applied HERE, at the read, so a
    # privilege-bearing `security.capability` or an integrity signature over the
    # OLD bytes (`security.ima`/`security.evm`) is never captured to be replayed
    # onto content the caller supplied. See its allowlist in atomic_write.py.
    src_xattrs: list[tuple[str, bytes]] = []
    if all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        try:
            for _attr in os.listxattr(fd):
                if not _should_carry_xattr(_attr):
                    continue
                src_xattrs.append((_attr, os.getxattr(fd, _attr)))
        except OSError as exc:
            if exc.errno not in _XATTR_UNSUPPORTED_ERRNOS:
                logger.warning(
                    "refusing source write to %r: could not read its extended attributes "
                    "(%s), so a replacement could silently drop access controls",
                    path,
                    exc,
                )
                try:
                    os.close(fd)
                except OSError:
                    pass
                return "refused"
            src_xattrs = []

    # POSIX: the descriptor's job is done -- the staged rename below is pinned by
    # the directory fd instead, and holding a second handle buys nothing.
    try:
        os.close(fd)
    except OSError:
        pass

    dfd = -1
    created = False
    try:
        if use_dir_fd:
            try:
                dfd = os.open(parent, os.O_RDONLY | o_directory | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                return "refused"
        if use_dir_fd and within_root is not None:
            dir_real = _fd_real_path(dfd)
            if dir_real is None:
                return "refused"  # cannot verify containment -> fail closed
            if not _opened_path_within_root(dir_real, within_root) or is_sensitive_path(dir_real):
                return "refused"
        elif within_root is not None:
            # No directory handle to interrogate, so the parent is verified by
            # its resolved path. Weaker than the pinned check (a swap between
            # this and the rename is not detectable) but it still refuses a
            # parent outside the authorised root or inside a sensitive location.
            dir_real = os.path.realpath(parent)
            root_real = os.path.realpath(within_root)
            try:
                contained = os.path.commonpath([dir_real, root_real]) == root_real
            except ValueError:
                contained = False
            if not contained or is_sensitive_path(dir_real):
                return "refused"
        # O_NOFOLLOW guards only the FINAL component, so opening the parent by
        # name leaves an INTERMEDIATE ancestor swappable between the file's
        # validation and this open: /project/a/c/doc with `a` replaced by a
        # symlink to /project/b yields a directory fd for a different `c`, and
        # the rename would overwrite /project/b/c/doc instead. Re-resolving
        # `base` through the pinned fd and requiring the SAME (dev, ino) closes
        # that: if any ancestor changed, this resolves to a different inode or
        # not at all.
        if use_dir_fd:
            try:
                dst = os.stat(base, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                return "refused"
            if (dst.st_dev, dst.st_ino) != src_ident:
                logger.warning(
                    "refusing source write to %r: the pinned parent no longer resolves to "
                    "the validated file",
                    path,
                )
                # "refused" in BOTH modes: this predicate is the ancestor-swap
                # security guard (see the comment above), and auditing a
                # hostile swap under the benign concurrent-save code would
                # blind the SEL trail. The two post-staging re-checks below
                # own the benign-concurrency vocabulary.
                return "refused"
            tfd = os.open(
                tmp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
                dir_fd=dfd,
            )
        else:
            tfd = os.open(
                os.path.join(parent, tmp_name),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
        created = True
        try:
            written = 0
            while written < len(encoded):
                written += os.write(tfd, encoded[written:])
            # Restore the target's permissions on the descriptor BEFORE the
            # rename, so the replacement is never briefly visible as 0600.
            # Via platform_compat: os.fchmod does not exist on Windows, and a
            # bare call would raise AttributeError -- which `except OSError`
            # would NOT catch, surfacing as a 500 mid-update.
            platform_compat.fchmod_safe(tfd, src_mode)
            # Carry extended attributes across the replace. The in-place write
            # this replaced preserved them for free by never changing the inode;
            # a fresh inode starts with none, which silently drops POSIX ACLs
            # (stored as system.posix_acl_access) and any user.* metadata.
            #
            # Split by what the attribute DOES, rather than one policy for all.
            # `src_xattrs` is already narrowed to the carriable allowlist at the
            # read above, so the split below is only over POSIX ACLs and `user.*`:
            #
            #  * an ACCESS-CONTROL attribute that fails to copy is a security
            #    regression -- the rename would install an inode the owner has
            #    protected less than the one it replaced -- so the write is
            #    REFUSED and the original is left untouched;
            #  * an informational `user.*` attribute is best effort, because
            #    failing closed there would make every linked write fail on a
            #    filesystem that simply does not support xattrs (tmpfs, several
            #    network mounts), which is worse than losing a tag.
            #
            # A source with NO xattrs needs nothing carried, so an unsupported
            # filesystem is not an error -- there is nothing to lose.
            #
            # Values were captured from the validated DESCRIPTOR above, so this
            # loop cannot be affected by a path that moved since.
            for attr, value in src_xattrs:
                try:
                    os.setxattr(tfd, attr, value)
                except OSError:
                    if _is_access_control_xattr(attr):
                        logger.warning(
                            "refusing source write to %r: could not carry access-control "
                            "attribute %r onto the replacement",
                            path,
                            attr,
                        )
                        return "refused"
                    continue  # informational attribute -- keep going
            os.fsync(tfd)
        finally:
            os.close(tfd)
        # LAST-MOMENT re-check, immediately before the rename.
        #
        # rename() replaces whatever the name points at RIGHT NOW, and the
        # earlier identity check ran before the payload was staged -- a write
        # plus fsync, which on a slow filesystem is a wide window. An editor
        # doing its own atomic save in that window swaps in a NEW inode, and the
        # rename would silently overwrite content newer than what the user is
        # editing here. Re-checking last shrinks the window from "duration of
        # the staged write" to the few instructions below, and a detected change
        # REFUSES rather than clobbers.
        #
        # This is a narrowing, not a guarantee: a genuine compare-and-swap
        # rename needs renameat2(RENAME_EXCHANGE), which the stdlib does not
        # expose (and which is Linux-only). The remaining window cannot be
        # closed with os.rename, so the caller keeps its own snapshot and the
        # user's newer file wins -- the safe direction.
        try:
            pre = (
                os.stat(base, dir_fd=dfd, follow_symlinks=False)
                if use_dir_fd
                else os.stat(path, follow_symlinks=False)
            )
        except OSError:
            return "refused"
        if (pre.st_dev, pre.st_ino) != src_ident:
            logger.warning(
                "refusing source write to %r: the file changed on disk after validation "
                "(concurrent save); not overwriting the newer content",
                path,
            )
            return "conflict" if base_hash is not None else "refused"
        if src_state is not None and (pre.st_mtime_ns, pre.st_size) != src_state:
            # Same inode, different content: an IN-PLACE write landed after the
            # descriptor-anchored verify. The identity check above cannot see
            # it, but a changed mtime or size can — a narrowing on filesystems
            # with coarse timestamps, never a widening, and the failure
            # direction is the safe one: the newer file wins.
            logger.warning(
                "refusing verified replace of %r: the file was rewritten in place "
                "after its base hash was verified (concurrent save)",
                path,
            )
            return "conflict"
        if use_dir_fd:
            os.rename(tmp_name, base, src_dir_fd=dfd, dst_dir_fd=dfd)
        else:
            # os.replace, not os.rename: on Windows rename REFUSES an existing
            # destination, while replace overwrites it -- and does so atomically,
            # which is the property that matters here.
            os.replace(os.path.join(parent, tmp_name), path)
        created = False  # renamed away; nothing left to clean up
        return "ok"
    except OSError:
        return "refused"
    finally:
        if created:
            try:
                if dfd >= 0:
                    os.unlink(tmp_name, dir_fd=dfd)
                else:
                    os.unlink(os.path.join(parent, tmp_name))
            except OSError:
                pass
        if dfd >= 0:
            try:
                os.close(dfd)
            except OSError:
                pass


def safe_write_file_nolink(
    raw: str,
    content: str,
    within_root: str | None = None,
) -> bool:
    """Overwrite an EXISTING regular file, pinned to the descriptor opened.

    The no-verification entry point of :func:`_pinned_replace`; see there for
    the full contract. Returns True when the bytes were written, False on any
    rejection.
    """
    return _pinned_replace(raw, content, within_root=within_root) == "ok"


def verified_replace_file_nolink(
    raw: str,
    content: str,
    base_hash: str,
    *,
    max_bytes: int,
    within_root: str | None = None,
) -> str:
    """Compare-and-swap replace: verify *base_hash*, then atomically install.

    One ``O_NOFOLLOW`` open anchors everything — validation, the sha256 of the
    current bytes, metadata capture, and (via the pinned directory descriptor)
    the staged rename — so verification and replacement share one name
    resolution instead of a by-name read followed by an independent by-name
    write. Concurrency changes detected at any point after verification
    (identity swap, or an in-place rewrite visible through mtime/size) answer
    ``"conflict"`` rather than overwriting: the newer file wins, never the
    stale edit. Returns ``"ok"``, ``"conflict"``, ``"too_large"`` (the file
    outgrew *max_bytes* since the base was read), or ``"refused"``.
    """
    # Deny-by-default (AUTOSDE backend-security-controls): a verifying
    # primitive that accepts an unverifiable base and proceeds anyway has the
    # wrong contract regardless of caller. An explicit check, not an assert —
    # asserts vanish under ``python -O``, and the falsy value would silently
    # SKIP verification rather than refuse (fail-open, the exact lost-update
    # class this primitive exists to close).
    if not isinstance(base_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", base_hash):
        return "refused"
    return _pinned_replace(
        raw, content, within_root=within_root, base_hash=base_hash, max_bytes=max_bytes
    )
