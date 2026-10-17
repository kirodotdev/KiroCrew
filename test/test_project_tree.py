"""Tests for ``GET /api/project/tree``."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import pinned_fs
from kiro_crew.dashboard.handlers import api_project_tree
from kiro_crew.security.redaction import _PATH_SEGMENT_DISCRIMINATOR_SEP, _path_segment_label
from kiro_crew.testing.links import make_dir_link


@contextlib.contextmanager
def _fence_targets(*paths: Path):
    """Double BOTH halves of the fence the walk consults, from one target set.

    The walk asks two questions about each directory -- whether the directory is
    itself inside a store (``is_sensitive_resolved_path``) and whether a store
    lies beneath it (``path_contains_sensitive``) -- and the second is
    what lets it settle a whole directory of entries without asking about each
    one. Doubling one alone leaves the pair disagreeing: a directory that holds
    nothing, whose entries are fenced. So both answer from *paths* here, matched
    the way the real gates match them -- a target and everything under it is
    inside a store, and a directory with a target under it holds one.
    """
    targets = [os.path.normcase(os.path.realpath(p)) for p in paths]

    def inside(path: str) -> bool:
        candidate = os.path.normcase(path)
        return any(
            candidate == target or candidate.startswith(target + os.sep) for target in targets
        )

    def holds(directory: str, *args, **kwargs) -> bool:
        # Accepts the containment gate's full signature: the walk claims
        # ``pre_resolved=True``, and a double that refused the keyword would
        # pass while the real call site raised.
        prefix = os.path.normcase(directory).rstrip(os.sep) + os.sep
        return any(target.startswith(prefix) for target in targets)

    handlers = "kiro_crew.dashboard.handlers.files"
    with (
        patch(f"{handlers}.is_sensitive_resolved_path", side_effect=inside),
        patch(f"{handlers}.path_contains_sensitive", side_effect=holds),
    ):
        yield


# The read branches this platform has: descriptor listing exists only where
# ``scandir`` takes a descriptor (POSIX); the by-name branch is reachable
# everywhere, since a test can force the walk onto it.
_LISTING_BRANCHES = [
    *(
        [pytest.param(True, id="descriptor")]
        if os.scandir in os.supports_fd and pinned_fs.supports_pinned_walk()
        else []
    ),
    pytest.param(False, id="by-name"),
]


def _refuse_link_open(monkeypatch, path: os.PathLike[str] | str, armed=lambda: True) -> None:
    """Make the walk's ``O_NOFOLLOW`` open of *path* fail as it does on a link.

    A real symlink needs a privilege on Windows; the walk only sees the
    ``ELOOP`` the open raises, so this seam gives that shape on every host.
    """
    target = os.path.normcase(os.path.abspath(os.fspath(path)))
    real_open = os.open

    def fake_open(name, *args, **kwargs):
        if (
            isinstance(name, (str, os.PathLike))
            and armed()
            and os.path.normcase(os.path.abspath(os.fspath(name))) == target
        ):
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", os.fspath(name))
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


def _identity(path_or_fd) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of a directory named by path OR open descriptor.

    The walk lists a folder through a descriptor where the platform can (POSIX)
    and by path where it cannot (Windows), so a seam that recognised only a path
    would never see the POSIX reads. Identity names the folder either way.
    """
    try:
        st = os.stat(path_or_fd)
    except (OSError, TypeError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _deny_directory_read(monkeypatch, *denied: os.PathLike[str] | str) -> None:
    """Make ``scandir`` refuse the named directories, the way a mode-000 directory
    does for a non-root process.

    A SEAM, not a real permission bit: ``chmod 000`` denies nothing to root, to
    Windows (``chmod`` there touches only the read-only attribute, which does not
    govern listing) or on a filesystem that ignores POSIX modes, so a test built
    on it would have to skip on those hosts -- and the assertions it guards would
    never execute on those CI shards. The walk reads ``scandir`` off the ``os``
    module for every folder it opens and records a folder whose read raises as
    unreadable, so refusing it here exercises the handler's own recording path
    on every platform, including the process's own kernel saying yes.
    """
    real_scandir = os.scandir
    refused = {_identity(os.fspath(d)) for d in denied}

    def scandir(path=".", *args, **kwargs):
        if _identity(path) in refused:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)


def _pretend_directory_symlink(monkeypatch, link: os.PathLike[str] | str) -> None:
    """Make the walk's link test answer yes for one real directory.

    Creating a symlink needs a privilege on Windows, so a real one would skip the
    test there. The walk classifies each listed subfolder by its ``lstat``
    (``_project_tree_is_link``) before it queues it, so one patched answer for
    this folder's identity gives the symlink-to-a-directory shape: listed among
    the subdirectories, never walked into.
    """
    from kiro_crew.dashboard.handlers import files as files_mod

    real_is_link = files_mod._project_tree_is_link
    target = _identity(os.fspath(link))

    def is_link(st) -> bool:
        return (st.st_dev, st.st_ino) == target or real_is_link(st)

    monkeypatch.setattr(files_mod, "_project_tree_is_link", is_link)


def _count_project_reads(monkeypatch, project: os.PathLike[str] | str) -> dict[str, int]:
    """Count each ``scandir`` of a folder inside *project*, and nothing else.

    An identity filter, not a wrapper around every call: the event loop, the
    test client and anything else in the process may list a directory while
    the patch is live, and they must neither be counted nor get anything but
    the real iterator back. The project's folders are mapped to their resolved
    paths when the seam is installed, so the counts read by path.
    """
    real_scandir = os.scandir
    root = os.path.realpath(os.fspath(project))
    folders = {_identity(root): root}
    for dirpath, dirnames, _files in os.walk(root):
        for name in dirnames:
            path = os.path.realpath(os.path.join(dirpath, name))
            folders[_identity(path)] = path
    counts: dict[str, int] = {}

    def scandir(path=".", *args, **kwargs):
        folder = folders.get(_identity(path))
        if folder is not None:
            counts[folder] = counts.get(folder, 0) + 1
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    return counts


