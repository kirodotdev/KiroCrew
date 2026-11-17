"""Live capture of a Bot's own Dashboard page from the person's open tab.

``dashboard_snapshot`` draws the page offline, over the Bot's real data. What the
person's tab shows RIGHT NOW (a mark a later push added, what a page script drew after
load, which option is selected) only that tab knows. What a person has typed into a
text field and not submitted is NOT part of it: the tab's script never reads it. This module is
the gateway's half of asking it: it holds one in-flight request per Bot, sends it to
the owner's tabs as a ``dashboard_capture`` frame, and takes the first visible tab's
answer on the owner-only ``POST /api/members/{slug}/dashboard/capture`` route.

The limits, each enforced here or in the route, not in the tab:

* OWN PAGE ONLY. The request names the slug the caller gate resolved; a reply must
  name the same slug and the request id this gateway minted for it.
* REQUEST ONLY. A reply is accepted only for a request that is pending right now; a
  tab cannot put text in front of a Bot that did not ask (``capture_expired``).
* READ ONLY. Nothing here writes the page, its record or its versions, and nothing
  is kept: a request lives in memory until it is answered or times out.
* SIZE-CAPPED. :data:`TEXT_MAX_CHARS` of text and :data:`FIELDS_MAX` form values,
  each value at most :data:`FIELD_VALUE_MAX` characters; a larger reply is refused
  whole, never cut here (the tab cuts and says so with ``truncated``).
* ONE ANSWER. The first valid reply wins; a second tab's reply is refused
  (``capture_answered``), so answers from several tabs are never merged.

Redaction is the reply path's: the MCP server passes the whole answer through the
same ``redact_via_context`` every other ``kirocrew-panel`` reply crosses.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any, Final

#: The frame asking the owner's tabs for one capture. Owner sockets only.
WS_DASHBOARD_CAPTURE: Final[str] = "dashboard_capture"

#: How long the agent waits for a tab. Covers the socket send, the frame's own
#: answer (the tab waits at most 1.5 s for it) and the reply's POST.
CAPTURE_WAIT_SECONDS: Final[float] = 5.0

#: The page text a capture may carry. About 4,000 tokens: a page several screens long
#: fits, and one answer cannot crowd the Bot's context the way a whole DOM could.
TEXT_MAX_CHARS: Final[int] = 16_000
#: Form controls a capture may carry, and the size of each label and value.
FIELDS_MAX: Final[int] = 64
FIELD_LABEL_MAX: Final[int] = 120
FIELD_VALUE_MAX: Final[int] = 500
#: The value a text field is listed with: its label is kept, what was typed is not read.
UNREAD_VALUE: Final[str] = "(not read)"
#: Requests in flight across the gateway. One per Bot is allowed at a time.
MAX_PENDING: Final[int] = 16

STATUS_CAPTURED: Final[str] = "captured"
STATUS_UNAVAILABLE: Final[str] = "unavailable"
#: ``status`` of the agent's answer when no tab answered.
STATUS_NOT_CAPTURED: Final[str] = "not_captured"

#: Why a tab that shows the page could not read it.
REASON_STATIC_PAGE: Final[str] = "static_page"
REASON_NOT_LOADED: Final[str] = "not_loaded"
TAB_REASONS: Final[frozenset[str]] = frozenset({REASON_STATIC_PAGE, REASON_NOT_LOADED})
#: Why the gateway has no capture to give.
REASON_NO_TAB: Final[str] = "no_tab"
REASON_BUSY: Final[str] = "busy"

_CAPTURED_KEYS: Final[frozenset[str]] = frozenset(
    {"request_id", "slug", "status", "text", "fields", "truncated"}
)
_UNAVAILABLE_KEYS: Final[frozenset[str]] = frozenset({"request_id", "slug", "status", "reason"})
_REQUEST_ID_MAX: Final[int] = 64


class CaptureError(Exception):
    """A refused reply: ``status`` for the route, ``code`` and ``message`` for the body."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class _Pending:
    request_id: str
    slug: str
    future: asyncio.Future[dict[str, Any]]


