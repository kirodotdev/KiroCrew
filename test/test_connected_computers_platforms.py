"""Connected computers on macOS and Windows: the plans, the env handover, the stores.

* macOS -- the Seatbelt profile's order and quoting, the environment that
  reaches the child only through :mod:`.confinement_exec`, the Mach witness in
  the self-test judge, and the Keychain store (a mocked ``security``).
* Windows -- no OS sandbox by the owner's decision: the unconfined plan's
  environment, a self-test that claims no fence, the consent describe that says
  so, and the DPAPI store (stand-in seal; the real DPAPI only on Windows).

The real Seatbelt fence is proved by ``test_connected_computers_real_sandbox_macos.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.connected_computers import confinement, confinement_exec, confinement_macos
from kiro_crew.connected_computers import protected_store as ps
from kiro_crew.connected_computers.confinement import (
    ConfinementUnavailable,
    WindowsUnconfinedPlan,
    judge_self_test,
    windows_job_argv,
)
from kiro_crew.connected_computers.protected_store import ProtectionUnavailable
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptors and paths")

KEY = bytes(range(32))


# -- which systems have a plan ----------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "plan", "fenced"),
    [
        ("linux", confinement.PLAN_LINUX, True),
        ("darwin", confinement.PLAN_MACOS, True),
        ("win32", confinement.PLAN_WINDOWS, False),
        ("freebsd14", "", False),
    ],
)
def test_describe_says_whether_the_plan_fences(platform, plan, fenced):
    assert confinement.describe_confinement(platform) == {"plan": plan, "fenced": fenced}
    assert confinement.sharing_supported(platform) is bool(plan)


def test_describe_and_support_default_to_this_system():
    assert confinement.describe_confinement() == confinement.describe_confinement(sys.platform)
    assert confinement.sharing_supported() == confinement.sharing_supported(sys.platform)


def test_darwin_gets_the_seatbelt_plan(monkeypatch):
    monkeypatch.setattr(confinement, "_bin", lambda name: "/usr/bin/sandbox-exec")
    plan = confinement.plan_for_this_machine("darwin")
    assert isinstance(plan, confinement_macos.MacPlan)
    assert plan.sandbox_exec == "/usr/bin/sandbox-exec" and plan.fenced is True


def test_darwin_without_sandbox_exec_is_unavailable(monkeypatch):
    monkeypatch.setattr(confinement, "_bin", lambda name: None)
    with pytest.raises(ConfinementUnavailable, match="sandbox-exec"):
        confinement.plan_for_this_machine("darwin")


def test_windows_gets_the_unconfined_plan_without_any_tool_lookup(monkeypatch):
    def no_lookup(name: str) -> None:
        raise AssertionError("Windows looks up no sandbox tool")

    monkeypatch.setattr(confinement, "_bin", no_lookup)
    plan = confinement.plan_for_this_machine("win32")
    assert isinstance(plan, WindowsUnconfinedPlan)
    assert plan.name == "windows-none" and plan.fenced is False


# -- the environment checks every plan shares ------------------------------------------


@pytest.mark.parametrize("env", [{"": "v"}, {"A=B": "v"}, {"A\0B": "v"}, {"A": "v\0--bind"}])
def test_a_setting_that_cannot_be_handed_over_is_refused(env):
    with pytest.raises(ConfinementUnavailable, match="character"):
        confinement.check_env(env)


def test_withheld_credentials_never_reach_any_child(monkeypatch):
    from kiro_crew.remote_session.worker import WITHHELD_ENV

    name = sorted(WITHHELD_ENV)[0]
    extra = {name: "secret", "KEEP": "yes"}
    assert name not in confinement.child_env("/s", extra=extra)
    assert name not in confinement.windows_child_env("/s", extra=extra)
    assert confinement.windows_child_env("/s", extra=extra)["KEEP"] == "yes"


# -- Windows: no OS sandbox ------------------------------------------------------------


def test_windows_env_is_built_from_nothing_with_homes_in_the_scratch(monkeypatch):
    for name in list(os.environ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("PATH", r"C:\Windows\System32")
    monkeypatch.setenv("KIRO_CREW_SECRET_LIKE", "never")
    env = confinement.windows_child_env(r"C:\scratch")
    assert env["SYSTEMROOT"] == r"C:\Windows" and env["PATH"] == r"C:\Windows\System32"
    for home in ("HOME", "USERPROFILE", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"):
        assert env[home] == r"C:\scratch"
    assert "KIRO_CREW_SECRET_LIKE" not in env
    assert env["USERNAME"]


def _files(monkeypatch, *present: str) -> list[str]:
    """Make exactly *present* exist for :func:`windows_program`; the paths it tried."""
    from kiro_crew.remote_session import workspace_tools as wt

    tried: list[str] = []

    def is_file(path: str) -> bool:
        tried.append(path)
        return path in present

    monkeypatch.setattr(wt, "_is_file", is_file)
    return tried


def test_windows_plan_runs_in_the_root_and_finds_the_program_on_the_childs_path(monkeypatch):
    tried = _files(monkeypatch, r"C:\tools\npx.CMD")
    monkeypatch.setenv("PATH", r".;relative\bin;;C:\tools")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    child = WindowsUnconfinedPlan().wrap(
        ["npx", "server"], root="root-dir", scratch="scratch-dir", reads=["ignored"], env={"K": "v"}
    )
    assert child.argv == windows_job_argv([r"C:\tools\npx.CMD", "server"])
    assert child.cwd == "root-dir" and child.pass_fds == ()
    assert child.env["K"] == "v" and child.env["TEMP"] == "scratch-dir"
    # Only the absolute entry is tried: never ".", an empty or a relative one.
    assert all(path.startswith("C:\\tools\\") for path in tried)


def test_windows_plan_keeps_an_absolute_program_and_refuses_an_unfound_one(monkeypatch):
    _files(monkeypatch)
    # A bare name left as it is would be found in the shared root by CreateProcess.
    with pytest.raises(FileNotFoundError):
        WindowsUnconfinedPlan().wrap(["tool"], root="r", scratch="s")
    absolute = os.path.abspath("prog")
    assert WindowsUnconfinedPlan().wrap([absolute], root="r", scratch="s").argv == (
        windows_job_argv([absolute])
    )


def test_every_windows_child_starts_through_the_kill_on_close_job_launcher():
    from kiro_crew.remote_session import workspace_lifetime

    argv = windows_job_argv([r"C:\tools\server.exe", "--stdio"])
    assert argv[0] == sys.executable and argv[1] == "-I"
    assert os.path.basename(argv[2]) == "workspace_lifetime.py"
    assert argv[3:] == [
        workspace_lifetime.RUN_IN_JOB_FLAG,
        "--",
        r"C:\tools\server.exe",
        "--stdio",
    ]


def test_the_job_launcher_runs_nothing_without_its_job(monkeypatch):
    from kiro_crew.remote_session import workspace_lifetime as wl

    ran: list[Any] = []
    monkeypatch.setattr(wl, "end_children_on_exit", lambda: False)
    monkeypatch.setattr(wl.subprocess, "call", lambda *a, **k: ran.append(a) or 0)
    assert wl.run_in_job(["server.exe"]) == wl.JOB_UNAVAILABLE_EXIT
    assert ran == []


def test_the_job_launcher_runs_the_program_once_its_job_holds(monkeypatch):
    from kiro_crew.remote_session import workspace_lifetime as wl

    ran: list[Any] = []
    monkeypatch.setattr(wl, "end_children_on_exit", lambda: True)
    monkeypatch.setattr(wl.subprocess, "call", lambda argv, **k: ran.append(argv) or 7)
    assert wl.run_in_job(["server.exe", "a"]) == 7
    assert ran == [["server.exe", "a"]]


def test_a_windows_program_is_found_on_its_real_entry_never_in_the_current_directory(
    monkeypatch,
):
    from kiro_crew.remote_session import workspace_tools as wt

    # A git.exe in the shared root (the current directory) and the real one.
    tried = _files(monkeypatch, "git.EXE", r".\git.EXE", r"C:\Git\cmd\git.EXE")
    assert wt.windows_program("git", r".;C:\first;C:\Git\cmd") == r"C:\Git\cmd\git.EXE"
    assert all(path[1:3] == ":\\" for path in tried)
    # PATHEXT order decides within one entry, and its default applies when unset.
    assert tried[:4] == [rf"C:\first\git{ext}" for ext in (".COM", ".EXE", ".BAT", ".CMD")]


def test_a_windows_program_follows_the_given_pathext_and_tries_a_named_extension_first(
    monkeypatch,
):
    from kiro_crew.remote_session import workspace_tools as wt

    tried = _files(monkeypatch, r"C:\t\npx.CMD")
    assert wt.windows_program("npx", r"C:\t", ".EXE;.CMD") == r"C:\t\npx.CMD"
    assert tried == [r"C:\t\npx.EXE", r"C:\t\npx.CMD"]
    tried = _files(monkeypatch, r"C:\t\npx.cmd")
    assert wt.windows_program("npx.cmd", r"C:\t", ".EXE;.CMD") == r"C:\t\npx.cmd"
    assert tried[0] == r"C:\t\npx.cmd"
    found = _files(monkeypatch, r"\\srv\share\bin\tool.EXE")
    assert wt.windows_program("tool", r"\\srv\share\bin") == r"\\srv\share\bin\tool.EXE"
    assert found


@pytest.mark.parametrize(
    "name, path",
    [
        ("git", r".;bin;;\rooted"),  # no entry with a drive or a share
        ("git", ""),
        ("git", r"\\?\C:\Git\cmd"),  # a device path names no ordinary folder
        (r".\git", r"C:\Git\cmd"),  # a name with a separator or a drive
        ("sub/git", r"C:\Git\cmd"),
        ("C:git", r"C:\Git\cmd"),
        ("", r"C:\Git\cmd"),
    ],
)
def test_a_windows_program_has_no_answer_from_a_relative_place(monkeypatch, name, path):
    from kiro_crew.remote_session import workspace_tools as wt

    monkeypatch.setattr(wt, "_is_file", lambda path: True)
    assert wt.windows_program(name, path) is None


def test_windows_git_views_run_git_by_its_path_on_path_or_not_at_all(tmp_path, monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    ran: list[list[str]] = []

    def run_grouped(argv: list[str], **_kw: Any) -> Any:
        ran.append(list(argv))
        raise FileNotFoundError("stop after the first run")

    class _WindowsSys:
        platform = "win32"

        def __getattr__(self, name: str) -> Any:
            return getattr(sys, name)

    # Only this module reads Windows: the process's own sys is untouched.
    monkeypatch.setattr(wt, "sys", _WindowsSys())
    monkeypatch.setattr(wt, "_run_grouped", run_grouped)
    monkeypatch.setenv("PATH", r".;C:\Git\cmd")
    ws = wt.Workspace(str(tmp_path))
    monkeypatch.setattr(wt, "windows_program", lambda name, path, pathext="": None)
    reply = wt._git_read(ws, "git_status", ".")
    assert reply["git"] is False and reply["error_text"] == "git is not installed"
    assert ran == []
    monkeypatch.setattr(
        wt,
        "windows_program",
        lambda name, path, pathext="": r"C:\Git\cmd\git.exe" if name == "git" else None,
    )
    wt._git_read(ws, "git_status", ".")
    assert ran and ran[0][0] == r"C:\Git\cmd\git.exe"


def test_windows_plan_refuses_a_setting_it_cannot_pass():
    with pytest.raises(ConfinementUnavailable):
        WindowsUnconfinedPlan().wrap(["tool"], root="r", scratch="s", env={"A": "x\0y"})


@pytest.mark.asyncio
async def test_the_self_test_claims_no_fence_for_an_unfenced_plan(monkeypatch, caplog):
    def never(*_a: Any, **_k: Any) -> None:
        raise AssertionError("an unfenced plan prepares no witnesses")

    monkeypatch.setattr(confinement, "_prepare", never)
    with caplog.at_level("WARNING", logger=confinement.__name__):
        await confinement.self_test(WindowsUnconfinedPlan(), "/root")
    assert "no OS sandbox" in caplog.text


@pytest.mark.asyncio
async def test_a_plan_that_does_not_say_is_still_probed(monkeypatch):
    class _Unsaid:
        name = "unsaid"

    def refuse(*_a: Any, **_k: Any) -> None:
        raise ConfinementUnavailable("probed")

    monkeypatch.setattr(confinement, "_prepare", refuse)
    with pytest.raises(ConfinementUnavailable, match="probed"):
        await confinement.self_test(_Unsaid(), "/root")


# -- the self-test judge's Mach witness (macOS) ----------------------------------------


def _answers(confined: bool, *, mach: bool) -> dict[str, str]:
    deny = "denied" if confined else None
    out = {
        "root_read": "R",
        "root_write": "allowed",
        "out_read": deny or "O",
        "out_write": deny or "allowed",
        "out_list": deny or "allowed",
        "link_read": "denied",
        "ipc": deny or "allowed",
        "net": "allowed",
        "scratch_write": "allowed",
        "keyring": "skip",
    }
    if mach:
        out["mach"] = "denied" if confined else "allowed"
    return out


_TOKENS = {"root": "R", "out": "O"}


def test_a_fenced_mac_result_passes():
    judge_self_test(
        _answers(False, mach=True), _answers(True, mach=True), tokens=_TOKENS, mach=True
    )


def test_a_mac_probe_that_reached_the_mach_witness_is_refused():
    leaked = _answers(True, mach=True)
    leaked["mach"] = "allowed"
    with pytest.raises(ConfinementUnavailable, match="mach"):
        judge_self_test(_answers(False, mach=True), leaked, tokens=_TOKENS, mach=True)


def test_a_mac_run_without_the_mach_answer_is_refused():
    with pytest.raises(ConfinementUnavailable, match="mach"):
        judge_self_test(
            _answers(False, mach=True), _answers(True, mach=False), tokens=_TOKENS, mach=True
        )


def test_a_mach_control_that_could_not_reach_proves_nothing():
    control = _answers(False, mach=True)
    control["mach"] = "error"
    with pytest.raises(ConfinementUnavailable, match="prepare.*mach"):
        judge_self_test(control, _answers(True, mach=True), tokens=_TOKENS, mach=True)


# -- macOS: the Seatbelt profile -------------------------------------------------------


def _profile(root: str = "/Users/u/proj", scratch: str = "/private/var/folders/s") -> str:
    return confinement_macos.macos_profile(
        root=root, scratch=scratch, reads=["/Users/u/proj/tools"]
    )


def test_the_profile_denies_by_default_and_allows_the_root_and_scratch():
    lines = _profile().splitlines()
    assert lines[:2] == ["(version 1)", "(deny default)"]
    assert (
        '(allow file-read* file-write* (subpath "/Users/u/proj") (subpath "/private/var/folders/s"))'
        in lines
    )


def test_read_only_paths_are_denied_writes_after_the_root_is_allowed_them():
    lines = _profile().splitlines()
    allowed = next(
        i for i, ln in enumerate(lines) if ln.startswith("(allow file-read* file-write*")
    )
    denied = next(i for i, ln in enumerate(lines) if ln.startswith("(deny file-write*"))
    # Seatbelt is last-match-wins: the deny must come later to win inside the root.
    assert denied > allowed
    assert '(subpath "/Users/u/proj/tools")' in lines[denied]
    for runtime in confinement.runtime_reads():
        assert f'(subpath "{runtime}")' in lines[denied]


def _readable(profile: str, path: str) -> bool:
    """Whether *profile* lets a child read *path*: Seatbelt's last matching rule wins."""
    import re

    verdict = False
    for line in profile.splitlines():
        if line.startswith("(allow file-read*"):
            allow = True
        elif line.startswith("(deny file-read*"):
            allow = False
        else:
            continue
        for kind, target in re.findall(r'\((subpath|literal) "([^"]*)"\)', line):
            hit = path == target or (kind == "subpath" and path.startswith(target + "/"))
            if hit:
                verdict = allow
    return verdict


