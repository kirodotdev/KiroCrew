"""The persistent-sandbox dashboard routes and the provider branch (flag, owner, ledger).

Routes are driven through a real aiohttp test client; the service is the loopback fake service.
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_engine import FakeRegistry, Row

from kiro_crew.managed_env import PROVISIONER_ID
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.routes import SERVICE_TEXT, SandboxRoutes, register_sandbox_routes

TOKEN = "tok-SECRET-0123456789"


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


@pytest.fixture
def env(bff, tmp_path, monkeypatch):
    """Flag on, owner allowed; returns (routes, registry, guard, bff)."""
    return _env(bff, tmp_path, monkeypatch)


def _env(bff, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import _shared

    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(lambda: TOKEN, guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock)
    reg = FakeRegistry()
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: reg)
    routes = SandboxRoutes(lambda: engine, run_in_thread=False)

    async def allow(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    return routes, reg, guard, bff


def own(bff, guard, reg=None, status="RUNNING", name="kc-aaaaaa", crew="kirocrew-main"):
    sid = bff.add_sandbox(status, name)
    guard.record_created(dict(bff.sandboxes[sid]), name)
    if reg is not None:
        reg.rows[f"row-{sid}"] = Row(f"row-{sid}", crew, f"crew-sbx-{sid}")
    return sid


def call(routes, method, path):
    async def go():
        app = web.Application()
        register_sandbox_routes(app, routes)
        async with TestClient(TestServer(app)) as c:
            r = await c.request(method, path)
            return r.status, await r.json()

    return asyncio.run(go())


# -- list ----------------------------------------------------------------------------


def test_list_merges_the_bff_list_with_registry_rows(env):
    routes, reg, guard, bff = env
    a = own(bff, guard, reg)
    b = own(bff, guard)  # live and ours, but no row
    reg.rows["row-gone"] = Row("row-gone", "old-crew", "crew-sbx-env-999")  # row, no service entry
    reg.rows["row-ds"] = Row("row-ds", "ds", "ds-1", provisioner_id="other_lane")
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 200 and body["error"] == ""
    by = {s["sandbox_id"]: s for s in body["sandboxes"]}
    assert set(by) == {a, b, "env-999"}
    assert by[a]["instance_id"] == f"row-{a}" and by[a]["name"] == "kirocrew-main"
    assert by[a]["status"] == "RUNNING" and by[a]["ssh_host"] == f"crew-sbx-{a}"
    assert by[b]["instance_id"] == ""
    assert by["env-999"]["status"] == "GONE" and by["env-999"]["instance_id"] == "row-gone"


def test_product_rules_list_shows_another_clients_sandbox_as_not_own_and_no_error(env):
    routes, reg, guard, bff = env
    reg.rows["row-1"] = Row("row-1", "c1", "crew-sbx-env-001")
    other = bff.add_sandbox("RUNNING", "somebody-else")
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 200 and body["error"] == ""
    # another client's box is listed too, marked not own
    got = [(i["sandbox_id"], i["status"], i["own"]) for i in body["sandboxes"]]
    assert got == [(other, "RUNNING", False)]
    assert body["sandboxes"][0]["display_name"] == "somebody-else" and "somebody-else" in str(body)
    assert guard.stopped == ""


def test_list_marks_own_and_other_clients_sandboxes(env):
    routes, reg, guard, bff = env
    mine = own(bff, guard, reg)
    other = bff.add_other("RUNNING")
    # registry only: GONE, and own
    reg.rows["row-gone"] = Row("row-gone", "old-crew", "crew-sbx-env-999")
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 200 and body["error"] == ""
    by = {s["sandbox_id"]: s for s in body["sandboxes"]}
    assert set(by) == {mine, "other-001", "env-999"} and other == "other-001"
    assert by[mine]["own"] is True and by["other-001"]["own"] is False
    assert by["env-999"]["own"] is True and by["env-999"]["status"] == "GONE"
    assert by["other-001"]["status"] == "RUNNING" and by["other-001"]["instance_id"] == ""
    n = len(bff.requests)
    for action in ("stop", "resume", "delete"):
        status, answer = call(routes, "POST", f"/api/managed-environments/{other}/{action}")
        assert status == 404 and answer == {"error": "not a sandbox of this crew client"}
    status, answer = call(routes, "POST", f"/api/managed-environments/{other}/ssh-session")
    assert status == 404 and answer == {"error": "not a sandbox of this crew client"}
    assert len(bff.requests) == n and bff.naming(other) == []
    assert routes.actions.get(other) == {}


def test_list_route_flag_off_and_not_owner_answer_before_any_bff_call(bff, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import _shared

    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    bff.add_other("RUNNING")
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    assert call(routes, "GET", "/api/managed-environments/list") == (404, {"error": "not found"})

    async def deny(request, operation):
        return web.json_response({"error": "owner_only"}, status=403)

    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    monkeypatch.setattr(_shared, "require_owner_dashboard_request", deny)
    assert call(routes, "GET", "/api/managed-environments/list") == (403, {"error": "owner_only"})
    assert bff.requests == []


def test_list_carries_no_token_or_tuple(env):
    routes, reg, guard, bff = env
    own(bff, guard, reg)
    _, body = call(routes, "GET", "/api/managed-environments/list")
    text = str(body)
    for secret in (TOKEN, "STREAMSECRET", "MGSSECRET", "streamUrl", "tokenValue"):
        assert secret not in text


# -- actions -------------------------------------------------------------------------


def test_stop_calls_the_client_for_an_own_id(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg)
    status, body = call(routes, "POST", f"/api/managed-environments/{sid}/stop")
    assert status == 202 and body == {"ok": True, "action": "stop"}
    assert bff.ops() == ["StopSandbox"]
    assert bff.sandboxes[sid]["status"] == "STOPPED"
    assert routes.actions.get(sid)["error"] == "" and not routes.actions.get(sid)["running"]


def test_resume_calls_resume_then_polls_to_running(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg, status="STOPPED")
    bff.get_statuses[sid] = deque(["PENDING", "RUNNING"])
    status, _ = call(routes, "POST", f"/api/managed-environments/{sid}/resume")
    assert status == 202
    assert bff.ops() == ["ResumeSandbox", "GetSandbox", "GetSandbox"]
    assert routes.actions.get(sid)["error"] == ""


def test_resume_that_never_runs_reports_the_error(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg, status="STOPPED")
    bff.get_statuses[sid] = deque(["FAILED"])
    call(routes, "POST", f"/api/managed-environments/{sid}/resume")
    assert "FAILED" in routes.actions.get(sid)["error"]


def test_resume_of_a_failed_sandbox_ends_at_once_with_the_resume_text(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg, status="STOPPED")
    for _ in range(40):  # the service's not-ready 409 for the whole window
        bff.queue_error("resume_409_text")
    bff.get_statuses[sid] = deque(["FAILED"])
    status, _ = call(routes, "POST", f"/api/managed-environments/{sid}/resume")
    assert status == 202
    assert routes.actions.get(sid)["error"] == f"Resume cannot start sandbox {sid}: it is FAILED"
    assert bff.ops() == ["ResumeSandbox", "GetSandbox"]  # no retry: no 60 s wait


def test_delete_reads_back_and_removes_the_row(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg)
    status, _ = call(routes, "POST", f"/api/managed-environments/{sid}/delete")
    assert status == 202
    assert bff.ops() == ["DeleteSandbox", "GetSandbox", "ListSandboxes"]
    assert reg.list() == []
    assert routes.actions.get(sid)["error"] == ""
    assert guard.ledger()[0].get("deletedAt")


def test_delete_that_does_not_read_back_says_so(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg)
    bff.queue("DeleteSandbox", 200, {"execEnvironmentId": sid, "status": "DELETING"})
    call(routes, "POST", f"/api/managed-environments/{sid}/delete")
    assert "Could not confirm" in routes.actions.get(sid)["error"]
    assert len(reg.list()) == 1


def test_an_id_not_in_the_ledger_is_refused_before_any_bff_call(env):
    routes, reg, guard, bff = env
    foreign = bff.add_sandbox("RUNNING", "somebody-else")
    for action in ("stop", "resume", "delete"):
        for sid in (foreign, "env-777", "signin", "..%2Fx"):
            status, _ = call(routes, "POST", f"/api/managed-environments/{sid}/{action}")
            assert status == 404
    assert bff.requests == []


def test_unknown_action_is_400_and_a_second_action_while_running_is_409(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg)
    assert call(routes, "POST", f"/api/managed-environments/{sid}/explode")[0] == 400
    assert routes.actions.begin(sid, "stop")
    assert call(routes, "POST", f"/api/managed-environments/{sid}/stop")[0] == 409
    assert bff.requests == []


def test_a_stopped_client_refuses_actions_with_the_reason(env):
    routes, reg, guard, bff = env
    sid = own(bff, guard, reg)
    guard.stop("test stop")
    call(routes, "POST", f"/api/managed-environments/{sid}/stop")
    assert routes.actions.get(sid)["error"] == SERVICE_TEXT["stopped"]  # the plain line
    assert bff.requests == []


# -- flag off, owner only, exposure --------------------------------------------------------


def _every_route():
    return [
        ("GET", "/api/managed-environments/list"),
        ("POST", "/api/managed-environments/env-001/stop"),
        ("POST", "/api/managed-environments/env-001/resume"),
        ("POST", "/api/managed-environments/env-001/delete"),
        ("POST", "/api/managed-environments/env-001/explode"),
    ]


def test_flag_off_every_route_is_404_and_nothing_else_runs(monkeypatch):
    from kiro_crew.dashboard.handlers import _shared

    asked = []

    async def spy(request, operation):
        asked.append(operation)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", spy)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)

    def no_engine():
        raise AssertionError("flag off must not build an engine")

    routes = SandboxRoutes(no_engine)
    every = list(_every_route())
    for method, path in every:
        assert call(routes, method, path)[0] == 404
    # The owner check runs first on every route (its answer is the route's answer); with
    # the flag off nothing after it runs.
    assert len(asked) == len(every)


def test_every_sandbox_route_is_owner_only(monkeypatch):
    from kiro_crew.dashboard.handlers import _shared

    asked = []

    async def deny(request, operation):
        asked.append(operation)
        return web.json_response({"error": "owner_only"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", deny)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)

    def no_engine():
        raise AssertionError("a denied request must not reach the engine")

    routes = SandboxRoutes(no_engine)
    for method, path in _every_route():
        assert call(routes, method, path)[0] == 403
    assert asked == [
        "sandboxes_list",
        "sandboxes_stop",
        "sandboxes_resume",
        "sandboxes_delete",
        "sandboxes_explode",
    ]


def test_the_dashboard_binds_loopback_by_default(monkeypatch, capsys):
    from kiro_crew.dashboard.urls import bind_address_for

    monkeypatch.delenv("KIROCREW_BIND", raising=False)
    addr = bind_address_for(True)
    print(addr)
    assert addr == "127.0.0.1"
    monkeypatch.setenv("KIROCREW_BIND", "not-an-ip")  # a typo only narrows back to loopback
    assert bind_address_for(True) == "127.0.0.1"


def _provider():
    from kiro_crew.managed_env.provider import ManagedEnvProvisionerProvider
    from kiro_crew.platform.defaults import DefaultRemoteProvisionerProvider

    base = DefaultRemoteProvisionerProvider()
    return base, ManagedEnvProvisionerProvider(base)


@pytest.mark.parametrize("on", [False, True])
def test_the_launch_row_and_its_engine_follow_the_lane_switch(monkeypatch, on):
    from kiro_crew.managed_env.engine import SandboxLaunchEngine

    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda cfg=None: on)
    base, provider = _provider()
    ids = [p.id for p in provider.provisioners()]
    assert [i for i in ids if i != PROVISIONER_ID] == [p.id for p in base.provisioners()]
    assert (PROVISIONER_ID in ids) is on
    if on:
        assert isinstance(provider.engine_for(PROVISIONER_ID), SandboxLaunchEngine)
    else:
        with pytest.raises(KeyError):
            provider.engine_for(PROVISIONER_ID)
