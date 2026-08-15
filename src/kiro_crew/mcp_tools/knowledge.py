"""The the knowledge library tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import (
    KNOWLEDGE_ADD_DOCUMENT_SCHEMA,
    KNOWLEDGE_DEDUP_SCHEMA,
    LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
    validate_tool_args,
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the knowledge tools."""
    return [
        {
            "name": "local_knowledge_search",
            "description": (
                "Search the user's knowledge library. Call ONLY when the user's "
                "message contains one of these explicit signals:\n"
                "- Asks 'what do we know about X' or 'check knowledge for X'\n"
                "- References a specific document, wiki, or stored content by name\n"
                "- Says 'in my docs', 'in my notes', 'according to our knowledge'\n"
                "- Asks a factual question AND mentions a topic you know is in "
                "their knowledge base\n\n"
                "Do NOT call for: general coding questions, file operations, "
                "debugging, or any task you can answer from context alone."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant knowledge chunks",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 3, max 5)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "knowledge_add_document",
            "description": (
                "Add a document you have READ during this task to the user's "
                "knowledge library, so it stays searchable later. Use it for a "
                "document that turned out to be load-bearing -- a design doc, "
                "spec, RFC, runbook, or wiki page that explains intent, a "
                "decision, or how something works, and that you would want to "
                "find again in a future session.\n\n"
                "Pass the document TEXT you already have as `content` -- this tool "
                "never opens files, so read the document with your own tools first, "
                "then hand over the text. Also pass where it came from as "
                "`source_uri` (the path or URL you read it from): that is what tells "
                "two documents apart, so a second \"README\" does not silently "
                "replace the first. Adding the same document twice is harmless: "
                "identical content is refused, not duplicated.\n\n"
                "Do NOT add: source code, agent instruction files (AGENTS.md, "
                "SKILL.md), generated or machine-readable files, chat transcripts, "
                "your own notes and summaries, or a page you only skimmed. When in "
                "doubt, skip it -- a polluted library makes every future search "
                "worse."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Human-readable document title, as the user would "
                            "recognise it."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The document text, as you already have it.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One line on why this document is worth keeping. "
                            "Recorded in the audit trail."
                        ),
                    },
                    "source_uri": {
                        "type": "string",
                        "description": (
                            "Where you read this document from -- a file path, "
                            "URL, or any stable handle. This is the document's "
                            "identity: re-adding the same source_uri replaces that "
                            "document, and two documents with different URIs stay "
                            "separate even when their titles match. Nothing here "
                            "is opened or fetched -- it is only stored as a label. "
                            "Required: without it two documents sharing a title "
                            "would overwrite each other."
                        ),
                    },
                },
                "required": ["title", "content", "source_uri"],
            },
        },
        {
            "name": "knowledge_dedup",
            "description": (
                "Find and collapse cross-source duplicate documents in the Knowledge "
                "Base (e.g. the same file uploaded directly AND synced via a folder). "
                "Defaults to a DRY-RUN preview that lists which duplicate would be "
                "deleted and which copy is kept, changing nothing. Pass apply=true to "
                "perform the hard deletes. Use when the user asks to de-duplicate, "
                "clean up, or preview duplicates in their knowledge base."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "apply": {
                        "type": "boolean",
                        "description": (
                            "false (default) = dry-run preview, no changes. "
                            "true = perform the hard deletes."
                        ),
                        "default": False,
                    },
                },
            },
        },
    ]


