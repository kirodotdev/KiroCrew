"""Tool declarations of the ``sessions`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.sessions`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the sessions tools."""
    return [
        {
            "name": "search_chat_history",
            "description": (
                "Search your own past conversation transcripts (chat history) by "
                "keyword and get back ranked, snippet-level hits. Use this to "
                "recover the exact words of a past conversation — 'the error message "
                "from that debugging session', a name/number/path mentioned earlier, "
                "the verbatim evidence behind a conclusion memory_recall gave you. "
                "For what was decided or learned, call memory_recall first: it "
                "searches the memory store bound to this session by meaning. "
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
                    "crew": {
                        "type": "string",
                        "description": (
                            "Optional: target a remote crew (its instance id or name from "
                            "the crew switcher) instead of local history — searches that "
                            "crew's own sessions over the tunnel. The crew must be "
                            "connected. Local-only filters (before/after/all_workspaces) do "
                            "not apply in crew mode."
                        ),
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
                    "crew": {
                        "type": "string",
                        "description": (
                            "Optional: read the full transcript from a remote crew (its "
                            "instance id or name) instead of local history. Pair with a "
                            "session_key returned by search_chat_history/list_sessions run "
                            "with the same crew. The crew must be connected."
                        ),
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
                "owning agent, approximate message volume, and created time, "
                "ordered by newest activity first. Use this when the user asks 'what are you working on?', "
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
                    "crew": {
                        "type": "string",
                        "description": (
                            "Optional: list a remote crew's sessions (its instance id or "
                            "name) instead of local ones, over the tunnel. The crew must be "
                            "connected. 'summarize' does not apply in crew mode."
                        ),
                    },
                },
            },
        },
    ]
