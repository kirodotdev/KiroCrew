"""``GET /api/sessions/search/messages``: every matching message across chats.

``GET /api/sessions/search`` ranks whole sessions and returns one excerpt each. This
route keeps that ranking (the same candidates, app scope and gating) and, for each
ranked session the filters keep, lists the messages that hold the query with their
position in the paging corpus, so the dashboard can open the chat at that message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from aiohttp import web

from kiro_crew import history_search
from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import _audit_app_allow
from kiro_crew.dashboard.handlers.sessions_parts.history_reads import (
    _AppOwnedKeys,
    _mark_open_sessions,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    SEARCH_MIN_CHARS,
    ConversationLog,
    HistoryLockTimeout,
    transcript_lock_stems,
    transcript_stem,
)
from kiro_crew.history_search import search_candidate_cap, search_coverage
from kiro_crew.security import redact
from kiro_crew.validation import sanitize_string

#: Most sessions one reply lists.
MAX_SESSIONS = 50
#: Most messages listed per session; ``hit_count`` still counts them all.
MAX_HITS_PER_SESSION = 5
#: Characters of context kept before and after the match in a hit's excerpt.
SNIPPET_LEAD = 60
SNIPPET_TRAIL = 140
SNIPPET_MAX = 240
#: The ``role`` filter values and the transcript roles each one keeps.
ROLE_FILTERS: dict[str, frozenset[str]] = {
    "": frozenset({"user", "assistant"}),
    "user": frozenset({"user"}),
    "assistant": frozenset({"assistant"}),
}
#: The session fields the reply carries, besides ``hits`` and ``hit_count``.
_ROW_FIELDS = ("key", "title", "agent", "modified", "open")
#: Marks a time parameter the route refuses with 400.
_BAD_TIME = object()


def _epoch_param(request: web.Request, name: str) -> float | None | object:
    """*name* as epoch seconds, ``None`` when absent, ``_BAD_TIME`` when unreadable."""
    raw = request.query.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return _BAD_TIME
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return _BAD_TIME
    return value


def _limit_param(request: web.Request) -> int:
    try:
        return max(1, min(int(request.query.get("limit", str(MAX_SESSIONS))), MAX_SESSIONS))
    except (TypeError, ValueError):
        return MAX_SESSIONS


def _keeps_session(row: dict, agent: str, since: float | None) -> bool:
    if agent and row.get("agent") != agent:
        return False
    if since is not None:
        modified = row.get("modified")
        if not isinstance(modified, (int, float)) or modified < since:
            return False
    return True


def _excerpt(text: str, pos: int, needle: str) -> str:
    start = max(0, pos - SNIPPET_LEAD)
    end = min(len(text), pos + len(needle) + SNIPPET_TRAIL)
    frag = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + frag + suffix)[:SNIPPET_MAX]


class MessageQuery(NamedTuple):
    """A query parsed once for the per-message gate and excerpt anchor."""

    needles: list[history_search.SearchNeedle]
    adjacency_floor: bool
    tiers: list[list[str]]


def message_query(q: str) -> MessageQuery:
    """*q* parsed for :func:`message_hits`, with the session search's own rules.

    A leading ``title:`` term only narrows which chats rank, as it does for the
    session search, so the gate reads the rest of the query; a query that is only a
    ``title:`` term has no needles, and its chats list no messages. Nor does one
    whose rest holds only ``-exclusions``: with no term to find, every message
    without the excluded word would pass the gate.
    """
    scope = history_search.split_title_scope(q)
    content_q = scope.rest if scope is not None else q
    needles, _phrase, adjacency_floor = history_search.parse_search_query(content_q)
    if all(n.excluded for n in needles):
        needles = []
    return MessageQuery(needles, adjacency_floor, history_search.snippet_needle_tiers(content_q))


def _iso_epoch(ts: object) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def message_hits(
    messages: Sequence[Any],
    query: MessageQuery,
    roles: frozenset[str],
    since: float | None = None,
    max_hits: int = MAX_HITS_PER_SESSION,
) -> tuple[list[dict], int]:
    """``(hits, hit_count)`` for the messages of one transcript that match *query*.

    A message is a hit when its string ``content`` passes the session search's own
    gate on its own (``needles_match_text``: every required term, the adjacency
    floor, no excluded term), its role is in *roles*, and, with *since*, its ``ts``
    is not older. ``index`` is the message's position in *messages*, which callers
    pass as the paging corpus (``read_messages_chained_full``). Hits are in
    transcript order; only the first *max_hits* carry an excerpt, all are counted.
    """
    hits: list[dict] = []
    count = 0
    if not query.needles:
        return hits, count
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") not in roles:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            continue
        if since is not None:
            at = _iso_epoch(msg.get("ts"))
            if at is not None and at < since:
                continue
        if not history_search.needles_match_text(
            query.needles, content.casefold(), query.adjacency_floor
        ):
            continue
        count += 1
        if len(hits) < max_hits:
            # Redacted whole before it is cut: a credential split by the excerpt's
            # edge loses the shape the redactor recognizes.
            text = redact(content)
            anchor = history_search.tiered_first_hit([text], query.tiers)
            pos, needle = (anchor[1], anchor[2]) if anchor else (0, "")
            hit: dict[str, Any] = {
                "index": index,
                "role": msg["role"],
                "snippet": _excerpt(text, pos, needle),
            }
            if isinstance(msg.get("ts"), str):
                hit["ts"] = msg["ts"]
            meta = msg.get("meta")
            if isinstance(meta, dict) and isinstance(meta.get("mid"), str):
                hit["mid"] = meta["mid"]
            hits.append(hit)
    return hits, count


def _read_chain(
    log: ConversationLog, key: str, chain: list[str], keys: _AppOwnedKeys | None
) -> list[dict] | None:
    """*key*'s paging corpus, or None when it cannot be read for this caller.

    An app reads a chain only when it owns every transcript in it. That verdict and
    the read run under one hold of the chain's transcript locks, as
    ``app_scope._app_owned_messages`` does for one key, so a delete and same-key
    recreate cannot land between them and hand the app a transcript it does not own.
    """
    try:
        if keys is None:
            return log.read_messages_chained_full(key)
        if any(k not in keys for k in chain):
            return None
        stems = [stem for k in chain for stem in transcript_lock_stems(k)]
        with log.locked_stems(stems):
            if (log.chained_keys(key) or [key]) != chain or any(k not in keys for k in chain):
                return None
            return log.read_messages_chained_full(key)
    except (OSError, HistoryLockTimeout):
        return None


def _search(
    log: ConversationLog,
    q: str,
    request_app: str,
    agent: str,
    since: float | None,
    roles: frozenset[str],
    limit: int,
) -> tuple[list[dict], object, list[str]]:
    """Ranked, filtered sessions with their hits, and the Bots of every ranked chat.

    The Bot list comes from the whole ranked window, not the listed rows, so a Bot
    whose chats rank below the first ``limit`` can still be chosen as a filter.
    Blocking file IO; off the loop.
    """
    keys = _AppOwnedKeys(log, request_app) if request_app else None
    # The whole scan window, as the other post-filtering readers ask for it
    # (``search_candidate_cap``): the Bot, date and speaker filters run after the
    # ranking, so any smaller pull could hide a qualifying chat ranked below it.
    # The loop below still stops once ``limit`` chats are listed. Keyword ranking
    # alone: this lists the messages that hold the query, which a meaning row need not.
    ranked = log.search_sessions(
        q, search_candidate_cap(q), keys=keys, snippets=False, by_meaning=False
    )
    agents = sorted({r["agent"] for r in ranked if isinstance(r.get("agent"), str) and r["agent"]})
    query = message_query(q)
    if not query.needles:
        return [], ranked, agents
    out: list[dict] = []
    # File stems already listed as part of an earlier row's tab chain: a chain is
    # read whole, so its other members would list the same messages again. Held as
    # stems because ranked rows carry the stem (`dashboard_chat-x`) while the tab
    # index spells the same transcript `dashboard:chat-x`.
    listed: set[str] = set()
    for row in ranked:
        if len(out) >= limit:
            break
        if not _keeps_session(row, agent, since):
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key or transcript_stem(key) in listed:
            continue
        try:
            chain = log.chained_keys(key) or [key]
        except OSError:
            continue
        messages = _read_chain(log, key, chain, keys)
        if messages is None:
            continue
        listed.update(transcript_stem(k) for k in chain)
        hits, count = message_hits(messages, query, roles, since)
        if not hits:
            continue
        result = {f: row[f] for f in _ROW_FIELDS if f in row}
        result["hits"] = hits
        result["hit_count"] = count
        out.append(result)
    return out, ranked, agents


def _redacted(rows: Iterable[dict]) -> None:
    """Redact each row's title; hit excerpts are cut from redacted text already."""
    for row in rows:
        if row.get("title"):
            row["title"] = redact(row["title"])


