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
    "tags",
    "logins",
    "measured_at",
}

#: A create reply's snapshot: no login check runs on a create.
CREATE_HEADROOM_FIELDS = HEADROOM_FIELDS - {"logins"}

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
    reading = SimpleNamespace(
        status=_status(), memory_ok=True, cap=4, running=1, disk_gb=500.0, logins={}
    )

    async def peer_logins() -> dict:
        return dict(reading.logins)

    monkeypatch.setattr(headroom_mod, "peer_logins", peer_logins)
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


#: Wave 6 measured defaults (session-control spec, "Wave 6: estimate defaults").
MEASURED_MEMORY_GB = 5.0
MEASURED_DISK_GB = 5.0


def test_config_defaults_are_on_with_the_measured_estimates():
    cfg = _build_work_ledger_config({})
    assert cfg.remote_dispatch is True
    assert cfg.remote_dispatch_memory_gb == MEASURED_MEMORY_GB
    assert cfg.remote_dispatch_disk_gb == MEASURED_DISK_GB


def test_nothing_configured_falls_back_to_the_measured_estimates(monkeypatch):
    """Switch on, no estimate keys: the gate charges the measured defaults."""
    _config_load(monkeypatch, remote_dispatch=True)
    assert headroom_mod.remote_dispatch_settings() == headroom_mod.RemoteDispatchSettings(
        True, MEASURED_MEMORY_GB, MEASURED_DISK_GB
    )


def test_flag_off_ignores_the_estimates(monkeypatch):
    """Switch off: no estimate reaches the gate, defaults or configured."""
    _config_load(monkeypatch, remote_dispatch=False)
    assert headroom_mod.remote_dispatch_settings() == headroom_mod.RemoteDispatchSettings(
        False, 0.0, 0.0
    )
    _config_load(
        monkeypatch,
        remote_dispatch=False,
        remote_dispatch_memory_gb=7,
        remote_dispatch_disk_gb=70,
    )
    assert headroom_mod.remote_dispatch_settings() == headroom_mod.RemoteDispatchSettings(
        False, 0.0, 0.0
    )


@pytest.mark.parametrize("raw", ["true", 1, None, "yes"])
def test_a_value_that_is_not_a_bool_falls_back_to_the_default_on(raw):
    assert _build_work_ledger_config({"remote_dispatch": raw}).remote_dispatch is True
    assert _build_work_ledger_config({"remote_dispatch": False}).remote_dispatch is False


@pytest.mark.parametrize("raw", [0, -1, "big", True, float("nan")])
def test_a_bad_estimate_uses_the_default(raw):
    cfg = _build_work_ledger_config(
        {"remote_dispatch_memory_gb": raw, "remote_dispatch_disk_gb": raw}
    )
    assert (cfg.remote_dispatch_memory_gb, cfg.remote_dispatch_disk_gb) == (
        MEASURED_MEMORY_GB,
        MEASURED_DISK_GB,
    )


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
    assert headroom_mod.remote_dispatch_settings().enabled is True
    _config_load(monkeypatch, remote_dispatch=False)
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
    assert set(body["headroom"]) == CREATE_HEADROOM_FIELDS
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
    assert set(body["headroom"]) == CREATE_HEADROOM_FIELDS
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
    assert set(body["headroom"]) == CREATE_HEADROOM_FIELDS
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
        assert value is None or isinstance(value, (int, float)) or key in ("posture", "tags")
    assert snap["posture"] in ("ample", "tight", "critical", "unknown")
    assert snap["tags"] == []


# -- open hub workers reserve an estimate each (D3) ----------------------------------


def _hub_worker(n: int, *, board: str = "chat-hub") -> tuple[str, str]:
    """Open one ``hub:`` mailbox item bound to worker slot ``chat-w<n>``."""
    from kiro_crew import remote_dispatch_mailbox as mailbox

    request = mailbox.parse_work_item(
        {**WORK_ITEM, "item_id": f"it_{n:08x}", "conductor_key": board}
    )
    mailbox.open_mailbox(request, f"chat-w{n}")
    return request.board, request.item_id


def _memory_is(monkeypatch, free_gb: float) -> None:
    """A host whose memory check compares *free_gb* against what it is asked for."""
    monkeypatch.setattr(
        subagent_facade,
        "check_memory_available",
        lambda min_gb=2.0, **_: (free_gb >= min_gb, free_gb),
    )


@pytest.mark.parametrize("n", [0, 1, 3])
@pytest.mark.parametrize(("delta", "short"), [(0.01, ""), (-0.01, "memory")])
def test_each_open_hub_worker_reserves_one_memory_estimate(monkeypatch, host, n, delta, short):
    for i in range(n):
        _hub_worker(i + 1)
    _memory_is(monkeypatch, (n + 1) * 5.0 + delta)
    snap, got = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 5.0, 1.0))
    assert snap["open_workers"] == n
    assert got == short


