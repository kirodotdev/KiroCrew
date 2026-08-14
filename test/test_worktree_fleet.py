"""Tests for worktree listing, recyclability verdicts, and removal.

Two layers: the repo-agnostic service against real throwaway git repos, and the
two HTTP endpoints (``GET /api/worktree/list``, ``POST /api/worktree/remove``)
including the slot-project allow-list barrier they share with creation.

The safety-critical assertions are the refusals — a dirty tree, the main
worktree, an unregistered path, and an undeterminable dirty state must every one
of them hold the tree rather than delete it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_handlers import _sanitize_worktree_binding
from kiro_crew.dashboard.handlers import worktree_fleet as fleet
from kiro_crew.dashboard.handlers.worktree_fleet import api_worktree_list, api_worktree_remove
from kiro_crew.worktree import service
from kiro_crew.worktree.access import allowed_repo_roots, match_allowed_root
from kiro_crew.worktree.git_exec import SandboxUnavailable, run_git
from kiro_crew.worktree.service import (
    VERDICT_ACTIVE,
    VERDICT_DIRTY_CHECK_FAILED,
    VERDICT_EMPTY,
    VERDICT_FRESH,
    VERDICT_MERGED,
    VERDICT_MERGED_DIRTY,
    WorktreeInfo,
    find_worktree,
    list_worktrees,
    prune_verdict,
    remove_worktree,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(*args: str, cwd) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    return proc.stdout.strip()


def _require_sandbox_exec() -> None:
    """Skip where the OS sandbox cannot establish isolation for a git spawn.

    Mirrors ``test_worktree_create``: a backend-availability probe is not enough,
    because some hosts pass it and then deny ``unshare(NEWNS)`` at exec time, which
    only the child can report.
    """
    try:
        proc = run_git(["--version"], os.getcwd())
    except SandboxUnavailable as exc:
        pytest.skip(f"sandboxed git cannot run here: {str(exc)[:120]}")
    except OSError as exc:  # pragma: no cover - no git binary at all
        pytest.skip(f"git unavailable: {exc}")
    if proc.returncode != 0:
        pytest.skip("sandboxed git cannot run here")


@pytest.fixture(autouse=True)
def worktrees_flag():
    """Run the endpoint tests with the beta flag ON, via the config it reads.

    The flag ships OFF, so without this every endpoint test would assert against
    the refusal instead of the behaviour it is there to pin. Patching the CONFIG
    rather than ``worktrees_enabled`` keeps the real function under test in every
    case, including the flag's own tests, which flip this fixture's value.
    """
    with patch.object(fleet, "KiroCrewConfig") as cfg:
        cfg.load.return_value.dashboard.worktrees_enabled = True
        yield cfg


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit on ``main`` and a fetchable ``origin``.

    A real ``origin/HEAD`` matters: without it the base ref falls back to ``HEAD``
    and the interesting verdicts (merged, active) cannot be distinguished at all.
    """
    _require_sandbox_exec()
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-q", "origin", "main", cwd=root)
    _git("remote", "set-head", "origin", "main", cwd=root)
    return root


def _add_worktree(root, name: str, branch: str):
    dest = root.parent / name
    _git("worktree", "add", "-q", "-b", branch, str(dest), cwd=root)
    return dest


def _commit(path, filename: str, body: str) -> None:
    (path / filename).write_text(body)
    _git("add", filename, cwd=path)
    _git("commit", "-q", "-m", f"add {filename}", cwd=path)


def _make_app(*projects: str, app_claim: str | None = "", user: str = "owner"):
    """App exposing one slot per allowed project directory, as the barrier reads."""

    @web.middleware
    async def claims(request: web.Request, handler):
        if app_claim is not None:
            request["app"] = app_claim
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[claims])
    state = MagicMock()
    state.owner_id = "owner"
    state._slots = {
        f"chat-{i}": MagicMock(project=str(p)) for i, p in enumerate(projects) if p
    }
    app["state"] = state
    app.router.add_get("/api/worktree/list", api_worktree_list)
    app.router.add_post("/api/worktree/remove", api_worktree_remove)
    return app


