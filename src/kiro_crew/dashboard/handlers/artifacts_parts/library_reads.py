"""The read routes: the library list and its search, an artifact's detail and asset,
its versions and its lifecycle event log.

``GET /api/artifacts`` filters, scans and ranks the library; its search snippets are
built from stripped content under the content and snippet caches kept here.
"""

from __future__ import annotations

import asyncio
import functools
import re
import threading
from typing import Any

from aiohttp import web

from kiro_crew.artifacts import (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_store,
)
from kiro_crew.constants import md_link_destination
from kiro_crew.dashboard.chat_folders import (
    archived_session_names_in_folders,
    chat_folder_subtree_ids,
    live_slot_names_in_folders,
)
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import (
    _redact_text,
    _resolve_session_title,
    _serialize,
)
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _audit,
    _clean_origin_session_key,
    _err,
    _event_session_id,
    _json_response,
    _run_off_loop,
    logger,
)
from kiro_crew.executors import subprocess_executor


def _clean_pinned_filter(raw: Any) -> bool | None:
    """Parse a ``?pinned=`` query value into a tri-state filter.

    ``None`` (absent or unrecognized) = don't scope. Recognized truthy/falsy
    spellings map to ``True`` / ``False``. An unrecognized value must NOT be
    read as ``False`` — that would silently return only unpinned artifacts for
    e.g. ``?pinned=yep``, which reads as a filter failure rather than a typo.
    """
    if not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return None


#: Max length of a content preview snippet returned by the list endpoint when
#: ``?snippet=1`` is passed. Kept short so the list payload stays lean.
_SNIPPET_MAX_LEN = 160

#: Max accepted length for the ?q search string. Anything longer is truncated —
#: the scan substring-matches q against every artifact's full content, so an
#: unbounded query multiplies work for no legitimate use case.
_SEARCH_QUERY_MAX_CHARS = 256
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")
# Lightweight markdown → prose cleanup for previews (not a full parser).
# [text](url) / ![alt](url) -> text; a balanced ``(...)`` pair stays inside the url,
# and a label stops at the next ``[`` so a run of ``[`` cannot rescan the text.
_MD_LINK_RE = re.compile(rf"!?\[([^\[\]]*)\]\({md_link_destination('[^()]')}*\)")
_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")  # # headings
_MD_BLOCKQUOTE_RE = re.compile(r"(?m)^\s*>\s?")  # > quotes
_MD_LIST_RE = re.compile(r"(?m)^\s*(?:[-*+]|\d+\.)\s+")  # -, *, 1. list markers
_MD_FENCE_RE = re.compile(r"`{1,3}")  # code ticks/fences
_MD_EMPHASIS_RE = re.compile(r"[*_~]")  # bold/italic/strike markers


def _load_content(store: Any, slug: str) -> str:
    """Best-effort read of an artifact's current content ('' on any failure)."""
    try:
        return store.get(slug).content or ""
    except (ArtifactError, OSError):
        return ""