@pytest.mark.parametrize(("delta", "short"), [(0.01, ""), (-0.01, "disk")])
def test_each_open_hub_worker_reserves_one_disk_estimate(monkeypatch, host, delta, short):
    for i in range(2):
        _hub_worker(i + 1)
    host.disk_gb = 3 * 10.0 + delta
    _, got = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert got == short


@pytest.mark.asyncio
@pytest.mark.parametrize(("delta", "status"), [(0.01, 200), (-0.01, 503)])
async def test_a_create_is_admitted_or_refused_on_reserved_memory(
    tmp_path, monkeypatch, host, delta, status
):
    _flag(monkeypatch, True, memory_gb=5.0, disk_gb=1.0)
    for i in range(2):
        _hub_worker(i + 1)
    _memory_is(monkeypatch, 3 * 5.0 + delta)
    state = _make_state(tmp_path)
    before = set(state._slots)
    bindable = {**WORK_ITEM, "item_id": "it_000000ff", "conductor_key": "chat-hub"}
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"work_item": bindable})
        body = await resp.json()
    assert resp.status == status, body
    assert body["headroom"]["open_workers"] == 2
    if status == 503:
        assert body["code"] == "crew_busy" and body["short"] == "memory"
        assert set(state._slots) == before
    else:
        assert body["key"] in state._slots


@pytest.mark.asyncio
async def test_a_create_without_work_item_ignores_reservations(tmp_path, monkeypatch, host):
    _flag(monkeypatch, True, memory_gb=5.0)
    for i in range(2):
        _hub_worker(i + 1)
    _memory_is(monkeypatch, 5.0)
    state = _make_state(tmp_path)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"name": "plain"})
        assert resp.status == 200
        assert (await resp.json())["headroom"]["open_workers"] == 2
    assert "plain" in state._slots


def test_closed_accepted_and_non_hub_items_reserve_nothing(monkeypatch, host):
    from kiro_crew import remote_dispatch_mailbox as mailbox
    from kiro_crew import work_ledger as wl

    _hub_worker(1)  # the one that counts
    _hub_worker(2)
    assert mailbox.close_mailbox("chat-w2") is not None  # its slot was deleted
    board, item_id = _hub_worker(3)
    wl.apply_worker_report(board, item_id, status="done", summary="x", artifacts={"pr": "1"})
    wl.apply_conductor_action(board, "verdict", item_id=item_id, verdict="pass")
    wl.apply_conductor_action(board, "close", item_id=item_id, state="accepted")
    board, _ = _hub_worker(5)
    wl.apply_worker_report(board, "it_00000005", status="progress", summary="x")
    assert mailbox.close_mailbox("chat-w5") is not None  # reported, so kept abandoned
    wl.ensure_conductor("chat-local")
    wl.create_bound_item(
        "chat-local",
        "it_0000aaaa",
        title="t",
        acceptance={"kind": "manual"},
        decision="",
        worker_session_key="chat-w4",
    )
    snap, _ = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert snap["open_workers"] == 1


def test_a_torn_binding_counts_nothing(monkeypatch, host):
    from kiro_crew import work_ledger as wl

    _hub_worker(1)
    wl.bindings_dir().joinpath("torn.json").write_text("{not json", encoding="utf-8")
    snap, _ = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0))
    assert snap["open_workers"] == 1


def test_an_unreadable_store_reserves_nothing(monkeypatch, host):
    from kiro_crew import remote_dispatch_mailbox as mailbox

    def boom():
        raise OSError("store unreadable")

    monkeypatch.setattr(mailbox, "open_mailbox_workers", boom)
    _memory_is(monkeypatch, 5.0)
    snap, short = headroom_mod.measure(headroom_mod.RemoteDispatchSettings(True, 5.0, 1.0))
    assert snap["open_workers"] is None
    assert short == ""


def _bindable(n: int) -> dict:
    return {**WORK_ITEM, "item_id": f"it_{n:08x}", "conductor_key": "chat-hub"}


_ROOM_FOR_ONE = headroom_mod.RemoteDispatchSettings(True, 5.0, 1.0)


def _open() -> int:
    return headroom_mod.measure(_ROOM_FOR_ONE)[0]["open_workers"]


async def _post(state, body) -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json=body)
        return resp.status, await resp.json()


def test_the_bind_measures_again_and_the_second_of_two_is_refused(monkeypatch, host):
    """Room for one: the first bind is stored, the second sees it and stores nothing."""
    from kiro_crew import remote_dispatch_mailbox as mailbox

    _memory_is(monkeypatch, 5.0 + 0.01)
    first = mailbox.parse_work_item(_bindable(1))
    second = mailbox.parse_work_item(_bindable(2))
    assert headroom_mod.bind_if_room(_ROOM_FOR_ONE, first, "chat-w1")[1] == ""
    snap, short = headroom_mod.bind_if_room(_ROOM_FOR_ONE, second, "chat-w2")
    assert (snap["open_workers"], short) == (1, "memory")
    assert not mailbox.item_exists(second)
    assert _open() == 1


