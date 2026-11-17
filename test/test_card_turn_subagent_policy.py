"""A Dashboard card's turn starts no sub-agent.

The exit guard of ``_run_chat`` marks the session while a card turn runs
(``card_send.card_turn``). The spawn gate refuses a spawn whose parent is marked.
Otherwise the child would take its approvals from paths the card mark does not reach
(the gateway's sub-agent approver reads the parent thread's trust), a queued child
could read a later turn's policy, and the child's completion would run as a new turn
nobody marked as a card's. The mark is a registry of its own, not the session's approval
policy, because other writers rewrite that policy.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_subagent import _mock_ctx_builder, _mock_sessions

from kiro_crew.dashboard import card_send
from kiro_crew.subagent import SubagentManager
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_PARENT = "dashboard:chat-1"


def _spawn(*, card_turn: bool, policy: str = "", **spawn_kwargs):
    sessions = _mock_sessions()
    # Whatever a profile write left in the store; the gate must not read it.
    sessions.get_approval_policy = MagicMock(return_value=policy)
    announced = asyncio.Event()

    async def _announce(*_a, **_kw):
        announced.set()

    on_done = AsyncMock(side_effect=_announce)
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        on_spawn_approval=AsyncMock(return_value=True),
        on_done=on_done,
        is_yolo=lambda: True,
    )
    manager.test_on_done = on_done
    manager.test_announced = announced
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel") as audit:
        if card_turn:
            with card_send.card_turn(_PARENT):
                info = manager.spawn("do the thing", parent_session_key=_PARENT, **spawn_kwargs)
        else:
            info = manager.spawn("do the thing", parent_session_key=_PARENT, **spawn_kwargs)
    return manager, info, audit


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["", "auto"])
async def test_the_control_an_ordinary_parent_can_spawn_even_under_yolo(policy):
    manager, info, _ = _spawn(card_turn=False, policy=policy)

    assert info is not None and info.error == ""
    await manager._tasks[info.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["", "auto"])
async def test_a_card_turn_cannot_start_a_sub_agent_whatever_the_stored_policy_says(policy):
    """``auto`` is what a profile write leaves in a trusted thread's store."""
    manager, info, audit = _spawn(card_turn=True, policy=policy)

    assert info is not None
    assert info.done is True
    assert "Dashboard card" in info.error
    assert info.id not in manager._tasks
    outcomes = [
        c.kwargs.get("outcome") for c in audit.return_value.log_tool_invocation.call_args_list
    ]
    assert "rejected_card_origin_turn" in outcomes


@pytest.mark.asyncio
async def test_a_row_admitted_before_the_card_is_not_failed_by_it():
    """A row a typed turn queued earlier and the pump drains later is a re-entry, and
    must not be refused because a card turn is running when it drains."""
    _, info, _ = _spawn(card_turn=True, _from_queue=True)

    assert info is not None
    assert "Dashboard card" not in (info.error or "")


@pytest.mark.asyncio
async def test_a_refused_batch_member_is_not_announced_and_leaves_no_batch_behind():
    """A batch rejection is announced to the gateway, whose wave digest starts a turn on the
    parent that nobody marked as a card's. The refusal is answered as a non-batch one: the
    error comes back in the returned info, and nothing is counted or announced."""
    manager, info, _ = _spawn(card_turn=True, batch_id="wave-1", batch_total=2)
    await asyncio.sleep(0)

    assert info is not None and info.done is True and "Dashboard card" in info.error
    assert info.batch_id == ""
    manager.test_on_done.assert_not_awaited()
    assert "wave-1" not in manager._batch_submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("card_turn", [True, False])
async def test_a_lost_submission_from_a_card_turn_is_not_announced(card_turn):
    """A spawn that fails before the gate is reported to ``/api/spawn/lost``, which counts and
    announces a synthetic member; that announce starts a wave-digest turn on the parent. For
    a card turn nothing was counted (every spawn is refused first), so it is dropped."""
    manager, _, _ = _spawn(card_turn=False)
    manager.test_on_done.reset_mock()

    if card_turn:
        with card_send.card_turn(_PARENT):
            manager.record_lost_submission("wave2", 2, "unknown member", parent_session_key=_PARENT)
        # Dropped synchronously: nothing was scheduled, so there is nothing to wait for.
        assert "wave2" not in manager._batch_submitted
        assert manager.test_on_done.await_count == 0
    else:
        manager.record_lost_submission("wave2", 2, "unknown member", parent_session_key=_PARENT)
        await await_until(manager.test_announced, what="the lost-submission announce")
        assert "wave2" in manager._batch_submitted
        assert manager.test_on_done.await_count == 1


# -- a card turn stopped while its request is in flight ------------------------------


