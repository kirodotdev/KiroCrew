"""The Crew worker list keeps what only the session tree or a ledger write moves.

``GET /api/chat/slots/{root}/crew-workers`` reads, per root, the closed workers (a walk
of the session tree), their saved titles and every crew member's ledger store. Those are
kept and read again only when the tree object, the live keys or a store's change mark
moved; the live rows are read fresh on every call. These tests drive the real handler
with a real work-ledger store and count the reads.
"""

from __future__ import annotations

import gc
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode
from kiro_crew import work_ledger as wl
from kiro_crew.crew_log.session_tree import TreeNode
from kiro_crew.dashboard.handlers import crew_workers


class _Tree:
    """A seeded session tree whose ``nodes()`` object is replaced when it changes."""

    seeded_for_current_store = True

    def __init__(self, edges: dict[str, str | None]) -> None:
        self.set(edges)

    def set(self, edges: dict[str, str | None]) -> None:
        self._nodes = {slot: TreeNode(slot, parent, False) for slot, parent in edges.items()}

    def nodes(self) -> dict[str, TreeNode]:
        return self._nodes


class _Log:
    """Saved session metadata, counting the reads."""

    def __init__(self, titles: dict[str, str]) -> None:
        self.titles = titles
        self.reads: list[str] = []

    def get_metadata(self, key: str) -> dict[str, str]:
        self.reads.append(key)
        slot = key.removeprefix("dashboard:")
        return {"title": self.titles.get(slot, ""), "agent": "kirocrew-worker"}


def _row(key: str, **over: Any) -> dict[str, Any]:
    return {"key": key, "agent": "kirocrew-worker", "title": key, "running": False, **over}


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.crew_log import session_tree_projection

        self.rows = [_row("root", agent="kirocrew-conductor"), _row("w1", created_by="root")]
        self.tree = _Tree({"root": None, "w1": "root", "c1": "root", "c2": "c1"})
        self.log = _Log({"c1": "Closed one", "c2": "Closed two"})
        self.ledger_reads: list[str] = []
        self.walks = 0
        monkeypatch.setattr(session_tree_projection, "projection", lambda: self.tree)
        monkeypatch.setattr(
            crew_mode,
            "persisted_parents",
            lambda: {s: n.parent_slot for s, n in self.tree.nodes().items() if n.parent_slot},
        )
        walk, read = crew_workers._closed_workers, crew_workers._ledger_items

        def _walk(root: str, live: set[str]) -> list[str]:
            self.walks += 1
            return walk(root, live)

        def _read(key: str) -> dict[str, crew_workers.LedgerFacts]:
            self.ledger_reads.append(key)
            return read(key)

        async def _allow(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(crew_workers, "_closed_workers", _walk)
        monkeypatch.setattr(crew_workers, "_ledger_items", _read)
        monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
        monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _allow)

    def _state(self) -> Any:
        def serialize_slots(**_k: Any) -> list[dict[str, Any]]:
            out = [dict(r) for r in self.rows]
            crew_mode.stamp_rows(out)
            return out

        return SimpleNamespace(
            serialize_slots=serialize_slots,
            get_slot=lambda key: None,
            conversation_log=self.log,
        )

    async def read(self) -> dict[str, dict[str, Any]]:
        app = web.Application()
        app["state"] = self._state()
        req = make_mocked_request("GET", "/x", app=app, match_info={"slot": "root"})
        resp = await crew_workers.api_crew_workers(req)
        assert resp.status == 200, resp.text
        return {w["key"]: w for w in json.loads(resp.text)["workers"]}

    def reset_counts(self) -> None:
        self.ledger_reads.clear()
        self.log.reads.clear()
        self.walks = 0


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


@pytest.mark.asyncio
async def test_an_unchanged_world_is_answered_without_a_read(world: _World) -> None:
    first = await world.read()
    assert set(first) == {"w1", "c1", "c2"}
    assert first["c1"]["title"] == "Closed one"
    assert first["c2"]["dispatched_by"] == "c1"
    assert world.walks == 1 and world.ledger_reads and world.log.reads
    world.reset_counts()
    again = await world.read()
    assert again == first
    assert (world.walks, world.ledger_reads, world.log.reads) == (0, [], [])


@pytest.mark.asyncio
async def test_a_tree_change_reads_only_the_new_closed_worker(world: _World) -> None:
    await world.read()
    world.reset_counts()
    world.log.titles["c3"] = "Closed three"
    world.tree.set({"root": None, "w1": "root", "c1": "root", "c2": "c1", "c3": "w1"})
    out = await world.read()
    assert out["c3"]["title"] == "Closed three"
    assert out["c3"]["dispatched_by"] == "w1"
    assert world.walks == 1
    assert world.log.reads == ["dashboard:c3"]
    assert world.ledger_reads == ["c3"]


