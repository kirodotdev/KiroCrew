"""``kirocrew restart`` restarts a foreground POSIX gateway in place.

A gateway that a supervisor runs (a shell loop, a container entrypoint) must stay
the supervisor's child across a restart. ``_restart`` first asks the gateway to
re-exec itself (``POST /api/restart/when-idle`` with ``{"restart": true}``), waits
for the NEW generation (a fresh local secret plus a ready answer: the pid does not
change on exec), and never spawns a replacement after the gateway accepted. Every
gateway that refuses or cannot answer gets today's stop-and-spawn.

Nothing here reaches a real gateway: the secret reads, the send helper, the ready
probe, the run marker and the stop-and-spawn steps are all faked.
"""

from __future__ import annotations

import io
import json
import socket
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import cli_restart_headless
from kiro_crew import cli_restart_idle as idle_cli
from kiro_crew import cli_server, platform_compat
from kiro_crew.dashboard import headless as dashboard_headless
from kiro_crew.instances import run_marker
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_PORT = 7000
_OLD = "old-secret-value-0123456789abcdef"
_NEW = "new-secret-value-fedcba9876543210"
_OLD_PID = UNALLOCATABLE_PID
_NEW_PID = UNALLOCATABLE_PID + 1
_SPAWNED_PID = UNALLOCATABLE_PID + 2
#: The parent this CLI runs under: a shell, never the gateway it restarts.
_CLI_PARENT_PID = UNALLOCATABLE_PID + 3
_READY_TIMEOUT = 60
_BUDGET = int(cli_server._RESTART_STOP_TIMEOUT) + _READY_TIMEOUT
_SUCCESS = f"✅ Restarted the gateway in place (pid {_NEW_PID}). Logs: kirocrew logs -f"
_WARNING = "Could not read how the running gateway was started"


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        return 1_000_000.0 + self.t

    def sleep(self, secs: float) -> None:
        self.t += secs


class _ClockTime:
    """The ``time`` module the restart paths read, on a clock the test moves."""

    def __init__(self, clock: _Clock, sleep) -> None:
        self.monotonic = clock.now
        self.time = clock.wall
        self.sleep = sleep

    def __getattr__(self, name: str):
        return getattr(time, name)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status: int, body: dict | None = None) -> urllib.error.HTTPError:
    data = io.BytesIO(json.dumps(body or {}).encode())
    return urllib.error.HTTPError("http://127.0.0.1/", status, "error", {}, data)  # type: ignore[arg-type]


