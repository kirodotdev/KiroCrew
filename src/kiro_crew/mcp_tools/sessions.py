"""The reading this workspace's own chat history tools: what they advertise and what they do.

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

import hashlib
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.history import ConversationLog
from kiro_crew.validation import (
    GET_CHAT_SESSION_SCHEMA,
    LIST_SESSIONS_SCHEMA,
    SEARCH_CHAT_HISTORY_SCHEMA,
    validate_tool_args,
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the sessions tools."""
    return [
        {
            "name": "search_chat_history",
            "description": (
                "Search your own past conversation transcripts (chat history) by "
                "keyword and get back ranked, snippet-level hits. Use this to "
                "recover context that is NOT in your injected memory — e.g. 'what "
                "did we decide about X three weeks ago', 'the error message from "
                "that debugging session', a name/number/path mentioned earlier. "
                "Search like a human: try a query, read the snippets, then re-search "
                "with different keywords if the first hit isn't right. Returns "
                "metadata + a short snippet per session (NOT full transcripts) — "
                "call get_chat_session with a returned session_key to read the full "
                "thread once a hit looks promising. Scoped to your current workspace "
                "by default. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword(s) to search for in past conversations.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10, max 50).",
                        "default": 10,
                    },
                    "before": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified before this day.",
                    },
                    "after": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified on/after this day.",
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Search across all workspaces instead of just the current one (default false).",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_chat_session",
            "description": (
                "Read the full message transcript of one past conversation, "
                "identified by a session_key returned from search_chat_history. "
                "Returns the messages as role/content pairs, tail-capped at "
                "max_messages. Use after search_chat_history when a snippet hit "
                "looks like the thread you need. Refuses incognito/temporary "
                "sessions. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_key": {
                        "type": "string",
                        "description": "The session_key from a search_chat_history result.",
                    },
                    "max_messages": {
                        "type": "integer",
                        "description": "Max (most recent) messages to return (default 50, max 200).",
                        "default": 50,
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Allow reading a session from a different workspace than the caller's (default false — deny cross-workspace).",
                        "default": False,
                    },
                },
                "required": ["session_key"],
            },
        },
        {
            "name": "list_sessions",
            "description": (
                "List your recent conversation sessions in this workspace so you "
                "can see the work in flight and what you've been doing — titles, "
                "owning agent, message volume, and last-activity time, newest "
                "first. Use this when the user asks 'what are you working on?', "
                "'what sessions are open?', 'what have we been doing?', or when you "
                "need a bird's-eye view of your own workspace before acting. This "
                "is a READ — it never modifies memory or history. It complements "
                "search_chat_history (which finds a specific past thread by "
                "keyword): list_sessions is the browse/overview, search is the "
                "lookup. Scoped to your current workspace by default; "
                "incognito/temporary sessions are never listed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max sessions to return, newest first (default 20, max 100).",
                        "default": 20,
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "List sessions across all workspaces instead of just the current one (default false).",
                        "default": False,
                    },
                    "summarize": {
                        "type": "boolean",
                        "description": (
                            "When true, generate a fresh one-line LLM summary for the top "
                            "sessions (bounded, best-effort — costs tokens + latency, so it's "
                            "opt-in). When false (default), the existing session title is used "
                            "with zero cost."
                        ),
                        "default": False,
                    },
                },
            },
        },
    ]