class _Listing:
    """A ``scandir`` iterator that runs *on_entry* per pulled entry and *on_close*
    once when the listing is closed -- the two moments a test needs to act at.

    Delegates the whole iterator protocol, context manager and ``close``
    included, so the walk under test cannot tell it from the real one.
    """

    def __init__(self, inner, on_entry=None, on_close=None) -> None:
        self._inner = inner
        self._on_entry = on_entry
        self._on_close = on_close

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self._inner)
        if self._on_entry is not None:
            self._on_entry(entry)
        return entry

    def close(self) -> None:
        self._inner.close()
        on_close, self._on_close = self._on_close, None
        if on_close is not None:
            on_close()


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
    async def test_not_a_directory_root_takes_the_path_aware_redactor(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The early arm carries the same absolute project path as the listing.

        Same defect and same fix as /api/project/git's repoRoot: a macOS
        per-user temp root scans as one high-entropy token and the Files tab
        renders `[REDACTED: credential]` where the path belongs. Reached with a
        real non-directory rather than by patching `os.path.isdir`, which is
        process-global and breaks unrelated lazy imports.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        known = tmp_path / "project.txt"
        known.write_text("not a directory")

        seen: list[str] = []
        real = files_mod._redact_project_path

        def spy(value: str) -> str:
            seen.append(value)
            return real(value)

        monkeypatch.setattr(files_mod, "_redact_project_path", spy)
        async with TestClient(TestServer(_make_app(str(known)))) as client:
            resp = await client.get(f"/api/project/tree?path={known}")
            data = await resp.json()
        assert data["paths"] == []
        assert seen == [str(known)]

    @pytest.mark.asyncio
    async def test_listing_root_takes_the_path_aware_redactor(self, repo, mock_sel, monkeypatch):
        """`root` is absolute and takes the path-aware redactor; `paths` are
        project-relative, carry no OS temp prefix, and stay on the canonical
        one. Pinned on the call because the two agree off-Darwin."""
        from kiro_crew.dashboard.handlers import files as files_mod

        seen: list[str] = []
        real = files_mod._redact_project_path

        def spy(value: str) -> str:
            seen.append(value)
            return real(value)

        monkeypatch.setattr(files_mod, "_redact_project_path", spy)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()
        assert data["repo"] is True
        assert seen == [str(repo)]
        assert "a.txt" in data["paths"]

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
    async def test_non_repo_walk_skips_heavy_dirs_and_lists_dot_dirs(self, plain_project, mock_sel):
        # Dot-directories (``.worktrees``) are walked; the skip set
        # (``.git``, ``.kiro``, ``node_modules``, ...) is not.
        plain = plain_project
        (plain / "node_modules" / "dep").mkdir(parents=True)
        (plain / "node_modules" / "dep" / "index.js").write_text("x")
        (plain / ".git").mkdir()
        (plain / ".git" / "HEAD").write_text("x")
        (plain / ".kiro").mkdir()
        (plain / ".kiro" / "agent.json").write_text("{}")
        (plain / ".worktrees" / "x").mkdir(parents=True)
        (plain / ".worktrees" / "x" / "a.txt").write_text("x")
        (plain / "docs").mkdir()
        (plain / "docs" / "readme.md").write_text("x")
        (plain / "top.txt").write_text("x")
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()
        assert data["repo"] is False
        assert sorted(data["paths"]) == [".worktrees/x/a.txt", "docs/readme.md", "top.txt"]
        assert ".worktrees/x" in data["directories"]
        assert ".git" not in data["directories"]
        assert ".kiro" not in data["directories"]

    @pytest.mark.asyncio
    async def test_non_repo_walk_drops_sensitive_entries_at_any_depth_under_a_dot_dir(
        self, plain_project, mock_sel
    ):
        # A fenced store can sit one level below a benign dot-directory
        # (``.config/gcloud``, ``.docker/config.json``): every entry under a
        # dot-directory is checked, not only the top-level name.
        plain = plain_project
        (plain / ".creds").mkdir(parents=True)
        (plain / ".creds" / "token").write_text("x")
        (plain / ".config" / "gcloud").mkdir(parents=True)
        (plain / ".config" / "gcloud" / "key").write_text("x")
        (plain / ".config" / "app.toml").write_text("x")
        (plain / ".docker").mkdir()
        (plain / ".docker" / "config.json").write_text("x")
        (plain / "top.txt").write_text("x")
        with _fence_targets(
            plain / ".creds", plain / ".config/gcloud", plain / ".docker/config.json"
        ):
            async with TestClient(TestServer(_make_app(str(plain)))) as client:
                resp = await client.get(f"/api/project/tree?path={plain}")
                data = await resp.json()
        assert sorted(data["paths"]) == [".config/app.toml", "top.txt"]
        assert data["directories"] == [".config", ".docker"]
        # ``.docker`` holds only a fenced file, so it is hidden-only, not empty.
        assert data["hiddenOnlyDirectories"] == [".docker"]

    @pytest.mark.asyncio
    async def test_non_repo_walk_fences_a_project_rooted_inside_a_dot_dir(
        self, plain_project, mock_sel
    ):
        # A project whose root is itself under a dot-directory (``~/.config``)
        # has no dot in its relative paths; the fence reads the real path.
        project = plain_project.parent / ".cfg"
        (project / "gcloud").mkdir(parents=True)
        (project / "gcloud" / "key").write_text("x")
        (project / "app.toml").write_text("x")
        with _fence_targets(project / "gcloud"):
            async with TestClient(TestServer(_make_app(str(project)))) as client:
                resp = await client.get(f"/api/project/tree?path={project}")
                data = await resp.json()
        assert data["repo"] is False
        assert data["paths"] == ["app.toml"]
        assert data["directories"] == []

    @pytest.mark.asyncio
    async def test_the_real_fence_hides_a_sensitive_dot_entry_under_a_dot_root(
        self, plain_project, mock_sel, monkeypatch
    ):
        # No double: the gates that ship decide, so a shortcut that read one of
        # them wrongly shows up here as a leaked credential rather than as a
        # passing test against a stand-in. ``KIROCREW_HOME`` re-anchors the crew
        # leaves (``.env``, ``.vault``) under the project root, which is itself
        # under a dot-name -- the shape the Files tab reported, where every entry
        # is a fence candidate because the ROOT's real path carries the dot.
        project = plain_project.parent / ".state"
        (project / "sub").mkdir(parents=True)
        (project / "sub" / "note.md").write_text("x")
        (project / ".env").write_text("TOKEN=x")
        (project / ".vault").mkdir()
        (project / ".vault" / "key").write_text("x")
        (project / "app.toml").write_text("x")
        monkeypatch.setenv("KIROCREW_HOME", str(project))
        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get(f"/api/project/tree?path={project}")
            data = await resp.json()
        assert sorted(data["paths"]) == ["app.toml", "sub/note.md"]
        assert data["directories"] == ["sub"]

    @pytest.mark.asyncio
    async def test_the_real_fence_follows_a_link_out_of_a_clear_directory(
        self, plain_project, mock_sel, monkeypatch
    ):
        # The directory holds no store, so its entries are settled from the
        # directory itself -- but only the ones whose real path IS the
        # directory's plus the name. A link's is somewhere else, so it is asked
        # about on its own, and this one lands in a store.
        #
        # ``make_dir_link``, not ``symlink_to``: a directory symlink needs a
        # privilege an unelevated Windows shell does not have, and a test that
        # skipped there would leave these assertions unrun on the one platform
        # whose junctions are why the resolved path is compared at all.
        home = plain_project.parent / "home"
        (home / ".vault").mkdir(parents=True)
        (home / ".vault" / "key").write_text("x")
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        project = plain_project.parent / ".proj"
        (project / "sub").mkdir(parents=True)
        (project / "sub" / "note.md").write_text("x")
        make_dir_link(project / "sub" / ".secrets", home / ".vault")
        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get(f"/api/project/tree?path={project}")
            data = await resp.json()
        assert data["paths"] == ["sub/note.md"]
        assert data["directories"] == ["sub"]
        # Fenced, so it is not a link row either -- the listing never names it.
        assert data["linkedDirectories"] == []

    def test_the_fence_resolves_its_anchors_per_directory_not_per_entry(
        self, plain_project, monkeypatch
    ):
        # What this pins is a COST, so it is measured as one: the gate resolves
        # ``$HOME``, the override roots and the keystone leaves on every call,
        # and a call per entry makes that resolution a cost per entry. Under a
        # dot-named root every entry is a fence candidate, so a 980-directory
        # project pays it ~11,700 times for one listing and the Files tab misses
        # its timeout. Counted rather than timed: a wall-clock bound would be
        # the host's number, not this walk's.
        from kiro_crew.dashboard.handlers import files as files_mod
        from kiro_crew.security import paths as paths_mod

        project = plain_project.parent / ".counted"
        (project / "sub").mkdir(parents=True)
        # The target cache expires on the wall clock (a 0.1s floor), and an
        # expiry inside the counted walk is a rebuild that resolves the anchors
        # once more. A slow runner walking 400 entries outlasts that floor, so
        # the count would say how long the walk took, not what shape it has.
        # The expiry is held open for the measurement; the per-directory
        # property is what remains to be counted.
        monkeypatch.setattr(paths_mod, "_home_targets_ttl", lambda *_a, **_k: 3600.0)

        def resolutions(entries: int) -> int:
            for stale in project.glob("sub/f*.txt"):
                stale.unlink()
            for i in range(entries):
                (project / "sub" / f"f{i}.txt").write_text("x")
            calls = 0
            real = paths_mod._resolve_root_anchors

            def counting(logical_home: str):
                nonlocal calls
                calls += 1
                return real(logical_home)

            def walk() -> None:
                files_mod._project_tree_walk(
                    str(project),
                    files_mod._PROJECT_TREE_MAX_ENTRIES,
                    files_mod._PROJECT_TREE_SCAN_LIMIT,
                )

            # Uncounted first, so the gate's target cache is warm either way: a
            # cold build resolves the anchors a second time under its own lock,
            # and counting that would make the number say which call came first
            # rather than how many entries the walk saw.
            walk()
            monkeypatch.setattr(paths_mod, "_resolve_root_anchors", counting)
            try:
                walk()
            finally:
                monkeypatch.setattr(paths_mod, "_resolve_root_anchors", real)
            return calls

        few = resolutions(4)
        many = resolutions(400)
        # Same two directories either way, so the same number of resolutions --
        # a hundred times the entries buys none. Equality, not a ratio: the
        # count is a property of the walk's shape, and the two directories are
        # the only thing in this project that has one.
        assert few == many
        # And it is the directories that set it, so it is small and bounded.
        assert many <= 2 * len(list(os.walk(project)))

    @pytest.mark.asyncio
    async def test_non_repo_walk_lists_a_junction_as_a_link_and_never_walks_it(
        self, plain_project, mock_sel, monkeypatch
    ):
        # A Windows junction is a name surrogate, so the walk's ``lstat``
        # classification (``_project_tree_is_link``) lists it as a link row
        # and never reads beneath it -- under a dot-directory too.
        plain = plain_project
        junction = plain / ".worktrees" / "j"
        junction.mkdir(parents=True)
        (junction / "outside.txt").write_text("x")
        (plain / "top.txt").write_text("x")
        _pretend_directory_symlink(monkeypatch, junction)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()
        assert data["paths"] == ["top.txt"]
        assert data["linkedDirectories"] == [".worktrees/j"]
        assert data["hiddenOnlyDirectories"] == []

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
    async def test_walk_spends_the_cap_on_folder_rows_first_and_samples_files_fairly(
        self, plain_project, mock_sel, monkeypatch
    ):
        """One cap covers files AND folder rows. Folder rows come first,
        shallowest first, but never past half the cap while there are files to
        show; the files share the rest round-robin by direct parent, so no one
        folder takes it all, and every shown folder that lost something is
        named -- ``late`` lost its ``nested`` row."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 8)
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
        assert data["directories"] == ["alpha", "beta", "empty", "late"]
        assert data["paths"] == ["alpha/0.txt", "alpha/1.txt", "beta/0.txt", "beta/1.txt"]
        assert data["truncatedDirectories"] == ["alpha", "beta", "late"]

    @pytest.mark.asyncio
    async def test_walk_caps_folder_rows_too_shallowest_first(
        self, plain_project, mock_sel, monkeypatch
    ):
        """With no file to show, folder rows take the whole cap, breadth-first:
        the top-level folders keep their rows ahead of anything nested, and
        every shown folder that lost a child row is named as truncated -- the
        root included, which lost ``d3`` and ``d4``."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 3)
        plain = plain_project
        for index in range(5):
            (plain / f"d{index}" / "sub").mkdir(parents=True)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["directories"] == ["d0", "d1", "d2"]
        assert data["paths"] == []
        assert data["truncated"] is True
        assert data["truncatedDirectories"] == ["", "d0", "d1", "d2"]

    @pytest.mark.asyncio
    async def test_walk_keeps_root_files_when_folders_alone_would_fill_the_cap(
        self, plain_project, mock_sel, monkeypatch
    ):
        """More folders than the cap must not hide every file: the root's
        ``README.md`` is the first file a workspace is opened for, and in tree
        mode the rail's name filter searches only the listed rows."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 4)
        plain = plain_project
        for index in range(6):
            (plain / f"pkg{index}").mkdir(parents=True)
        (plain / "README.md").write_text("x")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["paths"] == ["README.md"]
        assert data["directories"] == ["pkg0", "pkg1", "pkg2"]
        assert data["truncatedDirectories"] == [""]

    @pytest.mark.asyncio
    async def test_spare_rows_go_to_folders_without_evicting_files_that_fit(
        self, plain_project, mock_sel, monkeypatch
    ):
        """Files counted in folders past the row budget leave file rows unused,
        and those go back to folder rows -- but the folders gaining a row do not
        then share the file rows, which would evict the root's README.md."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 6)
        plain = plain_project
        for index in range(6):
            (plain / f"pkg{index}").mkdir(parents=True)
        (plain / "LICENSE").write_text("x")
        (plain / "README.md").write_text("x")
        for index in range(5):
            (plain / "pkg3" / f"m{index}.py").write_text("x")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["paths"] == ["LICENSE", "README.md"]
        assert data["directories"] == ["pkg0", "pkg1", "pkg2", "pkg3"]
        assert data["truncatedDirectories"] == ["", "pkg3"]

    @pytest.mark.asyncio
    async def test_walk_of_a_big_tree_returns_at_most_the_cap_in_rows(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A large non-git project is listed every 10 s while its tree is open,
        so the rows per listing are what the gateway pays for each poll: files
        and folder rows together stop at the cap, and the cut is flagged."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 50)
        plain = plain_project
        for top in range(20):
            nested = plain / f"top{top:02d}" / "sub"
            nested.mkdir(parents=True)
            for index in range(10):
                (plain / f"top{top:02d}" / f"{index}.txt").write_text("x")
                (nested / f"{index}.txt").write_text("x")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert len(data["paths"]) + len(data["directories"]) == 50
        assert data["truncated"] is True
        # Every top-level folder keeps its row, and files still get theirs.
        assert {f"top{top:02d}" for top in range(20)} <= set(data["directories"])
        assert len(data["paths"]) == 25
        assert set(data["truncatedDirectories"]) <= {"", *data["directories"]}

    @pytest.mark.parametrize("big", ["data", "zz_data"])
    @pytest.mark.asyncio
    async def test_one_large_folder_does_not_starve_its_siblings_of_the_scan(
        self, big, plain_project, mock_sel, monkeypatch
    ):
        """One folder holding more entries than the whole scan budget must not
        starve its siblings, wherever it sorts: before ``src/`` it would spend
        the budget before ``src/`` is read, after it it would spend what the
        next depth needs to read ``src/lib/``. Each folder of a depth reads at
        most an even split of what is left, with the depths below counted as
        one more claimant, so ``src/`` and ``src/lib/`` are read in full and
        only the large folder is cut."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCAN_LIMIT", 100)
        plain = plain_project
        (plain / big).mkdir(parents=True)
        for index in range(110):
            (plain / big / f"{index:04d}.csv").write_text("x")
        (plain / "src" / "lib").mkdir(parents=True)
        (plain / "src" / "main.py").write_text("x")
        (plain / "src" / "lib" / "util.py").write_text("x")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert "src/lib" in data["directories"]
        assert "src/main.py" in data["paths"]
        assert "src/lib/util.py" in data["paths"]
        assert big in data["truncatedDirectories"]
        assert not {"src", "src/lib"} & set(data["truncatedDirectories"])

    @pytest.mark.parametrize("big", ["d00", "d09"])
    @pytest.mark.asyncio
    async def test_a_tree_inside_both_limits_is_read_whole_beside_one_large_folder(
        self, big, plain_project, mock_sel, monkeypatch
    ):
        """A tree under the row cap and well under the scan limit is listed in
        full, wherever its one large folder sorts: the large folder may read
        past an even split, because the small siblings after it need only their
        floor. An even split alone cut it here -- 200 files against a 45-entry
        share -- and reported a whole tree as truncated."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCAN_LIMIT", 500)
        plain = plain_project
        expected: set[str] = set()
        for top in range(10):
            folder = plain / f"d{top:02d}"
            folder.mkdir(parents=True)
            for index in range(200 if folder.name == big else 5):
                (folder / f"{index:03d}.txt").write_text("x")
                expected.add(f"{folder.name}/{index:03d}.txt")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["truncated"] is False
        assert data["truncatedDirectories"] == []
        assert expected <= set(data["paths"])

    @pytest.mark.asyncio
    async def test_a_large_folder_sorted_first_still_leaves_each_sibling_its_floor(
        self, plain_project, mock_sel, monkeypatch
    ):
        """Reading past an even split never takes what the folders after it are
        owed: with several folders larger than the whole budget, each small
        sibling sorted after them is still read in full."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCAN_LIMIT", 400)
        plain = plain_project
        for top in range(3):
            (plain / f"a{top}").mkdir(parents=True)
            for index in range(500):
                (plain / f"a{top}" / f"{index:03d}.csv").write_text("x")
        for top in range(5):
            (plain / f"z{top}").mkdir()
            (plain / f"z{top}" / "small.txt").write_text("x")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert {f"z{top}/small.txt" for top in range(5)} <= set(data["paths"])
        assert set(data["truncatedDirectories"]) == {"a0", "a1", "a2"}

    @pytest.mark.asyncio
    async def test_a_folder_the_scan_budget_never_reaches_is_named_and_never_read(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A budget of one entry reads the root's only entry, ``later/``, and
        nothing else: the folder that read named is shown and flagged, never
        called empty, and the walk does not open it. The root is flagged too:
        the look-ahead that would prove it held nothing more is drawn from the
        same budget, which that one read spent."""
        from kiro_crew.dashboard.handlers import files as files_mod

        plain = plain_project
        (plain / "later").mkdir(parents=True)
        (plain / "later" / "inside.txt").write_text("x")
        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCAN_LIMIT", 1)
        opened = _count_project_reads(monkeypatch, plain)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert opened == {os.path.realpath(plain): 1}
        assert data["directories"] == ["later"]
        assert data["paths"] == []
        assert data["truncated"] is True
        assert data["truncatedDirectories"] == ["", "later"]

    @pytest.mark.asyncio
    async def test_walk_reads_each_folder_once(self, plain_project, mock_sel, monkeypatch):
        """One pass: each folder is read once, never once to count and again to
        list. A nested tree, so a second read of any level shows."""
        plain = plain_project
        (plain / "a" / "b" / "c").mkdir(parents=True)
        (plain / "a" / "b" / "c" / "leaf.txt").write_text("x")
        (plain / "a" / "one.txt").write_text("x")
        (plain / "z").mkdir()
        opened = _count_project_reads(monkeypatch, plain)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["paths"] == ["a/one.txt", "a/b/c/leaf.txt"]
        expected = {os.path.realpath(plain / p) for p in (".", "a", "a/b", "a/b/c", "z")}
        assert opened == {path: 1 for path in expected}

    @pytest.mark.asyncio
    async def test_a_listing_that_fails_part_way_is_an_unreadable_row(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A read can fail after ``scandir`` opened the folder -- the iterator
        raises on a later entry. What it yielded first is not a listing of the
        folder, so none of it is shown: the folder is a row named unreadable,
        exactly as when ``scandir`` itself refuses."""
        plain = plain_project
        (plain / "flaky").mkdir(parents=True)
        for name in ("a.txt", "b.txt", "c.txt"):
            (plain / "flaky" / name).write_text("x")
        flaky = _identity(os.path.realpath(plain / "flaky"))
        real_scandir = os.scandir

        class _FailsAfterOne:
            def __init__(self, inner):
                self._inner = inner
                self._yielded = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._inner.close()

            def __iter__(self):
                return self

            def __next__(self):
                if self._yielded:
                    raise PermissionError(errno.EACCES, "Permission denied", "flaky")
                self._yielded += 1
                return next(self._inner)

            def close(self):
                self._inner.close()

        def scandir(path=".", *args, **kwargs):
            inner = real_scandir(path, *args, **kwargs)
            if _identity(path) == flaky:
                return _FailsAfterOne(inner)
            return inner

        monkeypatch.setattr(os, "scandir", scandir)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["directories"] == ["flaky"]
        assert data["unreadableDirectories"] == ["flaky"]
        assert data["paths"] == []

    @pytest.mark.asyncio
    async def test_a_root_cut_by_the_scan_limit_is_truncated_not_hidden_only(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A root whose first entries are all hidden folders, cut by the scan
        limit, has not been read in full: what follows is unknown, so it is
        truncated -- never judged hidden-only or empty on what it did read."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCAN_LIMIT", 3)
        plain = plain_project
        # Skip-set names: dot-directories are walked, these never are.
        for name in ("build", "dist", "node_modules", "target", "venv"):
            (plain / name).mkdir(parents=True)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["paths"] == [] and data["directories"] == []
        assert data["truncated"] is True
        assert data["truncatedDirectories"] == [""]
        assert data["hiddenOnlyDirectories"] == []

    def test_git_folders_are_ordered_the_way_the_walk_discovers_them(self):
        """Breadth-first, then segment by segment: ``a/z`` before ``a-b/c``,
        because the walk reads ``a`` before ``a-b`` -- a whole-string sort would
        put ``a-b/c`` first (``-`` sorts before ``/``) and the row cap would
        keep a different folder on each branch."""
        from kiro_crew.dashboard.handlers.files import _project_tree_git_layout

        rows, files = _project_tree_git_layout(sorted(["a-b/c/x.txt", "a/z/y.txt", "top.txt"]))

        assert rows == ["a", "a-b", "a/z", "a-b/c"]
        assert files == {"a-b/c": ["x.txt"], "a/z": ["y.txt"], "": ["top.txt"]}

    @pytest.mark.parametrize("swap", ["rename", "symlink"])
    @pytest.mark.parametrize("by_descriptor", _LISTING_BRANCHES)
    @pytest.mark.asyncio
    async def test_a_folder_swapped_after_its_parent_was_listed_is_not_read(
        self, swap, by_descriptor, plain_project, tmp_path, mock_sel, monkeypatch
    ):
        """A subfolder is queued when its parent is listed and read one level
        later, by name. Between the two, the name is swapped for a directory
        outside the project -- renamed into place, or a symlink to it. The read
        is pinned to the identity the parent's listing saw, so what now sits at
        the name is not listed: the row stays, named unreadable -- not
        truncated, which would claim an item limit the small tree never hit --
        and no entry of the outside directory reaches the payload. ``by-name``
        is the branch a platform without descriptor listing takes (Windows).
        The symlink swap renames the outside folder into place and makes the
        name answer as a link -- ``ELOOP`` on the descriptor open, a link
        ``lstat`` by name -- so it needs no symlink privilege."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCANDIR_TAKES_FD", by_descriptor)
        plain = plain_project
        (plain / "a").mkdir(parents=True)
        (plain / "a" / "ordinary.txt").write_text("x")
        outside = tmp_path / "outside"
        (outside / "OUTSIDE_FOLDER").mkdir(parents=True)
        (outside / "OUTSIDE_NAME.txt").write_text("x")
        swapped = []
        if swap == "symlink":
            outside_identity = _identity(outside)
            real_is_link = files_mod._project_tree_is_link

            def is_link(st) -> bool:
                return (st.st_dev, st.st_ino) == outside_identity or real_is_link(st)

            monkeypatch.setattr(files_mod, "_project_tree_is_link", is_link)
            _refuse_link_open(
                monkeypatch, os.path.join(os.path.realpath(plain), "a"), lambda: bool(swapped)
            )
        root = _identity(os.path.realpath(plain))
        real_scandir = os.scandir

        def swap_in():
            # Once: teardown's own cleanup lists the root again through this
            # seam (by path on Windows), and the swap has already happened.
            if swapped:
                return
            os.rename(plain / "a", tmp_path / "a-moved-away")
            os.rename(outside, plain / "a")
            swapped.append(True)

        def scandir(path=".", *args, **kwargs):
            inner = real_scandir(path, *args, **kwargs)
            if _identity(path) == root:
                return _Listing(inner, on_close=swap_in)
            return inner

        monkeypatch.setattr(os, "scandir", scandir)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert "OUTSIDE_" not in json.dumps(data)
        assert data["directories"] == ["a"]
        assert data["truncated"] is False
        assert data["truncatedDirectories"] == []
        assert data["unreadableDirectories"] == ["a"]

    def test_a_root_swapped_for_a_link_is_unreadable_not_truncated(
        self, plain_project, monkeypatch
    ):
        """The root is opened ``O_NOFOLLOW`` too, so one swapped for a symlink
        after the handler resolved it fails with ``ELOOP``. Nothing was listed and
        no limit was reached: the payload names the root unreadable (``.``), as
        for any root the walk cannot read, never truncated. The descriptor
        branch is forced and the ``ELOOP`` comes from a seam, so the case runs
        on every host."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCANDIR_TAKES_FD", True)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)
        plain = plain_project
        (plain / "inside").mkdir(parents=True)
        _refuse_link_open(monkeypatch, plain)

        result = files_mod._project_tree_walk(str(plain), 50, 1_000)

        assert result["directories"] == [] and result["paths"] == []
        assert result["truncated"] is False
        assert result["truncatedDirectories"] == []
        assert result["unreadableDirectories"] == ["."]

    @pytest.mark.parametrize(
        ("tag", "is_link"),
        [(0xA000000C, True), (0xA0000003, True), (0x9000001A, False), (0x80000013, False)],
        ids=["symlink", "junction", "cloud-placeholder", "dedup"],
    )
    def test_only_a_reparse_point_naming_another_path_is_a_link(self, tag, is_link):
        """A Windows folder can carry a reparse tag and still hold its own
        contents: a cloud-files placeholder (a OneDrive project), a dedup
        directory. Only a name surrogate -- a symlink, a junction -- leads to
        another path; the rest are walked like any folder, not shown as a
        link with nothing listed beneath it."""
        import stat
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.files import _project_tree_is_link

        st = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=tag)
        assert _project_tree_is_link(st) is is_link

    @pytest.mark.asyncio
    async def test_the_look_ahead_is_drawn_from_the_scan_budget(
        self, plain_project, mock_sel, monkeypatch
    ):
        """Telling a full folder from a cut one pulls one entry past its share;
        that entry is counted against the same budget, so however many folders
        are cut, the walk pulls no more than ``_PROJECT_TREE_SCAN_LIMIT``."""
        from kiro_crew.dashboard.handlers import files as files_mod

        plain = plain_project
        for top in range(6):
            for index in range(20):
                (plain / f"d{top}").mkdir(parents=True, exist_ok=True)
                (plain / f"d{top}" / f"{index:02d}.txt").write_text("x")
        monkeypatch.setattr(files_mod, "_PROJECT_TREE_SCAN_LIMIT", 40)
        folders = {_identity(os.path.realpath(plain))}
        folders |= {_identity(os.path.realpath(plain / f"d{top}")) for top in range(6)}
        pulled = 0
        real_scandir = os.scandir

        def count(_entry):
            nonlocal pulled
            pulled += 1

        def scandir(path=".", *args, **kwargs):
            inner = real_scandir(path, *args, **kwargs)
            return _Listing(inner, on_entry=count) if _identity(path) in folders else inner

        monkeypatch.setattr(os, "scandir", scandir)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert 0 < pulled <= 40
        assert data["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_failed_git_listing_falls_back_to_the_bounded_walk(
        self, repo, mock_sel, monkeypatch
    ):
        """A repository whose ``ls-files`` overflows the output cap or the
        timeout (or a host with no sandbox backend) gets a nonzero return code
        and falls into the walk, so the walk's bound is what protects it."""
        from kiro_crew.dashboard.handlers import files as files_mod

        real = files_mod._run_git_bounded

        def overflowing(argv, **kwargs):
            if "ls-files" in argv:
                return -9, "", True
            return real(argv, **kwargs)

        monkeypatch.setattr(files_mod, "_run_git_bounded", overflowing)
        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 2)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()

        assert data["repo"] is False
        assert data["directories"] == ["src"]
        assert len(data["paths"]) == 1
        assert data["truncated"] is True
        assert ".git" not in json.dumps(data["directories"])

    @pytest.mark.parametrize("layout", ["walk", "git"])
    @pytest.mark.asyncio
    async def test_redaction_and_serialization_run_off_the_event_loop(
        self, layout, plain_project, repo, mock_sel, monkeypatch
    ):
        """Redacting every row and serializing the body are linear in the size
        of the listing, and the tree refetches every 10 s: on the event loop
        they stall every other request the gateway serves."""
        from kiro_crew.dashboard.handlers import files as files_mod

        if layout == "walk":
            project = plain_project
            (project / "docs").mkdir(parents=True)
            (project / "docs" / "readme.md").write_text("x")
        else:
            project = repo
        loop_thread = threading.get_ident()
        threads: dict[str, set[int]] = {"segments": set(), "body": set()}
        real_segments = files_mod.redact_path_segments
        real_body = files_mod._project_tree_body

        def segments(path, redactor=None):
            threads["segments"].add(threading.get_ident())
            return real_segments(path, redactor)

        def body(result):
            threads["body"].add(threading.get_ident())
            return real_body(result)

        monkeypatch.setattr(files_mod, "redact_path_segments", segments)
        monkeypatch.setattr(files_mod, "_project_tree_body", body)
        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get(f"/api/project/tree?path={project}")
            assert resp.status == 200
            assert resp.content_type == "application/json"
            data = await resp.json()

        assert data["repo"] is (layout == "git")
        assert threads["segments"] and threads["body"]
        assert loop_thread not in threads["segments"] | threads["body"]

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
        """A folder holding ONLY entries the walk drops (``.git``, tooling
        caches) comes back as a directory row with nothing beneath it, exactly
        like a folder that is empty on disk. The tree draws a state row under a
        childless folder, and that row may only call the folder empty when it
        is: ``hiddenOnlyDirectories`` names the ones that are not.
        """
        plain = plain_project
        (plain / "_bg" / ".git").mkdir(parents=True)
        (plain / "_bg" / ".git" / "HEAD").write_text("x")
        (plain / "caches" / "node_modules").mkdir(parents=True)
        (plain / "empty").mkdir()
        # A hidden entry beside a listed file or a kept subfolder is not the
        # reported case: that folder has rows beneath it.
        (plain / "mixed" / ".git").mkdir(parents=True)
        (plain / "mixed" / "kept.txt").write_text("x")
        (plain / "nested" / ".git").mkdir(parents=True)
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
    async def test_a_folder_holding_only_a_directory_symlink_is_not_hidden_only_and_the_link_is_a_row(
        self, plain_project, mock_sel, monkeypatch
    ):
        """``ls deploy`` shows ``current``: a symlink to a directory is a visible,
        navigable entry, so its folder is NOT hidden-only (hidden-only means every
        entry is one the listing filters out by nature -- dot-directories, the
        skip set). The walk never follows a link (against link cycles), so
        nothing beneath it would be listed and the folder would read as empty;
        so the link is listed as a directory
        row of its own and named in ``linkedDirectories``, and nothing beneath it
        is listed. The link is a seam (``_pretend_directory_symlink``) over a real
        directory holding a file, so the assertion runs where symlinks need a
        privilege, and the file beneath proves the walk did not go in.
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

        assert data["hiddenOnlyDirectories"] == []
        assert data["linkedDirectories"] == ["linked/current"]
        # The folder AND the link are rows, in walk (breadth-first) order; the
        # link's target is never walked, so no file beneath it is listed.
        assert data["directories"] == ["linked", "releases", "linked/current"]
        assert data["paths"] == ["releases/kept.txt"]
        assert data["unreadableDirectories"] == []

    @pytest.mark.asyncio
    async def test_a_directory_symlink_named_like_a_skip_directory_is_hidden_and_not_a_row(
        self, plain_project, mock_sel, monkeypatch
    ):
        """``shared/node_modules -> ../store/node_modules`` is filtered exactly
        like the real ``node_modules`` beside it: the name filter applies to
        every entry alike, link or not, so what a folder shows is predictable
        from the name alone (Design lane on ``9f52681b54``). A filtered link is
        a hidden entry -- a folder holding nothing else is hidden-only -- and is
        no row, in ``directories`` or ``linkedDirectories``. A dot-named link
        (``dotted/.cache``) follows the same rule. A link the filter KEEPS
        (``deploy/current``) stays a row of its own, as the test above pins.
        """
        plain = plain_project
        (plain / "store" / "node_modules" / "dep").mkdir(parents=True)
        (plain / "store" / "node_modules" / "dep" / "index.js").write_text("x")
        (plain / "shared" / "node_modules" / "dep").mkdir(parents=True)
        (plain / "shared" / "node_modules" / "dep" / "index.js").write_text("x")
        (plain / "dotted" / ".cache").mkdir(parents=True)
        (plain / "dotted" / ".cache" / "entry").write_text("x")
        _pretend_directory_symlink(monkeypatch, plain / "shared" / "node_modules")
        _pretend_directory_symlink(monkeypatch, plain / "dotted" / ".cache")

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        # Each folder holds one hidden entry -- a real cache, a linked cache, a
        # linked dot-directory -- and nothing else: all three are hidden-only,
        # none of the hidden entries is a row, and no link is reported.
        assert data["hiddenOnlyDirectories"] == ["dotted", "shared", "store"]
        assert data["linkedDirectories"] == []
        assert data["directories"] == ["dotted", "shared", "store"]
        # Nothing behind a link or inside a hidden directory is listed.
        assert data["paths"] == []
        assert data["unreadableDirectories"] == []

    @pytest.mark.asyncio
    async def test_walk_lists_a_kept_directory_it_could_not_read(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A kept, non-symlink child whose ``scandir`` fails (permission denied)
        must not leave its parent childless: with no row beneath it the parent is
        not hidden-only (the child is no symlink), and the tree would call the
        parent an empty folder -- a lie, ``ls`` shows the child. The unreadable directory
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
    async def test_a_listed_child_whose_lstat_fails_is_unreadable_not_truncated(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A folder that is readable but not searchable (mode ``r--``) lists its
        names, and its subfolders' types come from the listing, yet the ``lstat``
        that records each subfolder's identity is refused. Such a child is a row
        nothing read -- unreadable, exactly as when its own ``scandir`` refuses
        -- and never reported as cut by the item limit: a three-entry tree is
        not truncated. The refusal is a seam on the identity read, so this runs
        as root and on Windows too."""
        from kiro_crew.dashboard.handlers import files as files_mod

        plain = plain_project
        for child in ("child", "child2"):
            (plain / "noexec" / child).mkdir(parents=True)
        (plain / "top.txt").write_text("x")
        (plain / "noexec" / "f.txt").write_text("x")
        refused = {_identity(os.path.realpath(plain / "noexec" / c)) for c in ("child", "child2")}
        real_identity = files_mod._project_tree_identity

        def identity(entry, native):
            if _identity(native) in refused:
                raise PermissionError(errno.EACCES, "Permission denied", native)
            return real_identity(entry, native)

        monkeypatch.setattr(files_mod, "_project_tree_identity", identity)
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert data["directories"] == ["noexec", "noexec/child", "noexec/child2"]
        assert data["unreadableDirectories"] == ["noexec/child", "noexec/child2"]
        assert data["truncated"] is False
        assert data["truncatedDirectories"] == []
        assert data["paths"] == ["top.txt", "noexec/f.txt"]

    @pytest.mark.asyncio
    async def test_walk_names_an_unreadable_root_instead_of_an_empty_workspace(
        self, plain_project, mock_sel, monkeypatch
    ):
        """A ``scandir`` failure on the project root itself leaves the walk with
        nothing read, so the payload
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
        entries the walk drops (here ``.git/`` and a ``node_modules/`` cache)
        yields no file and no kept subdirectory, so the payload is
        ``paths == [] and directories == []`` -- the empty-workspace shape --
        and the dashboard would paint "No files in this workspace yet" over a
        folder that is not empty: the claim this listing refuses to make one
        level down, made about the whole tree. The root is no directory row of
        its own, so it is named as ``.`` in ``hiddenOnlyDirectories``, exactly
        as an unreadable root is named in ``unreadableDirectories``.
        """
        plain = plain_project
        (plain / ".git").mkdir(parents=True)
        (plain / ".git" / "HEAD").write_text("x")
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
        (plain / "AKIAIOSFODNN7EXAMPLE" / ".git").mkdir(parents=True)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(data)
        assert len(data["directories"]) == 1
        assert data["directories"][0].startswith("[REDACTED: credential]")
        # The join the tree performs: the marker IS the redacted row.
        assert data["hiddenOnlyDirectories"] == data["directories"]

    @pytest.mark.asyncio
    async def test_a_linked_marker_is_redacted_like_its_directory_row(
        self, plain_project, mock_sel, monkeypatch
    ):
        """Mutation pin for ``"linkedDirectories"`` in the egress redaction
        tuple, same shape as the other two: the link's row and the marker
        naming it must be the same redacted string, and the raw credential-shaped
        name must appear nowhere in the body.
        """
        plain = plain_project
        link = plain / "AKIAIOSFODNN7EXAMPLE"
        link.mkdir(parents=True)
        _pretend_directory_symlink(monkeypatch, link)

        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/tree?path={plain}")
            data = await resp.json()

        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(data)
        assert len(data["directories"]) == 1
        assert data["directories"][0].startswith("[REDACTED: credential]")
        assert data["linkedDirectories"] == data["directories"]
        # The root holds a visible entry, so it is neither empty nor hidden-only.
        assert data["hiddenOnlyDirectories"] == []

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

    def test_truncation_copy_names_the_served_row_cap(self):
        """The state row under a truncated folder and the workspace-level notice
        state the row cap as a literal in every catalog (``10,000``; a payload
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
        # git lists a symlink as a file (the link is the tracked object), so
        # this branch never has a linked directory row to qualify.
        assert data["linkedDirectories"] == []
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
        # One cap covers files and folder rows; the four folders come first.
        assert data["directories"] == ["docs", "src", "zz_vendor", "zz_vendor/deep"]
        assert len(data["paths"]) == 16
        # The point of the fix: every tracked file keeps a row.
        for rel in tracked:
            assert rel in data["paths"], f"{rel} was evicted by the untracked block"
        # ...and the fix does not merely invert the loss: the untracked subtree
        # still spends the remaining budget, so it keeps a row too.
        assert any(p.startswith("zz_vendor/") for p in data["paths"])

    @pytest.mark.asyncio
    async def test_git_listing_spends_the_same_cap_as_the_walk(self, repo, mock_sel, monkeypatch):
        """The git branch spends the one cap the walk does: folder rows
        shallowest first, but never past half the cap while files remain, the
        root's files first among those -- and a file whose folder lost its row
        is not listed either. Every shown folder that lost something is named."""
        from kiro_crew.dashboard.handlers import files as files_mod

        monkeypatch.setattr(files_mod, "_PROJECT_TREE_MAX_ENTRIES", 3)
        for top in ("p", "q"):
            nested = repo / top / "deep"
            nested.mkdir(parents=True)
            (nested / "f.txt").write_text("x")

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/tree?path={repo}")
            data = await resp.json()

        assert data["repo"] is True
        assert data["directories"] == ["p"]
        assert data["paths"] == [".gitignore", "a.txt"]
        assert data["truncated"] is True
        assert data["truncatedDirectories"] == ["", "p"]
