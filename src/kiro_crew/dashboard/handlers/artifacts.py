"""Artifacts HTTP handlers — REST endpoints over :class:`ArtifactStore`.

Endpoints
---------
- ``GET    /api/artifacts``                    list (filter by ?tag, ?kind, ?q)
- ``POST   /api/artifacts``                    create (JSON body)
- ``GET    /api/artifacts/{slug}``             read current version
- ``PATCH  /api/artifacts/{slug}``             update (content/name/description/tags)
- ``DELETE /api/artifacts/{slug}``             delete
- ``GET    /api/artifacts/{slug}/versions``    list version numbers
- ``GET    /api/artifacts/{slug}/versions/{n}``  read a specific version
- ``GET    /api/artifacts/{slug}/events``      lifecycle event log

Authorization
~~~~~~~~~~~~~
Standard dashboard auth (token middleware). Restricted sessions cannot mutate
artifacts; reads are allowed so the agent can iterate from a hook callback.

The HTTP layer is the single source of truth for SEL audit events on artifact
mutations — MCP tools and the CLI both go through here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any

from aiohttp import web

from kiro_crew import sel as _sel_mod
from kiro_crew.artifacts import (
    ArtifactAlreadyExistsError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_folder_store,
    get_default_store,
)
from kiro_crew.dashboard.chat_folders import generate_emoji_for_name
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def sel():
    """Late-resolved sel() — calls the module function so test patching of
    ``kiro_crew.sel.sel`` (the canonical patch target) continues to work."""
    return _sel_mod.sel()


logger = logging.getLogger(__name__)


# Maximum size of an artifact create/update request body (bytes). The store
# itself enforces a stricter content cap; this layer caps the JSON envelope.
_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    """Read a JSON body, capped at ``_MAX_BODY_BYTES``."""
    raw = await request.read()
    if len(raw) > _MAX_BODY_BYTES:
        raise ArtifactValidationError(f"request body exceeds {_MAX_BODY_BYTES} bytes")
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ArtifactValidationError("request body must be a JSON object")
    return body


def _session_key(request: web.Request) -> str:
    return request.headers.get("X-Session-Key") or ""


def _audit(
    *,
    tool: str,
    request: web.Request,
    outcome: str,
    extra: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    try:
        sel().log_tool_invocation(
            session_key=_session_key(request),
            source="api",
            tool_name=tool,
            outcome=outcome,
            error=error or "",
            metadata=extra or {},
        )
    except Exception:  # pragma: no cover — audit must never break a request
        logger.debug("SEL audit failed for %s", tool, exc_info=True)


def _serialize(art: Any, *, include_content: bool = False) -> dict[str, Any]:
    """Serialize an Artifact for response.

    All LLM-originated string fields (``name``, ``description``, ``tags``,
    and — when ``include_content=True`` — ``content``) pass through
    ``redact_exfiltration_urls()`` + ``redact_credentials()`` per
    AUTOSDE.yaml's ``security-controls`` rule. Artifact metadata is set
    by the agent via ``artifact_save`` / ``artifact_update``, so any
    field originating in LLM output must not reach the dashboard surface
    unredacted.
    """
    out = art.to_dict(include_content=include_content)
    for key in ("name", "description"):
        val = out.get(key)
        if isinstance(val, str) and val:
            cleaned, _ = redact_exfiltration_urls(val)
            cleaned, _ = redact_credentials(cleaned)
            out[key] = cleaned
    if isinstance(out.get("tags"), list):
        out["tags"] = [_redact_text(t) if isinstance(t, str) else t for t in out["tags"]]
    if include_content and out.get("content"):
        cleaned = out["content"]
        cleaned, _ = redact_exfiltration_urls(cleaned)
        cleaned, _ = redact_credentials(cleaned)
        out["content"] = cleaned
    return out


def _redact_text(text: str) -> str:
    cleaned, _ = redact_exfiltration_urls(text)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


#: Max length of a content preview snippet returned by the list endpoint when
#: ``?snippet=1`` is passed. Kept short so the list payload stays lean.
_SNIPPET_MAX_LEN = 160

#: Max accepted length for the ?q search string. Anything longer is truncated —
#: the scan substring-matches q against every artifact's full content, so an
#: unbounded query multiplies work for no legitimate use case.
_SEARCH_QUERY_MAX_CHARS = 256
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")
# Lightweight markdown → prose cleanup for previews (not a full parser).
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")  # [text](url) / ![alt](url) -> text
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

    Redacts a generous prefix so patterns straddling the truncation boundary are
    still caught (same controls the detail path applies to ``content``), then
    trims to ``_SNIPPET_MAX_LEN``.
    """
    head = _redact_text(stripped[: _SNIPPET_MAX_LEN * 3]).strip()
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
    window = [ln[:_CONTEXT_LINE_LEN] for ln in lines[start : idx + 3][:_CONTEXT_MAX_LINES]]
    return _redact_text("\n".join(window))


