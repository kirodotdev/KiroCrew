"""Tests for ``GET /api/project/git/status`` and ``GET /api/project/git/log``."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_project_git_log, api_project_git_status


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get("/api/project/git/status", api_project_git_status)
    app.router.add_get("/api/project/git/log", api_project_git_log)
    return app


@pytest.fixture(autouse=True)
def passthrough_sandbox(monkeypatch):
    """Run git unwrapped: CI runners have no sandbox backend, and the handlers
    fail CLOSED without one (repo: False). The chokepoint's own behavior is
    covered by test_sandbox*/test_spawn_audit; these tests exercise the git
    parsing, so they pass argv through unchanged (the worktree tests' pattern).
    """
    from kiro_crew.dashboard.handlers import files as files_mod

    monkeypatch.setattr(
        files_mod, "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (list(argv), dict(os.environ), None),
    )


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _rmtree(path) -> None:
    """Delete a tree containing a git repo on any platform.

    Git marks objects and packs read-only, and Windows refuses to unlink a
    read-only file, so `shutil.rmtree` raises WinError 5 partway through.
    """
    for child in Path(path).rglob("*"):
        try:
            child.chmod(0o700)
        except OSError:
            pass
    shutil.rmtree(path)


def _git(cwd, *args) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    """One-commit repo template reused across tests."""
    root = tmp_path_factory.mktemp("git-status-seed") / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("line1\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "initial commit")
    return root


@pytest.fixture()
def repo(tmp_path, _repo_template):
    """A real git repo with one commit on branch ``trunk``."""
    root = tmp_path / "proj"
    shutil.copytree(_repo_template, root)
    return root


# ── /api/project/git/status tests ──


class TestMultiRepoDiscovery:
    """A project dir that is not itself a repo but contains them.

    A workspace laid out as one repo per package (``<ws>/src/<Package>``) has no
    repo at its own root, so upward-only resolution reported none at all.
    """

    @pytest.fixture()
    def workspace(self, tmp_path, _repo_template):
        """`ws/src/{PkgA,PkgB}` repos under a plain, non-repo workspace root."""
        src = tmp_path / "ws" / "src"
        src.mkdir(parents=True)
        for pkg in ("PkgA", "PkgB"):
            shutil.copytree(_repo_template, src / pkg)
        return tmp_path / "ws"

    @pytest.mark.asyncio
    async def test_groups_changes_per_descendant_repo(self, workspace, mock_sel):
        (workspace / "src" / "PkgA" / "a.txt").write_text("changed in A\n")
        (workspace / "src" / "PkgB" / "fresh.txt").write_text("new in B\n")

        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            resp = await client.get(f"/api/project/git/status?path={workspace}")
            data = await resp.json()

        assert data["repo"] is True
        groups = {g["name"]: g for g in data["repos"]}
        assert set(groups) == {os.path.join("src", "PkgA"), os.path.join("src", "PkgB")}
        assert [f["path"] for f in groups[os.path.join("src", "PkgA")]["files"]] == ["a.txt"]
        assert [f["path"] for f in groups[os.path.join("src", "PkgB")]["files"]] == ["fresh.txt"]
        assert all(g["branch"] == "trunk" for g in data["repos"])
        assert "branch" not in data
        # Nothing renders per-group ahead/behind, so the response does not carry it.
        assert all("ahead" not in g and "behind" not in g for g in data["repos"])

    @pytest.mark.asyncio
    async def test_every_row_names_its_own_repo(self, workspace, mock_sel):
        """`files` stays flat for existing consumers, with a per-row repoRoot."""
        (workspace / "src" / "PkgA" / "a.txt").write_text("changed in A\n")
        (workspace / "src" / "PkgB" / "a.txt").write_text("changed in B\n")

        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            resp = await client.get(f"/api/project/git/status?path={workspace}")
            data = await resp.json()

        assert [f["path"] for f in data["files"]] == ["a.txt", "a.txt"]
        roots = sorted(f["repoRoot"] for f in data["files"])
        assert roots == sorted(g["root"] for g in data["repos"])
        assert sum(len(g["files"]) for g in data["repos"]) == len(data["files"])

    @pytest.mark.asyncio
    async def test_a_group_starved_by_the_row_budget_says_so(
        self, workspace, mock_sel, monkeypatch
    ):
        """A repo whose rows the shared budget dropped must not read as clean.

        Rendering its count as 0 asserts the repo has nothing changed -- about the
        one repo the budget stopped us from reading.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        for pkg in ("PkgA", "PkgB"):
            for n in range(3):
                (workspace / "src" / pkg / f"f{n}.txt").write_text("x\n")
        monkeypatch.setattr(files_mod, "_REPO_ROW_BUDGET", 2)
        files_mod._repo_scan_cache.clear()

        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            data = await (
                await client.get(f"/api/project/git/status?path={workspace}")
            ).json()

        assert data["truncated"] is True
        assert len(data["files"]) == 2
        # BOTH cut shapes must be marked: the group whose rows the shared budget
        # dropped, AND the one that hit the per-repo cap inside `_run` while the
        # budget was still full -- the latter arrives already truncated, so a
        # length comparison alone cannot see it.
        assert all(g.get("truncated") for g in data["repos"]), [
            dict(name=g["name"], files=len(g["files"]), truncated=g.get("truncated"))
            for g in data["repos"]
        ]
        # No group may report an empty file list without saying it was cut short.
        for group in data["repos"]:
            if not group["files"]:
                assert group.get("truncated") is True, group["name"]

    @pytest.mark.asyncio
    async def test_clean_workspace_reports_no_files(self, workspace, mock_sel):
        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            resp = await client.get(f"/api/project/git/status?path={workspace}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["files"] == []
        assert len(data["repos"]) == 2

    @pytest.mark.asyncio
    async def test_dir_with_no_repos_anywhere_still_reports_false(self, tmp_path, mock_sel):
        bare = tmp_path / "bare"
        (bare / "src" / "notarepo").mkdir(parents=True)
        async with TestClient(TestServer(_make_app(str(bare)))) as client:
            resp = await client.get(f"/api/project/git/status?path={bare}")
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []
        assert "repos" not in data

    @pytest.mark.asyncio
    async def test_single_descendant_repo_takes_the_grouped_shape(
        self, tmp_path, _repo_template, mock_sel
    ):
        """One descendant is the same case as several, not an ancestor lookup.

        Re-probing the non-repo base answers "not a repo" and empties the panel,
        and a bare single-repo answer would hide both the repo's name and a
        per-repo refusal.
        """
        ws = tmp_path / "solo"
        src = ws / "src"
        src.mkdir(parents=True)
        shutil.copytree(_repo_template, src / "OnlyPkg")
        (src / "OnlyPkg" / "a.txt").write_text("changed\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            resp = await client.get(f"/api/project/git/status?path={ws}")
            data = await resp.json()

        assert data["repo"] is True
        assert [f["path"] for f in data["files"]] == ["a.txt"]
        assert data["files"][0]["repoRoot"] == str(src / "OnlyPkg")
        assert [g["name"] for g in data["repos"]] == [os.path.join("src", "OnlyPkg")]
        assert data["repos"][0]["files"] == data["files"]

    @pytest.mark.asyncio
    async def test_lone_descendant_refusal_is_visible_as_a_group(
        self, tmp_path, _repo_template, mock_sel
    ):
        """A repo refused for a filter driver must not read as clean."""
        ws = tmp_path / "filtered"
        src = ws / "src"
        src.mkdir(parents=True)
        pkg = src / "OnlyPkg"
        shutil.copytree(_repo_template, pkg)
        _git(pkg, "config", "filter.evil.clean", "touch /tmp/pwned")
        (pkg / "a.txt").write_text("changed\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            resp = await client.get(f"/api/project/git/status?path={ws}")
            data = await resp.json()

        assert data["repos"][0]["refused"] is True
        assert data["repos"][0]["files"] == []
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_single_repo_refusal_is_reported_on_the_response(
        self, repo, mock_sel
    ):
        """The project dir being the repo draws no group header, so the refusal
        has to reach the client as a field of its own."""
        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        (repo / "a.txt").write_text("changed\n")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert data["refused"] is True
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_capped_discovery_says_the_list_is_partial(
        self, tmp_path, _repo_template, mock_sel, monkeypatch
    ):
        """A repo dropped by a discovery bound must not read as nonexistent.

        Same failure mode the row-budget notice exists to prevent: a short list
        rendered as the whole workspace.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        ws = tmp_path / "big"
        src = ws / "src"
        src.mkdir(parents=True)
        for name in ("PkgA", "PkgB", "PkgC"):
            shutil.copytree(_repo_template, src / name)
        monkeypatch.setattr(files_mod, "_REPO_SCAN_MAX_REPOS", 2)
        files_mod._repo_scan_cache.clear()

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            resp = await client.get(f"/api/project/git/status?path={ws}")
            data = await resp.json()

        assert len(data["repos"]) == 2
        assert data["reposTruncated"] is True

    @pytest.mark.asyncio
    async def test_cached_root_swapped_for_an_outside_symlink_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """A cached root is re-checked at use time, not trusted for the TTL.

        Discovery refuses a symlinked child, but its answer is cached -- so a child
        replaced by a symlink out of the project inside that window would run git
        in an unauthorised directory and return its branch and paths.
        """
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        pkg = src / "PkgA"
        shutil.copytree(_repo_template, pkg)
        (pkg / "a.txt").write_text("inside the project\n")

        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            warm = await (await client.get(f"/api/project/git/status?path={ws}")).json()
            assert [f["path"] for f in warm["files"]] == ["a.txt"]

            # Same path, now pointing out of the project, while the scan is cached.
            _rmtree(pkg)
            os.symlink(outside, pkg)

            after = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert after.get("repos", []) == []
        assert after["files"] == []
        assert "confidential.txt" not in str(after)
        assert "secret-branch" not in str(after)

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs elevation on Windows")
    @pytest.mark.asyncio
    async def test_a_root_swapped_after_verification_is_refused(
        self, tmp_path, _repo_template, mock_sel, monkeypatch
    ):
        """Verification and the git spawn must agree on ONE identity.

        Re-resolving the root at use time proves only where the path points NOW, so
        a swap inside the window between the two passes every check.
        """
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")

        src = tmp_path / "ws" / "src"
        src.mkdir(parents=True)
        pkg = src / "PkgA"
        shutil.copytree(_repo_template, pkg)

        from kiro_crew.dashboard.handlers import files as files_mod

        real_verify = files_mod._verify_descendant_roots

        def _verify_then_swap(base: str, roots: list[str]):
            kept = real_verify(base, roots)
            # The window: verification has passed, git has not run yet.
            _rmtree(pkg)
            os.symlink(outside, pkg)
            return kept

        monkeypatch.setattr(files_mod, "_verify_descendant_roots", _verify_then_swap)

        async with TestClient(TestServer(_make_app(str(tmp_path / "ws")))) as client:
            data = await (
                await client.get(f"/api/project/git/status?path={tmp_path / 'ws'}")
            ).json()

        blob = str(data)
        assert "confidential.txt" not in blob, blob
        assert "secret-branch" not in blob, blob

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-shaped core.worktree path")
    @pytest.mark.asyncio
    async def test_a_contained_repo_pointing_its_worktree_outside_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """`.git` under the project is not enough: `core.worktree` moves the tree.

        The directory gate only ever sees a contained `.git`, and the grouped path
        skips the toplevel probe because discovery already named the root -- so
        without an effective-worktree check git happily reports a tree the project
        does not contain.
        """
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")

        src = tmp_path / "ws" / "src"
        src.mkdir(parents=True)
        pkg = src / "PkgA"
        shutil.copytree(_repo_template, pkg)
        _git(pkg, "config", "core.worktree", str(outside))

        async with TestClient(TestServer(_make_app(str(tmp_path / "ws")))) as client:
            data = await (
                await client.get(f"/api/project/git/status?path={tmp_path / 'ws'}")
            ).json()

        blob = str(data)
        assert "confidential.txt" not in blob, blob
        assert "secret-branch" not in blob, blob
        # Refused, not silently empty: the panel has to be able to say why.
        groups = data.get("repos") or []
        assert not groups or all(
            g.get("refused") or not g["files"] for g in groups
        ), blob

    @pytest.mark.asyncio
    async def test_a_symlinked_repo_inside_the_project_is_still_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """Containment is not the only test: a symlink is refused as such.

        Following one would report a repo twice under two names, and its target is
        only checkable at the moment of the check.
        """
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        real = src / "Real"
        shutil.copytree(_repo_template, real)
        (real / "a.txt").write_text("changed\n")
        os.symlink(real, src / "Alias")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert [g["name"] for g in data["repos"]] == [os.path.join("src", "Real")]

    @pytest.mark.asyncio
    async def test_a_git_dir_reached_through_a_link_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """The FORM of the indirection is never the test, only where it lands.

        A `.git` that is itself a link to an outside git directory reads as an
        ordinary directory to `isdir`, and a Windows junction is not even reported
        by `islink` -- so containment is decided on the resolved path instead.
        """
        ws = tmp_path / "ws"
        (ws / "src").mkdir(parents=True)
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "linked-secret")

        planted = ws / "src" / "Planted"
        planted.mkdir()
        os.symlink(outside / ".git", planted / ".git")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert data.get("repos", []) == []
        assert "linked-secret" not in str(data)

    @pytest.mark.asyncio
    async def test_gitdir_pointer_out_of_the_project_is_refused(
        self, tmp_path, _repo_template, mock_sel
    ):
        """A `.git` FILE can name a git directory anywhere.

        The candidate directory itself passes containment, so without following the
        pointer git runs against the outside repository and returns its branch plus
        its whole tracked file list -- absent locally, so reported as deletions.
        """
        ws = tmp_path / "ws"
        (ws / "src").mkdir(parents=True)
        outside = tmp_path / "outside"
        shutil.copytree(_repo_template, outside)
        _git(outside, "checkout", "-b", "secret-branch")
        (outside / "confidential.txt").write_text("must not leak\n")
        _git(outside, "add", "-A")
        _git(outside, "commit", "-qm", "secret")

        planted = ws / "src" / "Planted"
        planted.mkdir()
        (planted / ".git").write_text(f"gitdir: {outside / '.git'}\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        assert data.get("repos", []) == []
        assert data["files"] == []
        assert "secret-branch" not in str(data)
        assert "confidential.txt" not in str(data)

    @pytest.mark.asyncio
    async def test_a_linked_worktree_inside_the_project_still_works(
        self, tmp_path, _repo_template, mock_sel
    ):
        """The pointer form is legitimate when it stays inside the project."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        main_repo = src / "Main"
        shutil.copytree(_repo_template, main_repo)
        linked = src / "Linked"
        _git(main_repo, "worktree", "add", "-q", str(linked), "-b", "side")
        (linked / "a.txt").write_text("changed in the linked worktree\n")

        async with TestClient(TestServer(_make_app(str(ws)))) as client:
            data = await (await client.get(f"/api/project/git/status?path={ws}")).json()

        names = [g["name"] for g in data.get("repos", [])]
        assert os.path.join("src", "Linked") in names, names

    @pytest.mark.asyncio
    async def test_refresh_bypasses_the_root_scan_cache(
        self, workspace, _repo_template, mock_sel
    ):
        """A repo cloned after the first poll appears on an explicit refresh."""
        async with TestClient(TestServer(_make_app(str(workspace)))) as client:
            first = await (
                await client.get(f"/api/project/git/status?path={workspace}")
            ).json()
            shutil.copytree(_repo_template, workspace / "src" / "PkgC")
            cached = await (
                await client.get(f"/api/project/git/status?path={workspace}")
            ).json()
            refreshed = await (
                await client.get(f"/api/project/git/status?path={workspace}&refresh=1")
            ).json()

        assert len(first["repos"]) == 2
        assert len(cached["repos"]) == 2
        assert len(refreshed["repos"]) == 3

    def test_a_huge_directory_is_never_materialised_whole(self, tmp_path, monkeypatch):
        """The read itself is bounded, not the loop that follows it.

        ``sorted(os.scandir(...))`` materialises a whole directory before any cap is
        consulted, so a tree with millions of entries exhausts memory however low the
        per-entry budget is. Asserted at the seam the code actually uses -- patching
        the global ``os`` instead would outlive the test on platforms that finalise
        ``tmp_path`` before ``monkeypatch``.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        for n in range(8):
            (tmp_path / f"d{n}").mkdir()
        requested: list[int] = []
        real_islice = files_mod.islice

        def counting_islice(iterable, n):
            requested.append(n)
            return real_islice(iterable, n)

        monkeypatch.setattr(files_mod, "_REPO_SCAN_MAX_ENTRIES", 4)
        monkeypatch.setattr(files_mod, "islice", counting_islice)
        with patch.object(files_mod, "_run_git_bounded", return_value=(1, "", False)):
            files_mod._repo_scan_cache.clear()
            roots, bounded = files_mod._discover_repo_roots(str(tmp_path), ["git"], {})

        # An unbounded read would never ask islice for a count at all.
        assert requested, "the scan read a directory without bounding the pull"
        assert max(requested) <= 5, requested
        assert bounded is True
        assert roots == []

    def test_unreadable_entry_does_not_abort_discovery(self, tmp_path, monkeypatch):
        """An OSError from one entry probe is a skipped branch, not a 500."""
        from kiro_crew.dashboard.handlers import files as files_mod

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / ".git").mkdir()

        class _Hostile:
            def __init__(self, entry):
                self._entry = entry
                self.name = entry.name
                self.path = entry.path

            def is_dir(self, follow_symlinks=True):
                if self.name == "a":
                    raise PermissionError(self.path)
                return self._entry.is_dir(follow_symlinks=follow_symlinks)

        real_islice = files_mod.islice

        def hostile_islice(iterable, n):
            return [_Hostile(e) for e in real_islice(iterable, n)]

        monkeypatch.setattr(files_mod, "islice", hostile_islice)
        with patch.object(files_mod, "_run_git_bounded", return_value=(1, "", False)):
            files_mod._repo_scan_cache.clear()
            roots, bounded = files_mod._discover_repo_roots(str(tmp_path), ["git"], {})

        assert roots == [str(tmp_path / "b")]
        assert bounded is True

    def test_upward_root_is_normalised(self, tmp_path):
        """git spells the toplevel with forward slashes even on Windows.

        Discovery and the descendant scan must agree on separators: dispatch
        compares a root against the project dir as strings, and a row's
        ``repoRoot`` must not change shape with the route that resolved it.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        odd = f"{tmp_path}//sub/./repo"
        with patch.object(
            files_mod, "_run_git_bounded", return_value=(0, odd + "\n", False)
        ):
            files_mod._repo_scan_cache.clear()
            roots, _bounded = files_mod._discover_repo_roots(str(tmp_path), ["git"], {})

        assert roots == [os.path.normpath(odd)]
        assert roots[0] == str(tmp_path / "sub" / "repo")

    @pytest.mark.asyncio
    async def test_single_repo_answer_keeps_its_shape(self, repo, mock_sel):
        (repo / "a.txt").write_text("modified\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data.get("branch") == "trunk"
        assert "repos" not in data
        assert data["files"][0]["repoRoot"] == str(repo)


class TestGitStatus:
    @pytest.mark.asyncio
    async def test_non_repo_returns_repo_false(self, tmp_path, mock_sel):
        plain = tmp_path / "plain"
        plain.mkdir()
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/status?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_unknown_dir_is_refused(self, repo, tmp_path, mock_sel):
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={other}")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_staged_unstaged_untracked(self, repo, mock_sel):
        """Repo with staged, unstaged, and untracked files reports all."""
        # Modify tracked file (unstaged)
        (repo / "a.txt").write_text("modified\n")

        # Stage a new file
        (repo / "b.txt").write_text("new file\n")
        _git(repo, "add", "b.txt")

        # Untracked file
        (repo / "c.txt").write_text("untracked\n")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()

        assert data["repo"] is True
        assert "branch" in data
        assert data["branch"] == "trunk"

        paths = {f["path"]: f for f in data["files"]}
        # a.txt modified in worktree (unstaged)
        assert "a.txt" in paths
        a = paths["a.txt"]
        assert a["staged"] is False
        assert a["status"] == "M"

        # b.txt staged (added)
        assert "b.txt" in paths
        b = paths["b.txt"]
        assert b["staged"] is True
        assert b["status"] == "A"

        # c.txt untracked
        assert "c.txt" in paths
        c = paths["c.txt"]
        assert c["staged"] is False
        assert c["status"] == "?"

    @pytest.mark.asyncio
    async def test_clean_repo_empty_files(self, repo, mock_sel):
        """Clean repo returns empty files list."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_numstat_additions(self, repo, mock_sel):
        """Modified file gets additions/deletions from numstat."""
        (repo / "a.txt").write_text("line1\nline2\nline3\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        paths = {f["path"]: f for f in data["files"]}
        assert "a.txt" in paths
        a = paths["a.txt"]
        # Should have additions (2 new lines) and deletions (original line changed)
        assert "additions" in a or "deletions" in a


# ── /api/project/git/log tests ──


class TestGitLog:
    @pytest.mark.asyncio
    async def test_non_repo_returns_repo_false(self, tmp_path, mock_sel):
        plain = tmp_path / "plain"
        plain.mkdir()
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git/log?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert data["commits"] == []

    @pytest.mark.asyncio
    async def test_unknown_dir_is_refused(self, repo, tmp_path, mock_sel):
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={other}")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_returns_commits(self, repo, mock_sel):
        """Log returns at least the initial commit."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert len(data["commits"]) == 1
        c = data["commits"][0]
        assert c["message"] == "initial commit"
        assert c["author"] == "T"
        assert c["isHead"] is True
        assert "sha" in c
        assert "date" in c

    @pytest.mark.asyncio
    async def test_limit_parameter(self, repo, mock_sel):
        """limit=1 returns only 1 commit even if there are more."""
        # Add a second commit
        (repo / "d.txt").write_text("x\n")
        _git(repo, "add", "d.txt")
        _git(repo, "commit", "-qm", "second commit")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}&limit=1")
            data = await resp.json()
        assert len(data["commits"]) == 1
        assert data["commits"][0]["message"] == "second commit"
        assert data["commits"][0]["isHead"] is True

    @pytest.mark.asyncio
    async def test_limit_capped_at_100(self, repo, mock_sel):
        """limit > 100 is capped to 100."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}&limit=500")
            data = await resp.json()
        # Should still work, just capped
        assert data["repo"] is True
        assert len(data["commits"]) >= 1


class TestFilterDriverRefusal:
    """A repo whose own config names a content-filter driver gets a degraded
    empty answer: status re-hashes modified files through ``filter.<n>.clean``,
    so running any content-touching git against such a repo would execute a
    repository-supplied program on every poll."""

    @pytest.mark.asyncio
    async def test_status_refuses_clean_filter(self, repo, mock_sel):
        _git(repo, "config", "filter.evil.clean", "touch /tmp/pwned")
        (repo / "a.txt").write_text("modified\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["files"] == []

    @pytest.mark.asyncio
    async def test_log_refuses_process_filter(self, repo, mock_sel):
        _git(repo, "config", "filter.evil.process", "evil-daemon")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/log?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["commits"] == []

    @pytest.mark.asyncio
    async def test_clean_repo_is_not_refused(self, repo, mock_sel):
        """The probe only fires on filter drivers, not on ordinary config."""
        _git(repo, "config", "diff.noise.command", "irrelevant-but-not-a-filter")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        assert data["repo"] is True


class TestArrowFilename:
    @pytest.mark.skipif(os.name == "nt", reason="'>' is not a legal NTFS filename character")
    @pytest.mark.asyncio
    async def test_modified_file_named_like_a_rename_is_not_split(self, repo, mock_sel):
        """A literal 'foo -> bar' filename must survive intact: splitting it
        would point the row (and a subsequent open/save) at the unrelated
        file 'bar'."""
        name = "foo -> bar"
        (repo / name).write_text("v1\n")
        _git(repo, "add", name)
        _git(repo, "commit", "-qm", "add arrow file")
        (repo / name).write_text("v2\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        paths = [f["path"] for f in data["files"]]
        assert name in paths
        assert "bar" not in paths

    @pytest.mark.asyncio
    async def test_real_rename_still_reports_new_name(self, repo, mock_sel):
        _git(repo, "mv", "a.txt", "renamed.txt")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            data = await resp.json()
        paths = [f["path"] for f in data["files"]]
        assert "renamed.txt" in paths
        assert "a.txt -> renamed.txt" not in paths


class TestVanishedDirectory:
    @pytest.mark.asyncio
    async def test_dir_removed_between_check_and_spawn_returns_no_data(self, repo, mock_sel, monkeypatch):
        """TOCTOU: the project dir can vanish after the isdir gate and before
        the git spawn. The endpoint must answer degraded, never 500."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real_isdir = os.path.isdir

        def isdir_then_delete(path):
            ok = real_isdir(path)
            if ok and str(path) == str(repo):
                shutil.rmtree(repo, ignore_errors=True)
            return ok

        monkeypatch.setattr(files_mod.os.path, "isdir", isdir_then_delete)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git/status?path={repo}")
            assert resp.status == 200
            data = await resp.json()
        assert data["repo"] is False
        assert data["files"] == []
