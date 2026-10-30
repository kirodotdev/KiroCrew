"""Reading the session history: the Older-sessions list, search, one session, the archive.

``GET /api/sessions`` (paginated, with opt-in filters that drop open-slot and machine-run
rows and a lookup by short slot name), ``GET /api/sessions/search``,
``GET /api/sessions/{key}`` and its ``/meta`` probe, each judged per resource for an app
caller through :mod:`.app_scope`, and the consolidated archive (``GET /api/session/archive``
and ``/api/session/archive/{name}``), redacted before it is served. New read-only history
routes go here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable

from aiohttp import web

# circular import: handlers/__init__.py imports this family through the facade while
# the package is still initialising, so a `from ... import` of individual names would
# fail mid-cycle. `import ... as` binds via sys.modules and defers attribute access to
# call time, which also keeps tests' patches of ``handlers.<name>`` effective.
import kiro_crew.dashboard.handlers as _h
from kiro_crew.dashboard.handlers._shared import (
    SESSION_SEARCH_TEXT_FIELDS,
    guard_owner_surface_routes,
)
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import (
    _NOT_TRANSCRIPT_OWNER,
    _app_not_found,
    _app_owned_messages,
    _app_owned_sessions,
    _app_owns_transcript,
    _app_transcript_busy,
    _app_transcript_refusal,
    _audit_app_allow,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    SEARCH_MIN_CHARS,
    ConversationLog,
    HistoryLockTimeout,
    _archive_dir,
    transcript_stems,
)
from kiro_crew.messaging.link import _in_namespace
from kiro_crew.security import redact
from kiro_crew.validation import sanitize_string

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


def _open_slot_transcript_keys(state: DashboardState) -> set[str]:
    """Every transcript key and filename stem a live slot could be reading.

    A session in this set is reachable as an open tab. That single fact drives
    two callers: the bulk delete must not touch it, and the Older-sessions list
    must not repeat it (that list is the complement of the open tabs above it).

    Resolved FROM the slot, never derived from its name. A channel-born slot's
    transcript is its ``linked_session_key`` (``slack:<ts>``), so a hand-built
    ``dashboard:<slot>`` name would miss every channel tab. ``list_sessions``
    reports filename STEMS, so each candidate contributes its key and every
    stem it can occupy. The latter includes the pre-migration bare Slack stem;
    protecting only ``slack_<ts>`` would let Clear All unlink ``<ts>`` while
    that same open tab was still reading it.

    Both candidates are included because a slot's write target and its DISPLAY
    source can differ: a channel tab the dashboard could not bind runs under
    ``dashboard:<stem>`` while the conversation on screen lives in the channel
    transcript. Choosing between them would make the answer depend on provenance
    resolving correctly, and provenance is exactly what a legacy transcript
    cannot supply. Both names belong to the SAME slot, so covering both is safe
    in either direction — it can only protect, or hide, a transcript that one
    slot could itself be showing.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key, slot_transcript_key

    keys: set[str] = set()
    # Snapshot the values: a concurrent turn can add or remove a slot while this
    # iterates, and a dict mutated mid-iteration raises.
    for slot in list(state._slots.values()):
        for candidate in (slot_history_key(slot), slot_transcript_key(slot.key)):
            keys.add(candidate)
            keys.update(transcript_stems(candidate))
    return keys


