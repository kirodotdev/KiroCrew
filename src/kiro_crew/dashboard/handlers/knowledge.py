"""Knowledge Library API handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from aiohttp import web

from kiro_crew.artifacts import get_default_store
from kiro_crew.config.loader import KiroCrewConfig, config_dir, data_home
from kiro_crew.dashboard.handlers.files import (
    _ZIP_CONTAINER_EXTS,
    _content_matches_ext,
    _slot_project_snapshot,
)
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.knowledge.agent_fetch import fetch_url_content
from kiro_crew.knowledge.agent_source import add_agent_document
from kiro_crew.knowledge.artifact_ingest import ArtifactKnowledgeSync
from kiro_crew.knowledge.autosource import AUTO_ADDED_PROP
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.connectors.base import BaseConnector
from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector
from kiro_crew.knowledge.embedder import (
    create_embedder_from_config,
    embedder_signature,
    floats_to_bytes,
)
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.folder_watcher import SOURCE_TYPE_SKIP_DIRS
from kiro_crew.knowledge.ingestion import (
    IngestionPipeline,
    _redact,
    rebuild_embeddings,
    start_rebuild_job,
)
from kiro_crew.knowledge.llm_pool import LLMPool
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.sync import SyncScheduler
from kiro_crew.knowledge.watcher import KnowledgeWatcher
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Max length for a user-editable source display name (rename endpoint).
_MAX_SOURCE_NAME_LEN = 200


def _sel_log(tool: str, **kwargs: object) -> None:
    """Emit SEL audit event for knowledge API mutations."""
    sel().log_tool_invocation(
        session_key="dashboard", agent="knowledge-api",
        tool_name=f"knowledge.{tool}", outcome=str(kwargs.pop("outcome", "completed")),
        resources=str(kwargs) if kwargs else "",
    )


def _store(request: web.Request):
    return request.app["state"].knowledge_store


def _pipeline(request: web.Request):
    return request.app.get("knowledge_pipeline")


def _create_embedder(app):
    """Create embedder from KiroCrew config. Returns None if disabled/unavailable."""
    cfg_path = config_dir() / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    return create_embedder_from_config(cfg)


# ---------- Namespaces ----------


async def list_namespaces(request: web.Request) -> web.Response:
    """GET /api/knowledge/namespaces -- all namespaces with item counts."""
    store = _store(request)
    rows = store.db.execute(
        "SELECT namespace, COUNT(*) as count FROM items WHERE status = 'active' GROUP BY namespace ORDER BY count DESC"
    ).fetchall()
    return web.json_response([{"name": r["namespace"] or "default", "count": r["count"]} for r in rows])


# ---------- Source Watcher ----------

async def _start_watcher_async(app: web.Application) -> None:
    """Start the source watcher (auto-watches local_file sources)."""
    old_watcher = app.get("knowledge_watcher")
    if old_watcher:
        await old_watcher.stop()
    pipeline = app["knowledge_pipeline"]
    store = app["state"].knowledge_store
    state = app["state"]

    def _project_dirs() -> list[str]:
        """Directories the user is currently working in.

        Live chat-slot project dirs only -- deliberately NOT the recent-projects
        list, which includes directories the user merely picked once. Registering
        those would spend LLM extraction on trees they are not working in.

        Called by the watcher ON the event loop, because it copies a dict that
        other coroutines on the loop mutate; it does no I/O.
        """
        return _slot_project_snapshot(state)

    watcher = KnowledgeWatcher(store=store, pipeline=pipeline, project_dirs=_project_dirs)
    app["knowledge_watcher"] = watcher
    task = asyncio.create_task(watcher.start())
    app["_knowledge_watcher_task"] = task


async def _start_artifact_ingest_async(app: web.Application) -> None:
    """Wire artifact -> Knowledge Library sync when auto-ingest is enabled.

    Registers an in-process change-listener on the artifact store: every
    create / content-update / delete (from the agent's MCP tools, the CLI, the
    dashboard, bookmarks, and provider pull/clone -- all of which funnel
    through the store in the gateway process) ingests or removes that
    artifact's item group in the aggregate "Artifacts" Knowledge source. On the
    first run that creates the source row, a one-time backfill ingests
    pre-existing artifacts. Gated on ``knowledge.auto_ingest_artifacts`` (on by
    default). See ``kiro_crew.knowledge.artifact_ingest`` for the full design.
    """
    cfg = KiroCrewConfig.load()
    if not cfg.knowledge.auto_ingest_artifacts:
        return
    pipeline = app["knowledge_pipeline"]
    kinds = set(cfg.knowledge.auto_ingest_artifact_kinds)
    art_store = get_default_store()
    sync = ArtifactKnowledgeSync(
        art_store=art_store,
        pipeline=pipeline,
        kinds=kinds,
        loop=asyncio.get_running_loop(),
    )
    art_store.set_change_listener(sync.on_change)
    # Hold a reference so the listener binding isn't garbage-collected.
    app["artifact_knowledge_sync"] = sync
    await sync.start()


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
    folder_sids = {r["id"] for r in store.db.execute(
        f"SELECT id FROM sources WHERE id IN ({ph}) AND source_type IN ('local_folder', 'obsidian_vault')",  # noqa: S608
        list(source_ids)).fetchall()}
    artifact_sids = {r["id"] for r in store.db.execute(
        f"SELECT id FROM sources WHERE id IN ({ph}) AND source_type = 'artifact'",  # noqa: S608
        list(source_ids)).fetchall()}
    if not folder_sids and not artifact_sids:
        return
    # Build item_id -> group-label reverse map.
    item_to_file: dict[str, str] = {}
    # Folder/vault sources: group label is the file path.
    for sid in folder_sids:
        for row in store.db.execute(
                "SELECT file_path, item_ids FROM folder_file_state WHERE source_id = ?", (sid,)):
            try:
                ids = json.loads(row["item_ids"]) if row["item_ids"] else []
            except (json.JSONDecodeError, TypeError):
                continue
            for item_id in ids:
                item_to_file[item_id] = row["file_path"]
    # Aggregate artifact source: group label is the artifact name (fallback slug).
    for sid in artifact_sids:
        for row in store.db.execute(
                "SELECT slug, name, item_ids FROM artifact_item_state WHERE source_id = ?", (sid,)):
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
        chunk = item_ids[start:start + _SQLITE_VARIABLE_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = store.db.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders})",  # noqa: S608
            chunk,
        ).fetchall()
        for row in rows:
            out[row["id"]] = store._serialize_item(row)
    return out


async def list_items(request: web.Request) -> web.Response:
    """GET /api/knowledge/items -- list/search with pagination."""
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
        # The availability probe and retriever.search (blocking query embed to
        # Ollama) both do synchronous network I/O — run off-loop, mirroring
        # search_for_context below.
        embedder = request.app.get("knowledge_embedder")
        embed_fn = embedder.embed if embedder and await embedder.is_available_async() else None
        retriever = HybridRetriever(store, embedder=embed_fn)
        # mc-embed bulkhead: the search's query embed blocks on Ollama.
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
            all_results = await run_in_embed_pool(
                retriever.search, q, limit=limit * 3
            )
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
        items = filtered[offset:offset + limit]
        _attach_file_paths(store, items)
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
        where_clause = ' AND '.join(where)
        total = store.db.execute(
            f"SELECT COUNT(*) FROM items i WHERE {where_clause}",  # noqa: S608
            params).fetchone()[0]
        offset = (page - 1) * limit
        rows = store.db.execute(
            f"SELECT i.* FROM items i LEFT JOIN sources s ON i.source_id = s.id WHERE {where_clause} ORDER BY s.updated_at DESC, i.chunk_index ASC LIMIT ? OFFSET ?",  # noqa: S608, E501
            [*params, limit, offset]).fetchall()
        items = [store._serialize_item(r) for r in rows]
        _attach_file_paths(store, items)
        return web.json_response({"items": items, "total": total, "page": page, "limit": limit})


async def get_item(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id} -- single item with entities, relations, source_locations."""
    store = _store(request)
    item_id = request.match_info["id"]
    item = store.get_item(item_id)
    if not item:
        return web.json_response({"error": "not found"}, status=404)

    mentions = store.db.execute("SELECT entity_id, context FROM mentions WHERE item_id = ?", (item_id,)).fetchall()
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
                "SELECT * FROM entity_relations WHERE source_id = ? OR target_id = ?", (eid, eid)):
            r = dict(row)
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                # Resolve entity names for display
                src = store.db.execute("SELECT name FROM entities WHERE id = ?", (r["source_id"],)).fetchone()
                tgt = store.db.execute("SELECT name FROM entities WHERE id = ?", (r["target_id"],)).fetchone()
                r["source_name"] = src["name"] if src else r["source_id"]
                r["target_name"] = tgt["name"] if tgt else r["target_id"]
                relations.append(r)

    locations = [dict(r) for r in store.db.execute(
        "SELECT * FROM source_locations WHERE item_id = ?", (item_id,))]

    return web.json_response({**item, "entities": entities, "relations": relations, "source_locations": locations})