@pytest.mark.asyncio
async def test_a_spawn_seen_as_a_card_turn_on_arrival_is_refused_after_the_turn_stopped():
    """The handler's awaits (body, auth reads, agent warm-up) can outlast the turn: the
    mark is gone by the time the gate runs, but the arrival snapshot still refuses."""
    manager, info, _ = _spawn(card_turn=False, _card_turn=True)

    assert info is not None and info.done is True and "Dashboard card" in info.error
    assert info.id not in manager._tasks


class _ArrivedReq:
    def __init__(self, session: str, arrived: bool) -> None:
        self.headers = {"X-Session-Key": session}
        self._d = {card_send.CARD_TURN_AT_ARRIVAL: arrived}

    def get(self, key, default=None):
        return self._d.get(key, default)


@pytest.mark.parametrize(
    ("header", "arrived", "asked", "seen"),
    [
        (_PARENT, True, _PARENT, True),  # stopped since arrival: still a card turn
        (_PARENT, False, _PARENT, False),  # never was one
        ("dashboard:other", True, _PARENT, False),  # the snapshot is the header's session
        (_PARENT, True, "", False),
    ],
)
def test_card_turn_seen_reads_the_arrival_snapshot_for_the_callers_own_session(
    header, arrived, asked, seen
):
    assert card_send.card_turn_running(_PARENT) is False
    assert card_send.card_turn_seen(_ArrivedReq(header, arrived), asked) is seen


