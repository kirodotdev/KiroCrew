"""``kirocrew restart --when-idle`` and the gateway idle check it asks.

A Remote Crew "Update" or "Restart" must not stop the crew's running turns and
subagents. The remote job runs ``restart --when-idle``, which asks the crew's own gateway
(``POST /api/restart/when-idle`` -> ``GatewayOrchestrator._restart_gate_when_idle``)
whether a restart would interrupt work, using the census the gateway's own
scheduled update waits on. These tests pin the gate, its route and the CLI.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import cli_restart_idle as idle_cli
from kiro_crew.dashboard.handlers import restart_gate
from kiro_crew.slack import gateway


class _Sessions:
    """The admission half of ``SessionManager`` the gate drives."""

    def __init__(self, *, closed: bool = False) -> None:
        self.admission_closed = closed
        self.update_restart_fenced = False
        self.paused = 0
        self.resumed = 0

    async def pause_turn_admission_for_update(self) -> bool:
        self.paused += 1
        self.admission_closed = True
        return True

    async def resume_turn_admission_after_update(self) -> bool:
        self.resumed += 1
        self.admission_closed = False
        return True


def _orchestrator(counts: tuple[int, int], sessions: _Sessions | None) -> object:
    orch = object.__new__(gateway.GatewayOrchestrator)
    orch.sessions = sessions
    orch._background_tasks = set()
    orch._restart_gate_hold = None
    orch._in_flight_work_counts = lambda: counts
    orch._schedule_inbound_replay = MagicMock()
    return orch


@pytest.fixture(autouse=True)
def _no_shutdown():
    gateway.shutdown_event.clear()
    yield
    gateway.shutdown_event.clear()


@pytest.mark.asyncio
class TestGate:
    async def test_busy_reopens_admission_and_reports_the_counts(self):
        sessions = _Sessions()
        orch = _orchestrator((2, 3), sessions)

        assert await orch._restart_gate_when_idle() == (False, 2, 3)

        assert (sessions.paused, sessions.resumed) == (1, 1)
        assert sessions.admission_closed is False
        orch._schedule_inbound_replay.assert_called_once_with()
        assert orch._restart_gate_hold is None

    async def test_idle_keeps_admission_paused_for_the_restart(self):
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)

        assert await orch._restart_gate_when_idle() == (True, 0, 0)

        assert (sessions.paused, sessions.resumed) == (1, 0)
        assert sessions.admission_closed is True
        assert orch._restart_gate_hold in orch._background_tasks
        orch._restart_gate_hold.cancel()

    async def test_a_restart_that_never_comes_reopens_admission(self):
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)
        orch._RESTART_GATE_HOLD_SECS = 0.0

        await orch._restart_gate_when_idle()
        await orch._restart_gate_hold

        assert sessions.resumed == 1
        assert sessions.admission_closed is False

    async def test_a_hold_an_update_restart_fenced_is_left_alone(self):
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)
        orch._RESTART_GATE_HOLD_SECS = 0.0

        await orch._restart_gate_when_idle()
        sessions.update_restart_fenced = True
        await orch._restart_gate_hold

        assert sessions.resumed == 0

    async def test_asking_again_while_a_restart_is_approved_is_busy_and_keeps_the_hold(self):
        sessions = _Sessions()
        counts = [(0, 0)]
        orch = _orchestrator((0, 0), sessions)
        orch._in_flight_work_counts = lambda: counts[0]
        await orch._restart_gate_when_idle()
        first = orch._restart_gate_hold
        # A turn queued after the first answer: a second ask sees it.
        counts[0] = (1, 0)

        assert await orch._restart_gate_when_idle() == (False, 1, 0)

        # The approval stands: same hold, admission still paused, nothing reopened.
        assert orch._restart_gate_hold is first and not first.done()
        assert sessions.admission_closed is True and sessions.resumed == 0
        assert sessions.paused == 1
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    async def test_an_update_apply_waits_out_a_restart_the_gate_approved(self):
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)
        orch._mandatory_update_deferred_at = None
        orch._mandatory_update_deferred_key = None
        orch._update_apply_deferred = False
        await orch._restart_gate_when_idle()
        hold = orch._restart_gate_hold

        assert await orch._prepare_auto_update_apply(mandatory=False) is False

        # Deferred without releasing the gate's pause or its hold.
        assert orch._update_apply_deferred is True
        assert orch._restart_gate_hold is hold and not hold.done()
        assert sessions.resumed == 0 and sessions.admission_closed is True
        assert orch._update_pause_holder == "restart_gate"
        hold.cancel()
        await asyncio.gather(hold, return_exceptions=True)

    async def test_a_gate_ask_that_queues_behind_an_apply_answers_busy(self):
        """The gate read admission open, then the apply took the pause first."""
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)
        orch._mandatory_update_deferred_at = None
        orch._mandatory_update_deferred_key = None
        real_pause = sessions.pause_turn_admission_for_update

        async def _apply_wins_the_lock():
            sessions.pause_turn_admission_for_update = real_pause
            assert await orch._prepare_auto_update_apply(mandatory=False) is True
            return await real_pause()

        sessions.pause_turn_admission_for_update = _apply_wins_the_lock

        assert await orch._restart_gate_when_idle() == (False, 0, 0)

        # The apply's pause is left alone and no hold was started.
        assert sessions.resumed == 0 and sessions.admission_closed is True
        assert orch._update_pause_holder == "auto_update"
        assert orch._restart_gate_hold is None

    async def test_an_apply_that_queues_behind_the_gate_defers(self):
        """The apply waited for the lock while the gate paused and answered idle."""
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)
        orch._mandatory_update_deferred_at = None
        orch._mandatory_update_deferred_key = None
        real_pause = sessions.pause_turn_admission_for_update
        verdicts = []

        async def _gate_wins_the_lock():
            sessions.pause_turn_admission_for_update = real_pause
            verdicts.append(await orch._restart_gate_when_idle())
            return await real_pause()

        sessions.pause_turn_admission_for_update = _gate_wins_the_lock

        assert await orch._prepare_auto_update_apply(mandatory=False) is False

        assert verdicts == [(True, 0, 0)]
        assert orch._update_pause_holder == "restart_gate"
        assert sessions.resumed == 0
        orch._restart_gate_hold.cancel()
        await asyncio.gather(orch._restart_gate_hold, return_exceptions=True)

    async def test_an_update_apply_runs_once_the_hold_is_over(self):
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)
        orch._mandatory_update_deferred_at = None
        orch._mandatory_update_deferred_key = None
        orch._RESTART_GATE_HOLD_SECS = 0.0
        await orch._restart_gate_when_idle()
        await orch._restart_gate_hold

        assert await orch._prepare_auto_update_apply(mandatory=False) is True

    async def test_a_pause_someone_else_holds_is_busy_and_untouched(self):
        sessions = _Sessions(closed=True)
        orch = _orchestrator((0, 1), sessions)

        assert await orch._restart_gate_when_idle() == (False, 0, 1)

        assert (sessions.paused, sessions.resumed) == (0, 0)
        assert sessions.admission_closed is True

    async def test_a_stop_under_way_is_busy(self):
        sessions = _Sessions()
        orch = _orchestrator((0, 0), sessions)
        gateway.shutdown_event.set()

        assert await orch._restart_gate_when_idle() == (False, 0, 0)
        assert sessions.paused == 0


def _request(gate, *, remote: str = "127.0.0.1", internal: bool = True) -> MagicMock:
    req = MagicMock()
    req.remote = remote
    req.transport = None
    # A bare MagicMock.get is truthy and would pass the auth-mark check vacuously.
    req.get = ({"internal_auth": True} if internal else {"user": "member"}).get
    req.app = {"state": SimpleNamespace(_restart_idle_gate=gate)}
    return req


@pytest.mark.asyncio
class TestRoute:
    @pytest.mark.parametrize("managed", [True, False])
    async def test_answers_the_gates_verdict(self, monkeypatch, managed):
        async def _gate():
            return False, 1, 2

        monkeypatch.setattr(restart_gate, "launched_as_managed_service", lambda: managed)

        resp = await restart_gate.api_restart_when_idle(_request(_gate))

        assert resp.status == 200
        assert json.loads(resp.body) == {
            "idle": False,
            "turns": 1,
            "background": 2,
            "service_managed": managed,
        }

    async def test_a_caller_off_the_host_is_refused_before_the_gate(self):
        async def _gate():
            raise AssertionError("the gate must not run for a remote caller")

        resp = await restart_gate.api_restart_when_idle(
            _request(_gate, remote="10.0.0.9", internal=False)
        )

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restart_gate_not_local"

    async def test_a_loopback_cookie_caller_without_the_secret_is_refused(self):
        async def _gate():
            raise AssertionError("the gate must not run without the internal secret")

        resp = await restart_gate.api_restart_when_idle(_request(_gate, internal=False))

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restart_gate_not_local"

    async def test_no_orchestrator_is_unavailable(self):
        resp = await restart_gate.api_restart_when_idle(_request(None))

        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "restart_gate_unavailable"


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS

    assert "/api/restart/when-idle" in _STRICT_INTERNAL_API_PATHS


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


@pytest.fixture
def cli(monkeypatch):
    """Drive ``_restart_when_idle`` with scripted gate answers and a fake clock."""
    from kiro_crew import cli_server

    clock = _Clock()
    answers: list[idle_cli._Verdict] = []
    restarts: list[int | None] = []
    monkeypatch.setattr(idle_cli, "_now", lambda: clock.now)
    monkeypatch.setattr(idle_cli, "_sleep", clock.sleep)
    monkeypatch.setattr(idle_cli, "resolve_client_port", lambda port: port or 5476)
    monkeypatch.setattr(idle_cli, "_ask_gate", lambda port: answers.pop(0))
    # No probe of the host's real service manager from a unit test.
    monkeypatch.setattr(idle_cli, "_service_scopes_running", lambda: 0)
    monkeypatch.setattr(idle_cli, "_service_active", lambda: False)
    monkeypatch.setattr(cli_server, "_restart", lambda port: restarts.append(port))
    return SimpleNamespace(clock=clock, answers=answers, restarts=restarts)


class TestCli:
    def test_waits_while_busy_and_restarts_once_idle(self, cli):
        cli.answers.extend([idle_cli._Verdict("busy", 1, 0), idle_cli._Verdict("idle")])

        idle_cli._restart_when_idle(None)

        assert cli.restarts == [None]
        assert cli.clock.sleeps == [idle_cli.POLL_SECS]

    def test_restarts_at_once_when_idle(self, cli):
        cli.answers.append(idle_cli._Verdict("idle"))

        idle_cli._restart_when_idle(7000, max_wait_secs=0)

        assert cli.restarts == [7000]
        assert cli.clock.sleeps == []

    def test_a_gateway_that_is_not_running_is_restarted(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: False)
        cli.answers.append(idle_cli._Verdict("down"))

        idle_cli._restart_when_idle(None)

        assert cli.restarts == [None]

    def test_a_refused_port_beside_a_running_service_is_not_restarted(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("down"))

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_REFUSED
        assert cli.restarts == []

    def test_an_idle_gateway_the_service_did_not_launch_is_not_restarted(self, cli, monkeypatch):
        """Another home's idle foreground gateway says nothing about the busy service."""
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("idle", service_managed=False))

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_REFUSED
        assert cli.restarts == []

    def test_the_idle_service_gateway_is_restarted(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("idle", service_managed=True))

        idle_cli._restart_when_idle(None)

        assert cli.restarts == [None]

    def test_an_explicit_port_needs_no_service_proof(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("idle", service_managed=False))

        idle_cli._restart_when_idle(7004)

        assert cli.restarts == [7004]

    def test_units_in_both_scopes_are_refused_before_any_check(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_scopes_running", lambda: 2)

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_REFUSED
        # No answer was scripted, so asking the gate would have raised IndexError:
        # nothing was asked, and no gateway's admission was paused.
        assert cli.restarts == []

    def test_an_explicit_port_skips_the_scope_refusal(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_scopes_running", lambda: 2)
        cli.answers.append(idle_cli._Verdict("idle"))

        idle_cli._restart_when_idle(7003)

        assert cli.restarts == [7003]

    def test_an_explicit_port_that_refuses_is_restarted(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("down"))

        idle_cli._restart_when_idle(7002)

        assert cli.restarts == [7002]

    def test_the_busy_marker_is_the_last_stdout_line(self, cli, capsys):
        cli.answers.append(idle_cli._Verdict("busy", 3, 4))

        with pytest.raises(SystemExit):
            idle_cli._restart_when_idle(None, max_wait_secs=0)

        out = capsys.readouterr()
        marker = f"{idle_cli.BUSY_MARKER} turns=3 background=4"
        assert out.out.rstrip("\n").splitlines()[-1] == marker
        assert marker in out.err

    def test_still_busy_at_the_ceiling_exits_busy_without_restarting(self, cli, capsys):
        cli.answers.extend([idle_cli._Verdict("busy", 2, 5)] * 3)

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None, max_wait_secs=15)

        assert exc.value.code == idle_cli.EXIT_BUSY
        assert cli.restarts == []
        assert cli.clock.sleeps == [10.0, 5.0]
        assert f"{idle_cli.BUSY_MARKER} turns=2 background=5" in capsys.readouterr().err

    def test_one_check_with_no_wait(self, cli):
        cli.answers.append(idle_cli._Verdict("busy", 0, 1))

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None, max_wait_secs=0)

        assert exc.value.code == idle_cli.EXIT_BUSY
        assert cli.clock.sleeps == []

    def test_a_gateway_without_the_check_is_not_restarted(self, cli, capsys):
        cli.answers.append(idle_cli._Verdict("unsupported"))

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_UNSUPPORTED
        assert cli.restarts == []
        assert idle_cli.UNSUPPORTED_MARKER in capsys.readouterr().err

    def test_a_failed_check_is_not_restarted(self, cli, monkeypatch):
        def _fail(port):
            raise idle_cli._GateFailed("no answer")

        monkeypatch.setattr(idle_cli, "_ask_gate", _fail)

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_REFUSED
        assert cli.restarts == []

    def test_the_default_wait_is_the_gateways_mandatory_deferral_ceiling(self):
        assert (
            idle_cli.DEFAULT_MAX_WAIT_SECS
            == gateway.GatewayOrchestrator._MANDATORY_UPDATE_MAX_DEFER_SECS
        )


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status: int, body: dict | None = None) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode() if body is not None else b""
    return urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(raw))  # type: ignore[arg-type]


