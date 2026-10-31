"""kiro-cli ``toolsSettings.fs_write.allowedPaths`` entries, read and matched safely.

kiro-cli builds each entry into a glob set (Rust's ``globset``, its default
options: ``*`` and ``?`` also match ``/``, case-sensitive) after expanding ``~``
and anchoring a relative entry at the session's directory, and it adds the twin
``<entry>/**`` so that a directory entry covers what is inside it. A write whose
path one of them matches is allowed.

Crew reads the same entries at its own gate on KAS, where the spec's
``fallbackAction: "deny"`` refuses every write they do not cover. A misread there
would refuse a write kiro-cli allows, or allow one it refuses, so only the subset
both readers agree on is accepted: literal characters, ``*`` and ``**`` as "any run
of characters", ``?`` as "any one character", and a leading ``~`` or ``~/`` for the
home directory. Every other entry (a character class ``[...]``, an alternation
``{a,b}``, an escape ``\\``, a variable ``$NAME``, a ``~user``, a control
character, an empty or over-long entry) is refused (``None``): it covers nothing,
so a write only it would cover is refused, never let through.

``/**/`` also matches a single ``/``, as in ``globset`` (``src/**/x`` covers
``src/x``).

On Windows, where ``globset`` reads ``\\`` as a separator, both an entry and a
path are first put in one glob form (:func:`to_glob_path`): ``\\`` becomes ``/``
and ASCII letters are lower-cased, since NTFS names one file under every ASCII
case. A drive-letter path (``c:/work``) counts as absolute. An entry written with
a variable (``%USERPROFILE%``, ``$env:``), a UNC entry, and one whose ``..``
climbs off its drive cover nothing.

Matching is linear in practice and bounded: an entry holds at most
:data:`MAX_PATTERN_LENGTH` characters and at most :data:`_MAX_DOUBLE_STAR_DIRS`
``/**/`` segments, and a path longer than :data:`MAX_PATH_LENGTH` is not covered.

A LEAF on purpose: stdlib only, so both the KAS projection and the gate import it.
"""

from __future__ import annotations

import posixpath
import re
import sys
from collections.abc import Iterable

#: Whether entries and paths are read the Windows way (:func:`to_glob_path`).
#: Every function takes it as a keyword, so both readings are testable anywhere.
ON_WINDOWS = sys.platform == "win32"

#: A drive-letter root in glob form: ``c:/``.
_DRIVE_ROOT = re.compile(r"[a-z]:/")

#: ASCII-only case folding. NTFS compares names by simple upper-casing, so a
#: full Unicode fold (``ſ`` to ``s``) would cover a directory NTFS keeps apart.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _root(path: str) -> str:
    """The root of the absolute glob-form *path*: ``c:/`` or ``/``."""
    drive = _DRIVE_ROOT.match(path)
    return drive.group(0) if drive else _SEP


#: The separator kiro-cli's globs are written with, whatever the host's own is: an
#: entry is a glob string, not an OS path, so it is read with :mod:`posixpath`.
_SEP = "/"

#: Bound on one entry, so the record a session carries stays small.
MAX_PATTERN_LENGTH = 1024

#: Paths longer than this are not matched against an entry at all.
MAX_PATH_LENGTH = 4096

#: Characters whose glob meaning Crew does not read, so an entry holding one
#: covers nothing.
_UNREAD_GLOB_CHARS = frozenset("[]{}\\$")

#: A run of two or more ``/**`` segments followed by ``/``, folded to one ``/**/``.
_DOUBLE_STAR_RUN = re.compile(r"(?:/\*\*){2,}/")

#: ``/**/`` segments one entry may hold. Each one is tried both as ``/`` and as
#: ``/<any run>/``, so the work per match doubles with each.
_MAX_DOUBLE_STAR_DIRS = 3


def to_glob_path(path: str, *, windows: bool = ON_WINDOWS) -> str:
    """*path* in the form entries are matched in: unchanged on POSIX; on Windows
    with ``\\`` as ``/`` and ASCII letters lower-cased."""
    return path.replace("\\", _SEP).translate(_ASCII_LOWER) if windows else path


def join(head: str, rest: str) -> str:
    """*head* and the relative *rest*, joined in glob form."""
    return posixpath.join(head, rest)


def is_absolute(path: str, *, windows: bool = ON_WINDOWS) -> bool:
    """Whether the glob-form *path* is absolute: ``/``-rooted, or on Windows a
    drive-letter root (``c:/``)."""
    return path.startswith(_SEP) or (windows and _DRIVE_ROOT.match(path) is not None)


def parse(raw: object, *, windows: bool = ON_WINDOWS) -> str | None:
    """*raw* as Crew reads it (in glob form), or ``None`` when outside the subset."""
    if not isinstance(raw, str):
        return None
    if not raw or len(raw) > MAX_PATTERN_LENGTH:
        return None
    if windows:
        if "%" in raw:
            return None
        raw = to_glob_path(raw, windows=True)
        if raw.startswith("//"):
            # A UNC share: not a path this reader anchors.
            return None
    if any(c in _UNREAD_GLOB_CHARS or not c.isprintable() for c in raw):
        return None
    if raw.startswith("~") and raw != "~" and not raw.startswith("~/"):
        # ``~user``: another account's home, which this reader does not resolve.
        return None
    if raw.count("/**/") > _MAX_DOUBLE_STAR_DIRS:
        return None
    return raw