@pytest.mark.asyncio
async def test_a_worker_bound_after_the_early_gate_refuses_the_create(tmp_path, monkeypatch, host):
    """Two creates pass the early gate together; the bind refuses the later one."""
    from kiro_crew import remote_dispatch_mailbox as mailbox
    from kiro_crew.dashboard.handlers import remote_work_item

    _flag(monkeypatch, True, memory_gb=5.0, disk_gb=1.0)
    _memory_is(monkeypatch, 5.0 + 0.01)
    real = remote_work_item.work_item_for_create

    async def another_hub_binds_meanwhile(request, body):
        got = await real(request, body)
        _hub_worker(9)
        return got

    monkeypatch.setattr(remote_work_item, "work_item_for_create", another_hub_binds_meanwhile)
    state = _make_state(tmp_path)
    before = set(state._slots)
    got, body = await _post(state, {"work_item": _bindable(1)})
    assert got == 503, body
    assert body["code"] == "crew_busy" and body["short"] == "memory"
    assert set(state._slots) == before
    assert not mailbox.item_exists(mailbox.parse_work_item(_bindable(1)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"name": "w1"}, {"memory_mode": "temporary"}, {"work_item": {**WORK_ITEM, "title": 5}}],
    ids=["name-refused", "memory-mode-refused", "bad-work-item"],
)
async def test_a_create_refused_after_admission_reserves_nothing(
    tmp_path, monkeypatch, host, extra
):
    _flag(monkeypatch, True, memory_gb=5.0, disk_gb=1.0)
    _memory_is(monkeypatch, 5.0 + 0.01)
    state = _make_state(tmp_path)
    got, body = await _post(state, {"work_item": _bindable(1), **extra})
    assert got == 400, body
    assert _open() == 0
    got, body = await _post(state, {"work_item": _bindable(1)})
    assert got == 200, body


@pytest.mark.asyncio
async def test_a_failed_mint_reserves_nothing(tmp_path, monkeypatch, host):
    _flag(monkeypatch, True, memory_gb=5.0, disk_gb=1.0)
    _memory_is(monkeypatch, 5.0 + 0.01)
    state = _make_state(tmp_path)
    real = state.get_or_create_slot

    def boom(*a, **k):
        raise RuntimeError("mint failed")

    monkeypatch.setattr(state, "get_or_create_slot", boom)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots", json={"work_item": _bindable(1)})
        assert resp.status == 500
    assert _open() == 0
    monkeypatch.setattr(state, "get_or_create_slot", real)
    got, body = await _post(state, {"work_item": _bindable(1)})
    assert got == 200, body


@pytest.mark.asyncio
async def test_a_created_worker_counts_once(tmp_path, monkeypatch, host):
    _flag(monkeypatch, True, memory_gb=5.0, disk_gb=1.0)
    state = _make_state(tmp_path)
    got, body = await _post(state, {"work_item": _bindable(1)})
    assert got == 200, body
    assert _open() == 1


# -- tags and logins (audit D2) --------------------------------------------------


def test_config_tags_keep_only_plain_words(monkeypatch):
    tags = [
        "gpu",
        "docker",
        "gpu",
        "/home/u",
        "lab.example.com",
        "123456789012",
        "GPU",
        7,
        "x" * 33,
        "gpu\n",
        "glpat-" + "a" * 20,
    ]
    _config_load(monkeypatch, remote_dispatch=True, remote_dispatch_tags=tags)
    assert headroom_mod.remote_dispatch_settings().tags == ("gpu", "docker")
    _config_load(monkeypatch, remote_dispatch=True, remote_dispatch_tags="gpu")
    assert headroom_mod.remote_dispatch_settings().tags == ()
    assert _build_work_ledger_config({}).remote_dispatch_tags == []