async def update_item(request: web.Request) -> web.Response:
    """PATCH /api/knowledge/items/{id} -- update fields."""
    store = _store(request)
    item_id = request.match_info["id"]
    if not store.get_item(item_id):
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    allowed = {"tags", "item_type", "status", "title", "summary", "namespace"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        return web.json_response({"error": "no valid fields"}, status=400)
    store.update_item(item_id, **fields)
    _sel_log("item.update", item_id=item_id, fields=list(fields))
    return web.json_response({"ok": True})


async def delete_item(request: web.Request) -> web.Response:
    """DELETE /api/knowledge/items/{id}."""
    store = _store(request)
    item_id = request.match_info["id"]
    item = store.get_item(item_id)
    if not item:
        return web.json_response({"error": "not found"}, status=404)
    store.delete_item(item_id)
    # A now-empty source is reclaimed by the store's own orphan rule on the next
    # open, which checks the document-state tables, in-flight jobs and the location
    # table first. Deleting the row here instead raised on the foreign keys those
    # tables hold -- after the item delete had already committed -- and dropped a
    # source that still held documents by location.
    _sel_log("item.delete", item_id=item_id)
    return web.json_response({"ok": True})


async def get_item_content(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id}/content -- plain text for clipboard."""
    store = _store(request)
    item = store.get_item(request.match_info["id"])
    if not item:
        return web.Response(text="not found", status=404)
    return web.Response(text=item["content"], content_type="text/plain")


# ---------- Entities ----------


async def list_entities(request: web.Request) -> web.Response:
    """GET /api/knowledge/entities."""
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
    rows = store.db.execute(
        f"SELECT * FROM entities WHERE {' AND '.join(where)} ORDER BY name LIMIT ?", params).fetchall()  # noqa: S608
    return web.json_response([dict(r) for r in rows])


async def get_entity_graph(request: web.Request) -> web.Response:
    """GET /api/knowledge/entities/{id}/graph -- D3-compatible subgraph."""
    store = _store(request)
    entity_id = request.match_info["id"]
    try:
        depth = min(5, max(1, int(request.query.get("depth", 2) or 2)))
    except ValueError:
        return web.json_response({"error": "invalid depth"}, status=400)
    if not store.graph.has_node(entity_id):
        return web.json_response({"error": "entity not found"}, status=404)
    return web.json_response(store.get_entity_subgraph(entity_id, depth))


async def get_entity_items(request: web.Request) -> web.Response:
    """GET /api/knowledge/entities/by-name/{name}/items -- items containing entity."""
    store = _store(request)
    name = request.match_info["name"]
    # Search items via FTS5 for the entity name
    sanitized = name.replace('"', '""')
    rows = store.db.execute(
        "SELECT i.* FROM items i JOIN items_fts f ON i.rowid = f.rowid "
        "WHERE items_fts MATCH ? AND i.status = 'active' ORDER BY i.updated_at DESC LIMIT 50",
        (f'"{sanitized}"',),
    ).fetchall()
    return web.json_response([store._serialize_item(r) for r in rows])


async def get_related_items(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id}/related -- items sharing entities with given item."""
    store = _store(request)
    item_id = request.match_info["id"]
    try:
        limit = min(20, max(1, int(request.query.get("limit", 8) or 8)))
    except ValueError:
        return web.json_response({"error": "invalid limit"}, status=400)

    # Find entities mentioned in this item
    entity_ids = [r["entity_id"] for r in store.db.execute(
        "SELECT entity_id FROM mentions WHERE item_id = ?", (item_id,)).fetchall()]
    if not entity_ids:
        return web.json_response([])

    # Find other items that mention the same entities, ranked by overlap count
    placeholders = ",".join("?" * len(entity_ids))
    rows = store.db.execute(
        f"SELECT i.*, COUNT(DISTINCT m.entity_id) as shared_entities "  # noqa: S608
        f"FROM items i JOIN mentions m ON i.id = m.item_id "
        f"WHERE m.entity_id IN ({placeholders}) AND i.id != ? AND i.status = 'active' "
        f"GROUP BY i.id ORDER BY shared_entities DESC LIMIT ?",
        [*entity_ids, item_id, limit]
    ).fetchall()
    return web.json_response([{**store._serialize_item(r), "shared_entities": r["shared_entities"]} for r in rows])


async def get_full_graph(request: web.Request) -> web.Response:
    """GET /api/knowledge/graph -- full entity graph (top N by connections)."""
    store = _store(request)
    try:
        limit = min(200, max(1, int(request.query.get("limit", 100) or 100)))
    except ValueError:
        return web.json_response({"error": "invalid limit"}, status=400)
    nodes_by_degree = sorted(store.graph.nodes, key=lambda n: store.graph.degree(n), reverse=True)[:limit]
    if not nodes_by_degree:
        return web.json_response({"nodes": [], "edges": []})
    node_set = set(nodes_by_degree)
    nodes = [{"id": n, "name": store.graph.nodes[n].get("name"), "type": store.graph.nodes[n].get("entity_type")}
             for n in node_set]
    edges = [{"source": u, "target": v, "type": d.get("relation_type"), "weight": d.get("weight")}
             for u, v, d in store.graph.edges(data=True) if u in node_set and v in node_set]
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
    where_sl = [w.replace("source_id", "i.source_id") if "source_id" in w else f"i.{w}"
                if w != "1=1" else w for w in where]
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
    rows = await asyncio.to_thread(
        lambda: store.db.execute(sql, params + params).fetchall())
    counts = {r["sid"]: r["cnt"] for r in rows}
    # NOT sum(counts.values()): a document held by two sources appears in both
    # per-source counts, so summing them would exceed the number of documents and
    # contradict the Library's own item total.
    total_row = await asyncio.to_thread(
        lambda: store.db.execute(
            f"SELECT COUNT(*) FROM items WHERE {' AND '.join(where)}", params  # noqa: S608
        ).fetchone())
    return web.json_response({"counts": counts, "total": total_row[0]})


async def list_sources(request: web.Request) -> web.Response:
    """GET /api/knowledge/sources."""
    store = _store(request)
    uri_filter = request.query.get("uri")
    if uri_filter:
        resolved_filter = str(Path(uri_filter).resolve()) if uri_filter.startswith('/') else uri_filter
        rows = store.db.execute(
            "SELECT s.*, COALESCE(c.cnt, 0) AS item_count "
            "FROM sources s LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM items GROUP BY source_id) c "
            "ON s.id = c.source_id WHERE s.uri = ? ORDER BY s.updated_at DESC",
            (resolved_filter,)
        ).fetchall()
    else:
        rows = store.db.execute(
            "SELECT s.*, COALESCE(c.cnt, 0) AS item_count "
            "FROM sources s LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM items GROUP BY source_id) c "
            "ON s.id = c.source_id ORDER BY s.updated_at DESC"
        ).fetchall()
    return web.json_response([dict(r) for r in rows])


# Max wall-clock the native folder dialog may stay open before we give up.
_FOLDER_DIALOG_TIMEOUT = 180  # seconds


def _folder_picker_available(request: web.Request) -> bool:
    """The native folder picker is offered only on macOS (via osascript) and
    only when the dashboard is local -- a dialog on a remote gateway would open
    on the wrong screen."""
    return sys.platform == "darwin" and bool(request.app.get("local_only", False))


def _run_folder_dialog() -> str | None:
    """Open the macOS native folder chooser (blocking) and return the selected
    absolute path, or None if the user cancelled or it failed to launch. Meant
    to run off the event loop via an executor."""
    cmd = [
        "osascript", "-e",
        'POSIX path of (choose folder with prompt '
        '"Select a folder to add to your knowledge base")',
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=_FOLDER_DIALOG_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    # osascript exits non-zero (and prints nothing) when the user cancels.
    path = proc.stdout.strip()
    return path if proc.returncode == 0 and path else None


async def pick_folder(request: web.Request) -> web.Response:
    """POST /api/knowledge/pick-folder -- open the macOS native folder chooser on
    the gateway host and return the selected absolute path.

    Offered only on a local macOS dashboard (see _folder_picker_available). The
    returned path is not trusted -- it is fed back into the folder path field and
    re-validated by add_source like any typed path."""
    if not _folder_picker_available(request):
        return web.json_response(
            {"error": "Folder picker is not available on this system"},
            status=403,
        )
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(None, _run_folder_dialog)
    if path:
        _sel_log("source.pick_folder", outcome="completed")
    return web.json_response({"path": path})


async def add_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources -- add a remote source."""
    store = _store(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name", "")
    source_type = body.get("source_type", "")
    uri = body.get("uri", "")
    properties = body.get("properties", {})
    if not isinstance(properties, dict):
        return web.json_response(
            {"error": "properties must be an object"}, status=400
        )
    namespace = body.get("namespace", "")

    # Validate namespace if provided at top level or in properties
    if not namespace:
        namespace = properties.get("namespace", "")
    if namespace:
        if not isinstance(namespace, str):
            return web.json_response(
                {"error": "namespace must be a string"}, status=400
            )
        namespace = namespace.strip()[:64]

    if not source_type:
        return web.json_response({"error": "source_type required"}, status=400)

    # Validate via connector if available
    sync_scheduler = request.app.get("knowledge_sync")
    if sync_scheduler:
        connector = sync_scheduler.get_connector(source_type)
        if connector:
            valid, err = connector.validate_config({**properties, "url": uri})
            if not valid:
                return web.json_response({"error": err}, status=400)

    if not uri:
        return web.json_response({"error": "uri required"}, status=400)

    # Sandbox guard: reject sensitive paths for any local source
    if not uri.startswith(("https://", "http://", "upload://", "code://")):
        resolved_uri = str(Path(uri).resolve())
        if is_sensitive_path(resolved_uri):
            _sel_log("source.add_denied", reason="sensitive_path", uri=uri)
            return web.json_response({"error": "Path is restricted for security reasons"}, status=403)

    # Validate URI format for sources without a dedicated connector
    if source_type == "local_file":
        # local_file sources use absolute file paths as URIs. Use is_absolute()
        # rather than a leading-"/" test: a Windows absolute path starts with a
        # drive letter (C:\... or C:/...), never "/", so the string test
        # rejected every valid Windows input and made single-file ingest 100%
        # unusable there.
        if not Path(uri).is_absolute():
            return web.json_response(
                {"error": "local_file URI must be an absolute path", "code": "uri_not_absolute"},
                status=400,
            )
        # Resolve symlinks and .. components before security check
        resolved = Path(uri).resolve()
        if is_sensitive_path(str(resolved)):
            _sel_log("source.add_denied", reason="sensitive_path", uri=uri)
            return web.json_response({"error": "path is restricted"}, status=403)
        if not resolved.is_file():
            return web.json_response({"error": "file not found"}, status=404)
        # Use canonical resolved path for storage and ingestion
        uri = str(resolved)
    elif not (sync_scheduler and sync_scheduler.get_connector(source_type)):
        if not uri.startswith("https://"):
            return web.json_response({"error": "URI must start with https://"}, status=400)
        if len(uri) > 2048:
            return web.json_response({"error": "URI too long (max 2048)"}, status=400)

    # Check for existing source with same URI
    existing = store.get_source_by_uri(uri)
    if existing:
        return web.json_response({"error": "source already exists", "id": existing["id"]}, status=409)

    # Folder sources: discovery walk + pending_confirmation (no auto-scan)
    if source_type in ("local_folder", "obsidian_vault"):
        folder_path = Path(uri).resolve()
        if is_sensitive_path(str(folder_path)):
            _sel_log("source.add_denied", reason="sensitive_path", uri=uri)
            return web.json_response({"error": "Path is restricted for security reasons"}, status=403)
        if not folder_path.is_dir():
            return web.json_response({"error": f"Directory not found: {uri}"}, status=400)

        # Run discovery walk to count files (no ingestion)
        watcher = request.app.get("knowledge_watcher")
        file_count = 0
        if watcher:
            extra_skip = SOURCE_TYPE_SKIP_DIRS.get(source_type, set())
            ignore_patterns = properties.get("ignore_patterns", [])
            discovered = await asyncio.to_thread(watcher._folder_watcher._walk, str(folder_path), ignore_patterns, extra_skip)
            file_count = len(discovered)

        # Store with pending_confirmation status
        if isinstance(properties, dict):
            properties["sync_status"] = "pending_confirmation"
            # Fold top-level namespace into properties for folder watchers
            if namespace and "namespace" not in properties:
                properties["namespace"] = namespace
        sid = store.add_source(name=name or uri, source_type=source_type, uri=uri,
                               properties=properties)
        _sel_log("source.add", source_id=sid, source_type=source_type)
        return web.json_response({"id": sid, "status": "pending_confirmation", "file_count": file_count}, status=201)

    sid = store.add_source(name=name or uri, source_type=source_type, uri=uri,
                           properties=properties)
    _sel_log("source.add", source_id=sid, source_type=source_type)

    # Trigger immediate ingestion for local_file sources
    if source_type == "local_file":
        pipeline = request.app.get("knowledge_pipeline")
        if pipeline:
            store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (sid,))
            store.db.commit()

            task = asyncio.create_task(_ingest_local_file_task(pipeline, store, uri, sid))
            app_tasks = request.app.setdefault("_bg_tasks", set())
            app_tasks.add(task)
            task.add_done_callback(app_tasks.discard)

    return web.json_response({"id": sid, "status": "created"}, status=201)


async def _ingest_local_file_task(pipeline, store, path: str, source_id: str) -> None:  # type: ignore[no-untyped-def]
    """Re-ingest a local_file source via the FileReader pipeline.

    Shared by add_source (initial ingest) and sync_source (manual re-sync) so both
    entry points route local files through the same FileReader path and apply the
    same read-time sensitive-path re-validation (defense-in-depth against TOCTOU).
    Updates sync_status to 'synced' on success or 'error' on failure.
    """
    try:
        if is_sensitive_path(str(Path(path).resolve())):
            _sel_log("source.sensitive_path_blocked", path=path, source_id=source_id)
            logger.warning("Sensitive path detected at ingest time: %s", path)
            store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,))
            store.db.commit()
            return
        await pipeline.ingest_file(path, source_id=source_id)
        store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (source_id,))
        store.db.commit()
    except Exception:
        logger.exception("Background ingestion failed for %s", path)
        store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,))
        store.db.commit()


async def sync_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/sync -- trigger sync for a source."""
    source_id = request.match_info["id"]
    store = _store(request)
    source = store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not source:
        return web.json_response({"error": "not found"}, status=404)

    sync_scheduler = request.app.get("knowledge_sync")
    if sync_scheduler:
        connector = sync_scheduler.get_connector(source["source_type"])
        if connector:
            result = await sync_scheduler.sync_source(source_id)
            _sel_log("source.sync", source_id=source_id)
            return web.json_response(result)

    # local_file sources carry a filesystem path, not a URL -- re-ingest the file
    # directly through the FileReader pipeline (same path as add_source), never the
    # agent URL-fetch fallback below (which would hand the path to ReadInternalWebsites
    # and fail with "Invalid URL format").
    if source["source_type"] == "local_file":
        file_uri = source["uri"] or ""
        if not file_uri:
            return web.json_response({"error": "no file path to sync"}, status=400)
        if source["sync_status"] == "syncing":
            return web.json_response({"error": "sync already in progress", "source_id": source_id}, status=409)
        pipeline = _pipeline(request)
        if not pipeline:
            return web.json_response({"error": "pipeline not configured"}, status=503)
        store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (source_id,))
        store.db.commit()
        task = asyncio.create_task(_ingest_local_file_task(pipeline, store, file_uri, source_id))
        app_tasks = request.app.setdefault("_bg_tasks", set())
        app_tasks.add(task)
        task.add_done_callback(app_tasks.discard)
        _sel_log("source.sync.local_file", source_id=source_id)
        return web.json_response({"synced": False, "status": "syncing", "source_id": source_id})

    # Agent-assisted sync: fetch in background, no chat session needed
    uri = source["uri"] or ""
    props = json.loads(source["properties"] or "{}") if isinstance(source["properties"], str) else (source["properties"] or {})
    url = uri or props.get("url", "")
    if not url:
        return web.json_response({"error": "no URL to fetch"}, status=400)

    if source["sync_status"] == "syncing":
        return web.json_response({"error": "sync already in progress", "source_id": source_id}, status=409)

    store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (source_id,))
    store.db.commit()

    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response({"error": "pipeline not configured"}, status=503)
    pool = request.app["knowledge_llm_pool"]
    task = asyncio.create_task(_background_agent_sync(source_id, url, source["name"], store, pipeline, pool))
    app_tasks = request.app.setdefault("_bg_tasks", set())
    app_tasks.add(task)
    task.add_done_callback(app_tasks.discard)
    _sel_log("source.sync.agent", source_id=source_id, url=url)
    return web.json_response({"synced": False, "status": "syncing", "source_id": source_id})


