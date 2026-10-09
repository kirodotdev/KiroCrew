"""The ``kirocrew-guide`` HTTP routes, the post-success commit hook and the MCP shim.

Everything runs against a temp home and an in-process aiohttp app: no gateway,
no MCP process, no real service. The auth layer is replaced by a tiny middleware
that sets exactly the request attributes the real ``token_auth_middleware`` sets
(``internal_auth`` / ``user`` / ``app``), so every refusal here is the guide
handler's OWN check, not a middleware's.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from guide_route_helpers import CREWMATE, FakeState, agent, run_guide_app

from kiro_crew import guide_catalog, mcp_guide
from kiro_crew.dashboard import guide_runs
from kiro_crew.dashboard.guide_runs import guide_store_for
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.state import _ChatSlot

REPO = Path(__file__).resolve().parents[1]

MCP_OPEN = [{"id": "mcp.open_add", "params": {}}]


@pytest.fixture(autouse=True)
def _quiet_sel(monkeypatch):
    class _Null:
        def log_api_access(self, **_kw):
            return None

    monkeypatch.setattr(guide_routes, "sel", lambda: _Null())


_run = run_guide_app


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


def test_a_second_start_replaces_the_first_and_says_so():
    state = FakeState()
    state.open_slot("chat-1")

    async def go(c):
        out = []
        for _ in range(2):
            r = await c.post(
                "/api/guide/agent/start",
                json={"actions": CREWMATE},
                headers=agent("dashboard:chat-1"),
            )
            out.append((r.status, await r.json()))
        return out

    (s1, first), (s2, second) = _run(go, state)
    assert (s1, s2) == (200, 200), second
    assert first["superseded"] == [] and second["superseded"] == [first["guide_id"]]
    # The tab hears the old guide end before the new one is offered.
    updates = [p["guide"] for kind, p in state.frames if kind == "guide_update"]
    assert [(g["guide_id"], g["status"]) for g in updates[-2:]] == [
        (first["guide_id"], "cancelled"),
        (second["guide_id"], "offered"),
    ]
    assert updates[-2]["reason"] == "superseded"


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


def test_one_live_guide_per_slot_a_new_offer_replaces_it():
    state = FakeState()
    state.open_slot("chat-1")
    h = agent("dashboard:chat-1")

    async def go(c):
        a = await c.post("/api/guide/agent/start", json={"actions": CREWMATE}, headers=h)
        gid = (await a.json())["guide_id"]
        b = await c.post("/api/guide/agent/start", json={"actions": CREWMATE}, headers=h)
        old = await c.get(f"/api/guide/agent/status?guide_id={gid}", headers=h)
        x = await c.post("/api/guide/agent/cancel", json={"guide_id": gid}, headers=h)
        latest = await c.get("/api/guide/agent/status", headers=h)
        return (
            b.status,
            (await old.json())["status"],
            x.status,
            (await latest.json())["guide_id"] == (await b.json())["guide_id"],
        )

    # The first is cancelled by the second, so cancelling it again is refused,
    # and the conversation's guide is the new one.
    assert _run(go, state) == (200, "cancelled", 409, True)


def test_actions_route_lists_the_catalog_for_a_live_tab_only():
    state = FakeState()
    state.open_slot("chat-1")

    async def go(c):
        ok = await c.get("/api/guide/agent/actions", headers=agent("dashboard:chat-1"))
        no = await c.get(
            "/api/guide/agent/actions", headers=OWNER | {"X-Session-Key": "dashboard:chat-1"}
        )
        return ok.status, [a["id"] for a in (await ok.json())["actions"]], no.status

    assert _run(go, state) == (
        200,
        ["settings.show", "crewmate.create", "mcp.open_add", "ui.show"],
        403,
    )


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
        rp = await c.post(
            "/api/guide/replan",
            json={
                "guide_id": g["guide_id"],
                "tab_id": "t1",
                "revision": g["revision"],
                "action_index": 0,
                "placement": "mobile",
            },
            headers=h,
        )
        return p.status, cl.status, rp.status

    statuses = _run(go, state)
    assert all(s in (401, 403) for s in statuses), statuses
    assert guide_store_for(state)._guides[g["guide_id"]].owner_tab is None


def test_an_ended_guide_survives_a_reload_until_the_owner_dismisses_it():
    """``pending`` serves the cancelled guide (its chat's result line) after a
    reload; ``dismiss`` is owner-only and hides it for every tab."""
    state = FakeState()
    g = _started(state)

    async def go(c):
        await c.post(
            "/api/guide/cancel",
            json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": g["revision"]},
            headers=OWNER,
        )
        after_reload = await (await c.get("/api/guide/pending", headers=OWNER)).json()
        foreign = await c.post(
            "/api/guide/dismiss", json={"guide_id": g["guide_id"]}, headers={"X-Test-Auth": "app"}
        )
        d = await c.post("/api/guide/dismiss", json={"guide_id": g["guide_id"]}, headers=OWNER)
        gone = await (await c.get("/api/guide/pending", headers=OWNER)).json()
        return after_reload, foreign.status, d.status, await d.json(), gone

    after_reload, foreign, status, dismissed, gone = _run(go, state)
    assert [(x["guide_id"], x["status"]) for x in after_reload["guides"]] == [
        (g["guide_id"], "cancelled")
    ]
    assert foreign in (401, 403)
    assert status == 200 and dismissed["dismissed"] is True
    assert gone == {"guides": []}


def test_the_owner_tab_replans_a_ui_show_guide_over_http():
    state = FakeState()
    state.open_slot("chat-1")
    g = guide_store_for(state).start(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        actions=[{"id": "ui.show", "params": {"location_id": "chat.older-sessions"}}],
    )

    async def go(c):
        r = await c.post(
            "/api/guide/claim",
            json={
                "guide_id": g["guide_id"],
                "tab_id": "t1",
                "revision": g["revision"],
                "placements": ["desktop"],
            },
            headers=OWNER,
        )
        cur = await r.json()
        body = {
            "guide_id": cur["guide_id"],
            "tab_id": "t1",
            "revision": cur["revision"],
            "action_index": 0,
            "placement": "mobile",
        }
        stale = await c.post("/api/guide/replan", json={**body, "revision": 1}, headers=OWNER)
        ok = await c.post("/api/guide/replan", json=body, headers=OWNER)
        return stale.status, ok.status, await ok.json()

    stale, status, moved = _run(go, state)
    assert stale == 409
    assert status == 200 and moved["actions"][0]["placement"] == "mobile"
    assert moved["reason"] == "replanned"


def test_browser_cannot_report_a_commit_step_done():
    state = FakeState()
    g = _started(state)
    commit = guide_catalog.commit_step_index("crewmate.create")
    assert commit is not None

    async def go(c):
        r = await c.post(
            "/api/guide/claim",
            json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": g["revision"]},
            headers=OWNER,
        )
        cur = await r.json()
        for _ in range(commit):  # any ui steps ahead of the commit
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
                "step_index": commit,
                "outcome": "observed",
            },
            headers=OWNER,
        )
        return cur["step_index"], r.status, (await r.json())["code"]

    assert _run(go, state) == (commit, 409, "commit_step_requires_server_evidence")


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
_CREWMATE_COMMIT = guide_catalog.commit_step_index("crewmate.create")


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
    assert stored.status == "active" and stored.step_index == _CREWMATE_COMMIT
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
    assert stored.status == "active" and stored.step_index == _CREWMATE_COMMIT


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
    assert stored.status == "active" and stored.step_index == _CREWMATE_COMMIT


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
    text = (REPO / "src/kiro_crew/dashboard/server_runtime/mcp_routes.py").read_text(
        encoding="utf-8"
    )
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
    assert set(tools) == {
        "guide_list_actions",
        "guide_start",
        "guide_status",
        "guide_cancel",
        "list_change_kinds",
        "find_setting",
        "get_member_capabilities",
        "diagnose_settings",
        "propose_change",
        "get_change_status",
        "global_memory_recall",
        "global_preference_add",
        "search_docs",
        "find_ui",
    }
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


def test_crewmate_create_steps_match_the_page():
    # The page walks the New crewmate card: one step, its Create, completed only
    # by the gateway from what was created. The catalog counts the same steps,
    # or the browser's step index would name a step the gateway never offered.
    src = _ts("guide/guideActions.ts")
    body = re.search(r"function resolveCrewmateCreate\(.*?\n}\n", src, re.S).group(0)
    page_steps = re.findall(r"complete: \{ kind: '(\w+)'", body)
    steps = guide_catalog.ACTIONS["crewmate.create"].steps
    assert page_steps == ["committed"]
    assert [st.kind for st in steps] == [guide_catalog.STEP_COMMIT]


def test_crewmate_caps_fit_the_real_form():
    # The card a guide pre-fills sets no shorter limit on either field, so a
    # proposal the catalog accepts always lands whole and can be edited back.
    card = _ts("pages/members/NewCrewmateDialog.tsx")
    limits = [int(v) for v in re.findall(r"maxLength=\{?(\d+)", card)]
    assert all(v >= guide_catalog._GOAL_MAX_CHARS for v in limits)
    job = guide_catalog._GOAL_MAX_CHARS
    name = guide_catalog._NAME_MAX_CHARS
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


# ── a guide offer is part of the conversation ──

GUIDE_SID = "acp-guide-1"


@pytest.fixture
def guide_log(tmp_path, monkeypatch):
    from kiro_crew.crew_log import CrewLog
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.dashboard import chat_cards

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    crew_log_emit.reset_caches()
    chat_cards.reset()
    CrewLog.create(KIND_SESSION, GUIDE_SID, owner="owner", agent="kirocrew", slot="chat-1")

    def entries(prefix: str) -> list:
        assert crew_log_emit.flush(timeout=5.0)
        handle = CrewLog.open(KIND_SESSION, GUIDE_SID)
        try:
            return [e for e in handle.iter_from(1) if e.type.startswith(prefix)]
        finally:
            del handle

    yield entries
    crew_log_emit.drain_for_shutdown(timeout=2.0)
    crew_log_emit.reset_caches()
    chat_cards.reset()


def _live_slot(state: FakeState) -> _ChatSlot:
    from types import SimpleNamespace

    slot = state.open_slot("chat-1")
    slot._acp_client = SimpleNamespace(session_id=GUIDE_SID)
    return slot


def _rows(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "card"]


def test_a_guide_ended_because_its_save_went_through_records_why_on_its_row_and_log(guide_log):
    state = FakeState()
    slot = _live_slot(state)

    async def go(c):
        g = await (
            await c.post(
                "/api/guide/agent/start",
                json={"actions": CREWMATE},
                headers=agent("dashboard:chat-1"),
            )
        ).json()
        claimed = await (
            await c.post(
                "/api/guide/claim",
                json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": g["revision"]},
                headers=OWNER,
            )
        ).json()
        bad = await c.post(
            "/api/guide/cancel",
            json={
                "guide_id": g["guide_id"],
                "tab_id": "t1",
                "revision": claimed["revision"],
                "reason": ["saved_without_guide"],
            },
            headers=OWNER,
        )
        ok = await c.post(
            "/api/guide/cancel",
            json={
                "guide_id": g["guide_id"],
                "tab_id": "t1",
                "revision": claimed["revision"],
                "reason": "saved_without_guide",
            },
            headers=OWNER,
        )
        return bad.status, ok.status

    bad, ok = _run(go, state)
    assert (bad, ok) == (400, 200)
    (final,) = _rows(slot)
    assert final["meta"]["card"]["status"] == "cancelled"
    assert final["meta"]["card"]["reason"] == "saved_without_guide"
    # The log says why too: "cancelled" alone would read as the person giving up.
    (finished,) = guide_log("guide/finished")
    assert finished.data == {
        "guide_id": final["meta"]["card"]["id"],
        "status": "cancelled",
        "reason": "saved_without_guide",
    }


def test_an_offer_is_a_conversation_row_and_its_end_patches_it(guide_log):
    state = FakeState()
    slot = _live_slot(state)
    slot.append("user", "help me make a crewmate", "msg msg-u", broadcast=False)

    async def go(c):
        g = await (
            await c.post(
                "/api/guide/agent/start",
                json={"actions": CREWMATE},
                headers=agent("dashboard:chat-1"),
            )
        ).json()
        offered_row = json.loads(json.dumps(_rows(slot)))
        claimed = await (
            await c.post(
                "/api/guide/claim",
                json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": g["revision"]},
                headers=OWNER,
            )
        ).json()
        await c.post(
            "/api/guide/cancel",
            json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": claimed["revision"]},
            headers=OWNER,
        )
        return g, offered_row

    g, offered_row = _run(go, state)
    assert [m["role"] for m in slot.messages] == ["user", "card"]
    (row,) = offered_row
    assert row["meta"]["card"] == {
        "surface": "guide",
        "id": g["guide_id"],
        "slot": "chat-1",
        "kind": "crewmate.create",
        "actions": ["crewmate.create"],
        "status": "offered",
    }
    # The row never carries the action's parameters.
    assert "Scout" not in json.dumps(row)
    (final,) = _rows(slot)
    assert final["meta"]["mid"] == row["meta"]["mid"]
    assert final["meta"]["card"]["status"] == "cancelled"
    types = [(e.type, e.data.get("status")) for e in guide_log("guide/")]
    assert types == [
        ("guide/offered", None),
        ("guide/started", None),
        ("guide/finished", "cancelled"),
    ]
    (offered,) = guide_log("guide/offered")
    assert offered.data["mid"] == row["meta"]["mid"]
    assert offered.data["actions"] == ["crewmate.create"]
    assert "Scout" not in json.dumps(offered.data)


def test_a_re_claim_after_a_lapsed_lease_is_not_a_second_start(guide_log):
    state = FakeState()
    _live_slot(state)
    clock = {"now": 1_000.0}
    state._guide_store = guide_runs.GuideStore(clock=lambda: clock["now"])

    async def go(c):
        g = await (
            await c.post(
                "/api/guide/agent/start",
                json={"actions": CREWMATE},
                headers=agent("dashboard:chat-1"),
            )
        ).json()
        a = await (
            await c.post(
                "/api/guide/claim",
                json={"guide_id": g["guide_id"], "tab_id": "t1", "revision": g["revision"]},
                headers=OWNER,
            )
        ).json()
        clock["now"] += guide_runs.TAB_LEASE_SECONDS + 1
        lapsed = await (await c.get("/api/guide/pending", headers=OWNER)).json()
        rev = lapsed["guides"][0]["revision"]
        await c.post(
            "/api/guide/claim",
            json={"guide_id": g["guide_id"], "tab_id": "t2", "revision": rev},
            headers=OWNER,
        )
        return a

    _run(go, state)
    assert [e.type for e in guide_log("guide/started")] == ["guide/started"]
