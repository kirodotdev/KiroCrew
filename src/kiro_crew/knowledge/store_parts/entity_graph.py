"""The entity graph: entity, relation and mention rows, and the graph held in memory.

``SimpleDiGraph`` is the small directed graph the store keeps in memory, with its own
lock so a worker thread can walk it while the loop thread changes it. The methods
load it on first read, rebuild it from the tables and publish it with one reference
swap, write entities, relations and mentions, drop entities nothing references, and
walk a node's neighbourhood.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: ``_graph``, ``_graph_loaded``.
Reads: ``_graph_lock``.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore


class _NodeView:
    """Minimal node-attribute view supporting get, subscript, iteration, and len."""

    def __init__(self, data: dict[str, dict], lock: "threading.RLock | None" = None):
        self._data = data
        self._lock = lock

    def get(self, nid: str, default: dict | None = None) -> dict:
        return self._data.get(nid, default if default is not None else {})

    def __getitem__(self, nid: str) -> dict:
        return self._data[nid]

    def __contains__(self, nid: str) -> bool:
        return nid in self._data

    def __iter__(self):
        # Snapshot under the lock: a concurrent add_node/clear on the loop
        # thread must not mutate the dict while an mc-embed reader iterates it
        # ("dictionary changed size during iteration").
        if self._lock is not None:
            with self._lock:
                return iter(list(self._data))
        return iter(list(self._data))

    def __len__(self) -> int:
        return len(self._data)


class _EdgeView:
    """Minimal edge view supporting iteration and subscript access."""

    def __init__(self, fwd: dict[str, dict[str, dict]], lock: "threading.RLock | None" = None):
        self._fwd = fwd
        self._lock = lock

    def __getitem__(self, key: tuple[str, str]) -> dict:
        u, v = key
        return self._fwd[u][v]

    def __call__(self, *, data: bool = False):  # noqa: ARG002
        # Materialize a snapshot under the lock before yielding — see _NodeView.
        if self._lock is not None:
            with self._lock:
                snapshot = [
                    (u, v, attrs)
                    for u, targets in self._fwd.items()
                    for v, attrs in targets.items()
                ]
        else:
            snapshot = [
                (u, v, attrs) for u, targets in self._fwd.items() for v, attrs in targets.items()
            ]
        yield from snapshot


class SimpleDiGraph:
    """Minimal directed graph replacing networkx.DiGraph for the subset of API we use."""

    def __init__(self) -> None:
        # Guards all reads/writes of the three dicts. KnowledgeStore mutates the
        # graph inline on the event-loop thread (ingest _store_entities,
        # dedup -> _load_graph clear()+rebuild), while HybridRetriever.search()
        # traverses it on an mc-embed executor thread (get_neighbors ->
        # successors/predecessors/nodes). Without this, concurrent iterate +
        # clear/insert on the same dict raises "dictionary changed size during
        # iteration" (HTTP 500) or returns a half-rebuilt graph. Re-entrant
        # because a single logical op (e.g. get_neighbors) takes it repeatedly.
        self._lock = threading.RLock()
        self._node_attrs: dict[str, dict] = {}
        self._fwd: dict[str, dict[str, dict]] = defaultdict(dict)
        self._rev: dict[str, dict[str, dict]] = defaultdict(dict)
        self.nodes = _NodeView(self._node_attrs, self._lock)
        self.edges = _EdgeView(self._fwd, self._lock)

    def clear(self) -> None:
        with self._lock:
            self._node_attrs.clear()
            self._fwd.clear()
            self._rev.clear()

    def add_node(self, nid: str, **attrs: object) -> None:
        with self._lock:
            self._node_attrs[nid] = attrs

    def add_edge(self, u: str, v: str, **attrs: object) -> None:
        with self._lock:
            self._fwd[u][v] = attrs
            self._rev[v][u] = attrs

    def has_edge(self, u: str, v: str) -> bool:
        with self._lock:
            return v in self._fwd.get(u, {})

    def has_node(self, nid: str) -> bool:
        with self._lock:
            return nid in self._node_attrs

    def degree(self, nid: str) -> int:
        with self._lock:
            return len(self._fwd.get(nid, {})) + len(self._rev.get(nid, {}))

    def successors(self, nid: str):
        # Snapshot the neighbor keys under the lock so the caller can iterate
        # freely while the loop thread mutates the graph (see __init__).
        with self._lock:
            return iter(list(self._fwd.get(nid, {})))

    def predecessors(self, nid: str):
        with self._lock:
            return iter(list(self._rev.get(nid, {})))


def _prune_orphan_entities(self: KnowledgeStore) -> None:
    """Delete entities nothing references any more -- no mention, no relation.

    Every path that removes items or a source has to run this, because an
    entity is only reachable through the rows those paths delete. It takes no
    transaction of its own -- each call site is already inside an open write
    transaction -- and it does not reload the in-memory graph, which the sweep
    can leave holding dropped entities; that stays with whoever owns the
    transaction.
    """
    self.db.execute("""
            DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)
            AND id NOT IN (SELECT source_id FROM entity_relations)
            AND id NOT IN (SELECT target_id FROM entity_relations)
        """)


def ensure_graph_loaded(self: KnowledgeStore) -> None:
    """Materialise the entity graph if no reader has done so yet.

    Called by each graph reader before it touches :attr:`graph` --
    ``get_entity_graph`` and ``get_full_graph`` in the dashboard handlers --
    from a worker thread via ``asyncio.to_thread``. Deliberately NOT called
    from ``__init__``, for the reason ``ensure_fts_index_current`` gives
    about itself: the constructor runs on the gateway's boot path and this
    work is proportional to ``entities`` + ``entity_relations``, so doing it
    there stalls the gateway before the socket binds.

    **The offload is load-bearing, not hygiene.** Both handlers are
    ``async def`` and read the graph on the loop, where the loop-stall
    watchdog IS armed (it is started after the bind). Reaching this lazily
    from the loop would move a data-scaled read out of the pre-bind window,
    where nothing is armed and nothing is served, into the one window where
    a stall can hard-exit the gateway. ``allow_on_loop()`` is not an option
    here either -- its own contract restricts it to constructor-shaped setup
    paths and directs production code to offload.

    Steady state is a single boolean check. The first caller takes the lock
    and does the work; concurrent readers wait rather than each starting
    their own scan. The lock is re-entrant and :meth:`_load_graph` takes it
    again, so a mutation refresh cannot interleave with this load -- see that
    method for why serializing every rebuild is the property that matters.
    """
    if self._graph_loaded:
        return
    with self._graph_lock:
        if self._graph_loaded:
            return
        self._load_graph()


def _load_graph(self: KnowledgeStore):
    """Rebuild the in-memory graph from the tables, atomically.

    Takes ``_graph_lock`` around the WHOLE rebuild, not just the first one.
    Holding it only at the first-touch call site was not enough: the six
    mutation-refresh sites acquire no lock of their own, so a first graph GET
    racing a source DELETE put two threads through the rebuild at once, and
    the loser's rows survived into a graph whose ``_graph_loaded`` was then
    set True -- a flag asserting "loaded" over data that is wrong, which is
    worse than an unloaded graph because it never gets rescanned.

    Serializing the whole rebuild also fixes WHICH snapshot wins: the SELECTs
    below run after acquisition, so the rebuild that acquires last reads the
    freshest committed state rather than replaying rows it captured earlier.

    **Build a fresh graph, then publish it with one reference assignment.**
    Clearing the live ``self._graph`` and re-adding row
    by row would be stale-publish-safe under serialization but leave the object a
    reader could be iterating momentarily empty: a reader holding
    ``self._graph`` between the ``clear()`` and the last insert would see a torn
    (empty or truncated) graph, and a multi-step reader that re-read
    ``self.graph`` across its own steps -- degree ranking, then per-node
    attribute reads -- could miss a node that ``clear()`` had just removed.
    Building into a NEW ``SimpleDiGraph`` and swapping the reference
    under the lock closes that window: the old object is never mutated, so a
    reader holding it sees a complete, consistent OLD graph until it drops the
    reference, and the next read sees the complete NEW one. The multi-step
    readers pin one reference for the duration of their read (see
    ``get_entity_subgraph`` / ``get_neighbors`` and the graph handlers) so a
    swap mid-read cannot mix old and new nodes.

    The lock is only ever taken here and in :meth:`ensure_graph_loaded`, and
    this method never takes SQLite's writer lock -- it is read-only, and all
    six refresh sites call it after their own COMMIT -- so there is no
    ordering against ``BEGIN IMMEDIATE`` to invert. (That is the hazard the
    ``_fts_lock`` comment warns about, and it does not apply here: the FTS
    rebuild acquires its lock and THEN a writer lock.)
    """
    with self._graph_lock:
        rebuilt = SimpleDiGraph()
        for row in self.db.execute("SELECT id, name, entity_type FROM entities"):
            rebuilt.add_node(row["id"], name=row["name"], entity_type=row["entity_type"])
        for row in self.db.execute(
            "SELECT id, source_id, target_id, relation_type, weight FROM entity_relations"
        ):
            rebuilt.add_edge(
                row["source_id"],
                row["target_id"],
                id=row["id"],
                relation_type=row["relation_type"],
                weight=row["weight"],
            )
        # Single-reference publish. A reader that captured the previous
        # ``self._graph`` keeps iterating that complete object; readers after
        # this point see ``rebuilt``. Neither ever observes a half-built graph.
        self._graph = rebuilt
        # Truthful bookkeeping for the refresh call sites too: after any
        # rebuild the graph IS materialised, so a later first-touch must not
        # scan again. Set inside the lock, so no reader can observe the flag
        # True over a half-rebuilt graph.
        self._graph_loaded = True


def reload_graph(self: KnowledgeStore) -> None:
    """Rebuild the in-memory entity graph from the tables.

    For a caller that ran :meth:`delete_items_batch_in_txn` and therefore owes
    the reload that :meth:`delete_items_batch` would have done for it.
    """
    self._load_graph()


def add_entity(self: KnowledgeStore, name, entity_type, description=None, aliases=None) -> str:
    eid = str(uuid4())
    now = datetime.now().isoformat()
    self.db.execute(
        "INSERT INTO entities (id, name, entity_type, description, aliases, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (eid, name, entity_type, description, json.dumps(aliases or []), now, now),
    )
    # Hold ``_graph_lock`` across BOTH the commit and the in-memory add, as one
    # critical section. ``_load_graph`` -- which every delete / merge /
    # import path runs after its own COMMIT -- takes this same lock for its whole
    # rebuild-and-swap, so serializing commit+add here means a concurrent rebuild
    # can never land BETWEEN this commit and this add. Without that, a source
    # deletion that removes this entity's rows could rebuild and swap in the
    # window, and this late add would re-inject the deleted entity into the
    # published graph when SQLite has already dropped it. Whichever of the two paths
    # acquires last leaves the in-memory graph agreeing with the committed rows.
    with self._graph_lock:
        self.db.commit()
        self._graph.add_node(eid, name=name, entity_type=entity_type)
    return eid


def find_entity(self: KnowledgeStore, name):
    row = self.db.execute("SELECT * FROM entities WHERE name = ?", (name,)).fetchone()
    if row:
        return dict(row)
    row = self.db.execute("SELECT * FROM entities WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
    if row:
        return dict(row)
    # Only rows that can carry an alias. '' and '[]' (the column default)
    # parse to no aliases, so filtering them in SQL returns the same row
    # while sparing a json.loads per entity: on a 59k-entity library that
    # scan was ~half of every knowledge search, and it holds the GIL.
    for row in self.db.execute(
        "SELECT * FROM entities WHERE aliases IS NOT NULL AND aliases NOT IN ('', '[]')"
    ):
        aliases = json.loads(row["aliases"]) if row["aliases"] else []
        if any(a.lower() == name.lower() for a in aliases):
            return dict(row)
    return None


def merge_entities(self: KnowledgeStore, keep_id, merge_id):
    self.db.execute(
        "UPDATE entity_relations SET source_id = ? WHERE source_id = ?", (keep_id, merge_id)
    )
    self.db.execute(
        "UPDATE entity_relations SET target_id = ? WHERE target_id = ?", (keep_id, merge_id)
    )
    # Remove self-loops created by the merge
    self.db.execute(
        "DELETE FROM entity_relations WHERE source_id = ? AND target_id = ?", (keep_id, keep_id)
    )
    # Delete mentions that would conflict, then update the rest
    self.db.execute(
        "DELETE FROM mentions WHERE entity_id = ? AND item_id IN (SELECT item_id FROM mentions WHERE entity_id = ?)",
        (merge_id, keep_id),
    )
    self.db.execute("UPDATE mentions SET entity_id = ? WHERE entity_id = ?", (keep_id, merge_id))
    self.db.execute("DELETE FROM entities WHERE id = ?", (merge_id,))
    self.db.commit()
    self._load_graph()


def add_entity_relation(
    self: KnowledgeStore,
    source_id,
    target_id,
    relation_type,
    description=None,
    weight=1.0,
    source_item_id=None,
) -> str:
    rid = str(uuid4())
    now = datetime.now().isoformat()
    self.db.execute(
        "INSERT INTO entity_relations (id, source_id, target_id, relation_type, description, weight, source_item_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, source_id, target_id, relation_type, description, weight, source_item_id, now),
    )
    # Hold ``_graph_lock`` across commit + add, one critical section -- see
    # add_entity. This closes the delete-then-restore race: a source deletion
    # whose rebuild+swap would otherwise land between this commit and this add
    # cannot interleave, so this edge is never re-injected after its row is gone.
    with self._graph_lock:
        self.db.commit()
        self._graph.add_edge(
            source_id, target_id, id=rid, relation_type=relation_type, weight=weight
        )
    return rid


def add_mention(self: KnowledgeStore, item_id, entity_id, context=None):
    now = datetime.now().isoformat()
    self.db.execute(
        "INSERT OR IGNORE INTO mentions (item_id, entity_id, context, created_at) VALUES (?, ?, ?, ?)",
        (item_id, entity_id, context, now),
    )
    self.db.commit()


def get_neighbors(self: KnowledgeStore, entity_id, depth=1) -> list:
    # Pin one graph reference for the whole traversal. ``_load_graph``
    # publishes a rebuilt graph by swapping ``self._graph``, so
    # re-reading ``self.graph`` at each step could mix an old and a new graph
    # across the successor/predecessor walk and the per-node attribute reads.
    # Capturing it once means this read sees a single consistent snapshot.
    graph = self.graph
    visited = set()
    frontier = {entity_id}
    for _ in range(depth):
        next_frontier = set()
        for nid in frontier:
            for neighbor in graph.successors(nid):
                if neighbor not in visited and neighbor != entity_id:
                    next_frontier.add(neighbor)
            for neighbor in graph.predecessors(nid):
                if neighbor not in visited and neighbor != entity_id:
                    next_frontier.add(neighbor)
        visited |= frontier
        frontier = next_frontier
    visited |= frontier
    visited.discard(entity_id)
    result = []
    for nid in visited:
        data = graph.nodes.get(nid, {})
        result.append({"id": nid, "name": data.get("name"), "entity_type": data.get("entity_type")})
    return result


def get_entity_subgraph(self: KnowledgeStore, entity_id, depth=2) -> dict | None:
    """The D3-shaped subgraph around ``entity_id``, or ``None`` if absent.

    Pins ONE graph reference for the whole read and does the existence check
    against it, so the check and the traversal see the same snapshot -- a
    rebuild swapping in a fresh graph between them cannot let an entity pass
    the check on the old graph and be walked on the new one, returning a
    degenerate ``name: None`` subgraph instead of ``None``. The
    ``get_entity_graph`` handler relies on this ``None`` to answer 404.
    """
    graph = self.graph
    if not graph.has_node(entity_id):
        return None
    visited = set()
    frontier = {entity_id}
    for _ in range(depth):
        next_frontier = set()
        for nid in frontier:
            for neighbor in graph.successors(nid):
                next_frontier.add(neighbor)
            for neighbor in graph.predecessors(nid):
                next_frontier.add(neighbor)
        visited |= frontier
        frontier = next_frontier - visited
    visited |= frontier
    nodes = []
    for nid in visited:
        data = graph.nodes.get(nid, {})
        nodes.append({"id": nid, "name": data.get("name"), "type": data.get("entity_type")})
    edges = []
    for u, v, data in graph.edges(data=True):
        if u in visited and v in visited:
            edges.append(
                {
                    "source": u,
                    "target": v,
                    "type": data.get("relation_type"),
                    "weight": data.get("weight"),
                }
            )
    return {"nodes": nodes, "edges": edges}
