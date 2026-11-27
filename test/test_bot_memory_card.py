"""A bot's own memory cleanup: ``memory_list`` and the ``crewmate.memory`` card.

Run against real member stores (``member_memory_helpers.env``: Bots alice and bob,
each on its own V2 store). The card's Apply goes through the change-card hook and
the memory page's own bulk routes, the way the browser sends it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import Clock
from member_memory_helpers import env as _member_env

from kiro_crew import change_card_catalog as catalog
from kiro_crew import change_card_memory as memory_kind
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard import member_memory_records as records
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.handlers import member_memory_self
from kiro_crew.dashboard.handlers import memory_edit as memory_routes

env = _member_env

KIND = memory_kind.KIND_CREWMATE_MEMORY
SLOT = "alice-thread"


def _seed(tier) -> dict[str, str]:
    """Two facts and one experience in *tier*; returns their ids by role."""
    assert tier.set_semantic("project.db", "postgres 13", 0.8, "user_explicit") is None
    assert tier.set_semantic("project.ci", "jenkins", 0.6, "user_explicit") is None
    episode = tier.write_episodic(
        "Moved the nightly build from jenkins to GitHub Actions last spring.",
        conversation_id="c1",
        tags=["ci"],
    )
    assert episode
    row = tier.db.execute("SELECT id FROM episodic_memories WHERE is_deleted = 0").fetchone()
    return {"db": "project.db", "ci": "project.ci", "episode": str(row["id"])}


def _changes(ids: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"kind": "fact", "id": ids["ci"], "action": "delete", "reason": "CI moved off jenkins"},
        {
            "kind": "fact",
            "id": ids["db"],
            "action": "edit",
            "value": "postgres 16",
            "reason": "the database was upgraded",
        },
        {
            "kind": "episode",
            "id": ids["episode"],
            "action": "edit",
            "text": "Moved the nightly build from jenkins to GitHub Actions in 2025.",
            "reason": "the year is known now",
        },
    ]


def _params(ids, *, store="member-alice", member="alice", skip=None) -> dict[str, Any]:
    raw: dict[str, Any] = {"member": member, "store": store, "changes": _changes(ids)}
    if skip is not None:
        raw["skip"] = skip
    return catalog.validate_params(KIND, raw)


def _contents(tier, ids) -> dict[str, Any]:
    """What the three seeded records hold now (``None`` once deleted)."""

    def fact(key):
        row = tier.get_semantic(key)
        return json.loads(row["value_json"]) if row else None

    row = tier.db.execute(
        "SELECT text FROM episodic_memories WHERE id = ? AND is_deleted = 0", (ids["episode"],)
    ).fetchone()
    return {"db": fact(ids["db"]), "ci": fact(ids["ci"]), "episode": row["text"] if row else None}


# ── parameters ──


def test_the_card_takes_only_the_bots_own_store_and_only_skip_is_editable(env):
    ids = _seed(env.tiers["member-alice"])
    with pytest.raises(catalog.CardCatalogError):
        _params(ids, store="default")
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(KIND, {**_params(ids), "changes": _changes(ids) * 2})
    old = _params(ids)
    catalog.check_editable(KIND, old, _params(ids, skip=[1]))
    for field, value in (("store", "member-bob"), ("changes", _changes(ids)[:1])):
        with pytest.raises(catalog.CardCatalogError) as caught:
            catalog.check_editable(KIND, old, {**old, field: value})
        assert caught.value.code == "field_not_editable"
    # Only the bot itself raises it; propose_change never lists it.
    assert KIND in catalog.SELF_ONLY_KINDS
    assert KIND not in {k["id"] for k in catalog.list_kinds()}


@pytest.mark.asyncio
async def test_a_card_naming_another_bots_store_is_refused(env):
    ids = _seed(env.tiers["member-bob"])
    with pytest.raises(catalog.CardCatalogError) as caught:
        await records.memory_state(_params(ids, store="member-bob"), env.state)
    assert caught.value.code == "not_own_store"
    refused, _ = await member_memory_self.memory_params(
        "dashboard:alice", "alice", "member-bob", {"changes": _changes(ids)}
    )
    assert refused is not None and refused.status == 403
    refused, params = await member_memory_self.memory_params(
        "dashboard:alice", "alice", "member-alice", {"changes": _changes(ids)}
    )
    assert refused is None and params["store"] == "member-alice"


# ── the plan ──


@pytest.mark.asyncio
async def test_unticked_rows_leave_the_plan_and_deletes_share_one_group(env):
    ids = _seed(env.tiers["member-alice"])
    preview, before, _ = await cards.build(KIND, _params(ids), state=env.state, app=None)
    paths = [s["path"] for s in preview["apply"]]
    assert paths == [memory_kind.PREVIEW_ROUTE, memory_kind.APPLY_ROUTE] * 3
    assert all(s["body"]["store"] == "member-alice" for s in preview["apply"])
    assert preview["apply"][1]["fill"] == [
        {"field": "preview_id", "source": "step", "step": 0, "key": "preview_id"}
    ]
    # Edits run first, each alone; the deletes (no Undo) share the last group, so an
    # edit refused at apply time stops the plan before any delete has run.
    operations = [s["body"]["operation"] for s in preview["apply"] if "operation" in s["body"]]
    assert operations == [
        {"type": "set", "value": "postgres 16"},
        {"type": "set", "text": _changes(ids)[2]["text"]},
        {"type": "forget"},
    ]
    assert preview["apply"][-2]["body"]["selection"]["items"][0]["id"] == ids["ci"]
    assert [r["included"] for r in preview["changes"]] == [True, True, True]
    assert preview["changes"][1]["before"] == "postgres 13"
    assert preview["changes"][1]["after"] == "postgres 16"
    partial, _, _ = await cards.build(KIND, _params(ids, skip=[0, 2]), state=env.state, app=None)
    assert [r["included"] for r in partial["changes"]] == [False, True, False]
    assert len(partial["apply"]) == 2
    assert partial["apply"][0]["body"]["operation"] == {"type": "set", "value": "postgres 16"}
    assert partial["title"].startswith("Edit 1 of ")
    with pytest.raises(catalog.CardCatalogError) as caught:
        await cards.build(KIND, _params(ids, skip=[0, 1, 2]), state=env.state, app=None)
    assert caught.value.code == "no_change"
    assert catalog.offers_no_undo(KIND, _params(ids))
    del before


@pytest.mark.asyncio
async def test_an_edit_the_memory_editor_would_refuse_refuses_the_card(env):
    """A fact whose value the page hides is read-only there, so the card refuses it."""
    from kiro_crew.testing.synthetic_credentials import github_classic

    tier = env.tiers["member-alice"]
    ids = _seed(tier)
    secret = f"deploy with {github_classic()}"
    assert tier.set_semantic("project.deploy_token", secret, 0.9, "user_explicit") is None
    hidden = {
        "kind": "fact",
        "id": "project.deploy_token",
        "action": "edit",
        "value": "deploy with the vault token",
        "reason": "the token moved to the vault",
    }
    raw = {"member": "alice", "store": "member-alice", "changes": [*_changes(ids), hidden]}
    with pytest.raises(catalog.CardCatalogError) as caught:
        await cards.build(KIND, catalog.validate_params(KIND, raw), state=env.state, app=None)
    assert caught.value.code == "memory_not_editable"
    assert "project.deploy_token" in str(caught.value)
    # Unticking that row lets the rest of the card build.
    params = catalog.validate_params(KIND, {**raw, "skip": [3]})
    preview, _, _ = await cards.build(KIND, params, state=env.state, app=None)
    assert [r["included"] for r in preview["changes"]] == [True, True, True, False]
    assert secret not in json.dumps(preview)
    assert json.loads(tier.get_semantic("project.deploy_token")["value_json"]) == secret


@pytest.mark.asyncio
async def test_a_memory_gone_since_the_card_was_shown_can_be_unticked(env):
    tier = env.tiers["member-alice"]
    ids = _seed(tier)
    # Deleted on the Memory page after the card was raised.
    from kiro_crew import memory_edit

    row = memory_edit.refresh_records(tier, [{"kind": "fact", "id": ids["ci"]}])["entries"][0]
    selection = {"items": [{"kind": "fact", "id": ids["ci"], "revision": row["revision"]}]}
    shown = memory_edit.preview_edit(
        tier, "member-alice", b"k", {"selection": selection, "operation": {"type": "forget"}}
    )
    memory_edit.apply_edit(tier, "member-alice", b"k", shown["preview_id"])
    with pytest.raises(catalog.CardCatalogError) as caught:
        await cards.build(KIND, _params(ids), state=env.state, app=None)
    assert caught.value.code == "not_found"
    preview, _, _ = await cards.build(KIND, _params(ids, skip=[0]), state=env.state, app=None)
    assert [r["included"] for r in preview["changes"]] == [False, True, True]
    assert all(
        s["body"].get("selection", {}).get("items", [{}])[0].get("id") != ids["ci"]
        for s in preview["apply"]
        if "selection" in s["body"]
    )


# ── apply through the hook, as the browser sends it ──


@web.middleware
async def _owner(request: web.Request, handler):
    request["user"] = "owner"
    request["app"] = ""
    return await handler(request)


def _state(env, store: CardStore):
    env.state._change_card_store = store
    env.state.get_slot = lambda _key: None

    async def deliver(*_a, **_k):
        return 1

    env.state.deliver_ws_owners = deliver
    env.state.broadcast_ws = lambda *_a, **_k: None
    return env.state


async def _client(env, store: CardStore) -> TestClient:
    app = web.Application(middlewares=[_owner, routes.change_card_middleware])
    app["state"] = _state(env, store)
    app.router.add_post(memory_kind.PREVIEW_ROUTE, memory_routes.api_memory_bulk_preview)
    app.router.add_post(memory_kind.APPLY_ROUTE, memory_routes.api_memory_bulk_apply)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _propose(env, store: CardStore, params) -> dict[str, Any]:
    preview, before, context = await cards.build(KIND, params, state=env.state, app=None)
    return store.propose(
        slot_key=SLOT,
        session_key="dashboard:alice",
        kind=KIND,
        params=params,
        reason="you asked me to clean up my memory",
        preview=preview,
        before=before,
        context=context,
    )


async def _press_apply(client: TestClient, rec: dict[str, Any]) -> list[int]:
    """Send every apply step the way ``runPlan`` does; the statuses the routes gave."""
    responses: list[Any] = []
    statuses = []
    for at, plan_step in enumerate(rec["plan"]["apply"]):
        body = dict(plan_step["body"])
        for fill in plan_step.get("fill") or []:
            body[fill["field"]] = responses[fill["step"]][fill["key"]]
        r = await client.post(
            plan_step["path"],
            json=body,
            headers={
                "X-Session-Key": "dashboard:ui",
                "X-Card-Id": rec["id"],
                "X-Card-Revision": str(rec["revision"]),
                "X-Card-Op": "apply",
                "X-Card-Step": str(at),
            },
        )
        statuses.append(r.status)
        responses.append(await r.json())
        if r.status >= 300:
            break
    if routes._BACKGROUND:
        await asyncio.gather(*list(routes._BACKGROUND))
    return statuses


@pytest.fixture
def card_store(tmp_path):
    store = CardStore(tmp_path / "cards.json", clock=Clock())
    asyncio.run(store.warm())
    return store


@pytest.mark.asyncio
async def test_nothing_changes_until_the_user_applies_and_then_only_ticked_rows(env, card_store):
    tier = env.tiers["member-alice"]
    ids = _seed(tier)
    seeded = _contents(tier, ids)
    rec = await _propose(env, card_store, _params(ids))
    # A card the user has not pressed writes nothing.
    assert _contents(tier, ids) == seeded
    # The user unticks the experience: the card is previewed again with skip.
    params = catalog.validate_params(KIND, {**rec["params"], "skip": [2]})
    catalog.check_editable(KIND, rec["params"], params)
    preview, before, context = await cards.build(KIND, params, state=env.state, app=None)
    card_store.revise(
        rec,
        revision=rec["revision"],
        params=params,
        preview=preview,
        before=before,
        context=context,
    )
    client = await _client(env, card_store)
    try:
        assert await _press_apply(client, rec) == [200, 200, 200, 200]
    finally:
        await client.close()
    assert _contents(tier, ids) == {"db": "postgres 16", "ci": None, "episode": seeded["episode"]}
    assert card_store.get(rec["id"])["status"] == cards.STATUS_APPLIED
    # Bob's store was never touched.
    assert env.tiers["member-bob"].get_semantic("project.db") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["declined", "expired"])
async def test_a_declined_or_expired_card_changes_nothing(env, card_store, end):
    tier = env.tiers["member-alice"]
    ids = _seed(tier)
    seeded = _contents(tier, ids)
    rec = await _propose(env, card_store, _params(ids))
    if end == "declined":
        card_store.cancel(rec, rec["revision"])
    else:
        card_store._clock.now += cards.CARD_TTL_SECONDS + 1
    client = await _client(env, card_store)
    try:
        statuses = await _press_apply(client, rec)
    finally:
        await client.close()
    assert statuses[0] == 409
    assert _contents(tier, ids) == seeded


@pytest.mark.asyncio
async def test_a_memory_changed_after_the_card_was_shown_is_left_alone(env, card_store):
    tier = env.tiers["member-alice"]
    ids = _seed(tier)
    rec = await _propose(env, card_store, _params(ids))
    assert tier.set_semantic("project.ci", "buildkite", 0.9, "user_explicit") is None
    client = await _client(env, card_store)
    try:
        statuses = await _press_apply(client, rec)
    finally:
        await client.close()
    assert statuses == [409]
    assert _contents(tier, ids)["ci"] == "buildkite"
    assert _contents(tier, ids)["db"] == "postgres 13"


@pytest.mark.asyncio
async def test_an_edit_refused_since_the_card_was_shown_answers_its_code_not_500(env, card_store):
    """A ticked edit the editor would refuse at Apply is a coded 4xx; nothing is written."""
    from kiro_crew.testing.synthetic_credentials import github_classic

    tier = env.tiers["member-alice"]
    ids = _seed(tier)
    rec = await _propose(env, card_store, _params(ids))
    secret = f"postgres 13 with {github_classic()}"
    assert tier.set_semantic("project.db", secret, 0.9, "user_explicit") is None
    seeded = _contents(tier, ids)
    client = await _client(env, card_store)
    try:
        step = rec["plan"]["apply"][0]
        r = await client.post(
            step["path"],
            json=dict(step["body"]),
            headers={
                "X-Session-Key": "dashboard:ui",
                "X-Card-Id": rec["id"],
                "X-Card-Revision": str(rec["revision"]),
                "X-Card-Op": "apply",
                "X-Card-Step": "0",
            },
        )
        answer = await r.json()
    finally:
        await client.close()
    assert r.status != 500
    assert 400 <= r.status < 500
    assert answer["code"] == "memory_not_editable"
    assert _contents(tier, ids) == seeded
    assert card_store.get(rec["id"])["status"] == cards.STATUS_PENDING
    assert not card_store.get(rec["id"]).get("inflight")


# ── memory_list ──


@pytest.mark.asyncio
async def test_memory_list_reads_only_the_bots_own_store(env):
    _seed(env.tiers["member-alice"])
    env.tiers["member-bob"].set_semantic("bob.secret_plan", "bob only", 1.0, "user_explicit")
    facts = await records.list_own(env.state, "alice", kind="fact")
    assert {r["id"] for r in facts["entries"]} == {"project.db", "project.ci"}
    assert {r["text"] for r in facts["entries"]} == {"postgres 13", "jenkins"}
    assert all("confidence" in r and "last_used" not in r for r in facts["entries"])
    episodes = await records.list_own(env.state, "alice", kind="episode")
    assert [r["kind"] for r in episodes["entries"]] == ["episode"]
    # A V2 store never records when recall used an experience: unknown, not "".
    assert env.tiers["member-alice"].algorithm_version == "v2"
    assert "last_used" not in episodes["entries"][0]
    with pytest.raises(catalog.CardCatalogError):
        await records.list_own(env.state, "alice", kind="directive")


# ── POST /api/member/self/memory/list ──


@pytest.fixture
def list_client(env, monkeypatch):
    """A client for the list route whose caller is *callers[0]*: a bot thread or not."""
    from kiro_crew.dashboard.guide_runs import GuideError
    from kiro_crew.dashboard.handlers import guide, member_self

    callers: list[Any] = [("alice", "member-alice")]

    async def resolve(_request, _operation, **_kw):
        if isinstance(callers[0], GuideError):
            raise callers[0]
        return SLOT, "dashboard:alice"

    monkeypatch.setattr(guide, "_resolve_agent_caller", resolve)
    monkeypatch.setattr(member_self, "_caller_member", lambda _req, _slot: callers[0])

    async def start() -> TestClient:
        app = web.Application()
        app["state"] = env.state
        app.router.add_post(
            "/api/member/self/memory/list", member_memory_self.api_member_self_memory_list
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    return start, callers


async def _list(client: TestClient, body: Any) -> tuple[int, dict[str, Any]]:
    data = body if isinstance(body, str) else json.dumps(body)
    r = await client.post("/api/member/self/memory/list", data=data)
    return r.status, await r.json()


@pytest.mark.asyncio
async def test_the_list_route_answers_a_page_of_the_bots_own_facts(env, list_client):
    _seed(env.tiers["member-alice"])
    env.tiers["member-bob"].set_semantic("bob.secret_plan", "bob only", 1.0, "user_explicit")
    start, _ = list_client
    client = await start()
    try:
        status, answer = await _list(client, {"kind": "fact", "limit": 1})
        assert status == 200 and answer["ok"] is True
        assert len(answer["entries"]) == 1
        status, answer = await _list(client, {"kind": "fact", "query": "jenkins"})
        assert status == 200
        assert [r["id"] for r in answer["entries"]] == ["project.ci"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"kind": "fact", "query": "q" * 201}, 400, "invalid_param"),
        ({"kind": "fact", "query": 7}, 400, "invalid_param"),
        ({"kind": "fact", "offset": -1}, 400, "invalid_param"),
        ({"kind": "fact", "limit": True}, 400, "invalid_param"),
        ({"kind": "fact", "limit": 0}, 400, "invalid_param"),
        ({"kind": "directive"}, 400, None),
        ("[1]", 400, "invalid_body"),
        ("not json", 400, "invalid_json"),
    ],
)
async def test_the_list_route_refuses_a_bad_request_with_its_code(list_client, body, status, code):
    start, _ = list_client
    client = await start()
    try:
        got, answer = await _list(client, body)
    finally:
        await client.close()
    assert got == status
    if code is not None:
        assert answer["code"] == code


@pytest.mark.asyncio
async def test_the_list_route_refuses_any_caller_but_the_bot_in_its_own_thread(list_client):
    from kiro_crew.dashboard.guide_runs import GuideError

    start, callers = list_client
    client = await start()
    try:
        callers[0] = None
        status, answer = await _list(client, {"kind": "fact"})
        assert (status, answer["code"]) == (403, "not_a_crewmate")
        callers[0] = ("alice", "member-bob")
        status, answer = await _list(client, {"kind": "fact"})
        assert (status, answer["code"]) == (403, "not_own_store")
        callers[0] = GuideError(403, "unattested", "no")
        status, answer = await _list(client, {"kind": "fact"})
        assert (status, answer["code"]) == (403, "unattested")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_list_route_answers_503_when_the_store_cannot_be_read(list_client, monkeypatch):
    async def broken(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(records, "list_own", broken)
    start, _ = list_client
    client = await start()
    try:
        status, answer = await _list(client, {"kind": "fact"})
    finally:
        await client.close()
    assert (status, answer["code"]) == (503, "store_unavailable")