async def _background_agent_sync(  # type: ignore[no-untyped-def]
    source_id: str, url: str, name: str, store, pipeline, pool: LLMPool
) -> None:
    """Background task: fetch content via agent, then ingest."""
    try:
        content = await fetch_url_content(url, pool)
        redacted = _redact(content)
        content = redacted if redacted is not None else content
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md", prefix="agent_sync_")
        try:
            tmp.write(content.encode())
            tmp.close()
            await pipeline.ingest_file(tmp.name, original_name=name, source_id=source_id)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        store.db.execute(
            "UPDATE sources SET sync_status = 'synced' WHERE id = ?", (source_id,)
        )
        store.db.commit()
        logger.info("Agent sync complete: source=%s url=%s", source_id, url)
    except Exception:
        logger.exception("Agent sync failed: source=%s url=%s", source_id, url)
        store.db.execute(
            "UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,)
        )
        store.db.commit()


async def delete_source(request: web.Request) -> web.Response:
    """DELETE /api/knowledge/sources/{id} -- remove a source and its items."""
    store = _store(request)
    source_id = request.match_info["id"]
    row = store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    # An auto-discovered source must not come back on the next watcher sweep just
    # because its folder still exists -- tombstone the URI. This is passed INTO
    # the cascade so the tombstone and the delete share one transaction: written
    # afterwards, a sweep landing in between would see neither a source row nor a
    # tombstone and re-create what was just deleted. Only auto-added rows get a
    # tombstone; a hand-added source has no discovery loop to resurrect it.
    dismiss_uri = None
    try:
        props = json.loads(row["properties"]) if isinstance(row["properties"], str) else (row["properties"] or {})
        if isinstance(props, dict) and props.get(AUTO_ADDED_PROP):
            dismiss_uri = row["uri"]
    except Exception:
        logger.warning("Could not read source properties for dismissal", exc_info=True)
    try:
        # BEGIN IMMEDIATE takes the write lock eagerly and the connection's
        # busy_timeout is 10s, so a concurrent ingestion writer could park this
        # call for that long -- never on the event loop.
        await asyncio.to_thread(
            store.delete_source_cascade, source_id, dismiss_uri=dismiss_uri
        )
    except Exception:
        logger.exception("delete_source failed: source_id=%s", source_id)
        return web.json_response({"error": "internal server error"}, status=500)
    if dismiss_uri:
        _sel_log("source.auto_dismiss", source_id=source_id, uri=dismiss_uri)
    _sel_log("source.delete", source_id=source_id)
    return web.json_response({"status": "deleted"})


