"""Tests for ``GET /api/project/git/branch-diff``: the branch's changes since its base."""

from __future__ import annotations

import os
import random
import string
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.dashboard.handlers import api_project_git_branch_diff
from kiro_crew.dashboard.handlers import files as files_mod

ROUTE = "/api/project/git/branch-diff"


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get(ROUTE, api_project_git_branch_diff)
    return as_owner(app)


@pytest.fixture(autouse=True)
def passthrough_sandbox(_floor_monkeypatch):
    """Run Git unwrapped; production answers 503 when no sandbox backend exists."""
    _floor_monkeypatch.setattr(
        files_mod,
        "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (list(argv), dict(kw.get("env") or os.environ), None),
    )


@pytest.fixture(autouse=True)
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    env.update(git_maintenance_off_entries(env))
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=env)


def _commit(repo: Path, message: str, **files: str | None) -> None:
    """Write (or, for ``None``, delete) each file and commit the result."""
    for name, body in files.items():
        if body is None:
            _git(repo, "rm", "-q", name)
        else:
            (repo / name).write_text(body)
            _git(repo, "add", name)
    _git(repo, "commit", "-qm", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """``main`` with one commit, checked out on ``feature`` cut from it."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "initial", **{"a.txt": "one\n", "gone.txt": "bye\n"})
    _git(root, "switch", "-qc", "feature")
    return root


async def _get(repo: Path, *known: str, headers: dict | None = None) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(*(known or (str(repo),))))) as client:
        resp = await client.get(ROUTE, params={"path": str(repo)}, headers=headers)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_returns_the_branch_changes_since_it_left_main(repo: Path) -> None:
    _commit(repo, "edit and add", **{"a.txt": "one\ntwo\n", "new.txt": "fresh\n"})
    _commit(repo, "drop", **{"gone.txt": None})
    # Work the base gained after the branch left it, and uncommitted work,
    # are both outside the branch's own changes.
    _git(repo, "switch", "-q", "main")
    _commit(repo, "main moves on", **{"later.txt": "main only\n"})
    _git(repo, "switch", "-q", "feature")
    (repo / "a.txt").write_text("one\ntwo\nuncommitted\n")

    status, data = await _get(repo)

    assert status == 200
    assert (data["repo"], data["base"], data["omitted"]) == (True, "main", 0)
    diff = data["diff"]
    assert "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n" in diff
    assert "+two" in diff and "uncommitted" not in diff
    assert "diff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n" in diff
    assert "diff --git a/gone.txt b/gone.txt\n--- a/gone.txt\n+++ /dev/null\n" in diff
    assert "later.txt" not in diff


@pytest.mark.asyncio
async def test_lists_the_whole_branch_from_a_subdirectory_under_diff_relative(repo: Path) -> None:
    # A project in a subdirectory of a repository that sets diff.relative still
    # sees a change outside that subdirectory, under its repository path.
    sub = repo / "sub"
    sub.mkdir()
    (sub / "keep.txt").write_text("keep\n")
    _git(repo, "add", "sub/keep.txt")
    _commit(repo, "edit outside sub", **{"a.txt": "one\ntwo\n"})
    _git(repo, "config", "diff.relative", "true")

    status, data = await _get(sub)

    assert status == 200
    assert "diff --git a/a.txt b/a.txt\n" in data["diff"]
    assert "diff --git a/sub/keep.txt b/sub/keep.txt\n" in data["diff"]


@pytest.mark.asyncio
async def test_a_binary_addition_and_deletion_keep_their_change_type(repo: Path) -> None:
    _git(repo, "switch", "-q", "main")
    (repo / "old.bin").write_bytes(b"\0old")
    _git(repo, "add", "old.bin")
    _git(repo, "commit", "-qm", "binary on main")
    _git(repo, "switch", "-q", "feature")
    _git(repo, "rebase", "-q", "main")
    (repo / "new.bin").write_bytes(b"\0new")
    _git(repo, "add", "new.bin")
    _git(repo, "rm", "-q", "old.bin")
    _git(repo, "commit", "-qm", "swap binaries")

    status, data = await _get(repo)

    assert status == 200
    assert (
        "diff --git a/new.bin b/new.bin\n--- /dev/null\n+++ b/new.bin\n"
        "Binary files a/new.bin and b/new.bin differ"
    ) in data["diff"]
    assert (
        "diff --git a/old.bin b/old.bin\n--- a/old.bin\n+++ /dev/null\n"
        "Binary files a/old.bin and b/old.bin differ"
    ) in data["diff"]


@pytest.mark.asyncio
async def test_prefers_the_remote_default_branch(repo: Path, tmp_path: Path) -> None:
    # The clone's origin/HEAD names whatever the source has checked out.
    _git(repo, "switch", "-q", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _commit(clone, "local", **{"a.txt": "local\n"})

    status, data = await _get(clone)

    assert (status, data["base"]) == (200, "origin/main")
    assert "+local" in data["diff"]


@pytest.mark.asyncio
async def test_masks_each_whole_body_before_diffing(repo: Path) -> None:
    """A hunk shows key lines without the PEM frame the redactor reads them by."""
    rnd = random.Random(7)
    lines = ["".join(rnd.choices(string.ascii_letters + string.digits, k=64)) for _ in range(6)]
    frame = ("-----BEGIN " + "RSA PRIVATE KEY-----", "-----END " + "RSA PRIVATE KEY-----")
    _git(repo, "switch", "-q", "main")
    _commit(repo, "key", **{"id.pem": "\n".join([frame[0], *lines, frame[1]]) + "\n"})
    _git(repo, "switch", "-qC", "feature")
    lines[3] = lines[3][::-1]
    _commit(repo, "rotate one line", **{"id.pem": "\n".join([frame[0], *lines, frame[1]]) + "\n"})

    status, data = await _get(repo)

    assert status == 200
    assert not any(line in data["diff"] for line in lines)
    # Both masked bodies are equal, so the file has nothing to show and is counted.
    assert data["omitted"] == 1


@pytest.mark.asyncio
async def test_omits_a_sensitive_path(repo: Path, monkeypatch) -> None:
    real = files_mod.is_sensitive_path
    monkeypatch.setattr(
        files_mod, "is_sensitive_path", lambda p, *a: p.endswith("credentials") or real(p, *a)
    )
    _commit(repo, "creds", **{"credentials": "aws_secret=x\n", "b.txt": "added\n"})

    status, data = await _get(repo)

    assert (status, data["omitted"]) == (200, 1)
    assert "b.txt" in data["diff"] and "credentials" not in data["diff"]


@pytest.mark.asyncio
async def test_counts_files_past_the_cap_as_omitted(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(files_mod, "_BRANCH_DIFF_MAX_FILES", 1)
    _commit(repo, "two files", **{"a.txt": "edited\n", "b.txt": "added\n"})

    status, data = await _get(repo)

    assert (status, data["omitted"]) == (200, 1)
    assert "a/a.txt" in data["diff"] and "b.txt" not in data["diff"]


@pytest.mark.asyncio
async def test_answers_no_base_when_no_base_branch_exists(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    _commit(root, "initial", **{"a.txt": "one\n"})

    status, data = await _get(root)

    assert (status, data) == (200, {"repo": True, "base": None, "diff": "", "omitted": 0})


@pytest.mark.asyncio
async def test_refuses_a_repository_that_declares_a_filter_driver(repo: Path) -> None:
    _git(repo, "config", "filter.lfs.clean", "git-lfs clean -- %f")

    status, data = await _get(repo)

    assert (status, data["code"], data["cause"]) == (
        503,
        "git_branch_diff_filter_refused",
        "declared",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("git_reads_no_lazy_fetch", [True, False])
async def test_a_partial_clone_never_fetches_a_missing_blob(
    tmp_path: Path, _floor_monkeypatch, git_reads_no_lazy_fetch: bool
) -> None:
    """No transport starts, on a git that reads GIT_NO_LAZY_FETCH or one too old to."""
    # The variable arrived with the --no-lazy-fetch flag; a git without one has neither.
    has_flag = not subprocess.run(
        ["git", "--no-lazy-fetch", "version"], capture_output=True
    ).returncode
    if git_reads_no_lazy_fetch and not has_flag:
        pytest.skip("this git predates GIT_NO_LAZY_FETCH")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "first", **{"a.txt": "one\n"})
    _commit(seed, "second", **{"a.txt": "two\n"})
    _git(seed, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial"
    _git(
        tmp_path,
        "-c",
        "protocol.file.allow=always",
        "clone",
        "-q",
        "--filter=blob:none",
        seed.as_uri(),
        str(clone),
    )
    # The base moves back to the first commit, whose a.txt blob was never fetched.
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD~1")
    marker = tmp_path / "lazy-fetch-ran"
    script = tmp_path / "probe.py"
    script.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n")
    # remote-ext escapes spaces with %, not shell quoting; no network is used.
    command = " ".join(
        s.replace("%", "%%").replace(" ", "% ") for s in (sys.executable, str(script))
    )
    _git(clone, "config", "remote.origin.url", f"ext::{command}")
    _git(clone, "config", "protocol.ext.allow", "always")
    # An older git ignores the variable; the unwrapped spawn drops it the same way.
    if not git_reads_no_lazy_fetch:
        _floor_monkeypatch.setattr(
            files_mod,
            "sandboxed_spawn_argv",
            lambda argv, mode="standard", **kw: (
                list(argv),
                {
                    k: v
                    for k, v in (kw.get("env") or os.environ).items()
                    if k != "GIT_NO_LAZY_FETCH"
                },
                None,
            ),
        )

    status, data = await _get(clone)

    assert not marker.exists()
    # A git that reads the variable counts the missing blob; an older one fails the read.
    expected = (200, 1) if git_reads_no_lazy_fetch else (503, None)
    assert (status, data.get("omitted")) == expected


@pytest.mark.asyncio
async def test_refuses_a_caller_who_is_not_the_owner(repo: Path) -> None:
    status, data = await _get(repo, headers={"X-Test-User": "someone-else"})

    assert (status, data["code"]) == (403, "owner_only")


@pytest.mark.asyncio
async def test_refuses_an_unknown_directory(repo: Path, tmp_path: Path) -> None:
    status, data = await _get(repo, str(tmp_path / "elsewhere"))

    assert (status, data["code"]) == (403, "unknown_project_dir")
