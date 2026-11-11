"""An app token's door into the owner's Bots: ``permissions.bots`` plus the owner's ticks.

``apps.bot_grants`` owns the rule; these tests pin each refusal on its own. No scope,
the wrong scope, or a Bot the owner did not tick are all the same 404; the private
member routes stay 404 whatever the app holds; a wake carries only an opaque ref and
is rate limited per app and Bot; and only the owner can change the ticked list.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew import member_records
from kiro_crew import members as members_mod
from kiro_crew.apps import bot_grant_store, bot_grants, manager_lookup, manager_records
from kiro_crew.apps.manager_records import (
    InstalledApp,
    _approved_grants_after_manifest_change,
)
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.point_grants import grant_source
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard import app_bot_wake, create_rate_limit, mate_thread_wakes
from kiro_crew.dashboard.handlers import member_goals as goals_handlers
from kiro_crew.dashboard.handlers import members as m
from kiro_crew.testing.wait import await_until

APP = "notes-app"
CREW = "code-reviewer"
SLUG = "code-reviewer"
OTHER = "writer"
OWNER = "U_OWNER"


def _fake_cfg():
    return SimpleNamespace(
        agents={
            CREW: KiroCrewAgentConfig(
                kiro_agent="kirocrew", description="reviews code", model="m1"
            ),
            OTHER: KiroCrewAgentConfig(kiro_agent="kirocrew", description="writes", model="m1"),
        },
        default_agent=CREW,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


def _request(tmp_path, method, path, match=None, *, app_claim=APP, user=APP, state=None):
    state = state or _make_state(tmp_path)
    state.owner_id = OWNER
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, app=app, match_info=match or {})
    req["app"] = app_claim
    req["user"] = user
    return req


def _json_request(tmp_path, path, match, body, **kw):
    req = _request(tmp_path, "POST", path, match, **kw)

    async def _json():
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


@pytest.fixture
def grants(monkeypatch):
    """Set what the app holds: ``grants(scopes, ticked)``."""

    def _set(scopes=(), ticked=()):
        monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda _a: frozenset(scopes))
        monkeypatch.setattr(bot_grants, "app_granted_bots", lambda _a: tuple(ticked))

    _set()
    return _set


@pytest.fixture(autouse=True)
def _cfg():
    with (
        patch.object(m.KiroCrewConfig, "load", return_value=_fake_cfg()),
        patch.object(members_mod, "read_dm_binding", return_value=None),
    ):
        yield


async def _call(handler, req):
    resp = await handler(req)
    return resp.status, json.loads(resp.body)


# ── the roster ──


@pytest.mark.asyncio
async def test_roster_without_the_read_scope_is_not_found(tmp_path, grants):
    grants(scopes={"wake"}, ticked=[SLUG])
    status, body = await _call(m.api_members, _request(tmp_path, "GET", "/api/members"))
    assert (status, body["code"]) == (404, "not_found")


@pytest.mark.asyncio
async def test_roster_lists_only_the_ticked_bots_in_the_contract_shape(tmp_path, grants):
    grants(scopes={"read"}, ticked=[SLUG])
    status, body = await _call(m.api_members, _request(tmp_path, "GET", "/api/members"))
    assert status == 200
    assert body["object"] == "list" and body["has_more"] is False
    assert [b["id"] for b in body["data"]] == [SLUG]
    row = body["data"][0]
    # The allowlist: nothing of the owner's view (thread key, last line, memory store).
    assert set(row) == {"id", "object", "name", "description", "avatar_url", "status", "has_goal"}
    assert row["object"] == "bot" and row["status"] == "idle"
    assert row["description"] == "reviews code"


@pytest.mark.asyncio
async def test_roster_with_nothing_ticked_is_empty(tmp_path, grants):
    grants(scopes={"read"}, ticked=[])
    status, body = await _call(m.api_members, _request(tmp_path, "GET", "/api/members"))
    assert status == 200 and body["data"] == []


@pytest.mark.asyncio
async def test_roster_says_busy_while_the_bots_thread_runs_a_turn(tmp_path, grants):
    grants(scopes={"read"}, ticked=[SLUG])
    state = _make_state(tmp_path)
    key = f"member-{SLUG}"
    state._slots[key] = SimpleNamespace(running=True)
    binding = {"member": CREW, "slot_key": key}
    with patch.object(members_mod, "read_dm_binding", return_value=binding):
        status, body = await _call(
            m.api_members, _request(tmp_path, "GET", "/api/members", state=state)
        )
    assert status == 200 and body["data"][0]["status"] == "busy"


# ── the goals ──


def _goal_record():
    return {
        "goals": [{"id": "g1", "text": "ship it", "created_at": 0.0, "updated_at": 0.0}],
        "key_results": [
            {
                "id": "k1",
                "text": "tests green",
                "target": "100%",
                "current": "80%",
                "updated_at": 5.0,
                "goal_ids": ["g1"],
                "readings": [{"value": "80%", "at": 5.0}],
            }
        ],
        "schedules": {},
        "reports": [],
        "removed_key_result": None,
        "removed_goal": None,
        "heartbeat_offer": None,
    }


@pytest.mark.asyncio
async def test_goals_of_a_bot_the_owner_did_not_tick_are_not_found(tmp_path, grants):
    grants(scopes={"read"}, ticked=[OTHER])
    req = _request(tmp_path, "GET", f"/api/members/{SLUG}/goals", {"slug": SLUG})
    status, body = await _call(goals_handlers.api_member_goals_get, req)
    assert (status, body["code"]) == (404, "not_found")


@pytest.mark.asyncio
async def test_goals_of_a_ticked_bot_are_the_contract_shape(tmp_path, grants):
    grants(scopes={"read"}, ticked=[SLUG])
    req = _request(tmp_path, "GET", f"/api/members/{SLUG}/goals", {"slug": SLUG})
    with (
        patch("kiro_crew.member_goals.read_goals", return_value=_goal_record()),
        patch(
            "kiro_crew.member_goals.public_view",
            side_effect=lambda rec: {
                "goals": [{"id": "g1", "text": "ship it", "number": 1}],
                "key_results": rec["key_results"],
            },
        ),
    ):
        status, body = await _call(goals_handlers.api_member_goals_get, req)
    assert status == 200
    assert body["bot"] == SLUG
    assert body["goals"] == [{"id": "g1", "number": 1, "text": "ship it"}]
    assert body["key_results"][0]["history"] == [{"at": 5, "value": "80%"}]
    assert "schedules" not in body and "reports" not in body


# ── the private routes stay closed ──

PRIVATE = [
    pytest.param(m.api_member_rules_get, "GET", "rules", id="rules"),
    pytest.param(m.api_member_briefing, "GET", "briefing", id="briefing"),
    pytest.param(m.api_member_activity, "GET", "activity", id="activity"),
    pytest.param(m.api_member_projections, "GET", "projections", id="projections"),
    pytest.param(m.api_member_thread, "POST", "thread", id="thread"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "method", "tail"), PRIVATE)
async def test_bot_scopes_never_open_a_private_member_route(
    tmp_path, grants, handler, method, tail
):
    grants(scopes={"read", "wake"}, ticked=[SLUG])
    req = _request(tmp_path, method, f"/api/members/{SLUG}/{tail}?member={CREW}", {"slug": SLUG})
    status, body = await _call(handler, req)
    assert (status, body["code"]) == (404, "not_found")


# ── the wake ──


@pytest.mark.asyncio
async def test_wake_needs_the_wake_scope(tmp_path, grants):
    grants(scopes={"read"}, ticked=[SLUG])
    req = _json_request(tmp_path, f"/api/members/{SLUG}/wake", {"slug": SLUG}, {"ref": "r1"})
    status, body = await _call(m.api_member_wake, req)
    assert (status, body["code"]) == (404, "not_found")


@pytest.mark.asyncio
async def test_wake_of_an_unticked_bot_is_not_found(tmp_path, grants):
    grants(scopes={"wake"}, ticked=[OTHER])
    req = _json_request(tmp_path, f"/api/members/{SLUG}/wake", {"slug": SLUG}, {"ref": "r1"})
    status, _body = await _call(m.api_member_wake, req)
    assert status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"ref": "r1", "text": "transfer the money"}, "unexpected_field"),
        ({"ref": "r1", "message": "hi"}, "unexpected_field"),
        ({"ref": "do this now please"}, "invalid_ref"),
        ({"ref": ""}, "invalid_ref"),
        ({}, "invalid_ref"),
    ],
)
async def test_wake_refuses_anything_but_an_opaque_ref(tmp_path, grants, body, code):
    grants(scopes={"wake"}, ticked=[SLUG])

    async def _never(*_a, **_k):
        raise AssertionError("a refused wake must not reach the Bot")

    req = _json_request(tmp_path, f"/api/members/{SLUG}/wake", {"slug": SLUG}, body)
    with patch.object(app_bot_wake, "request_wake", _never):
        status, out = await _call(m.api_member_wake, req)
    assert (status, out["code"]) == (400, code)


@pytest.mark.asyncio
async def test_wake_past_the_limit_is_429(tmp_path, grants):
    grants(scopes={"wake"}, ticked=[SLUG])

    async def _limited(*_a, **_k):
        return app_bot_wake.RATE_LIMITED

    req = _json_request(tmp_path, f"/api/members/{SLUG}/wake", {"slug": SLUG}, {"ref": "r1"})
    with patch.object(app_bot_wake, "request_wake", _limited):
        resp = await m.api_member_wake(req)
    assert resp.status == 429
    assert resp.headers["Retry-After"] == str(int(create_rate_limit.WINDOW_SECS))


@pytest.mark.asyncio
async def test_a_dashboard_user_cannot_use_the_wake_route(tmp_path, grants):
    grants(scopes={"wake"}, ticked=[SLUG])
    req = _json_request(
        tmp_path,
        f"/api/members/{SLUG}/wake",
        {"slug": SLUG},
        {"ref": "r1"},
        app_claim="",
        user=OWNER,
    )
    status, body = await _call(m.api_member_wake, req)
    assert (status, body["code"]) == (403, "app_token_required")


class _Slot:
    def __init__(self, key, *, running=False):
        self.key = key
        self.mode = members_mod.DM_SLOT_MODE
        self.agent = CREW
        self.running = running
        self._queue = []
        self.is_closing = False
        self.task = None


class _State:
    def __init__(self, slot):
        self._slot = slot

    def get_slot(self, key):
        return self._slot if key == self._slot.key else None


@pytest.fixture
def wake_world(monkeypatch):
    """A Bot with an open thread; returns ``(state, slot, dispatched)``."""
    key = f"member-{SLUG}"
    slot = _Slot(key)
    state = _State(slot)
    dispatched: list[dict] = []
    monkeypatch.setattr(m, "_resolve", lambda _cfg, _slug: CREW)
    monkeypatch.setattr(m, "_thread_key", lambda _slug, _name: key)

    async def _loaded(_state, _key):
        return ""

    monkeypatch.setattr("kiro_crew.dashboard.goal_reminder._load_thread", _loaded)
    monkeypatch.setattr("kiro_crew.dashboard.mate_welcome.app_is_stopping", lambda _s: False)

    def _dispatch(_state, _slot, prompt, *, human_seen, actor="gateway"):
        dispatched.append({"prompt": prompt, "actor": actor, "human_seen": human_seen})
        return True

    monkeypatch.setattr("kiro_crew.dashboard.mate_welcome.dispatch_hidden_turn", _dispatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(app_bot_wake, "_now", lambda: clock["now"])
    create_rate_limit.reset_for_tests()
    # No upgrade notice owed: the turn-end tail would otherwise read the binding from disk.
    mate_thread_wakes._notice_checked(state).add(key)
    yield state, slot, dispatched, clock
    create_rate_limit.reset_for_tests()


async def _flushed(state, slot, what):
    """Wait for *slot*'s wake flush, the one ``mate_thread_wakes`` runs."""
    task = mate_thread_wakes._tasks(state).get(slot.key)
    if task is not None:
        await await_until(task, what=what)


