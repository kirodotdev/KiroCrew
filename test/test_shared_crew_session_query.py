"""Server-side creator filtering, search, paging and counts for the session list.

The query runs over real catalog rows: sessions are written to a real
``ConversationLog`` directory and listed through ``list_sessions``, then filtered
by ``query_sessions`` and served by ``GET /api/shared-crew/sessions``.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.history import ConversationLog
from kiro_crew.shared_crew import people, routes_identity
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    AttributionAvailability,
    Principal,
    Provenance,
    SessionAttribution,
    SessionQuery,
    Transport,
)
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal
from kiro_crew.shared_crew.owner import owner_principal, viewer_ids
from kiro_crew.shared_crew.session_query import decode_cursor, encode_cursor, query_sessions

pytestmark = pytest.mark.usefixtures("host_isolation")

ALICE = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "b", Provenance.TAILSCALE_SERVE)


def _write_session(base, key: str, *, title: str, creator: Principal | None, mtime: float) -> None:
    meta: dict[str, Any] = {"_type": "metadata", "title": title}
    if creator is not None:
        meta["attribution"] = SessionAttribution(
            creator=creator, availability=AttributionAvailability.RECORDED, created_at=mtime
        ).to_dict()
    path = base / f"{key}.jsonl"
    row = {"role": "user", "content": "x", "ts": "t"}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def catalog(tmp_path):
    base = tmp_path / "sessions"
    base.mkdir()
    # 5 by Alice, 3 by Bob, 2 legacy (no attribution), 1 machine transcript.
    t = 1_000_000.0
    for i in range(5):
        _write_session(base, f"dashboard_a{i}", title=f"alpha {i}", creator=ALICE, mtime=t + i)
    for i in range(3):
        _write_session(base, f"dashboard_b{i}", title=f"bravo {i}", creator=BOB, mtime=t + 10 + i)
    for i in range(2):
        _write_session(base, f"dashboard_l{i}", title=f"legacy {i}", creator=None, mtime=t + 20 + i)
    _write_session(base, "subagent_x", title="machine", creator=ALICE, mtime=t + 30)
    log = ConversationLog(base)
    return log


def _rows(log) -> list[dict]:
    return log.list_sessions()


def _machine(key: str) -> bool:
    return key.startswith("subagent_")


def _walk(rows, creator: str, viewer: Principal, limit: int, q: str = "") -> list[list[str]]:
    pages, cursor = [], ""
    while True:
        query = SessionQuery.from_query(
            {"creator": creator, "limit": str(limit), "cursor": cursor, "q": q}
        )
        page = query_sessions(rows, query, viewer, exclude=_machine)
        pages.append([i["key"] for i in page.items])
        cursor = page.next_cursor
        if not cursor:
            return pages


@pytest.mark.parametrize(
    "creator,viewer,expected",
    [
        ("all", ALICE, 10),
        ("mine", ALICE, 5),
        ("mine", BOB, 3),
        ("unknown", ALICE, 0),
        (BOB.id, ALICE, 3),
        ("mine", Principal.unknown(), 0),
    ],
    ids=["all", "mine-alice", "mine-bob", "unknown", "creator-id", "mine-unknown-viewer"],
)
def test_filter_counts_are_over_the_whole_catalog(catalog, creator, viewer, expected):
    query = SessionQuery.from_query({"creator": creator, "limit": "2"})
    page = query_sessions(_rows(catalog), query, viewer, exclude=_machine)
    assert page.total_matching == expected
    assert page.hidden_by_filter == 10 - expected
    assert len(page.items) == min(2, expected)


def test_legacy_sessions_belong_to_the_owner_not_to_unattributed(catalog):
    owner = owner_principal()
    mine = SessionQuery.from_query({"creator": "mine", "limit": "50"})
    page = query_sessions(
        _rows(catalog), mine, owner, exclude=_machine, viewer_ids=viewer_ids(owner)
    )
    assert {i["key"] for i in page.items} == {"dashboard_l0", "dashboard_l1"}
    unattributed = SessionQuery.from_query({"creator": "unattributed", "limit": "50"})
    assert query_sessions(_rows(catalog), unattributed, owner, exclude=_machine).items == ()
    # The creator filter by the owner's id lists them too; another person's mine does not.
    by_id = SessionQuery.from_query({"creator": owner.id, "limit": "50"})
    assert query_sessions(_rows(catalog), by_id, ALICE, exclude=_machine).total_matching == 2
    assert query_sessions(_rows(catalog), mine, ALICE, exclude=_machine).total_matching == 5


def test_the_owner_under_another_local_subject_still_finds_their_sessions(catalog):
    startup = local_operator_principal("local-startup")
    ids = viewer_ids(startup)
    assert owner_principal().id in ids and startup.id in ids
    mine = SessionQuery.from_query({"creator": "mine", "limit": "50"})
    page = query_sessions(_rows(catalog), mine, startup, exclude=_machine, viewer_ids=ids)
    assert page.total_matching == 2
    assert viewer_ids(ALICE) == frozenset()


def test_paging_walk_returns_every_match_once_newest_first(catalog):
    pages = _walk(_rows(catalog), "mine", ALICE, limit=2)
    flat = [k for p in pages for k in p]
    assert flat == [f"dashboard_a{i}" for i in (4, 3, 2, 1, 0)]
    assert [len(p) for p in pages] == [2, 2, 1]


def test_search_combines_with_the_filter_before_paging(catalog):
    query = SessionQuery.from_query({"creator": "all", "q": "BRAVO", "limit": "50"})
    page = query_sessions(_rows(catalog), query, ALICE, exclude=_machine)
    assert {i["key"] for i in page.items} == {"dashboard_b0", "dashboard_b1", "dashboard_b2"}
    assert page.total_matching == 3 and page.hidden_by_filter == 0
    query = SessionQuery.from_query({"creator": "mine", "q": "bravo"})
    page = query_sessions(_rows(catalog), query, ALICE, exclude=_machine)
    assert page.total_matching == 0 and page.hidden_by_filter == 3


def test_a_session_updated_mid_walk_is_not_returned_twice(catalog, tmp_path):
    rows = _rows(catalog)
    q1 = SessionQuery.from_query({"creator": "mine", "limit": "2"})
    first = query_sessions(rows, q1, ALICE, exclude=_machine)
    # a0 (oldest) is touched between pages and moves to the top.
    os.utime(tmp_path / "sessions" / "dashboard_a0.jsonl", (2_000_000.0, 2_000_000.0))
    q2 = SessionQuery.from_query({"creator": "mine", "limit": "50", "cursor": first.next_cursor})
    second = query_sessions(_rows(catalog), q2, ALICE, exclude=_machine)
    seen = [i["key"] for i in first.items] + [i["key"] for i in second.items]
    assert len(seen) == len(set(seen))


def test_items_carry_public_attribution_only(catalog):
    page = query_sessions(_rows(catalog), SessionQuery(), ALICE, exclude=_machine)
    for item in page.items:
        creator = item["attribution"]["creator"]
        assert set(creator) == {"id", "kind", "display"}
    legacy = [i for i in page.items if i["key"].startswith("dashboard_l")]
    assert all(i["attribution"]["availability"] == "legacy_missing" for i in legacy)


@pytest.mark.parametrize("cursor", ["!!", "bm90LWEtY3Vyc29y", encode_cursor((1.0, "xyz"))[:-4]])
def test_a_malformed_cursor_is_refused(cursor):
    with pytest.raises(ContractError):
        if cursor == "!!":
            SessionQuery.from_query({"cursor": cursor})
        else:
            decode_cursor(cursor)


def test_a_cursor_round_trips():
    assert decode_cursor(encode_cursor((-12.5, "0123456789abcdef"))) == (-12.5, "0123456789abcdef")


# -- The routes ------------------------------------------------------------- #


def _actor(p: Principal) -> ActorContext:
    return ActorContext(principal=p, request_id="r", transport=Transport.HTTP)


async def _client(log, *, viewer: Principal, dashboard_user: bool = True, app: str = ""):
    @web.middleware
    async def fake_auth(request: web.Request, handler: Any) -> web.StreamResponse:
        # Stands in for token_auth_middleware's published claims.
        request["is_dashboard_user"] = dashboard_user
        request["app"] = app
        # The viewer came in on the direct listener, which checked the access set.
        request["auth_tailnet_direct"] = True
        request[ACTOR_REQUEST_KEY] = _actor(viewer)
        return await handler(request)

    app_ = web.Application(middlewares=[fake_auth])
    app_["state"] = SimpleNamespace(conversation_log=log)
    routes_identity.register(app_)
    client = TestClient(TestServer(app_))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_sessions_route_filters_server_side_for_the_authenticated_viewer(catalog):
    client = await _client(catalog, viewer=BOB)
    try:
        # A "me" the client sends is just a filter value; mine is the viewer.
        resp = await client.get("/api/shared-crew/sessions", params={"creator": "mine"})
        body = await resp.json()
        assert resp.status == 200
        assert body["total_matching"] == 3 and body["hidden_by_filter"] == 7
        assert body["viewer"]["id"] == BOB.id
        assert {i["key"] for i in body["items"]} == {
            "dashboard_b0",
            "dashboard_b1",
            "dashboard_b2",
        }
        resp = await client.get(
            "/api/shared-crew/sessions", params={"creator": "all", "limit": "3"}
        )
        body = await resp.json()
        assert len(body["items"]) == 3 and body["total_matching"] == 10 and body["next_cursor"]
        resp = await client.get("/api/shared-crew/sessions", params={"creator": "bob@example.com"})
        assert resp.status == 400 and (await resp.json())["code"] == "invalid_filter"
        resp = await client.get("/api/shared-crew/sessions", params={"cursor": "bm9wZQ=="})
        assert resp.status == 400 and (await resp.json())["code"] == "invalid_cursor"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_folded_owner_creator_filter_includes_every_local_owner_subject(tmp_path):
    base = tmp_path / "sessions"
    base.mkdir()
    local_app = local_operator_principal("local-app")
    local_startup = local_operator_principal("local-startup")
    _write_session(base, "dashboard_owner_app", title="owner app", creator=local_app, mtime=1.0)
    _write_session(
        base,
        "dashboard_owner_startup",
        title="owner startup",
        creator=local_startup,
        mtime=2.0,
    )
    _write_session(base, "dashboard_alice", title="alice", creator=ALICE, mtime=3.0)
    client = await _client(ConversationLog(base), viewer=BOB)
    try:
        people_body = await (await client.get(routes_identity.PEOPLE_PATH)).json()
        owner_rows = [row for row in people_body["people"] if row["is_owner"]]
        assert len(owner_rows) == 1
        selected_owner_id = owner_rows[0]["principal"]["id"]
        assert selected_owner_id in {local_app.id, local_startup.id}

        owner_response = await client.get(
            routes_identity.SESSIONS_PATH, params={"creator": selected_owner_id}
        )
        owner_body = await owner_response.json()
        alice_response = await client.get(
            routes_identity.SESSIONS_PATH, params={"creator": ALICE.id}
        )
        alice_body = await alice_response.json()
    finally:
        await client.close()

    assert owner_response.status == 200
    assert {item["key"] for item in owner_body["items"]} == {
        "dashboard_owner_app",
        "dashboard_owner_startup",
    }
    assert owner_body["total_matching"] == 2 and owner_body["hidden_by_filter"] == 1
    assert alice_response.status == 200
    assert [item["key"] for item in alice_body["items"]] == ["dashboard_alice"]
    assert alice_body["total_matching"] == 1 and alice_body["hidden_by_filter"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("dashboard_user,app", [(False, ""), (True, "notes"), (False, "notes")])
async def test_routes_refuse_callers_other_than_the_dashboard_user(catalog, dashboard_user, app):
    client = await _client(catalog, viewer=ALICE, dashboard_user=dashboard_user, app=app)
    try:
        for path in (routes_identity.SESSIONS_PATH, routes_identity.PEOPLE_PATH):
            resp = await client.get(path)
            assert resp.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_people_route_separates_observed_from_session_creators(catalog):
    people.reset_for_tests(clock=iter([100.0, 200.0, 300.0, 400.0]).__next__)
    people.observe(BOB, "node:b")
    people.observe(Principal.unknown(), "x")
    people.observe(Principal.system("internal"), "")
    client = await _client(catalog, viewer=BOB)
    try:
        body = await (await client.get(routes_identity.PEOPLE_PATH)).json()
    finally:
        await client.close()
        people.reset_for_tests()
    assert body["directory"] == {"state": "unavailable"}
    assert body["usage_available"] is False
    by_id = {row["principal"]["id"]: row for row in body["people"]}
    # The two legacy sessions count to the owner, who never connected here.
    assert set(by_id) == {ALICE.id, BOB.id, owner_principal().id}
    owner_row = by_id[owner_principal().id]
    assert owner_row["is_owner"] is True and owner_row["sessions_created"] == 2
    assert owner_row["sources"] == ["session_creator"]
    assert owner_row["principal"]["display"]["name"] == "Test Machine"
    assert by_id[BOB.id]["is_owner"] is False
    assert by_id[BOB.id]["sources"] == ["observed", "session_creator"]
    assert by_id[BOB.id]["is_viewer"] is True and by_id[BOB.id]["sessions_created"] == 3
    # Alice was never seen by this process; she is known only as a creator.
    assert by_id[ALICE.id]["sources"] == ["session_creator"]
    assert by_id[ALICE.id]["last_seen_at"] is None and by_id[ALICE.id]["sessions_created"] == 5


def test_people_registry_is_bounded_and_drops_the_least_recently_seen():
    reg = people.ObservedPeople(max_entries=3, clock=iter(range(100)).__next__)
    ps = [local_operator_principal(f"user-{i}") for i in range(5)]
    for p in ps:
        reg.observe(p, "local")
    reg.observe(ps[2], "local")
    kept = [o.principal.id for o in reg.snapshot()]
    assert kept == [ps[2].id, ps[4].id, ps[3].id]


def test_people_usage_is_reported_only_when_a_source_is_wired():
    people.reset_for_tests()
    people.observe(ALICE, "node:a")
    people.register_usage_source(lambda: {ALICE.id: {"active": 1, "queued": 2}})
    try:
        body = people.people_view(ALICE, [])
    finally:
        people.reset_for_tests()
    assert body["usage_available"] is True
    assert body["people"][0]["usage"] == {"active": 1, "queued": 2}
