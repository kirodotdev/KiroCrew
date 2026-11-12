"""Tool declarations of the ``knowledge`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.knowledge`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any


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
                        "description": (
                            "Max results to return (default 3, max 5). One extra "
                            "result may be appended when an exact-keyword match "
                            "would otherwise be dropped."
                        ),
                        "default": 3,
                    },
                    "source_id": {
                        "type": "string",
                        "description": (
                            "Optional source ID to scope keyword/vector seeding "
                            "to one knowledge source (graph traversal still "
                            "surfaces cross-source connections). Discover valid "
                            "IDs with knowledge_list_sources."
                        ),
                    },
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Optional namespace to scope keyword/vector seeding "
                            "to documents filed under one organisational label "
                            "(graph traversal still surfaces cross-namespace "
                            "connections). This is a relevance filter, not a "
                            "security boundary. Discover namespaces in the "
                            "dashboard Knowledge panel; composes with source_id."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "knowledge_list_sources",
            "description": (
                "Read-only counts for the user's knowledge library: how many "
                "sources, documents and items it holds in total, then one "
                "'name — id (N items)' line per source. Use it to answer 'how "
                "much is in my knowledge base', and to discover a valid "
                "source_id before scoping local_knowledge_search to a single "
                "source. It only counts -- it never rebuilds, repairs or "
                "flushes anything."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
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
                "It works only when the operator turned on "
                "knowledge.auto_add_documents (off by default). A refusal naming "
                "that key means it is off: tell the user, do not retry.\n\n"
                "Pass the document TEXT you already have as `content` -- this tool "
                "never opens files, so read the document with your own tools first, "
                "then hand over the text. Also pass where it came from as "
                "`source_uri` (the path or URL you read it from): that is what tells "
                'two documents apart, so a second "README" does not silently '
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
                            "Human-readable document title, as the user would " "recognise it."
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