@pytest.mark.asyncio
async def test_stopping_a_card_turn_mid_request_does_not_let_a_delegation_through():
    """The arrival layer records the mark before anything awaits; a layer in between
    stops the turn (releases the mark); the delegation layer still refuses."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    marks = {_PARENT: 1}

    @web.middleware
    async def _auth(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    @web.middleware
    async def _stop_the_turn(request, handler):
        marks.clear()
        return await handler(request)

    async def _ok(_request):
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[
            card_send.card_turn_arrival_middleware,
            _auth,
            _stop_the_turn,
            card_send.card_turn_delegation_middleware,
        ]
    )
    app.router.add_post("/api/session-control/send", _ok)
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock()),
        patch.object(card_send, "_running_card_turns", marks),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/session-control/send", headers={"X-Session-Key": _PARENT}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "card_turn_delegation_refused"
            # Control: with no card turn at arrival the same request passes.
            ok = await client.post("/api/session-control/send", headers={"X-Session-Key": _PARENT})
            assert ok.status == 200


def test_the_spawn_handler_hands_the_arrival_snapshot_to_the_gate(tmp_path, monkeypatch):
    """``api_spawn`` reads the snapshot and passes ``_card_turn`` to the manager, so its
    own awaits (warm-up, memory reads) cannot outlast the card mark unnoticed."""
    cov = pytest.importorskip("test_handlers_messaging_coverage")
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

    def _call(arrived: bool):
        mgr = cov._mgr()
        mgr.spawn.return_value = cov._info()
        state = cov._state(subagents=mgr)
        # A parent with no record and no live selection: the default-template branch.
        state.sessions.get_agent_selection.return_value = ("template", "")
        req = cov._Req(
            state,
            {"task": "x", "parent_session": _PARENT},
            extra={card_send.CARD_TURN_AT_ARRIVAL: arrived},
        )
        req.headers["X-Session-Key"] = _PARENT
        resp = cov._run(cov.mod.api_spawn, req)
        assert mgr.spawn.call_args is not None, resp.body
        return mgr.spawn.call_args.kwargs

    assert _call(True).get("_card_turn") is True
    # Control: no card turn at arrival, nothing extra is passed.
    assert "_card_turn" not in _call(False)


def test_the_mark_is_counted_and_released():
    assert card_send.card_turn_running(_PARENT) is False
    with card_send.card_turn(_PARENT):
        with card_send.card_turn(_PARENT):
            assert card_send.card_turn_running(_PARENT) is True
        assert card_send.card_turn_running(_PARENT) is True
    assert card_send.card_turn_running(_PARENT) is False
    assert card_send.card_turn_running("dashboard:other") is False


# -- the other delegation entry points -----------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/session-control/create"),
        ("POST", "/api/session-control/fork"),
        ("POST", "/api/session-control/send"),
        ("POST", "/api/session-control/broadcast"),
        ("POST", "/api/session-control/continue"),
        ("POST", "/api/session-control/retry"),
        ("POST", "/api/session-control/revive"),
        ("POST", "/api/workflows/run"),
        ("POST", "/api/workflows/run_intent"),
        ("POST", "/api/workflows/definitions/saved-one/run"),
        ("POST", "/api/workflows/runs/abc/promote"),
        ("POST", "/api/workflows/runs/abc/rerun"),
        ("POST", "/api/spawn/a1/steer"),
        ("POST", "/api/spawn/a1/retry"),
        ("POST", "/api/spawn/a1/continue"),
        ("POST", "/api/taskrunner"),
        ("POST", "/api/taskrunner/plan"),
        ("POST", "/api/taskrunner/from-chat"),
        ("POST", "/api/taskrunner/t1/execute"),
        ("POST", "/api/taskrunner/t1/retry"),
        ("PATCH", "/api/taskrunner/t1/tasks/0"),
        ("PUT", "/api/crons/j1/secrets"),
        ("POST", "/api/crons"),
        ("PATCH", "/api/crons/j1"),
        ("POST", "/api/crons/j1/run"),
        ("POST", "/api/crons/j1/enable"),
        ("POST", "/api/autonudge"),
        ("PATCH", "/api/autonudge/l1"),
        ("POST", "/api/autonudge/l1/fire"),
        ("POST", "/api/monitors"),
        ("PATCH", "/api/monitors/m1"),
        ("POST", "/api/monitors/m1/restart"),
        # one trailing slash is the same route
        ("POST", "/api/session-control/create/"),
        ("POST", "/api/crons/"),
        ("POST", "/api/spawn/a1/steer/"),
        ("POST", "/api/taskrunner/"),
    ],
)
def test_every_delegation_route_is_named(method, path):
    assert card_send.is_delegation_route(method, path) is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/session-control/create"),
        ("POST", "/api/session-control/status"),
        ("POST", "/api/session-control/stop"),
        ("POST", "/api/taskrunner/cancel"),
        ("POST", "/api/taskrunner/plan/cancel"),
        ("POST", "/api/taskrunner/t1/pause"),
        ("GET", "/api/taskrunner"),
        ("DELETE", "/api/taskrunner/t1"),
        ("POST", "/api/workflows/runs/abc/cancel"),
        ("POST", "/api/monitors/m1/stop"),
        ("POST", "/api/chat"),
        ("POST", "/api/taskrunner/cancel/"),
        # a shape matches whole segments only: no empty id, no extra or missing segment
        ("POST", "/api/crons//run"),
        ("POST", "/api/workflows/definitions//run"),
        ("POST", "/api/crons/j1/run/extra"),
        ("POST", "/api/session-control/create//"),
        ("POST", "/api/spawn/a1"),
        ("POST", "/api/spawnx/a1/steer"),
        ("POST", "/apix/crons"),
        # the shared cron MCP tools route is judged by tool name in its handler
        ("POST", "/api/crons/tools"),
    ],
)
def test_reads_and_stops_are_not_delegation(method, path):
    assert card_send.is_delegation_route(method, path) is False


@pytest.mark.parametrize(
    ("tool", "marked", "refused"),
    [
        ("cron_list", True, False),
        ("cron_pause", True, False),
        ("cron_remove", True, False),
        ("cron_remove_all", True, False),
        ("cron_add", True, True),
        ("cron_update", True, True),
        ("cron_resume", True, True),
        ("cron_trigger", True, True),
        ("cron_secret_request", True, True),
        ("cron_add", False, False),
    ],
)
def test_a_card_turn_may_read_and_stop_cron_jobs_but_not_start_them(tool, marked, refused):
    audit = MagicMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", lambda: audit),
        patch.object(card_send, "_running_card_turns", {_PARENT: 1} if marked else {}),
    ):
        refusal = card_send.card_turn_cron_tool_refusal(MagicMock(), _PARENT, tool)
    assert (refusal is not None) is refused
    if refused:
        assert refusal.status == 403


def _delegation_app(internal: bool):
    from aiohttp import web

    @web.middleware
    async def _auth(request, handler):
        request["internal_auth"] = internal
        return await handler(request)

    async def _ok(_request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[_auth, card_send.card_turn_delegation_middleware])
    app.router.add_post("/api/session-control/send", _ok)
    app.router.add_post("/api/session-control/stop", _ok)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("internal", "marked", "path", "status"),
    [
        (True, True, "/api/session-control/send", 403),
        (True, True, "/api/session-control/stop", 200),
        (True, False, "/api/session-control/send", 200),
        (False, True, "/api/session-control/send", 200),
    ],
)
async def test_the_delegation_middleware_refuses_only_a_marked_internal_caller(
    internal, marked, path, status
):
    from aiohttp.test_utils import TestClient, TestServer

    audit = MagicMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", lambda: audit),
        patch.object(card_send, "_running_card_turns", {_PARENT: 1} if marked else {}),
    ):
        async with TestClient(TestServer(_delegation_app(internal))) as client:
            resp = await client.post(path, headers={"X-Session-Key": _PARENT})
            assert resp.status == status
            if status == 403:
                assert (await resp.json())["code"] == "card_turn_delegation_refused"
    refused = [c.kwargs for c in audit.log_api_access.call_args_list if c.kwargs.get("error")]
    assert bool(refused) is (status == 403)