class _Host:
    """A gateway on :data:`_PORT` as the CLI sees it, and the stop-and-spawn steps."""

    def __init__(self, monkeypatch) -> None:
        self.clock = _Clock()
        self.secret = _OLD
        #: After how many secret reads the new generation's secret is published.
        self.boot_after_reads: int | None = 3
        self.reads = 0
        self.ready: int | list[int] = 200
        self.on_sleep = self.clock.sleep
        self.pid = _OLD_PID
        #: The pid the run marker names once the new generation boots. Through a
        #: launcher it is the launcher's new child; a plain exec keeps _OLD_PID.
        self.pid_after_boot = _NEW_PID
        self.headless: list[bool | None] = [False]
        self.sent: list[tuple] = []
        self.answer: object = _Resp(b'{"ok":true,"status":"restarting"}')
        self.applying: float | None = None
        self.stop = MagicMock()
        self.spawn = MagicMock(return_value=SimpleNamespace(pid=_SPAWNED_PID))
        self.token_url = MagicMock()
        self.client_access = MagicMock()
        self.sel = MagicMock()

        def read_secret(port, dial_host=None):
            assert (port, dial_host) == (_PORT, cli_server._CLI_LOOPBACK)
            self.reads += 1
            if self.boot_after_reads is not None and self.reads > self.boot_after_reads:
                self.secret = _NEW
                self.pid = self.pid_after_boot
            return self.secret

        def send(req, port, *, timeout, operation):
            self.sent.append((req, port, timeout, operation))
            if isinstance(self.answer, BaseException):
                raise self.answer
            return self.answer

        def headless_read(port):
            return self.headless.pop(0) if len(self.headless) > 1 else self.headless[0]

        def probe(port, timeout=3):
            if isinstance(self.ready, list):
                return self.ready.pop(0) if len(self.ready) > 1 else self.ready[0]
            return self.ready

        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        # Through the facade, so every restart owner reads the same moving clock.
        monkeypatch.setattr(
            cli_server, "time", _ClockTime(self.clock, lambda secs: self.on_sleep(secs))
        )
        monkeypatch.setattr(cli_server, "read_local_secret", read_secret)
        monkeypatch.setattr(cli_server, "_owner_verified_secret_urlopen", send)
        monkeypatch.setattr(cli_server, "_probe_gateway_ready", probe)
        monkeypatch.setattr(cli_server, "_resolve_restart_ready_timeout", lambda: _READY_TIMEOUT)
        monkeypatch.setattr(run_marker, "read_pid", lambda port: self.pid)
        monkeypatch.setattr(
            cli_server.update_next_start, "applying_started", lambda pid, **_k: self.applying
        )
        monkeypatch.setattr(cli_server, "sel", self.sel)
        monkeypatch.setattr(cli_server, "_print_token_url", self.token_url)
        monkeypatch.setattr(dashboard_headless, "print_client_access", self.client_access)
        monkeypatch.setattr(cli_restart_headless, "live_gateway_headless", headless_read)
        # The steps before the foreground path, and the stop-and-spawn path itself.
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: port or _PORT)
        monkeypatch.setattr(cli_server, "_print_running_cron_runs", lambda: None)
        monkeypatch.setattr(cli_server, "_refuse_desktop_supervised", lambda *a, **kw: False)
        monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: True)
        monkeypatch.setattr(
            cli_server.platform_compat, "find_listening_pids", lambda port: [_OLD_PID]
        )
        monkeypatch.setattr(cli_server, "_wait_for_pids_exit", lambda *a, **kw: [])
        monkeypatch.setattr(cli_server, "_refuse_if_lock_held", lambda *a, **kw: None)
        monkeypatch.setattr(cli_server, "_gateway_log_mark", lambda: None)
        # Never the recorded gateway unless a test says so. ``raising=False``: an
        # older tree has no such seam, and these tests still collect and set up there.
        self.parent = _CLI_PARENT_PID
        monkeypatch.setattr(cli_server, "_parent_pid", lambda: self.parent, raising=False)
        # Whether the process the run marker names holds the port, and whether this
        # host can look that up at all. Never a real socket-table read.
        self.owns_port = True
        self.lookup = True

        def owns(port):
            assert port == _PORT
            return self.owns_port

        monkeypatch.setattr(cli_server, "_gateway_owns_port", owns)
        monkeypatch.setattr(
            cli_server.platform_compat, "listening_pid_tool_available", lambda: self.lookup
        )
        monkeypatch.setattr(cli_server, "_stop", self.stop)
        monkeypatch.setattr(cli_server, "_spawn_detached_gateway", self.spawn)
        monkeypatch.setattr(
            cli_server, "_wait_gateway_ready", lambda *a: (cli_server._READY_OK, None)
        )

    def audits(self) -> list[dict]:
        return [c.kwargs for c in self.sel.return_value.log_api_access.call_args_list]

    def assert_stopped_and_spawned(self) -> None:
        self.stop.assert_called_once_with(_PORT)
        self.spawn.assert_called_once()

    def assert_not_stopped_or_spawned(self) -> None:
        self.stop.assert_not_called()
        self.spawn.assert_not_called()


@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return _Host(monkeypatch)


