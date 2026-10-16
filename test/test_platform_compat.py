"""Unit tests for kiro_crew.platform_compat — the cross-platform shim that lets
KiroCrew run natively on Windows alongside macOS/Linux.

These exercise the PURE / platform-dispatching surface, spawning a real process
only where the contract IS an OS behavior (process-session semantics): the
signal constants, the file-lock context managers (POSIX path on
this host; the Windows branch is asserted via its dispatch shape), the
strftime directive translation (the one piece with a deterministic Windows
output we can assert directly), and the process-helper return contracts.

Ten topic siblings, ``test_platform_compat_<topic>.py``, hold process identity,
the process tree, Windows process trees, kill and reap, owner-only permissions,
filesystem primitives, resource readings, trusted binaries, pinned host tools and
port listeners. This file keeps the platform flags, the runtime and console
seams, the file lock and the no-replace rename, the process helpers, the TCP peer
and listening-pid lookups and the RSS reading.
"""

from __future__ import annotations

import ctypes
import errno
import io
import json
import mmap
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc

# Tests here read or build the macOS libc and libproc handles, so each one starts
# with both caches unbuilt and puts them back after (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"}, reason="supported kernel identity contract"
)
@pytest.mark.parametrize(
    "family, host",
    [
        (socket.AF_INET, "127.0.0.1"),
        pytest.param(socket.AF_INET6, "::1", marks=pytest.mark.ipv6_required),
    ],
)
def test_native_tcp_peer_identifies_client_process_not_server(family, host):
    with socket.socket(family) as listener:
        listener.settimeout(10)
        listener.bind((host, 0))
        listener.listen()
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,socket,sys; s=socket.socket(int(sys.argv[1])); "
                "s.connect((sys.argv[2],int(sys.argv[3]))); print(os.getpid(),flush=True); "
                "sys.stdin.read(1)",
                str(int(family)),
                host,
                str(listener.getsockname()[1]),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            accepted, _ = listener.accept()
            # A Windows venv launcher may exec a different interpreter PID.
            assert child.stdout is not None
            client_pid = int(child.stdout.readline())
            with accepted:
                server = accepted.getsockname()[:2]
                client = accepted.getpeername()[:2]
                assert pc.get_tcp_peer_pid(server, client) == client_pid
                start = pc.get_process_start_id(client_pid)
                assert start and start == pc.get_process_start_id(client_pid)
                assert start != pc.get_process_start_id(os.getpid())
                assert pc.get_tcp_peer_pid((server[0], server[1] % 65535 + 1), client) is None
                assert pc.get_tcp_peer_pid(("203.0.113.1", server[1]), client) is None
        finally:
            child.communicate(b"x", timeout=10)


def test_tcp_peer_identity_unavailable_on_unknown_platform(monkeypatch):
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc.sys, "platform", "unsupported")
    assert pc.get_tcp_peer_pid(("127.0.0.1", 1000), ("127.0.0.1", 2000)) is None


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("scenario", ["match", "other_port", "ambiguous", "unreadable"])
def test_macos_tcp_peer_requires_an_exact_unique_connection(monkeypatch, host, scenario):
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc.sys, "platform", "darwin")
    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: "/usr/sbin/lsof")
    rendered = f"[{host}]" if ":" in host else host
    port = 2001 if scenario == "other_port" else 2000
    data = f"p2468\nn{rendered}:{port}->{rendered}:1000\n"
    if scenario == "ambiguous":
        data += f"p9753\nn{rendered}:{port}->{rendered}:1000\n"

    def query(argv, **kwargs):
        assert argv == ["/usr/sbin/lsof", "-nP", "-a", "-iTCP:2000", "-sTCP:ESTABLISHED", "-Fpn"]
        assert kwargs["timeout"] == 2
        if scenario == "unreadable":
            raise subprocess.TimeoutExpired(argv, 2)
        return data.encode("ascii")

    monkeypatch.setattr(subprocess, "check_output", query)
    assert pc.get_tcp_peer_pid((host, 1000), (host, 2000)) == (
        2468 if scenario == "match" else None
    )


class TestPlatformFlags:
    def test_flags_are_mutually_consistent(self):
        # Exactly one of POSIX / Windows is true, and they're the negation of
        # each other — the whole module branches on this.
        assert pc.IS_POSIX == (not pc.IS_WINDOWS)
        assert pc.IS_WINDOWS == (sys.platform == "win32")
        assert pc.IS_LINUX == (sys.platform == "linux")

    def test_signal_constants_present_on_every_platform(self):
        # SIGKILL is undefined on Windows; the shim must still expose an int so
        # callers (kill_pid/kill_process_tree) never AttributeError.
        assert isinstance(pc.SIGKILL, int) and pc.SIGKILL > 0
        assert isinstance(pc.SIGTERM, int) and pc.SIGTERM > 0


