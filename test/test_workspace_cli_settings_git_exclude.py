"""The workspace cli.json overlay must not show up in a user's ``git status``.

kiro-cli reads workspace settings only from ``<cwd>/.kiro/settings/cli.json``, so
the overlay has to live in the work dir. When that dir is a git work tree, Crew
lists the overlay and its lock in the repository's local ``info/exclude`` so the
user's status stays clean while the settings still apply.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.providers.acp import _write_cli_overlay, _write_tool_search_overlay
from kiro_crew.workspace_cli_settings import (
    _EXCLUDE_COMMENT,
    CLI_SETTINGS_LOCK_NAME,
    workspace_cli_settings_lock,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "core.excludesFile=",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _status(path: Path) -> str:
    return _git(path, "status", "--porcelain", "--untracked-files=all")


def test_spawn_prep_leaves_git_status_clean_and_settings_apply(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")

    _write_tool_search_overlay(repo, True, 7, 1234)
    _write_cli_overlay(repo, "claude-opus-4.6", "high")

    assert _status(repo) == ""
    settings = json.loads((repo / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings["toolSearch.enabled"] is True
    assert settings["toolSearch.minPct"] == 7
    assert settings["toolSearch.minTokens"] == 1234
    assert settings["chat.modelDefaults"]["claude-opus-4.6"]["output_config"]["effort"] == "high"


def test_exclude_is_appended_once_and_keeps_user_lines(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    exclude = repo / ".git/info/exclude"
    exclude.write_text("# mine\n*.log", encoding="utf-8")  # no trailing newline

    for _ in range(3):
        with workspace_cli_settings_lock(repo) as cli_json:
            cli_json.write_text("{}", encoding="utf-8")

    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# mine", "*.log"]
    assert lines.count("/.kiro/settings/cli.json") == 1
    assert lines.count(f"/.kiro/settings/{CLI_SETTINGS_LOCK_NAME}") == 1
    assert _status(repo) == ""


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX append flags")
def test_exclude_is_opened_for_atomic_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fcntl = pytest.importorskip("fcntl")
    repo = _repo(tmp_path / "repo")
    opened_flags: list[int] = []
    real_fdopen = os.fdopen

    def capture_flags(fd: int, mode: str = "r", *args, **kwargs):
        if mode != "r":
            opened_flags.append(fcntl.fcntl(fd, fcntl.F_GETFL))
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", capture_flags)

    _write_tool_search_overlay(repo, True)

    assert opened_flags
    assert opened_flags[0] & os.O_APPEND


def test_a_line_appended_meanwhile_survives_the_exclude_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    exclude = repo / ".git/info/exclude"
    exclude_id = os.stat(exclude)
    real_fdopen = os.fdopen

    class AppendBeforeWrite:
        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, text: str) -> int:
            with open(exclude, "a", encoding="utf-8") as other:
                other.write("*.appended-meanwhile\n")
            return self._handle.write(text)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def __getattr__(self, name: str):
            return getattr(self._handle, name)

    def append_before_write(fd: int, mode: str = "r", *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        if ("w" in mode or "+" in mode) and os.path.samestat(os.fstat(fd), exclude_id):
            return AppendBeforeWrite(handle)
        return handle

    monkeypatch.setattr(os, "fdopen", append_before_write)

    _write_tool_search_overlay(repo, True)

    lines = exclude.read_text(encoding="utf-8").splitlines()
    # POSIX has always kept this green because O_APPEND positions the write.
    assert lines.count("*.appended-meanwhile") == 1
    assert "/.kiro/settings/cli.json" in lines
    assert lines.index("*.appended-meanwhile") < lines.index(_EXCLUDE_COMMENT)
    assert (repo / ".kiro/settings/cli.json").is_file()


def test_linked_worktree_status_stays_clean(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    tree = tmp_path / "tree"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(tree))

    _write_tool_search_overlay(tree, False)

    assert _status(tree) == ""
    assert _status(repo) == ""
    assert (tree / ".kiro/settings/cli.json").is_file()


def test_run_dirs_inside_a_repo_never_touch_its_exclude(tmp_path: Path) -> None:
    # A Crew workspace root that happens to sit inside a git repo: per-run dirs
    # are not work-tree tops, so the repo's exclude list must not grow per run.
    repo = _repo(tmp_path / "repo")
    exclude = repo / ".git/info/exclude"
    before = exclude.read_bytes()

    for name in ("subagent_a", "cron_job_1", "memory-consolidation_s_c"):
        run_dir = repo / "workspace" / name
        run_dir.mkdir(parents=True)
        _write_tool_search_overlay(run_dir, True)
        assert (run_dir / ".kiro/settings/cli.json").is_file()

    assert exclude.read_bytes() == before


def test_non_git_work_dir_gets_no_git_files(tmp_path: Path) -> None:
    work = tmp_path / "plain"
    work.mkdir()

    _write_tool_search_overlay(work, True)

    assert not (work / ".git").exists()
    assert (work / ".kiro/settings/cli.json").is_file()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
@pytest.mark.parametrize("planted", [".git/info/exclude", ".git/commondir"])
def test_a_fifo_at_a_git_path_does_not_hang_spawn_prep(tmp_path: Path, planted: str) -> None:
    repo = _repo(tmp_path / "repo")
    target = repo / planted
    target.unlink(missing_ok=True)
    os.mkfifo(target)

    _write_tool_search_overlay(repo, True)  # would block forever on a FIFO read

    settings = json.loads((repo / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings["toolSearch.enabled"] is True


def test_an_oversized_exclude_is_left_untouched(tmp_path: Path) -> None:
    # A newline exactly at the read limit and none at EOF: a truncated read
    # would skip the separator and glue Crew's comment onto the last pattern.
    repo = _repo(tmp_path / "repo")
    exclude = repo / ".git/info/exclude"
    exclude.write_text("x" * ((1 << 20) - 1) + "\n*.tail", encoding="utf-8")
    before = exclude.read_bytes()

    _write_tool_search_overlay(repo, True)

    assert exclude.read_bytes() == before
    assert (repo / ".kiro/settings/cli.json").is_file()


def test_a_planted_gitdir_pointer_writes_nothing_outside_a_real_git_dir(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "data-home" / "profiles"
    target.mkdir(parents=True)
    (work / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")

    _write_tool_search_overlay(work, True)

    assert list(target.iterdir()) == []
    assert (work / ".kiro/settings/cli.json").is_file()


@pytest.mark.parametrize(
    "link",
    [
        pytest.param(os.link, id="hardlink"),
        pytest.param(os.symlink, id="symlink", marks=requires_symlinks),
    ],
)
def test_a_linked_exclude_leaves_its_other_name_untouched(tmp_path: Path, link) -> None:
    # A hardlink is a second name for the same file, so O_NOFOLLOW cannot refuse
    # it; a symlink at ``exclude`` is followed where O_NOFOLLOW is 0 (Windows).
    repo = _repo(tmp_path / "repo")
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"ORIGINAL-LINE\n")
    exclude = repo / ".git/info/exclude"
    exclude.unlink()
    link(victim, exclude)

    _write_tool_search_overlay(repo, True)

    assert victim.read_bytes() == b"ORIGINAL-LINE\n"
    assert (repo / ".kiro/settings/cli.json").is_file()


def test_a_junctioned_info_dir_gets_nothing_written_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    info = repo / ".git/info"
    shutil.rmtree(info)
    platform_compat.symlink_or_junction(outside, info)
    # ``Path.is_symlink()`` is False for a Windows junction; make the link under
    # test answer that way on every platform so the guard is judged as there.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    assert platform_compat.is_link_or_junction(info)

    _write_tool_search_overlay(repo, True)

    assert not (outside / "exclude").exists()
    assert (repo / ".kiro/settings/cli.json").is_file()


def test_exclude_failure_does_not_block_the_overlay(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    exclude = repo / ".git/info/exclude"
    exclude.unlink()
    exclude.mkdir()  # an unreadable exclude must not break spawn prep

    _write_tool_search_overlay(repo, True)

    settings = json.loads((repo / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings["toolSearch.enabled"] is True
