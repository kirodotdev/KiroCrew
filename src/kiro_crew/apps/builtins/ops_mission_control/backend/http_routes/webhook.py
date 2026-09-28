"""The signed ``/webhook`` ingress: the one route that accepts input from whoever can reach
the port.

The body is read incrementally and refused one byte past the cap, trust is established by
``providers.webhook.enqueue`` before anything is parsed, and each rejection maps to the
status that tells the sender what to fix.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import AuditWriter
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook as webhook_mod

#: ``Retry-After`` for a 503 from a full webhook spool. One dispatch interval (the
#: manifest's ``dispatch`` cron is ``every: 120``), because that is when the spool next
#: drains — a shorter value invites a hot loop against a queue that cannot have moved.
_SPOOL_RETRY_AFTER_SECS = 120

#: Rejections that mean "I don't trust you" rather than "your body is wrong".
#: ``enqueue`` returns these before parsing anything, so they are the only ones that
#: are genuinely authentication/authorization failures.
_WEBHOOK_AUTH_REJECTIONS = frozenset(
    {
        "webhook source is not enabled",
        "no signing secret configured",
        "signature mismatch",
    }
)

#: Rejections that mean "you are trusted, but this body is wrong" — a 400. Listed
#: explicitly rather than inferred as "everything else" so an unclassified reason
#: falls through to 401 instead of being silently reported as a body fault.
_WEBHOOK_PAYLOAD_REJECTIONS = frozenset(
    {
        "malformed JSON",
        "payload must be a JSON object",
        "payload has no title",
    }
)


async def _read_capped(request: web.Request, cap: int) -> bytes | None:
    """Read at most ``cap`` bytes of body; ``None`` if the client sent more.

    Reads ONE byte past the cap so "exactly at the limit" is still accepted while
    anything larger is detected without buffering it — the peak is ``cap + chunk``,
    not whatever the sender chose. ``request.read()`` cannot do this: it returns only
    after the whole body is in memory, and these routes sit on the shared gateway
    application whose ``client_max_size`` is 60 MiB.

    ``Content-Length`` is checked first when present, so an honest oversized delivery
    is refused before a single chunk is read; a lying or absent header is caught by the
    streaming count, which is the authority.
    """
    declared = request.content_length
    if declared is not None and declared > cap:
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await request.content.read(webhook_mod.READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _webhook_reject_status(detail: str) -> int:
    """Map a rejection reason to its HTTP status.

    "malformed JSON" and "payload has no title" are *authenticated* requests with a
    bad body. Answering 401 for them tells a sender debugging a payload to go
    re-check credentials that are fine, and makes a real signature failure look
    identical to a typo. Payload faults are 400; only the trust checks are 401.
    Defaults to 401 for an unrecognized
    reason, so a newly-added rejection is treated as auth-ish rather than
    accidentally advertised as "your request was fine".
    """
    if detail in _WEBHOOK_AUTH_REJECTIONS:
        return 401
    if detail == webhook_mod.REJECT_BODY_TOO_LARGE:
        return 413
    if detail == webhook_mod.REJECT_SPOOL_FULL:
        # 503, not 4xx: the delivery was well-formed and trusted, WE are the ones who cannot
        # take it right now. That distinction is what makes it retriable — Alertmanager and
        # friends re-deliver on a 5xx, so a full spool becomes a delay rather than a lost page.
        return 503
    if detail in _WEBHOOK_PAYLOAD_REJECTIONS:
        return 400
    # Unrecognized: fail toward 401 rather than 400. A new rejection reason added to
    # ``enqueue`` without classifying it here is more likely to be a trust check than
    # a body complaint, and telling a caller "your request was fine, just malformed"
    # about a refusal we do not understand is the wrong default.
    return 401


async def _handle_webhook(request: web.Request, *, _audit: AuditWriter) -> web.StreamResponse:
    """Accept a signed inbound signal.

    Fail-closed on the HMAC: an unsigned or mis-signed delivery is rejected, so
    enabling this adapter cannot open an unauthenticated path that manufactures
    work on the board. Note the check ORDER in ``webhook.enqueue`` — enabled →
    secret → size → signature → parse. Nothing unauthenticated is ever parsed, and
    an oversized body is refused before it is hashed.

    The body is read INCREMENTALLY, and that is a memory bound rather than a
    nicety. ``enqueue``'s ``len(raw_body) > MAX_BODY_BYTES`` check can only run on a
    body already in memory, and these routes register on the shared gateway
    application whose ``client_max_size`` is 60 MiB (it carries file uploads), so a
    plain ``await request.read()`` buffered up to 60 MiB per concurrent delivery
    before refusing 256 KiB of it. Stopping one byte past the cap keeps the refusal
    O(cap) instead of O(what the client chose to send). Found in review (GPT 5.6).
    """
    raw = await _read_capped(request, webhook_mod.MAX_BODY_BYTES)
    if raw is None:
        # Reuses ``enqueue``'s own reason string so the body and the audit line read the
        # same whether the cap is hit here or inside ``enqueue``. The status is the
        # LITERAL 413 rather than `_webhook_reject_status(detail)`: this branch has exactly
        # one reason, so the mapping call would compute a statically-known value — and the
        # error-code contract gate ratchets computed statuses precisely because hoisting a
        # status into an expression is how a missing `code` escapes review.
        detail = webhook_mod.REJECT_BODY_TOO_LARGE
        _audit("webhook_ingest", detail, "rejected", error=detail)
        return web.json_response({"error": detail, "code": "webhook_rejected"}, status=413)
    signature = request.headers.get(webhook_mod.SIGNATURE_HEADER, "")
    accepted, detail = await asyncio.to_thread(webhook_mod.enqueue, raw, signature)
    _audit(
        "webhook_ingest",
        detail,
        "success" if accepted else "rejected",
        error="" if accepted else detail,
    )
    if not accepted:
        status = _webhook_reject_status(detail)
        headers = {}
        if status == 503:
            # Tell the sender WHEN, so a retry does not hot-loop against a full spool. One
            # dispatch interval is the honest answer: that is when the spool next drains.
            headers["Retry-After"] = str(_SPOOL_RETRY_AFTER_SECS)
        return web.json_response(
            {"error": detail, "code": "webhook_rejected"}, status=status, headers=headers
        )
    return web.json_response({"ok": True, "signal": detail, "queued": webhook_mod.queue_depth()})