@dataclass
class CaptureHub:
    """Every capture in flight, in memory. Nothing of an answer is kept."""

    _pending: dict[str, _Pending] = field(default_factory=dict)

    def begin(self, slug: str) -> _Pending:
        """Open one request for *slug*, or :class:`CaptureError` when one is already open."""
        if any(p.slug == slug for p in self._pending.values()):
            raise CaptureError(429, REASON_BUSY, "a capture of this page is already running")
        if len(self._pending) >= MAX_PENDING:
            raise CaptureError(429, REASON_BUSY, "too many captures are running")
        p = _Pending(
            request_id=f"c_{secrets.token_urlsafe(12)}",
            slug=slug,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[p.request_id] = p
        return p

    def end(self, request_id: str) -> None:
        """Forget a request: answered, timed out or abandoned."""
        p = self._pending.pop(request_id, None)
        if p is not None and not p.future.done():
            p.future.cancel()

    def pending_count(self) -> int:
        return len(self._pending)

    def deliver(self, slug: str, body: dict[str, Any]) -> None:
        """Validate one tab's reply for *slug* (the route's own) and hand it over.

        Raises :class:`CaptureError` for a malformed, oversized, foreign, late or
        second reply; nothing of a refused reply is kept.
        """
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > _REQUEST_ID_MAX:
            raise CaptureError(400, "invalid_capture", "request_id is required")
        p = self._pending.get(request_id)
        if p is None:
            raise CaptureError(409, "capture_expired", "no capture of this page is waiting")
        if p.slug != slug or body.get("slug") != slug:
            raise CaptureError(409, "capture_wrong_page", "this capture was asked of another page")
        if p.future.done():
            raise CaptureError(409, "capture_answered", "another tab already answered")
        p.future.set_result(_validated(body))


def _validated(body: dict[str, Any]) -> dict[str, Any]:
    """The answer a valid reply carries, or :class:`CaptureError`."""
    status = body.get("status")
    if status == STATUS_UNAVAILABLE:
        if set(body) != _UNAVAILABLE_KEYS or body.get("reason") not in TAB_REASONS:
            raise CaptureError(400, "invalid_capture", "unexpected capture fields")
        return {"status": STATUS_UNAVAILABLE, "reason": body["reason"]}
    if status != STATUS_CAPTURED or set(body) != _CAPTURED_KEYS:
        raise CaptureError(400, "invalid_capture", "unexpected capture fields")
    text, fields, truncated = body.get("text"), body.get("fields"), body.get("truncated")
    if not isinstance(text, str) or not isinstance(truncated, bool):
        raise CaptureError(400, "invalid_capture", "text must be text and truncated a boolean")
    if len(text) > TEXT_MAX_CHARS:
        raise CaptureError(413, "capture_too_large", f"text is over {TEXT_MAX_CHARS} characters")
    if not isinstance(fields, list):
        raise CaptureError(400, "invalid_capture", "fields must be a list")
    if len(fields) > FIELDS_MAX:
        raise CaptureError(413, "capture_too_large", f"more than {FIELDS_MAX} form values")
    out: list[dict[str, str]] = []
    for item in fields:
        if not isinstance(item, dict) or set(item) != {"label", "value"}:
            raise CaptureError(400, "invalid_capture", "a form value carries unexpected fields")
        label, value = item.get("label"), item.get("value")
        if not isinstance(label, str) or not isinstance(value, str):
            raise CaptureError(400, "invalid_capture", "a form label and value must be text")
        if len(label) > FIELD_LABEL_MAX or len(value) > FIELD_VALUE_MAX:
            raise CaptureError(413, "capture_too_large", "a form label or value is too long")
        out.append({"label": label, "value": value})
    return {"status": STATUS_CAPTURED, "text": text, "fields": out, "truncated": truncated}


def capture_hub_for(state: Any) -> CaptureHub:
    """The one hub attached to this gateway's dashboard state."""
    hub = getattr(state, "_dashboard_capture_hub", None)
    if not isinstance(hub, CaptureHub):
        hub = CaptureHub()
        state._dashboard_capture_hub = hub
    return hub
