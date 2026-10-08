"""Single-use capabilities binding mediated secret egress to an approved MCP call."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TOOL_SERVER = "kirocrew-secrets"
_TOOL_NAME = "call_api_with_secret"
_TTL_SECONDS = 30.0
#: 1 MiB ceiling on a mediated-request JSON body. One source of truth for the
#: whole mediated path: the in-sandbox ``kirocrew-secrets`` tool caps the payload
#: it sends with this, and the two host endpoints that receive mediated bodies
#: (the capability claim and the request itself) cap the body with it BEFORE JSON
#: decoding, so an attested caller cannot exhaust gateway memory by streaming a
#: near-60 MB body that is parsed in full before validation.
MAX_REQUEST_PAYLOAD_BYTES = 1_048_576
#: One bound covers EVERY retained grant, pending or active: a claimed grant
#: moves from _pending to _active but still counts against this single ceiling,
#: so claiming is not an escape from the cap. At the ceiling a new mint is
#: REFUSED (fail-closed, surfaced as a 403 the caller can retry) rather than
#: silently evicting a valid pending grant whose own approved call would then 403.
_MAX_OUTSTANDING = 256
#: Caller-supplied identifiers retained on a grant are length-bounded at the
#: retention point; an over-long id is refused rather than stored. The token and
#: request digest are self-generated fixed-length values, so they need no bound.
_MAX_ID_LEN = 512
#: Mints refused because the shared ceiling was full — counted, never silent.
_overflow_refusals = 0
#: Bounded observability for the overflow condition: a sustained burst at the
#: ceiling must not itself produce an unbounded stream of log lines, so the
#: warning is rate-limited to at most one per window and the suppressed count is
#: folded into the next emitted line. This matches the "a bound bounds every
#: field it retains" invariant — the signal is as bounded as the state it reports.
_OVERFLOW_LOG_WINDOW_SECONDS = 60.0
#: monotonic time the last overflow warning was emitted; -inf so the first fires.
_overflow_last_logged_at = float("-inf")
#: Overflow refusals coalesced since the last emitted warning, reported in the next.
_overflow_suppressed = 0


def outstanding_overflow_refusals() -> int:
    """Return how many mints were refused because the shared ceiling was full."""
    with _lock:
        return _overflow_refusals


@dataclass(frozen=True)
class _Grant:
    session_key: str
    tool_call_id: str
    request_digest: str
    token: str
    expires_at: float


_lock = threading.Lock()
_pending: dict[tuple[str, str], _Grant] = {}
_active: dict[str, _Grant] = {}


def request_digest(payload: Any) -> str:
    """Return stable bytes binding a capability to one exact request intent."""
    if not isinstance(payload, dict):
        raise TypeError("request intent must be an object")
    normalized = {
        "secret_name": payload.get("secret_name"),
        "method": payload.get("method"),
        "url": payload.get("url"),
        "headers": dict(payload.get("headers") or {}),
        "query": dict(payload.get("query") or {}),
        "json_body": payload.get("json_body"),
        "timeout_s": float(payload.get("timeout_s") or 20.0),
    }
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prune(now: float) -> None:
    for key, grant in list(_pending.items()):
        if grant.expires_at <= now:
            _pending.pop(key, None)
    for token, grant in list(_active.items()):
        if grant.expires_at <= now:
            _active.pop(token, None)


def _request_arguments(tool_args: Any) -> Any:
    """Return the inner request arguments to digest, unwrapping a Codex wrapper.

    kiro-cli and goose hand the mediated-secret tool's bare arguments
    (``{secret_name, method, url, ...}``) as ``raw_tool_params``. codex-acp
    reuses its shell builder, so an MCP call arrives wrapped as
    ``{server, tool, arguments}`` with the real arguments one level down (see
    ``acp/_dispatch.classify_tool_call``). The capability CLAIM always digests
    the inner request fields the tool actually sends, so approval must digest the
    SAME inner representation or the two digests never match and every approved
    Codex MCP call is refused 403. Unwrap only the Codex shape — a payload
    carrying ``arguments`` as a dict while lacking the request's own
    ``secret_name`` at top level — so the bare-arguments path is untouched.
    """
    if (
        isinstance(tool_args, dict)
        and "secret_name" not in tool_args
        and isinstance(tool_args.get("arguments"), dict)
    ):
        return tool_args["arguments"]
    return tool_args


def _note_overflow(now: float) -> None:
    """Count an overflow refusal and emit a BOUNDED, coalescing warning.

    Called with ``_lock`` held at the one site a mint is refused for capacity.
    Without a signal here the refused mint returns empty and the downstream claim
    reports a generic "no matching approved tool call", so an operator cannot tell
    capacity overflow (the ceiling is full of outstanding approvals) from a
    genuine no-match. The warning is RATE-LIMITED to one per
    ``_OVERFLOW_LOG_WINDOW_SECONDS`` and folds the refusals suppressed since the
    last emit into the next line, so a sustained burst at the ceiling cannot turn
    this diagnostic into an unbounded log stream — the signal is as bounded as the
    state it reports.
    """
    global _overflow_refusals, _overflow_last_logged_at, _overflow_suppressed
    _overflow_refusals += 1
    if now - _overflow_last_logged_at >= _OVERFLOW_LOG_WINDOW_SECONDS:
        coalesced = _overflow_suppressed
        _overflow_suppressed = 0
        _overflow_last_logged_at = now
        logger.warning(
            "mediated-secret capability mint refused: %d outstanding approvals at "
            "the ceiling of %d (capacity overflow, not a no-match); %d further "
            "overflow refusal(s) coalesced since the last warning. Total overflow "
            "refusals: %d.",
            len(_pending) + len(_active),
            _MAX_OUTSTANDING,
            coalesced,
            _overflow_refusals,
        )
    else:
        _overflow_suppressed += 1


def mint_for_approved_call(
    *,
    session_key: str,
    tool_call_id: str,
    mcp_server_name: str,
    tool_name: str,
    tool_args: dict[str, Any],
    identity_trusted: bool,
    args_trusted: bool,
) -> str:
    """Mint a pending capability only for the trusted mediated-secret tool identity."""
    if (
        not session_key
        or not tool_call_id
        or mcp_server_name != _TOOL_SERVER
        or tool_name != _TOOL_NAME
        or not identity_trusted
        or not args_trusted
    ):
        return ""
    if len(session_key) > _MAX_ID_LEN or len(tool_call_id) > _MAX_ID_LEN:
        return ""
    try:
        digest = request_digest(_request_arguments(tool_args))
    except (TypeError, ValueError, OverflowError):
        return ""
    now = time.monotonic()
    grant = _Grant(
        session_key=session_key,
        tool_call_id=tool_call_id,
        request_digest=digest,
        token=secrets.token_urlsafe(32),
        expires_at=now + _TTL_SECONDS,
    )
    with _lock:
        _prune(now)
        # One ceiling over pending AND active. Re-minting the same
        # (session_key, tool_call_id) replaces its own pending grant and does
        # not grow the count, so it is exempt; any other mint at the ceiling is
        # refused (counted) rather than evicting a valid grant.
        key = (session_key, tool_call_id)
        if key not in _pending and len(_pending) + len(_active) >= _MAX_OUTSTANDING:
            _note_overflow(now)
            return ""
        _pending[key] = grant
    return grant.token


def revoke_pending(session_key: str, tool_call_id: str, token: str) -> None:
    """Remove a mint whose ACP approval response failed to reach the harness."""
    with _lock:
        grant = _pending.get((session_key, tool_call_id))
        if grant is not None and secrets.compare_digest(grant.token, token):
            _pending.pop((session_key, tool_call_id), None)


def invalidate_call(session_key: str, tool_call_id: str) -> None:
    """Drop EVERY grant for one call id when its approval is canceled or denied.

    A grant must not outlive the approval decision for its call. ``mint`` fires on
    APPROVAL and the mint is keyed by ``(session_key, tool_call_id)``, so a call
    that was approved, then canceled or denied before the token was claimed, would
    otherwise leave its pending grant behind — and a later call reusing the SAME id
    with identical arguments could then claim that stale approval and send the
    secret-bearing request despite the denial. The deny/cancel path does not hold
    the mint token, so this invalidation is token-INDEPENDENT: it removes the
    pending grant for the id outright, and also any grant already moved to
    ``_active`` under that id (defense in depth, should a claim have raced the
    decision), so the current decision for the id is the only one that can
    authorize egress.
    """
    with _lock:
        _pending.pop((session_key, tool_call_id), None)
        for tok, grant in list(_active.items()):
            if grant.session_key == session_key and grant.tool_call_id == tool_call_id:
                _active.pop(tok, None)


def claim(session_key: str, tool_call_id: str, tool_args: dict[str, Any]) -> str:
    """Move one approved call's pending token into the endpoint-consumable set."""
    try:
        digest = request_digest(tool_args)
    except (TypeError, ValueError, OverflowError):
        return ""
    now = time.monotonic()
    with _lock:
        _prune(now)
        grant = _pending.pop((session_key, tool_call_id), None)
        if grant is None or not secrets.compare_digest(grant.request_digest, digest):
            return ""
        _active[grant.token] = grant
        return grant.token


def consume(session_key: str, token: str, tool_args: dict[str, Any]) -> bool:
    """Verify and irreversibly consume a capability bound to this exact request."""
    if not token:
        return False
    try:
        digest = request_digest(tool_args)
    except (TypeError, ValueError, OverflowError):
        return False
    now = time.monotonic()
    with _lock:
        _prune(now)
        grant = _active.pop(token, None)
        return bool(
            grant is not None
            and secrets.compare_digest(grant.session_key, session_key)
            and secrets.compare_digest(grant.request_digest, digest)
        )
