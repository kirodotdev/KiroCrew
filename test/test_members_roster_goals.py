"""The roster row's goal facts (``has_goal``, ``heartbeat_on``) and the refresh that
keeps them current (``kiro_crew.dashboard.members_roster_goals``).

Pinned here:

* a row reads both facts off the member's own goal record, false with no record;
* a record that cannot be read costs the row its facts, never the roster its answer;
* ``GET /api/members`` ships both on every row;
* one change listener is registered per process, and a goal write pushes the
  ``agents`` refresh frame on the serving loop, from any thread.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from member_goals_helpers import ORDINARY

from kiro_crew import member_goals
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard import members_roster_goals


def test_no_record_reads_as_no_goal_and_heartbeat_off() -> None:
    assert members_roster_goals.goal_facts("nobody", "nobody") == {
        "has_goal": False,
        "heartbeat_on": False,
    }


def test_a_goal_then_its_heartbeat_light_the_two_facts_in_turn() -> None:
    slug = "scout"
    member_goals.set_goal(slug, ORDINARY, "Keep main green", by="user")
    assert members_roster_goals.goal_facts(slug, ORDINARY) == {
        "has_goal": True,
        "heartbeat_on": False,
    }
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True})
    assert members_roster_goals.goal_facts(slug, ORDINARY) == {
        "has_goal": True,
        "heartbeat_on": True,
    }


def test_another_name_under_the_same_slug_reads_the_empty_record() -> None:
    slug = "scout"
    member_goals.set_goal(slug, ORDINARY, "Keep main green", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"heartbeat": True})
    assert members_roster_goals.goal_facts(slug, "Scout!") == {
        "has_goal": False,
        "heartbeat_on": False,
    }


def test_an_unreadable_record_costs_the_row_its_facts_not_the_roster_its_answer(
    monkeypatch,
) -> None:
    def _boom(slug: str, member: str) -> dict[str, Any]:
        raise member_goals.GoalsUnreadable("damaged")

    monkeypatch.setattr(member_goals, "read_goals", _boom)
    rows = [{"name": "a", "slug": "a"}, {"name": "b", "slug": "b"}]
    assert members_roster_goals.goal_facts_for_rows(rows) == {
        "a": {"has_goal": False, "heartbeat_on": False},
        "b": {"has_goal": False, "heartbeat_on": False},
    }


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    # The floor's own MonkeyPatch (D11): a test's `monkeypatch.undo()` must not
    # lift this patch.
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.mark.asyncio
async def test_the_roster_ships_both_facts_on_every_row(tmp_path) -> None:
    fake = SimpleNamespace(
        agents={
            "oncall": KiroCrewAgentConfig(kiro_agent="kirocrew"),
            "scribe": KiroCrewAgentConfig(kiro_agent="kirocrew"),
            "idle": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        },
        default_agent="oncall",
        memory_stores={},
        degraded_sections=frozenset(),
    )
    member_goals.set_goal("oncall", "oncall", "Keep main green", by="user")
    member_goals.set_schedules("oncall", "oncall", {"heartbeat": True})
    member_goals.set_goal("scribe", "scribe", "Ship the notes", by="user")
    state = _make_state(tmp_path)
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        async with TestClient(TestServer(_members_app(state))) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200
            rows = {r["name"]: r for r in (await resp.json())["members"]}
    assert (rows["oncall"]["has_goal"], rows["oncall"]["heartbeat_on"]) == (True, True)
    assert (rows["scribe"]["has_goal"], rows["scribe"]["heartbeat_on"]) == (True, False)
    assert (rows["idle"]["has_goal"], rows["idle"]["heartbeat_on"]) == (False, False)


class _State:
    def __init__(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.serving_loop = loop
        self.pushed: list[tuple[str, ...]] = []

    def push_refresh(self, *kinds: str) -> None:
        self.pushed.append(kinds)


def test_start_registers_one_listener_per_process(monkeypatch) -> None:
    monkeypatch.setattr(member_goals, "_change_listeners", [])
    monkeypatch.setattr(members_roster_goals, "_registered", False)
    members_roster_goals.start_roster_refresh(_State(None))
    members_roster_goals.start_roster_refresh(_State(None))
    assert member_goals._change_listeners == [members_roster_goals._on_goal_written]


@pytest.mark.asyncio
async def test_a_goal_write_pushes_the_agents_refresh_on_the_serving_loop(monkeypatch) -> None:
    monkeypatch.setattr(member_goals, "_change_listeners", [])
    monkeypatch.setattr(members_roster_goals, "_registered", False)
    state = _State(asyncio.get_running_loop())
    members_roster_goals.start_roster_refresh(state)
    pushed = asyncio.Event()
    state.push_refresh = lambda *kinds: (state.pushed.append(kinds), pushed.set())  # type: ignore[method-assign]
    # The writer runs on a worker thread, as the goals routes do. Waited on the
    # signal, under a lost-run ceiling (not a timing guess): a listener that
    # raised is swallowed by `_notify_change`, and this fails here, not at the
    # process-level timeout.
    worker = threading.Thread(target=member_goals._notify_change, args=("scout", ORDINARY, {}))
    worker.start()
    await asyncio.wait_for(pushed.wait(), timeout=60)
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert state.pushed == [("agents",)]


def test_a_goal_write_before_the_loop_is_bound_pushes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(member_goals, "_change_listeners", [])
    monkeypatch.setattr(members_roster_goals, "_registered", False)
    state = _State(None)
    members_roster_goals.start_roster_refresh(state)
    member_goals._notify_change("scout", ORDINARY, {})
    assert state.pushed == []
