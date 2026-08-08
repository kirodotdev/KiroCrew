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
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types

import pytest

from kiro_crew import platform_compat as pc


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

    def test_pid_exists_false_after_kill_even_while_handle_open(self):
        # Windows OpenProcess succeeds for an EXITED process while any handle to
        # it is open (asyncio's transport keeps one until GC). pid_exists must
        # still report False via GetExitCodeProcess, or every session recycle
        # logs a false "PID survived kill" and leaks a dead PID into the tracker.
        # On POSIX this reaps normally and is equally False.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
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

    def test_proc_rss_bytes_is_positive_for_a_live_process(self):
        # A running interpreter always has resident memory. This must be > 0 on
        # every supported platform: on Windows GetCurrentProcess's handle was
        # truncated without argtypes and this silently returned 0, disabling the
        # watchdog's RSS ceiling.
        assert pc.proc_rss_bytes() > 0

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
        # On Windows self (no children spawned by this test) reads a positive
        # tree total that is at least the single-process RSS.
        assert result is not None and result > 0
        single = pc.proc_rss_bytes_for_pid(os.getpid())
        assert single is not None
        assert result >= single / (1024 * 1024) - 1  # -1: sampled microseconds apart

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
        # and rather than via os.path.isjunction (3.12+ only; this project
        # supports 3.10).
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
                sys.executable, "-c",
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
        monkeypatch.setattr(pc.subprocess, "run",
                            self._fake_run(128, b"process not found"))
        with pytest.raises(ProcessLookupError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(ProcessLookupError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_rc5_maps_to_permission_error(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run",
                            self._fake_run(5, b"access denied"))
        with pytest.raises(PermissionError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(PermissionError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_generic_rc_maps_to_oserror(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run",
                            self._fake_run(42, b"weird error"))
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
    """Regression guard for the Windows icacls DACL argv fix.

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
        # Force the whoami fallback, which is what this test covers. On a real
        # Windows host the non-spawn primary (the process token) succeeds, so
        # without this the flaky_run below is never reached and the first
        # assertion sees a genuine SID instead of the simulated failure.
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
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
        # Windows path: shell out to icacls, then re-read the DACL via icacls
        # to confirm the expected owner-only shape (S-1-3-4 with F, no inherit).
        # This is the actual defect the review flagged, so verify it end-to-end.
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
        # handler fires (dead-code otherwise, per review-bot). Simulate by pointing
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
        blob = (
            "  TCP6   [::1]:7777             [::]:0                 LISTENING       77\n"
        )
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
        assert seen_executors == [sentinel], (
            f"expected the subprocess_executor sentinel, got {seen_executors!r}"
        )

    def test_windows_kill_process_tree_async_offloads_via_subprocess_executor(
        self, monkeypatch
    ):
        """Same offload contract as kill_pid_async but for the /T variant."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

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


class TestWin32StructsAreModuleScoped:
    """``ctypes.POINTER(T)`` memoises T -> POINTER(T) forever.

    ctypes keeps that memo in a module-level dict with no eviction, so a
    Structure subclass declared inside a function body pins a fresh pair of type
    objects on EVERY call. The Windows metrics/enumeration helpers are polled
    (the dashboard's system-metrics endpoint, the RSS-recycle watchdog, the
    tree-kill parent-map walk, the MCP pipe's per-connection peer check), which
    turned that into unbounded growth in a long-running gateway -- measured at
    ~8 KiB per ``proc_rss_bytes`` call, never reclaimed.

    Asserting on the source keeps this enforceable from the POSIX fleet, where
    the Windows branches never execute.
    """

    #: Helpers whose Win32 struct layouts must come from module scope.
    _WIN32_STRUCT_USERS = (
        "get_ppid",
        "_windows_process_parent_map",
        "_win_process_image_name",
        "_process_token_sid_unguarded",
        "proc_rss_bytes",
        "proc_rss_bytes_for_pid",
        "system_memory",
    )

    def test_the_shared_layouts_are_defined_once_at_module_scope(self) -> None:
        import ctypes

        for name in (
            "_ProcessEntry32",
            "_ProcessMemoryCounters",
            "_MemoryStatusEx",
            "_SidAndAttributes",
            "_TokenUser",
        ):
            assert issubclass(getattr(pc, name), ctypes.Structure), name

    @pytest.mark.parametrize("func_name", _WIN32_STRUCT_USERS)
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

    def test_windows_without_a_sid_collapses_to_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        and restrict_to_owner is the only thing that protects the directory."""
        calls: list[str] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "restrict_to_owner", lambda p: calls.append(str(p)))
        target = tmp_path / "win"
        pc.make_owner_only_dir(target)
        assert target.is_dir()
        assert calls == [str(target)]

    def test_directory_still_exists_when_tightening_fails(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort on the tightening step: the caller decides whether an
        un-tightened directory is fatal, so creation must not be rolled back."""
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc, "restrict_to_owner", lambda p: (_ for _ in ()).throw(OSError("nope"))
        )
        target = tmp_path / "partial"
        pc.make_owner_only_dir(target)
        assert target.is_dir()


class TestCurrentUserSidNeverSpawns:
    """``current_user_sid`` is called from three event-loop paths: the gatewayd
    admission check, the client-side server check, and the pipe DACL builder --
    which runs once per pipe instance and so sits on the accept path.

    It used to delegate to ``_current_user_sid``, whose fallback is a ``whoami``
    subprocess with a 5 s timeout. A token-lookup failure therefore stalled
    accepts for seconds at a time, repeatedly. ``restrict_to_owner`` keeps that
    fallback because it always runs through ``asyncio.to_thread``.
    """

    @staticmethod
    def _forbid_spawn(*_a, **_kw):
        raise AssertionError(
            "current_user_sid must not spawn -- it runs on the event loop"
        )

    def test_returns_none_without_spawning_when_the_token_read_fails(
        self, monkeypatch
    ):
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
        platform_compat, "_windows_process_parent_map", lambda: (_ for _ in ()).throw(OSError("boom"))
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

    shim = tmp_path / "ps"
    shim.write_text("#!/bin/sh\necho '999999 999998'\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    parent_map = platform_compat._posix_process_parent_map()
    assert 999999 not in parent_map, "planted PATH shim was executed"
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
