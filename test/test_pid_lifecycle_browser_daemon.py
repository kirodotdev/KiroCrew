"""Tests for the orphaned playwright-cli browser daemon sweep.

Reading the generated session name off a daemon's argv, deciding whether the
session's owner is still alive, and sweeping the daemons whose owner is gone.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pid_lifecycle_helpers import _POSIX_ONLY

from kiro_crew import platform_compat

# ── Orphaned playwright-cli browser daemon sweep ───────────────

#: A realistic NUL-separated cliDaemon argv. playwright-core spawns the daemon
#: as ``node <...>/entry/cliDaemon.js <sessionName> [flags]`` (see
#: cli-client/session.js ``startDaemon``), so the session name is the argv
#: element immediately after the entry script.
_DAEMON_CMDLINE = (
    b"/usr/bin/node\x00"
    b"/home/u/.npm/_npx/e41f/node_modules/playwright-core/lib/entry/cliDaemon.js\x00"
    b"kc-1a2b3c4d\x00--headed"
)


_OPERATOR_DAEMON_CMDLINE = (
    b"/usr/bin/node\x00"
    b"/home/u/.npm/_npx/e41f/node_modules/playwright-core/lib/entry/cliDaemon.js\x00"
    b"chrome"
)


class TestBrowserDaemonSessionArg:
    """Structural extraction of the generated session name from daemon argv."""

    def test_extracts_generated_session_name(self) -> None:
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_DAEMON_CMDLINE) == b"kc-1a2b3c4d"

    def test_rejects_operator_named_session(self) -> None:
        """Only Kiro-Crew-generated ``kc-<8hex>`` names are ever sweepable."""
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_OPERATOR_DAEMON_CMDLINE) is None

    def test_rejects_space_joined_cmdline(self) -> None:
        """A ps-style space-joined cmdline cannot delimit argv safely."""
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_DAEMON_CMDLINE.replace(b"\x00", b" ")) is None

    def test_rejects_non_daemon_cmdline(self) -> None:
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(b"/usr/bin/node\x00server.js\x00kc-1a2b3c4d") is None

    def test_session_env_name_matches_launch_module(self) -> None:
        """Drift ratchet: the local constant must track the real env var."""
        from kiro_crew.browser_cli.launch import SESSION_ENV
        from kiro_crew.session_pid import _BROWSER_SESSION_ENV

        assert _BROWSER_SESSION_ENV == SESSION_ENV


class TestBrowserDaemonOrphanSweep:
    """A stranded generated-session daemon is reclaimed; a live one never is."""

    def test_dead_owner_daemon_is_a_candidate(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[800]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == [800]

    def test_live_owner_daemon_is_never_a_candidate(self) -> None:
        """The safety invariant: a live agent's browser is never reclaimed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[801]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=True,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_operator_session_daemon_is_never_a_candidate(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[802]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_OPERATOR_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"chrome"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_unmarked_daemon_is_never_a_candidate(self) -> None:
        """No ``KIROCREW_SPAWNED`` marker means we did not spawn this tree."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[803]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_young_daemon_is_never_a_candidate(self) -> None:
        """The work-class age floor applies: a fresh daemon is never raced."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[804]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=200.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_env_argv_session_mismatch_is_never_a_candidate(self) -> None:
        """argv name must be the generated name this process was exec'd with."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[805]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-99999999"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    @pytest.mark.parametrize(
        ("start_tokens", "cmdlines", "expected_killed"),
        [
            pytest.param(
                ("daemon-token", "daemon-token"),
                (_DAEMON_CMDLINE, _DAEMON_CMDLINE),
                1,
                id="stable-identity",
            ),
            pytest.param(
                (None, None),
                (_DAEMON_CMDLINE, _DAEMON_CMDLINE),
                0,
                id="identity-unavailable",
            ),
            pytest.param(
                ("daemon-token", "replacement-token"),
                (_DAEMON_CMDLINE, _DAEMON_CMDLINE),
                0,
                id="pid-recycled",
            ),
            pytest.param(
                ("daemon-token", "daemon-token"),
                (_DAEMON_CMDLINE, b"/usr/bin/sleep\x0030"),
                0,
                id="cmdline-changed",
            ),
        ],
    )
    @_POSIX_ONLY
    def test_kill_revalidates_browser_daemon_identity(
        self,
        start_tokens: tuple[str | None, str | None],
        cmdlines: tuple[bytes, bytes],
        expected_killed: int,
    ) -> None:
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch.object(Path, "read_bytes", side_effect=cmdlines),
            patch("kiro_crew.session_pid._pid_start_token", side_effect=start_tokens),
            patch("kiro_crew.session_pid._is_sweepable_orphan_mcp", return_value=False),
            patch("kiro_crew.session_pid._is_sweepable_orphan_gatewayd", return_value=False),
            patch("kiro_crew.session_pid._is_sweepable_orphan_work", return_value=False),
            patch(
                "kiro_crew.session_pid._is_sweepable_orphan_browser_daemon",
                return_value=True,
            ),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch(
                "kiro_crew.session_pid._kill_orphan_browser_daemon",
                return_value=1,
            ) as mock_kill,
        ):
            mock_sys.platform = "linux"
            assert kill_orphan_mcps([806]) == expected_killed

        if expected_killed:
            mock_kill.assert_called_once_with(806, _DAEMON_CMDLINE)
        else:
            mock_kill.assert_not_called()


class TestBrowserSessionOwnerAlive:
    """The ownership probe reads only exec-time environ, never on-disk state."""

    def test_live_peer_holding_the_session_reads_as_alive(self) -> None:
        from kiro_crew import session_pid as sp

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/900"), Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=1),
            patch.object(sp, "_env_value", return_value=b"kc-1a2b3c4d"),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True

    def test_only_the_daemons_own_tree_reads_as_dead(self) -> None:
        """Chromium children share the daemon's SID and are not owners."""
        from kiro_crew import session_pid as sp

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/900"), Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=900),
            patch.object(sp, "_env_value", return_value=b"kc-1a2b3c4d"),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is False

    def test_unreadable_peer_fails_closed_to_alive(self) -> None:
        from kiro_crew import session_pid as sp

        def _boom(
            pid: int,
            key: str,
            proc_root: Path | None = None,
        ) -> bytes | None:
            raise PermissionError("inconclusive")

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=1),
            patch.object(sp, "_env_value", side_effect=_boom),
            patch.object(sp.platform_compat, "linux_process_name", return_value=None),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True

    def test_non_linux_fails_closed_to_alive(self) -> None:
        from kiro_crew import session_pid as sp

        with patch.object(sp, "sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True


class TestBrowserSessionOwnerFakeProc:
    """The full daemon predicate decides from a fixture-owned proc tree."""

    _SESSION = b"kc-1a2b3c4d"
    _DAEMON_ENV = b"PLAYWRIGHT_CLI_SESSION=kc-1a2b3c4d\x00" b"KIROCREW_SPAWNED=1\x00"

    @staticmethod
    def _process(
        proc_root: Path,
        pid: int,
        *,
        sid: int,
        cmdline: bytes,
        environ: bytes | None,
        cgroup: str = "0::/pod.slice\n",
    ) -> None:
        process = proc_root / str(pid)
        process.mkdir()
        (process / "stat").write_text(
            f"{pid} (fixture) S 1 {pid} {sid} 0 0 0 0\n",
            encoding="utf-8",
        )
        (process / "cmdline").write_bytes(cmdline)
        comm = cmdline.split(b"\x00", 1)[0].rsplit(b"/", 1)[-1].decode()
        (process / "comm").write_text(comm + "\n", encoding="utf-8")
        # The plausible-owner cases deliberately sit outside the daemon's
        # cgroup: name plausibility must keep them before cgroup filtering.
        if comm == "kiro-cli":
            cgroup = "0::/agent.slice\n"
        (process / "cgroup").write_text(cgroup, encoding="utf-8")
        if environ is None:
            # read_bytes() raises OSError on every platform without depending
            # on permission-bit enforcement or the test runner's uid.
            (process / "environ").mkdir()
        else:
            (process / "environ").write_bytes(environ)

    @staticmethod
    def _linux_fixture(
        monkeypatch: pytest.MonkeyPatch,
        sp: object,
        proc_root: Path,
    ) -> None:
        monkeypatch.setattr(sp, "sys", Mock(platform="linux"))
        monkeypatch.setattr(platform_compat, "sys", Mock(platform="linux"))
        monkeypatch.setattr(
            sp.os,
            "getuid",
            lambda: proc_root.stat().st_uid,
            raising=False,
        )

    def _daemon(self, proc_root: Path) -> None:
        self._process(
            proc_root,
            900,
            sid=900,
            cmdline=_DAEMON_CMDLINE,
            environ=self._DAEMON_ENV,
        )

    def test_unreadable_sd_pam_stub_does_not_veto_sweep(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=b"(sd-pam)\x00",
            environ=None,
            cgroup="0::/user.slice\n",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_pid"):
            assert sp._is_sweepable_orphan_browser_daemon(
                900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
            )

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "decision=ignore" in message and "reason=different_cgroup_unreadable" in message
            for message in messages
        )
        assert any(
            "decision=sweep" in message and "reason=no_owner_outside_daemon_tree" in message
            for message in messages
        )

    @pytest.mark.parametrize(
        ("proc_file", "invalid_content"),
        [
            ("comm", b"(sd-pam)\xff\n"),
            ("cgroup", b"0::/user.slice/\xff\n"),
        ],
    )
    def test_non_utf8_proc_metadata_still_reaches_sweep_verdict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        proc_file: str,
        invalid_content: bytes,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=b"(sd-pam)\x00",
            environ=None,
            cgroup="0::/user.slice\n",
        )
        (proc_root / "901" / proc_file).write_bytes(invalid_content)
        self._linux_fixture(monkeypatch, sp, proc_root)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_pid"):
            assert sp._is_sweepable_orphan_browser_daemon(
                900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
            )

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "decision=sweep" in message and "reason=no_owner_outside_daemon_tree" in message
            for message in messages
        )

    @pytest.mark.parametrize("owner_name", ["kiro-cli", "opencode"])
    def test_unreadable_plausible_owner_keeps_daemon(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        owner_name: str,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=f"/usr/bin/{owner_name}\x00chat\x00".encode(),
            environ=None,
            cgroup="0::/agent.slice\n",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert not sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )

    def test_live_matching_owner_keeps_daemon(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            901,
            sid=1,
            cmdline=b"/usr/bin/kiro-cli\x00chat\x00",
            environ=b"PLAYWRIGHT_CLI_SESSION=kc-1a2b3c4d\x00",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert not sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )

    def test_no_owner_is_sweepable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )

    def test_daemons_detached_tree_is_not_an_owner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import session_pid as sp

        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        self._daemon(proc_root)
        self._process(
            proc_root,
            902,
            sid=900,
            cmdline=b"/usr/bin/chromium\x00",
            environ=b"PLAYWRIGHT_CLI_SESSION=kc-1a2b3c4d\x00",
        )
        self._linux_fixture(monkeypatch, sp, proc_root)

        assert sp._is_sweepable_orphan_browser_daemon(
            900, _DAEMON_CMDLINE, 900.0, proc_root=proc_root
        )
