"""The guarded readers: path validation, the text and byte reads, the identity
samples, the no-hardlink bounded read, the prefix sniff and the private copy.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import errno
import os
import stat as _stat
import sys
import tempfile
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        MAX_FILE_BYTES,
        FileTooLargeError,
        _fd_real_path,
        _fold_extended_length_local,
        _hardlink_alias_matches,
        _is_representable_path,
        _opened_file_matches_validated_path,
        _opened_path_within_root,
        _screen_windows_links,
        is_sensitive_path,
        is_unc_shape,
        is_unverifiable_path_refusal,
        jsonl_util,
        platform_compat,
        sensitive_path_refusal,
        unc_probe_allowed,
    )


def validate_file_path(raw: str) -> str | None:
    """Validate and canonicalize a file path for dashboard file I/O.

    Enforces: representability in the OS path layer (BEFORE any syscall sees the
    string), the Windows UNC trusted-root gate (BEFORE any resolution --
    ``realpath`` on a UNC path is itself the outbound SMB probe), the Windows
    link-target screen (a link can launder the same probe past the
    lexical UNC check), is_sensitive_path(), realpath canonicalization.
    Returns the canonical path or None if rejected.
    """
    if not raw:
        return None
    if not _is_representable_path(raw):
        return None
    if os.name == "nt":
        # Fold a ``\\?\<drive>:\...`` extended-length LOCAL path down to its
        # plain ``<drive>:\...`` spelling BEFORE any gate below.
        # ``is_unc_shape`` correctly reports ``\\?\C:\...`` as non-UNC (it names
        # a local drive, not a share), so the UNC gate lets it through -- but
        # the sensitive-path fence at the tail compares the resolved path
        # against ``$HOME``-anchored credential leaves, and the ``\\?\``-prefixed
        # spelling matches none of them, so a dashboard read of
        # ``\\?\C:\Users\<user>\.aws\credentials`` would slip the fence.
        # Normalising here makes every downstream form the fence can see -- the
        # raw string, its ``normpath``, and ``realpath`` -- the ordinary
        # ``C:\Users\...`` path, so ``is_sensitive_path`` recognises the
        # credential leaf regardless of whether ``realpath`` happens to strip
        # the prefix (it does not for a non-existent target on every CPython).
        # Only a DRIVE-absolute remainder is folded: ``\\?\UNC\...`` and every
        # other extended namespace stay untouched and UNC-shaped so the gate
        # below refuses them fail-closed. Mirrors the readlink-target ``\\?\``
        # fold later in this function.
        raw = _fold_extended_length_local(raw)
    if os.name == "nt" and is_unc_shape(raw) and not unc_probe_allowed(raw):
        return None
    expanded = os.path.expanduser(raw)
    target = expanded
    if os.name == "nt":
        # Anchor a relative input lexically before the walk: the walk covers
        # only the components the path itself names, while `realpath` resolves
        # the CWD's own ancestors too. `abspath` performs no filesystem or
        # network I/O (GetFullPathNameW on Windows, a string normpath on
        # POSIX) -- but it also collapses `..` lexically, which changes what
        # `realpath` returns for a `..` that crosses a symlinked component, so
        # it is scoped to this branch: on POSIX the pre-change
        # resolve-through-every-symlink semantics stay byte-identical.
        target = os.path.abspath(expanded)
        # The ANCHORED form -- the exact string resolved and walked below --
        # is re-screened lexically: expansion (`~` on a roaming profile) or
        # anchoring (a CWD on a UNC share) can surface a UNC shape the raw
        # text did not have, and the ancestor walk is an lstat per component,
        # so on an untrusted UNC path the walk itself would be the probe.
        # `abspath` never strips UNC-ness, so this single screen covers the
        # expanded form too. Mirrors the both-forms screening in
        # dashboard/handlers/themes.py::_resolve_local_source.
        if is_unc_shape(target) and not unc_probe_allowed(target):
            return None
        screened = _screen_windows_links(target)
        if screened is None:
            return None
        target = screened
    # `realpath` consumes the SAME string the walk inspected -- resolving a
    # different form would traverse a chain the walk never saw.
    path = os.path.realpath(target)
    if is_sensitive_path(path):
        return None
    return path


def safe_read_file(path: str) -> str:
    """Read a file after enforcing ``is_sensitive_path``.

    Canonicalizes the path (following every symlink), re-checks the RESOLVED
    target against ``is_sensitive_path`` — so a symlink pointing into ``~/.aws``
    etc. is refused through the link — then re-opens the canonical path through
    :func:`kiro_crew.jsonl_util.open_regular_nofollow` as defense-in-depth
    against a TOCTOU swap of the final component into a link after the check.
    That opener carries the refusal on Windows too, where ``O_NOFOLLOW`` does not
    exist and a plain open would resolve a junction planted at the name.
    Opening the
    already-resolved canonical path never rejects a legitimate file (its final
    component is not a symlink by construction), so this only closes the race.

    The read is bounded by ``MAX_FILE_BYTES``, the same ceiling
    :func:`safe_read_file_bytes` applies, so a path an agent can write cannot
    drive an unbounded allocation here. The ceiling is charged against every
    read rather than checked once, so a file that GROWS after the open is
    refused mid-read instead of being materialised.

    The descriptor is authorized through the opener's ``authorize`` hook rather
    than after it yields, so a descriptor this function rejects is closed having
    never been wrapped in a content reader.

    Raises ``PermissionError`` if the path is sensitive, a symlink race is
    detected, or the opened node is not the regular file that was validated.
    Other read errors (missing file, permission denied, ``EFBIG`` past the
    ceiling) propagate as ``OSError`` so callers surface accurate messages — and
    so the many callers that already wrap this in ``except OSError`` degrade
    rather than crash.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    refusal = sensitive_path_refusal(resolved)
    if refusal:
        # {resolved!r}, not {resolved}: the resolved target is caller/attacker
        # influenced (a symlink target is chosen by whoever wrote the link) and
        # this text reaches log records via ``exc_info`` — a raw newline in it
        # would forge a second record. The unverifiable wording is recognised by
        # its fixed prefix, which no path spelling can produce, and already
        # quotes the path; anything else is re-spelled here with the repr.
        if not is_unverifiable_path_refusal(refusal):
            refusal = f"Blocked: access to sensitive path: {resolved!r}"
        raise PermissionError(refusal)

    def authorize(fd: int) -> None:
        if not _opened_file_matches_validated_path(fd, resolved):
            raise PermissionError(f"Blocked: opened file no longer matches safe path: {resolved!r}")

    try:
        with jsonl_util.open_regular_nofollow(
            resolved, max_bytes=MAX_FILE_BYTES, authorize=authorize
        ) as handle:
            data = handle.read()
    except PermissionError:
        raise
    except OSError as exc:
        # ELOOP on the canonical (symlink-free) path means a concurrent TOCTOU
        # swap of the final component into a symlink — refuse it.
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise PermissionError(f"Blocked: refusing to follow symlink at {resolved!r}") from exc
        # EINVAL is shared, so only the opener's own refusal may be remapped this way.
        if exc.errno == errno.EINVAL and exc.strerror == jsonl_util.NOT_REGULAR_FILE:
            raise PermissionError(
                f"Blocked: opened file no longer matches safe path: {resolved!r}"
            ) from exc
        raise
    # Text mode collapsed these on the way out; decoding bytes does not.
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def safe_read_file_bytes(raw: str) -> bytes | None:
    """Read file bytes through centralized is_sensitive_path() enforcement.

    ``validate_file_path`` already canonicalizes via ``realpath`` (following
    symlinks) and rejects sensitive resolved targets, so a workspace symlink
    into ``~/.aws`` etc. is refused before any read.  The final open goes through
    :func:`kiro_crew.platform_compat.open_file_no_reparse` as defense-in-depth
    against a TOCTOU swap of the final component into a link after the check —
    a refusal that holds on Windows as well, where ``O_NOFOLLOW`` does not exist.

    Before reading, the opened descriptor must be a regular file whose kernel
    path still matches the canonical name validated above and is not sensitive.
    This also refuses an ancestor-directory swap. Comparison is lexical except
    for a macOS case-only mismatch, which requires a no-follow walk back to the
    held inode. Resolving the original name again could authorize a swap.

    Returns file content as bytes, or None if path is rejected or unreadable.
    """
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return None
    try:
        if not _opened_file_matches_validated_path(fd, path):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise FileTooLargeError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB safety cap")
        return data
    except OSError:
        return None
    finally:
        os.close(fd)


