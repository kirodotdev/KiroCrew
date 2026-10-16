"""Build the channel's Code-tab diff: tracked changes, new files, and the size cap.

Pure apart from :func:`render_untracked`, which reads one file from the worktree.
Slack's ``agents.conversations.setView`` refuses ``content`` over 1,000,000 bytes
(``content_too_large``), so the assembled diff is cut to fit, at a FILE boundary
where one fits, and opens with a plain-text note saying so — a truncated diff that
looked complete would tell the channel the agent changed less than it did.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

#: Slack's setView content ceiling, in bytes of the UTF-8 body.
MAX_VIEW_BYTES = 1_000_000

#: Bytes held back for the truncation note so the note can never push the body
#: over the ceiling.
_NOTE_RESERVE = 512

#: How much of a file is inspected for a NUL byte (git's own binary heuristic).
_BINARY_SNIFF = 8000

_FILE_HEADER = b"diff --git "

#: Git porcelain always separates path components with "/", on every platform.
_GIT_SEP = "/"


@dataclass(frozen=True)
class BuiltDiff:
    """The view body. ``content`` is ``""`` when there is nothing to show."""

    content: str
    truncated: bool
    files: int


def _count_files(body: bytes) -> int:
    count = 1 if body.startswith(_FILE_HEADER) else 0
    return count + body.count(b"\n" + _FILE_HEADER)


def _cut_to_fit(body: bytes, budget: int) -> bytes:
    """``body`` cut to at most ``budget`` bytes, at a file boundary when one fits.

    Falls back to the last line boundary when even the first file is larger than
    the budget, so the view still shows the start of that file.
    """
    if len(body) <= budget:
        return body
    window = body[: budget + 1]
    boundary = window.rfind(b"\n" + _FILE_HEADER)
    if boundary > 0:
        return body[: boundary + 1]
    line = body[:budget].rfind(b"\n")
    return body[: line + 1] if line >= 0 else b""


def truncation_note(files_shown: int) -> str:
    """The plain-text line that opens a cut diff (outside every file section)."""
    return (
        "Diff truncated to fit Slack's 1,000,000-byte view limit: "
        f"{files_shown} file(s) shown, other changes are not included.\n"
    )


def _utf8(data: bytes) -> bytes:
    """``data`` as valid UTF-8: every invalid byte becomes U+FFFD (three bytes).

    Done BEFORE anything is measured or cut, so the size the cap is checked
    against is the size Slack receives, and the truncation note is decided on
    the final body rather than on bytes that grow afterwards.
    """
    return data.decode("utf-8", "replace").encode("utf-8")


def assemble(
    tracked: bytes,
    *,
    tracked_truncated: bool,
    untracked: list[bytes],
    untracked_truncated: bool,
    limit: int = MAX_VIEW_BYTES,
) -> BuiltDiff:
    """Join tracked and new-file sections under ``limit`` bytes.

    ``tracked_truncated`` / ``untracked_truncated`` say an upstream read already
    lost data (git output overflowed its cap, a new file could not be shown),
    which marks the result truncated even when what survived happens to fit.
    """
    budget = limit - _NOTE_RESERVE
    truncated = tracked_truncated or untracked_truncated
    tracked = _utf8(tracked)
    body = _cut_to_fit(tracked, budget)
    if len(body) < len(tracked):
        truncated = True
    if body and not body.endswith(b"\n"):
        body += b"\n"
    parts = [body] if body else []
    used = len(body)
    for chunk in untracked:
        chunk = _utf8(chunk)
        if used + len(chunk) > budget:
            truncated = True
            break
        parts.append(chunk)
        used += len(chunk)
    joined = b"".join(parts)
    files = _count_files(joined)
    if not joined:
        if truncated:
            # Everything was too large to show even one file: say so rather than
            # publish nothing and let the tab read as "no changes".
            return BuiltDiff(truncation_note(0), True, 0)
        return BuiltDiff("", False, 0)
    text = joined.decode("utf-8")
    if truncated:
        text = truncation_note(files) + "\n" + text
    return BuiltDiff(text, truncated, files)


def _safe_rel(rel: str) -> bool:
    """A git-listed relative path we are willing to render (no control chars, no ``..``)."""
    if not rel or os.path.isabs(rel) or any(ord(ch) < 32 or ord(ch) == 127 for ch in rel):
        return False
    return not {"", ".", ".."} & set(rel.replace("\\", _GIT_SEP).split(_GIT_SEP))


#: :func:`render_untracked`'s answer for a file that exists but cannot be shown
#: (too large for the remaining budget, or a name that would break the diff), so
#: the caller marks the diff truncated instead of dropping the file silently.
OMITTED = b""

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
#: ``O_NONBLOCK`` so a FIFO swapped in for a regular file cannot park the open forever.
_LEAF_FLAGS = (
    os.O_RDONLY
    | _NOFOLLOW
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


#: Whether this platform can open relative to a directory fd (not Windows).
_DIR_FD_OK = all(fn in os.supports_dir_fd for fn in (os.open, os.stat, os.readlink))


def _walk_beneath(root: str, parts: list[str]) -> int:
    """A directory fd for ``root/parts...``, following NO symlink on the way down.

    Every component -- the worktree root included -- is opened
    ``O_DIRECTORY | O_NOFOLLOW`` (each relative to its parent's fd), so a
    directory swapped for a symlink between two reads fails the open instead of
    steering the read outside the worktree. ``root`` is the worktree this
    package created, so its own last component is never a symlink.
    """
    fd = os.open(root, _DIR_FLAGS | _NOFOLLOW)
    try:
        for part in parts:
            nxt = os.open(part, _DIR_FLAGS | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_leaf(root: str, rel: str, budget: int) -> tuple[str, bytes, int] | None:
    """``(kind, data, mode)`` for one untracked path; kind ``link``/``file``/``large``.

    The gateway reads this file itself, outside any sandbox, so the read must be
    confined to the worktree against an agent racing it: directories are walked
    by fd (:func:`_walk_beneath`), the leaf is ``lstat``-ed and opened relative to
    that fd with ``O_NOFOLLOW | O_NONBLOCK``, and the opened fd must be the same
    regular file the ``lstat`` saw. A file with more than one hard link is never
    read: its other name can be anywhere on the filesystem, including a file the
    agent's own sandbox hides (it is shown as ``large``, i.e. omitted with the
    truncation note). Without ``dir_fd`` support (Windows) nothing is read.
    """
    if not _DIR_FD_OK:
        return None
    parts = rel.replace(os.sep, _GIT_SEP).split(_GIT_SEP)
    try:
        dir_fd = _walk_beneath(root, parts[:-1])
    except OSError:
        return None
    leaf = parts[-1]
    try:
        try:
            st = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                return "link", os.fsencode(os.readlink(leaf, dir_fd=dir_fd)), 0o120000
            if not stat.S_ISREG(st.st_mode):
                return None
            if st.st_size > budget or st.st_nlink > 1:
                return "large", b"", 0
            fd = os.open(leaf, _LEAF_FLAGS, dir_fd=dir_fd)
        except OSError:
            return None
    finally:
        os.close(dir_fd)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)
            or opened.st_nlink > 1
        ):
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(budget + 1)
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > budget:
        return "large", b"", 0
    return "file", data, (0o100755 if st.st_mode & 0o111 else 0o100644)


def render_untracked(root: str, rel: str, *, budget: int) -> bytes | None:
    """A ``new file`` diff section for one untracked path.

    Returns the section, :data:`OMITTED` for a file that exists but cannot be
    shown (too large, or an unsafe name), or ``None`` for something that is not
    a file to show at all (a directory, a FIFO, a path that vanished or escaped).

    Never follows a symlink: a link is rendered as git does (mode ``120000`` and
    its target text), so a link to a credential file cannot pull that file into a
    Slack view, and the read itself is confined as :func:`_read_leaf` describes.
    """
    if not _safe_rel(rel):
        return OMITTED
    leaf = _read_leaf(root, rel, budget)
    if leaf is None:
        return None
    kind, data, mode = leaf
    if kind == "large":
        return OMITTED
    name = rel.replace(os.sep, _GIT_SEP).encode("utf-8", "surrogateescape")
    header = b"diff --git a/" + name + b" b/" + name + b"\n"
    header += b"new file mode " + format(mode, "o").encode() + b"\n"
    if kind == "link":
        section = (
            header
            + b"--- /dev/null\n+++ b/"
            + name
            + b"\n@@ -0,0 +1 @@\n+"
            + data
            + b"\n\\ No newline at end of file\n"
        )
    elif not data:
        section = header
    elif b"\0" in data[:_BINARY_SNIFF]:
        section = header + b"Binary files /dev/null and b/" + name + b" differ\n"
    else:
        lines = data.split(b"\n")
        missing_newline = not data.endswith(b"\n")
        if not missing_newline:
            lines.pop()
        count = b"" if len(lines) == 1 else b"," + str(len(lines)).encode()
        hunk = [b"--- /dev/null\n", b"+++ b/" + name + b"\n", b"@@ -0,0 +1" + count + b" @@\n"]
        hunk.extend(b"+" + line + b"\n" for line in lines)
        if missing_newline:
            hunk.append(b"\\ No newline at end of file\n")
        section = header + b"".join(hunk)
    return section if len(section) <= budget else OMITTED
