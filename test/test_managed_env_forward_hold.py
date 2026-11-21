"""A lane crew's forward follows its sandbox: paused while the sandbox reads STOPPING or
STOPPED, connected once when it reads RUNNING again (``routes._ForwardHold``).

The core's tunnel manager is a fake with the two public calls the edition uses
(``disconnect(keep_intent=...)`` and ``connect``) and the core's self-heal as a fake clock:
each fake minute, every crew whose forward is up re-mints through the lane's real
``ssh-session`` route, so each try is one StartSshSession at the fake service (409 while STOPPED).
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from aiohttp import web
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_engine import FakeRegistry

from kiro_crew.managed_env import PROVISIONER_ID
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine, sandbox_id_from_host, ssh_host_for
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.routes import SandboxRoutes, register_sandbox_routes


class FakeManager:
    """The core manager's ``connect`` / ``disconnect`` and its self-heal on a fake clock."""

    def __init__(self, registry: FakeRegistry, bff: FakeBff):
        self.registry = registry
        self.bff = bff
        self.calls: list[tuple] = []
        self.up: dict[str, bool] = {}
        self.url = ""
        self.stops_at_disconnect: list[int] = []
        self.mints_at_disconnect: list[int] = []

    async def disconnect(self, instance_id, *, keep_intent=False):
        self.calls.append(("disconnect", instance_id, keep_intent))
        self.stops_at_disconnect.append(self.bff.count("StopSandbox"))
        self.mints_at_disconnect.append(self.bff.count("StartSshSession"))
        live = self.up.get(instance_id, False)
        self.up[instance_id] = False  # the teardown: a parked recovery goes stale
        if not keep_intent:
            self.registry.update(instance_id, was_connected=False)
        return live

    async def connect(self, instance_id):
        self.calls.append(("connect", instance_id))
        self.registry.update(instance_id, was_connected=True)
        self.up[instance_id] = True
        await asyncio.to_thread(self._mint, instance_id)

    def _mint(self, instance_id):
        sid = sandbox_id_from_host(self.registry.rows[instance_id].ssh_host)
        req = urllib.request.Request(
            f"{self.url}/api/managed-environments/{sid}/ssh-session", data=b"", method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError:
            pass

    def tick(self):
        """One fake minute of the core's self-heal: re-mint every forward still up."""
        for instance_id, live in list(self.up.items()):
            if live:
                self._mint(instance_id)

    def auto_connect_tick(self):
        """One fake minute of the page's auto-connect (the core website's useAutoConnectInstances):
        a connect for every crew that isn't live, whatever its intent; each mints through the
        route. Returns the route's answers."""
        answers = []
        for instance_id in list(self.registry.rows):
            if not self.up.get(instance_id):
                answers.append(self._post(instance_id))
        return answers

    def _post(self, instance_id):
        sid = sandbox_id_from_host(self.registry.rows[instance_id].ssh_host)
        req = urllib.request.Request(
            f"{self.url}/api/managed-environments/{sid}/ssh-session", data=b"", method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class LaneDash:
    """The sandbox routes on a loopback port, with the core's state carrying the manager."""

    def __init__(self, routes: SandboxRoutes, mgr):
        self.routes = routes
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def start():
            app = web.Application()
            if mgr is not None:
                app["state"] = SimpleNamespace(instances_manager=mgr)
            register_sandbox_routes(app, routes)
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            ready.set()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(start())
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def settle(self):
        asyncio.run_coroutine_threadsafe(self.routes.forwards.idle(), self.loop).result(20)

    def get_list(self):
        with urllib.request.urlopen(f"{self.url}/api/managed-environments/list", timeout=20) as r:
            body = json.loads(r.read())
        self.settle()
        return body

    def post(self, sid, action):
        req = urllib.request.Request(
            f"{self.url}/api/managed-environments/{sid}/{action}", data=b"", method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        self.settle()

    def close(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def lane(tmp_path, monkeypatch, request):
    """A RUNNING own sandbox with its lane crew row (intent: connected), the forward up."""
    from kiro_crew.dashboard.handlers import _shared

    bff = FakeBff()
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(
        lambda: "bff-tok", guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock
    )
    registry = FakeRegistry()
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: registry)
    routes = SandboxRoutes(lambda: engine, run_in_thread=False)

    async def allow(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    sid = bff.add_sandbox("RUNNING", "kc-aaaaaa")
    guard.record_created(dict(bff.sandboxes[sid]), "kc-aaaaaa")
    row = registry.add(
        name="kc",
        connection_method="ssh",
        ssh_host=ssh_host_for(sid),
        remote_port=5476,
        provisioner_id=PROVISIONER_ID,
    )
    registry.update(row.id, was_connected=True)
    mgr = FakeManager(registry, bff)
    with_mgr = getattr(request, "param", True)
    dash = LaneDash(routes, mgr if with_mgr else None)
    mgr.url = dash.url
    mgr.up[row.id] = True
    yield SimpleNamespace(
        bff=bff, sid=sid, iid=row.id, mgr=mgr, dash=dash, registry=registry, routes=routes
    )
    dash.close()
    bff.close()


HELD = {"error": "sandbox is not RUNNING", "kind": "not_ready"}


def mints(w):
    return w.bff.count("StartSshSession")


# ---- STOPPING or STOPPED: paused; no tuple asked while stopped ----------------------------------


@pytest.mark.parametrize("status", ["STOPPING", "STOPPED"])
def test_a_stopped_row_pauses_the_forward_and_asks_no_tuple_while_stopped(lane, status):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = status
    w.mgr.tick()  # the self-heal as today: one try, a 409
    assert mints(w) == 1
    w.dash.get_list()  # an idle-timeout stop or another client's: the list reads it
    assert w.mgr.calls == [("disconnect", w.iid, True)]
    before = mints(w)
    for _ in range(15):  # 15 fake minutes
        w.mgr.tick()
    assert mints(w) == before
    w.dash.get_list()
    assert w.mgr.calls == [("disconnect", w.iid, True)]  # once per stopped spell


def test_the_stop_request_pauses_before_its_stop_sandbox(lane):
    w = lane
    w.dash.post(w.sid, "stop")
    assert w.mgr.calls == [("disconnect", w.iid, True)]
    assert w.mgr.stops_at_disconnect == [0]  # the pause ran before StopSandbox
    assert w.bff.count("StopSandbox") == 1
    assert mints(w) == w.mgr.mints_at_disconnect[0] == 0  # no forward try between them
    for _ in range(15):
        w.mgr.tick()
    assert mints(w) == 0


# ---- held: the page's own auto-connect gets the stopped answer from memory ----------------------


@pytest.mark.parametrize("status", ["STOPPING", "STOPPED"])
def test_while_held_the_pages_auto_connect_asks_no_tuple(lane, status):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = status
    w.dash.get_list()
    answers = []
    for _ in range(15):  # 15 fake minutes of the page's auto-connect and the self-heal
        answers += w.mgr.auto_connect_tick()
        w.mgr.tick()
    assert mints(w) == 0
    assert len(answers) == 15
    assert all(code == 409 and body.get("kind") == "not_ready" for code, body in answers)
    assert w.routes.phases.phase(w.sid) == ""  # a refusal opens no connect window


@pytest.mark.parametrize("lane", [False], indirect=True)
def test_the_hold_needs_no_core_manager(lane):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    w.mgr.up[w.iid] = False  # no manager to pause it: the page's connect still comes
    assert w.mgr.auto_connect_tick() == [(409, HELD)]
    assert mints(w) == 0


def test_the_stop_request_holds_the_sandbox_before_any_list_read(lane):
    w = lane
    w.dash.post(w.sid, "stop")
    assert w.mgr.auto_connect_tick() == [(409, HELD)]
    assert mints(w) == 0


def test_a_failed_stop_releases_the_hold(lane):
    w = lane
    w.bff.queue_error("stop_409", op="StopSandbox")  # the StopSandbox fails: the box stays RUNNING
    w.dash.post(w.sid, "stop")
    assert w.bff.count("StopSandbox") == 1
    assert not w.routes.forwards.holds(w.sid)
    assert w.mgr._post(w.iid)[0] == 200  # served before any list read
    assert mints(w) == 1


def test_a_stopped_sandbox_with_no_crew_row_is_held_by_its_id(lane):
    w = lane
    rowless = w.bff.add_sandbox("STOPPED", "kc-dddddd")  # own, its crew row gone
    w.routes.engine.guard.record_created(dict(w.bff.sandboxes[rowless]), "kc-dddddd")
    listed = w.dash.get_list()
    item = next(i for i in listed["sandboxes"] if i["sandbox_id"] == rowless)
    assert item["instance_id"] == "" and item["status"] == "STOPPED"
    req = urllib.request.Request(
        f"{w.dash.url}/api/managed-environments/{rowless}/ssh-session", data=b"", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=20)
    assert e.value.code == 409 and json.loads(e.value.read()) == HELD
    assert mints(w) == 0
    assert w.mgr.calls == []  # no crew row: nothing to pause


def test_another_sandbox_is_not_held(lane):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    other = w.bff.add_sandbox("RUNNING", "kc-cccccc")
    w.routes.engine.guard.record_created(dict(w.bff.sandboxes[other]), "kc-cccccc")
    row = w.registry.add(
        name="kc2",
        connection_method="ssh",
        ssh_host=ssh_host_for(other),
        remote_port=5476,
        provisioner_id=PROVISIONER_ID,
    )
    assert w.mgr._post(row.id)[0] == 200
    assert mints(w) == 1


# ---- RUNNING again: one connect, the intent kept ------------------------------------------------


def test_running_again_connects_once_and_the_phase_starts_again(lane):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    w.bff.sandboxes[w.sid]["status"] = "RUNNING"  # the Resume's read-back
    w.dash.get_list()
    assert w.mgr.calls == [("disconnect", w.iid, True), ("connect", w.iid)]
    assert w.mgr.up[w.iid] is True and mints(w) == 1
    assert w.routes.phases.phase(w.sid) == "tunnel"  # the connect phase starts again
    w.dash.get_list()
    assert w.mgr.calls.count(("connect", w.iid)) == 1


def test_a_users_own_disconnect_is_not_undone_at_running(lane):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    w.registry.update(w.iid, was_connected=False)  # the user's own Disconnect clears the intent
    w.bff.sandboxes[w.sid]["status"] = "RUNNING"
    w.dash.get_list()
    assert ("connect", w.iid) not in w.mgr.calls


# ---- unchanged --------------------------------------------------------------------------------


def test_a_running_box_whose_forward_drops_self_heals_as_today(lane):
    w = lane
    for _ in range(3):
        w.dash.get_list()
        w.mgr.tick()
    assert w.mgr.calls == []
    assert mints(w) == 3


@pytest.mark.parametrize("lane", [False], indirect=True)
def test_with_no_core_manager_nothing_is_paused(lane):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    w.dash.post(w.sid, "stop")
    assert w.mgr.calls == []


def test_lane_off_or_a_non_lane_crew_is_left_alone(lane, monkeypatch):
    w = lane
    other = w.bff.add_sandbox("STOPPED", "kc-bbbbbb")
    w.registry.add(
        name="theirs",
        connection_method="ssh",
        ssh_host=ssh_host_for(other),
        remote_port=5476,
        provisioner_id="someone_else",
    )
    w.dash.get_list()
    assert w.mgr.calls == []  # a crew of another provisioner is not this lane's
    assert w.routes._crew_of(other) == ""  # nor would the Stop request pause it
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    with pytest.raises(urllib.error.HTTPError) as e:
        w.dash.get_list()
    assert e.value.code == 404
    assert w.mgr.calls == []


def test_the_pause_adds_no_bff_call(lane):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    before = list(w.bff.ops())
    w.dash.get_list()
    added = w.bff.ops()[len(before) :]
    assert w.mgr.calls == [("disconnect", w.iid, True)]
    assert added == ["ListSandboxes"]