def _resolve_folder_ref(ref: Any, *, create_missing: bool) -> tuple[str, str | None]:
    """Resolve a folder reference (id or ``/``-separated human path) to a folder id.

    Returns ``(folder_id, error_message)``. ``None`` / ``""`` / ``"root"`` →
    ``""`` (unfiled/root). When ``create_missing`` is True, missing path
    segments are created (``mkdir -p``); otherwise an unknown path errors.
    """
    if ref is None:
        return "", None
    if not isinstance(ref, str):
        return "", "folder must be a string"
    if len(ref) > 4096:
        return "", "folder reference too long"
    try:
        fid = get_default_folder_store().resolve_path(ref, create_missing=create_missing)
    except ArtifactError as exc:
        # str(exc) can echo the raw LLM-supplied ref (e.g. "folder path not
        # found: <ref>"); redact before it reaches the dashboard via _err().
        return "", _redact_text(str(exc))
    return fid, None


async def _resolve_folder_ref_off_loop(ref: Any, *, create_missing: bool) -> tuple[str, str | None]:
    """Async wrapper for :func:`_resolve_folder_ref`.

    When ``create_missing`` is True the resolver may persist new folders
    (``_save()`` → ``os.fsync``/``os.replace``), which is blocking filesystem
    IO — run it in the shared executor so it never blocks the event loop.
    ``create_missing=False`` is a pure in-memory walk, so it runs inline.
    """
    if not create_missing:
        return _resolve_folder_ref(ref, create_missing=False)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        subprocess_executor(),
        lambda: _resolve_folder_ref(ref, create_missing=True),
    )


