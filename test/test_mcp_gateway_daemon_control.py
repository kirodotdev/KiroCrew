"""``kirocrew stop``/``restart``/``doctor`` see and stop the MCP gateway daemon.

The daemon is a session leader the gateway's SIGTERM never reaches; its own
owner-liveness exit is on a 15 s interval, and a restart spawns the next gateway
inside that window -- which then adopts the still-running daemon. So the CLI
stops it synchronously, and the doctor shows its code revision beside ours.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import cli_doctor, cli_server, platform_compat
from kiro_crew.code_fingerprint import code_fingerprint
from kiro_crew.mcp_gateway import daemon_control as dc

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX listener")


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


@contextlib.contextmanager
def _daemon_on(path: Path, reply: bytes | None, *, chunked: bool = False):
    """Serve ``reply`` on a REAL endpoint at ``path`` for the duration.

    A real listener rather than a faked transport: the round trip is what these
    tests are about, and a fake would only assert the author's beliefs about the
    client back at itself. ``reply=None`` accepts the connection and never
    answers, which is the hung-daemon case.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (TimeoutError, OSError):
                continue
            try:
                conn.recv(4096)
                if reply is None:
                    stop.wait(1.0)
                    continue
                if chunked:
                    # A stream hands back whatever is buffered, not whole frames.
                    for i in range(0, len(reply), 4):
                        conn.sendall(reply[i : i + 4])
                        time.sleep(0.002)
                else:
                    conn.sendall(reply)
            except OSError:
                pass
            finally:
                conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=2)
        srv.close()


@_POSIX_ONLY
class TestThePingIsARealRoundTrip:
    """One client answers "is a daemon there", and both callers depend on it.

    Neither can render an inconclusive probe: a ``None`` makes the doctor print
    "not running" and makes the CLI stop signal nothing, so a probe that cannot
    ask reports a live daemon as gone and leaves it for the next gateway to
    adopt. These drive a real listener; the Windows endpoint's own semantics
    (pipe name, read mode, server-principal check) belong to
    ``transport.connect`` and are covered in test_mcp_gateway_transport.py.
    """

    def test_a_live_daemon_is_read_off_the_wire(self, short_sock_dir: Path) -> None:
        sock = short_sock_dir / "gw.sock"
        with _daemon_on(sock, json.dumps(_pong(pid=4321)).encode() + b"\n"):
            info = dc.describe_daemon(sock)
        assert info is not None and info.pid == 4321
        assert info.matches_this_code is True

    def test_a_pong_split_across_reads_is_reassembled(self, short_sock_dir: Path) -> None:
        sock = short_sock_dir / "gw.sock"
        with _daemon_on(sock, json.dumps(_pong(pid=4321)).encode() + b"\n", chunked=True):
            info = dc.describe_daemon(sock)
        assert info is not None and info.pid == 4321

    def test_nothing_listening_is_no_daemon(self, short_sock_dir: Path) -> None:
        assert dc.describe_daemon(short_sock_dir / "absent.sock") is None

    def test_a_reply_that_is_not_a_pong_is_rejected(self, short_sock_dir: Path) -> None:
        sock = short_sock_dir / "gw.sock"
        with _daemon_on(sock, b'{"type":"nope"}\n'):
            assert dc.describe_daemon(sock) is None

    def test_a_truncated_reply_is_rejected(self, short_sock_dir: Path) -> None:
        """No newline: readuntil raises IncompleteReadError rather than hanging."""
        sock = short_sock_dir / "gw.sock"
        with _daemon_on(sock, b'{"type":"pong"'):
            assert dc.describe_daemon(sock) is None

    def test_a_daemon_that_accepts_but_never_answers_is_bounded(
        self, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sock = short_sock_dir / "gw.sock"
        monkeypatch.setattr(dc, "_PING_TIMEOUT_SECS", 0.2)
        started = time.monotonic()
        with _daemon_on(sock, None):
            assert dc.describe_daemon(sock) is None
        assert time.monotonic() - started < 5.0, "the deadline is the client's"


class _Writer:
    """Only the ``StreamWriter`` surface ``_ping_async`` touches.

    ``drain`` and ``wait_closed`` can be made to stall, which is how each
    ``asyncio.wait_for`` around them is shown to be load-bearing.
    """

    def __init__(self, *, drain_stalls: bool = False, close_stalls: bool = False) -> None:
        self.written = b""
        self.closed = False
        self._drain_stalls = drain_stalls
        self._close_stalls = close_stalls

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        if self._drain_stalls:
            await asyncio.sleep(3600)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self._close_stalls:
            await asyncio.sleep(3600)


def _double_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: bytes | None = None,
    eof: bool = True,
    connect_stalls: bool = False,
    connect_raises: BaseException | None = None,
    drain_stalls: bool = False,
    close_stalls: bool = False,
) -> dict:
    """Point ``transport.connect`` at an in-process pair. Platform-independent.

    The reader is a REAL :class:`asyncio.StreamReader`, so ``readuntil`` framing
    and its ``IncompleteReadError`` come from asyncio rather than from a stub's
    idea of them -- only the transport hop is doubled. That is what lets the
    windows-latest shards exercise this client, which cannot bind the ``AF_UNIX``
    listener the round-trip tests above use.
    """
    holder: dict = {}

    async def connect(path, **kw):
        holder["path"] = str(path)
        if connect_stalls:
            await asyncio.sleep(3600)
        if connect_raises is not None:
            raise connect_raises
        reader = asyncio.StreamReader()
        if reply:
            reader.feed_data(reply)
        if eof:
            reader.feed_eof()
        writer = _Writer(drain_stalls=drain_stalls, close_stalls=close_stalls)
        holder["writer"] = writer
        return reader, writer

    monkeypatch.setattr(dc.transport, "connect", connect)
    return holder


