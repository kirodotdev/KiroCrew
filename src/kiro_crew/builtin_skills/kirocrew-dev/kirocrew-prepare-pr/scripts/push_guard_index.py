"""What this checkout's index and working tree hold before the guard acts.

It detects a git operation in progress (rebase, am, merge, cherry-pick, revert, a
sequencer), lists staged and unmerged paths, prints the refusal for a path someone else
staged here, answers ``--check-index``, and checks the state ``--commit``/``--amend``
need. New work on reading the shared index goes here.

Loaded by ``push_guard.py``, which rebinds every function defined here onto its own
globals, so the code runs as if it were written there and a name a test replaces on
``push_guard.py`` reaches it. This module therefore defines functions only: no module
state, no class, and nothing its functions read at import time.
"""

from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from push_guard import (
        _CONSUMED_HEADS,
        _LISTED_PATHS,
        _TODO_HEAD_CHARS,
        EXIT_FOREIGN_INDEX,
        _ask,
        _changed,
        _commit_info,
        _display,
        _empty_tree,
        _follow_renames,
        _git_paths,
        _indented,
        _top,
        _write_list,
        err,
    )


def _sequencer_op(sequencer):
    """The operation a sequencer-only stop belongs to, from its todo list."""
    try:
        with open(os.path.join(sequencer, "todo"), encoding="utf-8", errors="replace") as handle:
            # The first instruction is all it needs: read a bounded head only.
            head = handle.read(_TODO_HEAD_CHARS)
    except OSError:
        return "cherry-pick"
    for line in head.split("\n"):
        word = line.split(None, 1)[0] if line.strip() else ""
        if word and not word.startswith("#"):
            return "revert" if word in ("revert", "r") else "cherry-pick"
    return "cherry-pick"


def _in_progress():
    """(operation, its own paths or None) when one is stopped mid-way, else None.

    Decided the way ``git status`` decides it: a rebase is running only while
    its state directory exists, and only the pseudo-refs ``git commit``
    consumes count otherwise.  A stale REBASE_HEAD left by a finished rebase
    is not a rebase in progress.  The own paths are the stopped commit's,
    plus where the other side renamed them.
    """
    rebase_merge, rebase_apply, sequencer, applying = _git_paths(
        "rebase-merge", "rebase-apply", "sequencer", "rebase-apply/applying"
    )
    stopped = []
    for ref, op in _CONSUMED_HEADS:
        rc, sha = _ask(["git", "rev-parse", "-q", "--verify", ref], answers=(0, 1))
        if rc == 0:
            stopped.append((op, sha))
    if os.path.isdir(rebase_merge) or os.path.isdir(rebase_apply):
        op = "am" if os.path.exists(applying) else "rebase"
        rc, sha = _ask(["git", "rev-parse", "-q", "--verify", "REBASE_HEAD"], answers=(0, 1))
        stopped.insert(0, (op, sha if rc == 0 else ""))
    elif os.path.isdir(sequencer) and not stopped:
        return _sequencer_op(sequencer), None
    if not stopped:
        return None
    op, sha = stopped[0]
    if not sha:
        return op, set()
    if op == "merge":
        rc, mbase = _ask(["git", "merge-base", "HEAD", sha], answers=(0, 1))
        old = mbase if rc == 0 else _empty_tree()
    else:
        parents = _commit_info(sha)[1]
        old = parents[0] if parents else _empty_tree()
    own = _changed(old, sha)
    return op, _follow_renames(own, sha if op == "revert" else old, "HEAD")


def _staged():
    """[(status letter, top-relative path bytes)] staged that HEAD lacks (read-only)."""
    status = _ask(
        [
            "git",
            "diff-index",
            "--cached",
            "--no-renames",
            "--ignore-submodules=none",
            "--ita-invisible-in-index",
            "--name-status",
            "-z",
            "HEAD",
            "--",
        ],
        raw=True,
    )[1]
    fields = [f for f in status.split(b"\0") if f]
    return list(zip(fields[0::2], fields[1::2]))


def _unmerged():
    """Top-relative paths (bytes) with unresolved conflicts, from any subdirectory."""
    out = _ask(["git", "ls-files", "-u", "-z", "--full-name", "--", ":(top)"], raw=True)[1]
    return sorted({entry.split(b"\t", 1)[1] for entry in out.split(b"\0") if b"\t" in entry})


def _carry_remedy():
    return (
        "git worktree add -b <new-branch> <dir> HEAD (it carries your commits, and "
        "the record of those the guard made); move "
        "uncommitted edits of yours with git diff --binary HEAD -- <your paths> > "
        "<patch-file> and git -C <dir> apply <patch-file> (never git stash)"
    )


