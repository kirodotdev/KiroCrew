"""Credential isolation and startup ownership for the remote Git channel."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.remote_session import git_gateway


def _files_holding(root: Path, secret: bytes) -> set[Path]:
    """Every file under ``root`` whose bytes contain ``secret``."""
    return {p for p in root.rglob("*") if p.is_file() and secret in p.read_bytes()}


@pytest.mark.asyncio
async def test_host_git_ignores_repository_and_environment_helpers(tmp_path, monkeypatch):
    from kiro_crew.platform_compat import trusted_git_bin

    git = trusted_git_bin()
    if git is None:
        pytest.skip("trusted Git is required")
    # The subprocess boundary is real; provenance has its own host-independent
    # tests below (CI may run as root or use a non-system Git installation).
    monkeypatch.setattr(
        "kiro_crew.github_runner.validate_provider_executable",
        lambda path, *, require_protected: path,
    )
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
    safe_env.update(git_maintenance_off_entries(safe_env))

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
    global_marker = tmp_path / "global-helper-ran"
    # Forward slashes: a backslash in a quoted .gitconfig value is an escape, and
    # a Windows path written raw would make the whole file unreadable to git.
    global_helper = f"!echo ran > {shlex.quote(global_marker.as_posix())}"
    # An untrusted global helper must not execute; the fixed store selector
    # still supplies the fake operator login after it.
    (home / ".gitconfig").write_text(
        # The empty entry resets the helper list, so a system helper (osxkeychain,
        # manager) holding the operator's own login is never consulted.
        f'[credential]\n\thelper =\n\thelper = "{global_helper}"\n\thelper = store\n',
        encoding="utf-8",
    )
    (home / ".git-credentials").write_bytes("https://operator:test-only@bitbucket.org\n".encode())
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
        calls.append((dict(env), kwargs, list(argv), bytes(stdin)))
        return await original(argv, stdin, env, **kwargs)

    monkeypatch.setattr(git_gateway, "_run_quiet", recorded)
    before = _files_holding(tmp_path, b"test-only")
    # Every gateway lends here, Windows included: there each validated helper
    # runs directly, with no shell (git's own `sh` is not on the trusted PATH).
    assert await git_gateway._from_git_credential("bitbucket.org") == git_gateway._basic(
        "operator", "test-only"
    )
    assert not marker.exists()
    assert not global_marker.exists()
    env, kwargs, _argv, _stdin = calls[0]
    assert not (overrides.keys() & env.keys())
    # The secret came back over a helper's stdout pipe only: it is in no
    # command line, no environment, no request and no file written meanwhile.
    for call_env, _kwargs, argv, stdin in calls:
        assert not any("test-only" in part for part in argv)
        assert not any("test-only" in value for value in call_env.values())
        assert b"test-only" not in stdin
    assert _files_holding(tmp_path, b"test-only") == before
    assert kwargs["cwd"] == str(Path(git).parent)
    assert env["GIT_CEILING_DIRECTORIES"] == kwargs["cwd"]


@pytest.mark.asyncio
async def test_an_organisation_scoped_helper_fails_closed_and_says_what_to_set(
    tmp_path, monkeypatch
):
    """A helper set for one Azure organisation is not matched by the per-host
    lookup (a known limit): no login is lent, git is not left waiting on a
    prompt, and the refusal names the setting that does work."""
    from kiro_crew.platform_compat import trusted_git_bin

    if trusted_git_bin() is None:
        pytest.skip("trusted Git is required")
    monkeypatch.setattr(
        "kiro_crew.github_runner.validate_provider_executable",
        lambda path, *, require_protected: path,
    )

    async def no_az(_host):
        raise git_gateway._NoCredential("trusted az is not installed")

    monkeypatch.setitem(git_gateway._SOURCES, "az", no_az)
    # The gateway drops every GIT_* variable from the environment it gives git,
    # so a host's system config (macOS git ships credential.helper=osxkeychain)
    # would supply a general helper; keep it out so only this test's config counts.
    original_env = git_gateway._credential_env
    monkeypatch.setattr(
        git_gateway,
        "_credential_env",
        lambda *a, **k: {**original_env(*a, **k), "GIT_CONFIG_NOSYSTEM": "1"},
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[credential "https://dev.azure.com/example-org"]\n\thelper = store\n', encoding="utf-8"
    )
    (home / ".git-credentials").write_bytes(
        "https://operator:test-only@dev.azure.com/example-org\n".encode()
    )
    for key in [k for k in os.environ if k.upper().startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    with pytest.raises(git_gateway.GitProxyRefusal) as refused:
        await git_gateway.host_credential("dev.azure.com")
    message = refused.value.message
    assert refused.value.status == 502
    assert "credential.https://dev.azure.com/example-org.helper" in message
    assert "credential.https://dev.azure.com.helper" in message
    # The general sign-in steps are still named.
    assert "az login" in message
    assert "test-only" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,host", [("gh", "github.com"), ("glab", "gitlab.com"), ("az", "dev.azure.com")]
)
@pytest.mark.parametrize("readiness", [False, True])
async def test_agent_planted_provider_is_never_executed(
    tmp_path, monkeypatch, name, host, readiness
):
    from kiro_crew import github_runner

    binary = tmp_path / (name + (".exe" if os.name == "nt" else ""))
    binary.write_text("not a provider", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(github_runner.PROVIDER_CLI_OVERRIDE_ENV[name], raising=False)
    monkeypatch.setattr(github_runner, "provider_executable_candidates", lambda _: (str(binary),))
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: None)

    async def spawn(*args, **kwargs):
        pytest.fail("an agent-planted provider reached the unsandboxed spawn")

    monkeypatch.setattr(git_gateway.asyncio, "create_subprocess_exec", spawn)
    if readiness:
        assert await git_gateway.login_status(host) == (
            "not_signed_in" if name == "gh" else "unchecked"
        )
    else:
        with pytest.raises(git_gateway.GitProxyRefusal, match="no .* login"):
            await git_gateway.host_credential(host)


@pytest.mark.parametrize("name", ["gh", "glab", "az"])
def test_provider_override_requires_protected_provenance(monkeypatch, tmp_path, name):
    from kiro_crew import github_runner

    candidate = str(tmp_path / name)
    monkeypatch.setenv(github_runner.PROVIDER_CLI_OVERRIDE_ENV[name], candidate)
    checked = []

    def validate(path, *, require_protected=False):
        checked.append((path, require_protected))
        return path

    monkeypatch.setattr(github_runner, "validate_provider_executable", validate)
    assert git_gateway._binary(name) == candidate
    assert checked == [(candidate, True)]

    def reject(*args, **kwargs):
        raise ValueError("untrusted")

    monkeypatch.setattr(github_runner, "validate_provider_executable", reject)
    assert git_gateway._binary(name) is None


@pytest.mark.parametrize(
    "value",
    [
        "!sh -c 'echo password=bad'",
        "!/usr/bin/sh -c true",
        "!gh auth git-credential; echo bad",
        "!gh auth git-credential extra",
        "!gh auth git-credential # comment",
        "store --file=other",
        "cache --socket=other",
        "manager;echo bad",
        "/custom/git-credential-manager",
        "unknown",
    ],
)
def test_arbitrary_helper_programs_are_refused(value):
    assert git_gateway._helper_value(value, "unused") is None


@pytest.mark.parametrize("name", sorted(git_gateway._CREDENTIAL_HELPERS))
def test_fixed_helpers_use_protected_absolute_programs(tmp_path, monkeypatch, name):
    from kiro_crew import github_runner

    seen = []

    def validate(path, *, require_protected=False):
        seen.append((path, require_protected))
        return path

    monkeypatch.setattr(github_runner, "validate_provider_executable", validate)
    result = git_gateway._helper_value(name, str(tmp_path))
    assert result and shlex.split(result[1:]) == [seen[0][0]]
    assert seen == [
        (str(tmp_path / (f"git-credential-{name}" + (".exe" if os.name == "nt" else ""))), True)
    ]


def test_a_helper_outside_the_exec_path_is_found_in_a_trusted_system_dir(tmp_path, monkeypatch):
    """Git Credential Manager installs to /usr/local/bin, not Git's exec-path."""
    from kiro_crew import github_runner

    exec_path, system = tmp_path / "exec", tmp_path / "usr-local-bin"
    exec_path.mkdir()
    system.mkdir()
    name = "git-credential-manager" + (".exe" if os.name == "nt" else "")
    (system / name).write_text("#!/bin/sh\n", encoding="utf-8")
    protected: set[str] = {str(system / name)}

    def validate(path, *, require_protected=False):
        assert require_protected
        if path not in protected:
            raise ValueError("not protected")
        return path

    monkeypatch.setattr(github_runner, "validate_provider_executable", validate)
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_path", lambda: str(system))
    result = git_gateway._helper_value("manager", str(exec_path))
    assert result and shlex.split(result[1:]) == [str(system / name)]
    # Found but not protected: refused, as an exec-path helper would be.
    protected.clear()
    assert git_gateway._helper_value("manager", str(exec_path)) is None
    # A directory that is not a trusted system directory is never searched.
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_path", lambda: None)
    protected.add(str(system / name))
    assert git_gateway._helper_value("manager", str(exec_path)) is None