def _clean_markdown(text: str) -> str:
    """Strip HTML tags + common markdown syntax, preserving line breaks."""
    text = _STRIP_TAGS_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BLOCKQUOTE_RE.sub("", text)
    text = _MD_LIST_RE.sub("", text)
    text = _MD_FENCE_RE.sub("", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    return text


def _strip_content(content: str) -> str:
    """Plain, readable single-line prose (markdown/HTML stripped, whitespace
    collapsed) for the default preview snippet and content matching."""
    return " ".join(_clean_markdown(content).split())


def _snippet_from(stripped: str) -> str:
    """Redacted, truncated display snippet from already-stripped text.

    Redacts over the FULL text, then trims to ``_SNIPPET_MAX_LEN``. A bounded
    pre-redaction window can cut a credential at its edge into fragments no
    redaction regex matches, and redaction shrinking earlier text can slide
    such a fragment into the final snippet. The full-text pass is expensive,
    so list scans call this through :func:`_cached_snippet` (memoized per
    artifact version) and always from the scan's executor thread, keeping it
    off the event loop.
    """
    head = _redact_text(stripped).strip()
    return head[:_SNIPPET_MAX_LEN]


#: Max lines in a match-centered context snippet, and max chars per line.
_CONTEXT_MAX_LINES = 5
_CONTEXT_LINE_LEN = 160


def _context_snippet(content: str, q_lower: str) -> str:
    """A match-centered preview: the line containing *q_lower* plus up to two
    lines before and after (``_CONTEXT_MAX_LINES`` total), markdown-cleaned and
    newline-joined so the matched term is always shown in context. Falls back to
    the prefix snippet when the match is in the name/tags/description (not the
    body). Redacted like the rest of the content path.
    """
    lines = [" ".join(ln.split()) for ln in _clean_markdown(content).splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    idx = next((i for i, ln in enumerate(lines) if q_lower in ln.lower()), -1)
    if idx == -1:
        # Match came from name/tags/description — no body line to center on.
        return _snippet_from(" ".join(lines))
    start = max(0, idx - 2)
    window = lines[start : idx + 3][:_CONTEXT_MAX_LINES]
    # Redact over the FULL window lines, then bound each line: cutting a line
    # first can split a credential at the cut into fragments no redaction regex
    # matches (same class as the prefix snippet above). Redaction tags carry no
    # newlines, so the line structure survives the pass.
    redacted = _redact_text("\n".join(window))
    return "\n".join(ln[:_CONTEXT_LINE_LEN] for ln in redacted.splitlines())


# ── List ─────────────────────────────────────────────────────────────────────


#: Cache of loaded+stripped artifact content, keyed by slug. The cache key
#: tuple is (version, updated_at) — version bumps on every content change,
#: so a stale entry can never be served. Bounded TWO ways: a per-item size cap
#: keeps huge bodies read-through (never cached), and a cumulative byte budget
#: drops the whole cache if churn ever exceeds it (which also ages out entries
#: for deleted artifacts). All access is serialized by
#: :data:`_content_cache_lock` — scans run on executor worker threads, so two
#: concurrent searches would otherwise mutate the dict mid-iteration
#: (guaranteed hazard on free-threaded builds, latent one elsewhere).
_CONTENT_CACHE_MAX_ITEM_BYTES = 256 * 1024
_CONTENT_CACHE_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_content_cache: dict[str, tuple[tuple[int, str], str, str]] = {}
_content_cache_bytes = 0
_content_cache_lock = threading.Lock()

#: Cache of the redacted prefix snippet, keyed by slug under the same
#: (version, updated_at) key as the content cache. The snippet path redacts
#: the FULL stripped body (see :func:`_snippet_from`), which is too expensive
#: to repeat for every listed artifact on every debounced request — memoizing
#: under the version key pays that pass once per artifact version. Entries are
#: at most ``_SNIPPET_MAX_LEN`` chars, but slugs accumulate across
#: create/delete cycles with no other pruning, so a fixed entry cap drops the
#: whole cache when crossed (the same drop-all pressure valve the content
#: cache uses); shares :data:`_content_cache_lock` (same executor-thread
#: access pattern).
_SNIPPET_CACHE_MAX_ENTRIES = 4096
_snippet_cache: dict[str, tuple[tuple[int, str], str]] = {}


def _cached_snippet(a: Any, stripped: str) -> str:
    """The redacted prefix snippet for artifact *a*, memoized per version."""
    key = (a.version, a.updated_at)
    with _content_cache_lock:
        hit = _snippet_cache.get(a.slug)
        if hit and hit[0] == key:
            return hit[1]
    snippet = _snippet_from(stripped)
    with _content_cache_lock:
        if len(_snippet_cache) >= _SNIPPET_CACHE_MAX_ENTRIES:
            _snippet_cache.clear()
        _snippet_cache[a.slug] = (key, snippet)
    return snippet


def _cache_entry_bytes(raw: str, stripped: str) -> int:
    return len(raw) + len(stripped)


def _scan_artifacts(
    store: Any,
    items: list[Any],
    q_lower: str,
    want_snippet: bool,
    do_content: bool,
) -> list[dict[str, Any]]:
    """Content-match + snippet scan over listed artifacts.

    Runs OFF the event loop (sync file IO + regex stripping — see the
    run_in_executor call site). Content reads hit a (version, updated_at)-keyed
    cache so repeated queries (every debounced keystroke) only re-read files
    whose content actually changed.
    """
    global _content_cache_bytes
    # No live-slug pruning here: ``items`` may be a FILTERED subset (?tag=,
    # ?kind=, ?folder=), so evicting everything outside it would thrash the
    # cache on scoped queries. The per-item size cap + cumulative byte budget
    # below already bound growth; deleted artifacts' entries age out via the
    # budget's drop-all valve.
    out: list[dict[str, Any]] = []
    need_content = want_snippet or do_content
    for a in items:
        raw = ""
        stripped = ""
        if need_content:
            cache_key = (a.version, a.updated_at)
            with _content_cache_lock:
                hit = _content_cache.get(a.slug)
            if hit and hit[0] == cache_key:
                raw, stripped = hit[1], hit[2]
            else:
                raw = _load_content(store, a.slug)
                stripped = _strip_content(raw)
                size = _cache_entry_bytes(raw, stripped)
                # Oversized bodies stay read-through; everything else is
                # cached under the cumulative byte budget (blown budget =>
                # drop-all, the simple pressure valve for pathological churn).
                if size <= _CONTENT_CACHE_MAX_ITEM_BYTES:
                    with _content_cache_lock:
                        old = _content_cache.get(a.slug)
                        if old:
                            _content_cache_bytes -= _cache_entry_bytes(old[1], old[2])
                        _content_cache[a.slug] = (cache_key, raw, stripped)
                        _content_cache_bytes += size
                        if _content_cache_bytes > _CONTENT_CACHE_MAX_TOTAL_BYTES:
                            _content_cache.clear()
                            _content_cache_bytes = 0
        if do_content:
            hay = f"{a.name} {' '.join(a.tags)} {a.description} {stripped}".lower()
            if q_lower not in hay:
                continue
        d = _serialize(a)
        if want_snippet:
            # Match-centered context for content queries; prefix otherwise.
            d["snippet"] = (
                _context_snippet(raw, q_lower)
                if (do_content and q_lower)
                else _cached_snippet(a, stripped)
            )
        out.append(d)
    return out


async def _chat_folder_session_names(state: Any, folder_id: str) -> set[str]:
    """Slot names of every session filed in chat folder *folder_id* or under it.

    Empty for no dashboard state, an unknown id, or a folder subtree with no
    sessions. The open slots are read on the loop that owns the slot table;
    the transcript scan for archived sessions runs on the slow-work pool.
    """
    if state is None:
        return set()
    folder_ids = chat_folder_subtree_ids(state, folder_id)
    if not folder_ids:
        return set()
    names = live_slot_names_in_folders(state, folder_ids)
    names |= await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), archived_session_names_in_folders, state, folder_ids
    )
    return names