#: Session namespaces whose transcripts are a machine run rather than a conversation
#: anyone addressed. This membership is a PRESENTATION judgement for one pane, not a
#: shared roster: two other modules carry similar-looking tuples that answer different
#: questions, and none of the three agree.
#:
#: * ``handlers/_shared_session_memory.py`` — colon-only, wf-family only, to dispatch a memory-mode
#:   lookup.
#: * ``handlers/cron_caller_session.py`` — colon-only, wf-family plus ``subagent``, to answer
#:   whether a session is live and shared.
#: * this one — matches PERSISTED folded keys via ``_in_namespace``, adds ``secretary``
#:   and ``channel``, and deliberately excludes ``cron``, ``side`` and ``taskrunner``
#:   because each of those holds conversations a reader started.
#:
#: So do not hoist the three into one constant: it would force a shared meaning none of
#: them has, and the next namespace would have to be correct for all three at once.
#:
#: It is spelled literally rather than derived from
#: ``messaging.link._TELEMETRY_LOCAL_PREFIXES`` because that registry exists to bound
#: telemetry label cardinality, and ``wf-unpooled`` and ``wf-worker`` are live session
#: keys absent from it — a derived filter cannot see them.
#:
#: Absent on purpose:
#:
#: * ``dashboard`` and the channel namespaces (``slack``, ``discord``, …) — the
#:   reader's own conversations.
#: * ``cron`` — a job without ``hide_in_chat`` backs a real chat slot, and the key
#:   does not record which kind wrote it.
#: * ``side`` — the slot's own side panel, which the reader typed into; ``sel.py``
#:   attributes ``side:`` to the ``dashboard`` surface for that reason.
#: * ``taskrunner`` — ``POST /api/taskrunner/{id}/to-chat`` opens a real chat slot on
#:   ``taskrunner:<task_id>:chat:<token>`` and titles it ``Plan: <task_id>``, so the
#:   namespace holds conversations as well as runs and the key cannot reliably tell
#:   them apart. Listing a plain ``taskrunner_`` run is the declared residual, and it
#:   is the safe one: showing a machine row costs less than hiding a conversation.
#:
#: Anything not named here stays listed, which is the direction every residual in
#: this filter points.
_MACHINE_NAMESPACES: tuple[str, ...] = (
    # fmt: off
    "subagent", "secretary", "channel",
    "wf", "wf-pool", "wf-unpooled", "wf-worker", "wf-author"
    # fmt: on
)


def _is_machine_only_session(key: str) -> bool:
    """True when *key* sits in a namespace no user ever addressed directly.

    Matched through ``_in_namespace`` because this reads a PERSISTED name:
    ``history._safe_key`` folds ``subagent:<id>`` to the stem ``subagent_<id>``, so
    the colon spelling every other subagent guard in the tree uses can never match
    here. That fold is the whole defect this filter repairs.
    """
    return any(_in_namespace(key, ns) for ns in _MACHINE_NAMESPACES)


def _has_user_content(log: object, row: dict) -> bool:
    """True when *row* has something a reader could resume: a title or a message.

    ``list_sessions`` titles a session from its metadata or its first user
    message, and falls back to the key itself. A titled row is kept without a
    read, even when it has no messages: a title is a user signal. Only a row
    whose title IS its key pays for a file read.
    """
    key = row.get("key", "")
    if row.get("title", key) != key:
        return True
    try:
        return bool(log.has_messages(key))  # type: ignore[attr-defined]
    except OSError:
        # Unreadable is not empty: never hide what could not be read.
        return True


def _is_user_listed_row(log: object, row: dict) -> bool:
    """True when the ``user_only`` listing shows *row*.

    ONE predicate, shared by ``GET /api/sessions?user_only=1`` and the
    ``user_only`` bulk clear, so the Older Sessions "Delete all" removes only
    rows the reader could see in that pane.
    """
    return not _is_machine_only_session(row.get("key", "")) and _has_user_content(log, row)


_SHORT_SLOT_NAME_RE = re.compile(r"chat-\d+")
_DASHBOARD_SLOT_STEM_RE = re.compile(r"dashboard_(chat-\d+)-(\d+)")


