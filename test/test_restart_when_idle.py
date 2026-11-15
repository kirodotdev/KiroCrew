"""``kirocrew restart --when-idle`` and the gateway idle check it asks.

A Remote Crew "Update" or "Restart" must not stop the crew's running turns and
subagents. The remote job runs ``restart --when-idle``, which asks the crew's own gateway
(``POST /api/restart/when-idle``) whether a restart would interrupt work. The route
READS the one quiescence census: no pause, no hold, no restart. These tests pin the
route and the CLI.
"""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_restart_idle as idle_cli
from kiro_crew import cli_server, cli_server_stop, quiescence, restart_admission
from kiro_crew.dashboard.handlers import restart_gate
from kiro_crew.service import controller as service_controller
from kiro_crew.slack import gateway


@pytest.fixture(autouse=True)
def _no_shutdown():
    gateway.shutdown_event.clear()
    yield
    gateway.shutdown_event.clear()


def _census(**counts) -> quiescence.Quiescence:
    return quiescence.Quiescence(**counts)


def _request(
    probe, *, remote: str = "127.0.0.1", internal: bool = True, body: object = None
) -> MagicMock:
    req = MagicMock()
    req.remote = remote
    req.transport = None
    req.can_read_body = body is not None
    req.json = AsyncMock(return_value=body)
    # A bare MagicMock.get is truthy and would pass the auth-mark check vacuously.
    req.get = ({"internal_auth": True} if internal else {"user": "member"}).get
    req.app = {"state": SimpleNamespace(_quiescence_probe=probe)}
    return req


def _no_identity_ask() -> str:
    raise AssertionError(
        "the service manager is asked only for a service-only ask without the marker"
    )


