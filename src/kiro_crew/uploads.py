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

import os
import re
import uuid
from pathlib import Path

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


def create_upload_file(name: str, data: bytes) -> Path:
    """Write *data* into the uploads directory as a NEW owner-only file.

    Blocking (mkdir + open + write); async callers offload it. ``O_EXCL`` makes
    the create refuse an existing path, so a colliding name -- however unlikely
    behind a uuid -- can never overwrite another user file, and ``0o600`` keeps
    the bytes owner-only from the first byte rather than after a later chmod.
    Returns the path written.
    """
    directory = upload_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = new_upload_path(name)
    fd = os.open(
        str(dest),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return dest


def is_upload_path(path: str | os.PathLike[str]) -> bool:
    """Whether *path* sits lexically inside :func:`upload_dir`.

    Lexical on purpose: this answers "is this a file the uploads directory owns",
    the same question a transcript row answers by its prefix. Whether the bytes
    behind it may be READ is a separate gate (the descriptor-pinned read in
    :mod:`kiro_crew.messaging.outbound_files`), not this predicate.
    """
    try:
        root = os.path.abspath(upload_dir())
        return os.path.commonpath((os.path.abspath(os.fspath(path)), root)) == root
    except (OSError, ValueError):
        return False
