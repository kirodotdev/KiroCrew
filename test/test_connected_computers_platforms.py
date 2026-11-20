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
import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.connected_computers import confinement, confinement_exec, confinement_macos
from kiro_crew.connected_computers import protected_store as ps
from kiro_crew.connected_computers.confinement import (
    ConfinementUnavailable,
    WindowsUnconfinedPlan,
    judge_self_test,
)
from kiro_crew.connected_computers.protected_store import ProtectionUnavailable
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


def test_windows_plan_runs_in_the_root_and_finds_the_program_on_the_childs_path(monkeypatch):
    seen: dict[str, Any] = {}

    def which(cmd, path=None):
        seen["lookup"] = (cmd, path)
        return r"C:\tools\npx.cmd"

    monkeypatch.setattr(confinement.shutil, "which", which)
    monkeypatch.setenv("PATH", "child-path")
    child = WindowsUnconfinedPlan().wrap(
        ["npx", "server"], root="root-dir", scratch="scratch-dir", reads=["ignored"], env={"K": "v"}
    )
    assert child.argv == [r"C:\tools\npx.cmd", "server"]
    assert child.cwd == "root-dir" and child.pass_fds == ()
    assert child.env["K"] == "v" and child.env["TEMP"] == "scratch-dir"
    assert seen["lookup"] == ("npx", "child-path")


def test_windows_plan_keeps_an_absolute_or_unfound_program(monkeypatch):
    monkeypatch.setattr(confinement.shutil, "which", lambda cmd, path=None: None)
    child = WindowsUnconfinedPlan().wrap(["tool"], root="r", scratch="s")
    assert child.argv == ["tool"]
    absolute = os.path.abspath("prog")
    assert WindowsUnconfinedPlan().wrap([absolute], root="r", scratch="s").argv == [absolute]


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


def test_only_the_listed_mach_services_and_owned_sockets_are_reachable():
    text = _profile()
    for name in confinement_macos.MACOS_MACH_SERVICES:
        assert f'(global-name "{name}")' in text
    assert confinement.MACOS_MACH_WITNESS not in text
    assert '(allow network-outbound (literal "/private/var/run/mDNSResponder"))' in text
    assert '(allow network-outbound (remote ip "*:*"))' in text


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
def test_the_probe_reaches_a_socket_file_and_answers_the_mach_witness(tmp_path):
    import socket

    from kiro_crew.connected_computers import confinement_probe as probe

    base = Path(os.path.realpath(tmp_path))
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

    from kiro_crew.remote_session import workspace_git_bodies, workspace_tools

    # A copy outside the package, as a guest or a connected computer runs it.
    for module in (workspace_tools, workspace_git_bodies):
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
            probe + "; import workspace_git_bodies as m;"
            f" assert m.__file__ == {str(tmp_path / 'workspace_git_bodies.py')!r}",
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