def local_knowledge_search(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, LOCAL_KNOWLEDGE_SEARCH_SCHEMA)
    query = args["query"]
    limit = args.get("limit", 3)

    db_path = Path(mcp_core.config_dir()) / "workspace" / "knowledge" / "knowledge.db"
    if not db_path.exists():
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="local_knowledge_search",
            outcome="not_configured",
        )
        return "Knowledge Library is not configured. Ingest documents via the dashboard first."

    # Reuse a cached store + embedder across calls; rebuilt only when the
    # knowledge DB (or its -wal) or config.json changes (see
    # _get_knowledge_search). Avoids the per-call schema/migrate/graph-load
    # and the embedder availability probe.
    cfg_path = Path(mcp_core.config_dir()) / "config.json"
    store, embedder = mcp_core._get_knowledge_search(db_path, cfg_path)
    embed_fn = embedder.embed if embedder and embedder.is_available() else None
    retriever = mcp_core.HybridRetriever(store, embedder=embed_fn)

    results = retriever.search(query, limit=limit)

    # Filter by minimum confidence score
    min_score = 0.012
    results = [r for r in results if r.get("score", 0) >= min_score]

    if not results:
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="local_knowledge_search",
            outcome="no_results",
            metadata={"query": query},
        )
        return "No relevant knowledge found."

    # Format output. Source identity (source_type/source_name/source_uri)
    # and the per-document locator (file_path for folders, artifact_slug +
    # artifact_name for artifacts) are attached by HybridRetriever
    # (_attach_citation_sources).
    lines = [
        "\U0001f4da Knowledge Library "
        "(supplementary reference \u2014 extract only what's relevant to the question):"
    ]
    for r in results:
        title = r.get("title") or "(untitled)"
        source_type = r.get("source_type") or ""
        artifact_slug = r.get("artifact_slug")
        artifact_name = r.get("artifact_name")
        # Document identity shown before the section. For artifacts this is
        # the artifact's own name -- the aggregate "Artifacts" source name
        # carries no information; for every other type it's the source name.
        if source_type == "artifact":
            source = artifact_name or r.get("source_name") or artifact_slug or ""
        else:
            source = r.get("source_name") or ""
        content = r.get("content", "")
        lines.append("\n---")
        lines.append(f"## {title}")
        if source:
            # Citation: [type] name, then section + line range when present.
            cite = "**Source:**"
            if source_type:
                cite += f" [{source_type}]"
            cite += f" {source}"
            section = r.get("section_title")
            if section:
                cite += f" \u2014 {section}"
            chunk_range = r.get("chunk_range")
            if chunk_range:
                cite += f" (lines {chunk_range})"
            lines.append(cite)
            # The most specific locator the source type affords, mirroring
            # the folder File: line.
            file_path = r.get("file_path")
            uri = r.get("source_uri") or ""
            if file_path:
                lines.append(f"**File:** {file_path}")
            elif artifact_slug:
                lines.append(f"**Artifact:** {artifact_slug}")
            elif uri:
                lines.append(f"**Link:** {uri}")
        lines.append(f"\n{content}")

    output = "\n".join(lines)
    output, _ = redact_exfiltration_urls(output)
    output, _ = redact_credentials(output)
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="local_knowledge_search",
        outcome="success",
        metadata={"query": query, "result_count": len(results)},
    )
    return output


def knowledge_add_document(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, KNOWLEDGE_ADD_DOCUMENT_SCHEMA)
    title = args["title"]
    content = args.get("content", "")
    if not content.strip():
        return "Provide the document text as content."
    # Routed through the gateway, not the store: ingestion needs the chunker,
    # extraction pool and embedder, and only the gateway holds them.
    resp = mcp_core._post("/api/knowledge/agent-document", {
        "title": title, "content": content,
        "reason": args.get("reason", ""),
        "source_uri": args.get("source_uri", ""),
    }, timeout=180)
    # The title arrives straight from the tool call, so it reaches the audit
    # log before the server-side redaction the document body gets. SEL is
    # persisted and readable, so redact it here.
    audit_title, _ = redact_credentials(title)
    audit_title, _ = redact_exfiltration_urls(audit_title)
    if resp.get("error"):
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="knowledge_add_document",
            outcome="error",
            metadata={"title": audit_title},
        )
        return f"Could not add the document: {resp['error']}"
    add_status = str(resp.get("status") or "")
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="knowledge_add_document",
        outcome=add_status,
        metadata={"title": audit_title, "items": resp.get("items", 0)},
    )
    if add_status == "duplicate":
        return (f"Already in the knowledge library, nothing added "
                f"({resp.get('reason', 'duplicate content')}).")
    # audit_title, not title: a document name is caller-supplied and free-form
    # enough to carry a credential, and this string is rendered into chat and
    # persisted in the transcript -- a wider audience than the audit log that
    # already takes the redacted form. Redaction is a no-op for an ordinary name.
    return (f"Added {audit_title!r} to the knowledge library "
            f"({resp.get('items', 0)} chunk(s)). It is now searchable via "
            f"local_knowledge_search.")


def knowledge_dedup(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, KNOWLEDGE_DEDUP_SCHEMA)
    apply = bool(args.get("apply", False))
    db_path = Path(mcp_core.config_dir()) / "workspace" / "knowledge" / "knowledge.db"
    if not db_path.exists():
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="knowledge_dedup",
            outcome="not_configured",
        )
        return "Knowledge Library is not configured. Ingest documents via the dashboard first."
    store = mcp_core.KnowledgeStore(str(db_path))
    try:
        results = mcp_core.dedup_sweep(store, apply=apply)
    finally:
        store.db.close()
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="knowledge_dedup",
        outcome="applied" if apply else "preview",
        metadata={"duplicate_count": len(results), "apply": apply},
    )
    if not results:
        return "No cross-source duplicate documents found."
    mode = "Deleted" if apply else "Would delete (dry run; set apply=true to delete)"
    lines = [f"{mode} — {len(results)} duplicate document(s):"]
    for r in results:
        lines.append(
            f"- {r['loser']} ({r['items_deleted']} chunks) -> kept "
            f"{r['winner']} [{r['reason']}]"
        )
    output = "\n".join(lines)
    output, _ = redact_exfiltration_urls(output)
    output, _ = redact_credentials(output)
    return output


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "local_knowledge_search": local_knowledge_search,
    "knowledge_add_document": knowledge_add_document,
    "knowledge_dedup": knowledge_dedup,
}
