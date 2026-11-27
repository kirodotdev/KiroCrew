"""One refresh pass asks git each read-only question about its checkout once.

The pass's steps (the PR lookup, both bars, the Code tab, the worker rows)
share the branch and ``origin`` answers (:mod:`..workspace.checkout_reads`);
the branch only while the checkout's ``HEAD`` is unchanged, the next pass asks
git again, and an operation that creates the worktree shares only what it read
after the creation.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import test_slack_code_channel_board as suite

from kiro_crew.slack.code_channels.workspace import checkout_reads, git_ops
from kiro_crew.testing.links import make_dir_link

# The board suite's fixtures (autouse ones included), bound here so they apply.
_no_sandbox = suite._no_sandbox
_feature_on = suite._feature_on
_surface_store = suite._surface_store
_pinned_identity = suite._pinned_identity
publisher_clock = suite.publisher_clock
repo = suite.repo
_repo_template = suite._repo_template
_Harness = suite._Harness
_FakeBinding = suite._FakeBinding


def _counted(monkeypatch, name):
    """Record each call of ``git_ops.<name>`` as ``(cwd, answer)``."""
    real = getattr(git_ops, name)
    calls = []

    def counting(cwd, *args):
        answer = real(cwd, *args)
        calls.append((cwd, answer))
        return answer

    monkeypatch.setattr(git_ops, name, counting)
    return calls


def _bar_branches(h):
    """The branch label of every bar sent, in order."""
    return [
        next(i["label"] for i in kw["items"] if i["key"] == "branch")
        for m, kw in h.client.calls
        if m == "set_context_bar"
    ]


def test_each_pass_asks_git_again_and_shares_the_answers_inside_it(tmp_path, repo, monkeypatch):
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    asked = [_counted(monkeypatch, n) for n in ("current_branch", "remote_url")]
    asked.append(_counted(monkeypatch, "has_remote_branch"))
    counts = []
    for _ in range(2):
        for calls in asked:
            calls.clear()
        asyncio.run(h.svc.refresh(binding))
        counts.append(tuple(len([c for c, _ in calls if c == rec.worktree]) for calls in asked))
    # Asked once in every pass (4 branch, 3 origin and 2 pushed reads without the sharing).
    assert counts == [(1, 1, 1), (1, 1, 1)]
    assert h.pr_queries == [rec.branch, rec.branch]


def test_a_pass_that_creates_the_worktree_shares_the_branch_read_after_it(
    tmp_path, repo, monkeypatch
):
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    branches = _counted(monkeypatch, "current_branch")
    asyncio.run(h.svc.refresh(binding))  # no record yet: the pass binds first
    rec = h.svc.store.get("T1", "C1")
    assert rec is not None and rec.worktree
    first = next(answer for cwd, answer in branches if cwd == rec.worktree)
    assert first == rec.branch
    assert h.pr_queries == [rec.branch] and _bar_branches(h)[-1] == rec.branch


def test_a_branch_switched_during_the_pr_lookup_shows_on_the_next_bar(tmp_path, repo, monkeypatch):
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    real_find = h.svc._find_pr

    async def switch_then_find(found_repo, branch):
        suite._git("checkout", "-q", "-b", "kc/switched", cwd=rec.worktree)
        return await real_find(found_repo, branch)

    h.svc._find_pr = switch_then_find
    before = len(_bar_branches(h))
    branches = _counted(monkeypatch, "current_branch")
    asyncio.run(h.svc.refresh(binding))
    # The pass's first bar, sent after the switch, already shows it.
    assert _bar_branches(h)[before] == "kc/switched"
    assert [a for c, a in branches if c == rec.worktree] == [rec.branch, "kc/switched"]


def test_outside_a_pass_and_in_a_task_it_starts_git_is_asked_every_time(monkeypatch):
    asked = []
    monkeypatch.setattr(git_ops, "remote_url", lambda cwd: asked.append(cwd) or "u")

    async def run():
        await checkout_reads.remote_url("/w")
        with checkout_reads.reading():
            await checkout_reads.remote_url("/w")
            await checkout_reads.remote_url("/w")
            await asyncio.ensure_future(checkout_reads.remote_url("/w"))
        await checkout_reads.remote_url("/w")

    asyncio.run(run())
    assert asked == ["/w"] * 4


async def _lent_in_this_task(cwd):
    return await asyncio.to_thread(checkout_reads.lend(checkout_reads.branch_in_thread), cwd)


def test_the_branch_is_reused_only_while_head_is_unchanged_and_the_pass_open(repo, monkeypatch):
    asked = _counted(monkeypatch, "current_branch")
    cwd = str(repo)
    seen = []

    async def run():
        with checkout_reads.reading():
            seen.append(await checkout_reads.current_branch(cwd))
            lent = checkout_reads.lend(checkout_reads.branch_in_thread)
            seen.append(await asyncio.to_thread(lent, cwd))
            # A thread the pass did not lend its reads to (one a spawned task
            # starts, or a bare one) asks git.
            seen.append(await asyncio.to_thread(checkout_reads.branch_in_thread, cwd))
            seen.append(await asyncio.ensure_future(_lent_in_this_task(cwd)))
            suite._git("checkout", "-q", "-b", "kc/other", cwd=repo)
            seen.append(await checkout_reads.current_branch(cwd))
        seen.append(await asyncio.to_thread(lent, cwd))

    asyncio.run(run())
    assert seen == ["main"] * 4 + ["kc/other"] * 2
    assert [a for _, a in asked] == ["main", "main", "main", "kc/other", "kc/other"]


def test_a_head_that_cannot_be_found_is_never_reused(tmp_path):
    """No ``.git``, a ``.git`` file naming no git dir, or one that is no regular file."""
    stamp = checkout_reads._head_stamp
    assert stamp(str(tmp_path)) is None
    (tmp_path / ".git").write_text("not a pointer\n")
    assert stamp(str(tmp_path)) is None
    (tmp_path / ".git").write_text("gitdir: missing\n")
    assert stamp(str(tmp_path)) is None
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    (tmp_path / ".git").unlink()
    os.mkfifo(tmp_path / ".git")
    assert stamp(str(tmp_path)) is None  # opened non-blocking: never waits on a writer


def test_a_git_pointer_that_reaches_a_sensitive_path_is_not_read(tmp_path, repo, monkeypatch):
    from kiro_crew import hooks

    secret = tmp_path / "secret"
    secret.mkdir()
    target = secret / ".git"
    target.write_text(f"gitdir: {repo / '.git'}\n")
    # The checkout is a directory link (a junction on Windows), so its ``.git``
    # pointer is reached through the link.
    tree = tmp_path / "tree"
    make_dir_link(tree, secret)
    assert checkout_reads._head_stamp(str(tree)) is not None  # the pointer is followed
    refused = str(target.resolve())
    monkeypatch.setattr(
        hooks,
        "sensitive_path_refusal",
        lambda path: "Blocked: sensitive" if path == refused else "",
    )
    assert checkout_reads._head_stamp(str(tree)) is None
