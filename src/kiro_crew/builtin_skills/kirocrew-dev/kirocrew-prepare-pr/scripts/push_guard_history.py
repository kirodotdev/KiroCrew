"""The branch's history against the freshly fetched base.

It holds the pre-squash checks (the base is an ancestor of HEAD, the commit count stays
under ``--max-ahead``, no commit replays one already on the base) and the post-squash
check that HEAD is one commit on the base. New work on what a pushable history looks
like goes here.

Loaded by ``push_guard.py``, which rebinds every function defined here onto its own
globals, so the code runs as if it were written there and a name a test replaces on
``push_guard.py`` reaches it. This module therefore defines functions only: no module
state, no class, and nothing its functions read at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from push_guard import (
        REPLAY_HISTORY_WINDOW,
        Refused,
        _ask,
        _changed,
        _commit_info,
        _head_ref,
        _refuse_unrecorded,
        _remote_ref,
        _sha,
        err,
    )
    from push_guard_record import _unrecorded


def _resquash_remedy(base):
    """The one way this skill rebuilds a squash; every refusal prints it."""
    return (
        "rebase onto the fresh base (git rebase {}), then squash with "
        "push_guard.py --base {} --squash".format(_remote_ref(base), base)
    )


def _check_single_on_base(base, git_dir, vouched=()):
    """Post-squash guard: HEAD's ONLY parent is origin/<base>, and the record covers HEAD."""
    pinned = _sha(_remote_ref(base))
    head = _sha("HEAD")
    tree, parents, _ = _commit_info(head)
    shown = " ".join(p[:12] for p in parents) or "none"

    print("parents:         " + shown)
    print("origin/{}:     {}".format(base, pinned[:12]))
    print("HEAD:            " + head[:12])

    if parents != [pinned]:
        err(
            "REFUSED: HEAD's parents ({}) are not exactly origin/{} ({}). "
            "The squashed commit does not sit directly on the freshly fetched "
            "remote base — either the squash landed on a stale ref or the "
            "branch carries unexpected history.\n"
            "  To fix: {}.".format(shown, base, pinned[:12], _resquash_remedy(base))
        )
        raise Refused()
    unknown = _unrecorded(
        git_dir, _head_ref(), _changed(pinned, tree), [head], pinned, head, vouched
    )
    if unknown:
        _refuse_unrecorded(unknown, "HEAD ({})".format(head[:12]), git_dir, base, "single")


def _patch_ids(revs):
    """patch-id -> sha for ``revs`` (fail closed: a git failure refuses)."""
    found: dict[str, str] = {}
    for commit_sha in revs:
        diff_out = _ask(["git", "diff-tree", "-p", commit_sha, "--"])[1]
        if not diff_out:
            # diff-tree SUCCEEDED with empty output — empty commit, skip.
            continue
        pid_out = _ask(["git", "patch-id", "--stable"], input=diff_out)[1]
        if pid_out:
            found[pid_out.split()[0]] = commit_sha
    return found


def _check_pre_squash(base, max_ahead):
    """Pre-squash guard: merge-base ancestry, commit count, replayed commits.

    HEAD and origin/<base> are resolved ONCE; every check reads those shas,
    which are returned as (head, pinned, the commits between them).  Fail-closed contract: every git
    failure, timeout or OSError refuses (Refused) with a diagnostic naming
    the failed command.  The ONLY path that returns is one where every git
    command SUCCEEDED.
    """
    pinned = _sha(_remote_ref(base))
    head = _sha("HEAD")
    rc, merge_base = _ask(["git", "merge-base", head, pinned], answers=(0, 1))
    if rc != 0 or not merge_base:
        err(
            "REFUSED: cannot compute merge-base between HEAD and origin/{}. "
            "The branch may have no common history with the remote base.".format(base)
        )
        raise Refused()

    # origin/<base> must be an ancestor of HEAD — HEAD sits on the fetched
    # tip.  After a correct rebase (Phase 1 step 2) this always holds; if it
    # fails, a squash would bake in reversions of newer base changes.
    if merge_base != pinned:
        err(
            "REFUSED: HEAD is not based on the fresh origin/{} tip — the "
            "branch forks from a stale base and squashing would bake in "
            "reversions of newer base changes. To fix: {}.".format(base, _resquash_remedy(base))
        )
        raise Refused()

    ahead_revs = _ask(["git", "rev-list", "{}..{}".format(pinned, head), "--"])[1].split()
    ahead = len(ahead_revs)

    print("merge-base:      " + merge_base[:12])
    print("origin/{}:     {}".format(base, pinned[:12]))
    print("HEAD:            " + head[:12])
    print("commits ahead:   {}".format(ahead))
    print("max allowed:     {}".format(max_ahead))

    if ahead > max_ahead:
        err(
            "REFUSED: HEAD is {} commits ahead of origin/{} (max allowed: {}). "
            "This is far too many for a squashed single-commit PR — the branch "
            "likely carries unshipped local integration commits that would "
            "clobber upstream work if force-pushed.\n"
            "  To fix (if you authored all {} commits): squash them down "
            "(push_guard.py --base {} --squash --max-ahead {}) so the branch "
            "carries a single deliverable commit.\n"
            "  To fix (if any ahead-commit is unfamiliar): STOP and diagnose — "
            "do not squash foreign history. The branch may have picked up "
            "commits from a local integration trunk.\n"
            "  To fix (stale fork): {}.".format(
                ahead, base, max_ahead, ahead, base, ahead, _resquash_remedy(base)
            )
        )
        raise Refused()

    # Detect replayed commits: an ahead-commit patch-equivalent to a commit in
    # a BOUNDED window (REPLAY_HISTORY_WINDOW) of base history replays
    # upstream patches (e.g. a stale fork that cherry-picked base commits).
    ahead_ids = _patch_ids(ahead_revs)
    if ahead_ids:
        base_out = _ask(
            ["git", "rev-list", "--max-count={}".format(REPLAY_HISTORY_WINDOW), pinned, "--"]
        )[1]
        base_ids = _patch_ids(base_out.split())
        replayed = [(a, base_ids[pid]) for pid, a in ahead_ids.items() if pid in base_ids]
        if replayed:
            err(
                "REFUSED: {} ahead-commit(s) are patch-equivalent to commits "
                "already on origin/{} — the branch replays upstream history.\n"
                "  replayed (ahead ↔ base): {}\n"
                "  To fix: STOP and diagnose. Do not squash — one or more "
                "ahead-commits duplicate patches already on the base branch. "
                "Rebase onto a fresh origin/{} so only novel changes remain, "
                "or cherry-pick your original commits onto origin/{}.".format(
                    len(replayed),
                    base,
                    ", ".join("{} ↔ {}".format(a[:12], b[:12]) for a, b in replayed),
                    base,
                    base,
                )
            )
            raise Refused()
    return head, pinned, ahead_revs
