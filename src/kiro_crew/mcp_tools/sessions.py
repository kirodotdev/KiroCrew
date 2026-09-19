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
from urllib.parse import urlencode

from kiro_crew import mcp_core
from kiro_crew.context import RECALL_ROLES
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


def _history_memory_scope() -> tuple[str | None, dict[str, tuple[str, ...]], str]:
    from kiro_crew.member_memory_auth import (
        mcp_memory_scope,
        private_history_session_index,
        private_memory_boundaries_active,
    )

    if not private_memory_boundaries_active():
        return None, {}, ""
    session, error = mcp_core.require_strict_session_key(
        "Error: chat history requires a verified session."
    )
    if not session:
        return None, {}, error
    try:
        scope = mcp_memory_scope(session)
        return scope, private_history_session_index(), ""
    except (OSError, ValueError):
        return None, {}, "Error: this session's private memory is unavailable."


def _history_memory_visible(key: str, scope: str | None, index: dict[str, tuple[str, ...]]) -> bool:
    if scope is None:
        return True
    from kiro_crew.history import transcript_stems
    from kiro_crew.member_memory_auth import private_memory_store_for_session

    try:
        # An unsigned transcript cannot supply its own canonical identity.
        # Every candidate comes from the protected snapshot and is re-read
        # against its current store and transcript before any content is shown.
        candidates = set(index.get(key, ()))
        for stem in transcript_stems(key):
            candidates.update(index.get(stem, ()))
        return all(
            private_memory_store_for_session(candidate) == scope
            for candidate in candidates or {key}
        )
    except (OSError, ValueError):
        return False


# ── crew scope: delegate to the local gateway's MCP-only crew-read endpoints ──
#
# When a read tool is called with crew=<id|name>, it reads that REMOTE crew's
# sessions instead of the local ConversationLog. The MCP process cannot touch
# the tunnel, so it GETs the local gateway's internal-secret crew-sessions
# endpoints (handlers_instances.api_crew_sessions_*), which proxy over the
# tunnel and return already-redacted rows. The local (no-crew) branches below
# are unchanged.


def _crew_scope_refusal(memory_scope: str | None) -> str:
    """Refuse a crew-scoped read from a session fenced to a private member store.

    ``memory_scope`` is the store name for a Crew Member bound to a private V2
    store, ``""`` for an ordinary session, and ``None`` when the install has no
    private boundaries at all — so a NON-EMPTY value is the fenced case.

    A remote crew's transcripts carry no local private-store provenance:
    ``private_history_session_index()`` holds records for this crew's own
    members only, so ``_history_memory_visible`` has nothing to check a peer key
    against. Reading a peer crew's whole corpus is the boundary crossing the
    fence exists to prevent, so it fails closed here rather than reaching the
    tunnel and relying on a local index that cannot vouch for the answer.
    """
    if memory_scope:
        return "Access denied: a private-memory session cannot read another crew's sessions."
    return ""


def _crew_caller_key() -> "tuple[str, str]":
    """The caller's STRICTLY resolved key for a crew read, or a refusal.

    A crew read must not fall back to :func:`mcp_core._resolve_session_key`, the
    lenient resolution that includes the ``/proc`` ancestor walk. Two things go
    wrong if it does, and the second is a disclosure:

    * Under the lenient walk a subagent resolves to its PARENT slot, so the
      request would carry an authority the caller does not hold.
    * When resolution degrades to empty, ``_get`` omits ``X-Session-Key``
      ENTIRELY -- and the peer-read gate treats an absent key as the gateway/CLI
      trust root, because that is what a loopback ``curl`` holding the internal
      secret looks like. A degraded agent identity is then indistinguishable
      from the gateway itself and is admitted.

    So the failure is refused HERE, where the caller is known to be an MCP tool
    and absence means "identity unavailable", rather than at the route, where
    absence is a legitimate trust root that cannot be taken away without
    breaking the CLI.

    The key this returns is the key that goes on the wire: the three helpers
    below take it as a REQUIRED parameter and hand it to ``_get``, so re-resolving
    at the request cannot check one identity and act as another. Requiring it
    positionally is deliberate -- a future crew call site that forgets it is a
    ``TypeError`` rather than a silent return to the lenient walk.
    """
    return mcp_core.require_strict_session_key(
        "Error: reading another crew's sessions requires a verified session."
    )


