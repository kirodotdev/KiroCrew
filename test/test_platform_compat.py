"""Unit tests for kiro_crew.platform_compat — the cross-platform shim that lets
KiroCrew run natively on Windows alongside macOS/Linux.

These exercise the PURE / platform-dispatching surface, spawning a real process
only where the contract IS an OS behavior (process-session semantics): the
signal constants, the file-lock context managers (POSIX path on
this host; the Windows branch is asserted via its dispatch shape), the
strftime directive translation (the one piece with a deterministic Windows
output we can assert directly), and the process-helper return contracts.
"""

from __future__ import annotations

import errno
import json
import logging
import mmap
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc

#: The REAL same-group probe, bound at module import so this file can test it.
#: The rootdir conftest pins ``pc._shares_own_process_group`` for every test
#: (see ``_pin_kill_and_reap_group_probe``), and that pin lands after this
#: import -- so reaching for the module attribute inside a test would exercise
#: the stub instead of the function.
_real_shares_own_process_group = pc._shares_own_process_group


def _fake_windows_bins(monkeypatch):
    """Resolve Windows system binaries while ``IS_WINDOWS`` is faked on POSIX.

    The Windows branches are deliberately exercised on the Linux CI fleet by
    flipping ``IS_WINDOWS``. Those branches resolve their binary from the
    trusted system directories before spawning, which a Linux host cannot
    satisfy, so the lookup is faked alongside the platform flag — otherwise the
    spawn reports the tool missing before the branch under test is reached.
    """

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: rf"C:\Windows\System32\{name}.exe")


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
    def test_windows_uses_space_free_argv0(self, monkeypatch):
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
                ["python.exe", "-m", "kiro_crew", "gateway", "--port", "5476"],
            )
        ]
        assert os.environ["PYTHONUTF8"] == "1"
        assert os.environ["PYTHONIOENCODING"] == "utf-8:backslashreplace"

    def test_posix_preserves_full_argv0_and_pins_utf8(self, monkeypatch):
        executable = "/opt/Kiro Crew/bin/python3"
        calls = []
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc.sys, "executable", executable)
        monkeypatch.setattr(pc.os, "execv", lambda path, argv: calls.append((path, argv)))
        monkeypatch.setenv("PYTHONUTF8", "0")
        monkeypatch.setenv("PYTHONIOENCODING", "latin-1")

        pc.reexec_python_module("kiro_crew", ["gateway"])

        assert calls == [(executable, [executable, "-m", "kiro_crew", "gateway"])]
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
        env = {
            **os.environ,
            "PYTHONUTF8": "0",
            "PYTHONIOENCODING": "cp1252",
            "PYTHONPATH": os.pathsep.join(p for p in (source_root, inherited_path) if p),
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

    def test_get_ppid_returns_int(self):
        # Returns the parent (>0 normally) or -1 on failure — never raises.
        ppid = pc.get_ppid(os.getpid())
        assert isinstance(ppid, int)

    def test_kill_pid_nonexistent_is_safe(self):
        # Both platforms raise on non-existent pid — same exception shape so
        # callers' ``except (ProcessLookupError, OSError)`` handlers fire
        # uniformly. POSIX: os.kill raises ProcessLookupError. Windows:
        # taskkill returns rc=128 which _raise_taskkill_error re-badges as
        # ProcessLookupError.
        with pytest.raises(ProcessLookupError):
            pc.kill_pid(2_000_000_000, pc.SIGKILL)

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
        # offsets assume no longer matches the kernel's, so the path cannot be
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
        # Regression for the gateway-worker UnicodeEncodeError: when the worker's
        # stderr is a cp1252 TextIOWrapper that reconfigure() can't flip (observed
        # through the 3-layer Windows spawn), a logging StreamHandler bound to it
        # crashed on the first non-ASCII log record. ensure_utf8_console() must
        # re-wrap the underlying buffer so the record emits cleanly.
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

    def test_proc_rss_bytes_falls_back_down_when_memory_is_released(self):
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
        """
        chunk = 128 * 1024 * 1024
        page = 4096
        baseline = pc.proc_rss_bytes()
        buf = mmap.mmap(-1, chunk)
        try:
            for offset in range(0, chunk, page):  # fault the pages in
                buf[offset] = 1
            while_held = pc.proc_rss_bytes()
            peak_while_held = pc.proc_peak_rss_bytes()
        finally:
            buf.close()
        after_free = pc.proc_rss_bytes()

        # Rose by most of the buffer while it was resident.
        assert while_held - baseline > chunk // 2
        # And gave a real part of it back. Deliberately relative to `while_held`
        # rather than an absolute `baseline + chunk // 2` ceiling: how much the
        # OS actually returns on free is its decision, not ours. Windows keeps
        # freed pages in the working set until there is pressure, so it returned
        # ~45MB of a 128MB buffer where Linux returns nearly all of it, and an
        # absolute ceiling failed there on a reading that was behaving correctly.
        # A peak-based implementation cannot pass this at any tolerance, because
        # it returns a number that has not moved at all.
        assert after_free < while_held - chunk // 8
        # The decisive property, and the one the bug got wrong: after a free the
        # CURRENT reading must be strictly below the peak. `ru_maxrss` returns
        # exactly the peak here, so this is the assertion that fails for it.
        assert after_free < peak_while_held
        # The peak, by contrast, is not allowed to fall.
        assert pc.proc_peak_rss_bytes() >= peak_while_held

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

    def test_proc_rss_tree_mb_for_pid_windows_only(self):
        # Windows-only: the lineage-validated tree walk. On POSIX it returns None
        # (callers keep their /proc or ps route), and it must never raise.
        result = pc.proc_rss_tree_mb_for_pid(os.getpid())
        if not pc.IS_WINDOWS:
            assert result is None
            return
        # This is a real-boundary smoke test only. Comparing it with a second RSS
        # sample is scheduler-dependent: Windows may trim this process's working
        # set between the tree and single-process reads. Fixed-value tests in
        # TestProcRssTree pin the root-plus-descendants sum deterministically.
        assert result is not None and result > 0

    def test_proc_rss_tree_mb_for_pid_rejects_reserved_pid(self):
        # A reserved/non-int pid must not anchor a tree walk (recycled-root risk).
        assert pc.proc_rss_tree_mb_for_pid(1) is None
        assert pc.proc_rss_tree_mb_for_pid(0) is None

    def test_proc_cpu_seconds_nonnegative(self):
        assert pc.proc_cpu_seconds() >= 0.0

    def test_proc_cpu_seconds_is_positive_for_a_running_process(self):
        # A running interpreter has always consumed some CPU. This must be > 0
        # on every supported platform: on Windows GetCurrentProcess's handle was
        # truncated without argtypes, so GetProcessTimes failed and this read 0.0.
        assert pc.proc_cpu_seconds() > 0.0

    def test_proc_cpu_nanos_for_pid_reads_a_running_process(self):
        # Linux /proc, macOS libproc, Windows GetProcessTimes: a live interpreter
        # has consumed CPU on all three. None is reserved for a platform with no
        # per-pid counter at all, where the caller keeps its prior behavior.
        ns = pc.proc_cpu_nanos_for_pid(os.getpid())
        if ns is None:
            pytest.skip("no per-pid CPU counter on this platform")
        assert ns > 0

    def test_proc_cpu_nanos_for_pid_refuses_a_reserved_pid(self):
        assert pc.proc_cpu_nanos_for_pid(0) is None
        assert pc.proc_cpu_nanos_for_pid(-1) is None

    def test_raise_nofile_soft_limit_is_safe(self):
        # No-op on Windows; best-effort raise on POSIX. Must never raise.
        pc.raise_nofile_soft_limit(4096)


class TestChmodShims:
    def test_chmod_safe_noop_on_missing_is_safe(self):
        # chmod_safe logs + swallows on failure (POSIX) and is a no-op on
        # Windows — a non-existent path must not raise either way.
        pc.chmod_safe(os.path.join(tempfile.gettempdir(), "no-such-mc-file"), 0o600)

    def test_fchmod_safe_on_real_fd_is_safe(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        fd = os.open(str(f), os.O_RDONLY)
        try:
            pc.fchmod_safe(fd, 0o600)  # applies on POSIX, no-op on Windows
        finally:
            os.close(fd)


class TestDirLinkShims:
    """``symlink_or_junction`` / ``is_link_or_junction`` / ``unlink_link_or_junction``.

    These run on every platform: the contract is the same everywhere (a name
    that means another directory), only the mechanism differs — a symlink on
    POSIX, a directory junction on Windows, where an ordinary account holds no
    ``SeCreateSymbolicLinkPrivilege`` and ``os.symlink`` fails with
    ``WinError 1314``.
    """

    def test_link_is_created_and_transparent(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "index.html").write_text("hi")
        link = tmp_path / "link"

        pc.symlink_or_junction(target, link)

        assert pc.is_link_or_junction(link)
        assert link.is_dir()
        assert link.resolve() == target.resolve()
        # Reads go through, and later writes to the target are visible via the
        # link — the property the dist resolver relies on for rebuild pickup.
        assert (link / "index.html").read_text(encoding="utf-8") == "hi"
        (target / "later.txt").write_text("fresh")
        assert (link / "later.txt").read_text(encoding="utf-8") == "fresh"

    def test_plain_dir_and_file_are_not_links(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        regular = tmp_path / "f.txt"
        regular.write_text("x")

        assert not pc.is_link_or_junction(plain)
        assert not pc.is_link_or_junction(regular)
        assert not pc.is_link_or_junction(tmp_path / "does-not-exist")

    def test_dangling_link_is_still_reported_as_a_link(self, tmp_path):
        """A link whose target is gone must still answer True.

        The dist resolver's replace path keys off exactly this: ``exists()``
        follows the link and is already False, so only the link-ness test can
        tell "stale link to clean up" from "nothing here".
        """
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)
        shutil.rmtree(target)

        assert pc.is_link_or_junction(link)
        assert not link.exists()

    def test_unlink_removes_the_link_and_spares_the_target(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)

        pc.unlink_link_or_junction(link)

        assert not pc.is_link_or_junction(link)
        assert not os.path.lexists(str(link))
        assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_unlink_removes_a_dangling_link(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)
        shutil.rmtree(target)

        pc.unlink_link_or_junction(link)

        assert not os.path.lexists(str(link))

    def test_unlink_refuses_a_real_directory(self, tmp_path):
        """A non-link must raise on both platforms, empty or not.

        POSIX ``os.unlink`` refuses a directory outright, so the Windows
        ``rmdir`` fallback has to be fenced to reparse points: unfenced it
        DELETES a real empty directory, so a caller that mis-detects link-ness
        loses data on Windows only while POSIX raises.
        """
        empty = tmp_path / "real-empty"
        empty.mkdir()
        full = tmp_path / "real-full"
        full.mkdir()
        (full / "keep.txt").write_text("keep")

        with pytest.raises(OSError):
            pc.unlink_link_or_junction(empty)
        with pytest.raises(OSError):
            pc.unlink_link_or_junction(full)

        assert empty.is_dir()
        assert (full / "keep.txt").read_text(encoding="utf-8") == "keep"

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="junctions exist only on Windows")
    def test_windows_link_is_usable_without_elevation(self, tmp_path):
        """Windows gets a working directory link either way it is made.

        ``symlink_or_junction`` tries ``os.symlink`` FIRST and only falls back to
        a junction, so which mechanism lands depends on whether the host holds
        ``SeCreateSymbolicLinkPrivilege`` — GitHub's runners do, an ordinary
        account does not. Asserting "junction, never symlink" would therefore
        pin the unprivileged host as if it were universal, and fail on CI.

        What matters to every caller is the same on both paths, so that is what
        is asserted: the name is a reparse point that ``is_link_or_junction``
        recognises (an ``is_symlink()``-only test does NOT see a junction, which
        is the bug this shim exists for), it is transparent to path operations,
        and ``rmtree`` refuses it — which is why ``unlink_link_or_junction``
        exists. The junction branch specifically is covered by
        ``test_junction_is_recognised_and_removable`` below.
        """
        target = tmp_path / "target"
        target.mkdir()
        (target / "f.txt").write_text("hi", encoding="utf-8")
        link = tmp_path / "link"

        pc.symlink_or_junction(target, link)

        assert pc.is_link_or_junction(link)
        assert link.is_dir()  # transparent to path operations
        assert (link / "f.txt").read_text(encoding="utf-8") == "hi"
        # rmtree refuses any directory link, which is why unlink_link_or_junction exists.
        with pytest.raises(OSError):
            shutil.rmtree(str(link))
        pc.unlink_link_or_junction(link)
        assert not link.exists()
        assert target.is_dir(), "removing the link must spare the target"

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="junctions exist only on Windows")
    def test_junction_is_recognised_and_removable(self, tmp_path):
        """A JUNCTION specifically — the form an unprivileged Windows user gets.

        Created directly via ``_winapi.CreateJunction`` rather than through the
        shim, so this covers the unprivileged branch even on a runner that holds
        the symlink privilege and would otherwise take the symlink path.
        """
        import _winapi

        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "junction"
        _winapi.CreateJunction(str(target), str(link))

        # A junction reports is_symlink() False — the whole reason the shim's
        # detector cannot be an is_symlink() test.
        assert not link.is_symlink()
        assert pc.is_link_or_junction(link)
        # 0xA0000003 = IO_REPARSE_TAG_MOUNT_POINT, spelled literally rather than
        # read from the module under test (so the assertion is independent of it)
        # and rather than via os.path.isjunction, which would couple the
        # assertion to the same stdlib helper the module itself may use.
        assert os.lstat(str(link)).st_reparse_tag == 0xA0000003
        pc.unlink_link_or_junction(link)
        assert not link.exists()
        assert target.is_dir()

    @pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX symlink mechanism")
    def test_posix_link_is_a_symlink(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"

        pc.symlink_or_junction(target, link)

        assert link.is_symlink()
        assert os.readlink(str(link)) == str(target)


# ---------------------------------------------------------------------------
# POSIX-branch coverage for the new platform_compat helpers. The
# tests below deliberately exercise the ``if IS_POSIX:`` / Linux ``/proc`` paths
# and the POSIX ``except`` fall-throughs that run on the Linux build fleet. The
# Windows branches (msvcrt / ctypes / wintypes / netstat / taskkill / WMI /
# OpenProcess) cannot execute here and are intentionally left to Windows CI.
# ---------------------------------------------------------------------------


class TestFileLockContention:
    def test_try_acquire_lock_fails_under_exclusive_contention(self, tmp_path):
        # flock is per open-file-description: two independent os.open() calls to
        # the same path are independent OFDs, so a second LOCK_EX|LOCK_NB on a
        # path already held exclusively raises BlockingIOError -> the helper's
        # POSIX failure branch returns False (this is what we're covering).
        lock = tmp_path / ".contend.lock"
        fd_holder = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        fd_contender = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            # Real blocking exclusive lock on the holder fd.
            pc.acquire_lock(fd_holder, exclusive=True)
            # Non-blocking exclusive acquire on the *other* OFD must fail.
            assert pc.try_acquire_lock(fd_contender, exclusive=True) is False
            # Once the holder releases, the same contender fd can take it.
            pc.release_lock(fd_holder)
            assert pc.try_acquire_lock(fd_contender, exclusive=True) is True
            pc.release_lock(fd_contender)
        finally:
            os.close(fd_holder)
            os.close(fd_contender)

    def test_shared_try_acquire_then_release_relocks(self, tmp_path):
        # Take a shared non-blocking lock, release it, and confirm an independent
        # OFD can then take an EXCLUSIVE lock -- which is only possible if the
        # shared lock was genuinely released by release_lock.
        lock = tmp_path / ".sh-release.lock"
        fd_shared = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        fd_other = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            assert pc.try_acquire_lock(fd_shared, exclusive=False) is True
            pc.release_lock(fd_shared)
            # Exclusive acquire from a separate OFD now succeeds (lock is free).
            assert pc.try_acquire_lock(fd_other, exclusive=True) is True
            pc.release_lock(fd_other)
        finally:
            os.close(fd_shared)
            os.close(fd_other)

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows LK_LOCK ceiling regression")
    def test_windows_blocking_acquire_waits_past_lk_lock_ceiling(self, tmp_path):
        # Regression for issue #470: msvcrt's LK_LOCK "blocking" code gives up
        # after ~10s with EDEADLOCK and the old shim treated that as "acquired".
        # A holder that keeps the lock LONGER than that ceiling must make a
        # blocking contender WAIT (until release or its own timeout) — never
        # fall through and enter the critical section unserialized at ~10s.
        #
        # Drive _win_acquire_blocking directly with an EXPLICIT timeout past the
        # ceiling: the module default is a short on-loop-safety ceiling, but the
        # bug being pinned is specifically the ~10s LK_LOCK give-up point.
        import threading

        lock = tmp_path / ".ceiling.lock"
        hold_secs = 13.0
        fd_holder = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fd_contender = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        released_at = {"t": 0.0}
        entered_at = {"t": 0.0}
        holder_ready = threading.Event()

        def _hold():
            with pc.file_lock(fd_holder, exclusive=True, required=True):
                holder_ready.set()
                time.sleep(hold_secs)
                released_at["t"] = time.monotonic()

        holder = threading.Thread(target=_hold)
        holder.start()
        try:
            assert holder_ready.wait(timeout=10.0), "holder never took the lock"
            # Blocking acquire on the OTHER fd with a timeout past the ~10s
            # ceiling: it must not succeed until the holder releases at ~13s.
            got = pc._win_acquire_blocking(fd_contender, timeout=30.0)
            entered_at["t"] = time.monotonic()
            assert got is True, "contender never acquired the lock after release"
            # It entered only AFTER the holder released — proving it waited past
            # the 10s ceiling that used to let it slip through early.
            assert entered_at["t"] >= released_at["t"], (
                "contender entered the critical section before the holder "
                "released — the blocking acquire fell through the LK_LOCK ceiling"
            )
            pc.release_lock(fd_contender)
        finally:
            holder.join(timeout=20.0)
            os.close(fd_holder)
            os.close(fd_contender)

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows on-loop single-shot acquire")
    def test_windows_contended_lock_on_event_loop_fails_fast(self, tmp_path):
        # On the asyncio event-loop thread a contended lock must NOT spin-sleep
        # (that freezes chat/heartbeat): _win_acquire_blocking is single-shot
        # there, so file_lock fails closed immediately instead of waiting out
        # the timeout. Assert both the fast-fail AND that it took ~no time.
        import asyncio

        lock = tmp_path / ".onloop.lock"
        fd_holder = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fd_contender = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)

        async def _contend_on_loop():
            # Hold on THIS fd (non-blocking), then a second in-loop acquire on
            # the other fd must raise at once rather than sleep to the ceiling.
            assert pc.try_acquire_lock(fd_holder, exclusive=True) is True
            start = time.monotonic()
            with pytest.raises(OSError):
                with pc.file_lock(fd_contender, exclusive=True):
                    pass
            elapsed = time.monotonic() - start
            pc.release_lock(fd_holder)
            # Single-shot: nowhere near the multi-second timeout ceiling.
            assert elapsed < 1.0, f"on-loop acquire spun for {elapsed:.2f}s"

        try:
            asyncio.run(_contend_on_loop())
        finally:
            os.close(fd_holder)
            os.close(fd_contender)


class TestProcessIdentityPosix:
    def test_get_ppid_of_self_is_positive_on_posix(self):
        # POSIX: get_ppid parses /proc/<pid>/status PPid: and returns it as a
        # positive int (every live process has a real parent). The existing
        # test_get_ppid_returns_int only checks the type, not the parsed value.
        ppid = pc.get_ppid(os.getpid())
        assert isinstance(ppid, int)
        if pc.IS_POSIX:
            assert ppid > 0

    def test_get_ppid_of_unused_pid_returns_minus_one(self):
        # No /proc/<pid>/status entry -> read_text() raises -> swallowed by the
        # bare except -> get_ppid returns the -1 failure sentinel (never raises).
        assert pc.get_ppid(2_000_000_000) == -1

    def test_get_ppid_of_child_equals_self(self):
        # A child we spawn must report THIS process as its parent. Exercises the
        # Linux /proc PPid parse + int(...) return for a non-self pid.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert child.poll() is None  # alive
            ppid = pc.get_ppid(child.pid)
            assert isinstance(ppid, int)
            if pc.IS_POSIX:
                assert ppid == os.getpid()
        finally:
            child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def test_process_matches_true_for_a_child_with_a_known_token(self):
        # Asserts against a token we KNOW is in the child's command line,
        # instead of assuming the running interpreter's own command line
        # contains "python". That assumption held on Linux (/proc/<pid>/cmdline
        # names the interpreter) and failed on the first macOS run: there
        # process_matches shells out to `ps -o command=`, the hosted runner
        # launches the suite as `.../hostedtoolcache/Python/3.12/x64/bin/pytest`,
        # and the needle comparison is case-sensitive -- "python" is not in
        # "Python". Production needles ("kiro-cli", "claude") appear verbatim in
        # the argv they guard, so only the test's choice of needle was fragile.
        token = "kirocrew-procmatch-probe"
        # Use a readiness pipe: the child signals after exec completes, so we
        # never race /proc/<pid>/cmdline population on a loaded runner.
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import sys, time; sys.stdout.write('R'); sys.stdout.flush(); "
                f"time.sleep(30)  # {token}",
            ],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for the readiness byte (generous timeout for slow CI).
            ready = child.stdout.read(1)
            assert ready == b"R", f"child did not signal readiness: {ready!r}"
            assert child.poll() is None  # still alive after signalling
            if pc.IS_POSIX:
                # /proc/<pid>/cmdline is guaranteed populated after exec, but
                # keep a short retry for edge cases on exotic kernels.
                deadline = time.monotonic() + 10.0
                result = pc.process_matches(child.pid, (token,))
                while not result and time.monotonic() < deadline:
                    time.sleep(0.05)
                    result = pc.process_matches(child.pid, (token,))
                assert result is True
        finally:
            child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def test_process_matches_false_for_self_with_absent_needle(self):
        # Same /proc read as the True case, but a needle that cannot occur in a
        # python interpreter's argv -> any() is False (not via an exception).
        result = pc.process_matches(os.getpid(), ("zzz-not-in-any-cmdline",))
        assert isinstance(result, bool)
        if pc.IS_POSIX:
            assert result is False


class TestProcessArgvMatchesExact:
    """The strict identity check behind reclaiming a recorded-but-orphaned
    child: the WHOLE argv must match, element for element, and every failure
    answers False — an unconfirmable identity must never be signalled."""

    def _spawn(self, token: str):
        argv = [
            sys.executable,
            "-c",
            f"import sys, time; sys.stdout.write('R'); sys.stdout.flush(); "
            f"time.sleep(30)  # {token}",
        ]
        child = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        ready = child.stdout.read(1)
        assert ready == b"R", f"child did not signal readiness: {ready!r}"
        return child, argv

    @staticmethod
    def _reap(child):
        child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def test_exact_argv_matches_and_near_misses_do_not(self):
        if pc.IS_POSIX:
            # A plain binary that does NOT re-exec, so its kernel-visible argv
            # is exactly the spawn argv on Linux AND macOS (a macOS framework
            # python re-execs Python.app and rewrites argv[0], which is a
            # property of the interpreter stand-in, not of the production
            # targets — ssh and the aws v2 binary do not re-exec).
            sleep_bin = shutil.which("sleep") or "/bin/sleep"
            argv = [sleep_bin, "300"]
            child = subprocess.Popen(argv, start_new_session=True, stderr=subprocess.DEVNULL)
        else:
            child, argv = self._spawn("kirocrew-argvexact-probe")
        try:
            if pc.IS_POSIX:
                # Exact match: retry briefly for slow /proc population on
                # loaded runners (same shape as the process_matches test).
                deadline = time.monotonic() + 10.0
                result = pc.process_argv_matches_exact(child.pid, argv)
                while not result and time.monotonic() < deadline:
                    time.sleep(0.05)
                    result = pc.process_argv_matches_exact(child.pid, argv)
                assert result is True
                # Anything less than the whole argv is a different process:
                # a subset (prefix), a superset, and a one-element difference
                # must all answer False — substring semantics are exactly what
                # this function exists to NOT have.
                assert pc.process_argv_matches_exact(child.pid, argv[:-1]) is False
                assert pc.process_argv_matches_exact(child.pid, argv + ["-x"]) is False
                changed = list(argv)
                changed[-1] = changed[-1] + " "
                assert pc.process_argv_matches_exact(child.pid, changed) is False
            else:
                # Windows: element-exact argv equality is not verifiable (the
                # raw command line carries shell quoting, not a vector) — the
                # guard fails closed even for the true argv.
                assert pc.process_argv_matches_exact(child.pid, argv) is False
        finally:
            self._reap(child)

    def test_unconfirmable_identities_answer_false(self):
        # A pid that cannot exist, reserved pids, and an empty expectation all
        # fail closed rather than raising.
        assert pc.process_argv_matches_exact(2_000_000_000, ("x",)) is False
        assert pc.process_argv_matches_exact(0, ("x",)) is False
        assert pc.process_argv_matches_exact(1, ("x",)) is False
        assert pc.process_argv_matches_exact(-5, ("x",)) is False
        assert pc.process_argv_matches_exact(os.getpid(), ()) is False

    def test_own_process_with_wrong_argv_is_false(self):
        result = pc.process_argv_matches_exact(os.getpid(), ("zzz-not-this-interpreter", "--nope"))
        assert result is False


class TestProcessStartTime:
    """The identity source every PID-reuse guard compares before signalling.

    The value is opaque and its units differ per platform; the contract is only
    that it is stable for one process object on one host and that an unreadable
    answer is ``None`` — which every caller treats as "identity unconfirmed, do
    not kill".
    """

    def test_this_process_has_a_stable_identity(self):
        first = pc.process_start_time(os.getpid())
        assert first, "no start-time identity for the running process"
        assert pc.process_start_time(os.getpid()) == first, "identity is not stable"

    def test_an_unreadable_pid_fails_safe(self):
        # PID 0 is never a queryable user process on any supported platform.
        assert pc.process_start_time(0) is None

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc contract")
    def test_linux_reports_stat_field_22(self):
        stat_text = Path(f"/proc/{os.getpid()}/stat").read_text()
        expected = stat_text.rsplit(")", 1)[1].split()[19]
        assert pc.process_start_time(os.getpid()) == expected

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows creation-FILETIME contract")
    def test_windows_reports_a_positive_creation_filetime(self):
        value = pc.process_start_time(os.getpid())
        assert value is not None and value.isdigit()
        assert int(value) > 0

    def test_linux_reads_the_starttime_field_past_a_parenthesised_comm(self, monkeypatch):
        """Splitting on the FIRST ')' would mis-index any comm containing one."""

        tail = " ".join(str(i) for i in range(4, 24))

        class _FakeStatPath:
            def __init__(self, _p):
                pass

            def read_text(self):
                return f"4242 (my (odd) proc) S 1 {tail}"

        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "Path", _FakeStatPath)
        assert pc.process_start_time(4242) == "21"

    def test_a_malformed_stat_line_fails_safe(self, monkeypatch):
        class _FakeStatPath:
            def __init__(self, _p):
                pass

            def read_text(self):
                return "no closing paren here"

        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "Path", _FakeStatPath)
        assert pc.process_start_time(4242) is None

    def test_the_bsd_leg_resolves_ps_through_trusted_system_bin(self, monkeypatch):
        """A PATH-resolved `ps` would let a planted binary forge process identity.

        The value gates a kill, so its source binary must come from the pinned
        lookup rather than whatever `PATH` leads with.
        """
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/usr/bin/ps")
        seen: list[list[str]] = []

        def _check_output(argv, **_k):
            seen.append(list(argv))
            return b" Mon Jan  1 00:00:00 2024\n"

        monkeypatch.setattr(pc.subprocess, "check_output", _check_output)

        assert pc.process_start_time(4242) == "Mon Jan  1 00:00:00 2024"
        assert seen and seen[0][0] == "/usr/bin/ps", "ps was not the pinned binary"

    def test_an_absent_ps_fails_safe(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        assert pc.process_start_time(4242) is None

    def test_undecodable_ps_output_fails_safe(self, monkeypatch):
        """Bytes that are not valid UTF-8 are not an identity.

        A lossy decode would turn unreadable output into a NON-EMPTY string, so
        the caller would treat garbage as a confirmed identity — the fail-OPEN
        direction at a kill boundary, and two different processes whose output
        both decoded to replacement characters would compare equal.
        """
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/usr/bin/ps")
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: b"\xff\xfe not utf-8")
        assert pc.process_start_time(4242) is None

    def test_empty_ps_output_fails_safe(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/usr/bin/ps")
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: b"\n")
        assert pc.process_start_time(4242) is None

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows handle-rights contract")
    def test_windows_identity_does_not_require_terminate_rights(self, monkeypatch):
        """Reading identity must not demand the right to kill.

        This value is what DECIDES whether a kill may happen, so routing it
        through the termination handle (PROCESS_TERMINATE + SYNCHRONIZE) would
        deny the guard for exactly the processes a caller must be most careful
        about — they would read as "identity unconfirmed" for a permissions
        reason rather than a recycling one.
        """

        def _refuse(_pid):
            raise AssertionError("start-time identity opened a termination handle")

        monkeypatch.setattr(pc, "_open_process_termination_handle", _refuse)
        assert pc.process_start_time(os.getpid())


class TestOwnProcessStartTime:
    """The module-cached self identity the metrics exporter stamps on shards.

    The cache IS the contract: every reader in one process must observe the
    same token for the process lifetime, so metric records written before and
    after an in-process provider rebuild stitch into one stream.
    """

    @pytest.fixture(autouse=True)
    def _cold_cache(self, monkeypatch):
        """Start every test on a cold cache and restore the global after.

        Without this, whichever test runs first fills the module global for
        the rest of the worker session, making the first-read assertions
        order-dependent.
        """
        monkeypatch.setattr(pc, "_OWN_START_TIME", None)

    def test_matches_the_identity_token_and_is_stable(self):
        token = pc.own_process_start_time()
        if token is None:
            pytest.skip("process start time unavailable on this platform")
        assert token == pc._own_identity_token(os.getpid())
        assert pc.own_process_start_time() == token

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux boot-scope contract")
    def test_linux_token_is_boot_scoped(self):
        """The durable token carries the boot UUID, not bare start ticks.

        ``/proc`` start ticks count from boot, and metric shards outlive
        boots: a post-reboot process repeating an earlier boot's (PID, ticks)
        pair must still read as a different process.
        """
        ticks = pc.process_start_time(os.getpid())
        boot = pc._linux_boot_id()
        assert ticks
        token = pc.own_process_start_time()
        if boot is None:
            assert token is None
        else:
            assert token == f"{ticks}:{boot}"

    def test_same_ticks_across_boots_yield_distinct_tokens(self, monkeypatch):
        """A repeated (PID, ticks) pair after a reboot is a NEW identity."""
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "process_start_time", lambda _pid: "12345")
        monkeypatch.setattr(pc, "_linux_boot_id", lambda: "boot-aaaa")
        first_boot = pc._own_identity_token(os.getpid())
        monkeypatch.setattr(pc, "_linux_boot_id", lambda: "boot-bbbb")
        second_boot = pc._own_identity_token(os.getpid())
        assert first_boot == "12345:boot-aaaa"
        assert second_boot == "12345:boot-bbbb"
        assert first_boot != second_boot

    def test_a_degraded_read_yields_no_identity_at_all(self, monkeypatch):
        """A token that cannot honor one-token-one-process is refused.

        The aggregator MUTES its value-drop reset heuristic for any stream
        carrying a token, so an aliasable coarse token (bare boot-relative
        ticks, 1s ``lstart``) would merge two lifetimes AND disable the
        detector that catches the merge — strictly worse than no token, which
        routes the stream onto the legacy heuristic.
        """
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "process_start_time", lambda _pid: "12345")
        monkeypatch.setattr(pc, "_linux_boot_id", lambda: None)
        assert pc._own_identity_token(os.getpid()) is None

        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)
        assert pc._own_identity_token(os.getpid()) is None

        # Platforms with only the 1s ``ps`` probe are outside the closed list.
        monkeypatch.setattr(pc.sys, "platform", "freebsd14")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc._own_identity_token(os.getpid()) is None

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc contract")
    def test_darwin_microtime_is_used_when_available(self):
        """The microsecond ``proc_pidinfo`` instant outranks 1s ``ps`` output.

        A PID recycled within one second aliases under ``lstart``; the
        microsecond instant cannot.
        """
        micro = pc._darwin_process_start_microtime(os.getpid())
        if micro is None:
            pytest.skip("libproc unavailable in this environment")
        assert re.fullmatch(r"[1-9]\d*\.\d{6}", micro)
        assert pc.own_process_start_time() == micro

    def test_darwin_microtime_parses_the_bsdinfo_layout(self, monkeypatch):
        """The sec/usec pair is sliced from the pinned struct offsets."""

        class _FakeLib:
            @staticmethod
            def proc_pidinfo(_pid, _flavor, _arg, buf, size):
                raw = bytearray(size)
                raw[pc._DARWIN_PBI_START_TVSEC_OFFSET : pc._DARWIN_PBI_START_TVSEC_OFFSET + 8] = (
                    1724500000
                ).to_bytes(8, "little")
                raw[pc._DARWIN_PBI_START_TVUSEC_OFFSET : pc._DARWIN_PBI_START_TVUSEC_OFFSET + 8] = (
                    42
                ).to_bytes(8, "little")
                buf.raw = bytes(raw)
                return size

        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _FakeLib())
        assert pc._darwin_process_start_microtime(4242) == "1724500000.000042"

    def test_darwin_microtime_refuses_a_mismatched_struct_size(self, monkeypatch):
        """A partial fill means the assumed layout is wrong: answer None."""

        class _ShortLib:
            @staticmethod
            def proc_pidinfo(_pid, _flavor, _arg, _buf, _size):
                return 64

        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _ShortLib())
        assert pc._darwin_process_start_microtime(4242) is None

    def test_darwin_cpu_nanos_parses_the_taskinfo_layout(self, monkeypatch):
        """user+system CPU are sliced from the pinned ``proc_taskinfo`` offsets."""

        class _FakeLib:
            @staticmethod
            def proc_pidinfo(_pid, _flavor, _arg, buf, size):
                raw = bytearray(size)
                raw[pc._DARWIN_PTI_TOTAL_USER_OFFSET : pc._DARWIN_PTI_TOTAL_USER_OFFSET + 8] = (
                    7_000_000_000
                ).to_bytes(8, "little")
                raw[pc._DARWIN_PTI_TOTAL_SYSTEM_OFFSET : pc._DARWIN_PTI_TOTAL_SYSTEM_OFFSET + 8] = (
                    500_000_000
                ).to_bytes(8, "little")
                buf.raw = bytes(raw)
                return size

        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _FakeLib())
        assert pc._darwin_process_cpu_nanos(4242) == 7_500_000_000

    def test_darwin_cpu_nanos_refuses_a_mismatched_struct_size(self, monkeypatch):
        """Same layout check as the start-time probe: a partial fill answers None."""

        class _ShortLib:
            @staticmethod
            def proc_pidinfo(_pid, _flavor, _arg, _buf, _size):
                return 64

        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _ShortLib())
        assert pc._darwin_process_cpu_nanos(4242) is None

    def test_darwin_cpu_nanos_without_libproc_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)
        assert pc._darwin_process_cpu_nanos(4242) is None

    def test_reads_the_platform_once_then_serves_the_cache(self, monkeypatch):
        first = pc.own_process_start_time()  # populate the cache for THIS pid

        def _boom(_pid):
            raise AssertionError("cached identity was re-read from the platform")

        monkeypatch.setattr(pc, "_own_identity_token", _boom)
        assert pc.own_process_start_time() == first

    def test_cache_is_pid_keyed_so_a_forked_child_rereads(self, monkeypatch):
        """A stale inherited cache entry must be recomputed, not served.

        The OTEL SDK re-installs exporters in fork children, so a child that
        served the parent's token would share (PID, identity) with any later
        sibling reusing its PID — the exact merge the identity exists to
        prevent. Simulate the inherited state directly rather than patching
        ``os.getpid`` (other threads read it during the patch window).
        """
        real = pc.own_process_start_time()
        monkeypatch.setattr(pc, "_OWN_START_TIME", (os.getpid() + 1, "inherited-stale"))
        assert pc.own_process_start_time() == real


class TestPidLivenessPosix:
    def test_pid_liveness_alive_for_self(self):
        # POSIX ALIVE path: os.kill(getpid(), 0) succeeds for our own live
        # process, so pid_liveness reports PID_ALIVE.
        assert pc.pid_liveness(os.getpid()) == pc.PID_ALIVE

    def test_pid_liveness_dead_for_unused_pid(self):
        # ProcessLookupError path: a PID well above pid_max is not running,
        # so os.kill(pid, 0) raises ProcessLookupError -> PID_DEAD.
        if pc.IS_POSIX:
            assert pc.pid_liveness(2_000_000_000) == pc.PID_DEAD

    def test_pid_liveness_unsignalable_on_permission_error(self, monkeypatch):
        # EPERM path (cannot be reached as an unprivileged test user): force
        # os.kill to raise PermissionError so pid_liveness returns
        # PID_UNSIGNALABLE. Patch the module's own os.kill; monkeypatch
        # auto-restores it after the test.
        if not pc.IS_POSIX:
            pytest.skip("POSIX EPERM-via-os.kill branch")

        def fake_kill(pid, sig):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(pc.os, "kill", fake_kill)
        assert pc.pid_liveness(os.getpid()) == pc.PID_UNSIGNALABLE

    def test_pid_liveness_unsignalable_on_generic_oserror(self, monkeypatch):
        # Generic-OSError fallback: an unknown errno from os.kill is treated
        # conservatively as PID_UNSIGNALABLE. A bare OSError (not
        # PermissionError) skips the PermissionError clause and hits this one.
        if not pc.IS_POSIX:
            pytest.skip("POSIX generic-OSError-via-os.kill branch")

        def fake_kill(pid, sig):
            raise OSError(errno.EINVAL, "Invalid argument")

        monkeypatch.setattr(pc.os, "kill", fake_kill)
        assert pc.pid_liveness(os.getpid()) == pc.PID_UNSIGNALABLE

    def test_pid_exists_true_on_permission_error(self, monkeypatch):
        # pid_exists EPERM branch: a PID we exist-but-cannot-signal must still
        # count as existing. Force os.kill to raise PermissionError; pid_exists
        # returns True. monkeypatch auto-restores.
        if not pc.IS_POSIX:
            pytest.skip("POSIX EPERM-via-os.kill branch")

        def fake_kill(pid, sig):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(pc.os, "kill", fake_kill)
        assert pc.pid_exists(os.getpid()) is True


class TestProcessDescendants:
    def test_descendants_from_parent_map_walks_full_tree(self):
        parent_map = {
            11: 10,
            12: 11,
            13: 10,
            14: 12,
            99: 1,
            10: 14,
        }

        assert pc._descendants_from_parent_map(10, parent_map) == [11, 13, 12, 14]

    @pytest.mark.asyncio
    async def test_descendant_termination_handles_async_is_empty_on_posix(self):
        if pc.IS_WINDOWS:
            pytest.skip("POSIX process groups do not need retained descendants")

        assert await pc.descendant_termination_handles_async(os.getpid()) == {}

    def test_windows_parent_map_raises_when_snapshot_creation_fails(self, monkeypatch):
        class FakeCall:
            def __init__(self, result):
                self.result = result

            def __call__(self, *_args):
                return self.result

        kernel32 = types.SimpleNamespace(
            CreateToolhelp32Snapshot=FakeCall(pc.wintypes.HANDLE(-1).value),
            Process32First=FakeCall(False),
            Process32Next=FakeCall(False),
            CloseHandle=FakeCall(True),
        )
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc.ctypes,
            "windll",
            types.SimpleNamespace(kernel32=kernel32),
            raising=False,
        )

        with pytest.raises(OSError, match="process snapshot"):
            pc._windows_process_parent_map()

    def test_windows_parent_map_raises_when_initial_enumeration_fails(self, monkeypatch):
        class FakeCall:
            def __init__(self, result):
                self.result = result
                self.calls = 0

            def __call__(self, *_args):
                self.calls += 1
                return self.result

        close_handle = FakeCall(True)
        kernel32 = types.SimpleNamespace(
            CreateToolhelp32Snapshot=FakeCall(123),
            Process32First=FakeCall(False),
            Process32Next=FakeCall(False),
            CloseHandle=close_handle,
        )
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc.ctypes,
            "windll",
            types.SimpleNamespace(kernel32=kernel32),
            raising=False,
        )

        with pytest.raises(OSError, match="first process"):
            pc._windows_process_parent_map()

        assert close_handle.calls == 1

    def test_windows_parent_map_raises_when_later_enumeration_fails(self, monkeypatch):
        class FakeCall:
            def __init__(self, result):
                self.result = result
                self.calls = 0

            def __call__(self, *_args):
                self.calls += 1
                return self.result

        close_handle = FakeCall(True)
        kernel32 = types.SimpleNamespace(
            CreateToolhelp32Snapshot=FakeCall(123),
            Process32First=FakeCall(True),
            Process32Next=FakeCall(False),
            CloseHandle=close_handle,
            SetLastError=FakeCall(True),
            GetLastError=FakeCall(5),
        )
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc.ctypes,
            "windll",
            types.SimpleNamespace(kernel32=kernel32),
            raising=False,
        )
        with pytest.raises(OSError, match="process enumeration"):
            pc._windows_process_parent_map()

        assert close_handle.calls == 1

    def test_windows_descendant_lifetime_accepts_genuine_pre_exit_child(
        self,
        monkeypatch,
    ):
        parent_maps = iter(({101: 100}, {101: 100}))
        closed: list[int] = []
        identities = {
            8001: (100, 10, 20),
            9001: (101, 15, None),
        }
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: next(parent_maps))
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 9001)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            identities.get,
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        assert pc.descendant_termination_handles(100, {}, 8001) == {101: 9001}
        assert closed == []

    def test_windows_descendant_lifetime_rejects_post_exit_recycled_child(
        self,
        monkeypatch,
    ):
        parent_maps = iter(({101: 100}, {101: 100}))
        closed: list[int] = []
        identities = {
            8001: (100, 10, 20),
            9001: (101, 21, None),
        }
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: next(parent_maps))
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 9001)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            identities.get,
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        assert pc.descendant_termination_handles(100, {}, 8001) == {}
        assert closed == [9001]

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows process handles only")
    def test_retained_handle_targets_original_windows_child(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=pc.CREATE_NEW_PROCESS_GROUP,
        )
        handles: dict[int, int] = {}
        root_handle = pc._open_process_termination_handle(os.getpid())
        assert root_handle is not None
        try:
            deadline = time.monotonic() + 5
            while child.pid not in handles and time.monotonic() < deadline:
                handles.update(
                    pc.descendant_termination_handles(
                        os.getpid(),
                        handles,
                        root_handle,
                    )
                )
                if child.pid not in handles:
                    time.sleep(0.05)
            assert child.pid in handles
            assert pc.terminate_process_handle(handles[child.pid]) is True
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            for handle in handles.values():
                pc.close_process_handle(handle)
            pc.close_process_handle(root_handle)


@pytest.mark.skipif(
    not pc.IS_WINDOWS,
    reason="exercises the real Windows ctypes identity path (ctypes.WinDLL, "
    "wintypes.FILETIME); the logic is Windows-native and runs on the Windows shard",
)
class TestWindowsHandleIdentityExitFiletimeRace:
    """GetExitCodeProcess reports the exit before the exit FILETIME is published.

    A handle read inside that window looks exited-with-exit_time==0. Treating it
    as "no identity" made ``descendant_termination_handles`` raise on a healthy
    tree, which surfaced as a ~1-in-3 false "Install Kiro CLI" on Windows.
    """

    # The pid every faked handle below reports.
    FAKE_PID = 4242

    @classmethod
    def _kernel32(cls, exit_filetimes):
        """Fake kernel32 replaying *exit_filetimes* from successive time reads.

        A ``0`` entry is the exited-but-unpublished window; a non-zero entry is a
        published exit FILETIME. The process always reports as exited.
        """

        reads = iter(exit_filetimes)

        class _Fn:
            """Stands in for a ctypes function pointer (assignable argtypes)."""

            argtypes: list = []
            restype = None

            def __init__(self, impl):
                self._impl = impl

            def __call__(self, *args):
                return self._impl(*args)

        def _get_process_times(_handle, creation, exit_, _kernel, _user):
            creation._obj.dwHighDateTime = 0
            creation._obj.dwLowDateTime = 100
            exit_._obj.dwHighDateTime = 0
            exit_._obj.dwLowDateTime = next(reads, 0)
            return 1

        def _get_exit_code(_handle, code):
            code._obj.value = 0  # any value but STILL_ACTIVE (259)
            return 1

        return types.SimpleNamespace(
            GetProcessId=_Fn(lambda _handle: cls.FAKE_PID),
            GetProcessTimes=_Fn(_get_process_times),
            GetExitCodeProcess=_Fn(_get_exit_code),
        )

    def test_identity_retries_until_exit_filetime_is_published(self, monkeypatch):
        # First two reads land inside the unpublished window; the third has the
        # real exit time. The identity must be returned, not refused.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0, 0, 0, 777])
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)

        identity = pc._windows_process_handle_identity(5)

        assert identity is not None
        pid, creation, exit_time = identity
        assert (pid, creation, exit_time) == (4242, 100, 777)

    def test_identity_gives_up_when_exit_filetime_never_publishes(self, monkeypatch):
        # A handle whose exit time never appears must still be refused, so the
        # PID-recycling guard the caller depends on is not weakened into a
        # blanket "assume it is fine".
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0] * 500)
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)
        monkeypatch.setattr(pc, "_WINDOWS_EXIT_FILETIME_TIMEOUT_SECS", 0.01)

        assert pc._windows_process_handle_identity(5) is None

    def test_descendant_scan_does_not_raise_for_a_root_inside_the_window(
        self,
        monkeypatch,
    ):
        # The defect's actual blast radius: an exited root whose FILETIME has not
        # published yet must not make the scan raise "root handle identity
        # mismatch" at its caller, which is what failed the whole kiro-cli probe.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = self._kernel32([0, 0, 555])
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: {})

        # 4242 is the pid the fake handle reports, so the root identity matches.
        assert pc.descendant_termination_handles(4242, {}, 8001) == {}


class TestKillSubprocessPosix:
    @pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX os.kill path; Windows uses taskkill")
    def test_kill_pid_terminates_real_child_posix(self):
        # POSIX kill_pid success path (os.kill + return True): spawn a real
        # long-lived child, confirm it is alive, SIGKILL it via the shim, then
        # reap it so its PID leaves the table and pid_exists() flips to False.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert pc.pid_exists(child.pid) is True
            assert pc.kill_pid(child.pid, pc.SIGKILL) is True
            # Reap the killed child so it is no longer a zombie occupying the
            # PID; otherwise os.kill(pid, 0) would still report it as existing.
            child.wait(timeout=5)
            deadline = time.monotonic() + 2.0
            while pc.pid_exists(child.pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert pc.pid_exists(child.pid) is False
        finally:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    @pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX killpg path; Windows uses taskkill /T")
    def test_kill_process_tree_kills_group_posix(self):
        # POSIX kill_process_tree success path (os.getpgid + os.killpg + return
        # True): spawn the child in its OWN session/process group so its pgid
        # equals its pid, then tree-kill the group and confirm it is gone.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            assert os.getpgid(child.pid) == child.pid
            assert pc.pid_exists(child.pid) is True
            assert pc.kill_process_tree(child.pid, pc.SIGKILL) is True
            child.wait(timeout=5)
            deadline = time.monotonic() + 2.0
            while pc.pid_exists(child.pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert pc.pid_exists(child.pid) is False
        finally:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


class TestTaskkillErrorMapping:
    """Regression guards for the Windows taskkill rc -> exception mapping.

    Ensures the shim raises the same exception TYPES the POSIX branch raises
    so callers' ``except (ProcessLookupError, PermissionError, OSError)``
    guards fire uniformly on both platforms. Runs on POSIX by monkeypatching
    IS_WINDOWS + subprocess.run — the mapping is platform-independent code,
    and doing so keeps the Windows security branches regression-guarded on
    the Linux CI fleet.
    """

    @staticmethod
    def _fake_run(rc: int, stderr: bytes = b""):
        def _run(*_a, **_kw):
            r = types.SimpleNamespace(returncode=rc, stdout=b"", stderr=stderr)
            return r

        return _run

    def test_taskkill_rc128_maps_to_process_lookup(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(128, b"process not found"))
        with pytest.raises(ProcessLookupError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(ProcessLookupError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_rc5_maps_to_permission_error(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(5, b"access denied"))
        with pytest.raises(PermissionError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(PermissionError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_generic_rc_maps_to_oserror(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(42, b"weird error"))
        with pytest.raises(OSError) as ei:
            pc.kill_pid(99999, pc.SIGKILL)
        # not one of the more specific subclasses
        assert not isinstance(ei.value, (ProcessLookupError, PermissionError))

    def test_taskkill_success_returns_true_on_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(0))
        assert pc.kill_pid(99999, pc.SIGKILL) is True
        assert pc.kill_process_tree(99999, pc.SIGKILL) is True

    def test_taskkill_subprocess_error_wraps_as_oserror(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

        def _boom(*_a, **_kw):
            raise FileNotFoundError(2, "taskkill.exe not found")

        monkeypatch.setattr(pc.subprocess, "run", _boom)
        with pytest.raises(OSError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(OSError):
            pc.kill_process_tree(99999, pc.SIGKILL)


class TestRestrictToOwnerArgvOnLinux:
    """Regression guard for the Windows owner-only DACL, exercised on Linux.

    Runs on the Linux CI fleet by monkeypatching IS_WINDOWS + the DACL writer --
    the decision of WHICH principals to grant, and whether the grants are
    inheritable, is platform-independent code, and without this it is only
    exercised on the author's manual Windows E2E (skipif-Windows tests don't run
    on AL2). A regression that drops the S-1-3-4 grant or the invoking-user grant
    silently reopens the parent-inherited-DACL gap.

    The observable used to be the ``icacls`` argv. The lockdown now goes through
    ``windows_acl.apply_owner_only`` in-process, so the observable is that call's
    arguments instead -- the same seam, one layer down, and still the only thing
    visible off Windows (NTFS reports 0o666 for any file regardless of its DACL,
    so no mode assertion can substitute).
    """

    @staticmethod
    def _capture(monkeypatch, sid="S-1-5-21-1-2-3-1000"):
        """Force the Windows branch and record the DACL write instead of doing it."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # Reset the SID memo so the monkeypatched stub wins. The lockdown reads
        # current_user_sid, which is token-only and cannot spawn.
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "current_user_sid", lambda: sid)
        calls: list[dict] = []

        def fake_apply(path, *, inherit, sids, **_kw):
            calls.append({"path": os.fspath(path), "inherit": inherit, "sids": tuple(sids)})

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", fake_apply)
        return calls

    def test_dacl_grants_owner_rights_and_the_invoking_user(self, tmp_path, monkeypatch):
        calls = self._capture(monkeypatch)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert len(calls) == 1, calls
        assert calls[0]["path"] == os.fspath(f)
        # Bare SIDs: the `*` prefix is icacls argv syntax and the API rejects it.
        assert calls[0]["sids"] == ("S-1-3-4", "S-1-5-21-1-2-3-1000"), calls[0]
        assert not any(s.startswith("*") for s in calls[0]["sids"]), calls[0]

    def test_write_failure_raises_oserror(self, tmp_path, monkeypatch):
        # With a resolvable SID, a failure to apply the DACL still raises OSError
        # so the caller's warn-and-continue handler fires. Complements the
        # None-SID early-raise test below.
        self._capture(monkeypatch, sid="S-1-5-21-9-9-9-9")

        def boom(path, *, inherit, sids, **_kw):
            raise pc.windows_acl.AclWriteFailed("SetNamedSecurityInfoW failed (error 5)")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", boom)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)

    def test_unreadable_platform_api_raises_oserror(self, tmp_path, monkeypatch):
        # AclUnavailable (the descriptor API could not be loaded at all) must
        # reach the caller as OSError too, not escape as a bare RuntimeError that
        # no caller's handler catches.
        self._capture(monkeypatch, sid="S-1-5-21-9-9-9-9")

        def boom(path, *, inherit, sids, **_kw):
            raise pc.windows_acl.AclUnavailable("cannot load the Windows security API")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", boom)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)

    def test_none_sid_raises_before_icacls_to_avoid_lockout(self, tmp_path, monkeypatch):
        # When current_user_sid() returns None (the process token read is
        # unavailable), restrict_to_owner MUST refuse to apply a lockdown —
        # granting only S-1-3-4 (Owner Rights) with inheritance stripped
        # locks non-owner users out of their own file (elevated first-run,
        # backup restore, SYSTEM-context service scenarios). Fail-loud with
        # OSError BEFORE touching the DACL; the caller's warn handler fires
        # and the pre-existing DACL is preserved unchanged.
        calls = self._capture(monkeypatch, sid=None)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError) as ei:
            pc.restrict_to_owner(f)
        assert "current user SID" in str(ei.value)
        # The DACL must NOT have been touched — the whole point is to avoid
        # applying a half-configured lockdown.
        assert calls == [], f"no DACL write may happen when the SID is unknown: {calls}"

    def test_directory_grants_are_inheritable(self, tmp_path, monkeypatch):
        # The bug this pins: make_owner_only_dir used to delegate to the
        # FILE-shaped restrict_to_owner, whose grants are not inheritable. Those
        # ACEs apply to the directory alone, so a file created inside an
        # "owner-only" directory got no explicit ACE and fell back to the
        # creating token's default DACL.
        calls = self._capture(monkeypatch)
        d = tmp_path / "secrets-dir"
        d.mkdir()
        pc.restrict_dir_to_owner(d)
        assert len(calls) == 1, calls
        assert calls[0]["path"] == os.fspath(d)
        # Both grants must propagate to children, or the directory guarantee
        # covers nothing created inside it.
        assert calls[0]["inherit"] is True, calls[0]
        assert calls[0]["sids"] == ("S-1-3-4", "S-1-5-21-1-2-3-1000"), calls[0]

    def test_file_grants_stay_non_inheritable(self, tmp_path, monkeypatch):
        # The other half of the split, asserted negatively: inheritance flags are
        # meaningless on a file, so restrict_to_owner must NOT acquire them when
        # the directory shape does. Without this, "just make both inheritable"
        # reads as a passing simplification.
        calls = self._capture(monkeypatch)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert len(calls) == 1, calls
        assert calls[0]["inherit"] is False, calls[0]

    def test_owner_rights_is_not_granted_twice(self, tmp_path, monkeypatch):
        # Degenerate case: when the invoking user's SID IS Owner Rights, the two
        # grants collapse to one rather than producing a duplicate ACE.
        calls = self._capture(monkeypatch, sid="S-1-3-4")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert calls[0]["sids"] == ("S-1-3-4",), calls[0]

    def test_file_helper_warns_when_handed_a_directory(self, tmp_path, monkeypatch, caplog):
        # The misuse guard. The DACL-argument tests cannot see this from the call
        # site, so a directory reaching the file-shaped helper has to be caught
        # here -- it tightens the directory but leaves files created inside on the
        # creating token's default DACL. Warn, not raise: the ACE still applies
        # to the named object, so the lockdown is partial rather than absent.
        #
        # _capture stubs the DACL writer: this test is about the warning, and the
        # real writer refuses off Windows (_load raises AclUnavailable), which
        # would fail this on the POSIX CI runners while passing on Windows.
        self._capture(monkeypatch)
        d = tmp_path / "a-directory"
        d.mkdir()
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.restrict_to_owner(d)
        assert any("not inheritable" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_file_helper_stays_quiet_for_a_file(self, tmp_path, monkeypatch, caplog):
        # The guard must not fire on the helper's actual purpose, or every
        # secret-file lockdown would emit a spurious warning.
        self._capture(monkeypatch)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.restrict_to_owner(f)
        assert not [r for r in caplog.records if "not inheritable" in r.getMessage()]

    def test_directory_shape_uses_0o700_on_posix(self, tmp_path, monkeypatch):
        # The POSIX half of the split: 0o700, not the file helper's 0o600 —
        # a directory without the execute bit is not traversable at all.
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        modes: list[int] = []
        monkeypatch.setattr(pc.os, "chmod", lambda p, m: modes.append(m))
        pc.restrict_dir_to_owner(tmp_path)
        assert modes == [0o700], modes


class TestChmodShimsApply:
    def test_fchmod_safe_applies_mode_on_posix(self, tmp_path):
        # POSIX: fchmod_safe must actually apply the mode to the open fd. Verify
        # via os.fstat (the assert is POSIX-only; Windows has no perm bits).
        f = tmp_path / "fchmod-apply.txt"
        f.write_text("x")
        fd = os.open(str(f), os.O_RDONLY)
        try:
            pc.fchmod_safe(fd, 0o600)
            if pc.IS_POSIX:
                assert os.fstat(fd).st_mode & 0o777 == 0o600
        finally:
            os.close(fd)

    def test_fchmod_safe_swallows_oserror(self, tmp_path, monkeypatch):
        # The except branch: os.fchmod raising OSError must be logged + swallowed,
        # never propagated. Force the error since a real fd would just succeed.
        if not pc.IS_POSIX:
            pytest.skip("POSIX os.fchmod branch")
        f = tmp_path / "fchmod-err.txt"
        f.write_text("x")
        fd = os.open(str(f), os.O_RDONLY)

        def boom(*args, **kwargs):
            raise OSError("forced")

        monkeypatch.setattr(pc.os, "fchmod", boom)
        try:
            pc.fchmod_safe(fd, 0o600)  # must NOT raise out
        finally:
            os.close(fd)

    def test_chmod_safe_applies_mode_on_posix(self, tmp_path):
        # POSIX: chmod_safe must apply the mode to the path on disk.
        f = tmp_path / "chmod-apply.txt"
        f.write_text("x")
        pc.chmod_safe(str(f), 0o640)
        if pc.IS_POSIX:
            assert oct(os.stat(str(f)).st_mode & 0o777) == "0o640"

    def test_chmod_safe_swallows_oserror(self, tmp_path, monkeypatch):
        # The except branch: os.chmod raising OSError is logged + swallowed.
        if not pc.IS_POSIX:
            pytest.skip("POSIX os.chmod branch")
        f = tmp_path / "chmod-err.txt"
        f.write_text("x")

        def boom(*args, **kwargs):
            raise OSError("forced")

        monkeypatch.setattr(pc.os, "chmod", boom)
        pc.chmod_safe(str(f), 0o640)  # must NOT raise out


#: An ACE that icacls prints with a bare ``(I)`` flag is INHERITED, so its presence
#: means ``/inheritance:r`` did not take. Matching the flag rather than a rights token
#: keeps this locale-independent: ``(I)`` is a flag spelling, not a display name.
_INHERITED_ACE_RE = re.compile(r"\(I\)")

#: The Owner Rights principal, in either spelling icacls may print: the raw
#: ``S-1-3-4`` SID that ``restrict_to_owner`` grants, or the display name Windows
#: substitutes for it. Both are accepted because the substitution is LOCALIZED --
#: an English host prints ``OWNER RIGHTS`` and a translated one does not, so pinning
#: a single spelling turns a security assertion into a system-language assertion.
_OWNER_RIGHTS_FULL_RE = re.compile(r"(?:OWNER RIGHTS|S-1-3-4)\s*:\s*\(F\)")


def _owner_only_dacl_violations(icacls_dump: str) -> list[str]:
    """Reasons an ``icacls <path>`` dump is not the owner-only DACL we applied.

    An empty list means compliant. The predicate is factored out of the Windows
    test so it is exercised on every platform: the icacls spawn itself only runs on
    Windows, and a predicate that silently matches nothing there leaves the
    secret-at-rest posture (token signing key, per-app secrets, refresh-token state,
    snapshot tarball, cron internal-secret temp file) verified by nothing at all.
    """
    problems: list[str] = []
    if not _OWNER_RIGHTS_FULL_RE.search(icacls_dump):
        problems.append("no full-control ACE for Owner Rights (S-1-3-4)")
    if _INHERITED_ACE_RE.search(icacls_dump):
        # Any surviving inherited ACE is a finding, not just an inherited (F):
        # an inherited (RX) or (M) for Users still lets another local principal
        # read the secret.
        problems.append("an inherited ACE survived /inheritance:r")
    return problems


class TestOwnerOnlyDaclPredicate:
    """Cover the DACL predicate on the POSIX matrix, where it always executes.

    ``test_applies_owner_only_dacl_on_windows`` can only run on Windows, so without
    these the predicate it asserts through would be unverified everywhere the suite
    actually runs. Dumps are realistic ``icacls`` output shapes.
    """

    _LOCKED = (
        "C:\\Temp\\x\\secret.key OWNER RIGHTS:(F)\n"
        "                        RUNNER\\runneradmin:(F)\n"
        "\n"
        "Successfully processed 1 files; Failed processing 0 files.\n"
    )

    def test_locked_down_dump_has_no_violations(self):
        assert _owner_only_dacl_violations(self._LOCKED) == []

    def test_sid_spelling_of_owner_rights_is_accepted(self):
        # A host that does not resolve S-1-3-4 to a display name must still pass;
        # otherwise the Windows assertion fails for a correctly locked file.
        dump = self._LOCKED.replace("OWNER RIGHTS", "S-1-3-4")
        assert _owner_only_dacl_violations(dump) == []

    def test_missing_owner_rights_ace_is_flagged(self):
        dump = self._LOCKED.replace("OWNER RIGHTS:(F)", "RUNNER\\runneradmin:(RX)")
        assert any("Owner Rights" in p for p in _owner_only_dacl_violations(dump))

    def test_surviving_inherited_ace_is_flagged(self):
        dump = (
            "C:\\Temp\\x\\secret.key OWNER RIGHTS:(F)\n"
            "                        BUILTIN\\Users:(I)(RX)\n"
        )
        assert any("inherited" in p for p in _owner_only_dacl_violations(dump))

    def test_owner_rights_without_full_control_is_flagged(self):
        # A downgrade from (F) to (RX) must not read as compliant.
        dump = self._LOCKED.replace("OWNER RIGHTS:(F)", "OWNER RIGHTS:(RX)")
        assert any("Owner Rights" in p for p in _owner_only_dacl_violations(dump))


class TestRestrictToOwner:
    """Fail-loud owner-only lockdown used by every ~/.kirocrew secret writer.

    The review finding was that the earlier
    ``if IS_POSIX: os.chmod(...)`` guard left Windows with NO per-file owner-only
    restriction on the token signing key, per-app secrets, refresh-token state,
    snapshot tarball, and cron internal-secret temp file — a secret-at-rest
    posture regression. ``restrict_to_owner`` closes that: POSIX chmod 0o600,
    Windows an owner-only DACL applied via icacls (S-1-3-4 = Owner Rights).
    """

    def test_applies_owner_only_mode_on_posix(self, tmp_path):
        # POSIX path: exact 0o600 mode on disk. Verified only on POSIX because
        # NTFS has no ``st_mode`` perm bits and would report 0o666/0o444 based
        # on the read-only attribute, not the DACL.
        if not pc.IS_POSIX:
            pytest.skip("POSIX chmod branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        assert os.stat(str(f)).st_mode & 0o777 == 0o600

    def test_propagates_oserror_on_posix(self, tmp_path, monkeypatch):
        # The fail-loud contract: OSError from os.chmod MUST propagate so the
        # security-warning handlers in the callers (token_secret,
        # refresh_tokens, snapshot, cron_script, server, token_auth) fire.
        # Distinct from chmod_safe (which swallows). Regression guard.
        if not pc.IS_POSIX:
            pytest.skip("POSIX chmod branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)

        def boom(*args, **kwargs):
            raise OSError(errno.EPERM, "forced")

        monkeypatch.setattr(pc.os, "chmod", boom)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)

    def test_applies_owner_only_dacl_on_windows(self, tmp_path):
        # Windows path: apply the DACL in-process, then re-read it via icacls to
        # confirm the owner-only shape end-to-end. Reading through the external
        # tool is deliberate here -- it is an independent check, not the same
        # ctypes code that wrote the descriptor. Windows is the ONLY platform
        # that can execute this branch, so the node id must never be added to
        # windows-expected-failures.txt: listed there alongside this self-skip it
        # would run on no platform at all, and the DACL would be the one control
        # in the secret-at-rest posture that nothing verifies.
        if not pc.IS_WINDOWS:
            pytest.skip("Windows DACL branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        out = subprocess.check_output(
            ["icacls", str(f)],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
        assert _owner_only_dacl_violations(out) == [], out

    def test_propagates_oserror_on_windows_when_the_dacl_write_fails(self, tmp_path, monkeypatch):
        # The fail-loud contract on Windows: a DACL that cannot be applied MUST
        # raise OSError so the caller's warn-and-continue handler fires
        # (dead-code otherwise, per review-bot). Simulate at the writer seam --
        # there is no longer a subprocess to make un-launchable, and the failure
        # this models (SetNamedSecurityInfoW returning ERROR_ACCESS_DENIED on a
        # file whose owner we cannot change) is not reproducible on demand.
        if not pc.IS_WINDOWS:
            pytest.skip("Windows DACL branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)

        def boom(path, *, inherit, sids, **_kw):
            raise pc.windows_acl.AclWriteFailed("SetNamedSecurityInfoW failed (error 5)")

        monkeypatch.setattr(pc.windows_acl, "apply_owner_only", boom)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)


class TestResourceShimFailures:
    def test_proc_rss_bytes_returns_zero_when_every_source_fails(self, monkeypatch):
        # getrusage is no longer the primary source for proc_rss_bytes -- it is
        # the labelled last-resort peak -- so reaching 0 now needs BOTH the
        # current-RSS reader and the fallback to fail. Asserting only the
        # getrusage failure would pass on a platform whose primary reader was
        # silently removed.
        if not pc.IS_POSIX:
            pytest.skip("POSIX resource.getrusage branch")

        def boom(*args, **kwargs):
            raise OSError("getrusage failed")

        monkeypatch.setattr(pc.resource, "getrusage", boom)
        monkeypatch.setattr(pc, "_linux_current_rss_bytes", lambda: None)
        monkeypatch.setattr(pc, "_macos_current_rss_bytes", lambda: None)
        assert pc.proc_rss_bytes() == 0

    def test_proc_peak_rss_bytes_returns_zero_on_getrusage_failure(self, monkeypatch):
        # The peak reading has getrusage as its ONLY POSIX source, so its
        # failure branch is still a plain 0.
        if not pc.IS_POSIX:
            pytest.skip("POSIX resource.getrusage branch")

        def boom(*args, **kwargs):
            raise OSError("getrusage failed")

        monkeypatch.setattr(pc.resource, "getrusage", boom)
        assert pc.proc_peak_rss_bytes() == 0

    def test_proc_cpu_seconds_returns_zero_on_getrusage_failure(self, monkeypatch):
        # The failure branch: getrusage raising OSError must yield 0.0, not raise.
        if not pc.IS_POSIX:
            pytest.skip("POSIX resource.getrusage branch")

        def boom(*args, **kwargs):
            raise OSError("getrusage failed")

        monkeypatch.setattr(pc.resource, "getrusage", boom)
        assert pc.proc_cpu_seconds() == 0.0

    def test_raise_nofile_soft_limit_executes_setrlimit(self):
        # Exercise the POSIX getrlimit/setrlimit branch with a real limit nudge,
        # then restore the original limit so no other test is affected. Lower the
        # soft limit first (never the hard limit) so the subsequent shim call
        # takes the `soft < target` setrlimit path; restore in finally.
        if not pc.IS_POSIX:
            pytest.skip("POSIX RLIMIT_NOFILE branch")
        soft, hard = pc.resource.getrlimit(pc.resource.RLIMIT_NOFILE)
        lowered = max(64, (soft if soft != pc.resource.RLIM_INFINITY else hard) // 2)
        try:
            pc.resource.setrlimit(pc.resource.RLIMIT_NOFILE, (lowered, hard))
            # target above the lowered soft limit -> setrlimit branch executes.
            pc.raise_nofile_soft_limit(lowered + 1)
            new_soft = pc.resource.getrlimit(pc.resource.RLIMIT_NOFILE)[0]
            assert new_soft >= lowered + 1
        finally:
            pc.resource.setrlimit(pc.resource.RLIMIT_NOFILE, (soft, hard))

    def test_raise_nofile_soft_limit_swallows_setrlimit_error(self, monkeypatch):
        # The except branch: if setrlimit raises (e.g. EPERM raising the soft
        # limit on a locked-down host), the shim logs at debug and never raises.
        if not pc.IS_POSIX:
            pytest.skip("POSIX RLIMIT_NOFILE branch")

        def boom(*args, **kwargs):
            raise OSError("setrlimit denied")

        # getrlimit reports a soft below the target so the setrlimit call is
        # attempted (and then fails), exercising the try-body + except.
        monkeypatch.setattr(pc.resource, "getrlimit", lambda which: (100, 1_000_000))
        monkeypatch.setattr(pc.resource, "setrlimit", boom)
        pc.raise_nofile_soft_limit(500)  # must NOT raise out


class TestFindPythonInterpreterReal:
    def test_real_resolve_returns_none_or_valid_python(self):
        # No mocks: drive the REAL resolution loop. On the Linux build host a
        # versioned python3.1x resolves and runs the version probe, returning
        # its path; in a stripped sandbox nothing resolves and we get None.
        # Tolerant either-way so it can never flake.
        got = pc.find_python_interpreter()
        assert got is None or isinstance(got, str)
        if got is not None:
            assert os.path.exists(got)
            assert "python" in got.lower()

    def test_returns_none_when_version_probe_raises(self, monkeypatch):
        # Force the version-probe subprocess to fail for a resolvable, non-stub
        # path: the except (OSError, ValueError, SubprocessError) -> continue
        # branch fires for every candidate, so the loop exhausts -> None.
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3.99")

        def boom(*args, **kwargs):
            raise subprocess.SubprocessError("probe failed")

        monkeypatch.setattr(pc.subprocess, "check_output", boom)
        assert pc.find_python_interpreter() is None

    def test_version_gate_ignores_a_sitecustomize_decoy_on_pythonpath(self, tmp_path, monkeypatch):
        # The selection-side twin of test_origin_probe_ignores_pythonpath: at
        # child startup the ``site`` module imports any ``sitecustomize.py``
        # found on the caller's PYTHONPATH, and that module can monkeypatch
        # ``sys.version_info`` — here forcing this real >= 3.12 interpreter to
        # report 3.4, which would make the version gate reject it and steer
        # selection. The gate runs the probe isolated (-I), so the decoy is
        # never imported and the candidate is judged by its REAL version.
        # This spawns a real child; the probe is a read-only version query
        # that creates nothing, so no cwd pin is needed.
        decoy = tmp_path / "decoy-pythonpath"
        decoy.mkdir()
        (decoy / "sitecustomize.py").write_text(
            "import sys\nsys.version_info = (3, 4, 0, 'final', 0)\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PYTHONPATH", str(decoy))
        # Every candidate name resolves to this suite's own interpreter — a
        # real, runnable >= 3.12 CPython on every platform CI runs.
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)

        assert pc.find_python_interpreter() == sys.executable


class TestFindListeningPidsErrors:
    def test_returns_empty_when_lsof_missing(self, monkeypatch):
        # Simulate lsof not being installed: check_output raises
        # FileNotFoundError -> the except returns [] (fail-closed).
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")

        def no_lsof(*args, **kwargs):
            raise FileNotFoundError("lsof")

        monkeypatch.setattr(pc.subprocess, "check_output", no_lsof)
        assert pc.find_listening_pids(59998) == []

    def test_dedupes_pids_from_lsof_output(self, monkeypatch):
        # lsof can emit the same (pid, address) socket multiple times (one row
        # per fd) and one PID can hold several addresses on the port; the PID
        # accessor must dedupe while preserving first-seen order.
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")
        blob = "p111\nn127.0.0.1:7777\nn127.0.0.1:7777\nn*:7777\np222\nn[::1]:7777\n"
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: blob)
        assert pc.find_listening_pids(7777) == [111, 222]

    def test_posix_listeners_carry_their_local_address(self, monkeypatch):
        # The lsof -Fptn field output attributes each LISTEN socket's local
        # address AND family to its owning PID, so callers can scope ownership
        # to the address they actually probed (family is what tells the two
        # wildcard binds apart — lsof prints both as ``*``). v6 brackets are
        # stripped; rows for a different port (defensive — the -i filter
        # already scopes) and malformed p-lines are ignored.
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")
        blob = (
            "p111\n"
            "tIPv4\n"
            "n127.0.0.1:7777\n"
            "p222\n"
            "tIPv6\n"
            "n[::1]:7777\n"
            "tIPv4\n"
            "n192.168.1.5:7777\n"
            "pbogus\n"
            "n10.0.0.1:7777\n"
            "p333\n"
            "tIPv4\n"
            "n*:7778\n"
        )
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: blob)
        assert pc.find_port_listeners(7777) == [
            pc.PortListener(111, "127.0.0.1", "4"),
            pc.PortListener(222, "::1", "6"),
            pc.PortListener(222, "192.168.1.5", "4"),
        ]

    def test_posix_lookup_is_bounded_by_a_timeout(self, monkeypatch):
        # A wedged lsof (stale mount, jammed process table) must degrade to
        # "no listener found" instead of hanging every port->PID caller: the
        # spawn carries a timeout, and its expiry folds into [].
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")
        captured: dict = {}

        def _capture(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

        monkeypatch.setattr(pc.subprocess, "check_output", _capture)
        assert pc.find_port_listeners(7777) == []
        assert captured["kwargs"].get("timeout") == pc._LSOF_TIMEOUT_SECS

    def _fake_netstat(self, blob: str):
        """Return a fake subprocess.check_output that returns *blob*."""

        def _run(*_a, **_kw):
            return blob

        return _run

    def test_windows_finds_ipv6_listener_via_netstat(self, monkeypatch):
        # Regression:. Windows netstat -ano prints IPv6 LISTEN rows
        # with proto column "TCP" (NOT "TCP6") and address form [::1]:<port>.
        # Before this fix `-p tcp` on the netstat argv dropped these entirely,
        # so `kirocrew stop` / `kirocrew restart` silently no-op'd when the
        # gateway bound v6. This canned blob mirrors what real Windows netstat
        # actually prints (verified on Windows 11 24H2 with an AF_INET6
        # loopback listener) — regression-guards without a Windows CI lane.
        blob = (
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    [::1]:7777             [::]:0                 LISTENING       12345\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [12345]

    def test_windows_dedupes_dualstack_v4_and_v6_rows(self, monkeypatch):
        # A dual-stack listener shows up as TWO netstat rows sharing a PID
        # (very common for aiohttp / http.server with an empty host). Existing
        # dict.fromkeys() dedup must collapse them and preserve first-seen
        # order.
        blob = (
            "  TCP    0.0.0.0:7777           0.0.0.0:0              LISTENING       99\n"
            "  TCP    [::]:7777              [::]:0                 LISTENING       99\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [99]

    def test_windows_accepts_tcp6_label_defensively(self, monkeypatch):
        # Today Windows netstat prints plain "TCP" for both families, but we
        # relaxed the proto check from `== "TCP"` to `startswith("TCP")` to
        # future-proof against a hypothetical Windows build that switches to
        # "TCP6" (the netstat -p flag already accepts "tcpv6"). Guard the
        # defensive path so a future relabel doesn't silently re-break this.
        blob = "  TCP6   [::1]:7777             [::]:0                 LISTENING       77\n"
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [77]

    def test_windows_ignores_non_listening_rows(self, monkeypatch):
        # ESTABLISHED / TIME_WAIT etc. must never match: their foreign
        # endpoint is a real peer (not the 0.0.0.0:0 / [::]:0 wildcard) and
        # their state is not LISTENING, so both signals reject them.
        blob = (
            "  TCP    127.0.0.1:7777         127.0.0.1:9999         ESTABLISHED     55\n"
            "  TCP    127.0.0.1:7777         0.0.0.0:0              LISTENING       88\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [88]

    def test_windows_finds_listener_on_localized_netstat(self, monkeypatch):
        # netstat localizes state names (German "ABHÖREN", French, Cyrillic…),
        # so matching the English "LISTENING" literal alone returns [] on any
        # non-English Windows and stop/restart silently no-op with the gateway
        # still holding the port. Listener detection therefore keys off the
        # wildcard FOREIGN endpoint (0.0.0.0:0 / [::]:0), which is
        # locale-independent; the English literal remains as a second signal.
        blob = (
            "  Proto  Lokale Adresse         Remoteadresse          Status          PID\n"
            "  TCP    127.0.0.1:7777         0.0.0.0:0              ABHÖREN         44\n"
            "  TCP    [::1]:7777             [::]:0                 ABHÖREN         44\n"
            "  TCP    127.0.0.1:7777         127.0.0.1:9999         HERGESTELLT     66\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [44]

    def test_windows_listeners_carry_their_local_address(self, monkeypatch):
        # The netstat parse attributes each row's local address to its PID so
        # callers can scope ownership to the address they probed; a dual-stack
        # listener keeps one entry per bound address, v6 brackets stripped.
        blob = (
            "  TCP    0.0.0.0:7777           0.0.0.0:0              LISTENING       99\n"
            "  TCP    [::]:7777              [::]:0                 LISTENING       99\n"
            "  TCP    192.168.1.5:7777       0.0.0.0:0              LISTENING       55\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_port_listeners(7777) == [
            pc.PortListener(99, "0.0.0.0", "4"),
            pc.PortListener(99, "::", "6"),
            pc.PortListener(55, "192.168.1.5", "4"),
        ]

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows netstat branch")
    def test_windows_finds_real_ipv6_loopback_listener(self):
        # End-to-end guard on a live host: bind AF_INET6 to ::1 at an ephemeral
        # port and confirm find_listening_pids returns THIS process's pid.
        # Loopback-only (::1) so no firewall prompt fires. Complements the
        # canned-blob tests above by exercising the real netstat parse against
        # whatever this Windows build actually prints.
        import socket as _socket

        s = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
        try:
            s.bind(("::1", 0))
            s.listen()
            port = s.getsockname()[1]
            pids = pc.find_listening_pids(port)
            assert os.getpid() in pids, f"expected pid {os.getpid()} in {pids}"
        finally:
            s.close()


class TestAddressCoversLoopback:
    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "0.0.0.0", "*", "::", "[::]", "::ffff:127.0.0.1", " 0.0.0.0 "],
    )
    def test_loopback_covering_addresses(self, address):
        assert pc.address_covers_loopback(address) is True

    @pytest.mark.parametrize(
        "address",
        # ::1 cannot receive a connect addressed to 127.0.0.1, so a
        # v6-loopback-only listener is deliberately NOT loopback-covering.
        ["::1", "[::1]", "192.168.1.5", "10.0.0.1", "fe80::1", "127.0.0.2", ""],
    )
    def test_other_addresses_do_not_cover_loopback(self, address):
        assert pc.address_covers_loopback(address) is False


class TestLoopbackOwnerPids:
    """The most-specific-bind dispatch tiers of :func:`loopback_owner_pids`."""

    def test_an_exact_loopback_bind_beats_wildcards(self):
        # The kernel routes a 127.0.0.1 connect to the exact bind, so wildcard
        # listeners on the same port never saw the probe and are not owners.
        listeners = [
            pc.PortListener(111, "127.0.0.1", "4"),
            pc.PortListener(999, "*", "4"),
            pc.PortListener(888, "::", "6"),
        ]
        assert pc.loopback_owner_pids(listeners) == [111]

    def test_a_v4_wildcard_beats_a_possibly_v6only_wildcard(self):
        # An unrelated IPV6_V6ONLY wildcard next to the real v4 owner must not
        # be claimed: a v4 connect reaches the v4 wildcard socket, never the
        # v6-only one. lsof spells both ``*`` — the family is the separator.
        listeners = [
            pc.PortListener(111, "*", "4"),
            pc.PortListener(999, "*", "6"),
        ]
        assert pc.loopback_owner_pids(listeners) == [111]

    def test_a_lone_v6_wildcard_is_the_responder(self):
        # Callers only ask after a successful 127.0.0.1 probe; with nothing
        # more specific on the port, the v6 wildcard must be dual-stack and is
        # the adopted owner (refusing it would break [::]-bound externally
        # managed backends).
        listeners = [pc.PortListener(77, "::", "6")]
        assert pc.loopback_owner_pids(listeners) == [77]

    def test_multi_worker_backends_share_ownership(self):
        # Pre-fork / multi-worker backends legitimately share one listening
        # socket: every PID in the winning tier is recorded.
        listeners = [
            pc.PortListener(11, "127.0.0.1", "4"),
            pc.PortListener(12, "127.0.0.1", "4"),
            pc.PortListener(999, "*", "4"),
        ]
        assert pc.loopback_owner_pids(listeners) == [11, 12]

    def test_unknown_family_wildcards_fall_to_the_covering_tier(self):
        # A source that reported no family (old lsof output) still resolves:
        # the covering tier keeps adoption working rather than refusing it.
        listeners = [
            pc.PortListener(11, "*"),
            pc.PortListener(22, "192.168.1.5"),
        ]
        assert pc.loopback_owner_pids(listeners) == [11]

    def test_no_covering_listener_yields_no_owner(self):
        listeners = [pc.PortListener(999, "192.168.1.5", "4")]
        assert pc.loopback_owner_pids(listeners) == []


class TestKillAsyncVariants:
    """Regression guards for the async ``kill_pid_async`` / ``kill_process_tree_async``
    variants.

    The async wrappers exist so async call sites can offload the blocking
    Windows ``taskkill`` spawn to :func:`kiro_crew.executors.subprocess_executor`
    without stalling the event loop. The POSIX branch dispatches inline to the
    sync ``kill_pid`` / ``kill_process_tree`` (``os.kill`` / ``os.killpg`` are
    non-blocking, and preserving the same callable keeps existing tests that
    patch the sync entrypoints working). Windows offload is exercised via
    monkeypatching IS_WINDOWS + subprocess.run so the branch is covered on
    the Linux CI fleet.
    """

    def test_posix_kill_pid_async_dispatches_inline_to_kill_pid(self, monkeypatch):
        """POSIX branch: kill_pid_async calls kill_pid synchronously so tests
        that patch platform_compat.kill_pid observe the call unchanged."""
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        seen: list[tuple[int, int]] = []

        def fake_kill_pid(pid: int, sig: int) -> bool:
            seen.append((pid, sig))
            return True

        monkeypatch.setattr(pc, "kill_pid", fake_kill_pid)
        import asyncio as _asyncio

        result = _asyncio.new_event_loop().run_until_complete(pc.kill_pid_async(4242, pc.SIGKILL))
        assert result is True
        assert seen == [(4242, pc.SIGKILL)]

    def test_posix_kill_process_tree_async_dispatches_inline(self, monkeypatch):
        """POSIX branch: kill_process_tree_async calls kill_process_tree inline
        (same-callable dispatch keeps existing patch-based tests working)."""
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        seen: list[tuple[int, int]] = []

        def fake_kill_tree(pid: int, sig: int) -> bool:
            seen.append((pid, sig))
            return True

        monkeypatch.setattr(pc, "kill_process_tree", fake_kill_tree)
        import asyncio as _asyncio

        result = _asyncio.new_event_loop().run_until_complete(
            pc.kill_process_tree_async(9999, pc.SIGTERM)
        )
        assert result is True
        assert seen == [(9999, pc.SIGTERM)]

    def test_posix_kill_pid_async_propagates_process_lookup_error(self, monkeypatch):
        """POSIX branch propagates ProcessLookupError from kill_pid — callers'
        ``except (ProcessLookupError, OSError)`` guards must still fire."""
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)

        def raiser(*_a, **_kw):
            raise ProcessLookupError("gone")

        monkeypatch.setattr(pc, "kill_pid", raiser)
        import asyncio as _asyncio

        loop = _asyncio.new_event_loop()
        with pytest.raises(ProcessLookupError):
            loop.run_until_complete(pc.kill_pid_async(1, pc.SIGKILL))

    def test_windows_kill_pid_async_offloads_via_subprocess_executor(self, monkeypatch):
        """Windows branch: kill_pid_async submits the taskkill spawn to
        subprocess_executor() (so the event loop never blocks on taskkill.exe).

        Monkeypatched on Linux by flipping IS_WINDOWS and stubbing the executor
        to a synchronous callable-runner; asserts the run_in_executor path was
        taken by observing the executor sentinel captured at call time.
        """
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

        # Fake subprocess_executor sentinel — anything hashable-and-truthy.
        sentinel = object()
        seen_executors: list[object] = []

        # Stub subprocess.run so kill_pid returns success without spawning.
        def fake_run(*_a, **_kw):
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(pc.subprocess, "run", fake_run)

        # Patch the `subprocess_executor` name bound in the platform_compat
        # module namespace (top-level `from kiro_crew.executors import ...`)
        # to return our sentinel.
        monkeypatch.setattr(pc, "subprocess_executor", lambda: sentinel)

        # Intercept the loop's run_in_executor to record which executor is used.
        import asyncio as _asyncio

        real_loop = _asyncio.new_event_loop()

        async def _driver() -> bool:
            loop = _asyncio.get_running_loop()
            orig_rie = loop.run_in_executor

            def spy(executor, func, *args):
                seen_executors.append(executor)
                # Run the callable inline in a completed future so we don't
                # actually need the sentinel to be a real Executor.
                fut: _asyncio.Future[bool] = loop.create_future()
                try:
                    fut.set_result(func(*args))
                except BaseException as exc:  # pragma: no cover — defensive
                    fut.set_exception(exc)
                return fut

            loop.run_in_executor = spy  # type: ignore[method-assign]
            try:
                return await pc.kill_pid_async(1234, pc.SIGKILL)
            finally:
                loop.run_in_executor = orig_rie  # type: ignore[method-assign]

        result = real_loop.run_until_complete(_driver())
        assert result is True
        assert seen_executors == [
            sentinel
        ], f"expected the subprocess_executor sentinel, got {seen_executors!r}"

    def test_windows_kill_process_tree_async_offloads_via_subprocess_executor(self, monkeypatch):
        """Same offload contract as kill_pid_async but for the /T variant."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

        sentinel = object()
        seen_executors: list[object] = []

        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_kw: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        )
        monkeypatch.setattr(pc, "subprocess_executor", lambda: sentinel)

        import asyncio as _asyncio

        real_loop = _asyncio.new_event_loop()

        async def _driver() -> bool:
            loop = _asyncio.get_running_loop()

            def spy(executor, func, *args):
                seen_executors.append(executor)
                fut: _asyncio.Future[bool] = loop.create_future()
                fut.set_result(func(*args))
                return fut

            loop.run_in_executor = spy  # type: ignore[method-assign]
            return await pc.kill_process_tree_async(5678, pc.SIGTERM)

        assert real_loop.run_until_complete(_driver()) is True
        assert seen_executors == [sentinel]

    def test_windows_kill_pid_async_propagates_taskkill_rc128(self, monkeypatch):
        """Windows offload preserves the taskkill rc→exception mapping:
        rc=128 must still surface as ProcessLookupError so the callers'
        ``except (ProcessLookupError, OSError)`` guards fire.
        """
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_kw: types.SimpleNamespace(
                returncode=128, stdout=b"", stderr=b"not found"
            ),
        )
        monkeypatch.setattr(pc, "subprocess_executor", lambda: object())

        import asyncio as _asyncio

        async def _driver() -> None:
            loop = _asyncio.get_running_loop()

            def spy(_executor, func, *args):
                fut: _asyncio.Future = loop.create_future()
                try:
                    fut.set_result(func(*args))
                except BaseException as exc:
                    fut.set_exception(exc)
                return fut

            loop.run_in_executor = spy  # type: ignore[method-assign]
            await pc.kill_pid_async(99999, pc.SIGKILL)

        loop = _asyncio.new_event_loop()
        with pytest.raises(ProcessLookupError):
            loop.run_until_complete(_driver())


class TestProcessTokenSid:
    """The non-spawn SID lookup.

    ``whoami`` is the fallback, not the primary, because the primary sits on
    the gateway's bind path: a Windows CI run showed the spawn returning
    nothing under parallel test load, which made every named pipe refuse to be
    created (the DACL cannot be built without a SID).
    """

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows access tokens")
    def test_reads_a_real_sid_from_our_own_token(self) -> None:
        # The unguarded body on purpose: a ctypes prototype mistake surfaces as
        # a traceback naming the failing call instead of collapsing to None.
        sid = pc._process_token_sid_unguarded()
        assert sid is not None
        assert sid.startswith("S-1-")

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows SID lookup")
    def test_some_path_always_resolves_our_sid(self) -> None:
        """The property the gateway depends on: without a SID it cannot build
        the pipe DACL and refuses to bind at all."""
        assert pc.current_user_sid()

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows access tokens")
    def test_agrees_with_the_public_accessor(self) -> None:
        assert pc.current_user_sid() == pc._process_token_sid()

    @pytest.mark.skipif(pc.IS_WINDOWS, reason="the off-Windows guard")
    def test_returns_none_off_windows(self) -> None:
        assert pc._process_token_sid() is None


class TestCtypesStructsAreModuleScoped:
    """``ctypes.POINTER(T)`` memoises T -> POINTER(T) forever.

    ctypes keeps that memo in a module-level dict with no eviction, so a
    Structure subclass declared inside a function body pins a fresh pair of type
    objects on EVERY call. The leak is ctypes', not Win32's, so this covers the
    Mach layouts too. The Windows metrics/enumeration helpers are polled
    (the dashboard's system-metrics endpoint, the RSS-recycle watchdog, the
    tree-kill parent-map walk, the MCP pipe's per-connection peer check), which
    turned that into unbounded growth in a long-running gateway -- measured at
    ~8 KiB per ``proc_rss_bytes`` call, never reclaimed.

    Asserting on the source keeps this enforceable from the POSIX fleet, where
    the Windows branches never execute.
    """

    #: Helpers whose ctypes struct layouts must come from module scope.
    _CTYPES_STRUCT_USERS = (
        "get_ppid",
        "_windows_process_parent_map",
        "_win_process_image_name",
        "_process_token_sid_unguarded",
        "proc_rss_bytes",
        "proc_peak_rss_bytes",
        "_windows_memory_counters",
        "_macos_current_rss_bytes",
        "proc_rss_bytes_for_pid",
        "system_memory",
        "apply_job_limits",
        "resume_process_main_thread",
        # Mach, not Win32: same memo, same unbounded growth. This one is polled by
        # the sub-agent auto-sizer and by the xdist worker budget.
        "macos_vm_statistics",
    )

    def test_the_shared_layouts_are_defined_once_at_module_scope(self) -> None:
        import ctypes

        for name in (
            "_ProcessEntry32",
            "_ProcessMemoryCounters",
            "_MemoryStatusEx",
            "_SidAndAttributes",
            "_TokenUser",
            "_IoCounters",
            "_JobObjectBasicLimitInformation",
            "_JobObjectExtendedLimitInformation",
            "_ThreadEntry32",
            "_VMStatistics64",
            "_MachTimeValue",
            "_MachTaskBasicInfo",
        ):
            assert issubclass(getattr(pc, name), ctypes.Structure), name

    @pytest.mark.parametrize("func_name", _CTYPES_STRUCT_USERS)
    def test_no_helper_declares_a_structure_in_its_body(self, func_name: str) -> None:
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(pc, func_name))))
        local_structs = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(base, ast.Attribute) and base.attr in ("Structure", "Union")
                for base in node.bases
            )
        ]
        assert not local_structs, (
            f"{func_name} declares {local_structs} in its body; each call would pin a new "
            "type in ctypes' pointer-type memo. Hoist the layout to module scope."
        )

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Win32 metrics paths")
    def test_repeated_metrics_calls_add_no_pointer_memo_entries(self) -> None:
        """The behavioural half: polling must not grow ctypes' memo at all."""
        import ctypes

        memo = ctypes._pointer_type_cache  # type: ignore[attr-defined]
        pid = os.getpid()
        probes = (
            pc.proc_rss_bytes,
            pc.proc_peak_rss_bytes,
            lambda: pc.proc_rss_bytes_for_pid(pid),
            pc.system_memory,
            lambda: pc.get_ppid(pid),
            lambda: pc.process_owner_sid(pid),
        )
        for probe in probes:
            probe()  # a first call may legitimately populate the memo once
        before = len(memo)
        for _ in range(25):
            for probe in probes:
                probe()
        assert len(memo) == before


class TestLocalUserId:
    """The pool-partitioning identity. Must stay an int on every platform."""

    def test_matches_getuid_on_posix(self) -> None:
        if pc.IS_WINDOWS:
            pytest.skip("POSIX uid")
        assert pc.local_user_id() == os.getuid()

    def test_is_an_int_not_a_bool(self) -> None:
        """PoolKey type-checks this dimension and refuses to coerce, because
        bool is a subclass of int and would slip into the wrong partition."""
        value = pc.local_user_id()
        assert isinstance(value, int) and not isinstance(value, bool)

    def test_windows_derives_a_stable_int_from_the_sid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-9-8-7-1001")
        first = pc.local_user_id()
        assert isinstance(first, int) and not isinstance(first, bool)
        assert pc.local_user_id() == first  # stable across calls
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-9-8-7-1002")
        assert pc.local_user_id() != first  # and distinct per user

    def test_windows_without_a_sid_collapses_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A partition collapse, not a privilege change: the endpoint is already
        per-user, so two users cannot reach the same pool regardless."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: None)
        assert pc.local_user_id() == 0


class TestMakeOwnerOnlyDir:
    def test_creates_nested_directory_owner_only_on_posix(self, tmp_path) -> None:
        if pc.IS_WINDOWS:
            pytest.skip("POSIX mode bits")
        target = tmp_path / "a" / "b" / "c"
        pc.make_owner_only_dir(target)
        assert target.is_dir()
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_tightens_a_preexisting_loose_directory_on_posix(self, tmp_path) -> None:
        """The case a bare mkdir(mode=...) cannot cover: the mode argument is
        ignored entirely when the directory already exists."""
        if pc.IS_WINDOWS:
            pytest.skip("POSIX mode bits")
        loose = tmp_path / "loose"
        loose.mkdir(mode=0o755)
        pc.make_owner_only_dir(loose)
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700

    def test_uses_the_dacl_helper_on_windows(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows derives access from the DACL, so the mode argument is inert
        and the DACL helper is the only thing that protects the directory.

        It must be the DIRECTORY helper. ``restrict_to_owner`` is file-shaped:
        its grants carry no ``(OI)(CI)``, so routing a directory through it
        tightened the directory itself and left every file created inside on
        the creating token's default DACL -- which is why the negative
        assertion below is the load-bearing half of this test.
        """
        calls: list[str] = []
        wrong: list[str] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "restrict_dir_to_owner", lambda p, **_kw: calls.append(str(p)))
        monkeypatch.setattr(pc, "restrict_to_owner", lambda p, **_kw: wrong.append(str(p)))
        target = tmp_path / "win"
        pc.make_owner_only_dir(target)
        assert target.is_dir()
        assert calls == [str(target)]
        assert wrong == [], "a directory must not go through the file-shaped helper"

    def test_directory_still_exists_when_tightening_fails(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort on the tightening step: the caller decides whether an
        un-tightened directory is fatal, so creation must not be rolled back.

        Patches the same helper ``make_owner_only_dir`` actually calls -- when
        this named the file helper instead, the raise never fired and the test
        passed without exercising the handler at all.
        """
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc,
            "restrict_dir_to_owner",
            lambda p, **_kw: (_ for _ in ()).throw(OSError("nope")),
        )
        target = tmp_path / "partial"
        pc.make_owner_only_dir(target)
        assert target.is_dir()


class TestCurrentUserSidNeverSpawns:
    """``current_user_sid`` is called from three event-loop paths: the gatewayd
    admission check, the client-side server check, and the pipe DACL builder --
    which runs once per pipe instance and so sits on the accept path.

    It used to delegate to a helper whose fallback was a ``whoami`` subprocess
    with a 5 s timeout, so a token-lookup failure stalled accepts for seconds at
    a time, repeatedly. That helper is gone: the owner-only lockdown was its last
    caller and now reads the token directly too, so no path here can spawn.
    """

    @staticmethod
    def _forbid_spawn(*_a, **_kw):
        raise AssertionError("current_user_sid must not spawn -- it runs on the event loop")

    def test_returns_none_without_spawning_when_the_token_read_fails(self, monkeypatch):
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
        monkeypatch.setattr(pc.subprocess, "run", self._forbid_spawn)

        # Fails closed: every caller treats None as "principal unverifiable".
        assert pc.current_user_sid() is None

    def test_returns_the_bare_token_sid_and_memoises_it(self, monkeypatch):
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "IS_POSIX", False)
        calls: list[int] = []

        def _token():
            calls.append(1)
            return "S-1-5-21-1-2-3-1001"

        monkeypatch.setattr(pc, "_process_token_sid", _token)
        monkeypatch.setattr(pc.subprocess, "run", self._forbid_spawn)

        assert pc.current_user_sid() == "S-1-5-21-1-2-3-1001"
        assert pc.current_user_sid() == "S-1-5-21-1-2-3-1001"
        assert len(calls) == 1, "the SID is constant for the process lifetime"

    def test_strips_the_icacls_star_prefix(self, monkeypatch):
        """The icacls form carries a leading ``*``; SDDL and the Win32 security
        APIs want the bare SID."""
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: "*S-1-5-21-9-9-9-500")
        monkeypatch.setattr(pc.subprocess, "run", self._forbid_spawn)

        assert pc.current_user_sid() == "S-1-5-21-9-9-9-500"


def test_process_descendants_snapshots_a_new_session_grandchild():
    """A grandchild in its OWN session is still a descendant.

    This is the case a bare ``killpg`` misses, so the walk that broadens a kill
    must be able to see it.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX session semantics
        pytest.skip("POSIX session semantics")

    grandchild: int | None = None
    child_code = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "start_new_session=True);"
        "print(c.pid,flush=True);time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        grandchild = int(proc.stdout.readline().strip())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if grandchild in platform_compat.process_descendants(proc.pid):
                break
            time.sleep(0.05)
        descendants = platform_compat.process_descendants(proc.pid)
        assert grandchild in descendants
        # It is genuinely outside the parent's process group -- otherwise this
        # test would pass even without the escape it exists to describe.
        assert os.getpgid(grandchild) != os.getpgid(proc.pid)
    finally:
        for pid in (grandchild, proc.pid):
            if pid is None:
                continue
            try:
                platform_compat.kill_process_tree(pid)
            except (ProcessLookupError, OSError, ValueError):
                pass
        proc.wait(timeout=5)


def test_process_descendants_is_best_effort_on_unreadable_table(monkeypatch):
    """Introspection failure must not raise into a caller's kill path."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(
        platform_compat,
        "_posix_process_parent_map",
        lambda: (_ for _ in ()).throw(OSError("boom")),
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_process_parent_map",
        lambda: (_ for _ in ()).throw(OSError("boom")),
    )
    assert platform_compat.process_descendants(os.getpid()) == []


def test_process_descendants_refuses_reserved_pids():
    from kiro_crew import platform_compat

    assert platform_compat.process_descendants(1) == []
    assert platform_compat.process_descendants(0) == []


def test_parent_map_ignores_a_planted_ps_earlier_on_path(tmp_path, monkeypatch):
    """A gateway PATH can lead with agent-writable dirs, so PATH is not trusted.

    The shim below would report a bogus tree (and could run any code) if the
    lookup honored PATH.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    # The sentinel must be a number NO real process table can contain, because the
    # assertion below reads its absence as proof the shim did not run. A plausible
    # PID cannot do that job: `pid_max` is 4194304 on Linux, so a host whose counter
    # has passed 999999 has a live process with that id and the test failed with
    # "planted PATH shim was executed" while the shim had not run at all.
    unreachable_pid = 99999999999
    shim = tmp_path / "ps"
    shim.write_text(f"#!/bin/sh\necho '{unreachable_pid} {unreachable_pid - 1}'\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    parent_map = platform_compat._posix_process_parent_map()
    assert unreachable_pid not in parent_map, "planted PATH shim was executed"
    # A real snapshot still came back, so this is not passing by returning {}.
    assert os.getpid() in parent_map


def test_trusted_system_bin_rejects_a_name_not_in_system_dirs(tmp_path, monkeypatch):
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    fake = tmp_path / "definitely-not-a-system-tool"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert platform_compat.trusted_system_bin("definitely-not-a-system-tool") is None
    assert platform_compat.trusted_system_bin("ps") is not None


def test_trusted_system_bin_dirs_are_not_limited_to_fhs():
    # A distribution may keep ps/lsof/systemd-run outside /usr/{s}bin; an
    # FHS-only pin resolves nothing at all there.
    from kiro_crew import platform_compat

    fhs = {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}
    assert set(platform_compat._TRUSTED_SYSTEM_BIN_DIRS) - fhs


def test_trusted_system_bin_resolves_outside_fhs(tmp_path, monkeypatch):
    # A tool reachable only through a non-FHS pinned directory still resolves.
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    system_dir = tmp_path / "sw" / "bin"
    system_dir.mkdir(parents=True)
    tool = system_dir / "definitely-not-a-system-tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)

    monkeypatch.setattr(platform_compat, "_TRUSTED_SYSTEM_BIN_DIRS", (str(system_dir),))
    assert platform_compat.trusted_system_bin("definitely-not-a-system-tool") == str(tool)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "asserts the POSIX degradation path: neutering trusted_system_bin only "
        "disarms _posix_process_parent_map, while process_descendants on Windows "
        "goes through the Win32 snapshot and still reports this process's real "
        "live children -- so the == [] assertion depends on whether the xdist "
        "worker happens to have a subprocess alive at that instant"
    ),
)
def test_parent_map_is_empty_when_no_trusted_ps_exists(monkeypatch):
    """No trusted binary must degrade to best-effort, never fall back to PATH."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    assert platform_compat._posix_process_parent_map() == {}
    assert platform_compat.process_descendants(os.getpid()) == []


def _listening_port(sock):
    """Bind and listen on an ephemeral loopback port, returning it."""

    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock.getsockname()[1]


def test_listening_pid_lookup_ignores_a_planted_tool_on_path(tmp_path, monkeypatch):
    """A PATH-planted lsof must never answer the port->PID lookup.

    This lookup feeds ``cli_server._gateway_owns_port``, so a shim that names an
    attacker-chosen PID as the port holder subverts an ownership gate rather
    than merely returning bad diagnostics.

    POSIX-only by necessity: a faithful Windows shim would have to be a real
    ``.exe``, because ``CreateProcess`` appends only that extension when it
    resolves a bare argv name and so never reaches a planted ``.bat``. The
    Windows guarantee is covered at the resolution level instead, by
    ``test_trusted_system_bin_resolves_system32_and_rejects_path_on_windows``.
    """

    import socket

    if pc.IS_WINDOWS:  # pragma: no cover - POSIX binary resolution
        pytest.skip("POSIX binary resolution")

    bogus = 999_999
    tool = pc.listening_pid_tool()
    shim = tmp_path / tool
    shim.write_text(f"#!/bin/sh\necho {bogus}\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        pids = pc.find_listening_pids(_listening_port(sock))

    assert bogus not in pids, "planted PATH shim was executed"
    if pc.trusted_system_bin(tool) is not None:
        # A trusted tool exists on this host, so an empty list would not be a
        # real answer — the lookup must still see this process holding the port.
        # Without this the test could pass simply by returning nothing.
        assert os.getpid() in pids


def test_listening_pid_lookup_still_resolves_the_pinned_tool(tmp_path, monkeypatch):
    """Pinning must not cost the lookup its real answer, PATH notwithstanding.

    Guards the other direction from the shim test: a pin that resolved nothing
    would make every port read as unheld, which is silent and fails open into
    "no gateway is running".
    """

    import socket

    tool = pc.listening_pid_tool()
    if pc.trusted_system_bin(tool) is None:  # pragma: no cover - host lacks the tool
        pytest.skip(f"no trusted {tool} on this host")

    # An empty PATH proves the resolution owes nothing to it.
    monkeypatch.setenv("PATH", str(tmp_path))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        pids = pc.find_listening_pids(_listening_port(sock))

    assert os.getpid() in pids


def test_listening_pid_tool_available_ignores_a_planted_tool_on_path(tmp_path, monkeypatch):
    """The availability probe must agree with the lookup it describes.

    Probing PATH here while the lookup resolves from the trusted directories
    would let the two disagree: a shim would answer "available" for a tool the
    lookup refuses to run, and a live gateway would read as stopped.
    """

    tool = pc.listening_pid_tool()
    planted = tmp_path / (f"{tool}.exe" if pc.IS_WINDOWS else tool)
    planted.write_text("")
    if not pc.IS_WINDOWS:
        planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    # Empty the trusted directories so the only resolvable copy of the tool is
    # the planted one. A host that genuinely ships the tool would otherwise
    # answer True for both the pinned and the PATH lookup, and the test could
    # not tell them apart.
    monkeypatch.setattr(pc, "_TRUSTED_SYSTEM_BIN_DIRS", ())
    monkeypatch.setattr(pc, "_windows_system_dirs", lambda: ())

    assert pc.trusted_system_bin(tool) is None
    assert pc.listening_pid_tool_available() is False


def test_listening_pid_lookup_degrades_when_no_trusted_tool_exists(monkeypatch):
    """No trusted tool must read as "absent", never as a silent empty answer."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    assert pc.find_listening_pids(8000) == []
    assert pc.listening_pid_tool_available() is False


def test_process_owner_uid_ignores_a_planted_ps_on_path(tmp_path, monkeypatch):
    """The uid backing the port-trust gate must not come from a PATH shim.

    ``process_owner_uid`` reads ``/proc`` on Linux and shells out to ``ps`` only
    on macOS, so the darwin branch is selected explicitly to exercise the spawn
    on any POSIX host rather than leaving it covered on macOS CI alone.
    """

    if pc.IS_WINDOWS:  # pragma: no cover - POSIX binary resolution
        pytest.skip("POSIX binary resolution")

    shim = tmp_path / "ps"
    shim.write_text("#!/bin/sh\necho 999999\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sys, "platform", "darwin")

    assert pc.process_owner_uid(os.getpid()) == os.getuid()


def test_process_owner_uid_denies_when_no_trusted_ps_exists(monkeypatch):
    """An unresolvable ``ps`` must report "unknown owner", which the gate denies on."""

    if pc.IS_WINDOWS:  # pragma: no cover - POSIX binary resolution
        pytest.skip("POSIX binary resolution")

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pc.process_owner_uid(os.getpid()) is None


def test_trusted_system_bin_resolves_system32_and_rejects_path_on_windows(tmp_path, monkeypatch):
    """Windows argv names must resolve from the real system directory only."""

    if not pc.IS_WINDOWS:  # pragma: no cover - Windows binary resolution
        pytest.skip("Windows binary resolution")

    planted = tmp_path / "definitely-not-a-system-tool.exe"
    planted.write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert pc.trusted_system_bin("definitely-not-a-system-tool") is None
    # A bare argv name still resolves, extension supplied by the lookup.
    resolved = pc.trusted_system_bin("taskkill")
    assert resolved is not None and resolved.lower().endswith("taskkill.exe")
    assert os.path.isfile(resolved)


def test_kill_helpers_fail_loud_when_taskkill_is_unresolvable(monkeypatch):
    """Windows kills must raise, not silently report success, with no taskkill.

    Callers branch on the exception to escalate; a quiet ``True`` would strand a
    live process while reporting it terminated.
    """

    if not pc.IS_WINDOWS:  # pragma: no cover - Windows kill path
        pytest.skip("Windows kill path")

    # A PID that does not exist, so a regression that reaches the real taskkill
    # cannot terminate the test runner; ``match`` pins the failure to the
    # resolution step rather than to taskkill rejecting an unknown PID.
    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    with pytest.raises(OSError, match="trusted system directories"):
        pc.kill_pid(999_999)
    with pytest.raises(OSError, match="trusted system directories"):
        pc.kill_process_tree(999_999)


def _plant_on_path(tmp_path, monkeypatch, name):
    """Make *name* the only thing PATH can resolve, and return its path."""

    planted = tmp_path / (f"{name}.exe" if pc.IS_WINDOWS else name)
    planted.write_text("")
    if not pc.IS_WINDOWS:
        planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    return planted


def _pin_warnings(caplog):
    """Only this module's records, so an unrelated warning cannot skew the count."""

    return [r for r in caplog.records if r.name == "kiro_crew.platform_compat"]


def test_a_tool_installed_outside_the_trusted_dirs_is_diagnosable(tmp_path, monkeypatch, caplog):
    """A non-FHS host must learn the pin is why its tool reads as unavailable.

    NixOS and Homebrew/conda prefixes keep a perfectly good ``lsof`` outside the
    system directories. The pin still refuses it, but without this line the
    operator sees only ``kirocrew stop`` no-opping and a prompt to install a
    tool they already have.
    """

    monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
    planted = _plant_on_path(tmp_path, monkeypatch, "definitely-not-a-system-tool")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform_compat"):
        assert pc.trusted_system_bin("definitely-not-a-system-tool") is None

    records = _pin_warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    # Case-insensitive: Windows resolution reports the PATHEXT entry's own
    # casing (".EXE"), not the casing the file was created with.
    assert str(planted).casefold() in message.casefold(), "must name where the tool actually is"
    assert "unavailable" in message


def test_the_unpinned_tool_diagnostic_does_not_repeat(tmp_path, monkeypatch, caplog):
    """One line per name: these lookups run on every teardown and gate check."""

    monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
    _plant_on_path(tmp_path, monkeypatch, "definitely-not-a-system-tool")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform_compat"):
        for _ in range(3):
            assert pc.trusted_system_bin("definitely-not-a-system-tool") is None

    assert len(_pin_warnings(caplog)) == 1


def test_a_genuinely_absent_tool_is_not_reported_as_misplaced(tmp_path, monkeypatch, caplog):
    """Nothing on PATH means nothing to explain, so the line must stay quiet.

    Claiming a tool sits outside the trusted directories when it is simply not
    installed would send the operator hunting for a path that does not exist.
    """

    monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
    monkeypatch.setenv("PATH", str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform_compat"):
        assert pc.trusted_system_bin("definitely-not-a-system-tool") is None

    assert _pin_warnings(caplog) == []


def test_a_resolvable_tool_is_never_reported_as_sitting_outside_the_pin(monkeypatch):
    """Nothing to explain when the pinned lookup succeeded."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: os.path.join("/usr/bin", name))
    assert pc.tool_outside_trusted_dirs("lsof") is None


def test_the_unpinned_path_is_reported_so_stop_can_name_it(tmp_path, monkeypatch):
    """``stop`` needs the real location to tell a NixOS operator what happened."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    planted = _plant_on_path(tmp_path, monkeypatch, "definitely-not-a-system-tool")

    found = pc.tool_outside_trusted_dirs("definitely-not-a-system-tool")

    assert found is not None
    assert found.casefold() == str(planted).casefold()


def test_an_absent_tool_reports_no_unpinned_path(tmp_path, monkeypatch):
    """Absent everywhere must stay ``None``, or ``stop`` would claim a path that
    does not exist instead of saying the tool is missing."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert pc.tool_outside_trusted_dirs("definitely-not-a-system-tool") is None


# ── Desktop bundled-interpreter detection ──

_REPO_ROOT = Path(__file__).parent.parent


class TestIsBundledInterpreter:
    """``is_bundled_interpreter`` is the single runtime owner of the desktop
    packaging-layout sentinel; these tests pin both its behavior and its
    agreement with the packaging layer, so a bundler directory rename breaks a
    test here instead of silently un-matching the runtime guard (which would
    let pip write into the signed macOS bundle)."""

    def test_bundled_interpreter_path_is_detected(self, tmp_path, monkeypatch):
        """The real desktop layout — a python-build-standalone runtime under
        ``Resources/backend-dist/`` — must be recognized. The literal directory
        name is deliberate here: the test pins the real-world layout, not the
        constant (asserting via the constant would be tautological)."""
        bundled = (
            tmp_path
            / "App.app"
            / "Contents"
            / "Resources"
            / "backend-dist"
            / "kirocrew-backend-arm64"
            / "bin"
            / "python3.12"
        )
        monkeypatch.setattr(pc.sys, "executable", str(bundled))
        assert pc.is_bundled_interpreter() is True

    def test_regular_interpreter_path_is_not_detected(self, tmp_path, monkeypatch):
        """An ordinary venv interpreter must not trip the guard — a false
        positive would refuse every Python app build on normal installs."""
        regular = tmp_path / "gateway-venv" / "bin" / "python3.12"
        monkeypatch.setattr(pc.sys, "executable", str(regular))
        assert pc.is_bundled_interpreter() is False

    def test_sentinel_matches_electron_builder_packaging_layout(self):
        """Pin the constant to electron-builder's ``extraResources`` target so
        a packaging rename fails HERE, not at runtime inside a signed bundle."""
        pkg_json = _REPO_ROOT / "website" / "electron" / "package.json"
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        targets = {
            res["to"]
            for res in pkg["build"]["extraResources"]
            if isinstance(res, dict) and "to" in res
        }
        assert pc.BUNDLED_BACKEND_DIST_DIRNAME in targets, (
            "platform_compat.BUNDLED_BACKEND_DIST_DIRNAME no longer matches the "
            "electron-builder extraResources target in website/electron/package.json. "
            "If the desktop packaging directory was renamed, update the constant "
            "(and this test) in the same change — otherwise the bundled-interpreter "
            "guard silently stops matching and pip can write into the signed bundle."
        )

    def test_sentinel_matches_desktop_build_script_staging_dir(self):
        """Same pin against the build script that stages the runtime trees.

        Asserts the directory NAME as a path component — not any exact
        shell-quoted expression — so a script refactor that introduces a
        variable for the staging path does not false-positive this pin."""
        script = (_REPO_ROOT / "packaging" / "build-desktop.sh").read_text(encoding="utf-8")
        needle = f"/{pc.BUNDLED_BACKEND_DIST_DIRNAME}"
        assert needle in script, (
            "packaging/build-desktop.sh no longer stages anything under a "
            f"'{pc.BUNDLED_BACKEND_DIST_DIRNAME}' directory — keep "
            "platform_compat.BUNDLED_BACKEND_DIST_DIRNAME in sync with the "
            "packaging layer (see is_bundled_interpreter)."
        )


class TestKillProcessTreePinned:
    """The verified identity must stay PINNED for the whole terminate.

    ``kill_process_tree`` addresses the target by PID, and on Windows it does so
    from a separate ``taskkill`` process. A caller that only read the start time
    first has released every handle by then, so the process can exit and Windows
    can recycle the PID onto an unrelated one in between -- which
    ``taskkill /T /F /PID`` would then tear down with its whole tree. Windows
    keeps a process ID reserved while ANY handle to the process object is open,
    so holding the query handle that verified the identity across the terminate
    is what makes the PID still mean the same process when taskkill resolves it.

    Driven through the module seams with ``IS_WINDOWS`` patched, so every case
    runs on every platform: the invariant is about handle LIFETIME, not about
    which OS the test host happens to be.
    """

    HANDLE = 4242

    def _wire(self, monkeypatch, *, handle=HANDLE, identity=(4321, 777, None)):
        """Patch the seams; return (opened, closed, killed) recorders."""
        opened: list[int] = []
        closed: list[int] = []
        killed: list[tuple[int, int]] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        def _open(pid):
            opened.append(pid)
            return handle

        def _identity(h):
            assert h == handle, "the identity must be read from the handle just opened"
            return identity

        def _close(h):
            closed.append(h)

        def _kill(pid, sig):
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(pc, "_open_process_query_handle", _open)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", _identity)
        monkeypatch.setattr(pc, "_close_process_handle", _close)
        monkeypatch.setattr(pc, "kill_process_tree", _kill)
        return opened, closed, killed

    def test_a_matching_identity_kills_and_then_releases_the_handle(self, monkeypatch):
        opened, closed, killed = self._wire(monkeypatch)

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGTERM) is True

        assert opened == [4321]
        assert killed == [(4321, pc.SIGTERM)]
        assert closed == [self.HANDLE]

    def test_a_mismatched_identity_never_invokes_the_kill(self, monkeypatch):
        """The pid was recycled: refuse, and do not spawn taskkill at all.

        Asserting on "no kill" rather than on the return value is the point --
        a terminate that ran and then failed would still have torn down whatever
        now owns the pid.
        """
        _, closed, killed = self._wire(monkeypatch, identity=(4321, 999, None))

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGKILL) is False

        assert killed == []
        assert closed == [self.HANDLE], "the handle must still be released"

    def test_an_unopenable_process_never_invokes_the_kill(self, monkeypatch):
        """No handle means no pin, and an unpinned pid must not be killed."""
        _, closed, killed = self._wire(monkeypatch, handle=None)

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [], "nothing was opened, so nothing may be closed"

    def test_an_unreadable_identity_never_invokes_the_kill(self, monkeypatch):
        """A handle that cannot answer WHO it is confirms nothing."""
        _, closed, killed = self._wire(monkeypatch, identity=None)

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [self.HANDLE]

    def test_the_handle_is_still_open_while_the_kill_is_in_flight(self, monkeypatch):
        """The invariant itself, observed rather than inferred.

        The fake terminate is gated on an event, so the assertion runs at a
        moment that is CAUSALLY inside the kill rather than at a moment chosen by
        a sleep. The two ``wait`` calls are bounded hang guards; nothing asserts
        on elapsed time.
        """
        closed: list[int] = []
        entered = threading.Event()
        release = threading.Event()
        seen_closed_during_kill: list[list[int]] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_query_handle", lambda pid: self.HANDLE)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda h: (4321, 777, None))
        monkeypatch.setattr(pc, "_close_process_handle", closed.append)

        def _gated_kill(pid, sig):
            seen_closed_during_kill.append(list(closed))
            entered.set()
            assert release.wait(10), "the gate was never released"
            return True

        monkeypatch.setattr(pc, "kill_process_tree", _gated_kill)

        result: list[bool] = []
        worker = threading.Thread(
            target=lambda: result.append(pc.kill_process_tree_pinned(4321, "777"))
        )
        worker.start()
        try:
            assert entered.wait(10), "the kill never started"
            assert closed == [], (
                "the handle was released while taskkill was still in flight -- "
                "the pid is unpinned for exactly the window this exists to close"
            )
        finally:
            release.set()
            worker.join(10)

        assert not worker.is_alive()
        assert seen_closed_during_kill == [[]]
        assert result == [True]
        assert closed == [self.HANDLE], "released once the kill returned"

    def test_the_handle_is_released_when_the_kill_raises(self, monkeypatch):
        """A failing terminate must not leak the handle.

        A leaked handle keeps the pid reserved for the life of the gateway, so
        the failure mode is a slow resource leak rather than a loud one.
        """
        closed: list[int] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_query_handle", lambda pid: self.HANDLE)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda h: (4321, 777, None))
        monkeypatch.setattr(pc, "_close_process_handle", closed.append)

        def _raising_kill(pid, sig):
            raise ProcessLookupError("gone between the pin and the signal")

        monkeypatch.setattr(pc, "kill_process_tree", _raising_kill)

        with pytest.raises(ProcessLookupError):
            pc.kill_process_tree_pinned(4321, "777")

        assert closed == [self.HANDLE]

    def test_posix_delegates_straight_through(self, monkeypatch):
        """POSIX is unchanged: no handle exists to hold, so none is sought.

        ``os.killpg`` is issued in-process by the same interpreter that did the
        check. Introducing a Windows-shaped pin here would change a path this
        finding is not about.
        """
        killed: list[tuple[int, int]] = []
        opened: list[int] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "_open_process_query_handle", opened.append)
        monkeypatch.setattr(
            pc, "kill_process_tree", lambda pid, sig: killed.append((pid, sig)) or True
        )

        assert pc.kill_process_tree_pinned(4321, "anything", pc.SIGTERM) is True

        assert killed == [(4321, pc.SIGTERM)]
        assert opened == [], "no handle work on POSIX"

    def test_the_pinned_identity_is_the_same_half_process_start_time_returns(self, monkeypatch):
        """Both sides must read the CREATION half, or the comparison is nonsense.

        ``process_start_time`` records ``str(identity[1])``; if the pin compared
        a different element the guard would refuse every legitimate reap while
        reporting itself as working.
        """
        # ``process_start_time`` checks ``sys.platform == "linux"`` BEFORE
        # ``IS_WINDOWS``, so on a Linux runner the /proc arm answers None for a
        # pid that does not exist and the patched Windows arm is never reached.
        # Steering the platform too is what keeps this case host-independent --
        # the same technique test_app_backend_stale_reap uses to model a
        # ps-less host.
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_query_handle", lambda pid: self.HANDLE)
        monkeypatch.setattr(pc, "_close_process_handle", lambda h: None)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda h: (4321, 777, 888))
        monkeypatch.setattr(pc, "kill_process_tree", lambda pid, sig: True)

        recorded = pc.process_start_time(4321)

        assert recorded == "777"
        assert pc.kill_process_tree_pinned(4321, recorded) is True
        # The exit half moves as the process dies and must never be the identity.
        assert pc.kill_process_tree_pinned(4321, "888") is False


class TestKillPidPinned:
    """Single-process variant of the pinned kill: same handle-lifetime
    invariant as :class:`TestKillProcessTreePinned`, delegating to ``kill_pid``
    instead of the tree teardown. Driven through the module seams with
    ``IS_WINDOWS`` patched so every case runs on every platform."""

    HANDLE = 4242

    def _wire(self, monkeypatch, *, handle=HANDLE, identity=(4321, 777, None)):
        opened: list[int] = []
        closed: list[int] = []
        killed: list[tuple[int, int]] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        def _open(pid):
            opened.append(pid)
            return handle

        def _identity(h):
            assert h == handle, "the identity must be read from the handle just opened"
            return identity

        def _close(h):
            closed.append(h)

        def _kill(pid, sig):
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(pc, "_open_process_query_handle", _open)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", _identity)
        monkeypatch.setattr(pc, "_close_process_handle", _close)
        monkeypatch.setattr(pc, "kill_pid", _kill)
        return opened, closed, killed

    def test_a_matching_identity_kills_and_then_releases_the_handle(self, monkeypatch):
        opened, closed, killed = self._wire(monkeypatch)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is True

        assert opened == [4321]
        assert killed == [(4321, pc.SIGTERM)]
        assert closed == [self.HANDLE]

    def test_a_mismatched_identity_never_invokes_the_kill(self, monkeypatch):
        _, closed, killed = self._wire(monkeypatch, identity=(4321, 999, None))

        assert pc.kill_pid_pinned(4321, "777", pc.SIGKILL) is False

        assert killed == []
        assert closed == [self.HANDLE], "the handle must still be released"

    def test_an_unopenable_process_never_invokes_the_kill(self, monkeypatch):
        _, closed, killed = self._wire(monkeypatch, handle=None)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [], "nothing was opened, so nothing may be closed"

    def test_an_unreadable_identity_never_invokes_the_kill(self, monkeypatch):
        _, closed, killed = self._wire(monkeypatch, identity=None)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [self.HANDLE]

    def test_posix_delegates_straight_through(self, monkeypatch):
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: killed.append((pid, sig)) or True)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is True
        assert killed == [(4321, pc.SIGTERM)]


class TestTrustedGitBin:
    """`git` resolution for privileged/unattended callers.

    Moved here from `test_cli_doctor` with the logic: the doctor and the update
    seam are two callers of one resolver, so the resolution rules belong beside
    the resolver rather than in either caller's tests.
    """

    def test_uses_the_trusted_system_resolver(self, monkeypatch) -> None:
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/usr/bin/git")
        assert pc.trusted_git_bin() == "/usr/bin/git"

    def test_windows_falls_back_to_the_git_for_windows_roots(self, monkeypatch, tmp_path) -> None:
        """Git for Windows installs under Program Files, never System32.

        Without the fallback every supported Windows source install resolves to
        None, which would silently disable the callers that depend on it.
        """
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        gfw = tmp_path / "Git" / "cmd"
        gfw.mkdir(parents=True)
        exe = gfw / "git.exe"
        exe.write_text("")
        exe.chmod(0o755)
        monkeypatch.setattr(pc, "_WINDOWS_GIT_DIRS", (str(gfw),))
        assert pc.trusted_git_bin() == str(exe)

    def test_windows_returns_none_when_the_roots_are_empty(self, monkeypatch) -> None:
        """Fixed roots only -- a miss returns None without consulting PATH.

        Reading `%ProgramFiles%` instead would let a poisoned variable redirect
        the lookup to an agent-writable directory, which is the hole the pin
        exists to close.
        """
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_WINDOWS_GIT_DIRS", (r"Z:\nonexistent\Git\cmd",))
        assert pc.trusted_git_bin() is None

    def test_posix_never_probes_the_windows_roots(self, monkeypatch) -> None:
        """On POSIX the trusted-dirs decision is final."""
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(
            pc,
            "_WINDOWS_GIT_DIRS",
            property(lambda _s: (_ for _ in ()).throw(AssertionError("probed on POSIX"))),
        )
        assert pc.trusted_git_bin() is None


class TestKillAndReap:
    """The shared kill-the-tree + bounded-pipe-draining-reap helper (#5989)."""

    @staticmethod
    def _proc(pid: int = 4242):
        from unittest import mock

        proc = mock.MagicMock()
        proc.pid = pid
        proc.kill = mock.MagicMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.wait = mock.AsyncMock()
        return proc

    @pytest.mark.asyncio
    async def test_kills_the_whole_tree_then_the_pid(self) -> None:
        """A spawned command is often a shell line, so the whole group must be
        signalled; the pid-scoped kill backs up a group signal that missed."""
        from unittest import mock

        proc = self._proc(pid=4242)
        with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()) as tree:
            await pc.kill_and_reap(proc)
        tree.assert_awaited_once()
        assert tree.await_args.args == (4242, pc.SIGKILL)
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_the_group_kill_for_a_same_group_child(self) -> None:
        """A child sharing OUR group leads no tree, so the group signal is
        skipped and the pid-scoped kill covers it -- otherwise every routine
        timeout would trip ``kill_process_tree``'s broadcast refusal.

        Also the escape hatch for the rootdir conftest's autouse pin of this
        probe: a test that wants the skip patches the seam itself and wins.
        """
        from unittest import mock

        proc = self._proc()
        with (
            mock.patch.object(pc, "_shares_own_process_group", lambda _pid: True),
            mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()) as tree,
        ):
            await pc.kill_and_reap(proc)
        tree.assert_not_awaited()
        proc.kill.assert_called_once()
        proc.communicate.assert_awaited_once()

    @pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX only")
    def test_group_probe_reports_our_own_group(self) -> None:
        """Our own pid is in our own group by construction."""
        assert _real_shares_own_process_group(os.getpid()) is True

    @pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX only")
    def test_group_probe_fails_closed_for_an_unreadable_pid(self, monkeypatch) -> None:
        """Fail-closed, so a vanished or unreadable pid still gets its tree
        signalled rather than silently skipping the kill."""
        monkeypatch.setattr(
            pc.os,
            "getpgid",
            lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
        )
        assert _real_shares_own_process_group(4242) is False

    def test_group_probe_is_posix_only(self, monkeypatch) -> None:
        """Windows has no process groups to compare, so nothing is ever skipped
        there -- and the probe must not reach a missing ``os.getpgid``.

        This case runs on EVERY platform on purpose -- the non-POSIX branch is
        what it covers -- so the tripwire is installed with ``raising=False``:
        ``os.getpgid`` is Unix-only, and a strict ``setattr`` raises
        ``AttributeError`` during the test's own arrangement on Windows, which
        is where the assertion matters most. With ``raising=False`` the sentinel
        is created where the attribute is absent, never called (that is the
        assertion), and removed at teardown.
        """
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(
            pc.os,
            "getpgid",
            lambda _pid: (_ for _ in ()).throw(AssertionError("probed on Windows")),
            raising=False,
        )
        assert _real_shares_own_process_group(os.getpid()) is False

    @pytest.mark.asyncio
    async def test_reaps_via_communicate_never_wait(self) -> None:
        """The reap must drain the pipes: a killed child blocked writing into
        a full pipe makes a bare ``wait()`` hang the calling task forever."""
        from unittest import mock

        proc = self._proc()
        with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()):
            await pc.kill_and_reap(proc)
        proc.communicate.assert_awaited_once()
        proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bounds_the_reap(self) -> None:
        """A descendant that ignores SIGKILL's effects on the pipe (e.g. an
        inherited fd held open) must not turn cleanup into a hang."""
        import asyncio
        from unittest import mock

        assert 0 < pc.REAP_TIMEOUT_SECS <= 30
        proc = self._proc(pid=1)

        async def _never_returns():
            await asyncio.sleep(3600)

        proc.communicate = _never_returns
        with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()):
            # Outer bound is a hang detector only; the helper's own bound
            # (passed explicitly) is what must return first.
            await asyncio.wait_for(pc.kill_and_reap(proc, timeout=0.01), timeout=30)

    @pytest.mark.asyncio
    async def test_tolerates_dead_child_and_mock_pids(self) -> None:
        """Best-effort throughout: an already-exited child (or a non-int test
        pid refused by the broadcast guard) must not mask the caller's own
        timeout or cancellation handling."""
        from unittest import mock

        proc = mock.MagicMock()  # pid is a MagicMock -> tree kill refuses it
        proc.kill = mock.MagicMock(side_effect=ProcessLookupError())
        proc.communicate = mock.AsyncMock(side_effect=RuntimeError("already reaped"))
        await pc.kill_and_reap(proc)

    @pytest.mark.asyncio
    async def test_repeat_cancellation_does_not_abandon_cleanup(self) -> None:
        """A second Task.cancel() landing mid-cleanup is a BaseException that
        escapes ``suppress(Exception)``: without the shield it aborts the
        cleanup before the reap, leaving the killed child un-drained. The
        helper must finish the kill + reap, then re-deliver the cancellation
        exactly once."""
        import asyncio
        from unittest import mock

        started = asyncio.Event()
        release = asyncio.Event()
        events: list[str] = []

        class Proc:
            pid = 4242

            def kill(self):
                events.append("killed")

            async def communicate(self):
                events.append("reap-started")
                started.set()
                await release.wait()
                events.append("reaped")
                return b"", b""

        async def _caller():
            with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()):
                await pc.kill_and_reap(Proc())

        task = asyncio.ensure_future(_caller())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # the repeat cancellation that used to abort the reap
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["killed", "reap-started", "reaped"]
