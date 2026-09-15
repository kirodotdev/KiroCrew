"""``kirocrew stop``/``restart``/``doctor`` see and stop the MCP gateway daemon.

The daemon is a session leader the gateway's SIGTERM never reaches; its own
owner-liveness exit is on a 15 s interval, and a restart spawns the next gateway
inside that window -- which then adopts the still-running daemon. So the CLI
stops it synchronously, and the doctor shows its code revision beside ours.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import cli_doctor, cli_server, platform_compat
from kiro_crew.code_fingerprint import code_fingerprint
from kiro_crew.mcp_gateway import daemon_control as dc


def _pong(**over):
    base = {
        "type": "pong",
        "targets": ["CORE"],
        "fingerprint": code_fingerprint(),
        "owner_pid": 4242,
        "pid": 9999,
        "start_time": "1000",
    }
    base.update(over)
    return base


class TestDescribeDaemon:
    def test_no_daemon_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: None)
        assert dc.describe_daemon(tmp_path / "gw.sock") is None

    def test_reads_every_field_and_tolerates_junk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid="nope", owner_pid=True, targets="x"))
        info = dc.describe_daemon(tmp_path / "gw.sock")
        assert info is not None
        assert info.pid == 0 and info.owner_pid == 0 and info.targets == ()
        assert info.matches_this_code is True
        assert info.owner_alive is None

    def test_owner_alive_consults_the_process_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: _pong())
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 4242)
        info = dc.describe_daemon(tmp_path / "gw.sock")
        assert info is not None and info.owner_alive is True


class TestConfiguredSocketPath:
    def test_a_configured_socket_path_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        cfg.mcp_gateway.socket_path = "/srv/crew/custom.sock"
        cfg.save()
        assert dc.configured_socket_path() == Path("/srv/crew/custom.sock")

    def test_no_configured_path_falls_back_to_the_data_home_default(self) -> None:
        assert dc.configured_socket_path() == dc.default_socket_path()

    def test_the_zero_argument_readers_use_the_configured_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`kirocrew stop` and `doctor` call these with no argument; a moved
        socket must not make them report "absent" for a running daemon."""
        seen: list[Path] = []
        monkeypatch.setattr(dc, "configured_socket_path", lambda: Path("/srv/crew/custom.sock"))
        monkeypatch.setattr(dc, "_ping", lambda p: seen.append(p) or None)
        assert dc.describe_daemon() is None
        assert dc.stop_daemon() == "absent"
        assert seen == [Path("/srv/crew/custom.sock")] * 2


