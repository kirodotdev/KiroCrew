"""The restart mode of ``POST /api/restart/when-idle``: the gateway restarts itself in place.

``kirocrew restart`` on a foreground POSIX gateway asks for ``{"restart": true}``
so a supervised gateway re-execs with its own pid and its supervisor keeps its
child. Only the CLI on this host may ask, and only a supervised gateway accepts; a
committed ``--when-idle`` hold is taken over by the restart rather than asked for
again; a refusal that needs no side effect answers before the 200. The restart
itself is faked: these tests pin the gate.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew import member_memory_auth, platform_compat, quiescence, restart_admission
from kiro_crew import sel as sel_module
from kiro_crew import update_ownership
from kiro_crew.dashboard import chat as dashboard_chat
from kiro_crew.dashboard.handlers import restart_gate, updates, updates_apply
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.shared_crew.contracts import SSM_REQUEST_KEY
from kiro_crew.slack import gateway
from kiro_crew.testing.wait import await_until

_real_sleep = asyncio.sleep


class _NoFlushWait:
    """``asyncio`` for the restart task, without the wait that lets the 200 flush."""

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(_delay: float, result: object = None) -> object:
        await _real_sleep(0)
        return result


@pytest.fixture(autouse=True)
def _no_shutdown():
    gateway.shutdown_event.clear()
    yield
    gateway.shutdown_event.clear()


class _Gate:
    """A wired gateway state whose restart is recorded, not run."""

    def __init__(self, monkeypatch) -> None:
        self.restarts: list[dict] = []
        #: When set, the faked restart waits on it, as a real one runs on to its exec.
        self.block: "asyncio.Future[None] | None" = None
        self.target = AsyncMock(return_value=(None, "/python", None))
        self.preflight = MagicMock(return_value=restart_admission.RestartAdmission(True))
        self.admission = AsyncMock(
            return_value=restart_admission.RestartAdmission(True, "", quiescence.Quiescence())
        )

        async def _restart_gateway(state, **kwargs):
            self.restarts.append(kwargs)
            if self.block is not None:
                await self.block
            return True

        monkeypatch.setattr(updates_apply, "_resolve_restart_target", self.target)
        monkeypatch.setattr(updates_apply, "_restart_gateway", _restart_gateway)
        # Through the facade: every owner of the update routes binds ``asyncio``.
        monkeypatch.setattr(updates, "asyncio", _NoFlushWait())
        # A POSIX gateway, whatever host runs the test: only POSIX restarts in place.
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        # The host owner's CLI unless a test says otherwise; the real check reads
        # the peer's process, which a fake request has none of.
        self.provenance: object = member_memory_auth.LocalOwnerProvenance(
            basis="linux-namespace-match"
        )
        monkeypatch.setattr(
            member_memory_auth,
            "local_owner_bootstrap_provenance",
            lambda request: self.provenance,
        )
        # A supervised gateway unless a test says otherwise: no service unit started
        # it, and the shape check passes. The real check reads this test process.
        # ``raising=False`` lets the fixture run against a tree without the helper,
        # so each test there fails on its own assertion instead of in this setup.
        self.managed = False
        self.shape: str | None = None
        monkeypatch.setattr(restart_gate, "launched_as_managed_service", lambda: self.managed)
        monkeypatch.setattr(
            platform_compat, "supervision_shape_failure", lambda: self.shape, raising=False
        )
        self.sel = MagicMock()
        # The gate reads ``sel`` from its home when it records a decision.
        monkeypatch.setattr(sel_module, "sel", self.sel)
        self.state = SimpleNamespace(
            _quiescence_probe=MagicMock(return_value=quiescence.Quiescence()),
            _restart_preflight=self.preflight,
            _restart_admission=self.admission,
            _restart_release=AsyncMock(),
            _restart_commit_hold=None,
            _restart_commit_release_task=None,
            _gateway_restart_task=None,
            _gateway_restart_in_progress=False,
            _background_tasks=set(),
            push_update_progress=lambda *_a, **_k: None,
        )

    def request(
        self,
        body: object = None,
        *,
        marks: dict | None = None,
        headers: dict | None = None,
    ) -> MagicMock:
        req = MagicMock()
        req.remote = "127.0.0.1"
        req.transport = None
        req.headers = dict(headers or {})
        req.can_read_body = body is not None
        req.json = AsyncMock(return_value=body)
        # A bare MagicMock.get is truthy and would pass the auth-mark check vacuously.
        req.get = ({"internal_auth": True} if marks is None else marks).get
        req.app = {"state": self.state}
        return req

    async def ask(self, body: object = None, **kwargs) -> tuple[int, dict]:
        resp = await restart_gate.api_restart_when_idle(self.request(body, **kwargs))
        return resp.status, json.loads(resp.body)

    async def settle(self) -> None:
        task = self.state._gateway_restart_task
        if task is not None:
            await await_until(task, what="the restart task")

    def tasks(self) -> int:
        return len(self.state._background_tasks)

    def audits(self) -> list[dict]:
        return [c.kwargs for c in self.sel.return_value.log_api_access.call_args_list]

    def decision(self) -> tuple[str, str]:
        """The one restart-mode SEL decision: ``(outcome, resources)``."""
        (record,) = [a for a in self.audits() if a["operation"] == "gateway_restart_in_place"]
        return record["outcome"], record["resources"]


@pytest.fixture
def gate(monkeypatch):
    return _Gate(monkeypatch)


async def _arm_hold(gate: _Gate) -> object:
    status, body = await gate.ask({"commit": True})
    assert (status, body["idle"]) == (200, True)
    handle = gate.state._restart_commit_hold
    assert handle is not None
    return handle


_RESTART = {"restart": True}


@pytest.mark.asyncio
class TestAccept:
    async def test_a_cli_ask_starts_one_forced_restart_and_answers_restarting(self, gate):
        status, body = await gate.ask(_RESTART)

        assert (status, body) == (200, {"ok": True, "status": "restarting"})
        await gate.settle()
        assert gate.restarts == [{"force": True}]
        gate.preflight.assert_called_once_with()
        gate.admission.assert_not_awaited()

    async def test_a_running_restart_is_joined_not_repeated(self, gate):
        running = asyncio.get_running_loop().create_future()
        gate.state._gateway_restart_task = running

        status, body = await gate.ask(_RESTART)

        assert status == 200
        assert body == {"ok": True, "status": "restarting", "already_in_progress": True}
        assert gate.tasks() == 0
        running.cancel()

    async def test_two_concurrent_asks_make_one_restart(self, gate):
        gate.block = asyncio.get_running_loop().create_future()

        answers = await asyncio.gather(gate.ask(_RESTART), gate.ask(_RESTART))

        assert sorted(b.get("already_in_progress", False) for _s, b in answers) == [False, True]
        gate.block.set_result(None)
        await gate.settle()
        assert gate.restarts == [{"force": True}]

    async def test_a_gateway_with_no_orchestrator_is_unavailable(self, gate):
        gate.state._quiescence_probe = None

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (503, "restart_gate_unavailable")
        gate.target.assert_not_awaited()
        assert gate.tasks() == 0


@pytest.mark.asyncio
class TestCaller:
    @pytest.mark.parametrize(
        ("marks", "headers", "code"),
        [
            ({"user": "member"}, {}, "restart_gate_not_local"),
            ({"internal_auth": True, SSM_REQUEST_KEY: True}, {}, "restart_gate_not_local"),
            ({"internal_auth": True}, {"X-Session-Key": "dashboard:s1"}, "restart_caller_not_cli"),
            ({"internal_auth": True, "app": "mochi"}, {}, "restart_caller_not_cli"),
            ({"internal_auth": True, "is_dashboard_user": False}, {}, "restart_caller_not_cli"),
        ],
        ids=["no-secret", "ssm-forward", "session-key", "app-claim", "not-dashboard-user"],
    )
    async def test_only_the_cli_on_the_host_may_restart(self, gate, marks, headers, code):
        status, body = await gate.ask(_RESTART, marks=marks, headers=headers)

        assert (status, body["code"]) == (403, code)
        gate.target.assert_not_awaited()
        assert gate.tasks() == 0 and gate.restarts == []
        # The same ask from the CLI's shape is the one that restarts.
        assert (await gate.ask(_RESTART))[0] == 200


@pytest.mark.asyncio
class TestBody:
    @pytest.mark.parametrize(
        "body",
        [
            {"restart": True, "commit": True},
            {"restart": True, "release": True},
            {"restart": True, "service_managed_only": True},
            {"restart": 1},
            {"restart": "yes"},
        ],
    )
    async def test_restart_is_a_bool_and_the_only_true_flag(self, gate, body):
        status, answer = await gate.ask(body)

        assert (status, answer["code"]) == (400, "invalid_body")
        assert gate.tasks() == 0
        assert (await gate.ask(_RESTART))[0] == 200

    async def test_a_false_restart_is_the_plain_ask(self, gate):
        status, body = await gate.ask({"restart": False})

        assert (status, body["idle"]) == (200, True)
        gate.target.assert_not_awaited()


@pytest.mark.asyncio
class TestRefusedBeforeThe200:
    @pytest.mark.parametrize("code", ["restart_launcher_invalid", "restart_interpreter_missing"])
    async def test_a_target_that_cannot_run_is_409(self, gate, code):
        gate.target.return_value = (None, None, (code, "Cannot restart: invalid path"))

        status, body = await gate.ask(_RESTART)

        assert status == 409
        assert body == {"ok": False, "error": "Cannot restart: invalid path", "code": code}
        assert gate.tasks() == 0

    @pytest.mark.parametrize(
        "reason", [restart_admission.REASON_SHUTTING_DOWN, restart_admission.REASON_PAUSE_HELD]
    )
    async def test_a_preflight_refusal_is_409(self, gate, reason):
        refusal = restart_admission.RestartAdmission(False, reason)
        gate.preflight.return_value = refusal

        status, body = await gate.ask(_RESTART)

        assert status == 409
        assert body == {"ok": False, "error": refusal.detail(), "code": reason}
        assert gate.tasks() == 0


def _a_restart_took_the_pause(gate: _Gate) -> None:
    """The real preflight over an orchestrator whose pause a running restart holds."""
    orchestrator = SimpleNamespace(_update_pause_holder=restart_admission.HOLDER)
    gate.state._restart_preflight = lambda: restart_admission.preflight_restart(orchestrator)


_JOINED = {"ok": True, "status": "restarting", "already_in_progress": True}


@pytest.mark.asyncio
class TestJoinBeforeThePreflight:
    """A restart already running holds the pause; restart mode joins it rather than refuse."""

    async def test_a_live_restart_task_is_joined_before_the_preflight(self, gate):
        _a_restart_took_the_pause(gate)
        running = asyncio.get_running_loop().create_future()
        gate.state._gateway_restart_task = running
        try:
            status, body = await gate.ask(_RESTART)

            assert (status, body) == (200, _JOINED)
            assert gate.state._gateway_restart_task is running
            assert gate.tasks() == 0 and gate.restarts == []
        finally:
            running.cancel()

    async def test_another_routes_restart_claim_is_joined(self, gate):
        _a_restart_took_the_pause(gate)
        gate.state._gateway_restart_in_progress = True

        status, body = await gate.ask(_RESTART)

        assert (status, body) == (200, _JOINED)
        assert gate.state._gateway_restart_task is None
        assert gate.tasks() == 0 and gate.restarts == []

    async def test_another_routes_restart_claim_is_joined_and_the_hold_kept(self, gate):
        handle = await _arm_hold(gate)
        _a_restart_took_the_pause(gate)
        gate.state._gateway_restart_in_progress = True
        try:
            status, body = await gate.ask(_RESTART)

            assert (status, body) == (200, _JOINED)
            assert gate.state._gateway_restart_task is None
            assert gate.tasks() == 0 and gate.restarts == []
            assert gate.state._restart_commit_hold is handle and not handle.cancelled()
        finally:
            restart_gate._cancel_commit_hold(gate.state)

    async def test_a_target_refusal_still_answers_before_the_join(self, gate):
        gate.state._gateway_restart_in_progress = True
        gate.target.return_value = (None, None, ("restart_launcher_invalid", "Cannot restart"))

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (409, "restart_launcher_invalid")

    async def test_the_dashboard_restart_keeps_its_order_and_refuses(self, gate, monkeypatch):
        monkeypatch.setattr(
            updates, "require_owner_dashboard_request", AsyncMock(return_value=None)
        )
        _a_restart_took_the_pause(gate)
        running = asyncio.get_running_loop().create_future()
        gate.state._gateway_restart_task = running
        gate.state._gateway_restart_in_progress = True
        try:
            resp = await updates_apply.api_gateway_restart(gate.request())
            body = json.loads(resp.body)

            assert (resp.status, body["code"]) == (409, restart_admission.REASON_PAUSE_HELD)
            assert gate.tasks() == 0
        finally:
            running.cancel()


@pytest.mark.asyncio
class TestHoldTakeover:
    async def test_a_committed_hold_is_taken_over_not_asked_for_again(self, gate):
        handle = await _arm_hold(gate)
        # The hold owns the pause, which the preflight reads as held elsewhere.
        gate.preflight.return_value = restart_admission.RestartAdmission(
            False, restart_admission.REASON_PAUSE_HELD
        )

        status, body = await gate.ask(_RESTART)

        assert (status, body) == (200, {"ok": True, "status": "restarting"})
        assert gate.state._restart_commit_hold is None
        assert handle.cancelled()
        await gate.settle()
        assert gate.restarts == [{"admission_held": True}]
        gate.admission.assert_awaited_once_with(force=False)
        gate.state._restart_release.assert_not_awaited()

    async def test_a_gateway_shutting_down_refuses_the_takeover_and_keeps_the_hold(self, gate):
        handle = await _arm_hold(gate)
        refusal = restart_admission.RestartAdmission(False, restart_admission.REASON_SHUTTING_DOWN)
        gate.preflight.return_value = refusal
        try:
            status, body = await gate.ask(_RESTART)

            assert (status, body["code"]) == (409, restart_admission.REASON_SHUTTING_DOWN)
            assert gate.state._restart_commit_hold is handle and not handle.cancelled()
            assert gate.tasks() == 0
        finally:
            restart_gate._cancel_commit_hold(gate.state)

    async def test_a_refused_takeover_keeps_the_hold_and_its_timer(self, gate):
        handle = await _arm_hold(gate)
        gate.target.return_value = (None, None, ("restart_launcher_invalid", "Cannot restart"))
        try:
            status, _body = await gate.ask(_RESTART)

            assert status == 409
            assert gate.state._restart_commit_hold is handle
            assert not handle.cancelled()
            assert gate.tasks() == 0
        finally:
            restart_gate._cancel_commit_hold(gate.state)

    async def test_a_joined_restart_keeps_the_hold(self, gate):
        handle = await _arm_hold(gate)
        running = asyncio.get_running_loop().create_future()
        gate.state._gateway_restart_task = running
        try:
            status, body = await gate.ask(_RESTART)

            assert (status, body.get("already_in_progress")) == (200, True)
            assert gate.state._restart_commit_hold is handle and not handle.cancelled()
        finally:
            running.cancel()
            restart_gate._cancel_commit_hold(gate.state)

    async def test_a_hold_that_lapsed_during_the_target_check_is_not_taken_over(self, gate):
        await _arm_hold(gate)

        async def _lapse(*_a):
            restart_gate._cancel_commit_hold(gate.state)
            return None, "/python", None

        gate.target.side_effect = _lapse

        status, _body = await gate.ask(_RESTART)

        assert status == 200
        await gate.settle()
        assert gate.restarts == [{"force": True}]
        gate.preflight.assert_called_once_with()


@pytest.mark.asyncio
class TestRestartWithHeldAdmission:
    """``_restart_gateway(admission_held=True)`` never asks for admission again.

    The admission seam here refuses, as the real one does while the held pause is
    in place (``pause_held_elsewhere``), so a restart that asked again would stop.
    """

    def _state(self) -> SimpleNamespace:
        refusal = restart_admission.RestartAdmission(False, restart_admission.REASON_PAUSE_HELD)
        return SimpleNamespace(
            _restart_admission=AsyncMock(return_value=refusal),
            _restart_release=AsyncMock(),
            _gateway_restart_in_progress=False,
            push_update_progress=lambda *_a, **_k: None,
        )

    async def test_a_held_admission_is_not_asked_for_again(self, monkeypatch):
        class _PastAdmission(Exception):
            pass

        def _committed() -> None:
            raise _PastAdmission()

        monkeypatch.setattr(
            updates_apply, "_resolve_restart_target", AsyncMock(return_value=(None, "/py", None))
        )
        monkeypatch.setattr(dashboard_chat, "save_all_slots_to_history", lambda _state: None)
        # The first step after the admission: reaching it proves none was asked for.
        monkeypatch.setattr(update_ownership, "restart_committed", _committed)
        state = self._state()

        with pytest.raises(_PastAdmission):
            await updates_apply._restart_gateway(state, admission_held=True)

        state._restart_admission.assert_not_called()
        # Stopped before the commit: the admission it was handed is reopened.
        state._restart_release.assert_awaited_once_with()
        assert state._gateway_restart_in_progress is False

    async def test_a_restart_that_stops_before_it_commits_reopens_the_held_admission(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            updates_apply,
            "_resolve_restart_target",
            AsyncMock(return_value=(None, None, ("restart_launcher_invalid", "Cannot restart"))),
        )
        state = self._state()

        assert await updates_apply._restart_gateway(state, admission_held=True) is False

        state._restart_admission.assert_not_called()
        state._restart_release.assert_awaited_once_with()
        assert state._gateway_restart_in_progress is False

    async def test_only_a_coalesced_restart_handed_admission_reopens_it(self):
        state = self._state()
        state._gateway_restart_in_progress = True

        assert await updates_apply._restart_gateway(state) is False
        state._restart_release.assert_not_awaited()

        assert await updates_apply._restart_gateway(state, admission_held=True) is False
        state._restart_release.assert_awaited_once_with()


# -- the host owner and the audit trail ------------------------------------------------


@pytest.mark.asyncio
class TestHostOwner:
    """The secret and a missing session header do not prove the operator: the kernel does."""

    async def test_a_gateway_off_posix_refuses_before_any_provenance_lookup(
        self, gate, monkeypatch
    ):
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)

        def _no_lookup(request):
            raise AssertionError("no provenance lookup off POSIX")

        monkeypatch.setattr(member_memory_auth, "local_owner_bootstrap_provenance", _no_lookup)

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (403, "restart_in_place_unsupported")
        gate.target.assert_not_awaited()
        assert gate.tasks() == 0 and gate.restarts == []
        assert gate.decision() == ("denied", "reason=restart_in_place_unsupported")

    async def test_a_peer_with_no_host_provenance_is_refused(self, gate):
        gate.provenance = None

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (403, "restart_caller_not_host_owner")
        gate.target.assert_not_awaited()
        assert gate.tasks() == 0 and gate.restarts == []
        assert gate.decision() == ("denied", "reason=restart_caller_not_host_owner")

    async def test_an_app_backend_is_refused(self, gate):
        gate.provenance = member_memory_auth.LocalOwnerProvenance(app="mochi", basis="spawned-app")

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (403, "restart_caller_not_host_owner")
        assert gate.tasks() == 0 and gate.restarts == []
        assert gate.decision() == ("denied", "reason=restart_caller_not_host_owner app=mochi")

    async def test_the_host_owners_cli_is_accepted(self, gate):
        status, _body = await gate.ask(_RESTART)

        assert status == 200
        await gate.settle()
        assert gate.restarts == [{"force": True}]
        assert gate.decision() == ("allowed", "status=started basis=linux-namespace-match")


@pytest.mark.asyncio
class TestEveryDecisionIsAudited:
    async def test_a_session_caller_is_recorded_as_refused(self, gate):
        await gate.ask(_RESTART, headers={"X-Session-Key": "dashboard:s1"})

        assert gate.decision() == ("denied", "reason=restart_caller_not_cli")
        # The header is the caller's own text: the record names its kind, never its value.
        assert gate.audits()[0]["caller"] == "session"
        assert "dashboard:s1" not in repr(gate.audits())

    async def test_a_secret_sent_as_the_session_header_never_reaches_the_record(self, gate):
        await gate.ask(_RESTART, headers={"X-Session-Key": _GATE_SECRET})

        assert gate.decision() == ("denied", "reason=restart_caller_not_cli")
        assert _GATE_SECRET not in repr(gate.audits())

    async def test_a_joined_restart_is_recorded(self, gate):
        gate.state._gateway_restart_in_progress = True

        await gate.ask(_RESTART)

        assert gate.decision() == ("allowed", "status=joined basis=linux-namespace-match")

    async def test_a_target_refusal_is_recorded(self, gate):
        gate.target.return_value = (None, None, ("restart_launcher_invalid", "Cannot restart"))

        await gate.ask(_RESTART)

        assert gate.decision() == ("denied", "reason=restart_launcher_invalid")

    async def test_a_preflight_refusal_is_recorded(self, gate):
        gate.preflight.return_value = restart_admission.RestartAdmission(
            False, restart_admission.REASON_SHUTTING_DOWN
        )

        await gate.ask(_RESTART)

        assert gate.decision() == ("denied", "reason=shutting_down")

    async def test_an_unwired_gateway_is_recorded(self, gate):
        gate.state._quiescence_probe = None

        await gate.ask(_RESTART)

        assert gate.decision() == ("denied", "reason=restart_gate_unavailable")

    async def test_an_audit_that_fails_does_not_undo_the_restart(self, gate):
        gate.sel.return_value.log_api_access.side_effect = OSError("disk full")

        status, _body = await gate.ask(_RESTART)

        assert status == 200
        await gate.settle()
        assert gate.restarts == [{"force": True}]


# -- only a supervised gateway restarts in place --------------------------------------

_UNSUPERVISED = "restart_in_place_unsupervised"


def _unsupervised(gate: _Gate, why: str) -> None:
    """Make the gateway fail the supervision rule for *why*."""
    if why == "service":
        gate.managed = True
    else:
        gate.shape = why


@pytest.mark.asyncio
class TestOnlyASupervisedGatewayRestartsInPlace:
    """A gateway nothing would relaunch keeps the CLI's stop-and-spawn: 403, no restart."""

    @pytest.mark.parametrize("why", ["service", "terminal", "own_group"])
    async def test_an_unsupervised_gateway_is_refused_and_nothing_restarts(self, gate, why):
        _unsupervised(gate, why)

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (403, _UNSUPERVISED)
        assert isinstance(body["error"], str) and body["error"]
        gate.target.assert_not_awaited()
        gate.preflight.assert_not_called()
        assert gate.tasks() == 0 and gate.restarts == []
        assert gate.decision() == ("denied", f"reason={_UNSUPERVISED} why={why}")

    @pytest.mark.parametrize("why", ["service", "terminal", "own_group"])
    async def test_a_committed_hold_stays_with_its_timer(self, gate, why):
        handle = await _arm_hold(gate)
        _unsupervised(gate, why)
        try:
            status, body = await gate.ask(_RESTART)

            assert (status, body["code"]) == (403, _UNSUPERVISED)
            assert gate.state._restart_commit_hold is handle and not handle.cancelled()
            assert gate.tasks() == 0 and gate.restarts == []
            gate.state._restart_release.assert_not_awaited()
        finally:
            restart_gate._cancel_commit_hold(gate.state)

    async def test_an_unverified_caller_gets_its_own_refusal_not_the_shape(self, gate):
        gate.provenance = None
        gate.shape = "terminal"

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (403, "restart_caller_not_host_owner")
        assert gate.decision() == ("denied", "reason=restart_caller_not_host_owner")

    async def test_the_shape_refusal_comes_before_an_unwired_gateway(self, gate):
        gate.state._quiescence_probe = None
        gate.shape = "own_group"

        status, body = await gate.ask(_RESTART)

        assert (status, body["code"]) == (403, _UNSUPERVISED)
        assert gate.decision() == ("denied", f"reason={_UNSUPERVISED} why=own_group")

    async def test_a_running_restart_is_not_joined_and_is_left_alone(self, gate):
        running = asyncio.get_running_loop().create_future()
        gate.state._gateway_restart_task = running
        gate.state._gateway_restart_in_progress = True
        gate.shape = "terminal"
        try:
            status, body = await gate.ask(_RESTART)

            assert (status, body["code"]) == (403, _UNSUPERVISED)
            assert "already_in_progress" not in body
            assert gate.state._gateway_restart_task is running and not running.done()
            assert gate.state._gateway_restart_in_progress is True
            assert gate.tasks() == 0 and gate.restarts == []
        finally:
            running.cancel()