@pytest.mark.asyncio
class TestRoute:
    """The route READS the census: it pauses nothing, holds nothing and restarts nothing."""

    async def test_an_idle_gateway_answers_idle_and_pauses_nothing(self):
        probe = MagicMock(return_value=_census())

        resp = await restart_gate.api_restart_when_idle(_request(probe))

        assert resp.status == 200
        body = json.loads(resp.body)
        assert (body["idle"], body["turns"], body["background"]) == (True, 0, 0)
        probe.assert_called_once_with(quiet_window_secs=0.0)

    async def test_a_settle_after_busy_work_does_not_refuse_an_idle_gateway(self):
        probe = MagicMock(return_value=_census(settle_remaining_secs=45.0))

        resp = await restart_gate.api_restart_when_idle(_request(probe))

        body = json.loads(resp.body)
        assert (body["idle"], body["turns"], body["background"]) == (True, 0, 0)

    async def test_a_busy_gateway_answers_the_counts_with_queued_work_folded_in(self):
        probe = MagicMock(
            return_value=_census(turns=2, background=1, queued_messages=3, pending_approvals=1)
        )

        resp = await restart_gate.api_restart_when_idle(_request(probe))

        body = json.loads(resp.body)
        assert (body["idle"], body["turns"], body["background"]) == (False, 2, 5)

    async def test_a_commit_ask_takes_the_restart_admission_unforced(self, monkeypatch):
        probe = MagicMock(return_value=_census())
        admit = AsyncMock(return_value=restart_admission.RestartAdmission(True, "", _census()))
        req = _request(probe, body={"commit": True})
        req.app["state"]._restart_admission = admit
        req.app["state"]._restart_release = AsyncMock()
        try:
            resp = await restart_gate.api_restart_when_idle(req)

            assert json.loads(resp.body)["idle"] is True
            admit.assert_awaited_once_with(force=False)
            probe.assert_not_called()
            # The hold it armed lets go on release, and only then.
            released = await restart_gate.api_restart_when_idle(
                _request(probe, body={"release": True})
            )
            assert json.loads(released.body)["released"] is False
            req.app["state"]._restart_release.assert_not_awaited()
            req2 = _request(probe, body={"release": True})
            req2.app["state"] = req.app["state"]
            released = await restart_gate.api_restart_when_idle(req2)
            assert json.loads(released.body)["released"] is True
            req.app["state"]._restart_release.assert_awaited_once()
        finally:
            restart_gate._cancel_commit_hold(req.app["state"])

    async def test_a_commit_ask_refused_for_work_answers_busy_with_the_counts(self):
        probe = MagicMock(return_value=_census())
        census = _census(turns=2, background=1)
        admit = AsyncMock(
            return_value=restart_admission.RestartAdmission(
                False, restart_admission.REASON_BUSY, census
            )
        )
        req = _request(probe, body={"commit": True})
        req.app["state"]._restart_admission = admit

        resp = await restart_gate.api_restart_when_idle(req)

        body = json.loads(resp.body)
        assert (body["idle"], body["turns"], body["background"]) == (False, 2, 1)
        assert getattr(req.app["state"], "_restart_commit_hold", None) is None

    async def test_a_commit_ask_without_an_admission_seam_is_503(self):
        probe = MagicMock(return_value=_census())

        resp = await restart_gate.api_restart_when_idle(_request(probe, body={"commit": True}))

        assert resp.status == 503

    async def test_a_recent_chat_with_no_running_work_reads_idle_for_the_explicit_ask(self):
        probe = MagicMock(return_value=_census(since_input_secs=10.0))

        resp = await restart_gate.api_restart_when_idle(_request(probe))

        assert json.loads(resp.body)["idle"] is True

    async def test_running_work_holds_the_ask_even_after_a_long_quiet_spell(self):
        probe = MagicMock(return_value=_census(turns=1, since_input_secs=3600.0))

        resp = await restart_gate.api_restart_when_idle(_request(probe))

        body = json.loads(resp.body)
        assert (body["idle"], body["turns"]) == (False, 1)

    async def test_a_service_only_ask_is_answered_without_the_census_off_the_service(
        self, monkeypatch
    ):
        def _gate(**_kw):
            raise AssertionError("no census for a restart that goes elsewhere")

        monkeypatch.setattr(restart_gate, "launched_as_managed_service", lambda: False)
        monkeypatch.setattr(restart_gate, "service_identity", lambda: "other")

        resp = await restart_gate.api_restart_when_idle(
            _request(_gate, body={"service_managed_only": True})
        )

        assert resp.status == 200
        assert json.loads(resp.body) == {
            "idle": False,
            "turns": 0,
            "background": 0,
            "service_managed": False,
            "service_identity": "other",
        }

    @pytest.mark.parametrize(
        "body", [[], {"service_managed_only": 1}, {"other": True}, {"service_managed_only": "yes"}]
    )
    async def test_a_malformed_body_is_400_before_the_census(self, body):
        def _gate(**_kw):
            raise AssertionError("the census must not be read for a malformed body")

        resp = await restart_gate.api_restart_when_idle(_request(_gate, body=body))

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_body"

    async def test_a_caller_off_the_host_is_refused_before_the_census(self):
        def _gate(**_kw):
            raise AssertionError("the census must not be read for a remote caller")

        resp = await restart_gate.api_restart_when_idle(
            _request(_gate, remote="10.0.0.9", internal=False)
        )

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restart_gate_not_local"

    async def test_a_loopback_cookie_caller_without_the_secret_is_refused(self):
        def _gate(**_kw):
            raise AssertionError("the census must not be read without the internal secret")

        resp = await restart_gate.api_restart_when_idle(_request(_gate, internal=False))

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restart_gate_not_local"

    async def test_no_orchestrator_is_unavailable(self):
        resp = await restart_gate.api_restart_when_idle(_request(None))

        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "restart_gate_unavailable"


class _Reservation:
    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_an_envelope_that_is_not_a_person_typing_does_not_refill_the_typing_window():
    """Slack admits every raw envelope (reactions, bot traffic) before knowing the sender."""
    from kiro_crew.messaging.dispatch_admission import admit_inbound_callback

    sessions = SimpleNamespace(reserve_inbound_callback=_Reservation)

    assert await admit_inbound_callback(
        sessions, channel_type="slack", route=None, human_input=False
    )
    ledger = quiescence._ledger(sessions, create=False)
    assert ledger is None or ledger.last_input_at is None

    assert await admit_inbound_callback(sessions, channel_type="slack", route=None)
    ledger = quiescence._ledger(sessions, create=False)
    assert ledger is not None and ledger.last_input_at is not None


