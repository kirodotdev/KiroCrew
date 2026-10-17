"""Remote artifacts: browsing, cloning, forking and reading an artifact a publish
provider hosts, and its comments, read and written straight through to the provider
behind a short in-memory cache.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew.artifacts import ArtifactValidationError, get_default_store
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import (
    _SERIALIZE_REDACTED_KEYS,
    _redact_remote_response,
    _redact_text,
    _serialize,
)
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _REMOTE_PROVIDER_TIMEOUT_S,
    _audit,
    _err,
    _json_response,
    _non_owner_refusal,
    _publish_governance_denied,
    _read_json_body,
    _run_off_loop,
    logger,
)
from kiro_crew.publish_provider import (
    DEFAULT_PROVIDER,
    Capability,
    CommentAnchor,
    PublishUnavailableError,
    get_provider,
    is_registered,
)


# ── Remote artifacts (provider-routed browse / clone / fork) ─────────────────
def _annotate_local_slugs(out: dict[str, Any], index: dict[str, str], provider: str = "") -> None:
    """Annotate each browse row with ``local_slug`` (the local copy if already
    cloned/forked) so the UI shows open-vs-clone without a round-trip.

    ``index`` is a prebuilt map (one off-loop store scan via
    ``ArtifactStore.index_by_artifact_id``) — NOT a per-row store scan on the
    event loop. Lookups are provider-namespaced (``provider\\x00id``) so a
    browse against provider B never annotates provider A's local copy that
    happens to share an id; the bare-id key is tried only as a legacy fallback
    for records that predate provider tracking. Must run on the UN-redacted
    rows: a high-entropy ``external_id`` can be rewritten to
    ``[REDACTED: credential]`` by the credential heuristic, which would miss the
    local match and wrongly offer Clone instead of Open."""

    items = out.get("artifacts")
    if not isinstance(items, list):
        return
    # A legacy no-provider record only emits a bare-id key (see
    # index_by_artifact_id), and such a record originated from the DEFAULT
    # provider — so the bare-id fallback may resolve ONLY a browse against that
    # same default provider. Applying it to an arbitrary provider B would let
    # B's row inherit provider A's legacy slug on a shared id, wrongly marking
    # B's artifact already-local and hiding its clone/fork action (mirrors the
    # _provider_ok gate in find_by_artifact_id).
    allow_bare = provider == DEFAULT_PROVIDER or not provider
    for item in items:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("external_id") or item.get("artifactId") or item.get("id") or "")
        if not aid:
            item["local_slug"] = None
            continue
        scoped = index.get(get_default_store().artifact_index_key(provider, aid))
        if scoped is not None:
            item["local_slug"] = scoped
        else:
            item["local_slug"] = index.get(aid) if allow_bare else None


async def api_remote_artifacts_browse(request: web.Request) -> web.Response:
    """GET /api/remote-artifacts/{provider}/browse?scope=&q= — provider-routed
    discovery via ``list_remote`` / ``search_remote``.

    A non-empty ``q`` runs full-text ``search_remote`` (providers whose
    ``discovery_model().full_text_search`` is True); otherwise
    ``list_remote(scope)``. ``None`` from the provider means that discovery
    primitive isn't supported (400; an unsupported scope listing also carries
    ``code: "scope_unsupported"``). Gated like other reads-with-state. A
    provider name with nothing registered under it answers 404; in the public
    edition the registry is empty, so every browse returns 404 — the surface is
    inert until a companion registers a provider. A registered provider that
    raises ``PublishUnavailableError`` answers 503.
    """
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_browse_remote",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot browse remote artifacts", status=403)
    provider_name = request.match_info.get("provider", "")
    scope = request.rel_url.query.get("scope", "mine")
    query = request.rel_url.query.get("q") or ""
    page_token = request.rel_url.query.get("pageToken")
    if not is_registered(provider_name):
        return _err(_redact_text(f"unknown publish provider: {provider_name!r}"), status=404)
    try:
        provider = get_provider(provider_name)
    except PublishUnavailableError as exc:
        # A registered provider whose tooling is unavailable.
        return _err(_redact_text(str(exc)), status=503)
    except Exception as exc:
        return _err(_redact_text(str(exc)), status=502)
    try:
        if query:
            result = await asyncio.wait_for(
                provider.search_remote(query=query, page_token=page_token),
                timeout=_REMOTE_PROVIDER_TIMEOUT_S,
            )
        else:
            result = await asyncio.wait_for(
                provider.list_remote(scope=scope, page_token=page_token),
                timeout=_REMOTE_PROVIDER_TIMEOUT_S,
            )
    except asyncio.TimeoutError:
        # Bounded provider read (CWE-400): a hung list/search maps to a Gateway
        # Timeout rather than blocking indefinitely. Distinct outcome so the SEL
        # feed separates timeouts from other provider errors (mirrors
        # api_remote_artifact_get's timeout branch).
        _audit(
            tool="artifact_browse_remote",
            request=request,
            outcome="timeout",
            error=f"provider read exceeded {_REMOTE_PROVIDER_TIMEOUT_S:g}s",
            extra={"provider": provider_name},
        )
        return _err("remote provider timed out", status=504)
    except Exception as exc:
        # Non-timeout provider failure — surface a neutral, non-empty reason
        # (str(exc) can be empty) so the SEL feed and the client both see a real
        # message without mislabeling it as a timeout.
        reason = _redact_text(str(exc)) or "remote provider error"
        _audit(
            tool="artifact_browse_remote",
            request=request,
            outcome="error",
            error=reason,
            extra={"provider": provider_name},
        )
        return _err(reason, status=502)
    if result is None:
        if query:
            return _err(f"{provider_name} does not support full-text search", status=400)
        # A scope the provider does not list is not a failure: the code lets the
        # browse UI move on to the next scope the provider advertises instead of
        # rendering an error card, and keeps it apart from a provider that raised.
        return _json_response(
            {
                "error": f"{provider_name} does not support {scope} listing",
                "code": "scope_unsupported",
            },
            status=400,
        )
    # Annotate on the UN-redacted rows (external_ids intact) using a single
    # off-loop store scan, THEN redact — annotating after redaction would look up
    # a credential-shaped external_id in its ``[REDACTED]`` form and miss the
    # local match. The per-row scan is replaced by one indexed scan off the loop.
    index = await _run_off_loop(lambda: get_default_store().index_by_artifact_id())
    _annotate_local_slugs(result, index, provider_name)
    out = _redact_remote_response(result)
    _audit(
        tool="artifact_browse_remote",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "scope": "search" if query else scope},
    )
    return _json_response(out)


async def api_remote_artifacts_clone(request: web.Request) -> web.Response:
    """POST /api/remote-artifacts/{provider}/clone (``external_id`` in the JSON
    body) — provider-routed bidirectional clone (sets a ``publication``;
    collab_mode from the provider).

    Governance: a clone arms future egress — ``clone_from_remote`` sets
    ``auto_sync=True``, so every later local snapshot auto-pushes to the remote
    via the ungated ``push_version``. The ``capabilities.publish`` ceiling is
    therefore enforced HERE, before the clone binds the two copies (same
    fail-closed gate as publish). Fork (pull-only lineage) stays ungated.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.remote_artifacts_clone")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot clone artifacts", status=403)
    provider_name = request.match_info.get("provider", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    external_id = str(body.get("external_id") or "").strip()
    if not external_id:
        return _err("external_id is required")
    gov_denial = _publish_governance_denied(request, provider_name)
    if gov_denial is not None:
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(gov_denial, status=403)
    try:
        art = await publish_sync.clone_from_remote(external_id, provider_name=provider_name)
    except PublishUnavailableError as exc:
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=503)
    except Exception as exc:
        _audit(
            tool="artifact_clone",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)
    _audit(
        tool="artifact_clone",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id, "slug": art.slug},
    )
    return _json_response(
        _redact_remote_response(
            _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
        ),
        status=201,
    )


