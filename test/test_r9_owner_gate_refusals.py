"""Refusal rows for every route the dashboard owner gate covers in these six modules.

The subject is the one ``slack/allowlist.py`` mints a dashboard token for: any
allow-listed channel user, carrying ``app == ""`` and a subject that is NOT
``state.owner_id``. ``is_owner_dashboard_request`` answers False for it.

Every row asserts the refusal AND that nothing behind the gate ran. For the
taskrunner and memory rows the state object fails the test on any attribute read
other than ``owner_id``, which is the one attribute the gate itself reads, so a
gate placed after the first collaborator call cannot pass.
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


@web.middleware
async def _non_owner(request: web.Request, handler):
    request["user"] = NON_OWNER
    request["app"] = ""
    return await handler(request)


class _GateOnlyState:
    """A state that only the owner gate may read: any other attribute fails the test."""

    owner_id = OWNER

    def __getattr__(self, name: str):
        raise AssertionError(f"handler read state.{name} before refusing the non-owner")


def _client(app: web.Application) -> TestClient:
    return TestClient(TestServer(app))


async def _assert_owner_only(response) -> None:
    text = await response.text()
    assert response.status == 403, f"non-owner was not refused: {response.status} {text}"
    assert (await response.json())["code"] == "owner_only", text


# ── autonudge: POST, PATCH, legacy DELETE, fire ──


def _autonudge_app(monkeypatch):
    from kiro_crew.dashboard.handlers import autonudge

    svc = MagicMock()
    svc.get_by_id = MagicMock(return_value=None)
    svc.remove = AsyncMock()
    svc.fire_now = AsyncMock()
    monkeypatch.setattr(autonudge, "_autonudge_get", lambda: svc)
    monkeypatch.setattr(autonudge, "sel", lambda: MagicMock())
    updated = AsyncMock(return_value=(SimpleNamespace(loop_id="l1"), None, 200))
    monkeypatch.setattr(autonudge, "authorize_and_update_nudge", updated)

    app = web.Application(middlewares=[_non_owner])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app.router.add_post("/api/autonudge", autonudge.api_autonudge_start)
    app.router.add_patch("/api/autonudge/{loop_id}", autonudge.api_autonudge_update)
    app.router.add_delete("/api/autonudge/{loop_id}", autonudge.api_autonudge_delete)
    app.router.add_post("/api/autonudge/{loop_id}/fire", autonudge.api_autonudge_fire)
    return app, svc, updated


async def _assert_monitor_owner_required(response) -> None:
    text = await response.text()
    assert response.status == 403, f"non-owner was not refused: {response.status} {text}"
    assert (await response.json())["code"] == "dashboard_owner_required", text


async def test_autonudge_start_refuses_non_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import autonudge

    armed = AsyncMock(return_value=(SimpleNamespace(loop_id="l1"), None, 200))
    monkeypatch.setattr(autonudge, "authorize_and_add_nudge", armed)
    app, _svc, _updated = _autonudge_app(monkeypatch)
    async with _client(app) as client:
        await _assert_monitor_owner_required(
            await client.post("/api/autonudge", json={"slot_key": "chat-1-1", "message": "x"})
        )
    armed.assert_not_awaited()


async def test_autonudge_update_refuses_non_owner(monkeypatch) -> None:
    app, _svc, updated = _autonudge_app(monkeypatch)
    async with _client(app) as client:
        await _assert_monitor_owner_required(
            await client.patch("/api/autonudge/l1", json={"message": "exfiltrate"})
        )
    updated.assert_not_awaited()


async def test_autonudge_delete_legacy_refuses_non_owner(monkeypatch) -> None:
    app, svc, _updated = _autonudge_app(monkeypatch)
    async with _client(app) as client:
        await _assert_monitor_owner_required(await client.delete("/api/autonudge/l1"))
    svc.remove.assert_not_awaited()


async def test_autonudge_fire_refuses_non_owner(monkeypatch) -> None:
    app, svc, _updated = _autonudge_app(monkeypatch)
    async with _client(app) as client:
        await _assert_monitor_owner_required(await client.post("/api/autonudge/l1/fire"))
    svc.get_by_id.assert_not_called()
    svc.fire_now.assert_not_awaited()


# ── taskrunner: every mutating route ──

_TASKRUNNER_ROUTES = [
    (
        "put",
        "/api/taskrunner/{task_id}/plan",
        "/api/taskrunner/t1/plan",
        "update_plan",
        {"steps": [{"title": "x"}]},
    ),
    ("post", "/api/taskrunner", "/api/taskrunner", "start", {"spec": "__inline__:# x"}),
    ("post", "/api/taskrunner/cancel", "/api/taskrunner/cancel", "cancel", {"task_id": "t1"}),
    ("post", "/api/taskrunner/plan", "/api/taskrunner/plan", "plan", {"input": "x"}),
    ("post", "/api/taskrunner/plan/cancel", "/api/taskrunner/plan/cancel", "plan_cancel", None),
    (
        "post",
        "/api/taskrunner/from-chat",
        "/api/taskrunner/from-chat",
        "from_chat",
        {"steps": [{"title": "x"}]},
    ),
    ("post", "/api/taskrunner/refine", "/api/taskrunner/refine", "refine", {"input": "x"}),
    (
        "post",
        "/api/taskrunner/refine/cancel",
        "/api/taskrunner/refine/cancel",
        "refine_cancel",
        None,
    ),
    (
        "post",
        "/api/taskrunner/refine/answer",
        "/api/taskrunner/refine/answer",
        "refine_answer",
        {"answer": "x"},
    ),
    ("delete", "/api/taskrunner/{task_id}", "/api/taskrunner/t1", "delete", None),
    ("patch", "/api/taskrunner/{task_id}/name", "/api/taskrunner/t1/name", "rename", {"name": "x"}),
    (
        "patch",
        "/api/taskrunner/{task_id}/tasks/{index}",
        "/api/taskrunner/t1/tasks/0",
        "update_task",
        {"title": "x"},
    ),
    (
        "post",
        "/api/taskrunner/{task_id}/retry",
        "/api/taskrunner/t1/retry",
        "retry",
        {"from_step": 1},
    ),
    ("post", "/api/taskrunner/{task_id}/pause", "/api/taskrunner/t1/pause", "pause", None),
    ("post", "/api/taskrunner/{task_id}/to-chat", "/api/taskrunner/t1/to-chat", "to_chat", None),
    (
        "post",
        "/api/taskrunner/{task_id}/execute",
        "/api/taskrunner/t1/execute",
        "execute_plan",
        {"auto_approve": True},
    ),
]


@pytest.mark.parametrize(
    ("method", "pattern", "url", "handler", "body"),
    _TASKRUNNER_ROUTES,
    ids=[row[3] for row in _TASKRUNNER_ROUTES],
)
async def test_taskrunner_route_refuses_non_owner(method, pattern, url, handler, body) -> None:
    from kiro_crew.dashboard.handlers import taskrunner

    app = web.Application(middlewares=[_non_owner])
    app["state"] = _GateOnlyState()
    app.router.add_route(method.upper(), pattern, getattr(taskrunner, f"api_taskrunner_{handler}"))
    async with _client(app) as client:
        await _assert_owner_only(await getattr(client, method)(url, json=body))


# ── memory: every durable-memory write ──

_MEMORY_ROUTES = [
    ("put", "/api/memory/preferences", "/api/memory/preferences", "preferences", {"content": "x"}),
    ("put", "/api/memory/projects", "/api/memory/projects", "projects", {"content": "x"}),
    ("put", "/api/memory/history", "/api/memory/history", "history", {"content": "x"}),
    ("put", "/api/memory/settings", "/api/memory/settings", "settings", {"history_idle_hours": 1}),
    (
        "put",
        "/api/memory/semantic",
        "/api/memory/semantic",
        "semantic_write",
        {"key": "pref.x", "value": "v"},
    ),
    (
        "delete",
        "/api/memory/semantic/{key:.+}",
        "/api/memory/semantic/pref.x",
        "semantic_delete",
        None,
    ),
    ("delete", "/api/memory/episodic/{id}", "/api/memory/episodic/e1", "episodic_delete", None),
    ("post", "/api/memory/migrate", "/api/memory/migrate", "migrate", None),
    ("post", "/api/memory/import", "/api/memory/import", "import", {"semantic": []}),
    ("post", "/api/memory/consolidate", "/api/memory/consolidate", "consolidate", {"key": "k"}),
    ("post", "/api/memory/promote", "/api/memory/promote", "promote", {}),
]


@pytest.mark.parametrize(
    ("method", "pattern", "url", "handler", "body"),
    _MEMORY_ROUTES,
    ids=[row[3] for row in _MEMORY_ROUTES],
)
async def test_memory_write_refuses_non_owner(
    monkeypatch, method, pattern, url, handler, body
) -> None:
    from kiro_crew.dashboard.handlers import memory

    # Three routes resolve their store before the write gate: a read that loads the
    # markdown tier, or the lesson-store identity check. Both are stubbed so the
    # strict state stays the witness, and the loaded document must see no call.
    document = MagicMock()
    monkeypatch.setattr(memory, "markdown_memory_for_store", AsyncMock(return_value=document))
    monkeypatch.setattr(memory, "resolve_lesson_memory_store", AsyncMock(return_value=("", None)))
    app = web.Application(middlewares=[_non_owner])
    app["state"] = _GateOnlyState()
    app.router.add_route(method.upper(), pattern, getattr(memory, f"api_memory_{handler}"))
    async with _client(app) as client:
        await _assert_owner_only(
            await getattr(client, method)(url, json=body, headers={"X-Session-Key": "dashboard:ui"})
        )
    assert document.mock_calls == []


# ── computer use, update, side turn ──


async def test_computer_use_config_refuses_non_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import computer_use

    write_state = MagicMock()
    monkeypatch.setattr(computer_use, "_write_state", write_state)
    app = web.Application(middlewares=[_non_owner])
    app["state"] = _GateOnlyState()
    app.router.add_put("/api/computer-use/config", computer_use.api_computer_use_config_save)
    async with _client(app) as client:
        await _assert_owner_only(
            await client.put("/api/computer-use/config", json={"enabled": True})
        )
    write_state.assert_not_called()


async def test_update_apply_refuses_non_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import updates
    from kiro_crew.platform import update_provider

    applied = AsyncMock(return_value=None)
    monkeypatch.setattr(update_provider, "apply_policy_update", applied)
    app = web.Application(middlewares=[_non_owner])
    app["state"] = _GateOnlyState()
    app.router.add_post("/api/update", updates.api_update_apply)
    async with _client(app) as client:
        await _assert_owner_only(await client.post("/api/update", json={}))
    applied.assert_not_awaited()


async def test_side_turn_refuses_non_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import side

    dispatch = MagicMock(return_value="run-1")
    monkeypatch.setattr(side, "_dispatch_side_turn", dispatch)
    slot = MagicMock(key="chat-1-1700000000", _app="")
    slot._side = SimpleNamespace(open=True, last_run_id="", is_complete=True, queue=[])
    app = web.Application(middlewares=[_non_owner])
    app["state"] = SimpleNamespace(owner_id=OWNER, _slots={"chat-1-1700000000": slot})
    app.router.add_post("/api/chat/slots/{slot}/side/turn", side.api_side_turn)
    async with _client(app) as client:
        await _assert_owner_only(
            await client.post("/api/chat/slots/chat-1-1700000000/side/turn", json={"question": "x"})
        )
    dispatch.assert_not_called()