class TestReexecPythonModule:
    def test_windows_uses_space_free_argv0(self, monkeypatch, nonbundled_python_without_user_site):
        executable = (
            r"C:\Users\alice\AppData\Local\Programs\KiroCrew Nightly"
            r"\resources\backend-dist\kirocrew-backend\python.exe"
        )
        calls = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.sys, "executable", executable)
        monkeypatch.setattr(pc.os, "execv", lambda path, argv: calls.append((path, argv)))
        monkeypatch.setenv("PYTHONUTF8", "0")
        monkeypatch.setenv("PYTHONIOENCODING", "cp1252")

        pc.reexec_python_module("kiro_crew", ["gateway", "--port", "5476"])

        assert calls == [
            (
                executable,
                ["python.exe", "-s", "-P", "-m", "kiro_crew", "gateway", "--port", "5476"],
            )
        ]
        assert os.environ["PYTHONUTF8"] == "1"
        assert os.environ["PYTHONIOENCODING"] == "utf-8:backslashreplace"

    def test_posix_preserves_full_argv0_and_pins_utf8(
        self, monkeypatch, nonbundled_python_without_user_site
    ):
        executable = "/opt/Kiro Crew/bin/python3"
        calls = []
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc.sys, "executable", executable)
        monkeypatch.setattr(pc.os, "execv", lambda path, argv: calls.append((path, argv)))
        monkeypatch.setenv("PYTHONUTF8", "0")
        monkeypatch.setenv("PYTHONIOENCODING", "latin-1")

        pc.reexec_python_module("kiro_crew", ["gateway"])

        assert calls == [(executable, [executable, "-s", "-P", "-m", "kiro_crew", "gateway"])]
        assert os.environ["PYTHONUTF8"] == "1"
        assert os.environ["PYTHONIOENCODING"] == "utf-8:backslashreplace"

    def test_reexec_successor_survives_hostile_parent_encoding(self, tmp_path):
        """Exercise the real failure shape behind desktop in-app restarts.

        The first interpreter intentionally starts with cp1252 streams on every
        OS.  It re-execs without calling ensure_utf8_console, so only the
        environment published by reexec_python_module can make the successor's
        first emoji print safe.
        """
        probe = tmp_path / "utf8_reexec_probe.py"
        probe.write_text(
            "import os\n"
            "from kiro_crew.platform_compat import reexec_python_module\n"
            "if os.environ.get('_KIROCREW_UTF8_REEXEC_PROBE') == '1':\n"
            "    print('👻 restarted')\n"
            "else:\n"
            "    os.environ['_KIROCREW_UTF8_REEXEC_PROBE'] = '1'\n"
            "    reexec_python_module('utf8_reexec_probe', [])\n",
            encoding="utf-8",
        )
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        inherited_path = os.environ.get("PYTHONPATH", "")
        # The probe directory rides on PYTHONPATH, not on the cwd: the re-exec
        # passes -P, which keeps the successor's cwd off sys.path, so a probe
        # found only through the cwd would vanish on the second hop.
        env = {
            **os.environ,
            "PYTHONUTF8": "0",
            "PYTHONIOENCODING": "cp1252",
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(tmp_path), source_root, inherited_path) if p
            ),
        }

        result = subprocess.run(
            [sys.executable, "-m", "utf8_reexec_probe"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            timeout=15,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert "👻 restarted".encode() in result.stdout


class TestCancelWheelAppliesInFlight:
    """Every exit path cancels a managed-venv apply through ONE lookup, not a registry.

    The apply module is found in ``sys.modules`` and never imported, so an exit
    in a process that never loaded it costs nothing and imports nothing from a
    tree an update may have changed.
    """

    _MODULE = "kiro_crew.platform.wheel_apply"

    def _stub_module(self, monkeypatch, cancel) -> None:
        stub = types.ModuleType(self._MODULE)
        stub.cancel_wheel_applies = cancel  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, self._MODULE, stub)

    def test_calls_the_loaded_module_with_the_reason(self, monkeypatch):
        reasons: list[str] = []
        self._stub_module(monkeypatch, reasons.append)

        pc.cancel_wheel_applies_in_flight("shutdown")

        assert reasons == ["shutdown"]

    def test_does_nothing_and_imports_nothing_when_the_module_is_not_loaded(self, monkeypatch):
        monkeypatch.delitem(sys.modules, self._MODULE, raising=False)

        pc.cancel_wheel_applies_in_flight("exit")

        assert self._MODULE not in sys.modules

    @pytest.mark.parametrize("seam", ["reexec_launcher", "reexec_python_module", "hard_exit"])
    def test_every_exit_path_cancels_first_and_survives_a_raising_cancel(
        self, monkeypatch, tmp_path, seam, nonbundled_python_without_user_site
    ):
        """The cancel runs before the exec / ``os._exit``; one that raises never stops it."""
        order: list[str] = []

        def cancel(reason: str) -> None:
            order.append(f"cancel:{reason}")
            raise RuntimeError("the apply's cancel blew up")

        self._stub_module(monkeypatch, cancel)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc.os, "execv", lambda path, argv: order.append("execv"))
        monkeypatch.setattr(pc.os, "_exit", lambda code: order.append(f"_exit:{code}"))

        if seam == "hard_exit":
            pc.hard_exit(3)
            assert order == ["cancel:exit", "_exit:3"]
        elif seam == "reexec_launcher":
            pc.reexec_launcher(str(tmp_path / "kirocrew"), ["gateway"])
            assert order == ["cancel:exec", "execv"]
        else:
            pc.reexec_python_module("kiro_crew", ["gateway"], executable=str(tmp_path / "python"))
            assert order == ["cancel:exec", "execv"]


class TestWindowsOnArm:
    """``is_windows_on_arm`` answers "will pip accept a win_amd64 wheel here?".

    Callers use it to refuse a package that publishes no win-arm64 wheel, so the
    predicate has to be a property of the running PROCESS rather than of the host
    CPU — the two disagree under Windows' x86-64 emulation, and only the process
    answer matches what pip does.
    """

    def test_true_for_a_native_arm64_interpreter(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.platform, "machine", lambda: "ARM64")
        assert pc.is_windows_on_arm() is True

    def test_accepts_the_aarch64_spelling(self, monkeypatch):
        # Reaches Windows through cross-built and MSYS/Cygwin interpreters.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.platform, "machine", lambda: "aarch64")
        assert pc.is_windows_on_arm() is True

    def test_case_is_irrelevant(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.platform, "machine", lambda: "aRm64")
        assert pc.is_windows_on_arm() is True

    def test_false_for_an_emulated_x86_64_interpreter(self, monkeypatch):
        """The case a host-architecture probe would get WRONG.

        Windows on ARM runs x86-64 processes under emulation, and such an
        interpreter reports AMD64 and installs win_amd64 wheels perfectly well.
        Reporting it as ARM would refuse a package that works.
        """
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.platform, "machine", lambda: "AMD64")
        assert pc.is_windows_on_arm() is False

    def test_false_on_apple_silicon(self, monkeypatch):
        # arm64 alone must not trip it: macOS and Linux both publish arm64 wheels
        # for the packages this gate exists to refuse on Windows.
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc.platform, "machine", lambda: "arm64")
        assert pc.is_windows_on_arm() is False

    def test_does_not_consult_machine_off_windows(self, monkeypatch):
        """Short-circuits on the platform constant.

        Keeps the predicate loop-safe for the dashboard's STT config GET, which
        calls it inline rather than from the threaded probe block.
        """
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        calls = []

        def _machine():
            calls.append(1)
            return "arm64"

        monkeypatch.setattr(pc.platform, "machine", _machine)
        assert pc.is_windows_on_arm() is False
        assert calls == []

    def test_uses_the_modules_own_windows_predicate(self, monkeypatch):
        """Keyed off IS_WINDOWS, not a second platform.system() call.

        Two Windows predicates in one module can drift; this pins that there is
        one. Flipping only IS_WINDOWS must flip the answer.
        """
        monkeypatch.setattr(pc.platform, "machine", lambda: "arm64")
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc.is_windows_on_arm() is True
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.is_windows_on_arm() is False


