"""Positive controls for the owner gates on the autonudge, taskrunner, memory, computer-use, update and side routes.

Each row sends a request the gate must ADMIT and asserts the collaborator behind the
gate was reached, not only the status. A gate moved somewhere it never runs, or one
that refuses a caller class it has no business ruling on, fails here.

The caller classes:

* the owner's dashboard cookie: ``app == ""`` with the owner's subject;
* an app token: ``app`` carries the app's name, and ``_enforce_app_scope`` confines
  it to its manifest's declared paths (the Projects app declares ``/api/taskrunner``,
  and several apps declare ``/api/chat/*``);
* an ``X-Internal-Secret`` loopback process: ``app`` is ABSENT and
  ``internal_auth`` is True (the ``task_run`` MCP tool posts ``/api/taskrunner``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio

OWNER = "U0OWNER0000"
NON_OWNER = "U0NONOWNER"

_ABSENT = object()


def _identity(user: object = _ABSENT, app: object = _ABSENT, internal: bool = False):
    """Publish exactly the claims one caller class carries, as the auth middleware does."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        if user is not _ABSENT:
            request["user"] = user
        if app is not _ABSENT:
            request["app"] = app
        if internal:
            request["internal_auth"] = True
        return await handler(request)

    return middleware


def _owner():
    return _identity(user=OWNER, app="")


# ── autonudge: PATCH / DELETE (legacy and structured) ──


def _autonudge_app(monkeypatch, middleware, *, existing=None):
    from kiro_crew.dashboard.handlers import autonudge

    svc = MagicMock()
    svc.get_by_id = MagicMock(return_value=existing)
    svc.remove = AsyncMock()
    monkeypatch.setattr(autonudge, "_autonudge_get", lambda: svc)
    monkeypatch.setattr(autonudge, "_serialize", lambda loop: {"loop_id": "l1"})
    monkeypatch.setattr(autonudge, "sel", lambda: MagicMock())
    updated = AsyncMock(return_value=(SimpleNamespace(loop_id="l1"), None, 200))
    monkeypatch.setattr(autonudge, "authorize_and_update_nudge", updated)
    stopped = AsyncMock(return_value=(SimpleNamespace(loop_id="l1"), None, 200))
    monkeypatch.setattr(autonudge, "authorize_and_stop_monitor", stopped)

    app = web.Application(middlewares=[middleware])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_patch("/api/autonudge/{loop_id}", autonudge.api_autonudge_update)
    app.router.add_delete("/api/autonudge/{loop_id}", autonudge.api_autonudge_delete)
    return app, svc, updated, stopped


async def test_autonudge_update_allows_owner(monkeypatch) -> None:
    app, _svc, updated, _stopped = _autonudge_app(monkeypatch, _owner())
    async with TestClient(TestServer(app)) as client:
        response = await client.patch("/api/autonudge/l1", json={"message": "keep going"})
        assert response.status == 200, await response.text()
    updated.assert_awaited_once()


async def test_autonudge_delete_legacy_allows_owner(monkeypatch) -> None:
    app, svc, _updated, _stopped = _autonudge_app(monkeypatch, _owner())
    async with TestClient(TestServer(app)) as client:
        response = await client.delete("/api/autonudge/l1")
        assert response.status == 200, await response.text()
    svc.remove.assert_awaited_once_with("l1")


async def test_autonudge_delete_structured_branch_still_gates_once_and_stops(monkeypatch) -> None:
    """The structured branch keeps its own gate; the legacy gate adds nothing to it."""
    from kiro_crew.dashboard.handlers import autonudge

    live = SimpleNamespace(slot_key="chat-1-1", monitor=SimpleNamespace(outcome=None))
    monkeypatch.setattr(autonudge, "is_structured_monitor_loop", lambda loop: True)
    gate = AsyncMock(wraps=autonudge._require_monitor_owner)
    monkeypatch.setattr(autonudge, "_require_monitor_owner", gate)
    app, svc, _updated, stopped = _autonudge_app(monkeypatch, _owner(), existing=live)
    async with TestClient(TestServer(app)) as client:
        response = await client.delete("/api/autonudge/l1?intent=stop")
        assert response.status == 200, await response.text()
    stopped.assert_awaited_once()
    svc.remove.assert_not_awaited()
    assert [c.args[1] for c in gate.await_args_list] == ["monitor_stop"]


