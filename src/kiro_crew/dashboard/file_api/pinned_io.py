"""Descriptor-checked file access: the no-follow open, the checked open the readers share, the text read, the project-relative resolve and the pinned write."""

from __future__ import annotations

import contextlib
import errno
import ntpath
import os
import stat as _stat_mod
from pathlib import PurePath
from typing import TYPE_CHECKING, BinaryIO, NamedTuple

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _FILE_READ_BINARY_EXTS,
        _FILE_READ_SNIFF_BYTES,
        _probe_request_path,
        _sel,
        atomic_write,
        is_sensitive_path,
        logger,
        open_access_control_source,
        pinned_fs,
        pinned_parent_replace_supported,
    )


def _open_rb_nofollow(path: str) -> int:
    """Open *path* read-only in binary, refusing symlinks, on every platform.

    POSIX gets the atomic form: ``O_NOFOLLOW`` makes the kernel itself fail
    the open with ``ELOOP`` when the final component is a symlink, so there is
    no check-then-open race. Windows has no ``O_NOFOLLOW`` (referencing it
    raises AttributeError, turning every read into an HTTP 500), so there the
    guard is a pre-open ``lstat``: reject symlinks and any reparse point
    (junctions included) with the same ``ELOOP`` errno the POSIX branch
    produces, keeping callers' error handling identical. The window between
    lstat and open is acceptable defence-in-depth there -- path containment
    was already enforced by the caller's validation, and creating a symlink
    on Windows requires elevated or developer-mode privileges. ``O_BINARY``
    keeps the CRT from text-mode translating file bytes on Windows; it is 0
    elsewhere.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        st = os.lstat(path)
        if _stat_mod.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) != 0:
            raise OSError(errno.ELOOP, "symlinks not allowed", path)
    return os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_BINARY", 0))


class _TextRead(NamedTuple):
    """The outcome of one :func:`_read_request_path` transaction.

    ``kind`` is the verdict the endpoint maps onto its status and audit outcome:
    ``invalid`` (validation refused), ``dir`` / ``missing`` (nothing to read),
    ``file`` (``content`` is the capped text) or ``read_failed``. ``path`` is the
    validated path, or ``""`` for ``invalid`` -- the raw input is the caller's to
    log, as before. ``lossy`` says the UTF-8 decode had to substitute
    replacement characters, so ``content`` is not the file as written.
    """

    kind: str
    path: str
    content: str
    lossy: bool = False


def _read_request_path(raw: str, read_cap: int) -> _TextRead:
    """Validate, no-follow open and read a request path in ONE transaction.

    Blocking; callers run it on a worker thread. It exists because validating in
    one hop and opening in another is a symlink TOCTOU: between the two, the
    validated name can be replaced with a link into a location the validator
    would have refused, and a bare ``open`` then follows it. Whether the hops are
    two ``await``s or two statements, only ONE transaction closes that window.

    The transaction is :func:`_open_checked_file` -- the file API's own
    open-and-check prefix, shared with file-raw, file-download, file-stream and
    file-sheet -- not a copy of it. That is the point: a later hardening fix to
    the prefix reaches this endpoint too, which a hand-rolled second copy would
    silently miss. Its ``is_sensitive_path`` rung is a re-check rather than a new
    gate here, because ``validate_file_path`` already applies that predicate; its
    ``except ValueError`` fold (an embedded NUL) is the prefix's decision for
    every adopter, and this endpoint now inherits it instead of answering that
    input differently from its four siblings.

    What stays endpoint POLICY, per the prefix's own contract: the ``isdir``
    probe, because a READ distinguishes a directory from a missing path in its
    404 (it runs inside the transaction for the same reason the open does), and
    the bounded byte snapshot used for both the binary verdict and text decode.
    One snapshot prevents an in-place rewrite between two reads from pairing a
    text verdict with binary bytes. The snapshot is ``read_cap * 4`` bytes (a
    UTF-8 character is at most four bytes), decoded whole and sliced to
    ``read_cap`` CHARACTERS, so multi-byte content does not mis-set
    ``X-Truncated`` and a character split at the byte bound can only fall
    beyond the slice. A file that itself ends mid-codepoint decodes to a
    trailing U+FFFD, exactly as it did before this change.

    Pass ``read_cap`` 0 for the verdict only: HEAD answers from the stat and must
    open nothing.
    """
    if read_cap <= 0:
        probe = _probe_request_path(raw)
        if not probe.path:
            return _TextRead("invalid", "", "")
        if probe.is_file:
            return _TextRead("file", probe.path, "")
        return _TextRead("dir" if probe.is_dir else "missing", probe.path, "")
    # log_open_failure=False: this endpoint's own handler logs the one traceback
    # for a failed read, so the prefix must not write a second.
    checked = _open_checked_file(raw, tool_name="file_read", log_open_failure=False)
    if isinstance(checked, _OpenDenied):
        if checked.code == "not_found":
            # The prefix answers "not a regular file"; which kind it is belongs
            # to this endpoint, and the probe stays inside the transaction.
            return _TextRead("dir" if os.path.isdir(checked.path) else "missing", checked.path, "")
        if checked.code in ("invalid_path", "sensitive_path"):
            return _TextRead("invalid", "", "")
        # symlink_refused (the final component became a link inside this
        # transaction), read_failed, file_too_large: the read did not happen.
        return _TextRead("read_failed", checked.path, "")
    # Binary BY FORMAT, decided before any byte is decoded: a NUL-free binary
    # (a GNU thin `.a` holds only ASCII member references) would otherwise read
    # as text and open an editable buffer that a save turns into corruption.
    if PurePath(checked.path).suffix.lower() in _FILE_READ_BINARY_EXTS:
        with contextlib.suppress(Exception):
            checked.file.close()
        return _TextRead("binary", checked.path, "")
    try:
        # Read one bounded byte snapshot for both the binary verdict and the
        # content. Two descriptor reads would let an in-place rewrite pair a
        # text verdict from the sniff with binary bytes from the later decode.
        # The decode is deliberately lossy (``errors="replace"``), which is
        # right for a text file with one bad byte and actively wrong for a .zip
        # or a .sqlite. The sniff is what catches a binary with NO extension, so
        # it runs even though the check above already answered every known one.
        with contextlib.closing(checked.file):
            data = checked.file.read(read_cap * 4)
        if b"\x00" in data[:_FILE_READ_SNIFF_BYTES]:
            return _TextRead("binary", checked.path, "")
        try:
            return _TextRead("file", checked.path, data.decode("utf-8")[:read_cap])
        except UnicodeDecodeError:
            # A text file the UTF-8 decode cannot render faithfully -- Latin-1,
            # one stray byte, a codepoint split at the snapshot bound. The
            # replacement characters make this body NOT the file as written,
            # and the viewer must know before it offers the body as a copy.
            return _TextRead(
                "file", checked.path, data.decode("utf-8", errors="replace")[:read_cap], True
            )
    except OSError:
        with contextlib.suppress(Exception):
            checked.file.close()
        return _TextRead("read_failed", checked.path, "")


class _OpenDenied(NamedTuple):
    """A refusal from :func:`_open_checked_file`: why, and the path to log.

    ``code`` uses the machine vocabulary the streaming endpoint already
    exposes (``invalid_path`` / ``sensitive_path`` / ``not_found`` /
    ``symlink_refused`` / ``file_too_large`` / ``read_failed``); each adopter
    maps it onto its own SEL outcome and response body, which is where the
    endpoints legitimately differ. ``path`` is the raw input for
    ``invalid_path`` (validation produced nothing) and the validated path
    otherwise -- exactly what each adopter logs today.
    """

    code: str
    path: str


class _CheckedFile(NamedTuple):
    """A successful :func:`_open_checked_file`: the checked open file.

    ``size`` is the fstat size of THIS fd -- authoritative for a streaming
    caller that must announce a length, advisory for whole-read callers
    whose bounded read is their own size guard.
    """

    path: str
    file: BinaryIO
    size: int


def _open_checked_file(
    raw_path: str,
    *,
    tool_name: str,
    fstat_cap: int | None = None,
    log_open_failure: bool = True,
) -> _CheckedFile | _OpenDenied:
    """The open-and-check half of the file-serving security prefix.

    validate -> sensitive-path check -> is-file -> ``_open_rb_nofollow`` ->
    fstat (cap enforced only when *fstat_cap* is passed), then RETURNS the
    checked open file object. What happens to the bytes afterwards is
    per-endpoint POLICY and stays with the caller: the whole-read envelope
    (:func:`_open_checked`) reads and closes it, the streaming endpoint
    sniffs and serves ranges from it, the sheet endpoint hands it to the
    workbook parser. The split exists because the streaming and sheet
    endpoints must keep the open file object, so they cannot use the
    whole-read envelope -- sharing the prefix keeps ONE copy of this
    boundary for every endpoint.

    The sensitive-path gate runs through the handlers module's import-time
    ``is_sensitive_path`` alias -- ONE binding for one guard, so a test
    override (or a future hardening change) applied to
    ``files.is_sensitive_path`` is observed by every adopter instead of
    landing on whichever binding an endpoint happened to import.

    *fstat_cap* is the streaming endpoint's size policy: its fd stays open
    for range reads, so the announced size must be authoritative up front.
    Whole-read adopters pass no cap here -- an fstat pre-check races a
    concurrent writer (the file can grow between the stat and the read),
    while their bounded read caps memory unconditionally.

    *log_open_failure* keeps log volume a per-endpoint decision: a
    caller-reachable open failure (mode-000 file, EACCES on a parent) writes
    a full traceback per request when True. The whole-read envelope wants
    that traceback (its 500 is the only signal); the streaming and sheet
    endpoints answer a coded refusal instead and pass False, so a request
    loop against a known-unreadable path cannot amplify into the log.

    Synchronous by design -- callers run it on a worker thread. Refusals are
    returned as typed codes, not responses: SEL logging and the HTTP body
    vocabulary belong to each endpoint.
    """
    import kiro_crew.dashboard.handlers as _h  # noqa: F811  # circular import

    try:
        path = _h._validate_dashboard_path(raw_path)
    except ValueError:
        # A malformed path (an embedded NUL makes realpath raise) is an
        # invalid path, not a crash.
        path = None
    if not path:
        return _OpenDenied("invalid_path", raw_path)
    if is_sensitive_path(path):
        return _OpenDenied("sensitive_path", path)
    if not os.path.isfile(path):
        return _OpenDenied("not_found", path)
    # Symlinks rejected atomically (O_NOFOLLOW on POSIX; lstat guard +
    # O_BINARY on Windows -- see _open_rb_nofollow).
    try:
        fd = _open_rb_nofollow(path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # symlink with O_NOFOLLOW
            return _OpenDenied("symlink_refused", path)
        if log_open_failure:
            # The only traceback for a failed open: adopters map the code
            # onto an outcome, and SEL records outcome, not cause.
            logger.exception("%s open failed for %s", tool_name, path)
        return _OpenDenied("read_failed", path)
    fobj = os.fdopen(fd, "rb")
    try:
        # fstat is authoritative for THIS fd; a file that grows afterwards
        # only extends past the size announced here, never past the cap.
        size = os.fstat(fobj.fileno()).st_size
    except OSError:
        with contextlib.suppress(Exception):
            fobj.close()
        if log_open_failure:
            logger.exception("%s fstat failed for %s", tool_name, path)
        return _OpenDenied("read_failed", path)
    if fstat_cap is not None and size > fstat_cap:
        fobj.close()
        return _OpenDenied("file_too_large", path)
    return _CheckedFile(path=path, file=fobj, size=size)


class _OpenRefusal(NamedTuple):
    """A refusal from :func:`_open_checked`: the response to return, already audited."""

    response: web.Response


class _OpenedFile(NamedTuple):
    """A successful :func:`_open_checked`: the validated path and full bytes."""

    path: str
    data: bytes


def _open_checked(
    raw_path: str,
    *,
    tool_name: str,
    max_bytes: int,
) -> _OpenedFile | _OpenRefusal:
    """The dashboard file endpoints' shared WHOLE-READ envelope.

    The open-and-check half lives in :func:`_open_checked_file` (the prefix
    shared with the streaming and sheet endpoints); this layer is the
    whole-read policy on top: bounded read (cap enforced on the bytes
    actually read, so a concurrent writer cannot outgrow it), close, and the
    mapping of every refusal onto this envelope's SEL vocabulary and
    response bodies.

    This is a SECURITY boundary: hand-rolled copies of one mean a future
    hardening fix — a new TOCTOU guard, a tightened sniff, a cap change —
    lands in some and silently leaves the others on the old posture (the
    same shape the zip-vetting surfaces guard against).

    Per-endpoint POLICY stays with the endpoint and is passed in rather than
    copied: which cap applies, and what the endpoint does with the bytes
    afterwards (``data`` is the full file — a caller sniffing magic slices
    its own header). Only the envelope is shared.

    Returns the opened result, or a refusal carrying the response to return —
    a typed either, so a caller cannot accidentally use the data on a refusal
    path the way an ``(data, error)`` tuple invites.

    Synchronous by design — callers offload it via ``asyncio.to_thread``:
    everything here is blocking file I/O and must not run on the event loop.
    SEL audit writes are thread-safe (locked appends).
    """

    def _log(outcome: str, res: str, error: str = "") -> None:
        kw = {"error": error} if error else {}
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name=tool_name,
            outcome=outcome,
            resources=res,
            **kw,
        )

    checked = _open_checked_file(raw_path, tool_name=tool_name)
    if isinstance(checked, _OpenDenied):
        code, res = checked.code, checked.path
        if code == "invalid_path":
            _log("denied", res)
            return _OpenRefusal(
                web.json_response({"error": "invalid or forbidden path"}, status=400)
            )
        if code == "sensitive_path":
            _log("denied", res, "sensitive_path")
            return _OpenRefusal(web.json_response({"error": "sensitive path blocked"}, status=403))
        if code == "not_found":
            _log("not_found", res)
            return _OpenRefusal(web.json_response({"error": "not found"}, status=404))
        if code == "symlink_refused":
            _log("denied", res, "symlink_rejected")
            return _OpenRefusal(web.json_response({"error": "symlinks not allowed"}, status=403))
        if code == "file_too_large":
            # Reachable only through a caller that passes fstat_cap; mapped so
            # a policy refusal can never masquerade as the 500 below.
            _log("denied", res, "file_too_large")
            return _OpenRefusal(
                web.json_response({"error": "file too large", "code": "file_too_large"}, status=413)
            )
        # read_failed: the residual code. (This envelope's own size guard is
        # the bounded read below, because an fstat pre-check races a
        # concurrent writer while reading at most cap+1 bytes bounds memory
        # unconditionally -- the same shape as _load_sheet_payload's guard.)
        _log("failure", res)
        return _OpenRefusal(
            web.json_response({"error": "cannot read file", "code": "read_failed"}, status=500)
        )

    path = checked.path
    try:
        with checked.file as f:
            data = f.read(max_bytes + 1)
    except OSError:
        # Keep the traceback for a failed read: this 500 is the only signal,
        # and SEL records outcome, not cause.
        logger.exception("%s read failed for %s", tool_name, path)
        _log("failure", path)
        return _OpenRefusal(web.json_response({"error": "cannot read file"}, status=500))
    if len(data) > max_bytes:
        _log("denied", path, "file_too_large")
        return _OpenRefusal(web.json_response({"error": "file too large"}, status=413))

    return _OpenedFile(path=path, data=data)


def _resolve_project_relative(raw: str) -> tuple[str, str | None]:
    """Resolve a relative path against KIROCREW_PROJECT_DIR (resolve=1).

    Returns (path, None) on success -- absolute and ~-paths pass through
    unchanged -- or ("", error_code) with "cannot_resolve" (no project dir
    configured) or "outside_project" (the joined path escapes the project
    directory after realpath).
    """
    if not raw or raw.startswith(("/", "~")):
        return raw, None
    # Windows-absolute shapes (UNC \\server\share, drive C:\...) are not
    # project-relative: pass them to the validator unchanged. Joining them
    # would let os.path.realpath contact the named host (SMB round-trip)
    # before any validation runs; the validator's own network-path gate
    # sits BEFORE its realpath, so it is the safe place for these.
    if raw.startswith("\\") or ntpath.splitdrive(raw)[0]:
        return raw, None
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return "", "cannot_resolve"
    candidate = os.path.realpath(os.path.join(proj, raw))
    resolved_proj = os.path.realpath(proj)
    if not (candidate == resolved_proj or candidate.startswith(resolved_proj + os.sep)):
        return "", "outside_project"
    return candidate, None


def _file_write_blocking(path: str, content: str) -> str | None:
    """Replace *path*'s contents atomically, carrying its access controls.

    Returns ``None`` on success or ``"notfound"`` when the target was rejected;
    any other failure propagates for the caller to log.

    Split out of :func:`api_file_write` so the whole transaction runs OFF the
    event loop. Every call in here is a blocking filesystem call, and on a
    network-backed path (an SMB share, a stalled FUSE mount) each one can take
    seconds, which on the loop thread freezes chat and the heartbeat alongside
    it. Being on a worker thread also re-arms the Windows rename retry inside
    ``atomic_write``, which deliberately degrades to a single attempt when it
    finds a running loop in its own thread.

    Routing through ``open_access_control_source`` rather than a bare ``os.open``
    is what keeps this working on Windows: it returns ``None`` where the xattr
    syscalls do not exist, and a read handle held open across the write would
    make ``os.replace`` fail with ``PermissionError`` on every save there.

    ``path`` is already canonicalized by ``_validate_dashboard_path``
    (``realpath``), so its final component is symlink-free and the helper's
    ``O_NOFOLLOW`` rejects nothing legitimate -- it closes the window where that
    component is swapped for a link after the check. That refusal is a rejected
    target rather than a server fault, hence ``"notfound"`` and not an exception.
    """
    # Pin the parent chain FIRST, then address the leaf only through that
    # descriptor. The pin is what stops atomic_write's temp create and publishing
    # rename from re-resolving the parent by name, and the ORDER is what stops the
    # metadata read below from re-resolving it either: a directory replaced at
    # that name between the pin and the leaf open would otherwise supply the mode
    # and ACL while the write published into the pinned original.
    #
    # pin_parent, NOT open_dir_pinned: ``path`` is already realpath-canonicalized,
    # so every component of its parent was a real directory at validation time.
    # pin_parent walks THAT recorded chain with O_NOFOLLOW per component, so a
    # component swapped for a link since is REFUSED. open_dir_pinned would
    # realpath the chain again here and follow the swap instead -- a fresh
    # resolution cannot be more faithful than the one already done, only less.
    #
    # None on a platform that cannot walk a parent by descriptor or cannot stage
    # and rename through one, where atomic_write keeps the by-name floor. Both
    # probes are asked because they are two capabilities: atomic_write refuses a
    # descriptor it cannot use rather than silently writing by name.
    dir_fd: int | None = None
    if pinned_fs.supports_pinned_walk() and pinned_parent_replace_supported():
        try:
            dir_fd = pinned_fs.pin_parent(os.path.dirname(path), what="file directory")
        except (pinned_fs.PinnedPathRefusal, OSError):
            # Both are the same disposition -- a target that cannot be
            # reached through the tree the caller validated is rejected, not a
            # server fault -- so they share one arm rather than drifting apart.
            return "notfound"
    src_fd: int | None = None
    try:
        try:
            src_fd = open_access_control_source(path, dir_fd=dir_fd)
        except OSError:
            return "notfound"
        # os.stat by name only where nothing was pinned: with dir_fd the helper
        # always hands back a descriptor, so the mode comes from the same inode
        # the ACL does and neither is re-resolved.
        src_stat = os.fstat(src_fd) if src_fd is not None else os.stat(path)
        # mode= carries copymode's permission bits, and
        # preserve_access_control_from is ADDITIVE to it: bits alone drop a named
        # POSIX ACL (system.posix_acl_access) the owner set, silently, the moment
        # the replace installs a fresh inode. The
        # carry is allowlisted to the ACL and user.* names -- it must NOT replay a
        # privilege-bearing security.capability onto caller-supplied content.
        atomic_write(
            path,
            content,
            mode=_stat_mod.S_IMODE(src_stat.st_mode),
            preserve_access_control_from=src_fd,
            parent_dir_fd=dir_fd,
        )
    finally:
        for fd in (src_fd, dir_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return None