async def api_artifacts_list(request: web.Request) -> web.Response:
    tag = request.query.get("tag") or None
    kind = request.query.get("kind") or None
    # Bounded: q feeds a substring scan over every artifact's full content —
    # an unbounded query string is free DoS ammunition.
    q = (request.query.get("q") or "")[:_SEARCH_QUERY_MAX_CHARS] or None
    source = request.query.get("source") or None
    source_path = request.query.get("source_path") or None
    want_snippet = (request.query.get("snippet") or "").lower() in ("1", "true", "yes")
    content_match = (request.query.get("content") or "").lower() in ("1", "true", "yes")
    q_lower = (q or "").lower()
    # ?content=1 broadens ?q from a name-only substring to name + tags + content.
    do_content = content_match and bool(q_lower)
    # ``folder`` scopes the browse view to one folder id. Absent = all folders
    # (unscoped); present-but-empty ("?folder=") = the unfiled/root bucket. We
    # must distinguish the two, so read the raw key rather than ``or None``.
    folder = request.query["folder"] if "folder" in request.query else None
    # ``session`` scopes to the artifacts one chat session produced (the
    # in-session Artifacts tab). Validated through the same grammar as a save's
    # ``origin_session_key`` so a hostile value can't reach the store as a filter.
    #
    # On a validation MISS the raw value is kept rather than collapsed to "".
    # Collapsing is right for a WRITE (attributing a save to no session is safe)
    # but wrong for a READ filter: "" is the real no-origin bucket, so an
    # over-long key would return some OTHER session's artifacts — e.g. every
    # ``artifact_save`` from the MCP path, which stores ``session_key=""``. A slot
    # key can exceed the 128-char limit today: the artifact companion-chat flow
    # names a slot ``Artifact: <name>`` and names run to ``MAX_NAME_LEN`` (200).
    # ``store.list`` compares exactly, so the raw over-long key matches zero
    # records — an honestly empty tab instead of a foreign one.
    _raw_session = request.query.get("session")
    session = (
        (_clean_origin_session_key(_raw_session) or _raw_session)
        if "session" in request.query
        else None
    )
    pinned = _clean_pinned_filter(request.query.get("pinned"))
    # ``touched_by`` is the involvement scope (origin OR any event this session
    # left): what the in-session Artifacts tab lists as "this session", so an
    # artifact the agent merely READ or ITERATED ON shows up, not just ones it
    # authored. Same raw-on-miss handling as ``session`` above and for the same
    # reason — but with no empty-bucket case, since an empty value here means
    # "don't scope" in the store rather than "no session ever touched it".
    _raw_touched = request.query.get("touched_by")
    touched_by = (_clean_origin_session_key(_raw_touched) or _raw_touched) if _raw_touched else None
    # ``chat_folder`` scopes to what the chats filed in one sidebar folder, or in
    # the folders nested under it, touched: the union of ``touched_by`` over
    # those sessions, open and archived. The sessions are resolved here from the
    # folder id, so the client never names them, and the scope only narrows the
    # same listing an unscoped call returns. Present-but-empty, an unknown
    # folder, and a folder subtree holding no sessions all resolve to an empty
    # set, which matches nothing.
    touched_by_any: set[str] | None = None
    if "chat_folder" in request.query:
        touched_by_any = await _chat_folder_session_names(
            request.app.get("state"), request.query.get("chat_folder") or ""
        )
    try:
        store = get_default_store()
        # The listing reads one meta.json per artifact through the store's
        # sensitive-path fence. Off the loop that fence asks about the path the
        # store already canonicalised with no resolver-pool hop (see
        # ``ArtifactStore._read_text`` / ``_fence_refuses``), and a slow mount
        # stalls this worker rather than every other request.
        items = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                store.list,
                tag=tag,
                kind=kind,
                # When content-matching, don't let the store's name-only filter
                # exclude content/tag matches: filter in this layer instead.
                name_contains=None if do_content else q,
                source=source,
                source_path=source_path,
                folder=folder,
                session_key=session,
                touched_by_session=touched_by,
                touched_by_any=touched_by_any,
                pinned=pinned,
            ),
        )
    except (ArtifactError, OSError) as exc:
        logger.warning("artifact list failed: %s", exc)
        return _err(str(exc), status=500)
    # File reads + regex stripping are sync — keep them off the event loop so
    # a large-library content scan can't stall unrelated requests. Cached
    # content (version-keyed) makes repeated keystroke queries cheap.
    out = await asyncio.get_running_loop().run_in_executor(
        None, _scan_artifacts, store, items, q_lower, want_snippet, do_content
    )
    # Live-resolve each artifact's originating-session title for the Source
    # column (done here, on the event loop, where dashboard ``state`` is
    # available — the off-loop scan is stateless).
    state = request.app.get("state")
    if state is not None:
        for d in out:
            title = _resolve_session_title(state, d.get("session_key") or "")
            if title:
                d["session_title"] = _redact_text(title)
    return _json_response({"artifacts": out})