class TestPruneVerdict:
    """The classification table, exercised directly so every branch is covered."""

    def test_undeterminable_dirty_state_is_never_recyclable(self):
        code, ok = prune_verdict(WorktreeInfo(path="/x", dirty=None))
        assert (code, ok) == (VERDICT_DIRTY_CHECK_FAILED, False)

    def test_unknown_base_is_never_recyclable(self):
        code, ok = prune_verdict(WorktreeInfo(path="/x", dirty=False, base_known=False))
        assert ok is False
        assert code != VERDICT_MERGED

    def test_main_worktree_is_never_recyclable(self):
        code, ok = prune_verdict(WorktreeInfo(path="/x", dirty=False, is_main=True))
        assert (code, ok) == (VERDICT_ACTIVE, False)

    def test_landed_work_is_recyclable(self):
        info = WorktreeInfo(path="/x", dirty=False, own_commits=3, ahead=0)
        assert prune_verdict(info) == (VERDICT_MERGED, True)

    def test_landed_but_dirty_is_refused(self):
        info = WorktreeInfo(path="/x", dirty=True, own_commits=3, ahead=0)
        assert prune_verdict(info) == (VERDICT_MERGED_DIRTY, False)

    def test_unlanded_commits_are_active(self):
        info = WorktreeInfo(path="/x", dirty=False, own_commits=2, ahead=2)
        assert prune_verdict(info) == (VERDICT_ACTIVE, False)

    def test_young_empty_tree_is_kept(self):
        info = WorktreeInfo(path="/x", dirty=False, own_commits=0, age_s=60)
        assert prune_verdict(info) == (VERDICT_FRESH, False)

    def test_old_empty_tree_is_recyclable(self):
        info = WorktreeInfo(path="/x", dirty=False, own_commits=0, age_s=72 * 3600)
        assert prune_verdict(info) == (VERDICT_EMPTY, True)

    def test_dirty_empty_tree_is_kept(self):
        info = WorktreeInfo(path="/x", dirty=True, own_commits=0, age_s=72 * 3600)
        assert prune_verdict(info) == (VERDICT_ACTIVE, False)


class TestListWorktrees:
    def test_main_worktree_is_flagged_and_linked_trees_listed(self, repo):
        _add_worktree(repo, "proj-wt-a", "feat/a")
        result = list_worktrees(str(repo))
        by_main = {w.is_main: w for w in result.worktrees}
        assert set(by_main) == {True, False}
        assert by_main[True].path.endswith("proj")
        assert by_main[False].branch == "feat/a"

    def test_base_is_resolved_to_a_commit_not_a_ref_name(self, repo):
        result = list_worktrees(str(repo))
        assert result.base_ref == "origin/HEAD"
        assert len(result.base_sha) == 40

    def test_fresh_worktree_has_no_commits_of_its_own(self, repo):
        _add_worktree(repo, "proj-wt-b", "feat/b")
        tree = [w for w in list_worktrees(str(repo)).worktrees if not w.is_main][0]
        assert (tree.own_commits, tree.ahead, tree.dirty) == (0, 0, False)
        assert tree.verdict == VERDICT_FRESH

    def test_unlanded_commit_reads_as_active(self, repo):
        wt = _add_worktree(repo, "proj-wt-c", "feat/c")
        _commit(wt, "c.txt", "c\n")
        tree = [w for w in list_worktrees(str(repo)).worktrees if not w.is_main][0]
        assert (tree.own_commits, tree.ahead) == (1, 1)
        assert (tree.verdict, tree.recyclable) == (VERDICT_ACTIVE, False)

    def test_squash_merged_work_reads_as_merged_without_gh(self, repo):
        """Patch identity, not ancestry: the branch's commit is gone from history
        after a squash, but its patch is present upstream, so `git cherry` reports
        zero patch-unique commits."""
        wt = _add_worktree(repo, "proj-wt-d", "feat/d")
        _commit(wt, "d.txt", "d\n")
        _git("merge", "--squash", "feat/d", cwd=repo)
        _git("commit", "-q", "-m", "squash feat/d", cwd=repo)
        _git("push", "-q", "origin", "main", cwd=repo)

        tree = [w for w in list_worktrees(str(repo)).worktrees if not w.is_main][0]
        assert tree.own_commits >= 1
        assert tree.ahead == 0
        assert (tree.verdict, tree.recyclable) == (VERDICT_MERGED, True)

    def test_uncommitted_change_marks_the_tree_dirty(self, repo):
        wt = _add_worktree(repo, "proj-wt-e", "feat/e")
        (wt / "scratch.log").write_text("untracked counts\n")
        tree = [w for w in list_worktrees(str(repo)).worktrees if not w.is_main][0]
        assert tree.dirty is True
        assert tree.recyclable is False

    def test_worktree_missing_on_disk_is_reported_not_dropped(self, repo):
        wt = _add_worktree(repo, "proj-wt-f", "feat/f")
        shutil.rmtree(wt)
        paths = [w.path for w in list_worktrees(str(repo)).worktrees]
        assert str(wt) in paths
        gone = [w for w in list_worktrees(str(repo)).worktrees if w.path == str(wt)][0]
        assert gone.recyclable is False


