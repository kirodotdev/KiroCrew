"""The KAS MCP launcher, run in-process: every refusal and both start paths.

``test_kas_env_launch.py`` drives the launcher end to end through a real child
process. These tests call it directly, with snapshots written by hand, so each
refusal is pinned to its reason and the exec / Windows-spawn tails are reached
without starting a server.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import queue
import subprocess
import sys
import threading
import venv
from pathlib import Path

import pytest

from kiro_crew import kas_env_shim
from kiro_crew.platform_compat_native import _JobObjectExtendedLimitInformation
from kiro_crew.testing.wait import default_timeout

SESSION, AGENT, SERVER, VERSION = "dashboard_chat-shim", "kirocrew", "srv", "a" * 32
REF = ["--session", SESSION, "--agent", AGENT, "--server", SERVER, "--version", VERSION]


@pytest.fixture
def home(tmp_path) -> Path:
    data_home = tmp_path / "crew-home"
    (data_home / kas_env_shim.SNAPSHOT_LEAF).mkdir(parents=True, mode=0o700)
    return data_home


def _path(home: Path) -> Path:
    return (
        home
        / kas_env_shim.SNAPSHOT_LEAF
        / kas_env_shim.snapshot_name(SESSION, AGENT, SERVER, VERSION)
    )


def _doc(**overrides) -> dict:
    doc = {
        kas_env_shim.SNAPSHOT_MARKER: kas_env_shim.SNAPSHOT_FORMAT,
        "session": SESSION,
        "agent": AGENT,
        "server": SERVER,
        "version": VERSION,
        "command": "server-bin",
        "args": ["--flag"],
        "env": {"TOKEN_FOR_TEST": "value"},
    }
    doc.update(overrides)
    return doc


def _write(home: Path, payload: bytes) -> Path:
    path = _path(home)
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _environ(home: Path) -> dict[str, str]:
    return {"KIROCREW_HOME": str(home), "PATH": os.environ.get("PATH", "")}


def _refused(home: Path, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        kas_env_shim.resolve_launch(list(REF), _environ(home))
    assert exc.value.code == 78
    err = capsys.readouterr().err
    assert "value" not in err, "a refusal never prints an env value"
    return err


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({kas_env_shim.SNAPSHOT_MARKER: 2}, "unknown format"),
        ({"command": "  "}, "names no command"),
        ({"args": ["ok", 3]}, "malformed arguments"),
        ({"env": {"K": 1}}, "malformed environment"),
    ],
)
def test_a_snapshot_with_a_bad_field_is_refused(home, capsys, overrides, reason):
    _write(home, json.dumps(_doc(**overrides)).encode())
    assert reason in _refused(home, capsys)


@pytest.mark.parametrize(
    ("payload", "reason"), [(b"{not json", "not valid JSON"), (b"[]", "wrong shape")]
)
def test_a_snapshot_that_is_not_a_json_object_is_refused(home, capsys, payload, reason):
    _write(home, payload)
    assert reason in _refused(home, capsys)


def test_a_snapshot_that_is_too_large_is_refused(home, capsys):
    _write(home, b" " * (kas_env_shim._MAX_SNAPSHOT_BYTES + 1))
    assert "too large" in _refused(home, capsys)


@pytest.mark.skipif(os.name != "posix", reason="a directory opens read-only on POSIX")
def test_a_snapshot_that_is_not_a_regular_file_is_refused(home, capsys):
    _path(home).mkdir(mode=0o700)
    assert "not a regular file" in _refused(home, capsys)


@pytest.mark.skipif(os.name != "posix", reason="symlink creation")
def test_a_linked_launch_directory_is_refused(home, tmp_path, capsys):
    leaf = home / kas_env_shim.SNAPSHOT_LEAF
    real = tmp_path / "elsewhere"
    leaf.rename(real)
    leaf.symlink_to(real, target_is_directory=True)
    _write(home, json.dumps(_doc()).encode())
    assert "launch directory is a link" in _refused(home, capsys)


def _failing_open(monkeypatch, path: Path, error: type[OSError], times: int | None) -> list[str]:
    """Make the launcher's open of *path* raise *error* *times* times (``None``: always)."""
    real_open, tries = os.open, []
    code = errno.ENOENT if error is FileNotFoundError else errno.EACCES

    def _open(name, flags, *args, **kwargs):
        if os.fspath(name) == str(path):
            tries.append(name)
            if times is None or len(tries) <= times:
                raise error(code, os.strerror(code))
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(kas_env_shim.os, "open", _open)
    return tries


