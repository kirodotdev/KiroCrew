"""Materializing the release-channel worktree, and publishing where it sits.

Three classes of defect are guarded here, all of which produce a tree that looks
healthy:

* **Resolving stale.** Create must fetch BEFORE resolving. The background refresher
  keeps the fleet ROWS current, but it runs on its own schedule -- a mutation that
  resolved first would pin whatever the tag mirror held at that moment and report the
  result as the channel tip.
* **Adopting a tree that is not ours.** ``release-channel-stable`` is a reserved name,
  and a user's own branch checkout under that name must never have its HEAD moved.
  Adoption requires the SHAPE (detached), never the name.
* **Running the repository's own config while checking the release out.** A
  ``filter.<name>.smudge`` in ``.git/config`` is an arbitrary command git executes on
  the files it writes, and a ``core.worktree`` beside it decides where those files
  land. The populate is given a private git common dir so neither is in reach; the
  real-git test at the end of this module proves both arms.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    release_channel_pin,
    repository,
    runtime,
    worktree_ops,
)
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import await_until

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

_TIP_OID = "1" * 40
_OTHER_OID = "2" * 40
_TAG_OBJECT_OID = "3" * 40


class _Git:
    """Records every argv and answers the reads these ops make."""

    def __init__(
        self,
        *,
        tags=("v0.5.0",),
        head=_TIP_OID,
        head_tag="v0.4.9",
        fail=None,
        gitdir="/fake/gitdir",
        advertised=None,
        peeled=None,
        staged_head=None,
    ):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self._tags = list(tags)
        self._head = head
        # What the mirror answers when narrowed to HEAD: the release the worktree is
        # ACTUALLY on. Deliberately a different release from the lane tip in `tags`,
        # because a fake that answered with the tip could not tell a row showing its
        # own version from one showing the tip's.
        self._head_tag = head_tag
        self._fail = fail or {}
        self._gitdir = gitdir
        # What `rev-parse HEAD` answers for the STAGED worktree, which the create reads
        # back to confirm the detached pin. Defaults to the resolved tip.
        self._staged_head = _TIP_OID if staged_head is None else staged_head
        # What the REMOTE advertises per tag, as `ls-remote` prints it. Default: every
        # tag in `_tags` advertises the oid `rev-parse` answers with, i.e. a checkout
        # whose mirror agrees with the remote. `peeled` adds `^{}` lines, which is what
        # an ANNOTATED tag looks like -- plain entry = tag object, peel = commit.
        self._advertised = {t: _TIP_OID for t in self._tags} if advertised is None else advertised
        self._peeled = peeled or {}
        self.modes: list[str] = []

    async def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        self.kwargs.append(dict(kw))
        self.modes.append(kw.get("mode", "standard"))
        for needle, result in self._fail.items():
            if needle in cmd:
                return result
        if "rev-parse" in cmd and "--absolute-git-dir" in cmd:
            return 0, f"{self._gitdir}\n", ""
        if "rev-parse" in cmd and "--git-common-dir" in cmd:
            return 0, ".git\n", ""
        if "fetch" in cmd:
            return 0, "", ""
        if "for-each-ref" in cmd and "--points-at" in cmd:
            return (0, self._head_tag + "\n", "") if self._head_tag else (0, "", "")
        if "for-each-ref" in cmd:
            return 0, "\n".join(self._tags) + "\n", ""
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"  # detached
        if "ls-remote" in cmd:
            lines = [f"{oid}\trefs/tags/{t}" for t, oid in self._advertised.items()]
            lines += [f"{oid}\trefs/tags/{t}^{{}}" for t, oid in self._peeled.items()]
            return 0, ("\n".join(lines) + "\n" if lines else ""), ""
        if "read-tree" in cmd:
            return 0, "", ""
        if "worktree" in cmd:
            return 0, "", ""
        if "rev-parse" in cmd:
            if cmd[-1] == "HEAD":
                staged = "worktree" not in cmd and any(".staging." in part for part in cmd)
                answer = self._staged_head if staged else self._head
                return (0, answer + "\n", "") if answer else (128, "", "fatal: bad revision")
            return 0, _TIP_OID + "\n", ""
        return 1, "", f"unexpected argv: {cmd}"

    def argv_with(self, needle: str) -> list[str] | None:
        for call in self.calls:
            if needle in call:
                return call
        return None

    def kwargs_with(self, needle: str) -> dict | None:
        for call, kw in zip(self.calls, self.kwargs):
            if needle in call:
                return kw
        return None

    def order(self, *needles: str) -> list[int]:
        """Index of the first call containing each needle."""
        return [next(i for i, c in enumerate(self.calls) if needle in c) for needle in needles]


@pytest.fixture(autouse=True)
def _isolated_fleet_epochs(_floor_monkeypatch):
    """Every test here starts from, and leaves behind, clean fleet-cache epochs.

    A successful Create calls ``fleet_state._fleet_adopted``, which advances
    ``_FLEET_EPOCH`` and ``_FLEET_ADOPTED_EPOCH``; both are module globals read by
    every fleet build in the worker. Without this, a later test in another file that
    resets only ``_FLEET_EPOCH`` sees ``_FLEET_ADOPTED_EPOCH > started`` and builds
    twice. Through ``_floor_monkeypatch`` so a test's own ``monkeypatch.undo()``
    cannot lift it (testing-conventions D11).
    """
    _floor_monkeypatch.setattr(fleet_state, "_FLEET_EPOCH", 0)
    _floor_monkeypatch.setattr(fleet_state, "_FLEET_ADOPTED_EPOCH", 0)
    _floor_monkeypatch.setattr(fleet_state, "_FLEET_TOMBSTONES", {})
    _floor_monkeypatch.setattr(fleet_state, "_FLEET_INFLIGHT", None)
    _floor_monkeypatch.setattr(fleet_state, "_FLEET_CACHE", {"data": None, "ts": 0.0})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A primary checkout path whose sibling lane dir genuinely does not exist."""
    # A DIRECTORY name, not prose: it mirrors the real primary checkout's own folder,
    # and `worktree_path` derives the lane dir as that folder's sibling -- so
    # respelling it would stop the fixture matching the layout under test.
    checkout = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    checkout.mkdir()
    monkeypatch.setattr(repository, "_repo", lambda: str(checkout))
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    # Each test owns its own lock, so a refusal in one cannot leak into the next.
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    return str(checkout)


def _found(entry: dict):
    async def _find(_name: str):
        return entry, None

    return _find


def _missing(err: str | None = None):
    async def _find(name: str):
        return None, err or f"worktree not found: {name}"

    return _find


# --------------------------------------------------------------------------
# create: refusals before any mutation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_refuses_when_the_worktree_already_exists(repo, monkeypatch):
    """Refuse, and send the operator only to controls that exist.

    This is the one collision an operator is guaranteed to reach, so the message is the
    whole remedy they get. The pin does not move in place, so "move it to the tip"
    would send them hunting for a control that was never shipped.
    """
    monkeypatch.setattr(
        repository, "_find_worktree", _found({"path": "/somewhere/release-channel-stable"})
    )
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert got["code"] == "exists"
    assert "remove" in got["error"].lower()


@pytest.mark.asyncio
async def test_create_refuses_an_ambiguous_name_before_any_git_mutation(repo, monkeypatch):
    """The fleet's own writer only ever creates the canonical sibling path."""
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    monkeypatch.setattr(repository, "_find_worktree", _missing("ambiguous worktree name"))
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "ambiguous_name"
    assert git.calls == []