class TestStopDaemon:
    def test_absent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: None)
        assert dc.stop_daemon(tmp_path / "gw.sock") == "absent"

    def test_refuses_to_signal_a_pid_that_is_not_gatewayd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A forged pong cannot aim a SIGTERM at an arbitrary process."""
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=1))
        monkeypatch.setattr(platform_compat, "process_command_line", lambda pid: "/sbin/init")
        killed: list[int] = []
        monkeypatch.setattr(
            platform_compat, "kill_pid_pinned", lambda pid, start, sig: killed.append(pid) or True
        )
        assert dc.stop_daemon(tmp_path / "gw.sock") == "unverified"
        assert killed == []

    def test_a_daemon_that_reported_no_start_token_is_not_signalled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999, start_time=""))
        monkeypatch.setattr(
            platform_compat,
            "process_command_line",
            lambda pid: "python -m kiro_crew.mcp_gateway.gatewayd",
        )
        killed: list[int] = []
        monkeypatch.setattr(
            platform_compat, "kill_pid_pinned", lambda pid, start, sig: killed.append(pid) or True
        )
        assert dc.stop_daemon(tmp_path / "gw.sock") == "unverified"
        assert killed == []

    def test_a_recycled_pid_is_caught_on_posix_before_the_signal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """POSIX kill_pid_pinned has no handle to hold; the compare is here."""
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999, start_time="1000"))
        monkeypatch.setattr(
            platform_compat,
            "process_command_line",
            lambda pid: "python -m kiro_crew.mcp_gateway.gatewayd",
        )
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "7777")
        killed: list[int] = []
        monkeypatch.setattr(
            platform_compat, "kill_pid_pinned", lambda pid, start, sig: killed.append(pid) or True
        )
        assert dc.stop_daemon(tmp_path / "gw.sock") == "stopped"
        assert killed == []

    def test_a_pid_recycled_between_ping_and_signal_is_not_signalled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999))
        monkeypatch.setattr(
            platform_compat,
            "process_command_line",
            lambda pid: "python -m kiro_crew.mcp_gateway.gatewayd",
        )
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "1000")
        # The pinned kill reports the identity did not hold: nothing was sent.
        monkeypatch.setattr(platform_compat, "kill_pid_pinned", lambda pid, start, sig: False)
        assert dc.stop_daemon(tmp_path / "gw.sock") == "stopped"

    def test_sigterms_and_waits_for_the_lock(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999))
        monkeypatch.setattr(
            platform_compat,
            "process_command_line",
            lambda pid: "python -m kiro_crew.mcp_gateway.gatewayd",
        )
        sent: list[tuple[int, str, int]] = []
        # The pin is the token the DAEMON reported; the local read only CONFIRMS
        # it still names the same process, immediately before the signal.
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "1000")
        monkeypatch.setattr(
            platform_compat,
            "kill_pid_pinned",
            lambda pid, start, sig: sent.append((pid, start, sig)) or True,
        )
        monkeypatch.setattr(dc.transport, "singleton_lock_free", lambda p: True)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
        assert dc.stop_daemon(tmp_path / "gw.sock", wait_secs=1.0) == "stopped"
        assert sent == [
            (9999, "1000", platform_compat.SIGTERM)
        ], "SIGTERM through the start-time-pinned kill, never SIGKILL: the drain reaps the pool"

    def test_a_slow_drain_is_reported_not_forced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999))
        monkeypatch.setattr(
            platform_compat,
            "process_command_line",
            lambda pid: "python -m kiro_crew.mcp_gateway.gatewayd",
        )
        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "1000")
        monkeypatch.setattr(platform_compat, "kill_pid_pinned", lambda pid, start, sig: True)
        monkeypatch.setattr(dc.transport, "singleton_lock_free", lambda p: False)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
        assert dc.stop_daemon(tmp_path / "gw.sock", wait_secs=0.2) == "draining"

    def test_permission_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999))
        monkeypatch.setattr(
            platform_compat,
            "process_command_line",
            lambda pid: "python -m kiro_crew.mcp_gateway.gatewayd",
        )

        def denied(pid, start, sig):
            raise PermissionError

        monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "1000")
        monkeypatch.setattr(platform_compat, "kill_pid_pinned", denied)
        assert dc.stop_daemon(tmp_path / "gw.sock") == "denied"


class TestCliStopTakesTheDaemonDown:
    @pytest.mark.parametrize(
        ("outcome", "fragment"),
        [
            ("stopped", "Stopped the MCP gateway daemon"),
            ("draining", "draining"),
            ("denied", "No permission to stop the MCP gateway daemon"),
            ("unverified", "left it alone"),
            ("absent", None),
        ],
    )
    def test_each_outcome_is_reported_once_and_never_fails_the_stop(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], outcome, fragment
    ) -> None:
        monkeypatch.setattr(dc, "stop_daemon", lambda: outcome)
        cli_server._stop_mcp_gateway_daemon()
        out = capsys.readouterr().out
        if fragment is None:
            assert out == ""
        else:
            assert fragment in out

    def test_the_service_path_stops_the_daemon_too(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A systemd-managed gateway still spawns its own daemon."""
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
        monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: True)
        called: list[int] = []
        monkeypatch.setattr(dc, "stop_daemon", lambda: called.append(1) or "stopped")
        cli_server._stop(None)
        assert called == [1]
        assert "Stopped the MCP gateway daemon" in capsys.readouterr().out


class TestDoctorShowsTheDaemonRevision:
    def test_not_running(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(dc, "describe_daemon", lambda: None)
        issues: list[str] = []
        cli_doctor._doctor_mcp_gateway_daemon(issues)
        assert "not running" in capsys.readouterr().out
        assert issues == []

    def test_same_code_is_fine(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        info = dc.DaemonInfo(Path("s"), 9999, 4242, code_fingerprint(), ("CORE",))
        monkeypatch.setattr(dc, "describe_daemon", lambda: info)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
        issues: list[str] = []
        cli_doctor._doctor_mcp_gateway_daemon(issues)
        out = capsys.readouterr().out
        assert "✅" in out and "same code" in out and "owner pid 4242 alive" in out
        assert issues == []

    def test_different_code_is_an_issue_with_the_fix_named(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        info = dc.DaemonInfo(Path("s"), 9999, 4242, "old-checkout", ("CORE",))
        monkeypatch.setattr(dc, "describe_daemon", lambda: info)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
        issues: list[str] = []
        cli_doctor._doctor_mcp_gateway_daemon(issues)
        out = capsys.readouterr().out
        assert "❌" in out and "old-checkout" in out and "GONE" in out
        assert "kirocrew restart" in out
        assert issues == ["MCP gateway daemon runs a different code revision than this install"]

    def test_a_pre_fingerprint_daemon_is_named_as_such(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        info = dc.DaemonInfo(Path("s"), 9999, 0, "", ())
        monkeypatch.setattr(dc, "describe_daemon", lambda: info)
        issues: list[str] = []
        cli_doctor._doctor_mcp_gateway_daemon(issues)
        assert "pre-fingerprint build" in capsys.readouterr().out
        assert len(issues) == 1
