"""The entity graph is materialised by its first reader, not by construction.

Boot cost, not correctness, is the reason (#8329): ``_load_graph`` full-scans
``entities`` + ``entity_relations`` on the event-loop thread before the socket
binds. These tests pin the three properties that make the deferral safe rather
than merely cheaper -- construction does not scan, the first touch is
serialised, and the readers that run on the loop materialise it off-loop.
"""

import asyncio
import threading

import pytest

from kiro_crew.knowledge.store import KnowledgeStore, SimpleDiGraph


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


def _seed(store, *, entities=3):
    ids = [store.add_entity(name=f"e{i}", entity_type="concept") for i in range(entities)]
    for a, b in zip(ids, ids[1:]):
        store.add_entity_relation(a, b, relation_type="rel")
    return ids


class TestConstructionDoesNotScan:
    def test_a_fresh_store_has_not_loaded_the_graph(self, tmp_path):
        s = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            assert s._graph_loaded is False
        finally:
            s.close()

    def test_construction_does_not_read_the_graph_tables(self, tmp_path, monkeypatch):
        """The scan is the cost being deferred, so pin it by counting it."""
        calls: list[int] = []
        original = KnowledgeStore._load_graph

        def counting(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(KnowledgeStore, "_load_graph", counting)
        s = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            assert calls == [], "construction scanned the graph tables"
        finally:
            s.close()

    def test_reopening_a_populated_store_still_does_not_scan(self, tmp_path):
        path = str(tmp_path / "k.db")
        first = KnowledgeStore(path)
        _seed(first, entities=4)
        first.close()

        second = KnowledgeStore(path)
        try:
            assert second._graph_loaded is False
        finally:
            second.close()


class TestFirstReaderMaterialises:
    def test_ensure_graph_loaded_populates_from_the_tables(self, tmp_path):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        ids = _seed(writer, entities=3)
        writer.close()

        reader = KnowledgeStore(path)
        try:
            reader.ensure_graph_loaded()
            assert reader._graph_loaded is True
            for eid in ids:
                assert reader._graph.has_node(eid)
        finally:
            reader.close()

    def test_the_property_is_a_backstop_that_loads_rather_than_returning_empty(self, tmp_path):
        """A caller nobody found must get a correct graph, not a silently empty one."""
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        ids = _seed(writer, entities=3)
        writer.close()

        reader = KnowledgeStore(path)
        try:
            assert reader._graph_loaded is False
            assert reader.graph.has_node(ids[0]), "property served an unloaded graph"
            assert reader._graph_loaded is True
        finally:
            reader.close()

    def test_a_second_call_does_not_rescan(self, tmp_path, monkeypatch):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        _seed(writer, entities=2)
        writer.close()

        reader = KnowledgeStore(path)
        try:
            reader.ensure_graph_loaded()
            calls: list[int] = []
            monkeypatch.setattr(type(reader), "_load_graph", lambda self: calls.append(1))
            reader.ensure_graph_loaded()
            reader.ensure_graph_loaded()
            assert calls == [], "steady state re-scanned"
        finally:
            reader.close()


class TestFirstTouchIsSerialised:
    def test_two_threads_racing_the_first_touch_scan_once(self, tmp_path):
        """Safe under concurrency, not safe by invariant.

        Both threads are released together and both see ``_graph_loaded`` False,
        so without the lock each would run its own full scan.
        """
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        _seed(writer, entities=3)
        writer.close()

        reader = KnowledgeStore(path)
        scans: list[int] = []
        original = type(reader)._load_graph
        barrier = threading.Barrier(2)

        def slow_load(self):
            scans.append(1)
            return original(self)

        try:
            reader._load_graph = slow_load.__get__(reader)  # type: ignore[method-assign]

            def racer():
                barrier.wait(timeout=10)
                reader.ensure_graph_loaded()

            threads = [threading.Thread(target=racer) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
                assert not t.is_alive()

            assert len(scans) == 1, f"first touch scanned {len(scans)} times, expected 1"
            assert reader._graph_loaded is True
        finally:
            reader.close()

    def test_a_refresh_marks_the_graph_loaded(self, tmp_path):
        """The six refresh call sites rebuild too, so the flag must stay truthful.

        Otherwise a later first-touch would scan a graph that is already
        materialised.
        """
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            assert store._graph_loaded is False
            store._load_graph()
            assert store._graph_loaded is True
        finally:
            store.close()


class TestRebuildsDoNotInterleave:
    """A mutation refresh must not interleave with the first load.

    The defect this pins is not a crash: two threads running ``clear()`` +
    re-add at once leave the loser's rows behind, and ``_graph_loaded`` is then
    True over a graph that is WRONG -- a flag asserting "loaded" above stale
    data, which is never rescanned because the flag says there is nothing to do.
    Holding the lock only at the first-touch call site did not prevent it,
    because the six refresh sites take no lock of their own.

    A first-load-then-read test cannot see this, which is why the rest of this
    file did not catch it.
    """

    def test_a_concurrent_refresh_cannot_interleave_with_the_first_load(self, tmp_path):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        # Chained relations, because the constructor's orphan sweep prunes an
        # entity with no mentions and no relations -- a bare add_entity set would
        # be gone by the time this store is reopened, and the load would have
        # nothing to add.
        ids = _seed(writer, entities=6)
        writer.close()
        doomed = ids[2]
        keep = [e for e in ids if e != doomed]

        store = KnowledgeStore(path)
        # Who added each node, in order. If the two rebuilds serialize, each
        # thread's adds form one contiguous run; if they interleave, the labels
        # alternate. This is a direct observation, not a timing inference.
        adds: list[str] = []
        first_add_seen = threading.Event()
        mutation_committed = threading.Event()
        original_add_node = SimpleDiGraph.add_node

        def traced_add_node(self, node_id, **attrs):
            label = threading.current_thread().name
            adds.append(label)
            if label == "loader" and not first_add_seen.is_set():
                first_add_seen.set()
                # Hold the first load open and give the mutation every chance to
                # interleave. Without serialization it will.
                mutation_committed.wait(timeout=10)
            return original_add_node(self, node_id, **attrs)

        def loader():
            store.ensure_graph_loaded()

        def mutator():
            assert first_add_seen.wait(timeout=10), "first load never started"
            store.db.execute("BEGIN IMMEDIATE")
            store.db.execute(
                "DELETE FROM entity_relations WHERE source_id = ? OR target_id = ?",
                (doomed, doomed),
            )
            store.db.execute("DELETE FROM entities WHERE id = ?", (doomed,))
            store.db.execute("COMMIT")
            mutation_committed.set()
            store._load_graph()  # the refresh path, holding no lock of its own

        try:
            SimpleDiGraph.add_node = traced_add_node  # type: ignore[method-assign]
            threads = [
                threading.Thread(target=loader, name="loader"),
                threading.Thread(target=mutator, name="mutator"),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
                assert not t.is_alive(), "a rebuild deadlocked"
        finally:
            SimpleDiGraph.add_node = original_add_node  # type: ignore[method-assign]

        # The interleave window really opened -- otherwise this passed by timing
        # luck and proves nothing about serialization.
        assert first_add_seen.is_set(), "the first load never reached a node add"
        assert mutation_committed.is_set(), "the mutation never committed"
        assert {"loader", "mutator"} <= set(
            adds
        ), f"both rebuilds must have run; saw only {sorted(set(adds))}"

        # The mutation is genuinely applied at the database, so a graph that
        # still carries the row is stale rather than merely early.
        live = {r["id"] for r in store.db.execute("SELECT id FROM entities")}
        assert doomed not in live, "the DELETE did not commit -- test proves nothing"

        # Serialization: each thread's adds form one contiguous run.
        runs = [label for i, label in enumerate(adds) if i == 0 or adds[i - 1] != label]
        assert len(runs) == len(set(runs)), (
            "rebuilds interleaved -- each rebuild must hold the lock for its whole "
            f"clear+re-add, got run order {runs}"
        )

        # And the published graph matches the database, with the flag honest.
        assert store._graph_loaded is True
        assert not store._graph.has_node(
            doomed
        ), "a deleted entity survived the interleave and _graph_loaded is True over it"
        for eid in keep:
            assert store._graph.has_node(eid)
        store.close()


class TestLoopReadersMaterialiseOffLoop:
    """The offload is what makes the deferral safe, so pin it at the handlers.

    Both handlers read ``store.graph`` on the event-loop thread, where the
    loop-stall watchdog is armed. If they stopped calling
    ``ensure_graph_loaded`` off-loop, the deferred scan would run on the loop --
    moving the stall from the pre-bind window, where nothing is armed, into the
    one where it can hard-exit the gateway.
    """

    @pytest.mark.parametrize("handler_name", ["get_entity_graph", "get_full_graph"])
    def test_the_handler_offloads_the_materialisation(self, handler_name):
        import inspect

        from kiro_crew.dashboard.handlers import knowledge as handlers

        src = inspect.getsource(getattr(handlers, handler_name))
        assert (
            "asyncio.to_thread(store.ensure_graph_loaded)" in src
        ), f"{handler_name} must materialise the graph off-loop before reading it"

    @pytest.mark.parametrize("handler_name", ["get_entity_graph", "get_full_graph"])
    def test_the_offload_precedes_every_graph_read(self, handler_name):
        import inspect

        from kiro_crew.dashboard.handlers import knowledge as handlers

        src = inspect.getsource(getattr(handlers, handler_name))
        offload = src.index("asyncio.to_thread(store.ensure_graph_loaded)")
        first_read = min(
            (
                src.index(tok)
                for tok in ("store.graph.", "store.graph,", "store.graph)")
                if tok in src
            ),
            default=None,
        )
        assert first_read is not None, "handler no longer reads store.graph"
        assert offload < first_read, "graph is read before it is materialised off-loop"

    def test_ensure_graph_loaded_is_callable_off_loop(self, tmp_path):
        """``to_thread`` runs it with no running loop; the guard must allow that."""
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        ids = _seed(writer, entities=2)
        writer.close()

        reader = KnowledgeStore(path)

        async def drive():
            await asyncio.to_thread(reader.ensure_graph_loaded)

        try:
            asyncio.run(drive())
            assert reader._graph_loaded is True
            assert reader._graph.has_node(ids[0])
        finally:
            reader.close()