class TestFileLock:
    def test_exclusive_lock_round_trips(self, tmp_path):
        # The lock must acquire + release cleanly and run the body, on whatever
        # platform the test runs (POSIX flock here; msvcrt on Windows CI).
        lock = tmp_path / ".test.lock"
        lock.write_text("")
        ran = False
        with open(lock, "r+") as fh:
            with pc.file_lock(fh.fileno(), exclusive=True):
                ran = True
        assert ran

    def test_shared_lock_round_trips(self, tmp_path):
        lock = tmp_path / ".test-sh.lock"
        lock.write_text("")
        with open(lock, "r") as fh:
            with pc.file_lock(fh.fileno(), exclusive=False):
                pass  # no exception = pass

    def test_flock_exclusive_alias_runs_body(self, tmp_path):
        lock = tmp_path / ".test-ex.lock"
        lock.write_text("")
        seen = []
        with open(lock, "w") as fh:
            with pc.flock_exclusive(fh.fileno()):
                seen.append(1)
        assert seen == [1]

    def test_acquire_release_pair(self, tmp_path):
        # The fd-handoff form (cron_history) — acquire now, release later.
        lock = tmp_path / ".test-pair.lock"
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            pc.acquire_lock(fd, exclusive=True)
            pc.release_lock(fd)
        finally:
            os.close(fd)

    def test_try_acquire_lock_succeeds_on_free_file(self, tmp_path):
        lock = tmp_path / ".test-try.lock"
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            assert pc.try_acquire_lock(fd, exclusive=False) is True
            pc.release_lock(fd)
        finally:
            os.close(fd)


class TestRenameNoReplace:
    @pytest.mark.skipif(
        not pc.RENAME_NOREPLACE_AVAILABLE,
        reason="native atomic no-replace rename is unavailable",
    )
    def test_rename_is_atomic_and_preserves_an_existing_destination(self, tmp_path):
        first = tmp_path / "first"
        first.mkdir()
        (first / "payload").write_text("published")
        parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            pc.rename_noreplace("first", "published", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            assert not first.exists()
            assert (tmp_path / "published" / "payload").read_text() == "published"

            losing = tmp_path / "losing"
            losing.mkdir()
            destination = tmp_path / "occupied"
            destination.mkdir(mode=0o700)
            before = destination.stat()
            with pytest.raises(FileExistsError):
                pc.rename_noreplace(
                    "losing",
                    "occupied",
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            after = destination.stat()
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            assert losing.is_dir()
        finally:
            os.close(parent_fd)

    def test_unavailable_native_contract_fails_closed(self, monkeypatch):
        monkeypatch.setattr(pc, "_RENAME_NOREPLACE_FN", None)
        with pytest.raises(NotImplementedError):
            pc.rename_noreplace("source", "target", src_dir_fd=-1, dst_dir_fd=-1)

    def test_syscall_fallback_builds_working_callable_on_known_arch(self, tmp_path, monkeypatch):
        # Verify marshalling on every host. Linux additionally exercises the
        # native syscall, independent of which libc path import-time chose.
        host_machine = pc.platform.machine()
        calls = []

        def syscall(*args):
            calls.append(args)
            ctypes.set_errno(errno.EEXIST)
            return -1

        for machine, number in pc._SYS_RENAMEAT2_BY_MACHINE.items():
            with monkeypatch.context() as patcher:
                patcher.setattr(pc.platform, "machine", lambda: machine)
                fn = pc._build_renameat2_via_syscall(types.SimpleNamespace(syscall=syscall))
                assert fn is not None
                assert fn(11, b"first", 12, b"published", 1) == -1
                assert calls[-1] == (number, 11, b"first", 12, b"published", 1)
                assert syscall.restype is ctypes.c_long
                assert syscall.argtypes == [
                    ctypes.c_long,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                assert ctypes.get_errno() == errno.EEXIST
        assert len(calls) == len(pc._SYS_RENAMEAT2_BY_MACHINE)
        if not pc.IS_LINUX or host_machine not in pc._SYS_RENAMEAT2_BY_MACHINE:
            # Portable marshalling above still runs; never issue a Linux
            # syscall against another OS or guess an unmapped syscall number.
            return
        libc = ctypes.CDLL(None, use_errno=True)
        fn = pc._build_renameat2_via_syscall(libc)
        assert fn is not None

        first = tmp_path / "first"
        first.mkdir()
        (first / "payload").write_text("published")
        parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            rc = fn(parent_fd, b"first", parent_fd, b"published", 1)  # RENAME_NOREPLACE
            assert rc == 0
            assert (tmp_path / "published" / "payload").read_text() == "published"

            # Occupied destination must refuse (EEXIST), not clobber.
            (tmp_path / "loser").mkdir()
            ctypes.set_errno(0)
            rc = fn(parent_fd, b"loser", parent_fd, b"published", 1)
            assert rc != 0
            assert ctypes.get_errno() == errno.EEXIST
        finally:
            os.close(parent_fd)

    def test_syscall_fallback_returns_none_on_unknown_arch(self, monkeypatch):
        # An unmapped architecture must fail closed rather than issue a
        # wrong-numbered syscall.
        monkeypatch.setattr(pc.platform, "machine", lambda: "totally-made-up-arch")

        class UnavailableLibc:
            def __getattr__(self, name):
                raise AssertionError(f"unknown architecture must not access libc.{name}")

        assert pc._build_renameat2_via_syscall(UnavailableLibc()) is None


class TestProcessHelpers:
    def test_pid_exists_true_for_self(self):
        # The current process obviously exists — on POSIX via os.kill(0), on
        # Windows via OpenProcess.
        assert pc.pid_exists(os.getpid()) is True

    def test_pid_exists_false_for_unused_pid(self):
        # A very high PID is almost certainly not live on any test host.
        assert pc.pid_exists(2_000_000_000) is False

    def test_pid_exists_false_after_kill_even_while_handle_open(self):
        # Windows OpenProcess succeeds for an EXITED process while any handle to
        # it is open (asyncio's transport keeps one until GC). pid_exists must
        # still report False via GetExitCodeProcess, or every session recycle
        # logs a false "PID survived kill" and leaks a dead PID into the tracker.
        # On POSIX this reaps normally and is equally False.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert pc.pid_exists(child.pid) is True
            child.kill()
            child.wait()  # reap; the Popen keeps its OS handle referenced here
            assert pc.pid_exists(child.pid) is False
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_pid_is_zombie_reads_the_running_state_of_self(self):
        # A running process is not a zombie on the platforms that expose the
        # state (Linux /proc, macOS kinfo); elsewhere the answer is "unknown".
        expected = False if sys.platform in ("linux", "darwin") else None
        assert pc.pid_is_zombie(os.getpid()) is expected

    def test_pid_is_zombie_is_unknown_for_an_unreadable_or_invalid_pid(self):
        assert pc.pid_is_zombie(0) is None
        assert pc.pid_is_zombie(-1) is None
        if sys.platform == "linux":
            # No /proc entry: unreadable, not "not a zombie".
            assert pc.pid_is_zombie(2_000_000_000) is None

    def test_pid_is_zombie_reads_the_linux_stat_state_field(self, monkeypatch, tmp_path):
        # The comm field is parenthesised and may itself contain spaces and
        # parentheses, so the state is the first field after the LAST ')'.
        tail = " ".join(str(i) for i in range(4, 24))
        real_path = pc.Path
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "Path", lambda p: tmp_path if p == "/proc" else real_path(p))
        (tmp_path / "4242").mkdir()
        stat = tmp_path / "4242" / "stat"
        for state, expected in (("Z", True), ("X", True), ("x", True), ("S", False), ("R", False)):
            stat.write_bytes(f"4242 (kiro (cli) worker) {state} 1 {tail}".encode())
            assert pc.pid_is_zombie(4242) is expected, state

        stat.unlink()
        assert pc.pid_is_zombie(4242) is None

    def test_kill_process_group_signals_the_captured_id_and_resolves_nothing(self, monkeypatch):
        # The caller hands over a group id it captured while the leader was alive;
        # the primitive addresses THAT id -- no pid is consulted, so a recycled pid
        # cannot redirect the signal. The POSIX branch, on every platform: the
        # branch flag is pinned and the two group syscalls are supplied through
        # the seams the primitive reads (created where the runner lacks them).
        signalled: list[tuple[int, int]] = []
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(
            os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)), raising=False
        )
        monkeypatch.setattr(
            os,
            "getpgid",
            lambda _pid: pytest.fail("kill_process_group resolved a group from a pid"),
            raising=False,
        )

        assert pc.kill_process_group(2**22 + 4242, pc.SIGKILL) is True

        assert signalled == [(2**22 + 4242, pc.SIGKILL)]

    def test_kill_process_group_refuses_broadcast_and_self_instead_of_degrading(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(
            os, "killpg", lambda pgid, sig: pytest.fail(f"signalled group {pgid}"), raising=False
        )
        for refused in (0, 1, -1, pc._OWN_PGID, "4242", 4242.0):
            with pytest.raises(ValueError, match="refusing broadcast/self process group"):
                pc.kill_process_group(refused, pc.SIGKILL)  # type: ignore[arg-type]

    def test_kill_process_group_lets_the_signal_s_errors_propagate(self, monkeypatch):
        def _gone(pgid, sig):
            raise ProcessLookupError("[Errno 3] No such process")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(os, "killpg", _gone, raising=False)
        with pytest.raises(ProcessLookupError):
            pc.kill_process_group(2**22 + 4343, pc.SIGKILL)

    def test_kill_process_group_is_posix_only(self, monkeypatch):
        # The other branch: no group syscall is reached, whatever the runner has.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(
            os, "killpg", lambda pgid, sig: pytest.fail(f"signalled group {pgid}"), raising=False
        )
        with pytest.raises(OSError, match="no POSIX process groups"):
            pc.kill_process_group(2**22 + 4444, pc.SIGKILL)

    def test_get_ppid_returns_int(self):
        # Returns the parent (>0 normally) or -1 on failure — never raises.
        ppid = pc.get_ppid(os.getpid())
        assert isinstance(ppid, int)

    def test_kill_pid_nonexistent_is_safe(self, monkeypatch):
        # Both platforms raise ProcessLookupError on a non-existent pid, so
        # callers' ``except (ProcessLookupError, OSError)`` handlers fire
        # uniformly. POSIX: os.kill raises it. Windows: taskkill's rc=128 is
        # re-badged to it by _raise_taskkill_error.
        #
        # On POSIX this is a REAL SIGKILL, so "nonexistent" has to hold by
        # construction, not by luck: any number inside the kernel's pid range
        # can be handed to an unrelated process between the premise and the
        # signal. Linux caps pids at PID_MAX_LIMIT (4194304) and macOS at
        # PID_MAX (99998), so a probe pid above both is refused by the range
        # check itself and never resolves to a process. Pinned against the
        # running host where the ceiling is readable, so a kernel that raised
        # it would fail here rather than turn this test into a kill at whatever
        # holds that number.
        #
        # Windows exposes no readable pid ceiling, so no pid can be proven
        # unused there and a real ``taskkill /F`` at this number could stop an
        # unrelated process. The Windows arm therefore runs the same call with
        # ``taskkill`` stubbed to the rc=128 it returns for a missing pid, so the
        # contract stays asserted on the Windows shard without the real kill.
        nonexistent = 2_000_000_000
        if pc.IS_WINDOWS:
            recorded: list[list[str]] = []

            def _taskkill_not_found(argv, *_a, **_kw):
                recorded.append(list(argv))
                return types.SimpleNamespace(returncode=128, stdout=b"", stderr=b"not found")

            monkeypatch.setattr(pc.subprocess, "run", _taskkill_not_found)
        elif pc.IS_LINUX:
            assert nonexistent > int(Path("/proc/sys/kernel/pid_max").read_text())
        with pytest.raises(ProcessLookupError):
            pc.kill_pid(nonexistent, pc.SIGKILL)
        if pc.IS_WINDOWS:
            assert len(recorded) == 1 and str(nonexistent) in recorded[0], recorded

    def test_process_matches_false_for_unused_pid(self):
        assert pc.process_matches(2_000_000_000, ("kiro-cli", "claude")) is False


