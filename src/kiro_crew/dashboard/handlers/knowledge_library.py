"""Browsing and editing the library: namespaces, items, entities and the graph.

The source-first list view's routes (``/api/knowledge/items`` paged within a
source, ``/source-counts``), one item's read, edit, delete and content, the entity
and graph reads, and the library statistics.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial

from aiohttp import web

from kiro_crew._sqlite_compat import fts5_segment_for_index
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.knowledge_access import (
    _audited_write,
    _require_knowledge_owner,
    _sel_log,
    _store,
)
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.knowledge.retrieval import HybridRetriever, vector_leg
from kiro_crew.validation import clamp_query_offset

# ---------- Namespaces ----------


def _namespace_rows(store) -> list:
    """Namespace item counts, in one off-loop take.

    Sync on purpose: this is a full GROUP BY over ``items``, which grows without
    bound, and the Library polls it. The caller dispatches this to a worker
    thread; ``store.db`` is thread-local, so the thread gets its own connection.
    """
    return store.db.execute(
        "SELECT namespace, COUNT(*) as count FROM items WHERE status = 'active' GROUP BY namespace ORDER BY count DESC"
    ).fetchall()


async def list_namespaces(request: web.Request) -> web.Response:
    """GET /api/knowledge/namespaces -- all namespaces with item counts."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.namespaces.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    rows = await asyncio.to_thread(_namespace_rows, store)
    return web.json_response(
        [{"name": r["namespace"] or "default", "count": r["count"]} for r in rows]
    )


# ---------- Items ----------


def _attach_file_paths(store, items: list[dict]) -> None:
    """Attach _file_path to items for sub-grouping in the Sources UI.

    Folder/vault sources group by file path (from folder_file_state); the
    aggregate ``artifact`` source groups per-artifact, labelled with the
    artifact name (from artifact_item_state, falling back to the slug)."""
    source_ids = {i["source_id"] for i in items if i.get("source_id")}
    if not source_ids:
        return
    ph = ",".join("?" * len(source_ids))
    folder_sids = {
        r["id"]
        for r in store.db.execute(
            f"SELECT id FROM sources WHERE id IN ({ph}) AND source_type IN ('local_folder', 'obsidian_vault')",  # noqa: S608
            list(source_ids),
        ).fetchall()
    }
    artifact_sids = {
        r["id"]
        for r in store.db.execute(
            f"SELECT id FROM sources WHERE id IN ({ph}) AND source_type = 'artifact'",  # noqa: S608
            list(source_ids),
        ).fetchall()
    }
    if not folder_sids and not artifact_sids:
        return
    # Build item_id -> group-label reverse map.
    item_to_file: dict[str, str] = {}
    # Folder/vault sources: group label is the file path.
    for sid in folder_sids:
        for row in store.db.execute(
            "SELECT file_path, item_ids FROM folder_file_state WHERE source_id = ?", (sid,)
        ):
            try:
                ids = json.loads(row["item_ids"]) if row["item_ids"] else []
            except (json.JSONDecodeError, TypeError):
                continue
            for item_id in ids:
                item_to_file[item_id] = row["file_path"]
    # Aggregate artifact source: group label is the artifact name (fallback slug).
    for sid in artifact_sids:
        for row in store.db.execute(
            "SELECT slug, name, item_ids FROM artifact_item_state WHERE source_id = ?", (sid,)
        ):
            try:
                ids = json.loads(row["item_ids"]) if row["item_ids"] else []
            except (json.JSONDecodeError, TypeError):
                continue
            label = row["name"] or row["slug"]
            for item_id in ids:
                item_to_file[item_id] = label
    # Attach to items
    for item in items:
        fp = item_to_file.get(item["id"])
        if fp:
            item["_file_path"] = fp


_NO_SOURCE = "__none__"

# Candidate-pool escalation for a source-scoped hybrid search: start here, then
# double until retrieval is exhausted. Capped so a pathological corpus cannot
# turn one request into an unbounded scan.
# Ids bound per `IN (...)` statement. Comfortably under the 999-variable floor
# of older SQLite builds (SQLITE_MAX_VARIABLE_NUMBER).
_SQLITE_VARIABLE_CHUNK = 500