class TestRemoveWorktree:
    def test_clean_tree_is_removed(self, repo):
        wt = _add_worktree(repo, "proj-wt-g", "feat/g")
        payload, status = remove_worktree(str(repo), str(wt))
        assert status == 200, payload
        assert not os.path.isdir(wt)

    def test_branch_survives_removal(self, repo):
        """Removing a tree must not delete its branch: a leftover ref costs bytes,
        a wrongly deleted one costs commits."""
        wt = _add_worktree(repo, "proj-wt-h", "feat/h")
        _commit(wt, "h.txt", "h\n")
        remove_worktree(str(repo), str(wt), force=True)
        assert _git("rev-parse", "--verify", "refs/heads/feat/h", cwd=repo)

    def test_dirty_tree_is_refused_without_force(self, repo):
        wt = _add_worktree(repo, "proj-wt-i", "feat/i")
        (wt / "wip.txt").write_text("unsaved\n")
        payload, status = remove_worktree(str(repo), str(wt))
        assert status == 409
        assert payload["dirty"] is True
        assert os.path.isdir(wt)

    def test_dirty_tree_is_removed_with_force(self, repo):
        wt = _add_worktree(repo, "proj-wt-j", "feat/j")
        (wt / "wip.txt").write_text("unsaved\n")
        payload, status = remove_worktree(str(repo), str(wt), force=True)
        assert status == 200, payload
        assert not os.path.isdir(wt)

    def test_main_worktree_is_refused(self, repo):
        payload, status = remove_worktree(str(repo), str(repo))
        assert status == 400
        assert "main worktree" in payload["error"]
        assert os.path.isdir(repo)

    def test_unregistered_path_is_refused(self, repo, tmp_path):
        stranger = tmp_path / "not-a-worktree"
        stranger.mkdir()
        payload, status = remove_worktree(str(repo), str(stranger))
        assert status == 404
        assert os.path.isdir(stranger)

    def test_undeterminable_dirty_state_is_refused(self, repo):
        """A tree whose cleanliness cannot be read is held, not deleted: "we could
        not tell" and "there is nothing to lose" must not collapse together."""
        wt = _add_worktree(repo, "proj-wt-k", "feat/k")
        unknown = WorktreeInfo(path=str(wt), dirty=None)
        with patch(
            "kiro_crew.worktree.service.find_worktree", return_value=unknown
        ):
            payload, status = remove_worktree(str(repo), str(wt))
        assert status == 409
        assert os.path.isdir(wt)