def test_a_windows_open_that_meets_a_replace_reads_the_snapshot_a_moment_later(
    home, monkeypatch, capsys
):
    """Windows refuses an open while the gateway replaces the shared snapshot,
    which it rewrites on every projection there; the launcher tries again."""
    path = _write(home, json.dumps(_doc()).encode())
    tries = _failing_open(monkeypatch, path, PermissionError, times=1)
    monkeypatch.setattr(kas_env_shim.os, "name", "nt")
    try:
        argv, env = kas_env_shim.resolve_launch(list(REF), _environ(home))
    except SystemExit:
        pytest.fail(f"the launcher refused: {capsys.readouterr().err}")
    assert argv == ["server-bin", "--flag"] and env["TOKEN_FOR_TEST"] == "value"
    assert len(tries) == 2


@pytest.mark.parametrize(
    ("os_name", "error", "reason", "retried"),
    [
        ("nt", PermissionError, "cannot be opened (Permission denied)", True),
        ("posix", PermissionError, "cannot be opened (Permission denied)", False),
        ("nt", FileNotFoundError, "launch snapshot is missing", False),
    ],
    ids=["windows-held-past-the-budget", "posix-at-once", "windows-missing-at-once"],
)
def test_an_open_that_keeps_failing_is_refused_with_its_reason(
    home, monkeypatch, capsys, os_name, error, reason, retried
):
    path = _write(home, json.dumps(_doc()).encode())
    tries = _failing_open(monkeypatch, path, error, times=None)
    monkeypatch.setattr(kas_env_shim.os, "name", os_name)
    assert reason in _refused(home, capsys)
    assert len(tries) == (kas_env_shim._OPEN_ATTEMPTS if retried else 1)


_LAUNCH = (["server-bin", "--flag"], {"TOKEN_FOR_TEST": "value"})


def _stub_resolve(monkeypatch) -> list[list[str]]:
    """Replace the snapshot read, so these tests reach only the exec tail.

    They force ``os.name`` to ``posix`` to pick the exec branch; the real read
    would then run POSIX-only ownership checks on a Windows host. The read
    itself is covered by the refusal tests above and by test_kas_env_launch.py.
    """
    refs: list[list[str]] = []

    def _resolve(argv, _environ):
        refs.append(list(argv))
        command, *args = _LAUNCH[0]
        return SERVER, {"command": command, "args": args, "env": dict(_LAUNCH[1])}, {}

    monkeypatch.setattr(kas_env_shim, "_resolve_snapshot", _resolve)
    return refs


def test_main_execs_the_recorded_command_with_its_env(monkeypatch):
    refs = _stub_resolve(monkeypatch)
    seen: list[tuple] = []

    def _exec(file, argv, env):
        seen.append((file, argv, env))
        raise SystemExit(0)

    monkeypatch.setattr(kas_env_shim.os, "name", "posix")
    monkeypatch.setattr(kas_env_shim.os, "execvpe", _exec)
    with pytest.raises(SystemExit):
        kas_env_shim.main(list(REF))
    assert refs == [REF]
    ((file, argv, env),) = seen
    assert (file, argv) == ("server-bin", ["server-bin", "--flag"])
    assert env == {"TOKEN_FOR_TEST": "value"}


