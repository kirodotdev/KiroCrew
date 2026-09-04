"""The entity graph is materialised by its first reader, not by construction.

Boot cost, not correctness, is the reason (#8329): ``_load_graph`` full-scans
``entities`` + ``entity_relations`` on the event-loop thread before the socket
binds. These tests pin the three properties that make the deferral safe rather
than merely cheaper -- construction does not scan, the first touch is
serialised, and the readers that run on the loop materialise it off-loop.
"""

import asyncio
import threading
import time

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
        # The graph must be materialised off-loop BEFORE anything reads it.
        # get_full_graph reads the graph directly (`graph = store.graph` and reads
        # through that local); get_entity_graph delegates the read to
        # store.get_entity_subgraph, which pins and reads the graph itself (#8692).
        # Either way the touch that reaches the graph must follow the offload.
        read_tokens = (
            "store.graph.",
            "store.graph,",
            "store.graph)",
            "store.graph\n",
            "store.graph ",
            "store.get_entity_subgraph(",
        )
        first_read = min(
            (src.index(tok) for tok in read_tokens if tok in src),
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


class TestRebuildIsPublishedByReferenceSwap:
    """A concurrent rebuild must never empty the graph a reader is holding.

    Serialization (``TestRebuildsDoNotInterleave``) fixed WHICH rebuild wins and
    that the flag is never True over stale data, but the rebuild still mutated
    the live object in place: ``clear()`` then row-by-row re-add. A reader that
    captured ``store.graph`` and iterated it -- or a multi-step reader that
    re-read it across degree ranking then per-node attribute reads -- could
    observe the window between ``clear()`` and the last insert and return an
    empty or truncated graph (#8692, consequence 1).

    The fix builds a fresh ``SimpleDiGraph`` and publishes it with a single
    reference assignment, so the object a reader holds is never mutated. These
    tests pin that: a held reference stays whole across a rebuild, and no reader
    ever sees a node count drop to zero mid-rebuild.
    """

    def test_a_held_reference_is_never_mutated_by_a_rebuild(self, tmp_path):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        ids = _seed(writer, entities=5)
        writer.close()

        store = KnowledgeStore(path)
        try:
            store.ensure_graph_loaded()
            # Capture the reference the way a reader does, then force a rebuild.
            pinned = store.graph
            before = {n for n in pinned.nodes}
            assert before == set(ids)

            store._load_graph()

            # The pinned object must be untouched -- a swap leaves the old object
            # alone; an in-place clear()+re-add would have mutated it.
            after = {n for n in pinned.nodes}
            assert after == before, (
                "the rebuild mutated a reference a reader was holding; "
                "it must build fresh and publish by assignment"
            )
            # And the store now serves the freshly-built graph.
            assert store.graph is not pinned
            for eid in ids:
                assert store.graph.has_node(eid)
        finally:
            store.close()

    def test_a_reader_never_observes_the_graph_emptied_mid_rebuild(self, tmp_path):
        """The direct torn-read observation: sample the held reference while a
        rebuild is paused partway through, and require it to stay whole.

        With an in-place ``clear()`` + re-add the sample lands on an empty (or
        truncated) graph. With build-fresh-then-swap the sampled reference is the
        untouched old object, so it stays complete for the whole rebuild.
        """
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        ids = _seed(writer, entities=6)
        writer.close()

        store = KnowledgeStore(path)
        original_count = len(ids)

        rebuild_reached_midpoint = threading.Event()
        sampler_has_read = threading.Event()
        original_add_node = SimpleDiGraph.add_node
        adds_during_rebuild: list[int] = []
        min_seen = [original_count]

        def traced_add_node(self, node_id, **attrs):
            adds_during_rebuild.append(1)
            # Pause once, partway through the rebuild, and let the sampler read
            # the reference it pinned before the rebuild started.
            if len(adds_during_rebuild) == 2 and not rebuild_reached_midpoint.is_set():
                rebuild_reached_midpoint.set()
                sampler_has_read.wait(timeout=10)
            return original_add_node(self, node_id, **attrs)

        try:
            store.ensure_graph_loaded()
            pinned = store.graph  # what a reader holds for the duration of a read

            def rebuilder():
                store._load_graph()

            def sampler():
                assert rebuild_reached_midpoint.wait(timeout=10), "rebuild never started adding"
                # Read the pinned reference while the rebuild is paused midway.
                min_seen[0] = min(min_seen[0], len({n for n in pinned.nodes}))
                sampler_has_read.set()

            SimpleDiGraph.add_node = traced_add_node  # type: ignore[method-assign]
            threads = [
                threading.Thread(target=rebuilder, name="rebuilder"),
                threading.Thread(target=sampler, name="sampler"),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
                assert not t.is_alive(), "a thread deadlocked"
        finally:
            SimpleDiGraph.add_node = original_add_node  # type: ignore[method-assign]
            store.close()

        # The window really opened: the rebuild paused partway and the sampler
        # read during that pause.
        assert rebuild_reached_midpoint.is_set(), "the rebuild never paused midway"
        assert sampler_has_read.is_set(), "the sampler never read"
        # The held reference stayed whole throughout -- it was never cleared.
        assert min_seen[0] == original_count, (
            f"a reader saw {min_seen[0]} of {original_count} nodes mid-rebuild; "
            "the held reference was emptied by an in-place clear()"
        )


class TestConcurrentIncrementalAddSurvivesARebuild:
    """A committed incremental add must not be discarded by a concurrent swap.

    ``add_entity`` / ``add_entity_relation`` write the row, commit, and apply an
    incremental ``add_node`` / ``add_edge`` to the live graph. Publishing a
    rebuild by swapping ``self._graph`` (#8692) means that in-memory add would be
    LOST if it landed on the old object a rebuild was about to discard: the row is
    in the database, but the in-memory graph misses it until the next full
    rebuild, which in steady-state ingestion may never come. Both the add and the
    rebuild take ``_graph_lock``, so the add lands on whichever graph is currently
    published; this test forces the interleave and asserts the entity survives.
    """

    def test_an_add_entity_racing_a_rebuild_is_not_dropped(self, tmp_path):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        _seed(writer, entities=3)
        writer.close()

        store = KnowledgeStore(path)
        # Force the rebuild to pause mid-scan so the add is guaranteed to
        # interleave. The add takes ``_graph_lock``, which the paused rebuild
        # holds, so it will block until the rebuild finishes and then apply to the
        # swapped-in graph -- exactly the serialization being pinned.
        rebuild_holding_lock = threading.Event()
        adder_started = threading.Event()
        original_add_node = SimpleDiGraph.add_node
        new_id = [""]

        def traced_add_node(self, node_id, **attrs):
            if not rebuild_holding_lock.is_set() and threading.current_thread().name == "rebuilder":
                rebuild_holding_lock.set()
                # Hold the lock open only until the adder is running and about to
                # contend for it. Do NOT wait on the add COMPLETING -- the add
                # blocks on this very lock, so that would deadlock. A bounded wait
                # plus a short settle gives the adder time to commit and reach the
                # lock; the serialization we pin then does the rest.
                adder_started.wait(timeout=10)
                time.sleep(0.2)
            return original_add_node(self, node_id, **attrs)

        try:
            store.ensure_graph_loaded()

            def rebuilder():
                store._load_graph()

            def adder():
                assert rebuild_holding_lock.wait(timeout=10), "rebuild never started"
                adder_started.set()
                new_id[0] = store.add_entity("LateComer", "concept")

            SimpleDiGraph.add_node = traced_add_node  # type: ignore[method-assign]
            threads = [
                threading.Thread(target=rebuilder, name="rebuilder"),
                threading.Thread(target=adder, name="adder"),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
                assert not t.is_alive(), "a thread deadlocked"
        finally:
            SimpleDiGraph.add_node = original_add_node  # type: ignore[method-assign]

        try:
            assert rebuild_holding_lock.is_set(), "the rebuild never ran"
            assert adder_started.is_set(), "the add never started"
            # The committed entity is present in the published in-memory graph --
            # not merely on disk -- so it was not orphaned onto a discarded object.
            assert new_id[0]
            assert store.graph.has_node(
                new_id[0]
            ), "a committed incremental add was discarded by the rebuild swap"
            db_ids = {r["id"] for r in store.db.execute("SELECT id FROM entities")}
            assert new_id[0] in db_ids, "the add did not commit -- test proves nothing"
        finally:
            store.close()


class TestEntitySubgraphSharesOnePinnedSnapshot:
    """The 404 decision and the walk read one internally-pinned snapshot.

    ``get_entity_subgraph`` captures ``self.graph`` ONCE, does the existence check
    against that reference, and walks the same reference; it returns ``None`` when
    the entity is absent, which ``get_entity_graph`` maps to 404. Because the check
    and the walk share the one captured reference, a rebuild swapping in a fresh
    graph between them cannot let an entity pass the check and then be walked on a
    different graph (#8692).
    """

    def test_absent_entity_returns_none(self, tmp_path):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        _seed(writer, entities=2)
        writer.close()

        store = KnowledgeStore(path)
        try:
            store.ensure_graph_loaded()
            assert store.get_entity_subgraph("does-not-exist", depth=2) is None
        finally:
            store.close()

    def test_present_entity_yields_a_subgraph(self, tmp_path):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        ids = _seed(writer, entities=3)
        writer.close()

        store = KnowledgeStore(path)
        try:
            store.ensure_graph_loaded()
            sg = store.get_entity_subgraph(ids[0], depth=2)
            assert sg is not None
            assert ids[0] in {n["id"] for n in sg["nodes"]}
        finally:
            store.close()


class TestARebuildCannotRestoreAConcurrentlyDeletedWrite:
    """The in-memory graph must agree with the committed rows after an add races
    a rebuild that removes the same edge (#8692, GPT F1: delete-then-restore).

    ``add_entity_relation`` commits its row and applies the in-memory ``add_edge``
    as ONE ``_graph_lock`` critical section. A delete path (``delete_source_cascade``
    etc.) removes rows and then rebuilds via ``_load_graph``, which takes the same
    lock across its whole rebuild-and-swap. Because both are serialized on that
    lock, a rebuild can never land BETWEEN the add's commit and its add_edge -- so
    the add can never re-inject an edge whose row the delete just removed. The
    invariant pinned here: whatever the interleave, the published graph's edges
    equal the committed ``entity_relations`` rows, never a phantom.
    """

    def _committed_edges(self, store):
        return {
            (r["source_id"], r["target_id"])
            for r in store.db.execute("SELECT source_id, target_id FROM entity_relations")
        }

    def _graph_edges(self, store):
        return {(u, v) for u, v, _ in store.graph.edges(data=True)}

    def test_add_edge_racing_a_rebuild_that_deletes_it_leaves_no_phantom(self, tmp_path):
        path = str(tmp_path / "k.db")
        writer = KnowledgeStore(path)
        ids = _seed(writer, entities=3)
        writer.close()

        store = KnowledgeStore(path)
        # A adds a fresh edge between two existing entities; B deletes that very
        # edge's row and rebuilds. Force B to pause while holding the lock so A's
        # commit+add is guaranteed to contend for it -- the atomic section is what
        # keeps A's add from landing after B's swap.
        src, tgt = ids[0], ids[2]
        rebuild_holding_lock = threading.Event()
        adder_started = threading.Event()
        original_add_node = SimpleDiGraph.add_node

        def traced_add_node(self, node_id, **attrs):
            if not rebuild_holding_lock.is_set() and threading.current_thread().name == "rebuilder":
                rebuild_holding_lock.set()
                adder_started.wait(timeout=10)
                time.sleep(0.2)
            return original_add_node(self, node_id, **attrs)

        try:
            store.ensure_graph_loaded()

            def rebuilder():
                # Delete any edge between src and tgt at the DB, then rebuild --
                # standing in for the delete-cascade's "remove rows then _load_graph".
                store.db.execute(
                    "DELETE FROM entity_relations WHERE source_id = ? AND target_id = ?",
                    (src, tgt),
                )
                store.db.commit()
                store._load_graph()

            def adder():
                assert rebuild_holding_lock.wait(timeout=10), "rebuild never started"
                adder_started.set()
                store.add_entity_relation(src, tgt, "calls")

            SimpleDiGraph.add_node = traced_add_node  # type: ignore[method-assign]
            threads = [
                threading.Thread(target=rebuilder, name="rebuilder"),
                threading.Thread(target=adder, name="adder"),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
                assert not t.is_alive(), "a thread deadlocked"
        finally:
            SimpleDiGraph.add_node = original_add_node  # type: ignore[method-assign]

        try:
            assert rebuild_holding_lock.is_set(), "the rebuild never ran"
            assert adder_started.is_set(), "the add never started"
            # The core invariant: no edge is present in memory that the committed
            # rows do not have. A phantom (graph has (src,tgt), DB does not) is
            # exactly the delete-then-restore defect.
            graph_edges = self._graph_edges(store)
            db_edges = self._committed_edges(store)
            assert graph_edges == db_edges, (
                f"in-memory graph disagrees with committed rows: "
                f"phantom={graph_edges - db_edges}, missing={db_edges - graph_edges}"
            )
        finally:
            store.close()