_SCOPED_SEARCH_START = 200
_SCOPED_SEARCH_MAX = 20000


async def _search_until_exhausted(retriever, q: str, limit: int) -> list[dict]:
    """Retrieve hybrid-search candidates until the retriever runs out.

    A source scope is applied *after* ranking, so a fixed window can hide every
    matching item behind higher-ranked hits from other sources. Growing the
    window until the retriever returns fewer rows than requested means the
    caller has seen the whole ranking, so its filtered count is the true total.
    """
    want = max(limit * 3, _SCOPED_SEARCH_START)
    results: list[dict] = []
    while True:
        results = await run_in_embed_pool(retriever.search, q, limit=want)
        # Short read means the ranking is exhausted; nothing further to fetch.
        if len(results) < want or want >= _SCOPED_SEARCH_MAX:
            return results
        want = min(want * 2, _SCOPED_SEARCH_MAX)


def _matches_source(item: dict, source_id: str) -> bool:
    """True when `item` belongs to `source_id`. The `__none__` sentinel matches
    items with no source (NULL or empty string), mirroring how the list view
    groups sourceless items into a single 'No source' bucket."""
    own = item.get("source_id")
    if source_id == _NO_SOURCE:
        return not own
    return own == source_id


def _load_items_by_id(store, item_ids: list[str]) -> dict[str, dict]:
    """Batch-load and serialize items by id, keyed by id.

    Chunked because a source-scoped hybrid search escalates its candidate pool:
    binding 20k ids into a single `IN (...)` exceeds SQLITE_MAX_VARIABLE_NUMBER
    (999 on older builds) and fails the request with "too many SQL variables".

    Runs off the event loop: both the SELECT and the per-row serialization can be
    large enough to stall the gateway if run inline.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(item_ids), _SQLITE_VARIABLE_CHUNK):
        chunk = item_ids[start : start + _SQLITE_VARIABLE_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = store.db.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders})",  # noqa: S608
            chunk,
        ).fetchall()
        for row in rows:
            out[row["id"]] = store._serialize_item(row)
    return out


def _items_page(
    store, where_clause: str, params: list, limit: int, offset: int
) -> tuple[int, list[dict]]:
    """One page of the item listing, with its total, in one off-loop take.

    Sync on purpose: the COUNT is a full scan over ``items``, which grows
    without bound, and ``_attach_file_paths`` adds two more queries plus the row
    serialization. The total and the page are separate reads on an autocommit
    connection, so a concurrent insert can make the total disagree with the rows
    by one page-worth; one take removes the suspensions between them, not the
    disagreement. The caller dispatches this to a worker thread; ``store.db``
    is thread-local, so the thread gets its own connection.
    """
    total = store.db.execute(
        f"SELECT COUNT(*) FROM items i WHERE {where_clause}", params  # noqa: S608
    ).fetchone()[0]
    rows = store.db.execute(
        f"SELECT i.* FROM items i LEFT JOIN sources s ON i.source_id = s.id WHERE {where_clause} ORDER BY s.updated_at DESC, i.chunk_index ASC LIMIT ? OFFSET ?",  # noqa: S608, E501
        [*params, limit, offset],
    ).fetchall()
    items = [store._serialize_item(r) for r in rows]
    _attach_file_paths(store, items)
    return total, items


async def list_items(request: web.Request) -> web.Response:
    """GET /api/knowledge/items -- list/search with pagination."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.items.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    q = request.query.get("q")
    item_type = request.query.get("type")
    status = request.query.get("status")
    namespace = request.query.get("namespace")
    # Scope the page to a single source. The list view pages *within* a source
    # group, so its pager math must see that source's total, not the global one.
    # The sentinel "__none__" selects items with no source at all.
    source_id = request.query.get("source_id")
    try:
        page = max(1, int(request.query.get("page", 1)))
        limit = min(100, max(1, int(request.query.get("limit", 20))))
    except ValueError:
        return web.json_response({"error": "invalid page/limit"}, status=400)

    if q:
        # Use hybrid search: FTS5 keyword + graph traversal + optional vector + RRF fusion.
        # The availability probe and retriever.search (its blocking query embed)
        # both occupy the shared in-process model — run off-loop, mirroring
        # search_for_context in ``knowledge_agent_context``.
        embedder = request.app.get("knowledge_embedder")
        available = bool(embedder) and await embedder.is_available_async()
        embed_fn, embed_sig = vector_leg(embedder if available else None)
        retriever = HybridRetriever(store, embedder=embed_fn, embed_sig=embed_sig)
        # mc-embed bulkhead: the search's query embed blocks on the shared model.
        # The retriever ranks globally, so post-retrieval filtering can discard
        # an unbounded share of any fixed window: if enough higher-ranked hits
        # belong to other sources, a scoped search would report zero matches and
        # later pages could never reach the real ones. Under a source scope,
        # escalate the candidate pool until retrieval is exhausted (it returns
        # fewer rows than asked for), which makes the scoped total exact.
        # Unscoped searches keep the cheap limit * 3 window.
        if source_id:
            all_results = await _search_until_exhausted(retriever, q, limit)
        else:
            all_results = await run_in_embed_pool(retriever.search, q, limit=limit * 3)
        # Batch fetch all candidate items (avoid N+1). A scoped search escalates
        # its candidate pool, so this query and the row serialization can both be
        # large: run them in a worker thread rather than on the event loop.
        # `store.db` is a thread-local property, so the thread gets its own
        # connection.
        result_ids = [r["id"] for r in all_results]
        items_by_id = await asyncio.to_thread(_load_items_by_id, store, result_ids)
        filtered = []
        for r in all_results:
            item = items_by_id.get(r["id"])
            if not item:
                continue
            if status and item.get("status") != status:
                continue
            if item_type and item.get("item_type") != item_type:
                continue
            if namespace and item.get("namespace") != namespace:
                continue
            if source_id and not _matches_source(item, source_id):
                continue
            item["_score"] = r["score"]
            item["_match_type"] = r["match_type"]
            filtered.append(item)
        total = len(filtered)
        offset = (page - 1) * limit
        items = filtered[offset : offset + limit]
        await asyncio.to_thread(_attach_file_paths, store, items)
        return web.json_response({"items": items, "total": total, "page": page, "limit": limit})
    else:
        where, params = ["1=1"], []  # type: list[str], list[object]
        if item_type:
            where.append("i.item_type = ?")
            params.append(item_type)
        if status:
            where.append("i.status = ?")
            params.append(status)
        if namespace:
            where.append("i.namespace = ?")
            params.append(namespace)
        if source_id == _NO_SOURCE:
            where.append("(i.source_id IS NULL OR i.source_id = '')")
        elif source_id:
            where.append("i.source_id = ?")
            params.append(source_id)
        where_clause = " AND ".join(where)
        # The derived offset is bound as a SQLite parameter below, so it takes
        # the same ceiling every offset-binding route applies; the search branch
        # above slices a Python list and needs none.
        offset = clamp_query_offset((page - 1) * limit)
        total, items = await asyncio.to_thread(
            _items_page, store, where_clause, params, limit, offset
        )
        return web.json_response({"items": items, "total": total, "page": page, "limit": limit})