def test_main_reads_its_own_argv_when_given_none(monkeypatch):
    refs = _stub_resolve(monkeypatch)
    monkeypatch.setattr(kas_env_shim.os, "name", "posix")
    monkeypatch.setattr(kas_env_shim.os, "execvpe", lambda *_a: None)
    monkeypatch.setattr(kas_env_shim.sys, "argv", ["kas_env_shim", *REF])
    with pytest.raises(SystemExit) as exc:
        kas_env_shim.main()
    assert refs == [REF]
    assert exc.value.code == 1, "an exec that returns is a failure, not a start"


def test_main_reports_a_command_that_cannot_start(monkeypatch, capsys):
    _stub_resolve(monkeypatch)

    def _missing(*_a):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(kas_env_shim.os, "name", "posix")
    monkeypatch.setattr(kas_env_shim.os, "execvpe", _missing)
    with pytest.raises(SystemExit) as exc:
        kas_env_shim.main(list(REF))
    assert exc.value.code == 127
    err = capsys.readouterr().err
    assert "'server-bin' could not be started" in err and "value" not in err


def test_main_on_windows_runs_the_server_as_a_child(home, monkeypatch):
    _write(home, json.dumps(_doc()).encode())
    seen: list[list[str]] = []

    def _spawn(argv, env):
        seen.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(kas_env_shim.os, "name", "nt")
    monkeypatch.setattr(kas_env_shim, "_spawn_child", _spawn)
    monkeypatch.setattr(kas_env_shim.os, "environ", _environ(home))
    with pytest.raises(SystemExit):
        kas_env_shim.main(list(REF))
    assert seen == [["server-bin", "--flag"]]


class _Proc:
    def __init__(self, rc=None, error: BaseException | None = None):
        self.rc, self.error, self.terminated = rc, error, False

    def wait(self):
        if self.error is not None:
            raise self.error
        return self.rc

    def terminate(self):
        self.terminated = True


def _exit_raises(code):
    raise SystemExit(code)


@pytest.mark.parametrize(("rc", "code"), [(3, 3), (-9, 1)])
def test_the_windows_child_exits_with_the_servers_status(monkeypatch, tmp_path, rc, code):
    started: list[list[str]] = []

    def _popen(argv, **kwargs):
        assert (kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]) == (0, 1, 2)
        started.append(argv)
        return _Proc(rc)

    monkeypatch.setattr(kas_env_shim.shutil, "which", lambda name, path=None: name)
    monkeypatch.setattr(kas_env_shim.subprocess, "Popen", _popen)
    monkeypatch.setattr(kas_env_shim.os, "_exit", _exit_raises)
    with pytest.raises(SystemExit) as exc:
        kas_env_shim._spawn_child(["server-bin", "--flag"], {"PATH": str(tmp_path)})
    assert exc.value.code == code
    assert started == [[str(tmp_path / "server-bin"), "--flag"]]


def test_the_windows_child_that_cannot_start_ends_with_the_one_line(monkeypatch, tmp_path, capsys):
    def _popen(*_a, **_k):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(kas_env_shim.os, "name", "nt")
    monkeypatch.setattr(kas_env_shim.shutil, "which", lambda name, path=None: name)
    monkeypatch.setattr(kas_env_shim.subprocess, "Popen", _popen)
    env = {"PATH": str(tmp_path), "TOKEN_FOR_TEST": "value"}
    with pytest.raises(SystemExit) as exc:
        kas_env_shim._spawn_child(["server-bin", "--flag"], env)
    assert exc.value.code == 127
    err = capsys.readouterr().err
    assert err == (
        "kirocrew: MCP server command 'server-bin' could not be started: "
        "The system cannot find the file specified\n"
    )


def test_the_windows_child_is_stopped_when_the_launcher_is_interrupted(monkeypatch, tmp_path):
    proc = _Proc(error=KeyboardInterrupt())
    monkeypatch.setattr(kas_env_shim.shutil, "which", lambda name, path=None: name)
    monkeypatch.setattr(kas_env_shim.subprocess, "Popen", lambda argv, **_k: proc)
    with pytest.raises(KeyboardInterrupt):
        kas_env_shim._spawn_child(["server-bin"], {"PATH": str(tmp_path)})
    assert proc.terminated


