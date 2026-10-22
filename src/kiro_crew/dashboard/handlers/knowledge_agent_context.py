"""The chat context-injection search (ranked cards within a token budget, used by the
dashboard's knowledge picker and command palette), and the route an agent calls to
add a document to the agent source.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, coerce_config_field, data_home, read_config_text
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.knowledge_access import (
    _pipeline,
    _require_knowledge_owner,
    _sel_log,
    _store,
)
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.knowledge.agent_source import add_agent_document
from kiro_crew.knowledge.ingestion import _redact
from kiro_crew.knowledge.retrieval import HybridRetriever, vector_leg

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


def _read_knowledge_config() -> dict:
    """The ``knowledge`` block of the gateway's ``config.json``, or ``{}``.

    Blocking: it stats and reads the gateway's own file under ``data_home()``, and on a
    home whose mount stopped answering that takes as long as the kernel does. So
    ``search_for_context`` runs it with ``asyncio.to_thread``. The path is never the
    request's choice, so the default executor is the lane, not the bounded probe pool
    request-chosen paths use. A missing, unreadable, malformed or wrong-typed file or
    block reads as ``{}``.
    """
    cfg_path = data_home() / "config.json"
    try:
        cfg = json.loads(read_config_text(cfg_path)) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        return {}
    knowledge_cfg = cfg.get("knowledge")
    return knowledge_cfg if isinstance(knowledge_cfg, dict) else {}


async def search_for_context(request: web.Request) -> web.Response:
    """GET /api/knowledge/search-for-context?q=...&limit=N

    Returns top results formatted for chat injection cards.
    Each result includes token count so frontend can show budget.
    """
    owner_denied = await _require_knowledge_owner(request, "knowledge.search_for_context.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    q = request.query.get("q", "").strip()
    if not q:
        return web.json_response({"error": "q parameter required"}, status=400)

    knowledge_cfg = await asyncio.to_thread(_read_knowledge_config)
    # A hand-edited config.json can carry a wrong-typed fetch_top_n/
    # fetch_max_tokens; coerce them the way the validated loader degrades a
    # mistyped field, so a non-numeric value falls back to the default instead
    # of reaching int() (TypeError) on this read-only GET.
    top_n = coerce_config_field(knowledge_cfg, "fetch_top_n", int, KNOWLEDGE_FETCH_TOP_N)
    max_tokens = coerce_config_field(
        knowledge_cfg, "fetch_max_tokens", int, KNOWLEDGE_FETCH_MAX_TOKENS
    )

    try:
        limit = min(100, max(1, int(request.query.get("limit", top_n))))
    except ValueError:
        limit = top_n

    embedder = request.app.get("knowledge_embedder")
    available = bool(embedder) and await embedder.is_available_async()
    embed_fn, embed_sig = vector_leg(embedder if available else None)
    retriever = HybridRetriever(store, embedder=embed_fn, embed_sig=embed_sig)
    # HybridRetriever.search runs on an mc-embed worker thread; KnowledgeStore
    # hands each thread its own sqlite connection, so all sqlite
    # access is thread-safe here. mc-embed bulkhead: the query embed occupies
    # the shared model.
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
            content = content[: remaining_budget * 4]
            tokens = remaining_budget
        cards.append(_build_context_card(r, content, tokens))
        total_tokens += tokens

    _sel_log("search_for_context", query=_redact(q), results=len(cards))
    return web.json_response(
        {
            "query": _redact(q),
            "results": cards,
            "total_tokens": total_tokens,
            "max_tokens": max_tokens,
        }
    )


async def add_agent_document_route(request: web.Request) -> web.Response:
    """POST /api/knowledge/agent-document -- the agent adds one document.

    Gated on ``knowledge.auto_add_documents``. Lives on the gateway rather than in
    the MCP process because ingestion needs the pipeline (reader, chunker,
    extraction pool, embedder), which only the gateway holds.
    """
    owner_denied = await _require_knowledge_owner(request, "knowledge.agent_document.add")
    if owner_denied is not None:
        return owner_denied
    cfg = KiroCrewConfig.load()
    if not cfg.knowledge.auto_add_documents:
        return web.json_response(
            {
                "error": "Adding documents to the knowledge library is turned off "
                "(knowledge.auto_add_documents).",
                "code": "auto_add_documents_disabled",
            },
            status=403,
        )
    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response(
            {"error": "pipeline not configured", "code": "pipeline_unavailable"}, status=503
        )
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    result = await add_agent_document(
        pipeline,
        title=str(body.get("title") or ""),
        content=str(body.get("content") or ""),
        reason=str(body.get("reason") or ""),
        source_uri=str(body.get("source_uri") or ""),
    )
    if result.get("status") == "error":
        return web.json_response(
            {"error": result["error"], "code": "document_rejected"}, status=400
        )
    _sel_log(
        "agent_document.add",
        title=_redact(result.get("title", "")) or "",
        status=result.get("status", ""),
    )
    return web.json_response(result)
