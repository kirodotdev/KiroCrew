"""A STOPPED crew's row reads Stopped with Resume, not a red Error with ssh text.

While the lane is on, the lane turns the crew's Remote Crew row off for
a sandbox the List reads STOPPING or STOPPED, by the core's own locked path
(``mgr.reconfigure(..., needs_teardown=None)``, as the core's PATCH ``disabled`` does). The core
page's auto-connect skips a turned-off row (``selectAutoConnectTargets``) and the core row draws
no status badge for it, so no failed try is shown; the edition's row detail reads "Sandbox
stopped" with Resume. RUNNING turns on only the rows the lane turned off, after a restart too,
then connects once as Task 8b does. A row the user turned off is left alone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_engine import FakeRegistry
from test_managed_env_forward_hold import FakeManager, LaneDash, mints

from kiro_crew.managed_env import PROVISIONER_ID
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine, ssh_host_for
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.routes import SandboxRoutes


class CoreManager(FakeManager):
    """The fake manager with the core's ``reconfigure`` (teardown keeping the intent, then the
    write, as one step) and the page's auto-connect skipping a turned-off row, as the core's."""

    def __init__(self, registry, bff):
        super().__init__(registry, bff)
        self.fail_reconfigure = False
        self.stops_at_reconfigure: list[int] = []

    async def reconfigure(self, instance_id, apply, needs_teardown=None):
        self.calls.append(("reconfigure", instance_id, needs_teardown))
        self.stops_at_reconfigure.append(self.bff.count("StopSandbox"))
        if self.fail_reconfigure:
            raise RuntimeError("could not close the current tunnel")
        self.up[instance_id] = False  # teardown with keep_intent=True: was_connected stays
        return apply()

    def auto_connect_tick(self):
        answers = []
        for instance_id, row in list(self.registry.rows.items()):
            if getattr(row, "disabled", False):
                continue  # the core's selectAutoConnectTargets skips a turned-off row
            if not self.up.get(instance_id):
                answers.append(self._post(instance_id))
        return answers


def _stack(tmp_path, bff, registry, mgr):
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(
        lambda: "bff-tok", guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock
    )
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: registry)
    routes = SandboxRoutes(lambda: engine, run_in_thread=False)
    dash = LaneDash(routes, mgr)
    mgr.url = dash.url
    return guard, routes, dash


@pytest.fixture
def row_on(monkeypatch):
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A RUNNING own sandbox with its lane crew row (on, intent connected), the forward up."""
    from kiro_crew.dashboard.handlers import _shared

    async def allow(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    bff = FakeBff()
    registry = FakeRegistry()
    mgr = CoreManager(registry, bff)
    guard, routes, dash = _stack(tmp_path, bff, registry, mgr)
    sid = bff.add_sandbox("RUNNING", "kc-aaaaaa")
    guard.record_created(dict(bff.sandboxes[sid]), "kc-aaaaaa")
    row = registry.add(
        name="kc",
        connection_method="ssh",
        ssh_host=ssh_host_for(sid),
        remote_port=5476,
        provisioner_id=PROVISIONER_ID,
    )
    registry.update(row.id, was_connected=True, disabled=False)
    mgr.up[row.id] = True
    w = SimpleNamespace(
        bff=bff,
        sid=sid,
        iid=row.id,
        mgr=mgr,
        dash=dash,
        registry=registry,
        routes=routes,
        guard=guard,
        tmp_path=tmp_path,
        dashes=[dash],
    )
    yield w
    for d in w.dashes:
        d.close()
    bff.close()


def _off(w):
    return bool(getattr(w.registry.rows[w.iid], "disabled", False))


# ---- STOPPED: the row reads off (no badge, no auto-connect), the hold stays ----------------------


@pytest.mark.parametrize("status", ["STOPPING", "STOPPED"])
def test_a_stopped_row_is_turned_off_and_its_auto_connect_tries_stop(lane, row_on, status):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = status
    w.dash.get_list()
    assert w.mgr.calls == [("reconfigure", w.iid, None)]  # the core's own disable path
    assert _off(w) and w.registry.rows[w.iid].was_connected is True  # the intent kept
    assert w.guard.turned_off_ids() == {w.iid}
    answers = []
    for _ in range(15):  # 15 fake minutes of the page's auto-connect and the self-heal
        answers += w.mgr.auto_connect_tick()
        w.mgr.tick()
    assert answers == [] and mints(w) == 0  # no try, so no red Error and no ssh text
    w.dash.get_list()
    assert w.mgr.calls == [("reconfigure", w.iid, None)]  # once per stopped spell
    assert w.routes.forwards.is_paused(w.iid)


def test_the_held_connect_tries_stay_held(lane, row_on):
    """Task 8b: a connect try that still reaches the route (the user's own Connect on the turned
    off row, or an old page) gets the stopped answer from memory, no tuple asked."""
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    code, body = w.mgr._post(w.iid)
    assert code == 409 and body.get("kind") == "not_ready"
    assert mints(w) == 0


def test_the_stop_request_turns_the_row_off_before_its_stop_sandbox(lane, row_on):
    w = lane
    w.dash.post(w.sid, "stop")
    assert w.mgr.calls == [("reconfigure", w.iid, None)]
    assert w.mgr.stops_at_reconfigure == [0]  # turned off before StopSandbox
    assert w.bff.count("StopSandbox") == 1 and _off(w)
    for _ in range(15):
        assert w.mgr.auto_connect_tick() == []
        w.mgr.tick()
    assert mints(w) == 0


def test_the_turn_off_adds_no_bff_call(lane, row_on):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    before = list(w.bff.ops())
    w.dash.get_list()
    assert w.bff.ops()[len(before) :] == ["ListSandboxes"]


# ---- RUNNING: unchanged; RUNNING again turns on what the lane turned off ------------------------


def test_a_running_row_is_unchanged(lane, row_on):
    w = lane
    for _ in range(3):
        w.dash.get_list()
        w.mgr.tick()
    assert w.mgr.calls == [] and not _off(w)
    assert w.guard.turned_off_ids() == set()
    assert mints(w) == 3  # the self-heal as today


def test_running_again_turns_the_row_on_and_connects_once(lane, row_on):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    w.bff.sandboxes[w.sid]["status"] = "RUNNING"  # the Resume's read-back
    w.dash.get_list()
    assert not _off(w)
    assert w.mgr.calls == [("reconfigure", w.iid, None), ("connect", w.iid)]
    assert w.guard.turned_off_ids() == set()
    w.dash.get_list()
    assert w.mgr.calls.count(("connect", w.iid)) == 1


def test_a_row_the_user_turned_off_is_left_alone(lane, row_on):
    w = lane
    w.registry.update(w.iid, disabled=True)  # the user's own Connections off
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    assert ("reconfigure", w.iid, None) not in w.mgr.calls
    assert w.mgr.calls == [("disconnect", w.iid, True)]  # Task 8b's plain pause
    assert w.guard.turned_off_ids() == set()
    w.bff.sandboxes[w.sid]["status"] = "RUNNING"
    w.dash.get_list()
    assert _off(w)  # still the user's off


def test_a_restart_still_turns_on_the_row_the_lane_turned_off(lane, row_on):
    w = lane
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    assert _off(w)
    # a restart: new routes over the same state dir and registry, nothing paused in memory
    _, routes2, dash2 = _stack(w.tmp_path, w.bff, w.registry, w.mgr)
    w.dashes.append(dash2)
    w.mgr.calls.clear()
    w.bff.sandboxes[w.sid]["status"] = "RUNNING"
    dash2.get_list()
    assert not _off(w)
    assert w.mgr.calls == [("connect", w.iid)]
    assert routes2.engine.guard.turned_off_ids() == set()


def test_a_failed_turn_off_falls_back_to_the_plain_pause(lane, row_on):
    w = lane
    w.mgr.fail_reconfigure = True
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    assert w.mgr.calls == [("reconfigure", w.iid, None), ("disconnect", w.iid, True)]
    assert not _off(w) and w.guard.turned_off_ids() == set()


# ---- the switch off: Task 8b as before ---------------------------------------------------------


def test_a_cancelled_turn_off_keeps_its_note_and_running_turns_the_row_on(lane, row_on):
    # the core's shielded write lands, then the cancel: the note stays, RUNNING turns it on
    w = lane
    real = w.mgr.reconfigure

    async def written_then_cancelled(instance_id, apply, needs_teardown=None):
        await real(instance_id, apply, needs_teardown)
        raise asyncio.CancelledError()

    w.mgr.reconfigure = written_then_cancelled
    w.bff.sandboxes[w.sid]["status"] = "STOPPED"
    w.dash.get_list()
    assert _off(w) and w.guard.turned_off_ids() == {w.iid}  # noted, not dropped
    w.mgr.reconfigure = real
    w.bff.sandboxes[w.sid]["status"] = "RUNNING"
    w.dash.get_list()
    assert not _off(w) and ("connect", w.iid) in w.mgr.calls
    assert w.guard.turned_off_ids() == set()