@pytest.mark.parametrize("launcher", ["kas_env_shim", "stub_fallback"])
def test_a_windows_launch_never_starts_a_file_from_the_working_directory(
    monkeypatch, tmp_path, launcher
):
    """A bare server command is looked up only in the absolute entries of the server's PATH.

    Windows looks in the current directory first, both in ``shutil.which`` and in
    ``CreateProcess``, so a file there named like the command must not start, and
    a relative PATH entry must not reach it either. Both Windows launchers share
    the lookup.
    """
    from kiro_crew.mcp_gateway import stub_fallback

    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    name = "zzsrvcmd.cmd" if os.name == "nt" else "zzsrvcmd"
    work, empty, tools = (tmp_path / leaf for leaf in ("work", "empty", "tools"))

    def plant(folder):
        folder.mkdir()
        target = folder / name
        target.write_text("@exit /b 7\n")
        target.chmod(0o755)
        return str(target)

    plant(work)
    empty.mkdir()
    monkeypatch.chdir(work)
    started: list[str] = []

    def _popen(argv, **_kwargs):
        started.append(os.path.normcase(argv[0]))
        return _Proc(0)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(os, "_exit", _exit_raises)
    if launcher == "kas_env_shim":
        spawn, not_found = kas_env_shim._spawn_child, SystemExit
    else:
        spawn, not_found = stub_fallback._fallback_spawn_child, FileNotFoundError

    with pytest.raises(not_found):
        spawn(["zzsrvcmd"], {"PATH": os.pathsep.join([str(empty), os.curdir])})
    assert started == []

    real = plant(tools)
    with pytest.raises(SystemExit):
        spawn(["zzsrvcmd"], {"PATH": os.pathsep.join([os.curdir, str(tools)])})
    assert started == [os.path.normcase(real)]


class _Kernel32:
    """A fake kernel32 that records the job calls the launcher makes."""

    def __init__(self, *, assign_ok: bool = True, parent: int = 0):
        self.calls: list[tuple] = []
        self.flags: int | None = None
        k = self

        def create(attrs, name):
            k.calls.append(("create", attrs, name))
            return 0x1234

        def set_info(job, kind, info, size):
            k.flags = ctypes.cast(info, ctypes.POINTER(_JobObjectExtendedLimitInformation))[
                0
            ].BasicLimitInformation.LimitFlags
            k.calls.append(("set", job, kind))
            return 1

        def assign(job, process):
            k.calls.append(("assign", job, process))
            return 1 if assign_ok else 0

        self.CreateJobObjectW = create
        self.SetInformationJobObject = set_info
        self.GetCurrentProcess = lambda: -1
        self.AssignProcessToJobObject = assign
        self.CloseHandle = lambda handle: k.calls.append(("close", handle)) or 1
        self.OpenProcess = lambda access, inherit, pid: k.calls.append(("open", access, pid)) or (
            parent
        )
        self.WaitForSingleObject = lambda handle, ms: k.calls.append(("wait", handle, ms)) or 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_the_launcher_binds_nothing_off_windows():
    assert kas_env_shim.windows_bind_children_to_self() is None


def _on_fake_windows(monkeypatch, kernel_factory) -> None:
    monkeypatch.setattr(kas_env_shim, "_on_windows", lambda: True)
    monkeypatch.setattr(kas_env_shim, "_kernel32", kernel_factory)
    monkeypatch.setattr(kas_env_shim, "_engine_pid", lambda: 4242)