def _item_detail(store, item_id: str) -> dict | None:
    """One item with its entities, relations and locations, in one off-loop take.

    ``None`` when the item is absent, which is how the handler answers 404.

    Sync on purpose: the queries fan out per mentioned entity -- one lookup each,
    plus two name lookups per relation -- so a contended connection multiplies
    its busy wait by the item's entity count. The caller dispatches this to a
    worker thread; ``store.db`` is thread-local, so the thread gets its own
    connection.
    """
    item = store.get_item(item_id)
    if not item:
        return None

    mentions = store.db.execute(
        "SELECT entity_id, context FROM mentions WHERE item_id = ?", (item_id,)
    ).fetchall()
    entity_ids = [m["entity_id"] for m in mentions]
    entities = []
    for eid in entity_ids:
        row = store.db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        if row:
            entities.append(dict(row))

    relations = []
    seen_ids = set()
    for eid in entity_ids:
        for row in store.db.execute(
            "SELECT * FROM entity_relations WHERE source_id = ? OR target_id = ?", (eid, eid)
        ):
            r = dict(row)
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                # Resolve entity names for display
                src = store.db.execute(
                    "SELECT name FROM entities WHERE id = ?", (r["source_id"],)
                ).fetchone()
                tgt = store.db.execute(
                    "SELECT name FROM entities WHERE id = ?", (r["target_id"],)
                ).fetchone()
                r["source_name"] = src["name"] if src else r["source_id"]
                r["target_name"] = tgt["name"] if tgt else r["target_id"]
                relations.append(r)

    locations = [
        dict(r)
        for r in store.db.execute("SELECT * FROM source_locations WHERE item_id = ?", (item_id,))
    ]

    return {**item, "entities": entities, "relations": relations, "source_locations": locations}