async def _run_off_loop(fn):  # type: ignore[no-untyped-def]
    """Run a blocking store call (small filesystem read/write) in the shared
    executor so its ``os.fsync``/``os.replace`` never blocks the event loop.
    Exceptions raised by ``fn`` propagate to the caller unchanged."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), fn)


def _set_folder_and_reload(slug: str, folder_id: str) -> Any:
    """Move an artifact into a folder and return the reloaded record (blocking)."""
    store = get_default_store()
    store.set_folder(slug, folder_id)
    return store.get(slug)


# ── List / Create ─────────────────────────────────────────────────────────────


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
                else _snippet_from(stripped)
            )
        out.append(d)
    return out


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
    try:
        store = get_default_store()
        items = store.list(
            tag=tag,
            kind=kind,
            # When content-matching, don't let the store's name-only filter
            # exclude content/tag matches — filter in this layer instead.
            name_contains=None if do_content else q,
            source=source,
            source_path=source_path,
            folder=folder,
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
    return _json_response({"artifacts": out})


async def api_artifacts_create(request: web.Request) -> web.Response:
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot create artifacts", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # ── Auto-dedup by source_path (Mesh-1654 Phase 6) ─────────────────────
    # When the caller passes a source_path that matches an existing artifact,
    # silently bump the existing one to a new version rather than creating a
    # parallel duplicate. This makes the "Add to artifacts" action on file
    # paths idempotent — clicking it twice on the same file just produces v2,
    # not two separate artifacts. Returns 200 OK on bump (vs 201 Created on
    # genuine new save) so the caller can distinguish if it cares.
    source_path = body.get("source_path") or ""
    if isinstance(source_path, str) and source_path:
        store = get_default_store()
        try:
            existing = store.find_by_source_path(source_path)
        except (ArtifactError, OSError) as exc:
            # find_by_source_path scans meta.json files; on a corrupt store
            # we fall through to the regular create path rather than
            # blocking the save.
            logger.warning("source_path lookup failed: %s", exc)
            existing = None
        if existing is not None:
            # Same auth-based actor inference as api_artifact_update — if the
            # caller is MCP (X-Internal-Secret header), the lifecycle event
            # gets tagged 'iterated' (agent), not 'edited' (user). Without
            # this, MCP-driven re-saves would silently misattribute on the
            # activity timeline.
            is_mcp = request.headers.get("X-Internal-Secret") is not None
            actor = "agent" if is_mcp else "user"
            try:
                art = store.update(
                    existing.slug,
                    content=body.get("content"),
                    actor=actor,
                    snapshot=True,
                )
            except ArtifactValidationError as exc:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(str(exc))
            except ArtifactError as exc:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="error",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(str(exc), status=500)
            _audit(
                tool="artifact_save",
                request=request,
                outcome="success",
                extra={
                    "slug": art.slug,
                    "kind": art.kind,
                    "version": art.version,
                    "deduped": True,
                },
            )
            # 200 OK signals "bumped existing"; the create path below returns 201.
            return _json_response(_serialize(art, include_content=True), status=200)
    # Resolve an optional folder placement (id or human path; mkdir -p missing
    # segments) so a save can file the artifact in one call (Mesh-2720). Off the
    # event loop — mkdir -p may persist new folders (blocking fsync).
    folder_id, ferr = await _resolve_folder_ref_off_loop(body.get("folder"), create_missing=True)
    if ferr:
        _audit(tool="artifact_save", request=request, outcome="denied", error=ferr)
        return _err(ferr)
    try:
        art = get_default_store().create(
            name=body.get("name", ""),
            content=body.get("content", ""),
            slug=body.get("slug"),
            kind=body.get("kind"),
            source=body.get("source", "chat"),
            description=body.get("description", ""),
            tags=body.get("tags") or [],
            source_path=body.get("source_path", ""),
            folder_id=folder_id,
        )
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc))
    except ArtifactAlreadyExistsError as exc:
        # Explicit slug collision — semantically a 409 Conflict (the resource
        # already exists). Distinct from base ArtifactError fallback below
        # which catches store-level refusals (sensitive-path, write failure)
        # and returns 500.
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc), status=409)
    except ArtifactError as exc:
        # Base-class fallback — store._write_text() can raise ArtifactError
        # ("refusing to write sensitive path: ...") after the duplicate-slug
        # check passes. Returning 409 there would be wrong; this is a server
        # error, not a conflict. Mirrors the pattern in api_artifact_update
        # and api_artifact_delete.
        _audit(
            tool="artifact_save",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": body.get("slug", "")},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_save",
        request=request,
        outcome="success",
        extra={"slug": art.slug, "kind": art.kind, "version": art.version},
    )
    return _json_response(_serialize(art, include_content=True), status=201)


# ── Item: read / update / delete ──────────────────────────────────────────────


async def api_artifact_detail(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_update(request: web.Request) -> web.Response:
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_update",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot update artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    try:
        # Determine actor authoritatively from how the request was authed,
        # NOT from the body. MCP-originated calls carry X-Internal-Secret
        # (validated by upstream middleware before we see them); browser
        # dashboard calls don't. Tagging by auth method is both more
        # accurate (the agent's MCP layer doesn't have to remember to set
        # actor='agent') and more secure (a body field could be spoofed).
        is_mcp = request.headers.get("X-Internal-Secret") is not None
        actor = "agent" if is_mcp else "user"
        # Session correlation: MCP calls carry X-Session-Key with a real
        # chat-slot key; the dashboard's browser client sets it to the
        # literal "dashboard:ui" for every request (see api/client.ts) which
        # is NOT a slot the user can navigate to. Drop it so the activity
        # timeline doesn't render a broken "from session dashboard:ui" link.
        session_id_hdr = request.headers.get("X-Session-Key")
        if session_id_hdr == "dashboard:ui":
            session_id_hdr = None
        # Snapshot semantics (Mesh-1654 round 5): saves don't bump version
        # by default — that's the user's "save while editing" path. Agent
        # updates via MCP always snapshot (each iteration is a meaningful
        # state change worth versioning, like a git commit). The dashboard
        # can also explicitly request a snapshot via ``snapshot: true`` in
        # the body (the "Snapshot" button next to Save).
        raw_snapshot = body.get("snapshot")
        if raw_snapshot is None:
            snapshot = is_mcp  # MCP defaults to True; dashboard defaults to False.
        else:
            snapshot = bool(raw_snapshot)
        # event_type / from_version overrides — used by the revert flow to
        # mark its update as ``reverted`` (with the source version pinned)
        # rather than the default ``edited``. Validation lives in
        # store.update() — invalid values raise ArtifactValidationError →
        # 400 below. Reverts always snapshot regardless of the snapshot
        # flag because the entire point is to record the rollback.
        raw_event_type = body.get("event_type")
        event_type = raw_event_type if isinstance(raw_event_type, str) and raw_event_type else None
        if event_type == "reverted":
            snapshot = True
        raw_from_version = body.get("from_version")
        try:
            from_version = int(raw_from_version) if raw_from_version is not None else None
        except (TypeError, ValueError):
            from_version = None
        art = get_default_store().update(
            slug,
            content=body.get("content"),
            description=body.get("description"),
            tags=body.get("tags"),
            name=body.get("name"),
            actor=actor,
            session_id=session_id_hdr,
            event_type=event_type,
            from_version=from_version,
            snapshot=snapshot,
        )
        # store.update() only loads content into the returned Artifact when
        # the caller passed new content (because that path is on the write
        # branch of the store). For metadata-only updates the returned
        # Artifact has content=None, which then serializes as "content": null
        # in the response — inconsistent with api_artifact_detail which
        # always returns the actual content. Refetch in that case so the MCP
        # tool / dashboard caller always sees a populated content field.
        if art.content is None:
            art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_update",
            request=request,
            outcome="error",
            error=str(exc),
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_update",
            request=request,
            outcome="denied",
            error=str(exc),
        )
        return _err(str(exc))
    except ArtifactError as exc:
        # Catches the base class fallback — store._write_text() raises
        # ArtifactError("refusing to write sensitive path: ...") which is
        # neither ArtifactNotFoundError nor ArtifactValidationError. Without this
        # branch the request would 500 with no audit trail.
        _audit(
            tool="artifact_update",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    # Optional folder placement (Mesh-2720). Metadata-only — does not bump the
    # version. The dedicated PATCH /folder route is the canonical path; this
    # honours a ``folder`` key on the generic update for convenience/parity.
    if "folder" in body:
        folder_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("folder"), create_missing=True
        )
        if ferr:
            _audit(
                tool="artifact_update",
                request=request,
                outcome="denied",
                error=ferr,
                extra={"slug": slug},
            )
            return _err(ferr)
        try:
            art = await _run_off_loop(lambda: _set_folder_and_reload(slug, folder_id))
        except ArtifactError as exc:
            _audit(
                tool="artifact_update",
                request=request,
                outcome="error",
                error=str(exc),
                extra={"slug": slug, "folder_id": folder_id},
            )
            return _err(str(exc), status=500)
    # SEL audit for the mutation. When this update also placed the artifact in
    # a folder, the audit must carry the folder context (security guideline:
    # permission-relevant mutations audit their full effect).
    _success_extra: dict[str, Any] = {"slug": art.slug, "version": art.version}
    if "folder" in body:
        _success_extra["folder_id"] = art.folder_id
    _audit(
        tool="artifact_update",
        request=request,
        outcome="success",
        extra=_success_extra,
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_delete(request: web.Request) -> web.Response:
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot delete artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        get_default_store().delete(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        # Base-class fallback — defends against any ArtifactError subclass
        # not specifically handled above (e.g. future store-level errors).
        # Without this branch the request would 500 with no audit trail.
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_delete",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response({"ok": True})


# ── Versions ─────────────────────────────────────────────────────────────────


async def api_artifact_versions(request: web.Request) -> web.Response:
    slug = request.match_info.get("slug", "")
    try:
        versions = get_default_store().list_versions(slug)
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
        art = get_default_store().get(slug, version=version)
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
        art = get_default_store().get(slug)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    return _json_response({"slug": art.slug, "events": list(art.events)})


async def api_artifact_record_event(request: web.Request) -> web.Response:
    """Record an impression-style lifecycle event without modifying content.

    Currently only ``referenced`` events go through this endpoint —
    ``WidgetFrame`` posts here when each chat impression mounts so the
    activity timeline can show "this artifact was referenced N times
    across M sessions". Other event types (``created``, ``edited``,
    ``iterated``, ``reverted``) are emitted internally by the store as a
    side effect of the corresponding mutation; only ``referenced`` is a
    pure annotation that doesn't change content/version, which is why it
    needs a dedicated endpoint.

    Auth: same X-Internal-Secret + X-Session-Key model as the rest of
    the artifacts API. Browser-originated requests get ``by='user'``;
    MCP-originated requests get ``by='agent'``. Session ID is taken
    from the X-Session-Key header (with the literal ``dashboard:ui``
    dropped — same rule as other handlers).

    Appending events mutates ``meta.json``, so this is gated behind the
    same deny-by-default ``_is_restricted_session`` check as the other
    mutation endpoints — a restricted session must not be able to flood
    an artifact's event log.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_reference",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot record artifact events", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    event_type = body.get("type")
    # Restrict to ``referenced`` for now — the other event types must
    # come from the mutation paths so version-bump bookkeeping and
    # snapshot creation stay coupled to actual content changes.
    # Callers passing anything else are likely confused; reject loudly.
    if event_type != "referenced":
        return _err(
            "this endpoint only accepts type='referenced'; "
            "use POST /api/artifacts (create), PATCH /api/artifacts/{slug} "
            "(update / iterate / revert) for content-mutating events"
        )
    is_mcp = request.headers.get("X-Internal-Secret") is not None
    actor = "agent" if is_mcp else "user"
    session_id_hdr = request.headers.get("X-Session-Key")
    if session_id_hdr == "dashboard:ui":
        session_id_hdr = None
    raw_metadata = body.get("metadata") or {}
    if not isinstance(raw_metadata, dict):
        return _err("metadata must be an object")
    message_ts = raw_metadata.get("message_ts")
    widget_index = raw_metadata.get("widget_index")
    # Light type coercion at the boundary — store-side _append_event
    # also defends, but failing fast with a clear 400 is friendlier
    # than a silent metadata drop.
    if message_ts is not None and not isinstance(message_ts, str):
        return _err("metadata.message_ts must be a string")
    if widget_index is not None and not isinstance(widget_index, int):
        return _err("metadata.widget_index must be an integer")
    try:
        art, appended = get_default_store().record_impression(
            slug,
            by=actor,
            session_id=session_id_hdr,
            message_ts=message_ts,
            widget_index=widget_index,
        )
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    except (ArtifactError, OSError) as exc:
        logger.warning("record_impression failed for %s: %s", slug, exc)
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_reference",
        request=request,
        outcome="ok",
        extra={"slug": art.slug, "suppressed": not appended},
    )
    # When the impression was suppressed (the session already has a CUD
    # event on this artifact) no `referenced` event was appended, so
    # `art.events[-1]` would be an unrelated prior event. Signal the
    # suppression explicitly rather than echoing a misleading payload.
    if not appended:
        return _json_response({"slug": art.slug, "event": None, "suppressed": True})
    # Return only the latest event entry — the full event log can be
    # fetched via the GET endpoint. Keeps this response small for the
    # high-frequency impression-logging case.
    latest = art.events[-1] if art.events else None
    return _json_response({"slug": art.slug, "event": latest})