@pytest.mark.asyncio
async def test_an_idle_bot_gets_the_gateways_line_as_an_app_turn(wake_world):
    state, _slot, dispatched, _clock = wake_world
    outcome = await app_bot_wake.request_wake(state, APP, SLUG, "msg-42")
    assert outcome == app_bot_wake.STARTED
    await _flushed(state, _slot, "the app wake flush")
    assert len(dispatched) == 1
    sent = dispatched[0]
    assert sent["actor"] == "app" and sent["human_seen"] is False
    assert sent["prompt"] == app_bot_wake.wake_prompt({APP: ["msg-42"]})
    assert sent["prompt"].startswith(f'{app_bot_wake.WAKE_PREFIX}{APP}"]')
    assert "#msg-42" in sent["prompt"]


@pytest.mark.parametrize("busy", ["running", "queue"])
@pytest.mark.asyncio
async def test_a_busy_bot_gets_its_wakes_in_one_turn_at_its_next_turn_end(wake_world, busy):
    state, slot, dispatched, _clock = wake_world
    if busy == "running":
        slot.running = True
    else:
        slot._queue = ["held prompt"]
    assert await app_bot_wake.request_wake(state, APP, SLUG, "a") == app_bot_wake.QUEUED
    assert await app_bot_wake.request_wake(state, APP, SLUG, "b") == app_bot_wake.QUEUED
    await _flushed(state, slot, "the flush to find the thread busy")
    assert dispatched == []
    # The thread's turn ends: the turn-end tail every top-level turn runs delivers it.
    slot.running = False
    slot._queue = []
    mate_thread_wakes.note_turn_end(state, slot, "done")
    await _flushed(state, slot, "the turn-end flush")
    assert [d["prompt"] for d in dispatched] == [app_bot_wake.wake_prompt({APP: ["a", "b"]})]
    assert dispatched[0]["actor"] == "app"
    assert mate_thread_wakes.app_wakes(state) == {}