@pytest.mark.asyncio
async def test_a_ledger_write_shows_on_the_next_read(world: _World) -> None:
    wl.ensure_conductor("root")
    wl.create_bound_item(
        "root",
        "it_0000c001",
        title="Port the pages",
        acceptance={},
        decision="",
        worker_session_key="w1",
    )
    assert (await world.read())["w1"]["summary"] == "Port the pages"
    world.reset_counts()
    wl.apply_worker_report("root", "it_0000c001", status="progress", summary="Half the pages")
    out = await world.read()
    assert out["w1"]["summary"] == "Half the pages"
    assert out["w1"]["ledger_item"] == "it_0000c001"
    # Only the store that was written is read again.
    assert world.ledger_reads == ["root"]
    assert (world.walks, world.log.reads) == (0, [])


@pytest.mark.asyncio
async def test_a_live_status_change_shows_without_a_cached_read(world: _World) -> None:
    assert (await world.read())["w1"]["status"] == crew_workers.STATUS_IDLE
    world.reset_counts()
    world.rows[1]["running"] = True
    world.rows[1]["title"] = "Renamed live"
    out = await world.read()
    assert out["w1"]["status"] == crew_workers.STATUS_WORKING
    assert out["w1"]["title"] == "Renamed live"
    assert (world.walks, world.ledger_reads, world.log.reads) == (0, [], [])


@pytest.mark.asyncio
async def test_a_worker_seen_live_reads_its_title_again_once_closed(world: _World) -> None:
    await world.read()
    world.rows.append(_row("c1", created_by="root", title="Reopened"))
    assert (await world.read())["c1"]["status"] != crew_workers.STATUS_CLOSED
    world.rows.pop()
    world.log.titles["c1"] = "Renamed after reopen"
    assert (await world.read())["c1"]["title"] == "Renamed after reopen"


@pytest.mark.asyncio
async def test_a_kept_answer_older_than_the_bound_is_read_again(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.read()
    world.reset_counts()
    # A negative bound makes every kept answer already too old: no clock is waited on.
    monkeypatch.setattr(crew_workers, "CACHE_MAX_AGE_SECS", -1.0)  # flake-ok: no clock wait
    await world.read()
    assert world.walks == 1 and world.log.reads and world.ledger_reads


def test_every_lock_hold_moves_its_stores_change_mark() -> None:
    wl.ensure_conductor("mark-board")
    before, newest = wl.change_mark("mark-board"), wl.last_change_mark()
    with wl.conductor_lock("mark-board"):
        pass
    after_conductor = wl.change_mark("mark-board")
    assert after_conductor > before and wl.last_change_mark() > newest
    wl.create_bound_item(
        "mark-board",
        "it_0000c002",
        title="t",
        acceptance={},
        decision="",
        worker_session_key="w9",
    )
    after_create = wl.change_mark("mark-board")
    with wl.item_lock("mark-board", "it_0000c002", create=False):
        pass
    assert wl.change_mark("mark-board") > after_create > after_conductor


def test_a_dropped_change_mark_never_answers_a_mark_kept_from_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wl, "CHANGE_MARKS_KEPT", 2)
    seen = wl.change_mark("drop-a")
    for key in ("drop-a", "drop-b", "drop-c"):
        wl._mark_changed(key)
    assert len(wl._change_marks) == 2
    # drop-a was written after `seen` was read, then dropped: it must not answer `seen`.
    assert wl.change_mark("drop-a") > seen
    assert wl.change_mark("drop-c") == wl.last_change_mark()


@pytest.mark.asyncio
async def test_a_root_past_the_budget_is_answered_in_full_and_not_kept(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crew_workers, "CACHE_ROOT_SESSIONS", 1)
    first = await world.read()
    assert set(first) == {"w1", "c1", "c2"}
    entry = crew_workers._root_caches["root"]
    assert (entry.walk, entry.ledger, entry.boards, entry.titles) == (None, None, {}, {})
    world.reset_counts()
    assert await world.read() == first
    assert world.walks == 1 and world.ledger_reads and world.log.reads


@pytest.mark.asyncio
async def test_no_root_entry_holds_the_session_tree(world: _World) -> None:
    old = world.tree.nodes()
    await world.read()
    entry = crew_workers._root_caches["root"]
    assert entry.walk is not None and entry.walk[3] == {"c1": "root", "c2": "c1"}
    world.tree.set({"root": None, "w1": "root", "c1": "root", "c2": "c1", "c3": "c1"})
    await world.read()
    holders = [entry.__dict__, entry.walk, crew_workers._tree_seen]
    assert not any(any(r is h for h in holders) for r in gc.get_referrers(old))


@pytest.mark.asyncio
async def test_a_title_past_the_cap_is_answered_in_full_and_read_each_time(
    world: _World,
) -> None:
    long = "x" * (crew_workers.CACHE_TITLE_CHARS + 1)
    world.log.titles["c1"] = long
    assert (await world.read())["c1"]["title"] == long
    world.reset_counts()
    assert (await world.read())["c1"]["title"] == long
    assert world.log.reads == ["dashboard:c1"]


@pytest.mark.asyncio
async def test_a_store_that_left_the_crew_is_not_kept(world: _World) -> None:
    await world.read()
    entry = crew_workers._root_caches["root"]
    assert "c2" in entry.boards and "c2" in entry.titles
    world.tree.set({"root": None, "w1": "root", "c1": "root"})
    out = await world.read()
    assert "c2" not in out
    assert "c2" not in entry.boards and "c2" not in entry.titles