# ── Folders (Mesh-2720) ─────────────────────────────────────────────────────


def _serialize_folder(folder: dict[str, Any], *, path: str | None = None) -> dict[str, Any]:
    """Serialize a folder record; redact the (user/LLM-set) name, icon, and path."""
    out = dict(folder)
    if isinstance(out.get("name"), str) and out["name"]:
        out["name"] = _redact_text(out["name"])
    # icon is LLM-derived (generate_emoji_for_name) or user-supplied (set_icon
    # API) — never trust either on the way back out to the dashboard.
    if isinstance(out.get("icon"), str) and out["icon"]:
        out["icon"] = _redact_text(out["icon"])
    if path is not None:
        out["path"] = _redact_text(path) if path else path
    return out


async def api_artifact_folders(request: web.Request) -> web.Response:
    """GET /api/artifact-folders — list folders enriched with item_count + path."""
    store = get_default_store()
    fstore = get_default_folder_store()
    try:
        # list_with_counts walks every artifact's meta.json (O(N) filesystem
        # scan). Offload it so the dashboard event loop stays responsive —
        # same pattern as api_chat_folders.
        loop = asyncio.get_running_loop()
        folders = await loop.run_in_executor(
            subprocess_executor(), fstore.list_with_counts, store
        )
    except (ArtifactError, OSError) as exc:
        logger.warning("artifact folder list failed: %s", exc)
        return _err(str(exc), status=500)
    out = [_serialize_folder(f, path=fstore.breadcrumb(f["id"])) for f in folders]
    return _json_response({"folders": out})


