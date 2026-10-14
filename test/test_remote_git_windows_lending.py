"""Windows git credential lending: helpers run without a shell, owner-only files.

Git runs every credential helper through ``sh -c``; on Windows the gateway's
trusted ``PATH`` has no ``sh``, so each validated helper is run directly there
(``_fill_directly``). The rules that path follows run on every shard through its
seams; the tests marked ``_WINDOWS`` exercise the real Windows primitives.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.remote_session import git_gateway, relay
from kiro_crew.testing.wait import async_wait_until

_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows primitives")
_POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")
SECRET = "test-only-secret"


# ── Which helpers git would ask, in its order ────────────────────────────


@pytest.mark.parametrize(
    ("key", "applies"),
    [
        ("credential.helper", True),
        ("credential.https://bitbucket.org.helper", True),
        ("credential.https://BITBUCKET.ORG/.helper", True),
        ("credential.https://bitbucket.org:443.helper", True),
        ("credential.https://*.org.helper", True),
        ("credential.https://gitlab.com.helper", False),
        ("credential.http://bitbucket.org.helper", False),
        ("credential.https://me@bitbucket.org.helper", False),
        ("credential.https://bitbucket.org:8443.helper", False),
        ("credential.https://bitbucket.org/team.helper", False),
        ("credential.bitbucket.org.helper", False),
        ("credential.https://*.bitbucket.org.helper", False),
    ],
)
def test_a_url_scoped_helper_applies_only_to_its_own_host(key, applies):
    assert git_gateway._helper_applies(key, "bitbucket.org") is applies


def test_the_helper_order_follows_config_order_and_its_resets():
    entries = [
        ("credential.helper", ["first"]),
        ("credential.https://gitlab.com.helper", ["other-host"]),
        ("credential.helper", []),  # git's reset: everything above is dropped
        ("credential.https://bitbucket.org.helper", ["scoped"]),
        ("credential.helper", ["last"]),
    ]
    assert git_gateway._windows_helper_order(entries, "bitbucket.org") == [["scoped"], ["last"]]


# ── The protocol git speaks to a helper, without git ────────────────────


def _scripted(monkeypatch, answers):
    """Stand in for the helper processes: one scripted stdout per argv[0]."""
    calls = []

    async def run(argv, stdin=b"", env=None, *, cwd=None):
        calls.append((list(argv), stdin, dict(env or {})))
        answer = answers[argv[0]]
        if isinstance(answer, Exception):
            raise answer
        return answer.encode()

    monkeypatch.setattr(git_gateway, "_run_quiet", run)
    return calls


@pytest.mark.asyncio
async def test_direct_fill_passes_over_failures_and_carries_the_user_name(monkeypatch):
    calls = _scripted(
        monkeypatch,
        {
            "broken": git_gateway._NoCredential("broken has no login"),
            "names": "username=operator\n",
            "keeps": f"password={SECRET}\n",
        },
    )
    entries = [("credential.helper", [name]) for name in ("broken", "names", "keeps")]
    got = await git_gateway._fill_directly("bitbucket.org", entries, {"HOME": "h"})
    assert got == git_gateway._basic("operator", SECRET)
    assert [argv for argv, _stdin, _env in calls] == [
        ["broken", "get"],
        ["names", "get"],
        ["keeps", "get"],
    ]
    # The next helper hears the user name, never a secret; no argv or env holds it.
    assert calls[2][1] == b"protocol=https\nhost=bitbucket.org\nusername=operator\n\n"
    for argv, stdin, env in calls:
        assert SECRET not in " ".join(argv) and SECRET.encode() not in stdin
        assert not any(SECRET in value for value in env.values())


@pytest.mark.asyncio
async def test_direct_fill_drops_an_expired_password_and_stops_on_quit(monkeypatch):
    past = int(time.time()) - 60
    calls = _scripted(
        monkeypatch,
        {
            "expired": f"username=u\npassword=old\npassword_expiry_utc={past}\n",
            "quits": "quit=1\n",
            "never": f"username=u\npassword={SECRET}\n",
        },
    )
    entries = [("credential.helper", [name]) for name in ("expired", "quits", "never")]
    with pytest.raises(git_gateway._NoCredential, match="nothing stored"):
        await git_gateway._fill_directly("bitbucket.org", entries, {})
    assert [argv[0] for argv, _stdin, _env in calls] == ["expired", "quits"]


@pytest.mark.asyncio
async def test_direct_fill_with_no_applicable_helper_says_what_to_set(monkeypatch):
    _scripted(monkeypatch, {})
    scoped = [("credential.https://dev.azure.com/contoso.helper", ["gcm"])]
    with pytest.raises(git_gateway._NoCredential, match="no trusted") as refused:
        await git_gateway._fill_directly("dev.azure.com", scoped, {})
    assert "credential.https://dev.azure.com.helper" in refused.value.hint


# ── Real git, helpers run directly (every shard; the Windows path) ──────


@pytest.mark.asyncio
async def test_real_git_lends_through_a_directly_run_store_helper(tmp_path, monkeypatch):
    """The Windows path end to end with real git: no shell, no secret anywhere but stdout."""
    from kiro_crew.platform_compat import trusted_git_bin

    git = trusted_git_bin()
    if git is None:
        pytest.skip("trusted Git is required")
    monkeypatch.setattr(git_gateway, "_runs_helpers_directly", lambda: True)
    monkeypatch.setattr(
        "kiro_crew.github_runner.validate_provider_executable",
        lambda path, *, require_protected: path,
    )
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "shell-helper-ran"
    (home / ".gitconfig").write_text(
        # The empty entry resets the helper list, so a system helper (osxkeychain,
        # manager) holding the operator's own login is never consulted.
        f'[credential]\n\thelper =\n\thelper = "!echo ran > {marker.as_posix()}"\n\thelper = store\n',
        encoding="utf-8",
    )
    (home / ".git-credentials").write_bytes(f"https://operator:{SECRET}@bitbucket.org\n".encode())
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    for name, value in git_maintenance_off_entries(os.environ).items():
        monkeypatch.setenv(name, value)
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    original, calls = git_gateway._run_quiet, []

    async def recorded(argv, stdin=b"", env=None, **kwargs):
        calls.append((list(argv), bytes(stdin), dict(env or {})))
        return await original(argv, stdin, env, **kwargs)

    monkeypatch.setattr(git_gateway, "_run_quiet", recorded)
    files = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    got = await git_gateway._from_git_credential("bitbucket.org")
    assert got == git_gateway._basic("operator", SECRET)
    assert not marker.exists(), "a configured shell helper ran"
    # git itself was asked only for config; the helper ran as its own argv, and
    # the reset kept every system helper out: the store is the only one run.
    helper_runs = [" ".join(argv) for argv, _stdin, _env in calls if argv[-1] == "get"]
    assert helper_runs and all("credential-store" in run for run in helper_runs), helper_runs
    assert not any(argv[1:2] == ["credential"] for argv, _stdin, _env in calls)
    for argv, stdin, env in calls:
        assert SECRET not in " ".join(argv) and SECRET.encode() not in stdin
        assert not any(SECRET in value for value in env.values())
    # No file was written or changed by the lookup.
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == files


@_POSIX
@pytest.mark.asyncio
async def test_a_helper_listed_before_the_reset_is_never_consulted(tmp_path, monkeypatch):
    """The fixtures' empty ``helper =`` keeps a system helper and its login out.

    macOS and Git for Windows configure ``osxkeychain`` / ``manager`` system-wide;
    a test fixture that did not reset the list would read the operator's own
    login and print it in a failed assertion. Here an ``osxkeychain`` found in a
    trusted directory holds another login, listed before the reset.
    """
    from kiro_crew import platform_compat
    from kiro_crew.platform_compat import trusted_git_bin

    if trusted_git_bin() is None:
        pytest.skip("trusted Git is required")
    monkeypatch.setattr(git_gateway, "_runs_helpers_directly", lambda: True)
    monkeypatch.setattr("kiro_crew.github_runner.validate_provider_executable", _real_files_only)
    system = tmp_path / "system-bin"
    system.mkdir()
    ran = tmp_path / "system-helper-ran"
    helper = system / "git-credential-osxkeychain"
    helper.write_text(
        f"#!/bin/sh\necho ran > '{ran}'\necho username=someone-else\necho password=not-mine\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    real_dirs = platform_compat.trusted_system_path() or ""
    monkeypatch.setattr(
        "kiro_crew.platform_compat.trusted_system_path",
        lambda: os.pathsep.join(d for d in (str(system), real_dirs) if d),
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[credential]\n\thelper = osxkeychain\n\thelper =\n\thelper = store\n", encoding="utf-8"
    )
    (home / ".git-credentials").write_bytes(f"https://operator:{SECRET}@bitbucket.org\n".encode())
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    for name, value in git_maintenance_off_entries(os.environ).items():
        monkeypatch.setenv(name, value)
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    got = await git_gateway._from_git_credential("bitbucket.org")
    assert got == git_gateway._basic("operator", SECRET)
    assert not ran.exists(), "a helper listed before the reset was consulted"


@_WINDOWS
@pytest.mark.asyncio
async def test_a_windows_gateway_lends_with_real_git(tmp_path, monkeypatch):
    """No seam patched: the real Windows gateway decides, and it lends."""
    assert git_gateway._runs_helpers_directly() is True
    assert git_gateway.lending_unverifiable() is False
    from kiro_crew.platform_compat import trusted_git_bin

    if trusted_git_bin() is None:
        pytest.skip("trusted Git is required")
    monkeypatch.setattr(
        "kiro_crew.github_runner.validate_provider_executable",
        lambda path, *, require_protected: path,
    )
    home = tmp_path / "home"
    home.mkdir()
    # The empty entry resets the helper list past Git for Windows' own `manager`.
    (home / ".gitconfig").write_text(
        "[credential]\n\thelper =\n\thelper = store\n", encoding="utf-8"
    )
    (home / ".git-credentials").write_bytes(f"https://operator:{SECRET}@bitbucket.org\n".encode())
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    assert await git_gateway._from_git_credential("bitbucket.org") == git_gateway._basic(
        "operator", SECRET
    )


# ── Helpers beside git.exe (Git Credential Manager) ─────────────────────


def _real_files_only(path, *, require_protected):
    """Accept an existing file as validated; refuse anything else, as the walk does."""
    if not os.path.isfile(path):
        raise ValueError("not a file")
    return path


def _fake_helper(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"")
    return path


@_WINDOWS
def test_git_credential_manager_beside_git_exe_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.github_runner.validate_provider_executable", _real_files_only)
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_path", lambda: "")
    gcm = _fake_helper(tmp_path / "mingw64" / "bin", "git-credential-manager.exe")
    exec_path = str(tmp_path / "mingw64" / "libexec" / "git-core")
    git = str(gcm.parent / "git.exe")
    assert git_gateway._helper_argv("manager", exec_path, git) == [str(gcm)]
    assert git_gateway._helper_argv("manager", exec_path) is None
    # git's builtin helpers have no dashed executable on Windows: git runs them.
    assert git_gateway._helper_argv("store", exec_path, git) == [git, "credential-store"]
    assert git_gateway._helper_argv("store", exec_path) is None


@_POSIX
def test_off_windows_the_git_directory_is_not_a_helper_source(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.github_runner.validate_provider_executable", _real_files_only)
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_path", lambda: "")
    beside = _fake_helper(tmp_path / "bin", "git-credential-manager")
    exec_path = str(tmp_path / "libexec")
    git = str(beside.parent / "git")
    assert git_gateway._helper_argv("manager", exec_path, git) is None
    assert git_gateway._helper_argv("store", exec_path, git) is None


def test_the_posix_helper_spelling_is_unchanged(tmp_path, monkeypatch):
    """Off the direct path git still gets the same ``!`` text it always did."""
    monkeypatch.setattr("kiro_crew.github_runner.validate_provider_executable", _real_files_only)
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_path", lambda: "")
    suffix = ".exe" if os.name == "nt" else ""
    store = _fake_helper(tmp_path / "with space", f"git-credential-store{suffix}")
    import shlex

    assert git_gateway._helper_value("store", str(store.parent)) == "!" + shlex.quote(str(store))
    assert git_gateway._helper_value("", str(store.parent)) == ""
    assert git_gateway._helper_value("!rm -rf /", str(store.parent)) is None


# ── Fails closed when the account cannot be verified ────────────────────


@_POSIX
def test_off_windows_lending_is_never_unverifiable():
    assert git_gateway.lending_unverifiable() is False


@_WINDOWS
@pytest.mark.parametrize("broken", ["sid", "acl"])
def test_a_windows_gateway_that_cannot_verify_itself_lends_nothing(monkeypatch, broken):
    from kiro_crew import platform_compat, windows_acl

    assert git_gateway.lending_unverifiable() is False
    if broken == "sid":
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: None)
    else:

        def unreadable(_path):
            raise windows_acl.AclUnavailable("no security API")

        monkeypatch.setattr(windows_acl, "describe", unreadable)
    assert git_gateway.lending_unverifiable() is True
    assert git_gateway.gateway_logins_lent() is False


@pytest.mark.asyncio
async def test_every_coroutine_reads_the_windows_walk_off_the_event_loop(monkeypatch):
    """The walk reads a security descriptor and looks up account names, which can
    stall on an unreachable domain controller; no coroutine runs it on the loop.
    Fails if a call site goes back to the inline ``lending_unverifiable()``."""
    import threading
    import types

    loop_thread = threading.get_ident()
    walked_on: list[int] = []

    def walk():
        walked_on.append(threading.get_ident())
        return True

    monkeypatch.setattr(git_gateway, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(git_gateway, "lending_unverifiable", walk)
    assert await git_gateway.git_readiness() == {h: "unsupported" for h in git_gateway.HOSTS}
    with pytest.raises(git_gateway.GitProxyRefusal):
        await git_gateway.host_credential("github.com")
    with pytest.raises(git_gateway._NoCredential):
        await git_gateway._from_git_credential("bitbucket.org")
    assert len(walked_on) == 3 and loop_thread not in walked_on


# ── The relay token file is owner-only ──────────────────────────────────


@_WINDOWS
def test_the_relay_token_file_has_an_owner_only_dacl(tmp_path):
    from kiro_crew import platform_compat, windows_acl

    path = tmp_path / "relay.token"
    relay.write_token_file(path, b"token-bytes")
    assert path.read_bytes() == b"token-bytes"
    me = platform_compat.current_user_sid()
    owner = platform_compat._OWNER_RIGHTS_SID
    sids = (owner,) if me == owner else (owner, me)
    assert windows_acl.owner_only_dacl_matches(path, inherit=False, sids=sids)


@_POSIX
def test_the_relay_token_file_is_0600(tmp_path):
    import stat

    path = tmp_path / "relay.token"
    relay.write_token_file(path, b"token-bytes")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_token_file_that_cannot_be_locked_down_is_never_written(tmp_path, monkeypatch):
    def refuse(_path):
        raise OSError("relay token: the owner-only DACL did not read back")

    monkeypatch.setattr(relay, "_needs_owner_dacl", lambda: True)
    monkeypatch.setattr(relay, "_lock_to_owner", refuse)
    path = tmp_path / "relay.token"
    with pytest.raises(OSError, match="did not read back"):
        relay.write_token_file(path, b"token-bytes")
    assert not path.exists()


@pytest.mark.parametrize("failure", ["no-sid", "apply-fails", "read-back-differs"])
def test_the_owner_lock_refuses_whatever_it_cannot_confirm(tmp_path, monkeypatch, failure):
    from kiro_crew import platform_compat, windows_acl

    applied = []
    monkeypatch.setattr(
        platform_compat, "current_user_sid", lambda: None if failure == "no-sid" else "S-1-5-21-1"
    )

    def apply(path, *, inherit, sids):
        applied.append((inherit, sids))
        if failure == "apply-fails":
            raise windows_acl.AclWriteFailed("denied")

    monkeypatch.setattr(windows_acl, "apply_owner_only", apply)
    monkeypatch.setattr(windows_acl, "owner_only_dacl_matches", lambda *a, **k: False)
    with pytest.raises(OSError):
        relay._lock_to_owner(tmp_path / "relay.token")
    if failure == "no-sid":
        assert applied == []  # refused before any DACL was written
    else:
        assert applied == [(False, (platform_compat._OWNER_RIGHTS_SID, "S-1-5-21-1"))]


# ── A stopped lookup takes its whole process tree with it ───────────────


@pytest.mark.asyncio
async def test_a_stopped_lookup_kills_the_tree_where_the_tree_is_killed(monkeypatch):
    killed = []

    async def tree(pid, *args):
        killed.append(pid)
        return True

    monkeypatch.setattr(git_gateway, "_kills_process_tree", lambda: True)
    monkeypatch.setattr("kiro_crew.platform_compat.kill_process_tree_async", tree)
    monkeypatch.setattr(git_gateway, "_GH_TIMEOUT_SECS", 0.2)
    with pytest.raises(git_gateway._NoCredential, match="timed out"):
        await git_gateway._run_quiet(
            [sys.executable, "-c", "import time; time.sleep(30)"], env=dict(os.environ)
        )
    assert len(killed) == 1


@_WINDOWS
@pytest.mark.asyncio
async def test_on_windows_a_timed_out_lookup_leaves_no_grandchild(tmp_path, monkeypatch):
    """``az`` is ``az.cmd``: the Python under ``cmd.exe`` must go too."""
    from kiro_crew.platform_compat import pid_exists

    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(git_gateway, "_GH_TIMEOUT_SECS", 5.0)
    with pytest.raises(git_gateway._NoCredential, match="timed out"):
        await git_gateway._run_quiet([sys.executable, "-c", script], env=dict(os.environ))
    grandchild = int(pid_file.read_text())
    try:
        await async_wait_until(
            lambda: not pid_exists(grandchild), describe=lambda: f"grandchild {grandchild} alive"
        )
    finally:
        if pid_exists(grandchild):
            with contextlib.suppress(OSError):
                os.kill(grandchild, signal.SIGTERM)