class TestFeatureFlag:
    """The switch must be a real off switch, not only a hidden control.

    Hiding the composer button would leave the endpoints reachable by anything
    that already knows the URL, which is not what turning a beta feature off is
    asking for.
    """

    @pytest.mark.asyncio
    async def test_list_is_refused_when_the_feature_is_off(self, repo, worktrees_flag):
        worktrees_flag.load.return_value.dashboard.worktrees_enabled = False
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/worktree/list", params={"repo": str(repo)})
            assert resp.status == 403
            assert "turned off" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_remove_is_refused_when_the_feature_is_off(self, repo, worktrees_flag):
        wt = _add_worktree(repo, "proj-wt-flag", "feat/flag")
        worktrees_flag.load.return_value.dashboard.worktrees_enabled = False
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/remove",
                json={"repo": str(repo), "path": str(wt), "force": True},
            )
            assert resp.status == 403
        # Refusing must not have touched the tree.
        assert os.path.isdir(wt)

    @pytest.mark.asyncio
    async def test_list_works_when_the_feature_is_on(self, repo):
        """The autouse fixture supplies the ON state every other endpoint test
        relies on; assert it here so the two states sit side by side."""
        _add_worktree(repo, "proj-wt-flagon", "feat/flagon")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/worktree/list", params={"repo": str(repo)})
            assert resp.status == 200

    def test_a_config_read_failure_reads_as_off(self, worktrees_flag):
        """Fail closed: an unreadable config must not switch a beta surface on."""
        worktrees_flag.load.side_effect = RuntimeError("boom")
        assert fleet.worktrees_enabled() is False


class TestAccessBarrier:
    """What the caller may name, and why a session inside a worktree still can."""

    def test_a_slot_project_is_allowed(self, repo):
        state = MagicMock()
        state._slots = {"a": MagicMock(project=str(repo), worktree={})}
        roots = allowed_repo_roots(state)
        assert match_allowed_root(str(repo), roots) == os.path.realpath(str(repo))

    def test_the_repo_of_a_worktree_bound_slot_is_allowed(self, repo):
        """A session that entered a worktree has `project` = the tree, so without
        the binding half of the barrier it could no longer list, enter or leave the
        trees of its own repository."""
        wt = _add_worktree(repo, "proj-wt-barrier", "feat/barrier")
        state = MagicMock()
        state._slots = {
            "a": MagicMock(project=str(wt), worktree={"repo": str(repo), "path": str(wt)}),
        }
        roots = allowed_repo_roots(state)
        assert match_allowed_root(str(repo), roots) == os.path.realpath(str(repo))

    def test_an_unrelated_directory_is_still_refused(self, repo, tmp_path):
        outsider = tmp_path / "elsewhere"
        outsider.mkdir()
        state = MagicMock()
        state._slots = {"a": MagicMock(project=str(repo), worktree={})}
        assert match_allowed_root(str(outsider), allowed_repo_roots(state)) is None

    def test_a_sibling_with_a_shared_prefix_is_refused(self, repo):
        """`/repo-evil` must not pass as inside `/repo`."""
        sibling = repo.parent / f"{repo.name}-evil"
        sibling.mkdir()
        state = MagicMock()
        state._slots = {"a": MagicMock(project=str(repo), worktree={})}
        assert match_allowed_root(str(sibling), allowed_repo_roots(state)) is None


class TestListCaching:
    """The listing is cached briefly because every probe is a sandboxed spawn."""

    @pytest.mark.asyncio
    async def test_a_second_read_inside_the_ttl_does_not_re_measure(self, repo):
        _add_worktree(repo, "proj-wt-cache", "feat/cache")
        service.invalidate_cache()
        first = await service.list_worktrees_cached(str(repo))
        with patch.object(service, "_prepare", side_effect=AssertionError("re-measured")):
            second = await service.list_worktrees_cached(str(repo))
        assert [w.path for w in second.worktrees] == [w.path for w in first.worktrees]

    @pytest.mark.asyncio
    async def test_zero_ttl_forces_a_re_measure(self, repo):
        _add_worktree(repo, "proj-wt-fresh", "feat/fresh")
        service.invalidate_cache()
        await service.list_worktrees_cached(str(repo))
        with patch.object(service, "_prepare", wraps=service._prepare) as spy:
            await service.list_worktrees_cached(str(repo), ttl=0.0)
        assert spy.called

    @pytest.mark.asyncio
    async def test_removal_invalidates_so_the_tree_stops_being_listed(self, repo):
        wt = _add_worktree(repo, "proj-wt-inval", "feat/inval")
        service.invalidate_cache()
        before = await service.list_worktrees_cached(str(repo))
        assert str(wt) in [w.path for w in before.worktrees]

        remove_worktree(str(repo), str(wt))
        service.invalidate_cache(str(repo))
        after = await service.list_worktrees_cached(str(repo))
        assert str(wt) not in [w.path for w in after.worktrees]

    @pytest.mark.asyncio
    async def test_sized_and_unsized_reads_are_cached_separately(self, repo):
        _add_worktree(repo, "proj-wt-sizekey", "feat/sizekey")
        service.invalidate_cache()
        plain = await service.list_worktrees_cached(str(repo))
        sized = await service.list_worktrees_cached(str(repo), with_size=True)
        assert all(w.size_bytes == 0 for w in plain.worktrees)
        assert any(w.size_bytes > 0 for w in sized.worktrees if not w.is_main)