def _crew_qs(params: "dict[str, str]") -> str:
    """The URL-encoded query for a crew read, WITHOUT a leading ``?``.

    Empty params are dropped so an absent optional never becomes ``key=``. The
    gateway owns tunnel auth, peer-reply byte caps, and redaction.

    The ``?`` stays at each call site rather than being added here, and each call
    site spells its own literal path instead of receiving one as an argument.
    That is what lets ``test_every_transport_call_resolves_to_a_path`` vouch for
    these three sites: its resolver truncates a path at the first ``?``, so the
    literal prefix must be visible in the f-string. Routing all three through one
    ``_get`` with a ``path`` parameter made the resolved path start with an
    unknown, and the guard cannot vouch for a call whose endpoint it cannot name.
    """
    return urlencode({k: v for k, v in params.items() if v not in ("", None)})


def _crew_error(crew: str, verb: str, resp: object) -> str:
    msg = resp.get("error") if isinstance(resp, dict) else "unreachable"
    return mcp_core._redact_history_output(f"Crew '{crew}' {verb} failed: {msg}")


def _crew_search_history(crew: str, query: str, limit: int, session_key: str) -> str:
    qs = _crew_qs({"crew": crew, "q": query, "limit": str(limit)})
    resp = mcp_core._get(f"/api/crew-sessions/search?{qs}", session_key)
    if not isinstance(resp, dict) or resp.get("error"):
        return _crew_error(crew, "search", resp)
    rows = resp.get("sessions") or []
    if not rows:
        return mcp_core._redact_history_output(f"No matching conversations found on crew '{crew}'.")
    lines = [
        f"\U0001f50e Chat history matches on crew '{crew}' (snippets only — use "
        f"get_chat_session with crew='{crew}' to read a full thread):"
    ]
    for r in rows:
        lines.append("\n---")
        lines.append(f"**{r.get('title') or r.get('key')}**  ·  `{r.get('key')}`")
        if r.get("date"):
            lines.append(f"_{r['date']}_")
        if r.get("snippet"):
            lines.append(f"\n{r['snippet']}")
    return mcp_core._redact_history_output("\n".join(lines))


def _crew_list_sessions(crew: str, limit: int, session_key: str) -> str:
    qs = _crew_qs({"crew": crew, "limit": str(limit)})
    resp = mcp_core._get(f"/api/crew-sessions/list?{qs}", session_key)
    if not isinstance(resp, dict) or resp.get("error"):
        return _crew_error(crew, "session list", resp)
    rows = resp.get("sessions") or []
    if not rows:
        return mcp_core._redact_history_output(f"No sessions found on crew '{crew}'.")
    lines = [f"\U0001f5c2\ufe0f Sessions on crew '{crew}' ({len(rows)}, newest first):"]
    for r in rows:
        key = r.get("key")
        title = r.get("title") or key
        meta_bits = []
        if r.get("agent"):
            meta_bits.append(f"agent={r['agent']}")
        if r.get("messages") is not None:
            meta_bits.append(f"~{r['messages']} msgs")
        if r.get("created"):
            meta_bits.append(str(r["created"])[:16])
        lines.append("\n---")
        lines.append(f"**{title}**  ·  `{key}`")
        if meta_bits:
            lines.append(f"_{'  ·  '.join(meta_bits)}_")
        if r.get("preview"):
            lines.append(f"\n{r['preview']}")
    return mcp_core._redact_history_output("\n".join(lines))


