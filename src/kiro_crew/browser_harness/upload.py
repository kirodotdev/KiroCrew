"""``upload {ref, path, via}``: give a page local files.

``path`` is one entry or a list of up to :data:`UPLOAD_MAX_FILES`. An entry is the
absolute path of a file, of a folder, or a glob pattern (``*``, ``?``, ``[``) over
files; a leading ``~/`` is the user's home. ``via`` says how the page takes them:

* ``input`` (the default): *ref* is a visible, enabled ``<input type=file>`` (the
  walker's ``fileInput``), set with CDP ``DOM.setFileInputFiles``
  (``helper_worker._local_set_files``), which fires the page's own ``input`` and
  ``change`` events;
* ``chooser``: *ref* is clicked with file-chooser interception on, and the chooser
  the click opens is answered (``helper_worker._local_chooser_files``), for a button
  over a hidden input or an input the page makes on the fly;
* ``drop``: the files are dropped on *ref* (``helper_worker._local_drop_files``),
  for a drop zone with no input.

Neither of the last two delivers into a frame from another page: a chooser that
opens in one, or a drop point over one, is ``frame_refused``.

A folder goes to an input or chooser only when it takes folders (``webkitdirectory``),
and to any drop zone; the page gets its files under their paths inside it. A glob
gives its matching files, as if each were listed.

The gateway and the browser run outside the agent's sandbox, so a file the browser
reads is one the agent itself might not be allowed to read. The op therefore never
hands the browser the agent's path. Each file is copied with
``hooks.safe_copy_file_nolink`` (one descriptor, refused unless it is a regular file
with one link whose OPENED real path is outside every sensitive location) into a
staging leaf the agent cannot read or write (``OpContext.upload_dir``, under the
hidden IPC leaf), and the browser gets the COPY: what was checked is what the page
gets. Before the copy, :func:`_check_path` names the refusal: absolute, the shared
path screen, one regular file by ``lstat`` (not a link, a folder or a device), at
most :data:`UPLOAD_MAX_BYTES`.

Inside a folder or a glob's matches, dot entries, links and anything not a
regular file are skipped (counted in ``skipped``), never followed; a protected file
or folder refuses the whole call, naming that folder, never a child of it. With no
card (a person's act site grant, ``OpContext.upload_grant_host``) the strict
sandbox's hidden folders are refused too. One call is at most :data:`UPLOAD_MAX_TREE_FILES` files and
:data:`UPLOAD_MAX_BYTES` together, and a folder walk reads at most
:data:`UPLOAD_MAX_WALK` entries. Two delivered names that differ only by case are
refused. A list stays under ``path`` so the tool gate's path keystone, which reads
every string under ``path``, judges each entry before the card.

Every attempt that reaches the path check writes one SEL row per file (per entry
when it is refused before expansion): the path, the size and an outcome word, never
the content. ``ops._upload`` reaches this module by a call-time import.
"""

from __future__ import annotations

import asyncio
import fnmatch
import glob
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import browser_host_trust
from kiro_crew.browser_harness import ops as _ops
from kiro_crew.browser_harness.frames import split_frame_ref
from kiro_crew.platform_compat import lstat_is_name_surrogate

logger = logging.getLogger(__name__)

#: Largest total one ``upload`` sends. A test fixture or a document is far below it.
#: shortcut: one fixed cap; make it a setting if real apps need larger files.
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
#: Most entries in ``path``.
UPLOAD_MAX_FILES = 10
#: Most files one call sends once folders and globs are expanded
#: (``helper_worker._MAX_FILES`` is the same, kept equal by a test).
UPLOAD_MAX_TREE_FILES = 100
#: Most folder entries one call walks, files and folders together.
UPLOAD_MAX_WALK = 10_000
#: Deepest folder one call walks into. Far past a real project tree, and well inside
#: the stack of every recursive step the staged copy later meets (``shutil.rmtree``).
UPLOAD_MAX_DEPTH = 64
#: Staged copies kept for the page to read when its form submits: the newest
#: :data:`UPLOAD_KEEP`, none older than :data:`UPLOAD_KEEP_S`.
UPLOAD_KEEP = 20
UPLOAD_KEEP_S = 24 * 3600
#: The three ways a page takes files.
VIAS = ("input", "chooser", "drop")


