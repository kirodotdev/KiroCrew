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
    ],
)
def test_reads_and_stops_are_not_delegation(method, path):
    assert card_send.is_delegation_route(method, path) is False


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
