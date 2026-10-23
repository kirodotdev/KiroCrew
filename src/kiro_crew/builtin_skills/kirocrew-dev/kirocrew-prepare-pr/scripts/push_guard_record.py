"""Which commits push_guard.py built, and which of HEAD's paths nothing vouches for.

It keeps the build record (one file per commit the guard made, under git's common
directory), reads a branch's past tips and the paths another author published on it, and
derives the paths HEAD changes that no record, publication or ``--`` path covers. New
work on what counts as a path the guard may push goes here.

Loaded by ``push_guard.py``, which rebinds every function defined here onto its own
globals, so the code runs as if it were written there and a name a test replaces on
``push_guard.py`` reaches it. This module therefore defines functions only: no module
state, no class, and nothing its functions read at import time.
"""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from push_guard import (
        _BUILT_DIR,
        _BUILT_MAX_AGE_S,
        _REFLOG_TIPS,
        _RESET_ENTRIES,
        _STAGED_LIST_MAX_AGE_S,
        Refused,
        _ask,
        _changed,
        _follow_renames,
        _ident,
        _own_diff,
        _remote_ref,
        _unlink,
        _write_git_file,
        err,
        run,
    )


def _common_dir(git_dir):
    """The repository's common git dir: shared by all its worktrees."""
    try:
        with open(os.path.join(git_dir, "commondir"), encoding="utf-8") as handle:
            relative = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return git_dir
    return os.path.normpath(os.path.join(git_dir, relative)) if relative else git_dir


def _built_dir(git_dir):
    return os.path.join(_common_dir(git_dir), _BUILT_DIR)


def _is_sha(text):
    return re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text) is not None


def record_built(git_dir, sha, paths):
    """Record ``paths`` (top-relative bytes) as what the guard committed in ``sha``.

    One file per commit, written whole and moved into place, so a concurrent
    writer can never drop another's entry.  Entries past _BUILT_MAX_AGE_S are
    pruned on the way.  Refuses (never a traceback) when it cannot write.
    """
    directory = _built_dir(git_dir)
    if not _is_sha(sha):
        err("REFUSED: {!r} is not a commit id the record can hold.".format(sha))
        raise Refused()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        err("REFUSED: cannot write {}: {}".format(directory, exc))
        raise Refused()
    try:
        for name in os.listdir(directory):
            entry = os.path.join(directory, name)
            age = time.time() - os.path.getmtime(entry)
            if age > (_STAGED_LIST_MAX_AGE_S if name.endswith(".tmp") else _BUILT_MAX_AGE_S):
                _unlink(entry)
    except OSError:
        pass
    path = os.path.join(directory, sha)
    tmp = "{}.{}-{}.tmp".format(path, os.getpid(), time.time_ns())
    _write_git_file(tmp, b"".join(p + b"\0" for p in sorted(set(paths) | _built(path))))
    try:
        os.replace(tmp, path)
    except OSError as exc:
        _unlink(tmp)
        err("REFUSED: cannot write {}: {}".format(path, exc))
        raise Refused()


def _built(path):
    """The paths one record file holds; empty when there is none."""
    try:
        with open(path, "rb") as handle:
            return {p for p in handle.read().split(b"\0") if p}
    except OSError:
        return set()


def _branch_tips(ref):
    """The past tips of the branch ``ref`` (its reflog), newest first.

    The walk stops at the entry that last created or reset the name (``git
    switch -C``, ``checkout -B``, ``branch -f`` and ``git reset`` keep the
    reflog and write one), so a reused name inherits nothing from what it
    named before.  A detached HEAD has no branch reflog: no tips.
    """
    if not ref.startswith("refs/heads/"):
        return []
    out = _ask(
        [
            "git",
            "log",
            "--walk-reflogs",
            "--no-show-signature",
            "--format=%H%x00%gs",
            "--max-count={}".format(_REFLOG_TIPS),
            ref,
            "--",
        ]
    )[1]
    tips = []
    for line in out.split("\n"):
        sha, _, subject = line.partition("\0")
        if not _is_sha(sha):
            continue
        tips.append(sha)
        if subject.startswith(_RESET_ENTRIES):
            break
    return list(dict.fromkeys(tips))