def safe_file_identity(raw: str) -> tuple[int, int, int, int] | None:
    """``(st_dev, st_ino, st_mtime_ns, st_size)`` of a file, through the same gate as a read.

    Every call screens the path afresh with :func:`validate_file_path` -- no
    admission is remembered between calls, because a link swapped in after one
    screen must not be followed by the next sample -- and takes the identity
    from a descriptor opened by
    :func:`kiro_crew.platform_compat.open_file_no_reparse`, never from a
    name-based ``stat`` that would follow a junction to an attacker-chosen UNC
    target. The descriptor must still be the regular file the screen
    validated, exactly as :func:`safe_read_file_bytes` requires before it reads.

    Returns ``None`` when the file does not exist. Raises ``PermissionError``
    when the path is refused or the opened descriptor does not match it, and
    ``OSError`` for any other failure to open.
    """
    path = validate_file_path(raw)
    if path is None:
        raise PermissionError("Blocked: file path was refused")
    try:
        # lstat never follows the final component, so absence is known before
        # anything is opened -- the same probe order safe reads use.
        os.lstat(path)
    except FileNotFoundError:
        return None
    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except FileNotFoundError:
        return None
    try:
        if not _opened_file_matches_validated_path(fd, path):
            raise PermissionError("Blocked: opened file no longer matches safe path")
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    finally:
        os.close(fd)