def search_chat_history(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SEARCH_CHAT_HISTORY_SCHEMA)
    query = args["query"]
    limit = args.get("limit", 10)
    all_workspaces = args.get("all_workspaces", False)
    # A supplied-but-unparseable date (e.g. 2026-02-30 passes the regex but is
    # not a real calendar date) must ERROR, not be silently dropped — a silent
    # drop would return the UNFILTERED set and mislead the caller.
    after_epoch = before_epoch = None
    if args.get("after"):
        after_epoch = mcp_core._parse_iso_date_epoch(args["after"])
        if after_epoch is None:
            return "Invalid 'after' date — use a real calendar date (YYYY-MM-DD)."
    if args.get("before"):
        before_epoch = mcp_core._parse_iso_date_epoch(args["before"])
        if before_epoch is None:
            return "Invalid 'before' date — use a real calendar date (YYYY-MM-DD)."

    cl = ConversationLog()
    session_key = mcp_core._resolve_session_key()
    # Default scoping: confine to the caller's workspace (fail-closed — unset
    # buckets to "default"). all_workspaces opts out.
    current_ws: str | None = None if all_workspaces else mcp_core._caller_workspace(cl, session_key)

    # Fetch the FULL ranked match set (bounded by the backend's scan window),
    # not a fixed limit*3 over-fetch: heavy incognito/workspace/date drops on
    # the first page could otherwise starve a caller whose real matches rank
    # lower, returning "no results" while hits exist.
    ranked: list[dict] = cl.search_sessions(query, limit=mcp_core._SEARCH_HISTORY_SCAN)

    results: list[dict] = []
    for meta in ranked:
        key = meta.get("key", "")
        if not key:
            continue
        # TOCTOU: the file may be unlinked (clear-sessions, rotation, concurrent
        # process) between the ranked snapshot and this read. has_log is the
        # existence gate so we never emit a ghost row for a session the read
        # tool can no longer retrieve. Do NOT additionally require non-empty
        # metadata: a legacy session whose file predates the metadata line
        # returns {} here yet get_chat_session serves it fine, so rejecting {}
        # would hide those sessions from search while they remain readable.
        if not cl.has_log(key):
            continue
        full_meta = cl.get_metadata(key)
        if mcp_core._history_is_incognito(full_meta) or mcp_core._history_is_incognito(meta):
            continue  # EB-5: incognito/temporary never surface
        if current_ws is not None and mcp_core._ws_bucket(full_meta.get("workspace")) != current_ws:
            continue  # EB-cc3: workspace scoping (fail-closed; normalizes non-str)
        modified = meta.get("modified", 0) or 0
        if after_epoch is not None and modified < after_epoch:
            continue
        if before_epoch is not None and modified >= before_epoch:
            continue

        snippet = mcp_core._extract_history_snippet(cl.read_messages(key), query)
        results.append(
            {
                "session_key": key,
                "title": meta.get("title") or key,
                "date": meta.get("created") or "",
                "snippet": snippet,
            }
        )
        if len(results) >= limit:
            break

    if not results:
        mcp_core.sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="search_chat_history",
            outcome="no_results",
            metadata={"query_len": len(query)},
        )
        return "No matching conversations found. Try different keywords."

    lines = [
        "\U0001f50e Chat history matches "
        "(snippets only — use get_chat_session to read a full thread):"
    ]
    for r in results:
        lines.append("\n---")
        lines.append(f"**{r['title']}**  ·  `{r['session_key']}`")
        if r["date"]:
            lines.append(f"_{r['date']}_")
        if r["snippet"]:
            lines.append(f"\n{r['snippet']}")

    output = "\n".join(lines)
    # EB-6: redact secrets/exfil URLs from snippets before returning.
    output = mcp_core._redact_history_output(output)
    mcp_core.sel().log_tool_invocation(
        session_key=session_key,
        source="mcp",
        tool_name="search_chat_history",
        outcome="success",
        metadata={"query_len": len(query), "result_count": len(results)},
    )
    return output


def get_chat_session(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, GET_CHAT_SESSION_SCHEMA)
    key = args["session_key"]
    max_messages = args.get("max_messages", 50)
    all_workspaces = args.get("all_workspaces", False)

    # Defense-in-depth on a path-bearing identifier: ConversationLog._safe_key
    # already neutralizes separators. Reject path separators outright, and ".."
    # only as a STANDALONE component — not as a substring — so legitimate keys
    # like "dashboard_chat-2..3" round-trip between search and read. (A strict
    # allowlist regex is avoided: real keys legitimately contain ':' and '.')
    if "/" in key or "\\" in key or key in ("..", "."):
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="get_chat_session",
            outcome="rejected_bad_key",
        )
        return "Invalid session_key."

    cl = ConversationLog()
    if not cl.has_log(key):
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="get_chat_session",
            outcome="not_found",
        )
        # Do NOT echo the raw caller-supplied key: the dashboard renders it as
        # live markdown, so a crafted key (e.g. "[x](https://evil/)") would be a
        # reflected phishing/prompt-injection payload. Return a stable
        # fingerprint instead — enough to correlate, safe to render. (Not a
        # security signature — just a display-safe correlation id — but use
        # sha256 anyway so no weak-hash scanner flags this egress path.)
        fp = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:12]
        return f"No conversation found for that session_key (fp:{fp})."

    meta = cl.get_metadata(key)
    if mcp_core._history_is_incognito(meta):
        # EB-7b: no bypass of incognito exclusion via direct fetch.
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="get_chat_session",
            outcome="refused_incognito",
        )
        return "That conversation is private (incognito/temporary) and cannot be read."

    # Deny-by-default workspace isolation: mirror search_chat_history's
    # fail-closed scoping so a caller can't bypass it by fetching a session
    # from another workspace directly. Unset/non-string workspaces bucket as
    # "default" via _ws_bucket.
    if not all_workspaces:
        caller_ws = mcp_core._caller_workspace(cl, mcp_core._resolve_session_key())
        if mcp_core._ws_bucket(meta.get("workspace")) != caller_ws:
            mcp_core.sel().log_tool_invocation(
                session_key=mcp_core._resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="denied_cross_workspace",
            )
            return "Access denied: that conversation belongs to a different workspace."

    messages = cl.recent(key, max_messages=max_messages, roles={"user", "assistant"})
    if not messages:
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="get_chat_session",
            outcome="empty",
        )
        return mcp_core._redact_history_output(f"Conversation `{key}` has no readable messages.")

    title = meta.get("title") or key
    lines = [f"\U0001f4dc Conversation: **{title}**  ·  `{key}`", ""]
    for m in messages:
        role = str(m.get("role", "?")).title()
        lines.append(f"**{role}:** {m.get('content', '')}")
        lines.append("")

    output = mcp_core._redact_history_output("\n".join(lines))
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="get_chat_session",
        outcome="success",
        metadata={"message_count": len(messages)},
    )
    return output


