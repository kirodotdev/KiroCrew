"""The ``kirocrew-guide`` HTTP routes, the post-success commit hook and the MCP shim.

Everything runs against a temp home and an in-process aiohttp app: no gateway,
no MCP process, no real service. The auth layer is replaced by a tiny middleware
that sets exactly the request attributes the real ``token_auth_middleware`` sets
(``internal_auth`` / ``user`` / ``app``), so every refusal here is the guide
handler's OWN check, not a middleware's.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import guide_catalog, mcp_guide
from kiro_crew.dashboard import guide_runs
from kiro_crew.dashboard.guide_runs import guide_store_for
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.state import _ChatSlot

REPO = Path(__file__).resolve().parents[1]

CREWMATE = [{"id": "crewmate.create", "params": {"name": "Scout"}}]
MCP_OPEN = [{"id": "mcp.open_add", "params": {}}]


class FakeState:
    """The slice of ``DashboardState`` the guide routes read."""

    owner_id = ""

    def __init__(self) -> None:
        self._slots: dict[str, _ChatSlot] = {}
        self.frames: list[tuple[str, dict[str, Any]]] = []

    def open_slot(self, key: str) -> _ChatSlot:
        slot = _ChatSlot(key)
        self._slots[key] = slot
        return slot

    def get_slot(self, name: str) -> _ChatSlot | None:
        return self._slots.get(name)

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        return 1


@web.middleware
async def _fake_auth(request: web.Request, handler):
    who = request.headers.get("X-Test-Auth", "")
    if who == "internal":
        request["internal_auth"] = True
        request["app"] = ""
    elif who == "internal-app":
        request["internal_auth"] = True
        request["app"] = "some-app"
    elif who == "owner":
        request["user"] = "local-app"
        request["app"] = ""
    elif who == "app":
        request["user"] = "local-app"
        request["app"] = "some-app"
    return await handler(request)


@pytest.fixture(autouse=True)
def _quiet_sel(monkeypatch):
    class _Null:
        def log_api_access(self, **_kw):
            return None

    monkeypatch.setattr(guide_routes, "sel", lambda: _Null())


def _run(coro_fn, state: FakeState, extra_routes=()):
    async def _main():
        app = web.Application(middlewares=[_fake_auth])
        app["state"] = state
        guide_routes.register_guide_routes(app)
        for method, path, handler in extra_routes:
            app.router.add_route(method, path, handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await coro_fn(client)
        finally:
            await client.close()

    return asyncio.run(_main())


def agent(sk: str, auth: str = "internal") -> dict[str, str]:
    return {"X-Test-Auth": auth, "X-Session-Key": sk}


OWNER = {"X-Test-Auth": "owner"}


# ── agent half: who may start a guide, and for which tab ──


def test_start_binds_the_callers_own_live_slot():
    state = FakeState()
    state.open_slot("chat-1")

    async def go(c):
        r = await c.post(
            "/api/guide/agent/start", json={"actions": CREWMATE}, headers=agent("dashboard:chat-1")
        )
        return r.status, await r.json()

    status, body = _run(go, state)
    assert status == 200, body
    assert body["slot_key"] == "chat-1"
    assert body["status"] == "offered"
    assert body["delivered_clients"] == 1
    assert state.frames and state.frames[0][0] == "guide_update"
    stored = guide_store_for(state)._guides[body["guide_id"]]
    assert stored.session_key == "dashboard:chat-1"


@pytest.mark.parametrize(
    "headers, status, code",
    [
        # Owner cookie on the agent half: no internal secret, so refused.
        (
            {"X-Test-Auth": "owner", "X-Session-Key": "dashboard:chat-1"},
            403,
            "internal_secret_required",
        ),
        ({"X-Session-Key": "dashboard:chat-1"}, 403, "internal_secret_required"),
        (agent("dashboard:chat-1", "internal-app"), 403, "app_caller"),
        (agent("subagent:abc"), 403, "subagent_caller"),
        (agent(""), 400, "missing_session_key"),
        # A key with no live slot (e.g. a surface-registry-only session).
        (agent("dashboard:chat-gone"), 409, "no_live_slot"),
    ],
)
def test_agent_half_refuses_every_caller_that_is_not_a_live_tab(headers, status, code):
    state = FakeState()
    state.open_slot("chat-1")

    async def go(c):
        r = await c.post("/api/guide/agent/start", json={"actions": CREWMATE}, headers=headers)
        return r.status, await r.json()

    got, body = _run(go, state)
    assert (got, body.get("code")) == (status, code)
    assert guide_store_for(state)._guides == {}


def test_unattended_and_app_scoped_slots_cannot_start():
    state = FakeState()
    state.open_slot("cron-job1")
    app_slot = state.open_slot("chat-app")
    app_slot._app = "mochi"

    async def go(c):
        a = await c.post(
            "/api/guide/agent/start",
            json={"actions": CREWMATE},
            headers=agent("dashboard:cron-job1"),
        )
        b = await c.post(
            "/api/guide/agent/start",
            json={"actions": CREWMATE},
            headers=agent("dashboard:chat-app"),
        )
        return (a.status, (await a.json())["code"]), (b.status, (await b.json())["code"])

    assert _run(go, state) == ((403, "unattended_caller"), (403, "app_scoped_caller"))


def test_no_request_field_can_name_a_slot():
    state = FakeState()
    state.open_slot("chat-1")
    state.open_slot("chat-2")

    async def go(c):
        r = await c.post(
            "/api/guide/agent/start",
            json={"actions": CREWMATE, "slot_key": "chat-2"},
            headers=agent("dashboard:chat-1"),
        )
        return r.status, await r.json()

    status, body = _run(go, state)
    assert status == 400 and body["code"] == "invalid_body"


def test_a_foreign_caller_sees_only_absence():
    state = FakeState()
    state.open_slot("chat-1")
    state.open_slot("chat-2")

    async def go(c):
        r = await c.post(
            "/api/guide/agent/start", json={"actions": CREWMATE}, headers=agent("dashboard:chat-1")
        )
        gid = (await r.json())["guide_id"]
        foreign = agent("dashboard:chat-2")
        s = await c.get(f"/api/guide/agent/status?guide_id={gid}", headers=foreign)
        latest = await c.get("/api/guide/agent/status", headers=foreign)
        x = await c.post("/api/guide/agent/cancel", json={"guide_id": gid}, headers=foreign)
        mine = await c.get(
            f"/api/guide/agent/status?guide_id={gid}", headers=agent("dashboard:chat-1")
        )
        return [
            (s.status, (await s.json())["code"]),
            (latest.status, (await latest.json())["code"]),
            (x.status, (await x.json())["code"]),
            (mine.status, (await mine.json())["status"]),
        ]

    assert _run(go, state) == [
        (404, "guide_not_found"),
        (404, "guide_not_found"),
        (404, "guide_not_found"),
        (200, "offered"),
    ]


def test_a_closed_slot_loses_its_agent_half():
    state = FakeState()
    state.open_slot("chat-1")

    async def go(c):
        r = await c.post(
            "/api/guide/agent/start", json={"actions": CREWMATE}, headers=agent("dashboard:chat-1")
        )
        gid = (await r.json())["guide_id"]
        del state._slots["chat-1"]
        s = await c.get(
            f"/api/guide/agent/status?guide_id={gid}", headers=agent("dashboard:chat-1")
        )
        return s.status, (await s.json())["code"]

    assert _run(go, state) == (409, "no_live_slot")


def test_one_live_guide_per_slot_and_cancel_frees_it():
    state = FakeState()
    state.open_slot("chat-1")
    h = agent("dashboard:chat-1")

    async def go(c):
        a = await c.post("/api/guide/agent/start", json={"actions": CREWMATE}, headers=h)
        gid = (await a.json())["guide_id"]
        b = await c.post("/api/guide/agent/start", json={"actions": CREWMATE}, headers=h)
        x = await c.post("/api/guide/agent/cancel", json={"guide_id": gid}, headers=h)
        again = await c.post("/api/guide/agent/start", json={"actions": CREWMATE}, headers=h)
        return b.status, (await x.json())["status"], again.status

    assert _run(go, state) == (409, "cancelled", 200)


def test_actions_route_lists_the_catalog_for_a_live_tab_only():
    state = FakeState()
    state.open_slot("chat-1")

    async def go(c):
        ok = await c.get("/api/guide/agent/actions", headers=agent("dashboard:chat-1"))
        no = await c.get(
            "/api/guide/agent/actions", headers=OWNER | {"X-Session-Key": "dashboard:chat-1"}
        )
        return ok.status, [a["id"] for a in (await ok.json())["actions"]], no.status

    assert _run(go, state) == (200, ["settings.show", "crewmate.create", "mcp.open_add"], 403)


# ── browser half ──


def _started(state: FakeState) -> dict[str, Any]:
    state.open_slot("chat-1")
    return guide_store_for(state).start(
        slot_key="chat-1", session_key="dashboard:chat-1", actions=CREWMATE
    )


@pytest.mark.parametrize("auth", ["", "app", "internal"])
def test_browser_half_is_owner_cookie_only(auth):
    state = FakeState()
    g = _started(state)

    async def go(c):
        h = {"X-Test-Auth": auth} if auth else {}
        p = await c.get("/api/guide/pending", headers=h)
        cl = await c.post(
            "/api/guide/claim",
            json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": g["revision"]},
            headers=h,
        )
        return p.status, cl.status

    statuses = _run(go, state)
    assert all(s in (401, 403) for s in statuses), statuses
    assert guide_store_for(state)._guides[g["guide_id"]].owner_tab is None


def test_browser_cannot_report_a_commit_step_done():
    state = FakeState()
    g = _started(state)

    async def go(c):
        r = await c.post(
            "/api/guide/claim",
            json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": g["revision"]},
            headers=OWNER,
        )
        cur = await r.json()
        for _ in range(2):  # goal, name (ui steps)
            r = await c.post(
                "/api/guide/progress",
                json={
                    "guide_id": cur["guide_id"],
                    "tab_id": "t1",
                    "revision": cur["revision"],
                    "action_index": cur["action_index"],
                    "step_index": cur["step_index"],
                    "outcome": "observed",
                },
                headers=OWNER,
            )
            cur = await r.json()
        r = await c.post(
            "/api/guide/progress",
            json={
                "guide_id": cur["guide_id"],
                "tab_id": "t1",
                "revision": cur["revision"],
                "action_index": 0,
                "step_index": 2,
                "outcome": "observed",
            },
            headers=OWNER,
        )
        return cur["step_index"], r.status, (await r.json())["code"]

    assert _run(go, state) == (2, 409, "commit_step_requires_server_evidence")


def test_second_tab_needs_explicit_take_over_and_stale_revision_is_refused():
    state = FakeState()
    g = _started(state)

    async def go(c):
        a = await (
            await c.post(
                "/api/guide/claim",
                json={"guide_id": g["guide_id"], "tab_id": "tA", "revision": g["revision"]},
                headers=OWNER,
            )
        ).json()
        b = await c.post(
            "/api/guide/claim",
            json={"guide_id": g["guide_id"], "tab_id": "tB", "revision": a["revision"]},
            headers=OWNER,
        )
        stale = await c.post(
            "/api/guide/claim",
            json={
                "guide_id": g["guide_id"],
                "tab_id": "tB",
                "revision": g["revision"],
                "take_over": True,
            },
            headers=OWNER,
        )
        take = await c.post(
            "/api/guide/claim",
            json={
                "guide_id": g["guide_id"],
                "tab_id": "tB",
                "revision": a["revision"],
                "take_over": True,
            },
            headers=OWNER,
        )
        return (
            (b.status, (await b.json())["code"]),
            (await stale.json())["code"],
            (await take.json())["owner_tab"],
        )

    assert _run(go, state) == ((409, "owned_elsewhere"), "stale_revision", "tB")


# ── the post-success commit hook ──


def _at_commit(state: FakeState, actions=CREWMATE, tab="t1") -> dict[str, Any]:
    state.open_slot("chat-1")
    store = guide_store_for(state)
    g = store.start(slot_key="chat-1", session_key="dashboard:chat-1", actions=actions)
    g = store.claim(guide_id=g["guide_id"], tab_id=tab, revision=g["revision"])
    while (
        guide_runs.catalog.step_kind(g["actions"][g["action_index"]]["id"], g["step_index"]) == "ui"
    ):
        g = store.progress(
            guide_id=g["guide_id"],
            tab_id=tab,
            revision=g["revision"],
            action_index=g["action_index"],
            step_index=g["step_index"],
            outcome="observed",
        )
    return g


def _guide_headers(g: dict[str, Any], tab="t1", **over) -> dict[str, str]:
    h = {
        "X-Guide-Id": g["guide_id"],
        "X-Guide-Tab": tab,
        "X-Guide-Revision": str(g["revision"]),
    }
    h.update(over)
    return h


def _hooked(impl):
    async def route(request):
        return await guide_routes.run_guided_crewmate_create(request, impl)

    return route


CREATED = {"ok": True, "name": "Scout", "member_id": "m_123", "memory_store": "x"}


def _commit(state, g, impl, *, headers=None):
    async def go(c):
        r = await c.post("/x", json={}, headers=OWNER | (headers or _guide_headers(g)))
        return r.status

    status = _run(go, state, extra_routes=[("POST", "/x", _hooked(impl))])
    return status, guide_store_for(state)._guides[g["guide_id"]]


def test_success_is_the_sole_completion_and_association_precedes_the_await():
    state = FakeState()
    g = _at_commit(state)
    seen: dict[str, Any] = {}

    async def impl(request):
        seen["pending"] = guide_store_for(state)._guides[g["guide_id"]].pending_commit
        return web.json_response(CREATED)

    status, stored = _commit(state, g, impl)
    assert status == 200
    assert seen["pending"], "the request must be associated BEFORE the handler awaits"
    assert stored.status == "completed"
    assert stored.actions[0]["result"] == {"member_id": "m_123", "name": "Scout"}
    assert stored.pending_commit is None and guide_store_for(state)._commits == {}


@pytest.mark.parametrize(
    "response",
    [
        web.json_response(
            {"error": "Agent 'Scout' already exists", "code": "agent_exists"}, status=409
        ),
        web.json_response({"ok": True, "name": "Scout"}),  # no member_id
        web.json_response({"ok": False, "member_id": "m", "name": "Scout"}),
        web.Response(text="not json"),
        web.json_response([CREATED]),
    ],
)
def test_failure_or_unknown_result_never_completes(response):
    state = FakeState()
    g = _at_commit(state)

    async def impl(request):
        return response

    _status, stored = _commit(state, g, impl)
    assert stored.status == "active" and stored.step_index == 2
    assert stored.pending_commit is None and "result" not in stored.actions[0]


def test_a_raising_handler_releases_the_association():
    state = FakeState()
    g = _at_commit(state)

    async def impl(request):
        raise RuntimeError("boom")

    status, stored = _commit(state, g, impl)
    assert status == 500
    assert stored.status == "active" and stored.pending_commit is None


def test_cancel_during_the_save_cannot_be_revived_by_its_success():
    state = FakeState()
    g = _at_commit(state)

    async def impl(request):
        cur = guide_store_for(state)._guides[g["guide_id"]]
        guide_store_for(state).cancel_by_tab(
            guide_id=g["guide_id"], tab_id="t1", revision=cur.revision
        )
        return web.json_response(CREATED)

    status, stored = _commit(state, g, impl)
    assert status == 200  # the human's save itself is never blocked or altered
    assert stored.status == "cancelled"
    assert "result" not in stored.actions[0]


def test_expiry_during_the_save_cannot_be_revived():
    state = FakeState()
    now = [1000.0]
    state._guide_store = guide_runs.GuideStore(clock=lambda: now[0])
    g = _at_commit(state)

    async def impl(request):
        now[0] += guide_runs.GUIDE_TTL_SECONDS + 1
        return web.json_response(CREATED)

    _status, stored = _commit(state, g, impl)
    assert stored.status == "expired"


@pytest.mark.parametrize(
    "case",
    ["other_tab", "stale_revision", "not_owner", "bad_revision_text"],
)
def test_a_save_that_does_not_match_the_waiting_step_does_not_count(case):
    state = FakeState()
    g = _at_commit(state)
    headers = OWNER | _guide_headers(g)
    if case == "other_tab":
        headers = OWNER | _guide_headers(g, tab="t2")
    elif case == "stale_revision":
        headers = OWNER | _guide_headers(g, **{"X-Guide-Revision": str(g["revision"] - 1)})
    elif case == "not_owner":
        headers = {"X-Test-Auth": "app"} | _guide_headers(g)
    elif case == "bad_revision_text":
        headers = OWNER | _guide_headers(g, **{"X-Guide-Revision": f"+{g['revision']}"})

    async def impl(request):
        return web.json_response(CREATED)

    _status, stored = _commit(state, g, impl, headers=headers)
    assert stored.status == "active" and stored.step_index == 2


@pytest.mark.parametrize("revision", ["²", "①", "١"])
def test_a_non_ascii_digit_revision_is_refused_not_raised(revision):
    # str.isdigit() accepts these while int() rejects them; a ValueError here
    # would escape begin_commit and fail the owner's own save with a 500.
    state = FakeState()
    g = _at_commit(state)
    store = guide_store_for(state)
    token = store.begin_commit(
        guide_id=g["guide_id"],
        tab_id="t1",
        revision=revision,
        kind=guide_catalog.ACTION_CREWMATE_CREATE,
    )
    assert token is None
    stored = store._guides[g["guide_id"]]
    assert stored.status == "active" and stored.step_index == 2


def test_a_commit_of_another_kind_is_not_associated_with_the_crewmate_step():
    state = FakeState()
    g = _at_commit(state)
    store = guide_store_for(state)
    token = store.begin_commit(
        guide_id=g["guide_id"],
        tab_id="t1",
        revision=str(g["revision"]),
        kind=guide_catalog.ACTION_MCP_OPEN_ADD,
    )
    assert token is None
    stored = store._guides[g["guide_id"]]
    assert stored.status == "active" and stored.step_index == 2


def test_two_concurrent_saves_credit_at_most_one():
    state = FakeState()
    g = _at_commit(state)
    store = guide_store_for(state)
    first = store.begin_commit(
        guide_id=g["guide_id"], tab_id="t1", revision=str(g["revision"]), kind="crewmate.create"
    )
    second = store.begin_commit(
        guide_id=g["guide_id"], tab_id="t1", revision=str(g["revision"]), kind="crewmate.create"
    )
    assert first and second is None
    assert store.finish_commit(first, {"member_id": "m", "name": "n"})["status"] == "completed"
    # A replayed token is retired.
    assert store.finish_commit(first, {"member_id": "m", "name": "n"}) is None


def test_the_public_agents_create_route_is_wired_through_the_hook(monkeypatch):
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    state = FakeState()
    g = _at_commit(state)

    async def impl(request):
        return web.json_response(CREATED)

    monkeypatch.setattr(agents_handlers, "_api_kirocrew_agents_create", impl)

    async def go(c):
        r = await c.post("/api/agents", json={}, headers=OWNER | _guide_headers(g))
        return r.status

    status = _run(
        go,
        state,
        extra_routes=[("POST", "/api/agents", agents_handlers.api_kirocrew_agents_create)],
    )
    assert status == 200
    assert guide_store_for(state)._guides[g["guide_id"]].status == "completed"


def test_mcp_open_add_only_points_at_the_existing_add_form():
    """``mcp.open_add`` is UI-only: it completes on reaching the form, never a save."""
    assert guide_catalog.commit_step_index("mcp.open_add") is None
    assert [st.key for st in guide_catalog.ACTIONS["mcp.open_add"].steps] == [
        "servers-tab",
        "add",
    ]
    assert guide_catalog.ACTIONS["mcp.open_add"].mutates is False
    assert guide_catalog.validate_actions(MCP_OPEN) == [
        {"id": "mcp.open_add", "params": {}, "step_count": 2}
    ]
    for params in ({"name": "echo"}, {"spec": {"command": "echo"}}):
        with pytest.raises(guide_catalog.GuideCatalogError):
            guide_catalog.validate_actions([{"id": "mcp.open_add", "params": params}])

    state = FakeState()
    state.open_slot("chat-1")
    store = guide_store_for(state)
    g = store.start(slot_key="chat-1", session_key="dashboard:chat-1", actions=MCP_OPEN)
    g = store.claim(guide_id=g["guide_id"], tab_id="t1", revision=g["revision"])
    for step in (0, 1):
        # No save can be credited on any step: there is no commit step to associate.
        assert (
            store.begin_commit(
                guide_id=g["guide_id"],
                tab_id="t1",
                revision=str(g["revision"]),
                kind="mcp.open_add",
            )
            is None
        )
        g = store.progress(
            guide_id=g["guide_id"],
            tab_id="t1",
            revision=g["revision"],
            action_index=0,
            step_index=step,
            outcome="observed",
        )
    assert g["status"] == "completed" and "result" not in g["actions"][0]


def test_no_mcp_mutation_is_wired_to_the_guide_hook():
    from kiro_crew.dashboard.handlers import mcp_custom

    assert not hasattr(guide_routes, "_EVIDENCE")
    assert "run_guided_crewmate_create" not in (
        REPO / "src/kiro_crew/dashboard/handlers/mcp_custom.py"
    ).read_text(encoding="utf-8")
    assert mcp_custom.api_mcp_custom_add


# ── route registration and the strict-internal prefix ──


def test_server_route_table_matches_the_guide_module():
    app = web.Application()
    guide_routes.register_guide_routes(app)
    mine = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    text = (REPO / "src/kiro_crew/dashboard/server.py").read_text(encoding="utf-8")
    server = set(re.findall(r'\("(GET|POST)", "(/api/guide/[a-z/]+)", "api_guide_[a-z_]+"\)', text))
    assert server == mine
    for method, path in mine:
        name = re.search(
            rf'\("{method}", "{re.escape(path)}", "(api_guide_[a-z_]+)"\)', text
        ).group(1)
        assert callable(getattr(guide_routes, name))


def test_only_the_agent_half_is_strict_internal():
    from kiro_crew.dashboard import server

    strict = server._STRICT_INTERNAL_API_PATHS
    assert "/api/guide/agent" in strict
    assert "/api/guide" not in strict
    for p in ("/api/guide/pending", "/api/guide/claim", "/api/guide/progress"):
        assert not any(p == s or p.startswith(s + "/") for s in strict)


# ── MCP shim: schemas and statelessness ──


def test_no_tool_takes_a_session_slot_or_tab():
    tools = {t["name"]: t for t in mcp_guide._list_tools()}
    assert set(tools) == {"guide_list_actions", "guide_start", "guide_status", "guide_cancel"}
    blob = json.dumps([t["inputSchema"] for t in tools.values()]).lower()
    for word in ("session", "slot", "tab_id", "owner_tab"):
        assert word not in blob
    enum = tools["guide_start"]["inputSchema"]["properties"]["actions"]["items"]["properties"][
        "id"
    ]["enum"]
    assert enum == list(guide_catalog.ACTIONS)
    assert "autoApprove" not in json.dumps(tools)


def test_shim_sends_the_strictly_verified_key_per_call(monkeypatch):
    sent: list[tuple[str, str, Any]] = []
    keys = iter(["dashboard:chat-1", "dashboard:chat-2"])
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: (next(keys), ""))
    monkeypatch.setattr(
        mcp_guide,
        "_post",
        lambda path, body, session_key: sent.append((path, session_key, body))
        or {"guide_id": "g_1"},
    )
    monkeypatch.setattr(
        mcp_guide,
        "_get",
        lambda path, session_key: sent.append((path, session_key, None)) or {"guide_id": "g_1"},
    )

    mcp_guide._call_tool_inner("guide_start", {"actions": CREWMATE})
    mcp_guide._call_tool_inner("guide_status", {"guide_id": "g_1"})
    assert sent == [
        ("/api/guide/agent/start", "dashboard:chat-1", {"actions": CREWMATE}),
        ("/api/guide/agent/status?guide_id=g_1", "dashboard:chat-2", None),
    ]


def test_shim_refuses_without_strict_identity_and_never_calls_out(monkeypatch):
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("", "Error: no identity"))

    def boom(*_a, **_k):
        raise AssertionError("no network call without a verified key")

    monkeypatch.setattr(mcp_guide, "_post", boom)
    monkeypatch.setattr(mcp_guide, "_get", boom)
    for name in ("guide_list_actions", "guide_start", "guide_status", "guide_cancel"):
        assert (
            mcp_guide._call_tool_inner(name, {"guide_id": "g_1", "actions": CREWMATE})
            == "Error: no identity"
        )


def test_shim_routes_each_tool_to_its_endpoint(monkeypatch):
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:chat-1", ""))
    monkeypatch.setattr(
        mcp_guide, "_get", lambda path, session_key: calls.append((path, None)) or {"actions": []}
    )
    monkeypatch.setattr(
        mcp_guide,
        "_post",
        lambda path, body, session_key: calls.append((path, body)) or {"status": "cancelled"},
    )

    assert json.loads(mcp_guide._call_tool_inner("guide_list_actions", {})) == {"actions": []}
    mcp_guide._call_tool_inner("guide_status", {})
    assert json.loads(mcp_guide._call_tool_inner("guide_cancel", {"guide_id": "g_1"})) == {
        "status": "cancelled"
    }
    assert calls == [
        ("/api/guide/agent/actions", None),
        ("/api/guide/agent/status", None),
        ("/api/guide/agent/cancel", {"guide_id": "g_1"}),
    ]


def test_shim_surfaces_a_gateway_error_and_refuses_unknown_tools(monkeypatch):
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:chat-1", ""))
    monkeypatch.setattr(mcp_guide, "_get", lambda path, session_key: {"error": "no tab attached"})

    assert mcp_guide._call_tool_inner("guide_status", {}) == "Error: no tab attached"
    assert mcp_guide._call_tool_inner("guide_open", {}) == "Error: unknown tool 'guide_open'"


def test_shim_validates_only_known_tools():
    assert mcp_guide._validate_args("not_a_tool", {"x": 1}) == {"x": 1}
    assert mcp_guide._validate_args("guide_status", {"guide_id": "g_1"}) == {"guide_id": "g_1"}


def test_strict_session_key_names_the_parent_session_remedy(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mcp_guide,
        "require_strict_session_key",
        lambda message, server: seen.append((message, server)) or ("", message),
    )

    _, err = mcp_guide._strict_session_key()
    assert "parent session" in err
    assert seen[0][1] == mcp_guide.SERVER_NAME


def test_shim_module_holds_no_per_caller_state():
    mutable = {
        n
        for n, v in vars(mcp_guide).items()
        if not n.startswith("__")
        and isinstance(v, (dict, list, set))
        and n not in {"_ACTION_ITEM_SCHEMA", "MCP_GUIDE_SCHEMAS"}  # static, imported/const
    }
    assert mutable == set()


def test_shim_schema_rejects_an_injected_guide_id():
    from kiro_crew.validation import MCP_GUIDE_SCHEMAS, validate_tool_args

    with pytest.raises(Exception):
        validate_tool_args({"guide_id": "../x"}, MCP_GUIDE_SCHEMAS["guide_cancel"])


# ── catalog: aligned with the page that renders it ──


def _ts(path: str) -> str:
    return (REPO / "website/src" / path).read_text(encoding="utf-8")


def test_crewmate_caps_match_the_real_form():
    flow = _ts("components/MeetCrewmatesFlow.tsx")
    job = int(re.search(r"const JOB_MAX = (\d+)", flow).group(1))
    name = int(re.search(r"const NAME_MAX = (\d+)", flow).group(1))
    assert guide_catalog._GOAL_MAX_CHARS == job
    assert guide_catalog._NAME_MAX_CHARS == name
    ok = [{"id": "crewmate.create", "params": {"name": "a" * name, "goal": "g" * job}}]
    assert guide_catalog.validate_actions(ok)
    for params in ({"goal": "g" * (job + 1)}, {"name": "a" * (name + 1)}):
        with pytest.raises(guide_catalog.GuideCatalogError):
            guide_catalog.validate_actions([{"id": "crewmate.create", "params": params}])


def test_every_setting_the_page_refuses_is_refused_here_too():
    src = _ts("guide/guideActions.ts")
    tabs = set(
        re.findall(
            r"'([a-z-]+)'", re.search(r"SENSITIVE_TABS[^=]*= new Set\(\[([^\]]*)\]", src).group(1)
        )
    )
    ids = set(
        re.findall(
            r"'([a-z0-9.-]+)'",
            re.search(r"SENSITIVE_IDS[^=]*= new Set\(\[([^\]]*)\]", src, re.S).group(1),
        )
    )
    assert tabs and ids
    cred = re.compile(re.search(r"const CREDENTIAL_RE = /([^/]+)/i", src).group(1), re.I)
    registry = json.loads(guide_catalog._REGISTRY_PATH.read_text(encoding="utf-8"))["settings"]
    guidable = guide_catalog.guidable_settings()
    leaked = [
        e["id"]
        for e in registry
        if e["id"] in guidable
        and (
            e.get("tab") in tabs
            or e["id"] in ids
            or (
                e.get("type") == "input"
                and (cred.search(e["id"]) or cred.search(e.get("label", "")))
            )
        )
    ]
    assert leaked == []
    assert "chat.response-verbosity" in guidable