class TestProcessCwd:
    """``process_cwd`` is polled per open terminal, so its contract is that it
    answers from ``/proc`` or ``libproc`` and NEVER spawns a subprocess. The
    macOS branch is exercised on every platform by faking the ``libproc``
    handle, since the byte offsets it slices with are the risky part."""

    def test_returns_own_cwd(self):
        cwd = pc.process_cwd(os.getpid())
        if cwd is None:
            pytest.skip("no /proc and no libproc on this host")
        assert os.path.samefile(cwd, os.getcwd())

    def test_returns_none_for_unused_pid(self):
        assert pc.process_cwd(2_000_000_000) is None

    def test_never_spawns_a_subprocess(self, monkeypatch):
        # The whole point of this helper: a fork+exec of the gateway per poll is
        # what it exists to avoid, so a regression that reintroduces one here
        # must fail loudly rather than just get slower.
        def explode(*a, **k):
            raise AssertionError("process_cwd must not spawn a subprocess")

        monkeypatch.setattr(subprocess, "run", explode)
        monkeypatch.setattr(subprocess, "Popen", explode)
        pc.process_cwd(os.getpid())
        pc.process_cwd(2_000_000_000)

    @staticmethod
    def _fake_libproc(path: bytes, *, filled: int | None = None):
        """A libproc stand-in whose proc_pidinfo writes *path* at the cwd offset."""
        size = pc._DARWIN_PROC_VNODEPATHINFO_SIZE

        class _Lib:
            def proc_pidinfo(self, pid, flavor, arg, buf, buffersize):
                buf.raw = (
                    b"\0" * pc._DARWIN_VNODE_INFO_SIZE
                    + path
                    + b"\0" * (size - pc._DARWIN_VNODE_INFO_SIZE - len(path))
                )
                return size if filled is None else filled

        return _Lib()

    def test_darwin_reads_the_path_at_the_cwd_offset(self, monkeypatch):
        monkeypatch.setattr(
            pc,
            "_darwin_libproc_handle",
            lambda: self._fake_libproc(b"/Users/u/proj"),
        )
        assert pc._darwin_process_cwd(4242) == "/Users/u/proj"

    def test_darwin_refuses_a_short_write(self, monkeypatch):
        # A byte count other than the exact struct size means the layout the
        # offsets assume does not match the kernel's, so the path cannot be
        # sliced out safely — the caller falls back instead of getting garbage.
        monkeypatch.setattr(
            pc,
            "_darwin_libproc_handle",
            lambda: self._fake_libproc(b"/Users/u/proj", filled=64),
        )
        assert pc._darwin_process_cwd(4242) is None

    def test_darwin_refuses_an_error_return(self, monkeypatch):
        monkeypatch.setattr(
            pc,
            "_darwin_libproc_handle",
            lambda: self._fake_libproc(b"/x", filled=-1),
        )
        assert pc._darwin_process_cwd(4242) is None

    def test_darwin_returns_none_without_libproc(self, monkeypatch):
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)
        assert pc._darwin_process_cwd(4242) is None

    def test_darwin_swallows_a_throwing_libproc(self, monkeypatch):
        class _Boom:
            def proc_pidinfo(self, *a):
                raise OSError("nope")

        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _Boom())
        assert pc._darwin_process_cwd(4242) is None