@pytest.mark.asyncio
async def test_a_waiting_worker_report_goes_before_the_app_wakes(wake_world):
    state, slot, dispatched, _clock = wake_world
    slot.running = True
    assert await app_bot_wake.request_wake(state, APP, SLUG, "a") == app_bot_wake.QUEUED
    mate_thread_wakes._record_report(state, slot.key, "worker-1", "Worker", "finished")
    await _flushed(state, slot, "the flush to find the thread busy")
    assert dispatched == []
    slot.running = False
    mate_thread_wakes.note_turn_end(state, slot, "done")
    await _flushed(state, slot, "the turn-end flush")
    # The report's turn runs first, as the gateway; the refs stay held.
    assert [d["actor"] for d in dispatched] == ["gateway"]
    assert mate_thread_wakes.app_wakes(state) == {slot.key: {APP: ["a"]}}
    mate_thread_wakes.note_turn_end(state, slot, "read the report")
    await _flushed(state, slot, "the flush after the report's turn")
    assert [d["actor"] for d in dispatched] == ["gateway", "app"]
    assert dispatched[1]["prompt"] == app_bot_wake.wake_prompt({APP: ["a"]})


@pytest.mark.asyncio
async def test_a_turn_end_that_runs_before_the_turn_releases_the_slot_still_delivers(wake_world):
    state, slot, dispatched, _clock = wake_world
    slot.running = True
    slot.task = asyncio.get_running_loop().create_future()
    assert await app_bot_wake.request_wake(state, APP, SLUG, "r1") == app_bot_wake.QUEUED
    # The turn-end tail runs while the turn still holds the slot.
    mate_thread_wakes.note_turn_end(state, slot, "done")
    flush = mate_thread_wakes._tasks(state)[slot.key]
    await asyncio.sleep(0)  # flake-ok: lets the flush reach its wait; the result is asserted below
    assert dispatched == [] and not flush.done()
    # The turn releases the slot: the waiting flush delivers.
    slot.running = False
    slot.task.set_result(None)
    await await_until(flush, what="the flush waiting on the turn")
    assert [d["prompt"] for d in dispatched] == [app_bot_wake.wake_prompt({APP: ["r1"]})]