@pytest.mark.asyncio
async def test_create_refuses_an_occupied_path_by_name(repo, monkeypatch):
    """A directory at the lane path is not ours, so it is named rather than adopted."""
    Path(release_channel_pin.worktree_path(repo)).mkdir()
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "path_exists"
    assert release_channel_pin.WORKTREE_NAME in got["error"]
    assert git.calls == []


@pytest.mark.asyncio
async def test_create_refuses_while_another_operation_holds_the_worktrees_lock(repo, monkeypatch):
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    lock = worktree_ops._wt_lock(release_channel_pin.WORKTREE_NAME)
    async with lock:
        got = await worktree_ops._release_channel_create()
    assert got["code"] == "busy"
    assert git.calls == []


@pytest.mark.asyncio
async def test_create_reports_a_failed_fetch_instead_of_resolving_locally(repo, monkeypatch):
    """A stale mirror must not be mistaken for the channel's real tip."""
    git = _Git(fail={"fetch": (128, "", "fatal: could not read from remote repository")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "fetch_failed"
    assert git.argv_with("worktree") is None


@pytest.mark.asyncio
async def test_create_fetches_before_it_resolves(repo, monkeypatch):
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    fetch_at, listing_at = git.order("fetch", "for-each-ref")
    assert fetch_at < listing_at


@pytest.mark.asyncio
async def test_create_reports_an_empty_channel_as_unpublished(repo, monkeypatch):
    git = _Git(tags=())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "unpublished"
    assert git.argv_with("worktree") is None


@pytest.mark.asyncio
async def test_create_refuses_a_tag_the_remote_does_not_publish(repo, monkeypatch):
    """A mirror ref planted after the fetch is pruned by no fetch that already ran."""
    git = _Git(advertised={})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "provenance_mismatch"
    assert "written to locally" in got["error"]
    assert git.argv_with("worktree") is None


@pytest.mark.asyncio
async def test_create_refuses_a_published_tag_whose_local_oid_was_rewritten(repo, monkeypatch):
    git = _Git(advertised={"v0.5.0": _OTHER_OID})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "provenance_mismatch"
    assert git.argv_with("worktree") is None


@pytest.mark.asyncio
async def test_an_annotated_release_tag_passes_provenance_through_its_peel_line(repo, monkeypatch):
    """The plain entry carries the TAG OBJECT; only the peel line carries the commit."""
    git = _Git(advertised={"v0.5.0": _TAG_OBJECT_OID}, peeled={"v0.5.0": _TIP_OID})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True, got


@pytest.mark.asyncio
async def test_create_refuses_when_the_remote_cannot_be_asked(repo, monkeypatch):
    """Fails CLOSED: an unverifiable release is not checked out on the local oid."""
    git = _Git(fail={"ls-remote": (128, "", "fatal: could not read from remote repository")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "provenance_unverifiable"
    assert git.argv_with("worktree") is None


# --------------------------------------------------------------------------
# create: staging and adoption
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_stages_the_worktree_then_adopts_the_lane_path(repo, monkeypatch):
    """The lane is built under a name only this call can produce, then moved into place.

    `worktree move` refuses an existing destination, which is what makes IT the step
    that decides the race rather than the path-exists guard: a second writer that
    reached the lane path first wins, and this call discards its own staging tree.
    """
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True, got
    path = release_channel_pin.worktree_path(repo)
    add = git.argv_with("add")
    assert add is not None
    staging = add[-2]
    assert staging.startswith(f"{path}.staging.")
    assert str(os.getpid()) in staging
    assert "--no-checkout" in add and "--detach" in add
    assert add[-1] == _TIP_OID
    move = git.argv_with("move")
    assert move is not None
    assert move[-2:] == [staging, path]
    add_at, move_at = git.order("add", "move")
    assert add_at < move_at


@pytest.mark.asyncio
async def test_two_calls_never_stage_under_the_same_name(repo):
    path = release_channel_pin.worktree_path(repo)
    first = worktree_ops._lane_staging_path(path)
    second = worktree_ops._lane_staging_path(path)
    assert first != second
    assert Path(first).parent == Path(path).parent


@pytest.mark.asyncio
async def test_the_successful_result_names_only_what_a_caller_reads(repo, monkeypatch):
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert set(got) == {"ok", "lane", "name", "ref", "version", "path"}
    assert got["name"] == release_channel_pin.WORKTREE_NAME
    assert got["ref"] == "refs/tags/v0.5.0"
    assert got["version"] == "0.5.0"


@pytest.mark.asyncio
async def test_a_failed_create_never_force_removes_the_lane_path(repo, monkeypatch):
    """The cleanup may only ever name a staging path this call produced.

    Aimed at the lane path it would infer ownership from the earlier existence check,
    and a second gateway sharing the repo invalidates that inference -- the cleanup
    would then delete THEIR populated worktree with no reflog to recover from.
    """
    git = _Git(fail={"add": (128, "", "fatal: could not create work tree dir")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "git_failed"
    path = release_channel_pin.worktree_path(repo)
    removes = [c for c in git.calls if "remove" in c]
    assert removes, git.calls
    for call in removes:
        assert call[-1].startswith(f"{path}.staging.")
        assert call[-1] != path
    assert not any("prune" in c for c in git.calls), git.calls


@pytest.mark.asyncio
async def test_a_failed_create_does_not_leave_the_lane_uncreatable(repo, monkeypatch):
    """`worktree add` can fail after registering the record, so the record is dropped."""
    git = _Git(fail={"add": (128, "", "fatal: boom")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    remove = git.argv_with("remove")
    assert remove is not None
    assert "--force" in remove


@pytest.mark.asyncio
async def test_a_failed_move_reports_the_collision_it_lost(repo, monkeypatch):
    path = release_channel_pin.worktree_path(repo)

    async def git(cmd, **kw):
        if "move" in cmd:
            Path(path).mkdir(exist_ok=True)
            return 128, "", "fatal: target already exists"
        return await base(cmd, **kw)

    base = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "path_exists"
    assert base.argv_with("remove") is not None


@pytest.mark.asyncio
async def test_a_directory_that_appeared_at_the_lane_path_is_refused_before_the_move(
    repo, monkeypatch
):
    """`worktree move <src> <existing dir>` nests INTO the directory, exit 0 (mv(1)
    semantics, measured on git 2.54). So the destination is re-checked under the lock
    right before the move: a directory that appeared during the fetch, resolve and
    populate is a collision, and no move is attempted against it."""
    path = release_channel_pin.worktree_path(repo)
    base = _Git()

    async def git(cmd, **kw):
        if "read-tree" in cmd:
            # Another writer lands at the lane path while we populate our staging tree.
            Path(path).mkdir(exist_ok=True)
        return await base(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "path_exists"
    assert base.argv_with("move") is None
    removed = base.argv_with("remove")
    assert removed is not None and removed[-1].startswith(f"{path}.staging."), removed


@pytest.mark.asyncio
async def test_a_move_that_nested_into_a_foreign_directory_is_undone_and_reported(
    repo, monkeypatch
):
    """The check no lock can replace: a directory created by ANOTHER process between the
    pre-move check and the move makes git nest our tree inside it and exit 0. The
    landing is verified by asking git which admin dir the worktree at the lane path
    uses; a foreign answer means the nested tree -- ours by construction -- is removed
    by ITS path, never the lane path, and the collision is reported."""
    path = release_channel_pin.worktree_path(repo)
    base = _Git()
    staging_seen: list[str] = []

    async def git(cmd, **kw):
        if "move" in cmd:
            staging_seen.append(cmd[-2])
        if "rev-parse" in cmd and "--absolute-git-dir" in cmd and cmd[2] == path:
            return 0, "/elsewhere/.git/worktrees/theirs\n", ""
        return await base(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "path_exists"
    assert staging_seen, "the move itself must have run for the landing to be checked"
    removed = base.argv_with("remove")
    assert removed is not None
    assert removed[-1] == os.path.join(path, os.path.basename(staging_seen[0])), removed
    assert removed[-1] != path


@pytest.mark.asyncio
async def test_a_landed_move_is_verified_by_its_own_admin_dir(repo, monkeypatch):
    """The happy path asks the same question and gets the staging worktree's answer."""
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True
    path = release_channel_pin.worktree_path(repo)
    landed = [c for c in git.calls if "--absolute-git-dir" in c and c[2] == path]
    assert len(landed) == 1, git.calls
    assert git.argv_with("remove") is None


@pytest.mark.asyncio
async def test_a_successful_create_names_the_lane_the_toast_reads(repo, monkeypatch):
    """The page's success toast interpolates `lane`; a reply without it renders
    "Created  worktree" with the channel missing."""
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True
    assert got["lane"] == release_channel_pin.CHANNEL
    assert set(got) == {"ok", "lane", "name", "ref", "version", "path"}


@pytest.mark.asyncio
async def test_a_successful_create_marks_the_fleet_adopted(repo, monkeypatch):
    """So a rebuild already in flight cannot publish a snapshot without the new row."""
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    marks: list[int] = []
    monkeypatch.setattr(fleet_state, "_fleet_adopted", lambda: marks.append(1))
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True
    assert marks == [1]


@pytest.mark.asyncio
async def test_a_refused_create_never_marks_the_fleet_adopted(repo, monkeypatch):
    git = _Git(staged_head=_OTHER_OID)
    monkeypatch.setattr(runtime, "_run_cmd", git)
    marks: list[int] = []
    monkeypatch.setattr(fleet_state, "_fleet_adopted", lambda: marks.append(1))
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is False
    assert marks == []


@pytest.mark.asyncio
async def test_a_fleet_build_that_predates_an_adoption_reads_git_again(monkeypatch):
    """The `fresh=1` the page sends after Create coalesces onto whatever build is in
    flight. A build that started before the adoption read git before the worktree
    existed; publishing its snapshot would show the placeholder the toast just said
    was replaced. The adoption epoch makes that build re-read, once."""
    builds: list[int] = []

    async def build():
        builds.append(len(builds) + 1)
        if len(builds) == 1:
            fleet_state._fleet_adopted()  # lands while the first read is in flight
            return {"worktrees": [], "release_channel": {"worktree": None}}
        return {"worktrees": [{"name": "release-channel-stable"}], "release_channel": {}}

    monkeypatch.setattr(fleet_state, "_build_fleet", build)
    monkeypatch.setattr(fleet_state, "_FLEET_CACHE", {"data": None, "ts": 0.0})
    data = await fleet_state._fleet_build()
    assert builds == [1, 2]
    assert data["worktrees"] == [{"name": "release-channel-stable"}]
    assert fleet_state._FLEET_CACHE["data"] is data


@pytest.mark.asyncio
async def test_a_fleet_build_with_no_adoption_reads_git_once(monkeypatch):
    builds: list[int] = []

    async def build():
        builds.append(1)
        return {"worktrees": []}

    monkeypatch.setattr(fleet_state, "_build_fleet", build)
    monkeypatch.setattr(fleet_state, "_FLEET_CACHE", {"data": None, "ts": 0.0})
    await fleet_state._fleet_build()
    assert builds == [1]


@pytest.mark.asyncio
async def test_an_adoption_zeroes_the_cache_timestamp_so_a_cached_reader_rebuilds(monkeypatch):
    monkeypatch.setattr(fleet_state, "_FLEET_CACHE", {"data": {"worktrees": []}, "ts": 1e12})
    before = fleet_state._FLEET_EPOCH
    fleet_state._fleet_adopted()
    assert fleet_state._FLEET_CACHE["ts"] == 0.0
    assert fleet_state._FLEET_ADOPTED_EPOCH == fleet_state._FLEET_EPOCH == before + 1


def test_error_detail_strips_scrubs_and_bounds_a_git_stderr(monkeypatch):
    """The one spelling of git-stderr-into-a-result, applied where it is classified."""
    monkeypatch.setattr(runtime, "_redact", lambda text: text.replace("hunter2", "***"))
    assert runtime._error_detail("  password hunter2 leaked\n") == "password *** leaked"
    assert runtime._error_detail(None) == ""
    assert runtime._error_detail("x" * 500) == "x" * 200
    assert runtime._error_detail("x" * 500, limit=10) == "x" * 10


def test_the_release_owners_carry_no_redactor_of_their_own():
    """The classification guard (`test_security_posture`) walks for redactor call sites
    by name; these two modules carry none. Git stderr is scrubbed by `runtime._error_detail`
    (the module classified for that path), and every other string in a result -- a path,
    a ref -- is scrubbed by the route that serves the run record, `api_dev_fleet_run`,
    where it leaves the process. Pinned so a later edit that calls a redactor here knows
    it is adding a classification, not just a line."""
    import inspect
    import re

    from kiro_crew.apps.builtins.dev_fleet import worktree_ops_release

    call = re.compile(r"\b\w*redact\w*\(")
    for module in (release_channel_pin, worktree_ops_release):
        assert not call.search(inspect.getsource(module)), module.__name__


@pytest.mark.asyncio
async def test_a_staged_tree_not_detached_at_the_release_is_discarded(repo, monkeypatch):
    """The read-back is what makes "detached at the verified oid" a checked fact."""
    git = _Git(staged_head=_OTHER_OID)
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "git_failed"
    assert "not detached at the resolved release" in got["error"]
    assert git.argv_with("move") is None
    assert git.argv_with("remove") is not None


@pytest.mark.asyncio
async def test_a_cancelled_create_discards_the_staging_tree_on_the_way_out(repo, monkeypatch):
    """A return is not the only exit: an ordinary shutdown unwinds this frame."""
    git = _Git()
    seen: list[list[str]] = []

    async def cancelling(cmd, **kw):
        seen.append(list(cmd))
        if "read-tree" in cmd:
            raise asyncio.CancelledError
        return await git(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", cancelling)
    with pytest.raises(asyncio.CancelledError):
        await worktree_ops._release_channel_create()
    assert any("remove" in c for c in seen), seen


@pytest.mark.asyncio
async def test_every_create_mutation_runs_at_the_repo_controlled_tier(repo, monkeypatch):
    """The checkout acts on repo-controlled content, so it gets no credential helper."""
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    for call, mode in zip(git.calls, git.modes):
        if "worktree" in call or "read-tree" in call:
            assert mode == "strict", call


@pytest.mark.asyncio
async def test_the_remote_facing_calls_keep_their_credentials(repo, monkeypatch):
    """The fetch and the provenance query authenticate, so they stay at the standard tier."""
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    for call, mode in zip(git.calls, git.modes):
        if "fetch" in call or "ls-remote" in call:
            assert mode == "standard", call


# --------------------------------------------------------------------------
# create: the hermetic populate
# --------------------------------------------------------------------------
def _populate_env(git: _Git) -> dict:
    kw = git.kwargs_with("read-tree")
    assert kw is not None, git.calls
    return dict(kw["env"])


@pytest.mark.asyncio
async def test_the_populate_is_one_read_tree_and_never_a_reset(repo, monkeypatch):
    """`read-tree -u --reset` updates index and tree and leaves HEAD where it is.

    The read-back of HEAD is what confirms the detached pin, so a populate that moved
    HEAD would make that check measure its own write.
    """
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    populate = git.argv_with("read-tree")
    assert populate == ["git", "read-tree", "-u", "--reset", _TIP_OID]
    assert not any("--hard" in c for c in git.calls), git.calls


@pytest.mark.asyncio
async def test_the_populate_spawn_carries_its_own_git_location(repo, monkeypatch):
    """Three variables decide what the checkout reads and where it writes.

    ``GIT_COMMON_DIR`` is the one that matters: it points config and
    ``info/attributes`` at files this process just created, so the repository's own
    ``.git/config`` is never opened by this spawn and no write to it, at any instant,
    can reach it.
    """
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    env = _populate_env(git)
    staging = (git.argv_with("add") or [])[-2]
    assert env["GIT_DIR"] == "/fake/gitdir"
    assert env["GIT_WORK_TREE"] == staging
    assert env["GIT_COMMON_DIR"] and env["GIT_COMMON_DIR"] != "/fake/gitdir"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert git.kwargs_with("read-tree")["cwd"] == staging


@pytest.mark.asyncio
async def test_the_populate_env_inherits_no_other_git_variable(repo, monkeypatch):
    """A variable that must be ABSENT cannot be handled by merging over the environment.

    An inherited ``GIT_INDEX_FILE``, ``GIT_OBJECT_DIRECTORY``,
    ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` or ``GIT_NAMESPACE`` would survive an update()
    and silently retarget this write.
    """
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/stolen-index")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/stolen-objects")
    monkeypatch.setenv("GIT_NAMESPACE", "stolen")
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create()
    env = _populate_env(git)
    supplied = {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
    }
    assert {k for k in env if k.startswith("GIT_")} == supplied


@pytest.mark.asyncio
async def test_the_private_common_dir_is_a_minimal_repository_pointing_at_the_real_objects(
    repo, monkeypatch
):
    """Every object must resolve while every config scope must not.

    The contents are captured while the spawn is in flight, because the directory is
    deleted the moment the populate returns.
    """
    git = _Git()
    captured: dict[str, str] = {}

    async def capturing(cmd, **kw):
        if "read-tree" in cmd:
            common = Path(kw["env"]["GIT_COMMON_DIR"])
            captured["mode"] = oct(common.stat().st_mode & 0o777)
            captured["config"] = (common / "config").read_text(encoding="utf-8")
            captured["head"] = (common / "HEAD").read_text(encoding="utf-8")
            captured["alternates"] = (common / "objects" / "info" / "alternates").read_text(
                encoding="utf-8"
            )
            captured["refs"] = str((common / "refs").is_dir())
            captured["attributes"] = str((common / "info" / "attributes").exists())
        return await git(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", capturing)
    await worktree_ops._release_channel_create()
    # POSIX access bits only: Windows `stat()` reports 0o777 for every directory,
    # so the mode assertion is a POSIX fact; the content assertions hold everywhere.
    if os.name == "posix":
        assert captured["mode"] == "0o700"
    assert "bare = false" in captured["config"]
    assert "filter" not in captured["config"]
    assert captured["head"].startswith("ref: ")
    assert captured["alternates"].strip() == os.path.realpath(os.path.join(repo, ".git", "objects"))
    assert captured["refs"] == "True"
    assert captured["attributes"] == "False"


@pytest.mark.asyncio
async def test_the_private_common_dir_is_deleted_after_a_successful_populate(repo, monkeypatch):
    git = _Git()
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["ok"] is True, got
    assert not Path(_populate_env(git)["GIT_COMMON_DIR"]).exists()


@pytest.mark.asyncio
async def test_the_private_common_dir_is_deleted_after_a_failed_populate(repo, monkeypatch):
    git = _Git(fail={"read-tree": (128, "", "fatal: unable to write new index file")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "git_failed"
    assert "unable to write new index file" in got["error"]
    assert not Path(_populate_env(git)["GIT_COMMON_DIR"]).exists()
    assert git.argv_with("move") is None
    assert git.argv_with("remove") is not None


@pytest.mark.asyncio
async def test_the_private_common_dir_is_deleted_when_the_populate_is_cancelled(repo, monkeypatch):
    git = _Git()
    seen: dict[str, str] = {}

    async def cancelling(cmd, **kw):
        if "read-tree" in cmd:
            seen["common"] = kw["env"]["GIT_COMMON_DIR"]
            raise asyncio.CancelledError
        return await git(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", cancelling)
    with pytest.raises(asyncio.CancelledError):
        await worktree_ops._release_channel_create()
    assert not Path(seen["common"]).exists()


@pytest.mark.asyncio
async def test_an_unstatable_git_dir_refuses_the_populate(repo, monkeypatch):
    """A populate whose index location had to be guessed must not run."""
    git = _Git(fail={"--absolute-git-dir": (128, "", "fatal: not a git repository")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "git_failed"
    assert git.argv_with("read-tree") is None
    assert git.argv_with("move") is None


@pytest.mark.asyncio
async def test_an_unstatable_object_store_refuses_the_populate(repo, monkeypatch):
    git = _Git(fail={"--git-common-dir": (128, "", "fatal: not a git repository")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert got["code"] == "git_failed"
    assert git.argv_with("read-tree") is None


@pytest.mark.asyncio
async def test_the_populate_error_is_redacted_before_it_reaches_the_page(repo, monkeypatch):
    secret = "https://x-access-token:ghp_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB@github.com/o/r"
    git = _Git(fail={"read-tree": (128, "", f"fatal: {secret}")})
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create()
    assert "ghp_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB" not in got["error"]


# --------------------------------------------------------------------------
# the hermetic populate, against real git
# --------------------------------------------------------------------------
def _fixture_git_env(home: Path) -> dict[str, str]:
    """Env for the fixture's own git calls: no ambient config, templates or identity.

    The attack under test is planted in the fixture repo's LOCAL config on purpose, so
    the operator's global and system config are pointed at ``os.devnull`` and
    ``init.templateDir`` is emptied -- otherwise an inherited template's hooks would be
    copied into the fixture repo and executed by its commit, which is a host-side effect
    of running the suite and would also muddy what this test proves.

    Pointing the global config at ``os.devnull`` also drops the suite's maintenance
    floor, so the entries that keep git's auto maintenance off are re-applied last.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TEMPLATE_DIR": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    env.update(git_maintenance_off_entries(env))
    return env


async def _spawn(argv: list[str], *, cwd: str, env: dict[str, str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Bounded, so a stalled git child fails this test by name instead of hanging the
    # xdist worker until the suite watchdog kills it (and every test sharing the
    # worker with it). Half the suite's --timeout=120.
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AssertionError(f"git did not finish within 60s: {argv}") from None
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


@pytest.mark.asyncio
@requires_git
async def test_the_hermetic_populate_runs_no_repo_declared_filter_and_writes_only_into_staging(
    tmp_path, monkeypatch
):
    """Both arms, against real git, from one fixture repository.

    The repository's own ``.git/config`` declares a REQUIRED ``filter.evil.smudge``
    whose command writes a marker file, and a ``core.worktree`` pointing outside the
    worktree; the committed tree carries a ``.gitattributes`` naming that filter for the
    file being checked out. An ordinary populate reads that config: the marker appears
    and the file lands outside. The production populate reads a private common dir
    instead: no marker, and the file lands in the staging path alone.

    The two arms share one repository so neither can pass by accident -- a fixture that
    failed to arm the attack would fail the control arm first.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _fixture_git_env(home)
    primary = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    primary.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = tmp_path / "filter-ran"
    (primary / "payload.txt").write_text("release payload\n", encoding="utf-8")
    (primary / ".gitattributes").write_text("payload.txt filter=evil\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q"],
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        ["git", "add", "payload.txt", ".gitattributes"],
        ["git", "commit", "-q", "-m", "one"],
        # The attack: an arbitrary command git EXECUTES on every file it writes, named
        # by a key whose `<name>` is the config author's choice -- which is why no fixed
        # set of neutralizer keys can disarm it.
        # git runs the driver through `sh -c` itself, so no nested shell; the marker
        # path is POSIX-spelled and shell-quoted so a Windows backslash path, or one
        # with a space, reaches the redirection intact.
        [
            "git",
            "config",
            "--local",
            "filter.evil.smudge",
            f"printf ran > {shlex.quote(marker.as_posix())}; cat",
        ],
        ["git", "config", "--local", "filter.evil.required", "true"],
        ["git", "config", "--local", "extensions.worktreeConfig", "true"],
        ["git", "config", "--local", "core.worktree", str(outside)],
    ):
        rc, _out, err = await _spawn(argv, cwd=str(primary), env=env)
        assert rc == 0, f"fixture setup failed: {argv} -> {err}"
    rc, out, err = await _spawn(["git", "rev-parse", "HEAD"], cwd=str(primary), env=env)
    assert rc == 0, err
    oid = out.strip()

    # --- control: an ordinary populate reads the repository's own config ---
    control = tmp_path / "release-channel-stable.staging.control"
    rc, _out, err = await _spawn(
        ["git", "worktree", "add", "--no-checkout", "--detach", str(control), oid],
        cwd=str(primary),
        env=env,
    )
    assert rc == 0, err
    rc, _out, err = await _spawn(
        ["git", "read-tree", "-u", "--reset", oid], cwd=str(control), env=env
    )
    assert rc == 0, err
    assert marker.exists(), "the fixture never armed the attack: the control filter did not run"
    assert (outside / "payload.txt").exists(), "the fixture never armed the core.worktree redirect"
    assert not (control / "payload.txt").exists()
    marker.unlink()
    (outside / "payload.txt").unlink()
    (outside / ".gitattributes").unlink()

    # --- the production populate, on the same armed repository ---
    async def real_git(cmd, *, cwd=None, env=None, **kw):
        base = dict(_fixture_git_env(home))
        base.update(env or {})
        return await _spawn(list(cmd), cwd=cwd or str(primary), env=base)

    monkeypatch.setattr(runtime, "_run_cmd", real_git)
    staging = tmp_path / "release-channel-stable.staging.hermetic"
    rc, _out, err = await _spawn(
        ["git", "worktree", "add", "--no-checkout", "--detach", str(staging), oid],
        cwd=str(primary),
        env=env,
    )
    assert rc == 0, err

    assert await worktree_ops._populate_release_tree(str(primary), str(staging), oid) is None

    assert not marker.exists(), "the repository's smudge filter was executed"
    assert not (outside / "payload.txt").exists(), "the populate wrote outside the worktree"
    assert (staging / "payload.txt").read_text(encoding="utf-8") == "release payload\n"
    assert (staging / ".gitattributes").exists()
    # HEAD is untouched and the tree is still detached: the populate writes files, the
    # `worktree add --detach` above is what pinned the release.
    rc, out, err = await _spawn(["git", "rev-parse", "HEAD"], cwd=str(staging), env=env)
    assert rc == 0 and out.strip() == oid, err
    rc, _out, _err = await _spawn(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=str(staging), env=env
    )
    assert rc != 0, "the populated staging worktree must remain detached"
    # Nothing of this call's own is left beside the operator's checkouts.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".release-channel-")]
    assert leftovers == []
    staged_leftovers = [p.name for p in staging.parent.iterdir() if "gitcommon" in p.name]
    assert staged_leftovers == []


# --------------------------------------------------------------------------
# the fleet payload
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fleet_publishes_a_channel_with_no_worktree_as_a_placeholder(repo, monkeypatch):
    """With no worktree there is no path, so this is not an extra ``worktrees`` row."""
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    row = await fleet_state._release_channel([])
    assert row is not None
    assert row["worktree"] is None
    assert row["name"] == release_channel_pin.WORKTREE_NAME
    assert row["lane"] == release_channel_pin.CHANNEL
    assert row["ref"] == "refs/tags/v0.5.0"
    assert row["version"] == "0.5.0"
    assert row["tip_version"] == "0.5.0"
    assert row["at_tip"] is None
    assert row["unpublished"] is False
    assert row["error"] is None
    assert row["name_taken_by_branch"] is False


@pytest.mark.asyncio
async def test_fleet_adopts_a_detached_worktree_and_reports_its_currency(repo, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head=_OTHER_OID, head_tag="v0.4.1"))
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert row["worktree"] == release_channel_pin.WORKTREE_NAME
    # The tree's OWN release, not the lane tip: a row showing the tip would rename the
    # checkout to a build it does not contain the moment a newer release ships.
    assert row["version"] == "0.4.1"
    assert row["tip_version"] == "0.5.0"
    assert row["at_tip"] is False


@pytest.mark.asyncio
async def test_a_tree_at_the_tip_reports_at_tip(repo, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head=_TIP_OID))
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert row["at_tip"] is True
    assert row["version"] == "0.5.0"


@pytest.mark.asyncio
async def test_an_adopted_row_on_no_release_tag_reports_no_version(repo, monkeypatch):
    """Adoption is for being detached; an operator at an arbitrary commit is on no release."""
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head=_OTHER_OID, head_tag=""))
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert row["worktree"] == release_channel_pin.WORKTREE_NAME
    assert row["version"] is None


@pytest.mark.asyncio
async def test_an_adopted_row_keeps_its_own_release_when_the_tip_lookup_fails(repo, monkeypatch):
    """The tree's release is a local fact; a failed resolve is no reason to withhold it."""
    git = _Git(head=_OTHER_OID, head_tag="v0.4.1")

    async def failing(cmd, **kw):
        if "for-each-ref" in cmd and "--points-at" not in cmd:
            return 128, "", "fatal: not a git repository"
        return await git(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", failing)
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert row["worktree"] == release_channel_pin.WORKTREE_NAME
    assert row["version"] == "0.4.1"
    assert row["tip_version"] is None
    # With no tip there is no answer, and the row says so rather than guessing.
    assert row["at_tip"] is None
    assert row["error"]


@pytest.mark.asyncio
async def test_fleet_does_not_adopt_a_branch_checkout_that_shares_the_name(repo, monkeypatch):
    """A user's own branch tree under the reserved name must never be driven as a pin."""

    async def on_a_branch(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/main\n", ""
        return await _Git(head=_OTHER_OID)(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", on_a_branch)
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert row["worktree"] is None
    assert row["name_taken_by_branch"] is True


@pytest.mark.asyncio
async def test_fleet_does_not_claim_a_branch_when_the_probe_could_not_read_head(repo, monkeypatch):
    """Three states, not two: an unreadable HEAD is neither adopted nor called a branch."""
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head=""))
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert row["worktree"] is None
    assert row["name_taken_by_branch"] is False
    assert "could not be read" in row["error"]


@pytest.mark.asyncio
async def test_fleet_adopts_nothing_when_two_checkouts_share_the_reserved_name(repo, monkeypatch):
    """Two registered trees named for the channel: neither is the pin, and the row says so.

    Adopting one would dress the other -- a branch checkout listed first, say -- in the
    adopted tree's version badge and take its Rebase away, because the page binds
    channel treatment to rows by name. The error is the surface an unadopted row that
    exists in the fleet has, and the name being present is what keeps Create off.
    """
    git = _Git(head=_OTHER_OID, head_tag="v0.4.1")
    monkeypatch.setattr(runtime, "_run_cmd", git)
    name = release_channel_pin.WORKTREE_NAME
    row = await fleet_state._release_channel(
        [{"path": f"/x/{name}", "branch": "main"}, {"path": f"/elsewhere/{name}"}]
    )
    assert row is not None
    assert row["worktree"] is None
    assert row["name_taken_by_branch"] is False
    assert row["at_tip"] is None
    assert row["version"] == "0.5.0"  # the tip Create would check out, not either tree's
    assert "2 registered worktrees are named" in row["error"]
    assert "can drive only one" in row["error"]
    # Neither tree is probed: there is no candidate to classify, so no HEAD is read.
    touched = [c for c in git.calls if any(f"/{name}" in str(a) for a in c)]
    assert touched == []


@pytest.mark.asyncio
async def test_fleet_ignores_the_main_checkout_and_a_pathless_entry(repo, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    row = await fleet_state._release_channel(
        [
            {"path": f"/x/{release_channel_pin.WORKTREE_NAME}", "is_main": True},
            {"path": None},
        ]
    )
    assert row is not None
    assert row["worktree"] is None


@pytest.mark.asyncio
async def test_fleet_flags_an_empty_channel_as_unpublished_not_an_error(repo, monkeypatch):
    """Create stays enabled: the resolver's own fetch is what fills the mirror."""
    monkeypatch.setattr(runtime, "_run_cmd", _Git(tags=()))
    row = await fleet_state._release_channel([])
    assert row is not None
    assert row["unpublished"] is True
    assert row["error"] is None
    assert row["ref"] is None
    assert row["version"] is None


@pytest.mark.asyncio
async def test_fleet_reports_a_git_failure_as_an_error_not_unpublished(repo, monkeypatch):
    async def broken(cmd, **kw):
        return 128, "", "fatal: not a git repository"

    monkeypatch.setattr(runtime, "_run_cmd", broken)
    row = await fleet_state._release_channel([])
    assert row is not None
    assert row["unpublished"] is False
    assert row["error"]


@pytest.mark.asyncio
async def test_fleet_release_channel_is_none_when_resolution_raises(repo, monkeypatch):
    """A channel that cannot be resolved must not take the whole fleet view down."""

    async def boom(**kw):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(release_channel_pin, "resolve_tip", boom)
    assert await fleet_state._release_channel([]) is None


@pytest.mark.asyncio
async def test_fleet_release_channel_is_none_without_a_checkout(monkeypatch):
    def no_repo():
        raise repository.RepoUnavailable("no checkout")

    monkeypatch.setattr(repository, "_repo", no_repo)
    assert await fleet_state._release_channel([]) is None


@pytest.mark.asyncio
async def test_an_overflowing_points_at_listing_becomes_the_rows_error(repo, monkeypatch):
    rows = "\n".join(f"v0.0.{n}" for n in range(release_channel_pin._MAX_RELEASE_REFS + 1))

    async def overflowing(cmd, **kw):
        if "for-each-ref" in cmd and "--points-at" in cmd:
            return 0, rows + "\n", ""
        return await _Git(head=_OTHER_OID)(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", overflowing)
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert row["worktree"] == release_channel_pin.WORKTREE_NAME
    assert row["version"] is None
    assert "refusing to name the release" in row["error"]


@pytest.mark.asyncio
async def test_worktree_overflow_does_not_overwrite_a_resolver_error(repo, monkeypatch):
    """The first error wins: the resolver's failure is the one the operator must act on."""
    rows = "\n".join(f"v0.0.{n}" for n in range(release_channel_pin._MAX_RELEASE_REFS + 1))

    async def overflowing(cmd, **kw):
        if "for-each-ref" in cmd and "--points-at" in cmd:
            return 0, rows + "\n", ""
        if "for-each-ref" in cmd:
            return 128, "", "fatal: the mirror could not be listed"
        return await _Git(head=_OTHER_OID)(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", overflowing)
    row = await fleet_state._release_channel([{"path": f"/x/{release_channel_pin.WORKTREE_NAME}"}])
    assert row is not None
    assert "cannot list tags" in row["error"]


@pytest.mark.asyncio
async def test_the_fleet_payload_carries_the_channel_under_its_own_key():
    """A pathless row injected into ``worktrees`` would be a fabricated worktree."""
    import inspect

    source = inspect.getsource(fleet_state._build_fleet)
    assert '"release_channel": await _release_channel(worktrees)' in source


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------
class _FakeRequest:
    """Enough of ``web.Request`` for the audited decorator and the handler."""

    def __init__(self, body: bytes = b""):
        self._body = body
        self.content_length = len(body) or None
        self.can_read_body = bool(body)

    async def read(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_the_route_audits_the_one_worktree_it_acts_on(monkeypatch):
    """The target is a constant, so no body can name what the mutation is recorded against."""
    recorded: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            recorded.append(kw)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())

    async def started(on_finish=None):
        return {"ok": True, "run_id": "r1", "name": release_channel_pin.WORKTREE_NAME}

    monkeypatch.setattr(worktree_ops, "_release_channel_create_start", started)
    resp = await http_api.api_dev_fleet_release_channel_create(
        _FakeRequest(b'{"name": "kirocrew-wt-somebody-elses-tree"}')
    )
    assert resp.status == 202
    assert len(recorded) == 1
    assert recorded[0]["resources"] == release_channel_pin.WORKTREE_NAME
    assert recorded[0]["tool_name"] == "dev_fleet_release_channel_create"
    assert recorded[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_the_route_answers_the_run_id_before_the_create_is_done(repo, monkeypatch):
    """The reply is the run, not the result: the proxy in front gives up at 30 s.

    The create is held open by a gate here, and the route must still have answered --
    with an id whose run is ``running`` -- before that gate opens.
    """

    class _Sel:
        def log_tool_invocation(self, **kw):
            return None

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(worktree_ops, "_CREATE_RID", None)
    gate = asyncio.Event()

    async def slow_create():
        await gate.wait()
        return {"ok": True, "lane": "stable", "ref": "refs/tags/v0.5.0", "version": "0.5.0"}

    monkeypatch.setattr(worktree_ops, "_release_channel_create", slow_create)
    resp = await http_api.api_dev_fleet_release_channel_create(_FakeRequest())
    assert resp.status == 202
    body = json.loads(resp.text)
    assert body["ok"] is True
    rid = body["run_id"]
    assert runtime._RUNS[rid]["status"] == "running"
    assert runtime._RUNS[rid]["label"] == "create " + release_channel_pin.WORKTREE_NAME
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime._RUNS[rid]["status"] == "done"
    assert runtime._RUNS[rid]["exit_code"] == 0
    assert runtime._RUNS[rid]["result"]["version"] == "0.5.0"


@pytest.mark.asyncio
async def test_the_run_record_carries_the_outcome_the_route_used_to_answer_with(repo, monkeypatch):
    """``/api/run`` is where the result now lives; the row's ``error`` reaches it scrubbed."""
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(worktree_ops, "_CREATE_RID", None)
    monkeypatch.setattr(runtime, "_redact", lambda text: text.replace("hunter2", "***"))

    async def failed_create():
        return {
            "ok": False,
            "code": "git_failed",
            "error": "helper hunter2 refused",
            "path": "/srv/hunter2/lane",
            "attempt": 1,
        }

    monkeypatch.setattr(worktree_ops, "_release_channel_create", failed_create)
    started = await worktree_ops._release_channel_create_start()
    rid = started["run_id"]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime._RUNS[rid]["exit_code"] == 1

    class _Req:
        query = {"id": rid}

    resp = await http_api.api_dev_fleet_run(_Req())
    snap = json.loads(resp.text)
    assert snap["status"] == "done"
    assert snap["result"]["code"] == "git_failed"
    # EVERY string in the result is scrubbed at this egress, not only `error`: the
    # owner that wrote the result carries no redactor, so the path it quotes is
    # scrubbed here or nowhere.
    assert snap["result"]["error"] == "helper *** refused"
    assert snap["result"]["path"] == "/srv/***/lane"
    assert snap["result"]["attempt"] == 1
    # The record itself is untouched: the scrub is the route's, applied on the way out.
    assert runtime._RUNS[rid]["result"]["error"] == "helper hunter2 refused"


@pytest.mark.asyncio
async def test_a_start_while_the_run_body_holds_the_lock_still_names_the_run(repo, monkeypatch):
    """The reattach must work for the WHOLE life of a create, not only before it locks.

    The run body holds ``_wt_lock(name)`` from the fetch to the move, which is minutes.
    A start arriving then must answer with the running create's id, not with the
    preflight's own ``busy`` (which names no run), or a reloaded page sees a failure.
    """
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(worktree_ops, "_CREATE_RID", None)
    gate = asyncio.Event()
    locked = asyncio.Event()

    async def locking_create():
        async with worktree_ops._wt_lock(release_channel_pin.WORKTREE_NAME):
            locked.set()
            await gate.wait()
        return {"ok": True}

    monkeypatch.setattr(worktree_ops, "_release_channel_create", locking_create)
    first = await worktree_ops._release_channel_create_start()
    task = runtime._ACTIVE_RUNS[first["run_id"]][0]
    try:
        # Bounded by name: the worker is the only thing that sets `locked`, and a worker
        # that fails before the lock is absorbed by `_start_task_run` without
        # signalling, so a bare wait would hang to the suite timeout instead of failing
        # here. `await_until` carries the lost-run bound (half the test's timeout).
        await await_until(locked, what="the create run to take the worktree lock")
        assert worktree_ops._wt_lock(release_channel_pin.WORKTREE_NAME).locked()
        second = await worktree_ops._release_channel_create_start()
        assert second["code"] == "busy"
        assert second["run_id"] == first["run_id"]
    finally:
        # Drain the worker either way, so nothing outlives the test; a worker that
        # does not finish once the gate opens is cancelled and the failure names it.
        gate.set()
        try:
            await await_until(task, what="the create run to finish once the gate opens")
        except AssertionError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
    # With the create finished and the lock released, another operation holding the
    # lock is still `busy` -- and names no run, because there is none.
    async with worktree_ops._wt_lock(release_channel_pin.WORKTREE_NAME):
        third = await worktree_ops._release_channel_create_start()
    assert third["code"] == "busy"
    assert "run_id" not in third


def test_dropping_the_channels_worktree_demotes_the_channel_to_its_placeholder():
    """A tombstoned removal must take the channel's adopted binding with the row.

    `_drop_worktrees` is the one reconciliation a removal mid-build gets; leaving
    `release_channel.worktree` set would publish a snapshot with no release row and no
    Create either, since the page offers Create only when `worktree` is null.
    """
    name = release_channel_pin.WORKTREE_NAME
    data = {
        "worktrees": [{"name": "main"}, {"name": name}],
        "release_channel": {
            "name": name,
            "worktree": name,
            "version": "0.4.9",
            "tip_version": "0.5.0",
            "at_tip": False,
            "name_taken_by_branch": False,
        },
    }
    out = fleet_state._drop_worktrees(data, {name})
    assert [w["name"] for w in out["worktrees"]] == ["main"]
    assert out["release_channel"]["worktree"] is None
    assert out["release_channel"]["at_tip"] is None
    assert out["release_channel"]["version"] == "0.5.0"  # back to what Create would check out
    assert out["release_channel"]["tip_version"] == "0.5.0"
    # Copied, never mutated: a response in flight may still hold the old dict.
    assert data["release_channel"]["worktree"] == name
    # Dropping an unrelated row leaves the channel alone.
    same = fleet_state._drop_worktrees(data, {"main"})
    assert same["release_channel"] is data["release_channel"]


@pytest.mark.asyncio
async def test_the_route_audits_how_the_run_ended_not_only_that_it_started(repo, monkeypatch):
    """Two entries per create: admission at the response, outcome at the terminal state."""
    recorded: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            recorded.append(kw)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(worktree_ops, "_CREATE_RID", None)

    async def refused_create():
        return {"ok": False, "code": "provenance_mismatch", "error": "refusing: v0.5.0 differs"}

    monkeypatch.setattr(worktree_ops, "_release_channel_create", refused_create)
    resp = await http_api.api_dev_fleet_release_channel_create(_FakeRequest())
    assert resp.status == 202
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [e["tool_name"] for e in recorded] == [
        "dev_fleet_release_channel_create",
        "dev_fleet_release_channel_create_result",
    ]
    assert recorded[0]["outcome"] == "success"  # the run was admitted
    assert recorded[1]["outcome"] == "denied"  # the run ended in a refusal
    assert recorded[1]["resources"] == release_channel_pin.WORKTREE_NAME
    assert "v0.5.0 differs" in recorded[1]["error"]


def test_the_result_audit_splits_outcomes_the_way_the_response_did():
    assert http_api._release_channel_outcome({"ok": True})[0] == "success"
    assert http_api._release_channel_outcome({"ok": False, "code": "exists"})[0] == "denied"
    assert http_api._release_channel_outcome({"ok": False, "code": "git_failed"})[0] == "failure"
    outcome, error = http_api._release_channel_outcome(None)
    assert outcome == "failure" and error


@pytest.mark.asyncio
async def test_a_second_start_while_one_runs_names_the_run_in_flight(repo, monkeypatch):
    """``busy`` with a ``run_id`` is an attach point, not a collision to report."""
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(worktree_ops, "_CREATE_RID", None)
    gate = asyncio.Event()

    async def slow_create():
        await gate.wait()
        return {"ok": True}

    monkeypatch.setattr(worktree_ops, "_release_channel_create", slow_create)
    first = await worktree_ops._release_channel_create_start()
    second = await worktree_ops._release_channel_create_start()
    assert second["ok"] is False
    assert second["code"] == "busy"
    assert second["run_id"] == first["run_id"]
    assert len(runtime._RUNS) == 1
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Once the run has left ``running``, the next start is a new run.
    third = await worktree_ops._release_channel_create_start()
    assert third["ok"] is True
    assert third["run_id"] != first["run_id"]
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_refusal_the_route_can_decide_is_answered_without_a_run(repo, monkeypatch):
    """``exists`` is a 409 at once, and no run record is written for it."""
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(worktree_ops, "_CREATE_RID", None)
    monkeypatch.setattr(
        repository, "_find_worktree", _found({"path": "/somewhere/release-channel-stable"})
    )

    class _Sel:
        def log_tool_invocation(self, **kw):
            return None

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    resp = await http_api.api_dev_fleet_release_channel_create(_FakeRequest())
    assert resp.status == 409
    body = json.loads(resp.text)
    assert body["code"] == "exists"
    assert body["run_id"] is None  # nothing to attach to
    assert runtime._RUNS == {}


@pytest.mark.asyncio
async def test_the_route_relays_the_in_flight_run_on_busy(monkeypatch):
    class _Sel:
        def log_tool_invocation(self, **kw):
            return None

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())

    async def busy(on_finish=None):
        return {"ok": False, "code": "busy", "error": "already running", "run_id": "r9"}

    monkeypatch.setattr(worktree_ops, "_release_channel_create_start", busy)
    resp = await http_api.api_dev_fleet_release_channel_create(_FakeRequest())
    assert resp.status == 409
    assert json.loads(resp.text)["run_id"] == "r9"


@pytest.mark.asyncio
async def test_a_task_run_that_raises_ends_like_a_failed_subprocess(monkeypatch):
    """Same record shape as ``_start_run``: ``exit_code -1`` and the reason as output."""
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    seen: list = []

    async def boom():
        raise RuntimeError("the lane fell over")

    rid = await runtime._start_task_run("create probe", boom, on_finish=seen.append)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    run = runtime._RUNS[rid]
    assert run["status"] == "done"
    assert run["exit_code"] == -1
    assert run["output"] == ["[error] the lane fell over"]
    assert "result" not in run
    assert seen == [None]
    assert rid not in runtime._ACTIVE_RUNS


@pytest.mark.asyncio
async def test_a_task_run_is_cancelled_by_shutdown_like_a_subprocess_run(monkeypatch):
    """The task is in ``_ACTIVE_RUNS`` so ``dev_fleet_cleanup`` reaches it."""
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    gate = asyncio.Event()

    async def forever():
        await gate.wait()
        return {"ok": True}

    rid = await runtime._start_task_run("create probe", forever)
    task, proc = runtime._ACTIVE_RUNS[rid]
    assert proc is None
    await asyncio.sleep(0)  # the worker is at its await, like a create mid-fetch
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = runtime._RUNS[rid]
    assert run["status"] == "done"
    assert run["exit_code"] == -1
    assert run["output"] == ["[shutdown] run aborted: gateway stopping"]


@pytest.mark.asyncio
async def test_a_task_run_is_refused_once_shutdown_began(monkeypatch):
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(runtime, "_SHUTDOWN_IN_PROGRESS", True)
    called = []

    async def work():
        called.append(True)
        return {"ok": True}

    with pytest.raises(RuntimeError, match="shutdown in progress"):
        await runtime._start_task_run("create probe", work)
    await asyncio.sleep(0)
    assert called == []
    assert runtime._RUNS == {}
    assert runtime._ACTIVE_RUNS == {}


@pytest.mark.asyncio
async def test_a_worktree_route_still_reads_its_target_from_the_body(monkeypatch):
    """The static target is opt-in per route, not a change to how the others behave."""
    recorded: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            recorded.append(kw)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())

    @http_api._audited("dev_fleet_probe")
    async def handler(request):
        from aiohttp import web

        return web.json_response({"ok": True})

    await handler(_FakeRequest(b'{"name": "kirocrew-wt-feature"}'))
    assert recorded[0]["resources"] == "kirocrew-wt-feature"


@pytest.mark.asyncio
async def test_a_refusal_answers_409_and_an_unexpected_git_failure_500(monkeypatch):
    """The status is picked from the result's code, so a client can tell the two apart."""

    class _Sel:
        def log_tool_invocation(self, **kw):
            return None

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    for code, expected in (
        ("exists", 409),
        ("path_exists", 409),
        ("busy", 409),
        ("fetch_failed", 409),
        ("unpublished", 409),
        ("provenance_mismatch", 409),
        ("git_failed", 500),
        (None, 500),
    ):

        async def refused(on_finish=None, _code=code):
            return {"ok": False, "code": _code, "error": "nope"}

        monkeypatch.setattr(worktree_ops, "_release_channel_create_start", refused)
        resp = await http_api.api_dev_fleet_release_channel_create(_FakeRequest())
        assert resp.status == expected, code


def test_every_refusal_code_the_mutation_emits_is_classified():
    """A code the frozenset does not list silently becomes a 500 the caller cannot act on."""
    import inspect
    import re

    source = inspect.getsource(worktree_ops._release_channel_preflight)
    source += inspect.getsource(worktree_ops._release_channel_create_start)
    source += inspect.getsource(worktree_ops._release_channel_create_locked)
    source += inspect.getsource(worktree_ops._stage_and_adopt)
    # Every code literal a ``"code":`` value can take, not just the first: the
    # resolve refusal picks between two codes in one conditional expression. The
    # match stops at the value's comma so a one-line dict's ``"error"`` key is
    # not read as a code.
    emitted = {
        code
        for line in source.splitlines()
        if '"code":' in line
        for code in re.findall(r'"([a-z_]+)"', line.split('"code":', 1)[1].split(",", 1)[0])
    }
    assert emitted - {"git_failed"} == set(worktree_ops._RELEASE_CHANNEL_REFUSALS)


# --------------------------------------------------------------------------
# prune
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_never_offers_a_detached_tree_however_it_was_made(tmp_path, monkeypatch):
    """`empty` reads clean-and-old as ABANDONMENT, which only holds for a tree on a branch.

    A detached tree has no branch and therefore no PR, and every commit it holds is
    reachable from the base branch -- so `own == 0` is the steady state rather than
    neglect, and for the channel pin it is exactly what the operator asked for.
    """
    tree = tmp_path / "release-channel-stable"
    tree.mkdir()
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((False, [])))
    monkeypatch.setattr(repository, "_git", _async_return("abc123"))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return(None))
    # Old enough that `empty` would be offered for a branch checkout.
    ManualClock(start=Path(tree).stat().st_ctime + 400_000).install(monkeypatch, worktree_ops)

    verdict = await worktree_ops._prunable(str(tree), None)
    assert verdict["ok"] is False
    assert verdict["code"] == "fresh"


@pytest.mark.asyncio
async def test_prune_still_offers_a_branch_checkout_holding_the_reserved_name(
    tmp_path, monkeypatch
):
    """Keyed on the SHAPE, not the basename: a branch tree is an ordinary feature tree."""
    tree = tmp_path / "release-channel-stable"
    tree.mkdir()
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((False, [])))
    monkeypatch.setattr(repository, "_git", _async_return("abc123"))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return(None))
    ManualClock(start=Path(tree).stat().st_ctime + 400_000).install(monkeypatch, worktree_ops)

    verdict = await worktree_ops._prunable(str(tree), "feat/borrowed-the-name")
    assert verdict["ok"] is True
    assert verdict["code"] == "empty"


def _async_return(value):
    async def _call(*_args, **_kw):
        return value

    return _call
