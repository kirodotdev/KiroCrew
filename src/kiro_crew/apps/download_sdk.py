"""Verified download: one pinned https file into an app's own data directory (seam level 20).

An app that installs a third-party artifact (a compiler, an engine archive) needs the
transfer the gateway uses for its own pinned files. The private
``asset_downloader.download_to`` is not offered: it moves with the wheel, takes a
cross-host redirect switch and a target anywhere on disk, and any number of callers
may write one target at once. :func:`download_into` is that transfer with every choice
fixed. An app reaches it as ``ctx.verified_download`` in-process and as
``backend_sdk.verified_download`` in a spawned backend.

**One writer per destination.** Every way a fetch could destroy a file it did not
write came from a second writer at the same place, so a fetch takes an exclusive
``platform_compat.file_lock`` on :data:`LOCK_NAME` in the destination's folder before
it creates anything, and holds it until its staging file is installed or gone. A
second fetch into that folder is refused at once, never run beside the first. The
lock is keyed on the FOLDER, which the fetch holds open, and not on the file's name:
a name has spellings one filesystem folds together (case, a trailing dot, a Windows
short name), and a lock keyed on one spelling would let two spellings of one
destination write at once. The lock file is never written and never removed, since
removing a lock file another fetch has open splits the lock in two.

**Verified before it is visible.** The bytes stream into a staging file this fetch
creates itself, exclusively (``O_CREAT | O_EXCL``), under a random name in the
destination's own folder, so the install is one same-filesystem rename. The size and
the sha256 must both match before that rename; a mismatch leaves the destination as
it was.

**Cleanup removes only what this fetch created.** The folder handle given to the
engine (:class:`_OwnFiles`) records the identity (device, inode) of each file it
creates, and removes a name only while it still names one of those. Anything else
at the name, whoever put it there, is left in place.

**Contained in the data directory.** ``dest`` is a relative path. The data
directory is resolved once, so a link at or above it is followed once, as every other
write the app makes through it follows it. Then it and every folder of ``dest`` are
opened from the filesystem root down, one component at a time, through
``platform_compat.pinned_directory``, which refuses a link or a junction at any
component, and the chain stays open for the whole fetch. On POSIX every file step is
relative to the last descriptor; on Windows the open chain stops each folder from
being renamed while the steps go by path.

The pin does not stop an EMPTY Windows folder from being turned into a junction in
place, and a step by path would then land in the junction's target. So the lock is
the folder's first entry: its create is refused when the pinned folder already reads
as a link, and its handle, held without delete sharing for the whole fetch, keeps the
folder non-empty, which a junction cannot be. Each handle that matters is then held
to the kernel's own answer for it (``pinned_fs.fd_real_path``), on every platform:
the pinned folder must be the folder that was checked and the handle must sit in it.
That is checked for the lock when it is opened, for the staging file right before
the rename that publishes it, and for the destination right after the install,
before success is reported.

**The network is the engine's own.** https only; the certificate and the host name
are verified (``asset_downloader.make_ssl_context``) with no option to turn either
off; a redirect is followed only on the same host and port, the engine's policy for a
url whose origin it cannot vouch for. The address the url names is not judged: the
request runs in the caller's own process, which can reach that address with any
client of its own, so a rule here would narrow nothing.

**Residual.** Removing a name is an identity check and then an ``unlink``, two steps.
A process outside the SDK that swaps a file in at this fetch's random staging name
between the two loses that file; no other fetch can, because no other fetch runs in
the folder. On Windows the lock's create is by path, so an empty folder turned into a
junction between the check that it is not one and that create gets an empty lock file
in the junction's target, never truncated and never written, and the fetch is then
refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import stat
from pathlib import Path, PurePath

from kiro_crew import asset_downloader, pinned_fs, platform_compat

logger = logging.getLogger(__name__)

#: The lock every fetch into a folder takes, as a file in that folder.
LOCK_NAME = ".kc-dl.lock"

#: The staging file's suffix, after a dot and a random token.
STAGING_SUFFIX = ".kc-dl"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
#: In ``stat`` at runtime everywhere, but typed for Windows only.
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class VerifiedDownloadError(OSError):
    """A verified download installed nothing.

    An ``OSError``, so an app's failed-I/O branch handles it. The message is the
    transfer engine's own wording (a transport cause, the size, a digest mismatch, a
    refused redirect, a refused folder, another fetch running), which names a host and
    a local path and never the url's path, query or a credential.
    """


def _same_real_path(first: str, second: str) -> bool:
    """Whether two resolved paths name one place, spelled the way this OS compares names.

    Lexical on purpose (the extended-length prefix dropped, then ``normpath`` and
    ``normcase``): both sides are already the kernel's or ``realpath``'s answer, and
    resolving again here could bless the redirect the caller checks for.
    """

    def fold(path: str) -> str:
        return os.path.normcase(
            os.path.normpath(platform_compat.strip_extended_length_prefix(path))
        )

    return fold(first) == fold(second)


class _OwnFiles(asset_downloader.PinnedTargetDir):
    """The destination folder, where a name is removed only while it names a file of ours.

    ``download_to`` sends every filesystem step through this. ``open`` with
    ``O_CREAT | O_EXCL`` records the identity of the file it made, and ``unlink`` reads
    the name's identity first and leaves anything it did not make in place, so a
    refusal's cleanup, the engine's own included, cannot remove another writer's file.
    """

    def __init__(self, directory: Path, fd: int, *, relative: bool) -> None:
        super().__init__(directory, fd, relative=relative)
        self._created: set[tuple[int, int]] = set()

    def open(self, name: str, flags: int, mode: int = 0o600) -> int:
        fd = super().open(name, flags, mode)
        if flags & os.O_CREAT and flags & os.O_EXCL:
            try:
                self._created.add(asset_downloader.file_identity(os.fstat(fd)))
            except OSError:
                os.close(fd)
                raise
        return fd

    def unlink(self, name: str) -> None:
        named = self.lstat(name)
        if named is None:
            return
        if asset_downloader.file_identity(named) not in self._created:
            logger.warning(
                "leaving %s in place: it is not a file this download created", self.describe(name)
            )
            return
        super().unlink(name)

    def discard_own(self, name: str) -> None:
        """Remove *name* if it is still a file this fetch created. Never raises."""
        try:
            self.unlink(name)
        except OSError:
            logger.debug("could not remove %s", self.describe(name), exc_info=True)

    def require_inside(self, fd: int, name: str) -> None:
        """Raise ``StagingRefused`` unless the open *fd* sits in the folder that was checked.

        Both answers are the kernel's for handles held open (``pinned_fs.fd_real_path``,
        ``GetFinalPathNameByHandleW`` on Windows): the pinned folder's must be the
        folder that was checked, and *fd*'s parent must be that folder. A folder
        moved after the pin, or turned into a junction in place, reads as another
        place, and a path that cannot be read refuses.
        """
        held = pinned_fs.fd_real_path(self.fd)
        real = pinned_fs.fd_real_path(fd)
        if (
            held is None
            or real is None
            or not _same_real_path(held, os.fspath(self.directory))
            or not _same_real_path(os.path.dirname(real), held)
        ):
            raise asset_downloader.StagingRefused(
                f"refusing {self.describe(name)}: it is not in the folder that was checked "
                "inside the app's data directory"
            )

    def require_name_inside(self, name: str) -> None:
        """:meth:`require_inside` for whatever *name* opens as now, never through a link."""
        fd = self.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
        try:
            self.require_inside(fd, name)
        finally:
            os.close(fd)

    def replace(self, src: str, dst: str) -> None:
        # The rename publishes the staging file, so it must still be in the checked folder.
        self.require_name_inside(src)
        super().replace(src, dst)

    def open_lock(self, name: str) -> int:
        """Open or create the lock file *name*: never truncated, never through a link.

        It is the first entry the fetch makes in the folder, and the folder it is in
        must be the one that was checked (:meth:`require_inside`). On Windows the open
        goes by path, so it is refused first when the pinned folder already reads as a
        link: an empty folder can be turned into a junction in place while it is
        pinned, and once the lock is in it the folder is not empty and cannot be.
        """
        if self._relative:
            fd = platform_compat.open_create_or_existing(
                name, os.O_RDWR | _O_NOFOLLOW, 0o600, dir_fd=self.fd
            )
        else:
            if os.name == "nt" and self._pinned_folder_is_a_link():
                raise asset_downloader.StagingRefused(
                    f"refusing {self.directory}: it became a link or a junction after it was "
                    "checked"
                )
            fd = platform_compat.open_create_no_reparse(self.directory / name, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"the download lock {self.describe(name)} is not a regular file")
            self.require_inside(fd, name)
        except OSError:
            os.close(fd)
            raise
        return fd

    def _pinned_folder_is_a_link(self) -> bool:  # pragma: no cover - Windows
        attributes = getattr(os.fstat(self.fd), "st_file_attributes", 0) or 0
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) and platform_compat.win_fd_is_link(
            self.fd
        )


def _checked_url(url: object) -> str:
    if not isinstance(url, str) or url[:8].lower() != "https://":
        raise ValueError("url must be an https:// URL")
    return url


def _checked_sha256(sha256: object) -> str:
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256.lower()):
        raise ValueError("sha256 must be the file's 64-character hex SHA-256 digest")
    return sha256.lower()


def _checked_size(size: object) -> int:
    ceiling = asset_downloader.DEFAULT_MAX_BYTES
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= ceiling:
        raise ValueError(f"size must be the file's exact length in bytes, from 1 to {ceiling}")
    return size


def _checked_dest(dest: object) -> tuple[tuple[str, ...], str]:
    """``(folder components, file name)`` of *dest*, parsed once, or ``ValueError``.

    A relative path of plain names only: no anchor or drive, no ``..``. The file name
    must not be the lock file's or end like a staging file's, and must not end in a
    dot or a space, which Windows drops from a name.
    """
    if not isinstance(dest, (str, os.PathLike)):
        raise TypeError(f"dest must be a str or os.PathLike, not {type(dest).__name__}")
    raw = os.fspath(dest)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("dest must be a non-empty str path")
    path = PurePath(raw)
    if path.anchor:
        raise ValueError("dest must be a path relative to the app's data directory")
    if not path.parts or ".." in path.parts:
        raise ValueError("dest must name a file inside the app's data directory")
    *folder, name = path.parts
    lowered = name.lower()
    if lowered == LOCK_NAME or lowered.endswith(STAGING_SUFFIX) or name[-1] in ". ":
        raise ValueError(f"dest cannot be named {name!r}")
    return tuple(folder), name


def _staging_name() -> str:
    """A fresh staging name: 128 random bits, so one already there was planted."""
    return f".{secrets.token_hex(16)}{STAGING_SUFFIX}"


def _refuse_on_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "a verified download blocks until the whole file has arrived: "
        "call it from a worker thread (asyncio.to_thread)"
    )


def _pin_folder(
    stack: contextlib.ExitStack, root: str, folder: tuple[str, ...]
) -> platform_compat.PinnedDirectory:
    """Pin *root* and then each of *folder*, from the filesystem root down; return the last.

    Every pin stays on *stack*. A link or a junction at any component, or one that
    is missing or not a folder, raises :class:`VerifiedDownloadError`.
    """
    parts = PurePath(root).parts
    try:
        current = stack.enter_context(platform_compat.pinned_directory(parts[0], links_only=True))
        for part in (*parts[1:], *folder):
            current = stack.enter_context(current.child(part))
    except OSError as exc:
        raise VerifiedDownloadError(
            "the destination folder must be a real folder inside the app's data directory; "
            "a missing folder, a file, a link or a junction on the way to it is refused "
            f"({type(exc).__name__})"
        ) from None
    return current


def download_into(
    data_dir: str | os.PathLike[str],
    url: str,
    dest: str | os.PathLike[str],
    *,
    sha256: str,
    size: int,
    label: str,
) -> Path:
    """Fetch *url* to *dest* inside *data_dir*, verified, and return the installed path.

    The one implementation behind ``ctx.verified_download`` and
    ``backend_sdk.verified_download``; *data_dir* and *label* come from the gateway's
    record of the calling app, never from the app. Raises ``ValueError`` or
    ``TypeError`` for a refused argument before anything is opened,
    ``RuntimeError`` on an event-loop thread, and :class:`VerifiedDownloadError` when
    nothing was installed. Blocking.
    """
    _refuse_on_event_loop()
    url = _checked_url(url)
    digest = _checked_sha256(sha256)
    size = _checked_size(size)
    folder, name = _checked_dest(dest)
    root = os.path.realpath(os.fspath(data_dir))
    with contextlib.ExitStack() as stack:
        pinned = _pin_folder(stack, root, folder)
        directory = Path(pinned.path)
        where = _OwnFiles(directory, pinned.fileno(), relative=platform_compat.IS_POSIX)
        try:
            lock_fd = where.open_lock(LOCK_NAME)
        except asset_downloader.StagingRefused as exc:
            raise VerifiedDownloadError(str(exc)) from None
        except OSError as exc:
            raise VerifiedDownloadError(
                f"the download lock in {directory} could not be opened ({type(exc).__name__})"
            ) from None
        stack.callback(os.close, lock_fd)
        try:
            stack.enter_context(platform_compat.file_lock(lock_fd, exclusive=True, wait=False))
        except BlockingIOError:
            raise VerifiedDownloadError(
                f"another verified download into {directory} is running; try again once it ends"
            ) from None
        except OSError as exc:
            raise VerifiedDownloadError(
                f"the download lock in {directory} could not be taken ({type(exc).__name__})"
            ) from None
        staging = _staging_name()
        # Last in, first out: the staging file goes while the lock is still held.
        stack.callback(where.discard_own, staging)
        try:
            ok, reason = asset_downloader.download_to(
                directory / name,
                url,
                sha256=digest,
                size=size,
                min_bytes=size,
                staging=directory / staging,
                label=label,
                target_dir=where,
            )
        except OSError as exc:
            # A close that fails again after the transfer's own error escapes the engine.
            ok, reason = False, f"the transfer did not finish ({type(exc).__name__})"
        if not ok:
            raise VerifiedDownloadError(reason)
        try:
            where.require_name_inside(name)
        except OSError as exc:
            # Not reported as installed: the file goes if it is still the one this fetch
            # made, wherever the name now leads, and nothing else is touched.
            where.discard_own(name)
            if isinstance(exc, asset_downloader.StagingRefused):
                raise VerifiedDownloadError(str(exc)) from None
            raise VerifiedDownloadError(
                f"the installed {where.describe(name)} could not be checked ({type(exc).__name__})"
            ) from None
        return directory / name


__all__ = ["LOCK_NAME", "STAGING_SUFFIX", "VerifiedDownloadError", "download_into"]