async def api_remote_artifacts_fork(request: web.Request) -> web.Response:
    """POST /api/remote-artifacts/{provider}/fork (``external_id`` in the JSON
    body) — provider-routed fork (independent copy with pull-only
    ``fork_metadata`` lineage). Ingress only — never arms a push — so no publish
    governance gate."""
    owner_denied = await _non_owner_refusal(request, "artifacts.remote_artifacts_fork")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_fork",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot fork artifacts", status=403)
    provider_name = request.match_info.get("provider", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    external_id = str(body.get("external_id") or "").strip()
    if not external_id:
        return _err("external_id is required")
    try:
        art = await publish_sync.fork_from_remote(external_id, provider_name=provider_name)
    except PublishUnavailableError as exc:
        _audit(
            tool="artifact_fork",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=503)
    except Exception as exc:
        _audit(
            tool="artifact_fork",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)
    _audit(
        tool="artifact_fork",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id, "slug": art.slug},
    )
    return _json_response(
        _redact_remote_response(
            _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
        ),
        status=201,
    )


def _audit_remote_denied(tool: str, request: web.Request, reason: str) -> None:
    """Emit a denied SEL event for a remote-artifact permission rejection.

    Every permission decision on the remote-artifact endpoints must produce an
    SEL record (backend-security-controls) — both the restricted-session guard
    and the provider capability-gate rejection. Provider/external_id are pulled
    from the route match so the event carries the same context as the
    success/error audits, without assuming the local vars are bound yet.
    """
    _audit(
        tool=tool,
        request=request,
        outcome="denied",
        error=reason,
        extra={
            "provider": request.match_info.get("provider", ""),
            "external_id": request.match_info.get("external_id", ""),
        },
    )