def _report_staged(foreign, git_dir, lead="", op=None):
    """Print the refusal for staged paths HEAD lacks; return EXIT_FOREIGN_INDEX.

    Every path whose worktree copy may hold edits is listed and marked (*),
    however many there are; the command that clears the list is offered only
    when none is marked, and never in the middle of an operation.
    """
    edited = {
        p
        for p in _ask(
            ["git", "diff-files", "--name-only", "-z", "--ignore-submodules=none"], raw=True
        )[1].split(b"\0")
        if p
    }
    top = os.fsencode(_top())

    def marked(letter, path):
        # A staged deletion is absent from the index, so diff-files cannot see
        # it; a copy still on disk may hold edits.
        if letter == b"D":
            return os.path.lexists(os.path.join(top, path))
        return path in edited

    flagged = [(p, marked(s, p)) for s, p in foreign]
    n_marked = sum(1 for _, m in flagged if m)
    budget = max(0, _LISTED_PATHS - n_marked)
    lines = []
    hidden = 0
    for path, mark in flagged:
        if mark:
            lines.append("    {}   (*)".format(_display(path)))
        elif budget:
            budget -= 1
            lines.append("    " + _display(path))
        else:
            hidden += 1
    if hidden:
        lines.append("    ... and {} more, none marked (*)".format(hidden))
    list_file = _write_list(git_dir, [p for _, p in foreign])
    added = sum(1 for s, _ in foreign if s == b"A")
    text = [
        "REFUSED: {}{} staged path(s) differ from HEAD ({} not in HEAD):".format(
            lead, len(foreign), added
        ),
        "\n".join(lines),
        "  Full list (NUL-separated pathspecs): {}".format(
            list_file or "(the git dir is not writable)"
        ),
    ]
    if op is None:
        text.append(
            "  Yours: commit them by name — push_guard.py --commit -m '<subject>' -- <path>..."
        )
        text.append(
            "  Not yours (another session staged here, e.g. `git checkout <ref> -- .`): "
            "leave this checkout alone and continue in your own: " + _carry_remedy() + "."
        )
        if n_marked:
            text.append(
                "  No command here clears them: (*) marks a worktree copy that differs "
                "from what is staged, so it may hold edits of yours."
            )
        elif list_file:
            text.append(
                "  To clear them here instead — only when none is yours: git restore "
                "--source=HEAD --staged --worktree --pathspec-from-file={} "
                "--pathspec-file-nul".format(shlex.quote(list_file))
            )
    else:
        text.append(
            "  Yours (staged on purpose during the {0}): unstage them, keeping your "
            "copy (git restore --staged -- <path>...), finish the {0}, then commit them "
            "by name.".format(op)
        )
        text.append(
            "  Not yours: leave them, abort the {} (git {} --abort), and continue in "
            "your own worktree: {}.".format(op, op, _carry_remedy())
        )
        if n_marked:
            text.append(
                "  (*) the worktree copy differs from what is staged, so it may hold "
                "edits of yours."
            )
    err("\n".join(text))
    return EXIT_FOREIGN_INDEX


def _check_index(git_dir, report=False):
    """Refuse an index that holds anything HEAD lacks; return 0 or EXIT_FOREIGN_INDEX.

    Read-only: ``diff-index --cached`` takes no lock and writes nothing, and a
    name-only listing never reads blob content.  At a stopped rebase, merge,
    cherry-pick or revert, that operation's own paths are expected; anything
    else staged is listed.  With ``report`` (--check-index), a clean index
    prints its STATUS, and a stop whose staged paths are all its own is 0 —
    safe to ``--continue``.
    """
    progress = _in_progress()
    conflicts = _unmerged()
    if conflicts:
        shown = _indented(conflicts[:_LISTED_PATHS])
        if progress is None:
            err(
                "REFUSED: the index holds unresolved conflicts (e.g. from a git stash "
                "pop):\n{}\n  Resolve them and read the result with git diff before any "
                "commit; never commit a file that still carries conflict markers.".format(shown)
            )
        else:
            err(
                "REFUSED: the {0} stopped on conflicts in:\n{1}\n  Resolve each one "
                "(edit it, then git add <path>), run push_guard.py --check-index again, "
                "and continue the {0} only on exit 0.".format(progress[0], shown)
            )
        return EXIT_FOREIGN_INDEX
    staged = _staged()
    if progress is None:
        if staged:
            return _report_staged(staged, git_dir)
        if report:
            print("STATUS: INDEX MATCHES HEAD")
        return 0
    op, own = progress
    foreign = [(s, p) for s, p in staged if own is None or p not in own]
    if foreign and own is not None:
        return _report_staged(
            foreign, git_dir, lead="outside the {} in progress, ".format(op), op=op
        )
    if report and own is not None:
        print("STATUS: CLEAN ({} in progress; only its own paths are staged)".format(op))
        return 0
    err(
        "REFUSED: a {0} is in progress. Finish it (git {0} --continue) or abort "
        "it (git {0} --abort), then re-run.".format(op)
    )
    return EXIT_FOREIGN_INDEX


def _check_index_for_commit():
    """--commit/--amend's preconditions on state: no operation mid-way, no conflict."""
    progress = _in_progress()
    if progress is not None:
        err(
            "REFUSED: a {0} is in progress; finish it (git {0} --continue) or "
            "abort it first.".format(progress[0])
        )
        return EXIT_FOREIGN_INDEX
    if _unmerged():
        err(
            "REFUSED: the index holds unresolved conflicts; resolve them before "
            "any commit, and never commit a file that still carries conflict markers."
        )
        return EXIT_FOREIGN_INDEX
    return 0
