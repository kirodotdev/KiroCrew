"""Read-only git bodies for the workspace tool server's diff view.

The two sides of one changed file, their modes and git's own text-or-binary
verdict, read through the fixed, hook-free ``run`` prefix that
:func:`workspace_tools._git_read` builds. Stdlib only and free of
module state: it ships beside ``workspace_tools.py`` to every guest and to a
connected computer, and is loaded from that file's own folder
(``workspace_tools._beside``), never from ``sys.path``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

if TYPE_CHECKING:
    from kiro_crew.remote_session.workspace_tools import Workspace


def _git_bool(run: Callable, key: str, default: bool) -> bool:
    """A boolean config value as git reads it; ``default`` when it is unset."""
    try:
        return run(["config", "--bool", "--get", key]).strip() == b"true"
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        return default


def _git_index_entry(run: Callable, target: str, rel: str) -> Optional[Tuple[str, str]]:
    """The mode and object id the index records for ``rel`` (stage 0), if any."""
    for row in run(["ls-files", "--stage", "--full-name", "-z", "--", target]).split(b"\0"):
        meta, _, name = row.partition(b"\t")
        fields = meta.split(b" ")
        if name == os.fsencode(rel) and len(fields) == 3 and fields[2] == b"0":
            return fields[0].decode("ascii", "replace"), fields[1].decode("ascii", "replace")
    return None


def _git_index_mode(run: Callable, target: str, rel: str) -> Optional[str]:
    """The mode the index records for the file at ``rel`` (stage 0), if any."""
    entry = _git_index_entry(run, target, rel)
    return entry[0] if entry else None


def _worktree_mode(st_mode: int) -> str:
    """Git's mode for a worktree entry, from ``lstat`` alone."""
    if stat.S_ISLNK(st_mode):
        return "120000"
    # Git records 100755 only for the owner's execute bit (ce_permissions).
    return "100755" if st_mode & stat.S_IXUSR else "100644"


# Control bytes git's gather_stats counts as non-printable: below 32 except
# BS, TAB, ESC and FF (CR and LF are counted apart), and DEL.
_GIT_NONPRINTABLE = tuple(bytes([c]) for c in (*range(32), 127) if c not in (8, 9, 10, 12, 13, 27))


def _git_auto_text(body: bytes) -> bool:
    """Whether git's automatic conversion would treat ``body`` as text.

    Mirrors convert.c's ``convert_is_binary``: a lone CR, a NUL, or more than
    one non-printable byte per 128 printable ones makes git leave the bytes as
    they are, so their CRLFs must not be folded either. A trailing Ctrl-Z
    (DOS end of file) is not counted as non-printable.
    """
    lone_cr = body.count(b"\r") - body.count(b"\r\n")
    nonprintable = sum(body.count(c) for c in _GIT_NONPRINTABLE)
    if body.endswith(b"\x1a"):
        nonprintable -= 1
    printable = len(body) - nonprintable - body.count(b"\r") - body.count(b"\n")
    if body.endswith(b"\x1a"):
        printable -= 1
    return not lone_cr and b"\0" not in body and (printable >> 7) >= nonprintable


