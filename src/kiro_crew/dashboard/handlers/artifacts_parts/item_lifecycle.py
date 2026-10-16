"""The other state changes of one artifact: settling a blank, delete, recording a
lifecycle event and relocating its ``source_path``.
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from pathlib import Path

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew.artifacts import (
    EXPECT_ABSENT,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactReplacedError,
    ArtifactStillPublishedError,
    ArtifactValidationError,
    get_default_store,
    slug_is_well_formed,
)
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import _serialize
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _audit,
    _err,
    _json_response,
    _non_owner_refusal,
    _notify_artifact_update,
    _read_json_body,
    _run_off_loop,
    logger,
)
from kiro_crew.security import is_sensitive_path


async def api_artifact_settle_blank(request: web.Request) -> web.Response:
    """Atomically resolve a just-created blank document the user is leaving.

    The library's "New artifact" action creates a document empty and opens its
    editor. On leaving, exactly one of three things is right: keep it (something
    was invested), save the draft still in the editor, or delete the abandoned
    shell. The decision has to be atomic -- deciding in the browser means reading
    the artifact and then acting, and a save landing in that gap from a popout
    window or an agent would be overwritten or deleted. So the browser states its
    intent and the store decides under its own lock.

    Body: ``{"untitled_name": str, "draft": str}``. ``untitled_name`` is the
    localised placeholder the creating client used, so the store can recognise an
    unnamed document without owning a copy of the UI's copy.

    Responds ``{"outcome": "kept" | "saved" | "deleted"}``.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_settle_blank")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_settle_blank",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot settle artifacts", status=403)
    slug = request.match_info.get("slug", "")

    # Every rejection is audited, not just the ones the store raises: this
    # endpoint can DELETE a document, so a refusal is exactly as interesting to an
    # auditor as a success, and a silent 400 would leave the attempt with no trace.
    def _denied(error: str) -> web.Response:
        _audit(
            tool="artifact_settle_blank",
            request=request,
            outcome="denied",
            error=error,
            extra={"slug": slug},
        )
        return _err(error)

    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _denied(str(exc))
    untitled_name = body.get("untitled_name")
    if not isinstance(untitled_name, str) or not untitled_name.strip():
        return _denied("untitled_name is required")
    raw_draft = body.get("draft", "")
    if not isinstance(raw_draft, str):
        return _denied("draft must be a string")
    # A client that has issued its own writes sends allow_delete=false: those
    # writes may not have been applied yet, so deletion cannot be made safe. It
    # does not block the draft rescue, which only needs the stored content to be
    # empty. Defaults to true so an omitted field is the ordinary leave-time call.
    allow_delete = body.get("allow_delete", True)
    if not isinstance(allow_delete, bool):
        return _denied("allow_delete must be a boolean")
    try:
        outcome = await _run_off_loop(
            lambda: get_default_store().settle_blank(
                slug,
                untitled_name=untitled_name,
                draft=raw_draft,
                allow_delete=allow_delete,
            )
        )
    except ArtifactNotFoundError as exc:
        # Already gone (double navigation, or deleted in another window). Nothing
        # to settle and nothing to report as an error.
        _audit(
            tool="artifact_settle_blank",
            request=request,
            outcome="success",
            extra={"slug": slug, "result": "absent"},
        )
        return _json_response({"outcome": "kept", "detail": str(exc)})
    except ArtifactValidationError as exc:
        return _denied(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_settle_blank",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_settle_blank",
        request=request,
        outcome="success",
        extra={"slug": slug, "result": outcome},
    )
    if outcome != "kept":
        _notify_artifact_update(state, slug, 1, deleted=outcome == "deleted")
    return _json_response({"outcome": outcome})