async def api_remote_artifact_get(request: web.Request) -> web.Response:
    """GET /api/remote-artifacts/{provider}/{external_id} — fetch one remote artifact.

    Provider-neutral read of a provider-hosted artifact the user has no local
    copy of — the content source for the remote-detail view. Routes through the
    registered provider's ``fetch_content`` (``Capability.CONTENT_PULL``); the
    returned ``{content, content_type, title, owner, visibility, ...}`` is
    redacted before it leaves the process. A provider name with nothing
    registered under it (every name in the public default) answers 404; a
    registered provider that fails answers 502. Read-only, so no
    publish-governance gate.
    """
    provider_name = request.match_info["provider"]
    external_id = request.match_info["external_id"]

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit_remote_denied(
            "remote_artifact_fetch",
            request,
            "restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session", status=403)

    if not is_registered(provider_name):
        error = _redact_text(f"unknown publish provider: {provider_name!r}")
        _audit(
            tool="remote_artifact_fetch",
            request=request,
            outcome="error",
            error=error,
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(error, status=404)
    try:
        provider = get_provider(provider_name)
        if Capability.CONTENT_PULL not in provider.capabilities():
            _audit_remote_denied("remote_artifact_fetch", request, "provider lacks CONTENT_PULL")
            return _err(f"{provider_name} does not support fetching content", status=400)
        result = await asyncio.wait_for(
            provider.fetch_content(external_id=external_id),
            timeout=_REMOTE_PROVIDER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # Bounded provider read (CWE-400): a hung provider maps to a Gateway
        # Timeout rather than blocking the request indefinitely. Distinct
        # outcome so the SEL feed separates timeouts from other provider errors.
        _audit(
            tool="remote_artifact_fetch",
            request=request,
            outcome="timeout",
            error=f"provider read exceeded {_REMOTE_PROVIDER_TIMEOUT_S:g}s",
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err("remote provider timed out", status=504)
    except Exception as exc:  # noqa: BLE001 — provider failure must not 500 the view
        _audit(
            tool="remote_artifact_fetch",
            request=request,
            outcome="error",
            error=_redact_text(str(exc)),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)

    if result is None:
        return _err("remote artifact not found or unreadable", status=404)

    _audit(
        tool="remote_artifact_fetch",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id},
    )
    # Redact the provider payload (content + any metadata) before it leaves. The
    # content can be large (up to MAX_CONTENT_BYTES), so run the redaction regex
    # off the event loop — same discipline as api_artifact_comments.
    payload = dict(result)
    redacted = await _run_off_loop(lambda: _redact_remote_response(payload))
    return _json_response(redacted)


# ── Remote-artifact comments (browse a provider-hosted artifact directly) ────
# A user can open an artifact hosted by a publish provider that they do NOT have
# a local copy of (via the remote-detail page). These endpoints read/write that
# artifact's comments straight through to the provider — there is no local store
# to mirror into — with a short in-memory TTL cache in front of the GET so
# repeated views don't re-hit the provider. When no provider is registered (the
# public default) get_provider raises and every endpoint degrades to a clear
# error rather than a 500.

_REMOTE_COMMENT_TTL_SECS = 300
# Cap retained entries so the cache tracks ACTIVE artifacts, not lifetime browse
# count: without a bound, every distinct provider:external_id ever opened would
# live for the process lifetime (a slow leak on long-running gateways).
_REMOTE_COMMENT_CACHE_MAX = 256
# key "provider:external_id" -> (fetched_at_monotonic, serialized_comments)
_remote_comment_cache: "OrderedDict[str, tuple[float, list[dict[str, Any]]]]" = OrderedDict()


def _remote_cache_sweep(now: float) -> None:
    """Evict entries older than the TTL so cache size tracks active artifacts."""
    stale = [
        k for k, (ts, _) in _remote_comment_cache.items() if now - ts >= _REMOTE_COMMENT_TTL_SECS
    ]
    for k in stale:
        _remote_comment_cache.pop(k, None)


def _remote_cache_put(key: str, value: "tuple[float, list[dict[str, Any]]]") -> None:
    """Insert/refresh an entry LRU-style and evict the oldest past the cap."""
    _remote_comment_cache.pop(key, None)  # move-to-end on refresh
    _remote_comment_cache[key] = value
    while len(_remote_comment_cache) > _REMOTE_COMMENT_CACHE_MAX:
        _remote_comment_cache.popitem(last=False)  # evict oldest


def _serialize_remote_comment(rc: Any, provider: str) -> dict[str, Any]:
    """Serialize a provider RemoteComment to the same wire shape the local
    comments endpoint uses, so the frontend renders both identically.

    ``provider`` is the route's provider name — the origin/provider fields are
    keyed off it (never a hardcoded provider) so this stays provider-neutral.
    """
    entry: dict[str, Any] = {
        "id": rc.remote_id,
        "origin": f"{provider}:{rc.remote_id}",
        "provider": provider,
        "scope": "shared",
        # Provider-controlled author string — redact credentials/exfil URLs like
        # the body/anchor before it reaches the dashboard (backend-security-controls).
        "author": _redact_text(rc.author),
        "is_agent": rc.is_agent,
        "body": _redact_text(rc.body),
        "thread_id": rc.thread_id,
        "parent_id": rc.parent_id,
        "status": rc.status,
        "sync_state": "synced",
        "created_at": rc.created_at,
        "updated_at": rc.updated_at,
    }
    if rc.anchor and rc.anchor.quote:
        # Provider-controlled anchor text is echoed to the dashboard, so redact
        # credentials/exfil-URLs like the body (backend-security-controls).
        entry["anchor"] = {
            "quote": _redact_text(rc.anchor.quote),
            "prefix": (_redact_text(rc.anchor.prefix) if rc.anchor.prefix else rc.anchor.prefix),
            "suffix": (_redact_text(rc.anchor.suffix) if rc.anchor.suffix else rc.anchor.suffix),
            "start_offset": rc.anchor.start_offset,
            "end_offset": rc.anchor.end_offset,
            "version_number": rc.anchor.version_number,
        }
    return entry


def _invalidate_remote_comment_cache(provider: str, external_id: str) -> None:
    _remote_comment_cache.pop(f"{provider}:{external_id}", None)


async def api_remote_artifact_comments(request: web.Request) -> web.Response:
    """GET /api/remote-artifacts/{provider}/{external_id}/comments.

    Fetch comments for a provider-hosted artifact (no local store). TTL-cached
    in memory. Provider failures surface as ``remote_sync_error`` rather than a
    500, so the remote detail view still renders content.
    """
    provider_name = request.match_info["provider"]
    external_id = request.match_info["external_id"]
    cache_key = f"{provider_name}:{external_id}"

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit_remote_denied(
            "remote_artifact_comments",
            request,
            "restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session", status=403)

    now = time.monotonic()
    _remote_cache_sweep(now)
    cached = _remote_comment_cache.get(cache_key)
    if cached and (now - cached[0]) < _REMOTE_COMMENT_TTL_SECS:
        return _json_response({"comments": cached[1], "remote_sync_error": None, "cached": True})

    comments: list[dict[str, Any]] = []
    remote_sync_error: str | None = None
    try:
        provider = get_provider(provider_name)
        if Capability.COMMENTS_READ not in provider.capabilities():
            _audit_remote_denied(
                "remote_artifact_comments", request, "provider lacks COMMENTS_READ"
            )
            remote_sync_error = f"{provider_name} does not support comments"
        else:
            remote = await asyncio.wait_for(
                provider.fetch_comments(external_id=external_id),
                timeout=_REMOTE_PROVIDER_TIMEOUT_S,
            )
            comments = [
                _serialize_remote_comment(rc, provider_name) for rc in remote if not rc.deleted
            ]
            _remote_cache_put(cache_key, (time.monotonic(), comments))
    except asyncio.TimeoutError:
        # Bounded provider read (CWE-400). This view degrades rather than 500s
        # (docstring contract), so a hung provider becomes a non-empty
        # remote_sync_error the detail view can show — str(TimeoutError()) is
        # empty. Still audited as a distinct ``timeout`` outcome.
        _audit(
            tool="remote_artifact_comments",
            request=request,
            outcome="timeout",
            error=f"provider comment fetch exceeded {_REMOTE_PROVIDER_TIMEOUT_S:g}s",
            extra={"provider": provider_name, "external_id": external_id},
        )
        remote_sync_error = "remote provider timed out"
    except Exception as exc:  # noqa: BLE001 — provider failure must not 500 the view
        logger.warning("remote comments fetch failed for %s: %s", cache_key, exc)
        remote_sync_error = _redact_text(str(exc))

    return _json_response({"comments": comments, "remote_sync_error": remote_sync_error})


async def api_remote_artifact_post_comment(request: web.Request) -> web.Response:
    """POST /api/remote-artifacts/{provider}/{external_id}/comments — scope=shared."""
    owner_denied = await _non_owner_refusal(request, "artifacts.remote_artifact_post_comment")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit_remote_denied(
            "remote_artifact_post_comment",
            request,
            "restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session", status=403)

    provider_name = request.match_info["provider"]
    external_id = request.match_info["external_id"]
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))

    text = str(body.get("text") or "").strip()
    if not text:
        return _err("text is required")
    if len(text) > 10000:
        return _err("text exceeds 10000 chars")
    text = _redact_text(text)

    # Writing a comment to a provider-hosted artifact is outbound egress (bytes
    # leave the box), so it goes through the same fail-closed capabilities.publish
    # gate as artifact publish and the local shared-comment path — otherwise a
    # policy denying publish for this provider would be bypassed. No local mirror
    # to fall back to here, so a denial is an audited 403.
    gov_denial = _publish_governance_denied(request, provider_name)
    if gov_denial is not None:
        _audit(
            tool="remote_artifact_post_comment",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(gov_denial, status=403)

    try:
        provider = get_provider(provider_name)
        if Capability.COMMENTS_WRITE not in provider.capabilities():
            _audit_remote_denied(
                "remote_artifact_post_comment", request, "provider lacks COMMENTS_WRITE"
            )
            return _err(f"{provider_name} does not support comments", status=400)

        anchor_obj = None
        anchor_data = body.get("anchor")
        if isinstance(anchor_data, dict) and anchor_data.get("quote"):
            anchor_obj = CommentAnchor(
                quote=anchor_data.get("quote"),
                prefix=anchor_data.get("prefix"),
                suffix=anchor_data.get("suffix"),
                start_offset=anchor_data.get("start_offset"),
                end_offset=anchor_data.get("end_offset"),
                version_number=anchor_data.get("version_number"),
            )
        rc = await asyncio.wait_for(
            provider.post_comment(external_id=external_id, body=text, anchor=anchor_obj),
            timeout=_REMOTE_PROVIDER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _audit(
            tool="remote_artifact_post_comment",
            request=request,
            outcome="timeout",
            error=f"provider comment write exceeded {_REMOTE_PROVIDER_TIMEOUT_S:g}s",
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err("remote provider timed out", status=504)
    except Exception as exc:  # noqa: BLE001
        _audit(
            tool="remote_artifact_post_comment",
            request=request,
            outcome="error",
            error=_redact_text(str(exc)),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)

    _invalidate_remote_comment_cache(provider_name, external_id)
    _audit(
        tool="remote_artifact_post_comment",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id},
    )
    return _json_response({"comment": _serialize_remote_comment(rc, provider_name)}, status=201)


async def api_remote_artifact_reply_comment(request: web.Request) -> web.Response:
    """POST /api/remote-artifacts/{provider}/{external_id}/comments/{id}/reply."""
    owner_denied = await _non_owner_refusal(request, "artifacts.remote_artifact_reply_comment")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit_remote_denied(
            "remote_artifact_reply_comment",
            request,
            "restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session", status=403)

    provider_name = request.match_info["provider"]
    external_id = request.match_info["external_id"]
    parent_id = request.match_info["comment_id"]
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))

    text = str(body.get("text") or "").strip()
    if not text:
        return _err("text is required")
    if len(text) > 10000:
        return _err("text exceeds 10000 chars")
    text = _redact_text(text)

    # Replying is outbound egress to the provider — same fail-closed publish gate.
    gov_denial = _publish_governance_denied(request, provider_name)
    if gov_denial is not None:
        _audit(
            tool="remote_artifact_reply_comment",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(gov_denial, status=403)

    try:
        provider = get_provider(provider_name)
        if Capability.COMMENTS_WRITE not in provider.capabilities():
            _audit_remote_denied(
                "remote_artifact_reply_comment", request, "provider lacks COMMENTS_WRITE"
            )
            return _err(f"{provider_name} does not support comments", status=400)
        rc = await asyncio.wait_for(
            provider.reply_comment(external_id=external_id, parent_remote_id=parent_id, body=text),
            timeout=_REMOTE_PROVIDER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _audit(
            tool="remote_artifact_reply_comment",
            request=request,
            outcome="timeout",
            error=f"provider comment reply exceeded {_REMOTE_PROVIDER_TIMEOUT_S:g}s",
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err("remote provider timed out", status=504)
    except Exception as exc:  # noqa: BLE001
        _audit(
            tool="remote_artifact_reply_comment",
            request=request,
            outcome="error",
            error=_redact_text(str(exc)),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)

    _invalidate_remote_comment_cache(provider_name, external_id)
    _audit(
        tool="remote_artifact_reply_comment",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id},
    )
    return _json_response({"comment": _serialize_remote_comment(rc, provider_name)}, status=201)


async def api_remote_artifact_mark_review(request: web.Request) -> web.Response:
    """POST /api/remote-artifacts/{provider}/{external_id}/comments/{id}/review.

    Advance a shared-artifact comment thread to REVIEW on the provider. The
    user does not own this artifact locally, so the status change writes
    straight through to the source.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.remote_artifact_mark_review")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit_remote_denied(
            "remote_artifact_mark_review",
            request,
            "restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session", status=403)

    provider_name = request.match_info["provider"]
    external_id = request.match_info["external_id"]
    comment_id = request.match_info["comment_id"]

    # Advancing a thread's status mutates provider-side state — same publish gate.
    gov_denial = _publish_governance_denied(request, provider_name)
    if gov_denial is not None:
        _audit(
            tool="remote_artifact_mark_review",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(gov_denial, status=403)

    try:
        provider = get_provider(provider_name)
        if Capability.COMMENTS_WRITE not in provider.capabilities():
            _audit_remote_denied(
                "remote_artifact_mark_review", request, "provider lacks COMMENTS_WRITE"
            )
            return _err(f"{provider_name} does not support comments", status=400)
        await asyncio.wait_for(
            provider.mark_review(external_id=external_id, remote_id=comment_id),
            timeout=_REMOTE_PROVIDER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _audit(
            tool="remote_artifact_mark_review",
            request=request,
            outcome="timeout",
            error=f"provider mark-review exceeded {_REMOTE_PROVIDER_TIMEOUT_S:g}s",
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err("remote provider timed out", status=504)
    except Exception as exc:  # noqa: BLE001
        _audit(
            tool="remote_artifact_mark_review",
            request=request,
            outcome="error",
            error=_redact_text(str(exc)),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)

    _invalidate_remote_comment_cache(provider_name, external_id)
    _audit(
        tool="remote_artifact_mark_review",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id},
    )
    return _json_response({"status": "review"})


async def api_remote_artifact_delete_comment(request: web.Request) -> web.Response:
    """DELETE /api/remote-artifacts/{provider}/{external_id}/comments/{id}.

    Delete a shared-artifact comment on the provider (writes through to the
    source — the user has no local copy to mirror it in).
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.remote_artifact_delete_comment")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit_remote_denied(
            "remote_artifact_delete_comment",
            request,
            "restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session", status=403)

    provider_name = request.match_info["provider"]
    external_id = request.match_info["external_id"]
    comment_id = request.match_info["comment_id"]

    # Deleting a provider comment mutates provider-side state — same publish gate.
    gov_denial = _publish_governance_denied(request, provider_name)
    if gov_denial is not None:
        _audit(
            tool="remote_artifact_delete_comment",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(gov_denial, status=403)

    try:
        provider = get_provider(provider_name)
        if Capability.COMMENTS_WRITE not in provider.capabilities():
            _audit_remote_denied(
                "remote_artifact_delete_comment", request, "provider lacks COMMENTS_WRITE"
            )
            return _err(f"{provider_name} does not support comments", status=400)
        await asyncio.wait_for(
            provider.delete_comment(external_id=external_id, remote_id=comment_id),
            timeout=_REMOTE_PROVIDER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _audit(
            tool="remote_artifact_delete_comment",
            request=request,
            outcome="timeout",
            error=f"provider comment delete exceeded {_REMOTE_PROVIDER_TIMEOUT_S:g}s",
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err("remote provider timed out", status=504)
    except Exception as exc:  # noqa: BLE001
        _audit(
            tool="remote_artifact_delete_comment",
            request=request,
            outcome="error",
            error=_redact_text(str(exc)),
            extra={"provider": provider_name, "external_id": external_id},
        )
        return _err(_redact_text(str(exc)), status=502)

    _invalidate_remote_comment_cache(provider_name, external_id)
    _audit(
        tool="remote_artifact_delete_comment",
        request=request,
        outcome="success",
        extra={"provider": provider_name, "external_id": external_id},
    )
    return _json_response({"deleted": True})
