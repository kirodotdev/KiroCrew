"""Tests for ``GET /api/project/tree``."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_project_tree
from kiro_crew.security.redaction import _PATH_SEGMENT_DISCRIMINATOR_SEP, _path_segment_label


def _deny_directory_read(monkeypatch, *denied: os.PathLike[str] | str) -> None:
    """Make ``scandir`` refuse the named directories, the way a mode-000 directory
    does for a non-root process.

    A SEAM, not a real permission bit: ``chmod 000`` denies nothing to root, to
    Windows (``chmod`` there touches only the read-only attribute, which does not
    govern listing) or on a filesystem that ignores POSIX modes, so a test built
    on it would have to skip on those hosts -- and the assertions it guards would
    never execute on those CI shards. ``os.walk`` reads ``scandir`` off the ``os``
    module on every call and reports its failure through ``onerror`` (verified
    for the walk under test: the failing directory reaches ``onerror`` with its
    path as ``filename`` and is not yielded), so refusing it here exercises the
    handler's own recording path on every platform, including the process's own
    kernel saying yes.
    """
    real_scandir = os.scandir
    refused = {os.path.realpath(os.fspath(d)) for d in denied}

    def scandir(path=".", *args, **kwargs):
        # ``rmtree`` and friends pass a file descriptor: not a path, never refused.
        if isinstance(path, (str, os.PathLike)) and os.path.realpath(os.fspath(path)) in refused:
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)


def _pretend_directory_symlink(monkeypatch, link: os.PathLike[str] | str) -> None:
    """Make ``os.path.islink`` answer yes for one real directory.

    Creating a symlink needs a privilege on Windows, so a real one would skip the
    test there. ``os.walk`` binds ``islink`` from ``os.path`` at call time and asks
    it before descending (``followlinks`` is off), and the handler's own
    hidden-only predicate asks the same function, so one patched answer gives both
    the symlink-to-a-directory shape: listed among the subdirectories, never
    walked into, no row of its own.
    """
    real_islink = os.path.islink
    target = os.path.realpath(os.fspath(link))

    def islink(path) -> bool:
        return os.path.realpath(os.fspath(path)) == target or real_islink(path)

    monkeypatch.setattr(os.path, "islink", islink)


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get("/api/project/tree", api_project_tree)
    return app


@pytest.fixture(autouse=True)
def passthrough_sandbox(monkeypatch):
    """Run git unwrapped: CI runners have no sandbox backend, and the handlers
    fail CLOSED without one. The chokepoint's own behavior is covered by
    test_sandbox*/test_spawn_audit; these tests exercise the listing logic.
    """
    from kiro_crew.dashboard.handlers import files as files_mod

    monkeypatch.setattr(
        files_mod,
        "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (list(argv), dict(os.environ), None),
    )


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture()
def plain_project(tmp_path, monkeypatch):
    """A project directory that is NOT inside any repository, wherever ``tmp_path`` is.

    The walk branch is what these tests exercise, and "not a repository" is not a
    property ``tmp_path`` has on every host: a harness that pins ``TMPDIR`` under
    the checkout gives it a real ``.git`` among its ancestors, git's upward
    discovery finds it, and the handler answers from ``ls-files`` -- ``repo: true``
    and git's ordering -- instead of walking. ``GIT_CEILING_DIRECTORIES`` is git's
    own seam for that walk (discovery stops below the named directory) and the
    handler builds its git environment from ``os.environ``, so the state is
    constructed here rather than assumed of the host. The project is a CHILD of
    the ceiling because git checks its starting directory before consulting it.
    The directory is not created: each test lays out its own tree under it.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return tmp_path / "plain"


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
    root = tmp_path_factory.mktemp("tree-seed") / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("line1\n")
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("x = 1\n")
    (root / ".gitignore").write_text("ignored.log\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial commit")
    return root


@pytest.fixture()
def repo(tmp_path, _repo_template):
    root = tmp_path / "proj"
    shutil.copytree(_repo_template, root)
    return root