class TestFindListeningPids:
    def test_returns_list_of_ints_for_unused_port(self):
        # A very-high port nothing is bound to → empty list, never raises, on any OS.
        result = pc.find_listening_pids(59999)
        assert isinstance(result, list)
        assert all(isinstance(p, int) for p in result)

    def test_finds_a_real_listener(self):
        # Bind a real loopback listener and confirm the helper sees our PID.
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            pids = pc.find_listening_pids(port)
            # netstat/lsof should attribute the listener to this process. Some CI
            # sandboxes restrict that output — tolerate an empty result rather than
            # flake, but when populated it must include us.
            assert isinstance(pids, list)
            if pids:
                assert os.getpid() in pids
        finally:
            s.close()

    def test_linux_per_process_probe_runs_on_every_runner(self, tmp_path, monkeypatch):
        proc_root = tmp_path / "proc"
        (proc_root / "net").mkdir(parents=True)
        (proc_root / "4242" / "fd").mkdir(parents=True)
        (proc_root / "4242" / "fd" / "7").touch()
        (proc_root / "net" / "tcp").write_text(
            "header\n"
            "0: 0100007F:1E61 00000000:0000 0A 00000000:00000000 "
            "00:00000000 00000000 1000 0 12345\n",
            encoding="ascii",
        )
        (proc_root / "net" / "tcp6").write_text("header\n", encoding="ascii")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "readlink", lambda path: "socket:[12345]")

        assert pc.process_owns_loopback_listener(4242, 7777, proc_root=proc_root) is True

    def test_windows_listener_probe_marks_browser_capable(self, monkeypatch):
        """Use real Windows/Darwin attribution there; both synthetic shapes elsewhere."""
        from kiro_crew.browser_cli import view as browser_view

        def _assert_capable(port: int) -> None:
            assert pc.process_owns_loopback_listener(os.getpid(), port) is True
            browser_view._invalidate_listener_lookup_self_test_cache()
            try:
                assert browser_view._listener_lookup_functional() is True
                assert browser_view._structurally_blind_listener_attribution() is False
            finally:
                browser_view._invalidate_listener_lookup_self_test_cache()

        if sys.platform in {"win32", "darwin"}:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                _assert_capable(int(listener.getsockname()[1]))
            return

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(
            pc,
            "_windows_loopback_listener_owner_pids",
            lambda port: {os.getpid()},
        )
        _assert_capable(45613)

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "listening_pid_tool", lambda: "lsof")
        monkeypatch.setattr(
            pc,
            "trusted_system_bin",
            lambda name: "/usr/bin/lsof" if name == "lsof" else None,
        )
        monkeypatch.setattr(
            pc,
            "probe_port_listeners",
            lambda port, process_pid=None: (
                [pc.PortListener(os.getpid(), "127.0.0.1", "4")],
                True,
            ),
        )
        _assert_capable(45613)


