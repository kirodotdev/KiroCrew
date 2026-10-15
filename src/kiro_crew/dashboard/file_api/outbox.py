"""The agent outbox routes: ``POST /api/outbox/notify``, ``GET /api/outbox/{filename}`` and ``GET /api/outbox``, with the owner grant a flagged download passes."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _INLINE_DISPOSITION_PREFIXES,
        _SUBAGENT_SESSION_PREFIX,
        BINARY_MIME_ALLOWLIST,
        DashboardState,
        FileTooLargeError,
        _body_err_code,
        _PathProbeBusy,
        _probe_busy_response,
        _resolve_session_target,
        _run_path_probe,
        _sel,
        _subagent_parent_session_key,
        append_and_surface,
        binary_content_is_flagged,
        config_loader,
        dashboard_slot_key,
        file_delivery_consent,
        load_window_off_loop,
        logger,
        read_bounded_json,
        redact,
        rehydrate_slot_from_history_async,
        summary_rows,
        wide_content_is_flagged,
    )


def _read_outbox_file(raw_path: str, relative: bool = False) -> tuple[Path | None, bytes | None]:
    """Resolve, authorize, read and close on one bounded transfer worker.

    Only bytes and a path leave the worker, so cancelling its waiter never
    transfers descriptor cleanup to a cancelled coroutine.
    """
    from kiro_crew.hooks import safe_read_file_bytes  # noqa: F811

    outbox = config_loader.outbox_dir()
    path = ((outbox / raw_path) if relative else Path(raw_path)).resolve()
    if not path.is_relative_to(outbox.resolve()):
        return None, None
    return path, safe_read_file_bytes(str(path))


async def api_outbox_notify(request: web.Request) -> web.Response:
    """POST /api/outbox/notify — agent sent a file, notify the user."""
    state: DashboardState = request.app["state"]
    # Default cap: the body names an outbox file (path, filename, short
    # description, size) — the file bytes themselves never travel in it.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error=_body_err_code(body_err),
        )
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    raw_path = body.get("path", "")
    raw_filename = body.get("filename", "")
    raw_desc = body.get("description", "")
    # Reject files whose names/paths contain sensitive patterns
    if redact(raw_filename) != raw_filename or redact(raw_path) != raw_path:

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error="sensitive_filename_rejected",
        )
        file_delivery_consent.audit_refusal(
            file_delivery_consent.CLASS_OWNER_DASHBOARD,
            leg="notify",
            name=redact(raw_filename),
            reason="flagged name or path",
        )
        return web.json_response(
            {"error": "filename or path contains sensitive content"}, status=400
        )
    file_data = {
        "filename": raw_filename,
        "path": raw_path,
        "description": redact(raw_desc),
        "size": body.get("size", 0),
        "content_type": mimetypes.guess_type(raw_filename)[0] or "application/octet-stream",
    }
    # Validate file is readable + UTF-8 before creating a persistent card.
    try:
        resolved, raw = await _run_path_probe(_read_outbox_file, raw_path, transfer=True)
        if resolved is None:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error="path_outside_outbox",
            )
            return web.json_response({"error": "path must be inside outbox"}, status=403)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=raw_path, tool_name="file_send", session_key="api", source="api"
        )
    except FileTooLargeError as e:

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error=f"file_too_large: {e}",
        )
        return web.json_response({"error": str(e)}, status=413)
    if raw is None:

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error="file_not_found_or_access_denied",
        )
        return web.json_response({"error": "File not found or access denied"}, status=404)
    # Content is scanned whichever way it decodes: UTF-8 text below, non-UTF-8
    # bytes through the shared ``binary_content_is_flagged``. Binary must also
    # carry an allow-listed MIME type.
    try:
        text = raw.decode("utf-8")
        # The owner's grant covers this leg: the card renders in the owner's own
        # authenticated dashboard. No DELIVERY entry here -- that decision is
        # already recorded by the tool leg, and the byte handover is recorded by
        # the download route; a third entry for rendering a card would only bury
        # the two that answer a real question.
        #
        # The store read goes through a thread: ``is_granted`` ends in a
        # synchronous file read, and a coroutine that waits on storage stalls the
        # whole gateway. Ordered after the scan so a clean file never reads it.
        #
        # The wide pass runs here as well as on the binary branch below: a
        # credential written at UTF-16/UTF-32 spacing is NUL-interleaved ASCII,
        # which is valid UTF-8, so it decodes cleanly into this branch and the
        # contiguous-ASCII detectors in ``redact`` match none of it.
        flagged = redact(text) != text or await asyncio.to_thread(wide_content_is_flagged, raw)
        if flagged and not await asyncio.to_thread(
            file_delivery_consent.is_granted, file_delivery_consent.CLASS_OWNER_DASHBOARD
        ):
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error="sensitive_content_detected",
            )
            file_delivery_consent.audit_refusal(
                file_delivery_consent.CLASS_OWNER_DASHBOARD,
                leg="notify",
                name=raw_filename,
                reason="flagged content",
            )
            return web.json_response({"error": "file content contains sensitive data"}, status=400)
    except UnicodeDecodeError:
        # Binary file — only allow known-safe media types
        guessed_type = mimetypes.guess_type(raw_filename)[0] or ""
        if guessed_type not in BINARY_MIME_ALLOWLIST:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error=f"binary_mime_not_allowed: {guessed_type}",
            )
            return web.json_response(
                {"error": f"Binary file type not allowed: {guessed_type or 'unknown'}"}, status=400
            )
        # An allow-listed media type is a container, not a guarantee about its
        # contents, so the same grant decides here as on the text branch above.
        # Off the event loop: the scan is CPU work over up to the read cap, and a
        # media file is routinely orders of magnitude larger than a text one.
        if await asyncio.to_thread(binary_content_is_flagged, raw) and not await asyncio.to_thread(
            file_delivery_consent.is_granted, file_delivery_consent.CLASS_OWNER_DASHBOARD
        ):
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error="binary_credential_detected",
            )
            file_delivery_consent.audit_refusal(
                file_delivery_consent.CLASS_OWNER_DASHBOARD,
                leg="notify",
                name=raw_filename,
                reason="flagged binary content",
            )
            return web.json_response(
                {
                    "error": "binary file contains embedded credentials",
                    "code": "binary_credential_detected",
                },
                status=400,
            )
    # Inject the file card into the caller's chat slot so it persists in the
    # correct session. This runs even when ``state._slots`` is empty: a headless
    # script cron typically has no dashboard tab open at all, and its origin slot
    # is rehydrated from history below — gating the whole block on
    # ``if state._slots`` skipped exactly that case.
    # Prefer the caller's own slot via X-Session-Key header
    session_key = request.headers.get("X-Session-Key", "").strip()
    active = None
    if session_key.startswith("cron:"):
        # A cron slot is named cron-<job-id>, which is not the session key folded.
        # Only the JOB ID: a cron turn's key can carry a further segment
        # (`cron:<job>:<run>` for a per-run session, `cron:<job>:<agent>` for a
        # multi-agent one), and folding the whole tail asks for a `cron-<job>:<run>`
        # slot that never exists — so every suffixed turn missed its own open tab
        # and fell through to origin resolution or suppression.
        job_id = session_key.removeprefix("cron:").split(":", 1)[0]
        active = state.get_slot(f"cron-{job_id}")
        if active is None:
            # A headless script cron has no live "cron-<id>" slot. Rather than
            # leak the card into whichever tab happens to be focused, route it to
            # the cron's ORIGIN dashboard session — the chat that created the cron
            # — through the same resolver ``send_message(session="origin")`` uses,
            # so both delivery paths agree on where a cron's output belongs.
            origin_slot_key, _origin_job = _resolve_session_target(state, "origin", session_key)
            if origin_slot_key:
                # get_slot is the hot path (O(1)); on a miss the origin session
                # exists on disk but has no tab open, so rehydrate it — with the
                # transcript read off the loop, the shape the sibling origin path
                # established, because a large store would otherwise stall the
                # gateway. A truly-gone session (never persisted, deleted, or
                # closed) returns None and falls through to suppression below; no
                # phantom empty tab is ever created.
                active = state.get_slot(origin_slot_key)
                if active is None:
                    active = await rehydrate_slot_from_history_async(state, origin_slot_key)
    elif session_key.startswith(_SUBAGENT_SESSION_PREFIX):
        # A sub-agent has no tab of its own, so route its card to the PARENT slot
        # — the same destination its completion injection and its ``subagent_*`` WS
        # frames already use. Only the dedicated-process arm arrives here: a
        # shared-runtime sub-agent's MCP stub carries the parent's own key and is
        # resolved by the branch below. An unknown run or a parent with no open tab
        # yields "" and falls through to suppression — never into an unrelated
        # conversation.
        parent_key = _subagent_parent_session_key(state, session_key)
        parent_slot_key = dashboard_slot_key(parent_key) if parent_key else ""
        if parent_slot_key:
            active = state.get_slot(parent_slot_key)
    else:
        # A channel-born conversation keeps its channel key (slack:<ts>)
        # while its tab is open, so the slot name comes from the surface
        # lookup — stripping a "dashboard:" prefix would miss it and drop the
        # card into whichever tab happened to be active last.
        slot_key = dashboard_slot_key(session_key)
        if slot_key:
            active = state.get_slot(slot_key)
    # An explicitly header-targeted slot receives the file even when empty
    header_targeted = active is not None
    # Fallback: most recently active slot — ONLY for a legacy headerless caller,
    # the best-effort case it was written for. A key that IS present but resolves
    # to nothing names a session we could not reach (a cron with no originating
    # chat, an unknown job, a sub-agent whose parent has no tab, a task-runner or
    # webhook session that owns no chat, a closed tab); suppress the card rather
    # than surface it in an unrelated conversation.
    if not active and not session_key and state._slots:
        # The summary rows, so picking the newest tab does not load every
        # restored sidebar row's window to read one timestamp.
        active = max(
            state._slots.values(),
            key=lambda s: summary_rows(s)[-1]["ts"] if summary_rows(s) else "",
        )
    if active is not None and getattr(active, "window_pending", False) is True:
        # The card is appended to the destination's rows: a tab restored as a
        # sidebar row loads its window first, with the read off the loop, and the
        # destination is read again after the await.
        await load_window_off_loop(active)
        if state.get_slot(active.key) is not active:
            active = None
    delivered = False
    if active is not None and (active.messages or header_targeted):
        delivered = True
        # Route through the context-aware redact() so a loaded companion's
        # extra credential regexes scrub the broadcast file JSON too — the
        # same overlay-aware pass the filename/path/description gates use.
        redacted_file_json = redact(json.dumps(file_data))
        # append_and_surface, not a hand-built broadcast_ws: hand-built
        # frames ship the row a second time and carry no ``meta.mid``, so
        # the client cannot recognise the redelivery and renders a
        # duplicate card.
        append_and_surface(state, active, "file", redacted_file_json)
    else:
        # Suppression is the RIGHT outcome — better nowhere than in an unrelated
        # conversation — but it is silent, and a caller that reads `ok: true` has
        # no way to tell a delivered card from a vanished one. So say so once, at
        # the only point that knows both that a key was supplied and that it
        # resolved to no destination. The key is logged because it is the whole
        # diagnosis (which namespace, which id); the file is already named in the
        # audit event below.
        logger.info(
            "outbox notify: no destination for session key %r; file card suppressed",
            session_key or "<none>",
        )

    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind="notify",
        outcome="completed",
        # `delivered` distinguishes the two outcomes this endpoint folds into one
        # 200: the card reached a session, or it was suppressed for want of a
        # destination. Carried here rather than as a separate SEL outcome so the
        # existing "completed" consumers keep working.
        resources=f"filename={file_data['filename']} delivered={int(delivered)}",
    )
    return web.json_response({"ok": True})


async def _owner_grant_releases_flagged(request: web.Request) -> bool:
    """Whether the owner's ``owner_dashboard`` grant releases flagged bytes to THIS caller.

    The one test every owner-facing download route applies to a file the scanner
    flagged: ``GET /api/outbox/{filename}`` and ``GET /api/file-download`` serve the
    same audience, so they share one gate rather than two copies of it. Call it only
    inside a flagged-content branch, so a clean file never touches the store.

    The store read goes through ``asyncio.to_thread``: ``is_granted`` ends in a
    synchronous file read, and a coroutine that waits on storage stalls the whole
    gateway event loop.

    TWO conjuncts, and the second is not redundant. A route absent from every
    ``token_auth`` bypass list needs AUTHENTICATION, which is not OWNER IDENTITY. A
    Slack allow-listed non-owner running ``!dashboard`` authenticates with
    ``app == ""`` and ``sub != owner_id``, so ordinary token auth admits them while
    ``is_owner_dashboard_request`` does not. Without the owner conjunct the grant
    would convert a clean 400-for-everyone into raw bytes for every authenticated
    caller -- widening the audience as a side effect of a control meant to narrow
    it. A route with its own front owner gate still asks here, so the grant's
    audience never depends on each caller remembering that gate.
    """
    from kiro_crew.dashboard.handlers.source_providers import (  # lazy: import cycle
        is_owner_dashboard_request,
    )

    granted = await asyncio.to_thread(
        file_delivery_consent.is_granted, file_delivery_consent.CLASS_OWNER_DASHBOARD
    )
    return granted and is_owner_dashboard_request(request)


def _audit_owner_grant_refusal(request: web.Request, *, name: str, what: str) -> None:
    """Record a flagged download the grant gate refused, naming the conjunct that refused.

    *what* names the scan that tripped (``"flagged content"`` or ``"flagged binary
    content"``). The reason then says which conjunct stopped it.

    Identity is the discriminator, not the grant. The conjunction short-circuits, so
    in the default no-grant state a non-owner is refused before identity is ever
    examined; an entry keyed off the grant would record a Slack allow-listed
    non-owner reaching for a flagged file as an ordinary scanner hold-back,
    byte-identical to the owner's own -- losing the attribution exactly in the
    configuration almost every install runs. The identity read is pure attribute
    reads, so unlike the grant it needs no thread.
    """
    from kiro_crew.dashboard.handlers.source_providers import (  # lazy: import cycle
        is_owner_dashboard_request,
    )

    cross_principal = not is_owner_dashboard_request(request)
    file_delivery_consent.audit_refusal(
        file_delivery_consent.CLASS_OWNER_DASHBOARD,
        leg="download",
        name=name,
        reason=f"{what}, non-owner caller" if cross_principal else f"{what}, no grant",
        caller=str(request.get("user") or "unknown") if cross_principal else "",
    )


def _audit_consented_download(name: str) -> None:
    """Record that flagged bytes left for the owner's browser under the grant.

    The refusal this replaces is self-evident in the 400, whereas a successful
    consented download would otherwise leave no trace.
    """
    file_delivery_consent.audit_decision(
        file_delivery_consent.CLASS_OWNER_DASHBOARD,
        outcome="delivered",
        detail=f"download: {name}",
    )


async def api_outbox_download(request: web.Request) -> web.StreamResponse:
    """GET /api/outbox/{filename} — download a file from the outbox."""
    filename = request.match_info["filename"]
    try:
        path, raw = await _run_path_probe(_read_outbox_file, filename, True, transfer=True)
        if path is None:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="download",
                outcome="denied",
                error=f"path_traversal: {filename}",
            )
            return web.json_response({"error": "forbidden"}, status=403)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=filename, tool_name="file_send", session_key="api", source="api"
        )
    except FileTooLargeError as e:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"file_too_large: {e}",
        )
        return web.json_response({"error": str(e)}, status=413)
    if raw is None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"safe_read_file_bytes rejected: {filename}",
        )
        return web.json_response({"error": "forbidden"}, status=403)
    # Content is scanned whichever way it decodes: UTF-8 text here, non-UTF-8
    # bytes once the MIME allow-list below has admitted the type.
    is_text = True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        is_text = False

    def _audit_consented_handover() -> None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="completed",
            error="sensitive_content_delivered_with_consent",
        )
        _audit_consented_download(path.name)

    if is_text:
        # Two passes on this branch, not one. ``redact`` reads the correctly
        # decoded text, which is the more accurate read of it; the wide pass reads
        # the raw bytes, because a credential at UTF-16/UTF-32 spacing is
        # NUL-interleaved ASCII, decodes as valid UTF-8 into this very branch, and
        # arrives with its characters separated so no contiguous-ASCII detector
        # matches. Short-circuited, so a file the text pass already flags pays no
        # second scan.
        redacted = redact(text)
        narrow_flagged = redacted != text
        if narrow_flagged or await asyncio.to_thread(wide_content_is_flagged, raw):
            if not await _owner_grant_releases_flagged(request):
                _sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name="file_send",
                    tool_kind="download",
                    outcome="denied",
                    error="content_redacted" if narrow_flagged else "wide_credential_detected",
                )
                _audit_owner_grant_refusal(request, name=path.name, what="flagged content")
                return web.json_response(
                    {"error": "file content was redacted; download aborted"}, status=400
                )
            _audit_consented_handover()
    safe_name = urllib.parse.quote(path.name, safe="")
    content_type, _ = mimetypes.guess_type(path.name)
    if not content_type:
        content_type = "application/octet-stream"
    # Binary files must be in the allowlist
    if not is_text and content_type not in BINARY_MIME_ALLOWLIST:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"binary_mime_not_allowed: {content_type}",
        )
        return web.json_response(
            {"error": f"Binary file type not allowed: {content_type}"}, status=403
        )
    if not is_text:
        # An allow-listed media type says the browser can render these bytes
        # safely, not that a credential cannot be sitting inside them. Scanned
        # after the allow-list so a type this route refuses outright is never
        # scanned, and off the event loop because the scan is CPU work over up to
        # the read cap and a media file is routinely far larger than a text one.
        if await asyncio.to_thread(binary_content_is_flagged, raw):
            if not await _owner_grant_releases_flagged(request):
                _sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name="file_send",
                    tool_kind="download",
                    outcome="denied",
                    error="binary_credential_detected",
                )
                _audit_owner_grant_refusal(request, name=path.name, what="flagged binary content")
                return web.json_response(
                    {
                        "error": "binary file contains embedded credentials; download aborted",
                        "code": "binary_credential_detected",
                    },
                    status=400,
                )
            _audit_consented_handover()
    # Inline disposition for media types the browser can render
    disposition = (
        "inline"
        if any(content_type.startswith(t) for t in _INLINE_DISPOSITION_PREFIXES)
        else "attachment"
    )
    # SVG can contain scripts — never serve inline on the dashboard origin
    if content_type == "image/svg+xml":
        disposition = "attachment"
    # Text files always attachment — prevents content injection via crafted filenames
    if is_text:
        disposition = "attachment"
    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind="download",
        outcome="completed",
        resources=f"filename={filename}",
    )
    return web.Response(
        body=raw,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{safe_name}",
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
        },
    )


async def api_outbox_list(request: web.Request) -> web.Response:
    """GET /api/outbox — list files in the outbox."""
    from kiro_crew.config.loader import outbox_dir  # noqa: F811

    entries = []
    odir = outbox_dir()
    if not odir.is_dir():
        return web.json_response({"files": []})
    for f in odir.iterdir():
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        if f.is_file() and redact(f.name) == f.name:
            entries.append({"filename": f.name, "size": st.st_size, "modified": st.st_mtime})
    entries.sort(key=lambda x: float(x["modified"]), reverse=True)  # type: ignore[arg-type,return-value]

    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind="list",
        outcome="completed",
        resources=f"count={len(entries)}",
    )
    return web.json_response({"files": entries[:50]})
