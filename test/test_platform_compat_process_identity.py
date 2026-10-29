"""Who a process is: its parent, start identity, exact argv and liveness.

``get_ppid`` and ``process_matches`` against a real child, the whole-argv check
behind reclaiming a recorded child (``process_argv_matches_exact``), the start
time every PID-reuse guard compares, this process's own cached start identity,
the three-way ``pid_liveness`` probe, and the thread-group-leader reads that
narrow liveness on Linux. A topic of the ``test_platform_compat`` family; see
``test_platform_compat.py``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import wait_until

# Tests here read or build the macOS libc and libproc handles, so each one starts
# with both caches unbuilt and puts them back after (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


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
            # flake-ok: the child writes its readiness byte first; EOF on its death fails the assert below
            ready = child.stdout.read(1)
            assert ready == b"R", f"child did not signal readiness: {ready!r}"
            assert child.poll() is None  # still alive after signalling
            if pc.IS_POSIX:
                # /proc/<pid>/cmdline is guaranteed populated after exec, but
                # keep a short retry for edge cases on exotic kernels.
                result = wait_until(
                    lambda: pc.process_matches(child.pid, (token,)),
                    timeout=10.0,
                    describe=lambda: f"process_matches never returned True for {child.pid}",
                )
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


#: Lost-run ceiling for the positive exact-argv read, not a race to tune. One
#: Windows read is a PowerShell spawn that ``process_command_line`` bounds at 10 s.
#: Measured from the windows-pytest-progress artifacts: the whole test's call phase
#: takes 1.2 s on a passing run, and a loaded runner has spent the full 10.02 s on
#: one read that timed out. 25 s is over 10x the passing call, leaves room for a
#: retry after two timed-out reads, and is under half the suite's 120 s
#: ``--timeout``, so a read that never matches fails by name.
_ARGV_READ_CEILING_SECS = 25.0

#: How long a probe child sleeps after its readiness byte. It outlives every read
#: a test makes (the ceiling, a matching read that runs its full 10 s, then the
#: near-miss reads) and any ``--timeout``; ``_reap`` kills it in ``finally``.
_PROBE_CHILD_LIFE_SECS = 300


class TestProcessArgvMatchesExact:
    """The strict identity check behind reclaiming a recorded-but-orphaned
    child: the WHOLE argv must match, element for element, and every failure
    answers False — an unconfirmable identity must never be signalled."""

    def _spawn(self, token: str):
        argv = [
            sys.executable,
            "-c",
            f"import sys, time; sys.stdout.write('R'); sys.stdout.flush(); "
            f"time.sleep({_PROBE_CHILD_LIFE_SECS})  # {token}",
        ]
        child = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # flake-ok: the child writes its readiness byte first; EOF on its death fails the assert below
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

    @staticmethod
    def _assert_matches_once_readable(pid, argv):
        """Poll the positive exact-argv read until it confirms; fail by name at the ceiling.

        The read is best-effort on every OS: /proc can lag on a loaded runner,
        and on Windows one read is a PowerShell ``Get-CimInstance`` spawn with
        its own 10 s timeout that answers "" when it runs out.
        """
        wait_until(
            lambda: pc.process_argv_matches_exact(pid, argv),
            timeout=_ARGV_READ_CEILING_SECS,
            describe=lambda: f"last command line {pc.process_command_line(pid)!r}",
        )

    @staticmethod
    def _argv_read_diagnosis(pid):
        """Say which branch of ``process_argv_fingerprint`` answered None for *pid*."""
        if sys.platform == "linux":
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            except Exception as exc:  # noqa: BLE001 - the diagnosis names it
                return f"/proc/{pid}/cmdline read raised {type(exc).__name__}: {exc}"
            return f"/proc/{pid}/cmdline read {len(raw)} byte(s)"
        if sys.platform == "darwin":
            return f"darwin_process_argv({pid}) answered {pc.darwin_process_argv(pid)!r}"
        return f"no argv reader on {sys.platform}"

    @classmethod
    def _fingerprint_once_readable(cls, pid):
        """Poll the argv fingerprint until it reads; fail by name at the ceiling.

        ``Popen`` returns once the child's close-on-exec pipe shuts, which Linux
        does before it publishes the new image's argv, so a read right after it
        can find ``/proc/<pid>/cmdline`` still empty and answer None.
        """
        return wait_until(
            lambda: pc.process_argv_fingerprint(pid),
            timeout=_ARGV_READ_CEILING_SECS,
            interval=0.05,
            describe=lambda: f"pid {pid}: {cls._argv_read_diagnosis(pid)}",
        )

    def test_the_fingerprint_read_retries_an_argv_that_is_not_published_yet(self, monkeypatch):
        # The first reads after Popen can see an empty cmdline, which answers None.
        reads = iter([None, None, "f" * 64])
        seen: list[int] = []

        def read(pid):
            seen.append(pid)
            return next(reads)

        monkeypatch.setattr(pc, "process_argv_fingerprint", read)

        assert self._fingerprint_once_readable(4242) == "f" * 64
        assert seen == [4242, 4242, 4242]

    @pytest.mark.skipif(sys.platform != "linux", reason="names the Linux /proc branch")
    def test_a_fingerprint_that_never_reads_fails_naming_the_branch(self, monkeypatch):
        monkeypatch.setattr(pc, "process_argv_fingerprint", lambda pid: None)
        monkeypatch.setitem(globals(), "_ARGV_READ_CEILING_SECS", 0.0)

        with pytest.raises(AssertionError) as failed:
            self._fingerprint_once_readable(UNALLOCATABLE_PID)

        message = str(failed.value)
        assert "1 polls" in message
        assert f"pid {UNALLOCATABLE_PID}: /proc/{UNALLOCATABLE_PID}/cmdline read raised" in message
        assert "FileNotFoundError" in message

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
            self._assert_matches_once_readable(child.pid, argv)
            changed = list(argv)
            changed[-1] = changed[-1] + " "
            if pc.IS_POSIX:
                # Anything less than the whole argv is a different process:
                # a subset (prefix), a superset, and a one-element difference
                # must all answer False — substring semantics are exactly what
                # this function exists to NOT have.
                near_misses = {"subset": argv[:-1], "superset": argv + ["-x"], "changed": changed}
            else:
                # Windows: the live command line must equal list2cmdline(argv).
                # The -c payload carries spaces and quotes, so real quoting runs.
                near_misses = {"subset": argv[:-1], "changed": changed}
            answers = {
                name: pc.process_argv_matches_exact(child.pid, near)
                for name, near in near_misses.items()
            }
            # A read of a process that has exited answers False for the wrong
            # reason. A child still running after the last read was running
            # for every one of them, so only then do the False answers count.
            assert child.poll() is None, (
                f"probe child exited (returncode {child.returncode!r}) before the "
                f"near-miss reads finished; their answers {answers!r} prove nothing"
            )
            for name, answer in answers.items():
                assert answer is False, f"{name} near-miss matched: {near_misses[name]!r}"
        finally:
            self._reap(child)

    # Shapes a Windows ssh forward argv can carry: a host or ProxyCommand with
    # spaces, embedded double quotes, and backslashes before a quote or at the end.
    _WINDOWS_ARGVS = [
        [r"C:\Windows\System32\OpenSSH\ssh.exe", "-N", "-L", "7778:127.0.0.1:7777", "host"],
        [r"C:\Program Files\OpenSSH\ssh.exe", "-N", "my host alias"],
        [
            r"C:\Windows\System32\OpenSSH\ssh.exe",
            "-o",
            'ProxyCommand=C:\\tools\\proxy.exe --name "bastion one" %h %p',
            "h",
        ],
        ["ssh.exe", "-o", 'ProxyCommand=cmd /c "a\\" b', "trail\\"],
        ["ssh.exe", ""],
    ]

    def _as_windows(self, monkeypatch, command_line):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        seen: list[int] = []

        def read(pid):
            seen.append(pid)
            if isinstance(command_line, BaseException):
                raise command_line
            return command_line

        monkeypatch.setattr(pc, "process_command_line", read)
        return seen

    @pytest.mark.parametrize("argv", _WINDOWS_ARGVS)
    def test_windows_matches_the_list2cmdline_string_exactly(self, monkeypatch, argv):
        seen = self._as_windows(monkeypatch, subprocess.list2cmdline(argv))

        assert pc.process_argv_matches_exact(4242, argv) is True
        assert seen == [4242]

    @pytest.mark.parametrize("argv", _WINDOWS_ARGVS)
    def test_windows_naive_join_or_near_miss_does_not_match(self, monkeypatch, argv):
        expected = subprocess.list2cmdline(argv)
        for actual in {
            " ".join(argv),
            expected + " ",
            expected + " -x",
            subprocess.list2cmdline(argv[:-1]),
            expected.replace("ssh", "SSH"),
        } - {expected}:
            self._as_windows(monkeypatch, actual)
            assert pc.process_argv_matches_exact(4242, argv) is False, actual

    @pytest.mark.parametrize("unreadable", ["", OSError("wmi down"), RuntimeError("boom")])
    def test_windows_unreadable_command_line_answers_false(self, monkeypatch, unreadable):
        self._as_windows(monkeypatch, unreadable)

        assert pc.process_argv_matches_exact(4242, ["ssh.exe", "h"]) is False

    def test_the_positive_read_retries_a_command_line_that_is_not_readable_yet(self, monkeypatch):
        # A loaded Windows runner can time out the PowerShell read, which
        # answers "". The live test's positive read must retry it, not fail.
        argv = [r"C:\Windows\System32\OpenSSH\ssh.exe", "-N", "my host alias"]
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        reads = iter(["", "", subprocess.list2cmdline(argv)])
        seen: list[int] = []

        def read(pid):
            seen.append(pid)
            return next(reads)

        monkeypatch.setattr(pc, "process_command_line", read)

        self._assert_matches_once_readable(4242, argv)
        assert seen == [4242, 4242, 4242]

    @pytest.mark.parametrize("exits_after_read", [None, 1, 2])
    def test_the_near_miss_answers_count_only_against_a_live_probe_child(
        self, monkeypatch, exits_after_read
    ):
        # A slow runner can spend the probe child's life on the reads. An
        # exited child's command line reads as "", so a near-miss read then
        # answers False with nothing proven, and the test must fail instead.
        argv = [r"C:\Python\python.exe", "-c", "import sys; print('a b') # probe"]
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        reads: list[int] = []

        def gone():
            return exits_after_read is not None and len(reads) >= exits_after_read

        child = types.SimpleNamespace(pid=UNALLOCATABLE_PID, returncode=None)

        def poll():
            child.returncode = 0 if gone() else None
            return child.returncode

        child.poll = poll
        reaped: list[int] = []
        monkeypatch.setattr(
            TestProcessArgvMatchesExact, "_spawn", lambda self, token: (child, list(argv))
        )
        monkeypatch.setattr(
            TestProcessArgvMatchesExact, "_reap", staticmethod(lambda c: reaped.append(c.pid))
        )

        def read(pid):
            was_gone = gone()
            reads.append(pid)
            return "" if was_gone else subprocess.list2cmdline(argv)

        monkeypatch.setattr(pc, "process_command_line", read)

        if exits_after_read is None:
            self.test_exact_argv_matches_and_near_misses_do_not()
        else:
            with pytest.raises(AssertionError, match="probe child exited"):
                self.test_exact_argv_matches_and_near_misses_do_not()
        # One matching read, then both near-miss reads, in every case.
        assert reads == [UNALLOCATABLE_PID] * 3
        assert reaped == [UNALLOCATABLE_PID]

    def test_windows_rejects_bad_pid_before_any_read(self, monkeypatch):
        seen = self._as_windows(monkeypatch, "ssh.exe h")

        for bad in (0, 1, -5, True, "4242"):
            assert pc.process_argv_matches_exact(bad, ["ssh.exe", "h"]) is False
        assert pc.process_argv_matches_exact(4242, []) is False
        assert seen == []

    def test_argv_fingerprint_is_stable_and_tells_processes_apart(self):
        """The same live process fingerprints the same every time; another
        argv fingerprints differently; a pid nothing runs at answers None."""

        def spawn(n):
            if pc.IS_POSIX:
                sleep_bin = shutil.which("sleep") or "/bin/sleep"
                return subprocess.Popen([sleep_bin, str(300 + n)], start_new_session=True)
            return self._spawn(f"kirocrew-argvsig-{n}")[0]

        first = spawn(0)
        try:
            second = spawn(1)
            try:
                if pc.IS_POSIX:
                    a = self._fingerprint_once_readable(first.pid)
                    b = self._fingerprint_once_readable(second.pid)
                    assert len(a) == 64
                    assert pc.process_argv_fingerprint(first.pid) == a
                    assert b != a
                else:
                    # Windows records no fingerprint: reclaim keeps the exact check.
                    assert pc.process_argv_fingerprint(first.pid) is None
                    assert pc.process_argv_fingerprint(second.pid) is None
            finally:
                self._reap(second)
        finally:
            self._reap(first)
        for bad in (0, 1, -5, True, "4242"):
            assert pc.process_argv_fingerprint(bad) is None

    def test_windows_records_no_argv_fingerprint(self, monkeypatch):
        """Windows keeps the exact command-line check, so nothing is read."""
        seen = self._as_windows(monkeypatch, "ssh.exe -N h")

        assert pc.process_argv_fingerprint(4242) is None
        assert seen == []

    def test_cmd_shim_target_never_matches_on_windows(self, monkeypatch):
        # A .cmd entrypoint runs under cmd.exe, whose command line differs.
        argv = [r"C:\Program Files\Amazon\AWSCLIV2\aws.cmd", "ssm", "start-session"]
        self._as_windows(
            monkeypatch,
            r'C:\WINDOWS\system32\cmd.exe /c ""C:\Program Files\Amazon\AWSCLIV2\aws.cmd" ssm start-session"',
        )
        assert pc.process_argv_matches_exact(4242, argv) is False

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

    @staticmethod
    def _fake_linux_proc(monkeypatch, tmp_path, stat: bytes) -> None:
        """Serve *stat* as ``/proc/4242/stat`` on any host."""
        (tmp_path / "4242").mkdir()
        (tmp_path / "4242" / "stat").write_bytes(stat)
        real_path = pc.Path
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "Path", lambda p: tmp_path if p == "/proc" else real_path(p))

    def test_linux_reads_the_starttime_field_past_a_parenthesised_comm(self, monkeypatch, tmp_path):
        """Splitting on the FIRST ')' would mis-index any comm containing one."""

        tail = " ".join(str(i) for i in range(4, 24))
        self._fake_linux_proc(monkeypatch, tmp_path, f"4242 (my (odd) proc) S 1 {tail}".encode())
        assert pc.process_start_time(4242) == "21"

    def test_a_comm_that_is_not_utf8_still_has_a_start_time(self, monkeypatch, tmp_path):
        """A name cut mid-character at 15 bytes: the identity is still readable."""
        tail = " ".join(str(i) for i in range(4, 24))
        comm = "run_データ処理.py".encode()[:15]
        self._fake_linux_proc(monkeypatch, tmp_path, b"4242 (" + comm + b") S 1 " + tail.encode())
        assert pc.process_start_time(4242) == "21"
        assert pc.parent_pid(4242) == 1

    def test_a_malformed_stat_line_fails_safe(self, monkeypatch, tmp_path):
        self._fake_linux_proc(monkeypatch, tmp_path, b"no closing paren here")
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

    # flake-ok: no test in this class calls monkeypatch.undo(), so the shared monkeypatch cannot lift this pin
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this class calls monkeypatch.undo()
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
        monkeypatch.setattr(pc, "_darwin_tb_frequency", lambda: 1_000_000_000)
        assert pc._darwin_process_cpu_nanos(4242) == 7_500_000_000

    @staticmethod
    def _taskinfo_lib(user: int, system: int):
        class _FakeLib:
            @staticmethod
            def proc_pidinfo(_pid, _flavor, _arg, buf, size):
                raw = bytearray(size)
                raw[pc._DARWIN_PTI_TOTAL_USER_OFFSET : pc._DARWIN_PTI_TOTAL_USER_OFFSET + 8] = (
                    user
                ).to_bytes(8, "little")
                raw[pc._DARWIN_PTI_TOTAL_SYSTEM_OFFSET : pc._DARWIN_PTI_TOTAL_SYSTEM_OFFSET + 8] = (
                    system
                ).to_bytes(8, "little")
                buf.raw = bytes(raw)
                return size

        return _FakeLib()

    def test_darwin_cpu_nanos_scales_mach_ticks_by_the_tb_frequency(self, monkeypatch):
        """``proc_taskinfo`` counts Mach ticks. Apple Silicon's timebase runs at
        24 MHz, so 180M ticks of CPU are 7.5 s, not 0.18 s."""
        lib = self._taskinfo_lib(168_000_000, 12_000_000)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: lib)
        monkeypatch.setattr(pc, "_darwin_tb_frequency", lambda: 24_000_000)
        assert pc._darwin_process_cpu_nanos(4242) == 7_500_000_000

    def test_darwin_cpu_nanos_without_a_tb_frequency_is_none(self, monkeypatch):
        """An unscaled count would be wrong by the timebase factor: refuse it."""
        lib = self._taskinfo_lib(7_000_000_000, 500_000_000)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: lib)
        monkeypatch.setattr(pc, "_darwin_tb_frequency", lambda: None)
        assert pc._darwin_process_cpu_nanos(4242) is None

    @staticmethod
    def _sysctl_libc(answers: dict, rc: int = 0, size: int = 8):
        """A libc whose ``sysctlbyname`` answers *answers* and whose
        ``mach_timebase_info`` answers 1/1, as Rosetta 2 does for an x86_64
        process on Apple Silicon."""

        class _FakeLibc:
            asked: list = []

            @staticmethod
            def sysctlbyname(name, buf, size_ref, _newp, _newlen):
                _FakeLibc.asked.append(name)
                if rc != 0 or name not in answers:
                    return rc or -1
                ctypes.memmove(buf, answers[name].to_bytes(8, "little"), size)
                size_ref._obj.value = size
                return 0

            @staticmethod
            def mach_timebase_info(ref):
                ref._obj.numer = 1
                ref._obj.denom = 1
                return 0

        return _FakeLibc()

    def test_tb_frequency_reads_the_sysctl(self, monkeypatch):
        libc = self._sysctl_libc({b"hw.tbfrequency": 24_000_000})
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: libc)
        assert pc._darwin_tb_frequency() == 24_000_000
        assert libc.asked == [b"hw.tbfrequency"]

    def test_tb_frequency_accepts_a_32_bit_answer(self, monkeypatch):
        libc = self._sysctl_libc({b"hw.tbfrequency": 24_000_000}, size=4)
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: libc)
        assert pc._darwin_tb_frequency() == 24_000_000

    def test_rosetta_timebase_does_not_undercount_cpu(self, monkeypatch):
        """Under Rosetta 2 ``mach_timebase_info`` answers 1/1 while
        ``proc_taskinfo`` still counts native 24 MHz ticks. The reader takes the
        scale from ``hw.tbfrequency``, so 1 s of CPU (24M ticks) reads 1 s, not
        the 24 ms a 1/1 timebase would give -- which the 10 ms movement floor
        would otherwise see as near idle."""
        libc = self._sysctl_libc({b"hw.tbfrequency": 24_000_000})
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: libc)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: self._taskinfo_lib(24_000_000, 0))
        assert pc._darwin_process_cpu_nanos(4242) == 1_000_000_000

    @pytest.mark.parametrize(
        ("answers", "rc", "size"),
        [
            ({b"hw.tbfrequency": 0}, 0, 8),
            ({b"hw.tbfrequency": 24_000_000}, 5, 8),
            ({b"hw.tbfrequency": 24_000_000}, 0, 2),
            ({}, 0, 8),
        ],
    )
    def test_tb_frequency_refuses_zero_a_failed_call_or_a_wrong_size(
        self, monkeypatch, answers, rc, size
    ):
        libc = self._sysctl_libc(answers, rc=rc, size=size)
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: libc)
        assert pc._darwin_tb_frequency() is None

    def test_tb_frequency_without_a_libc_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: None)
        assert pc._darwin_tb_frequency() is None
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: object())
        assert pc._darwin_tb_frequency() is None

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

    def test_pid_liveness_unsignalable_for_out_of_range_pid(self, monkeypatch):
        """A corrupt PID stamp is unknown, never evidence that a holder died."""
        monkeypatch.setattr(pc, "IS_POSIX", True)

        def fake_kill(pid, sig):
            raise OverflowError("Python int too large to convert to C long")

        monkeypatch.setattr(pc.os, "kill", fake_kill)
        assert pc.pid_liveness(10**100) == pc.PID_UNSIGNALABLE
        assert pc.pid_exists(10**100) is True

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


class TestLiveThreadGroupLeaders:
    """``live_thread_group_leaders`` narrows liveness; it must fail OPEN."""

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc lists leaders on Linux only")
    def test_own_process_is_a_leader(self):
        leaders = pc.live_thread_group_leaders()
        assert leaders is not None
        assert os.getpid() in leaders

    @pytest.mark.skipif(sys.platform != "linux", reason="tids share the pid space on Linux only")
    def test_a_live_thread_is_not_a_leader(self):
        """The whole point: a tid is signalable and has /proc, but is not a process.

        Uses a real thread's native id rather than a synthetic ``/proc`` so the
        assertion rests on kernel behaviour, not on a fixture's idea of it.
        """
        box: dict[str, int] = {}
        captured = threading.Event()
        release = threading.Event()

        def _hold() -> None:
            box["tid"] = threading.get_native_id()
            captured.set()
            release.wait(timeout=30)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        try:
            assert captured.wait(timeout=30)
            tid = box["tid"]
            assert tid != os.getpid()
            # Signalable and openable under /proc — the two things a naive check reads.
            assert pc.pid_exists(tid) is True
            leaders = pc.live_thread_group_leaders()
            assert leaders is not None
            assert tid not in leaders, "a non-leader tid must not appear in the /proc listing"
            assert os.getpid() in leaders, "its group leader must still appear"
        finally:
            release.set()
            holder.join(timeout=30)

    def test_non_linux_fails_open(self, monkeypatch):
        """Off Linux the question is unanswerable, so never claim 'thread'."""
        monkeypatch.setattr(pc, "IS_LINUX", False)
        assert pc.live_thread_group_leaders() is None

    def test_unreadable_proc_fails_open(self, monkeypatch):
        """An OSError reading /proc yields None (retain), never an empty set."""
        monkeypatch.setattr(pc, "IS_LINUX", True)

        def _boom(*_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(pc.os, "listdir", _boom)
        assert pc.live_thread_group_leaders() is None

    def test_numberless_proc_fails_open(self, monkeypatch):
        """A listing with no pids is nonsense, not 'every recorded pid is a thread'."""
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc.os, "listdir", lambda *_a, **_k: ["cpuinfo", "meminfo", "self"])
        assert pc.live_thread_group_leaders() is None


class TestIsThreadGroupLeader:
    """The per-pid re-read, for a pid a host-wide snapshot answers wrongly."""

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc carries Tgid on Linux only")
    def test_own_process_is_a_leader(self):
        assert pc.is_thread_group_leader(os.getpid()) is True

    @pytest.mark.skipif(sys.platform != "linux", reason="tids share the pid space on Linux only")
    def test_a_live_thread_is_not_a_leader(self):
        """The whole point: a signalable tid must answer False, not None."""
        box: dict[str, int] = {}
        captured = threading.Event()
        release = threading.Event()

        def _hold() -> None:
            box["tid"] = threading.get_native_id()
            captured.set()
            release.wait(timeout=30)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        try:
            assert captured.wait(timeout=30), "helper thread never reported its tid"
            tid = box["tid"]
            assert tid != os.getpid()
            assert pc.pid_exists(tid) is True, "a tid is signalable -- that is the trap"
            assert pc.is_thread_group_leader(tid) is False
        finally:
            release.set()
            holder.join(timeout=30)

    def test_non_linux_is_unknowable(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_LINUX", False)
        assert pc.is_thread_group_leader(os.getpid()) is None

    def test_missing_status_is_unknowable(self, monkeypatch):
        """A pid that has gone must not read as 'not a process'."""
        monkeypatch.setattr(pc, "IS_LINUX", True)

        def _boom(*_a, **_k):
            raise FileNotFoundError("no such pid")

        monkeypatch.setattr("builtins.open", _boom)
        assert pc.is_thread_group_leader(4242) is None

    def test_malformed_status_is_unknowable(self, monkeypatch):
        """A status file with no parsable Tgid answers None, never False."""
        import io

        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(
            "builtins.open", lambda *_a, **_k: io.StringIO("Name:\tx\nTgid:\tnotanumber\n")
        )
        assert pc.is_thread_group_leader(4242) is None
