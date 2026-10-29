"""Tests for nested repositories in ``GET /api/project/tree``.

``git ls-files`` names a submodule (a gitlink) as one entry printed like a file,
and an untracked nested clone as one entry with a trailing slash. Both are
folders on disk: they must be rows the tree can open, named in
``nestedRepoDirectories``, and ``?dir=`` must list what is inside them. The
project's git cannot, so the identity-checked walk does, which does not apply
the nested repository's own ``.gitignore``.

The app, the sandbox passthrough and the project fixtures are
``test_project_tree.py``'s, imported so pytest registers them here too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_project_tree import (  # noqa: F401
    _git,
    _make_app,
    mock_sel,
    passthrough_sandbox,
)


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-qm", message)


@pytest.fixture()
def workspace(tmp_path):
    """A repo holding a submodule, an untracked nested clone and a plain folder.

    The submodule has its own ``.gitignore`` (``build/``, ``out/``), a source
    folder, two ignored build artifacts and one untracked file; the nested clone has a file
    and a folder; ``plain/`` is an ordinary tracked folder.
    """
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-q", "-b", "trunk")
    (inner / "README.md").write_text("inner\n")
    (inner / "src").mkdir()
    (inner / "src" / "greet.py").write_text("x = 1\n")
    (inner / ".gitignore").write_text("build/\nout/\n")
    _commit_all(inner, "inner")

    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    (root / "plain").mkdir()
    (root / "plain" / "a.txt").write_text("a\n")
    (root / "README.md").write_text("root\n")
    _commit_all(root, "root")
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "libs/sub")
    _git(root, "commit", "-qm", "add submodule")

    (root / "libs" / "sub" / "build").mkdir()
    (root / "libs" / "sub" / "build" / "out.bin").write_text("junk\n")
    (root / "libs" / "sub" / "out").mkdir()
    (root / "libs" / "sub" / "out" / "app.bin").write_text("junk\n")
    (root / "libs" / "sub" / "new.txt").write_text("untracked\n")

    clone = root / "scratch" / "clone"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "trunk")
    (clone / "notes.txt").write_text("n\n")
    (clone / "lib").mkdir()
    (clone / "lib" / "util.py").write_text("y = 2\n")
    return root


async def _get(project: Path, **query: str) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(str(project)))) as client:
        resp = await client.get("/api/project/tree", params={"path": str(project), **query})
        return resp.status, await resp.json()


@pytest.mark.usefixtures("mock_sel")
class TestNestedReposInTheWholeListing:
    @pytest.mark.asyncio
    async def test_a_submodule_and_an_untracked_clone_are_folder_rows(self, workspace):
        status, data = await _get(workspace)
        assert status == 200
        assert data["repo"] is True
        # Neither is a file row: a file row cannot be opened (the reported bug).
        assert "libs/sub" not in data["paths"]
        assert "scratch/clone" not in data["paths"]
        assert "scratch/clone/" not in data["paths"]
        assert {"libs", "libs/sub", "scratch", "scratch/clone"} <= set(data["directories"])
        assert data["nestedRepoDirectories"] == ["libs/sub", "scratch/clone"]
        # Ordinary entries are untouched, and a nested repo is not a cut.
        assert "plain/a.txt" in data["paths"]
        assert ".gitmodules" in data["paths"]
        assert data["truncated"] is False
        assert data["truncatedDirectories"] == []

    @pytest.mark.asyncio
    async def test_a_listing_without_nested_repos_names_none(self, tmp_path):
        root = tmp_path / "plainrepo"
        root.mkdir()
        _git(root, "init", "-q", "-b", "trunk")
        (root / "a.txt").write_text("a\n")
        _commit_all(root, "a")
        _, data = await _get(root)
        assert data["nestedRepoDirectories"] == []

    @pytest.mark.asyncio
    async def test_the_marker_takes_the_same_redaction_as_the_rows(self, tmp_path):
        secret = "AKIAIOSFODNN7EXAMPLE"
        root = tmp_path / "proj"
        root.mkdir()
        _git(root, "init", "-q", "-b", "trunk")
        (root / "a.txt").write_text("a\n")
        _commit_all(root, "a")
        clone = root / secret
        clone.mkdir()
        _git(clone, "init", "-q", "-b", "trunk")
        (clone / "f.txt").write_text("x\n")
        _, data = await _get(root)
        assert secret not in json.dumps(data)
        assert len(data["nestedRepoDirectories"]) == 1
        assert data["nestedRepoDirectories"][0] in data["directories"]

    @pytest.mark.asyncio
    async def test_an_unreadable_index_leaves_a_gitlink_a_file_row(self, workspace, monkeypatch):
        """The gitlink read failing degrades to the old listing, never to an error."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded

        def fail_stage(argv, **kwargs):
            if "--stage" in argv:
                return 1, "", "boom"
            return real(argv, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", fail_stage)
        status, data = await _get(workspace)
        assert status == 200
        assert "libs/sub" in data["paths"]
        # The untracked clone is still told apart by its trailing slash.
        assert data["nestedRepoDirectories"] == ["scratch/clone"]


def _stage_reads(monkeypatch) -> list[list[str]]:
    """Record every ``git ls-files --stage`` the tree listing starts."""
    from kiro_crew.dashboard.handlers import files as files_mod

    real = files_mod._run_git_bounded
    seen: list[list[str]] = []

    def spy(cmd, **kwargs):
        if "--stage" in cmd:
            seen.append(list(cmd))
        return real(cmd, **kwargs)

    monkeypatch.setattr(files_mod, "_run_git_bounded", spy)
    return seen


@pytest.mark.usefixtures("mock_sel")
class TestTheGitlinkReadIsGated:
    """The gitlink read costs a sandboxed git run, so only a repository that
    has a ``.gitmodules`` pays for it."""

    @pytest.mark.asyncio
    async def test_a_repository_without_submodules_makes_no_gitlink_read(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "plainrepo"
        root.mkdir()
        _git(root, "init", "-q", "-b", "trunk")
        (root / "a.txt").write_text("a\n")
        _commit_all(root, "a")
        seen = _stage_reads(monkeypatch)
        status, data = await _get(root)
        assert status == 200
        assert data["paths"] == ["a.txt"]
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_repository_with_a_gitmodules_reads_the_gitlinks_once(
        self, workspace, monkeypatch
    ):
        seen = _stage_reads(monkeypatch)
        await _get(workspace)
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_an_untracked_clone_needs_no_gitlink_read(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        _git(root, "init", "-q", "-b", "trunk")
        (root / "a.txt").write_text("a\n")
        _commit_all(root, "a")
        clone = root / "clone"
        clone.mkdir()
        _git(clone, "init", "-q", "-b", "trunk")
        (clone / "f.txt").write_text("x\n")
        seen = _stage_reads(monkeypatch)
        _, data = await _get(root)
        assert seen == []
        assert data["nestedRepoDirectories"] == ["clone"]

    @pytest.mark.asyncio
    async def test_a_project_below_the_repository_top_still_finds_its_submodule(self, workspace):
        """``.gitmodules`` sits at the repository's top, above the project."""
        status, data = await _get(workspace / "libs")
        assert status == 200
        assert data["nestedRepoDirectories"] == ["sub"]
        assert "sub" not in data["paths"]

    @pytest.mark.asyncio
    async def test_a_gitlink_with_no_gitmodules_stays_a_file_row(self, workspace):
        """The old behaviour, kept for the one case the gate does not cover."""
        (workspace / ".gitmodules").unlink()
        _, data = await _get(workspace)
        assert "libs/sub" in data["paths"]
        assert data["nestedRepoDirectories"] == ["scratch/clone"]


@pytest.mark.usefixtures("mock_sel")
class TestNestedReposFolderListing:
    @pytest.mark.asyncio
    async def test_a_submodule_lists_its_own_files(self, workspace):
        status, data = await _get(workspace, dir="libs/sub")
        assert status == 200
        assert data["dir"] == "libs/sub"
        assert sorted(data["paths"]) == [
            "libs/sub/.gitignore",
            "libs/sub/README.md",
            "libs/sub/new.txt",
            "libs/sub/out/app.bin",
            "libs/sub/src/greet.py",
        ]
        # Listed by the walk, not by git: the walk's own skip list hides
        # `build/`, but the submodule's `.gitignore` is not applied, so `out/`
        # shows. The submodule's `.git` pointer file is dropped, as git's own
        # listing never names it. A listed folder is not named as a nested
        # repository.
        assert data["repo"] is False
        assert data["nestedRepoDirectories"] == []

    @pytest.mark.asyncio
    async def test_a_folder_inside_a_submodule_lists_its_files(self, workspace):
        status, data = await _get(workspace, dir="libs/sub/src")
        assert status == 200
        assert data["dir"] == "libs/sub/src"
        assert data["paths"] == ["libs/sub/src/greet.py"]

    @pytest.mark.asyncio
    async def test_an_untracked_clone_lists_its_files(self, workspace):
        status, data = await _get(workspace, dir="scratch/clone")
        assert status == 200
        assert sorted(data["paths"]) == [
            "scratch/clone/lib/util.py",
            "scratch/clone/notes.txt",
        ]
        assert data["directories"] == ["scratch/clone/lib"]

    @pytest.mark.asyncio
    async def test_a_repository_inside_a_submodule_is_listed_with_it(self, workspace):
        deeper = workspace / "libs" / "sub" / "vendor" / "deep"
        deeper.mkdir(parents=True)
        _git(deeper, "init", "-q", "-b", "trunk")
        (deeper / "d.txt").write_text("d\n")
        _, data = await _get(workspace, dir="libs/sub")
        # The walk descends every folder itself, so a repository inside a listed
        # one is an ordinary folder with its files, never a marker.
        assert data["nestedRepoDirectories"] == []
        assert "libs/sub/vendor/deep/d.txt" in data["paths"]
        # Nor does the inner repository's own `.git` show.
        assert not [p for p in data["paths"] if p.endswith("/.git")]

    @pytest.mark.asyncio
    async def test_a_submodule_that_was_never_initialised_lists_as_empty(self, workspace, tmp_path):
        sub = workspace / "libs" / "sub"
        shutil.rmtree(sub)
        sub.mkdir()
        status, data = await _get(workspace, dir="libs/sub")
        assert status == 200
        assert data["paths"] == []

    @pytest.mark.asyncio
    async def test_git_never_runs_inside_a_nested_repository(self, workspace, monkeypatch):
        """A nested repository is listed by the identity-checked walk: git is
        never started with it (or anything inside it) as its working directory,
        so a folder swapped for a link cannot redirect the read."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded
        cwds: list[str] = []

        def spy(cmd, *, cwd, **kwargs):
            cwds.append(str(cwd))
            return real(cmd, cwd=cwd, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", spy)
        # The whole listing runs git (the spy works); the nested folders do not.
        await _get(workspace)
        assert cwds
        cwds.clear()
        for folder in ("libs/sub", "libs/sub/src", "scratch/clone"):
            status, _ = await _get(workspace, dir=folder)
            assert status == 200
        nested = [str(workspace / "libs" / "sub"), str(workspace / "scratch" / "clone")]
        assert not [c for c in cwds if any(c.startswith(n) for n in nested)]