async def get_item(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id} -- single item with entities, relations, source_locations."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.item.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    detail = await asyncio.to_thread(_item_detail, store, request.match_info["id"])
    if detail is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(detail)


async def update_item(request: web.Request) -> web.Response:
    """PATCH /api/knowledge/items/{id} -- update fields."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.item.update")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    item_id = request.match_info["id"]
    if not await asyncio.to_thread(store.get_item, item_id):
        return web.json_response({"error": "not found"}, status=404)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    allowed = {"tags", "item_type", "status", "title", "summary", "namespace"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        return web.json_response({"error": "no valid fields"}, status=400)
    await _audited_write(
        partial(store.update_item, item_id, **fields),
        event="item.update",
        fields={"item_id": item_id, "fields": list(fields)},
    )
    return web.json_response({"ok": True})


async def delete_item(request: web.Request) -> web.Response:
    """DELETE /api/knowledge/items/{id}."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.item.delete")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    item_id = request.match_info["id"]
    item = await asyncio.to_thread(store.get_item, item_id)
    if not item:
        return web.json_response({"error": "not found"}, status=404)

    # BEGIN IMMEDIATE takes the write lock eagerly (busy_timeout 10s, so a
    # concurrent writer can park this call for that long) and the commit is
    # followed by _load_graph(), a full scan of entities and entity_relations
    # that grows linearly with the library -- never on the event loop.
    # The SEL record rides in the same worker as the commit: a client
    # disconnect cancels this coroutine at the await, and a cancellation
    # landing after the worker committed must not skip the audit line. The
    # worker thread runs to completion regardless, and SEL writes are
    # lock-guarded, so emitting from it is safe.
    def _delete_and_audit() -> None:
        store.delete_item(item_id)
        _sel_log("item.delete", item_id=item_id)

    await asyncio.to_thread(_delete_and_audit)
    # A now-empty source is reclaimed by the store's own orphan rule on the next
    # open, which checks the document-state tables, in-flight jobs and the location
    # table first. Deleting the row here instead raises on the foreign keys those
    # tables hold -- after the item delete has already committed -- and drops a
    # source that still holds documents by location.
    return web.json_response({"ok": True})


async def get_item_content(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id}/content -- plain text for clipboard."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.item.content.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    item = await asyncio.to_thread(store.get_item, request.match_info["id"])
    if not item:
        return web.Response(text="not found", status=404)
    return web.Response(text=item["content"], content_type="text/plain")


# ---------- Entities ----------


def _entity_list_rows(store, where_clause: str, params: list) -> list:
    """The entity listing, in one off-loop take.

    Sync on purpose: an unanchored ``name LIKE '%q%'`` is a full scan over
    ``entities``, which grows without bound. The caller dispatches this to a
    worker thread; ``store.db`` is thread-local, so the thread gets its own
    connection.
    """
    return store.db.execute(
        f"SELECT * FROM entities WHERE {where_clause} ORDER BY name LIMIT ?", params
    ).fetchall()  # noqa: S608