class TestInPlace:
    def test_the_gateway_restarts_itself_and_nothing_is_stopped_or_spawned(self, host, capsys):
        cli_server._restart(_PORT)

        host.assert_not_stopped_or_spawned()
        req, port, timeout, operation = host.sent[0]
        assert len(host.sent) == 1
        assert req.full_url == f"http://{cli_server._CLI_LOOPBACK}:{_PORT}/api/restart/when-idle"
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {"restart": True}
        assert req.get_header("X-internal-secret") == _OLD
        assert (port, operation) == (_PORT, "gateway_restart_in_place")
        out, err = capsys.readouterr()
        assert _SUCCESS in out.splitlines()
        assert host.audits() == [
            {
                "caller": "cli",
                "operation": "gateway_restart",
                "outcome": "allowed",
                "source": "cli",
                "resources": f"port={_PORT} via=in_place pid={_NEW_PID}",
            }
        ]
        host.token_url.assert_called_once_with(_PORT)
        host.client_access.assert_not_called()
        for text in (out, err):
            assert _OLD not in text and _NEW not in text
        assert _WARNING not in err

    def test_a_plain_exec_keeps_the_pid_and_the_line_names_it(self, host, capsys):
        host.pid_after_boot = _OLD_PID

        cli_server._restart(_PORT)

        host.assert_not_stopped_or_spawned()
        out, _err = capsys.readouterr()
        assert (
            f"✅ Restarted the gateway in place (pid {_OLD_PID}). Logs: kirocrew logs -f"
            in out.splitlines()
        )
        assert [a["resources"] for a in host.audits()] == [
            f"port={_PORT} via=in_place pid={_OLD_PID}"
        ]

    def test_a_ready_answer_under_the_old_secret_is_the_old_gateway(self, host):
        host.boot_after_reads = 6

        cli_server._restart(_PORT)

        # Five polls saw 200 with the old secret and kept waiting; the sixth saw the new one.
        assert host.reads == 7
        assert host.clock.t == pytest.approx(5 * cli_server._RESTART_READY_POLL_INTERVAL)

    def test_a_new_secret_before_the_gateway_is_ready_keeps_waiting(self, host):
        host.boot_after_reads = 1
        host.ready = [503, 503, 200]

        cli_server._restart(_PORT)

        assert host.clock.t == pytest.approx(2 * cli_server._RESTART_READY_POLL_INTERVAL)
        host.assert_not_stopped_or_spawned()

    def test_a_ready_answer_from_a_listener_the_gateway_does_not_hold_is_not_success(
        self, host, capsys
    ):
        # The new gateway wrote its secret and died; something else answers the port.
        host.owns_port = False

        with pytest.raises(SystemExit) as exc:
            cli_server._restart(_PORT)

        assert exc.value.code == 1
        assert _SUCCESS not in capsys.readouterr().out
        host.assert_not_stopped_or_spawned()

    def test_without_a_listener_lookup_the_fresh_secret_and_ready_answer_suffice(
        self, host, capsys
    ):
        host.lookup = False
        host.owns_port = False

        cli_server._restart(_PORT)

        assert _SUCCESS in capsys.readouterr().out.splitlines()
        host.assert_not_stopped_or_spawned()

    def test_a_gateway_that_does_not_come_back_is_reported_and_never_replaced(self, host, capsys):
        host.boot_after_reads = None

        with pytest.raises(SystemExit) as exc:
            cli_server._restart(_PORT)

        assert exc.value.code == 1
        host.assert_not_stopped_or_spawned()
        out = capsys.readouterr().out
        assert (
            f"❌ The gateway on port {_PORT} accepted the restart but did not come back "
            f"within {_BUDGET}s."
        ) in out
        assert "kirocrew logs -f" in out and "kirocrew status" in out
        assert "Still starting" in out
        (audit,) = host.audits()
        assert audit["outcome"] == "denied"
        assert audit["resources"] == (
            f"port={_PORT} via=in_place pid={_OLD_PID} reason=in_place_not_back_within={_BUDGET}s"
        )

    def test_a_marker_with_no_pid_leaves_the_pid_out_of_the_line_and_the_audit(
        self, host, capsys, monkeypatch
    ):
        monkeypatch.setattr(run_marker, "read_pid", lambda port: None)

        cli_server._restart(_PORT)

        assert "✅ Restarted the gateway in place. Logs: kirocrew logs -f" in (
            capsys.readouterr().out.splitlines()
        )
        (audit,) = host.audits()
        assert audit["resources"] == f"port={_PORT} via=in_place"
        host.assert_not_stopped_or_spawned()

    def test_a_timeout_with_no_pid_leaves_the_pid_out_of_the_audit(self, host, monkeypatch):
        host.boot_after_reads = None
        monkeypatch.setattr(run_marker, "read_pid", lambda port: None)

        with pytest.raises(SystemExit):
            cli_server._restart(_PORT)

        (audit,) = host.audits()
        assert audit["resources"] == (
            f"port={_PORT} via=in_place reason=in_place_not_back_within={_BUDGET}s"
        )
        host.assert_not_stopped_or_spawned()

    def test_a_boot_time_update_apply_extends_the_wait(self, host, capsys):
        host.boot_after_reads = None
        host.applying = host.clock.wall()

        def boot_late(secs: float) -> None:
            host.clock.t += secs
            if host.clock.t > _BUDGET + 100:
                host.boot_after_reads = 0

        host.on_sleep = boot_late

        cli_server._restart(_PORT)

        out = capsys.readouterr().out
        assert "applying an update" in out
        assert _SUCCESS in out.splitlines()
        host.assert_not_stopped_or_spawned()

    def test_a_wait_the_apply_extended_names_the_seconds_it_waited(self, host, capsys):
        host.boot_after_reads = None
        host.applying = host.clock.wall()
        bound = cli_server.update_next_start.APPLY_BOUND_SECS

        def slow_poll(secs: float) -> None:
            # Each pass costs more than the poll interval (a slow secret read, say),
            # so the wait ends past the extended deadline, not on it.
            host.clock.t += secs + 2.2
            if host.clock.t > bound:
                host.applying = None

        host.on_sleep = slow_poll

        with pytest.raises(SystemExit) as exc:
            cli_server._restart(_PORT)

        assert exc.value.code == 1
        waited = int(host.clock.t)
        extended = int(bound + _READY_TIMEOUT)
        assert waited != extended
        out = capsys.readouterr().out
        assert f"did not come back within {waited}s." in out
        (audit,) = host.audits()
        assert audit["resources"].endswith(f"reason=in_place_not_back_within={waited}s")
        host.assert_not_stopped_or_spawned()

    def test_the_readiness_window_still_follows_the_apply_bound(self, host, capsys):
        host.boot_after_reads = None
        host.applying = host.clock.wall()
        bound = cli_server.update_next_start.APPLY_BOUND_SECS

        def boot_after_the_apply(secs: float) -> None:
            host.clock.t += secs
            # The new gateway serves inside the readiness window after the apply bound.
            if host.clock.t > bound + _READY_TIMEOUT / 2:
                host.boot_after_reads = 0

        host.on_sleep = boot_after_the_apply

        cli_server._restart(_PORT)

        assert _SUCCESS in capsys.readouterr().out.splitlines()
        host.assert_not_stopped_or_spawned()

    def test_a_headless_gateway_prints_its_api_access(self, host):
        host.headless = [True, True]

        cli_server._restart(_PORT)

        host.client_access.assert_called_once_with(_PORT)
        host.token_url.assert_not_called()
        host.assert_not_stopped_or_spawned()

    def test_the_new_generations_headless_mode_wins_over_an_unknown_one_before(self, host):
        host.headless = [None, True]

        cli_server._restart(_PORT)

        host.client_access.assert_called_once_with(_PORT)
        host.token_url.assert_not_called()
        host.assert_not_stopped_or_spawned()

    def test_the_new_generations_ordinary_mode_wins_over_a_headless_one_before(self, host):
        host.headless = [True, False]

        cli_server._restart(_PORT)

        host.token_url.assert_called_once_with(_PORT)
        host.client_access.assert_not_called()
        host.assert_not_stopped_or_spawned()

    def test_the_mode_read_before_stands_when_the_new_one_cannot_be_read(self, host, capsys):
        host.headless = [True, None]

        cli_server._restart(_PORT)

        host.client_access.assert_called_once_with(_PORT)
        assert _WARNING not in capsys.readouterr().err
        host.assert_not_stopped_or_spawned()

    def test_an_unreadable_mode_warns_nothing_when_the_gateway_restarts_itself(self, host, capsys):
        host.headless = [None, None]

        cli_server._restart(_PORT)

        assert _WARNING not in capsys.readouterr().err
        host.token_url.assert_called_once_with(_PORT)


