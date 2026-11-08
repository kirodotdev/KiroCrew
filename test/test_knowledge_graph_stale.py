"""Deletes drop the in-memory entity graph instead of rebuilding it.

An artifact sync or the orphan sweep at boot deletes items; rebuilding the whole
graph there held every entity and relation in memory for a graph no reader had
asked for. The graph is now marked stale and rebuilt by its next reader, which
must still see exactly the tables.
"""

from __future__ import annotations

import pytest

from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


def _item_with_entity(store, name):
    item = store.add_item(f"doc {name}", "content", "doc")
    eid = store.add_entity(name, "concept")
    store.add_mention(item, eid)
    return item, eid


def _count_rebuilds(monkeypatch) -> list[int]:
    calls: list[int] = []
    original = KnowledgeStore._load_graph

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(KnowledgeStore, "_load_graph", counting)
    return calls


@pytest.mark.parametrize("delete", ["batch", "single"])
def test_a_delete_never_materialises_an_unloaded_graph(store, monkeypatch, delete):
    item, _ = _item_with_entity(store, "widget")
    rebuilds = _count_rebuilds(monkeypatch)
    if delete == "batch":
        store.delete_items_batch([item])
    else:
        store.delete_item(item)
    assert rebuilds == [] and store._graph_loaded is False


def test_deleting_a_source_never_materialises_an_unloaded_graph(store, monkeypatch):
    src = store.add_source("s", "file", "file:///s")
    store.add_item("doc", "content", "doc", source_id=src)
    rebuilds = _count_rebuilds(monkeypatch)
    store.delete_source_cascade(src)
    assert rebuilds == [] and store._graph_loaded is False


def test_a_loaded_graph_is_dropped_and_its_next_reader_sees_the_tables(store, monkeypatch):
    item, eid = _item_with_entity(store, "widget")
    _, kept = _item_with_entity(store, "gadget")
    store.ensure_graph_loaded()
    assert store.graph.has_node(eid)
    rebuilds = _count_rebuilds(monkeypatch)
    store.delete_items_batch([item])
    assert rebuilds == [] and store._graph_loaded is False
    assert not store.graph.has_node(eid), "the pruned entity is gone for the next reader"
    assert store.graph.has_node(kept)
    assert rebuilds == [1]


def test_reload_graph_after_an_in_transaction_delete_marks_it_stale(store, monkeypatch):
    item, _ = _item_with_entity(store, "widget")
    store.ensure_graph_loaded()
    rebuilds = _count_rebuilds(monkeypatch)
    store.db.execute("BEGIN IMMEDIATE")
    store.delete_items_batch_in_txn([item])
    store.db.execute("COMMIT")
    store.reload_graph()
    assert rebuilds == [] and store._graph_loaded is False


def test_the_loaded_graph_a_reader_holds_survives_a_later_stale_mark(tmp_path):
    """A handler reads the graph ``ensure_graph_loaded`` returned, not the property.

    A delete that marks the graph stale after the load must not reach that reader:
    the object it holds stays complete, and the next load rebuilds a full graph.
    """
    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        a = store.add_entity(name="a", entity_type="concept")
        b = store.add_entity(name="b", entity_type="concept")
        store.add_entity_relation(a, b, relation_type="rel")
        held = store.ensure_graph_loaded()
        assert held is store._graph and held.has_node(a) and held.has_node(b)
        store.mark_graph_stale()
        assert held.has_node(a) and held.has_node(b), "a stale mark emptied a held graph"
        rebuilt = store.ensure_graph_loaded()
        assert rebuilt is not held and rebuilt.has_node(a) and rebuilt.has_node(b)
    finally:
        store.close()


def test_the_fast_path_never_returns_a_placeholder_published_meanwhile(tmp_path):
    """Read graph, then flag, then identity: a rebuild landing in between misses.

    The fast path reads the empty placeholder a stale mark left, then another
    thread's rebuild publishes the full graph and raises the flag before the fast
    path reads it. Returning the graph it read would hand back the placeholder.
    The interleaving is forced by a graph attribute that runs the rebuild on its
    first read, the moment between the fast path's two reads.
    """
    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        a = store.add_entity(name="a", entity_type="concept")
        store.ensure_graph_loaded()
        store.mark_graph_stale()

        class _RebuildOnFirstRead:
            fired = False

            def __get__(self, obj, objtype=None):
                value = obj.__dict__["_graph"]
                if not _RebuildOnFirstRead.fired:
                    _RebuildOnFirstRead.fired = True
                    obj._load_graph()
                return value

            def __set__(self, obj, value):
                obj.__dict__["_graph"] = value

        store.__dict__["_graph"] = store._graph
        type_patch = type("PatchedStore", (type(store),), {"_graph": _RebuildOnFirstRead()})
        store.__class__ = type_patch
        graph = store.ensure_graph_loaded()
        assert graph.has_node(a), "the fast path returned the empty placeholder"
    finally:
        store.__class__ = KnowledgeStore
        store.close()