def safe_read_file_bytes_with_identity(
    raw: str, allowed_identities: set[tuple[int, int]]
) -> bytes | None:
    """Read file bytes, authorizing the OPENED descriptor by inode identity.

    Like :func:`safe_read_file_bytes`, but closes the authorize-then-read TOCTOU
    window for callers that keep a filesystem allowlist. The file is opened ONCE
    through :func:`kiro_crew.platform_compat.open_file_no_reparse`, which refuses a
    link at the final component on every platform, and the ``fstat`` identity
    ``(st_dev, st_ino)`` of that
    very descriptor MUST be in ``allowed_identities`` before any bytes are
    returned. Because authorization and read share one descriptor, a symlink- or
    directory-swap slipped in between ``realpath`` and ``open`` cannot substitute
    an unauthorized file — its inode is not in the allowlist. ``validate_file_path``
    still rejects sensitive resolved targets (``~/.aws`` …) up front, so
    all filesystem reads stay funnelled through this centralized chokepoint.

    Returns bytes on success. Raises :class:`PermissionError` when the opened
    inode is not allowlisted or a final-component link swap is detected
    (reported as ``ELOOP``), and :class:`FileTooLargeError` when the file
    exceeds ``MAX_FILE_BYTES``. Returns ``None`` when the path is rejected by
    :func:`validate_file_path` or is otherwise unreadable.
    """
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise PermissionError(f"Blocked: refusing to follow symlink at {path!r}") from exc
        return None
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) not in allowed_identities:
            raise PermissionError("Blocked: file is not in the authorized set")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise FileTooLargeError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB safety cap")
        return data
    finally:
        os.close(fd)


