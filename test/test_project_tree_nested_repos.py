"""Tests for nested repositories in ``GET /api/project/tree``.

``git ls-files`` names a submodule (a gitlink) as one entry printed like a file,
and an untracked nested clone as one entry with a trailing slash. Both are
folders on disk: they must be rows the tree can open, named in
``nestedRepoDirectories``, and ``?dir=`` must list what is inside them. The
project's git cannot, so the identity-checked walk does, and the nested
repository's own git says which of the walked entries it ignores.

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
            "libs/sub/src/greet.py",
        ]
        assert "libs/sub/out" not in data["directories"]
        # Listed by the walk: its own skip list hides `build/`, and the
        # submodule's git reports `out/` as ignored, so it is not even a row,
        # as when the submodule is the project. The `.git` pointer file is dropped, as git's own
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
    async def test_git_inside_a_nested_repository_only_names_what_it_ignores(
        self, workspace, monkeypatch
    ):
        """A nested repository is listed by the identity-checked walk. The only
        git run inside it, from its top, asks which paths it ignores
        (`ls-files --others --ignored --exclude-standard`, `core.fsmonitor=`);
        its answer can only drop rows the walk read."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded
        calls: list[tuple[str, list[str]]] = []

        def spy(cmd, *, cwd, **kwargs):
            calls.append((str(cwd), list(cmd)))
            return real(cmd, cwd=cwd, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", spy)
        # The whole listing runs git (the spy works).
        await _get(workspace)
        assert calls
        calls.clear()
        for folder in ("libs/sub", "libs/sub/src", "scratch/clone"):
            status, _ = await _get(workspace, dir=folder)
            assert status == 200
        tops = {str(workspace / "libs" / "sub"), str(workspace / "scratch" / "clone")}
        inside = [(cwd, cmd) for cwd, cmd in calls if any(cwd.startswith(t) for t in tops)]
        assert {cwd for cwd, _ in inside} == tops
        for _, cmd in inside:
            assert "ls-files" in cmd and "core.fsmonitor=" in cmd
            assert {"--others", "--ignored", "--exclude-standard", "--directory"} <= set(cmd)


def _sub(workspace: Path) -> Path:
    return workspace / "libs" / "sub"


@pytest.mark.usefixtures("mock_sel")
class TestNestedRepoGitignore:
    """A nested repository's listing hides what its own git ignores."""

    @pytest.mark.asyncio
    async def test_an_ancestor_gitignore_applies_to_a_folder_listed_inside(self, workspace):
        (_sub(workspace) / ".gitignore").write_text("build/\nout/\n*.log\n")
        (_sub(workspace) / "src" / "trace.log").write_text("t\n")
        _, data = await _get(workspace, dir="libs/sub/src")
        assert data["paths"] == ["libs/sub/src/greet.py"]

    @pytest.mark.asyncio
    async def test_a_negated_pattern_re_includes(self, workspace):
        (_sub(workspace) / ".gitignore").write_text("build/\nout/\n*.log\n!keep.log\n")
        (_sub(workspace) / "drop.log").write_text("d\n")
        (_sub(workspace) / "keep.log").write_text("k\n")
        _, data = await _get(workspace, dir="libs/sub")
        assert "libs/sub/keep.log" in data["paths"]
        assert "libs/sub/drop.log" not in data["paths"]

    @pytest.mark.asyncio
    async def test_a_tracked_file_the_gitignore_matches_still_shows(self, workspace):
        """Git never reports a tracked path as ignored, and re-includes nothing
        untracked inside an excluded folder."""
        sub = _sub(workspace)
        (sub / "src" / "kept.gen").write_text("g\n")
        (sub / "src" / "loose.gen").write_text("g\n")
        (sub / "out" / "kept.bin").write_text("k\n")
        _git(sub, "add", "src/kept.gen")
        _git(sub, "add", "-f", "out/kept.bin")
        _git(sub, "commit", "-qm", "tracked")
        (sub / ".gitignore").write_text("build/\nout/\n*.gen\n!out/keep.txt\n")
        (sub / "out" / "keep.txt").write_text("u\n")
        for folder in ("libs/sub", "libs/sub/out"):
            _, data = await _get(workspace, dir=folder)
            shown = {p for p in data["paths"] if p.startswith("libs/sub/out/")}
            assert shown == {"libs/sub/out/kept.bin"}, folder
        _, data = await _get(workspace, dir="libs/sub")
        assert "libs/sub/src/kept.gen" in data["paths"]
        assert "libs/sub/src/loose.gen" not in data["paths"]

    @pytest.mark.asyncio
    async def test_a_repository_inside_is_not_filtered_by_the_outer_rules(self, workspace):
        (_sub(workspace) / ".gitignore").write_text("build/\nout/\nd.txt\n")
        deeper = _sub(workspace) / "vendor" / "deep"
        deeper.mkdir(parents=True)
        _git(deeper, "init", "-q", "-b", "trunk")
        (deeper / "d.txt").write_text("d\n")
        (_sub(workspace) / "d.txt").write_text("d\n")
        _, data = await _get(workspace, dir="libs/sub")
        assert "libs/sub/d.txt" not in data["paths"]
        assert "libs/sub/vendor/deep/d.txt" in data["paths"]

    @pytest.mark.asyncio
    async def test_the_projects_own_gitignore_does_not_reach_inside(self, workspace):
        (workspace / ".gitignore").write_text("notes.txt\n")
        _, data = await _get(workspace, dir="scratch/clone")
        assert "scratch/clone/notes.txt" in data["paths"]

    @pytest.mark.asyncio
    async def test_a_failed_git_run_hides_nothing(self, workspace, monkeypatch):
        """When git cannot say, nothing is hidden, as the project listing falls
        back to the plain walk when its git fails."""
        from kiro_crew.dashboard.handlers import files as files_mod

        sub = str(_sub(workspace))
        real = files_mod._run_git_bounded

        def failing(cmd, *, cwd, **kwargs):
            if str(cwd).startswith(sub):
                return (1, "", True)
            return real(cmd, cwd=cwd, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", failing)
        status, data = await _get(workspace, dir="libs/sub")
        assert status == 200
        assert "libs/sub/out/app.bin" in data["paths"]

    @pytest.mark.asyncio
    async def test_a_name_only_git_reports_is_never_listed(self, workspace, monkeypatch):
        """Git's answer only drops entries the pinned walk read: a redirected
        run can hide rows but never add one."""
        from kiro_crew.dashboard.handlers import files as files_mod

        sub = str(_sub(workspace))
        real = files_mod._run_git_bounded

        def redirected(cmd, *, cwd, **kwargs):
            if str(cwd).startswith(sub):
                return (0, "phantom/\0elsewhere/secret.txt\0", False)
            return real(cmd, cwd=cwd, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", redirected)
        _, data = await _get(workspace, dir="libs/sub")
        assert "libs/sub/out/app.bin" in data["paths"]
        assert not [p for p in data["paths"] if "phantom" in p or "secret" in p]

    @pytest.mark.asyncio
    async def test_git_paths_and_entries_are_compared_by_normcase(self, workspace, monkeypatch):
        """On a case-insensitive checkout (Windows ``normcase``) git's spelling
        of an ignored path still matches the entry the walk read."""
        import os

        from kiro_crew.dashboard.handlers import files as files_mod

        sub = str(_sub(workspace))
        real = files_mod._run_git_bounded

        (_sub(workspace) / "Gen").mkdir()
        (_sub(workspace) / "Gen" / "x.bin").write_text("x\n")

        def other_case(cmd, *, cwd, **kwargs):
            if str(cwd).startswith(sub):
                return (0, "gen/\0", False)
            return real(cmd, cwd=cwd, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", other_case)
        monkeypatch.setattr(os.path, "normcase", lambda p: p.lower())
        _, data = await _get(workspace, dir="libs/sub")
        assert "libs/sub/Gen/x.bin" not in data["paths"]
        assert "libs/sub/Gen" not in data["directories"]
