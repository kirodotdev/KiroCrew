"""The memory graph routes over every memory in a store, and the upkeep behind them."""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import memory_graph_files, memory_graph_service
from kiro_crew.dashboard.handlers import memory_edit
from kiro_crew.dashboard.handlers import memory_graph_layout_api as api
from kiro_crew.security import redaction
from kiro_crew.testing import synthetic_credentials

env = _member_env
pytestmark = pytest.mark.xdist_group("member_memory_api")

EVERYWHERE = "-1e9,-1e9,1e9,1e9"
STORES = ["default", "member-alice"]


def _tier(env, name):
    return env.tiers["" if name == "default" else name]


async def _get(env, handler, store, **query):
    response = await handler(request(env, query={"store": store, **query}, owner=True))
    assert response.status == 200, response.text
    return json.loads(response.text)


async def _ready(env, store):
    """The overview once the first build is in, waiting for the build child."""
    # One build at most, waited on for under half the per-test timeout; the reset
    # fixture kills a child that stalls past it.
    for _ in range(2):
        body = await _get(env, api.api_memory_graph_overview, store)
        if body["status"] == memory_graph_service.STATUS_READY:
            return body
        graph = memory_graph_service.graph_for(Path(_tier(env, store)._db_path))
        await asyncio.to_thread(graph.wait_for_build, 50)
    raise AssertionError("the layout never became ready")


def _seed(tier, n, word="zebra", every=1):
    for i in range(n):
        text = f"note {i} about {word}" if i % every == 0 else f"note {i} about otters"
        assert tier.set_semantic(f"user.n{i}", text, 0.9, "user_explicit") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_each_node_names_its_links_both_ways_and_a_forgotten_memory_loses_them(env, name):
    tier = _tier(env, name)
    _seed(tier, 30)
    # Two tight topics, so each memory has near neighbours above the floor.
    table = "memory_items" if tier.algorithm_version == "v2" else "semantic_memory"
    with tier._db_lock:
        for i in range(30):
            v = (1.0, 0.02 * i, 0.0, 0.0) if i % 2 else (0.0, 0.0, 1.0, 0.02 * i)
            tier.db.execute(
                f"UPDATE {table} SET embedding = ? WHERE key = ?",
                (struct.pack("4f", *v), f"user.n{i}"),
            )
        tier.db.commit()
    await _ready(env, name)
    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    links = {n["id"]: {other: sim for other, sim in n["links"]} for n in page["nodes"]}
    assert any(links.values()), "a store of near-identical memories has links"
    for a, out in links.items():
        for b, sim in out.items():
            assert links[b][a] == sim
            assert sim >= 0.55
    gone = next(i for i, out in links.items() if out)
    tier.delete_semantic(gone, "user_explicit")
    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    assert all(gone not in {o for o, _ in n["links"]} for n in page["nodes"])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_overview_counts_every_memory_in_the_store(env, name):
    _seed(_tier(env, name), 30)
    body = await _ready(env, name)
    assert body["totals"].get("fact", 0) == 30
    assert sum(c["count"] for c in body["clusters"]) == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_search_finds_every_match_none_of_them_loaded(env, name):
    _seed(_tier(env, name), 300, every=6)
    await _ready(env, name)

    body = await _get(env, api.api_memory_graph_search, name, q="zebra")

    assert body["total"] == 50
    assert {m["id"] for m in body["matches"]} == {f"user.n{i}" for i in range(0, 300, 6)}
    assert all(isinstance(m["x"], float) and isinstance(m["y"], float) for m in body["matches"])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_box_select_counts_nodes_never_loaded_and_the_list_shows_them(env, name):
    _seed(_tier(env, name), 120)
    await _ready(env, name)
    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE, limit="5")
    assert len(page["nodes"]) == 5

    selected = await _get(env, api.api_memory_graph_select, name, bbox=EVERYWHERE)
    assert selected["count"] == 120

    listed = await memory_edit.api_memory_records(
        request(
            env,
            query={
                "store": name,
                "box": EVERYWHERE,
                "box_layout": selected["layout"],
                "limit": "100",
            },
            owner=True,
        )
    )
    assert listed.status == 200, listed.text
    assert json.loads(listed.text)["total"] == 120