@pytest.mark.parametrize("prefix", ["/opt/homebrew", "/usr/local"])
@pytest.mark.parametrize(
    "inside, readable",
    [
        ("bin/git", True),
        ("lib/libssl.dylib", True),
        ("Cellar/git/2.47/bin/git", True),
        ("opt/python@3.12/bin/python3", True),
        ("share/git-core/templates/x", True),
        ("Frameworks/Python.framework/x", True),
        # A service's data and configs belong to the user, not the tools.
        ("var/postgresql@16/base/1", False),
        ("var/mysql/ibdata1", False),
        ("var/db/redis/dump.rdb", False),
        ("etc/redis.conf", False),
        ("etc/postgresql/pg_hba.conf", False),
        ("etc/ca-certificates/cert.pem", True),
        ("etc/openssl@3/cert.pem", True),
        ("etc/openssl@3/private/key.pem", False),
        ("etc/gitconfig", True),  # Homebrew git stops on EPERM reading it
        ("etc/gitattributes", True),
        ("Caskroom/x/app", False),
    ],
)
def test_a_package_prefix_shares_its_toolchain_never_its_data(prefix, inside, readable):
    assert _readable(_profile(), f"{prefix}/{inside}") is readable


def test_a_python_install_at_a_prefix_cannot_reopen_its_data(monkeypatch):
    monkeypatch.setattr(confinement_macos, "runtime_reads", lambda: ["/usr/local"])
    profile = _profile()
    assert _readable(profile, "/usr/local/lib/python3.12/os.py")
    assert not _readable(profile, "/usr/local/var/mysql/ibdata1")
    assert not _readable(profile, "/usr/local/etc/my.cnf")


