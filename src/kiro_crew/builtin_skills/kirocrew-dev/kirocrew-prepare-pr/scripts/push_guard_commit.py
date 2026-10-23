"""``--commit``/``--amend``: a commit of exactly the named paths.

It reads a path as named from the current directory or as ``:(top,literal)<path>``, builds a
private index from HEAD, commits through git with the caller's hooks and signing,
verifies what landed (parents, author, the paths swept in), takes a wrong commit back
by compare-and-swap, and levels the shared index. New work on committing by name goes
here.

Loaded by ``push_guard.py``, which rebinds every function defined here onto its own
globals, so the code runs as if it were written there and a name a test replaces on
``push_guard.py`` reaches it. This module therefore defines functions only: no module
state, no class, and nothing its functions read at import time.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from push_guard import (
        _LISTED_PATHS,
        _TOP_LITERAL,
        EXIT_REFUSED,
        Refused,
        UsageError,
        _ask,
        _changed,
        _commit_info,
        _empty_tree,
        _git_paths,
        _head_ref,
        _ident,
        _indented,
        _interactive,
        _local_base,
        _move_ref,
        _outcome,
        _own_diff,
        _refuse_unrecorded,
        _sha,
        _tail,
        _top,
        _unlink,
        _write_git_file,
        err,
        run,
    )
    from push_guard_index import _check_index_for_commit, _report_staged, _staged
    from push_guard_record import _unrecorded, record_built


def _top_relative(path, top):
    """``path`` as the caller named it, relative to ``top`` (real paths both), or None.

    Only the parent directories are resolved: the named entry itself may be a
    tracked symlink.  None when it lies outside ``top`` (or on another drive).
    """
    absolute = os.path.abspath(path)
    parent, name = os.path.split(absolute)
    real = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(absolute)
    try:
        rel = os.path.relpath(real, top)
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _named_paths(paths, top):
    """{top-relative bytes: the name as given}, or raises UsageError for a path outside.

    A name is a path from this directory, or ``:(top,literal)<path>`` from the
    top: the form refusals print, so a listed path is named back as printed.
    """
    named = {}
    for path in paths:
        given = path
        if path.startswith(_TOP_LITERAL):
            rel = _literal_relative(path[len(_TOP_LITERAL) :], given)
        else:
            rel = _top_relative(path, top)
        if rel is None:
            err("ERROR: {!r} is not inside this repository ({}).".format(given, top))
            raise UsageError()
        named[os.fsencode(rel)] = given
    return named


def _literal_relative(rest, given):
    """The top-relative path a ``:(top,literal)`` name spells, or None when it leaves the top.

    Read as written, never resolved: it is already top-relative, so a parent
    that is now a symlink in the worktree still names the path git lists.
    Raises UsageError for a name that names nothing.
    """
    if not rest.strip():
        err("ERROR: {!r} names nothing; name each path.".format(given))
        raise UsageError()
    if os.path.isabs(rest) or os.path.splitdrive(rest)[0]:
        return None
    text = rest.replace(os.sep, posixpath.sep)
    if os.pardir in text.split(posixpath.sep):
        return None
    return posixpath.normpath(text)


def _index_entries():
    """{top-relative path bytes: (mode, sha, stage) of each entry} in the shared index.

    ``--full-name``: without it ls-files names entries relative to this
    directory, and a lookup by top-relative path would miss from a subdirectory.
    """
    out = _ask(["git", "ls-files", "-s", "-z", "--full-name", "--", ":(top)"], raw=True)[1]
    entries: dict[bytes, tuple] = {}
    for entry in out.split(b"\0"):
        meta, tab, name = entry.partition(b"\t")
        if tab:
            entries[name] = entries.get(name, ()) + (tuple(meta.decode("ascii").split(" ")),)
    return entries


def _worktree_matches(rel, top, entry):
    """True when the worktree copy of ``rel`` holds exactly the staged content."""
    mode, sha = entry
    path = os.path.join(top, os.fsdecode(rel))
    if mode == "160000":
        # A gitlink: a checked-out submodule shows its commit; one that is not
        # checked out shows none, so the staged commit is the only copy.
        if not os.path.exists(os.path.join(path, ".git")):
            return True
        rc, out = _ask(["git", "-C", path, "rev-parse", "--verify", "-q", "HEAD"], answers=(0, 1))
        return rc == 0 and out == sha
    try:
        if mode == "120000":
            target = os.readlink(path)
            data = os.fsencode(target) if isinstance(target, str) else target
            return _ask(["git", "hash-object", "--stdin"], input=data)[1] == sha
        if os.path.islink(path) or not os.path.isfile(path):
            return False
    except OSError:
        return False
    return _ask(["git", "hash-object", "--", path])[1] == sha


def _private_index(git_dir, rev, env):
    """Seed the index at ``env``'s GIT_INDEX_FILE with ``rev``'s tree.

    A copy of the shared index first, so its stat data spares re-hashing the
    worktree; ``read-tree --reset`` then makes the entries ``rev``'s and keeps
    the stat data only where the content is the same.
    """
    try:
        # copy2, never copyfile: the index's own mtime is how git tells a
        # racily clean entry (a file edited in the second it was staged) from
        # a clean one, and a fresh mtime would make it trust the stale entry.
        shutil.copy2(_git_paths("index")[0], env["GIT_INDEX_FILE"])
    except OSError:
        pass
    _ask(["git", "read-tree", "--reset", rev], env=env)


def _commit_by_name(paths, message, message_file, amend, git_dir, base_arg=""):
    """Commit (or amend) exactly the named paths; return 0 or an exit code.

    Refuses before touching anything when an operation is mid-way, when any
    entry is unmerged, or when anything is staged that is not literally one
    of the named paths (a directory does not name the entries under it).
    The commit is built in a private index seeded from HEAD: each named
    path's worktree copy is added (``-f`` only for a file, never for a
    directory), except that a staged change the worktree cannot show
    (``git rm --cached``, ``update-index --chmod``) is kept.  ``git commit``
    commits that index, hooks and signing included; the shared index changes
    only after it lands, for the committed paths alone, and only where no one
    staged anything since this run read it.  The commit landed when git says
    so AND HEAD is what was built: its parents, its author, and its tree up to
    the named paths (a hook may reformat those).  Anything else is taken back
    with a compare-and-swap.  Only a landed commit is recorded, so a commit a
    hook rejected leaves no path recorded.
    """
    rc = _check_index_for_commit()
    if rc:
        return rc
    top = os.path.realpath(_top())
    named = _named_paths(paths, top)
    # Read first: the sync after landing levels only entries still as read here.
    before = _index_entries()
    staged = {p: s for s, p in _staged()}
    unnamed = [(s, p) for p, s in staged.items() if p not in named]
    if unnamed:
        return _report_staged(unnamed, git_dir, lead="not among the paths you named, ")
    branch = _head_ref()
    pre = _sha("HEAD")
    pre_tree, pre_parents, pre_author = _commit_info(pre)
    # The paths this commit changes are diffed against its parent: HEAD or, for an
    # amend, HEAD's first parent (the empty tree when HEAD is a root commit).
    if not amend:
        diff_base = pre
    elif pre_parents:
        diff_base = pre_parents[0]
    else:
        diff_base = _empty_tree()
    if amend:
        # The base as last fetched (an amend fetches nothing), so renames the
        # base made and published paths count exactly as in the push check.
        base, pinned = _local_base(base_arg)
        unknown = _unrecorded(git_dir, branch, _own_diff(pre), [pre], pinned, pre, named)
        if unknown:
            _refuse_unrecorded(unknown, "HEAD ({})".format(pre[:12]), git_dir, base, "amend")
    author = pre_author if amend else _ident(_ask(["git", "var", "GIT_AUTHOR_IDENT"])[1])

    tag = "{}-{}".format(os.getpid(), time.time_ns())
    env = {"GIT_INDEX_FILE": os.path.join(git_dir, "push_guard-index-" + tag)}
    msg_path = os.path.join(git_dir, "push_guard-message-{}.txt".format(tag))
    try:
        _private_index(git_dir, pre, env)
        files: list[str] = []
        dirs: list[str] = []
        for rel in named:
            spec = _TOP_LITERAL + os.fsdecode(rel)
            letter = staged.get(rel)
            if letter == b"D":
                _ask(["git", "rm", "-q", "--cached", "--ignore-unmatch", "--", spec], env=env)
                continue
            entry = before[rel][0][:2] if letter is not None and rel in before else None
            if entry is not None and _worktree_matches(rel, top, entry):
                info = "{} {}\t".format(*entry).encode("ascii") + rel + b"\0"
                _ask(["git", "update-index", "-z", "--index-info"], env=env, input=info)
                continue
            on_disk = os.path.join(top, os.fsdecode(rel))
            is_dir = os.path.isdir(on_disk) and not os.path.islink(on_disk)
            (dirs if is_dir else files).append(spec)
        if files:
            _ask(["git", "add", "-A", "-f", "--", *files], env=env)
        if dirs:
            _ask(["git", "add", "-A", "--", *dirs], env=env)
        built = _ask(["git", "write-tree"], env=env)[1]
        if built == pre_tree and not amend:
            err("REFUSED: the named paths hold no change against HEAD; nothing was committed.")
            return EXIT_REFUSED

        argv = ["git", "commit", "--quiet"] + (["--amend"] if amend else [])
        if message_file is not None:
            argv += ["-F", os.path.abspath(message_file)]
        elif message is not None:
            _write_git_file(msg_path, message + b"\n")
            argv += ["-F", msg_path]
        else:
            argv.append("--no-edit")
        res = _interactive(argv, env)
    finally:
        _unlink(env["GIT_INDEX_FILE"])
        _unlink(env["GIT_INDEX_FILE"] + ".lock")
        _unlink(msg_path)

    head = _sha("HEAD")
    tree, parents, head_author = _commit_info(head)
    want = pre_parents if amend else [pre]
    if res.timed_out or res.rc != 0:
        if head == pre:
            err(
                "REFUSED: git commit {} — nothing was committed. {}".format(
                    "timed out" if res.timed_out else "failed (exit {})".format(res.rc),
                    _tail(res.out.decode("utf-8", "replace")) or "Its output is above.",
                )
            )
        else:
            err(
                "REFUSED: git commit reported {} but HEAD moved to {}; read git show "
                "{} before anything else.".format(_outcome(res), head[:12], head[:12])
            )
        return EXIT_REFUSED
    swept = _outside(built, tree, named)
    if parents != want or head_author != author or swept:
        _take_back(
            head, built, named, swept, pre=pre, parents=parents, placed=parents == want, amend=amend
        )
        return EXIT_REFUSED
    try:
        record_built(git_dir, head, _changed(diff_base, tree))
    except Refused:
        err(
            "WARNING: the commit landed but is not recorded, so the push check will "
            "list its paths; name them after -- there."
        )
    sync = _changed(pre_tree, tree) | set(named)
    # Staged by someone else since this run read the index: theirs, left alone.
    now = _index_entries() if sync else {}
    restaged = sorted(p for p in sync if before.get(p) != now.get(p))
    sync -= set(restaged)
    # Nothing to level (an amend of the message alone): an empty pathspec list
    # would make the reset below a full reset of the shared index.
    rc, stderr = 0, ""
    if sync:
        listed = b"".join(os.fsencode(_TOP_LITERAL) + p + b"\0" for p in sorted(sync))
        rc, _, stderr = run(
            ["git", "reset", "-q", head, "--pathspec-from-file=-", "--pathspec-file-nul"],
            input=listed,
        )
    if restaged:
        err(
            "WARNING: the commit landed; these paths were staged again while it "
            "ran, so the index keeps that:\n{}".format(_indented(restaged))
        )
    if rc != 0:
        err(
            "WARNING: the commit landed, but the index still shows the committed "
            "paths as before ({}); git reset -q -- <path> brings them level.".format(
                _tail(stderr) if rc is not None else "timed out"
            )
        )
    print("HEAD:            " + head[:12])
    return 0


def _covered(path, named):
    """True when ``path`` is a named path or lies under a named directory."""
    return any(n in (path, b".") or path.startswith(n + b"/") for n in named)


def _outside(old, new, named):
    """Paths (sorted bytes) that differ between two trees and are not named."""
    return sorted(p for p in _changed(old, new) if not _covered(p, named))


def _take_back(head, built, named, swept, *, pre, parents, placed, amend):
    """Refuse a commit that is not the one built; move HEAD back over it when that is safe.

    ``pre`` is HEAD as this run read it, ``parents`` the commit's own and
    ``placed`` whether they are the ones it was built for.  Where HEAD was is
    the reflog's previous entry, and only two shapes are unambiguous: HEAD
    sits where it was built and that entry is ``pre``; or git built on (or,
    for an amend, replaced) a commit that landed in between, which that entry
    is, holding a change outside the named paths.  A commit a hook stacked
    on top, or rewrote again, is left for the reader.  The move is a
    compare-and-swap from ``head``, so a commit that landed after it is never
    undone.
    """
    rc, prev = _ask(["git", "rev-parse", "--verify", "-q", "HEAD@{1}"], answers=(0, 1))
    if rc != 0 or (placed and prev != pre):
        prev = ""
    elif not placed:
        prev_tree, prev_parents, _ = _commit_info(prev)
        follows = prev_parents == parents if amend else parents[:1] == [prev]
        if not follows or not _outside(prev_tree, built, named):
            prev = ""
    if swept:
        why = "a hook added paths you did not name:\n" + _indented(swept[:_LISTED_PATHS])
    else:
        why = "its parents or author differ: another commit landed at the same time, or a hook rewrote it"
    reason = "push_guard: take back a commit that is not the one built"
    if prev and _move_ref("HEAD", prev, head, reason) == prev:
        err(
            "REFUSED: the commit git made ({}) is not the one that was built ({}).\n"
            "  It was taken back: HEAD is {} again, and the index and worktree are "
            "as they were. git show {} shows what it held; re-run once the cause "
            "is gone.".format(head[:12], why, prev[:12], head[:12])
        )
        return
    err(
        "REFUSED: HEAD ({}) is not the commit that was built ({}). Read git show {} "
        "before anything else.".format(head[:12], why, head[:12])
    )