def _local_email():
    """The email of the identity this checkout commits as, or None when git has none."""
    rc, out, _ = run(["git", "var", "GIT_AUTHOR_IDENT"])
    if rc != 0 or not out:
        return None
    return _ident(out).rpartition("<")[2].rstrip(">").strip().lower()


def _published(ref, pinned, head, tips):
    """Paths another author's commit on the pushed branch changes that ``head`` holds as pushed.

    Those came from a commit already on the pull request -- a maintainer's or
    a co-author's -- not from this checkout's index.  The pushed branch
    (origin/<branch>) counts only when it is in this branch's lineage
    (reachable from ``head`` or one of its ``tips``), so a stale remote ref of
    an earlier branch with the same name counts for nothing.  Only commits
    whose author is not this checkout's identity count, so a hand commit of
    a stale copy pushed once without the guard is never published.  Only the
    branch's own changes count (against its merge-base with ``pinned``), so a
    pushed copy of an older base can never make a stale path look published.
    """
    if not ref.startswith("refs/heads/"):
        return set()
    remote = _remote_ref(ref[len("refs/heads/") :])
    rc, sha = _ask(
        ["git", "rev-parse", "--verify", "--quiet", remote + "^{commit}"], answers=(0, 1)
    )
    if rc != 0 or not sha:
        return set()
    lineage = "".join("^{}\n".format(t) for t in [head, *tips]).encode("ascii")
    if _ask(["git", "rev-list", "--max-count=1", sha, "--stdin"], input=lineage)[1]:
        return set()
    rc, mbase = _ask(["git", "merge-base", sha, pinned], answers=(0, 1))
    mine = _local_email()
    if rc != 0 or not mbase or mine is None:
        return set()
    out = _ask(
        [
            "git",
            "rev-list",
            "--format=%ae",
            "--max-count={}".format(_REFLOG_TIPS + 1),
            "{}..{}".format(mbase, sha),
            "--",
        ]
    )[1]
    # rev-list --format prints a "commit <sha>" line, then the format.
    lines = out.split("\n")
    authored = list(zip(lines[0::2], lines[1::2]))
    if len(authored) > _REFLOG_TIPS:
        return set()
    theirs: set = set()
    for header, email in authored:
        if email.strip().lower() != mine:
            theirs |= _own_diff(header.split(" ", 1)[1])
    return theirs & (_changed(mbase, sha) - _changed(sha, head))


def _unrecorded(git_dir, ref, paths, commits, pinned=None, head=None, vouched=()):
    """The ``paths`` nothing accounts for, sorted.

    A path is accounted for when it is named in ``vouched``; when the guard
    recorded it for one of ``commits`` (the branch's own: what HEAD carries);
    when it recorded it for a past tip of the branch ``ref`` (_branch_tips:
    what a rebase or squash rewrote), followed through the renames the base
    made since, given ``pinned``; or, given ``head`` (a commit), when it is
    published on the pushed branch (_published).  A detached HEAD has no
    branch reflog, so only its own commits count; a deleted, re-created or
    reset branch name has no tips from before, so nothing recorded for what
    it named before carries over.
    """
    unknown = set(paths) - set(vouched)
    if not unknown:
        return []
    directory = _built_dir(git_dir)
    try:
        names = set(os.listdir(directory))
    except OSError:
        names = set()
    commits = set(commits)
    for sha in commits:
        if unknown and sha in names:
            unknown -= _built(os.path.join(directory, sha))
    tips = _branch_tips(ref) if unknown else []
    past = []
    for sha in tips:
        if unknown and sha in names and sha not in commits:
            own = _built(os.path.join(directory, sha))
            unknown -= own
            past.append((sha, own))
    if unknown and pinned and past:
        by_base: dict = {}
        for sha, own in past:
            rc, mbase = _ask(["git", "merge-base", sha, pinned], answers=(0, 1))
            if rc == 0 and mbase and mbase != pinned:
                by_base.setdefault(mbase, set()).update(own)
        for mbase, own in by_base.items():
            if unknown:
                unknown -= _follow_renames(own, mbase, pinned)
    if unknown and pinned and head:
        unknown -= _published(ref, pinned, head, tips)
    return sorted(unknown)