async def rename_source(request: web.Request) -> web.Response:
    """PATCH /api/knowledge/sources/{id} -- rename a source (name only).

    Only ``name`` is editable; ``uri`` (the source identity) stays immutable.
    """
    store = _store(request)
    source_id = request.match_info["id"]
    if not store.db.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = body.get("name")
    if not isinstance(name, str):
        return web.json_response({"error": "name must be a string"}, status=400)
    name = name.strip()
    if not name:
        return web.json_response({"error": "name cannot be empty"}, status=400)
    if len(name) > _MAX_SOURCE_NAME_LEN:
        return web.json_response(
            {"error": f"name must be {_MAX_SOURCE_NAME_LEN} characters or fewer"}, status=400)
    store.update_source(source_id, name=name)
    _sel_log("source.rename", source_id=source_id)
    return web.json_response({"ok": True, "name": name})


def _track_scan_task(app: web.Application, task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Keep strong reference to scan task and log exceptions."""
    tasks = app.setdefault("_scan_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(lambda t: logger.exception("scan_source failed", exc_info=t.exception()) if not t.cancelled() and t.exception() else None)


async def confirm_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/confirm -- confirm and start scanning."""
    store = _store(request)
    source_id = request.match_info["id"]
    row = store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    # TOCTOU: re-resolve path in case symlink was swapped since add-time
    resolved_uri = str(Path(row["uri"]).resolve())
    if is_sensitive_path(resolved_uri):
        _sel_log("source.confirm_denied", source_id=source_id, reason="sensitive_path")
        return web.json_response({"error": "Path is restricted for security reasons"}, status=403)
    props = json.loads(row["properties"]) if isinstance(row["properties"], str) else (row["properties"] or {})
    props["sync_status"] = "active"
    props.pop("scan_paused", None)
    store.update_source(source_id, properties=props, sync_status="active")
    _sel_log("source.confirm", source_id=source_id)
    # Trigger scan
    watcher = request.app.get("knowledge_watcher")
    if watcher:
        source = {"id": source_id, "uri": row["uri"], "source_type": row["source_type"], "properties": json.dumps(props)}
        task = asyncio.create_task(watcher._folder_watcher.scan_source(source))
        _track_scan_task(request.app, task)
    return web.json_response({"status": "scanning"})


async def pause_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/pause -- pause active scan."""
    store = _store(request)
    source_id = request.match_info["id"]
    row = store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    props = json.loads(row["properties"]) if isinstance(row["properties"], str) else (row["properties"] or {})
    props["scan_paused"] = True
    # Keep the JSON copy in sync with the column: the watcher's pre-scan skip
    # reads properties["sync_status"] (it selects `properties`, not the column),
    # so leaving this stale meant a paused folder was still fully walked and
    # delete-reconciled every sweep -- only the deeper scan_paused gate in
    # folder_watcher stopped the ingestion. confirm/resume already do this.
    props["sync_status"] = "paused"
    store.update_source(source_id, properties=props, sync_status="paused")
    _sel_log("source.pause", source_id=source_id)
    return web.json_response({"status": "paused"})


async def resume_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/resume -- resume paused scan."""
    store = _store(request)
    source_id = request.match_info["id"]
    row = store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    # TOCTOU: re-resolve path in case symlink was swapped while paused
    resolved_uri = str(Path(row["uri"]).resolve())
    if is_sensitive_path(resolved_uri):
        _sel_log("source.resume_denied", source_id=source_id, reason="sensitive_path")
        return web.json_response({"error": "Path is restricted for security reasons"}, status=403)
    props = json.loads(row["properties"]) if isinstance(row["properties"], str) else (row["properties"] or {})
    props.pop("scan_paused", None)
    props["sync_status"] = "active"
    store.update_source(source_id, properties=props, sync_status="active")
    _sel_log("source.resume", source_id=source_id)
    # Trigger scan to pick up remaining files
    watcher = request.app.get("knowledge_watcher")
    if watcher:
        source = {"id": source_id, "uri": row["uri"], "source_type": row["source_type"], "properties": json.dumps(props)}
        task = asyncio.create_task(watcher._folder_watcher.scan_source(source))
        _track_scan_task(request.app, task)
    return web.json_response({"status": "scanning"})


async def list_source_files(request: web.Request) -> web.Response:
    """GET /api/knowledge/sources/{id}/files -- list files with scan status."""
    store = _store(request)
    source_id = request.match_info["id"]
    rows = store.db.execute(
        "SELECT file_path, status, error_message, mtime, content_hash, item_ids, last_seen "
        "FROM folder_file_state WHERE source_id = ? ORDER BY last_seen DESC",
        (source_id,)).fetchall()
    files = [{"file_path": r["file_path"], "status": r["status"] or "pending",
              "error_message": _redact(r["error_message"]) if r["error_message"] else None,
              "mtime": r["mtime"],
              "item_count": len(json.loads(r["item_ids"] or "[]"))} for r in rows]
    # Also count totals
    total = len(files)
    done = sum(1 for f in files if f["status"] == "done")
    failed = sum(1 for f in files if f["status"] == "failed")
    skipped = sum(1 for f in files if f["status"] == "skipped")
    return web.json_response({"files": files, "total": total, "done": done, "failed": failed, "skipped": skipped})


async def retry_file(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/files/retry -- reset file to pending."""
    store = _store(request)
    source_id = request.match_info["id"]
    body = await request.json()
    file_path = body.get("file_path", "")
    if not file_path:
        return web.json_response({"error": "file_path required"}, status=400)
    if is_sensitive_path(file_path) or is_sensitive_path(str(Path(file_path).resolve())):
        _sel_log("source.file.retry_denied", source_id=source_id, reason="sensitive_path")
        return web.json_response({"error": "path is restricted"}, status=403)
    store.db.execute(
        "UPDATE folder_file_state SET status = 'pending', error_message = NULL WHERE source_id = ? AND file_path = ?",
        (source_id, file_path))
    store.db.commit()
    _sel_log("source.file.retry", source_id=source_id)
    return web.json_response({"status": "pending"})


async def skip_file(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/files/skip -- mark file as skipped."""
    store = _store(request)
    source_id = request.match_info["id"]
    body = await request.json()
    file_path = body.get("file_path", "")
    if not file_path:
        return web.json_response({"error": "file_path required"}, status=400)
    if is_sensitive_path(file_path) or is_sensitive_path(str(Path(file_path).resolve())):
        _sel_log("source.file.skip_denied", source_id=source_id, reason="sensitive_path")
        return web.json_response({"error": "path is restricted"}, status=403)
    store.db.execute(
        "UPDATE folder_file_state SET status = 'skipped', error_message = NULL WHERE source_id = ? AND file_path = ?",
        (source_id, file_path))
    store.db.commit()
    _sel_log("source.file.skip", source_id=source_id)
    return web.json_response({"status": "skipped"})


async def ingest_text(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/ingest-text -- agent submits fetched text."""
    source_id = request.match_info["id"]
    store = _store(request)
    source = store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not source:
        return web.json_response({"error": "source not found"}, status=404)
    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response({"error": "pipeline not configured"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    text = body.get("text", "")
    if not text:
        return web.json_response({"error": "no text provided"}, status=400)
    redacted = _redact(text)
    text = redacted if redacted is not None else text
    name = body.get("name", source["name"])
    namespace = body.get("namespace", "default")
    # Write to temp file and ingest
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md", prefix="agent_sync_")
    try:
        tmp.write(text.encode())
        tmp.close()
        job_id = await pipeline.ingest_file(tmp.name, original_name=name,
                                            namespace=namespace, source_id=source_id)
        # Update source status
        store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (source_id,))
        store.db.commit()
        _sel_log("source.ingest_text", source_id=source_id, name=name)
        return web.json_response({"ok": True, "job_id": job_id})
    except Exception:
        logger.exception("Agent ingest_text failed for source %s", source_id)
        return web.json_response({"error": "internal server error"}, status=500)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# ---------- Config ----------


async def get_config(request: web.Request) -> web.Response:
    """GET /api/knowledge/config -- returns supported formats and status."""
    pipeline = request.app.get("knowledge_pipeline")
    # ``FileReader.SUPPORTED`` contains '' (the empty suffix) to mark that
    # extensionless files (e.g. ``README``, ``Makefile``) are ingestable as
    # plain text. An empty string is not a valid HTML ``accept`` token, so we
    # keep ``supported_formats`` as the clean extension list and surface the
    # no-extension capability via an explicit boolean instead of stripping the
    # information away entirely.
    return web.json_response({
        "enabled": pipeline is not None,
        "supported_formats": sorted(FileReader.SUPPORTED - {''}),
        "accepts_no_extension": '' in FileReader.SUPPORTED,
        "folder_picker": _folder_picker_available(request),
    })


# ---------- Stats ----------


async def get_stats(request: web.Request) -> web.Response:
    """GET /api/knowledge/stats."""
    store = _store(request)
    stats = store.get_stats()
    embedder = request.app.get("knowledge_embedder")
    if embedder:
        embedded_count = store.db.execute("SELECT COUNT(*) FROM items WHERE embedding IS NOT NULL").fetchone()[0]
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


# ---------- Ingestion ----------


_MAX_INGEST_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
# Decompression-bomb bounds for zip-container uploads (.docx/.xlsx/.pptx/...).
# A valid PK signature passes the magic-byte gate but the archive can still be
# a bomb whose members expand unbounded once a parser (python-docx) opens it
# (CWE-770). Bound the declared aggregate uncompressed size and member count
# from the central directory before any parser touches the file.
_MAX_INGEST_ARCHIVE_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB uncompressed total
_MAX_INGEST_ARCHIVE_MEMBERS = 10000


def _inspect_zip_archive(path: str) -> str | None:
    """Bound a zip-container's member count + declared aggregate uncompressed
    size using central-directory metadata only (no extraction).

    Returns a short rejection reason, or ``None`` if within limits. This does
    synchronous zip I/O, so callers MUST run it off the event loop (via
    ``asyncio.to_thread``) — a large/hostile central directory would otherwise
    stall the gateway loop and heartbeat.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_INGEST_ARCHIVE_MEMBERS:
                return "too_many_members"
            uncompressed = 0
            for zi in infos:
                uncompressed += zi.file_size
                if uncompressed > _MAX_INGEST_ARCHIVE_UNCOMPRESSED:
                    return "uncompressed_too_large"
    except zipfile.BadZipFile:
        return "bad_archive"
    return None


async def ingest_file(request: web.Request) -> web.Response:
    """POST /api/knowledge/ingest -- multipart file upload."""
    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response({"error": "ingestion pipeline not configured"}, status=503)

    namespace = request.query.get("namespace", "default")
    reader = await request.multipart()
    field = await reader.next()
    if not field or not hasattr(field, "read_chunk") or field.name != "file":  # type: ignore[union-attr]
        return web.json_response({"error": "missing 'file' field"}, status=400)

    filename = getattr(field, "filename", None) or "upload"
    suffix = Path(filename).suffix
    ext = suffix.lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="kn_")
    try:
        total_size = 0
        # Capture the leading bytes so the claimed extension can be verified
        # against the file signature; 16 bytes covers every prefix in the
        # sibling gate (PNG magic is 8, WEBP needs bytes 8:12, zip/PDF fewer).
        head = bytearray()
        while True:
            chunk = await field.read_chunk()  # type: ignore[union-attr]
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > _MAX_INGEST_FILE_SIZE:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                return web.json_response(
                    {"error": f"file too large (max {_MAX_INGEST_FILE_SIZE // (1024 * 1024)} MB)"}, status=413)
            if len(head) < 16:
                head.extend(chunk[: 16 - len(head)])
            tmp.write(chunk)
        tmp.close()

        # Content-signature gate (CWE-434): the extension is attacker-controlled
        # and FileReader dispatches to binary parsers (.pdf/.docx) purely by
        # extension, so verify the magic bytes match the claimed type BEFORE the
        # file is handed to a parser. Text formats have no reliable signature and
        # pass through, matching the sibling upload gate in handlers/files.py.
        if not _content_matches_ext(ext, bytes(head)):
            Path(tmp.name).unlink(missing_ok=True)
            _sel_log("ingest", filename=filename, outcome="rejected")
            return web.json_response(
                {"error": f"file content does not match its type: {ext}"}, status=400)

        # Decompression-bomb guard (CWE-770): a valid-signature OOXML/zip can
        # still be a bomb whose members expand unbounded once python-docx / the
        # zip parser opens it. Bound the declared member count and aggregate
        # uncompressed size from the central directory (metadata only, no
        # extraction) BEFORE the file reaches a parser; reject a breach or a
        # corrupt/lying archive. Run off the event loop so a hostile central
        # directory can't stall the gateway loop/heartbeat.
        if ext in _ZIP_CONTAINER_EXTS:
            reason = await asyncio.to_thread(_inspect_zip_archive, tmp.name)
            if reason is not None:
                Path(tmp.name).unlink(missing_ok=True)
                _sel_log("ingest", filename=filename, outcome="rejected", reason=reason)
                return web.json_response(
                    {"error": f"{ext} archive rejected ({reason})"}, status=400)

        # Create source record immediately so it appears in the UI
        store = _store(request)
        uri = f"upload://{filename}"
        existing = store.get_source_by_uri(uri)
        if not existing:
            source_id = store.add_source(
                name=filename, source_type='local_file', uri=uri,
                properties={},
            )
            store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (source_id,))
            store.db.commit()
        else:
            source_id = existing['id']

        # Run extraction in background so response returns immediately
        async def _bg_ingest(tmp_path: str, src_id: str) -> None:
            try:
                await pipeline.ingest_file(tmp_path, original_name=filename, namespace=namespace, source_id=src_id)
            except Exception:
                logger.exception("Background ingestion failed for %s", filename)
                store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (src_id,))
                store.db.commit()
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        task = asyncio.create_task(_bg_ingest(tmp.name, source_id))
        app_tasks = request.app.setdefault("_bg_tasks", set())
        app_tasks.add(task)
        task.add_done_callback(app_tasks.discard)

        _sel_log("ingest", filename=filename)
        return web.json_response({"source_id": source_id, "status": "processing"})
    except Exception:
        logger.exception("Ingestion failed for %s", filename)
        Path(tmp.name).unlink(missing_ok=True)
        return web.json_response({"error": "internal server error"}, status=500)