def _spawn_artifact_folder_icon_task(request: web.Request, folder_id: str, name: str) -> None:
    """Fire-and-forget: derive a single-emoji icon for an artifact folder via
    the shared LLM helper (same mechanism as chat-sidebar folders) and store
    it. Best-effort — any failure leaves the folder with the default glyph."""
    state = request.app.get("state")
    if state is None:
        return

    async def _run() -> None:
        try:
            icon = await generate_emoji_for_name(state, name)
            if not icon:
                return
            fstore = get_default_folder_store()
            if fstore.exists(folder_id):
                await _run_off_loop(lambda: fstore.set_icon(folder_id, icon))
        except Exception:  # noqa: BLE001 — best-effort background task
            logger.debug("artifact folder icon generation failed for %s", folder_id, exc_info=True)

    task = asyncio.ensure_future(_run())
    _ARTIFACT_FOLDER_ICON_TASKS.add(task)
    task.add_done_callback(_ARTIFACT_FOLDER_ICON_TASKS.discard)


# Keep strong refs so in-flight icon tasks aren't garbage-collected mid-run.
_ARTIFACT_FOLDER_ICON_TASKS: set[asyncio.Task[None]] = set()


async def api_artifact_folder_create(request: web.Request) -> web.Response:
    """POST /api/artifact-folders — create a folder. Body: {name, parent?|parent_id?}."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_create",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot create folders", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    name = str(body.get("name") or "").strip()
    if not name:
        return _err("name required")
    # ``parent`` accepts an id OR a human path (mkdir -p); ``parent_id`` is
    # id-only — resolved read-only so a path-looking value can never
    # auto-create folders through the id-only key.
    if "parent" in body:
        parent_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("parent"), create_missing=True
        )
    else:
        parent_id, ferr = _resolve_folder_ref(body.get("parent_id"), create_missing=False)
    if ferr:
        _audit(tool="artifact_folder_create", request=request, outcome="denied", error=ferr)
        return _err(ferr)
    fstore = get_default_folder_store()
    color = str(body.get("color") or "")
    try:
        folder = await _run_off_loop(lambda: fstore.create(name, parent_id=parent_id, color=color))
    except ArtifactValidationError as exc:
        _audit(tool="artifact_folder_create", request=request, outcome="denied", error=str(exc))
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(tool="artifact_folder_create", request=request, outcome="error", error=str(exc))
        return _err(str(exc), status=500)
    # Derive an emoji icon from the name in the background (chat-folder parity).
    _spawn_artifact_folder_icon_task(request, folder["id"], name)
    _audit(
        tool="artifact_folder_create",
        request=request,
        outcome="success",
        extra={"folder_id": folder["id"]},
    )
    return _json_response(
        _serialize_folder(folder, path=fstore.breadcrumb(folder["id"])), status=201
    )


async def api_artifact_folder_update(request: web.Request) -> web.Response:
    """PATCH /api/artifact-folders/{id} — rename / reparent / reorder / icon."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"folder_id": request.match_info.get("id", "")},
        )
        return _err("restricted session cannot update folders", status=403)
    fid = request.match_info.get("id", "")
    fstore = get_default_folder_store()
    if not fstore.exists(fid):
        return _err("folder not found", status=404)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    folder = fstore.get(fid)
    if folder is None:  # exists() checked above; guards against a concurrent delete
        return _err("folder not found", status=404)

    def _apply_updates() -> dict[str, Any]:
        # Each mutation persists via _save() (fsync/replace) — runs in the
        # executor, off the event loop.
        f = fstore.get(fid)
        if f is None:
            raise ArtifactNotFoundError(f"folder not found: {fid}")
        if "name" in body:
            f = fstore.rename(fid, str(body["name"]))
        if "parent_id" in body:
            f = fstore.reparent(fid, str(body.get("parent_id") or ""))
        if "icon" in body:
            f = fstore.set_icon(fid, str(body.get("icon") or ""))
        if "color" in body:
            f = fstore.set_color(fid, str(body.get("color") or ""))
        if "order" in body:
            fstore.reorder([{"id": fid, "order": int(body["order"])}])
            ref = fstore.get(fid)
            if ref is not None:
                f = ref
        return f

    try:
        updated = await _run_off_loop(_apply_updates)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except (ArtifactValidationError, ValueError, TypeError) as exc:
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc), status=500)
    _audit(tool="artifact_folder_update", request=request, outcome="success", extra={"folder_id": fid})
    # A rename re-derives the emoji icon from the new name (chat-folder
    # parity) — unless this same request set an explicit icon, which wins.
    if "name" in body and "icon" not in body:
        _spawn_artifact_folder_icon_task(request, fid, str(body["name"]))
    return _json_response(_serialize_folder(updated, path=fstore.breadcrumb(fid)))