class TestProjectTree:
    @pytest.mark.asyncio
    async def test_missing_path_is_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/project/tree")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_project_is_403(self, tmp_path, mock_sel):
        known = tmp_path / "known"
        known.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(known)))) as client:
            resp = await client.get(f"/api/project/tree?path={other}")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_vanished_directory_response_is_redacted(self, tmp_path, mock_sel, monkeypatch):
        """A known project dir deleted between the allow-list match and the stat.

        The early return still echoes the path, so it goes through the same egress
        redaction as the listing below it -- a project directory can carry a
        credential-shaped segment, and this arm is reachable, not defensive.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        known = tmp_path / "AKIAIOSFODNN7EXAMPLE"
        known.mkdir()
        monkeypatch.setattr(files_mod.os.path, "isdir", lambda p: False)
        async with TestClient(TestServer(_make_app(str(known)))) as client:
            resp = await client.get(f"/api/project/tree?path={known}")
            data = await resp.json()
        assert data["paths"] == []
        assert "AKIAIOSFODNN7EXAMPLE" not in data["root"]

    @pytest.mark.asyncio
    async def test_git_repo_lists_tracked_and_untracked(self, repo, mock_sel):
        (repo / "untracked.md").write_text("hi\n")
        (repo / "ignored.log").write_text("nope\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert "a.txt" in data["paths"]
        assert "src/mod.py" in data["paths"]
        assert "untracked.md" in data["paths"]
        assert "ignored.log" not in data["paths"]

    @pytest.mark.asyncio
    async def test_listing_disables_the_repo_writable_fsmonitor_hook(
        self, repo, mock_sel, monkeypatch
    ):
        # `core.fsmonitor` names a command git SPAWNS and lives in the
        # repository's own config, which an agent can write — so a tree listing
        # must not let it run. Pinned on the argv because the flag is invisible
        # in the response: a listing with the hook enabled looks identical.
        seen: list[list[str]] = []
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded

        def spy(argv, **kwargs):
            seen.append(list(argv))
            return real(argv, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", spy)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            assert resp.status == 200
        ls_argv = next(a for a in seen if "ls-files" in a)
        assert "core.fsmonitor=" in ls_argv
        assert ls_argv.index("-c") < ls_argv.index("ls-files")

    @pytest.mark.asyncio
    async def test_non_repo_walk_skips_heavy_and_hidden_dirs(self, plain_project, mock_sel):
        plain = plain_project
        (plain / "node_modules" / "dep").mkdir(parents=True)
        (plain / "node_modules" / "dep" / "index.js").write_text("x")
        (plain / ".hidden").mkdir()
        (plain / ".hidden" / "secret.txt").write_text("x")
        (plain / "docs").mkdir()
        (plain / "docs" / "readme.md").write_text("x")
        (plain / "top.txt").write_text("x")
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert sorted(data["paths"]) == ["docs/readme.md", "top.txt"]

    @pytest.mark.asyncio
    async def test_redaction_collision_paths_stay_distinct(self, repo, mock_sel):
        """Two genuinely-different paths that redact() collapses to one string
        must BOTH appear in the listing, as two distinct redacted entries.

        Uses a real ls-files collision: two files whose only differing segment is
        a credential-shaped token (distinct AKIA... ids, each 4-letter prefix +
        16 uppercase alphanumerics) both flatten to
        ``[REDACTED: credential]_model.txt`` under the whole-string redact().
        Each path is redacted with ``redact_path_segments`` so each member of
        the collision carries an opaque label keyed per gateway process --
        distinct between the two and stable across responses -- and neither
        vanishes; the de-dup behind it still guards a true collision, and the
        raw tokens never leak.
        """
        # Two DISTINCT keys are the point: the test proves two different
        # credential-shaped names collapse to ONE placeholder. key_a is the
        # documented example id Semgrep allowlists; key_b must stay a split
        # literal because detected-aws-access-key-id-value matches an
        # AKIA-shaped literal and cannot tell a fixture from a real leak. Do
        # not re-join it -- the runtime value is identical and CI, not the
        # test, is what breaks.
        key_a = "AKIAIOSFODNN7EXAMPLE"
        key_b = "AKIA" + "JKLMNOPQRSTUVWXY"
        (repo / f"{key_a}_model.txt").write_text("one\n")
        (repo / f"{key_b}_model.txt").write_text("two\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()
        paths = data["paths"]
        # Both files survive, each redacted and distinct from the other, each
        # carrying exactly the keyed label of its own original segment...
        sep = _PATH_SEGMENT_DISCRIMINATOR_SEP
        redacted = [p for p in paths if p.startswith(f"[REDACTED: credential]_model.txt{sep}")]
        assert sorted(redacted) == sorted(
            f"[REDACTED: credential]_model.txt{sep}{_path_segment_label(f'{k}_model.txt')}"
            for k in (key_a, key_b)
        ), paths
        assert len(paths) == len(set(paths))
        # ...and the raw credential-shaped tokens never leak.
        assert key_a not in "\n".join(paths)
        assert key_b not in "\n".join(paths)
        # Non-colliding entries survive unchanged.
        assert "a.txt" in paths
        assert "src/mod.py" in paths

    @pytest.mark.asyncio
    async def test_a_redacted_path_labels_identically_across_responses(self, repo, mock_sel):
        """The dashboard joins the tree response with the git-status response
        by path, so a redacted path must carry the same label in every response
        this process serves -- here, two tree responses, one of which lists a
        colliding neighbour and one of which does not."""
        key_a = "AKIAIOSFODNN7EXAMPLE"
        key_b = "AKIA" + "JKLMNOPQRSTUVWXY"
        (repo / f"{key_a}_model.txt").write_text("one\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            alone = await resp.json()
            (repo / f"{key_b}_model.txt").write_text("two\n")
            resp = await client.get(f"/api/project/tree?path={repo}")
            with_neighbour = await resp.json()
        sep = _PATH_SEGMENT_DISCRIMINATOR_SEP
        label_a = (
            f"[REDACTED: credential]_model.txt{sep}{_path_segment_label(f'{key_a}_model.txt')}"
        )
        assert label_a in alone["paths"]
        assert label_a in with_neighbour["paths"]
        assert (
            len([p for p in with_neighbour["paths"] if p.startswith("[REDACTED: credential]")]) == 2
        )

    @pytest.mark.asyncio
    async def test_a_true_redaction_collision_is_still_deduplicated(
        self, repo, mock_sel, monkeypatch
    ):
        """When the path helper (``redact_path_segments``) hands back the same
        string for two paths, the de-dup keeps first occurrence so
        @pierre/trees never sees adjacent identical entries."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(
            files_mod,
            "redact_path_segments",
            lambda p, r=None: (
                "[REDACTED: credential]_model.txt" if p.endswith("_model.txt") else p
            ),
        )
        (repo / "one_model.txt").write_text("one\n")
        (repo / "two_model.txt").write_text("two\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()
        paths = data["paths"]
        assert paths.count("[REDACTED: credential]_model.txt") == 1
        assert len(paths) == len(set(paths))
        assert "a.txt" in paths

    @pytest.mark.asyncio
    async def test_walk_caps_entries_and_flags_truncation(
        self, plain_project, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 2)
        plain = plain_project
        plain.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (plain / name).write_text("x")
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()
        assert data["truncated"] is True
        assert len(data["paths"]) == 2
        assert data["directories"] == []
        assert data["truncatedDirectories"] == [""]

    @pytest.mark.asyncio
    async def test_walk_keeps_the_full_directory_skeleton_and_samples_files_fairly(
        self, plain_project, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 4)
        plain = plain_project
        for directory in ("alpha", "beta", "late/nested"):
            target = plain / directory
            target.mkdir(parents=True)
            for index in range(3):
                (target / f"{index}.txt").write_text("x")
        (plain / "empty").mkdir()

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["truncated"] is True
        assert len(data["paths"]) == 4
        assert {path.rsplit("/", 1)[0] for path in data["paths"]} == {
            "alpha",
            "beta",
            "late/nested",
        }
        assert data["directories"] == ["alpha", "beta", "empty", "late", "late/nested"]
        assert data["truncatedDirectories"] == ["alpha", "beta", "late/nested"]

    @pytest.mark.asyncio
    async def test_walk_under_cap_keeps_all_files_and_directory_rows(
        self, plain_project, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 4)
        plain = plain_project
        (plain / "docs").mkdir(parents=True)
        (plain / "docs" / "readme.md").write_text("docs")
        (plain / "empty").mkdir()
        (plain / "top.txt").write_text("top")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["paths"] == ["top.txt", "docs/readme.md"]
        assert data["directories"] == ["docs", "empty"]
        assert data["truncated"] is False
        assert data["truncatedDirectories"] == []

    @pytest.mark.asyncio
    async def test_walk_reports_directories_left_childless_by_its_own_filter(
        self, plain_project, mock_sel
    ):
        """A folder holding ONLY entries the walk drops (dot-directories, tooling
        caches) comes back as a directory row with nothing beneath it, exactly
        like a folder that is empty on disk. The tree draws a state row under a
        childless folder, and that row may only call the folder empty when it
        is: ``hiddenOnlyDirectories`` names the ones that are not.
        """
        plain = plain_project
        (plain / "_bg" / ".kiro").mkdir(parents=True)
        (plain / "_bg" / ".kiro" / "agent.json").write_text("{}")
        (plain / "caches" / "node_modules").mkdir(parents=True)
        (plain / "empty").mkdir()
        # A hidden entry beside a listed file or a kept subfolder is not the
        # reported case: that folder has rows beneath it.
        (plain / "mixed" / ".hidden").mkdir(parents=True)
        (plain / "mixed" / "kept.txt").write_text("x")
        (plain / "nested" / ".hidden").mkdir(parents=True)
        (plain / "nested" / "sub").mkdir()

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["hiddenOnlyDirectories"] == ["_bg", "caches"]
        # Every one of them is still a directory row -- the folder is shown,
        # only its emptiness is qualified.
        assert set(data["hiddenOnlyDirectories"]) <= set(data["directories"])
        assert "empty" in data["directories"]
        assert data["paths"] == ["mixed/kept.txt"]

    @pytest.mark.asyncio
    async def test_walk_reports_a_folder_holding_only_a_directory_symlink_as_hidden_only(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A symlink to a directory is listed among the walk's subdirectories but
        never descended (``followlinks`` is off), so it becomes neither a row nor
        a parent: one more entry the listing hides, not an empty folder. The link
        is a seam (``_pretend_directory_symlink``) over a real directory holding a
        file, so the assertion runs where symlinks need a privilege, and the file
        beneath proves the walk did not go in.
        """
        plain = plain_project
        (plain / "releases").mkdir(parents=True)
        (plain / "releases" / "kept.txt").write_text("x")
        (plain / "linked" / "current").mkdir(parents=True)
        (plain / "linked" / "current" / "behind-the-link.txt").write_text("x")
        _pretend_directory_symlink(monkeypatch, plain / "linked" / "current")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["hiddenOnlyDirectories"] == ["linked"]
        assert "linked" in data["directories"]
        assert "linked/current" not in data["directories"]
        assert data["paths"] == ["releases/kept.txt"]

    @pytest.mark.asyncio
    async def test_walk_lists_a_kept_directory_it_could_not_read(
        self, plain_project, mock_sel, monkeypatch
    ):
        """``os.walk`` skips a subdirectory whose ``scandir`` fails (permission
        denied) WITHOUT yielding it, so a kept, non-symlink child that cannot be
        read would leave no trace: its parent has no row beneath it, is not
        hidden-only (the child is no symlink), and the tree would call the parent
        an empty folder -- a lie, ``ls`` shows the child. The unreadable directory
        is instead a row of its own, named in ``unreadableDirectories`` so the
        dashboard can report it above the tree, and the parent is not childless
        at all. Its files are never listed: nothing read them. The refusal is a
        seam (``_deny_directory_read``), so this runs as root and on Windows too.
        """
        plain = plain_project
        locked = plain / "vault" / "locked"
        locked.mkdir(parents=True)
        (locked / "inside.txt").write_text("x")
        (plain / "open").mkdir()
        (plain / "open" / "kept.txt").write_text("x")
        _deny_directory_read(monkeypatch, locked)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        # The folder is shown, in walk order; without it ``vault`` is a directory
        # row with nothing beneath it and no qualifier -- the "Empty folder" lie.
        assert data["directories"] == ["open", "vault", "vault/locked"]
        assert data["unreadableDirectories"] == ["vault/locked"]
        assert data["hiddenOnlyDirectories"] == []
        assert data["paths"] == ["open/kept.txt"]
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_walk_names_an_unreadable_root_instead_of_an_empty_workspace(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A ``scandir`` failure on the project root itself reaches ``onerror``
        with the root as its path and the walk then yields nothing, so the payload
        would be ``paths == [] and directories == []`` -- exactly what a workspace
        with no files in it sends, and the dashboard would paint "No files in this
        workspace yet" over a folder nothing ever read: the same "empty" claim the
        listing refuses to make one level down. The root is no directory row of
        its own (rows are relative to it), so it is named as ``.`` in
        ``unreadableDirectories`` and the dashboard shows its not-readable state
        in place of the empty-workspace notice. The root passes the handler's
        ``isdir`` check and the git probe finds no repository here, so the walk is
        what answers -- as it is for a real mode-000 root, where the probe fails
        closed on the unreadable ``cwd``.
        """
        plain = plain_project
        plain.mkdir()
        (plain / "inside.txt").write_text("x")
        (plain / "nested").mkdir()
        _deny_directory_read(monkeypatch, plain)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert resp.status == 200
        # ``.`` is not a path segment the redactor touches, so it survives egress.
        assert data["unreadableDirectories"] == ["."]
        assert data["directories"] == []
        assert data["paths"] == []
        assert data["hiddenOnlyDirectories"] == []
        assert data["truncated"] is False
        assert data["repo"] is False

    @pytest.mark.asyncio
    async def test_walk_names_a_root_holding_only_skipped_or_hidden_folders(
        self, plain_project, mock_sel
    ):
        """The hidden-only rule must judge the project root by the same test as
        every folder beneath it. A project directory whose top level holds only
        entries the walk drops (here ``.kiro/`` and a ``node_modules/`` cache)
        yields no file and no kept subdirectory, so the payload is
        ``paths == [] and directories == []`` -- the empty-workspace shape --
        and the dashboard would paint "No files in this workspace yet" over a
        folder that is not empty: the claim this listing refuses to make one
        level down, made about the whole tree. The root is no directory row of
        its own, so it is named as ``.`` in ``hiddenOnlyDirectories``, exactly
        as an unreadable root is named in ``unreadableDirectories``.
        """
        plain = plain_project
        (plain / ".kiro").mkdir(parents=True)
        (plain / ".kiro" / "agent.json").write_text("{}")
        (plain / "node_modules" / "dep").mkdir(parents=True)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert resp.status == 200
        assert data["hiddenOnlyDirectories"] == ["."]
        assert data["directories"] == []
        assert data["paths"] == []
        assert data["unreadableDirectories"] == []
        assert data["truncated"] is False
        assert data["repo"] is False

    @pytest.mark.asyncio
    async def test_a_root_with_nothing_in_it_is_still_an_empty_workspace(
        self, plain_project, mock_sel
    ):
        """The root rule must not over-reach: a project directory with no entry
        at all is genuinely empty, and the empty-workspace notice is the truth.
        """
        plain = plain_project
        plain.mkdir()

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["hiddenOnlyDirectories"] == []
        assert data["directories"] == []
        assert data["paths"] == []

    @pytest.mark.asyncio
    async def test_a_hidden_only_marker_is_redacted_like_its_directory_row(
        self, plain_project, mock_sel
    ):
        """Mutation pin for ``"hiddenOnlyDirectories"`` in the egress redaction
        tuple: a directory whose NAME is credential-shaped is listed in
        ``directories`` redacted, and the dashboard finds it in
        ``hiddenOnlyDirectories`` by string equality to pick the row beneath it.
        Drop the entry from the tuple and the marker leaks the raw name -- and no
        longer matches its own redacted row, so the folder would be called empty.
        """
        plain = plain_project
        (plain / "AKIAIOSFODNN7EXAMPLE" / ".kiro").mkdir(parents=True)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(data)
        assert len(data["directories"]) == 1
        assert data["directories"][0].startswith("[REDACTED: credential]")
        # The join the tree performs: the marker IS the redacted row.
        assert data["hiddenOnlyDirectories"] == data["directories"]

    @pytest.mark.asyncio
    async def test_an_unreadable_marker_is_redacted_like_its_directory_row(
        self, plain_project, mock_sel, monkeypatch
    ):
        """Mutation pin for ``"unreadableDirectories"`` in the egress redaction
        tuple, same shape as the hidden-only pin: the directory row and the
        marker naming it must be the same redacted string, and the raw
        credential-shaped name must appear nowhere in the body.
        """
        plain = plain_project
        locked = plain / "AKIAIOSFODNN7EXAMPLE"
        locked.mkdir(parents=True)
        _deny_directory_read(monkeypatch, locked)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(data)
        assert len(data["directories"]) == 1
        assert data["directories"][0].startswith("[REDACTED: credential]")
        assert data["unreadableDirectories"] == data["directories"]

    def test_truncation_copy_names_the_served_file_cap(self):
        """The state row under a truncated folder and the workspace-level notice
        state the file cap as a literal in every catalog (``10,000``; a payload
        field would be a new contract for one number). Pin each string to
        ``_PROJECT_TREE_MAX_ENTRIES`` so a change to the constant reds every
        locale still naming the old cap, instead of the dashboard stating a
        limit the server does not enforce. Digit grouping follows the locale
        (``10,000`` / ``10.000`` / ``10 000``), so the comparison drops the
        separators between digits and looks for the bare number.
        """
        from kiro_crew.dashboard.handlers.files import _PROJECT_TREE_MAX_ENTRIES

        locales = Path(__file__).resolve().parents[1] / "website" / "src" / "i18n" / "locales"
        # ``en.json`` is the extracted catalog and carries none of the manual keys.
        catalogs = sorted(p for p in locales.glob("*.json") if p.name != "en.json")
        assert len(catalogs) >= 13, [p.name for p in catalogs]
        cap = str(_PROJECT_TREE_MAX_ENTRIES)
        ungroup = re.compile(r"(?<=\d)[,.\s\u202f\u00a0](?=\d)")
        # The cap as a whole number, not a substring: `1000` is inside `10000`,
        # so lowering the constant must still red every catalog naming the old cap.
        names_cap = re.compile(rf"(?<!\d){re.escape(cap)}(?!\d)")
        for path in catalogs:
            catalog = json.loads(path.read_text(encoding="utf-8"))
            copy = {
                "row_truncated": catalog["components"]["workspaceTree"]["row_truncated"],
                "workspace_truncated": catalog["pages"]["chat"]["activityViewer"][
                    "workspace_truncated"
                ],
            }
            for key, text in copy.items():
                flat = ungroup.sub("", text)
                message = f"{path.name} {key}: {text!r} does not name the cap {cap}"
                assert names_cap.search(flat), message

    @pytest.mark.asyncio
    async def test_git_listing_reports_no_hidden_only_directories(self, repo, mock_sel):
        """Inside a repository a directory row exists only as the parent of a
        listed file, so an ignored-only folder is absent rather than childless
        -- the list is empty by construction, and present so the payload shape
        does not depend on which branch answered. ``unreadableDirectories`` is
        empty for the same reason: ``--others`` cannot scan a directory git
        cannot read, so it contributes no untracked file and, with no indexed
        file beneath it, is absent rather than childless; an indexed path
        beneath it still comes from the index and makes it a populated row."""
        (repo / "logs").mkdir()
        (repo / "logs" / "ignored.log").write_text("nope\n")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert data["hiddenOnlyDirectories"] == []
        assert data["unreadableDirectories"] == []
        assert "logs" not in data["directories"]

    @pytest.mark.asyncio
    async def test_cap_does_not_drop_the_whole_tracked_block(self, tmp_path, mock_sel, monkeypatch):
        """A fat UNTRACKED subtree must not evict every tracked file.

        ``git ls-files --cached --others`` emits all untracked entries as one
        complete block and only then the tracked ones, so capping with a plain
        prefix cut never reaches the tracked block once untracked alone fill it
        -- the whole source tree loses its rows. The listing is sorted before
        the cut so the budget is spent by path, not by which block git happened
        to emit first.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 20)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", ".")
        tracked = ("README.md", "docs/real.py", "src/real.py")
        for rel in tracked:
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        # Untracked (NOT ignored) and larger than the cap on its own. Named to
        # sort after every tracked path, so a sorted cut reaches them all.
        fat = repo / "zz_vendor" / "deep"
        fat.mkdir(parents=True)
        for i in range(40):
            (fat / f"u{i:04d}.txt").write_text("x")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()

        assert data["repo"] is True
        assert data["truncated"] is True
        assert len(data["paths"]) == 20
        # The point of the fix: every tracked file keeps a row.
        for rel in tracked:
            assert rel in data["paths"], f"{rel} was evicted by the untracked block"
        # ...and the fix does not merely invert the loss: the untracked subtree
        # still spends the remaining budget, so it keeps a row too.
        assert any(p.startswith("zz_vendor/") for p in data["paths"])