class TestThePingContractOnEveryPlatform:
    """The client's own behavior, with only the transport hop doubled.

    The round-trip class above needs an ``AF_UNIX`` listener and so cannot run on
    Windows; these carry the same contract there. Each stall case is what makes
    the corresponding ``asyncio.wait_for`` load-bearing: delete the wrapper and
    the case hangs instead of returning.
    """

    def test_a_pong_is_decoded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        h = _double_transport(monkeypatch, reply=json.dumps(_pong(pid=4321)).encode() + b"\n")
        info = dc.describe_daemon(tmp_path / "gw.sock")
        assert info is not None and info.pid == 4321
        assert h["writer"].written == b'{"type":"ping"}\n'
        assert h["writer"].closed is True

    @pytest.mark.parametrize(
        ("reply", "why"),
        [
            (b'{"type":"pong"', "no newline: readuntil raises IncompleteReadError"),
            (b"not json\n", "undecodable frame"),
            (b'{"type":"nope"}\n', "a reply that is not a pong"),
            (b"\n", "an empty frame"),
        ],
    )
    def test_a_reply_that_is_not_a_pong_is_no_daemon(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reply, why
    ) -> None:
        _double_transport(monkeypatch, reply=reply)
        assert dc.describe_daemon(tmp_path / "gw.sock") is None, why

    @pytest.mark.parametrize(
        "stall",
        ["connect_stalls", "drain_stalls"],
    )
    def test_a_stalled_phase_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stall
    ) -> None:
        monkeypatch.setattr(dc, "_PING_TIMEOUT_SECS", 0.05)
        _double_transport(monkeypatch, reply=json.dumps(_pong()).encode() + b"\n", **{stall: True})
        started = time.monotonic()
        assert dc.describe_daemon(tmp_path / "gw.sock") is None
        assert time.monotonic() - started < 5.0, f"{stall} is not bounded"

    def test_a_reply_that_never_arrives_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Accepted, drained, then silence: the read carries its own deadline."""
        monkeypatch.setattr(dc, "_PING_TIMEOUT_SECS", 0.05)
        _double_transport(monkeypatch, reply=None, eof=False)
        started = time.monotonic()
        assert dc.describe_daemon(tmp_path / "gw.sock") is None
        assert time.monotonic() - started < 5.0

    def test_a_stalled_close_does_not_swallow_the_answer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The pong is already decoded, so a peer that never finishes closing
        must not turn a successful probe into a hung CLI command."""
        monkeypatch.setattr(dc, "_PING_TIMEOUT_SECS", 0.05)
        _double_transport(
            monkeypatch, reply=json.dumps(_pong(pid=4321)).encode() + b"\n", close_stalls=True
        )
        started = time.monotonic()
        info = dc.describe_daemon(tmp_path / "gw.sock")
        assert info is not None and info.pid == 4321
        assert time.monotonic() - started < 5.0, "the close wait is bounded"