@pytest.mark.asyncio
async def test_a_busy_bots_full_ref_queue_refuses_new_refs_instead_of_dropping_old(
    wake_world, monkeypatch
):
    state, slot, dispatched, _clock = wake_world
    monkeypatch.setitem(create_rate_limit._BUDGETS, create_rate_limit.APP_BOT_WAKE, 100)
    monkeypatch.setattr(mate_thread_wakes, "schedule_flush", lambda *_a: None)
    slot.running = True
    for i in range(app_bot_wake.MAX_PENDING_REFS):
        assert await app_bot_wake.request_wake(state, APP, SLUG, f"r{i}") == app_bot_wake.QUEUED
    assert await app_bot_wake.request_wake(state, APP, SLUG, "over") == app_bot_wake.QUEUE_FULL
    # A ref already waiting is not new, so it is still taken.
    assert await app_bot_wake.request_wake(state, APP, SLUG, "r0") == app_bot_wake.QUEUED
    held = mate_thread_wakes.app_wakes(state)[slot.key][APP]
    assert held == [f"r{i}" for i in range(app_bot_wake.MAX_PENDING_REFS)]
    assert dispatched == []


@pytest.mark.asyncio
async def test_wakes_past_the_limit_are_refused_until_the_window_passes(wake_world, monkeypatch):
    state, _slot, _dispatched, clock = wake_world
    # The limit is the budget row; delivery is the flush's, pinned above.
    monkeypatch.setattr(mate_thread_wakes, "schedule_flush", lambda *_a: None)
    limit = create_rate_limit.MAX_APP_BOT_WAKES_PER_WINDOW
    for i in range(limit):
        assert await app_bot_wake.request_wake(state, APP, SLUG, f"r{i % 5}") != (
            app_bot_wake.RATE_LIMITED
        )
    assert await app_bot_wake.request_wake(state, APP, SLUG, "over") == app_bot_wake.RATE_LIMITED
    # The limit is per app: another app still gets through.
    assert await app_bot_wake.request_wake(state, "other-app", SLUG, "x") == app_bot_wake.STARTED
    clock["now"] += create_rate_limit.WINDOW_SECS + 1
    assert await app_bot_wake.request_wake(state, APP, SLUG, "r0") == app_bot_wake.STARTED