@pytest.mark.asyncio
async def test_route_carries_the_tags_and_one_word_per_login(tmp_path, monkeypatch, host):
    settings = headroom_mod.RemoteDispatchSettings(True, 2.0, 10.0, tags=("gpu",))
    monkeypatch.setattr(headroom_mod, "remote_dispatch_settings", lambda: settings)
    host.logins = {"gh": "valid", "az": "expired"}
    async with TestClient(TestServer(_app(_make_state(tmp_path)))) as client:
        body = await (await client.get("/api/chat/headroom", params={"logins": "1"})).json()
        created = await (await client.post("/api/chat/slots", json={"name": "w"})).json()
    assert body["tags"] == ["gpu"]
    assert body["logins"] == {"gh": "valid", "az": "expired"}
    assert "logins" not in created["headroom"]
    assert created["headroom"]["tags"] == ["gpu"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, checked",
    [({"logins": "0"}, False), ({}, True), ({"logins": "1"}, True), ({"logins": ""}, True)],
)
async def test_only_the_no_logins_query_skips_the_login_check(
    tmp_path, monkeypatch, host, query, checked
):
    _flag(monkeypatch, True)
    checks: list[int] = []

    async def peer_logins() -> dict:
        checks.append(1)
        return {"gh": "valid"}

    monkeypatch.setattr(headroom_mod, "peer_logins", peer_logins)
    async with TestClient(TestServer(_app(_make_state(tmp_path)))) as client:
        resp = await client.get("/api/chat/headroom", params=query)
        assert resp.status == 200
        body = await resp.json()
    assert ("logins" in body) is checked
    assert checks == ([1] if checked else [])


def _git(monkeypatch, states: dict, *, refusal: str = "", delay: dict | None = None):
    """Stub ``git_gateway``'s readiness pieces; returns the hosts asked."""
    from kiro_crew.remote_session import git_gateway

    asked: list[str] = []

    async def lending_refusal() -> str:
        return refusal

    async def login_status(host: str) -> str:
        asked.append(host)
        gate = (delay or {}).get(host)
        if gate is not None:
            await gate.wait()
        answer = states.get(host, "unchecked")
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(git_gateway, "lending_refusal", lending_refusal)
    monkeypatch.setattr(git_gateway, "login_status", login_status)
    monkeypatch.setattr(headroom_mod, "_login_cache", {})
    monkeypatch.setattr(headroom_mod, "_login_tasks", {})

    return asked


@pytest.mark.asyncio
async def test_login_words_come_from_the_git_proxy_readiness_probe(monkeypatch):
    asked = _git(
        monkeypatch,
        {"github.com": "signed_in", "gitlab.com": "not_signed_in", "dev.azure.com": RuntimeError()},
    )
    # gitlab's "unchecked" (helper fallback) and az's failure are absent, never valid.
    assert await headroom_mod.peer_logins() == {"gh": "valid", "glab": "expired"}
    # bitbucket.org has no status-command source, so it is never asked.
    assert sorted(asked) == ["dev.azure.com", "github.com", "gitlab.com"]


def test_each_host_maps_to_the_tool_its_readiness_asks():
    from kiro_crew.remote_session import git_gateway

    tools = {h: git_gateway.readiness_tool(h) for h in git_gateway.HOSTS}
    assert tools == {
        "github.com": "gh",
        "gitlab.com": "glab",
        "bitbucket.org": None,
        "dev.azure.com": "az",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["withheld", "unsupported"])
async def test_a_crew_that_lends_no_login_reports_none_even_from_cache(monkeypatch, refusal):
    import time

    asked = _git(monkeypatch, {"github.com": "signed_in"}, refusal=refusal)
    # A fresh reading taken before the sandbox went strict must not answer.
    monkeypatch.setattr(
        headroom_mod, "_login_cache", {"github.com": (time.monotonic(), "signed_in")}
    )
    assert await headroom_mod.peer_logins() == {}
    assert asked == []


@pytest.mark.asyncio
async def test_git_readiness_reads_the_same_gate(monkeypatch):
    from kiro_crew.remote_session import git_gateway

    async def refusal() -> str:
        return "withheld"

    monkeypatch.setattr(git_gateway, "lending_refusal", refusal)
    assert set((await git_gateway.git_readiness()).values()) == {"withheld"}


@pytest.mark.asyncio
async def test_readings_are_cached_and_one_slow_check_hides_no_other(monkeypatch):
    import asyncio

    slow = asyncio.Event()
    asked = _git(
        monkeypatch,
        {"github.com": "signed_in", "gitlab.com": "signed_in", "dev.azure.com": "signed_in"},
        delay={"dev.azure.com": slow},
    )
    monkeypatch.setattr(headroom_mod, "LOGINS_WAIT_S", 0.05)  # flake-ok: az never ends first
    # Cold cache and az hangs: gh and glab still answer, az reads as still checking.
    assert await headroom_mod.peer_logins() == {"gh": "valid", "glab": "valid", "az": "unknown"}
    slow.set()
    await headroom_mod._login_tasks["dev.azure.com"]
    assert await headroom_mod.peer_logins() == {"gh": "valid", "glab": "valid", "az": "valid"}
    assert len(asked) == 3  # fresh readings: nothing asked twice
    stamp, state = headroom_mod._login_cache["github.com"]
    headroom_mod._login_cache["github.com"] = (stamp - headroom_mod.LOGINS_TTL_S, state)
    await headroom_mod.peer_logins()
    assert asked.count("github.com") == 2
