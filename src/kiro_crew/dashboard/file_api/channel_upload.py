"""Shipping a local file to a channel: ``POST /api/slack/upload-file`` and ``POST /api/channel/upload-file``, behind their shared admission gate."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        BINARY_MIME_ALLOWLIST,
        DashboardState,
        FileTooLargeError,
        OutboundFile,
        _audit_file_send,
        _body_err_code,
        _probe_persisted_session,
        _sel,
        binary_content_is_flagged,
        config_loader,
        read_bounded_json,
        redact,
        redact_for_display,
        safe_read_file_bytes,
        upload_destination,
        wide_content_is_flagged,
    )


def is_tracked_channel(channel_id: str) -> bool:
    """Load the Slack probe only when a file delivery needs it."""
    from kiro_crew.slack.handler import is_tracked_channel as probe

    return probe(channel_id)


def _gate_upload_file(
    file_path: str, filename: str, *, tool_kind: str
) -> tuple[web.Response | None, Path | None, bytes | None]:
    """The shared admission gate for shipping a local file to a channel.

    One site computes the judgment for every channel-upload endpoint —
    containment (outbox or workspace root), the descriptor-safe read, the
    binary MIME allowlist, and the content credential scans — so the Slack
    and channel legs cannot drift apart gate by gate. Returns
    ``(error_response, None, None)`` on refusal, ``(None, resolved, bytes)``
    when the file may ship. *tool_kind* keys the SEL records so each caller
    keeps its own audit lane.

    Blocking by design (a full read of up to ``MAX_FILE_BYTES`` plus content
    regex scans): async handlers MUST run it off the event loop via
    ``asyncio.to_thread`` — SEL appends are internally locked, so the audit
    calls are thread-safe. The loader is called through its module so tests
    (and config reloads) resolve at call time, not import time.
    """

    def _audit_denial(error: str, *, outcome: str = "denied") -> None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind=tool_kind,
            outcome=outcome,
            downstream_service=tool_kind,
            error=error,
        )

    if not file_path or not filename:
        _audit_denial("missing_required_fields")
        return (
            web.json_response(
                {"error": "file_path, filename required", "code": "missing_required_fields"},
                status=400,
            ),
            None,
            None,
        )
    # The name is DELIVERED (Slack upload title, Telegram document name,
    # Discord message text fallback), so a credential embedded in it leaves
    # with the file. Checked in the shared gate so no leg can drift from the
    # others, and before path resolution so a sensitive name never even
    # selects a file. Mirrors the MCP-side file_send refusal.
    if redact(filename) != filename:
        _audit_denial(f"sensitive_filename_rejected: {redact(filename)}")
        return (
            web.json_response(
                {
                    "error": "filename contains sensitive content",
                    "code": "sensitive_filename",
                },
                status=400,
            ),
            None,
            None,
        )
    resolved = Path(file_path).resolve()
    allowed_outbox = config_loader.outbox_dir().resolve()
    allowed_workspace = config_loader.workspace_root().resolve()
    if not (resolved.is_relative_to(allowed_outbox) or resolved.is_relative_to(allowed_workspace)):
        _audit_denial(f"path_not_allowed: {file_path}")
        return (
            web.json_response(
                {
                    "error": "file_path must be under the outbox directory or the workspace root",
                    "code": "path_not_allowed",
                },
                status=403,
            ),
            None,
            None,
        )
    try:
        raw = safe_read_file_bytes(str(resolved))
    except FileTooLargeError as e:
        _audit_denial(f"file_too_large: {e}")
        return (
            web.json_response({"error": str(e), "code": "file_too_large"}, status=413),
            None,
            None,
        )
    if raw is None:
        _audit_denial(f"safe_read_file_bytes rejected: {file_path}")
        return (
            web.json_response(
                {
                    "error": f"File not found or access denied: {file_path}",
                    "code": "file_not_found",
                },
                status=404,
            ),
            None,
            None,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Binary file — only allow known-safe media types
        guessed_type = mimetypes.guess_type(filename)[0] or ""
        if guessed_type not in BINARY_MIME_ALLOWLIST:
            _audit_denial(f"binary_mime_not_allowed: {guessed_type}")
            return (
                web.json_response(
                    {
                        "error": f"Binary file type not allowed: {guessed_type or 'unknown'}",
                        "code": "binary_mime_not_allowed",
                    },
                    status=400,
                ),
                None,
                None,
            )
        text = None  # signal: skip text redaction path
        # An allow-listed media type is a container, not a guarantee about its
        # contents: base64 key material inside a PDF is the case this catches.
        # Unconditional on this leg. The owner-facing gates weigh a recorded
        # owner decision against a positive result; this leg has a third-party
        # audience and so has nothing to weigh, which is why it reads no store at
        # all -- a property asserted on this function's own source.
        if binary_content_is_flagged(raw):
            _audit_denial(f"binary_credential_detected: {filename}")
            return (
                web.json_response(
                    {
                        "error": "binary file contains embedded credentials",
                        "code": "binary_credential_detected",
                    },
                    status=400,
                ),
                None,
                None,
            )
    if text is not None:
        try:
            redacted = redact(text)
            if redacted != text:
                _audit_denial(f"content_redacted: {filename}")
                return (
                    web.json_response(
                        {
                            "error": "file content was redacted; upload aborted",
                            "code": "content_redacted",
                        },
                        status=400,
                    ),
                    None,
                    None,
                )
            # Wide-encoded credentials reach this branch rather than the binary one
            # above: NUL-interleaved ASCII is valid UTF-8, so the decode succeeds
            # and ``redact`` sees characters separated by NUL, which matches no
            # detector. Unconditional here for the same reason the binary scan is:
            # this leg has a third-party audience and no owner grant to weigh.
            if wide_content_is_flagged(raw):
                _audit_denial(f"wide_credential_detected: {filename}")
                return (
                    web.json_response(
                        {
                            "error": "file contains embedded credentials",
                            "code": "wide_credential_detected",
                        },
                        status=400,
                    ),
                    None,
                    None,
                )
        except Exception as redact_err:
            _audit_denial(f"redaction_failed: {redact_err}", outcome="error")
            return (
                web.json_response(
                    {"error": f"Redaction failed: {redact_err}", "code": "redaction_failed"},
                    status=500,
                ),
                None,
                None,
            )
    return None, resolved, raw


async def api_slack_upload_file(request: web.Request) -> web.Response:
    """POST /api/slack/upload-file — upload a file to Slack (internal, called by file_send).

    Destination and authorization come from the shared oracle
    (:func:`kiro_crew.dashboard.upload_destination.resolve_slack`), which holds
    this leg's ladder — the ``channels``-scope governance vet, the
    restricted-session ceiling, then a request-named channel, a
    session-map-linked thread, or the owner-DM fallback with its tracked-channel
    authorization — next to the non-Slack leg's, so the two cannot drift apart
    rung by rung. What stays here is what only this leg can
    answer: the Slack client, its upload verb, and the response shapes.

    The client-presence check stays AHEAD of the body parse: a gateway with no
    Slack client answers ``skipped: no_slack`` even for a malformed body.
    """
    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        _audit_file_send(leg="slack", outcome="skipped", error="no_slack_client")
        return web.json_response({"ok": True, "skipped": "no_slack"})
    # Default cap: the body carries a file path, a filename, and Slack routing
    # ids — the file bytes are read from disk, never from this body.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        _audit_file_send(leg="slack", outcome="denied", error=_body_err_code(body_err))
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    file_path_raw = body.get("file_path", "")
    filename = body.get("filename", "")
    # Off-loop: the gate reads up to MAX_FILE_BYTES and regex-scans the content
    # (no-blocking-call-on-event-loop).
    error_resp, resolved, raw = await asyncio.to_thread(
        _gate_upload_file, file_path_raw, filename, tool_kind="slack"
    )
    if error_resp is not None:
        return error_resp
    assert resolved is not None and raw is not None  # narrowed by the gate
    # ``is_tracked_channel`` and the persisted-transcript probe are handed to the
    # oracle rather than imported there: one binding site, and the module stays
    # free of both the Slack handler's config dependency and the ``dashboard``
    # package ``messaging.upload_gate`` may not import.
    destination = await upload_destination.resolve_slack(
        state,
        slack,
        session_key=request.headers.get("X-Session-Key", "").strip(),
        requested_channel=body.get("channel", ""),
        thread_ts=body.get("thread_ts"),
        tracked_probe=is_tracked_channel,
        persisted_probe=_probe_persisted_session,
    )
    if isinstance(destination, upload_destination.Refusal):
        _audit_file_send(
            leg="slack",
            outcome="denied",
            error=destination.audit_error,
            downstream=destination.downstream,
        )
        # One branch per literal status, body inline. `status=<expression>` and a
        # body hoisted into a variable are both invisible to the error-code
        # contract scanner, which counts either as its own bucket
        # (test_error_code_contract) -- so the refusal says WHICH answer it is
        # and each answer is spelled out here.
        if destination.status == 400:
            return web.json_response(
                {"error": destination.error, "code": destination.code}, status=400
            )
        return web.json_response({"error": destination.error, "code": destination.code}, status=403)
    if isinstance(destination, upload_destination.Skip):
        _audit_file_send(leg="slack", outcome="skipped", error=destination.reason)
        return web.json_response({"ok": True, "skipped": destination.reason})
    try:
        # The filename was already cleared by the shared admission gate above —
        # same predicate, same value, strictly earlier in this function — so the
        # leg does not re-check it. That gate is the one site for the rule; a
        # second copy here could only drift from it.
        await slack.upload_file(
            destination.channel,
            destination.thread_ts,
            str(resolved),
            filename,
            filename,
        )
        _audit_file_send(
            leg="slack",
            outcome="completed",
            downstream="slack",
            resources=f"channel={destination.channel} file={file_path_raw}",
        )
        return web.json_response({"ok": True})
    except Exception as e:
        # A Slack SDK / network exception can carry file paths, host and URL
        # fragments, or credentials embedded in a URL. Sanitize before it
        # reaches the client or the audit record (see api_slack_pins).
        from kiro_crew.security import redact as redact_composed

        safe_error = redact_composed(str(e))
        _audit_file_send(leg="slack", outcome="error", downstream="slack", error=safe_error)
        return web.json_response({"error": safe_error}, status=500)


async def api_channel_upload_file(request: web.Request) -> web.Response:
    """POST /api/channel/upload-file — deliver a file to the caller's own
    conversation on a non-Slack channel (internal, called by file_send).

    Destination and authorization come from the shared oracle
    (:func:`kiro_crew.dashboard.upload_destination.resolve_channel`), which for
    this leg is the SAME send ladder the cross-surface reply mirror uses
    (``_resolve_mirror_target``): channel-scope governance, transport
    registration, proactive-send capability, and ``may_send_to`` recipient
    re-authorization, all fail-closed and SEL-audited in one place — plus the
    restricted-session ceiling the renderers' extraction path enforces, on the
    same shared predicate. The destination comes exclusively from the caller's
    session map entry — a request cannot name an arbitrary conversation, which is
    what keeps this endpoint from being a broadcast primitive. The oracle also
    resolves the delivery verb, since which channels have one is part of "can
    this file land here": the channels in ``upload_destination.DOCUMENT_CHANNELS``
    (Telegram, Discord, WeCom), each via its own purpose-built name-preserving
    ``send_document``; every other channel is a
    skip until its transport grows that verb. The Slack counterpart above
    resolves through the same module, one rung table away.

    "Cannot deliver here" is a SKIP (``delivered: false``), not an error: most
    sessions mirror nowhere, and the caller falls back to the dashboard card
    and the Slack leg.
    """
    state: DashboardState = request.app["state"]
    # Default cap: same shape as the Slack leg — a path, a filename, and a
    # short description; the file bytes are read from disk by the gate.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        _audit_file_send(leg="channel", outcome="denied", error=_body_err_code(body_err))
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    def _skip(reason: str) -> web.Response:
        _audit_file_send(leg="channel", outcome="skipped", error=reason)
        return web.json_response({"ok": True, "delivered": False, "skipped": reason})

    destination = await upload_destination.resolve_channel(
        state,
        request.headers.get("X-Session-Key", "").strip(),
        persisted_probe=_probe_persisted_session,
    )
    if isinstance(destination, upload_destination.Skip):
        return _skip(destination.reason)
    link, deliver = destination.link, destination.deliver
    # Off-loop: the gate reads up to MAX_FILE_BYTES and regex-scans the content
    # (no-blocking-call-on-event-loop).
    error_resp, resolved, raw = await asyncio.to_thread(
        _gate_upload_file,
        body.get("file_path", ""),
        body.get("filename", ""),
        tool_kind="channel",
    )
    if error_resp is not None:
        return error_resp
    assert resolved is not None and raw is not None  # narrowed by the gate
    filename = body.get("filename", "")
    # Display-form redaction, not just literal: redact() scans bytes, and the
    # channel's renderer strips markup at display time — ``AKIA**…**`` passes
    # a literal scan and displays as an intact key. Same boundary rule every
    # renderer sink applies (``redact_for_display``) before text reaches a
    # transport.
    description, _ = redact_for_display(body.get("description", "") or "", redact)
    outbound = OutboundFile(
        path=str(resolved),
        data=raw,
        alt=description,
        mime=mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )
    try:
        mid = await deliver(
            link.channel_id,
            outbound,
            caption=description,
            thread_id=link.thread_id,
        )
    except Exception as e:
        # A transport / network exception can carry file paths, host and URL
        # fragments, or credentials embedded in a URL. Sanitize before it
        # reaches the client or the audit record (see api_slack_upload_file).
        from kiro_crew.security import redact as redact_composed

        safe_error = redact_composed(str(e))
        _audit_file_send(
            leg="channel",
            outcome="error",
            downstream=link.channel_type,
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)
    if not mid:
        # The transport reported failure without raising (the clients return
        # an empty id on an API-level refusal).
        _audit_file_send(
            leg="channel",
            outcome="error",
            downstream=link.channel_type,
            error="delivery_reported_no_message_id",
        )
        return web.json_response({"error": "channel delivery failed"}, status=502)
    _audit_file_send(
        leg="channel",
        outcome="completed",
        downstream=link.channel_type,
        resources=f"channel_type={link.channel_type} file={body.get('file_path', '')}",
    )
    return web.json_response({"ok": True, "delivered": True, "channel_type": link.channel_type})