async def get_job(request: web.Request) -> web.Response:
    """GET /api/knowledge/jobs/{id}."""
    store = _store(request)
    row = store.db.execute("SELECT * FROM ingestion_jobs WHERE id = ?",
                           (request.match_info["id"],)).fetchone()
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(dict(row))


# ---------- Export / Import ----------


async def export_item(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id}/export -- .knowledge JSON bundle."""
    store = _store(request)
    item_id = request.match_info["id"]
    bundle = store.export_item(item_id)
    if not bundle:
        return web.json_response({"error": "not found"}, status=404)
    _sel_log("export_item", item_id=item_id)
    return web.json_response(bundle, headers={"Content-Disposition": "attachment; filename=item.knowledge"})


async def export_all(request: web.Request) -> web.Response:
    """GET /api/knowledge/export -- full .knowledge JSON bundle, optionally filtered by namespace."""
    namespace = request.query.get("namespace")
    _sel_log("export_all", namespace=namespace)
    store = _store(request)
    bundle = store.export_all(namespace=namespace)
    safe_ns = re.sub(r'[^\w.-]', '_', namespace) if namespace else None
    filename = f"{safe_ns}.knowledge" if safe_ns else "knowledge.knowledge"
    return web.json_response(bundle, headers={"Content-Disposition": f"attachment; filename={filename}"})


async def import_bundle(request: web.Request) -> web.Response:
    """POST /api/knowledge/import -- accept .knowledge JSON bundle."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Redact imported text fields (may contain LLM-derived content from another instance)
    for item in body.get("items", []):
        redacted_title = _redact(item.get("title"))
        item["title"] = redacted_title if redacted_title is not None else ""
        item["summary"] = _redact(item.get("summary"))
        redacted_content = _redact(item.get("content"))
        item["content"] = redacted_content if redacted_content is not None else ""
    for ent in body.get("entities", []):
        redacted_name = _redact(ent.get("name"))
        ent["name"] = redacted_name if redacted_name is not None else ""
        ent["description"] = _redact(ent.get("description"))
    for rel in body.get("relations", []):
        redacted_type = _redact(rel.get("relation_type"))
        rel["relation_type"] = redacted_type if redacted_type is not None else ""
        rel["description"] = _redact(rel.get("description"))
    result = _store(request).import_bundle(body)
    _sel_log("import", **result)
    return web.json_response(result)