@pytest.mark.asyncio
async def test_a_bot_without_an_open_thread_cannot_be_woken(wake_world, monkeypatch):
    state, _slot, dispatched, _clock = wake_world
    monkeypatch.setattr(m, "_thread_key", lambda _slug, _name: "")
    assert await app_bot_wake.request_wake(state, APP, SLUG, "r") == app_bot_wake.NO_THREAD
    monkeypatch.setattr(m, "_resolve", lambda _cfg, _slug: None)
    assert await app_bot_wake.request_wake(state, APP, SLUG, "r") == app_bot_wake.NO_BOT
    assert dispatched == []


# ── the owner's ticked list ──


@pytest.mark.asyncio
async def test_an_app_token_cannot_tick_bots_for_itself(tmp_path):
    from kiro_crew.apps.routes_bot_grants import handle_bot_grants

    def _never(*_a, **_k):
        raise AssertionError("an app must not write its own Bot grants")

    req = _request(tmp_path, "PUT", f"/api/apps/{APP}/bot-grants", {"name": APP})

    async def _json():
        return {"bots": [SLUG]}

    req.json = _json  # type: ignore[method-assign]
    with patch("kiro_crew.apps.routes_bot_grants.set_app_granted_bots", _never):
        resp = await handle_bot_grants(req)
    assert resp.status in (401, 403)


@pytest.mark.asyncio
async def test_the_owner_ticks_bots_for_an_app(tmp_path, monkeypatch):
    from kiro_crew.apps import routes_bot_grants

    stored: list[list[str]] = []
    monkeypatch.setattr(
        routes_bot_grants,
        "set_app_granted_bots",
        lambda _n, bots: stored.append(list(bots)) or list(bots),
    )
    monkeypatch.setattr(
        routes_bot_grants,
        "_view",
        lambda _n: {"bots": stored[-1], "declaredScopes": [], "scopes": []},
    )
    req = _request(
        tmp_path, "PUT", f"/api/apps/{APP}/bot-grants", {"name": APP}, app_claim="", user=OWNER
    )

    async def _json():
        return {"bots": [SLUG]}

    req.json = _json  # type: ignore[method-assign]
    resp = await routes_bot_grants.handle_bot_grants(req)
    assert resp.status == 200
    assert stored == [[SLUG]]


def _install(installed_at="2026-11-08T00:00:00Z"):
    """A real install record for APP; returns its ``installed.json`` path."""
    manager_records.app_dir(APP).mkdir(parents=True, exist_ok=True)
    manager_records._write_installed(
        APP, InstalledApp(name=APP, version="1.0.0", enabled=True, installedAt=installed_at)
    )
    return manager_records.app_dir(APP) / manager_records.INSTALLED_META_FILENAME


def _set_fields(record_path, **fields):
    """Set *fields* in a JSON record under the app's directory."""
    data = json.loads(record_path.read_text(encoding="utf-8"))
    data.update(fields)
    record_path.write_text(json.dumps(data), encoding="utf-8")