async def api_artifact_folder_delete(request: web.Request) -> web.Response:
    """DELETE /api/artifact-folders/{id}?delete_contents=<bool>.

    Default (``delete_contents`` falsy) is the SAFE path: re-parent this
    folder's direct children (folders + artifacts) up to its parent and delete
    only this folder. ``delete_contents=true`` cascades the whole subtree,
    permanently deleting every descendant artifact.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_delete",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"folder_id": request.match_info.get("id", "")},
        )
        return _err("restricted session cannot delete folders", status=403)
    fid = request.match_info.get("id", "")
    fstore = get_default_folder_store()
    if not fstore.exists(fid):
        return _err("folder not found", status=404)
    raw = (request.query.get("delete_contents") or "").strip().lower()
    delete_contents = raw in ("1", "true", "yes")
    try:
        # delete() scans every artifact (O(N)) and, in cascade mode, recursively
        # removes directories — offload off the event loop.
        loop = asyncio.get_running_loop()
        summary = await loop.run_in_executor(
            subprocess_executor(),
            lambda: fstore.delete(
                fid, delete_contents=delete_contents, artifact_store=get_default_store()
            ),
        )
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactError as exc:
        _audit(
            tool="artifact_folder_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_folder_delete",
        request=request,
        outcome="success",
        extra={
            "folder_id": fid,
            "delete_contents": delete_contents,
            "deleted_artifacts": len(summary.get("deleted_artifact_slugs", [])),
        },
    )
    return _json_response({"ok": True, **summary})


async def api_artifact_set_folder(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/folder — move an artifact into a folder.

    Body accepts ``{folder}`` (id OR human path, mkdir -p) or ``{folder_id}``
    (id-only). ``""`` / ``"root"`` / null unfiles. Metadata-only — no version bump.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot move artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    if "folder" in body:
        folder_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("folder"), create_missing=True
        )
    else:
        folder_id, ferr = _resolve_folder_ref(body.get("folder_id"), create_missing=False)
    if ferr:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error=ferr,
            extra={"slug": slug},
        )
        return _err(ferr)
    # A non-empty id passed directly must reference a real folder.
    if folder_id and not get_default_folder_store().exists(folder_id):
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error="folder not found",
            extra={"slug": slug, "folder_id": folder_id},
        )
        return _err("folder not found", status=400)
    try:
        art = await _run_off_loop(lambda: _set_folder_and_reload(slug, folder_id))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_set_folder",
        request=request,
        outcome="success",
        extra={"slug": slug, "folder_id": folder_id},
    )
    return _json_response(_serialize(art, include_content=True))