# ── Item: detail and asset ────────────────────────────────────────────────────
async def api_artifact_detail(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        store = get_default_store()
        art = await _run_off_loop(lambda: store.get(slug, assign_token=True))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    state = request.app.get("state")
    # Agent reads leave a ``referenced`` breadcrumb so the in-session Artifacts
    # tab can list what a session CONSUMED, not only what it authored (the
    # ``touched_by`` filter reads these events). Deliberately narrow:
    #
    #  * MCP only (``X-Internal-Secret``). A browser GET carries the literal
    #    ``dashboard:ui`` key, which ``_event_session_id`` drops anyway — but
    #    gating on the secret keeps a plain page view from ever mutating state.
    #  * Never for a restricted (incognito / temporary) session — leaving a
    #    durable trace is exactly what those sessions exist to avoid. Reads are
    #    otherwise ungated here, so this check is local rather than a 403.
    #  * Best-effort. ``record_impression`` dedupes to one breadcrumb per
    #    session and suppresses entirely when that session already has a
    #    lifecycle event, so a read after an edit is a no-op. A failure must
    #    never turn a successful read into an error.
    session_id = _event_session_id(request)
    if session_id and request.headers.get("X-Internal-Secret") is not None:
        # Deny by default: a falsy ``state`` must withhold the breadcrumb, not
        # skip the check. Written as its own positive gate (rather than folded
        # into the condition above) so no falsy value can short-circuit past it.
        if state is None or _is_restricted_session(state, request):
            _audit(
                tool="artifact_reference",
                request=request,
                outcome="denied",
                error="restricted session" if state is not None else "missing dashboard state",
                extra={"slug": slug, "via": "detail_read"},
            )
        else:
            try:
                # NB: ``record_impression`` returns a meta loaded via ``_load_meta``,
                # which does NOT carry ``content`` — assigning it over ``art`` would
                # silently strip the body out of every agent read. Take only the
                # refreshed event log so the response stays self-consistent.
                #
                # Off-thread because it rewrites ``meta.json``: an artifact at the
                # 500-event cap makes that a non-trivial synchronous write, and
                # this runs on every agent read. Blocking here would stall chat
                # and heartbeat processing for the whole gateway.
                fresh, appended = await asyncio.to_thread(
                    store.record_impression, slug, by="agent", session_id=session_id
                )
                art.events = fresh.events
                _audit(
                    tool="artifact_reference",
                    request=request,
                    outcome="ok",
                    extra={"slug": slug, "suppressed": not appended, "via": "detail_read"},
                )
            except (ArtifactError, OSError) as exc:
                logger.warning("artifact impression not recorded for %s: %s", slug, exc)
                _audit(
                    tool="artifact_reference",
                    request=request,
                    outcome="error",
                    error=str(exc),
                    extra={"slug": slug, "via": "detail_read"},
                )
    return _json_response(_serialize(art, include_content=True, state=state))


async def api_artifact_asset(request: web.Request) -> web.Response:
    """Serve an image artifact's raw raster bytes.

    ``GET /api/artifacts/{slug}/asset`` — returns the stored ``asset.<ext>``
    bytes with the correct ``Content-Type`` so an ``<img src=...>`` can point
    straight at it. Content-addressed by slug+version (the bytes for a given
    image artifact never change — a re-generated image is a new artifact), so
    it is served ``immutable`` with a long max-age.

    A read, like ``api_artifact_detail`` — no restricted-session mutation gate
    applies (nothing is written), and no ``referenced`` breadcrumb is recorded
    (an asset fetch is a sub-resource load of a detail view already counted).
    404 when the slug does not resolve or is not an image artifact.
    """
    slug = request.match_info.get("slug", "")
    try:
        store = get_default_store()
        # Off the loop: the sidecar can be up to MAX_CONTENT_BYTES, and a
        # synchronous read of that size would stall every other gateway task
        # (the user's chat turn and the liveness heartbeat included).
        data, mime = await asyncio.to_thread(store.read_image_bytes, slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    except (ArtifactError, OSError) as exc:
        logger.warning("artifact asset read failed for %s: %s", slug, exc)
        return _err("could not read image asset", status=500)
    return web.Response(
        body=data,
        content_type=mime,
        # ``private``: these bytes are behind token auth, so a shared caching
        # proxy must never keep a copy it could hand to an unauthenticated
        # requester. Still immutable for the browser's own cache — the bytes for
        # a given image artifact never change (a re-generated image is a new
        # artifact).
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# ── Versions ─────────────────────────────────────────────────────────────────
async def api_artifact_versions(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        # Off the loop although it only reads: it takes the store's lock, and a
        # writer holds that lock across the sync of every file it publishes.
        versions = await _run_off_loop(lambda: get_default_store().list_versions(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response({"slug": slug, "versions": versions})


async def api_artifact_version_detail(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    version_str = request.match_info.get("version", "")
    try:
        version = int(version_str)
    except ValueError:
        return _err(f"invalid version: {version_str}")
    try:
        # A read that can write: an artifact stored before the event log existed
        # gets its synthetic history backfilled and persisted on first read, so
        # this reaches _write_meta and its sync.
        art = await _run_off_loop(lambda: get_default_store().get(slug, version=version))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response(_serialize(art, include_content=True))


# ── Lifecycle events ─────────────────────────────────────────────────────────


async def api_artifact_events(request: web.Request) -> web.Response:
    """Return the lifecycle event log for an artifact.

    Triggers the lazy backfill in ``store.get`` for legacy artifacts that
    pre-date the events field, so the activity timeline is never empty for
    a real artifact (the fallback synthesizes ``created`` / ``edited`` from
    ``created_at`` / ``updated_at``).
    """
    slug = request.match_info.get("slug", "")
    try:
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response({"slug": art.slug, "events": list(art.events)})