def stat_identity(raw: str) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)`` of a file through the sensitive-path gate.

    Metadata-only companion to :func:`safe_read_file_bytes_with_identity` for
    callers that must build an inode allowlist from LLM-influenced paths without
    reading content. ``validate_file_path`` canonicalizes via ``realpath`` and
    rejects sensitive resolved targets, so a path that resolves into ``~/.aws``
    etc. is refused (returns ``None``) rather than ``stat``'d — keeping all
    LLM-path filesystem access funnelled through this centralized chokepoint.

    Returns ``(dev, ino)`` or ``None`` if the path is rejected or unstattable.
    """
    path = validate_file_path(raw)
    if path is None:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def safe_read_file_bytes_nolink(
    raw: str,
    within_root: str | None = None,
    *,
    max_bytes: int | None = None,
    allow_truncate: bool = False,
    within_root_is_canonical: bool = False,
    admit_hardlinked: Callable[[str, bytes], bool] | None = None,
) -> bytes | None:
    """Like :func:`safe_read_file_bytes` but also rejects hardlinked inodes.

    Staging must pin its hardlink check to the SAME inode it reads.
    A caller that lstat()s the path and then opens it by name leaves a race
    window where the file is swapped for a hardlink to a sensitive file
    (e.g. ``~/.aws/config``) between the check and the open. Here the open
    happens first, refusing a link at the final component, then ``fstat()`` on
    the descriptor —
    the inode that is validated is exactly the inode that is read:
    ``st_nlink > 1`` or a non-regular file type is rejected.

    When ``within_root`` is given, the OPENED descriptor's real path
    (via ``/proc/self/fd`` on Linux, ``fcntl.F_GETPATH`` on macOS, or
    ``GetFinalPathNameByHandleW`` on Windows) must resolve inside that root and
    must not be sensitive. Refusing the link only guards the
    FINAL path component — a nested directory swapped for a symlink between
    the tree walk and the open would silently escape the approved tree. The
    fd-path check is pinned to the inode actually opened, so no check-to-use
    window remains. If the fd's real path cannot be determined, fail closed.
    ``within_root_is_canonical`` preserves a caller's already-resolved admission
    root literally, so replacing that directory with a link cannot redefine it.

    ``admit_hardlinked`` is the one opt-in exception to the hardlink refusal, and
    it is decided on CONTENT, never on the link count alone. A hardlinked inode
    still passes every other check here (regular file, opened-path identity,
    containment, sensitive path, size cap); then the callback receives the
    validated path and the exact bytes read from the descriptor, and only a
    ``True`` answer returns them. Without it, ``st_nlink > 1`` is refused as
    before.

    That final-component refusal comes from
    :func:`kiro_crew.platform_compat.open_file_no_reparse`, not from an
    ``O_NOFOLLOW`` flag, because the flag does not exist on Windows:
    ``getattr(os, "O_NOFOLLOW", 0)`` is ``0`` there, so a plain ``os.open``
    resolves a junction at the name and this chokepoint would hold one fewer
    guarantee on one platform than the paragraph above claims. ``CreateFileW``
    with ``FILE_FLAG_OPEN_REPARSE_POINT`` opens the reparse point AS ITSELF and
    the helper reports ``ELOOP``, which is what POSIX reports for the same
    shape, so the open is one operation with one contract everywhere.

    Returns file content as bytes, or None if the path is rejected,
    hardlinked, non-regular, escaping ``within_root``, or unreadable.
    """
    # Callers that pass an explicit limit own the higher-level bound (for
    # example, the importer's trusted 64 MiB SQLite snapshot cap). Keep the
    # default cap for general reads, but do not silently narrow a documented
    # caller-specific limit back to 50 MiB.
    read_limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
    if read_limit < 0:
        raise ValueError("max_bytes must be non-negative")
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        hardlinked = st.st_nlink > 1
        if (hardlinked and admit_hardlinked is None) or not _stat.S_ISREG(st.st_mode):
            return None
        if not _opened_file_matches_validated_path(fd, path):
            return None
        if within_root is not None:
            fd_real = _fd_real_path(fd)
            if fd_real is None:
                return None  # cannot verify containment -> fail closed
            if hardlinked and _hardlink_alias_matches(fd, path, fd_real):
                # The kernel named a sibling link (outside the root, for an
                # installed file linked into the skills tree); the witness walk
                # proved *path* holds this inode, so containment is judged on it.
                # A sibling under a sensitive path still refuses the read.
                if is_sensitive_path(fd_real):
                    return None
                fd_real = path
            if not _opened_path_within_root(
                fd_real, within_root, root_is_canonical=within_root_is_canonical
            ):
                return None  # opened inode escapes the approved tree
            if is_sensitive_path(fd_real):
                return None
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(read_limit + 1)
        fd = -1  # consumed by fdopen
        if len(data) > read_limit:
            # ``allow_truncate`` is for callers whose contract is "show as much
            # as fits" rather than "refuse oversize" -- the artifact store
            # displays a truncated view of a large linked file. The memory bound
            # is unaffected: at most ``read_limit + 1`` bytes were ever read.
            # An admitted hardlink is judged on its WHOLE content, so it is
            # never handed back as a prefix the admission did not see.
            if allow_truncate and not hardlinked:
                return data[:read_limit]
            raise FileTooLargeError(f"File exceeds {read_limit // (1024 * 1024)} MB safety cap")
        if hardlinked and not (admit_hardlinked is not None and admit_hardlinked(path, data)):
            return None
        return data
    except OSError:
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def safe_read_prefix(raw: str, n: int) -> bytes | None:
    """Read the first *n* bytes of a file through is_sensitive_path enforcement.

    Like :func:`safe_read_file_bytes` but reads only a bounded prefix, for
    magic-byte / format sniffing of large binaries that exceed
    ``MAX_FILE_BYTES`` (e.g. the ~100 MB kiro-cli binary). ``validate_file_path``
    canonicalizes via ``realpath`` (following symlinks) and rejects sensitive
    resolved targets, so a symlink pointing into ``~/.aws`` etc. is refused
    before any read. The open goes through
    :func:`kiro_crew.platform_compat.open_file_no_reparse` as TOCTOU defense
    against a final-component link swap after the check — a refusal that holds on
    Windows too, where ``O_NOFOLLOW`` does not exist.

    Returns up to *n* bytes, or None if the path is rejected or unreadable.
    """
    if n <= 0:
        return b""
    path = validate_file_path(raw)
    if path is None:
        return None
    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return None
    try:
        if not _opened_file_matches_validated_path(fd, path):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(n)
    except OSError:
        return None
    finally:
        os.close(fd)


def safe_read_range(raw: str, offset: int, n: int) -> bytes | None:
    """Read *n* bytes starting at *offset* through :func:`safe_read_prefix`'s guards.

    For a caller that probes a large file at a few offsets (a binary search of a
    pack index) and so cannot read a prefix. ``validate_file_path``
    canonicalizes via ``realpath`` (following symlinks) and rejects sensitive
    resolved targets, so a symlink pointing into ``~/.aws`` etc. is refused
    before any read. The open goes through
    :func:`kiro_crew.platform_compat.open_file_no_reparse` as TOCTOU defense
    against a final-component link swap after the check — a refusal that holds on
    Windows too, where ``O_NOFOLLOW`` does not exist. A short read means the file
    ends before ``offset + n``; the caller decides what that means.

    Returns the bytes read, or None if the path is rejected or unreadable.
    """
    if n <= 0:
        return b""
    if offset < 0:
        return None
    if offset > sys.maxsize:
        return None
    path = validate_file_path(raw)
    if path is None:
        return None
    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return None
    try:
        if not _opened_file_matches_validated_path(fd, path):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            fh.seek(offset)
            return fh.read(n)
    except (OSError, ValueError, OverflowError):
        return None
    finally:
        os.close(fd)


def safe_copy_file_nolink(raw: str, dest_dir: str) -> str | None:
    """Copy a file into *dest_dir* with the full descriptor-pinned validation
    chain; return the private copy's path, or None if the source is rejected.

    For large binaries (media files) that libraries must consume BY PATH from
    a subprocess: the bytes are streamed from the vetted descriptor into a
    freshly created 0600 temp file inside *dest_dir*, so downstream readers
    never touch the caller-influenced original path again.

    Validation mirrors :func:`safe_read_file_bytes_nolink`: open first, refusing a
    link at the final component, then ``fstat()`` on the descriptor (regular file,
    ``st_nlink == 1``), then the OPENED descriptor's real path (via
    ``/proc/self/fd`` on Linux, ``fcntl.F_GETPATH`` on macOS,
    ``GetFinalPathNameByHandleW`` on Windows) must not be sensitive. Refusing the
    link only guards the FINAL path component — an ancestor directory swapped for a
    symlink between validation and open would otherwise reach a sensitive file. The
    fd-path check is pinned to the inode actually opened and copied, so no
    check-to-use window remains. If the fd's real path cannot be determined, fail
    closed.

    The open goes through :func:`kiro_crew.platform_compat.open_file_no_reparse`
    because this function copies BYTES with a raw ``os.read``, and both halves of
    that matter on Windows: ``O_NOFOLLOW`` does not exist there, so a plain
    ``os.open`` follows a reparse point at the name AND hands back a CRT descriptor
    in text mode, which truncates a binary payload at its first ``0x1A``. Media
    files — what this function exists for — are exactly the payloads that carry one.
    """
    path = validate_file_path(raw)
    if path is None:
        return None

    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return None
    tmp_fd = -1
    tmp_path: str | None = None
    try:
        st = os.fstat(fd)
        if st.st_nlink > 1 or not _stat.S_ISREG(st.st_mode):
            return None
        fd_real = _fd_real_path(fd)
        if fd_real is None:
            return None  # cannot verify what was opened -> fail closed
        if is_sensitive_path(fd_real):
            return None
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".safe-copy-", suffix=os.path.splitext(fd_real)[1], dir=dest_dir
        )
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(tmp_fd, view)
                view = view[written:]
        return tmp_path
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if tmp_fd >= 0:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