class TestAskGate:
    @pytest.fixture(autouse=True)
    def _no_secret(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(idle_cli, "read_local_secret", lambda *a, **k: "")

    def _answer(self, monkeypatch, outcome):
        def _open(req, port, timeout, *, operation):
            assert req.full_url.endswith("/api/restart/when-idle")
            if isinstance(outcome, BaseException):
                raise outcome
            return _Resp(json.dumps(outcome).encode())

        monkeypatch.setattr(idle_cli, "_owner_verified_secret_urlopen", _open)

    def test_reads_idle_and_busy(self, monkeypatch):
        self._answer(monkeypatch, {"idle": True, "turns": 0, "background": 0})
        assert idle_cli._ask_gate(5476).state == "idle"
        self._answer(monkeypatch, {"idle": False, "turns": 1, "background": 4})
        assert idle_cli._ask_gate(5476) == idle_cli._Verdict("busy", 1, 4)
        self._answer(
            monkeypatch, {"idle": True, "turns": 0, "background": 0, "service_managed": True}
        )
        assert idle_cli._ask_gate(5476).service_managed is True

    @pytest.mark.parametrize("status", [401, 403, 404, 405, 503])
    def test_an_older_gateway_is_unsupported(self, monkeypatch, status):
        self._answer(monkeypatch, _http_error(status, {"error": "Token required"}))
        assert idle_cli._ask_gate(5476).state == "unsupported"

    def test_the_gates_own_refusal_fails(self, monkeypatch):
        self._answer(monkeypatch, _http_error(403, {"code": "restart_gate_not_local"}))
        with pytest.raises(idle_cli._GateFailed):
            idle_cli._ask_gate(5476)

    def test_nothing_listening_is_down(self, monkeypatch):
        self._answer(monkeypatch, urllib.error.URLError(ConnectionRefusedError()))
        assert idle_cli._ask_gate(5476).state == "down"

    @pytest.mark.parametrize(
        "error",
        [urllib.error.URLError(ConnectionResetError()), ConnectionResetError(), BrokenPipeError()],
    )
    def test_a_reset_from_a_live_gateway_is_never_read_as_down(self, monkeypatch, error):
        self._answer(monkeypatch, error)
        with pytest.raises(idle_cli._GateFailed):
            idle_cli._ask_gate(5476)

    def test_a_gateway_that_does_not_answer_fails(self, monkeypatch):
        self._answer(monkeypatch, urllib.error.URLError(TimeoutError()))
        with pytest.raises(idle_cli._GateFailed):
            idle_cli._ask_gate(5476)

    def test_an_answer_without_counts_fails(self, monkeypatch):
        self._answer(monkeypatch, {"idle": True})
        with pytest.raises(idle_cli._GateFailed):
            idle_cli._ask_gate(5476)


class TestCommandLine:
    def test_when_idle_dispatches_to_the_idle_restart(self, monkeypatch):
        from kiro_crew import cli, cli_server

        seen = {}
        monkeypatch.setattr(
            idle_cli,
            "_restart_when_idle",
            lambda port, *, max_wait_secs: seen.update(port=port, wait=max_wait_secs),
        )
        monkeypatch.setattr(
            cli_server, "_restart", lambda port: pytest.fail("plain restart must not run")
        )
        monkeypatch.setattr("sys.argv", ["kirocrew", "restart", "--when-idle", "--max-wait", "0"])

        cli.main()

        assert seen == {"port": None, "wait": 0.0}

    def test_a_plain_restart_is_unchanged(self, monkeypatch):
        from kiro_crew import cli, cli_server

        seen = []
        monkeypatch.setattr(cli_server, "_restart", lambda port: seen.append(port))
        monkeypatch.setattr(
            idle_cli, "_restart_when_idle", lambda *a, **k: pytest.fail("no idle check")
        )
        monkeypatch.setattr("sys.argv", ["kirocrew", "restart", "--port", "7001"])

        cli.main()

        assert seen == [7001]

    def test_max_wait_without_when_idle_is_refused(self, monkeypatch):
        from kiro_crew import cli, cli_server

        monkeypatch.setattr(cli_server, "_restart", lambda port: pytest.fail("must not restart"))
        monkeypatch.setattr("sys.argv", ["kirocrew", "restart", "--max-wait", "5"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 2


def test_the_dashboard_registers_the_route():
    from aiohttp import web

    from kiro_crew.dashboard.routes import system

    app = web.Application()
    system.register(app)

    handlers = {
        (r.method, r.resource.canonical): r.handler
        for r in app.router.routes()
        if r.resource is not None
    }
    assert handlers[("POST", "/api/restart/when-idle")] is restart_gate.api_restart_when_idle


@pytest.mark.asyncio
class TestInstancesRestartRoute:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path):
        self.home = tmp_path

    async def _call(self, monkeypatch, result, body=None):
        from instances_helpers import _enable, _FakeReq, _State

        from kiro_crew.dashboard import handlers_instances as hi

        seen = {}

        class _Mgr:
            async def restart_remote(self, iid, *, force=False):
                seen["force"] = force
                return result

        class _Reg:
            def get(self, iid):
                return object()

        _enable(self.home, monkeypatch)
        req = _FakeReq(_State(_Reg(), _Mgr()), match={"id": "box"}, body=body)
        return await hi.api_instances_restart(req), seen

    async def test_a_busy_crew_is_409_with_its_counts(self, monkeypatch):
        resp, seen = await self._call(
            monkeypatch,
            {"ok": False, "code": "busy", "turns": 1, "background": 2, "message": "busy"},
        )

        assert resp.status == 409
        body = json.loads(resp.body)
        assert (body["code"], body["turns"], body["background"]) == ("busy", 1, 2)
        assert seen == {"force": False}

    async def test_an_old_crew_is_409(self, monkeypatch):
        resp, _ = await self._call(
            monkeypatch, {"ok": False, "code": "peer_cannot_wait", "message": "old"}
        )

        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "peer_cannot_wait"

    async def test_force_reaches_the_manager(self, monkeypatch):
        resp, seen = await self._call(
            monkeypatch, {"ok": True, "message": "requested"}, body={"force": True}
        )

        assert resp.status == 200
        assert seen == {"force": True}

    async def test_a_malformed_body_is_400(self, monkeypatch):
        resp, seen = await self._call(monkeypatch, {"ok": True}, body={"force": "now"})

        assert resp.status == 400
        assert seen == {}


def test_the_scope_count_is_the_scopes_a_service_restart_restarts(monkeypatch):
    from kiro_crew.service import common, linux

    monkeypatch.setattr(linux, "_running_scopes", lambda: [False, True])
    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.SYSTEMD)
    assert linux.running_scope_count() == 2
    assert idle_cli._service_scopes_running() == 2

    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.LAUNCHD)
    assert idle_cli._service_scopes_running() == 0


@pytest.mark.parametrize(
    ("platform", "linux_active", "mac_loaded", "expected"),
    [
        ("SYSTEMD", True, False, True),
        ("SYSTEMD", False, True, False),
        # From SSH, ``launchctl list`` misses a gui/<uid> agent that is loaded:
        # the guard must see what ``restart_service`` will restart.
        ("LAUNCHD", False, True, True),
        ("LAUNCHD", True, False, False),
        ("UNSUPPORTED", True, True, False),
    ],
)
def test_the_service_check_is_the_one_restart_service_gates_on(
    monkeypatch, platform, linux_active, mac_loaded, expected
):
    from kiro_crew.service import common, linux, macos

    plat = getattr(common.Platform, platform, None) or object()
    monkeypatch.setattr(common, "current_platform", lambda: plat)
    monkeypatch.setattr(linux, "is_active", lambda: linux_active)
    monkeypatch.setattr(macos, "is_loaded", lambda: mac_loaded)
    monkeypatch.setattr(macos, "is_active", lambda: not mac_loaded)

    assert idle_cli._service_active() is expected
