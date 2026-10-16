"""Credential isolation and startup ownership for the remote Git channel."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import git_gateway


@pytest.mark.asyncio
async def test_host_git_ignores_repository_and_environment_helpers(tmp_path, monkeypatch):
    from kiro_crew.platform_compat import trusted_git_bin

    git = trusted_git_bin()
    if git is None:
        pytest.skip("trusted Git is required")
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    marker = tmp_path / "repository-helper-ran"
    helper = f"!echo ran > {shlex.quote(str(marker))}; echo username=bad; echo password=bad"
    # Git's shell helper syntax is the same with Git for Windows. All config
    # files and markers belong to this test, including the positive control.
    safe_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    safe_env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home))
    safe_env["GIT_CONFIG_NOSYSTEM"] = "1"

    def run(*args, data=None):
        return subprocess.run(
            [git, *args],
            input=data,
            cwd=repo,
            env=safe_env,
            capture_output=True,
            check=True,
            timeout=30,
        )

    run("init", "-q")
    run("config", "credential.helper", helper)
    query = b"protocol=https\nhost=bitbucket.org\n\n"
    run("credential", "fill", data=query)
    assert marker.read_text().strip() == "ran"
    marker.unlink()
    (home / ".gitconfig").write_text(
        "[credential]\n\thelper =\n"
        '\thelper = "!f() { echo username=operator; echo password=test-only; }; f"\n',
        encoding="utf-8",
    )
    overrides = {
        "GIT_DIR": str(repo / ".git"),
        "GIT_WORK_TREE": str(repo),
        "GIT_COMMON_DIR": str(repo / ".git"),
        "GIT_CONFIG_GLOBAL": str(repo / ".git" / "config"),
        "GIT_CONFIG_SYSTEM": str(repo / ".git" / "config"),
        "GIT_CONFIG": str(repo / ".git" / "config"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": helper,
        "GIT_CONFIG_PARAMETERS": "'credential.helper=" + helper + "'",
        "GIT_EXEC_PATH": str(repo),
    }
    for key, value in {**safe_env, **overrides}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(repo)
    # Spy on the real seam: the actual Git process must still answer from the
    # operator's global helper, with no inherited repository/config selectors.
    original = git_gateway._run_quiet
    calls = []

    async def recorded(argv, stdin=b"", env=None, **kwargs):
        calls.append((env, kwargs))
        return await original(argv, stdin, env, **kwargs)

    monkeypatch.setattr(git_gateway, "_run_quiet", recorded)
    assert await git_gateway._from_git_credential("bitbucket.org") == git_gateway._basic(
        "operator", "test-only"
    )
    assert not marker.exists()
    env, kwargs = calls[0]
    assert not (overrides.keys() & env.keys())
    assert kwargs["cwd"] == str(Path(git).parent)
    assert env["GIT_CEILING_DIRECTORIES"] == kwargs["cwd"]


@pytest.mark.asyncio
async def test_git_credentials_refuse_an_untrusted_binary(monkeypatch):
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: None)
    monkeypatch.setattr(git_gateway, "_binary", lambda _: "agent-controlled-git")
    with pytest.raises(git_gateway._NoCredential, match="trusted git"):
        await git_gateway._from_git_credential("bitbucket.org")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["link", "readiness", "cancelled"])
async def test_channel_startup_failure_reaps_child_and_joins_serve(tmp_path, monkeypatch, failure):
    from unittest.mock import AsyncMock, Mock

    import test_remote_git_proxy as proxy_tests

    process = SimpleNamespace(
        returncode=None,
        killed=False,
        reaped=False,
        stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
    )

    def kill():
        process.killed = True
        process.returncode = -9

    async def wait():
        process.reaped = True
        return process.returncode

    process.kill, process.wait = kill, wait

    async def spawn(*args, **kwargs):
        assert kwargs["cwd"] == str(tmp_path)
        return process

    def link(_process):
        if failure == "link":
            raise RuntimeError("link startup failed")
        return object()

    serving = []
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Channel:
        def __init__(self, *args, **kwargs):
            pass

        async def serve(self):
            serving.append(asyncio.current_task())
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    async def readiness(*args, **kwargs):
        await asyncio.wait_for(entered.wait(), 30)
        if failure == "cancelled":
            raise asyncio.CancelledError()
        raise AssertionError("proxy port missing")

    monkeypatch.setattr(proxy_tests.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(proxy_tests, "ProxyLink", link)
    monkeypatch.setattr(proxy_tests, "GitProxyChannel", Channel)
    monkeypatch.setattr(proxy_tests, "async_wait_until", readiness)
    expected = {
        "link": RuntimeError,
        "readiness": AssertionError,
        "cancelled": asyncio.CancelledError,
    }
    try:
        with pytest.raises(expected[failure]):
            await proxy_tests._run_channel(tmp_path, object())
        assert process.killed and process.reaped
        if failure != "link":
            assert stopped.is_set() and serving[0].done()
    finally:
        # The mutation check must not leak the serving task it deliberately exposes.
        for task in serving:
            task.cancel()
        await asyncio.gather(*serving, return_exceptions=True)


def test_textconv_setup_confines_children_at_spawn(tmp_path, monkeypatch):
    from test_coder_session_launch import test_git_diff_view_does_not_run_a_repository_textconv

    original = subprocess.run
    setups = []

    def run(argv, **kwargs):
        if argv[1:2] == ["-C"]:
            assert kwargs.get("cwd") == tmp_path / "repo"
            setups.append(argv[3])
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    test_git_diff_view_does_not_run_a_repository_textconv(tmp_path)
    assert setups == ["init", "add", "commit", "config"]


@pytest.fixture
def git_workdir(tmp_path, monkeypatch):
    from kiro_crew.platform_compat import trusted_git_bin
    from kiro_crew.remote_session import workspace_tools

    git = trusted_git_bin()
    if git is None:
        pytest.skip("Git is required")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))

    def run(*args):
        return subprocess.run(
            [git, *args], cwd=tmp_path, check=True, capture_output=True, timeout=30
        )

    run("init", "-b", "main")
    run(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "seed",
    )
    return workspace_tools.Workspace(str(tmp_path)), run


@pytest.mark.parametrize(
    "name", ["new.txt", "nested/new.txt", "space name.txt", "é.txt", "arrow -> name.txt", "[x].txt"]
)
def test_untracked_files_have_real_addition_diffs(git_workdir, name):
    from kiro_crew.remote_session import workspace_tools

    ws, _ = git_workdir
    if os.name == "nt" and ">" in name:
        pytest.skip("Windows filesystems do not support > in filenames")
    file = Path(ws.root) / name
    file.parent.mkdir(exist_ok=True)
    file.write_text("entirely new content\n", encoding="utf-8")
    status = workspace_tools.browse(ws, {"op": "git_status"})
    assert f"?? {name}\0" in status["output"]
    result = workspace_tools.browse(ws, {"op": "git_diff", "path": name})
    assert result["git"] is True and "+entirely new content" in result["output"]
    assert "error_text" not in result


@pytest.mark.parametrize("name", ["space name.txt", "é.txt", "arrow -> name.txt", "[x].txt"])
def test_status_literal_paths_reach_the_tracked_diff(git_workdir, name):
    from kiro_crew.remote_session import workspace_tools

    ws, git = git_workdir
    if os.name == "nt" and ">" in name:
        pytest.skip("Windows filesystems do not support > in filenames")
    file = Path(ws.root) / name
    file.write_text("before\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "tracked")
    file.write_text("after\n", encoding="utf-8")
    result = workspace_tools.browse(ws, {"op": "git_status"})
    assert f" M {name}\0" in result["output"]
    diff = workspace_tools.browse(ws, {"op": "git_diff", "path": name})
    assert "-before" in diff["output"] and "+after" in diff["output"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("## main...origin/main\0", None),
        (
            "## main...origin/main [ahead 2]\0 M space name.txt\0?? é.txt\0",
            "2 uncommitted files, 2 unpushed commits",
        ),
        ("## feature\0", "branch feature was never pushed"),
        ("## No commits yet on main\0?? new.txt\0", "1 uncommitted file"),
        ("## main...origin/main\0R  dest\nname\0src\nname\0?? a -> b\0", "2 uncommitted files"),
        ("## main...origin/main\0 C copy\0source\0 M tracked\0", "2 uncommitted files"),
    ],
)
def test_unsaved_warning_understands_nul_status(status, expected):
    from kiro_crew.coder.session_browse import summarize_unsaved

    assert summarize_unsaved(status) == expected


def test_guest_shell_case_declares_windows_prerequisite():
    import sys

    import test_remote_git_proxy_port_file as port_tests

    marks = port_tests.test_two_launches_in_one_guest_keep_separate_git_proxy_port_files.pytestmark
    assert any(
        mark.name == "skipif"
        and mark.args == (sys.platform == "win32",)
        and mark.kwargs.get("reason") == "POSIX guest shell"
        for mark in marks
    )
    # The subprocess/relay seam cases must still execute on every platform.
    for test in (
        port_tests.test_the_tool_server_routes_git_through_its_own_port_file,
        port_tests.test_tool_server_argv_names_the_launch_port_file,
    ):
        assert not getattr(test, "pytestmark", [])
