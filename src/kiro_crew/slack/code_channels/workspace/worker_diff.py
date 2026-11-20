"""The Code tab's helper-agent groups: each worker's own commits, read from git.

Kept out of :mod:`.service` so the refresh owner stays under the file cap. Every
byte read here is handed to the service's *screen* (the whole-file screen and
the outbound redaction of the channel's own diff) and every header to its
*header* before it is kept, so nothing here decides what reaches Slack.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew.slack.code_channels.workspace import git_ops
from kiro_crew.slack.code_channels.workspace.diff import MAX_VIEW_BYTES, DiffGroup

#: ``(raw diff, cut) -> (screened bytes ready for Slack, cut)``.
Screen = Callable[[bytes, bool], tuple[bytes, bool]]
#: ``(index, total, row) -> header ready for Slack``.
Header = Callable[[int, int, Any], str]


def worker_groups(
    cwd: str,
    board: Any,
    channel_branch: str,
    trees: dict[str, str],
    *,
    scan_bytes: int,
    screen: Screen,
    header: Header,
) -> list[DiffGroup]:
    """Blocking: one diff group per worker branch with commits of its own.

    Each worker's change is ``merge-base(channel HEAD, worker) .. worker``, read
    from git's object database in the channel's checkout (worktrees of one
    repository share it), so nothing in the worker's own worktree is opened. The
    merge base is taken against the channel's HEAD: a worker branch cut from the
    channel branch shows only its own commits, and one already merged into the
    channel shows nothing (its change is in the channel's group). The branch name
    is worker-reported, so only a valid name under ``refs/heads/`` or
    ``refs/remotes/origin/`` is looked up (:func:`git_ops.branch_commit`), and
    only a tip reachable from the HEAD of that worker's own tree (*trees*: slot
    key -> its project) is shown: a branch the worker only claims (another
    channel's, say) shows no commits.

    The reads are bounded twice: *scan_bytes* of raw diff per refresh, and the
    view's own :data:`MAX_VIEW_BYTES`. A worker left out (past either budget, or
    beyond the board's plan) adds a cut group, so the tab says so.
    """
    rows = list(getattr(board, "rows", ()))
    # The workers the board's plan shows; settled workers beyond the board's own
    # view cost git calls for changes that are usually merged already.
    view = {id(r) for r in getattr(board, "shown", rows)}
    shown = {
        id(r)
        for r in getattr(board, "plan_rows", rows)
        if id(r) in view or not getattr(r, "settled", False)
    }
    groups: list[DiffGroup] = []
    heads: dict[str, str] | None = None  # every tree's HEAD, read once when needed
    spent = 0  # screened bytes, the view's share
    scanned = 0  # raw bytes read, the refresh's work
    lost = False
    # Numbered by board position, so "Helper agent 2 of 5" is the board's second row.
    for index, row in enumerate(rows, start=1):
        if not row.branch or row.branch == channel_branch:
            continue
        if id(row) not in shown:
            lost = True  # a worker the plan leaves out ("+N more"): said, not read
            continue
        if spent > MAX_VIEW_BYTES or scanned >= scan_bytes:
            lost = True  # either budget spent: the rest are left out, and said so
            break
        head = git_ops.branch_commit(cwd, row.branch)
        if not head:
            continue
        if heads is None:
            heads = git_ops.worktree_heads(cwd)
        if not heads:
            lost = True  # git listed no tree at all (it lists the checkout's own): said
            break
        own = git_ops.tree_head(heads, trees.get(row.session_key, ""))
        if not own or git_ops.merge_base(cwd, head, own) != head:
            continue  # not in that worker's own checkout: its branch is only a claim
        fork = git_ops.merge_base(cwd, "HEAD", head)
        if not fork or fork == head:
            continue
        got = git_ops.commit_range_diff(cwd, fork, head, cap=scan_bytes - scanned)
        if got is None:
            lost = True  # git failed on a branch with commits of its own
            continue
        scanned += len(got[0])
        body, cut = screen(got[0], got[1])
        spent += len(body)
        groups.append(
            DiffGroup(header=header(index, len(rows), row), tracked=body, tracked_truncated=cut)
        )
    if lost:
        # An empty group that was cut: the assembly drops its header and adds
        # the truncation note, so a left-out worker is never silently absent.
        groups.append(DiffGroup(header="", tracked=b"", tracked_truncated=True))
    return groups