# ── taskrunner: the app-token and internal-secret callers are not owner-gated ──


def _taskrunner_app(middleware, runner):
    from kiro_crew.dashboard.handlers import taskrunner

    app = web.Application(middlewares=[middleware])
    app["state"] = SimpleNamespace(task_runner=runner, owner_id=OWNER)
    app.router.add_put("/api/taskrunner/{task_id}/plan", taskrunner.api_taskrunner_update_plan)
    app.router.add_post("/api/taskrunner/cancel", taskrunner.api_taskrunner_cancel)
    return app


def _runner():
    runner = MagicMock()
    runner._runs = {"t1": SimpleNamespace(task_id="t1")}
    runner.update_plan = AsyncMock(return_value=SimpleNamespace(tasks=[], task_id="t1"))
    runner._group_parallel_tasks = MagicMock(return_value=[])
    return runner


async def test_taskrunner_update_plan_app_token_still_reaches_runner() -> None:
    """The Projects app's token (``app="projects"``) is scoped by its manifest, not by owner."""
    runner = _runner()
    app = _taskrunner_app(_identity(user="dashboard", app="projects"), runner)
    async with TestClient(TestServer(app)) as client:
        response = await client.put("/api/taskrunner/t1/plan", json={"steps": []})
        assert response.status == 200, await response.text()
    runner.update_plan.assert_awaited_once_with("t1", [])


async def test_taskrunner_update_plan_allows_owner() -> None:
    runner = _runner()
    app = _taskrunner_app(_owner(), runner)
    async with TestClient(TestServer(app)) as client:
        response = await client.put("/api/taskrunner/t1/plan", json={"steps": []})
        assert response.status == 200, await response.text()
    runner.update_plan.assert_awaited_once_with("t1", [])


async def test_taskrunner_cancel_internal_secret_still_cancels() -> None:
    """The internal-secret caller arrives with ``app`` ABSENT, which the gate never rules on."""
    runner = _runner()
    app = _taskrunner_app(_identity(internal=True), runner)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/taskrunner/cancel", json={"task_id": "t1"})
        assert response.status == 200, await response.text()
    runner.cancel.assert_called_once_with("t1", exact=True)


async def test_taskrunner_cancel_refuses_non_owner() -> None:
    """The same route, same body, with a non-owner dashboard subject: refused before the runner."""
    runner = _runner()
    app = _taskrunner_app(_identity(user=NON_OWNER, app=""), runner)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/taskrunner/cancel", json={"task_id": "t1"})
        assert response.status == 403, await response.text()
    runner.cancel.assert_not_called()


# ── side turn: an app token keeps its slot scoping ──


def _side_app(monkeypatch, middleware, *, slot_app: str):
    from kiro_crew.dashboard.handlers import side

    dispatch = MagicMock(return_value="run-1")
    monkeypatch.setattr(side, "_dispatch_side_turn", dispatch)
    monkeypatch.setattr(side, "sel", lambda: MagicMock())
    sidecar = SimpleNamespace(open=True, last_run_id="", is_complete=True, queue=[], messages=[])
    slot = MagicMock(key="chat-1-1700000000", _app=slot_app)
    slot._side = sidecar

    app = web.Application(middlewares=[middleware])
    app["state"] = MagicMock(owner_id=OWNER, _slots={"chat-1-1700000000": slot})
    app.router.add_post("/api/chat/slots/{slot}/side/turn", side.api_side_turn)
    return app, dispatch


_SIDE_PATH = "/api/chat/slots/chat-1-1700000000/side/turn"