def test_the_owners_ticks_live_outside_the_apps_tree():
    record = _install()
    assert bot_grants.set_app_granted_bots(APP, [SLUG, "../etc", SLUG, "Bad"]) == [SLUG]
    assert bot_grants.app_granted_bots(APP) == (SLUG,)
    stored = bot_grant_store.grants_path(APP)
    assert stored.parent.parent.name == member_records.MEMBER_RECORDS_LEAF
    assert manager_records.apps_dir().resolve() not in stored.parents
    assert "botGrants" not in json.loads(record.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_save_that_would_not_read_back_is_refused_not_shown_as_saved(tmp_path):
    from kiro_crew.apps import routes_bot_grants

    # The install record is in the app's tree, so its ``installedAt`` is the app's to write.
    _set_fields(_install(), installedAt="9" * 65536)
    with pytest.raises(ValueError):
        bot_grants.set_app_granted_bots(APP, [SLUG])
    assert not bot_grant_store.grants_path(APP).exists()

    req = _request(
        tmp_path, "PUT", f"/api/apps/{APP}/bot-grants", {"name": APP}, app_claim="", user=OWNER
    )

    async def _json():
        return {"bots": [SLUG]}

    req.json = _json  # type: ignore[method-assign]
    status, body = await _call(routes_bot_grants.handle_bot_grants, req)
    assert status == 500 and body["code"] == "bot_grants_not_saved"


@pytest.mark.asyncio
async def test_an_app_that_edits_installed_json_reaches_no_unticked_bot(tmp_path, monkeypatch):
    record = _install()
    bot_grants.set_app_granted_bots(APP, [SLUG])
    # A `botGrants` key naming the other Bot in the install record is not read.
    _set_fields(record, botGrants=[SLUG, OTHER])
    assert bot_grants.app_granted_bots(APP) == (SLUG,)
    monkeypatch.setattr(bot_grants, "app_bot_scopes", lambda _a: frozenset({"read"}))
    req = _request(tmp_path, "GET", f"/api/members/{OTHER}/goals", {"slug": OTHER})
    status, body = await _call(goals_handlers.api_member_goals_get, req)
    assert (status, body["code"]) == (404, "not_found")


def _declare_bots(monkeypatch, scopes):
    """The live manifest declares ``permissions.bots`` *scopes* and ``/api/members``."""
    manifest = SimpleNamespace(
        permissions=Permissions.from_dict({"api": ["/api/members"], "bots": list(scopes)}),
        contributes=None,
    )
    for owner in ("kiro_crew.apps.manager", "kiro_crew.apps.manager_records"):
        monkeypatch.setattr(f"{owner}.get_app_manifest", lambda _n: manifest)
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda _n: True)


@pytest.mark.asyncio
async def test_an_app_that_writes_wake_into_its_own_tree_cannot_wake(tmp_path, monkeypatch):
    _install()
    bot_grants.set_app_granted_bots(APP, [SLUG])
    _declare_bots(monkeypatch, ["read", "wake"])
    # The owner approved only `read`, in the protected store.
    manager_records._write_approved_grants(
        APP, {"api": ["/api/members"], "events": [], "bots": ["read"]}
    )
    # A set planted in the app's own tree is never read.
    planted = manager_records.app_dir(APP) / manager_records.APPROVED_GRANTS_FILENAME
    planted.write_text(json.dumps({"api": [], "events": [], "bots": ["read", "wake"]}))
    assert manager_records._approved_grants_path(APP).parent.parent.name == (
        member_records.MEMBER_RECORDS_LEAF
    )
    assert bot_grants.app_bot_scopes(APP) == frozenset({"read"})
    req = _json_request(
        tmp_path, f"/api/members/{SLUG}/wake", {"slug": SLUG}, {"ref": "r1"}, app_claim=APP
    )
    with patch.object(app_bot_wake, "request_wake", side_effect=AssertionError("woke")):
        status, body = await _call(m.api_member_wake, req)
    assert (status, body["code"]) == (404, "not_found")


def test_an_app_that_rewrites_its_record_after_the_owners_disable_holds_no_bot_scope(
    monkeypatch,
):
    record = _install()
    real_switch = manager_lookup.is_app_enabled
    _declare_bots(monkeypatch, ["read", "wake"])
    # The real switch, not a stand-in: `_read_installed` honours the owner's disable.
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", real_switch)
    manager_records._write_approved_grants(APP, {"api": [], "events": [], "bots": ["read"]})
    assert bot_grants.app_bot_scopes(APP) == frozenset({"read"})
    manager_records.protected_app_state.set_owner_disabled(APP, True)
    # The app's own tree says enabled; the owner's disable lives outside it.
    _set_fields(record, enabled=True)
    assert bot_grants.app_bot_scopes(APP) == frozenset()


