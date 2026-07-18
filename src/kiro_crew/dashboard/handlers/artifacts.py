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
import getpass
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew import sel as _sel_mod
from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    ArtifactAlreadyExistsError,
    ArtifactComment,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_folder_store,
    get_default_store,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_folders import generate_emoji_for_name
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.executors import subprocess_executor
from kiro_crew.publish_provider import (
    Capability,
    CommentAnchor,
    KindSupport,
    NotPublishedError,
    PublishConflictError,
    PublishError,
    PublishUnavailableError,
    get_provider,
    list_providers,
)
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)


def sel():
    """Late-resolved sel() — calls the module function so test patching of
    ``kiro_crew.sel.sel`` (the canonical patch target) continues to work."""
    return _sel_mod.sel()


logger = logging.getLogger(__name__)


# Maximum size of an artifact create/update request body (bytes). Sized to the
# store's content cap (MAX_CONTENT_BYTES = 25 MiB) PLUS headroom for JSON
# envelope overhead (base64/escaping + the other body fields), so content the
# store + validation accept (up to 25 MiB) is never rejected earlier at this
# HTTP boundary. Previously pinned at 2 MiB, which silently became the effective
# ceiling for dashboard/MCP artifact_save/update once the content cap was raised
# 1 MiB -> 25 MiB (the "store enforces a stricter cap" assumption inverted).
_MAX_BODY_BYTES = MAX_CONTENT_BYTES + 8 * 1024 * 1024  # 25 MiB content + 8 MiB envelope headroom

# Publish-provider name grammar. Upstream imports this from ``validation`` where
# it also backs the MCP publish-tool FieldSpecs; the public fork's validation
# module doesn't carry those tools, so the constraint lives here at the sole
# HTTP boundary that accepts a provider name.
_ARTIFACT_PROVIDER_RE = re.compile(r"^[a-z0-9-]{1,32}$")


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _notify_artifact_update(state: Any, slug: str, version: int, *, deleted: bool = False) -> None:
    """Best-effort WS broadcast of an artifact content change (Mesh-2772).

    Called from the mutation funnel (create / content update / revert /
    relocate / delete) — the same choke points as the SEL audit, so panel
    chat, other dashboard sessions, Slack, and CLI mutations all emit.
    Fire-and-forget:
    react-query's 30s staleness window remains the safety net if the broadcast
    fails or a client misses it. Known limitation (accepted): external edits to
    a file-backed artifact's source_path never pass through a handler, so those
    stay on pull-based refresh.
    """
    try:
        if state is not None:
            state.push_artifact_update(slug, version, deleted=deleted)
    except Exception:  # pragma: no cover — fire-and-forget by design
        logger.debug("artifact_update broadcast failed for %s", slug, exc_info=True)


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