# ---------- Route registration ----------


async def get_embedding_status(request: web.Request) -> web.Response:
    """GET /api/knowledge/embedding/status -- embedding config and progress."""
    store = _store(request)
    embedder = request.app.get("knowledge_embedder")
    total = store.db.execute(
        "SELECT COUNT(*) as c FROM items WHERE status = 'active'"
    ).fetchone()["c"]
    embedded = store.db.execute(
        "SELECT COUNT(*) as c FROM items WHERE status = 'active' AND embedding IS NOT NULL"
    ).fetchone()["c"]
    # Polled every 30s by the frontend — loop-safe probe.
    available = await embedder.is_available_async() if embedder else False
    return web.json_response({
        "enabled": embedder is not None,
        "available": available,
        "model": embedder.model if embedder else None,
        "total_items": total,
        "embedded_items": embedded,
    })


async def _rebuild_embeddings_job(app: web.Application, store, embedder, job_id: str,
                                  force: bool = False) -> None:
    """Background wrapper: run the sig-gated rebuild and finalize the job row.

    The re-embed loop itself lives in ``knowledge.ingestion.rebuild_embeddings`` so
    the watcher self-heal path shares one implementation. Vectors are overwritten
    one item at a time, so existing vectors stay queryable throughout -- search
    degrades gracefully during the rebuild instead of going dark.
    """
    try:
        processed = await rebuild_embeddings(store, embedder, job_id=job_id, force=force)
        store.db.execute(
            "UPDATE ingestion_jobs SET status = 'completed', items_processed = ?, updated_at = ? "
            "WHERE id = ?",
            (processed, datetime.now().isoformat(), job_id))
        store.db.commit()
        _sel_log("batch_embed", count=processed, rebuild=True, force=force)
    except BaseException as exc:
        # CancelledError is a BaseException in 3.8+; finalize the row so a shutdown
        # cancellation can't leave it 'processing' and block the single-flight guard.
        is_cancel = isinstance(exc, asyncio.CancelledError)
        status = "cancelled" if is_cancel else "failed"
        if is_cancel:
            logger.debug("Embedding rebuild job %s cancelled", job_id)
        else:
            logger.exception("Embedding rebuild job %s failed", job_id)
        store.db.execute(
            "UPDATE ingestion_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, str(exc), datetime.now().isoformat(), job_id))
        store.db.commit()
        _sel_log("batch_embed", rebuild=True, force=force, outcome=status)
        if is_cancel:
            raise


async def batch_embed_items(request: web.Request) -> web.Response:
    """POST /api/knowledge/embedding/generate -- embed unembedded items, or re-embed all.

    ``{"rebuild": true}`` re-embeds every active item; because that can span the
    whole corpus it runs as a background job and returns a ``job_id`` to poll via
    ``GET /api/knowledge/jobs/{id}``. The default (fill-NULL) path stays synchronous
    since it only touches items missing an embedding at cold start.
    """
    store = _store(request)
    embedder = request.app.get("knowledge_embedder")
    if not embedder:
        return web.json_response({"error": "Embedding not enabled"}, status=400)
    if not await embedder.is_available_async():
        return web.json_response({"error": "Embedding model not available"}, status=503)

    body = await request.json() if request.can_read_body else {}
    rebuild = body.get("rebuild", False)
    force = body.get("force", False)

    if rebuild:
        # Single-flight: atomically claim the slot (sweeps crashed leftovers, races
        # safely against the watcher self-heal). None -> a rebuild is already running.
        # Offloaded: start_rebuild_job runs a blocking BEGIN IMMEDIATE write-lock
        # acquisition (busy_timeout up to 10s), which must never block the gateway
        # event loop (no-blocking-call-on-event-loop).
        job_id = await asyncio.to_thread(start_rebuild_job, store)
        if job_id is None:
            active = store.db.execute(
                "SELECT id FROM ingestion_jobs WHERE source_id IS NULL AND status = 'processing' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return web.json_response(
                {"job_id": active["id"] if active else None, "status": "processing"}
            )
        task = asyncio.create_task(
            _rebuild_embeddings_job(request.app, store, embedder, job_id, force=force))
        app_tasks = request.app.setdefault("_bg_tasks", set())
        app_tasks.add(task)
        task.add_done_callback(app_tasks.discard)
        return web.json_response({"job_id": job_id, "status": "processing"})

    rows = store.db.execute(
        "SELECT id, title, summary, content FROM items "
        "WHERE status = 'active' AND embedding IS NULL LIMIT 200"
    ).fetchall()

    loop = asyncio.get_running_loop()
    sig = embedder_signature(embedder)
    embedded = 0
    for row in rows:
        vec = await loop.run_in_executor(
            None, embedder.embed_for_item, row["title"], row["summary"], row["content"]
        )
        if vec:
            store.db.execute(
                "UPDATE items SET embedding = ?, embedding_sig = ?, embedded_at = ? WHERE id = ?",
                (floats_to_bytes(vec), sig, datetime.now().isoformat(), row["id"]))
            embedded += 1
            if embedded % 50 == 0:
                store.db.commit()

    store.db.commit()
    remaining = store.db.execute(
        "SELECT COUNT(*) as c FROM items WHERE status = 'active' AND embedding IS NULL"
    ).fetchone()["c"]
    _sel_log("batch_embed", count=embedded, rebuild=False)
    return web.json_response({"embedded": embedded, "total": len(rows), "remaining": remaining})


# ---------- Knowledge Fetch (for chat context injection) ----------

KNOWLEDGE_FETCH_TOP_N = 3
KNOWLEDGE_FETCH_MAX_TOKENS = 4096


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4


def _build_context_card(result: dict, content: str, tokens: int) -> dict:
    """Build a chat-injection context card with citation fields.

    All user/LLM-derived string fields are passed through ``_redact()``
    (redact_exfiltration_urls + redact_credentials). Source identity
    (``source_type``/``source_name``/``source_uri``) and the per-document
    locator (``file_path`` for folders, ``artifact_slug``/``artifact_name``
    for artifacts) are attached by HybridRetriever (_attach_citation_sources);
    ``section_title``/``chunk_range`` come from the item's stored location.
    Any field is absent (None) when the source type or item does not afford it.
    """
    safe_content = _redact(content) or ""
    return {
        "id": result["id"],
        "title": _redact(result["title"]) or "(untitled)",
        "source": _redact(result.get("source")),
        "source_type": _redact(result.get("source_type")),
        "source_name": _redact(result.get("source_name")),
        "source_uri": _redact(result.get("source_uri")),
        "file_path": _redact(result.get("file_path")),
        "artifact_slug": _redact(result.get("artifact_slug")),
        "artifact_name": _redact(result.get("artifact_name")),
        "section_title": _redact(result.get("section_title")),
        "chunk_range": _redact(result.get("chunk_range")),
        "match_type": result.get("match_type", "keyword"),
        "tokens": tokens,
        "summary": _redact(result.get("summary")) or safe_content[:200],
        "content": safe_content,
    }


async def search_for_context(request: web.Request) -> web.Response:
    """GET /api/knowledge/search-for-context?q=...&limit=N

    Returns top results formatted for chat injection cards.
    Each result includes token count so frontend can show budget.
    """
    store = _store(request)
    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response({"error": "q parameter required"}, status=400)

    cfg_path = data_home() / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    top_n = cfg.get("knowledge", {}).get("fetch_top_n", KNOWLEDGE_FETCH_TOP_N)
    max_tokens = cfg.get("knowledge", {}).get("fetch_max_tokens", KNOWLEDGE_FETCH_MAX_TOKENS)

    try:
        limit = min(100, max(1, int(request.query.get("limit", top_n))))
    except ValueError:
        limit = top_n

    embedder = request.app.get("knowledge_embedder")
    embed_fn = embedder.embed if embedder and await embedder.is_available_async() else None
    retriever = HybridRetriever(store, embedder=embed_fn)
    # HybridRetriever.search runs on an mc-embed worker thread; KnowledgeStore
    # hands each thread its own sqlite connection, so all sqlite
    # access is thread-safe here. mc-embed bulkhead: the query embed
    # blocks on Ollama.
    results = await run_in_embed_pool(retriever.search, q, limit=limit)

    cards = []
    total_tokens = 0
    for r in results:
        # _redact() calls redact_exfiltration_urls() + redact_credentials() (see ingestion.py)
        content = _redact(r.get("content", "")) or ""
        tokens = _estimate_tokens(content)
        remaining_budget = max_tokens - total_tokens
        if remaining_budget <= 0:
            break
        if tokens > remaining_budget:
            content = content[:remaining_budget * 4]
            tokens = remaining_budget
        cards.append(_build_context_card(r, content, tokens))
        total_tokens += tokens

    _sel_log("search_for_context", query=_redact(q), results=len(cards))
    return web.json_response({
        "query": _redact(q),
        "results": cards,
        "total_tokens": total_tokens,
        "max_tokens": max_tokens,
    })


async def add_agent_document_route(request: web.Request) -> web.Response:
    """POST /api/knowledge/agent-document -- the agent adds one document.

    Gated on ``knowledge.auto_add_documents``. Lives on the gateway rather than in
    the MCP process because ingestion needs the pipeline (reader, chunker,
    extraction pool, embedder), which only the gateway holds.
    """
    cfg = KiroCrewConfig.load()
    if not cfg.knowledge.auto_add_documents:
        return web.json_response(
            {"error": "Adding documents to the knowledge library is turned off "
                      "(knowledge.auto_add_documents).",
             "code": "auto_add_documents_disabled"}, status=403)
    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response(
            {"error": "pipeline not configured",
             "code": "pipeline_unavailable"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON", "code": "invalid_json"}, status=400)
    result = await add_agent_document(
        pipeline,
        title=str(body.get("title") or ""),
        content=str(body.get("content") or ""),
        reason=str(body.get("reason") or ""),
        source_uri=str(body.get("source_uri") or ""),
    )
    if result.get("status") == "error":
        return web.json_response(
            {"error": result["error"], "code": "document_rejected"}, status=400)
    _sel_log("agent_document.add", title=_redact(result.get("title", "")) or "",
             status=result.get("status", ""))
    return web.json_response(result)


def setup_knowledge_routes(app: web.Application) -> None:
    # Initialize pipeline and sync scheduler if not already set
    if "knowledge_pipeline" not in app:
        store = app["state"].knowledge_store
        pool = LLMPool()
        embedder = _create_embedder(app)
        pipeline = IngestionPipeline(store=store, extractor=EntityExtractor(pool=pool),
                                     chunker=HeadingAwareChunker(), reader=FileReader(),
                                     embedder=embedder)
        app["knowledge_llm_pool"] = pool
        app["knowledge_embedder"] = embedder
        connectors: dict[str, "BaseConnector"] = {}
        # Local folder connector (always available)
        connectors["local_folder"] = LocalFolderConnector()
        connectors["obsidian_vault"] = LocalFolderConnector()
        # Edition-contributed connectors (CPP KnowledgeProvider seam). Built-ins
        # are set FIRST so an edition can both ADD a new source_type and, if it
        # ever needs to, override a built-in. The Default returns {} → standalone
        # keeps exactly {local_folder, obsidian_vault}. Fail-closed: a
        # non-standalone host that cannot compose raises (via safe_context_call);
        # a transient adapter error degrades to built-ins only.
        from kiro_crew.platform.context import current_context, safe_context_call

        _no_extra: dict[str, "BaseConnector"] = {}

        def _extra_connectors() -> "dict[str, BaseConnector]":
            # Bind the context ONCE so the KnowledgeProvider adapter and the cfg it
            # receives come from the SAME PlatformContext (a context swap between
            # two lookups could otherwise pair an adapter with a foreign cfg).
            ctx = current_context()
            return ctx.knowledge.extra_connectors(ctx.cfg)

        connectors.update(
            safe_context_call(
                _extra_connectors,
                fallback=_no_extra,
                log_message="knowledge.extra_connectors failed; built-in connectors only",
            )
        )
        app["knowledge_pipeline"] = pipeline
        app["knowledge_sync"] = SyncScheduler(store=store, pipeline=pipeline,
                                              connectors=connectors)
        # Start source watcher (auto-watches local_file sources)
        app.on_startup.append(_start_watcher_async)
        # Start artifact ingest watcher (no-op unless auto-ingest is enabled)
        app.on_startup.append(_start_artifact_ingest_async)

    app.router.add_get("/api/knowledge/config", get_config)
    app.router.add_get("/api/knowledge/items", list_items)
    app.router.add_get("/api/knowledge/namespaces", list_namespaces)
    app.router.add_get("/api/knowledge/stats", get_stats)
    app.router.add_get("/api/knowledge/sources", list_sources)
    app.router.add_get("/api/knowledge/source-counts", source_counts)
    app.router.add_post("/api/knowledge/sources", add_source)
    app.router.add_post("/api/knowledge/pick-folder", pick_folder)
    app.router.add_post("/api/knowledge/sources/{id}/sync", sync_source)
    app.router.add_post("/api/knowledge/sources/{id}/confirm", confirm_source)
    app.router.add_post("/api/knowledge/sources/{id}/pause", pause_source)
    app.router.add_post("/api/knowledge/sources/{id}/resume", resume_source)
    app.router.add_get("/api/knowledge/sources/{id}/files", list_source_files)
    app.router.add_post("/api/knowledge/sources/{id}/files/retry", retry_file)
    app.router.add_post("/api/knowledge/sources/{id}/files/skip", skip_file)
    app.router.add_post("/api/knowledge/sources/{id}/ingest-text", ingest_text)
    app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
    app.router.add_patch("/api/knowledge/sources/{id}", rename_source)
    app.router.add_get("/api/knowledge/entities", list_entities)
    app.router.add_get("/api/knowledge/graph", get_full_graph)
    app.router.add_get("/api/knowledge/export", export_all)
    app.router.add_post("/api/knowledge/ingest", ingest_file)
    app.router.add_post("/api/knowledge/agent-document", add_agent_document_route)
    app.router.add_post("/api/knowledge/import", import_bundle)
    app.router.add_get("/api/knowledge/items/{id}", get_item)
    app.router.add_patch("/api/knowledge/items/{id}", update_item)
    app.router.add_delete("/api/knowledge/items/{id}", delete_item)
    app.router.add_get("/api/knowledge/items/{id}/content", get_item_content)
    app.router.add_get("/api/knowledge/items/{id}/related", get_related_items)
    app.router.add_get("/api/knowledge/items/{id}/export", export_item)
    app.router.add_get("/api/knowledge/entities/by-name/{name}/items", get_entity_items)
    app.router.add_get("/api/knowledge/entities/{id}/graph", get_entity_graph)
    app.router.add_get("/api/knowledge/jobs/{id}", get_job)
    app.router.add_get("/api/knowledge/embedding/status", get_embedding_status)
    app.router.add_post("/api/knowledge/embedding/generate", batch_embed_items)
    app.router.add_get("/api/knowledge/search-for-context", search_for_context)

    # Pool lifecycle: lazy start on first request, shutdown on app exit
    async def _shutdown_pool(app: web.Application) -> None:
        pool = app.get("knowledge_llm_pool")
        if pool:
            await pool.shutdown()

    app.on_cleanup.append(_shutdown_pool)

    async def _stop_watcher(app: web.Application) -> None:
        watcher = app.get("knowledge_watcher")
        if watcher:
            await watcher.stop()
        task = app.get("_knowledge_watcher_task")
        if task and not task.done():
            task.cancel()

    app.on_cleanup.append(_stop_watcher)