def _sessions_named(
    canonical_key: Callable[[str], str], sessions: list[dict], name: str
) -> list[dict]:
    """The rows of *sessions* minted under the short slot name *name*, oldest first.

    A row matches when its canonical transcript stem is ``dashboard_<name>-<ts>``.
    Anything that is not a short name (a full key, a channel key, junk) matches
    nothing, so the filter can never widen the list it narrows.
    """
    if not _SHORT_SLOT_NAME_RE.fullmatch(name):
        return []
    matched: list[tuple[int, dict]] = []
    for row in sessions:
        m = _DASHBOARD_SLOT_STEM_RE.fullmatch(canonical_key(row.get("key", "")))
        if m and m.group(1) == name:
            matched.append((int(m.group(2)), row))
    matched.sort(key=lambda pair: pair[0])
    return [row for _, row in matched]


async def api_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions — list conversation session files.

    Query params:
      - ``limit``: max sessions to return (default 50, max 200)
      - ``offset``: skip first N sessions (default 0)
      - ``preview``: when truthy, attach a redacted last-message ``preview``
        to each returned session (bounded tail read; page-scoped so the
        default list stays a cheap metadata scan)
      - ``exclude_open``: when truthy, drop sessions a live slot already holds
        open. Opt-in, not the default: the full inventory is what the memory
        "Consolidate all" action and the command palette's recents read, and
        both would silently skip the user's active conversations if this
        endpoint decided on their behalf. Only the caller rendering the
        complement of the open tabs asks for it.
      - ``user_only``: when truthy, drop sessions whose key is in a machine-only
        namespace (see :data:`_MACHINE_NAMESPACES`). Opt-in for the same reason as
        ``exclude_open``: a subagent or workflow transcript is still a session the
        memory-consolidation and recents callers must see. Only the sidebar's
        Older-sessions pane asks, because it is the surface that presents these
        rows as a LIST OF CONVERSATIONS — and a machine transcript carries no
        title, so it renders its own storage key as the row label there.
        The same flag drops a NEVER-USED session: no title and no row besides
        its metadata line. That is a tab opened and closed without a message
        (for example the blank tab the chat page opens when no tab is left).
        It has nothing to resume, and it too would show its storage key.
      - ``name``: a dashboard session's SHORT name (``chat-7``). Keeps only the
        dashboard transcripts minted under that name, open or closed, oldest
        first by the mint time in the key. A slot number is reused across
        gateway generations, so one name can match several sessions. Any other
        value matches nothing. This is how a ``/chat?sid=chat-7`` link finds
        the session it names.

    Returns ``{sessions, total, has_more}`` for pagination.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"sessions": [], "total": 0, "has_more": False})
    # Both bounds are floored, not just capped: the page below is a Python list
    # slice, where a negative bound counts from the END: ``offset=-3`` would read
    # the far end of the list, and ``limit=-5`` would return every row but five,
    # past the 200-row cap.
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0
    want_preview = (request.query.get("preview") or "").lower() in ("1", "true", "yes")
    exclude_open = (request.query.get("exclude_open") or "").lower() in ("1", "true", "yes")
    user_only = (request.query.get("user_only") or "").lower() in ("1", "true", "yes")
    # list_sessions() globs, stats, and reads the first line of EVERY session file
    # in the history dir — O(all sessions). At 2000 sessions, that's ~200 ms of
    # blocking IO (measured: 208 ms / 2000 files on a dev host). Running that on
    # the event loop freezes chat, heartbeat, and every other coroutine for the
    # full duration. Offload to a worker thread.
    all_sessions = await asyncio.to_thread(state.conversation_log.list_sessions)
    if "name" in request.query:
        # Before the app filter, which reads each row's transcript under its lock.
        all_sessions = _sessions_named(
            state.conversation_log._canonical_key, all_sessions, request.query["name"]
        )
    request_app = str(request.get("app") or "")
    if request_app:
        # Before every count, so ``total`` and ``has_more`` describe the app's
        # own list rather than the size of everyone else's.
        all_sessions = await asyncio.to_thread(
            _app_owned_sessions, state.conversation_log, request_app, all_sessions
        )
        _audit_app_allow(request_app, "session_list", f"sessions={len(all_sessions)}")
    if exclude_open:
        open_keys = _open_slot_transcript_keys(state)
        # Fold through ``_canonical_key`` as well: ``list_sessions`` deduplicates
        # by canonical name but reports the RAW stem of whichever file won on
        # mtime, so a resume round-trip's ``dashboard_dashboard_<name>`` file
        # reaches here under a name no slot ever produces. Without the fold that
        # session is listed as a second, separate conversation.
        canon = state.conversation_log._canonical_key
        all_sessions = [
            s
            for s in all_sessions
            if s.get("key", "") not in open_keys and canon(s.get("key", "")) not in open_keys
        ]
    if user_only:
        log = state.conversation_log

        def _user_rows(sessions: list[dict]) -> list[dict]:
            if request_app:
                owned: list[dict] = []
                for row in sessions:
                    key = row.get("key", "")
                    if _is_machine_only_session(key):
                        continue
                    try:
                        with log._locked(key):
                            if _app_owns_transcript(log, request_app, key) and _has_user_content(
                                log, row
                            ):
                                owned.append(row)
                    except HistoryLockTimeout:
                        continue
                return owned
            return [s for s in sessions if _is_user_listed_row(log, s)]

        all_sessions = await asyncio.to_thread(_user_rows, all_sessions)
    # Count AFTER the exclusion so the page, ``total`` and ``has_more`` describe
    # one list. The client advances its offset by the number of rows it received,
    # so filtering on its side instead would skip or repeat rows across pages.
    total = len(all_sessions)
    page = all_sessions[offset : offset + limit]
    if want_preview:
        log = state.conversation_log

        def _attach_previews(sessions: list[dict]) -> None:
            def _sanitize(text: str) -> str:
                # Injected so redaction runs BEFORE the preview's length cap:
                # a credential split by truncation leaves a partial token the
                # patterns cannot match, letting its raw prefix through.
                text, _ = _h.redact_exfiltration_urls(text)
                text, _ = _h.redact_credentials(text)
                return text

            if request_app:
                owned: list[dict] = []
                for row in sessions:
                    key = row.get("key", "")
                    try:
                        # Keep the verdict, metadata and preview in one incarnation.
                        with log._locked(key):
                            if not _app_owns_transcript(log, request_app, key):
                                continue
                            current = log.list_sessions(keys=(key,))
                            if not current:
                                continue
                            row = current[0]
                            preview = log.last_message_preview(key, sanitize=_sanitize)
                            if preview:
                                row["preview"] = preview
                            owned.append(row)
                    except HistoryLockTimeout:
                        continue
                sessions[:] = owned
                return
            for s in sessions:
                preview = log.last_message_preview(s.get("key", ""), sanitize=_sanitize)
                if preview:
                    s["preview"] = preview

        # Tail reads are sync file IO — keep them off the event loop.
        page_size = len(page)
        await asyncio.get_running_loop().run_in_executor(None, _attach_previews, page)
        if request_app:
            total -= page_size - len(page)
    return web.json_response(
        {
            "sessions": page,
            "total": total,
            "has_more": offset + (len(page) if request_app else limit) < total,
        }
    )