async def list_entities(request: web.Request) -> web.Response:
    """GET /api/knowledge/entities."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.entities.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    etype = request.query.get("type")
    q = request.query.get("q")
    try:
        limit = min(500, max(1, int(request.query.get("limit", 100) or 100)))
    except ValueError:
        return web.json_response({"error": "invalid limit"}, status=400)

    where, params = ["1=1"], []  # type: list[str], list[object]
    if etype:
        where.append("entity_type = ?")
        params.append(etype)
    if q:
        where.append("name LIKE ?")
        params.append(f"%{q}%")
    params.append(limit)
    rows = await asyncio.to_thread(_entity_list_rows, store, " AND ".join(where), params)
    return web.json_response([dict(r) for r in rows])


async def get_entity_graph(request: web.Request) -> web.Response:
    """GET /api/knowledge/entities/{id}/graph -- D3-compatible subgraph."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.entity.graph.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    entity_id = request.match_info["id"]
    try:
        depth = min(5, max(1, int(request.query.get("depth", 2) or 2)))
    except ValueError:
        return web.json_response({"error": "invalid depth"}, status=400)
    # Materialise the graph off-loop before touching it. The store defers the
    # load to its first reader, and every `.graph` read below runs on
    # the event loop, where the loop-stall watchdog is armed -- so the scan has
    # to happen on a worker thread, the same way this module already offloads
    # the store's SQL.
    await asyncio.to_thread(store.ensure_graph_loaded)
    # get_entity_subgraph pins one graph reference internally and does the
    # existence check against it, so the 404 decision and the walk read the SAME
    # snapshot even if a worker-thread mutation swaps in a rebuilt graph; it
    # returns None when the entity is absent.
    result = store.get_entity_subgraph(entity_id, depth)
    if result is None:
        return web.json_response({"error": "entity not found"}, status=404)
    return web.json_response(result)


