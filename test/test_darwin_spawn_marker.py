"""The macOS arm of the ``KIROCREW_SPAWNED`` identity oracle.

The marked-MCP-launcher sweep needs positive identity before it signals
anything, because the cmdlines it matches (``npx @playwright/mcp``,
``<launcher> mcp start-server <name>``) are ones a user's own shell can
reproduce exactly. That identity is the exec-time environment, and until now
only Linux could read it -- so on macOS the launcher markers matched and the
sweep still declined, so the reaper is inert on that platform.

``sysctl KERN_PROCARGS2`` is the macOS oracle: the same record ``ps -E`` prints,
readable for a same-uid process with no entitlement and no elevated privilege,
and already read in this codebase for argv. These tests pin the environ parse
and all three platform arms with a fake ``libc``, so they hold on every host;
the real-kernel proof is ``TestDarwinEnvironIsReadableSameUid`` below, which the
macOS suite runs.
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import session_pid as sp
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

_MARKER = f"{KIROCREW_SPAWNED_ENV}={KIROCREW_SPAWNED_VALUE}".encode()


def _procargs(
    argv: list[bytes],
    environ: list[bytes],
    *,
    exe: bytes = b"/usr/bin/npx",
    argc: int | None = None,
    padding: int = 8,
) -> bytes:
    """One ``KERN_PROCARGS2`` answer: argc, exec path, NUL padding, argv, environ.

    *argc* overrides the count the kernel reports, which is how a record whose
    argv runs past the strings actually present gets built.
    """
    count = len(argv) if argc is None else argc
    body = exe + b"\0" * padding
    for entry in argv + environ:
        body += entry + b"\0"
    return struct.pack("<i", count) + body


class _FakeLibc:
    """``sysctl(CTL_KERN, KERN_PROCARGS2, pid)`` over a canned record table.

    A missing pid is the kernel's refusal (-1), matching what it answers for
    another user's process and for one that has exited.
    """

    def __init__(self, table: dict[int, bytes]) -> None:
        self.table = table
        self.calls: list[int] = []

    def sysctl(self, mib, namelen, buf, size_ref, _newp, _newlen) -> int:  # noqa: ANN001
        assert namelen == 3
        assert mib[0] == pc._DARWIN_CTL_KERN and mib[1] == pc._DARWIN_KERN_PROCARGS2
        self.calls.append(mib[2])
        data = self.table.get(mib[2])
        if data is None:
            return -1
        size = size_ref._obj
        data = data[: size.value]  # the kernel truncates to the buffer, never fails
        ctypes.memmove(buf, data, len(data))
        size.value = len(data)
        return 0


@pytest.fixture
def libc(monkeypatch: pytest.MonkeyPatch):
    def _install(table: dict[int, bytes]) -> _FakeLibc:
        fake = _FakeLibc(table)
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: fake)
        return fake

    return _install


class TestDarwinProcessEnviron:
    """The parse: environ entries only, counted off argv rather than guessed."""

    def test_reads_the_environment_after_argv(self, libc) -> None:
        libc({77: _procargs([b"npx", b"@playwright/mcp"], [b"PATH=/usr/bin", _MARKER])})
        assert pc.darwin_process_environ(77) == [b"PATH=/usr/bin", _MARKER]

    def test_argv_shaped_like_an_environment_entry_is_not_returned(self, libc) -> None:
        """The forgery the count-based skip exists to refuse.

        A user's own shell can put anything in argv, so a record whose ARGUMENT
        reads ``KIROCREW_SPAWNED=1`` must not answer for the environment -- that
        would hand an unprivileged process the marker the sweep kills on.
        """
        libc({77: _procargs([b"npx", _MARKER], [b"PATH=/usr/bin"])})
        assert pc.darwin_process_environ(77) == [b"PATH=/usr/bin"]

    def test_empty_argv_entry_still_counts(self, libc) -> None:
        """An empty argument is a real argv slot; miscounting it shifts the split."""
        libc({77: _procargs([b"npx", b"", b"--headless"], [_MARKER])})
        assert pc.darwin_process_environ(77) == [_MARKER]

    def test_record_without_an_environment_is_unreadable(self, libc) -> None:
        """argv alone filling the record is not evidence of an empty environment."""
        libc({77: _procargs([b"npx", b"@playwright/mcp"], [])})
        assert pc.darwin_process_environ(77) is None

    def test_argc_past_the_strings_present_is_refused(self, libc) -> None:
        libc({77: _procargs([b"npx"], [_MARKER], argc=9)})
        assert pc.darwin_process_environ(77) is None

    @pytest.mark.parametrize("argc", [0, -1])
    def test_implausible_argc_is_refused(self, libc, argc: int) -> None:
        libc({77: _procargs([b"npx"], [_MARKER], argc=argc)})
        assert pc.darwin_process_environ(77) is None

    def test_truncated_record_is_refused(self, libc) -> None:
        libc({77: b"\x01\x00"})
        assert pc.darwin_process_environ(77) is None

    def test_unreadable_pid_is_none(self, libc) -> None:
        libc({})
        assert pc.darwin_process_environ(999999) is None

    def test_missing_libc_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: None)
        assert pc.darwin_process_environ(77) is None

    def test_buffer_is_sized_for_argv_plus_environment(self) -> None:
        """The environment sits after argv in one record.

        A long argv must not push the environment out of the read, because the
        launcher shape this gate identifies appends to its own argv on every
        generation. The bound is the kernel's own ``ARG_MAX`` ceiling on the
        pair, so the argv probe's smaller bound is not reusable here.
        """
        assert pc._DARWIN_PROCARGS_ENV_BUFSIZE >= 1024 * 1024
        assert pc._DARWIN_PROCARGS_ENV_BUFSIZE > pc._DARWIN_PROCARGS_BUFSIZE

    def test_a_long_argv_does_not_hide_the_environment(self, libc) -> None:
        """A launcher that self-appends: argv past the argv bound, marker behind it."""
        argv = [b"mcp-launcher", b"mcp", b"start-server", b"npm:@playwright/mcp"]
        argv += [b"/opt/node_modules/@playwright/mcp/cli.js"] * 4000
        libc({77: _procargs(argv, [_MARKER])})
        assert len(_procargs(argv, [_MARKER])) > pc._DARWIN_PROCARGS_BUFSIZE
        assert pc.darwin_process_environ(77) == [_MARKER]


class TestPlatformArms:
    """One arm per platform, and the two that were already correct stay so."""

    _CMDLINE = b"mcp-launcher\x00mcp start-server\x00npm:@playwright/mcp"

    def test_linux_reads_proc_and_never_the_darwin_oracle(self, tmp_path) -> None:
        """BEFORE and AFTER are the same read: ``/proc/<pid>/environ``."""
        proc = tmp_path / "101"
        proc.mkdir()
        (proc / "environ").write_bytes(b"PATH=/usr/bin\x00" + _MARKER + b"\x00")

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(pc, "darwin_process_environ") as mock_darwin,
        ):
            mock_sys.platform = "linux"
            assert sp._read_env_has_kirocrew_marker(101, tmp_path) is True
            assert sp._env_has_kirocrew_marker(101, tmp_path) is True
        mock_darwin.assert_not_called()

    def test_linux_without_the_marker_is_false(self, tmp_path) -> None:
        proc = tmp_path / "102"
        proc.mkdir()
        (proc / "environ").write_bytes(b"PATH=/usr/bin\x00")

        with patch("kiro_crew.session_pid.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert sp._read_env_has_kirocrew_marker(102, tmp_path) is False

    def test_windows_stays_unproven_and_never_reads_the_darwin_oracle(self) -> None:
        """Windows has no same-uid environ oracle; the gate must keep refusing."""
        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(pc, "darwin_process_environ") as mock_darwin,
        ):
            mock_sys.platform = "win32"
            assert sp._read_env_has_kirocrew_marker(103) is None
            assert sp._env_has_kirocrew_marker(103) is False
            assert sp._is_sweepable_orphan_mcp(103, self._CMDLINE) is False
        mock_darwin.assert_not_called()

    def test_darwin_marker_present_is_proven(self) -> None:
        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(pc, "darwin_process_environ", return_value=[b"PATH=/bin", _MARKER]),
        ):
            mock_sys.platform = "darwin"
            assert sp._read_env_has_kirocrew_marker(104) is True
            assert sp._is_sweepable_orphan_mcp(104, self._CMDLINE) is True

    def test_darwin_marker_absent_is_a_refusal_not_an_unknown(self) -> None:
        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(pc, "darwin_process_environ", return_value=[b"PATH=/bin"]),
        ):
            mock_sys.platform = "darwin"
            assert sp._read_env_has_kirocrew_marker(105) is False
            assert sp._is_sweepable_orphan_mcp(105, self._CMDLINE) is False

    def test_darwin_unreadable_fails_closed(self) -> None:
        """An unreadable environment is unproven identity, never a licence."""
        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(pc, "darwin_process_environ", return_value=None),
        ):
            mock_sys.platform = "darwin"
            assert sp._read_env_has_kirocrew_marker(106) is None
            assert sp._env_has_kirocrew_marker(106) is False
            assert sp._is_sweepable_orphan_mcp(106, self._CMDLINE) is False

    def test_darwin_does_not_relax_the_cmdline_gate(self) -> None:
        """A readable marker is not identity on its own: the shape still rules."""
        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(pc, "darwin_process_environ", return_value=[_MARKER]),
        ):
            mock_sys.platform = "darwin"
            assert sp._is_sweepable_orphan_mcp(107, b"vim\x00notes-about-mcp.md") is False

    def test_a_fixture_process_table_outranks_the_host(self, tmp_path) -> None:
        """A supplied *proc_root* must decide on every host, macOS included.

        Otherwise a fixture-driven test would read the machine it happens to run
        on instead of the table it was handed.
        """
        proc = tmp_path / "108"
        proc.mkdir()
        (proc / "environ").write_bytes(_MARKER + b"\x00")

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(pc, "darwin_process_environ") as mock_darwin,
        ):
            mock_sys.platform = "darwin"
            assert sp._read_env_has_kirocrew_marker(108, tmp_path) is True
        mock_darwin.assert_not_called()


class TestTheArmLicensesNothingElse:
    """Seven predicates read this marker; exactly one gains a macOS verdict.

    The oracle is shared, so making it answer on macOS could in principle hand
    kill authority to every other sweep class at once. It does not: each of the
    others is blocked by a *different* Linux-only read it ALSO requires, and
    these tests force the marker to its most permissive answer to prove the
    block is the other read rather than the marker.
    """

    # macOS cmdlines arrive from ``ps -o command=``: space-joined, no NULs.
    _PYTEST_PS = b"/opt/venv/bin/pytest test/test_thing.py"
    _DAEMON_PS = b"node /opt/playwright-core/lib/entry/cliDaemon.js kc-1a2b3c4d"

    @pytest.fixture
    def darwin_with_marker(self, monkeypatch: pytest.MonkeyPatch):
        """macOS, and every environ read answers "marked"."""
        monkeypatch.setattr(pc, "darwin_process_environ", lambda _pid: [_MARKER])
        with patch("kiro_crew.session_pid.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert sp._env_has_kirocrew_marker(4242) is True, "fixture must be permissive"
            yield

    def test_the_work_class_still_declines(self, darwin_with_marker) -> None:
        """Its session-leader test reads ``/proc`` and fails closed to "alive"."""
        assert sp._is_sweepable_orphan_work(4242, self._PYTEST_PS, 86_400.0) is False

    def test_the_browser_daemon_class_still_declines(self, darwin_with_marker) -> None:
        """Its session name must come from a NUL-separated argv, which ``ps`` is not."""
        assert sp._is_sweepable_orphan_browser_daemon(4242, self._DAEMON_PS, 86_400.0) is False

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="the reap reads the caller's process group, which Windows has no notion of",
    )
    def test_the_descendant_reap_still_signals_nothing(self, darwin_with_marker) -> None:
        """Per-member argv is ``/proc``-only, so each member fails closed unread.

        Windows is excluded because the reap asks ``os.getpgrp()`` for the
        caller's own group before it looks at any member -- the whole sweep is a
        documented no-op there, so there is no verdict to assert.
        """
        with patch.object(pc, "kill_pid") as mock_kill:
            killed = sp._kill_orphan_mcp_descendants([(4242, "tok-4242")], root=1, budget=5)
        assert killed == 0
        mock_kill.assert_not_called()

    def test_the_launcher_class_is_the_one_that_gains(self, darwin_with_marker) -> None:
        """The whole point of the change, stated next to what it did not change."""
        cmdline = b"mcp-launcher mcp start-server npm:@playwright/mcp /opt/mcp/cli.js"
        assert sp._is_sweepable_orphan_mcp(4242, cmdline) is True


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the real Darwin kernel")
@pytest.mark.xdist_group(name="subprocess_spawn")
class TestDarwinEnvironIsReadableSameUid:
    """The oracle's proof: a real same-uid child, no elevated privilege.

    An identity oracle that cannot be demonstrated is not usable here, because a
    wrong True is a SIGKILL on somebody else's process. These two tests are that
    demonstration -- ordinary user, ordinary child, real ``sysctl``.
    """

    @staticmethod
    def _child(env: dict[str, str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(["sleep", "30"], env=env)

    def test_a_child_spawned_with_the_marker_is_identified(self) -> None:
        """Polls briefly: the record shows the parent's environment until exec
        completes. Production is immune -- the sweep's min-age floor is minutes.
        """
        env = {**os.environ, KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE}
        proc = self._child(env)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if sp._env_has_kirocrew_marker(proc.pid):
                    break
                time.sleep(0.05)
            assert sp._env_has_kirocrew_marker(proc.pid) is True
            assert os.geteuid() != 0, "the read must be proven unprivileged"
        finally:
            proc.kill()
            proc.wait()

    def test_a_child_spawned_without_the_marker_is_refused(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != KIROCREW_SPAWNED_ENV}
        proc = self._child(env)
        try:
            assert sp._env_has_kirocrew_marker(proc.pid) is False
        finally:
            proc.kill()
            proc.wait()
