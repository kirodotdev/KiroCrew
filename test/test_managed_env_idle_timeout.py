"""The idle-timeout change (``UpdateSandbox``) for the persistent-sandbox lane: the client call and
``POST /api/managed-environments/{id}/idle-timeout`` behind the lane switch.

The service is the loopback fake; the clock only moves when something sleeps. The 400 and 409 bodies
are the recorded ones (the switch's, the busy one and the stop-first one
as the service sends them); see the route module's constants.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_engine import FakeRegistry, Row

from kiro_crew.managed_env import routes as routes_mod
from kiro_crew.managed_env.call_log import CallLog
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine
from kiro_crew.managed_env.guard import GuardRefused, SandboxGuard
from kiro_crew.managed_env.routes import IDLE_TEXT, SandboxRoutes, register_sandbox_routes

TOKEN = "tok-SECRET-0123456789"
_NS = "com.example.portal#"


def _answer(status, kind, message):
    return status, {"__type": kind if "#" in kind else _NS + kind, "message": message}


# recorded texts: the switch, busy, stop first
SWITCH_400 = _answer(400, "BadRequestException", "idleTimeoutMinutes isn't available")
BUSY_409 = _answer(
    409,
    "ConflictException",
    "Sandbox env-001 is changing state (the host refused the restart). Try again later.",
)
STOP_FIRST_409 = _answer(
    409,
    "ConflictException",
    "Changing the idle timeout of a running sandbox is not supported yet. "
    "Stop the sandbox, then update it.",
)
# stand-ins: no recorded body for these
OTHER_409 = _answer(409, "ConflictException", "held by an operator")
RANGE_400 = _answer(400, "ValidationException", "idleTimeoutMinutes must be between 15 and 10080")
NO_ROUTE = _answer(404, "com.example.rpc#UnknownOperationException", "unknown op")
GONE_404 = _answer(404, "ResourceNotFoundException", "no such sandbox")


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


class Env:
    def __init__(self, bff, tmp_path, monkeypatch, *, flag=True):
        from kiro_crew.dashboard.handlers import _shared

        self.ft = FakeTime()
        self.guard = SandboxGuard(tmp_path / "state", now=self.ft.now)
        self.log = CallLog(tmp_path / "calls.jsonl", clock=self.ft.now)
        self.client = BffClient(
            lambda: TOKEN,
            self.guard,
            endpoint=bff.endpoint,
            sleep=self.ft.sleep,
            clock=self.ft.clock,
            call_log=self.log,
        )
        self.reg = FakeRegistry()
        self.engine = SandboxLaunchEngine(client=self.client, registry_factory=lambda: self.reg)
        self.routes = SandboxRoutes(
            lambda: self.engine, run_in_thread=False, sleep=self.ft.sleep, now=self.ft.now
        )
        self.bff = bff

        async def allow(request, operation):
            return None

        monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
        monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
        monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: flag)

    def own(self, status="RUNNING", idle=60):
        sid = self.bff.add_sandbox(status, "kc-aaaaaa")
        self.bff.sandboxes[sid]["idleTimeoutMinutes"] = idle
        self.guard.record_created(dict(self.bff.sandboxes[sid]), "kc-aaaaaa")
        self.reg.rows[f"row-{sid}"] = Row(f"row-{sid}", "crew-a", f"crew-sbx-{sid}")
        return sid

    def call(self, method, path, body=None):
        async def go():
            app = web.Application()
            register_sandbox_routes(app, self.routes)
            async with TestClient(TestServer(app)) as c:
                if body is None:
                    r = await c.request(method, path)
                else:
                    r = await c.request(method, path, json=body)
                return r.status, await r.json()

        return asyncio.run(go())

    def put(self, sid, minutes):
        return self.call(
            "POST", f"/api/managed-environments/{sid}/idle-timeout", {"minutes": minutes}
        )

    def updates(self):
        return [r["body"] for r in self.bff.requests if r["op"] == "UpdateSandbox"]


@pytest.fixture
def env(bff, tmp_path, monkeypatch):
    return Env(bff, tmp_path, monkeypatch)


# -- the client call -----------------------------------------------------------------


def test_update_sandbox_sends_exactly_the_id_and_the_minutes(env):
    sid = env.own("STOPPED")
    got = env.client.update_sandbox(sid, idle_timeout_minutes=30)
    assert env.updates() == [{"execEnvironmentId": sid, "idleTimeoutMinutes": 30}]
    assert got["status"] == "STOPPED" and got["idleTimeoutMinutes"] == 30


def test_update_sandbox_refuses_a_foreign_id_before_any_call(env):
    other = env.bff.add_other("RUNNING")
    with pytest.raises(GuardRefused):
        env.client.update_sandbox(other, idle_timeout_minutes=30)
    assert env.bff.requests == []


def test_update_sandbox_is_never_retried(env):
    sid = env.own("RUNNING")
    env.bff.queue("UpdateSandbox", *BUSY_409)
    with pytest.raises(Exception):
        env.client.update_sandbox(sid, idle_timeout_minutes=30)
    assert len(env.updates()) == 1


def test_the_call_log_has_an_update_line_in_todays_form(env):
    sid = env.own("STOPPED")
    env.client.update_sandbox(sid, idle_timeout_minutes=30)
    rows = [json.loads(x) for x in (env.log.path).read_text().splitlines()]
    row = [r for r in rows if r["op"] == "UpdateSandbox"][-1]
    assert row["status"] == 200
    assert row["request_fields"] == ["execEnvironmentId", "idleTimeoutMinutes"]
    assert "idleTimeoutMinutes" in row["response_fields"] and sid not in json.dumps(row)


# -- the route -------------------------------------------------------------------------


@pytest.mark.parametrize("minutes", [14, 10081, "60", 60.5, True, None, 0])
def test_the_route_refuses_a_value_outside_15_to_10080_with_no_call(env, minutes):
    sid = env.own("STOPPED")
    n = len(env.bff.requests)
    status, body = env.put(sid, minutes)
    assert (status, body) == (400, {"error": IDLE_TEXT["range"]})
    assert "15 minutes to 7 days" in body["error"]
    assert len(env.bff.requests) == n and env.updates() == []


@pytest.mark.parametrize("minutes", [15, 10080])
def test_the_route_takes_both_ends_of_the_range(env, minutes):
    sid = env.own("STOPPED")
    assert env.put(sid, minutes) == (202, {"ok": True, "action": "idle-timeout"})
    assert env.updates() == [{"execEnvironmentId": sid, "idleTimeoutMinutes": minutes}]


def test_the_idle_timeout_route_wins_over_the_id_action_route(env):
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    register_sandbox_routes(app, env.routes)

    async def resolve(path):
        return (await app.router.resolve(make_mocked_request("POST", path, app=app))).handler

    got = asyncio.run(resolve("/api/managed-environments/env-001/idle-timeout"))
    assert got == env.routes.idle_timeout
    assert asyncio.run(resolve("/api/managed-environments/env-001/stop")) == env.routes.act


def test_the_route_is_absent_while_the_flag_is_off(bff, tmp_path, monkeypatch):
    e = Env(bff, tmp_path, monkeypatch, flag=False)
    sid = e.own("STOPPED")
    assert e.put(sid, 30) == (404, {"error": "not found"})
    assert e.bff.requests == []


def test_the_route_refuses_another_clients_sandbox_with_no_call(env):
    other = env.bff.add_other("STOPPED")
    assert env.put(other, 30) == (404, {"error": "not a sandbox of this crew client"})
    assert env.bff.naming(other) == []


def test_one_action_per_sandbox(env):
    sid = env.own("STOPPED")
    assert env.routes.actions.begin(sid, "stop")
    assert env.put(sid, 30) == (409, {"error": "another action is still running on this sandbox"})
    assert env.updates() == []


def test_stopped_takes_it_with_no_wait_and_the_list_shows_it(env):
    sid = env.own("STOPPED")
    assert env.put(sid, 30)[0] == 202
    assert env.bff.count("GetSandbox") == 0
    assert env.routes.actions.get(sid)["error"] == "" and "note" not in env.routes.actions.get(sid)
    item = {i["sandbox_id"]: i for i in env.routes.list_sync()["sandboxes"]}[sid]
    assert item["idle_timeout_minutes"] == 30 and item["status"] == "STOPPED"


def test_running_waits_for_the_restart_to_begin_then_for_running(env):
    sid = env.own("RUNNING")
    env.bff.get_statuses[sid].extend(["RUNNING", "PENDING", "RUNNING"])
    assert env.put(sid, 45)[0] == 202
    # the leave from RUNNING, then wait_running: it ended only after the second RUNNING
    assert env.bff.count("GetSandbox") == 3 and not env.bff.get_statuses[sid]
    assert env.routes.actions.get(sid)["error"] == "" and not env.routes.actions.get(sid)["running"]
    assert env.ft.sleeps[:2] == [routes_mod.POLL_INTERVAL_S, routes_mod.POLL_INTERVAL_S]


def test_running_with_no_restart_within_180_s_ends_with_the_not_begun_text(env):
    sid = env.own("RUNNING")
    assert env.put(sid, 45)[0] == 202
    got = env.routes.actions.get(sid)
    assert got["error"] == "" and got["note"] == IDLE_TEXT["not_begun"]
    polls = routes_mod.IDLE_RESTART_BEGIN_S / routes_mod.POLL_INTERVAL_S
    assert env.bff.count("GetSandbox") == polls == 18
    assert IDLE_TEXT["not_begun"] == "Saved. The restart hasn't begun yet; chats stop when it does."


def test_the_switch_400_shows_the_off_text_greys_for_10_minutes_and_does_not_stop_the_client(env):
    sid = env.own("RUNNING")
    env.bff.queue("UpdateSandbox", *SWITCH_400)
    assert env.put(sid, 45)[0] == 202
    assert env.routes.actions.get(sid)["error"] == IDLE_TEXT["off"]
    assert IDLE_TEXT["off"] == (
        "Idle stop is off for your account, so this sandbox doesn't stop on its own."
        " Stop it when you're done."
    )
    assert env.guard.stopped == ""
    n = env.bff.count("ListSandboxes")
    assert env.routes.list_sync()["idle_off"] is True  # a later list call runs
    assert env.bff.count("ListSandboxes") == n + 1
    at = env.routes._idle_off_at
    env.ft.t = at + 590  # the list's own call gap may add a second
    assert env.routes.list_sync()["idle_off"] is True
    env.ft.t = at + 601
    assert env.routes.list_sync()["idle_off"] is False


@pytest.mark.parametrize(
    "answer,want",
    [
        (BUSY_409, IDLE_TEXT["busy"]),
        (STOP_FIRST_409, IDLE_TEXT["stop_first"]),
        (OTHER_409, "held by an operator"),
        (RANGE_400, "idleTimeoutMinutes must be between 15 and 10080"),
        (NO_ROUTE, IDLE_TEXT["no_route"]),
    ],
)
def test_the_answers_texts(env, answer, want):
    sid = env.own("RUNNING")
    env.bff.queue("UpdateSandbox", *answer)
    assert env.put(sid, 45)[0] == 202
    assert env.routes.actions.get(sid)["error"] == want
    assert env.guard.stopped == ""
    assert IDLE_TEXT["busy"] == (
        "The sandbox is busy (starting, stopping or restarting). Try again in a moment."
    )
    assert IDLE_TEXT["stop_first"] == "Stop the sandbox, then change its idle timeout."
    assert IDLE_TEXT["no_route"] == "This Kiro service doesn't offer the idle timeout change yet."


def test_a_404_resource_not_found_is_todays_404_never_the_no_route_text(env):
    sid = env.own("RUNNING")
    env.bff.queue("UpdateSandbox", *GONE_404)
    assert env.put(sid, 45)[0] == 202
    err = env.routes.actions.get(sid)["error"]
    assert "ResourceNotFoundException" in err and err != IDLE_TEXT["no_route"]


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"__type": "x#ForbiddenException", "message": "no"}),
        (500, {"__type": "x#InternalFailureException", "message": "boom"}),
    ],
)
def test_403_and_5xx_are_the_guards_stop(env, answer):
    sid = env.own("RUNNING")
    env.bff.queue("UpdateSandbox", *answer)
    assert env.put(sid, 45)[0] == 202
    assert env.guard.stopped != ""
    assert env.routes.actions.get(sid)["error"]