async def api_artifact_delete(request: web.Request) -> web.Response:
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_delete")
    if owner_denied is not None:
        return owner_denied
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
    # last-known version.
    try:
        _existing = await _run_off_loop(lambda: get_default_store().get(slug))
    except ArtifactError:
        # Best-effort version capture only — swallow both the missing-slug and
        # invalid-slug (ArtifactValidationError) siblings so an invalid slug still
        # reaches the delete() call below, which returns a clean 4xx (a bare
        # ArtifactNotFoundError catch here would leak ArtifactValidationError as a 500).
        _existing = None
    # Withdraw the destination copy BEFORE the local delete, so the publication -- the
    # only handle that can withdraw it -- still exists while the attempt is made. The
    # reverse order was tried and reverted twice; this ordering is the one with the
    # smaller crash residue. Die between the two steps here and the copy is withdrawn but
    # the artifact remains, which the user simply deletes again. Die between them in the
    # other order and the record is already gone while the content is still public, with
    # nothing left to withdraw it by.
    #
    # The outcome decides whether the local delete may proceed, on ONE rule: the delete
    # goes ahead only when there is nothing left to withdraw (never published, or the
    # destination is CONFIRMED gone) or the destination confirmed the withdrawal. If the
    # published copy was not withdrawn -- rejected (FAILED) or unreachable so the call was
    # never made (UNREACHABLE) -- the delete is refused and says so.
    #
    # This deliberately refuses deletes that would otherwise succeed. Erasing the record
    # erases the only handle that could ever withdraw a world-readable copy, and no later
    # action recovers it, while a refused delete is recoverable by retrying once the
    # destination answers again. Note what "recoverable" does NOT include: there is no
    # action that accepts the exposure and clears the record, because `unpublish` keeps it
    # too unless a removal or a confirmed absence says otherwise. Failing loudly is still
    # the cheaper error, but the owner is genuinely stuck until the destination answers.
    if _existing is not None and _existing.publication is not None:
        # Captured here, where the record is known present: the clear below re-checks it
        # so that a re-publish of this same artifact -- which replaces the record and
        # leaves `created_at` untouched -- cannot pass for the copy this request withdrew.
        _withdrawn_publication_id = _existing.publication.artifact_id
        withdrawal = await publish_sync.delete_for_artifact(_existing)
        if withdrawal in (
            publish_sync.DeleteWithdrawal.FAILED,
            publish_sync.DeleteWithdrawal.UNREACHABLE,
        ):
            if withdrawal is publish_sync.DeleteWithdrawal.FAILED:
                msg = (
                    "The destination did not confirm removal of the published copy, so "
                    "this artifact was not deleted. Try again once the destination stops "
                    "erroring."
                )
            else:
                msg = (
                    "The published copy could not be withdrawn because its destination "
                    "could not be reached, so this artifact was not deleted -- deleting "
                    "it would leave a public copy with nothing able to take it down. "
                    "Restore access to that destination and try again. Unpublishing "
                    "first will not release it either: that refuses for the same reason, "
                    "because it also keeps the record unless the copy is confirmed gone."
                )
            _audit(
                tool="artifact_delete",
                request=request,
                outcome="error",
                error=msg,
                extra={"slug": slug},
            )
            return _err(msg, status=502)
        # The withdrawal above is a network round trip, so the slug can be deleted AND
        # RECREATED while it runs. `delete()` below removes by slug, so a delayed request
        # would delete the REPLACEMENT -- an artifact the user never asked to delete and
        # that nothing can restore. The ordering note above reasons that "a save landing
        # in that window is included in the delete the user asked for", which holds for a
        # save to the SAME artifact; a recreate is a DIFFERENT artifact wearing the same
        # slug, which that argument does not cover. `created_at` is minted fresh on create
        # at microsecond precision, so it identifies the generation rather than the name.
        # A slug that is simply GONE is not an error here: `delete()` reports that itself.
        # Off the loop: `store.get` returns the artifact WITH content, so on a large
        # artifact it is a multi-megabyte synchronous read, and this handler is async.
        # `_run_off_loop` is the routes' shared helper for exactly that and propagates the
        # exception unchanged, so the guard below still sees `ArtifactError`.
        try:
            _after = await _run_off_loop(lambda: get_default_store().get(slug))
        except ArtifactError:
            _after = None
        if _after is not None and _after.created_at != _existing.created_at:
            msg = (
                "This artifact was deleted and a new one created under the same name "
                "while its published copy was being withdrawn. The published copy was "
                "withdrawn; the new artifact was left alone. Delete it again if you "
                "meant to remove the replacement too."
            )
            _audit(
                tool="artifact_delete",
                request=request,
                outcome="denied",
                error=msg,
                extra={"slug": slug},
            )
            return _err(msg, status=409)
        # The copy is withdrawn, so DROP THE RECORD HERE rather than letting the
        # directory removal below carry it away implicitly. That is not tidiness: it is
        # what lets the removal be guarded. `delete(refuse_if_published=True)` re-reads
        # the record inside the same lock that removes the artifact, so once this clear
        # has run, a record found there again can only be a publication that landed
        # AFTER the withdrawal -- a live public copy whose handle the removal would
        # erase. Clearing first is therefore what converts "the record happens to still
        # be here" into a usable signal. Without it the record is still present on the
        # normal successful path and the guard could never fire.
        #
        # A vanished artifact is not an error: the removal below reports that itself,
        # and the copy is already withdrawn either way.
        #
        # Pinned to the generation AND the publication this request resolved. The 409
        # check above is taken before this clear and before the guard, so on its own it
        # leaves the order where the replacement arrives after it: this clear would then
        # discard the NEWCOMER's record, which is the only handle able to withdraw the copy
        # IT is serving, while the response says the new artifact was left alone. The
        # generation alone does not settle it either -- re-publishing the same artifact
        # replaces the record and leaves `created_at` untouched -- so the record's own id
        # is what makes the check decide. Either way the record is left alone, and the
        # guarded removal below refuses on the same stamp, so the request answers 409.
        try:
            await _run_off_loop(
                lambda: get_default_store().clear_publication(
                    slug,
                    expect_created_at=_existing.created_at,
                    expect_publication_id=_withdrawn_publication_id,
                )
            )
        except ArtifactNotFoundError:
            pass
        except ArtifactReplacedError:
            logger.warning(
                "withdrew the published copy of %s but left the record alone: the "
                "artifact under that slug was replaced, so the record there belongs "
                "to the newcomer",
                slug,
            )
    try:
        # `refuse_if_published=True` is safe for an artifact that was never published
        # (its record is None and nothing above touched it) and is the whole guard for
        # one that was: see the clear directly above.
        #
        # Under the publication guard, because that re-read is only decisive while no
        # first publish is halfway through. A first publish uploads the object BEFORE it
        # writes the record, so a destroy landing in that window finds no record, destroys
        # the artifact, and leaves a world-readable copy whose only handle went with it.
        # The guard makes the two orders the only ones reachable: the publish has not
        # started, so there is genuinely nothing published, or it has finished, so the
        # re-read finds the record and refuses below.
        #
        # The guard covers ANY well-formed slug, including one the store
        # did not resolve. The artifact that has to be excluded on an absent slug is one
        # CREATED and first-published entirely inside this window -- a case no pre-read can
        # resolve, since at read time it did not exist. The guard's registry is refcounted
        # and drops an entry once nobody holds or waits on it, so admitting a slug from the
        # request path does not let request volume grow that table: the cost is bounded by
        # concurrency, not by traffic. A malformed slug still reaches `delete` unguarded and
        # on purpose, so the store answers 4xx for it.
        #
        # Pinned to the generation this request resolved, for the same reason the record
        # re-read is: the 409 above is a check-then-act taken before this lock, so on its
        # own it leaves the narrow order where the replacement arrives after it. Passing
        # the stamp moves the decision inside the lock that removes the directory, which
        # is where it becomes decisive. When the store resolved nothing above, the stamp is
        # `EXPECT_ABSENT`: this request read the slug as empty, so any artifact the lock
        # finds there was created inside this window and the removal refuses it.
        #
        # Off the loop, INSIDE the guard: the removal is a recursive directory walk, so on
        # a versioned artifact over slow storage it blocks the gateway's only loop long
        # enough to stall chat and the liveness heartbeat, and the watchdog's answer to a
        # stalled loop is to kill and respawn. The guard and the generation stamp are
        # unaffected by moving the call to a worker thread: both are re-read inside the
        # store's own lock, which the worker takes, and `_run_off_loop` propagates the
        # store's exceptions unchanged so every refusal below still fires.
        async with AsyncExitStack() as guard:
            if slug_is_well_formed(slug):
                await guard.enter_async_context(publish_sync.publication_guard(slug))
            await _run_off_loop(
                lambda: get_default_store().delete(
                    slug,
                    refuse_if_published=True,
                    expect_created_at=(
                        EXPECT_ABSENT if _existing is None else _existing.created_at
                    ),
                )
            )
    except ArtifactStillPublishedError as exc:
        # A publish landed between the withdrawal and the removal. Refusing is the same
        # rule the withdrawal outcomes follow -- a refused delete is recoverable, an
        # orphaned public copy is not -- so the NEW publication keeps its handle and the
        # user is told what happened rather than silently losing the copy.
        msg = (
            "This artifact was published again while its earlier copy was being "
            "withdrawn, so it was not deleted. The earlier copy was withdrawn; the new "
            "one is still published and can still be taken down. Delete it again if you "
            "meant to remove the new copy too."
        )
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error=msg,
            extra={"slug": slug, "detail": str(exc)},
        )
        return _err(msg, status=409)
    except ArtifactReplacedError as exc:
        # Either a replacement arrived after the 409 check above and before this lock, or
        # an artifact was created at a slug this request read as empty. Both mean the same
        # thing to the owner: what is under this name is not what the delete started on,
        # and the removal has no undo.
        msg = (
            "The artifact under this name is not the one this delete started on -- it was "
            "created while the delete was running, so it was left alone. Delete it again "
            "if you meant to remove it."
        )
        _audit(
            tool="artifact_delete",
            request=request,
            outcome="denied",
            error=msg,
            extra={"slug": slug, "detail": str(exc)},
        )
        return _err(msg, status=409)
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
    # Deleted variant: open views of this slug toast + leave.
    _notify_artifact_update(
        state, slug, _existing.version if _existing is not None else 0, deleted=True
    )
    return _json_response({"ok": True})


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
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_record_event")
    if owner_denied is not None:
        return owner_denied
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
        # Off the loop for the same reason as the create path: an impression
        # appends to the event log and rewrites the record, both synced before
        # their rename. This one fires per rendered widget, so it is the more
        # frequent of the two.
        art, appended = await _run_off_loop(
            lambda: get_default_store().record_impression(
                slug,
                by=actor,
                session_id=session_id_hdr,
                message_ts=message_ts,
                widget_index=widget_index,
            )
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


async def api_artifact_relocate(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/relocate — update source_path."""
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_relocate")
    if owner_denied is not None:
        return owner_denied

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
    #   2. FIXED-ROOT containment — the resolved path must live under one of the
    #      roots produced by ``ArtifactStore.allowed_source_roots()`` (the user's
    #      home dir, the data home, and any operator-configured
    #      ``publish.relocate_roots``). Sharing that producer with the store's own
    #      read/write barriers is what stops the three copies drifting apart;
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
        # Fixed-root containment. The root SET comes from the store's single
        # producer (``ArtifactStore.allowed_source_roots``) so this barrier and
        # the store's own read/write barriers cannot drift: a copy omitting the
        # data-home root makes relocate refuse paths the store would then happily
        # read. is_relative_to on the resolved Paths is the sanitizer CodeQL
        # recognizes.
        allowed_roots = get_default_store().allowed_source_roots()
        # Fixed-root containment barrier — the COMPARISON stays inlined (NOT via
        # a helper) so CodeQL's intra-procedural taint tracker sees the
        # ``is_relative_to`` sanitizer guarding the SAME ``resolved_path`` that
        # the stat calls below use. Only the root list is factored out.
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
    # A source_path swap changes what live reads return.
    _notify_artifact_update(state, slug, art.version)
    return _json_response(_serialize(art, include_content=True))