class TestFallback:
    @pytest.mark.parametrize(
        "answer",
        [
            _http_error(400, {"code": "invalid_body"}),
            _http_error(401),
            _http_error(403, {"code": "restart_caller_not_cli"}),
            _http_error(
                403,
                {
                    "error": "this gateway is not run by a supervisor, so it does not "
                    "restart in place",
                    "code": "restart_in_place_unsupervised",
                },
            ),
            _http_error(404),
            _http_error(405),
            _http_error(503, {"code": "restart_gate_unavailable"}),
            _http_error(500),
            urllib.error.URLError(ConnectionRefusedError(111, "refused")),
            urllib.error.URLError(socket.timeout("timed out")),
            ConnectionResetError(104, "reset"),
            TimeoutError("timed out"),
            cli_server._GatewayPeerRefused("peer is not this install's gateway"),
            _Resp(b"not json"),
            _Resp(b'{"ok":true,"token":"x"}'),
            _Resp(b"[]"),
        ],
        ids=[
            "400",
            "401",
            "403",
            "403-unsupervised",
            "404",
            "405",
            "503",
            "500",
            "refused",
            "url-timeout",
            "reset",
            "timeout",
            "peer-refused",
            "unreadable",
            "other-answer",
            "not-an-object",
        ],
    )
    def test_a_gateway_that_cannot_restart_in_place_is_stopped_and_spawned(
        self, host, capsys, answer
    ):
        host.answer = answer

        cli_server._restart(_PORT)

        host.assert_stopped_and_spawned()
        assert len(host.sent) == 1
        out = capsys.readouterr().out
        assert "Started detached gateway" in out and "in place" not in out

    def test_a_refusal_names_the_gateways_reason_then_falls_back(self, host, capsys):
        host.answer = _http_error(
            409,
            {"ok": False, "error": "Restart skipped: the gateway is shutting down.", "code": "x"},
        )

        cli_server._restart(_PORT)

        host.assert_stopped_and_spawned()
        out = capsys.readouterr().out
        assert (
            "ℹ️ The gateway did not restart in place (Restart skipped: the gateway is "
            "shutting down); stopping it and starting a new one."
        ) in out.splitlines()

    def test_a_command_the_gateway_started_stops_and_spawns_without_asking(self, host):
        # Dev Fleet's foreground restart runs this command as the gateway's child.
        host.parent = _OLD_PID

        cli_server._restart(_PORT)

        assert host.sent == []
        host.assert_stopped_and_spawned()

    def test_no_secret_sends_nothing_and_falls_back(self, host):
        host.secret = ""
        host.boot_after_reads = None

        cli_server._restart(_PORT)

        assert host.sent == [] and host.reads == 1
        host.assert_stopped_and_spawned()

    def test_windows_keeps_stop_and_spawn_and_sends_nothing(self, host, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)

        from kiro_crew import cli_restart_in_place

        assert cli_restart_in_place.restart_in_place(_PORT, headless=False) is False

        assert host.sent == [] and host.reads == 0

    def test_the_unreadable_mode_warning_comes_with_the_fallback(self, host, capsys):
        host.headless = [None]
        host.answer = _http_error(404)

        cli_server._restart(_PORT)

        assert _WARNING in capsys.readouterr().err
        assert len(host.sent) == 1
        host.assert_stopped_and_spawned()


class TestWhenIdle:
    """``--when-idle`` commits, then the in-place restart takes over the gateway's hold."""

    @pytest.fixture
    def asks(self, monkeypatch, host):
        seen: list[dict] = []

        def ask(port, *, service_only=False, commit=False, release=False):
            seen.append({"commit": commit, "release": release})
            return idle_cli._Verdict("idle")

        monkeypatch.setattr(idle_cli, "_ask_gate", ask)
        return seen

    def test_a_committed_idle_answer_is_followed_by_the_in_place_restart(self, host, asks):
        idle_cli._restart_when_idle(_PORT, max_wait_secs=0)

        assert asks == [
            {"commit": False, "release": False},
            {"commit": True, "release": False},
        ]
        assert json.loads(host.sent[0][0].data) == {"restart": True}
        host.assert_not_stopped_or_spawned()

    def test_an_accepted_restart_that_does_not_come_back_releases_the_hold(self, host, asks):
        host.boot_after_reads = None

        with pytest.raises(SystemExit):
            idle_cli._restart_when_idle(_PORT, max_wait_secs=0)

        assert asks[-1] == {"commit": False, "release": True}
        host.assert_not_stopped_or_spawned()