def test_gh_helper_discards_configured_path_and_quotes_the_protected_binary(tmp_path, monkeypatch):
    binary = str(tmp_path / "system install" / "gh")
    monkeypatch.setattr(git_gateway, "_binary", lambda name: binary if name == "gh" else None)
    result = git_gateway._helper_value("!/agent/bin/gh auth git-credential", "unused")
    assert shlex.split(result[1:]) == [binary, "auth", "git-credential"]
    assert git_gateway._helper_value("", "unused") == ""


@pytest.mark.asyncio
async def test_credential_fill_uses_a_validated_snapshot_not_mutable_config(tmp_path, monkeypatch):
    from kiro_crew import github_runner

    git = str(tmp_path / "git")
    # The `git credential fill` snapshot; the Windows direct path resolves its
    # argv once, before any helper runs (test_remote_git_windows_lending).
    monkeypatch.setattr(git_gateway, "_runs_helpers_directly", lambda: False)
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: git)
    monkeypatch.setattr(
        github_runner, "validate_provider_executable", lambda path, *, require_protected: path
    )
    monkeypatch.setattr(
        git_gateway,
        "_helper_value",
        lambda value, path: "!protected-helper" if value == "store" else "",
    )
    calls = []

    async def run(argv, stdin=b"", env=None, **kwargs):
        calls.append((argv, dict(env), stdin))
        if "config" in argv:
            return b"credential.helper\nstore\0credential.https://bitbucket.org.helper\n\0credential.https://bitbucket.org.helper\nstore\0"
        if "--exec-path" in argv:
            return os.fsencode(tmp_path)
        return b"username=operator\npassword=test-only\n"

    monkeypatch.setattr(git_gateway, "_run_quiet", run)
    assert await git_gateway._from_git_credential("bitbucket.org") == git_gateway._basic(
        "operator", "test-only"
    )
    argv, env, stdin = calls[-1]
    assert argv == [git, "credential", "fill"]
    synthesized = [
        ("credential.helper", "!protected-helper"),
        ("credential.https://bitbucket.org.helper", ""),
        ("credential.https://bitbucket.org.helper", "!protected-helper"),
    ]
    assert env["GIT_CONFIG_COUNT"] == str(len(synthesized))
    assert [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(len(synthesized))
    ] == synthesized
    assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert stdin == b"protocol=https\nhost=bitbucket.org\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "credential.helper=!sh -c 'id' #.helper",  # a subsection holding "="
        "credential.https://x\x01y.helper",
    ],
)
async def test_a_crafted_helper_key_is_never_passed_to_git(tmp_path, monkeypatch, key):
    from kiro_crew import github_runner

    git = str(tmp_path / "git")
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: git)
    monkeypatch.setattr(
        github_runner, "validate_provider_executable", lambda path, *, require_protected: path
    )
    monkeypatch.setattr(git_gateway, "_helper_value", lambda value, path: "!protected-helper")
    calls = []

    async def run(argv, stdin=b"", env=None, **kwargs):
        calls.append((argv, dict(env)))
        if "config" in argv:
            return f"{key}\nstore\0".encode()
        if "--exec-path" in argv:
            return os.fsencode(tmp_path)
        pytest.fail("credential fill ran with a crafted helper key")

    monkeypatch.setattr(git_gateway, "_run_quiet", run)
    with pytest.raises(git_gateway._NoCredential, match="no trusted credential helpers"):
        await git_gateway._from_git_credential("bitbucket.org")