class TestProcessCommandLine:
    def test_self_cmdline_mentions_python(self):
        # Our own process is a Python interpreter, so when the probe returns
        # anything it must mention python/pytest — and the call must never raise.
        #
        # An EMPTY result is tolerated because it is the function's documented
        # failure return, not a defect: on Windows the probe shells out to
        # PowerShell `Get-CimInstance Win32_Process` under a 10s timeout, and
        # PowerShell cold-start plus a WMI query exceeds that on a loaded CI
        # runner (TimeoutExpired is a SubprocessError, so it returns ""). Asserting
        # non-empty there asserts more than `process_command_line` promises. Same
        # reasoning as the find_listening_pids probe above.
        cl = pc.process_command_line(os.getpid())
        assert isinstance(cl, str)
        if cl:
            assert "python" in cl.lower() or "pytest" in cl.lower()

    def test_dead_pid_returns_empty_string(self):
        # A non-existent PID yields "" (fail-closed), never an exception.
        assert pc.process_command_line(2_000_000_000) == ""

    @pytest.mark.skipif(sys.platform != "win32", reason="the WMI/PowerShell arm is Windows-only")
    def test_windows_cmdline_survives_a_character_outside_the_code_page(self):
        # The Windows arm reads the command line back through PowerShell, and
        # both ends of that pipe are pinned to UTF-8 so a character the host
        # code page cannot represent survives the round trip. With either end
        # left on the code page it is silently best-fitted away and the probe
        # answers a plausible string that does not equal the real command line
        # -- which is what the callers compare to decide whether a listening
        # PID is our own gateway.
        #
        # The needle is chosen against the live code page rather than hardcoded:
        # which characters are lost depends on the host (cp1252 loses the CJK
        # one, cp950 loses the accented one), and on a UTF-8 host nothing is
        # lost and there is nothing to assert.
        import locale

        code_page = locale.getpreferredencoding(False)
        needle = ""
        for candidate in ("張", "é", "Ж", "क"):
            try:
                candidate.encode(code_page)
            except UnicodeEncodeError:
                needle = candidate
                break
        if not needle:
            pytest.skip(f"{code_page} encodes every probe character; no divergence to assert")
        # Guard the guard: an encodable needle would make this test pass against
        # the unfixed decode, so prove the chosen one really is unrepresentable.
        with pytest.raises(UnicodeEncodeError):
            needle.encode(code_page)

        child = subprocess.Popen(
            [sys.executable, "-c", f"import time; _ = {needle!r}; time.sleep(30)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            cl = pc.process_command_line(child.pid)
            # "" stays tolerated for the same reason as the probe above: a cold
            # PowerShell plus a WMI query can exceed the 10s timeout on a loaded
            # runner, and that is the documented failure return, not a defect.
            # A NON-empty answer, though, is the real command line or it is wrong.
            if cl:
                assert needle in cl
        finally:
            child.kill()
            child.wait(timeout=10)


class TestProcessOwnerUid:
    """`process_owner_uid` backs the ownership half of the CLI's port-trust gate,
    so 'cannot determine' must be distinguishable from 'owned by me'."""

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX only")
    def test_self_pid_is_owned_by_current_user(self):
        assert pc.process_owner_uid(os.getpid()) == os.getuid()

    def test_dead_pid_returns_none(self):
        # None means "unknown" — callers fail closed on it rather than assuming.
        assert pc.process_owner_uid(2_000_000_000) is None

    @pytest.mark.skipif(hasattr(os, "getuid"), reason="Windows-only behaviour")
    def test_windows_reports_unknown(self):
        assert pc.process_owner_uid(os.getpid()) is None


class TestStrftime:
    def test_translates_dash_directives_on_windows(self):
        # The core Windows fix: %-I / %-d (glibc no-pad) → %#I / %#d (MSVCRT).
        # We assert the translation indirectly via a fake dt that records the
        # format string it was handed, so the test is platform-independent.
        class FakeDt:
            def __init__(self):
                self.fmt = None

            def strftime(self, fmt):
                self.fmt = fmt
                return "ok"

        dt = FakeDt()
        pc.strftime(dt, "%-I:%M %p")
        if pc.IS_WINDOWS:
            assert dt.fmt == "%#I:%M %p"
        else:
            assert dt.fmt == "%-I:%M %p"  # untouched on POSIX

    def test_real_datetime_formats_without_error(self):
        # End-to-end against a real datetime: must not raise ValueError on
        # Windows (where bare %-I would).
        import datetime as _dt

        d = _dt.datetime(2026, 4, 7, 9, 5)
        out = pc.strftime(d, "%-I:%M %p")
        assert "9" in out and ":05" in out


class TestIsExecutableFile:
    def test_posix_requires_x_bit(self, tmp_path):
        # POSIX: the execute bit gates runnability (so chmod -x disables a hook).
        # Windows: no x-bit, so a known script extension is runnable regardless.
        f = tmp_path / "hook.sh"
        f.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(f, 0o644)  # no x-bit
        if pc.IS_WINDOWS:
            assert pc.is_executable_file(f) is True  # .sh extension → runnable
        else:
            assert pc.is_executable_file(f) is False  # no x-bit → not runnable
        os.chmod(f, 0o755)  # +x
        assert pc.is_executable_file(f) is True  # runnable on both now

    def test_missing_file_is_not_executable(self, tmp_path):
        assert pc.is_executable_file(tmp_path / "nope.sh") is False

    def test_windows_rejects_unknown_extension(self, tmp_path):
        # Even on Windows, a non-script extension isn't treated as a runnable hook.
        f = tmp_path / "data.txt"
        f.write_text("x")
        if pc.IS_WINDOWS:
            assert pc.is_executable_file(f) is False

    def test_oserror_during_probe_is_not_executable(self, tmp_path, monkeypatch):
        # If the stat/access probe raises OSError (e.g. a path that triggers
        # ELOOP / permission failure), the helper fails closed -> False, never
        # propagating. Force the error since a normal path would just succeed.
        f = tmp_path / "boom.sh"
        f.write_text("#!/bin/sh\n")

        def boom(*args, **kwargs):
            raise OSError("probe failed")

        monkeypatch.setattr(pc.os.path, "isfile", boom)
        assert pc.is_executable_file(f) is False


class TestFindPythonInterpreter:
    def test_rejects_windows_store_stub_path(self):
        # The bug this guards: shutil.which("python3") resolves the Microsoft
        # Store App Execution Alias stub under WindowsApps; spawning it prints
        # "Python was not found" and exits 9009. The path heuristic must flag it
        # on Windows (and never misfire on POSIX, where the env var is absent).
        stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.EXE"
        real = r"C:\Program Files\Python312\python.EXE"
        if pc.IS_WINDOWS:
            assert pc._is_windows_store_python_stub(stub) is True
            assert pc._is_windows_store_python_stub(real) is False
        else:
            # POSIX never has the stub — the check is a no-op (always False).
            assert pc._is_windows_store_python_stub(stub) is False

    def test_skips_stub_and_returns_real_interpreter(self, monkeypatch):
        # which() returns the stub first, then a real python — the stub must be
        # skipped and the real interpreter (which reports 3.12) returned.
        real = r"C:\Python312\python.exe" if pc.IS_WINDOWS else "/usr/bin/python3.12"
        stub = (
            r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.EXE"
            if pc.IS_WINDOWS
            else None
        )

        def fake_which(name: str):
            # First candidate resolves to the stub (Windows) / nothing (POSIX),
            # everything else resolves to the real interpreter.
            return stub if name in ("python", "python3") else real

        monkeypatch.setattr("shutil.which", fake_which)
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: "3.12\n")
        got = pc.find_python_interpreter()
        assert got == real
        assert pc._is_windows_store_python_stub(got) is False

    def test_returns_none_when_only_stub_or_too_old(self, monkeypatch):
        # No usable interpreter: which() yields only the stub (Windows) / nothing,
        # or an interpreter that reports < 3.12. Either way → None, never the stub.
        if pc.IS_WINDOWS:
            stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.EXE"
            monkeypatch.setattr("shutil.which", lambda name: stub)
        else:
            monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")
            monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: "3.9\n")
        assert pc.find_python_interpreter() is None