def list_sessions(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, LIST_SESSIONS_SCHEMA)
    limit = args.get("limit", 20)
    all_workspaces = args.get("all_workspaces", False)
    summarize = args.get("summarize", False)

    cl = ConversationLog()
    session_key = mcp_core._resolve_session_key()
    list_ws: str | None = None if all_workspaces else mcp_core._caller_workspace(cl, session_key)

    rows: list[dict] = []
    for meta in cl.list_sessions():
        key = meta.get("key", "")
        if not key:
            continue
        if mcp_core._history_is_incognito(meta):
            continue  # incognito/temporary never surface
        if list_ws is not None:
            # list_sessions() rows omit `workspace`, so scope off the full
            # metadata line (mirrors search_chat_history). Runs in the MCP
            # process, not the gateway loop, so the extra read is fine.
            if mcp_core._ws_bucket(cl.get_metadata(key).get("workspace")) != list_ws:
                continue  # fail-closed workspace scoping
        rows.append(meta)
        if len(rows) >= limit:
            break

    if not rows:
        mcp_core.sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="list_sessions",
            outcome="no_results",
        )
        return "No sessions found in this workspace yet."

    # Opt-in: ask the gateway (which owns the LLM background session) to
    # generate fresh one-line summaries for the returned keys. Best-effort —
    # any failure falls back to titles, so the list is always returned.
    summaries: dict[str, str] = {}
    if summarize:
        resp = mcp_core._post(
            "/api/sessions/summarize",
            {"keys": [r["key"] for r in rows]},
            timeout=120,
        )
        if isinstance(resp, dict) and isinstance(resp.get("summaries"), dict):
            summaries = {str(k): str(v) for k, v in resp["summaries"].items() if v}

    scope_label = "across all workspaces" if all_workspaces else "in this workspace"
    lines = [f"\U0001f5c2\ufe0f Sessions {scope_label} ({len(rows)}, newest first):"]
    for r in rows:
        key = r["key"]
        title = r.get("title") or key
        agent = r.get("agent")
        msgs = r.get("messages", 0)
        created = r.get("created", "")
        meta_bits = []
        if agent:
            meta_bits.append(f"agent={agent}")
        meta_bits.append(f"~{msgs} msgs")
        if created:
            meta_bits.append(str(created)[:16])
        lines.append("\n---")
        lines.append(f"**{title}**  ·  `{key}`")
        lines.append(f"_{'  ·  '.join(meta_bits)}_")
        summary = summaries.get(key)
        if summary:
            lines.append(f"\n{summary}")

    output = mcp_core._redact_history_output("\n".join(lines))
    mcp_core.sel().log_tool_invocation(
        session_key=session_key,
        source="mcp",
        tool_name="list_sessions",
        outcome="success",
        metadata={"result_count": len(rows), "summarized": len(summaries)},
    )
    return output


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "search_chat_history": search_chat_history,
    "get_chat_session": get_chat_session,
    "list_sessions": list_sessions,
}