class _AppOwnedKeys:
    """The transcript keys *request_app* owns, judged per lookup.

    Lazy, so a search reads the owner marker of only the keys in its own scan
    window instead of listing and judging every transcript up front.
    """

    def __init__(self, log: ConversationLog, request_app: str) -> None:
        self._log = log
        self._request_app = request_app

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and _app_owns_transcript(self._log, self._request_app, key)


def _app_search(log: ConversationLog, request_app: str, q: str, limit: int) -> list[dict]:
    """*q* ranked over only the transcripts *request_app* owns. Blocking; off the loop."""
    return log.search_sessions(q, limit, keys=_AppOwnedKeys(log, request_app))


async def api_sessions_search(request: web.Request) -> web.Response:
    """GET /api/sessions/search — content search over session JSONL files.

    Query params:
      - ``q``: search string (min 2 chars; empty returns no results)
      - ``limit``: max results (default 50, max 200)

    Returns ``{sessions}`` — same metadata shape as:func:`api_sessions`.
    Session titles may be LLM-generated and are redacted before return.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"sessions": []})
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    if len(q) < SEARCH_MIN_CHARS:
        return web.json_response({"sessions": []})
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50
    request_app = str(request.get("app") or "")
    if request_app:
        # The ownership filter runs inside the ranking, before the limit cap, so
        # other sessions' matches cannot crowd the app's off the page.
        log = state.conversation_log
        sessions = await asyncio.to_thread(_app_search, log, request_app, q, limit)
        _audit_app_allow(request_app, "session_search", f"sessions={len(sessions)}")
    else:
        sessions = await asyncio.get_running_loop().run_in_executor(
            None, state.conversation_log.search_sessions, q, limit
        )
    for s in sessions:
        for field in SESSION_SEARCH_TEXT_FIELDS:
            value = s.get(field)
            if value:
                s[field] = redact(value)
    return web.json_response({"sessions": sessions})


async def api_session_detail(request: web.Request) -> web.Response:
    """GET /api/sessions/{key} — return messages for a session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    refusal = await _app_transcript_refusal(request, state, key, "session_detail")
    if refusal is not None:
        return refusal
    if not state.conversation_log:
        return web.json_response([])
    log = state.conversation_log
    request_app = str(request.get("app") or "")
    if request_app:
        # Re-judged with the read under one lock hold: the check above is a
        # snapshot a same-key delete and recreate can outlive.
        try:
            messages = await asyncio.to_thread(_app_owned_messages, log, request_app, key)
        except HistoryLockTimeout:
            return _app_transcript_busy()
        if messages is None:
            return _app_not_found(
                request_app, "session_detail", f"session={key}", _NOT_TRANSCRIPT_OWNER
            )
        _audit_app_allow(request_app, "session_detail", f"session={key}")
        return web.json_response(messages)
    # read_messages() opens and parses the transcript on a cache miss, which for
    # the multi-MB sessions a long-lived store accumulates is 100-300 ms of
    # blocking file IO — on the event loop, stalling every other request. Off the
    # loop, like every other conversation_log read of the session routes
    # (list_sessions, get_metadata, session_mtime, search_sessions, delete_session).
    messages = await asyncio.to_thread(state.conversation_log.read_messages, key)
    return web.json_response(messages)


