"""The memory graph over every memory in a store, served from its layout file.

``GET /api/memory/graph/overview`` -- clusters, per-kind totals and bounds, or
``{"status": "building"}`` while the first build runs.
``GET /api/memory/graph/nodes`` -- the nodes inside a box, heaviest first, paged.
``GET /api/memory/graph/search`` -- id and position of every record the Memories
list would list for the same words.
``GET /api/memory/graph/select`` -- how many nodes a box holds, loaded or not.

Each route resolves its store the way ``GET /api/memory/graph`` does, brings the
layout up to date in a worker thread (:mod:`kiro_crew.memory_graph_service`),
and answers only positions, ids, kinds, weights and redacted labels.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from kiro_crew import memory_edit
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard.handlers._shared import _redact_memory_field
from kiro_crew.dashboard.handlers._shared_memory_stores import (
    resolve_requested_memory_store,
    vector_memory_for_store,
)

# The graph service imports numpy, so it loads on the first graph request, not
# with the dashboard (no-new-work-on-gateway-boot-path).
from kiro_crew.memory_graph_files import LABEL_CHARS, PAGE_CAP, STATUS_READY
from kiro_crew.memory_record_facets import BOX_KINDS, FacetError, parse_box, record_ref
from kiro_crew.security.exfil import _exempt_exact_hosts
from kiro_crew.security.redaction import _token_param_exempt_prefixes

logger = logging.getLogger(__name__)

#: The ``layout`` a reply carries when a rebuilt file was adopted while it was
#: read: matching no revision, it tells the browser to read everything again.
LAYOUT_CHANGED = "changed"


def _read_at_one_layout(graph: Any, read: Callable[[], dict]) -> dict:
    """``read()``'s reply with the ``layout`` revision it was read at.

    The revision is read before and after, so a rebuild adopted by another
    request mid-read is reported as :data:`LAYOUT_CHANGED`, never as either side.
    """
    before = graph.sidecar.revision()
    body = read()
    after = graph.sidecar.revision()
    return {**body, "layout": before if before == after else LAYOUT_CHANGED}


#: A graph node kind -> the Memories list's record kind for the same row.
_RECORD_KIND = {"episode": "episode", "fact": "fact", "lesson": "directive"}

#: Most search matches one reply carries; ``total`` still counts all of them.
SEARCH_CAP = 50_000


#: Redacted strings remembered, keyed by a digest of the text so a long memory
#: is not kept whole: about a store's ids, labels and link ids twice over.
_REDACTED_CAP = 1 << 17
_redacted_cache: OrderedDict[bytes, str] = OrderedDict()
_redacted_lock = threading.Lock()


def _redaction_exemptions() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The installed context's two exemption sets, the chain's only input besides the text.

    Both are read live from ``installed_context()`` and can change while the process
    runs (a companion loads, a test installs another context), so they are part of
    the cache key.
    """
    return _token_param_exempt_prefixes(), tuple(sorted(_exempt_exact_hosts()))


def _redacted_key(text: str, cut: int | None, exemptions: object) -> bytes:
    # repr() escapes NUL, so the separator cannot occur in the first part.
    digest = hashlib.blake2b(repr((cut, exemptions)).encode(), digest_size=16)
    digest.update(b"\0")
    digest.update(text.encode())
    return digest.digest()


def _redacted(text: str, cut: int | None = None) -> str:
    """*text* through the memory redaction chain, cut to *cut* characters after.

    Besides the text, the chain reads only fixed patterns and the context's
    exemption sets (:func:`_redaction_exemptions`, in the key), and it costs about
    0.25 ms a string: a 23,000-memory store's node pages cost seconds of CPU per
    read without this, enough to starve the gateway past its 50 s proxy timeout.
    The graph re-reads the same ids and labels on every pan, so most are hits.
    The whole text is redacted before the cut, so a secret across it goes whole.
    An answer is stored only when the exemptions read the same after the chain
    ran as before it, so it is never filed under a set it was not computed under.
    """
    exemptions = _redaction_exemptions()
    key = _redacted_key(text, cut, exemptions)
    with _redacted_lock:
        hit = _redacted_cache.get(key)
        if hit is not None:
            _redacted_cache.move_to_end(key)
            return hit
    out = _redact_memory_field(text)[:cut]
    if _redaction_exemptions() != exemptions:
        return out
    with _redacted_lock:
        _redacted_cache[key] = out
        while len(_redacted_cache) > _REDACTED_CAP:
            _redacted_cache.popitem(last=False)
    return out


def _bad(message: str) -> web.Response:
    return web.json_response({"error": message, "code": "invalid_graph_query"}, status=400)


def _unavailable() -> web.Response:
    return web.json_response(
        {"error": "This store's memory graph is not available.", "code": "store_unavailable"},
        status=503,
    )


def _kinds(request: web.Request) -> list[str] | None:
    kinds = request.query.getall("kind", [])
    if any(k not in BOX_KINDS for k in kinds):
        raise ValueError("kind is episode, fact or lesson")
    return kinds or None


