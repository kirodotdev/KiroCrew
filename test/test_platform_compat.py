"""Unit tests for kiro_crew.platform_compat — the cross-platform shim that lets
KiroCrew run natively on Windows alongside macOS/Linux (Mesh-629).

These exercise the PURE / platform-dispatching surface without spawning real
processes: the signal constants, the file-lock context managers (POSIX path on
this host; the Windows branch is asserted via its dispatch shape), the
strftime directive translation (the one piece with a deterministic Windows
output we can assert directly), and the process-helper return contracts.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import time
import types

import pytest

from kiro_crew import platform_compat as pc


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


class TestProcessHelpers:
    def test_pid_exists_true_for_self(self):
        # The current process obviously exists — on POSIX via os.kill(0), on
        # Windows via OpenProcess.
        assert pc.pid_exists(os.getpid()) is True

    def test_pid_exists_false_for_unused_pid(self):
        # A very high PID is almost certainly not live on any test host.
        assert pc.pid_exists(2_000_000_000) is False

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
        # Our own process is a Python interpreter — its command line must mention
        # python/pytest on every platform, and the call must never raise.
        cl = pc.process_command_line(os.getpid())
        assert isinstance(cl, str)
        assert "python" in cl.lower() or "pytest" in cl.lower()

    def test_dead_pid_returns_empty_string(self):
        # A non-existent PID yields "" (fail-closed), never an exception.
        assert pc.process_command_line(2_000_000_000) == ""


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
            assert dt.fmt == "%-I:%M %p"   # untouched on POSIX

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
            assert pc.is_executable_file(f) is True   # .sh extension → runnable
        else:
            assert pc.is_executable_file(f) is False  # no x-bit → not runnable
        os.chmod(f, 0o755)  # +x
        assert pc.is_executable_file(f) is True       # runnable on both now

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
        monkeypatch.setattr(
            pc.subprocess, "check_output", lambda *a, **k: "3.12\n"
        )
        got = pc.find_python_interpreter()
        assert got == real
        assert pc._is_windows_store_python_stub(got) is False

    def test_returns_none_when_only_stub_or_too_old(self, monkeypatch):
        # No usable interpreter: which() yields only the stub (Windows) / nothing,
        # or an interpreter that reports < 3.10. Either way → None, never the stub.
        if pc.IS_WINDOWS:
            stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.EXE"
            monkeypatch.setattr("shutil.which", lambda name: stub)
        else:
            monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")
            monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: "3.9\n")
        assert pc.find_python_interpreter() is None


class TestUtf8Console:
    def test_ensure_utf8_console_is_safe_to_call(self):
        # No-op on POSIX; reconfigures stdout/stderr on Windows. Either way it
        # must never raise (it swallows non-reconfigurable streams), and must be
        # idempotent (safe to call from both __main__ and cli.main).
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
        # This is a WINDOWS-only behavior: ensure_utf8_console() is a deliberate
        # no-op on POSIX (which already defaults to UTF-8), so forcing a cp1252
        # stderr here and asserting emoji survives only makes sense on Windows —
        # on POSIX the function intentionally leaves the forced cp1252 stream
        # alone, so the emoji would (correctly) fail to encode. Gate accordingly.
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

    def test_proc_cpu_seconds_nonnegative(self):
        assert pc.proc_cpu_seconds() >= 0.0

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
            pc.fchmod_safe(fd, 0o600)   # applies on POSIX, no-op on Windows
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# POSIX-branch coverage for the new platform_compat helpers (Mesh-2329). The
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

    def test_process_matches_true_for_self_python(self):
        # The test interpreter's /proc/<pid>/cmdline contains "python", so the
        # needle matches -> True. Exercises the Linux read_bytes() + any() path.
        result = pc.process_matches(os.getpid(), ("python",))
        if pc.IS_POSIX:
            assert result is True

    def test_process_matches_false_for_self_with_absent_needle(self):
        # Same /proc read as the True case, but a needle that cannot occur in a
        # python interpreter's argv -> any() is False (not via an exception).
        result = pc.process_matches(os.getpid(), ("zzz-not-in-any-cmdline",))
        assert isinstance(result, bool)
        if pc.IS_POSIX:
            assert result is False


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
        monkeypatch.setattr(pc.subprocess, "run",
                            self._fake_run(128, b"process not found"))
        with pytest.raises(ProcessLookupError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(ProcessLookupError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_rc5_maps_to_permission_error(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.subprocess, "run",
                            self._fake_run(5, b"access denied"))
        with pytest.raises(PermissionError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(PermissionError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_generic_rc_maps_to_oserror(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.subprocess, "run",
                            self._fake_run(42, b"weird error"))
        with pytest.raises(OSError) as ei:
            pc.kill_pid(99999, pc.SIGKILL)
        # not one of the more specific subclasses
        assert not isinstance(ei.value, (ProcessLookupError, PermissionError))

    def test_taskkill_success_returns_true_on_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(0))
        assert pc.kill_pid(99999, pc.SIGKILL) is True
        assert pc.kill_process_tree(99999, pc.SIGKILL) is True

    def test_taskkill_subprocess_error_wraps_as_oserror(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        def _boom(*_a, **_kw):
            raise FileNotFoundError(2, "taskkill.exe not found")
        monkeypatch.setattr(pc.subprocess, "run", _boom)
        with pytest.raises(OSError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(OSError):
            pc.kill_process_tree(99999, pc.SIGKILL)


class TestRestrictToOwnerArgvOnLinux:
    """Regression guard for the Windows icacls DACL argv (bolichen-4d14 fix).

    Runs on the Linux CI fleet by monkeypatching IS_WINDOWS + subprocess.run —
    the argv construction is platform-independent code, and without this the
    security-critical `icacls /inheritance:r /grant:r "*S-1-3-4:F" /grant:r
    "*<user-sid>:F"` string is only exercised on the author's manual Windows
    E2E (skipif-Windows tests don't run on AL2). A regression that mangles
    the flags or the S-1-3-4 SID silently reopens the parent-inherited-DACL
    gap.
    """

    def test_icacls_argv_includes_owner_and_user_grants(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # Reset the success-only SID memo so the monkeypatched stub wins
        monkeypatch.setattr(pc, "_USER_SID_CACHE", [])
        monkeypatch.setattr(pc, "_current_user_sid",
                            lambda: "*S-1-5-21-1-2-3-1000")
        captured: dict = {}

        def fake_run(argv, **_kw):
            captured["argv"] = list(argv)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(pc.subprocess, "run", fake_run)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        argv = captured["argv"]
        # icacls + path + /inheritance:r + Owner Rights grant + user-SID grant.
        assert argv[0].endswith("icacls") or "icacls" in argv[0]
        assert os.fspath(f) in argv
        assert "/inheritance:r" in argv
        # Grants come in (flag, "SID:F") pairs — assert both are present.
        grants = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "/grant:r"]
        assert "*S-1-3-4:F" in grants, grants
        assert "*S-1-5-21-1-2-3-1000:F" in grants, grants

    def test_icacls_nonzero_rc_raises_oserror_on_linux_shim_path(self, tmp_path, monkeypatch):
        # With a resolvable SID, an icacls non-zero rc still raises OSError so
        # the caller's warn-and-continue handler fires. Complements the
        # None-SID early-raise test below.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_USER_SID_CACHE", [])
        monkeypatch.setattr(pc, "_current_user_sid",
                            lambda: "*S-1-5-21-9-9-9-9")
        monkeypatch.setattr(pc.subprocess, "run",
                            lambda *a, **k: types.SimpleNamespace(
                                returncode=1, stdout=b"", stderr=b"denied"))
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)

    def test_none_sid_raises_before_icacls_to_avoid_lockout(self, tmp_path, monkeypatch):
        # When _current_user_sid() returns None (whoami absent / fails /
        # unparseable), restrict_to_owner MUST refuse to apply a lockdown —
        # granting only S-1-3-4 (Owner Rights) with inheritance stripped
        # locks non-owner users out of their own file (elevated first-run,
        # backup restore, SYSTEM-context service scenarios). Fail-loud with
        # OSError BEFORE invoking icacls; the caller's warn handler fires
        # and the pre-existing DACL is preserved unchanged.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_USER_SID_CACHE", [])
        monkeypatch.setattr(pc, "_current_user_sid", lambda: None)
        called = []

        def fake_run(argv, **_kw):
            called.append(list(argv))
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(pc.subprocess, "run", fake_run)
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        with pytest.raises(OSError) as ei:
            pc.restrict_to_owner(f)
        assert "current user SID" in str(ei.value) or "whoami" in str(ei.value)
        # icacls must NOT have been spawned — the whole point is to avoid
        # applying a half-configured lockdown.
        assert called == [], f"icacls should not run when SID is unknown: {called}"

    def test_sid_failure_is_not_cached_success_is(self, monkeypatch):
        # A transient whoami failure (timeout under AV scan, non-zero rc) must
        # NOT be memoized: with lru_cache the first failure poisoned every
        # later restrict_to_owner for the process lifetime. The success-only
        # memo retries after a failure and caches only a resolved SID.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_USER_SID_CACHE", [])
        attempts = []

        def flaky_run(argv, **_kw):
            attempts.append(argv)
            if len(attempts) == 1:
                return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
            return types.SimpleNamespace(
                returncode=0, stdout=b'"ANT\\user","S-1-5-21-1-2-3-500"', stderr=b"")

        monkeypatch.setattr(pc.subprocess, "run", flaky_run)
        assert pc._current_user_sid() is None          # first call fails...
        assert pc._current_user_sid() == "*S-1-5-21-1-2-3-500"  # ...retry succeeds
        assert pc._current_user_sid() == "*S-1-5-21-1-2-3-500"  # ...and is cached
        assert len(attempts) == 2, "success must be memoized (no third spawn)"


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


class TestRestrictToOwner:
    """Fail-loud owner-only lockdown used by every ~/.kirocrew secret writer.

    The bolichen-4d14 finding on CR-283504528 was that the earlier
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
        # Windows path: shell out to icacls, then re-read the DACL via icacls
        # to confirm the expected owner-only shape (S-1-3-4 with F, no inherit).
        # This is the actual defect bolichen-4d14 flagged, so verify it end-to-end.
        if not pc.IS_WINDOWS:
            pytest.skip("Windows DACL branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)
        pc.restrict_to_owner(f)
        out = subprocess.check_output(
            ["icacls", str(f)], stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
        # Owner Rights SID rendered as "OWNER RIGHTS" in the DACL dump, with (F)
        # for full control; inheritance stripping means "(I)" (inherited) markers
        # from parent ACEs are gone.
        assert "OWNER RIGHTS:(F)" in out
        assert "(I)(F)" not in out  # no inherited full-control ACEs left

    def test_propagates_oserror_on_windows_when_icacls_missing(self, tmp_path, monkeypatch):
        # The fail-loud contract on Windows: icacls returning nonzero or
        # failing to launch MUST raise OSError so the caller's warn-and-continue
        # handler fires (dead-code otherwise, per AutoSDE). Simulate by pointing
        # the resolver at a nonexistent binary; the SubprocessError branch of
        # subprocess.run is what raises FileNotFoundError -> OSError below.
        if not pc.IS_WINDOWS:
            pytest.skip("Windows icacls branch")
        f = tmp_path / "secret.key"
        f.write_bytes(b"s" * 32)

        real_run = subprocess.run

        def failing_run(argv, **kwargs):
            if argv and "icacls" in str(argv[0]).lower():
                raise FileNotFoundError(2, "icacls.exe not found")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(pc.subprocess, "run", failing_run)
        monkeypatch.setattr(pc.shutil, "which", lambda _name: None)
        with pytest.raises(OSError):
            pc.restrict_to_owner(f)


class TestResourceShimFailures:
    def test_proc_rss_bytes_returns_zero_on_getrusage_failure(self, monkeypatch):
        # The failure branch: getrusage raising OSError must yield 0, not raise.
        if not pc.IS_POSIX:
            pytest.skip("POSIX resource.getrusage branch")

        def boom(*args, **kwargs):
            raise OSError("getrusage failed")

        monkeypatch.setattr(pc.resource, "getrusage", boom)
        assert pc.proc_rss_bytes() == 0

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
        # lsof can emit the same PID multiple times (one row per fd); the helper
        # must dedupe while preserving first-seen order.
        if not pc.IS_POSIX:
            pytest.skip("POSIX lsof branch")
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *a, **k: "111\n111\n222\n")
        assert pc.find_listening_pids(7777) == [111, 222]

    def _fake_netstat(self, blob: str):
        """Return a fake subprocess.check_output that returns *blob*."""
        def _run(*_a, **_kw):
            return blob
        return _run

    def test_windows_finds_ipv6_listener_via_netstat(self, monkeypatch):
        # Regression: Mesh-2364. Windows netstat -ano prints IPv6 LISTEN rows
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
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [99]

    def test_windows_accepts_tcp6_label_defensively(self, monkeypatch):
        # Today Windows netstat prints plain "TCP" for both families, but we
        # relaxed the proto check from `== "TCP"` to `startswith("TCP")` to
        # future-proof against a hypothetical Windows build that switches to
        # "TCP6" (the netstat -p flag already accepts "tcpv6"). Guard the
        # defensive path so a future relabel doesn't silently re-break this.
        blob = (
            "  TCP6   [::1]:7777             [::]:0                 LISTENING       77\n"
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
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
        monkeypatch.setattr(pc.subprocess, "check_output", self._fake_netstat(blob))
        assert pc.find_listening_pids(7777) == [44]

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


class TestKillAsyncVariants:
    """Regression guards for the async ``kill_pid_async`` / ``kill_process_tree_async``
    variants (Mesh-2801).

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

        result = _asyncio.new_event_loop().run_until_complete(
            pc.kill_pid_async(4242, pc.SIGKILL)
        )
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
        assert seen_executors == [sentinel], (
            f"expected the subprocess_executor sentinel, got {seen_executors!r}"
        )

    def test_windows_kill_process_tree_async_offloads_via_subprocess_executor(
        self, monkeypatch
    ):
        """Same offload contract as kill_pid_async but for the /T variant."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        sentinel = object()
        seen_executors: list[object] = []

        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_kw: types.SimpleNamespace(
                returncode=0, stdout=b"", stderr=b""
            ),
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
