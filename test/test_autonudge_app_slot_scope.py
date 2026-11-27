"""An app token may run goal loops over ``/api/autonudge`` on its own chat slots only.

RFC ``docs/request-for-change/rfc-app-slot-goal-loops.md``: with ``permissions.chatSlots``
and the app enabled, start, update, fire and stop pass for a slot stamped with the app;
any other slot reads as missing, the owner's own calls are unchanged, every app loop is
time-bounded, and disabling the app stops its loops.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import permissions
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h

APP = "loop-probe"
OWN = f"{APP}-task-1"
OTHER_APP = f"other-{APP}"
CEILING = 7200


def _app_loops() -> Any:
    return importlib.import_module("kiro_crew.apps.app_loops")


def _slot(key: str, app: str | None) -> SimpleNamespace:
    slot = SimpleNamespace(key=key, running=False, mode="chat", workspace="default")
    if app is not None:
        slot._app = app
    return slot


class _State:
    def __init__(self) -> None:
        self.slots = {
            OWN: _slot(OWN, APP),
            "foreign-1": _slot("foreign-1", OTHER_APP),
            "chat-1-111": _slot("chat-1-111", None),
        }

    def get_slot(self, key: str) -> Any:
        return self.slots.get(key)


def _loop(loop_id: str, slot_key: str) -> NudgeLoop:
    return NudgeLoop(id=loop_id, slot_key=slot_key, message="go")


@pytest.fixture
def svc() -> MagicMock:
    loops = {
        "own": _loop("own", OWN),
        "foreign": _loop("foreign", "foreign-1"),
        "unscoped": _loop("unscoped", "chat-1-111"),
    }
    service = MagicMock()
    service.get_by_id = lambda loop_id: loops.get(loop_id)
    service.remove = AsyncMock(return_value=None)
    service.fire_now = AsyncMock(side_effect=lambda loop_id: (loops[loop_id], "", 200))
    service.release_approval_hold = AsyncMock(return_value=None)
    return service


@pytest.fixture
def calls(monkeypatch: Any, svc: MagicMock) -> dict[str, list[dict[str, Any]]]:
    seen: dict[str, list[dict[str, Any]]] = {"add": [], "update": []}

    async def _add(**kwargs: Any) -> Any:
        seen["add"].append(kwargs)
        return _loop("new", kwargs["slot_key"]), None, 200

    async def _update(**kwargs: Any) -> Any:
        seen["update"].append(kwargs)
        return svc.get_by_id(kwargs["loop_id"]), None, 200

    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
    monkeypatch.setattr(h, "authorize_and_add_nudge", _add)
    monkeypatch.setattr(h, "authorize_and_update_nudge", _update)
    monkeypatch.setattr(h, "runtime_ceiling_secs", lambda: CEILING)  # flake-ok: not a wait
    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: CEILING)
    monkeypatch.setattr(h, "sel", MagicMock())
    monkeypatch.setattr("kiro_crew.dashboard.slot_ownership.audit_app_slot_denial", MagicMock())
    monkeypatch.setattr(h, "chat_slots_denial_cause", lambda app: "", raising=False)
    return seen


def _client(app_name: str) -> TestClient:
    @web.middleware
    async def identity(request: web.Request, handler: Any) -> Any:
        request["user"] = "local-app"
        request["app"] = app_name
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = _State()
    app.router.add_post("/api/autonudge", h.api_autonudge_start)
    app.router.add_patch("/api/autonudge/{loop_id}", h.api_autonudge_update)
    app.router.add_delete("/api/autonudge/{loop_id}", h.api_autonudge_delete)
    app.router.add_post("/api/autonudge/{loop_id}/fire", h.api_autonudge_fire)
    return TestClient(TestServer(app))


# ── granted app, own slot ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_granted_app_starts_a_loop_on_its_own_slot(calls: Any) -> None:
    async with _client(APP) as client:
        resp = await client.post("/api/autonudge", json={"slot_key": OWN, "message": "work"})
        assert resp.status == 200, await resp.text()
    (armed,) = calls["add"]
    assert armed["slot_key"] == OWN
    assert armed["source"] == f"app:{APP}"
    assert armed["max_runtime_secs"] == CEILING


@pytest.mark.asyncio
async def test_a_granted_app_updates_fires_and_stops_its_own_loop(
    calls: Any, svc: MagicMock
) -> None:
    async with _client(APP) as client:
        assert (await client.patch("/api/autonudge/own", json={"idle_secs": 300})).status == 200
        assert (await client.post("/api/autonudge/own/fire")).status == 200
        assert (await client.delete("/api/autonudge/own")).status == 200
    assert calls["update"][0]["source"] == f"app:{APP}"
    svc.fire_now.assert_awaited_once_with("own")
    svc.remove.assert_awaited_once()


# ── granted app, any other slot ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_key", ["foreign-1", "chat-1-111", "no-such-slot", ""])
async def test_a_slot_the_app_does_not_own_reads_as_missing(calls: Any, slot_key: str) -> None:
    async with _client(APP) as client:
        resp = await client.post("/api/autonudge", json={"slot_key": slot_key, "message": "x"})
        assert resp.status == 404
        assert (await resp.json())["code"] == "slot_not_found"
    assert calls["add"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("loop_id", ["foreign", "unscoped", "missing"])
async def test_an_app_cannot_touch_a_loop_on_another_slot(
    calls: Any, svc: MagicMock, loop_id: str
) -> None:
    async with _client(APP) as client:
        for resp in (
            await client.patch(f"/api/autonudge/{loop_id}", json={"idle_secs": 300}),
            await client.post(f"/api/autonudge/{loop_id}/fire"),
            await client.delete(f"/api/autonudge/{loop_id}"),
        ):
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
    assert calls["update"] == []
    svc.fire_now.assert_not_awaited()
    svc.remove.assert_not_awaited()


# ── the grant ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["chat_slots_not_declared", "disabled"])
async def test_without_the_grant_every_write_is_403(
    calls: Any, svc: MagicMock, monkeypatch: Any, cause: str
) -> None:
    monkeypatch.setattr(h, "chat_slots_denial_cause", lambda app: cause, raising=False)
    async with _client(APP) as client:
        for resp in (
            await client.post("/api/autonudge", json={"slot_key": OWN, "message": "x"}),
            await client.patch("/api/autonudge/own", json={"idle_secs": 300}),
            await client.post("/api/autonudge/own/fire"),
            await client.delete("/api/autonudge/own"),
        ):
            assert resp.status == 403
            body = await resp.json()
            assert body["code"] == "dashboard_owner_required"
            assert body["cause"] == cause
    assert calls["add"] == [] and calls["update"] == []
    svc.fire_now.assert_not_awaited()
    svc.remove.assert_not_awaited()


def test_the_grant_is_read_from_the_live_manifest_and_record(monkeypatch: Any) -> None:
    manifest = SimpleNamespace(permissions=SimpleNamespace(chatSlots=True))
    record = SimpleNamespace(enabled=True)
    monkeypatch.setattr(permissions, "get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(permissions, "_read_installed", lambda name, manifest=None: record)
    monkeypatch.setattr(permissions, "is_app_enabled", lambda name: True)
    cause = getattr(permissions, "chat_slots_denial_cause", None)
    assert cause is not None, "no live chatSlots grant check"
    assert cause(APP) == ""
    record.enabled = False
    assert cause(APP) == "disabled"
    manifest.permissions.chatSlots = False
    assert cause(APP) == "chat_slots_not_declared"


# ── fields and bounds ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("gate", True), ("judge", {"wake_when": "x"})])
async def test_an_app_may_not_send_the_owners_fields(calls: Any, field: str, value: Any) -> None:
    async with _client(APP) as client:
        resp = await client.post(
            "/api/autonudge", json={"slot_key": OWN, "message": "x", field: value}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_field"
    assert calls["add"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [0, CEILING + 1])
async def test_an_app_loop_must_end_within_the_owners_ceiling(calls: Any, runtime: int) -> None:
    async with _client(APP) as client:
        start = await client.post(
            "/api/autonudge", json={"slot_key": OWN, "message": "x", "max_runtime_secs": runtime}
        )
        update = await client.patch("/api/autonudge/own", json={"max_runtime_secs": runtime})
        assert start.status == 400 and update.status == 400
    assert calls["add"] == [] and calls["update"] == []


@pytest.mark.asyncio
async def test_the_owner_is_unchanged(calls: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(h, "is_owner_dashboard_request", lambda request: True)
    async with _client("") as client:
        resp = await client.post(
            "/api/autonudge",
            json={"slot_key": "chat-1-111", "message": "x", "max_runtime_secs": 0, "gate": True},
        )
        assert resp.status == 200, await resp.text()
    (armed,) = calls["add"]
    assert armed["source"] == "dashboard"
    assert armed["max_runtime_secs"] == 0


def test_the_sdk_arm_bounds_the_runtime_the_same_way(monkeypatch: Any) -> None:
    import asyncio

    from kiro_crew.apps.chat_slot_sdk import ChatSlotSDK

    seen: list[dict[str, Any]] = []

    async def _add(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return "loop", None, 200

    monkeypatch.setattr("kiro_crew.autonudge_authz.authorize_and_add_nudge", _add)
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: MagicMock())
    monkeypatch.setattr("kiro_crew.monitoring.limits.runtime_ceiling_secs", lambda: CEILING)
    monkeypatch.setattr(ChatSlotSDK, "_require_live", lambda self, state, slot: None)
    sdk = ChatSlotSDK(APP)
    slot = _slot(OWN, APP)

    def arm(**extra: Any) -> Any:
        return asyncio.run(
            sdk.arm_nudge(None, slot, message="go", idle_secs=60, max_cycles=0, **extra)
        )

    assert arm() == ("loop", None, 200)
    assert seen[-1]["max_runtime_secs"] == CEILING
    for bad in (0, CEILING + 1):
        loop, error, status = arm(max_runtime_secs=bad)
        assert loop is None and error and status == 400
    assert len(seen) == 1


# ── disabling the app stops its loops ───────────────────────────────────────


def _loop_service(*loops: NudgeLoop) -> MagicMock:
    service = MagicMock()
    service.list_all = lambda: list(loops)

    async def _update(loop_id: str, **changes: Any) -> Any:
        for loop in loops:
            if loop.id == loop_id:
                loop.active = changes["active"]
                loop.stopped_reason = changes["stopped_reason"]
                return loop
        return None

    service.update = AsyncMock(side_effect=_update)
    return service


@pytest.mark.asyncio
async def test_disabling_the_app_stops_only_the_loops_on_its_slots(monkeypatch: Any) -> None:
    monkeypatch.setattr("kiro_crew.sel.sel", MagicMock())
    own, foreign, unscoped = (
        _loop("own", OWN),
        _loop("foreign", "foreign-1"),
        _loop("unscoped", "chat-1-111"),
    )
    service = _loop_service(own, foreign, unscoped)
    stopped = await _app_loops().stop_app_loops(APP, service=service, state=_State())
    assert stopped == 1
    assert own.active is False and own.stopped_reason == "app_disabled"
    assert foreign.active is True and unscoped.active is True


@pytest.mark.asyncio
async def test_the_disable_request_stops_the_loops_before_the_apps_hook(monkeypatch: Any) -> None:
    from kiro_crew.apps import teardown

    own = _loop("own", OWN)
    service = _loop_service(own, _loop("foreign", "foreign-1"))
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: service)
    monkeypatch.setattr("kiro_crew.slack.handler.get_dashboard_state", lambda: _State())
    monkeypatch.setattr("kiro_crew.sel.sel", MagicMock())
    monkeypatch.setattr(teardown, "revoke_worker_auto_approve", lambda app: None)
    seen_at_hook: list[bool] = []

    async def _hook(app: str) -> None:
        seen_at_hook.append(own.active)

    teardown.register_app_disable_hook(APP, _hook)
    try:
        await teardown.notify_app_disabled(APP)
    finally:
        teardown.unregister_app_disable_hook(APP)
    assert seen_at_hook == [False], "the app's hook ran while its loop was still active"
    assert own.stopped_reason == "app_disabled"


@pytest.mark.asyncio
async def test_the_disable_route_stops_the_loops_with_its_own_state(monkeypatch: Any) -> None:
    from kiro_crew.apps import teardown

    own = _loop("own", OWN)
    service = _loop_service(own)
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: service)
    monkeypatch.setattr("kiro_crew.slack.handler.get_dashboard_state", lambda: None)
    monkeypatch.setattr("kiro_crew.sel.sel", MagicMock())
    monkeypatch.setattr(teardown, "revoke_worker_auto_approve", lambda app: None)
    await teardown.notify_app_disabled(APP, state=_State())
    assert own.active is False, "a gateway with no Slack left the disabled app's loop running"
    assert own.stopped_reason == "app_disabled"


def test_the_fire_backstop_judges_the_slots_app_record(monkeypatch: Any) -> None:
    records = {APP: SimpleNamespace(enabled=False), OTHER_APP: SimpleNamespace(enabled=True)}
    monkeypatch.setattr(
        "kiro_crew.apps.manager._read_installed", lambda name, **_: records.get(name)
    )
    assert _app_loops().app_disabled_for_slot(_slot(OWN, APP)) is True
    assert _app_loops().app_disabled_for_slot(_slot("foreign-1", OTHER_APP)) is False
    assert _app_loops().app_disabled_for_slot(_slot("chat-1-111", None)) is False
    assert _app_loops().app_disabled_for_slot(_slot("gone", "uninstalled-app")) is False


# ── audit names the app ─────────────────────────────────────────────────────


def _sources(sel_mock: MagicMock, tool: str) -> list[str]:
    return [
        c.kwargs["source"]
        for c in sel_mock.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("tool_name") == tool
    ]


@pytest.mark.asyncio
async def test_fire_and_stop_are_audited_under_the_app(calls: Any, monkeypatch: Any) -> None:
    sel_mock = MagicMock()
    monkeypatch.setattr(h, "sel", sel_mock)
    async with _client(APP) as client:
        assert (await client.post("/api/autonudge/own/fire")).status == 200
        assert (await client.delete("/api/autonudge/own")).status == 200
    assert set(_sources(sel_mock, "autonudge_fire")) == {f"app:{APP}"}
    assert _sources(sel_mock, "autonudge_delete") == [f"app:{APP}"]


@pytest.mark.asyncio
async def test_a_refused_fire_is_audited_under_the_app(calls: Any, monkeypatch: Any) -> None:
    sel_mock = MagicMock()
    monkeypatch.setattr(h, "sel", sel_mock)
    async with _client(APP) as client:
        assert (await client.post("/api/autonudge/missing/fire")).status == 404
    assert _sources(sel_mock, "autonudge_fire") == [f"app:{APP}"]


@pytest.mark.asyncio
async def test_a_failed_stop_is_audited_under_the_app(
    calls: Any, svc: MagicMock, monkeypatch: Any
) -> None:
    from kiro_crew.autonudge_service.model import NudgeAdmissionRefused

    sel_mock = MagicMock()
    monkeypatch.setattr(h, "sel", sel_mock)
    svc.remove = AsyncMock(side_effect=NudgeAdmissionRefused("the session changed"))
    async with _client(APP) as client:
        assert (await client.delete("/api/autonudge/own")).status >= 400
    assert _sources(sel_mock, "autonudge_delete") == [f"app:{APP}"]


# ── structured monitors on the app's slots ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("loop_id,status", [("own", 200), ("foreign", 404)])
async def test_stopping_a_structured_monitor_is_scoped_to_the_apps_slot(
    calls: Any, svc: MagicMock, monkeypatch: Any, loop_id: str, status: int
) -> None:
    stops: list[dict[str, Any]] = []

    async def _stop(**kwargs: Any) -> Any:
        stops.append(kwargs)
        return None, None, 200

    monkeypatch.setattr(h, "is_structured_monitor_loop", lambda loop: True)
    monkeypatch.setattr(h, "authorize_and_stop_monitor", _stop)
    svc.get_by_id(loop_id).monitor = SimpleNamespace(outcome=None)
    async with _client(APP) as client:
        assert (await client.delete(f"/api/autonudge/{loop_id}")).status == status
    assert [s["source"] for s in stops] == ([f"app:{APP}"] if status == 200 else [])


# ── the fire-time backstop ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fire_on_a_disabled_apps_slot_stops_the_loop_and_sends_nothing(
    monkeypatch: Any,
) -> None:
    from unittest.mock import patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    slot = _slot(OWN, APP)
    slot.is_closing = False
    slot.memory_mode = "persistent"
    slot.window_pending = False
    orch.dashboard_state = SimpleNamespace(get_slot=lambda key: slot, _background_tasks=set())
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.update = AsyncMock(return_value=None)
    orch._session_tasks = {}
    monkeypatch.setattr(
        "kiro_crew.apps.manager._read_installed",
        lambda name, **_: SimpleNamespace(enabled=False) if name == APP else None,
    )
    spawn = MagicMock()
    monkeypatch.setattr(gw, "spawn_guarded_turn", spawn)
    assert await orch._fire_dashboard_nudge(_loop("own", OWN)) is False
    orch.autonudge_svc.update.assert_awaited_once_with(
        "own", active=False, stopped_reason="app_disabled"
    )
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_clearing_a_finished_monitor_on_the_apps_slot_is_audited_under_the_app(
    calls: Any, svc: MagicMock, monkeypatch: Any
) -> None:
    clears: list[dict[str, Any]] = []

    async def _clear(**kwargs: Any) -> Any:
        clears.append(kwargs)
        return None, None, 200

    monkeypatch.setattr(h, "is_structured_monitor_loop", lambda loop: True)
    monkeypatch.setattr(h, "authorize_and_clear_monitor", _clear)
    svc.get_by_id("own").monitor = SimpleNamespace(outcome="done")
    async with _client(APP) as client:
        assert (await client.delete("/api/autonudge/own?intent=clear")).status == 200
    assert [c["source"] for c in clears] == [f"app:{APP}"]


def _fire_orchestrator(slot: Any) -> Any:
    from unittest.mock import patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.slack import gateway as gw

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    slot.is_closing = False
    slot.memory_mode = "persistent"
    slot.window_pending = False
    orch.autonudge_svc = MagicMock()
    orch._session_tasks = {}
    return orch


@pytest.mark.asyncio
async def test_the_fire_time_stop_is_audited_under_the_app(monkeypatch: Any) -> None:
    from kiro_crew.slack import gateway as gw

    slot = _slot(OWN, APP)
    orch = _fire_orchestrator(slot)
    orch.dashboard_state = SimpleNamespace(get_slot=lambda key: slot, _background_tasks=set())
    loop = _loop("own", OWN)
    orch.autonudge_svc.update = AsyncMock(return_value=loop)
    monkeypatch.setattr(
        "kiro_crew.apps.manager._read_installed",
        lambda name, **_: SimpleNamespace(enabled=False) if name == APP else None,
    )
    monkeypatch.setattr(gw, "spawn_guarded_turn", MagicMock())
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", sel_mock)
    assert await orch._fire_dashboard_nudge(loop) is False
    assert _sources(sel_mock, "autonudge_stop") == [f"app:{APP}"]
    (call,) = sel_mock.return_value.log_tool_invocation.call_args_list
    assert call.kwargs["metadata"] == {"loop_id": "own", "stopped_reason": "app_disabled"}


@pytest.mark.asyncio
async def test_a_turn_queued_before_the_disable_does_not_run(monkeypatch: Any) -> None:
    from kiro_crew.dashboard import chat, session_deliver
    from kiro_crew.slack import gateway as gw

    slot = _slot(OWN, APP)
    orch = _fire_orchestrator(slot)

    async def _admit(turn_slot: Any, coro: Any) -> Any:
        return await coro

    orch.dashboard_state = SimpleNamespace(
        get_slot=lambda key: slot,
        _background_tasks=set(),
        run_background_turn=_admit,
        push_slots_update=lambda: None,
    )
    monkeypatch.setattr(orch, "_dashboard_mode_admits", AsyncMock(return_value=True))
    monkeypatch.setattr(orch, "_monitor_completion_hook", lambda loop: None)
    monkeypatch.setattr(session_deliver, "write_origin_row", lambda *a, **k: None)
    enabled = {APP: True}
    monkeypatch.setattr(
        "kiro_crew.apps.manager._read_installed",
        lambda name, **_: SimpleNamespace(enabled=enabled[name]) if name in enabled else None,
    )
    queued: list[Any] = []
    monkeypatch.setattr(
        gw, "spawn_guarded_turn", lambda state, turn_slot, coro: queued.append(coro) or MagicMock()
    )
    run_chat = AsyncMock()
    monkeypatch.setattr(chat, "_run_chat", run_chat)
    unstarted: list[Any] = []
    monkeypatch.setattr(gw, "mark_turn_unstarted", unstarted.append)

    loop = _loop("own", OWN)
    await orch._start_dashboard_wake_turn(loop, slot, None, "nudge", "nudge", None, 0)
    assert len(queued) == 1, "the wake turn was not queued"
    enabled[APP] = False  # the owner disables the app while the turn waits for admission
    await queued[0]
    run_chat.assert_not_awaited()
    assert unstarted == [slot]