@pytest.mark.asyncio
async def test_a_crafted_helper_subsection_runs_nothing_with_real_git(tmp_path, monkeypatch):
    """``[credential "helper=!cmd #"] helper = store`` must not become ``-c`` text
    that git splits at the ``=`` and runs as a shell helper."""
    from kiro_crew.platform_compat import trusted_git_bin

    git = trusted_git_bin()
    if git is None:
        pytest.skip("trusted Git is required")
    monkeypatch.setattr(
        "kiro_crew.github_runner.validate_provider_executable",
        lambda path, *, require_protected: path,
    )
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "crafted-helper-ran"
    (home / ".gitconfig").write_text(
        f'[credential "helper=!echo ran > {marker.as_posix()} #"]\n\thelper = store\n'
        # The empty entry resets the helper list past any system helper.
        "[credential]\n\thelper =\n\thelper = store\n",
        encoding="utf-8",
    )
    (home / ".git-credentials").write_bytes("https://operator:test-only@bitbucket.org\n".encode())
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    assert await git_gateway._from_git_credential("bitbucket.org") == git_gateway._basic(
        "operator", "test-only"
    )
    assert not marker.exists()


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


@pytest.mark.parametrize("provider", ["gh", "glab", "az", "git"])
def test_credential_children_scrub_injection_and_keep_their_login(tmp_path, monkeypatch, provider):
    from kiro_crew import platform_compat

    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "NODE_PATH",
        "LD_PRELOAD",
        "BASH_ENV",
        "GIT_CONFIG_COUNT",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(key, "must-not-reach-child")
    monkeypatch.setenv("GH_TOKEN", "github-test-only")
    monkeypatch.setenv("GITLAB_TOKEN", "gitlab-test-only")
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "azure"))
    monkeypatch.setenv("PATH", str(tmp_path / "agent-bin"))
    trusted_path = str(tmp_path / "system-bin")
    monkeypatch.setattr(platform_compat, "trusted_system_path", lambda: trusted_path)
    env = git_gateway._credential_env(provider)
    assert "must-not-reach-child" not in env.values()
    assert env["PATH"] == trusted_path
    assert env["PYTHONNOUSERSITE"] == "1"
    if provider in ("git", "gh"):
        assert env["GH_TOKEN"] == "github-test-only"
        assert "GITLAB_TOKEN" not in env
    elif provider == "glab":
        assert env["GITLAB_TOKEN"] == "gitlab-test-only"
        assert "GH_TOKEN" not in env
    else:
        assert env["AZURE_CONFIG_DIR"] == str(tmp_path / "azure")
        assert env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
        assert "GH_TOKEN" not in env


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX pid probe")
async def test_a_cancelled_lookup_kills_and_reaps_its_credential_cli(tmp_path):
    import sys

    from kiro_crew.testing.wait import async_wait_until

    # A session torn down mid-lookup cancels the lending task: the credential
    # child must not outlive it with its pipes open.
    pid_file = tmp_path / "pid"
    script = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    task = asyncio.create_task(
        git_gateway._run_quiet([sys.executable, "-c", script], env=dict(os.environ))
    )
    await async_wait_until(lambda: pid_file.exists() and pid_file.read_text())
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Killed and reaped: the pid names no process at all (not even a zombie).
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