async def get_entity_items(request: web.Request) -> web.Response:
    """GET /api/knowledge/entities/by-name/{name}/items -- items containing entity."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.entity.items.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    name = request.match_info["name"]
    rows = await asyncio.to_thread(_entity_items_rows, store, name)
    return web.json_response([store._serialize_item(r) for r in rows])


def _entity_items_rows(store, name: str) -> list:
    """FTS lookup for an entity name. Runs on a worker thread, never the loop.

    Off-loop for two reasons: a legacy database migrates its FTS index on first
    read (`ensure_fts_index_current`), which is data-scaled, and the query itself
    is sqlite I/O.

    The name is matched as ONE FTS5 phrase over the segmented text, which is what
    an entity name is -- a contiguous string, not a bag of words. For a name with
    no CJK this is byte-identical to quoting the name directly, so a multi-word
    ASCII entity ("New York") still requires those words adjacent rather than
    merely both present. For a CJK name the segmentation makes the phrase address
    the individual characters the index stores, which quoting the whole run
    cannot.
    """
    store.ensure_fts_index_current()
    segmented = fts5_segment_for_index(name).strip()
    if not segmented:
        return []
    phrase = '"' + segmented.replace('"', '""') + '"'
    return store.db.execute(
        "SELECT i.* FROM items i JOIN items_fts f ON i.rowid = f.rowid "
        "WHERE items_fts MATCH ? AND i.status = 'active' ORDER BY i.updated_at DESC LIMIT 50",
        (phrase,),
    ).fetchall()


def _related_items(store, item_id: str, limit: int) -> list[dict]:
    """Items sharing entities with *item_id*, in one off-loop take.

    Sync on purpose: the ranking is a GROUP BY over the ``items``/``mentions``
    join, both of which grow without bound. One take keeps the two queries on
    one connection with no suspension between them -- not in one transaction,
    since the connection is autocommit, so a mention deleted between them just
    drops out of the ranking. The caller dispatches this to a worker thread;
    ``store.db`` is thread-local, so the thread gets its own connection.
    """
    # Find entities mentioned in this item
    entity_ids = [
        r["entity_id"]
        for r in store.db.execute(
            "SELECT entity_id FROM mentions WHERE item_id = ?", (item_id,)
        ).fetchall()
    ]
    if not entity_ids:
        return []

    # Find other items that mention the same entities, ranked by overlap count
    placeholders = ",".join("?" * len(entity_ids))
    rows = store.db.execute(
        f"SELECT i.*, COUNT(DISTINCT m.entity_id) as shared_entities "  # noqa: S608
        f"FROM items i JOIN mentions m ON i.id = m.item_id "
        f"WHERE m.entity_id IN ({placeholders}) AND i.id != ? AND i.status = 'active' "
        f"GROUP BY i.id ORDER BY shared_entities DESC LIMIT ?",
        [*entity_ids, item_id, limit],
    ).fetchall()
    return [{**store._serialize_item(r), "shared_entities": r["shared_entities"]} for r in rows]


async def get_related_items(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id}/related -- items sharing entities with given item."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.item.related.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    item_id = request.match_info["id"]
    try:
        limit = min(20, max(1, int(request.query.get("limit", 8) or 8)))
    except ValueError:
        return web.json_response({"error": "invalid limit"}, status=400)

    items = await asyncio.to_thread(_related_items, store, item_id, limit)
    return web.json_response(items)


async def get_full_graph(request: web.Request) -> web.Response:
    """GET /api/knowledge/graph -- full entity graph (top N by connections).

    Optional query params:
      limit: max nodes (1-200, default 100)
      source_id: comma-separated source IDs to filter entities by. When set,
        only entities mentioned in items belonging to those sources are included.
    """
    owner_denied = await _require_knowledge_owner(request, "knowledge.graph.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    try:
        limit = min(200, max(1, int(request.query.get("limit", 100) or 100)))
    except ValueError:
        return web.json_response({"error": "invalid limit"}, status=400)

    # Materialise the graph off-loop before any `.graph` read below. The store
    # defers the load to its first reader; this handler already offloads its SQL
    # for the same reason, and an inline graph read would stall the event loop.
    await asyncio.to_thread(store.ensure_graph_loaded)

    # Pin one graph reference for every read below. ``_load_graph`` publishes a
    # rebuilt graph by swapping ``store._graph``; re-reading
    # ``store.graph`` at each step (degree ranking, then per-node attribute
    # reads, then edges) could otherwise mix an old and a new graph and drop a
    # node between steps. One capture means this response is a single snapshot.
    graph = store.graph

    # Source filter: restrict to entities mentioned in items from specific sources
    source_id_param = request.query.get("source_id", "").strip()
    if source_id_param:
        source_ids = [s.strip() for s in source_id_param.split(",") if s.strip()]
        if not source_ids:
            return web.json_response({"nodes": [], "edges": []})
        placeholders = ",".join("?" * len(source_ids))
        # Find entity IDs mentioned in items belonging to the given sources.
        # Items belong to a source via items.source_id (ownership) OR via
        # source_locations.source_id (deduplication — item survives in another
        # source after a duplicate collapse).
        rows = await asyncio.to_thread(
            lambda: store.db.execute(
                f"SELECT DISTINCT m.entity_id FROM mentions m "  # noqa: S608
                f"JOIN items i ON m.item_id = i.id "
                f"WHERE i.status = 'active' AND ("
                f"  i.source_id IN ({placeholders})"
                f"  OR i.id IN ("
                f"    SELECT sl.item_id FROM source_locations sl"
                f"    WHERE sl.source_id IN ({placeholders})"
                f"  )"
                f")",
                source_ids + source_ids,
            ).fetchall()
        )
        allowed_entities = {row["entity_id"] for row in rows}
        if not allowed_entities:
            return web.json_response({"nodes": [], "edges": []})
        # Rank allowed entities by degree, take top N
        nodes_by_degree = sorted(
            allowed_entities,
            key=lambda n: graph.degree(n) if graph.has_node(n) else 0,
            reverse=True,
        )[:limit]
    else:
        nodes_by_degree = sorted(graph.nodes, key=lambda n: graph.degree(n), reverse=True)[:limit]

    if not nodes_by_degree:
        return web.json_response({"nodes": [], "edges": []})
    node_set = set(nodes_by_degree)
    nodes = [
        {"id": n, "name": graph.nodes[n].get("name"), "type": graph.nodes[n].get("entity_type")}
        for n in node_set
        if graph.has_node(n)
    ]
    edges = [
        {"source": u, "target": v, "type": d.get("relation_type"), "weight": d.get("weight")}
        for u, v, d in graph.edges(data=True)
        if u in node_set and v in node_set
    ]
    return web.json_response({"nodes": nodes, "edges": edges})


# ---------- Sources ----------


async def source_counts(request: web.Request) -> web.Response:
    """GET /api/knowledge/source-counts -- item count per source under the
    active type/status/namespace filters.

    The list view renders one collapsed row per source, so it needs a truthful
    per-source count *for the current filter set*. `/sources.item_count` is the
    source's unfiltered, all-namespace total and would over-report whenever a
    filter is on. Sourceless items are reported under the `__none__` key.
    """
    owner_denied = await _require_knowledge_owner(request, "knowledge.source_counts.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    item_type = request.query.get("type")
    status = request.query.get("status")
    namespace = request.query.get("namespace")
    where, params = ["1=1"], []  # type: list[str], list[object]
    if item_type:
        where.append("item_type = ?")
        params.append(item_type)
    if status:
        where.append("status = ?")
        params.append(status)
    if namespace:
        where.append("namespace = ?")
        params.append(namespace)
    # Counts what each source HOLDS, not only what it owns. After a duplicate
    # collapse a source is a location of the surviving copy rather than the owner of
    # a second one, and counting owners only would report 0 for a source that still
    # holds documents -- which the list view filters out, hiding a source the user
    # cannot then see or delete. The union is over item ids, so a document held both
    # ways counts once per source and never twice.
    where_sl: list[str] = []
    for w in where:
        if "source_id" in w:
            where_sl.append(w.replace("source_id", "i.source_id"))
        elif w == "1=1":
            where_sl.append(w)  # the constant-true clause takes no table alias
        else:
            where_sl.append(f"i.{w}")
    sql = (
        f"SELECT COALESCE(NULLIF(sid, ''), '{_NO_SOURCE}') AS sid, "  # noqa: S608
        "COUNT(DISTINCT item_id) AS cnt FROM ("
        f"  SELECT i.source_id AS sid, i.id AS item_id FROM items i WHERE {' AND '.join(where_sl)}"  # noqa: S608
        "  UNION"
        f"  SELECT sl.source_id AS sid, i.id AS item_id FROM source_locations sl"  # noqa: S608
        f"  JOIN items i ON i.id = sl.item_id WHERE {' AND '.join(where_sl)}"  # noqa: S608
        ") GROUP BY sid"
    )
    # This is a full aggregate scan over `items`, which grows without bound, so
    # unlike the point lookups elsewhere in this module it is offloaded rather
    # than run inline: blocking the event loop here would stall chat and
    # heartbeat processing on a large knowledge base.
    # The UNION repeats the filter clause, so the placeholders are bound twice.
    rows = await asyncio.to_thread(lambda: store.db.execute(sql, params + params).fetchall())
    counts = {r["sid"]: r["cnt"] for r in rows}
    # NOT sum(counts.values()): a document held by two sources appears in both
    # per-source counts, so summing them would exceed the number of documents and
    # contradict the Library's own item total.
    total_row = await asyncio.to_thread(
        lambda: store.db.execute(
            f"SELECT COUNT(*) FROM items WHERE {' AND '.join(where)}", params  # noqa: S608
        ).fetchone()
    )
    return web.json_response({"counts": counts, "total": total_row[0]})


# ---------- Stats ----------


def _stats_counts(store, with_embedded: bool) -> tuple[dict, int]:
    """The corpus counters, in one off-loop take.

    Sync on purpose: every counter is an unfiltered ``COUNT(*)`` over a table
    that grows without bound, and the dashboard polls this endpoint -- so on a
    large library, taken on the loop, it is the busiest stall in the module.
    The caller dispatches this to a worker thread; ``store.db`` is thread-local,
    so the thread gets its own connection.
    """
    stats = store.get_stats()
    if not with_embedded:
        return stats, 0
    embedded = store.db.execute(
        "SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    return stats, embedded


async def get_stats(request: web.Request) -> web.Response:
    """GET /api/knowledge/stats."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.stats.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    embedder = request.app.get("knowledge_embedder")
    stats, embedded_count = await asyncio.to_thread(_stats_counts, store, embedder is not None)
    if embedder:
        available = await embedder.is_available_async()
        stats["embeddings"] = {
            "enabled": True,
            "provider": "llama_cpp",
            "model": embedder.model,
            "available": available,
            "embedded_items": embedded_count,
        }
    else:
        stats["embeddings"] = {"enabled": False}
    return web.json_response(stats)