def _session_meta_row(log: ConversationLog, request_app: str, key: str) -> dict | None:
    """The ``GET /api/sessions`` row for the ONE transcript *key* names, else None.

    *key* may be a transcript stem (``dashboard_chat-7-<ts>``) or the slot key a
    chat link carries (``chat-7-<ts>``), whose transcript lives under its
    ``dashboard:`` key. Each spelling is checked directly, with no directory
    scan: a stat and the cached first line, plus, for a transcript with no
    title on that line, the bounded title fallback ``list_sessions`` reads (its
    first 20 lines). The rest of the transcript is never read. An app caller sees
    only a transcript it owns, the same rule every other ``/api/sessions*`` read
    applies, and the verdict and the row are read under one hold of that
    transcript's lock, so a same-key delete and recreate cannot land between
    them. Blocking file IO; call it off the event loop. A lock timeout raises
    ``HistoryLockTimeout``.
    """
    from kiro_crew.dashboard.chat_utils import slot_transcript_key

    for candidate in dict.fromkeys((key, slot_transcript_key(key))):
        if not log.has_log(candidate):
            continue
        if not request_app:
            rows = log.list_sessions(keys=(candidate,))
            if rows:
                return rows[0]
            continue
        # Unlocked first, so no lock sidecar is created for a key the app has no
        # claim on; then again with the read, under the lock.
        if not _app_owns_transcript(log, request_app, candidate):
            continue
        with log._locked(candidate):
            if not _app_owns_transcript(log, request_app, candidate):
                continue
            rows = log.list_sessions(keys=(candidate,))
        if rows:
            return rows[0]
    return None