def test_the_system_reads_stay_and_a_shared_root_under_a_prefix_is_still_shared():
    profile = _profile()
    for path in ("/usr/bin/git", "/usr/lib/libSystem.B.dylib", "/System/Library/x", "/bin/sh"):
        assert _readable(profile, path)
    assert not _readable(profile, "/Users/u/.ssh/id_ed25519")
    # The person chose this folder: the root's allow comes after the data deny.
    assert _readable(_profile(root="/opt/homebrew/var/www"), "/opt/homebrew/var/www/index.html")


def test_only_the_listed_mach_services_and_owned_sockets_are_reachable():
    text = _profile()
    for name in confinement_macos.MACOS_MACH_SERVICES:
        assert f'(global-name "{name}")' in text
    assert confinement.MACOS_MACH_WITNESS not in text
    assert '(allow network-outbound (literal "/private/var/run/mDNSResponder"))' in text
    assert '(allow network-outbound (remote ip "*:*"))' in text


@pytest.mark.parametrize(
    "device, readable",
    [
        ("/dev/null", True),
        ("/dev/urandom", True),
        ("/dev/tty", True),
        ("/dev/fd/0", True),
        ("/dev/ttys000", False),  # the user's other Terminal window
        ("/dev/disk0", False),
        ("/dev/console", False),
    ],
)
def test_only_the_devices_a_child_needs_are_readable(device, readable):
    assert _readable(_profile(), device) is readable


def test_the_root_parents_are_stat_only():
    [line] = [ln for ln in _profile().splitlines() if ln.startswith("(allow file-read-metadata")]
    assert '(literal "/Users/u")' in line and '(literal "/")' in line
    assert "subpath" not in line


def test_quotes_and_backslashes_in_a_folder_name_stay_one_literal():
    text = _profile(root='/Users/u/a"b\\c')
    assert '(subpath "/Users/u/a\\"b\\\\c")' in text


@pytest.mark.parametrize("bad", ["/Users/u/a\nb", "/Users/u/a\rb", "/Users/u/a\0b"])
def test_a_folder_name_with_a_line_break_or_nul_is_refused(bad):
    with pytest.raises(ConfinementUnavailable, match="macOS"):
        _profile(root=bad)


