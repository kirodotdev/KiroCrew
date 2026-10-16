"""Create and update: ``POST /api/artifacts`` and ``PATCH /api/artifacts/{slug}``.

Both run the owner gate, validate inbound webapp metadata, consult the dashboard-package
gate, write through the store off the event loop, audit and broadcast the change.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND
from kiro_crew.artifacts import (
    USER_SELECTABLE_KINDS,
    ArtifactAlreadyExistsError,
    ArtifactConflictError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_store,
    has_unthemed_hardcoded_colors,
    webapp_metadata_from_dict,
)
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.dashboard_package_gate import (
    _SNAPSHOT_ABSENT,
    _SNAPSHOT_UNREADABLE,
    _authoritative_promote_content,
    _binding_refusal,
    _gate_must_classify,
    _is_dashboard_package,
    _package_binding,
    _package_write_refusal,
    _promote_verdict,
    _stored_snapshot_for,
)
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import _serialize
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _artifact_source_for_request,
    _audit,
    _clean_origin_session_key,
    _err,
    _json_response,
    _non_owner_refusal,
    _notify_artifact_update,
    _publish_governance_denied,
    _read_json_body,
    _resolve_folder_ref_off_loop,
    _run_off_loop,
    _set_folder_and_reload,
    logger,
)
from kiro_crew.dashboard.handlers.artifacts_parts.session_documents import _collision_report


def _validate_inbound_webapp_metadata(body: dict[str, Any]) -> str | None:
    """Run the bounded webapp_metadata validation at the HTTP boundary.

    The MCP boundary already validates via ARTIFACT_SAVE/UPDATE_SCHEMA's
    custom validator; the HTTP handlers must apply the same gate so a
    dashboard/API caller cannot store what the MCP path would reject
    (e.g. a javascript: public_url). Returns an error message or None.
    """
    if body.get("webapp_metadata") is None:
        return None
    from kiro_crew.validation import ValidationError, _validate_artifact_save

    try:
        _validate_artifact_save({"webapp_metadata": body["webapp_metadata"]})
    except ValidationError as exc:
        return str(exc)
    return None


async def api_artifacts_create(request: web.Request) -> web.Response:
    owner_denied = await _non_owner_refusal(request, "artifacts.artifacts_create")
    if owner_denied is not None:
        return owner_denied
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
    # ── Auto-dedup by source_path ─────────────────────
    # When the caller passes a source_path that matches an existing artifact,
    # silently bump the existing one to a new version rather than creating a
    # parallel duplicate. This makes the "Add to artifacts" action on file
    # paths idempotent — clicking it twice on the same file just produces v2,
    # not two separate artifacts. Returns 200 OK on bump (vs 201 Created on
    # genuine new save) so the caller can distinguish if it cares.
    # The verdict runs BEFORE the dedup lookup because it is what canonicalizes
    # the path. Deduping on the raw string missed an existing artifact whenever
    # the two spellings differ -- promote a symlink (or any non-canonical path)
    # twice and the second lookup found nothing, so it minted a duplicate
    # alongside the canonical record.
    raw_source_path = body.get("source_path")
    link_source_path, link_source_root, link_copy_only, verdict_err = await _promote_verdict(
        request, raw_source_path if isinstance(raw_source_path, str) else ""
    )
    if verdict_err:
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error=verdict_err,
            extra={"source_path": str(raw_source_path)[:256]},
        )
        return _err(verdict_err)
    source_path = link_source_path
    # For a validated promotion the SERVER decides the bytes, not the client.
    promoted_content: str | None = None
    if link_source_path:
        promoted_content, perr = await _authoritative_promote_content(
            link_source_path, link_source_root
        )
        if perr:
            _audit(
                tool="artifact_save",
                request=request,
                outcome="denied",
                error=perr,
                extra={"source_path": link_source_path},
            )
            return _err(perr)
    if source_path:
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
            # Run the SAME validation as the normal save path before the dedup
            # update, so the dedup branch does not drop supplied fields.
            merr = _validate_inbound_webapp_metadata(body)
            if merr:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error=merr,
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(merr)

            # Kind conflict — if caller supplies a different kind than the
            # existing artifact, that's a dedup conflict, not a silent update.
            supplied_kind = body.get("kind")
            if supplied_kind and supplied_kind != existing.kind:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error="dedup kind conflict",
                    extra={
                        "slug": existing.slug,
                        "source_path": source_path,
                        "existing_kind": existing.kind,
                        "supplied_kind": supplied_kind,
                    },
                )
                return _err(
                    f"source_path dedup conflict: existing artifact '{existing.slug}' "
                    f"has kind='{existing.kind}' but request supplies kind='{supplied_kind}'. "
                    f"Use artifact_update to change kind explicitly.",
                    status=409,
                )

            # Same auth-based actor inference as api_artifact_update — if the
            # caller is MCP (X-Internal-Secret header), the lifecycle event
            # gets tagged 'iterated' (agent), not 'edited' (user). Without
            # this, MCP-driven re-saves would silently misattribute on the
            # activity timeline.
            is_mcp = request.headers.get("X-Internal-Secret") is not None
            actor = "agent" if is_mcp else "user"

            submitted_content = (
                promoted_content if promoted_content is not None else body.get("content")
            )
            # THE DEDUP TARGET'S OWN BYTES, read by slug. `find_by_source_path` scans
            # meta.json, so the record it returns carries no content -- and handing that
            # to the gate is the same hole as reading an empty snapshot: with no stored
            # binding to check, the gate judges only the binding this caller submitted,
            # which is a claim the caller wrote. So a member who copies another's source
            # path and re-saves it through this route could rebind their page.
            #
            # Content AND token from ONE read, for the reason the update path does it:
            # the gate judges a snapshot and the write lands later.
            dedup_content, dedup_token, dedup_state = await _run_off_loop(
                lambda: _stored_snapshot_for(existing.slug)
            )
            if dedup_state == _SNAPSHOT_UNREADABLE and await _run_off_loop(
                lambda: _gate_must_classify(existing.slug, submitted_content)
            ):
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error="stored snapshot unreadable",
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(
                    "that artifact could not be read, so this save cannot be authorized "
                    "against what it holds; nothing was written -- retry",
                    status=503,
                )
            # The same binding gate the update route asks, on the path a create
            # that lands on an existing slug takes. One of the two is not enough:
            # whichever went unasked is the spelling a worker would use.
            refused = await _run_off_loop(
                lambda: _package_write_refusal(request, submitted_content, dedup_content)
            )
            if refused:
                _audit(
                    tool="artifact_create",
                    request=request,
                    outcome="denied",
                    error="dashboard package binding",
                    extra={"slug": existing.slug},
                )
                return _err(refused, status=403)

            # Pass through ALL supported fields (not just content).
            update_kwargs: dict[str, Any] = {
                "content": submitted_content,
                "actor": actor,
                "snapshot": True,
            }
            # UNDER THE TOKEN OF THE SNAPSHOT JUST JUDGED, so a rebind landing between
            # the read above and this write is refused by the store rather than
            # committed over -- the same guard the update route carries, on the path
            # that reaches the same records through a source path.
            #
            # NO TOKEN MEANS NO WRITE for a package, rather than a write without the
            # guard. The snapshot can be gone by the time it is read -- the dedup
            # target deleted after the lookup, its slug recreated as somebody else's
            # dashboard -- and that absence is exactly the case the gate cannot judge:
            # with nothing stored it has only the binding this caller submitted, which
            # the caller wrote, and the store's default would commit it over whatever
            # now holds the slug.
            if await _run_off_loop(lambda: _is_dashboard_package(dedup_content, submitted_content)):
                if not dedup_token:
                    _audit(
                        tool="artifact_save",
                        request=request,
                        outcome="denied",
                        error="no authorized snapshot for a dashboard package write",
                        extra={"slug": existing.slug, "source_path": source_path},
                    )
                    return _err(
                        "that slug no longer holds the dashboard package this save was "
                        "authorized against, so there is nothing to write over; "
                        "nothing was written -- refetch and re-base",
                        status=409,
                    )
            # AND THE TOKEN OF THIS READ TRAVELS REGARDLESS, for the same reason as the
            # update route: the refusal above fires on the stored content classifying as
            # a package, so a dedup target that was ordinary prose would otherwise reach
            # the write with nothing to compare and a dashboard arriving in the gap
            # would be replaced. `update` decides what to do with it under its lock.
            if dedup_token:
                update_kwargs["snapshot_token"] = dedup_token
            if body.get("name"):
                update_kwargs["name"] = body["name"]
            if body.get("tags") is not None:
                update_kwargs["tags"] = body["tags"]
            if body.get("description") is not None:
                update_kwargs["description"] = body["description"]
            wm = body.get("webapp_metadata")
            if wm is not None:
                update_kwargs["webapp_metadata"] = webapp_metadata_from_dict(wm)
            try:
                # OFF the event loop, for the same reason as api_artifact_update:
                # this dedup bump writes current.html, a version snapshot and --
                # for a linked artifact -- the source file, and that write now
                # fsyncs. Inline, a slow filesystem or a large file stalls the
                # gateway (heartbeat included) for every other session.
                art = await _run_off_loop(lambda: store.update(existing.slug, **update_kwargs))
            except ArtifactValidationError as exc:
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                return _err(str(exc))
            except ArtifactConflictError as exc:
                # The record moved between the snapshot the gate judged and this
                # write, so nothing was written. Answered like the update route's
                # conflict: what to refetch, not a 500.
                _audit(
                    tool="artifact_save",
                    request=request,
                    outcome="denied",
                    error=str(exc),
                    extra={"slug": existing.slug, "source_path": source_path},
                )
                # REDACTED: the conflict names the SLUG, and a slug is
                # `[a-z0-9-]` -- which cannot be a `ghp_` but can be an `xoxb-` or a
                # `glpat-`, the same shapes the named-package read refuses for.
                from kiro_crew.platform.context import redact_via_context

                return _err(redact_via_context(str(exc)), status=409)
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
    # segments) so a save can file the artifact in one call. Off the
    # event loop — mkdir -p may persist new folders (blocking fsync).
    merr = _validate_inbound_webapp_metadata(body)
    if merr:
        _audit(tool="artifact_save", request=request, outcome="denied", error=merr)
        return _err(merr)
    # The third write path: a package created under a slug nothing holds yet. Gated
    # like the other two, so "create it somewhere new" is not the way past the rule.
    # No stored binding is passed because there is none: this path is reached only
    # after the dedupe branch above found no artifact to land on.
    #
    # Gated on the bytes that are actually STORED, which is `promoted_content` when a
    # `source_path` was promoted and the request body carried no `content` of its own
    # (the same expression the create below writes, and the same one the dedupe branch
    # gates as `submitted_content`). Asking about `body.get("content")` alone would let
    # a caller send an empty body with a `source_path` whose file declares
    # `bound_to: "crewmate:<someone else>"`: the gate would parse `""`, find no package
    # and pass, and the other crewmate's tab would then serve and run this caller's page
    # beside that crewmate's own fold values.
    #
    # OFF THE LOOP like its two siblings: the gate resolves the caller's slug through
    # the roster, which loads the config from disk.
    refused = await _run_off_loop(
        lambda: _package_write_refusal(
            request,
            promoted_content if promoted_content is not None else body.get("content"),
            None,
        )
    )
    if refused:
        _audit(
            tool="artifact_save",
            request=request,
            outcome="denied",
            error="dashboard package binding",
        )
        return _err(refused, status=403)
    folder_id, ferr = await _resolve_folder_ref_off_loop(body.get("folder"), create_missing=True)
    if ferr:
        _audit(tool="artifact_save", request=request, outcome="denied", error=ferr)
        return _err(ferr)
    # A new package always declares a binding for the first time, so there is no
    # previous one to compare against and every create is checked. Gated on the
    # DECLARED kind, which is also what makes the store validate the content as a
    # package at all: an inferred kind never comes back ``dashboard``, so a plain JSON
    # document that happens to carry a ``bound_to`` field is left alone.
    if body.get("kind") == DASHBOARD_KIND:
        new_binding = _package_binding(
            promoted_content if promoted_content is not None else body.get("content", "")
        )
        if new_binding is not None:
            berr = await _binding_refusal(state, new_binding)
            if berr:
                _audit(tool="artifact_save", request=request, outcome="denied", error=berr)
                return _err(berr)
    # Copy-vs-link: a disposable file (temp dir / Downloads / Desktop, or
    # anything with no project claim) is SNAPSHOTTED — we store no pointer at
    # all. A file inside a real project is LINKED, and the project root that
    # authorizes later reads is recorded with it. source_path is validated here
    # rather than stored as a raw string: an unvalidated project file outside
    # $HOME yields a pointer the store then refuses to read.
    try:
        # Off the loop: a create writes the record, its first version and the
        # index, and each of those syncs its bytes to the device before it
        # renames. On slow or busy storage that is long enough to stall chat
        # turns and heartbeats if it runs here.
        art = await _run_off_loop(
            lambda: get_default_store().create(
                name=body.get("name", ""),
                content=(
                    promoted_content if promoted_content is not None else body.get("content", "")
                ),
                slug=body.get("slug"),
                kind=body.get("kind"),
                # Honor an explicitly-supplied source (MCP tool / import path); for
                # UI saves that omit it, derive the ACTUAL session origin
                # (dashboard/slack/cli/cron/subagent/...) rather than "manual".
                source=(body.get("source") or _artifact_source_for_request(request)),
                description=body.get("description", ""),
                tags=body.get("tags") or [],
                source_path=link_source_path,
                source_root=link_source_root,
                source_copy_only=link_copy_only,
                folder_id=folder_id,
                # Originating chat session for the Source column. Only a real slot
                # key that passes the permitted grammar is stored (validated to
                # prevent attribution spoofing / metadata poisoning); anything else
                # collapses to "".
                session_key=_clean_origin_session_key(body.get("origin_session_key")),
                webapp_metadata=webapp_metadata_from_dict(body.get("webapp_metadata")),
            )
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
    # New library entries appear live in every open window.
    _notify_artifact_update(state, art.slug, art.version)
    # Report a de-duplicated slug to the CLI and the MCP tool, both of which are
    # HTTP clients and so cannot see the store's log line. Create-only: the value
    # describes this call, and a GET or LIST would carry it empty forever.
    payload = _serialize(art, include_content=True)
    payload["slug_collided_with"] = _collision_report(art)
    # Theme-contrast verdict for the clients, same relay pattern as
    # ``slug_collided_with``: computed once here at the convergence point so
    # EVERY authoring surface (MCP tool, CLI, dashboard) sees the same
    # verdict -- the motivating incident traveled the CLI, which never runs
    # the MCP-layer hint. Scans ``art.content`` (what was actually persisted),
    # not the request body: a file-promoted save persists the server-read
    # bytes, and the client's copy can be stale or redacted. Advisory only;
    # the save has already succeeded.
    payload["theme_contrast_warning"] = has_unthemed_hardcoded_colors(art.kind, art.content or "")
    return _json_response(payload, status=201)


async def api_artifact_update(request: web.Request) -> web.Response:
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_update")
    if owner_denied is not None:
        return owner_denied
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
        # Snapshot semantics: saves don't bump version
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
        merr = _validate_inbound_webapp_metadata(body)
        if merr:
            _audit(tool="artifact_update", request=request, outcome="denied", error=merr)
            return _err(merr)
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
        # Explicit render-kind change (the type control on the artifact page).
        # Restricted to the inline-editable kinds: `widget` / `html` render in a
        # sandboxed iframe and are NOT editable, so letting a caller select one
        # would strand the editor on a document the user is typing in — and
        # `webapp` carries deploy metadata that a hand-flip would desynchronise.
        # Setting a kind also PINS it: the store clears its auto-detect flag, so
        # a document the user has typed a kind onto is never re-typed under them.
        raw_kind = body.get("kind")
        if raw_kind is not None:
            # isinstance FIRST: a JSON body may carry any type here, and an
            # unhashable one (list / dict) raises TypeError on the set lookup --
            # a 500 where the caller deserves a 400.
            if not isinstance(raw_kind, str) or raw_kind not in USER_SELECTABLE_KINDS:
                msg = f"kind must be one of {sorted(USER_SELECTABLE_KINDS)}; " f"got {raw_kind!r}"
                _audit(
                    tool="artifact_update",
                    request=request,
                    outcome="denied",
                    error=msg,
                    extra={"slug": slug, "kind": str(raw_kind)[:32]},
                )
                return _err(msg)
        # WHAT THE SLUG HOLDS, read before either gate decides anything.
        # Its two transport refusals come first because the target gate below opens
        # with a read of its own, and on an unreadable or absent record that read is
        # what answers -- a 500 or a 404 in place of the refusal that says nothing
        # was authorized. The OWNERSHIP comparison this read feeds waits until after
        # that gate, so the narrower question keeps its own answer.
        #
        # A DASHBOARD PACKAGE IS ITS OWNER'S. Asked before the write, because a
        # worker that could write the package could write the page -- and the page
        # runs script inside the frame that holds the owner's own fold values.
        # Asked HERE rather than in the store because the rule needs the caller,
        # and the store is handed content with no request behind it.
        # WHAT THE SLUG HOLDS NOW is read first, because the stored binding is who the
        # page belongs to: gating on the submitted one alone authorizes the caller
        # against a claim the caller wrote, so a worker could rebind another
        # crewmate's page to itself in a single update.
        stored_content, stored_token, stored_state = await _run_off_loop(
            lambda: _stored_snapshot_for(slug)
        )
        if stored_state == _SNAPSHOT_UNREADABLE and await _run_off_loop(
            lambda: _gate_must_classify(slug, body.get("content"))
        ):
            # NOTHING IS AUTHORIZED AGAINST A READ THAT FAILED. Passing unknown content
            # to the gate checks only the binding this caller submitted -- so a
            # transient read error would be the one moment a worker's write lands on an
            # owner's page. Refused only where the gate is the one that decides this
            # write: every other kind keeps the behaviour it has without this gate,
            # because an unreadable prose document is the store's own problem to report
            # and turning it into a refusal here would be this feature charging every
            # other kind for its rule.
            _audit(
                tool="artifact_update",
                request=request,
                outcome="denied",
                error="stored snapshot unreadable",
                extra={"slug": slug},
            )
            return _err(
                "that artifact could not be read, so this write cannot be authorized "
                "against what it holds; nothing was written -- retry",
                status=503,
            )
        if stored_state == _SNAPSHOT_ABSENT and await _run_off_loop(
            lambda: _is_dashboard_package(body.get("content"))
        ):
            # A PACKAGE WRITE NEEDS A RECORD TO AUTHORIZE AGAINST. The slug holds
            # nothing, so the gate below would have only the binding this caller
            # submitted -- a claim the caller wrote -- and no token to carry forward.
            # The store answers an absent slug with its own 404, so the one case this
            # refusal changes is the race it exists for: the owner creating this slug
            # between the read and the write, where the authorization would be of
            # nothing and the write would land on their new page.
            _audit(
                tool="artifact_update",
                request=request,
                outcome="denied",
                error="dashboard package write to an empty slug",
                extra={"slug": slug},
            )
            return _err(
                "that slug holds no dashboard package, so there is no binding to "
                "authorize this write against; create the package instead",
                status=409,
            )
        # THE REBIND TARGET FIRST, because this question is the narrower one and it
        # has a definite answer: whether `bound_to` names exactly one servable
        # thing -- a crew member who exists, a slug two of them do not share, a live
        # root session. The owner gate below asks a BROADER question, whose caller
        # it cannot always resolve, and answering that one first replaced every one
        # of these refusals with a single 403 -- a shallow gate standing in front of
        # a specific one and hiding what it would have said.
        # A REBIND is checked; a layout edit is not. The stored record answers both
        # halves -- whether this is a package at all, and what it is bound to now -- so
        # it is read only once a submitted ``bound_to`` makes the question live, which
        # keeps every ordinary PATCH at the single write it already was. See the
        # binding section in dashboard_package_gate for why an unchanged binding is
        # never re-asked.
        #
        # A REVERT is not a rebind, however different its binding looks. It submits a
        # past version's whole package, and ``revert_package`` THROWS THAT BINDING AWAY
        # and keeps the live one -- precisely so a rollback past a rebind cannot hand
        # this page to whoever the stale binding names. Checking the submitted value
        # would refuse the rollback for naming a target the store was never going to
        # store, and the binding that does land was already checked when it was written.
        submitted_binding = _package_binding(body.get("content"))
        if submitted_binding is not None and event_type != "reverted":
            stored = await _run_off_loop(lambda: get_default_store().get(slug))
            # ``raw_kind`` is the kind this write SETTLES on, and a write converting the
            # record away from ``dashboard`` stores no package at all -- the store stops
            # reading the content as one, so there is no binding to be wrong.
            stays_a_package = stored.kind == DASHBOARD_KIND and raw_kind in (
                None,
                DASHBOARD_KIND,
            )
            if stays_a_package and submitted_binding != _package_binding(stored.content):
                berr = await _binding_refusal(state, submitted_binding)
                if berr:
                    _audit(
                        tool="artifact_update",
                        request=request,
                        outcome="denied",
                        error=berr,
                        extra={"slug": slug},
                    )
                    return _err(berr)
        # AND WHOSE PAGE THIS IS, after the target has been shown servable. Both
        # gates run: that one judges the binding this write SUBMITS, this one the
        # caller against the binding the record already HOLDS -- so a worker
        # rebinding another crewmate's page to a target that happens to be valid
        # is still refused here.
        refused = await _run_off_loop(
            lambda: _package_write_refusal(request, body.get("content"), stored_content)
        )
        if refused:
            _audit(
                tool="artifact_update",
                request=request,
                outcome="denied",
                error="dashboard package binding",
                extra={"slug": slug},
            )
            return _err(refused, status=403)
        # THE TOKEN OF THE READ THIS WRITE WAS AUTHORIZED AGAINST, carried for EVERY
        # write rather than only the ones classified as a package.
        #
        # The read above and the write below are two moments. Deciding HERE which
        # writes need the token is the mistake that kept this hole open: the token was
        # attached only where the stored snapshot or the submitted content classified
        # as a package, so a PATCH of `{"kind": "markdown", "content": ...}` against a
        # slug holding ordinary prose -- or holding nothing -- reached the store with
        # nothing to compare at all, and the owner's dashboard arriving in the gap was
        # rewritten, kind and bytes both.
        #
        # So this handler classifies nothing. It reports what it read and `update`
        # decides, under the lock that loads the record, whether a dashboard is what
        # the write is landing on. `expected_token` stays the CALLER's own
        # compare-and-set for every other kind, untouched, because the editor's
        # autosave depends on a prose write not being fenced it never asked for.
        expected_token = body.get("expected_token")
        # OFF the event loop: update() writes current.html, a version snapshot
        # and -- for a linked artifact -- the source file itself, so running it
        # inline stalls the gateway for every other session on a large file or a
        # slow filesystem. (The synchronous write predates the descriptor-pinned
        # helper; offloading fixes the stall without giving up the O_NOFOLLOW
        # guarantees that helper provides.)
        art = await _run_off_loop(
            lambda: get_default_store().update(
                slug,
                kind=raw_kind,
                content=body.get("content"),
                description=body.get("description"),
                tags=body.get("tags"),
                name=body.get("name"),
                webapp_metadata=webapp_metadata_from_dict(body.get("webapp_metadata")),
                actor=actor,
                session_id=session_id_hdr,
                event_type=event_type,
                from_version=from_version,
                snapshot=snapshot,
                expected_token=expected_token,
                snapshot_token=stored_token,
            )
        )
        # store.update() only loads content into the returned Artifact when
        # the caller passed new content (because that path is on the write
        # branch of the store). For metadata-only updates the returned
        # Artifact has content=None, which then serializes as "content": null
        # in the response — inconsistent with api_artifact_detail which
        # always returns the actual content. Refetch in that case so the MCP
        # tool / dashboard caller always sees a populated content field.
        if art.content is None:
            art = await _run_off_loop(lambda: get_default_store().get(slug))
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
    except ArtifactConflictError as exc:
        # A stale content write: nothing was written. The body carries what the
        # client needs to refetch and re-base.
        _audit(
            tool="artifact_update",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return web.json_response(
            {
                "error": str(exc),
                "code": "artifact_conflict",
                "current_token": exc.current_token,
            },
            status=409,
        )
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
    # Optional folder placement. Metadata-only — does not bump the
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
    # Live refresh: broadcast only when the artifact's content
    # actually changed — a content-carrying PATCH (Save / Snapshot / MCP
    # artifact_update) or a revert (event_type="reverted" is a content
    # rollback even when the body carries no content field). Metadata-only
    # updates (rename / retag / description / folder) don't move content, so
    # open views have nothing to re-render.
    content_changed = body.get("content") is not None or event_type == "reverted"
    if content_changed:
        _notify_artifact_update(state, art.slug, art.version)
    # Auto-sync egress: a snapshot that bumped the version on an artifact
    # published with ``auto_sync`` (the state a bidirectional ``clone`` arms)
    # pushes the new version to the remote — this is the leg that makes clone
    # actually bidirectional. Gated through the SAME ``capabilities.publish``
    # ceiling the clone passed, so a governance-denied surface can edit locally
    # but never egresses. Best-effort: a push failure must not fail the local
    # save (the version is already durable); the next snapshot retries.
    # Inert in the public edition (empty registry -> push_version_by_slug's
    # provider resolve raises PublishUnavailableError, swallowed here).
    if content_changed and snapshot and art.publication is not None and art.publication.auto_sync:
        if _publish_governance_denied(request, art.publication.provider) is None:
            try:
                await publish_sync.push_version_by_slug(art.slug)
            except Exception as exc:  # noqa: BLE001 - best-effort egress
                logger.info("auto-sync push after snapshot failed for %s: %s", art.slug, exc)
    payload = _serialize(art, include_content=True)
    # Same relay as the save path's ``theme_contrast_warning``: a
    # content-carrying update gets the verdict computed here at the
    # convergence point, so the CLI's PATCH (the motivating incident's
    # path) hears it too. Unlike save, PATCH never file-promotes, so the
    # body IS the persisted content -- and scanning the body (not
    # ``art.content``) keeps metadata-only updates (rename/retag) from
    # warning about pre-existing content they didn't touch.
    payload["theme_contrast_warning"] = has_unthemed_hardcoded_colors(
        art.kind, body.get("content") or ""
    )
    return _json_response(payload)