async def api_sessions_search_messages(request: web.Request) -> web.Response:
    """GET /api/sessions/search/messages — the messages that match, across chats.

    Query params:
      - ``q``: search string, the same syntax as ``/api/sessions/search``
        (at least ``SEARCH_MIN_CHARS``; shorter returns no results)
      - ``agent``: keep only sessions whose agent is exactly this name
      - ``role``: ``user`` or ``assistant``; empty keeps both
      - ``since``: keep sessions last modified at or after this (epoch s), and drop
        each hit whose ``ts`` is older
      - ``limit``: most sessions (default and max ``MAX_SESSIONS``)

    Returns ``{results: [{key, title, agent, modified, open?, hits, hit_count}],
    agents, scan?}``, ``agents`` being the Bots of every chat the ranking matched.
    Each hit is ``{index, role, ts?, mid?, snippet}``; ``ts`` and ``mid`` are what
    the chat deep link (``?msg=`` and ``?mid=``) resolves, and ``index`` addresses the
    slot-detail paging corpus. Titles and excerpts are redacted. An app caller
    searches only the transcripts it owns, as ``/api/sessions/search`` does.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"results": [], "agents": []})
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    role = request.query.get("role", "").strip()
    if role not in ROLE_FILTERS:
        return web.json_response(
            {"error": "role must be user or assistant", "code": "invalid_role"}, status=400
        )
    agent = sanitize_string(request.query.get("agent", "")).strip()[:128]
    since = _epoch_param(request, "since")
    if since is _BAD_TIME:
        return web.json_response(
            {"error": "since must be a time in seconds", "code": "invalid_since"}, status=400
        )
    if len(q) < SEARCH_MIN_CHARS:
        return web.json_response({"results": [], "agents": []})
    request_app = str(request.get("app") or "")
    results, ranked, agents = await asyncio.to_thread(
        _search,
        state.conversation_log,
        q,
        request_app,
        agent,
        since if isinstance(since, float) else None,
        ROLE_FILTERS[role],
        _limit_param(request),
    )
    if request_app:
        _audit_app_allow(request_app, "session_message_search", f"sessions={len(results)}")
    else:
        _mark_open_sessions(results, state)
    _redacted(results)
    body: dict[str, object] = {"results": results, "agents": agents}
    coverage = search_coverage(ranked)
    if coverage is not None:
        body["scan"] = {"searched": coverage[0], "total": coverage[1]}
    return web.json_response(body)


__all__ = [
    "MAX_HITS_PER_SESSION",
    "MAX_SESSIONS",
    "ROLE_FILTERS",
    "api_sessions_search_messages",
    "message_hits",
    "message_query",
]

# An owner surface like every ``api_session*`` history read: the history is the
# owner's own conversation (see ``history_reads``).
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(),
    dashboard_owner_only=True,
)