@posix_only
def test_mac_wrap_hands_the_env_to_the_launcher_on_an_unlinked_fd(tmp_path):
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    plan = confinement_macos.MacPlan("/usr/bin/sandbox-exec")
    child = plan.wrap(
        ["/usr/bin/tool", "--x"],
        root=str(root),
        scratch=str(scratch),
        env={"K": "v", "DYLD_INSERT_LIBRARIES": "/r/x.dylib"},
    )
    try:
        [fd] = child.pass_fds
        argv = child.argv
        assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
        assert argv[3:] == [
            sys.executable,
            "-I",
            "-S",
            confinement_macos.LAUNCHER_PATH,
            str(fd),
            "--",
            "/usr/bin/tool",
            "--x",
        ]
        assert confinement_macos.LAUNCHER_PATH in argv[2], "the launcher is readable inside"
        # No value in an argv, none in the wrapper's own environment.
        assert not any("/r/x.dylib" in a or a == "v" for a in argv)
        assert "K" not in child.env and "DYLD_INSERT_LIBRARIES" not in child.env
        assert child.env["HOME"] == os.path.realpath(scratch)
        assert child.cwd == os.path.realpath(root)
        assert os.listdir(scratch) == [], "the env file is unlinked at once"
        env = confinement_exec.read_env(os.dup(fd))
        assert env["K"] == "v" and env["DYLD_INSERT_LIBRARIES"] == "/r/x.dylib"
        assert env["TMPDIR"] == os.path.realpath(scratch)
        assert env["PATH"] == confinement_macos.MACOS_PATH
    finally:
        child.close()
    with pytest.raises(OSError):
        os.fstat(fd)


@posix_only
def test_mac_wrap_refuses_a_setting_it_cannot_pass(tmp_path):
    plan = confinement_macos.MacPlan("/usr/bin/sandbox-exec")
    with pytest.raises(ConfinementUnavailable):
        plan.wrap(["/bin/true"], root=str(tmp_path), scratch=str(tmp_path), env={"A=": "v"})
    assert os.listdir(tmp_path) == []


# -- macOS: the in-fence launcher ------------------------------------------------------


def _fd_with(data: bytes) -> int:
    rfd, wfd = os.pipe()
    os.write(wfd, data)
    os.close(wfd)
    return rfd


def test_the_launcher_reads_the_env_and_becomes_the_child():
    ran: list[Any] = []
    fd = _fd_with(b"A\0one\0B\0\0")
    code = confinement_exec.main(
        ["exec", str(fd), "--", "prog", "arg"], execvpe=lambda *a: ran.append(a)
    )
    assert code == 0
    assert ran == [("prog", ["prog", "arg"], {"A": "one", "B": ""})]


@pytest.mark.parametrize("data", [b"A\0one", b"A\0", b"\0v\0", b"A=B\0v\0"])
def test_the_launcher_refuses_a_malformed_env(data, capsys):
    fd = _fd_with(data)
    code = confinement_exec.main(
        ["exec", str(fd), "--", "prog"], execvpe=lambda *a: pytest.fail("ran")
    )
    assert code == 2
    assert "could not be read" in capsys.readouterr().err


def test_the_launcher_refuses_an_oversized_env(monkeypatch):
    monkeypatch.setattr(confinement_exec, "MAX_ENV_BYTES", 4)
    with pytest.raises(confinement_exec.EnvRefused, match="too large"):
        confinement_exec.read_env(_fd_with(b"AAAA\0vvvv\0"))


@pytest.mark.parametrize("argv", [["exec"], ["exec", "3", "x", "prog"], ["exec", "3", "--"]])
def test_the_launcher_refuses_a_bad_command_line(argv, capsys):
    assert confinement_exec.main(argv, execvpe=lambda *a: pytest.fail("ran")) == 2
    assert "usage" in capsys.readouterr().err


def test_the_launcher_reports_a_program_that_cannot_start(capsys):
    def missing(*_a: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    fd = _fd_with(b"")
    assert confinement_exec.main(["exec", str(fd), "--", "nope"], execvpe=missing) == 127
    assert "could not start nope" in capsys.readouterr().err


# -- macOS: the Keychain store ---------------------------------------------------------


class _Runner:
    def __init__(self, *answers: tuple[int, bytes, bytes]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv: list[str], stdin: bytes = b"") -> tuple[int, bytes, bytes]:
        self.calls.append((list(argv), stdin))
        return self.answers.pop(0)


def test_keychain_load_reads_the_item_for_this_account():
    runner = _Runner((0, KEY.hex().encode() + b"\n", b""))
    assert ps.MacKeychainStore("acct", runner=runner).load() == KEY
    [(argv, _)] = runner.calls
    assert argv == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        ps.SERVICE,
        "-a",
        "acct",
        "-w",
    ]


def test_keychain_absent_item_is_none_and_anything_else_unavailable():
    assert ps.MacKeychainStore("a", runner=_Runner((44, b"", b""))).load() is None
    with pytest.raises(ProtectionUnavailable, match="locked"):
        ps.MacKeychainStore("a", runner=_Runner((51, b"", b"denied"))).load()
    with pytest.raises(ProtectionUnavailable, match="unreadable"):
        ps.MacKeychainStore("a", runner=_Runner((0, b"zz", b""))).load()


def test_keychain_save_sends_the_key_on_stdin_and_reads_it_back():
    runner = _Runner((0, b"", b""), (0, KEY.hex().encode(), b""))
    ps.MacKeychainStore("acct", runner=runner).save(KEY)
    (argv, stdin), (back, _) = runner.calls
    assert argv == ["/usr/bin/security", "-i"]
    assert KEY.hex() not in " ".join(argv)
    assert stdin.decode("ascii").startswith("add-generic-password -U -s ")
    assert KEY.hex() in stdin.decode("ascii") and stdin.endswith(b"\n")
    assert back[1] == "find-generic-password"


@pytest.mark.parametrize(
    "answers",
    [
        [(1, b"", b"")],
        [(0, b"", b""), (44, b"", b"")],
        [(0, b"", b""), (0, (b"00" * 32), b"")],
        [(0, b"", b""), (51, b"", b"")],
    ],
)
def test_keychain_save_that_does_not_read_back_is_unavailable(answers):
    with pytest.raises(ProtectionUnavailable, match="Keychain"):
        ps.MacKeychainStore("acct", runner=_Runner(*answers)).save(KEY)


def test_keychain_needs_the_system_security_tool(monkeypatch):
    monkeypatch.setattr(ps.os.path, "isfile", lambda p: False)
    with pytest.raises(ProtectionUnavailable, match="security tool"):
        ps.MacKeychainStore.for_account("acct")
    monkeypatch.setattr(ps.os.path, "isfile", lambda p: p == "/usr/bin/security")
    assert isinstance(ps.MacKeychainStore.for_account("acct"), ps.MacKeychainStore)


# -- Windows: the DPAPI store ----------------------------------------------------------


def _seal(data: bytes, entropy: bytes) -> bytes:
    return b"S" + entropy + b"|" + data[::-1]


