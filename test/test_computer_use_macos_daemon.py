"""D-6: on macOS the driver daemon runs inside the env fence, or not at all.

Every external effect is faked: ``open``, the stop command, the kernel's record
of a process, the socket probe and pid liveness. Nothing here launches an app.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew.computer_use.driver_runtime import env as driver_env_mod
from kiro_crew.computer_use.driver_runtime import install as install_mod
from kiro_crew.computer_use.driver_runtime import macos_daemon as md
from kiro_crew.computer_use.driver_runtime import supervisor as sup_mod
from kiro_crew.computer_use.driver_runtime.supervisor import DriverSupervisor, DriverUnavailable

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the macOS daemon uses a Unix socket")

FAKE = Path(__file__).with_name("computer_use_fake_driver.py")
DAEMON_PID = 4711
#: The sealed, re-hashed copy the daemon module runs (never the /Applications one).
SEALED = Path("/opt/cua-driver")
OPEN_HELP = (
    "Usage: open [-e] ...\n      --env VAR  Add an enviroment variable to the launched process\n"
)


_CACHES: list[Path] = []


@pytest.fixture(autouse=True)
def _remove_short_caches():  # type: ignore[no-untyped-def]
    yield
    while _CACHES:
        shutil.rmtree(_CACHES.pop(), ignore_errors=True)


class FakeMac:
    """Plays LaunchServices, the daemon, the kernel and the stop command."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        env_ok: bool = True,
        open_env: bool = True,
        launch_extra: "dict[str, str] | None" = None,
        stop_works: bool = True,
    ) -> None:
        self.launch_extra = launch_extra or {}
        self.stop_works = stop_works
        self.envs: list[dict[str, str]] = []
        self.app_problem = ""

        self.app = tmp_path / "Applications" / "CuaDriver.app"
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        (self.app / install_mod.MACOS_APP_BINARY).write_bytes(b"bin")
        self.env_ok = env_ok
        self.open_env = open_env
        self.calls: list[list[str]] = []
        self.alive: set[int] = set()
        self.records: dict[int, md.ProcArgs] = {}
        self.listening_on: set[str] = set()
        self.peer: dict[str, int] = {}
        self.foreign: "dict[str, tuple[str, int | None]]" = {}
        self.daemon: "md.MacDaemon | None" = None
        self.cache = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "cud-", dir=short_tmp_base()))
        _CACHES.append(self.cache)

    def run(
        self, argv: list[str], *, timeout: float, env: Any
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        assert self.daemon is not None
        if argv == [md.OPEN, "-h"]:
            out = OPEN_HELP if self.open_env else "Usage: open [-e] [-t] [-f]\n"
            return subprocess.CompletedProcess(argv, 1, "", out)
        if argv[0] == md.OPEN:
            env = {}
            for i, a in enumerate(argv):
                if a == "--env":
                    key, _, value = argv[i + 1].partition("=")
                    env[key] = value
            if not self.env_ok:
                env.pop("DO_NOT_TRACK", None)
            env.update(self.launch_extra)
            serve = argv[argv.index("--args") + 1 :]
            self.records[DAEMON_PID] = md.ProcArgs(
                exec_path=str(self.app / install_mod.MACOS_APP_BINARY),
                argv=(str(self.app / install_mod.MACOS_APP_BINARY), *serve),
                env=env,
            )
            self.alive.add(DAEMON_PID)
            self.daemon.pid_path.write_text(str(DAEMON_PID))
            self.listening_on.add(str(self.daemon.socket_path))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(argv) > 1 and argv[1] == "stop":
            pid = int(argv[argv.index("--expected-pid") + 1])
            if not self.stop_works:
                return subprocess.CompletedProcess(argv, 1, "", "no daemon on socket")
            self.alive.discard(pid)
            self.listening_on.discard(argv[argv.index("--socket") + 1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def make(self, home: Path, **kw: Any) -> md.MacDaemon:
        # A short IPC dir: a test tmp path is too deep for a Unix socket.
        kw.setdefault("ipc_dir", self.cache / md.RUN_DIR_NAME)
        kw.setdefault("driver_binary", lambda: SEALED)
        kw.setdefault("verify_app", lambda: self.app_problem)
        self.daemon = md.MacDaemon(
            home,
            app=self.app,
            run=self.run,
            procargs=lambda pid: self.records.get(pid) if pid in self.alive else None,
            probe=self.probe,
            pid_alive=lambda pid: pid in self.alive,
            sleep=lambda _s: None,
            **kw,
        )
        return self.daemon

    def probe(self, path: Path) -> "tuple[str, int | None]":
        key = str(path)
        if key in self.foreign:
            return self.foreign[key]
        if key in self.listening_on:
            return md.SOCK_LIVE, self.peer.get(key, DAEMON_PID)
        return (md.SOCK_STALE, None) if path.exists() else (md.SOCK_ABSENT, None)

    def opens(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == md.OPEN and c[1:] != ["-h"]]

    def stops(self) -> list[list[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == "stop"]


def test_open_gets_exactly_the_fence_and_serves_on_the_private_socket(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    assert daemon.ensure() == DAEMON_PID
    (argv,) = fake.opens()
    private = str(install_mod.private_home(tmp_path / "home"))
    expected_env = {
        **driver_env_mod.FORCED_ENV,
        driver_env_mod.HOME_ENV: private,
        driver_env_mod.SHARED_HOME_ENV: private,
    }
    env_args = []
    for key, value in sorted(expected_env.items()):
        env_args += ["--env", f"{key}={value}"]
    sock = daemon.socket_path
    assert sock == fake.cache / md.RUN_DIR_NAME / md.SOCKET_NAME
    assert argv == [
        md.OPEN,
        "-n",
        "-g",
        *env_args,
        str(fake.app),
        "--args",
        "serve",
        "--socket",
        str(sock),
        "--pid-file",
        str(sock.with_name("d.pid")),
        "--no-permissions-gate",
    ]
    assert expected_env["DO_NOT_TRACK"] == "1"
    assert expected_env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
    assert expected_env["CUA_DRIVER_RS_UPDATE_CHECK"] == "false"
    assert daemon.mcp_args() == ("mcp", "--socket", str(sock))
    assert os.stat(sock.parent).st_mode & 0o777 == 0o700


def test_an_open_without_env_refuses_and_launches_nothing(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path, open_env=False)
    daemon = fake.make(tmp_path / "home")
    with pytest.raises(md.DaemonError, match="--env"):
        daemon.ensure()
    assert fake.opens() == []


def test_an_unproven_fence_stops_the_daemon_and_refuses(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path, env_ok=False)
    daemon = fake.make(tmp_path / "home")
    with pytest.raises(md.DaemonError, match="DO_NOT_TRACK"):
        daemon.ensure()
    (stop,) = fake.stops()
    assert stop == [
        str(SEALED),
        "stop",
        "--socket",
        str(daemon.socket_path),
        "--expected-pid",
        str(DAEMON_PID),
    ]
    assert DAEMON_PID not in fake.alive


def test_every_command_runs_inside_the_fence(tmp_path: Path) -> None:
    # ``stop`` is a finite upstream command that reports home unless fenced.
    fake = FakeMac(tmp_path, env_ok=False)
    daemon = fake.make(tmp_path / "home")
    with pytest.raises(md.DaemonError):
        daemon.ensure()
    assert fake.stops()
    for env in fake.envs:
        assert env["DO_NOT_TRACK"] == "1"
        assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
        assert env["CUA_DRIVER_RS_HOME"] == str(install_mod.private_home(tmp_path / "home"))


def test_an_inherited_driver_setting_is_unproven(tmp_path: Path) -> None:
    # launchd's user environment reaches an app LaunchServices starts.
    fake = FakeMac(tmp_path, launch_extra={"CUA_DRIVER_RS_MCP_HTTP_PORT": "9999"})
    daemon = fake.make(tmp_path / "home")
    with pytest.raises(md.DaemonError, match="CUA_DRIVER_RS_MCP_HTTP_PORT"):
        daemon.ensure()
    assert DAEMON_PID not in fake.alive


def test_a_repeated_fence_variable_is_unproven(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    real_run = fake.run

    def run(argv: list[str], *, timeout: float, env: Any) -> "subprocess.CompletedProcess[str]":
        res = real_run(argv, timeout=timeout, env=env)
        if argv[0] == md.OPEN and argv[1:] != ["-h"]:
            rec = fake.records[DAEMON_PID]
            fake.records[DAEMON_PID] = md.ProcArgs(
                rec.exec_path, rec.argv, rec.env, frozenset({"DO_NOT_TRACK"})
            )
        return res

    daemon._run = run
    with pytest.raises(md.DaemonError, match="more than once"):
        daemon.ensure()


def test_a_daemon_that_ignores_stop_is_never_killed_by_pid(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path, env_ok=False, stop_works=False)
    daemon = fake.make(tmp_path / "home", stop_timeout=0.0)
    with pytest.raises(md.DaemonError, match="did not stop"):
        daemon.ensure()
    assert not hasattr(daemon, "_kill")


def test_a_daemon_that_survives_stop_blocks_a_new_start(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path, stop_works=False)
    daemon = fake.make(tmp_path / "home", stop_timeout=0.0)
    assert daemon.ensure() == DAEMON_PID
    fake.listening_on.clear()  # ours and alive, but not serving
    with pytest.raises(md.DaemonError, match="did not stop"):
        daemon.ensure()
    assert len(fake.opens()) == 1  # never a second, untracked daemon


def test_the_real_supervisor_on_macos_uses_the_fenced_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_mod, "current_os", lambda: install_mod.OS_MACOS)
    sup = DriverSupervisor(tmp_path / "home")
    assert isinstance(sup._mac, md.MacDaemon)


def test_a_daemon_of_another_binary_is_unproven(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    real_run = fake.run

    def run(argv: list[str], *, timeout: float, env: Any) -> "subprocess.CompletedProcess[str]":
        res = real_run(argv, timeout=timeout, env=env)
        if argv[0] == md.OPEN and argv[1:] != ["-h"]:
            rec = fake.records[DAEMON_PID]
            fake.records[DAEMON_PID] = md.ProcArgs("/tmp/other/cua-driver", rec.argv, rec.env)
        return res

    fake.run = run  # type: ignore[method-assign,assignment]
    daemon._run = run
    with pytest.raises(md.DaemonError, match="not the verified"):
        daemon.ensure()


def test_a_stale_pid_file_and_socket_are_reclaimed(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    daemon.pid_path.parent.mkdir(parents=True, mode=0o700)
    daemon.pid_path.write_text("999")  # a pid that is not running
    daemon.socket_path.write_text("")  # a dead socket file left behind
    assert daemon.ensure() == DAEMON_PID
    assert fake.stops() == []
    assert len(fake.opens()) == 1


def test_a_socket_another_process_serves_is_left_alone(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    fake.foreign[str(daemon.socket_path)] = (md.SOCK_LIVE, 999)
    with pytest.raises(md.DaemonError, match="another process"):
        daemon.ensure()
    assert fake.opens() == [] and fake.stops() == []


def test_a_socket_that_cannot_be_probed_is_never_unlinked(tmp_path: Path) -> None:
    # A timeout or a full backlog is not proof the socket is dead.
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    daemon.pid_path.parent.mkdir(parents=True, mode=0o700)
    daemon.socket_path.write_text("")
    fake.foreign[str(daemon.socket_path)] = (md.SOCK_UNKNOWN, None)
    with pytest.raises(md.DaemonError, match="another process"):
        daemon.ensure()
    assert daemon.socket_path.exists() and fake.opens() == []


def test_a_socket_served_by_another_pid_is_not_our_daemon(tmp_path: Path) -> None:
    # The pid file names our proved daemon, but the endpoint answering on the
    # socket is a different process: never accepted.
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home", start_timeout=0.0)
    real_run = fake.run

    def run(argv: list[str], *, timeout: float, env: Any) -> "subprocess.CompletedProcess[str]":
        res = real_run(argv, timeout=timeout, env=env)
        if argv[0] == md.OPEN and argv[1:] != ["-h"]:
            fake.peer[str(daemon.socket_path)] = 999
        return res

    daemon._run = run
    with pytest.raises(md.DaemonError, match="did not start"):
        daemon.ensure()


def test_stop_targets_only_our_pid(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    daemon.pid_path.parent.mkdir(parents=True, mode=0o700)
    # The pid file names a live process that is NOT a serve on our socket
    # (pid reuse, or another tool): it is never asked to stop.
    fake.alive.add(321)
    fake.records[321] = md.ProcArgs("/usr/bin/other", ("other", "serve", "--socket", "/x"), {})
    daemon.pid_path.write_text("321")
    daemon.stop()
    assert fake.stops() == [] and 321 in fake.alive
    # Our own daemon is stopped with its expected pid.
    assert daemon.ensure() == DAEMON_PID
    daemon.stop()
    assert fake.stops()[-1][-2:] == ["--expected-pid", str(DAEMON_PID)]
    assert DAEMON_PID not in fake.alive
    assert not daemon.pid_path.exists()


def test_a_running_proven_daemon_of_ours_is_reused(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    assert daemon.ensure() == DAEMON_PID
    assert daemon.ensure() == DAEMON_PID
    assert len(fake.opens()) == 1 and fake.stops() == []


def test_the_socket_lives_only_in_the_hidden_ipc_leaf(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert md.run_dir(home) == home / md.IPC_LEAF / md.RUN_DIR_NAME
    assert md.MacDaemon(home).socket_path == home / md.IPC_LEAF / md.RUN_DIR_NAME / md.SOCKET_NAME
    # No cache-directory fallback exists any more.
    src = Path(md.__file__).read_text(encoding="utf-8")
    assert "Library/Caches" not in src and '"Caches"' not in src


def test_a_socket_path_that_cannot_fit_refuses_instead_of_moving(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / ("x" * 120), ipc_dir=tmp_path / ("y" * 120) / md.RUN_DIR_NAME)
    with pytest.raises(md.DaemonError, match="longer than macOS allows"):
        daemon.ensure()
    assert fake.opens() == [] and fake.calls == []
    assert not (tmp_path / ("y" * 120)).exists()


def test_the_ipc_leaf_is_hidden_from_the_sandbox_like_the_harness_ipc() -> None:
    from kiro_crew import sandbox_leaves, sandbox_precreate
    from kiro_crew.security import paths_lists

    for module in (sandbox_leaves, sandbox_precreate, paths_lists):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert f'"{md.IPC_LEAF}"' in text, module.__name__
    assert md.IPC_LEAF in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES


def test_stop_runs_the_sealed_copy_never_the_applications_binary(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    daemon.ensure()
    daemon.stop()
    (stop,) = fake.stops()
    assert stop[0] == str(SEALED)
    assert all(str(fake.app) not in c[0] for c in fake.calls)


def test_a_sealed_copy_that_fails_its_hash_runs_nothing(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    daemon.ensure()
    calls = len(fake.calls)

    def tampered() -> Path:
        raise install_mod.InstallError(
            "verifying", "the installed driver does not match its pinned hash"
        )

    daemon._driver_binary = tampered  # type: ignore[method-assign]
    with pytest.raises(md.DaemonError, match="pinned hash"):
        daemon.stop()
    with pytest.raises(md.DaemonError, match="pinned hash"):
        daemon.proxy_argv()
    assert len(fake.calls) == calls  # nothing was executed
    assert DAEMON_PID in fake.alive


def test_an_applications_copy_that_fails_its_checks_is_never_launched(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    fake.app_problem = "its driver binary is not the pinned 0.47.8"
    daemon = fake.make(tmp_path / "home")
    with pytest.raises(md.DaemonError, match="failed its checks"):
        daemon.ensure()
    assert fake.opens() == [[md.OPEN, "-h"]] or all("--args" not in c for c in fake.opens())
    assert DAEMON_PID not in fake.alive


def test_the_default_app_check_never_executes_the_applications_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []

    def check(app: Path, pinned: Any, home: Any, **kw: Any) -> str:
        seen.append({"app": app, **kw})
        return ""

    monkeypatch.setattr(install_mod, "check_app_copy", check)
    daemon = md.MacDaemon(tmp_path / "home", app=tmp_path / "CuaDriver.app")
    assert daemon._check_app() == ""
    assert seen == [{"app": tmp_path / "CuaDriver.app", "check_running_version": False}]


def test_no_driver_exec_in_driver_runtime_uses_the_applications_binary() -> None:
    """Every call that executes something in ``driver_runtime`` (and setup) builds its
    argv from a fixed system tool, the verified-exec helper, or a binary the helper
    returned. The /Applications binary appears in no argv, and ``app_binary()`` is
    read only by the fence proof, which compares, never executes."""
    import ast

    root = Path(install_mod.__file__).parent
    files = [*sorted(root.glob("*.py")), root.parent / "setup.py"]
    exec_names = {"run", "Popen", "_run", "_spawn", "runner", "_run_tool", "check_output", "call"}
    offenders: list[str] = []
    app_binary_users: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if name == "app_binary":
                        app_binary_users.append(f"{path.name}:{fn.name}")
                    if name in exec_names and node.args:
                        text = ast.unparse(node.args[0])
                        if (
                            "app_binary" in text
                            or "MACOS_APP_BINARY" in text
                            or "applications_app" in text
                        ):
                            offenders.append(f"{path.name}:{node.lineno} {text}")
    assert offenders == []
    assert sorted(set(app_binary_users)) == ["macos_daemon.py:_fence_problem"]
    # The daemon module's own exec sites: open (a fixed system tool) and the helper.
    daemon_src = (root / "macos_daemon.py").read_text(encoding="utf-8")
    tree = ast.parse(daemon_src)
    run_args = [
        ast.unparse(n.args[0])
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "_run" and n.args
    ]
    assert sorted(run_args) == sorted(["[OPEN, '-h']", "self.launch_argv()", "argv"])
    assert "argv = self._driver_command(" in daemon_src


def test_procargs2_parser() -> None:
    buf = (
        (3).to_bytes(4, "little")
        + b"/Applications/CuaDriver.app/Contents/MacOS/cua-driver\0\0\0\0"
        + b"cua-driver\0serve\0--socket\0"
        + b"DO_NOT_TRACK=1\0CUA_HOME=/h\0\0\0"
    )
    rec = md.parse_procargs2(buf)
    assert rec.exec_path.endswith("MacOS/cua-driver")
    assert rec.argv == ("cua-driver", "serve", "--socket")
    assert rec.env == {"DO_NOT_TRACK": "1", "CUA_HOME": "/h"}
    with pytest.raises(ValueError):
        md.parse_procargs2(b"\1")
    dup = md.parse_procargs2(
        (1).to_bytes(4, "little") + b"/x\0\0a\0DO_NOT_TRACK=0\0DO_NOT_TRACK=1\0\0"
    )
    assert dup.env["DO_NOT_TRACK"] == "0"  # the first wins, as getenv sees it
    assert dup.duplicates == frozenset({"DO_NOT_TRACK"})


def test_a_pid_file_with_a_non_ascii_digit_is_no_pid(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    daemon.pid_path.parent.mkdir(parents=True, mode=0o700)
    daemon.pid_path.write_text("\u00b2", encoding="utf-8")
    assert daemon._read_pid() is None


# ── the supervisor on macOS ──


def _mac_supervisor(tmp_path: Path, fake: FakeMac) -> "tuple[DriverSupervisor, list[tuple]]":
    spawned: list[tuple] = []
    log = tmp_path / "calls.jsonl"

    def spawn(argv: list[str], env: Any, cwd: str) -> "subprocess.Popen[bytes]":
        spawned.append((list(argv), dict(env)))
        # The real proxy argv is [binary, "mcp", "--socket", sock]; the fake
        # driver ignores the flags it does not know.
        return sup_mod._default_spawn(
            [sys.executable, str(FAKE), *argv[1:], "--log", str(log)], env, cwd
        )

    daemon = fake.make(tmp_path / "home")
    sup = DriverSupervisor(
        tmp_path / "home",
        resolve_binary=lambda: (Path("/opt/cua-driver"), ""),
        spawn=spawn,
        mac_daemon=daemon,
        backoff=(0.0,),
        base_env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    return sup, spawned


def test_the_supervisor_never_spawns_plain_mcp_on_macos(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    sup, spawned = _mac_supervisor(tmp_path, fake)
    try:
        sup.call("list_windows", {})
    finally:
        sup.stop()
    ((argv, env),) = spawned
    assert argv == ["/opt/cua-driver", "mcp", "--socket", str(fake.daemon.socket_path)]  # type: ignore[union-attr]
    assert env[md.EMBEDDED_ENV] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    # Shutdown stopped our daemon by its pid.
    assert fake.stops()[-1][-2:] == ["--expected-pid", str(DAEMON_PID)]


def test_the_supervisor_refuses_when_the_fence_is_unproven(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path, env_ok=False)
    sup, spawned = _mac_supervisor(tmp_path, fake)
    with pytest.raises(DriverUnavailable, match="could not prove"):
        sup.call("list_windows", {})
    assert spawned == []
    assert DAEMON_PID not in fake.alive
    assert "could not prove" in json.dumps(sup.status())


def test_a_substituted_endpoint_after_the_proxy_connects_refuses(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    sup, _spawned = _mac_supervisor(tmp_path, fake)
    real_spawn = sup._spawn

    def spawn(argv: list[str], env: Any, cwd: str) -> "subprocess.Popen[bytes]":
        # Between the proof and the proxy's own connection, someone else takes
        # over the socket.
        fake.peer[str(fake.daemon.socket_path)] = 999  # type: ignore[union-attr]
        return real_spawn(argv, env, cwd)

    sup._spawn = spawn
    with pytest.raises(DriverUnavailable, match="no longer served"):
        sup.call("list_windows", {})
    assert DAEMON_PID not in fake.alive


def test_restart_stops_our_daemon_so_new_grants_apply(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)
    sup, _spawned = _mac_supervisor(tmp_path, fake)
    try:
        sup.call("list_windows", {})
        sup.restart()
        assert DAEMON_PID not in fake.alive
        sup.call("list_windows", {})
        assert len(fake.opens()) == 2
    finally:
        sup.stop()


def test_probe_socket_tells_absent_stale_live_and_the_peer_pid() -> None:
    import socket

    root = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "cud-", dir=short_tmp_base()))
    _CACHES.append(root)
    path = root / "s.sock"
    assert md.probe_socket(path) == (md.SOCK_ABSENT, None)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        state, peer = md.probe_socket(path)
        assert state == md.SOCK_LIVE
        if hasattr(socket, "SO_PEERCRED") or sys.platform == "darwin":
            assert peer == os.getpid()
    finally:
        server.close()
    assert md.probe_socket(path) == (md.SOCK_STALE, None)
    path.unlink()
    path.write_text("not a socket")
    assert md.probe_socket(path) == (md.SOCK_UNKNOWN, None)


def test_the_daemon_never_runs_its_own_permission_gate(tmp_path: Path) -> None:
    # The upstream gate opens System Settings on every start; Kiro Crew prompts
    # only from a setup action the owner clicks.
    fake = FakeMac(tmp_path)
    daemon = fake.make(tmp_path / "home")
    daemon.ensure()
    (argv,) = fake.opens()
    serve = argv[argv.index("--args") + 1 :]
    assert serve[0] == "serve" and serve[-1] == md.NO_PERMISSIONS_GATE


def test_no_daemon_or_tool_path_prompts_or_opens_system_settings(tmp_path: Path) -> None:
    """Only ``request_permissions`` (one click) asks the driver to prompt."""
    import ast

    from kiro_crew.computer_use import setup as setup_mod

    root = Path(md.__file__).parent
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "x-apple.systempreferences" not in source, path
        assert '"prompt": True' not in source and "'prompt': True" not in source, path
    tree = ast.parse(Path(setup_mod.__file__).read_text(encoding="utf-8"))
    prompting = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(
            k.arg == "prompt" and getattr(k.value, "value", None) is True for k in node.keywords
        )
    ]
    assert len(prompting) == 1  # the request_permissions branch of run_setup_action


def test_no_daemon_is_launched_when_the_sealed_copy_that_stops_it_fails(tmp_path: Path) -> None:
    fake = FakeMac(tmp_path)

    def tampered() -> Path:
        raise install_mod.InstallError(
            "verifying", "the installed driver does not match its pinned hash"
        )

    daemon = fake.make(tmp_path / "home", driver_binary=tampered)
    with pytest.raises(md.DaemonError, match="pinned hash"):
        daemon.ensure()
    assert fake.opens() == []
    assert fake.alive == set()
