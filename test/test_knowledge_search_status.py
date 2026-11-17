"""Knowledge search keeps every leg to one item status.

Archive in the Knowledge Library sets ``items.status = 'archived'``, and an
archived item is out of agent search: ``HybridRetriever.search`` takes a
``status`` (``"active"`` by default, ``None`` for every status) and applies it
on the keyword, graph and vector legs alike, so no leg hands the item to
``local_knowledge_search``. The Library's own search passes its status filter
through, so its Archived and All statuses views still find archived items.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers.knowledge import list_items
from kiro_crew.knowledge import retrieval
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.retrieval import ANY_EMBEDDING_SPACE, HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path: Path, opened):
    retrieval.clear_vector_matrix_cache()
    yield opened(KnowledgeStore(str(tmp_path / "kb.db")))
    retrieval.clear_vector_matrix_cache()


def _ids(results: list[dict]) -> list[str]:
    return [r["id"] for r in results]


def _runbooks(store: KnowledgeStore, source_id: str | None = None) -> tuple[str, str]:
    """Two keyword matches for "zebrafish": an active one and an archived one."""
    kept = store.add_item("Runbook A", "zebrafish migration steps", "doc", source_id=source_id)
    gone = store.add_item("Runbook B", "zebrafish rollback steps", "doc", source_id=source_id)
    store.update_item(gone, status="archived")
    return kept, gone


def test_keyword_leg_skips_an_archived_item(store):
    kept, gone = _runbooks(store)
    found = _ids(HybridRetriever(store).search("zebrafish"))
    assert kept in found
    assert gone not in found


@pytest.mark.parametrize("json1", [True, False], ids=["json_each", "chunked"])
def test_graph_leg_skips_an_archived_item(store, monkeypatch, json1):
    # Only the graph leg can reach this item: its text never names the entity.
    gone = store.add_item("Notes", "nothing searchable here", "doc")
    store.add_mention(gone, store.add_entity("Quokka", "concept"))
    store.update_item(gone, status="archived")
    monkeypatch.setattr(retrieval, "_has_json1", lambda _db: json1)
    assert gone not in _ids(HybridRetriever(store).search("Quokka"))
    assert gone in _ids(HybridRetriever(store).search("Quokka", status=None))


def test_archived_items_do_not_take_the_graph_legs_slots(store):
    # The archived items carry more mentions, so a cap applied before the status
    # filter would spend the whole window on them.
    quokka = store.add_entity("Quokka", "concept")
    wombat = store.add_entity("Wombat", "concept")
    store.add_entity_relation(quokka, wombat, "related")
    kept = store.add_item("Kept", "nothing searchable here", "doc")
    store.add_mention(kept, quokka)
    for n in range(3):
        gone = store.add_item(f"Gone {n}", "nothing searchable here", "doc")
        store.add_mention(gone, quokka)
        store.add_mention(gone, wombat)
        store.update_item(gone, status="archived")
    assert HybridRetriever(store)._graph_search("Quokka", limit=1) == [(kept, 1)]


class _ExplainingConnection:
    """Records the query plan of every mentions count, then runs it as asked."""

    def __init__(self, inner, plans: list[str]):
        self._inner = inner
        self._plans = plans

    def execute(self, sql, params=()):
        if "FROM mentions" in sql:
            for step in self._inner.execute("EXPLAIN QUERY PLAN " + sql, params):
                self._plans.append(str(step[3]))
        return self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("json1", [True, False], ids=["json_each", "chunked"])
def test_the_graph_legs_status_check_does_not_scan_items_by_status(store, monkeypatch, json1):
    # Joining items lets SQLite drive the count from idx_items_status, reading
    # every item of the status instead of only the counted ones.
    store.add_mention(store.add_item("Notes", "n", "doc"), store.add_entity("Quokka", "concept"))
    plans: list[str] = []
    monkeypatch.setattr(store._thread_local, "conn", _ExplainingConnection(store.db, plans))
    monkeypatch.setattr(retrieval, "_has_json1", lambda _db: json1)
    assert HybridRetriever(store)._graph_search("Quokka") != []
    assert plans
    assert not [step for step in plans if "idx_items_status" in step]


def test_the_vector_leg_follows_the_status_asked_for(store):
    vec = [1.0, 0.0, 0.0, 0.0]
    gone = store.add_item("Gone", "c", "doc", embedding=floats_to_bytes(vec))
    store.update_item(gone, status="archived")
    retriever = HybridRetriever(store, embedder=lambda _q: vec, embed_sig=ANY_EMBEDDING_SPACE)
    assert retriever._vector_search("q") == []
    assert retriever._vector_search("q", status=None) == [(gone, 1)]
    assert retriever._vector_search("q", status="archived") == [(gone, 1)]


def test_a_named_status_searches_only_that_status(store):
    kept, gone = _runbooks(store)
    assert _ids(HybridRetriever(store).search("zebrafish", status="archived")) == [gone]
    assert set(_ids(HybridRetriever(store).search("zebrafish", status=None))) == {kept, gone}


def test_the_agent_search_tool_leaves_an_archived_item_out(tmp_path, monkeypatch, opened):
    from kiro_crew import mcp_core

    db_dir = tmp_path / "workspace" / "knowledge"
    db_dir.mkdir(parents=True)
    store = opened(KnowledgeStore(str(db_dir / "knowledge.db")))
    _runbooks(store)
    monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_core, "_get_knowledge_search", lambda _db, _cfg: (store, None))
    result = mcp_core._call_tool_inner("local_knowledge_search", {"query": "zebrafish"})
    assert "Runbook A" in result
    assert "Runbook B" not in result


async def _library_titles(store: KnowledgeStore, query: dict) -> set[str]:
    req = MagicMock()
    req.query = query
    req.app = {}
    with patch("kiro_crew.dashboard.handlers.knowledge._store", return_value=store):
        resp = await list_items(req)
    return {item["title"] for item in json.loads(resp.body.decode())["items"]}


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "source-scoped"])
@pytest.mark.parametrize(
    ("status", "titles"),
    [
        ("active", {"Runbook A"}),
        ("archived", {"Runbook B"}),
        (None, {"Runbook A", "Runbook B"}),
    ],
    ids=["active", "archived", "all-statuses"],
)
@pytest.mark.asyncio
async def test_the_library_search_finds_the_status_its_filter_names(store, status, titles, scoped):
    src = store.add_source("runbooks", "local_folder", "/tmp/runbooks")
    _runbooks(store, source_id=src)
    query = {"q": "zebrafish", "limit": "20"}
    if status is not None:
        query["status"] = status
    if scoped:
        query["source_id"] = src
    assert await _library_titles(store, query) == titles
