"""The chat context-injection search (ranked cards within a token budget, used by the
dashboard's knowledge picker and command palette), and the route an agent calls to
add a document to the agent source.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging

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

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")

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

    # Remote bedrock_kb sources are queried live under a hard timeout and
    # fail OPEN (empty on any failure), then interleaved by rank inside the
    # shared helper. to_thread keeps the boto round trips off the event
    # loop; the bound lives inside the helper. The deferred import ALSO runs
    # inside the thread: importing boto3 on first use takes long enough to
    # stall every gateway task if it ran on the loop.
    def _augment_in_thread():
        from kiro_crew.knowledge.connectors import bedrock_kb

        return bedrock_kb.augment_with_remote(store, q, limit, results)

    results = await asyncio.to_thread(_augment_in_thread)

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


async def remote_search(request: web.Request) -> web.Response:
    """POST /api/knowledge/remote-search — raw bedrock_kb results (INTERNAL).

    Exists for exactly one caller: the sandboxed MCP server's remote leg.
    The sandbox seals ``aws_service_consent.json`` with a file-level
    self-bind, which PINS THE INODE — a host-side withdrawal replaces the
    file by atomic rename, so a sandboxed process keeps reading the old,
    still-granted bytes. Consent must therefore be evaluated in THIS
    process, which reads the live keystone store; the MCP process receives
    results, never a consent verdict of its own. Loopback +
    X-Internal-Secret via the mixed-internal registry, and STRICT-internal
    on top: the mixed registry also admits cookie-authenticated browsers,
    but these results are RAW connector output (un-redacted content), so a
    browser session is refused — the dashboard's own surfaces get remote
    results through search-for-context, which redacts.
    """
    if request.get("internal_auth") is not True:
        # Best-effort SEL denial, mirroring every peer internal-auth handler:
        # an ordinary cookie session CAN reach this refusal through the
        # mixed-path middleware, and an unaudited denial is invisible to
        # `kirocrew security events`. Late-binding sel() is the package's
        # standing circular-import exception.
        try:
            import kiro_crew.dashboard.handlers as _pkg

            _pkg.sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="knowledge_remote_search",
                outcome="denied",
                source="dashboard",
                resources=request.path,
                error="internal secret required",
            )
        except Exception:
            pass
        return web.json_response(
            {"error": "internal callers only", "code": "internal_only"}, status=403
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        # ``[]`` or a bare scalar is valid JSON but reaches ``.get`` otherwise.
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    query = str(body.get("query") or "").strip()
    try:
        limit = max(1, min(int(body.get("limit") or 5), 20))
    except (TypeError, ValueError):
        limit = 5
    source_id = body.get("source_id")
    if not query:
        return web.json_response({"results": []})
    store = _store(request)

    def _run() -> list[dict]:
        from kiro_crew.knowledge.connectors import bedrock_kb

        # The BOUNDED variant: the connector's admission semaphore caps
        # concurrent remote legs and its wall-clock budget cancels laggards —
        # calling the raw search here bypassed both, so repeated MCP searches
        # could queue unbounded threads that keep issuing paid calls after
        # their caller timed out.
        return bedrock_kb.search_remote_sources_bounded(
            store, query, limit, source_id=str(source_id) if source_id else None
        )

    try:
        results = await asyncio.to_thread(_run)
    except Exception as e:
        logger.info("remote-search leg failed open: %s", e.__class__.__name__)
        results = []
    return web.json_response({"results": results})
