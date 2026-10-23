"""Build the channel's Code-tab diff: tracked changes, new files, and the size cap.

Pure apart from :func:`render_untracked`, which reads one file from the worktree.
Slack's ``agents.conversations.setView`` refuses ``content`` over 1,000,000 bytes
(``content_too_large``), so the assembled diff is cut to fit, at a FILE boundary
where one fits, and opens with a plain-text note saying so — a truncated diff that
looked complete would tell the channel the agent changed less than it did.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
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
        f"Diff truncated: {files_shown} file(s) shown. Other changes are not included: "
        "they are over Slack's 1,000,000-byte view limit, or were withheld because "
        "they may hold a credential.\n"
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


#: Lines of context kept around each change once a section is screened (git's default).
CONTEXT_LINES = 3

_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_EOL = b"\\"


def _sections(body: bytes) -> list[bytes]:
    """``body``'s per-file ``diff --git`` sections. A content line never starts one:
    every hunk line begins with a space, ``+``, ``-`` or ``\\``."""
    starts = [0] if body.startswith(_FILE_HEADER) else []
    at = body.find(b"\n" + _FILE_HEADER)
    while at >= 0:
        starts.append(at + 1)
        at = body.find(b"\n" + _FILE_HEADER, at + 1)
    starts.append(len(body))
    return [body[a:b] for a, b in zip(starts, starts[1:]) if b > a]


def _git_line_path(line: bytes) -> str | None:
    """The one path a ``diff --git a/X b/X`` line names, or None when it is not that.

    With renames off both sides name the same path, so its length is fixed by
    the line's; anything else (a quoted name, an unknown prefix) does not parse.
    """
    rest = line.rstrip(b"\n")[len(_FILE_HEADER) :]
    if not rest.startswith(b"a/") or (len(rest) - 5) % 2:
        return None
    name = rest[2 : 2 + (len(rest) - 5) // 2]
    if not name or rest != b"a/" + name + b" b/" + name:
        return None
    return os.fsdecode(name)


def _header_paths(header: list[bytes]) -> list[str] | None:
    """Every path a section's header names, or None when one cannot be read.

    Git quotes a path holding a control character, a quote or a backslash even
    with ``core.quotePath=false``; such a section is withheld rather than parsed,
    as is one whose ``diff --git`` line does not parse. That line is read too, so
    a header-only section (binary, mode change, empty file) is still checked.
    """
    if not header:
        return None
    first = _git_line_path(header[0])
    if first is None:
        return None
    paths: list[str] = [first]
    for line in header[1:]:
        line = line.rstrip(b"\n")
        if line in (b"--- /dev/null", b"+++ /dev/null"):
            continue
        if line.startswith((b"--- ", b"+++ ")) and not line.startswith((b"--- a/", b"+++ b/")):
            return None  # quoted, or a prefix the read did not ask for
        for marker in (b"--- a/", b"+++ b/"):
            if line.startswith(marker):
                name = line[len(marker) :]
                # Git ends the line with a tab when the name holds a space.
                paths.append(os.fsdecode(name[:-1] if name.endswith(b"\t") else name))
    return paths


def _versions(lines: list[bytes]) -> tuple[str, str]:
    """The base and worktree text a whole-file hunk spells out."""
    old: list[bytes] = []
    new: list[bytes] = []
    for line in lines:
        mark, text = line[:1], line[1:]
        if mark in (b" ", b"-"):
            old.append(text)
        if mark in (b" ", b"+"):
            new.append(text)
    return (
        b"\n".join(old).decode("utf-8", "replace"),
        b"\n".join(new).decode("utf-8", "replace"),
    )


def _lines(data: bytes) -> list[bytes]:
    """``data`` split after each LF only: a bare CR inside a line is content."""
    parts = data.split(b"\n")
    out = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _span(start: int, count: int) -> bytes:
    return str(start).encode() if count == 1 else f"{start},{count}".encode()


def _cut_context(header: bytes, lines: list[bytes], context: int) -> list[bytes] | None:
    """One hunk's lines regrouped into hunks with ``context`` lines around changes.

    None when the header does not parse (the caller withholds the section).
    """
    m = _HUNK_RE.match(header)
    if m is None:
        return None
    old_no = int(m[1]) + (1 if m[2] == b"0" else 0)
    new_no = int(m[3]) + (1 if m[4] == b"0" else 0)
    real = [i for i, line in enumerate(lines) if not line.startswith(_NO_EOL)]
    if any(lines[i][:1] not in (b" ", b"+", b"-") for i in real):
        return None
    changed = [n for n, i in enumerate(real) if lines[i][:1] in (b"+", b"-")]
    keep = [False] * len(real)
    for n in changed:
        for k in range(max(0, n - context), min(len(real), n + context + 1)):
            keep[k] = True
    out: list[bytes] = []
    group: list[bytes] = []
    first = (0, 0)
    counts = [0, 0]

    def flush() -> None:
        if not group:
            return
        o_start = first[0] if counts[0] else first[0] - 1
        n_start = first[1] if counts[1] else first[1] - 1
        out.append(
            b"@@ -" + _span(o_start, counts[0]) + b" +" + _span(n_start, counts[1]) + b" @@\n"
        )
        out.extend(group)
        group.clear()

    for n, i in enumerate(real):
        line, mark = lines[i], lines[i][:1]
        if keep[n]:
            if not group:
                first, counts = (old_no, new_no), [0, 0]
            group.append(line)
            follower = i + 1
            if follower < len(lines) and lines[follower].startswith(_NO_EOL):
                group.append(lines[follower])
            counts[0] += mark != b"+"
            counts[1] += mark != b"-"
        else:
            flush()
        old_no += mark != b"+"
        new_no += mark != b"-"
    flush()
    return out


#: What a :data:`Classify` callback answers for one complete file version.
CLEAN = "clean"  # nothing credential-shaped: show the hunks as they are
MASK_LINES = "mask_lines"  # credentials each line shows whole: mask line by line
WITHHOLD = "withhold"  # a credential only visible across lines: never show the file

#: The egress sink's verdict on one complete file version (see the constants).
Classify = Callable[[str], str]
#: The egress sink's masking of one line's text.
Mask = Callable[[str], str]


def _mask_lines(lines: list[bytes], mask: Mask) -> list[bytes]:
    """Each content line passed through ``mask`` on its own, without its diff mark."""
    out: list[bytes] = []
    for line in lines:
        mark = line[:1]
        if mark in (b" ", b"+", b"-"):
            text = line[1:].rstrip(b"\n").decode("utf-8", "replace")
            line = mark + mask(text).encode("utf-8") + b"\n"
        out.append(line)
    return out


def _screen_section(
    section: bytes,
    *,
    classify: Classify,
    mask: Mask,
    sensitive: Callable[[str], bool],
    context: int,
) -> bytes | None:
    """``section`` with its context cut, or None when it must not be shown.

    Both complete versions of the file are classified, because a hunk from the
    middle of a PEM key has neither marker and no hunk-level check can tell it
    is one. Also withheld: a credential-shaped path, and a section that does
    not parse.
    """
    lines = _lines(section)
    first_hunk = next((n for n, line in enumerate(lines) if line.startswith(b"@@ ")), len(lines))
    header, rest = lines[:first_hunk], lines[first_hunk:]
    paths = _header_paths(header)
    if paths is None or any(sensitive(p) for p in paths):
        return None
    out = list(header)
    while rest:
        nxt = next((n for n, line in enumerate(rest[1:], 1) if line.startswith(b"@@ ")), len(rest))
        hunk, body, rest = rest[0], rest[1:nxt], rest[nxt:]
        verdicts = {classify(v) for v in _versions([line.rstrip(b"\n") for line in body])}
        if verdicts - {CLEAN, MASK_LINES}:
            return None  # WITHHOLD, or an answer this screen does not know
        cut = _cut_context(hunk, body, context)
        if cut is None:
            return None
        out.extend(_mask_lines(cut, mask) if MASK_LINES in verdicts else cut)
    return b"".join(out)


def screen_tracked(
    body: bytes,
    *,
    truncated: bool,
    classify: Classify,
    mask: Mask,
    sensitive: Callable[[str], bool],
    context: int = CONTEXT_LINES,
) -> tuple[bytes, bool]:
    """A whole-file-context diff screened for credentials and cut to ``context`` lines.

    ``body`` comes from :func:`.git_ops.tracked_diff`; ``classify`` and ``mask``
    are the egress sink's (the service's outbound credential policy). Returns
    the publishable diff and whether anything was left out: a withheld section
    (see :func:`_screen_section`), or, when the read was ``truncated``, its
    last, incomplete section, which is never checked or shown. Hunk headers
    carry no function-name suffix; otherwise a clean file reads as ``git diff -U3``.
    """
    sections = _sections(body)
    omitted = truncated
    if truncated and sections:
        sections.pop()
    kept: list[bytes] = []
    for section in sections:
        screened = _screen_section(
            section, classify=classify, mask=mask, sensitive=sensitive, context=context
        )
        if screened is None:
            omitted = True
        else:
            kept.append(screened)
    return b"".join(kept), omitted


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
