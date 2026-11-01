"""Regression: the graph leg must survive a hub entity bigger than SQLite's variable limit.

``HybridRetriever._graph_search`` expands a matched entity to its two-hop
neighbourhood and counts item mentions over those ids. A high-degree ("hub")
entity's neighbourhood can exceed SQLite's bound-variable limit
(``SQLITE_MAX_VARIABLE_NUMBER`` -- 32766 on 3.32+, 999 before), and one
placeholder per id then raised ``too many SQL variables`` out of the whole
search. These tests pin that the leg now (a) answers for an oversized
neighbourhood and (b) degrades to keyword+vector instead of failing the search
when the leg itself raises ``OperationalError``.

The ``store`` fixture comes from ``knowledge_helpers.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from knowledge_helpers import store_fixture  # noqa: F401

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.knowledge import retrieval as retrieval_mod
from kiro_crew.knowledge.retrieval import HybridRetriever

# The oldest SQLite default SQLITE_MAX_VARIABLE_NUMBER (pre-3.32); 32766 from
# 3.32 on. The over-limit tests pin the store's connection to this floor so a
# small, fast neighbourhood reproduces the crash on EVERY build rather than
# depending on the host build's native (and possibly very large) limit.
_VAR_LIMIT = 999


@contextmanager
def _capped_variable_limit(store, limit: int = _VAR_LIMIT):
    """Pin the store connection's bound-variable limit to ``limit`` for the body.

    Uses the driver's own ``setlimit`` where the connection exposes it (so the
    real SQLite guard fires); otherwise installs a thin proxy over the thread's
    connection whose ``execute`` raises ``OperationalError('too many SQL
    variables')`` once a statement binds more than ``limit`` parameters and
    delegates everything else. Either way an IN-list over ``limit`` ids raises
    exactly as it would on a stock build, with no dependence on this build's
    native limit. ``store.db`` is a read-only property backed by a thread-local
    connection, so the proxy is installed and removed there.
    """
    conn = store.db  # materialise the thread's connection
    const = getattr(sqlite3, "SQLITE_LIMIT_VARIABLE_NUMBER", None)
    if const is not None and hasattr(conn, "setlimit"):
        previous = conn.setlimit(const, limit)
        try:
            yield
        finally:
            conn.setlimit(const, previous)
        return

    class _CappedProxy:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if len(params) > limit:
                raise sqlite3.OperationalError("too many SQL variables")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    store._thread_local.conn = _CappedProxy(conn)
    try:
        yield
    finally:
        store._thread_local.conn = conn


class TestGraphSearchVariableLimit:
    def test_hub_entity_neighbourhood_over_variable_limit(self, store):
        """A two-hop neighbourhood larger than the variable limit still returns rows.

        The connection's bound-variable limit is pinned to the 999 floor, and
        the hub gets ~1,200 neighbours: one placeholder per id (> 999) raises
        ``too many SQL variables`` on ``main``, while ``json_each`` binds two
        parameters and answers. Fast and deterministic on every build.
        """
        hub = store.add_entity("hub-entity", "concept")
        leaf = store.add_entity("real-leaf", "concept")
        store.add_entity_relation(hub, leaf, "uses")
        hub_item = store.add_item("Hub doc", "about the hub", "doc")
        leaf_item = store.add_item("Leaf doc", "about a leaf", "doc")
        store.add_mention(hub_item, hub)
        store.add_mention(leaf_item, leaf)
        # get_neighbors walks the in-memory graph, so the hub's neighbourhood
        # only needs to exist there; the mentions query is what receives the ids.
        for _ in range(1_200):
            store.graph.add_edge(hub, str(uuid4()), relation_type="uses")

        with _capped_variable_limit(store):
            results = HybridRetriever(store)._graph_search("hub-entity", limit=5)

        assert {item for item, _ in results} == {hub_item, leaf_item}
        assert [rank for _, rank in results] == [1, 2]

    def test_full_search_survives_hub_entity(self, store):
        """The user-visible search() answers instead of raising on a hub term.

        Same 999-pinned limit and ~1,200-neighbour hub as above, driven through
        the whole ``search()`` path.
        """
        hub = store.add_entity("hub-entity", "concept")
        hub_item = store.add_item("Hub doc", "about the hub-entity", "doc")
        store.add_mention(hub_item, hub)
        for _ in range(1_200):
            store.graph.add_edge(hub, str(uuid4()), relation_type="uses")

        with _capped_variable_limit(store):
            # Must not raise OperationalError: too many SQL variables.
            results = HybridRetriever(store).search("hub-entity", limit=5)

        assert any(r["id"] == hub_item for r in results)

    def test_real_large_neighbourhood_uses_json_each(self, store):
        """At the real (uncapped) limit a 33k-neighbour hub still answers.

        A fixed 33,000 neighbours sits above the 32,766 default, so the json_each
        single-parameter form is exercised against the live connection without a
        probe-driven, build-dependent neighbourhood size.
        """
        hub = store.add_entity("hub-entity", "concept")
        hub_item = store.add_item("Hub doc", "about the hub", "doc")
        store.add_mention(hub_item, hub)
        for _ in range(33_000):
            store.graph.add_edge(hub, str(uuid4()), relation_type="uses")

        results = HybridRetriever(store)._graph_search("hub-entity", limit=5)

        assert {item for item, _ in results} == {hub_item}

    def test_graph_leg_operationalerror_degrades_to_keyword(self, store, monkeypatch):
        """A graph-leg OperationalError returns keyword/vector results, not a crash."""
        ent = store.add_entity("Auth", "service")
        item = store.add_item("Auth doc", "all about Auth", "doc")
        store.add_mention(item, ent)
        retriever = HybridRetriever(store)

        def _boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("too many SQL variables")

        monkeypatch.setattr(retriever, "_count_item_mentions", _boom)

        # Graph leg degrades to [] rather than propagating.
        assert retriever._graph_search("Auth") == []
        # The whole search still answers from the keyword leg.
        results = retriever.search("Auth", limit=5)
        assert any(r["id"] == item for r in results)

    def test_small_neighbourhood_still_ranks_by_mention_count(self, store):
        """The new form keeps the count ranking and the item_id tie-break."""
        a = store.add_entity("Auth", "service")
        b = store.add_entity("JWT", "concept")
        store.add_entity_relation(a, b, "uses")
        both = store.add_item("Both", "mentions both", "doc")
        one = store.add_item("One", "mentions one", "doc")
        store.add_mention(both, a)
        store.add_mention(both, b)
        store.add_mention(one, b)

        results = HybridRetriever(store)._graph_search("Auth")

        assert [item for item, _ in results] == [both, one]

    def test_chunked_fallback_matches_json_each(self, store, monkeypatch):
        """Without JSON1, the chunked IN-list returns the same rows, order and cap.

        A neighbourhood larger than ``_MENTIONS_IN_CHUNK`` is split across
        several statements and merged, so this pins that the chunked path keeps
        the ``cnt DESC, item_id`` order and the ``LIMIT`` the single-statement
        ``json_each`` query produces -- including a count tie broken by
        ``item_id``.
        """
        # Small chunk so a modest neighbourhood crosses several boundaries.
        monkeypatch.setattr(retrieval_mod, "_MENTIONS_IN_CHUNK", 5)

        # Entities that receive mentions, plus padding entities with none, so
        # the id set comfortably exceeds the chunk size.
        counted = [store.add_entity(f"E{i}", "concept") for i in range(8)]
        padding = [store.add_entity(f"P{i}", "concept") for i in range(20)]
        all_ids = {e for e in counted} | {p for p in padding}

        # Varied counts, including a deliberate tie (two items at 3 mentions)
        # whose order the item_id tie-break must settle.
        items = [store.add_item(f"I{i}", f"doc {i}", "doc") for i in range(6)]
        counts = [5, 4, 3, 3, 2, 1]
        for item, k in zip(items, counts):
            for ent in counted[:k]:
                store.add_mention(item, ent)

        retriever = HybridRetriever(store)

        monkeypatch.setattr(retrieval_mod, "_has_json1", lambda db: True)
        json_rows = retriever._count_item_mentions(all_ids, limit=4)

        monkeypatch.setattr(retrieval_mod, "_has_json1", lambda db: False)
        chunked_rows = retriever._count_item_mentions(all_ids, limit=4)

        # Same items, same counts, same order, same LIMIT truncation.
        assert list(json_rows.items()) == list(chunked_rows.items())
        assert len(chunked_rows) == 4
