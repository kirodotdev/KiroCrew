"""Comments on a local artifact: list, post, reply, mark-review, resolve, reopen,
delete and edit. When the artifact is published, listing reads the publish provider's
thread, and every write but resolve and reopen pushes to it.
"""

from __future__ import annotations

import asyncio
import getpass
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew.artifacts import (
    ArtifactComment,
    ArtifactNotFoundError,
    ArtifactValidationError,
    filter_comments_for_forward,
    get_default_store,
)
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import _redact_text
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _REMOTE_PROVIDER_TIMEOUT_S,
    _audit,
    _err,
    _event_session_id,
    _json_response,
    _non_owner_refusal,
    _publish_governance_denied,
    _read_json_body,
    _run_off_loop,
    logger,
)
from kiro_crew.publish_provider import DEFAULT_PROVIDER, Capability, CommentAnchor, get_provider


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
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    # Surfaced to the UI so a provider-side failure (e.g. the remote comment op
    # not being reachable) is visible rather than silently dropped.
    remote_sync_error: str | None = None

    # Fetch-on-view: if this artifact is published to a provider that supports
    # COMMENTS_READ, pull the remote comments and reconcile them into the local
    # mirror before returning. The provider fetch is network IO and the merge is
    # blocking filesystem IO, so both run off the event loop. Best-effort: any
    # failure is surfaced as remote_sync_error but never fails the list. When no
    # provider is registered (the public default), get_provider raises and this
    # degrades to local-only comments — identical to the prior behavior.
    if art.publication and art.publication.artifact_id:
        try:
            provider = get_provider(art.publication.provider)
            if Capability.COMMENTS_READ in provider.capabilities():
                remote = await asyncio.wait_for(
                    provider.fetch_comments(external_id=art.publication.artifact_id),
                    timeout=_REMOTE_PROVIDER_TIMEOUT_S,
                )
                if remote:
                    mapped = [
                        ArtifactComment(
                            id=rc.remote_id,
                            origin=f"{art.publication.provider}:{rc.remote_id}",
                            provider=art.publication.provider,
                            scope="shared",
                            author=rc.author,
                            is_agent=rc.is_agent,
                            body=rc.body,
                            anchor_quote=rc.anchor.quote if rc.anchor else None,
                            anchor_prefix=rc.anchor.prefix if rc.anchor else None,
                            anchor_suffix=rc.anchor.suffix if rc.anchor else None,
                            anchor_start_offset=rc.anchor.start_offset if rc.anchor else None,
                            anchor_end_offset=rc.anchor.end_offset if rc.anchor else None,
                            anchor_version=rc.anchor.version_number if rc.anchor else None,
                            thread_id=rc.thread_id,
                            parent_id=rc.parent_id,
                            status=rc.status,
                            # Routing metadata so a later local edit/review/delete
                            # of this merged mirror reaches the provider — the
                            # write handlers gate on target_external_id before
                            # calling the provider, so without it those mutations
                            # would silently stay local and be resurrected/
                            # overwritten by the next fetch-on-view.
                            target_provider=art.publication.provider,
                            target_external_id=art.publication.artifact_id,
                            sync_state="synced",
                            created_at=rc.created_at,
                            updated_at=rc.updated_at,
                            deleted=rc.deleted,
                        )
                        for rc in remote
                    ]
                    await _run_off_loop(
                        lambda: store.merge_remote_comments(slug, art.publication.provider, mapped)
                    )
        except asyncio.TimeoutError:
            # str(TimeoutError()) is empty, so the generic branch below would
            # surface an EMPTY remote_sync_error. Set a non-empty, human-readable
            # reason (CWE-400 bounded await) while still degrading to a 200 render.
            logger.warning(
                "fetch-on-view comments timed out for %s after %gs",
                slug,
                _REMOTE_PROVIDER_TIMEOUT_S,
            )
            remote_sync_error = f"remote provider timed out after {_REMOTE_PROVIDER_TIMEOUT_S:g}s"
        except Exception as exc:  # noqa: BLE001 — fetch-on-view is best-effort
            logger.warning("fetch-on-view comments failed for %s: %s", slug, exc)
            remote_sync_error = _redact_text(str(exc))

    comments = await _run_off_loop(lambda: store.list_comments(slug))

    # ── Forwarding filter (comment→chat replay fix) ──
    # Opt-in: `?exclude_resolved=1` drops resolved threads, which is what the
    # forwarding paths want. Without the param the full list comes back, so
    # existing callers are unaffected.
    if request.query.get("exclude_resolved", "").lower() in ("1", "true"):
        comments = filter_comments_for_forward(comments)

    result = []
    for c in comments:
        entry: dict[str, Any] = {
            "id": c.id,
            "origin": c.origin,
            "provider": c.provider,
            "scope": c.scope,
            # Provider-controlled: a remote comment merged into the mirror carries
            # the provider's author string verbatim, so redact credentials/exfil
            # URLs at this read boundary like the body/anchor (comments are stored
            # raw; redaction happens on the way out — backend-security-controls).
            "author": _redact_text(c.author),
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
            # Anchor text can carry provider/agent-influenced content and is
            # echoed to the dashboard, so redact credentials/exfil-URLs like the
            # body (backend-security-controls). Remote comments merged from a
            # provider are stored raw, so redaction must happen at this read
            # boundary too — not only on the local POST path.
            entry["anchor"] = {
                "quote": _redact_text(c.anchor_quote),
                "prefix": _redact_text(c.anchor_prefix) if c.anchor_prefix else c.anchor_prefix,
                "suffix": _redact_text(c.anchor_suffix) if c.anchor_suffix else c.anchor_suffix,
                "start_offset": c.anchor_start_offset,
                "end_offset": c.anchor_end_offset,
                "version_number": c.anchor_version,
            }
        result.append(entry)
    return _json_response({"comments": result, "remote_sync_error": remote_sync_error})


async def api_artifact_post_comment(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/comments — create a new comment."""
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_post_comment")
    if owner_denied is not None:
        return owner_denied
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
        # Redaction runs over the FULL value (capping first can cut a credential
        # into fragments no regex matches), and the values are request-sized, so
        # the pass runs off-loop (no-blocking-call-on-event-loop).
        def _anchor_str(v: object) -> str | None:
            if not isinstance(v, str) or not v:
                return None
            return _redact_text(v)[:2000]

        anchor_quote = await asyncio.to_thread(_anchor_str, anchor_data.get("quote"))
        anchor_prefix = await asyncio.to_thread(_anchor_str, anchor_data.get("prefix"))
        anchor_suffix = await asyncio.to_thread(_anchor_str, anchor_data.get("suffix"))
        anchor_start = anchor_data.get("start_offset")
        anchor_end = anchor_data.get("end_offset")
        anchor_ver = anchor_data.get("version_number")

    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    comment_id = str(uuid.uuid4())

    # Determine if this is agent-authored
    is_agent = bool(body.get("is_agent"))

    # Author defaults to the dashboard user's alias (collaboration: comments
    # show who left them). Agent comments keep their explicit
    # author (or the agent badge). getpass.getuser() is the alias on dev desks.
    # The author is LLM/agent-influenced on the MCP path and echoed to the
    # dashboard, so redact + cap it like the body (backend-security-controls).
    author = (await asyncio.to_thread(_redact_text, str(body.get("author") or "")))[:256]
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
        _publish_governance_denied(request, comment.target_provider or DEFAULT_PROVIDER)
        if scope == "shared" and comment.target_external_id
        else "not shared"
    )
    if scope == "shared" and comment.target_external_id and gov_denied is None:
        try:

            provider = get_provider(comment.target_provider or DEFAULT_PROVIDER)
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
                rc = await asyncio.wait_for(
                    provider.post_comment(
                        external_id=comment.target_external_id,
                        body=text,
                        anchor=anchor_obj,
                    ),
                    timeout=_REMOTE_PROVIDER_TIMEOUT_S,
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
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_reply_comment")
    if owner_denied is not None:
        return owner_denied
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
    author = (await asyncio.to_thread(_redact_text, str(body.get("author") or "")))[:256]
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
        and _publish_governance_denied(request, reply.target_provider or DEFAULT_PROVIDER) is None
    ):
        try:

            provider = get_provider(reply.target_provider or DEFAULT_PROVIDER)
            if Capability.COMMENTS_WRITE in provider.capabilities():
                # Extract remote parent id from origin
                remote_parent_id = (
                    parent.origin.split(":", 1)[-1] if ":" in parent.origin else parent.id
                )
                rc = await asyncio.wait_for(
                    provider.reply_comment(
                        external_id=reply.target_external_id,
                        parent_remote_id=remote_parent_id,
                        body=text,
                    ),
                    timeout=_REMOTE_PROVIDER_TIMEOUT_S,
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
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_mark_review")
    if owner_denied is not None:
        return owner_denied
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
        and _publish_governance_denied(request, target.target_provider or DEFAULT_PROVIDER) is None
    ):
        try:

            provider = get_provider(target.target_provider or DEFAULT_PROVIDER)
            if Capability.COMMENTS_WRITE in provider.capabilities():
                remote_id = target.origin.split(":", 1)[-1]
                await asyncio.wait_for(
                    provider.mark_review(
                        external_id=target.target_external_id, remote_id=remote_id
                    ),
                    timeout=_REMOTE_PROVIDER_TIMEOUT_S,
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
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_resolve_comment")
    if owner_denied is not None:
        return owner_denied
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
    # the ``is_agent`` body flag is a defense-in-depth fallback rather than the
    # only gate (a body field can be spoofed).
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
    thread (set status back to open).
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_reopen_comment")
    if owner_denied is not None:
        return owner_denied
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
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_delete_comment")
    if owner_denied is not None:
        return owner_denied
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
    reason = (await asyncio.to_thread(_redact_text, str(body.get("reason") or "").strip()))[:500]

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
        and _publish_governance_denied(request, target.target_provider or DEFAULT_PROVIDER) is None
    ):
        try:

            provider = get_provider(target.target_provider or DEFAULT_PROVIDER)
            if Capability.COMMENTS_WRITE in provider.capabilities():
                remote_id = target.origin.split(":", 1)[-1]
                await asyncio.wait_for(
                    provider.delete_comment(
                        external_id=target.target_external_id, remote_id=remote_id
                    ),
                    timeout=_REMOTE_PROVIDER_TIMEOUT_S,
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
    in-place edit (``Capability.COMMENTS_EDIT`` — a live CRDT provider), the new body is also
    pushed to the provider, preserving the remote id / thread / replies.
    Providers without that capability (mirror providers) edit
    locally only; the response's ``remote_synced`` flag is False so the UI can
    surface that the change stayed local rather than silently diverging.

    Status (open/review/resolved) is untouched — that's what resolve/reopen/
    review are for. Authorship (``author`` / ``is_agent``) is preserved.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_edit_comment")
    if owner_denied is not None:
        return owner_denied
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
    # post/reply (security-controls).
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

    # Agent provenance is the structured is_agent flag, not a body prefix — an
    # edit stores the body verbatim (no emoji stamped into the text; the
    # dashboard renders a lucide Bot icon from is_agent per AGENTS.md).

    # Push the edit to the provider in place when its origin provider supports
    # it (a live CRDT provider). Others edit locally only. Gated by the same capabilities.publish
    # chokepoint as artifact publish — the edited body is outbound egress, so a
    # denied policy keeps the edit LOCAL (remote_synced stays False).
    remote_synced = False
    if (
        target.origin
        and target.origin != "local"
        and target.target_external_id
        and _publish_governance_denied(request, target.target_provider or DEFAULT_PROVIDER) is None
    ):
        try:
            provider = get_provider(target.target_provider or DEFAULT_PROVIDER)
            if Capability.COMMENTS_EDIT in provider.capabilities():
                remote_id = target.origin.split(":", 1)[-1]
                await asyncio.wait_for(
                    provider.edit_comment(
                        external_id=target.target_external_id,
                        remote_id=remote_id,
                        body=text,
                    ),
                    timeout=_REMOTE_PROVIDER_TIMEOUT_S,
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