def _unseal(blob: bytes, entropy: bytes) -> bytes:
    head = b"S" + entropy + b"|"
    if not blob.startswith(head):
        raise ProtectionUnavailable("Windows could not open the key for this computer")
    return blob[len(head) :][::-1]


def _dpapi_store(path: Path, account: str = "acct") -> ps.WindowsDpapiStore:
    return ps.WindowsDpapiStore(path, account, seal=_seal, unseal=_unseal)


def test_dpapi_store_round_trips_and_keeps_only_the_sealed_key(tmp_path):
    path = tmp_path / ps.DPAPI_KEY_FILE
    store = _dpapi_store(path)
    assert store.load() is None
    store.save(KEY)
    assert store.load() == KEY
    assert KEY not in path.read_bytes()
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o077 == 0, "owner-only from birth"


def test_dpapi_key_of_another_data_home_does_not_open(tmp_path):
    path = tmp_path / ps.DPAPI_KEY_FILE
    _dpapi_store(path, "home-a").save(KEY)
    with pytest.raises(ProtectionUnavailable):
        _dpapi_store(path, "home-b").load()


@pytest.mark.parametrize("blob", [b"", b"x" * (16384 + 1)])
def test_dpapi_empty_or_oversized_file_is_unreadable(tmp_path, blob):
    path = tmp_path / ps.DPAPI_KEY_FILE
    path.write_bytes(blob)
    with pytest.raises(ProtectionUnavailable, match="unreadable"):
        _dpapi_store(path).load()


def test_dpapi_key_of_the_wrong_length_is_unreadable(tmp_path):
    path = tmp_path / ps.DPAPI_KEY_FILE
    path.write_bytes(_seal(b"short", b"kiro-crew-connected-computers:acct"))
    with pytest.raises(ProtectionUnavailable, match="unreadable"):
        _dpapi_store(path).load()


def test_dpapi_unreadable_file_is_unavailable(tmp_path):
    (tmp_path / "dir").mkdir()
    with pytest.raises(ProtectionUnavailable):
        _dpapi_store(tmp_path / "dir").load()


def test_dpapi_save_that_does_not_read_back_is_unavailable(tmp_path):
    store = ps.WindowsDpapiStore(
        tmp_path / ps.DPAPI_KEY_FILE, "acct", seal=_seal, unseal=lambda b, e: bytes(32)
    )
    with pytest.raises(ProtectionUnavailable, match="did not keep"):
        store.save(KEY)