# -- through the real token middleware ------------------------------------------------

_GATE_PATH = "/api/restart/when-idle"
_GATE_SECRET = "internal-secret-for-the-restart-mode"


async def _ask_through_middleware(gate: _Gate, session_key: str | None) -> web.Response:
    store: dict = {"peer_verified": True}
    req = MagicMock(spec=web.Request)
    req.path = _GATE_PATH
    req.method = "POST"
    req.query = {}
    req.cookies = {}
    req.remote = "127.0.0.1"
    req.transport = None
    req.can_read_body = True
    req.json = AsyncMock(return_value=dict(_RESTART))
    req.headers = {"X-Internal-Secret": _GATE_SECRET}
    if session_key is not None:
        req.headers["X-Session-Key"] = session_key
    req.__setitem__.side_effect = store.__setitem__
    req.__getitem__.side_effect = store.__getitem__
    req.__contains__.side_effect = store.__contains__
    req.get.side_effect = store.get
    gate.state._slots = {"my-slot": SimpleNamespace(_app="", linked_session_key="")}
    gate.state.crons = SimpleNamespace(_jobs=[])
    gate.state.subagents = SimpleNamespace(_agents={})
    req.app = {"state": gate.state}
    middleware = token_auth_middleware(
        internal_paths=frozenset({_GATE_PATH}), internal_secret=_GATE_SECRET
    )
    return await middleware(req, restart_gate.api_restart_when_idle)


@pytest.mark.asyncio
class TestThroughTheMiddleware:
    async def test_the_cli_with_the_secret_and_no_session_restarts(self, gate):
        resp = await _ask_through_middleware(gate, None)

        assert resp.status == 200
        await gate.settle()
        assert gate.restarts == [{"force": True}]
        assert _GATE_SECRET not in repr(gate.audits())

    async def test_a_sandboxed_shell_with_the_secret_and_no_session_is_refused(self, gate):
        # The secret is readable in the agent sandbox; the kernel names no host process.
        gate.provenance = None

        resp = await _ask_through_middleware(gate, None)

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restart_caller_not_host_owner"
        assert gate.restarts == [] and gate.tasks() == 0
        assert _GATE_SECRET not in repr(gate.audits())

    async def test_an_agent_tool_call_with_the_secret_is_refused(self, gate):
        resp = await _ask_through_middleware(gate, "dashboard:my-slot")

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "restart_caller_not_cli"
        assert gate.restarts == [] and gate.tasks() == 0