def test_the_launcher_joins_a_job_that_ends_its_children_with_it(monkeypatch):
    kernel = _Kernel32()
    _on_fake_windows(monkeypatch, lambda: kernel)
    monkeypatch.setattr(kas_env_shim, "_last_error", lambda: 5)
    assert kas_env_shim.windows_bind_children_to_self() == 0x1234
    assert kernel.flags == (
        kas_env_shim._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | kas_env_shim._JOB_OBJECT_LIMIT_BREAKAWAY_OK
    ), "kill on close, explicit breakaway only"
    assert kernel.calls == [
        ("create", None, None),
        ("set", 0x1234, kas_env_shim._JOB_EXTENDED_LIMITS),
        ("assign", 0x1234, -1),
        ("open", kas_env_shim._SYNCHRONIZE, 4242),
    ], "this process itself joins, before any child starts, and the handle stays open"


@pytest.mark.parametrize(
    ("hops", "above", "watched"),
    [(1, 4242, 4242), (1, -1, None), (0, 4242, None), (1, OSError(), None)],
)
def test_a_launcher_behind_a_venv_redirector_watches_the_engine_above_it(
    monkeypatch, hops, above, watched
):
    """The redirector waits on the launcher, so the engine is the process one hop up."""
    from kiro_crew import platform_compat_identity, platform_compat_runtime

    def _get_ppid(pid):
        if isinstance(above, Exception):
            raise above
        return above

    monkeypatch.setattr(platform_compat_runtime, "python_launcher_hops", lambda: hops)
    monkeypatch.setattr(platform_compat_identity, "get_ppid", _get_ppid)
    assert kas_env_shim._engine_pid() == (os.getppid() if watched is None else watched)


def test_the_launcher_ends_itself_once_its_engine_is_gone(monkeypatch):
    """Windows leaves a child running when its parent exits; the launcher does not."""
    kernel = _Kernel32(parent=0x99)
    exits: list[int] = []
    monkeypatch.setattr(kas_env_shim, "_exit_process", exits.append)
    monkeypatch.setattr(kas_env_shim, "_engine_pid", lambda: 4242)
    watcher = kas_env_shim._windows_exit_with_parent(kernel)
    assert watcher is not None
    watcher.join(timeout=10)
    assert not watcher.is_alive() and watcher.daemon
    assert kernel.calls == [
        ("open", kas_env_shim._SYNCHRONIZE, 4242),
        ("wait", 0x99, kas_env_shim._INFINITE),
    ]
    assert exits == [kas_env_shim._EXIT_PARENT_GONE]


@pytest.mark.parametrize(("error", "exits"), [(87, [1]), (5, [])])
def test_a_parent_gone_before_the_launch_ends_the_launcher_first(monkeypatch, error, exits):
    """A parent with no process left ends the launcher; a refused open only skips the watch."""
    ended: list[int] = []
    monkeypatch.setattr(kas_env_shim, "_exit_process", ended.append)
    monkeypatch.setattr(kas_env_shim, "_last_error", lambda: error)
    monkeypatch.setattr(kas_env_shim, "_engine_pid", lambda: 4242)
    assert kas_env_shim._windows_exit_with_parent(_Kernel32(parent=0)) is None
    assert ended == exits


def test_a_launcher_whose_engine_is_gone_never_starts_the_server(monkeypatch, tmp_path):
    class _Ended(BaseException):
        pass

    def _end(code):
        raise _Ended(code)

    started: list[list[str]] = []
    _on_fake_windows(monkeypatch, lambda: _Kernel32(parent=0))
    monkeypatch.setattr(kas_env_shim, "_last_error", lambda: 87)
    monkeypatch.setattr(kas_env_shim, "_exit_process", _end)
    monkeypatch.setattr(kas_env_shim, "windows_child_command", lambda command, env: command)
    monkeypatch.setattr(kas_env_shim.subprocess, "Popen", lambda argv, **_k: started.append(argv))
    with pytest.raises(_Ended):
        kas_env_shim._spawn_child(["server-bin"], {"PATH": str(tmp_path)})
    assert started == []