def _git_bodies(
    ws: "Workspace", path: str, run: Callable, source: Optional[str] = None, *, limit: int
) -> Dict[str, Any]:
    """Read the two bodies and modes, never a patch of unmasked content.

    ``run`` supplies the fixed git prefix and checks every exit status. Only
    the parent is resolved: a symlink's body is its target text, not its referent.
    ``source`` is a rename's old path: the HEAD side is read there, confined the
    same way, and named in ``old_path``. When the two bodies together exceed
    ``limit`` bytes the reply is ``truncated`` and carries no body at all.
    """
    repo_root = os.fsdecode(run(["rev-parse", "--show-toplevel"])).rstrip("\r\n")

    def locate(name_in_repo: str) -> "tuple[str, str]":
        found = os.path.normpath(os.path.join(repo_root, name_in_repo))
        if found == ws.root:
            raise ValueError("a file path is required, not a directory")
        parent, leaf = os.path.split(found)
        found = os.path.join(ws.resolve(parent, must_exist=False), leaf)
        return found, os.path.relpath(found, repo_root).replace(os.sep, "/")

    target, rel = locate(path)
    head = "HEAD"
    try:
        entry = run(["ls-tree", "-z", head, "--", target])
    except subprocess.CalledProcessError:
        # Only an unborn branch has an absent baseline; corrupt objects fail.
        try:
            run(["log", "-1", "--format=%h"])
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 128 or not re.fullmatch(
                rb"fatal: your current branch '[^\r\n]+' does not have any commits yet\n?",
                exc.stderr,
            ):
                raise
        else:
            raise
        head = run(["hash-object", "-t", "tree", "--stdin"]).decode("ascii").strip()
        entry = b""
    old_rel = rel
    if source is not None and head == "HEAD":
        source_path, source_rel = locate(source)
        if source_rel != rel:
            source_entry = run(["ls-tree", "-z", head, "--", source_path])
            # A source HEAD does not hold is no rename; the target's own entry stands.
            if source_entry:
                entry, old_rel = source_entry, source_rel
    mode, sha = "", ""
    if entry:
        mode, _kind, sha = entry.split(b"\t", 1)[0].decode("ascii").split()
        if mode == "040000":
            raise ValueError("a file path is required, not a directory")
    old = None
    if sha:
        old = (
            f"Subproject commit {sha}\n".encode()
            if mode == "160000"
            else run(["cat-file", "blob", sha])[: limit + 1]
        )
    try:
        disk_mode = os.lstat(target).st_mode
    except FileNotFoundError:
        disk_mode = 0
    new = None
    new_mode: Optional[str] = None
    # A gitlink replaced by a file or symlink is read below with the bounded
    # worktree read; git's own diff of it would be buffered whole.
    if stat.S_ISDIR(disk_mode) or (mode == "160000" and not disk_mode):
        index = run(["ls-files", "--stage", "--full-name", "-z", "--", target])
        rows = [row for row in index.split(b"\0") if row]
        indexed_link = any(
            row.startswith(b"160000 ") and row.split(b"\t", 1)[-1] == os.fsencode(rel)
            for row in rows
        )
        # A gitlink the index replaced with files under the path is a directory
        # of files now: refuse it before git builds that subtree's whole patch.
        if not indexed_link and (mode != "160000" or rows):
            raise ValueError("a file path is required, not a directory")
        summary = run(
            [
                "diff",
                "--no-color",
                "--submodule=short",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=dirty",
                head,
                "--",
                target,
            ]
        )
        # The raw format uses an all-zero new id for an unstaged gitlink.
        # The short format names its commit without running inside the submodule;
        # only that synthetic commit line is retained, never the patch itself.
        match = re.search(rb"^\+Subproject commit ([0-9a-f]{40}(?:[0-9a-f]{24})?)$", summary, re.M)
        if match:
            new = b"Subproject commit " + match[1] + b"\n"
        elif not summary:
            new = old
        elif disk_mode and stat.S_ISDIR(disk_mode):
            raise ValueError("git did not report the submodule commit")
        if new is not None:
            new_mode = "160000"
    if stat.S_ISLNK(disk_mode):
        new = os.fsencode(os.readlink(target))
        new_mode = _worktree_mode(disk_mode)
    elif stat.S_ISREG(disk_mode):
        # Resolve again on hosts without O_NOFOLLOW; guest workspaces are POSIX.
        read_path = target if hasattr(os, "O_NOFOLLOW") else ws.resolve(target)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(read_path, flags)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("a regular file or symlink is required")
            new = handle.read(limit + 1)
        new_mode = _worktree_mode(disk_mode)
        filemode = _git_bool(run, "core.filemode", True)
        symlinks = _git_bool(run, "core.symlinks", True)
        if not filemode or not symlinks:
            # Where the filesystem cannot carry a mode, git takes the one the
            # index records (ce_mode_from_stat), so a staged chmod still shows;
            # a path the index does not hold keeps HEAD's.
            recorded = _git_index_mode(run, target, rel) or mode
            if recorded == "120000" and not symlinks:
                new_mode = recorded
            elif recorded in ("100644", "100755") and not filemode:
                new_mode = recorded
    elif disk_mode and not stat.S_ISDIR(disk_mode):
        raise ValueError("a regular file or symlink is required")
    if old is None and new is None:
        raise ValueError("the path is neither in HEAD nor a worktree file")
    truncated = len(old or b"") + len(new or b"") > limit
    result: Dict[str, Any] = {
        "root": ws.root,
        "git": True,
        "path": rel,
        "truncated": truncated,
        "old_mode": mode if old is not None else None,
        "new_mode": new_mode if new is not None else None,
    }
    if old_rel != rel:
        result["old_path"] = old_rel
    if any(b"\0" in body[:8000] for body in (old, new) if body is not None):
        if old == new:
            # Identical bytes are no content change: only a rename or mode
            # header can differ, so the bodies go as equal and empty.
            return {**result, "old": "", "new": ""}
        return {**result, "binary": True}
    if truncated:
        # Over the cap no body is sent: a partial body cannot give a true diff.
        return result
    if new is not None and stat.S_ISREG(disk_mode) and b"\r\n" in new:
        fields = run(["check-attr", "-z", "text", "eol", "--", target]).split(b"\0")
        attrs = dict(zip(fields[1::3], fields[2::3]))
        text, eol = attrs.get(b"text"), attrs.get(b"eol")
        convert = text == b"set"

        def crlf_in_index() -> bool:
            # git's has_crlf_in_index: automatic conversion leaves a file whose
            # INDEX blob is text holding CRLF unchanged; explicit text does not.
            # HEAD stays the old side. A staged renormalization makes the two
            # differ, and only then is the index blob read.
            staged = _git_index_entry(run, target, rel)
            if staged is None or staged[0] == "160000":
                return False
            blob = old if staged[1] == sha else run(["cat-file", "blob", staged[1]])
            return blob is not None and b"\r\n" in blob and _git_auto_text(blob)

        if text != b"unset":
            if text == b"auto":
                convert = _git_auto_text(new) and not crlf_in_index()
            elif eol in (b"lf", b"crlf"):
                convert = True
            elif not convert:
                try:
                    autocrlf = run(["config", "--get", "core.autocrlf"]).strip().lower()
                except subprocess.CalledProcessError as exc:
                    if exc.returncode != 1:
                        raise
                    autocrlf = b"false"
                convert = (
                    autocrlf in (b"true", b"input") and _git_auto_text(new) and not crlf_in_index()
                )
        if convert:
            new = new.replace(b"\r\n", b"\n")
    try:
        return {
            **result,
            "old": old.decode("utf-8") if old is not None else None,
            "new": new.decode("utf-8") if new is not None else None,
        }
    except UnicodeDecodeError:
        if old == new:
            # Identical bytes stay identical text: only a mode line can differ.
            body = old.decode("utf-8", "replace") if old is not None else None
            return {**result, "old": body, "new": body}
        # Replacement characters would make different non-UTF-8 bytes read
        # as equal text and hide the change, so such a body is not text here.
        return {**result, "binary": True}