def _event_session_id(request: web.Request) -> str | None:
    """Session key for activity-feed events, or None when not a real slot.

    The dashboard's browser client sets X-Session-Key to the literal
    ``dashboard:ui`` for every request — that is not a chat slot a user can
    navigate to, so drop it (same rule as ``api_artifact_update``).
    """
    sk = request.headers.get("X-Session-Key")
    if not sk or sk == "dashboard:ui":
        return None
    return sk


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
    # Publication block (Artifactory) is structural — view_url is an internal
    # CloudFront URL and aliases are user input — but ``last_error`` can echo
    # an arbitrary upstream error string, so redact it like other surfaced
    # text per AUTOSDE security-controls.
    pub = out.get("publication")
    if isinstance(pub, dict) and isinstance(pub.get("last_error"), str) and pub["last_error"]:
        pub["last_error"] = _redact_text(pub["last_error"])
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
            _notify_artifact_update(state, art.slug, art.version)
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
    # New library entries appear live in every open window (Mesh-2772).
    _notify_artifact_update(state, art.slug, art.version)
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
    # Live refresh (Mesh-2772): broadcast only when the artifact's content
    # actually changed — a content-carrying PATCH (Save / Snapshot / MCP
    # artifact_update) or a revert (event_type="reverted" is a content
    # rollback even when the body carries no content field). Metadata-only
    # updates (rename / retag / description / folder) don't move content, so
    # open views have nothing to re-render.
    if body.get("content") is not None or event_type == "reverted":
        _notify_artifact_update(state, art.slug, art.version)
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
    # Capture the pre-delete version so the deleted-variant WS event carries the
    # last-known version (Mesh-2772). The upstream cleanup block that fetched
    # this was tied to the removed Artifactory path, so fetch it here directly.
    try:
        _existing = get_default_store().get(slug)
    except ArtifactError:
        # Best-effort version capture only — swallow both the missing-slug and
        # invalid-slug (ArtifactValidationError) siblings so an invalid slug still
        # reaches the delete() call below, which returns a clean 4xx (a bare
        # ArtifactNotFoundError catch here would leak ArtifactValidationError as a 500).
        _existing = None
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
    # Deleted variant (Mesh-2772): open views of this slug toast + leave.
    _notify_artifact_update(
        state, slug, _existing.version if _existing is not None else 0, deleted=True
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


# ── Publishing / sharing (Artifactory — Mesh-1880) ───────────────────────────

_VALID_VISIBILITY = ("PRIVATE", "SHARED", "PUBLIC")


def _validate_sharing_body(body: dict[str, Any]) -> tuple[str, list[str]]:
    """Extract and validate (visibility, shared_with) from a request body.

    Raises ``ArtifactValidationError`` (→ 400) on any problem.
    """
    visibility = body.get("visibility") or "PRIVATE"
    if visibility not in _VALID_VISIBILITY:
        raise ArtifactValidationError("visibility must be PRIVATE, SHARED, or PUBLIC")
    shared_with = body.get("shared_with") or []
    if not isinstance(shared_with, list) or not all(isinstance(a, str) for a in shared_with):
        raise ArtifactValidationError("shared_with must be a list of alias strings")
    if visibility == "SHARED" and not shared_with:
        raise ArtifactValidationError(
            "SHARED visibility requires at least one alias in shared_with"
        )
    return visibility, shared_with


def _sync_error_response(
    tool: str, request: web.Request, slug: str, exc: Exception
) -> web.Response:
    """Map an Artifactory sync exception to an audited HTTP error response."""
    if isinstance(exc, ArtifactNotFoundError):
        status, outcome = 404, "error"
    elif isinstance(exc, ArtifactValidationError):
        status, outcome = 400, "denied"
    elif isinstance(exc, PublishUnavailableError):
        status, outcome = 503, "error"
    elif isinstance(exc, PublishConflictError):
        status, outcome = 409, "error"
    elif isinstance(exc, NotPublishedError):
        status, outcome = 409, "denied"
    elif isinstance(exc, PublishError):
        status, outcome = 502, "error"
    else:
        status, outcome = 500, "error"
    # The exception text can originate from untrusted Artifactory MCP responses
    # — redact credentials / exfiltration URLs before it reaches the dashboard
    # AND the SEL audit log (AUTOSDE security-controls).
    safe_msg = _redact_text(str(exc))
    _audit(tool=tool, request=request, outcome=outcome, error=safe_msg, extra={"slug": slug})
    return _err(safe_msg, status=status)


def _publish_governance_denied(request: web.Request, provider_name: str) -> str | None:
    """Plane-C governance chokepoint for artifact publishing.

    Publishing is a user-driven dashboard HTTP action ("NOT LLM tools"), so the
    host PreToolUse gate never sees it — this is where the ``capabilities.publish``
    ceiling is enforced. Returns a denial reason (caller → 403) or ``None`` to
    permit. Enforces, tightest-wins:
      1. governance ceiling ∩ profile — ``capabilities.publish`` gate AND its
         inner ``destinations`` ruleset (item ``destinations:<provider>``);
      2. the standalone operator's ``config.publish.allowed_destinations``
         allowlist (default-open, narrow-only — cannot widen past the ceiling).
    A ``PlatformCompositionError`` propagates (fail-closed CPP); any other
    governance error fails CLOSED (DENY) — publishing is an authorization
    decision (bytes leave the box), so unlike the messaging/cron chokepoints it
    must NOT degrade-to-permit. The DENY is produced inside ``governance_permits``
    (``fail_closed=True``), because that helper swallows its own internal errors —
    the handler-level ``except`` here only catches errors raised OUTSIDE it.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    session_key = _session_key(request)
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.publish",
            f"destinations:{provider_name}",
            session_key=session_key,
            # Authorization chokepoint: a governance-evaluation error must DENY
            # (bytes leave the box). governance_permits swallows its own internal
            # errors, so the fail-closed DENY has to be produced INSIDE it — the
            # handler-level ``except`` below only ever sees errors raised outside
            # governance_permits (e.g. the audit call).
            fail_closed=True,
        )
        # Default to DENY (permitted=False) if the Decision is malformed: this is
        # an exfil authorization chokepoint documented as "must NOT
        # degrade-to-permit", so a missing/odd attr must fail closed, not open.
        if not getattr(decision, "permitted", False):
            try:
                sel().log_governance_decision(
                    session_key=session_key,
                    tool_name=f"artifact_publish:{provider_name}",
                    scope="capabilities.publish",
                    item=f"destinations:{provider_name}",
                    outcome="denied",
                    rule=getattr(decision, "rule", ""),
                    layer=getattr(decision, "layer", ""),
                    reason=getattr(decision, "reason", ""),
                )
            except Exception:
                logger.debug("publish governance deny audit failed", exc_info=True)
            return getattr(decision, "reason", "publishing not permitted by policy")
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED: publishing is an authorization decision (bytes leave the
        # box to an external destination), so an unexpected error must DENY
        # rather than degrade-to-permit. governance_permits(fail_closed=True)
        # already denies on ITS own internal errors; this branch is the belt-and-
        # suspenders catch for anything raised OUTSIDE it (e.g. the deny-audit
        # call above), keeping the whole helper deny-on-error.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "artifact_publish", session_key=session_key, scope="capabilities.publish"
            )
        except Exception:
            logger.debug("publish governance degrade audit unavailable", exc_info=True)
        return "publishing denied: governance could not be evaluated"

    # Config allowlist (default-open, narrow-only). Empty list allows any
    # registered destination; a non-empty list restricts to those provider ids.
    # A config-read failure also fails CLOSED for the same reason as above.
    try:
        allowed = KiroCrewConfig.load().publish.allowed_destinations
    except Exception:
        logger.debug("publish config load failed; failing closed", exc_info=True)
        return "publishing denied: publish config could not be loaded"
    if allowed and provider_name not in allowed:
        return f"publish destination {provider_name!r} is not in the operator allowlist"
    return None


async def api_artifact_publish(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/publish — publish (or re-publish) to a
    registered publish destination.

    Body: ``{visibility, shared_with[]}``. Returns the full serialized artifact
    (now carrying the ``publication`` block). A side-panel file that isn't yet
    an artifact is auto-saved first by the frontend (POST /api/artifacts), so
    this endpoint is always slug-based.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_publish",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot publish artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
        visibility, shared_with = _validate_sharing_body(body)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # Provider is validated generically (any registered provider); the share
    # picker only offers providers whose kind_support() != UNSUPPORTED.
    requested_provider = body.get("provider") if isinstance(body, dict) else None
    provider_name = requested_provider or "artifactory"
    if not isinstance(provider_name, str) or not _ARTIFACT_PROVIDER_RE.match(provider_name):
        return _err("provider must match ^[a-z0-9-]{1,32}$")
    # Resolve the EFFECTIVE destination BEFORE the governance gate. For an
    # already-published artifact, publish_sync.publish() ignores provider_name
    # and re-pushes to publication.provider — so the gate must evaluate THAT
    # provider, not the (default) requested one, or a re-publish with no explicit
    # provider would gate on "artifactory" and permit bytes to a DENIED existing
    # destination. Mirrors api_artifact_update_sharing (which gates on the
    # existing publication's provider).
    try:
        # ≤25 MiB store read — offload off the event loop.
        existing_pub = (await _run_off_loop(lambda: get_default_store().get(slug))).publication
    except ArtifactNotFoundError:
        existing_pub = None
    # nrb review #19: reject an explicit provider switch on an already-published
    # artifact rather than silently ignoring it. publish() reuses the existing
    # publication's provider, so honoring a switch here would leave the original
    # remote orphaned — require an explicit unpublish first.
    if (
        requested_provider
        and existing_pub is not None
        and existing_pub.provider
        and requested_provider != existing_pub.provider
    ):
        return _err(
            f"artifact is already published to {existing_pub.provider!r}; "
            f"unpublish it before publishing to {requested_provider!r}",
            status=409,
        )
    # Effective provider: the existing publication's (re-publish dispatches to it)
    # else the requested/default. This is the destination bytes actually go to.
    effective_provider = (
        existing_pub.provider if existing_pub and existing_pub.provider else provider_name
    )
    # Governance chokepoint (Plane-C): the capabilities.publish ceiling + the
    # operator destination allowlist gate publishing here — the host PreToolUse
    # gate never sees this HTTP action. Runs BEFORE any provider dispatch.
    gov_denial = _publish_governance_denied(request, effective_provider)
    if gov_denial is not None:
        _audit(
            tool="artifact_publish",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": effective_provider},
        )
        return _err(gov_denial, status=403)
    is_mcp = request.headers.get("X-Internal-Secret") is not None
    actor = "agent" if is_mcp else "user"
    try:
        await publish_sync.publish(
            slug,
            visibility=visibility,
            shared_with=shared_with,
            actor=actor,
            provider_name=provider_name,
        )
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_publish", request, slug, exc)
    _audit(
        tool="artifact_publish",
        request=request,
        outcome="success",
        extra={"slug": slug, "visibility": visibility, "provider": effective_provider},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_update_sharing(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/sharing — change visibility / shared-with.

    Body: ``{visibility, shared_with[]}``. No re-upload. Returns the serialized
    artifact with the updated publication block.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_update_sharing",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot change artifact sharing", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
        visibility, shared_with = _validate_sharing_body(body)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # Changing sharing (e.g. PRIVATE -> PUBLIC) is an outbound-publish mutation,
    # so it MUST pass the same capabilities.publish governance gate as the
    # initial publish — otherwise an already-published artifact could be widened
    # to public after policy revocation. Gate on the existing publication's
    # provider (default provider when the block hasn't loaded).
    try:
        existing_pub = (await _run_off_loop(lambda: get_default_store().get(slug))).publication
    except ArtifactNotFoundError:
        existing_pub = None
    share_provider = (
        existing_pub.provider if existing_pub and existing_pub.provider else "artifactory"
    )
    gov_denial = _publish_governance_denied(request, share_provider)
    if gov_denial is not None:
        _audit(
            tool="artifact_update_sharing",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": share_provider},
        )
        return _err(gov_denial, status=403)
    try:
        await publish_sync.update_sharing(slug, visibility=visibility, shared_with=shared_with)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_update_sharing", request, slug, exc)
    _audit(
        tool="artifact_update_sharing",
        request=request,
        outcome="success",
        extra={"slug": slug, "visibility": visibility},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_unpublish(request: web.Request) -> web.Response:
    """DELETE /api/artifacts/{slug}/publish — remove from Artifactory.

    Deletes the Artifactory artifact (best-effort) and clears the local
    publication block. Returns the serialized artifact (now with
    ``publication: null``).
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_unpublish",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot unpublish artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        await publish_sync.unpublish(slug)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_unpublish", request, slug, exc)
    _audit(
        tool="artifact_unpublish",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_refresh_sharing(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/publish/refresh — reconcile local sharing
    state with the live destination.

    Pulls the destination's current visibility / shared-with (e.g. after the
    user changed them directly in the Artifactory UI) and updates the stored
    publication so the dashboard reflects truth. Gated like other mutations
    since it can update meta.json.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot refresh artifact sharing", status=403)
    slug = request.match_info.get("slug", "")
    try:
        await publish_sync.refresh_publication(slug)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover — refresh is best-effort
        return _sync_error_response("artifact_refresh_sharing", request, slug, exc)
    _audit(
        tool="artifact_refresh_sharing",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_relocate(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/relocate — update source_path."""

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_relocate",
            request=request,
            outcome="denied",
            error="restricted session",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot relocate artifacts", status=403)

    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))

    source_path = body.get("source_path")
    if source_path is None:
        return _err("source_path is required")
    if not isinstance(source_path, str):
        return _err("source_path must be a string")

    # Validate path. The user-controlled source_path is sanitized BEFORE any
    # filesystem access, in this order (each proves the value safe before it is
    # used in a path expression — this is also what CodeQL's path-injection taint
    # tracker requires as a sanitizer):
    #   1. ".." traversal guard on the raw request value;
    #   2. FIXED-ROOT containment — the resolved path must live under the user's
    #      home dir OR an operator-configured extra root (``publish.relocate_roots``);
    #   3. the ``is_sensitive_path`` denylist inside every allowed root.
    # The root confinement (2) is the barrier that turns relocate from an
    # arbitrary-local-file read primitive (an agent could aim an artifact at
    # /etc/passwd or another user's files, then exfiltrate via a later GET) into a
    # home-confined one, closing the CodeQL alert and the agent-reachable read.
    if source_path:  # non-empty = must exist and be a file
        # Path traversal guard (on the raw request value, before resolution).
        if ".." in Path(source_path).parts:
            _audit(
                tool="artifact_relocate",
                request=request,
                outcome="denied",
                error="path traversal",
                extra={"slug": slug, "source_path": source_path},
            )
            return _err("path traversal not allowed", status=403)
        resolved_path = Path(os.path.expanduser(source_path)).resolve()
        # Fixed-root containment: resolve the allowed roots (home + configured
        # extras) and require the target to be inside one. is_relative_to on the
        # resolved Paths is the sanitizer CodeQL recognizes.
        allowed_roots = [Path.home().resolve()]
        try:
            for extra in KiroCrewConfig.load().publish.relocate_roots:
                if isinstance(extra, str) and extra.strip():
                    allowed_roots.append(Path(os.path.expanduser(extra)).resolve())
        except Exception:
            logger.debug("relocate roots config load failed; home-only", exc_info=True)
        # Fixed-root containment barrier — inlined (NOT via a helper) so CodeQL's
        # intra-procedural taint tracker sees the ``is_relative_to`` sanitizer
        # guarding the SAME ``resolved_path`` that the stat calls below use.
        within_root = False
        for _root in allowed_roots:
            try:
                if resolved_path == _root or resolved_path.is_relative_to(_root):
                    within_root = True
                    break
            except (ValueError, OSError):  # pragma: no cover — defensive
                continue
        if not within_root:
            _audit(
                tool="artifact_relocate",
                request=request,
                outcome="denied",
                error="outside allowed roots",
                extra={"slug": slug, "source_path": source_path},
            )
            return _err(
                "source_path must be inside your home directory " "(or a configured relocate root)",
                status=403,
            )
        # Sensitive-path denylist still applies inside the allowed roots (e.g.
        # ~/.aws, ~/.ssh, ~/.kirocrew keystone).
        if is_sensitive_path(str(resolved_path)):
            _audit(
                tool="artifact_relocate",
                request=request,
                outcome="denied",
                error="sensitive path",
                extra={"slug": slug, "source_path": source_path},
            )
            return _err("cannot point to a sensitive path", status=403)
        # `resolved_path` is now proven under an allowed root AND not sensitive.
        if not resolved_path.exists():
            return _err(f"path does not exist: {source_path}", status=400)
        if resolved_path.is_dir():
            return _err("source_path must be a file, not a directory", status=400)
        source_path = str(resolved_path)

    store = get_default_store()
    try:
        # Blocking store read/write (meta.json + up to 25 MiB current.html) —
        # offload off the event loop.
        await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    await _run_off_loop(lambda: store.relocate(slug, source_path))
    # Reload the full artifact (with content from the new source_path) so the
    # response carries the live file bytes rather than content: null.
    art = await _run_off_loop(lambda: store.get(slug))

    _audit(
        tool="artifact_relocate",
        request=request,
        outcome="success",
        extra={"slug": slug, "source_path": source_path},
    )
    # A source_path swap changes what live reads return (Mesh-2772).
    _notify_artifact_update(state, slug, art.version)
    return _json_response(_serialize(art, include_content=True))


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
        folders = await loop.run_in_executor(subprocess_executor(), fstore.list_with_counts, store)
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
    _audit(
        tool="artifact_folder_update", request=request, outcome="success", extra={"folder_id": fid}
    )
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


# ── Comments ──────────────────────────────────────────────────────────────────


async def api_artifact_comments(request: web.Request) -> web.Response:
    """GET /api/artifacts/{slug}/comments — list durable local comments."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        return _err("restricted session", status=403)
    slug = request.match_info["slug"]
    store = get_default_store()

    try:
        # Existence check + sidecar read are blocking filesystem IO (store.get
        # reads current.html up to MAX_CONTENT_BYTES = 25 MiB); offload off the
        # event loop (no-blocking-call-on-event-loop).
        await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    # Surfaced to the UI so a provider-side failure would be visible rather than
    # silently dropped. Always None in the public fork (no remote comment sync).
    remote_sync_error: str | None = None

    comments = await _run_off_loop(lambda: store.list_comments(slug))
    result = []
    for c in comments:
        entry: dict[str, Any] = {
            "id": c.id,
            "origin": c.origin,
            "provider": c.provider,
            "scope": c.scope,
            "author": c.author,
            "is_agent": c.is_agent,
            "body": _redact_text(c.body),
            "thread_id": c.thread_id,
            "parent_id": c.parent_id,
            "status": c.status,
            "sync_state": c.sync_state,
            "anchor_orphaned": c.anchor_orphaned,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        }
        if c.anchor_quote:
            entry["anchor"] = {
                "quote": c.anchor_quote,
                "prefix": c.anchor_prefix,
                "suffix": c.anchor_suffix,
                "start_offset": c.anchor_start_offset,
                "end_offset": c.anchor_end_offset,
                "version_number": c.anchor_version,
            }
        result.append(entry)
    return _json_response({"comments": result, "remote_sync_error": remote_sync_error})


async def api_artifact_post_comment(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/comments — create a new comment."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_post_comment",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session", status=403)

    slug = request.match_info["slug"]
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))

    text = str(body.get("text") or "").strip()
    if not text:
        return _err("text is required")
    if len(text) > 10000:
        return _err("text exceeds 10000 chars")

    # Redact before storing/sending
    text = _redact_text(text)

    scope = str(body.get("scope") or "private")
    if scope not in ("private", "shared"):
        return _err("scope must be 'private' or 'shared'")

    store = get_default_store()
    try:
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    # Build anchor if provided
    anchor_data = body.get("anchor")
    anchor_quote = None
    anchor_prefix = None
    anchor_suffix = None
    anchor_start = None
    anchor_end = None
    anchor_ver = None
    if isinstance(anchor_data, dict):
        # Anchor strings are LLM/agent-influenced (esp. on the MCP path) and are
        # echoed back to the dashboard, so redact credentials/exfil-URLs and cap
        # length — same treatment as the comment body (backend-security-controls).
        def _anchor_str(v: object) -> str | None:
            if not isinstance(v, str) or not v:
                return None
            return _redact_text(v[:2000])

        anchor_quote = _anchor_str(anchor_data.get("quote"))
        anchor_prefix = _anchor_str(anchor_data.get("prefix"))
        anchor_suffix = _anchor_str(anchor_data.get("suffix"))
        anchor_start = anchor_data.get("start_offset")
        anchor_end = anchor_data.get("end_offset")
        anchor_ver = anchor_data.get("version_number")

    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    comment_id = str(uuid.uuid4())

    # Determine if this is agent-authored
    is_agent = bool(body.get("is_agent"))

    # Author defaults to the dashboard user's alias (collaboration: comments
    # show who left them, feedback #7). Agent comments keep their explicit
    # author (or the agent badge). getpass.getuser() is the alias on dev desks.
    # The author is LLM/agent-influenced on the MCP path and echoed to the
    # dashboard, so redact + cap it like the body (backend-security-controls).
    author = _redact_text(str(body.get("author") or "")[:256])
    if not author and not is_agent:

        try:
            author = getpass.getuser()
        except Exception:
            author = ""

    comment = ArtifactComment(
        id=comment_id,
        origin="local",
        provider=None,
        scope=scope,
        author=author,
        is_agent=is_agent,
        body=text,
        anchor_quote=anchor_quote,
        anchor_prefix=anchor_prefix,
        anchor_suffix=anchor_suffix,
        anchor_start_offset=anchor_start,
        anchor_end_offset=anchor_end,
        anchor_version=anchor_ver,
        thread_id=comment_id,
        parent_id=None,
        status="open",
        target_provider=art.publication.provider if art.publication else None,
        target_external_id=art.publication.artifact_id if art.publication else None,
        sync_state="local_only",
        created_at=now,
        updated_at=now,
    )

    # If scope=shared and we have a target, post to provider — but only after
    # the same capabilities.publish governance gate that guards artifact publish.
    # A shared comment body is outbound egress (it leaves the box to the
    # provider), so posting it to an existing publication after policy revocation
    # must be denied too. Denial keeps the comment LOCAL (local_only) rather than
    # 403-ing — the local comment store is unaffected.
    gov_denied = (
        _publish_governance_denied(request, comment.target_provider or "artifactory")
        if scope == "shared" and comment.target_external_id
        else "not shared"
    )
    if scope == "shared" and comment.target_external_id and gov_denied is None:
        try:

            provider = get_provider(comment.target_provider or "artifactory")
            if Capability.COMMENTS_WRITE in provider.capabilities():
                anchor_obj = None
                if anchor_quote:
                    anchor_obj = CommentAnchor(
                        quote=anchor_quote,
                        prefix=anchor_prefix,
                        suffix=anchor_suffix,
                        start_offset=anchor_start,
                        end_offset=anchor_end,
                        version_number=anchor_ver,
                    )
                rc = await provider.post_comment(
                    external_id=comment.target_external_id,
                    body=text,
                    anchor=anchor_obj,
                )
                comment.origin = f"{comment.target_provider}:{rc.remote_id}"
                comment.sync_state = "synced"
        except Exception as exc:
            logger.warning("post_comment to provider failed: %s", exc)
            comment.sync_state = "push_failed"

    await _run_off_loop(lambda: store.add_comment(slug, comment))
    _audit(
        tool="artifact_post_comment",
        request=request,
        outcome="success",
        extra={"slug": slug, "scope": scope, "is_agent": is_agent},
    )
    return _json_response(
        {"comment": {"id": comment_id, "sync_state": comment.sync_state}}, status=201
    )


async def api_artifact_reply_comment(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/comments/{id}/reply — reply to a thread."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_reply_comment",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session", status=403)

    slug = request.match_info["slug"]
    parent_id = request.match_info["comment_id"]

    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_reply_comment",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug, "parent_id": parent_id},
        )
        return _err(str(exc))

    text = str(body.get("text") or "").strip()
    if not text:
        return _err("text is required")
    if len(text) > 10000:
        return _err("text exceeds 10000 chars")
    text = _redact_text(text)

    store = get_default_store()
    try:
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    # Find parent comment
    comments = await _run_off_loop(lambda: store.list_comments(slug))
    parent = next((c for c in comments if c.id == parent_id), None)
    if not parent:
        return _err("parent comment not found", status=404)

    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    reply_id = str(uuid.uuid4())
    is_agent = bool(body.get("is_agent"))

    # Author defaults to the dashboard user's alias (collaboration: replies show
    # who left them), mirroring the create handler. Agent replies keep their
    # explicit author. Without this, replies render as "Unknown". Redact + cap
    # the LLM/agent-influenced author before it is echoed to the dashboard.
    author = _redact_text(str(body.get("author") or "")[:256])
    if not author and not is_agent:

        try:
            author = getpass.getuser()
        except Exception:
            author = ""

    reply = ArtifactComment(
        id=reply_id,
        origin="local",
        provider=parent.provider,
        scope="shared" if parent.origin != "local" else "private",
        author=author,
        is_agent=is_agent,
        body=text,
        thread_id=parent.thread_id or parent_id,
        parent_id=parent_id,
        status=parent.status,
        target_provider=parent.target_provider
        or (art.publication.provider if art.publication else None),
        target_external_id=parent.target_external_id
        or (art.publication.artifact_id if art.publication else None),
        sync_state="local_only",
        created_at=now,
        updated_at=now,
    )

    # If parent is provider-origin, reply back to provider — gated by the same
    # capabilities.publish chokepoint as artifact publish (the reply body is
    # outbound egress). A denial keeps the reply LOCAL (local_only) instead of
    # pushing it to the provider.
    if (
        parent.origin
        and parent.origin != "local"
        and reply.target_external_id
        and _publish_governance_denied(request, reply.target_provider or "artifactory") is None
    ):
        try:

            provider = get_provider(reply.target_provider or "artifactory")
            if Capability.COMMENTS_WRITE in provider.capabilities():
                # Extract remote parent id from origin
                remote_parent_id = (
                    parent.origin.split(":", 1)[-1] if ":" in parent.origin else parent.id
                )
                rc = await provider.reply_comment(
                    external_id=reply.target_external_id,
                    parent_remote_id=remote_parent_id,
                    body=text,
                )
                reply.origin = f"{reply.target_provider}:{rc.remote_id}"
                reply.sync_state = "synced"
        except Exception as exc:
            logger.warning("reply_comment to provider failed: %s", exc)
            reply.sync_state = "push_failed"

    await _run_off_loop(lambda: store.add_comment(slug, reply))
    _audit(
        tool="artifact_reply_comment",
        request=request,
        outcome="success",
        extra={"slug": slug, "parent_id": parent_id, "is_agent": is_agent},
    )
    return _json_response({"comment": {"id": reply_id, "sync_state": reply.sync_state}}, status=201)


async def api_artifact_mark_review(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/comments/{id}/review — advance to REVIEW."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_mark_review",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session", status=403)

    slug = request.match_info["slug"]
    comment_id = request.match_info["comment_id"]

    store = get_default_store()
    try:
        await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    comments = await _run_off_loop(lambda: store.list_comments(slug))
    target = next((c for c in comments if c.id == comment_id), None)
    if not target:
        return _err("comment not found", status=404)

    # If provider-origin, mark on provider too — gated by the same
    # capabilities.publish chokepoint (a provider-side review mutation is an
    # outbound state change). A denied policy keeps the review LOCAL.
    if (
        target.origin
        and target.origin != "local"
        and target.target_external_id
        and _publish_governance_denied(request, target.target_provider or "artifactory") is None
    ):
        try:

            provider = get_provider(target.target_provider or "artifactory")
            if Capability.COMMENTS_WRITE in provider.capabilities():
                remote_id = target.origin.split(":", 1)[-1]
                await provider.mark_review(
                    external_id=target.target_external_id, remote_id=remote_id
                )
        except Exception as exc:
            logger.warning("mark_review on provider failed: %s", exc)

    await _run_off_loop(lambda: store.update_comment(slug, comment_id, status="review"))
    _audit(
        tool="artifact_mark_review",
        request=request,
        outcome="success",
        extra={"slug": slug, "comment_id": comment_id},
    )
    await _run_off_loop(
        lambda: store.record_comment_event(
            slug,
            action="reviewed",
            by="agent" if request.headers.get("X-Internal-Secret") is not None else "user",
            session_id=_event_session_id(request),
            comment_snippet=_redact_text(target.body)[:100],
        )
    )
    return _json_response({"status": "review"})


async def api_artifact_resolve_comment(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/comments/{id}/resolve — human-only resolve."""
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_resolve_comment",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session", status=403)

    slug = request.match_info["slug"]
    comment_id = request.match_info["comment_id"]

    # Agent sessions cannot resolve. Actor is inferred from the auth path
    # (X-Internal-Secret header = MCP/agent), same as api_artifact_update —
    # the legacy ``is_agent`` body flag is kept as a defense-in-depth
    # fallback but is no longer the only gate (a body field can be spoofed).
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))

    if request.headers.get("X-Internal-Secret") is not None or body.get("is_agent"):
        return _err("agents cannot resolve comments — human-only", status=403)

    store = get_default_store()
    try:
        await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    resolved = await _run_off_loop(
        lambda: store.update_comment(slug, comment_id, status="resolved")
    )
    if resolved is None:
        return _err("comment not found", status=404)
    _audit(
        tool="artifact_resolve_comment",
        request=request,
        outcome="success",
        extra={"slug": slug, "comment_id": comment_id},
    )
    await _run_off_loop(
        lambda: store.record_comment_event(
            slug,
            action="resolved",
            by="user",
            session_id=_event_session_id(request),
            comment_snippet=_redact_text(resolved.body)[:100],
        )
    )
    return _json_response({"status": "resolved"})


async def api_artifact_reopen_comment(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/comments/{id}/reopen — reopen a resolved
    thread (set status back to open). Feedback #1: resolving was a one-way door.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_reopen_comment",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session", status=403)

    slug = request.match_info["slug"]
    comment_id = request.match_info["comment_id"]

    store = get_default_store()
    try:
        await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    if await _run_off_loop(lambda: store.update_comment(slug, comment_id, status="open")) is None:
        return _err("comment not found", status=404)
    _audit(
        tool="artifact_reopen_comment",
        request=request,
        outcome="success",
        extra={"slug": slug, "comment_id": comment_id},
    )
    return _json_response({"status": "open"})


async def api_artifact_delete_comment(request: web.Request) -> web.Response:
    """DELETE /api/artifacts/{slug}/comments/{id} — delete a comment.

    Actor is inferred from how the request was authed (X-Internal-Secret
    header = MCP/agent; absent = dashboard/human) — never from a body flag,
    which could be spoofed. Agent deletes carry extra contract:

      * ``reason`` (body, required for agents) — the one-line justification
        recorded in the SEL audit and the artifact's activity feed. The
        disposition policy (artifacts skill): delete only comments that were
        unambiguous directives fully applied; judgment calls go through
        mark_review instead.
      * provider-synced comments are refused (403) — provider reconciliation
        would resurrect or desync them; the agent should mark REVIEW and let
        the human act on the provider.

    Human dashboard deletes are unchanged (no reason required, provider
    cascade preserved).
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_delete_comment",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session", status=403)

    slug = request.match_info["slug"]
    comment_id = request.match_info["comment_id"]

    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # Same auth-derived actor inference as api_artifact_update: MCP-originated
    # calls carry X-Internal-Secret (validated upstream); browser calls don't.
    is_agent = request.headers.get("X-Internal-Secret") is not None
    # The delete reason is agent/LLM-supplied and lands in the SEL audit AND the
    # artifact activity feed (dashboard), so redact credentials/exfil URLs before
    # it is persisted or echoed (backend-security-controls) — same treatment as
    # comment bodies / author / anchors.
    reason = _redact_text(str(body.get("reason") or "").strip()[:500])

    store = get_default_store()
    try:
        await _run_off_loop(lambda: store.get(slug))  # verify artifact exists
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    comments = await _run_off_loop(lambda: store.list_comments(slug))
    target = next((c for c in comments if c.id == comment_id), None)
    is_provider_origin = bool(target and target.origin and target.origin != "local")

    if is_agent:
        if not reason:
            _audit(
                tool="artifact_delete_comment",
                request=request,
                outcome="denied",
                error="missing reason",
                extra={"slug": slug, "comment_id": comment_id, "actor": "agent"},
            )
            return _err("agent deletes require a reason")
        if is_provider_origin:
            _audit(
                tool="artifact_delete_comment",
                request=request,
                outcome="denied",
                error="provider-synced comment",
                extra={"slug": slug, "comment_id": comment_id, "actor": "agent"},
            )
            return _err(
                "agents cannot delete provider-synced comments — "
                "use artifact_mark_review instead",
                status=403,
            )

    # If provider-origin, delete on provider (human dashboard path only —
    # agent requests were refused above) — gated by the same capabilities.publish
    # chokepoint (a provider-side delete is an outbound mutation). A denied policy
    # deletes only the local copy.
    if (
        target
        and is_provider_origin
        and target.target_external_id
        and _publish_governance_denied(request, target.target_provider or "artifactory") is None
    ):
        try:

            provider = get_provider(target.target_provider or "artifactory")
            if Capability.COMMENTS_WRITE in provider.capabilities():
                remote_id = target.origin.split(":", 1)[-1]
                await provider.delete_comment(
                    external_id=target.target_external_id, remote_id=remote_id
                )
        except Exception as exc:
            logger.warning("delete_comment on provider failed: %s", exc)

    found = await _run_off_loop(lambda: store.delete_comment(slug, comment_id))
    if not found:
        return _err("comment not found", status=404)

    snippet = _redact_text(target.body)[:100] if target else ""
    actor = "agent" if is_agent else "user"
    audit_extra: dict[str, Any] = {
        "slug": slug,
        "comment_id": comment_id,
        "actor": actor,
        "comment_snippet": snippet,
    }
    if reason:
        audit_extra["reason"] = reason
    _audit(
        tool="artifact_delete_comment",
        request=request,
        outcome="success",
        extra=audit_extra,
    )
    await _run_off_loop(
        lambda: store.record_comment_event(
            slug,
            action="deleted",
            by=actor,
            session_id=_event_session_id(request),
            comment_snippet=snippet,
            reason=reason or None,
        )
    )
    return _json_response({"deleted": True})


async def api_artifact_edit_comment(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/comments/{id} — edit a comment's body.

    Local comments always edit in place (the store mutator patches ``body`` and
    bumps ``updated_at``). For a provider-origin comment whose provider supports
    in-place edit (``Capability.COMMENTS_EDIT`` — Chorus), the new body is also
    pushed to the provider, preserving the remote id / thread / replies.
    Providers without that capability (Artifactory / MarkBin / Pippin) edit
    locally only; the response's ``remote_synced`` flag is False so the UI can
    surface that the change stayed local rather than silently diverging.

    Status (open/review/resolved) is untouched — that's what resolve/reopen/
    review are for. Authorship (``author`` / ``is_agent``) is preserved.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_edit_comment",
            request=request,
            outcome="denied",
            extra={"reason": "restricted_session"},
        )
        return _err("restricted session", status=403)

    slug = request.match_info["slug"]
    comment_id = request.match_info["comment_id"]

    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))

    text = str(body.get("text") or "").strip()
    if not text:
        return _err("text is required")
    if len(text) > 10000:
        return _err("text exceeds 10000 chars")
    # Never trust the incoming body — redact before storing/sending, same as
    # post/reply (AUTOSDE security-controls).
    text = _redact_text(text)

    store = get_default_store()
    try:
        await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    comments = await _run_off_loop(lambda: store.list_comments(slug))
    target = next((c for c in comments if c.id == comment_id), None)
    if target is None:
        return _err("comment not found", status=404)

    # Preserve the agent watermark: an edit must never strip the 🤖 mark from an
    # agent-authored comment (artifact_post_comment prepends it on create).
    # Centralized here so both the UI edit and the MCP artifact_update_comment
    # tool keep the invariant regardless of what body they send.
    if target.is_agent and not text.startswith("\U0001f916"):
        text = f"\U0001f916 {text}"

    # Push the edit to the provider in place when its origin provider supports
    # it (Chorus). Others edit locally only. Gated by the same capabilities.publish
    # chokepoint as artifact publish — the edited body is outbound egress, so a
    # denied policy keeps the edit LOCAL (remote_synced stays False).
    remote_synced = False
    if (
        target.origin
        and target.origin != "local"
        and target.target_external_id
        and _publish_governance_denied(request, target.target_provider or "artifactory") is None
    ):
        try:
            provider = get_provider(target.target_provider or "artifactory")
            if Capability.COMMENTS_EDIT in provider.capabilities():
                remote_id = target.origin.split(":", 1)[-1]
                await provider.edit_comment(
                    external_id=target.target_external_id,
                    remote_id=remote_id,
                    body=text,
                )
                remote_synced = True
        except Exception as exc:
            logger.warning("edit_comment on provider failed: %s", exc)

    if await _run_off_loop(lambda: store.update_comment(slug, comment_id, body=text)) is None:
        return _err("comment not found", status=404)

    _audit(
        tool="artifact_edit_comment",
        request=request,
        outcome="success",
        extra={"slug": slug, "comment_id": comment_id, "remote_synced": remote_synced},
    )
    return _json_response({"comment": {"id": comment_id, "remote_synced": remote_synced}})


# ── Provider negotiation (Mesh-2445) ─────────────────────────────────────────


def _sharing_model_dict(sm: Any) -> dict[str, Any]:
    return {
        "supports_private": sm.supports_private,
        "supports_shared": sm.supports_shared,
        "supports_public": sm.supports_public,
        "principal_kind": sm.principal_kind,
        "supports_roles": sm.supports_roles,
        "supports_expiration": sm.supports_expiration,
        "programmable": sm.programmable,
        "out_of_band_url": sm.out_of_band_url,
    }


async def api_artifact_publish_providers(request: web.Request) -> web.Response:
    """GET /api/artifacts/publish-providers?kind=<kind> — available publishing
    providers with per-kind support + sharing/sync/discovery descriptors.

    Drives the share-panel picker: the FE shows a provider selector only when
    >1 *available* provider can host the artifact's kind (``kind_support !=
    unsupported``), and renders the right sharing controls per provider. Read-
    only; no mutation, so no restricted-session gate (matches the list endpoint).
    """
    kind = request.query.get("kind") or "widget"
    out: list[dict[str, Any]] = []
    for p in list_providers():
        try:
            if not p.available():
                continue
            ks = p.kind_support(kind)
            sm = p.sharing_model()
            sy = p.sync_model()
            dm = p.discovery_model()
        except Exception as exc:  # pragma: no cover — a flaky provider must not break the picker
            logger.warning("publish-providers: skipping %r: %s", getattr(p, "name", "?"), exc)
            continue
        out.append(
            {
                "name": p.name,
                "display_name": p.display_name,
                "capabilities": sorted(c.value for c in p.capabilities()),
                "kind_support": ks.value,
                "capable": ks != KindSupport.UNSUPPORTED,
                "sharing_model": _sharing_model_dict(sm),
                "sync_model": {
                    "authority": sy.authority,
                    "concurrency": sy.concurrency,
                    "collab_mode": sy.collab_mode,
                },
                "discovery_model": {
                    "list_mine": dm.list_mine,
                    "list_shared_with_me": dm.list_shared_with_me,
                    "list_public": dm.list_public,
                    "full_text_search": dm.full_text_search,
                    "pull_by_id": dm.pull_by_id,
                },
            }
        )
    return _json_response({"providers": out, "kind": kind})