def _crew_get_session(crew: str, key: str, max_messages: int, session_key: str) -> str:
    qs = _crew_qs({"crew": crew, "key": key, "max_messages": str(max_messages)})
    resp = mcp_core._get(f"/api/crew-sessions/read?{qs}", session_key)
    if not isinstance(resp, dict) or resp.get("error"):
        return _crew_error(crew, "session read", resp)
    msgs = resp.get("messages") or []
    if not msgs:
        return mcp_core._redact_history_output(
            f"No readable messages for `{key}` on crew '{crew}'."
        )
    lines = [f"\U0001f4dc Conversation `{key}` on crew '{crew}':", ""]
    for m in msgs:
        role = str(m.get("role", "?")).title()
        lines.append(f"**{role}:** {m.get('content', '')}")
        lines.append("")
    return mcp_core._redact_history_output("\n".join(lines))


def search_chat_history(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SEARCH_CHAT_HISTORY_SCHEMA)
    query = args["query"]
    limit = args.get("limit", 10)
    all_workspaces = args.get("all_workspaces", False)
    memory_scope, memory_index, refusal = _history_memory_scope()
    if refusal:
        return refusal
    crew = args.get("crew")
    if crew:
        # Remote crew scope: read that crew's sessions over the tunnel. Local
        # filters (before/after/all_workspaces) do not apply in this v1.
        refusal = _crew_scope_refusal(memory_scope)
        if refusal:
            return refusal
        caller_key, refusal = _crew_caller_key()
        if refusal:
            return refusal
        return _crew_search_history(crew, query, limit, caller_key)
    # A supplied-but-unparseable date (one that passes the regex but names no
    # real calendar day, like Feb 30) must ERROR, not be silently dropped — a silent
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
        if not _history_memory_visible(key, memory_scope, memory_index):
            continue
        # TOCTOU: the file may be unlinked (clear-sessions, rotation, concurrent
        # process) between the ranked snapshot and this read. has_log is the
        # existence gate so we never emit a ghost row for a session the read
        # tool cannot retrieve. Do NOT additionally require non-empty
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
    memory_scope, memory_index, refusal = _history_memory_scope()
    if refusal:
        return refusal
    if not _history_memory_visible(key, memory_scope, memory_index):
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="get_chat_session",
            outcome="denied_memory_scope",
        )
        return "Access denied: that conversation belongs to a different memory store."

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

    crew = args.get("crew")
    if crew:
        # Remote crew scope: read the transcript from that crew over the tunnel.
        # The local key guard above still applies as defense-in-depth; the
        # gateway re-vets the key before it reaches the peer path.
        refusal = _crew_scope_refusal(memory_scope)
        if refusal:
            return refusal
        caller_key, refusal = _crew_caller_key()
        if refusal:
            return refusal
        return _crew_get_session(crew, key, max_messages, caller_key)

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

    # RECALL_ROLES rather than a literal, because this is the one surface whose
    # whole purpose is reading a past session: a breadcrumb appended with
    # role="inject" (a /note, a cron result) is precisely a message meant to
    # survive the session boundary being crossed here, and a hardcoded
    # {"user", "assistant"} dropped it. The constant already governs replay and
    # compression in context.py, so sharing it keeps the fetch from drifting
    # from them. Note it is narrowING as well as widening: "system" is absent
    # from RECALL_ROLES, so passing no roles at all would not be equivalent --
    # recent() treats a falsy roles as "no filter" and would admit internal
    # rows here.
    messages = cl.recent(key, max_messages=max_messages, roles=RECALL_ROLES)
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
    memory_scope, memory_index, refusal = _history_memory_scope()
    if refusal:
        return refusal
    crew = args.get("crew")
    if crew:
        # Remote crew scope: list that crew's sessions over the tunnel.
        # 'summarize' (LLM pass on local sessions) does not apply in crew mode.
        refusal = _crew_scope_refusal(memory_scope)
        if refusal:
            return refusal
        caller_key, refusal = _crew_caller_key()
        if refusal:
            return refusal
        return _crew_list_sessions(crew, limit, caller_key)

    cl = ConversationLog()
    session_key = mcp_core._resolve_session_key()
    list_ws: str | None = None if all_workspaces else mcp_core._caller_workspace(cl, session_key)

    rows: list[dict] = []
    for meta in cl.list_sessions():
        key = meta.get("key", "")
        if not key:
            continue
        if not _history_memory_visible(key, memory_scope, memory_index):
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