class TestUtf8Console:
    @pytest.mark.parametrize("is_windows", [False, True])
    def test_call_publishes_utf8_for_children(self, monkeypatch, is_windows):
        monkeypatch.setattr(pc, "IS_WINDOWS", is_windows)
        monkeypatch.setattr(pc.sys, "stdout", None)
        monkeypatch.setattr(pc.sys, "stderr", None)
        monkeypatch.setenv("PYTHONUTF8", "0")
        monkeypatch.setenv("PYTHONIOENCODING", "cp1252")

        pc.ensure_utf8_console()

        assert os.environ["PYTHONUTF8"] == "1"
        assert os.environ["PYTHONIOENCODING"] == "utf-8:backslashreplace"

    def test_ensure_utf8_console_is_safe_to_call(self):
        # Publishes the child environment on every OS and reconfigures the
        # current stdout/stderr only on Windows. Either way it must never raise
        # (it swallows non-reconfigurable streams), and must be idempotent (safe
        # to call from both __main__ and cli.main).
        pc.ensure_utf8_console()
        pc.ensure_utf8_console()

    def test_emoji_print_does_not_raise_after_call(self, capsys):
        # The bug this guards: KiroCrew prints non-ASCII glyphs everywhere, and on
        # Windows cp1252 stdout that raised UnicodeEncodeError and killed the gateway.
        # After ensure_utf8_console(), a non-ASCII print must succeed on any platform.
        pc.ensure_utf8_console()
        print("中文 KiroCrew 日本語")  # non-cp1252-encodable glyphs
        out = capsys.readouterr().out
        assert "KiroCrew" in out

    def test_rewraps_cp1252_stream_so_emoji_log_record_survives(self, monkeypatch):
        # When the worker's stderr is a cp1252 TextIOWrapper that reconfigure()
        # can't flip (the 3-layer Windows spawn), a logging StreamHandler bound to
        # it crashes on the first non-ASCII log record, so ensure_utf8_console()
        # must re-wrap the underlying buffer so the record emits cleanly.
        #
        # This stream repair is WINDOWS-only behavior: on POSIX the function
        # publishes the environment for children but leaves current streams
        # alone. Forcing a cp1252 stderr and asserting emoji survives therefore
        # only makes sense on Windows. Gate accordingly.
        if not pc.IS_WINDOWS:
            pytest.skip("ensure_utf8_console re-wrap is Windows-only (no-op on POSIX)")

        import io
        import logging

        raw = io.BytesIO()
        monkeypatch.setattr(
            sys, "stderr", io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        )
        pc.ensure_utf8_console()
        # The fix must have produced a utf-8 stderr (reconfigure or buffer re-wrap).
        assert (sys.stderr.encoding or "").lower().startswith("utf-8")
        # A StreamHandler bound to the (now-fixed) stderr must not error on non-ASCII.
        handler = logging.StreamHandler(sys.stderr)
        errors: list = []
        monkeypatch.setattr(handler, "handleError", lambda record: errors.append(record))
        log = logging.getLogger("test_emoji_log")
        log.addHandler(handler)
        try:
            log.error("中文 non-ascii log record")
            handler.flush()
        finally:
            log.removeHandler(handler)
        assert errors == []


class _TtyTextIOWrapper(io.TextIOWrapper):
    """A text stream that answers like a terminal and counts reconfigure calls."""

    reconfigures = 0

    def isatty(self) -> bool:
        return True

    def reconfigure(self, **kwargs) -> None:
        type(self).reconfigures += 1
        super().reconfigure(**kwargs)


class TestLineBufferedStdout:
    STATUS_LINE = "👻 Kiro Crew gateway starting…"

    def test_status_line_reaches_a_non_tty_stdout_as_printed(self, monkeypatch):
        # A pipe or a file (the journal under systemd, the Desktop supervisor's
        # log fd, a detached gateway's own gateway.log) gets a block-buffered
        # stdout, so a status line sits in the buffer until it fills or the
        # process exits. After the gateway's stdout setup, one plain print()
        # with no flush must already be in the underlying bytes.
        raw = io.BytesIO()
        # newline="\n": one byte per newline on every OS, so the compare is about buffering.
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\n", line_buffering=False)
        assert not stream.isatty()
        monkeypatch.setattr(sys, "stdout", stream)

        pc.ensure_line_buffered_stdout()
        print(self.STATUS_LINE)

        assert raw.getvalue() == f"{self.STATUS_LINE}\n".encode("utf-8")

    def test_a_terminal_stdout_is_left_alone(self, monkeypatch):
        # A terminal is line-buffered by the interpreter itself; the seam must
        # not touch it (not even a flush through reconfigure).
        stream = _TtyTextIOWrapper(
            io.BytesIO(), encoding="utf-8", newline="\n", line_buffering=True
        )
        _TtyTextIOWrapper.reconfigures = 0
        monkeypatch.setattr(sys, "stdout", stream)

        pc.ensure_line_buffered_stdout()

        assert _TtyTextIOWrapper.reconfigures == 0
        assert stream.line_buffering is True

    @pytest.mark.parametrize(
        "stdout",
        [
            pytest.param(None, id="absent-pythonw"),
            pytest.param(io.StringIO(), id="no-reconfigure"),
            pytest.param(types.SimpleNamespace(), id="plain-object"),
        ],
    )
    def test_a_stream_that_cannot_be_reconfigured_does_not_raise(self, monkeypatch, stdout):
        # Boot must survive a replaced or captured stdout: a launcher's plain
        # object up a multi-process spawn chain, a test's StringIO, or no
        # stream at all. Nothing to buffer means nothing to do.
        monkeypatch.setattr(sys, "stdout", stdout)

        pc.ensure_line_buffered_stdout()  # must not raise

    def test_a_closed_stdout_does_not_raise(self, monkeypatch):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")
        stream.close()
        monkeypatch.setattr(sys, "stdout", stream)

        pc.ensure_line_buffered_stdout()  # must not raise


def _wire_mapping(buf: mmap.mmap, length: int) -> bool:
    """Pin *buf*'s pages resident with ``mlock``; True when the kernel agreed.

    Faulting a page in does not keep it in the resident set. Under memory
    pressure macOS hands anonymous pages to its compressor the moment they
    are touched, and a compressed page is not counted by
    ``task_info().resident_size`` -- so on a loaded 3-shard runner a 128 MB
    mapping that was written end to end read back as a 40 MB rise, and the
    "rose while held" precondition below failed on a reading that was
    exactly what ``ps -o rss=`` showed. Wired pages cannot be compressed or
    evicted, which turns "how much of the mapping is resident" from the
    kernel's discretion into a fixed quantity for the duration of the sample.

    Best-effort by design: ``RLIMIT_MEMLOCK`` is unlimited on macOS but a few
    megabytes on a stock Linux, where the call fails with ``ENOMEM`` and the
    plain fault-in is enough because Linux does not compress anonymous pages.
    Windows has no ``mlock``. Whatever happens, the mapping is still faulted
    in by the caller; the return value only records which case ran so a
    failing sample says so.
    """
    if sys.platform == "win32":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mlock = libc.mlock
    except (AttributeError, OSError):
        return False
    mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    mlock.restype = ctypes.c_int
    # ``from_buffer`` takes an export on the mapping; it must be dropped
    # before ``buf.close()`` or the close raises BufferError.
    view = ctypes.c_char.from_buffer(buf)
    try:
        addr = ctypes.addressof(view)
    finally:
        del view
    return mlock(addr, length) == 0