def test_the_slack_socket_listener_admits_envelopes_without_counting_them_as_typing():
    import ast
    import inspect

    from kiro_crew.slack import events

    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(events)))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "admit_inbound_callback"
    ]
    assert calls
    for call in calls:
        flag = {k.arg: k.value for k in call.keywords}.get("human_input")
        assert isinstance(flag, ast.Constant) and flag.value is False


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

    clock = _Clock()
    answers: list[idle_cli._Verdict] = []
    restarts: list[int | None] = []
    monkeypatch.setattr(idle_cli, "_now", lambda: clock.now)
    monkeypatch.setattr(idle_cli, "_sleep", clock.sleep)
    monkeypatch.setattr(idle_cli, "resolve_client_port", lambda port: port or 5476)
    asks: list[bool] = []

    commits: list[bool] = []
    releases: list[int] = []

    def _ask(port, *, service_only=False, commit=False, release=False):
        if release:
            releases.append(port)
            return idle_cli._Verdict("busy")
        asks.append(service_only)
        commits.append(commit)
        # The last scripted answer repeats: an idle answer is asked a second time
        # before the restart, and most tests script one.
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(idle_cli, "_ask_gate", _ask)
    # No probe of the host's real service manager from a unit test.
    monkeypatch.setattr(idle_cli, "_service_scopes_running", lambda: 0)
    monkeypatch.setattr(idle_cli, "_service_active", lambda: False)
    monkeypatch.setattr(service_controller, "service_alias_holder", lambda: None)
    monkeypatch.setattr(cli_server_stop, "desktop_app_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(cli_server, "_restart", lambda port: restarts.append(port))
    return SimpleNamespace(
        clock=clock,
        answers=answers,
        restarts=restarts,
        asks=asks,
        commits=commits,
        releases=releases,
    )


class TestCli:
    def test_waits_while_busy_and_restarts_once_idle(self, cli):
        cli.answers.extend([idle_cli._Verdict("busy", 1, 0), idle_cli._Verdict("idle")])

        idle_cli._restart_when_idle(None)

        assert cli.restarts == [None]
        assert cli.clock.sleeps == [idle_cli.POLL_SECS]

    def test_a_turn_admitted_after_the_idle_answer_is_not_restarted_over(self, cli):
        """The census is a snapshot: the idle answer is asked again before the restart."""
        cli.answers.extend([idle_cli._Verdict("idle"), idle_cli._Verdict("busy", 1, 0)])

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None, max_wait_secs=0)

        assert exc.value.code == idle_cli.EXIT_BUSY
        assert cli.restarts == []
        assert len(cli.asks) == 2

    def test_only_the_confirming_ask_takes_the_pause_and_fence(self, cli):
        cli.answers.append(idle_cli._Verdict("idle"))

        idle_cli._restart_when_idle(None, max_wait_secs=0)

        assert cli.commits == [False, True]
        assert cli.releases == []

    def test_a_legacy_gateway_rejecting_the_commit_ask_falls_back_to_one_step(
        self, cli, monkeypatch
    ):
        """A gateway older than the two-step check answers 400 to the committing ask."""
        cli.answers.append(idle_cli._Verdict("idle"))
        inner = idle_cli._ask_gate

        def _legacy(port, *, service_only=False, commit=False, release=False):
            if commit:
                raise idle_cli._CommitUnsupported()
            return inner(port, service_only=service_only, commit=commit, release=release)

        monkeypatch.setattr(idle_cli, "_ask_gate", _legacy)

        idle_cli._restart_when_idle(None, max_wait_secs=0)

        assert cli.restarts == [None]
        # Whatever the ask may have taken is released, and a restart that then
        # succeeds is not released again.
        assert cli.releases == [5476]

    def test_a_restart_that_fails_after_the_commit_reopens_admission(self, cli, monkeypatch):
        cli.answers.append(idle_cli._Verdict("idle"))

        def _refused(_port):
            raise SystemExit(1)

        monkeypatch.setattr(cli_server, "_restart", _refused)

        with pytest.raises(SystemExit):
            idle_cli._restart_when_idle(7000, max_wait_secs=0)

        assert cli.releases == [7000]

    def test_a_restart_that_succeeds_does_not_release(self, cli, monkeypatch):
        cli.answers.append(idle_cli._Verdict("idle"))

        def _done(_port):
            raise SystemExit(0)

        monkeypatch.setattr(cli_server, "_restart", _done)

        with pytest.raises(SystemExit):
            idle_cli._restart_when_idle(7000, max_wait_secs=0)

        assert cli.releases == []

    def test_a_turn_admitted_after_the_idle_answer_is_waited_on(self, cli):
        cli.answers.extend(
            [idle_cli._Verdict("idle"), idle_cli._Verdict("busy", 1, 0), idle_cli._Verdict("idle")]
        )

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
        # Asked so that gateway does not pause admission for this restart.
        assert cli.asks == [True]

    def test_a_gateway_the_service_did_not_launch_is_refused_at_once(self, cli, monkeypatch):
        """Its no-pause answer reads busy; it is refused, not waited on."""
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("busy", service_managed=False))

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_REFUSED
        assert cli.restarts == [] and cli.clock.sleeps == []

    def test_a_gateway_the_service_manager_says_is_another_process_is_refused(
        self, cli, monkeypatch, capsys
    ):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(
            idle_cli._Verdict("busy", service_managed=False, service_identity="other")
        )

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_REFUSED
        assert cli.restarts == []
        assert "is not the running kirocrew service's gateway" in capsys.readouterr().out

    def test_an_idle_legacy_unit_gateway_is_restarted_with_a_hint(self, cli, monkeypatch, capsys):
        """A unit from before the marker: nothing shows another process runs, so it restarts."""
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(
            idle_cli._Verdict("idle", service_managed=False, service_identity="unknown")
        )

        idle_cli._restart_when_idle(None, max_wait_secs=0)

        assert cli.restarts == [None]
        out = capsys.readouterr().out
        assert "kirocrew service install" in out
        assert "not the running kirocrew service's gateway" not in out

    def test_a_busy_legacy_unit_gateway_is_waited_on_not_refused(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.extend(
            [
                idle_cli._Verdict("busy", 1, 0, service_identity="unknown"),
                idle_cli._Verdict("idle", service_identity="unknown"),
            ]
        )

        idle_cli._restart_when_idle(None)

        assert cli.restarts == [None]
        assert cli.clock.sleeps == [idle_cli.POLL_SECS]

    def test_the_idle_service_gateway_is_restarted(self, cli, monkeypatch):
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("idle", service_managed=True))

        idle_cli._restart_when_idle(None)

        assert cli.restarts == [None]
        assert cli.asks == [True, True]

    def test_a_desktop_app_gateway_is_refused_before_the_gate_pauses_turns(
        self, cli, monkeypatch, capsys
    ):
        """``_restart`` refuses it, so an idle answer would pause turns for nothing.

        Refused the way ``_restart`` refuses it (exit 1, no gate marker), so a
        remote caller reads a failed restart, not an idle check to force past.
        """
        monkeypatch.setattr(cli_server_stop, "desktop_app_supervisor", lambda *a, **k: (11, 22))
        monkeypatch.setattr(cli_server, "sel", MagicMock())

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(7000)

        assert exc.value.code == 1
        assert idle_cli.REFUSED_MARKER not in capsys.readouterr().out
        assert cli.asks == [] and cli.restarts == []

    def test_an_aliased_service_name_is_refused_before_the_gate_pauses_turns(
        self, cli, monkeypatch, capsys
    ):
        holder = SimpleNamespace(unit_id="other.service", scope="user")
        monkeypatch.setattr(service_controller, "service_alias_holder", lambda: holder)
        monkeypatch.setattr(cli_server, "sel", MagicMock())

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == 1
        assert idle_cli.REFUSED_MARKER not in capsys.readouterr().out
        assert cli.asks == [] and cli.restarts == []

    def test_a_running_service_is_restarted_despite_an_alias_elsewhere(self, cli, monkeypatch):
        """``_restart`` restarts the service before it reaches either refusal."""
        holder = SimpleNamespace(unit_id="other.service", scope="system")
        monkeypatch.setattr(service_controller, "service_alias_holder", lambda: holder)
        monkeypatch.setattr(cli_server_stop, "desktop_app_supervisor", lambda *a, **k: (11, 22))
        monkeypatch.setattr(idle_cli, "_service_active", lambda: True)
        cli.answers.append(idle_cli._Verdict("idle", service_managed=True))

        idle_cli._restart_when_idle(None)

        assert cli.restarts == [None]

    def test_without_a_service_the_ask_is_unrestricted(self, cli):
        cli.answers.append(idle_cli._Verdict("idle"))

        idle_cli._restart_when_idle(None)

        assert cli.asks == [False, False]

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
        marker = 'kirocrew-restart-busy {"turns":3,"background":4}'
        assert out.out.rstrip("\n").splitlines()[-1] == marker
        assert marker in out.err

    def test_still_busy_at_the_ceiling_exits_busy_without_restarting(self, cli, capsys):
        cli.answers.extend([idle_cli._Verdict("busy", 2, 5)] * 3)

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None, max_wait_secs=15)

        assert exc.value.code == idle_cli.EXIT_BUSY
        assert cli.restarts == []
        assert cli.clock.sleeps == [10.0, 5.0]
        assert idle_cli.busy_marker(2, 5) in capsys.readouterr().err

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
        def _fail(port, **_):
            raise idle_cli._GateFailed("no answer")

        monkeypatch.setattr(idle_cli, "_ask_gate", _fail)

        with pytest.raises(SystemExit) as exc:
            idle_cli._restart_when_idle(None)

        assert exc.value.code == idle_cli.EXIT_GATE_REFUSED
        assert cli.restarts == []


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

    def _answer(self, monkeypatch, outcome, sent=None):
        def _open(req, port, timeout, *, operation):
            assert req.full_url.endswith("/api/restart/when-idle")
            if sent is not None:
                sent.append(json.loads(req.data))
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
        assert idle_cli._ask_gate(5476).service_identity is None
        self._answer(
            monkeypatch,
            {"idle": True, "turns": 0, "background": 0, "service_identity": "unknown"},
        )
        assert idle_cli._ask_gate(5476).service_identity == "unknown"

    def test_the_body_says_whether_only_the_service_may_answer(self, monkeypatch):
        sent: list = []
        self._answer(monkeypatch, {"idle": True, "turns": 0, "background": 0}, sent)

        idle_cli._ask_gate(5476)
        idle_cli._ask_gate(5476, service_only=True)

        assert sent == [{}, {"service_managed_only": True}]

    @pytest.mark.parametrize("status", [401, 403, 404, 405, 503])
    def test_an_older_gateway_is_unsupported(self, monkeypatch, status):
        self._answer(monkeypatch, _http_error(status, {"error": "Token required"}))
        assert idle_cli._ask_gate(5476).state == "unsupported"

    @pytest.mark.parametrize("status", [400, 404])
    def test_the_commit_ask_rejected_by_a_legacy_gateway_is_commit_unsupported(
        self, monkeypatch, status
    ):
        sent: list = []
        self._answer(monkeypatch, _http_error(status, {"error": "bad body"}), sent)
        with pytest.raises(idle_cli._CommitUnsupported):
            idle_cli._ask_gate(5476, commit=True)
        assert sent == [{"commit": True}]

    def test_a_400_to_the_plain_ask_still_fails(self, monkeypatch):
        self._answer(monkeypatch, _http_error(400, {"error": "bad body"}))
        with pytest.raises(idle_cli._GateFailed):
            idle_cli._ask_gate(5476)

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