class TestThePingRefusesWhatItCannotAttribute:
    def test_the_ping_is_not_gated_on_the_platform(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Neither platform short-circuits to ``None`` before asking.

        This is the whole contract: a ``None`` from :func:`_ping` has to mean
        "nothing answered", because the doctor renders it as "not running" and
        the CLI stop renders it as nothing to signal. An arm that returns it
        without connecting reports a live daemon as gone.
        """
        reached: list[str] = []

        async def connect(path, **kw):
            reached.append(str(path))
            raise FileNotFoundError(2, "nothing listening")

        monkeypatch.setattr(dc.transport, "connect", connect)
        sock = tmp_path / "gw.sock"
        for is_windows in (False, True):
            reached.clear()
            monkeypatch.setattr(platform_compat, "IS_WINDOWS", is_windows)
            assert dc._ping(sock) is None
            assert reached == [str(sock)], f"the client is reached with IS_WINDOWS={is_windows}"

    def test_a_refused_endpoint_is_no_daemon(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``transport.connect`` refuses a Windows pipe served by another principal.

        It raises ``ConnectionRefusedError`` for that, the same OSError shape as
        "nothing is listening", and it must stay a refusal here: the pong names
        the pid and start token :func:`stop_daemon` signals, so an endpoint that
        cannot be attributed would get to choose the process an operator kills.
        """

        async def refused(path, **kw):
            raise ConnectionRefusedError("server principal not confirmed (mismatch)")

        monkeypatch.setattr(dc.transport, "connect", refused)
        killed: list[int] = []
        monkeypatch.setattr(
            platform_compat, "kill_pid_pinned", lambda pid, start, sig: killed.append(pid) or True
        )
        assert dc.describe_daemon(tmp_path / "gw.sock") is None
        assert dc.stop_daemon(tmp_path / "gw.sock", wait_secs=0.0) == "absent"
        assert killed == [], "an unattributable endpoint directs no signal"

    @pytest.mark.asyncio
    async def test_a_caller_inside_a_loop_is_reported_not_crashed(self, tmp_path: Path) -> None:
        """``asyncio.run`` refuses to nest; the never-raises contract holds."""
        assert dc._ping(tmp_path / "gw.sock") is None


class TestTheWindowsStopPathIsReachable:
    """Everything under the gate -- the WMI argv check, the handle-pinned kill.

    The client is one shared implementation, so what is platform-specific here is
    the SIGNAL half: Windows skips the pre-signal start-time read because
    ``kill_pid_pinned`` holds the identity open across the kill instead.
    """

    def _windows_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999))
        monkeypatch.setattr(
            platform_compat,
            "process_command_line",
            lambda pid: "python.exe -m kiro_crew.mcp_gateway.gatewayd",
        )
        monkeypatch.setattr(dc.transport, "singleton_lock_free", lambda p: True)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)

    def test_a_live_daemon_is_signalled_not_reported_away(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._windows_daemon(monkeypatch)
        sent: list[tuple[int, str, int]] = []
        monkeypatch.setattr(
            platform_compat,
            "kill_pid_pinned",
            lambda pid, start, sig: sent.append((pid, start, sig)) or True,
        )
        assert dc.stop_daemon(tmp_path / "gw.sock", wait_secs=1.0) == "stopped"
        assert sent == [(9999, "1000", platform_compat.SIGTERM)]

    def test_the_cli_stop_reports_the_daemon_it_took_down(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """``absent`` has no print arm, so silence there means nothing was found."""
        self._windows_daemon(monkeypatch)
        monkeypatch.setattr(dc, "configured_socket_path", lambda: tmp_path / "gw.sock")
        monkeypatch.setattr(platform_compat, "kill_pid_pinned", lambda pid, start, sig: True)
        cli_server._stop_mcp_gateway_daemon()
        assert "Stopped the MCP gateway daemon" in capsys.readouterr().out

    def test_the_doctor_reports_the_revision(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """The in-app diagnosis an operator needs: which daemon, whose code."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(dc, "_ping", lambda p: _pong(pid=9999, fingerprint="old-checkout"))
        monkeypatch.setattr(dc, "configured_socket_path", lambda: tmp_path / "gw.sock")
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
        issues: list[str] = []
        cli_doctor._doctor_mcp_gateway_daemon(issues)
        out = capsys.readouterr().out
        assert "not running" not in out
        assert "old-checkout" in out and "pid 9999" in out
        assert issues == ["MCP gateway daemon runs a different code revision than this install"]


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
