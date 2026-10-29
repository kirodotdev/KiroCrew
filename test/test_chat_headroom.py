"""Remote-dispatch headroom (RFC conductor-remote-dispatch §2b, wave 3).

``GET /api/chat/headroom`` answers this crew's snapshot to the owner, a
``work_item`` create on a short crew is refused ``crew_busy`` and creates
nothing, and every create reply carries ``headroom``. All of it sits behind
``work_ledger.remote_dispatch``: off, the route is 404 and the create is
unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import resource_status
from kiro_crew import subagent as subagent_facade
from kiro_crew.config.section_builders import _build_work_ledger_config
from kiro_crew.dashboard import remote_dispatch_headroom as headroom_mod

HEADROOM_FIELDS = {
    "mem_free_gb",
    "cpu_count",
    "load_per_cpu",
    "posture",
    "disk_free_gb",
    "open_workers",
    "measured_at",
}

_REAL_CHECK_MEMORY = subagent_facade.check_memory_available

WORK_ITEM = {"item_id": "it_1", "title": "t", "acceptance": {"kind": "manual"}, "decision": ""}


def _status(**overrides) -> resource_status.ResourceStatus:
    fields = {
        "available_gb": 32.0,
        "cpu_count": 8,
        "load_per_cpu": 0.25,
        "posture": resource_status.POSTURE_AMPLE,
        "pressure_gb": 4.0,
        "critical_gb": 2.0,
    }
    fields.update(overrides)
    return resource_status.ResourceStatus(**fields)


@pytest.fixture
def host(monkeypatch):
    """A stub host with ample everything; tests turn one resource short."""
    reading = SimpleNamespace(status=_status(), memory_ok=True, cap=4, running=1, disk_gb=500.0)
    monkeypatch.setattr(resource_status, "probe", lambda cfg=None: reading.status)
    monkeypatch.setattr(
        subagent_facade, "check_memory_available", lambda min_gb=2.0, **_: (reading.memory_ok, 1.0)
    )
    monkeypatch.setattr(resource_status, "adaptive_exec_cap", lambda: reading.cap)
    monkeypatch.setattr(headroom_mod, "_running_agents", lambda: reading.running)
    monkeypatch.setattr(headroom_mod, "_disk_free_gb", lambda: reading.disk_gb)
    return reading


def _flag(monkeypatch, on: bool, *, memory_gb: float = 2.0, disk_gb: float = 10.0) -> list:
    """Set the preview switch as the config read reports it; returns the read log."""
    calls: list = []
    settings = headroom_mod.RemoteDispatchSettings(on, memory_gb, disk_gb)

    def read() -> headroom_mod.RemoteDispatchSettings:
        calls.append(1)
        return settings if on else headroom_mod._DISABLED

    monkeypatch.setattr(headroom_mod, "remote_dispatch_settings", read)
    return calls


def _config_load(monkeypatch, **work_ledger) -> None:
    from kiro_crew.config import loader

    section = _build_work_ledger_config(work_ledger)
    monkeypatch.setattr(
        loader.KiroCrewConfig, "load", staticmethod(lambda: SimpleNamespace(work_ledger=section))
    )


def _app(state, *, app_name: str = "", user: str = "local-app") -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_create

    def as_caller(handler):
        async def wrapped(request: web.Request) -> web.Response:
            request["app"] = app_name
            request["user"] = user
            return await handler(request)

        return wrapped

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/headroom", as_caller(headroom_mod.api_chat_headroom))
    app.router.add_post("/api/chat/slots", as_caller(api_chat_slot_create))
    return app


# -- config -----------------------------------------------------------------


def test_config_defaults_are_off_with_the_rfc_estimates():
    cfg = _build_work_ledger_config({})
    assert cfg.remote_dispatch is False
    assert cfg.remote_dispatch_memory_gb == 2.0
    assert cfg.remote_dispatch_disk_gb == 10.0


@pytest.mark.parametrize("raw", ["true", 1, None, "yes"])
def test_only_a_real_true_turns_the_preview_on(raw):
    assert _build_work_ledger_config({"remote_dispatch": raw}).remote_dispatch is False
    assert _build_work_ledger_config({"remote_dispatch": True}).remote_dispatch is True


@pytest.mark.parametrize("raw", [0, -1, "big", True, float("nan")])
def test_a_bad_estimate_uses_the_default(raw):
    cfg = _build_work_ledger_config(
        {"remote_dispatch_memory_gb": raw, "remote_dispatch_disk_gb": raw}
    )
    assert (cfg.remote_dispatch_memory_gb, cfg.remote_dispatch_disk_gb) == (2.0, 10.0)


def test_estimates_and_depth_read_together():
    cfg = _build_work_ledger_config(
        {"max_depth": 3, "remote_dispatch_memory_gb": 4, "remote_dispatch_disk_gb": 20.5}
    )
    assert (cfg.max_depth, cfg.remote_dispatch_memory_gb, cfg.remote_dispatch_disk_gb) == (
        3,
        4.0,
        20.5,
    )


def test_an_unloadable_config_reads_as_off(monkeypatch):
    from kiro_crew.config import loader

    def boom():
        raise RuntimeError("config broke")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(boom))
    assert headroom_mod.remote_dispatch_settings().enabled is False


def test_settings_read_the_config_keys(monkeypatch):
    _config_load(monkeypatch)
    assert headroom_mod.remote_dispatch_settings().enabled is False
    _config_load(
        monkeypatch,
        remote_dispatch=True,
        remote_dispatch_memory_gb=3,
        remote_dispatch_disk_gb=40,
    )
    assert headroom_mod.remote_dispatch_settings() == headroom_mod.RemoteDispatchSettings(
        True, 3.0, 40.0
    )


# -- flag off: nothing changes ---------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_route_is_404_and_create_is_unchanged(tmp_path, monkeypatch, host):
    _flag(monkeypatch, False)
    host.memory_ok = False  # short, but the preview is off: nothing is gated
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.get("/api/chat/headroom")
        assert resp.status == 404
        resp = await client.post("/api/chat/slots", json={"name": "w1", "work_item": WORK_ITEM})
        assert resp.status == 200
        assert "headroom" not in await resp.json()
        resp = await client.post("/api/chat/slots", json={"name": "plain"})
        assert "headroom" not in await resp.json()
    assert {"w1", "plain"} <= set(state._slots)


# -- the route --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_answers_the_snapshot_to_the_owner(tmp_path, monkeypatch, host):
    _flag(monkeypatch, True)
    async with TestClient(TestServer(_app(_make_state(tmp_path)))) as client:
        resp = await client.get("/api/chat/headroom")
        assert resp.status == 200
        body = await resp.json()
    assert set(body) == HEADROOM_FIELDS
    assert body["mem_free_gb"] == 32.0
    assert body["cpu_count"] == 8
    assert body["load_per_cpu"] == 0.25
    assert body["posture"] == "ample"
    assert body["disk_free_gb"] == 500.0
    assert body["open_workers"] == 0
    assert isinstance(body["measured_at"], float)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [{"app_name": "notes"}, {"user": ""}, {"user": "someone-else"}])
async def test_route_refuses_any_other_caller(tmp_path, monkeypatch, host, caller):
    _flag(monkeypatch, True)
    state = _make_state(tmp_path)
    state.owner_id = "the-owner"
    async with TestClient(TestServer(_app(state, **caller))) as client:
        resp = await client.get("/api/chat/headroom")
        assert resp.status in (401, 403)
        assert set(await resp.json()).isdisjoint(HEADROOM_FIELDS)


@pytest.mark.asyncio
async def test_flag_is_read_on_every_request(tmp_path, monkeypatch, host):
    calls = _flag(monkeypatch, True)
    async with TestClient(TestServer(_app(_make_state(tmp_path)))) as client:
        await client.get("/api/chat/headroom")
        await client.get("/api/chat/headroom")
    assert len(calls) == 2


def test_route_is_registered_on_the_dashboard():
    from kiro_crew.dashboard.routes import chat as chat_routes

    app = web.Application()
    chat_routes.register(app)
    handlers = {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
    }
    assert handlers[("GET", "/api/chat/headroom")] is headroom_mod.api_chat_headroom


# -- the admission gate ------------------------------------------------------------


def _make_short(host, resource: str) -> None:
    if resource == "memory-check":
        host.memory_ok = False
    elif resource == "memory-posture":
        host.status = _status(posture=resource_status.POSTURE_CRITICAL)
    elif resource == "cpu-load":
        host.status = _status(load_per_cpu=1.0)
    elif resource == "cpu-cap":
        host.cap, host.running = 4, 4
    elif resource == "disk":
        host.disk_gb = 9.99


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("how", "short"),
    [
        ("memory-check", "memory"),
        ("memory-posture", "memory"),
        ("cpu-load", "cpu"),
        ("cpu-cap", "cpu"),
        ("disk", "disk"),
    ],
)
async def test_short_work_item_create_is_crew_busy_and_creates_nothing(
    tmp_path, monkeypatch, host, how, short
):
    _flag(monkeypatch, True)
    _make_short(host, how)
    state = _make_state(tmp_path)
    before = set(state._slots)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"name": "w1", "work_item": WORK_ITEM})
        assert resp.status == 503
        body = await resp.json()
    assert body["error"] == body["code"] == "crew_busy"
    assert body["short"] == short
    assert set(body["headroom"]) == HEADROOM_FIELDS
    assert set(state._slots) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["memory-check", "cpu-load", "disk"])
async def test_create_without_work_item_is_not_gated(tmp_path, monkeypatch, host, how):
    _flag(monkeypatch, True)
    _make_short(host, how)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"name": "plain"})
        assert resp.status == 200
        body = await resp.json()
    assert set(body["headroom"]) == HEADROOM_FIELDS
    assert "plain" in state._slots


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [{"app_name": "notes"}, {"user": "someone-else"}])
async def test_a_non_owner_create_is_neither_gated_nor_shown_headroom(
    tmp_path, monkeypatch, host, caller
):
    _flag(monkeypatch, True)
    host.memory_ok = False
    state = _make_state(tmp_path)
    state.owner_id = "the-owner"
    async with TestClient(TestServer(_app(state, **caller))) as client:
        resp = await client.post("/api/chat/slots", json={"name": "x", "work_item": WORK_ITEM})
        body = await resp.json()
    assert body.get("error") != "crew_busy"
    assert "headroom" not in body


def test_disk_just_under_the_estimate_is_short(monkeypatch, host):
    """The gate compares the unrounded reading: 9.996 GB is under 10, not 10.0."""
    host.disk_gb = 9.996
    snap, short = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert short == "disk"
    assert snap["disk_free_gb"] == 10.0


@pytest.mark.parametrize(("saturating", "short"), [(3, ""), (4, "cpu")])
def test_the_execution_cap_is_full_at_one_admission_point(monkeypatch, saturating, short):
    """``saturating`` (the busiest point) is compared, not running summed over points."""
    from kiro_crew.adaptive import controller

    monkeypatch.setattr(resource_status, "probe", lambda cfg=None: _status())
    monkeypatch.setattr(
        subagent_facade, "check_memory_available", lambda min_gb=2.0, **_: (True, 9.0)
    )
    monkeypatch.setattr(headroom_mod, "_disk_free_gb", lambda: 500.0)
    state = {
        "enabled": True,
        "effective_exec_cap": 4,
        "last_sample": {"running": 6, "saturating": saturating},
    }
    monkeypatch.setattr(controller, "current_state", lambda: state)
    _, got = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert got == short


@pytest.mark.parametrize(
    "state",
    [
        {"enabled": False, "exec_ceiling": 4, "last_sample": {"saturating": 9}},
        {"enabled": True, "effective_exec_cap": 4, "last_sample": {"saturating": -1}},
        {"enabled": True, "effective_exec_cap": 4, "last_sample": None},
    ],
    ids=["disabled-stale-sample", "unmeasured", "no-sample"],
)
def test_an_unusable_occupancy_never_refuses(monkeypatch, state):
    """A disabled controller stops sampling, so its last sample is stale."""
    from kiro_crew.adaptive import controller

    monkeypatch.setattr(resource_status, "probe", lambda cfg=None: _status())
    monkeypatch.setattr(
        subagent_facade, "check_memory_available", lambda min_gb=2.0, **_: (True, 9.0)
    )
    monkeypatch.setattr(headroom_mod, "_disk_free_gb", lambda: 500.0)
    monkeypatch.setattr(controller, "current_state", lambda: state)
    _, got = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert got == ""


@pytest.mark.asyncio
async def test_admitted_work_item_create_carries_headroom(tmp_path, monkeypatch, host):
    _flag(monkeypatch, True)
    state = _make_state(tmp_path)
    # A create that is admitted also binds, so it carries a work item the binding
    # accepts: a hub conductor, a store-shaped id, and a new unnamed slot.
    bindable = {**WORK_ITEM, "item_id": "it_00000001", "conductor_key": "chat-hub"}
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"work_item": bindable})
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert set(body["headroom"]) == HEADROOM_FIELDS
    assert body["key"] in state._slots


@pytest.mark.asyncio
async def test_estimates_come_from_config(tmp_path, monkeypatch, host):
    _flag(monkeypatch, True, disk_gb=600.0)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"name": "w1", "work_item": WORK_ITEM})
        assert (await resp.json())["short"] == "disk"
    seen: list[float] = []

    def check(min_gb=2.0, **_):
        seen.append(min_gb)
        return True, 1.0

    monkeypatch.setattr(subagent_facade, "check_memory_available", check)
    headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 7.5, 10.0))
    assert seen == [7.5]


# -- nearby inputs ------------------------------------------------------------------


def test_probe_raising_fails_open_with_unknown_fields(monkeypatch, host):
    def boom(cfg=None):
        raise RuntimeError("probe broke")

    monkeypatch.setattr(resource_status, "probe", boom)
    snap, short = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert short == ""
    assert snap["mem_free_gb"] is None and snap["cpu_count"] is None
    assert snap["load_per_cpu"] is None and snap["posture"] == "unknown"


def test_unknown_readings_never_refuse(monkeypatch, host):
    host.status = _status(available_gb=-1.0, load_per_cpu=None, posture="unknown")
    host.cap, host.running, host.disk_gb = 0, None, None
    snap, short = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert short == ""
    assert snap["mem_free_gb"] is None and snap["disk_free_gb"] is None


def test_missing_scratch_root_measures_the_nearest_existing_parent(tmp_path, monkeypatch):
    from kiro_crew import agent_scratch

    monkeypatch.setattr(agent_scratch, "scratch_root", lambda: tmp_path / "no" / "such" / "dir")
    seen: list = []
    real = headroom_mod.shutil.disk_usage

    def usage(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(headroom_mod.shutil, "disk_usage", usage)
    free = headroom_mod._disk_free_gb()
    assert seen == [tmp_path]
    assert isinstance(free, float) and free >= 0


def test_disk_read_failure_is_unknown(monkeypatch):
    def boom(path):
        raise OSError("gone")

    monkeypatch.setattr(headroom_mod.shutil, "disk_usage", boom)
    assert headroom_mod._disk_free_gb() is None


def test_cgroup_clamped_memory_check_refuses(monkeypatch, host):
    """The memory gate is the real check_memory_available, clamped to the cgroup.

    The host reads plenty free; the container's cgroup leaves 0.5 GB, so a
    2 GB estimate is short on memory.
    """
    # The function runs on its facade's globals, so patch those.
    real_globals = _REAL_CHECK_MEMORY.__globals__
    monkeypatch.setattr(subagent_facade, "check_memory_available", _REAL_CHECK_MEMORY)
    monkeypatch.setattr(real_globals["platform_compat"], "IS_LINUX", True)
    monkeypatch.setitem(real_globals, "_cgroup_available_gb", lambda: 0.5)
    _, short = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert short == "memory"


def test_no_field_holds_credential_content_or_a_path(monkeypatch, host):
    secret = "ghp_" + "A" * 36
    host.status = _status(
        available_gb=f"/home/u/.aws/{secret}",
        cpu_count=f"{secret}",
        load_per_cpu="/etc/passwd",
        posture=f"critical {secret} /root",
    )
    host.disk_gb = None
    snap, _ = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    text = json.dumps(snap)
    assert secret not in text and "/" not in text
    for key, value in snap.items():
        assert value is None or isinstance(value, (int, float)) or key == "posture"
    assert snap["posture"] in ("ample", "tight", "critical", "unknown")