def test_a_set_that_defers_to_the_manifest_approves_no_bot_scope(monkeypatch):
    _install()
    _declare_bots(monkeypatch, ["read", "wake"])
    # The pre-approved-sets form, and a set written before `bots` existed.
    for text in (
        manager_records._APPROVE_DECLARED_TEXT,
        json.dumps({"api": ["/api/members"], "events": []}),
    ):
        manager_records._write_approved_grants(APP, None)
        manager_records.protected_app_state.write_grants_text(APP, text)
        grants = manager_records._read_approved_grants(APP)
        assert grants["bots"] == [] and grants["api"] == ["/api/members"]
        assert bot_grants.app_bot_scopes(APP) == frozenset()


def test_an_install_approves_no_bot_scope_until_the_owner_does(monkeypatch):
    _install()
    _declare_bots(monkeypatch, ["read", "wake"])
    perms = Permissions.from_dict({"api": ["/api/members"], "bots": ["read", "wake"]})
    consented = manager_records._consented_grant_entries(
        grant_source(SimpleNamespace(permissions=perms, contributes=None))
    )
    assert consented["bots"] == [] and consented["api"] == ["/api/members"]
    manager_records._write_approved_grants(APP, consented)
    assert bot_grants.app_bot_scopes(APP) == frozenset()
    # The owner's approval on the app's page is what grants a scope.
    manager_records._write_approved_grants(APP, {**consented, "bots": ["read", "wake"]})
    assert bot_grants.app_bot_scopes(APP) == frozenset({"read", "wake"})


def test_a_failed_uninstall_keeps_the_owners_ticks_and_a_finished_one_removes_them(monkeypatch):
    from kiro_crew.apps import manager_uninstall

    _install()
    bot_grants.set_app_granted_bots(APP, [SLUG])
    (manager_records.app_dir(APP) / "data").mkdir()
    # A linked data directory refuses the uninstall after the grants are withdrawn.
    real_is_link = manager_uninstall.platform_compat.is_link_or_junction
    monkeypatch.setattr(
        manager_uninstall.platform_compat,
        "is_link_or_junction",
        lambda path: Path(path).name == "data" or real_is_link(path),
    )
    assert not manager_uninstall.uninstall_app(APP, keep_data=True).ok
    assert bot_grants.app_granted_bots(APP) == (SLUG,)
    monkeypatch.setattr(manager_uninstall.platform_compat, "is_link_or_junction", real_is_link)
    result = manager_uninstall.uninstall_app(APP, keep_data=False)
    assert result.ok, result.error
    assert not bot_grant_store.grants_path(APP).exists()


def test_a_list_left_in_an_old_install_record_is_cleared():
    # No owner write in the gateway-only record: the old key is dropped and the
    # owner ticks again.
    _set_fields(_install(), botGrants=[SLUG])
    assert bot_grants.app_granted_bots(APP) == ()


def test_a_reinstall_reaches_no_bot_until_the_owner_ticks_again():
    _install("2026-11-08T00:00:00Z")
    bot_grants.set_app_granted_bots(APP, [SLUG])
    _install("2026-11-09T00:00:00Z")
    assert bot_grants.app_granted_bots(APP) == ()
    bot_grant_store.remove_granted_bots(APP)
    assert not bot_grant_store.grants_path(APP).exists()


# ── the manifest scopes ──


def test_permissions_keep_only_known_bot_scopes():
    perms = Permissions.from_dict({"bots": ["read", "admin", "wake", "read"]})
    assert perms.bots == ["read", "wake"]
    assert Permissions.from_dict({"bots": "read"}).bots == []
    assert perms.to_dict()["bots"] == ["read", "wake"]


def test_an_update_that_adds_a_bot_scope_waits_for_the_owner():
    old = Permissions.from_dict({"api": ["/api/members"], "bots": ["read"]})
    new = Permissions.from_dict({"api": ["/api/members"], "bots": ["read", "wake"]})
    approved = _approved_grants_after_manifest_change(
        existing_approved={"api": ["/api/members"], "events": [], "bots": ["read"]},
        old_permissions=grant_source(SimpleNamespace(permissions=old, contributes=None)),
        new_permissions=grant_source(SimpleNamespace(permissions=new, contributes=None)),
    )
    assert approved["bots"] == ["read"]
