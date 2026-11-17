"""``crew_list`` (remote-dispatch audit D1): the hub roster and its MCP body.

Drives :func:`remote_crew_list.list_crews` against stub crews behind one carrier,
the route's switch gate, and the MCP render and advertise gate. Pinned: only a
connected crew is probed; a version mismatch skips the headroom read; the
headroom is re-typed and carries the peer's ``open_workers``; no registry field
beyond id and name reaches a row; one crew's failure leaves the others listed.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew
from kiro_crew import mcp_crew_list
from kiro_crew.dashboard.handlers import remote_crew_list as crew_list
from kiro_crew.dashboard.handlers import remote_ingest
from kiro_crew.mcp_tools.dashboard_client import DashboardError


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def iter_any(self):
        yield self._raw


class Crews:
    """Stub carrier: per crew a state, a version, rosters and a headroom answer."""

    def __init__(self) -> None:
        self.crews: dict[str, dict[str, Any]] = {}
        self.paths: list[tuple[str, str]] = []
        self.params: list[Any] = []

    def add(self, iid: str, **over: Any) -> None:
        crew = {
            "state": "connected",
            "version": kiro_crew.__version__,
            "headroom": {
                "mem_free_gb": 12.5,
                "disk_free_gb": 80.0,
                "load_per_cpu": 0.3,
                "cpu_count": 8,
                "posture": "ample",
                "open_workers": 2,
                "measured_at": 1.5,
                "extra": "/home/secret",
            },
        }
        crew.update(over)
        self.crews[iid] = crew

    def status(self, iid: str):
        return SimpleNamespace(state=SimpleNamespace(value=self.crews[iid]["state"]))

    async def peer_capability(self, iid: str, path: str):
        self.paths.append((iid, path))
        crew = self.crews[iid]
        if crew.get("caps_raise"):
            raise RuntimeError("boom")
        if path == "/api/version":
            return True, {"version": crew["version"]}
        if path == "/api/agents":
            return True, {
                "agents": [{"name": "kirocrew-worker"}, {"name": "default"}],
                "default_agent": "default",
            }
        if path == "/api/models":
            return True, [{"model_name": "claude-opus"}, {"model_name": ""}]
        if path == "/api/workspaces":
            return True, {"workspaces": [{"name": "w", "path": "/srv/secret"}], "default": "w"}
        return True, []

    @asynccontextmanager
    async def proxy_request(self, iid, method, path, *, params=None, data=None, content_type=""):
        self.paths.append((iid, path))
        self.params.append(params)
        crew = self.crews[iid]
        if crew.get("stall"):
            raise asyncio.TimeoutError  # the carrier's own read timeout
        yield SimpleNamespace(
            status=crew.get("headroom_status", 200),
            headers={"Content-Type": "application/json"},
            content=_Content(json.dumps(crew["headroom"]).encode()),
        )


def _state(crews: Crews) -> Any:
    state = MagicMock()
    state.instances_manager = crews
    rows = lambda: [  # noqa: E731
        SimpleNamespace(id=i, name=f"name-{i}", ssh_host="host.example", aws_profile="p")
        for i in crews.crews
    ]
    state.instances_registry = SimpleNamespace(list=rows)
    return state


@pytest.fixture(autouse=True)
def _bindings(_floor_monkeypatch):
    _floor_monkeypatch.setattr(remote_ingest, "open_remote_bindings", lambda: {"a": 3})


def _list(crews: Crews) -> dict[str, dict[str, Any]]:
    rows, omitted = asyncio.run(crew_list.list_crews(_state(crews)))
    assert omitted == 0
    return {r["id"]: r for r in rows}


def test_a_connected_crew_lists_its_headroom_rosters_and_reservations():
    crews = Crews()
    crews.add("a")
    row = _list(crews)["a"]
    assert row["name"] == "name-a"
    assert row["crew_state"] == "connected"
    assert row["version"] == kiro_crew.__version__ and row["version_match"] is True
    assert row["headroom"] == {
        "mem_free_gb": 12.5,
        "disk_free_gb": 80.0,
        "load_per_cpu": 0.3,
        "measured_at": 1.5,
        "cpu_count": 8,
        "posture": "ample",
    }
    assert row["open_workers"] == 2 and row["bound_here"] == 3
    assert row["agents"] == ["kirocrew-worker", "default"] and row["default_agent"] == "default"
    assert row["models"] == ["claude-opus"]
    assert row["unavailable"] == []
    assert crews.params == [{"logins": "0"}]  # the roster never waits on login checks
    text = json.dumps(row)
    for leak in ("host.example", "/srv/secret", "/home/secret", '"p"'):
        assert leak not in text


def test_a_disconnected_crew_is_listed_but_never_probed():
    crews = Crews()
    crews.add("b", state="disconnected")
    row = _list(crews)["b"]
    assert row["crew_state"] == "disconnected"
    assert row["headroom"] is None and row["version_match"] is None
    assert crews.paths == []


def test_a_version_mismatch_skips_the_headroom_read():
    crews = Crews()
    crews.add("c", version="0.0.1")
    row = _list(crews)["c"]
    assert row["version_match"] is False and row["headroom"] is None
    assert ("c", "api/chat/headroom") not in crews.paths


def test_a_hostile_headroom_is_retyped():
    crews = Crews()
    crews.add(
        "d",
        headroom={
            "mem_free_gb": "lots",
            "disk_free_gb": -1,
            "posture": "<b>",
            "open_workers": 10**309,
            "cpu_count": "8",
        },
    )
    row = _list(crews)["d"]
    assert row["headroom"]["mem_free_gb"] is None and row["headroom"]["disk_free_gb"] is None
    assert row["headroom"]["posture"] == "unknown" and row["headroom"]["cpu_count"] is None
    assert row["open_workers"] is None


def test_one_failing_crew_does_not_fail_the_list():
    crews = Crews()
    crews.add("a")
    crews.add("e", stall=True)
    crews.add("f", headroom_status=404)
    crews.add("g", caps_raise=True)
    rows = _list(crews)
    assert rows["a"]["headroom"] is not None
    assert rows["e"]["unavailable"] == ["headroom"] and rows["e"]["agents"]
    assert rows["f"]["headroom"] is None and rows["f"]["unavailable"] == ["headroom"]
    assert rows["g"]["headroom"] is None and rows["g"]["version_match"] is None


def test_a_slow_model_catalog_costs_only_the_names(monkeypatch):
    from kiro_crew.dashboard import handlers_instances_peer

    async def _timed_out(mgr, instance_id):
        raise asyncio.TimeoutError  # what the roster bound raises on a cold catalog

    monkeypatch.setattr(handlers_instances_peer, "read_peer_capabilities", _timed_out)
    crews = Crews()
    crews.add("h")
    row = _list(crews)["h"]
    assert row["version_match"] is True and row["headroom"]["mem_free_gb"] == 12.5
    assert row["open_workers"] == 2
    assert row["agents"] == [] and row["unavailable"] == ["agents", "models"]


def test_past_the_cap_the_omitted_crews_are_counted(monkeypatch):
    monkeypatch.setattr(crew_list, "MAX_CREWS", 2)
    crews = Crews()
    for iid in ("c1", "c2", "c3"):
        crews.add(iid, state="disconnected")
    rows, omitted = asyncio.run(crew_list.list_crews(_state(crews)))
    assert [r["id"] for r in rows] == ["c1", "c2"] and omitted == 1
    out = mcp_crew_list.run_crew_list({}, _ctx(_Client({"crews": rows, "omitted": omitted})))
    assert "3 crew(s) registered" in out and "1 more crew(s)" in out


def test_the_route_refuses_while_the_switch_is_off(monkeypatch):
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    async def _internal(request):
        return None

    monkeypatch.setattr(crew_list, "_require_internal", _internal)
    monkeypatch.setattr(routes, "remote_dispatch_enabled", lambda: False)
    request = MagicMock()
    request.headers = {}
    resp = asyncio.run(crew_list.api_session_control_crews(request))
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "remote_dispatch_disabled"


def test_the_route_is_strict_internal():
    from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS

    assert mcp_crew_list.ROUTE in _STRICT_INTERNAL_API_PATHS


# ── MCP body ─────────────────────────────────────────────────────────────────


class _Client:
    def __init__(self, reply: dict[str, Any] | None = None, refuse: dict | None = None) -> None:
        self.reply, self.refuse = reply or {}, refuse
        self.calls: list[str] = []

    def get(self, path: str, *, session_key: str = "", timeout: float | None = None):
        self.calls.append(path)
        self.timeout = timeout
        if self.refuse is not None:
            raise DashboardError(self.refuse)
        return self.reply


def _ctx(client: _Client) -> Any:
    return SimpleNamespace(client=client, caller_key="dashboard:chat-1")


def test_the_tool_renders_each_crew():
    crews = Crews()
    crews.add("a")
    row = _list(crews)["a"]
    client = _Client({"crews": [row]})
    out = mcp_crew_list.run_crew_list({}, _ctx(client))
    assert client.calls == [mcp_crew_list.ROUTE]
    assert client.timeout > crew_list.ROSTER_TIMEOUT_S
    assert "`name-a` (crew=`a`): connected" in out
    assert "mem 12.5 GB free" in out and "open_workers 2, bound_here 3" in out
    assert "agents: kirocrew-worker, default (default default)" in out
    assert "models: claude-opus" in out


def test_a_mismatch_shows_without_the_version_text():
    row = {"id": "m", "name": "m", "crew_state": "connected", "version_match": False}
    out = mcp_crew_list.run_crew_list({}, _ctx(_Client({"crews": [row]})))
    assert "connected, version (MISMATCH)" in out


def test_the_tool_renders_an_out_of_range_integer():
    row = {"id": "x", "name": "x", "crew_state": "connected", "bound_here": 10**309}
    assert "bound_here 1" in mcp_crew_list.run_crew_list({}, _ctx(_Client({"crews": [row]})))


def test_the_tool_renders_an_empty_roster_and_a_refusal():
    assert "No remote crews" in mcp_crew_list.run_crew_list({}, _ctx(_Client({"crews": []})))
    refused = _Client(refuse={"error": "nope", "code": "conductor_mode_only"})
    assert mcp_crew_list.run_crew_list({}, _ctx(refused)).startswith("Error: could not list crews")


def test_the_tool_is_advertised_only_while_remote_dispatch_is_on():
    rows = [{"name": "crew_list"}, {"name": "session_status"}]
    assert mcp_crew_list.with_crew_list(rows, True) is rows
    assert [r["name"] for r in mcp_crew_list.with_crew_list(rows, False)] == ["session_status"]


# ── route gates and fault paths ───────────────────────────────────────────────


def _route_request(crews: Crews) -> Any:
    request = MagicMock()
    request.headers = {}
    request.app = {"state": _state(crews)}
    return request


@pytest.fixture
def _route_on(monkeypatch):
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    async def _internal(request):
        return None

    async def _authorized(request, verb):
        return "dashboard:chat-1"

    monkeypatch.setattr(crew_list, "_require_internal", _internal)
    monkeypatch.setattr(routes, "remote_dispatch_enabled", lambda: True)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(routes, "authorize_remote_dispatch", _authorized)
    return routes


def test_the_route_answers_the_roster_for_an_authorized_caller(_route_on):
    crews = Crews()
    crews.add("a")
    resp = asyncio.run(crew_list.api_session_control_crews(_route_request(crews)))
    body = json.loads(resp.body)
    assert resp.status == 200 and body["omitted"] == 0
    assert [r["id"] for r in body["crews"]] == ["a"]


def test_the_route_refuses_while_session_control_is_off(_route_on, monkeypatch):
    from kiro_crew.dashboard import session_control as sc

    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    resp = asyncio.run(crew_list.api_session_control_crews(_route_request(Crews())))
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "session_control_disabled"


def test_the_route_passes_the_caller_refusal_through(_route_on, monkeypatch):
    routes = _route_on

    async def _refused(request, verb):
        raise routes.CrewCallError(403, "conductor_mode_only", "conductor mode only")

    monkeypatch.setattr(routes, "authorize_remote_dispatch", _refused)
    resp = asyncio.run(crew_list.api_session_control_crews(_route_request(Crews())))
    assert resp.status == 403 and json.loads(resp.body)["code"] == "conductor_mode_only"


def test_unreadable_registry_and_bindings_read_as_empty(monkeypatch):
    def _boom(*_a):
        raise OSError("unreadable")

    monkeypatch.setattr(remote_ingest, "open_remote_bindings", _boom)
    crews = Crews()
    crews.add("a")
    row = _list(crews)["a"]
    assert row["bound_here"] == 0
    state = _state(crews)
    state.instances_registry = SimpleNamespace(list=_boom)
    assert asyncio.run(crew_list.list_crews(state)) == ([], 0)


def test_a_raising_probe_or_roster_marks_only_its_own_fields(monkeypatch):
    from kiro_crew.dashboard import handlers_instances_peer
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    async def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(routes, "check_version", _boom)
    monkeypatch.setattr(handlers_instances_peer, "read_peer_capabilities", _boom)
    crews = Crews()
    crews.add("a")
    row = _list(crews)["a"]
    assert row["headroom"] is None and row["agents"] == []
    assert sorted(row["unavailable"]) == ["agents", "headroom", "models"]
