"""A crew row's connect progress: the ``ssh-session`` route records each sandbox's phase in
memory, and ``GET /api/managed-environments/{id}/connect-phase`` serves it with no service call.

The phases, by order inside one connect window (opened by a tuple ask, closed by a failed ask
or 60 s): ``starting`` (asked), ``tunnel`` (one tuple served), ``signing_in`` (two served).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_engine import FakeRegistry
from test_managed_env_mint import Dashboard

from kiro_crew.managed_env import routes as routes_mod
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.routes import CONNECT_WINDOW_S, SandboxRoutes, _ConnectPhases


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """The lane and its row flag on, the owner check allowing, a RUNNING own sandbox."""
    from kiro_crew.dashboard.handlers import _shared

    bff = FakeBff()
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(
        lambda: "bff-tok", guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock
    )
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: FakeRegistry())
    clock = Clock()
    routes = SandboxRoutes(lambda: engine, run_in_thread=False, now=clock)
    asked = []

    async def allow(request, operation):
        asked.append(operation)
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    dash = Dashboard(routes)
    sid = bff.add_sandbox("RUNNING", "kc-aaaaaa")
    guard.record_created(dict(bff.sandboxes[sid]), "kc-aaaaaa")
    yield bff, dash, sid, clock, routes, asked
    dash.close()
    bff.close()


def get_phase(dash, sid):
    req = urllib.request.Request(f"{dash.url}/api/managed-environments/{sid}/connect-phase")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def get_code(dash, sid):
    req = urllib.request.Request(f"{dash.url}/api/managed-environments/{sid}/connect-phase")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=20)
    return e.value.code


def post_tuple(dash, sid):
    req = urllib.request.Request(
        f"{dash.url}/api/managed-environments/{sid}/ssh-session", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


# ---- the phases, in order, inside one window -------------------------------------------------


def test_the_phases_follow_the_tuples_in_order_and_end_with_the_window():
    clock = Clock()
    p = _ConnectPhases(clock)
    assert p.phase("sb") == ""
    p.asked("sb")
    assert p.phase("sb") == "starting"
    p.served("sb")
    assert p.phase("sb") == "tunnel"
    p.asked("sb")  # the token mint's ssh asks inside the same window
    assert p.phase("sb") == "tunnel"
    p.served("sb")
    assert p.phase("sb") == "signing_in"
    # a reconnect inside the 60 s (a self-heal, a second Connect): that connect had both its
    # tuples, so the next ask opens a new window and the label starts again
    clock.t += 10
    p.asked("sb")
    assert p.phase("sb") == "starting"
    p.served("sb")
    assert p.phase("sb") == "tunnel"
    clock.t += CONNECT_WINDOW_S - 1  # 59 s after the new window opened: still open
    assert p.phase("sb") == "tunnel"
    clock.t += 1
    assert p.phase("sb") == ""  # 60 s after the window opened
    p.asked("sb")
    assert p.phase("sb") == "starting"  # a new connect opens a new window
    p.failed("sb")
    assert p.phase("sb") == ""
    p.served("sb")  # a tuple served with no open window opens nothing
    assert p.phase("sb") == ""
    assert p.phase("other") == ""


def test_the_route_records_each_served_tuple(lane):
    bff, dash, sid, clock, routes, asked = lane
    assert get_phase(dash, sid) == {"phase": ""}
    assert post_tuple(dash, sid)[0] == 200
    assert get_phase(dash, sid) == {"phase": "tunnel"}
    assert post_tuple(dash, sid)[0] == 200
    assert get_phase(dash, sid) == {"phase": "signing_in"}
    clock.t += CONNECT_WINDOW_S
    assert get_phase(dash, sid) == {"phase": ""}
    assert bff.count("StartSshSession") == 2


def test_a_failed_ask_closes_the_window(lane):
    bff, dash, sid, clock, routes, asked = lane
    assert post_tuple(dash, sid)[0] == 200
    assert get_phase(dash, sid) == {"phase": "tunnel"}
    bff.sandboxes[sid]["status"] = "STOPPED"  # the next ask is answered 409
    assert post_tuple(dash, sid)[0] == 409
    assert get_phase(dash, sid) == {"phase": ""}


def test_a_foreign_sandboxs_ask_records_nothing(lane):
    bff, dash, sid, clock, routes, asked = lane
    foreign = bff.add_sandbox("RUNNING", "someone-else")  # not in this client's ledger
    assert post_tuple(dash, foreign)[0] == 404
    assert get_phase(dash, foreign) == {"phase": ""}
    assert routes.phases._open == {}
    assert bff.count("StartSshSession") == 0


def test_the_record_holds_no_tuple_value(lane):
    bff, dash, sid, clock, routes, asked = lane
    status, tup = post_tuple(dash, sid)
    assert status == 200 and tup["tokenValue"]
    held = repr(vars(routes.phases)) + json.dumps(get_phase(dash, sid))
    for value in tup.values():
        assert value not in held
    assert "SECRET" not in held


# ---- the GET: no service call, owner-only, behind both flags ----------------------------------------


def test_the_get_makes_no_bff_call(lane):
    bff, dash, sid, clock, routes, asked = lane
    assert post_tuple(dash, sid)[0] == 200
    before = list(bff.ops())
    for _ in range(3):
        get_phase(dash, sid)
    assert bff.ops() == before
    assert bff.count("ListSandboxes") == 0


def test_the_get_runs_the_owner_check(lane, monkeypatch):
    from aiohttp import web

    from kiro_crew.dashboard.handlers import _shared

    bff, dash, sid, clock, routes, asked = lane
    get_phase(dash, sid)
    assert asked == ["sandboxes_connect_phase"]

    async def deny(request, operation):
        return web.json_response({"error": "owner only"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", deny)
    assert get_code(dash, sid) == 403


@pytest.mark.parametrize("bad", ["..", "a b", "-x"])
def test_a_bad_id_is_refused(lane, bad):
    bff, dash, sid, clock, routes, asked = lane
    import urllib.parse

    assert get_code(dash, urllib.parse.quote(bad, safe="")) == 404
    assert bff.ops() == []


def test_the_window_length_is_sixty_seconds():
    assert routes_mod.CONNECT_WINDOW_S == 60.0
    assert routes_mod.CONNECT_PHASES == ("starting", "tunnel", "signing_in")
