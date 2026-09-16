"""Where a user's files live once they reach the gateway, for every surface.

The dashboard composer has always written a pasted picture into
``<data_home>/uploads/`` and recorded it in the transcript as
``![image](/abs/uploads/<uuidhex>_<name>.png)``: the transcript row points at a
file that outlives the turn, and the chat renders it from there. That directory
is the one place a surface may READ an image from as well -- the Slack renderer
authorizes it as an upload root beside the session's own cwd, so a picture the
user pasted in the dashboard (or one the agent drew there) can travel to the
linked thread. Inbound channel images are promoted into the same directory so
the dashboard renders them exactly like its own.

Three modules need the same answer to "where is that directory" and cannot import
each other: :mod:`kiro_crew.dashboard.handlers.files` writes it, while
:mod:`kiro_crew.messaging.attachments` and :mod:`kiro_crew.messaging.outbound_files`
read from and promote into it. It lives here, below all three, so none of them
has to reach across the dashboard/messaging boundary for a path.

Everything resolves against the LIVE data home on every call (see
:func:`kiro_crew.config.paths.data_home`): a ``KIROCREW_HOME`` set after import,
a pod, or a test home all redirect it. :data:`_UPLOAD_DIR` is the opt-in test
override, kept for the same reason the dashboard handler keeps its own -- a test
that pins the directory should not have to reach into the environment.
"""

from __future__ import annotations

import errno
import os
import re
import uuid
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.config.paths import data_home

#: Test override. ``None`` means "resolve against the live data home".
_UPLOAD_DIR: Path | None = None

#: Characters a stored filename may keep. Everything else -- path separators,
#: shell metacharacters, whitespace -- becomes ``_``, so a sender-supplied name
#: can never steer the destination out of the directory or carry a NUL.
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-]")


def upload_dir() -> Path:
    """The uploads directory, resolved against the live data home."""
    return _UPLOAD_DIR if _UPLOAD_DIR is not None else data_home() / "uploads"


def safe_upload_name(name: str) -> str:
    """Sanitize a sender-supplied filename to one path component.

    The dashboard composer's own rule, shared so an image promoted from a channel
    is named exactly the way a pasted one is: basename only (no traversal), and
    every character outside ``[\\w.-]`` replaced. Empty in, ``upload`` out.
    """
    return _UNSAFE_NAME_RE.sub("_", Path(name or "").name) or "upload"


def new_upload_path(name: str) -> Path:
    """A fresh, unique path inside :func:`upload_dir` for *name*.

    ``<uuid hex>_<sanitized name>`` -- the dashboard's shape, so a channel-born
    file and a composer-born one are indistinguishable on disk and in a
    transcript row. The uuid is also what makes the name unguessable to anyone
    who can only see the directory listing's siblings.
    """
    return upload_dir() / f"{uuid.uuid4().hex}_{safe_upload_name(name)}"


#: A Windows-shaped absolute path: drive letter or UNC host, then a separator.
_WINDOWS_SHAPED_RE = re.compile(r"^(?:[A-Za-z]:|\\\\[^\\/]+)[\\/]")

#: The destination alphabet micromark leaves byte-identical, so an unwrapped
#: destination inside it round-trips without any decoding step. ASCII on
#: purpose: the producer's ``\w`` is JavaScript's, which is ASCII-only.
_MD_DEST_SAFE_RE = re.compile(r"^[\w/.@:~\-]*$", re.ASCII)

#: Characters that must be backslash-escaped inside a ``<...>`` destination.
_MD_DEST_ESCAPE_RE = re.compile(r"[\\<>]")


def markdown_image_dest(path: str | os.PathLike[str]) -> str:
    """The destination the dashboard composer writes into ``![image](...)``.

    The composer's own rule (``mdImageDest`` in ``website/src/utils/fileTokens.ts``),
    mirrored here so a row the backend writes renders exactly like a pasted one on
    every host: a Windows-shaped path is spelled with forward slashes (CommonMark
    eats a raw ``\\`` before punctuation, so ``C:\\Users\\me\\.kiro`` would lose the
    dot's backslash and break the link); a destination inside the safe alphabet
    passes through unchanged; anything else -- a space in the data home, a ``%``
    -- is ``%``-escaped, ``\\`` ``<`` ``>`` backslash-escaped, and wrapped in
    ``<...>``, the CommonMark form for a destination that may contain spaces.
    The renderer percent-decodes exactly the wrapped form, so the two spellings
    never collide.
    """
    text = os.fspath(path)
    if _WINDOWS_SHAPED_RE.match(text):
        text = text.replace("\\", "/")
    if _MD_DEST_SAFE_RE.match(text) and "%" not in text:
        return text
    escaped = _MD_DEST_ESCAPE_RE.sub(lambda m: "\\" + m.group(0), text.replace("%", "%25"))
    return f"<{escaped}>"


def create_upload_file(name: str, data: bytes) -> Path:
    """Write *data* into the uploads directory as a NEW owner-only file.

    Blocking (mkdir + open + write); async callers offload it. ``O_EXCL`` makes
    the create refuse an existing path, so a colliding name -- however unlikely
    behind a uuid -- can never overwrite another user file, and ``0o600`` keeps
    the bytes owner-only from the first byte rather than after a later chmod.

    The directory is PINNED, never followed. An agent shares this data home,
    and ``uploads/`` replaced with a link would otherwise turn every promotion
    into a write wherever the link points. So the directory is created and
    opened through its pinned parent (:func:`kiro_crew.pinned_fs.create_and_open_dir_pinned`,
    which refuses a link at the name) and the file is created RELATIVE to that
    descriptor with ``O_NOFOLLOW``, so nothing between the check and the write is
    re-resolved by name. Where a platform cannot open relative to a directory
    descriptor (Windows -- see :func:`kiro_crew.pinned_fs.supports_pinned_walk`),
    the everywhere-floor still holds: a link or junction AT ``uploads/`` is
    refused before the by-name create, which is the attack needing no race.

    The write is ALL-OR-NOTHING. ``os.write`` may return a short count without
    raising (a nearly full disk is the ordinary case), so it is looped until
    every byte is on disk, and any failure -- a short write that stops making
    progress, or an ``OSError`` -- unlinks the partial destination before the
    error propagates. A caller that then deletes its temp source (the
    attachment promotion does) must never be left pointing a transcript row at
    a truncated picture with the only complete copy gone. Returns the path
    written.
    """
    directory = upload_dir()
    dest = new_upload_path(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    dir_fd = -1
    if pinned_fs.supports_pinned_walk():
        dir_fd = pinned_fs.create_and_open_dir_pinned(
            directory, what="uploads directory", refusal=OSError
        )
    else:
        if directory.exists() and pinned_fs.is_reparse_point(directory):
            raise OSError(
                errno.ELOOP, "refusing to write into a linked uploads directory", str(directory)
            )
        directory.mkdir(parents=True, exist_ok=True)
    try:
        if dir_fd >= 0:
            fd = os.open(dest.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        else:
            fd = os.open(str(dest), flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(errno.ENOSPC, "short write to the uploads directory", str(dest))
                view = view[written:]
        except BaseException:
            os.close(fd)
            fd = -1
            try:
                if dir_fd >= 0:
                    os.unlink(dest.name, dir_fd=dir_fd)
                else:
                    os.unlink(dest)
            except OSError:
                pass
            raise
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
    return dest