async def api_session_meta(request: web.Request) -> web.Response:
    """GET /api/sessions/{key}/meta: one session's key and title, or 404 when it has none.

    The cheap per-key probe a chat link needs to tell "this session exists on
    disk" from "no such session" without paging the whole list or reading the
    whole transcript (see :func:`_session_meta_row` for what it reads). ``key`` in the body is the transcript stem, which is what a
    resume takes. A missing key, and for an app caller a transcript it does not
    own, both get the uniform 404, so the answer reveals nothing beyond what
    ``GET /api/sessions`` does.
    """
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    request_app = str(request.get("app") or "")
    log = state.conversation_log
    row = None
    if log is not None:
        try:
            row = await asyncio.to_thread(_session_meta_row, log, request_app, key)
        except HistoryLockTimeout:
            return _app_transcript_busy()
    if row is None:
        if request_app:
            return _app_not_found(
                request_app, "session_meta", f"session={key}", _NOT_TRANSCRIPT_OWNER
            )
        from kiro_crew.dashboard.chat_handlers import _slot_not_found

        return _slot_not_found()
    if request_app:
        _audit_app_allow(request_app, "session_meta", f"session={key}")
    return web.json_response({"key": row.get("key", key), "title": row.get("title", key)})


async def api_session_archive_list(request: web.Request) -> web.Response:
    """GET /api/session/archive?key=... — list archive files for a session key."""
    from typing import Any

    from kiro_crew.history import _archive_dir, _safe_key

    key = request.query.get("key", "").strip()
    adir = _archive_dir()
    if not adir.exists():
        return web.json_response({"archives": []})
    prefix = f"{_safe_key(key)}__" if key else ""

    def _collect() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for p in adir.glob(f"{prefix}*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            stem = p.stem
            # Archive filenames use '__' delimiter: {safekey}__{stamp}.jsonl
            sep = stem.find("__")
            safekey = stem[:sep] if sep >= 0 else stem
            stamp = stem[sep + 2 :] if sep >= 0 else ""
            items.append(
                {
                    "name": p.name,
                    "key": safekey,
                    "stamp": stamp,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    items = await asyncio.to_thread(_collect)
    return web.json_response({"archives": items})


async def api_session_archive_read(request: web.Request) -> web.Response:
    """GET /api/session/archive/{name} — read a single archive file as JSONL text."""
    name = request.match_info.get("name", "")
    if not name.endswith(".jsonl"):
        return web.json_response({"error": "invalid archive name"}, status=400)
    adir = _archive_dir().resolve()
    try:
        resolved = (adir / name).resolve()
    except (OSError, RuntimeError, ValueError):
        return web.json_response({"error": "invalid archive name"}, status=400)
    # Canonical path check: file must be a direct child of the archive dir.
    if resolved.parent != adir:
        return web.json_response({"error": "invalid archive name"}, status=400)

    def _read_capped(p: Path, limit: int = 250_000) -> str:
        with p.open(encoding="utf-8") as f:
            data = f.read(limit)
        # Truncate at last newline to keep NDJSON valid.
        if len(data) == limit:
            nl = data.rfind("\n")
            if nl > 0:
                data = data[: nl + 1]
        return data

    try:
        raw = await asyncio.to_thread(_read_capped, resolved)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read archive %s: %s", name, exc)
        return web.json_response({"error": "unreadable archive"}, status=422)
    # Archives contain LLM output; redact credentials and exfiltration URLs before serving.
    redacted = await asyncio.to_thread(lambda: redact(raw))
    return web.Response(text=redacted, content_type="application/x-ndjson")


# Every ``api_session*`` handler is an owner surface, so a private member's
# internal call is refused before it runs (audit label = handler name). The
# history is the owner's own conversation, so a non-owner dashboard session (a
# channel allowlist link, same empty app claim) is refused too.
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(),
    dashboard_owner_only=True,
)