async def test_side_turn_app_token_owned_slot_still_runs(monkeypatch) -> None:
    """An app token on its own app's slot is not owner-gated: the turn is dispatched."""
    app, dispatch = _side_app(
        monkeypatch, _identity(user="dashboard", app="spec-builder"), slot_app="spec-builder"
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.post(_SIDE_PATH, json={"question": "status?"})
        assert response.status == 200, await response.text()
    dispatch.assert_called_once()


async def test_side_turn_app_token_foreign_slot_still_404(monkeypatch) -> None:
    """The app-isolation answer is unchanged: a foreign slot reads as missing."""
    app, dispatch = _side_app(
        monkeypatch, _identity(user="dashboard", app="spec-builder"), slot_app=""
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.post(_SIDE_PATH, json={"question": "status?"})
        assert response.status == 404, await response.text()
    dispatch.assert_not_called()


# ── computer use: an app token keeps its own refusal code ──


async def test_computer_use_config_app_token_still_dashboard_user_required(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import computer_use

    monkeypatch.setattr(computer_use, "_audit", lambda *a, **k: None)
    app = web.Application(middlewares=[_identity(user="dashboard", app="some-app")])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_put("/api/computer-use/config", computer_use.api_computer_use_config_save)
    async with TestClient(TestServer(app)) as client:
        response = await client.put("/api/computer-use/config", json={"enabled": True})
        assert response.status == 403
        assert (await response.json())["code"] == "dashboard_user_required"


# ── owner rows for the routes each module's own coverage file does not drive at 200 ──


async def test_autonudge_start_allows_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import autonudge

    armed = AsyncMock(return_value=(SimpleNamespace(loop_id="l1"), None, 200))
    monkeypatch.setattr(autonudge, "authorize_and_add_nudge", armed)
    app, _svc, _updated, _stopped = _autonudge_app(monkeypatch, _owner())
    app.router.add_post("/api/autonudge", autonudge.api_autonudge_start)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/autonudge", json={"slot_key": "chat-1-1", "message": "x"}
        )
        assert response.status == 200, await response.text()
    armed.assert_awaited_once()


async def test_computer_use_config_allows_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import computer_use

    write_state = MagicMock()
    monkeypatch.setattr(computer_use, "_write_state", write_state)
    monkeypatch.setattr(computer_use, "_write_limits", MagicMock())
    monkeypatch.setattr(computer_use, "_assert_writable", MagicMock())
    monkeypatch.setattr(computer_use.enable_state, "is_enabled", MagicMock(return_value=False))
    monkeypatch.setattr(computer_use, "_full_payload", AsyncMock(return_value={}))
    monkeypatch.setattr(computer_use, "_audit", lambda *a, **k: None)
    app = web.Application(middlewares=[_owner()])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_put("/api/computer-use/config", computer_use.api_computer_use_config_save)
    async with TestClient(TestServer(app)) as client:
        response = await client.put("/api/computer-use/config", json={"enabled": True})
        assert response.status == 200, await response.text()
    write_state.assert_called()


async def test_update_apply_allows_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import updates
    from kiro_crew.platform import update_provider

    applied = AsyncMock(return_value=True)
    restarted = AsyncMock()
    monkeypatch.setattr(update_provider, "apply_policy_update", applied)
    monkeypatch.setattr(updates, "_restart_gateway", restarted)
    app = web.Application(middlewares=[_owner()])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_post("/api/update", updates.api_update_apply)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/update", json={})
        assert response.status == 200, await response.text()
    applied.assert_awaited_once()
    restarted.assert_awaited_once()


async def test_side_turn_allows_owner(monkeypatch) -> None:
    app, dispatch = _side_app(monkeypatch, _owner(), slot_app="")
    async with TestClient(TestServer(app)) as client:
        response = await client.post(_SIDE_PATH, json={"question": "status?"})
        assert response.status == 200, await response.text()
    dispatch.assert_called_once()