class _PathRefused(Exception):
    def __init__(self, code: str, error: str) -> None:
        super().__init__(code)
        self.code = code
        self.error = error


@dataclass
class _Group:
    """One thing the browser is handed: a file, or a folder of files.

    *name* is what the page sees at the top; *files* is ``(source, path under the
    group)``, the path being ``name`` itself for a file.
    """

    name: str
    folder: bool
    files: list[tuple[str, str]] = field(default_factory=list)


def _audit(path: str, size: int | None, outcome: str) -> None:
    """SEL row: the path, its size and an outcome word. Never the file's content."""
    try:
        from kiro_crew.sel import sel

        resource = f"path={path}" + (f" size={size}" if size is not None else "")
        sel().log_api_access(
            caller="browser",
            operation="browser.upload",
            outcome=outcome,
            source="browser",
            resources=resource,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the op
        logger.debug("browser upload audit failed", exc_info=True)


#: The refusal an upload with no card answers for a strict-hidden path: the op
#: route turns it into the person's card (``browser_ops.run_browser_op``).
NEEDS_CARD = "needs_card"


def _folded(path: str) -> str:
    return os.path.normcase(path).casefold()


#: The kernel's own views: ``/proc/self/environ`` is a regular-looking file that holds
#: the gateway's environment. Never sent without a card.
_PSEUDO_FS: tuple[str, ...] = () if os.name == "nt" else ("/proc", "/sys", "/dev")


def _strict_hidden(path: str) -> bool:
    """Whether *path* is in a folder the strict sandbox hides (``strict_dirs()``), or in
    a kernel pseudo file system (:data:`_PSEUDO_FS`).

    The floor's own sensitive list fences only some files of those folders (a
    ``~/.config/gh`` token, a ``~/.docker`` or ``~/.kube`` file beside the config),
    and the card is the person's check on the rest; an upload with no card has none.
    Both sides are taken as spelled and as resolved (a home that is itself a link,
    a link on the way), and compared without case, as the floor compares. The list
    is the platform seam's, so an edition that hides more refuses more.
    """
    from kiro_crew.sandbox_tiers import _sandbox_policy

    home = os.path.expanduser("~")
    seen = {_folded(os.path.abspath(path)), _folded(os.path.realpath(path))}
    roots = [os.path.join(home, rel) for rel in _sandbox_policy().strict_dirs()]
    for root in [*roots, *_PSEUDO_FS]:
        for base in {_folded(os.path.abspath(root)), _folded(os.path.realpath(root))}:
            if any(c == base or c.startswith(base + os.sep) for c in seen):
                return True
    return False


def _needs_card(path: str) -> _PathRefused:
    return _PathRefused(
        NEEDS_CARD,
        f"{path} is in a folder the agent's sandbox hides, so a site grant does not cover it; "
        "the user is asked on a card",
    )


def _screen(path: str, *, strict: bool = False) -> None:
    """The shared path screen, before any probe of the disk: on Windows an ``lstat``
    of a UNC path would already open a connection to that host. *strict* (an upload
    with no card) also refuses the strict sandbox's hidden folders."""
    from kiro_crew import hooks

    if hooks.validate_file_path(path) is None:
        raise _PathRefused(
            "path_refused", f"{path} is a protected or network location; it is not uploaded"
        )
    if strict and _strict_hidden(path):
        raise _needs_card(path)


def _check_path(path: Any, *, strict: bool = False) -> str:
    """The path as given, once :func:`os.lstat` finds one regular file there.

    Only the refusal WORD comes from here: the decision on what is copied is
    ``hooks.safe_copy_file_nolink``'s, made again on the descriptor it opens. A
    folder is ``path_not_file`` on every OS because ``lstat`` judges it before any
    open (on Windows ``os.open`` on a folder fails with ``EACCES``).
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise _PathRefused("bad_path", "upload needs `path`: the absolute path of a file")
    if not os.path.isabs(path):
        raise _PathRefused("bad_path", f"`path` must be absolute: {path!r}")
    _screen(path, strict=strict)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise _PathRefused("path_missing", f"no file at {path}") from None
    except (OSError, ValueError):
        raise _PathRefused("path_unreadable", f"{path} could not be read") from None
    if stat.S_ISLNK(info.st_mode):
        raise _PathRefused("path_symlink", f"{path} is a link; give the file itself")
    if stat.S_ISDIR(info.st_mode):
        raise _PathRefused("path_not_file", f"{path} is a folder")
    if not stat.S_ISREG(info.st_mode):
        raise _PathRefused("path_not_file", f"{path} is not a regular file")
    if info.st_size > UPLOAD_MAX_BYTES:
        raise _PathRefused(
            "file_too_large", f"{path} is {info.st_size} bytes; the limit is {UPLOAD_MAX_BYTES}"
        )
    if not os.access(path, os.R_OK):
        raise _PathRefused("path_unreadable", f"{path} could not be read")
    return path


def _entry_path(entry: Any) -> str:
    """*entry* with a leading ``~`` expanded; refused unless it is then absolute."""
    if not isinstance(entry, str) or not entry or "\x00" in entry:
        raise _PathRefused("bad_path", "each `path` entry is an absolute path or a glob")
    path = os.path.expanduser(entry) if entry.startswith("~") else entry
    if not os.path.isabs(path):
        raise _PathRefused("bad_path", f"`path` must be absolute: {entry!r}")
    return path


class _Walk:
    """One call's expansion: what it found, what it skipped, and its caps.

    Folders and globs share it, so both follow one rule: a dot entry, a link (or a
    Windows junction) and anything not a regular file or folder is skipped and
    counted, never followed; every folder is screened before it is listed, so a
    refusal names that folder and never a child of it; at most
    :data:`UPLOAD_MAX_WALK` entries are read, :data:`UPLOAD_MAX_TREE_FILES` files
    kept and :data:`UPLOAD_MAX_BYTES` counted (by ``lstat``, before any copy).
    """

    def __init__(self, strict: bool, read_allowed: Callable[[str], bool]) -> None:
        self.strict = strict
        self.read_allowed = read_allowed
        self.seen = 0
        self.skipped = 0
        self.files = 0
        self.bytes = 0

    def keep(self, path: str) -> str:
        """Check *path* as one file and count it against the caps."""
        _check_path(path, strict=self.strict)
        if not self.read_allowed(path):
            # Not named: the file sits where this session may not read.
            raise _PathRefused(
                "path_refused", "the policy does not let this session read part of what was named"
            )
        self.files += 1
        if self.files > UPLOAD_MAX_TREE_FILES:
            raise _PathRefused(
                "too_many_files", f"that is more than {UPLOAD_MAX_TREE_FILES} files; pick fewer"
            )
        self.bytes += os.lstat(path).st_size
        if self.bytes > UPLOAD_MAX_BYTES:
            raise _PathRefused(
                "file_too_large", f"the files come to more than {UPLOAD_MAX_BYTES // 2**20} MiB"
            )
        return path

    def _listing(self, folder: str) -> list[str]:
        """The names in *folder*, sorted, once it passes the screen."""
        _screen(folder, strict=self.strict)
        names: list[str] = []
        try:
            with os.scandir(folder) as listing:
                for entry in listing:
                    # Charged as read, so a huge folder stops here, not after a full listing.
                    self.seen += 1
                    if self.seen > UPLOAD_MAX_WALK:
                        raise _PathRefused(
                            "too_many_files", f"that reads more than {UPLOAD_MAX_WALK} entries"
                        )
                    names.append(entry.name)
        except OSError:
            raise _PathRefused("path_unreadable", f"{folder} could not be read") from None
        return sorted(names)

    def collect(
        self, folder: str, rel: str, segments: list[str] | None, out: list[tuple[str, str]]
    ) -> None:
        """Add the files under *folder* (every one, or those *segments* match) to *out*.

        Depth-first in name order, on a stack of its own rather than Python's, at most
        :data:`UPLOAD_MAX_DEPTH` folders deep.
        """
        stack = [(folder, rel, segments, iter(self._listing(folder)))]
        while stack:
            here, here_rel, here_segments, names = stack[-1]
            name = next(names, None)
            if name is None:
                stack.pop()
                continue
            if here_segments is not None and not fnmatch.fnmatchcase(name, here_segments[0]):
                continue
            full = os.path.join(here, name)
            try:
                info = os.lstat(full)
            except OSError:
                self.skipped += 1
                continue
            mode = info.st_mode
            if name.startswith(".") or stat.S_ISLNK(mode) or lstat_is_name_surrogate(info):
                self.skipped += 1
                continue
            deeper = here_segments is not None and len(here_segments) > 1
            if stat.S_ISDIR(mode):
                if here_segments is None or deeper:
                    if len(stack) >= UPLOAD_MAX_DEPTH:
                        raise _PathRefused(
                            "too_deep", f"{full} is more than {UPLOAD_MAX_DEPTH} folders deep"
                        )
                    rest = None if here_segments is None else here_segments[1:]
                    sub_rel = os.path.join(here_rel, name)
                    stack.append((full, sub_rel, rest, iter(self._listing(full))))
                else:
                    self.skipped += 1  # a folder a glob's last part matched
            elif not stat.S_ISREG(mode):
                self.skipped += 1
            elif not deeper:
                out.append((self.keep(full), os.path.join(here_rel, name)))


def _climbs_after_a_pattern(path: str) -> bool:
    """Whether a ``..`` part follows a part with ``*``, ``?`` or ``[`` in *path*.

    Such a ``..`` cancels a pattern part (Windows folds ``x*\\..`` away as it opens
    a path), so the walk would list a folder the card never showed. A ``..`` that
    follows only literal parts names a real folder and is left alone.
    """
    seen_magic = False
    for part in re.split(r"[\\/]", path):
        if part == ".." and seen_magic:
            return True
        seen_magic = seen_magic or glob.has_magic(part)
    return False


def _glob_parts(pattern: str) -> tuple[str, list[str]]:
    """The fixed folder a glob starts from, and the patterns of the parts after it."""
    # ``normpath`` turns a Windows ``C:/Users/u/*.png`` into one separator first, and
    # the drive and root stay one anchor, so ``C:/*.png`` starts at ``C:\``, not ``C:``.
    drive, rest = os.path.splitdrive(os.path.normpath(pattern))
    anchor = drive + (os.sep if rest.startswith(os.sep) else "")
    parts = [part for part in rest.split(os.sep) if part]
    fixed: list[str] = []
    while parts and not glob.has_magic(parts[0]):
        fixed.append(parts.pop(0))
    if not parts:
        raise _PathRefused("bad_path", "the pattern names no file; give the folder itself")
    if any(part == "**" for part in parts):
        raise _PathRefused("bad_path", "`**` is not supported; give a folder to send it all")
    return os.path.join(anchor, *fixed) if fixed else (anchor or os.sep), parts


def _folder_root(path: str) -> None:
    """Refuse *path* as a place to list unless it is a real folder, not a link."""
    _screen(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise _PathRefused("path_missing", f"nothing at {path}") from None
    except (OSError, ValueError):
        raise _PathRefused("path_unreadable", f"{path} could not be read") from None
    if stat.S_ISLNK(info.st_mode) or lstat_is_name_surrogate(info):
        raise _PathRefused("path_symlink", f"{path} is a link; give the folder itself")
    if not stat.S_ISDIR(info.st_mode):
        raise _PathRefused("path_not_file", f"{path} is not a folder")


def expand(
    entries: list[Any],
    *,
    strict: bool = False,
    read_allowed: Callable[[str], bool] = lambda _path: True,
) -> tuple[list[_Group], int]:
    """The groups *entries* name, and how many folder or glob entries were skipped.

    *strict* is an upload with no card (:func:`_strict_hidden`). Raises
    :class:`_PathRefused` before anything is copied.
    """
    walk = _Walk(strict, read_allowed)
    groups: list[_Group] = []
    for entry in entries:
        path = _entry_path(entry)
        if _climbs_after_a_pattern(path):
            # Before the literal check: Windows folds ``x*\..`` away when it opens a
            # path, and ``normpath`` would too, leaving a folder the card never showed.
            raise _PathRefused(
                "bad_path", "a pattern may not climb with `..`; give the folder itself"
            )
        _screen(path, strict=strict)
        # A path that exists is taken as itself: a file named ``photo [1].png`` is
        # that file, never the pattern its brackets spell.
        if glob.has_magic(path) and not os.path.lexists(path):
            top, segments = _glob_parts(path)
            _folder_root(top)
            found: list[tuple[str, str]] = []
            walk.collect(top, "", segments, found)
            if not found:
                raise _PathRefused("no_match", f"no file matches {entry}")
            groups.extend(_Group(Path(src).name, False, [(src, "")]) for src, _rel in found)
            continue
        try:
            info = os.lstat(path)
        except OSError:
            info = None  # the file check below names the refusal
        if info is not None and lstat_is_name_surrogate(info):
            raise _PathRefused("path_symlink", f"{path} is a link; give the folder itself")
        if info is not None and stat.S_ISDIR(info.st_mode):
            name = os.path.basename(os.path.normpath(path)) or "folder"
            files: list[tuple[str, str]] = []
            walk.collect(path, name, None, files)
            if not files:
                raise _PathRefused("no_match", f"{entry} holds no file to send")
            # The staging disk may be case-insensitive or normalize Unicode where the
            # source is not: one copy would then replace another, or two folders merge.
            if _aliased(rel for _src, rel in files):
                raise _PathRefused(
                    "duplicate_name", f"{entry} holds two files whose names differ only by case"
                )
            groups.append(_Group(name, True, files))
            continue
        checked = walk.keep(path)
        groups.append(_Group(Path(checked).name or "upload", False, [(checked, "")]))
    # Compared without case, so the page never sees two things under one name.
    names = [g.name.casefold() for g in groups]
    if len(set(names)) != len(names):
        raise _PathRefused("duplicate_name", "two of the files or folders have the same name")
    return groups, walk.skipped


def _aliased(rels: Any) -> bool:
    """Whether two of the paths *rels*, or two of their folders, are one name once
    case and Unicode are folded."""
    seen: dict[str, str] = {}
    for rel in rels:
        parts = Path(rel).parts
        for end in range(1, len(parts) + 1):
            spelled = os.path.join(*parts[:end])
            folded = unicodedata.normalize("NFC", spelled).casefold()
            if seen.setdefault(folded, spelled) != spelled:
                return True
    return False


def _owner_only_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        # 0o700 is owner-only, the tightest mode a directory the owner reads can have.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover - Windows ACLs
        pass


def _prune(upload_dir: Path, now: float) -> None:
    """Drop staged copies past :data:`UPLOAD_KEEP_S`, then all but the newest few."""
    try:
        entries = sorted(
            (p for p in upload_dir.iterdir() if p.is_dir() and not p.is_symlink()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for index, entry in enumerate(entries):
        try:
            # shortcut: a form left unsubmitted past the 20th later upload loses its copy.
            if index >= UPLOAD_KEEP - 1 or now - entry.stat().st_mtime > UPLOAD_KEEP_S:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


@dataclass
class Staged:
    """What :func:`stage` made: the paths the browser gets, and each file's copy."""

    folder: Path
    deliver: list[Path]
    #: ``(source as given, copy, size)`` per file.
    files: list[tuple[str, Path, int]]
    has_folder: bool


def stage(
    groups: list[_Group], upload_dir: Path, *, now: float | None = None, strict: bool = False
) -> Staged:
    """Copy every file of *groups* into ONE fresh owner-only folder under *upload_dir*.

    Each group gets a folder of its own, so two names the disk treats as one (case,
    Unicode normalization, a trailing dot) never replace each other; a folder group
    keeps its layout under its own name. Every copy is ``hooks.safe_copy_file_nolink``'s,
    read through one descriptor it judged. Together the files are at most
    :data:`UPLOAD_MAX_BYTES`. Raises :class:`_PathRefused` and leaves nothing staged.
    """
    from kiro_crew import hooks

    _owner_only_dir(upload_dir)
    _prune(upload_dir, time.time() if now is None else now)
    folder = upload_dir / secrets.token_hex(8)
    _owner_only_dir(folder)
    deliver: list[Path] = []
    files: list[tuple[str, Path, int]] = []
    total = 0
    try:
        for index, group in enumerate(groups):
            own = folder / str(index)
            _owner_only_dir(own)
            for source, rel in group.files:
                target = own / (rel or group.name)
                _owner_only_dir(target.parent)
                copy = hooks.safe_copy_file_nolink(
                    source,
                    str(target.parent),
                    links_only=True,
                    # Judged again on the descriptor it opened: a parent folder
                    # swapped for a link after the walk cannot reach one.
                    refuse=_strict_hidden if strict else None,
                )
                if copy is None and strict and _strict_hidden(source):
                    raise _needs_card(source)
                if copy is None:
                    raise _PathRefused(
                        "path_refused",
                        f"{source} is a protected location, has another hard link or changed "
                        "while it was checked; it is not uploaded",
                    )
                size = os.stat(copy).st_size
                total += size
                # shortcut: the helper copies the whole file, so one that grows past the
                # cap after the lstat is refused only once copied; add a byte limit to
                # the helper if that disk use matters.
                if total > UPLOAD_MAX_BYTES:
                    raise _PathRefused(
                        "file_too_large",
                        f"the files come to more than {UPLOAD_MAX_BYTES // 2**20} MiB",
                    )
                os.replace(copy, target)
                files.append((source, target, size))
            deliver.append(own / group.name)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return Staged(folder, deliver, files, any(g.folder for g in groups))


def _entries(args: dict[str, Any]) -> list[Any] | None:
    """``path``: one entry, or a list of 1 to :data:`UPLOAD_MAX_FILES`; ``None`` otherwise.

    A list stays under ``path`` (never a key of its own) so the tool gate's path
    keystone, which reads every string under ``path`` at any depth, judges each entry
    before the card, as it judges one.
    """
    path = args.get("path")
    if isinstance(path, list):
        return path if 1 <= len(path) <= UPLOAD_MAX_FILES else None
    return [path]


#: A delivery the helper refused: the refusal the agent gets.
_DELIVERY_REFUSALS: dict[str, tuple[str, str]] = {
    "no_chooser": (
        "no_chooser",
        "the click opened no file picker; is this the right button, or a drop zone (via: drop)?",
    ),
    "not_multiple": ("not_multiple", "the file picker takes one file"),
    "frame_refused": (
        "frame_refused",
        "that point is inside a frame from another page; files go only to this page",
    ),
    "not_folder_input": (
        "not_folder_input",
        "this file field takes files, not a folder; give a glob like <folder>/* for its files",
    ),
}


async def _target(ctx: _ops.OpContext, via: str, ref: str, args: dict[str, Any]) -> dict[str, Any]:
    """What *ref* (or, for a chooser or a drop, the ``x``, ``y`` point) is for *via*:
    ``{"ok": True, ...}`` or a refusal."""
    if via != "input":
        if ref:
            return await _ops._resolve(ctx, ref)
        # A drop zone is often a plain element the snapshot gives no ref: the point,
        # in the same pixels ``click`` takes, is checked by the helper's own hit test.
        point = await _ops._point(ctx, args, "ref", "x", "y")
        if isinstance(point, dict):
            return point
        return {"ok": True, "x": point[0], "y": point[1]}
    found = await _ops._page(ctx, "fileInput", ref)
    code = found.get("code") if isinstance(found, dict) else None
    if isinstance(found, dict) and found.get("ok"):
        return found
    if code == "not_file_input":
        return _ops.refusal(
            "not_file_input",
            f"ref {ref} is not an <input type=file>; for a button that opens a file "
            "picker use via: chooser, for a drop zone via: drop",
        )
    if code in ("disabled", "not_visible"):
        return _ops.refusal(code, f"the file input {ref} is {code.replace('_', ' ')}")
    return _ops.refusal("ref_stale", f"ref {ref} is not on the current page; re-snapshot")


def _fits(via: str, found: dict[str, Any], groups: list[_Group]) -> dict[str, Any] | None:
    """Refuse a set of files the target cannot take, before anything is copied."""
    has_folder = any(g.folder for g in groups)
    if via == "drop":
        return None
    if has_folder and len(groups) > 1:
        return _ops.refusal("not_multiple", "a file field takes one folder, on its own")
    if via == "chooser":
        if has_folder:
            # A chooser is filled the way a field's own set is, which the browsers on
            # some hosts leave empty for a folder; a drop fills it on all of them.
            return _ops.refusal(
                "not_folder_input",
                "a file picker takes files here, not a folder: drop the folder on the "
                "page's drop zone (via: drop), or give a glob like <folder>/*",
            )
        return None  # the chooser's own mode is known only once it opens
    if has_folder and found.get("directory"):
        # Neither ``DOM.setFileInputFiles`` nor a drop on the field fills a folder
        # field on every build: both answered ok on some and left it empty, with no
        # change event, on others. Refused rather than reported as sent.
        return _ops.refusal(
            "folder_field_unsupported",
            "a folder field cannot be filled from a folder here: drop the folder on the "
            "page's drop zone (via: drop), or give its files as a list or a glob like "
            "<folder>/* to a field that takes several",
        )
    if has_folder:
        return _ops.refusal(*_DELIVERY_REFUSALS["not_folder_input"])
    if len(groups) > 1 and not found.get("multiple"):
        return _ops.refusal("not_multiple", "the file input takes one file")
    return None


async def _deliver(
    ctx: _ops.OpContext, via: str, ref: str, found: dict[str, Any], staged: Staged
) -> Any:
    files = [str(path) for path in staged.deliver]
    if via == "chooser":
        return await ctx.helper("chooser_files", x=found["x"], y=found["y"], files=files)
    if via == "drop":
        return await ctx.helper("drop_files", x=found["x"], y=found["y"], files=files)
    return await ctx.helper(
        "set_files",
        expression=f"{_ops.WALKER_SOURCE}.fileInput({json.dumps(ref)}, true)",
        files=files,
    )


async def run(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """Give the page checked copies of the files *path* names, by way of *via*."""
    via = args.get("via", "input")
    if via not in VIAS:
        return _ops.refusal("bad_via", f"`via` is one of {', '.join(VIAS)}")
    raw_ref = args.get("ref")
    ref = ""
    if raw_ref is not None or via == "input":
        if not _ops._is_ref(raw_ref):
            return _ops.refusal(
                "bad_ref",
                f"not a valid ref: {raw_ref!r}; call snapshot first (a chooser or a drop "
                "also takes `x`,`y` pixels instead)",
            )
        ref = str(raw_ref)
        if split_frame_ref(ref) is not None:
            return _ops.refusal("bad_ref", "upload into a cross-origin frame is not supported")
    entries = _entries(args)
    if entries is None:
        return _ops.refusal(
            "bad_path", f"`path` is one absolute path or a list of 1 to {UPLOAD_MAX_FILES}"
        )
    if ctx.upload_dir is None:
        return _ops.refusal("not_available", "upload is not available on this gateway")
    found = await _target(ctx, via, ref, args)
    if not found.get("ok"):
        return found
    where = ref or f"({found['x']:g}, {found['y']:g})"
    if via == "input" and len(entries) > 1 and not found.get("multiple"):
        # Before any file is read: the field's own answer decides it.
        return _ops.refusal("not_multiple", "the file input takes one file")
    shown = [p if isinstance(p, str) else "" for p in entries]
    upload_dir = ctx.upload_dir
    try:
        groups, skipped = await asyncio.to_thread(
            expand, entries, strict=bool(ctx.upload_grant_host), read_allowed=ctx.read_allowed
        )
    except _PathRefused as exc:
        for path in shown:
            _audit(path, None, exc.code)
        return _ops.refusal(exc.code, exc.error)
    unfit = _fits(via, found, groups)
    if unfit is not None:
        for path in shown:
            _audit(path, None, str(unfit.get("code")))
        return unfit
    try:
        staged = await asyncio.to_thread(
            stage, groups, upload_dir, strict=bool(ctx.upload_grant_host)
        )
    except _PathRefused as exc:
        for path in shown:
            _audit(path, None, exc.code)
        return _ops.refusal(exc.code, exc.error)
    if ctx.upload_grant_host:
        # Staging takes a while; a chooser or a drop acts on whatever page is there.
        moved = await browser_host_trust.recheck(ctx.upload_grant_host, "upload", args, ctx.helper)
        if moved is not None:
            await asyncio.to_thread(_drop_staged, staged.folder)
            _audit_all(staged, str(moved.get("code")))
            return moved
    names = ", ".join(path.name for path in staged.deliver)
    await _ops._step(ctx, f"Upload {names} to {where}"[:120])
    try:
        result = await _deliver(ctx, via, ref, found, staged)
    except BaseException:
        # Off the loop and not awaited: a folder tree can be large, and a cancelled
        # op must still unwind at once.
        asyncio.get_running_loop().run_in_executor(None, _drop_staged, staged.folder)
        _audit_all(staged, "browser_error")
        raise
    if not isinstance(result, dict) or not result.get("ok"):
        await asyncio.to_thread(_drop_staged, staged.folder)
        code = result.get("code") if isinstance(result, dict) else None
        code, error = _DELIVERY_REFUSALS.get(
            str(code), ("ref_stale", f"{where} is not on the current page; re-snapshot")
        )
        _audit_all(staged, code)
        return _ops.refusal(code, error)
    _audit_all(staged, "ok")
    out: dict[str, Any] = (
        {"ok": True, "ref": ref} if ref else {"ok": True, "at": [found["x"], found["y"]]}
    )
    if len(staged.files) == 1 and not staged.has_folder:
        out.update(file=staged.files[0][1].name, size=staged.files[0][2])
    else:
        # Each copy sits under its group's own numbered folder; the page sees the rest.
        out["files"] = [
            {"file": Path(*copy.relative_to(staged.folder).parts[1:]).as_posix(), "size": size}
            for _source, copy, size in staged.files
        ]
    if skipped:
        out["skipped"] = skipped
    return out


def _drop_staged(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def _audit_all(staged: Staged, outcome: str) -> None:
    for source, _copy, size in staged.files:
        _audit(source, size, outcome)