def _measure_rss_release():
    """Sample one real mapping's lifetime in the calling process."""
    chunk = 128 * 1024 * 1024
    page = 4096
    baseline = pc.proc_rss_bytes()
    buf = mmap.mmap(-1, chunk)
    try:
        wired = _wire_mapping(buf, chunk)
        for offset in range(0, chunk, page):  # fault the pages in
            buf[offset] = 1
        while_held = pc.proc_rss_bytes()
        peak_while_held = pc.proc_peak_rss_bytes()
    finally:
        buf.close()
    after_free = pc.proc_rss_bytes()
    peak_after = pc.proc_peak_rss_bytes()
    return {
        "baseline": baseline,
        "while_held": while_held,
        "peak_while_held": peak_while_held,
        "after_free": after_free,
        "peak_after": peak_after,
        "wired": wired,
    }


class TestResourceShims:
    def test_proc_rss_bytes_nonnegative(self):
        # Returns this process's RSS (>0 normally) or 0 on failure — never raises.
        assert pc.proc_rss_bytes() >= 0

    def test_proc_rss_bytes_is_positive_for_a_live_process(self):
        # A running interpreter always has resident memory. This must be > 0 on
        # every supported platform: on Windows GetCurrentProcess's handle was
        # truncated without argtypes and this silently returned 0, disabling the
        # watchdog's RSS ceiling.
        assert pc.proc_rss_bytes() > 0

    def test_proc_rss_bytes_falls_back_down_when_memory_is_released(self, tmp_path):
        """The reading must be CURRENT residency, not the high-water mark.

        Reported symptom: the dashboard's per-process memory figure only ever
        rose, so it disagreed with Activity Monitor / ``ps -o rss=`` by however
        much the gateway had ever transiently used. ``ru_maxrss`` never
        decreases, so this drives a real allocation and requires the number to
        come back down — the one property a peak cannot have.

        The allocation is an ``mmap`` rather than a ``bytearray`` because the
        RELEASE has to be observable on every platform, and only ``munmap`` is:
        freeing a ``bytearray`` returns the pages to the allocator, which decides
        for itself whether to hand them back to the OS. macOS's keeps all 128MB
        resident, so the current reading did not move and this failed there while
        agreeing exactly with ``ps -o rss=`` — a correct reading judged against an
        allocator's discretion rather than against the property under test.
        Closing a mapping unmaps immediately on Linux, macOS and Windows alike.

        RSS covers the whole process: unrelated allocations released by a prior
        test's threads or finalizers can cancel out this mapping's growth.
        A fresh interpreter removes that inherited state, not OS variability.

        The mapping is also WIRED where the platform allows (``mlock``), because
        faulting a page in does not keep it resident: under memory pressure the
        macOS compressor takes touched anonymous pages straight out of the
        resident set, and on a loaded 3-shard runner the 128 MB mapping read back
        as a 40 MB rise -- a correct reading that failed the "rose while held"
        precondition. Wired pages are the one thing the kernel cannot compress or
        evict, so the rise is the mapping's size rather than the compressor's
        mood. ``samples["wired"]`` records whether the lock took, so a failure
        here says which case it measured.
        """
        import kiro_crew

        root = Path(__file__).resolve().parents[1]
        source_root = root / "src"
        assert Path(kiro_crew.__file__).resolve().parent == source_root / "kiro_crew"
        assert Path(pc.__file__).resolve() == source_root / "kiro_crew" / "platform_compat.py"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import json, sys; from pathlib import Path; "
                "sys.path[:0] = sys.argv[1:]; "
                "import kiro_crew, test_platform_compat as probe; "
                "assert Path(kiro_crew.__file__).resolve().parent "
                "== Path(sys.argv[1]) / 'kiro_crew'; "
                "assert Path(probe.pc.__file__).resolve() "
                "== Path(sys.argv[1]) / 'kiro_crew' / 'platform_compat.py'; "
                "assert Path(probe.__file__).resolve() "
                "== Path(sys.argv[2]) / 'test_platform_compat.py'; "
                "print(json.dumps(probe._measure_rss_release()))",
                str(source_root),
                str(root / "test"),
                str(root),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        samples = json.loads(result.stdout)
        baseline = samples["baseline"]
        while_held = samples["while_held"]
        peak_while_held = samples["peak_while_held"]
        after_free = samples["after_free"]
        chunk = 128 * 1024 * 1024

        # Rose by most of the buffer while it was resident.
        assert while_held - baseline > chunk // 2, samples
        # And gave a real part of it back. Deliberately relative to `while_held`
        # rather than an absolute `baseline + chunk // 2` ceiling: how much the
        # OS actually returns on free is its decision, not ours. Windows keeps
        # freed pages in the working set until there is pressure, so it returned
        # ~45MB of a 128MB buffer where Linux returns nearly all of it, and an
        # absolute ceiling failed there on a reading that was behaving correctly.
        # A peak-based implementation cannot pass this at any tolerance, because
        # it returns a number that has not moved at all.
        assert after_free < while_held - chunk // 8, samples
        # The decisive property, and the one the bug got wrong: after a free the
        # CURRENT reading must be strictly below the peak. `ru_maxrss` returns
        # exactly the peak here, so this is the assertion that fails for it.
        assert after_free < peak_while_held, samples
        # The peak, by contrast, is not allowed to fall.
        assert samples["peak_after"] >= peak_while_held, samples

    def test_proc_peak_rss_bytes_reads_the_same_unit_as_the_current_reading(self):
        # The property under test is the UNIT, not the ordering: ru_maxrss is KiB on
        # Linux and bytes on macOS, so a missing or spurious conversion puts the two
        # readings 1024x apart. Asserted as a bounded ratio rather than
        # `peak >= current`, which reads as the tighter and more obvious invariant but
        # is not atomically observable on Linux: the two come from DIFFERENT kernel
        # accounting paths. proc_rss_bytes reads /proc/self/statm, recomputed on
        # read, while proc_peak_rss_bytes reads getrusage's high-water mark, which
        # the kernel maintains from per-CPU RSS deltas it syncs in batches. So while
        # the process is allocating, the live reading legitimately sits a little
        # above the last-synced peak -- measured up to 1.02x on this 32-core host,
        # which is what made the strict form fail under a loaded full-suite run.
        # 4x leaves that mechanism ~250x of headroom before a real unit error passes.
        current = pc.proc_rss_bytes()
        peak = pc.proc_peak_rss_bytes()
        assert peak > 0
        assert peak * 4 >= current, (
            f"peak {peak} is more than 4x under the live reading {current} -- too far "
            "apart to be counter-sync lag, so one side is in the wrong unit"
        )

    def test_proc_rss_bytes_for_pid_self_positive(self):
        rss = pc.proc_rss_bytes_for_pid(os.getpid())
        # macOS has no ctypes-only per-pid path and returns None by design.
        if rss is None:
            pytest.skip("per-pid RSS unavailable on this platform")
        assert rss > 0

    def test_proc_rss_bytes_for_pid_none_for_unused_pid(self):
        assert pc.proc_rss_bytes_for_pid(2_000_000_000) is None