class TestCommitCounts:
    def test_ahead_and_behind_come_from_one_call(self, repo):
        """`--left-right --count` yields both numbers, so the pair must be read in
        the right order: left is base-only (behind), right is HEAD-only (ahead)."""
        wt = _add_worktree(repo, "proj-wt-counts", "feat/counts")
        _commit(wt, "mine.txt", "mine\n")
        _commit(repo, "theirs.txt", "theirs\n")
        _git("push", "-q", "origin", "main", cwd=repo)

        base = _git("rev-parse", "--verify", "origin/HEAD^{commit}", cwd=repo)
        own, behind = service._ahead_behind(str(wt), base)
        assert (own, behind) == (1, 1)


class TestFindWorktree:
    def test_returns_none_for_a_path_git_does_not_list(self, repo, tmp_path):
        assert find_worktree(str(repo), str(tmp_path / "nope")) is None

    def test_finds_a_registered_tree(self, repo):
        wt = _add_worktree(repo, "proj-wt-l", "feat/l")
        found = find_worktree(str(repo), str(wt))
        assert found is not None and found.branch == "feat/l"


class TestListEndpoint:
    @pytest.mark.asyncio
    async def test_requires_repo(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/worktree/list")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_repo_outside_slot_projects_is_refused(self, repo, tmp_path):
        outsider = tmp_path / "elsewhere"
        outsider.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/worktree/list", params={"repo": str(outsider)})
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_app_callers_are_denied(self, repo):
        app = _make_app(str(repo), app_claim="some-app")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/worktree/list", params={"repo": str(repo)})
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_granted_subdirectory_does_not_grant_its_repo_root(self, repo):
        """Resolving upward from an allowed subdirectory can land on a root that
        was never granted, so the toplevel is re-checked against the barrier."""
        sub = repo / "nested"
        sub.mkdir()
        async with TestClient(TestServer(_make_app(str(sub)))) as client:
            resp = await client.get("/api/worktree/list", params={"repo": str(sub)})
            assert resp.status == 403
            assert (await resp.json())["code"] == "repo_root_not_allowed"

    @pytest.mark.asyncio
    async def test_a_sensitive_repo_root_is_refused(self, repo, monkeypatch):
        """The resolved root is screened too, not only the submitted path — they
        differ whenever the caller names a subdirectory."""
        sub = repo / "nested"
        sub.mkdir()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.worktree_fleet.is_sensitive_path",
            lambda p: os.path.realpath(p) == os.path.realpath(str(repo)),
        )
        app = _make_app(str(sub), str(repo))
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/worktree/list", params={"repo": str(sub)})
            assert resp.status == 403
            assert (await resp.json())["code"] == "root_sensitive_path"

    @pytest.mark.asyncio
    async def test_lists_the_repos_worktrees(self, repo):
        _add_worktree(repo, "proj-wt-m", "feat/m")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/worktree/list", params={"repo": str(repo)})
            assert resp.status == 200
            body = await resp.json()
        branches = {w["branch"] for w in body["worktrees"]}
        assert "feat/m" in branches
        assert body["base_ref"] == "origin/HEAD"

    @pytest.mark.asyncio
    async def test_size_is_opt_in(self, repo):
        _add_worktree(repo, "proj-wt-n", "feat/n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            plain = await (
                await client.get("/api/worktree/list", params={"repo": str(repo)})
            ).json()
            sized = await (
                await client.get(
                    "/api/worktree/list", params={"repo": str(repo), "size": "1"}
                )
            ).json()
        assert plain["disk_bytes"] == 0
        assert sized["disk_bytes"] > 0