@pytest.mark.parametrize("failure", ["assign", "create", "raise"])
def test_a_job_that_cannot_be_made_still_lets_the_server_start(monkeypatch, failure):
    kernel = _Kernel32(assign_ok=failure != "assign")
    if failure == "create":
        kernel.CreateJobObjectW = lambda *_a: 0

    def _windll(*_a, **_k):
        if failure == "raise":
            raise OSError("no kernel32")
        return kernel

    _on_fake_windows(monkeypatch, _windll)
    assert kas_env_shim.windows_bind_children_to_self() is None
    if failure == "assign":
        assert kernel.calls[-1] == ("close", 0x1234)


@pytest.mark.parametrize("launcher", ["kas_env_shim", "stub_fallback"])
def test_a_windows_launcher_joins_its_job_before_the_server_starts(monkeypatch, tmp_path, launcher):
    from kiro_crew.mcp_gateway import stub_fallback

    order: list[str] = []
    module = kas_env_shim if launcher == "kas_env_shim" else stub_fallback
    monkeypatch.setattr(module, "windows_bind_children_to_self", lambda: order.append("job"))
    monkeypatch.setattr(module, "windows_child_command", lambda command, env: command)

    def _popen(argv, **_kwargs):
        order.append("spawn")
        return _Proc(0)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(os, "_exit", _exit_raises)
    spawn = (
        kas_env_shim._spawn_child
        if launcher == "kas_env_shim"
        else stub_fallback._fallback_spawn_child
    )
    with pytest.raises(SystemExit):
        spawn(["server-bin"], {"PATH": str(tmp_path)})
    assert order == ["job", "spawn"]


_LAUNCHER = """
import subprocess, sys
from kiro_crew import kas_env_shim
assert kas_env_shim.windows_bind_children_to_self() is not None
import os
server = "import sys; sys.stdin.read()"
subprocess.Popen([sys.executable, "-c", server], stdin=0, stdout=1, stderr=2)
print("ready", os.getpid(), flush=True)
sys.stdin.read()
"""
#: The engine: starts the launcher on its own stdio, through the interpreter it is
#: given, and waits for it.
_ENGINE = """
import subprocess, sys
subprocess.run([sys.argv[2], "-c", sys.argv[1]], stdin=0, stdout=1, stderr=2)
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
@pytest.mark.parametrize("killed", ["launcher", "engine"])
def test_a_windows_server_ends_when_its_launcher_or_engine_is_killed(tmp_path, killed):
    """The server holds the launcher's stdin and stdout, as a real one does.

    The test keeps that stdin open, so only the job can end the server, and the
    stdout pipe reaches EOF only once every process holding it is gone.
    The launcher starts through a venv's ``python.exe`` redirector, as an
    installed Kiro Crew's does.
    """
    venv.create(tmp_path / "venv")
    engine = subprocess.Popen(
        # The base interpreter, so the killed engine is the process the launcher watches.
        [
            getattr(sys, "_base_executable", sys.executable),
            "-c",
            _ENGINE,
            _LAUNCHER,
            str(tmp_path / "venv" / "Scripts" / "python.exe"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=tmp_path,
        # The checkout's package by absolute path, since the cwd is not the checkout.
        env={**os.environ, "PYTHONPATH": str(Path(kas_env_shim.__file__).parents[1])},
        text=True,
        encoding="utf-8",
    )
    assert engine.stdin is not None and engine.stdout is not None
    stdout = engine.stdout
    lines: queue.Queue[str | None] = queue.Queue()

    def _read() -> None:
        for line in stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        ready = lines.get(timeout=default_timeout())
        assert ready is not None and ready.startswith("ready"), ready
        if killed == "engine":
            engine.kill()
        else:
            launcher_pid = ready.split()[1]
            subprocess.run(
                ["taskkill", "/F", "/PID", launcher_pid], check=True, timeout=default_timeout()
            )
        assert (
            lines.get(timeout=default_timeout()) is None
        ), "the server outlived the killed process"
    finally:
        if engine.poll() is None:
            engine.kill()
        # Closing stdin ends a server that survived, so the reader returns.
        engine.stdin.close()
        reader.join(timeout=60)
        engine.wait(timeout=60)
