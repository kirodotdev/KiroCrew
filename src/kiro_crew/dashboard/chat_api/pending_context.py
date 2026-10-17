"""App-supplied pending context: ``POST /api/chat/slots/{slot}/context``, with the
content, source and age validators and the per-source cap the note route shares.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    import time

    from kiro_crew.dashboard.chat_handlers import (
        _MAX_CONTEXT_CONTENT,
        _MAX_CONTEXT_PER_SOURCE,
        _MAX_SOURCE_LEN,
        _SOURCE_CTRL_RE,
        DashboardState,
        _ChatSlot,
        _reauthorize_after_await,
        context_entry_expired,
        deny_app_slot_session_access,
        read_bounded_json,
        redact_credentials,
        sel,
        slot_not_found,
    )


def _validate_content(content: object) -> web.Response | None:
    """Shared content validation for /context and /note.

    Validating at the request boundary in ONE place is what keeps the two entry
    points from drifting. Returns a 400 response on a bad value, else None.
    """
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"},
            status=400,
        )
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "empty_content"},
            status=400,
        )
    if len(content) > _MAX_CONTEXT_CONTENT:
        return web.json_response(
            {
                "error": f"content exceeds {_MAX_CONTEXT_CONTENT} char limit",
                "code": "content_too_long",
            },
            status=400,
        )
    return None


def _is_secret_shaped_context(content: str) -> bool:
    """Whether the credential redactor would rewrite pending-context *content*.

    Pending context is in-band: the drain prepends it to the next user message,
    so the model reads it as ordinary prompt text and "consume silently" is a
    request, not a guarantee. It is not a confidential channel, and a payload
    that carries a secret hands that secret to the model and to everything the
    turn's output reaches. The check is the credential redactor itself -- a
    payload it would rewrite is a payload that is refused -- so the admission
    rule and the redaction rule cannot drift apart.
    """
    cleaned, _ = redact_credentials(content)
    return cleaned != content


def _refuse_secret_shaped_context(content: str) -> web.Response | None:
    """400 ``context_secret_shaped`` for credential-shaped *content*, else None.

    The refusal never echoes any part of the content.
    """
    if not _is_secret_shaped_context(content):
        return None
    return web.json_response(
        {
            "error": (
                "pending context must not carry credentials: the content contains "
                "a credential-shaped value. Pending context is delivered as "
                "model-visible prompt text, not as a confidential channel"
            ),
            "code": "context_secret_shaped",
        },
        status=400,
    )


def _normalize_source(source: object) -> str:
    """Trim a caller source to its stored form: a stripped str.

    Non-str / None / blank collapse to ``""`` and the caller then applies its own
    default. Shared by validation and the /note default so a whitespace-only
    label cannot produce a blank drain frame, and so a padded label shares one
    per-source cap bucket with its trimmed form.
    """
    if not isinstance(source, str):
        return ""
    return source.strip()


def _validate_source(source: object) -> web.Response | None:
    """Shared source-label validation.

    Returns a 400 response on a bad value, else None. An empty, absent, or
    whitespace-only source is allowed here; the caller defaults it.
    """
    if source is not None and not isinstance(source, str):
        return web.json_response(
            {"error": "source must be a string", "code": "source_not_a_string"},
            status=400,
        )
    # Checked BEFORE the strip, which would otherwise silently drop a leading or
    # trailing tab/newline the documented contract says is a 400.
    if isinstance(source, str) and _SOURCE_CTRL_RE.search(source):
        return web.json_response(
            {
                "error": "source must not contain control characters or newlines",
                "code": "invalid_source",
            },
            status=400,
        )
    normalized = _normalize_source(source)
    if normalized == "":
        return None
    if len(normalized) > _MAX_SOURCE_LEN:
        return web.json_response(
            {"error": f"source exceeds {_MAX_SOURCE_LEN} char limit", "code": "source_too_long"},
            status=400,
        )
    if _SOURCE_CTRL_RE.search(normalized):
        return web.json_response(
            {
                "error": "source must not contain control characters or newlines",
                "code": "invalid_source",
            },
            status=400,
        )
    return None


def _validate_max_age(max_age: object) -> web.Response | None:
    """Shared maxAge validation. Returns a 400 response on a bad value, else None.

    ``drain_pending_context`` computes ``injected_at + max_age``, so a
    non-numeric value raises a TypeError on the user's NEXT send -- far from the
    request that introduced it. Rejecting it here turns that into a 400 at the
    boundary. Both callers validate UNCONDITIONALLY, not only when an entry is
    actually enqueued, so a visible-only note with a malformed maxAge is a 400
    rather than a silent ignore.

    ``bool`` is rejected because ``isinstance(True, int)`` is True but a boolean
    TTL is a caller bug. ``None`` is allowed, and both callers reach it from an
    omitted key as well as an explicit null -- they tell those apart themselves.
    """
    if max_age is None:
        return None
    if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
        return web.json_response(
            {"error": "maxAge must be a number (seconds) or omitted", "code": "invalid_max_age"},
            status=400,
        )
    # NaN and Infinity are floats that slip past the <= 0 check (NaN <= 0 is
    # False) and then make injected_at + max_age non-comparable at drain, so the
    # entry would never expire. Reject them at the boundary.
    # An arbitrary-precision int passes the isinstance check above, then
    # OverflowErrors inside isfinite's float conversion — same 400, not a 500.
    try:
        finite = math.isfinite(max_age)
    except OverflowError:
        finite = False
    if not finite:
        return web.json_response(
            {"error": "maxAge must be a finite number", "code": "non_finite_number"},
            status=400,
        )
    if max_age <= 0:
        return web.json_response(
            {"error": "maxAge must be positive", "code": "value_out_of_range"},
            status=400,
        )
    return None


def _source_cap_reached(slot: _ChatSlot, source: str) -> bool:
    """True if ``source`` already holds the max pending context entries.

    An empty source is uncapped (it shares no bucket). Shared by
    ``_enqueue_pending_context`` and the /note handler, which uses it to keep the
    visible transcript line independent of the context-queue cap.

    Expired entries do not count. They are dropped by ``drain_pending_context``
    but stay in the list until the next drain, so counting them would let ten
    already-dead notes lock a source out of fresh context indefinitely -- and the
    caller is told nothing, because the note still returns 200 with
    ``contextSkipped``. The same predicate decides both, so a count and a drain
    cannot disagree about which entries are live.

    Entries HELD for the deferred-note flush count as well. They are not in the
    queue yet, so a cap that reads the queue alone admits every one of them:
    ten same-source notes posted during one turn each see a clear cap, and the
    flush then promotes all ten at once, carrying that source past its
    per-source ceiling. Counting the held halves here holds the ceiling at
    admission so the flush never overfills the bucket.
    """
    if not source:
        return False
    now = time.time()
    held = [n["context"] for n in slot._deferred_notes if n.get("context") is not None]
    pending = sum(
        1
        for e in (*slot._pending_context, *held)
        if e.get("source") == source and not context_entry_expired(e, now)
    )
    return pending >= _MAX_CONTEXT_PER_SOURCE


def _enqueue_pending_context(
    slot: _ChatSlot,
    content: str,
    source: str,
    ephemeral: bool,
    max_age: int | float | None,
) -> web.Response | None:
    """Build, cap, and append a ``_pending_context`` entry.

    Returns a 4xx response on a bad request (429 could not be queued, 400
    invalid ``max_age`` or credential-shaped content), else None on success.
    The entry is consumed on the next user-initiated message via
    ``drain_pending_context``.

    A 400 leaves the queue untouched. The two 429s come from different places:
    ``_build_pending_context_entry`` returns the per-source ``capacity_reached``
    429 before any append, while the whole-queue ``context_not_queued`` 429 is
    decided by ``append_pending_context``, which reclaims expired entries on the
    way, so that refusal can have dropped dead entries -- nothing live is ever
    evicted.

    ``max_age`` is the resolved seconds-to-live, or None for no expiry. HTTP
    callers already validate it via ``_validate_max_age``; the same guard runs
    again here so a direct (non-HTTP) caller cannot slip a non-numeric TTL
    through to the drain.

    """
    entry, err = _build_pending_context_entry(slot, content, source, ephemeral, max_age)
    if err is not None:
        return err
    assert entry is not None
    # Asked once, of the authority: the append owns the ceiling and reports
    # whether the entry was seated, so a preflight here would ask twice.
    if not slot.append_pending_context(entry):
        return web.json_response(
            {
                "error": (
                    "pending context could not be queued for this session: the "
                    "queue is full, or the entry expired before it was queued"
                ),
                "code": "context_not_queued",
            },
            status=429,
        )
    return None


def _build_pending_context_entry(
    slot: _ChatSlot,
    content: str,
    source: str,
    ephemeral: bool,
    max_age: int | float | None,
) -> tuple[dict[str, object] | None, web.Response | None]:
    """Validate and build one context entry WITHOUT touching the queue.

    Returns ``(entry, None)`` or ``(None, 4xx response)``. Split from the append
    so /note can run every rejection synchronously -- the caller still gets its
    400 or 429 on the POST -- while HOLDING the entry until the running turn
    ends. Queueing it at the POST instead would hand it to the turn already in
    flight, since that turn drains the queue after its task is assigned.

    Every pending-context entry is built here, so this is where a
    credential-shaped payload is refused (``context_secret_shaped``): no
    producer can queue one by skipping a handler-level check.
    """
    bad_age = _validate_max_age(max_age)
    if bad_age is not None:
        return None, bad_age
    secret_shaped = _refuse_secret_shaped_context(content)
    if secret_shaped is not None:
        return None, secret_shaped
    if _source_cap_reached(slot, source):
        return None, web.json_response(
            {
                "error": f"source {source!r} has {_MAX_CONTEXT_PER_SOURCE} pending entries",
                "code": "capacity_reached",
            },
            status=429,
        )
    entry: dict[str, object] = {
        "content": content,
        "source": source,
        "ephemeral": ephemeral,
        "injectedAt": time.time(),
    }
    if max_age is not None:
        entry["maxAge"] = max_age
    return entry, None


async def api_chat_slot_context(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/context — inject silent background context.

    Adds a ContextEntry to the slot's ``_pending_context`` queue.
    The content is consumed on the next user-initiated message via
    ``ctx_builder.build_message()`` and prepended to the LLM prompt.

    No LLM turn is triggered, no WS event is broadcast, and no visible
    message is appended to the slot's chat history.

    The content reaches the model in-band, as prompt text, so it is not a
    confidential channel: a credential-shaped ``content`` is refused with a
    400 ``context_secret_shaped`` and the queue is left unchanged.

    Body::

        {
            "content": "...",
            "source": "watch-check",   // optional
            "ephemeral": true,         // optional, default true
            "maxAge": 300              // optional, seconds
        }
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        # Same body as the ownership denial below: two shapes would let an app
        # token tell "not mine" from "does not exist" and enumerate slot names.
        return slot_not_found()

    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "context_inject")
    if denied is not None:
        return denied

    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    content = body.get("content", "")
    bad = (
        _validate_content(content)
        or _validate_source(body.get("source"))
        or _validate_max_age(body.get("maxAge"))
    )
    if bad is not None:
        return bad

    # Same window as /note: authorized before the body read, so re-decide against
    # the slot as it is now, ahead of the only write.
    stale = _reauthorize_after_await(state, slot, name, request_app, "context_inject")
    if stale is not None:
        return stale

    # Normalize the source the same way /note does, so a whitespace-padded label
    # renders a clean drain frame and shares one cap bucket with its trimmed
    # form. /context keeps empty-source-uncapped and applies no default label: a
    # sourceless context injection is intentionally bucket-free.
    err = _enqueue_pending_context(
        slot,
        content,
        _normalize_source(body.get("source")),
        body.get("ephemeral", True),
        body.get("maxAge"),
    )
    if err is not None:
        return err

    # SEL audit logging
    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="context_inject",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    return web.json_response({"ok": True, "pending": len(slot._pending_context)})