def test_dpapi_save_that_cannot_write_is_unavailable(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_bytes(b"")
    with pytest.raises(ProtectionUnavailable, match="did not store"):
        _dpapi_store(blocker / ps.DPAPI_KEY_FILE).save(KEY)


def test_dpapi_save_refuses_a_key_of_the_wrong_size(tmp_path):
    with pytest.raises(ValueError):
        _dpapi_store(tmp_path / ps.DPAPI_KEY_FILE).save(b"short")


@pytest.mark.skipif(sys.platform == "win32", reason="checks the off-Windows refusal")
def test_dpapi_itself_is_unavailable_off_windows():
    with pytest.raises(ProtectionUnavailable, match="Windows data protection"):
        ps._dpapi(True, KEY, b"e")


@pytest.mark.skipif(sys.platform != "win32", reason="the real DPAPI is Windows only")
def test_real_dpapi_round_trips_for_this_user(tmp_path):
    store = ps.WindowsDpapiStore(tmp_path / ps.DPAPI_KEY_FILE, "acct")
    store.save(KEY)
    assert store.load() == KEY
    with pytest.raises(ProtectionUnavailable):
        ps._dpapi(False, (tmp_path / ps.DPAPI_KEY_FILE).read_bytes(), b"other-entropy")


@pytest.mark.parametrize(
    ("platform", "kind"), [("darwin", ps.MacKeychainStore), ("win32", ps.WindowsDpapiStore)]
)
def test_each_system_gets_its_own_store(monkeypatch, tmp_path, platform, kind):
    monkeypatch.setattr(ps.sys, "platform", platform)
    monkeypatch.setattr(ps.os.path, "isfile", lambda p: True)
    store = ps.protected_store_for(tmp_path)
    assert isinstance(store, kind)


# -- the desktop host's stdin on Windows -----------------------------------------------


@pytest.mark.asyncio
async def test_the_threaded_stdin_feeds_lines_then_end_of_file():
    from kiro_crew.connected_computers import desktop_host

    reader = asyncio.StreamReader()
    rfd, wfd = os.pipe()
    os.write(wfd, b'{"op": "hello"}\n')
    os.close(wfd)
    thread = desktop_host._feed_from_thread(rfd, reader)
    try:
        line = await asyncio.wait_for(reader.readline(), FALLBACK_TIMEOUT_SECS)
        assert line == b'{"op": "hello"}\n'
        assert await asyncio.wait_for(reader.read(), FALLBACK_TIMEOUT_SECS) == b""
    finally:
        thread.join(FALLBACK_TIMEOUT_SECS)
        os.close(rfd)


# -- the workspace tool server's cmd.exe shell -----------------------------------------


def test_run_command_with_the_cmd_shell_runs_one_cmd_line(tmp_path, monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        stdout = io.BytesIO(b"hello\r\n")

        def __init__(self, argv: Any, **kwargs: Any) -> None:
            seen["argv"] = argv
            seen["cwd"] = kwargs["cwd"]

        def wait(self, timeout: Any = None) -> int:
            return 0

    monkeypatch.setattr(wt, "_windows_system_dir", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(wt.subprocess, "Popen", _Proc)
    ws = wt.Workspace(str(tmp_path), shell="cmd")
    out = ws.run_command("echo hello & dir")
    assert seen["argv"] == r'"C:\Windows\System32' + os.sep + 'cmd.exe" /d /s /c "echo hello & dir"'
    assert seen["cwd"] == os.path.realpath(tmp_path)
    assert "hello" in out and out.endswith("[exit 0]")


@pytest.mark.parametrize("command", ["echo one\necho two", "echo one\r\necho two", "echo a\r"])
def test_run_command_with_the_cmd_shell_refuses_more_than_one_line(tmp_path, monkeypatch, command):
    from kiro_crew.remote_session import workspace_tools as wt

    def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a multi-line command must not start")

    monkeypatch.setattr(wt.subprocess, "Popen", _never)
    ws = wt.Workspace(str(tmp_path), shell="cmd")
    with pytest.raises(wt.ToolError, match="&&"):
        ws.run_command(command)


def test_only_the_cmd_shell_tells_the_model_to_keep_one_line():
    from kiro_crew.remote_session import workspace_tools as wt

    def described(shell: str) -> str:
        [spec] = [s for s in wt.tool_specs("/w", shell) if s["name"] == "run_command"]
        return str(spec["description"])

    assert "&&" in described("cmd") and "cmd.exe" in described("cmd")
    assert "cmd.exe" not in described("sh")


def test_the_tool_server_takes_the_shell_choice(monkeypatch, tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    made: list[str] = []

    class _Server:
        def __init__(self, ws: Any) -> None:
            made.append(ws.shell)

    monkeypatch.setattr(wt, "Server", _Server)
    monkeypatch.setattr(wt, "serve", lambda server: None)
    assert wt.main(["--root", str(tmp_path), "--shell", "cmd"]) == 0
    assert made == ["cmd"]


# -- the probe's macOS witnesses, as far as Linux can answer them -----------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux control answers")
def test_the_probe_reaches_a_socket_file_and_answers_the_mach_witness(tmp_path, monkeypatch):
    import socket

    from kiro_crew.connected_computers import confinement_probe as probe

    base = Path(os.path.realpath(tmp_path))
    # The scratch witness is written under TMPDIR: keep it inside this test's folder.
    monkeypatch.setenv("TMPDIR", str(base))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(str(base / "s"))
        listener.listen(1)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        (base / "w").write_text("token", encoding="ascii")
        spec = {
            "root_file": str(base / "w"),
            "root_new": str(base / "n"),
            "out_file": str(base / "w"),
            "out_new": str(base / "o"),
            "out_dir": str(base),
            "out_name": "w",
            "link": str(base / "absent-link"),
            "scratch_name": "scratch-file",
            "tcp_port": tcp.getsockname()[1],
            "ipc_abstract": "",
            "ipc_path": str(base / "s"),
            "mach": confinement.MACOS_MACH_WITNESS,
            "keyctl": 0,
        }
        answers = probe.run(spec)
    finally:
        listener.close()
        tcp.close()
    assert answers["ipc"] == "allowed"
    # No libSystem off macOS: the witness cannot answer, so it cannot prove a fence.
    assert answers["mach"] == "error"
    assert answers["root_read"] == "token" and answers["keyring"] == "skip"


# -- the tool server's sibling module, loaded from its own folder ----------------------


@pytest.mark.xdist_group(name="subprocess_spawn")
def test_the_tool_server_loads_its_git_bodies_beside_itself_under_isolation(tmp_path):
    import shutil
    import subprocess

    from kiro_crew.remote_session import workspace_git_bodies, workspace_lifetime, workspace_tools

    # A copy outside the package, as a guest or a connected computer runs it.
    for module in (workspace_tools, workspace_git_bodies, workspace_lifetime):
        shutil.copy(module.__file__, tmp_path)
    probe = (
        "import runpy, sys; sys.argv = ['workspace_tools.py', '--help'];"
        f" runpy.run_path({str(tmp_path / 'workspace_tools.py')!r}, run_name='loaded')"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe + "; import workspace_git_bodies as m, workspace_lifetime as l;"
            f" assert m.__file__ == {str(tmp_path / 'workspace_git_bodies.py')!r};"
            f" assert l.__file__ == {str(tmp_path / 'workspace_lifetime.py')!r}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_re_exported_git_helpers_are_the_siblings_own():
    from kiro_crew.remote_session import workspace_git_bodies, workspace_tools

    for name in ("_git_bodies", "_git_auto_text", "_worktree_mode", "_git_bool"):
        assert getattr(workspace_tools, name) is getattr(workspace_git_bodies, name)


# -- the reported root: POSIX or Windows, absolute and normalized ----------------------


@pytest.mark.parametrize(
    "root",
    [
        "/work",
        "/home/me/project",
        "C:\\Projects\\app",
        "d:\\a b\\c",
        "\\\\server\\share\\app",
        "\\\\server\\share\\a\\b",
    ],
)
def test_a_native_absolute_root_is_valid_on_every_system(root):
    from kiro_crew.connected_computers import protocol, ws_route

    assert protocol.valid_root(root) is True
    assert ws_route.valid_root(root) is True


@pytest.mark.parametrize(
    "root",
    [
        "",
        "/",
        "work",
        "/a/",
        "/a/../b",
        "/a\x00b",
        "/a\x1bb",
        7,
        None,
        "C:",
        "C:\\",
        "C:Projects",
        "C:/Projects/app",
        "C:\\Projects\\",
        "C:\\Projects\\..\\app",
        "C:\\Projects\\.\\app",
        "C:\\a\\\\b",
        "C:\\a\x00b",
        "C:\\a\nb",
        "C:\\a\\b:stream",
        "\\Projects\\app",
        "\\\\server\\share",
        "\\\\server\\share\\",
        "\\\\server",
        "\\\\?\\C:\\Projects",
        "\\\\.\\C:\\Projects",
        "1:\\Projects",
        "é:\\Projects",
    ],
)
def test_anything_else_is_refused_on_every_system(root):
    from kiro_crew.connected_computers import protocol

    assert protocol.valid_root(root) is False


# -- teardown ends what a command left running (macOS and Windows) --------------------


def test_cmd_output_in_the_console_code_page_is_read_in_that_page(monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    monkeypatch.setattr(wt, "_windows_oem_codec", lambda: "cp850")
    assert wt._command_text("café".encode("cp850"), windows_cmd=True) == "café"
    # A tool that writes UTF-8 under cmd is still read as UTF-8.
    assert wt._command_text("café".encode("utf-8"), windows_cmd=True) == "café"


def test_cmd_output_with_no_known_code_page_falls_back_to_utf8(monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    monkeypatch.setattr(wt, "_windows_oem_codec", lambda: None)
    assert wt._command_text(b"caf\x82", windows_cmd=True) == "caf\ufffd"


def test_posix_command_output_stays_utf8():
    from kiro_crew.remote_session import workspace_tools as wt

    assert wt._command_text("café".encode("cp850"), windows_cmd=False) == "caf\ufffd"


def test_a_stop_while_a_command_starts_is_raised_only_once_it_is_tracked():
    from kiro_crew.remote_session.workspace_lifetime import CommandGroups

    groups = CommandGroups()
    proc = SimpleNamespace(pid=UNALLOCATABLE_PID, returncode=None, poll=lambda: None)
    with pytest.raises(SystemExit):
        with groups.spawning():
            assert groups.defer_retire() is True  # the SIGTERM handler's call
            groups.add(proc)
    assert list(groups._procs) == [UNALLOCATABLE_PID], "end_all now sees the started command"
    assert groups.defer_retire() is False, "outside a start a stop is raised at once"


def test_ending_every_group_waits_for_a_starting_command_and_then_starts_none(monkeypatch):
    import threading

    from kiro_crew.remote_session import workspace_lifetime as wl

    ended: list[list[Any]] = []
    monkeypatch.setattr(wl, "end_groups", lambda procs, grace=0: ended.append(list(procs)))
    monkeypatch.setattr(wl, "_signal_group", lambda pgid, sig: True)
    groups = wl.CommandGroups()
    proc = SimpleNamespace(pid=UNALLOCATABLE_PID, returncode=None, poll=lambda: None)
    inside, release = threading.Event(), threading.Event()

    def start() -> None:
        with groups.spawning():
            inside.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)
            groups.add(proc)

    starter = threading.Thread(target=start)
    starter.start()
    assert inside.wait(FALLBACK_TIMEOUT_SECS)
    stopper = threading.Thread(target=groups.end_all)  # the parent watcher's stop
    stopper.start()
    # The start holds the gate, so the stop cannot have ended anything yet.
    assert groups._start_lock.locked() and ended == []
    release.set()
    starter.join(FALLBACK_TIMEOUT_SECS)
    stopper.join(FALLBACK_TIMEOUT_SECS)
    assert ended == [[proc]], "the command that was starting is ended too"
    with pytest.raises(SystemExit):
        with groups.spawning():
            raise AssertionError("no command starts after every group was ended")


def test_a_second_stop_waits_for_the_first_to_finish_ending_its_groups(monkeypatch):
    import threading

    from kiro_crew.remote_session import workspace_lifetime as wl

    inside, release = threading.Event(), threading.Event()
    calls: list[list[Any]] = []

    def slow_end(procs: list[Any], grace: float = 0) -> None:
        calls.append(list(procs))
        if procs:
            inside.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)

    monkeypatch.setattr(wl, "end_groups", slow_end)
    monkeypatch.setattr(wl, "_signal_group", lambda pgid, sig: True)
    groups = wl.CommandGroups()
    groups.add(SimpleNamespace(pid=UNALLOCATABLE_PID, returncode=None, poll=lambda: None))
    first = threading.Thread(target=groups.end_all)  # the parent watcher's stop
    first.start()
    assert inside.wait(FALLBACK_TIMEOUT_SECS)
    second = threading.Thread(target=groups.end_all)  # the main thread's stop
    second.start()
    # The first stop holds the gate, so the second has not ended anything yet.
    assert groups._end_lock.locked() and len(calls) == 1
    release.set()
    first.join(FALLBACK_TIMEOUT_SECS)
    second.join(FALLBACK_TIMEOUT_SECS)
    assert not second.is_alive() and calls[1] == []


def test_no_stop_while_a_command_starts_raises_nothing():
    from kiro_crew.remote_session.workspace_lifetime import CommandGroups

    groups = CommandGroups()
    with groups.spawning():
        pass
    assert groups.defer_retire() is False


def test_a_server_that_keeps_its_commands_tracks_each_command_s_own_group(tmp_path, monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        pid = 4242
        stdout = io.BytesIO(b"")

        def __init__(self, argv: Any, **kwargs: Any) -> None:
            seen["argv"] = argv
            seen["new_session"] = kwargs["start_new_session"]

        def wait(self, timeout: Any = None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(wt.subprocess, "Popen", _Proc)
    added: list[Any] = []
    ws = wt.Workspace(str(tmp_path), end_commands_with_server=True)
    if sys.platform == "win32":
        assert ws.command_groups is None
        return
    monkeypatch.setattr(ws.command_groups, "add", added.append)
    ws.run_command("true")
    # No timeout wrapper: the server ends the command's group itself.
    assert seen == {"argv": ["/bin/sh", "-c", "true"], "new_session": True}
    assert [p.pid for p in added] == [4242]
    assert wt.Workspace(str(tmp_path)).command_groups is None


def _gone(pid: int) -> bool:
    from kiro_crew import platform_compat

    return not platform_compat.pid_exists(pid) or bool(platform_compat.pid_is_zombie(pid))


#: A keep-alive child by absolute path: no ``sleep`` or anything else from PATH.
_SLEEPER = " ".join(
    shlex.quote(word) for word in (sys.executable, "-I", "-c", "import time; time.sleep(300)")
)
#: A command that returns at once and leaves a child running, printing its pid.
_BACKGROUNDS = f"{_SLEEPER} >/dev/null 2>&1 & echo $!"


async def _start_server(tmp_path, *prefix: str) -> Any:
    from kiro_crew.remote_session import workspace_tools as wt

    return await asyncio.create_subprocess_exec(
        # -B: isolated mode ignores the runner's cache redirect, so no bytecode
        # may land in the checkout beside the server files.
        *(prefix or (sys.executable, "-I", "-B", "-u", wt.__file__)),
        *(() if prefix else ("--end-commands-with-server", "--root", str(tmp_path))),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


async def _backgrounded_child(proc: Any) -> int:
    """Ask the server to run :data:`_BACKGROUNDS`; the pid of the child it left."""
    import json

    from kiro_crew import platform_compat

    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "run_command", "arguments": {"command": _BACKGROUNDS, "timeout": 30}},
    }
    proc.stdin.write(json.dumps(call).encode() + b"\n")
    await proc.stdin.drain()
    reply = json.loads(await asyncio.wait_for(proc.stdout.readline(), FALLBACK_TIMEOUT_SECS))
    child = int(reply["result"]["content"][0]["text"].split()[0])
    assert platform_compat.pid_exists(child)
    return child


async def _end(proc: Any) -> None:
    from kiro_crew import platform_compat

    with contextlib.suppress(Exception):
        proc.stdin.close()
    if proc.returncode is None:
        # SIGTERM first, with time for the server to end its tracked groups
        # (its own grace is GROUP_GRACE_SECS), and only then SIGKILL.
        await platform_compat.terminate_and_reap(proc, grace=FALLBACK_TIMEOUT_SECS / 4)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows uses a job")
@pytest.mark.xdist_group(name="subprocess_spawn")
def test_a_timed_out_command_s_grandchild_is_gone_after_the_timeout_reply(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.testing.wait import wait_until

    ws = wt.Workspace(str(tmp_path), end_commands_with_server=True)
    try:
        out = ws.run_command(f"{_BACKGROUNDS}; {_SLEEPER}", timeout=1)
        assert out.endswith("[timed out after 1s]")
        grandchild = int(out.split()[0])
        wait_until(lambda: _gone(grandchild), timeout=FALLBACK_TIMEOUT_SECS / 2)
    finally:
        ws.command_groups.end_all()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows uses a job")
@pytest.mark.xdist_group(name="subprocess_spawn")
@pytest.mark.asyncio
async def test_the_end_of_the_server_s_input_ends_a_command_s_background_child(tmp_path):
    from kiro_crew.testing.wait import async_wait_until

    proc = await _start_server(tmp_path)
    try:
        child = await _backgrounded_child(proc)
        # The bridge left: the server ends what its commands left, then exits.
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), FALLBACK_TIMEOUT_SECS)
        await async_wait_until(lambda: _gone(child), timeout=FALLBACK_TIMEOUT_SECS / 2)
    finally:
        await _end(proc)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows uses a job")
@pytest.mark.xdist_group(name="subprocess_spawn")
@pytest.mark.asyncio
async def test_sigterm_to_the_server_ends_every_tracked_group(tmp_path):
    from kiro_crew import platform_compat
    from kiro_crew.testing.wait import async_wait_until

    proc = await _start_server(tmp_path)
    try:
        child = await _backgrounded_child(proc)
        # The connector's stop: group SIGTERM, a grace, then SIGKILL.
        await platform_compat.terminate_and_reap(proc, grace=3.0)
        await async_wait_until(lambda: _gone(child), timeout=FALLBACK_TIMEOUT_SECS / 2)
    finally:
        await _end(proc)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows uses a job")
@pytest.mark.xdist_group(name="subprocess_spawn")
@pytest.mark.asyncio
async def test_losing_its_parent_ends_a_command_s_background_child(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt
    from kiro_crew.testing.wait import async_wait_until

    # A stand-in connector: it starts the server on its own pipes, then waits to
    # be killed. The input stays open, so only the parent watch can end it.
    parent = (
        "import subprocess, sys, time;"
        f" subprocess.Popen([sys.executable, '-I', '-B', '-u', {wt.__file__!r},"
        f" '--end-commands-with-server', '--root', {str(tmp_path)!r}],"
        " start_new_session=True); time.sleep(300)"
    )
    proc = await _start_server(tmp_path, sys.executable, "-I", "-c", parent)
    try:
        child = await _backgrounded_child(proc)
        proc.kill()
        await asyncio.wait_for(proc.wait(), FALLBACK_TIMEOUT_SECS)
        await async_wait_until(lambda: _gone(child), timeout=FALLBACK_TIMEOUT_SECS / 2)
    finally:
        await _end(proc)


# -- Windows: a network folder is never a cmd.exe working folder ----------------------


@pytest.mark.parametrize(
    "path, network",
    [
        ("\\\\server\\share\\proj", True),
        ("//server/share/proj", True),
        ("\\\\?\\UNC\\server\\share\\proj", True),
        ("//?/unc/server/share/proj", True),
        ("C:\\Projects\\app", False),
        ("\\\\?\\C:\\Projects\\app", False),
        ("\\\\.\\C:\\Projects", False),
        ("/home/u/proj", False),
    ],
)
def test_a_windows_network_folder_is_recognised_in_every_spelling(path, network):
    from kiro_crew.remote_session import workspace_tools as wt

    assert wt.windows_network_folder(path) is network


def test_cmd_refuses_to_run_in_a_network_folder(tmp_path, monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no command may start in a network folder")

    monkeypatch.setattr(wt.subprocess, "Popen", never)
    ws = wt.Workspace(str(tmp_path), shell="cmd")
    monkeypatch.setattr(ws, "resolve", lambda cwd: "\\\\server\\share\\proj")
    with pytest.raises(wt.ToolError, match="network folder"):
        ws.run_command("dir")


@pytest.mark.parametrize("name", ["safe & del victim.txt", "a|b", "x>y", "x<y", "x^y", 'a"b'])
def test_cmd_refuses_a_folder_whose_name_it_would_read_as_syntax(tmp_path, monkeypatch, name):
    from kiro_crew.remote_session import workspace_tools as wt

    def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no command may start where %CD% would add commands")

    monkeypatch.setattr(wt.subprocess, "Popen", never)
    ws = wt.Workspace(str(tmp_path), shell="cmd")
    monkeypatch.setattr(ws, "resolve", lambda cwd: "C:\\Projects\\app\\" + name)
    with pytest.raises(wt.ToolError, match="folder's name"):
        ws.run_command("echo %CD%")


def test_cmd_runs_in_an_ordinary_folder_name(tmp_path, monkeypatch):
    from kiro_crew.remote_session import workspace_tools as wt

    started: list[Any] = []

    class _Proc:
        returncode = 0
        pid = UNALLOCATABLE_PID
        stdout = io.BytesIO(b"")

        def __init__(self, argv: Any, **kwargs: Any) -> None:
            started.append(kwargs["cwd"])

        def wait(self, timeout: Any = None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(wt.subprocess, "Popen", _Proc)
    ws = wt.Workspace(str(tmp_path), shell="cmd")
    monkeypatch.setattr(ws, "resolve", lambda cwd: "C:\\Projects\\Copy (2) of app-v2.1")
    ws.run_command("dir")
    assert started == ["C:\\Projects\\Copy (2) of app-v2.1"]


def test_the_model_is_told_windows_commands_are_not_confined():
    from kiro_crew.connected_computers.provider import notice_for

    windows = notice_for("PC", "C:\\Projects\\app", "win32")
    assert "NOT confined" in windows and "commands run confined" not in windows
    for platform in ("linux", "darwin", ""):
        assert "commands run confined to it" in notice_for("Mac", "/w", platform)


def test_the_folder_the_interpreter_is_started_from_cannot_be_shared(tmp_path, monkeypatch):
    from kiro_crew.connected_computers import root_policy

    # A Homebrew-style layout: bin/python3 is a link into the Cellar install.
    cellar = tmp_path / "Cellar" / "python" / "bin"
    cellar.mkdir(parents=True)
    (cellar / "python3.12").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(root_policy.sys, "executable", str(bin_dir / "python3"))
    monkeypatch.setattr(
        "kiro_crew.connected_computers.confinement.runtime_reads", lambda: [str(cellar)]
    )
    assert bin_dir.resolve() in root_policy._program_dirs()


def test_windows_refuses_to_share_a_network_folder_by_its_real_path(monkeypatch):
    from kiro_crew.connected_computers import root_policy

    class _WindowsSys:
        platform = "win32"

    monkeypatch.setattr(root_policy, "sys", _WindowsSys())
    # A drive letter mapped to a share: its real path is the share.
    monkeypatch.setattr(root_policy, "_realpath", lambda raw: "\\\\server\\share\\proj")
    # Any absolute spelling here: only the real path the lookup answers decides.
    with pytest.raises(root_policy.RootRefused, match="network folder"):
        root_policy.resolve_root(os.path.abspath("proj"))


# -- the guest's case rule travels from the connection to the gate -------------------


def test_a_launch_keeps_the_computer_s_platform_and_the_gate_reads_only_true():
    from kiro_crew.acp.session_handle_permissions import _remote_case_insensitive
    from kiro_crew.connected_computers.launch import ConnectedComputerLaunch

    assert ConnectedComputerLaunch("mac", "Mac", session_key="s", platform="darwin").platform == (
        "darwin"
    )
    assert ConnectedComputerLaunch("pc", "PC", session_key="s").platform == ""
    assert _remote_case_insensitive({"workdir": "/w", "case_insensitive": True}) is True
    for workspace in ({"workdir": "/w"}, {"case_insensitive": "yes"}, None):
        assert _remote_case_insensitive(workspace) is False