class TestRemoveEndpoint:
    @pytest.mark.asyncio
    async def test_requires_repo_and_path(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post("/api/worktree/remove", json={"repo": str(repo)})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_dirty_tree_needs_the_force_flag(self, repo):
        wt = _add_worktree(repo, "proj-wt-o", "feat/o")
        (wt / "wip.txt").write_text("unsaved\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            refused = await client.post(
                "/api/worktree/remove", json={"repo": str(repo), "path": str(wt)}
            )
            assert refused.status == 409
            assert os.path.isdir(wt)

            forced = await client.post(
                "/api/worktree/remove",
                json={"repo": str(repo), "path": str(wt), "force": True},
            )
            assert forced.status == 200, await forced.text()
        assert not os.path.isdir(wt)

    @pytest.mark.asyncio
    async def test_truthy_non_boolean_force_does_not_authorize_the_delete(self, repo):
        """Only a JSON ``true`` consents to destroying uncommitted work.

        ``"false"`` is a plausible thing for a client to send and is truthy in
        Python, so a ``bool()`` coercion would read it as consent and delete the
        tree. Each of these must still be refused with 409, with the tree and its
        uncommitted file intact.
        """
        for i, value in enumerate(["false", "0", 1, {}, [], "no"]):
            wt = _add_worktree(repo, f"proj-wt-coerce{i}", f"feat/coerce{i}")
            (wt / "wip.txt").write_text("unsaved\n")
            async with TestClient(TestServer(_make_app(str(repo)))) as client:
                resp = await client.post(
                    "/api/worktree/remove",
                    json={"repo": str(repo), "path": str(wt), "force": value},
                )
                assert resp.status == 409, f"force={value!r}: {await resp.text()}"
            assert os.path.isdir(wt), f"force={value!r} deleted the tree"
            assert (wt / "wip.txt").exists(), f"force={value!r} destroyed work"

    @pytest.mark.asyncio
    async def test_path_outside_the_repo_is_refused(self, repo, tmp_path):
        """The repo is allow-listed but the path is not one of its worktrees."""
        stranger = tmp_path / "stranger"
        stranger.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/remove",
                json={"repo": str(repo), "path": str(stranger), "force": True},
            )
            assert resp.status == 404
        assert os.path.isdir(stranger)

    @pytest.mark.asyncio
    async def test_repo_outside_slot_projects_is_refused(self, repo, tmp_path):
        wt = _add_worktree(repo, "proj-wt-p", "feat/p")
        async with TestClient(TestServer(_make_app(str(tmp_path / "other")))) as client:
            resp = await client.post(
                "/api/worktree/remove",
                json={"repo": str(repo), "path": str(wt), "force": True},
            )
            assert resp.status == 403
        assert os.path.isdir(wt)


class TestWorktreeBindingSanitizer:
    """The slot binding is a LABEL. It is validated for shape, and nothing
    downstream trusts it for a filesystem decision."""

    def test_binding_matching_the_project_is_kept(self):
        out = _sanitize_worktree_binding(
            {"repo": "/r", "branch": "feat/x", "base": "origin/main", "path": "/r-wt-x"},
            "/r-wt-x",
        )
        assert out == {
            "repo": "/r",
            "branch": "feat/x",
            "base": "origin/main",
            "path": "/r-wt-x",
        }

    def test_binding_for_a_different_path_is_dropped(self):
        assert (
            _sanitize_worktree_binding({"repo": "/r", "path": "/r-wt-other"}, "/r-wt-x")
            == {}
        )

    def test_binding_without_a_repo_is_dropped(self):
        assert _sanitize_worktree_binding({"path": "/r-wt-x"}, "/r-wt-x") == {}

    def test_unknown_fields_are_stripped(self):
        out = _sanitize_worktree_binding(
            {"repo": "/r", "path": "/r-wt-x", "cmd": "rm -rf /"}, "/r-wt-x"
        )
        assert "cmd" not in out

    def test_non_dict_and_empty_project_are_dropped(self):
        assert _sanitize_worktree_binding("nope", "/r-wt-x") == {}
        assert _sanitize_worktree_binding({"repo": "/r", "path": "/r-wt-x"}, "") == {}