@pytest.mark.asyncio
async def test_a_box_lists_only_the_records_inside_it(env):
    name = "member-alice"
    _seed(_tier(env, name), 40)
    await _ready(env, name)
    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    one = page["nodes"][0]
    box = f"{one['x'] - 1e-6},{one['y'] - 1e-6},{one['x'] + 1e-6},{one['y'] + 1e-6}"

    listed = await memory_edit.api_memory_records(
        request(env, query={"store": name, "box": box, "box_layout": page["layout"]}, owner=True)
    )
    body = json.loads(listed.text)
    assert [e["id"] for e in body["entries"]] == [one["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_box_pages_cover_every_node(env, name):
    _seed(_tier(env, name), 75)
    await _ready(env, name)
    seen, cursor = [], None
    for _ in range(75):
        query = {"bbox": EVERYWHERE, "limit": "20", **({"cursor": cursor} if cursor else {})}
        page = await _get(env, api.api_memory_graph_nodes, name, **query)
        seen += [n["id"] for n in page["nodes"]]
        cursor = page["cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert sorted(seen) == sorted(f"user.n{i}" for i in range(75))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_new_and_forgotten_memories_reach_the_graph_without_a_rebuild(env, name):
    tier = _tier(env, name)
    _seed(tier, 30)
    await _ready(env, name)
    graph = memory_graph_service.graph_for(Path(tier._db_path))
    built_at = graph.sidecar.meta()["built_at"]

    assert tier.set_semantic("user.late", "arrived after the build", 0.9, "user_explicit") is None
    tier.delete_semantic("user.n3", "user_explicit")
    await _get(env, api.api_memory_graph_overview, name)

    ids = set(graph.sidecar.ids_in_box((-1e9, -1e9, 1e9, 1e9)))
    assert "user.late" in ids and "user.n3" not in ids
    assert graph.sidecar.meta()["built_at"] == built_at


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
@pytest.mark.parametrize("change", ["backfill", "new", "forget"])
async def test_box_revision_tracks_reconciled_placements(env, name, change):
    tier = _tier(env, name)
    _seed(tier, 30)
    table = "memory_items" if tier.algorithm_version == "v2" else "semantic_memory"
    vector = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    with tier._db_lock:
        tier.db.execute(f"UPDATE {table} SET embedding = ? WHERE key != 'user.n0'", (vector,))
        tier.db.commit()
    first = await _ready(env, name)
    graph = memory_graph_service.graph_for(Path(tier._db_path))
    built_at = graph.sidecar.meta()["built_at"]
    assert first["layout"] == f"{built_at}.0"
    before = graph.sidecar.positions(["user.n0"])["user.n0"]
    selected = await _get(env, api.api_memory_graph_select, name, bbox=EVERYWHERE)
    filters = {"box": EVERYWHERE, "box_layout": selected["layout"]}
    selection = {"query": {"filters": filters}, "exclude": []}
    body = {"store": name, "selection": selection, "operation": {"type": "forget"}}
    preview = await memory_edit.api_memory_bulk_preview(request(env, body=body, owner=True))
    assert preview.status == 200, preview.text
    token = json.loads(preview.text)["preview_id"]

    if change == "backfill":
        with tier._db_lock:
            tier.db.execute(f"UPDATE {table} SET embedding = ? WHERE key = 'user.n0'", (vector,))
            tier.db.commit()
    elif change == "new":
        assert tier_write(tier, "user.late", "new memory")
    else:
        tier.delete_semantic("user.n0", "user_explicit")
    current = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    assert graph.sidecar.meta()["built_at"] == built_at
    if change == "forget":
        assert current["layout"] == first["layout"]
        assert "user.n0" not in {n["id"] for n in current["nodes"]}
        listed = await _get(env, memory_edit.api_memory_records, name, **filters)
        assert listed["total"] == 29
        return
    if change == "backfill":
        assert graph.sidecar.positions(["user.n0"])["user.n0"] != before
    else:
        assert "user.late" in {n["id"] for n in current["nodes"]}
    assert current["layout"] == f"{built_at}.1"
    listed = await memory_edit.api_memory_records(
        request(env, query={"store": name, **filters}, owner=True)
    )
    assert listed.status == 409, listed.text
    for handler, payload in (
        (memory_edit.api_memory_bulk_preview, body),
        (memory_edit.api_memory_bulk_apply, {"store": name, "preview_id": token}),
    ):
        response = await handler(request(env, body=payload, owner=True))
        assert response.status == 409, response.text
        assert json.loads(response.text)["code"] == "stale_memory_box"
    unchanged = await _get(env, memory_edit.api_memory_records, name)
    assert unchanged["total"] == (31 if change == "new" else 30)


@pytest.mark.asyncio
async def test_every_reply_names_the_layout_and_a_rebuild_changes_it(env, monkeypatch):
    name = "member-alice"
    tier = _tier(env, name)
    _seed(tier, 30)
    first = (await _ready(env, name))["layout"]
    replies = [
        await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE),
        await _get(env, api.api_memory_graph_search, name, q="zebra"),
        await _get(env, api.api_memory_graph_select, name, bbox=EVERYWHERE),
    ]
    assert first and all(r["layout"] == first for r in replies)

    graph = memory_graph_service.graph_for(Path(tier._db_path))
    monkeypatch.setattr(memory_graph_service.gl, "REBUILD_DRIFT", -1.0)
    await _get(env, api.api_memory_graph_overview, name)  # starts a full rebuild
    await asyncio.to_thread(graph.wait_for_build, 50)
    monkeypatch.setattr(memory_graph_service.gl, "REBUILD_DRIFT", 0.2)
    adopted = await _get(env, api.api_memory_graph_overview, name)

    assert adopted["layout"] not in ("", first)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_secret_fact_keys_are_redacted_consistently_in_nodes_and_search(env, name):
    token = synthetic_credentials.github_classic()
    key = f"user.{token}"
    tier = _tier(env, name)
    assert tier_write(tier, "user.secret", "zebra")
    # Stored keys can predate today's write validation or come from an import.
    table = "memory_items" if tier.algorithm_version == "v2" else "semantic_memory"
    with tier._db_lock:
        tier.db.execute(f"UPDATE {table} SET key = ? WHERE key = ?", (key, "user.secret"))
        if tier.algorithm_version == "v2":
            tier.db.execute("UPDATE memory_items SET id = ? WHERE key = ?", (f"key:{key}", key))
        tier.db.commit()
    await _ready(env, name)

    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    search = await _get(env, api.api_memory_graph_search, name, q="zebra")
    # Read again: the second reply comes from the redaction cache and is as clean.
    again = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    for reply in (page, search, again):
        assert token[4:] not in json.dumps(reply)
    assert again["nodes"] == page["nodes"]
    assert len(page["nodes"]) == search["total"] == 1
    node = page["nodes"][0]
    match = search["matches"][0]
    assert node["id"] == node["record"]["id"] == match["id"]
    assert node["id"] != key
    for value in (node["id"], node["record"]["id"], node["label"], match["id"]):
        assert token[:4] not in value
        assert token[4:] not in value
    # Search highlights by node id; the opaque ref still opens the same record.
    assert node["id"] in {m["id"] for m in search["matches"]}
    listed = await _get(env, memory_edit.api_memory_records, name, ref=node["ref"])
    assert [e["id"] for e in listed["entries"]] == [node["record"]["id"]]


@pytest.mark.asyncio
async def test_a_secret_across_the_label_cut_is_redacted_whole(env):
    name = "member-alice"
    token = synthetic_credentials.github_classic()
    # The label is "user.secret = <text>": the token starts at character 47 of
    # it, so a cut at 80 before redaction would leave 33 of its characters.
    text = "x" * 32 + " " + token + " tail"
    assert tier_write(_tier(env, name), "user.secret", text)
    await _ready(env, name)

    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    label = next(n["label"] for n in page["nodes"] if n["id"] == "user.secret")
    assert token[4:30] not in label
    assert len(label) <= memory_graph_files.LABEL_CHARS


def test_a_remembered_redaction_follows_the_contexts_exempt_prefixes(monkeypatch):
    """The token-parameter exemption is read from the installed context, which can move.

    An answer cached under one prefix set must not be served under another: in the
    tightened direction that would return a token the current set redacts.
    """
    prefix = "https://approve.example.com/auth"  # neutral placeholder, not a real host
    value = "Zq7vK2mP9xR4tL8nW3cY6bH1"
    link = f"Approve here: {prefix}?token={value} then reply."
    prefixes = [(prefix,)]
    monkeypatch.setattr(api, "_token_param_exempt_prefixes", lambda: prefixes[0])
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", lambda: prefixes[0])

    assert value in api._redacted(link)
    prefixes[0] = ()
    assert value not in api._redacted(link)
    prefixes[0] = (prefix,)
    assert value in api._redacted(link)


def tier_write(tier, key: str, value: str) -> bool:
    return tier.set_semantic(key, value, 0.9, "user_explicit") is None


@pytest.mark.asyncio
async def test_a_write_during_the_reconcile_after_a_rebuild_reaches_the_graph(env, monkeypatch):
    name = "member-alice"
    tier = _tier(env, name)
    _seed(tier, 30)
    await _ready(env, name)
    graph = memory_graph_service.graph_for(Path(tier._db_path))
    monkeypatch.setattr(memory_graph_service.gl, "REBUILD_DRIFT", -1.0)
    await _get(env, api.api_memory_graph_overview, name)  # starts a full rebuild
    await asyncio.to_thread(graph.wait_for_build, 50)

    real = graph._reconcile
    calls = []

    def reconcile_then_write(*args):
        real(*args)
        if not calls:
            calls.append(1)
            assert (
                tier.set_semantic("user.mid", "written mid-reconcile", 0.9, "user_explicit") is None
            )

    monkeypatch.setattr(graph, "_reconcile", reconcile_then_write)
    await _get(env, api.api_memory_graph_overview, name)  # adopts the build
    assert calls
    await _get(env, api.api_memory_graph_overview, name)

    assert "user.mid" in set(graph.sidecar.ids_in_box((-1e9, -1e9, 1e9, 1e9)))


class _HeldBuild:
    """A rebuild child that has not exited yet, until it is killed."""

    def __init__(self):
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return None


@pytest.mark.asyncio
async def test_the_old_layout_serves_during_a_rebuild_and_forgetting_it_ends_the_build(
    env, monkeypatch
):
    name = "member-alice"
    tier = _tier(env, name)
    _seed(tier, 30)
    await _ready(env, name)
    monkeypatch.setattr(memory_graph_service.gl, "REBUILD_DRIFT", -1.0)
    monkeypatch.setattr(memory_graph_service.gl, "spawn_rebuild", lambda *_a: _HeldBuild())

    assert (
        tier.set_semantic("user.late", "arrived during the rebuild", 0.9, "user_explicit") is None
    )
    for _ in range(2):
        body = await _get(env, api.api_memory_graph_overview, name)
        assert body["status"] == memory_graph_service.STATUS_READY
        assert body["totals"]["fact"] == 31
    graph = memory_graph_service.graph_for(Path(tier._db_path))
    held, out = graph._build.proc, graph._build.out
    assert isinstance(held, _HeldBuild)
    out.write_bytes(b"half a build")

    memory_graph_service.forget_graphs()

    assert held.killed
    assert not out.exists()


class _StuckBuild(_HeldBuild):
    """A build child that never exits; waiting on it is a test failure."""

    def wait(self, timeout=None):
        # Only the reset's stop() may wait, after it killed the child.
        assert self.killed, "a graph request waited on the build child"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["overview", "nodes", "search", "select"])
async def test_a_request_during_a_build_answers_at_once_without_waiting(env, monkeypatch, route):
    name = "member-alice"
    _seed(_tier(env, name), 30)
    monkeypatch.setattr(memory_graph_service.gl, "spawn_rebuild", lambda *_a: _StuckBuild())
    handler = getattr(api, f"api_memory_graph_{route}")

    for _ in range(3):
        body = await _get(env, handler, name, bbox=EVERYWHERE, q="zebra")
        assert body["status"] == memory_graph_service.STATUS_BUILDING

    graph = memory_graph_service.graph_for(Path(_tier(env, name)._db_path))
    assert isinstance(graph._build.proc, _StuckBuild)


@pytest.mark.asyncio
async def test_a_build_an_earlier_gateway_left_is_removed_when_a_new_build_starts(env, monkeypatch):
    name = "member-alice"
    tier = _tier(env, name)
    _seed(tier, 30)
    await _ready(env, name)
    store_dir = Path(tier._db_path).parent
    # What a gateway that restarted mid-build leaves: its pid's file and journal.
    left = store_dir / memory_graph_files.building_name(999_999_999)
    left.write_bytes(b"half a build")
    journal = Path(f"{left}-journal")
    journal.write_bytes(b"j")
    monkeypatch.setattr(memory_graph_service.gl, "REBUILD_DRIFT", -1.0)
    monkeypatch.setattr(memory_graph_service.gl, "spawn_rebuild", lambda *_a: _HeldBuild())

    assert tier.set_semantic("user.late", "starts a rebuild", 0.9, "user_explicit") is None
    await _get(env, api.api_memory_graph_overview, name)

    assert memory_graph_service.graph_for(Path(tier._db_path))._build is not None
    assert not left.exists()
    assert not journal.exists()
    assert (store_dir / memory_graph_files.GRAPH_LAYOUT_FILE).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_a_node_opens_its_own_record_in_the_list(env, name):
    tier = _tier(env, name)
    _seed(tier, 5)
    assert tier.write_episodic("a lesson-free experience", tags=["t"], defer_embedding=True)
    await _ready(env, name)
    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    for node in page["nodes"]:
        listed = await memory_edit.api_memory_records(
            request(env, query={"store": name, "ref": node["ref"]}, owner=True)
        )
        entries = json.loads(listed.text)["entries"]
        assert [(e["kind"], e["id"]) for e in entries] == [
            (node["record"]["kind"], node["record"]["id"])
        ]


@pytest.mark.asyncio
async def test_node_labels_are_the_memory_text(env):
    name = "member-alice"
    assert (
        _tier(env, name).set_semantic("user.pet", "alice has a cat", 0.9, "user_explicit") is None
    )
    await _ready(env, name)
    page = await _get(env, api.api_memory_graph_nodes, name, bbox=EVERYWHERE)
    assert [n["label"] for n in page["nodes"]] == ["user.pet = alice has a cat"]


def _age(tier, text: str, days: int) -> None:
    """Backdate the experience *text* by *days* in the table its store keeps it in."""
    row = next(r for r in tier.get_episodic_list(limit=10) if r["text"] == text)
    table = "memory_items" if tier.algorithm_version == "v2" else "episodic_memories"
    when = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    with tier._db_lock:
        tier.db.execute(f"UPDATE {table} SET created_at = ? WHERE id = ?", (when, row["id"]))
        tier.db.commit()


@pytest.mark.asyncio
async def test_experiences_fade_by_their_recall_weight(env):
    tier = _tier(env, "default")
    assert tier.algorithm_version == "v1"
    assert tier.write_episodic("fresh memory", tags=["t"], defer_embedding=True)
    assert tier.write_episodic("old memory", tags=["t"], defer_embedding=True)
    _age(tier, "old memory", 90)
    await _ready(env, "default")

    page = await _get(env, api.api_memory_graph_nodes, "default", bbox=EVERYWHERE, kind="episode")
    weight = {n["label"]: n["weight"] for n in page["nodes"]}
    assert weight["old memory"] < weight["fresh memory"] <= 1.0


@pytest.mark.asyncio
async def test_a_v2_store_draws_no_experience_faded(env):
    tier = _tier(env, "member-alice")
    assert tier.algorithm_version == "v2"
    assert tier.write_episodic("fresh memory", tags=["t"], defer_embedding=True)
    assert tier.write_episodic("old memory", tags=["t"], defer_embedding=True)
    _age(tier, "old memory", 90)
    await _ready(env, "member-alice")

    page = await _get(
        env, api.api_memory_graph_nodes, "member-alice", bbox=EVERYWHERE, kind="episode"
    )
    assert {n["label"]: n["weight"] for n in page["nodes"]} == {
        "old memory": 1.0,
        "fresh memory": 1.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [{"bbox": "1,2,3"}, {"bbox": "a,b,c,d"}, {"bbox": EVERYWHERE, "kind": "people"}],
)
async def test_malformed_queries_are_refused(env, query):
    response = await api.api_memory_graph_nodes(
        request(env, query={"store": "member-alice", **query}, owner=True)
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_a_non_owner_cannot_read_the_graph(env):
    for handler in (
        api.api_memory_graph_overview,
        api.api_memory_graph_nodes,
        api.api_memory_graph_search,
        api.api_memory_graph_select,
    ):
        response = await handler(request(env, query={"store": "member-alice", "bbox": EVERYWHERE}))
        assert response.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("name", STORES)
async def test_box_from_replaced_layout_is_refused_before_any_edit(env, name, monkeypatch):
    tier = _tier(env, name)
    _seed(tier, 5)
    layout = (await _ready(env, name))["layout"]
    filters = {"box": EVERYWHERE, "box_layout": layout}
    selection = {"query": {"filters": filters}, "exclude": []}
    body = {"store": name, "selection": selection, "operation": {"type": "forget"}}
    preview = await memory_edit.api_memory_bulk_preview(request(env, body=body, owner=True))
    assert preview.status == 200, preview.text
    token = json.loads(preview.text)["preview_id"]
    assert json.loads(preview.text)["changed_count"] == 5

    graph = memory_graph_service.graph_for(Path(tier._db_path))
    monkeypatch.setattr(memory_graph_service.gl, "REBUILD_DRIFT", -1.0)
    await _get(env, api.api_memory_graph_overview, name)
    await asyncio.to_thread(graph.wait_for_build, 50)
    monkeypatch.setattr(memory_graph_service.gl, "REBUILD_DRIFT", 0.2)
    adopted = await _get(env, api.api_memory_graph_overview, name)
    assert adopted["layout"] != layout

    for view in ({}, {"sort": "updated", "facets": "1"}):
        listed = await memory_edit.api_memory_records(
            request(env, query={"store": name, **filters, **view}, owner=True)
        )
        assert listed.status == 409, listed.text
        assert json.loads(listed.text)["code"] == "stale_memory_box"
    for handler, payload in (
        (memory_edit.api_memory_records_refresh, {"store": name, "selection": selection}),
        (memory_edit.api_memory_bulk_preview, body),
        (memory_edit.api_memory_bulk_preview, {"store": name, "preview_id": token, "offset": 0}),
        (memory_edit.api_memory_bulk_apply, {"store": name, "preview_id": token}),
    ):
        response = await handler(request(env, body=payload, owner=True))
        assert response.status == 409, response.text
        assert json.loads(response.text)["code"] == "stale_memory_box"
    unchanged = await _get(env, memory_edit.api_memory_records, name)
    assert unchanged["total"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [None, "", 3, ["L1"]])
async def test_box_requires_a_layout_id(env, layout):
    filters = {"box": EVERYWHERE}
    if layout is not None:
        filters["box_layout"] = layout
    body = {
        "store": "default",
        "selection": {"query": {"filters": filters}},
        "operation": {"type": "forget"},
    }
    response = await memory_edit.api_memory_bulk_preview(request(env, body=body, owner=True))
    assert response.status == 400, response.text
    assert "box_layout" in json.loads(response.text)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["default", "tag"])
async def test_live_decay_rates_refresh_graph_weights_without_a_write(
    env, monkeypatch, manual_clock, setting
):
    manual_clock.install(monkeypatch, memory_graph_service, time=False, datetime=True)
    tier = _tier(env, "default")
    assert tier.write_episodic("aged memory", tags=["t"], defer_embedding=True)
    created = (manual_clock.now(timezone.utc) - timedelta(days=30)).isoformat()
    with tier._db_lock:
        tier.db.execute("UPDATE episodic_memories SET created_at = ?", (created,))
        tier.db.commit()
    monkeypatch.setattr(tier, "_decay_default", 0.01)
    monkeypatch.setattr(tier, "_decay_by_tag", {})
    await _ready(env, "default")
    before = await _get(env, api.api_memory_graph_nodes, "default", bbox=EVERYWHERE)
    stamp = memory_graph_service._files_stamp(Path(tier._db_path))
    if setting == "default":
        monkeypatch.setattr(tier, "_decay_default", 0.1)
    else:
        tier._decay_by_tag["t"] = 0.1
    after = await _get(env, api.api_memory_graph_nodes, "default", bbox=EVERYWHERE)
    assert memory_graph_service._files_stamp(Path(tier._db_path)) == stamp
    assert after["layout"] == before["layout"]
    assert 0 < after["nodes"][0]["weight"] < before["nodes"][0]["weight"]