def anchor(pattern: str, cwd: str, home: str, *, windows: bool = ON_WINDOWS) -> str | None:
    """*pattern* (already :func:`parse`'d) as the absolute entry kiro-cli matches.

    ``~`` becomes *home*; a relative entry is joined to *cwd*, the session's own
    directory. The part before the first wildcard is normalized (``..`` and ``.``
    removed, repeated ``/`` folded), as kiro-cli normalizes the entry it builds; a
    ``.`` or ``..`` segment after a wildcard covers nothing (``None``), because
    what it names depends on what the wildcard matched. ``None`` too for a
    relative entry with no *cwd*, a ``~`` entry with no *home*, and a result past
    :data:`MAX_PATTERN_LENGTH`.
    """
    cwd = to_glob_path(cwd, windows=windows)
    home = to_glob_path(home, windows=windows)
    if pattern == "~" or pattern.startswith("~/"):
        if not is_absolute(home, windows=windows):
            return None
        pattern = posixpath.join(home, pattern[2:].lstrip(_SEP))
    elif not is_absolute(pattern, windows=windows):
        if not is_absolute(cwd, windows=windows):
            return None
        pattern = posixpath.join(cwd, pattern)
    head, sep, tail = _split_at_wildcard(pattern)
    if sep and any(part in (".", "..") for part in tail.split(_SEP)):
        return None
    norm_head = posixpath.normpath(head) if head else _SEP
    if head.endswith(_SEP) and norm_head != _SEP:
        norm_head += _SEP
    out = norm_head + tail if sep else norm_head
    out = re.sub("/{2,}", _SEP, out)
    if windows and (not is_absolute(out, windows=True) or _root(out) != _root(pattern)):
        # ``..`` climbed past the drive root (``c:/../d:/x``): kiro-cli would not
        # land on another drive, so the entry covers nothing.
        return None
    return out if len(out) <= MAX_PATTERN_LENGTH else None


def literal_prefix_dir(anchored: str) -> str:
    """The directory part of *anchored* before its first wildcard.

    For a caller that resolves symlinks in that prefix: ``/a/b/*.txt`` gives
    ``/a/b``, ``/a/b`` gives ``/a/b``.
    """
    head, sep, _tail = _split_at_wildcard(anchored)
    if not sep:
        return head
    return posixpath.dirname(head) or _SEP


def matches(anchored: str, path: str, *, windows: bool = ON_WINDOWS) -> bool:
    """Whether the absolute entry *anchored* covers the absolute glob-form *path*
    (:func:`to_glob_path`).

    Covered when the entry matches *path* whole, or when *path* lies below what
    it matches (kiro-cli's ``<entry>/**`` twin). Neither side is touched on disk.
    """
    if not is_absolute(path, windows=windows) or len(path) > MAX_PATH_LENGTH:
        return False
    twin = anchored.rstrip("/") + "/*"
    return any(
        _glob_match(variant, path) for form in (anchored, twin) for variant in _variants(form)
    )


def covered(patterns: Iterable[str], path: str, *, windows: bool = ON_WINDOWS) -> bool:
    """Whether any of the anchored *patterns* covers the native *path*, put in glob
    form first."""
    form = to_glob_path(path, windows=windows)
    return any(matches(p, form, windows=windows) for p in patterns)


def _split_at_wildcard(pattern: str) -> tuple[str, bool, str]:
    """``(head, has_wildcard, rest)``: *pattern* cut at the start of the first
    ``/``-segment that holds a wildcard; ``(pattern, False, "")`` when none does."""
    idx = min((i for i in (pattern.find("*"), pattern.find("?")) if i >= 0), default=-1)
    if idx < 0:
        return pattern, False, ""
    cut = pattern.rfind("/", 0, idx) + 1
    return pattern[:cut], True, pattern[cut:]


def _variants(pattern: str) -> list[str]:
    """*pattern* with each ``/**/`` read both as ``/`` and as ``/*/``.

    Adjacent ``/**/**/`` runs are folded to one ``/**/`` first: zero or more
    directories twice over is zero or more directories once, and an unfolded run
    would leave one ``**`` needing a ``/`` of its own.
    """
    pattern = _DOUBLE_STAR_RUN.sub("/**/", pattern)
    parts = pattern.split("/**/")
    out = [parts[0]]
    for part in parts[1:]:
        out = [prefix + joiner + part for prefix in out for joiner in ("/", "/*/")]
    return out


def _glob_match(pattern: str, text: str) -> bool:
    """Whether *pattern* (``*`` any run, ``?`` one character) matches *text* whole.

    The pattern is cut at its ``*`` runs; the first piece must match at the start,
    the last at the end, and each middle piece is taken at its leftmost match,
    which is exact for a pattern whose only repetition is ``*``.
    """
    pieces = re.split(r"\*+", pattern)
    compiled = [_piece(p) for p in pieces]
    if len(pieces) == 1:
        return compiled[0].fullmatch(text) is not None
    first = compiled[0].match(text)
    if first is None:
        return False
    pos = first.end()
    for piece in compiled[1:-1]:
        found = piece.search(text, pos)
        if found is None:
            return False
        pos = found.end()
    last_len = len(pieces[-1])
    if len(text) - pos < last_len:
        return False
    return compiled[-1].fullmatch(text, len(text) - last_len) is not None


def _piece(piece: str) -> re.Pattern[str]:
    return re.compile("".join("." if c == "?" else re.escape(c) for c in piece), re.DOTALL)