async def _ready_graph(request: web.Request):
    """``(graph, tier, status)`` for the store the request names, or a refusal."""
    state = request.app["state"]
    store, denial = await resolve_requested_memory_store(request, state, "graph.read")
    if denial is not None:
        return None, None, None, denial
    try:
        tier = await vector_memory_for_store(state, store)
    except (OSError, sqlite3.Error):
        tier = None
    except Exception:  # noqa: BLE001 - a store that cannot open is reported, not raised
        logger.exception("memory graph: store %r did not open", store)
        tier = None
    db_path = getattr(tier, "_db_path", None)
    if tier is None or db_path is None:
        return None, None, None, _unavailable()
    from kiro_crew.memory_graph_service import graph_for

    graph = graph_for(Path(db_path))
    try:
        status = await asyncio.to_thread(graph.refresh, tier)
    except (OSError, sqlite3.Error):
        logger.exception("memory graph: layout refresh failed for %s", db_path)
        return None, None, None, _unavailable()
    return graph, tier, status, None


async def api_memory_graph_overview(request: web.Request) -> web.Response:
    graph, _tier, status, refusal = await _ready_graph(request)
    if refusal is not None:
        return refusal
    if status != STATUS_READY:
        return web.json_response({"status": status})

    def read() -> dict:
        clusters = graph.sidecar.clusters()
        for c in clusters:
            if c["label"] is not None:
                c["label"] = _redact_memory_field(c["label"])
        return {"status": STATUS_READY, "clusters": clusters, **graph.sidecar.summary()}

    return web.json_response(await asyncio.to_thread(_read_at_one_layout, graph, read))


async def api_memory_graph_nodes(request: web.Request) -> web.Response:
    try:
        box = tuple(parse_box(request.query.get("bbox", "")))
        kinds = _kinds(request)
        limit = int(request.query.get("limit", str(PAGE_CAP)))
    except (FacetError, ValueError) as exc:
        return _bad(str(exc))
    cursor = request.query.get("cursor") or None
    graph, _tier, status, refusal = await _ready_graph(request)
    if refusal is not None:
        return refusal
    if status != STATUS_READY:
        return web.json_response({"status": status, "nodes": [], "cursor": None})

    def read() -> dict:
        nodes, nxt = graph.sidecar.nodes_in_box(box, kinds, limit=limit, cursor=cursor)
        from kiro_crew.memory_graph_service import node_labels

        labels = node_labels(graph.db_path, nodes)
        links = graph.sidecar.links([n["id"] for n in nodes])
        # Every string in the reply is redacted on its own, through the cache: the
        # numbers, the kind and the cursor (weight:rowid) carry no memory text.
        for n in nodes:
            raw = n["id"]
            n["id"] = _redacted(raw)
            # Its related memories, ``[other id, cosine]``: the canvas joins the ones it holds.
            n["links"] = [[_redacted(other), sim] for other, sim in links.get(raw, [])]
            n["label"] = _redacted(labels.get(raw, ""), LABEL_CHARS)
            # The Memories list's identity of the row, so a click opens it there.
            kind = _RECORD_KIND[n["kind"]]
            n["record"] = {"kind": kind, "id": n["id"]}
            n["ref"] = record_ref(kind, raw)
        return {"status": STATUS_READY, "nodes": nodes, "cursor": nxt}

    try:
        return web.json_response(await asyncio.to_thread(_read_at_one_layout, graph, read))
    except ValueError:
        return _bad("cursor is not one this route returned")


async def api_memory_graph_search(request: web.Request) -> web.Response:
    q = request.query.get("q", "")
    mode = request.query.get("mode", "words")
    if mode not in ("words", "meaning"):
        return _bad("mode is words or meaning")
    try:
        kinds = _kinds(request)
    except ValueError as exc:
        return _bad(str(exc))
    graph, tier, status, refusal = await _ready_graph(request)
    if refusal is not None:
        return refusal
    if status != STATUS_READY:
        return web.json_response({"status": status, "matches": [], "total": 0})

    def read() -> dict:
        ids = memory_edit.matching_ids(tier, {"q": q}, meaning=mode == "meaning")
        pos = graph.sidecar.positions(ids, kinds)
        matches = [{"id": i, "x": pos[i][0], "y": pos[i][1]} for i in ids if i in pos]
        for m in matches[:SEARCH_CAP]:
            m["id"] = _redacted(m["id"])
        return {"status": STATUS_READY, "matches": matches[:SEARCH_CAP], "total": len(matches)}

    try:
        return web.json_response(await asyncio.to_thread(_read_at_one_layout, graph, read))
    except memory_edit.MemoryEditError as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)


async def api_memory_graph_select(request: web.Request) -> web.Response:
    try:
        box = tuple(parse_box(request.query.get("bbox", "")))
        kinds = _kinds(request)
    except (FacetError, ValueError) as exc:
        return _bad(str(exc))
    graph, _tier, status, refusal = await _ready_graph(request)
    if refusal is not None:
        return refusal
    if status != STATUS_READY:
        return web.json_response({"status": status, "count": 0})

    def read() -> dict:
        return {"status": STATUS_READY, "count": len(graph.sidecar.ids_in_box(box, kinds))}

    return web.json_response(await asyncio.to_thread(_read_at_one_layout, graph, read))
