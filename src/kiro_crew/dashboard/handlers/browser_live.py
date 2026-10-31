"""The live browser card's routes: visibility, frame acks, takeover, open-in-browser, reopen.

Every route here is the CARD's leg of the Wire contract (RFC "Live browser in
chat", CONTRACT "Wire contract"), so every one of them is the dashboard OWNER's and
nobody else's:

* ``request["internal_auth"]`` (the verified ``X-Internal-Secret``) is REFUSED with
  403 ``internal_secret_required``. The secret is how the agent's tools reach the
  gateway; an agent must not be able to claim input for itself, ack its own frames
  or mark its own card visible.
* Then the owner check, :func:`require_owner_dashboard_request`: a non-owner
  dashboard session, an app token or an anonymous caller gets 403 ``owner_only``.
* Then the session: ``session_key`` must name a slot this dashboard owns
  (``state._slots``, after the ``dashboard:`` prefix is folded off), else 404
  ``unknown_session``. The card cannot steer another instance's stream.

The handlers are thin: bounded body in, one :class:`LiveHub` call, JSON out. Input
events are validated in ``browser_harness.takeover`` and never logged.

Routes (all ``POST``, JSON body, registered in ``routes/browser_live.py``):
``/api/browser/live/visibility`` ``{"session_key","visible"}``,
``/api/browser/live/ack`` ``{"session_key","seq","stream"?}``,
``/api/browser/input/claim`` and ``/release`` ``{"session_key"}``,
``/api/browser/input/event`` ``{"session_key","event":{...}}``,
``/api/browser/live/open-in-browser`` ``{"session_key"}``,
``/api/browser/live/reopen`` ``{"session_key","turn_id"}`` and
``/api/browser/live/close`` ``{"session_key"}`` (the finished card's "Open live"
and the reopened view's "Close").
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness.live_hub import (
    HANDBACK_CHOICE_MAX,
    HANDBACK_NOTE_MAX,
    REOPEN_LIVE_OFF,
    REOPEN_TAB_GONE,
    LiveHub,
    get_live_hub,
    handback_text,
)
from kiro_crew.browser_harness.takeover import InputEventError
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.state import _normalize_slot_key

logger = logging.getLogger(__name__)

#: A session key is an opaque slot key; bounded so it cannot carry free text.
_SESSION_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

#: Largest input-event body; a real one is under 200 bytes.
_MAX_BODY_BYTES = 4 * 1024


def _sel() -> Any:
    from kiro_crew.sel import sel

    return sel()


def _audit(request: web.Request, *, operation: str, outcome: str, error: str = "") -> None:
    """Best-effort SEL audit; a logging failure never changes the answer."""
    try:
        _sel().log_api_access(
            caller=str(request.get("user") or "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def _owner_gate(request: web.Request, operation: str) -> web.Response | None:
    """Refuse the internal secret, then anyone who is not the dashboard owner."""
    if request.get("internal_auth") is True:
        _audit(request, operation=operation, outcome="denied", error="internal secret refused")
        return _error("internal_secret_required", "forbidden", 403)
    return await require_owner_dashboard_request(request, operation)


def _owned_session_key(request: web.Request, body: dict[str, Any]) -> str | None:
    """The slot key the body names, when this dashboard owns such a slot."""
    raw = body.get("session_key")
    if not isinstance(raw, str) or not _SESSION_KEY_RE.fullmatch(raw):
        return None
    key = _normalize_slot_key(raw)
    if not key:
        return None
    slots = getattr(request.app["state"], "_slots", None) or {}
    return key if key in slots else None


Action = Callable[[LiveHub, str, dict[str, Any]], Awaitable[dict[str, Any] | web.Response]]


async def _run(request: web.Request, operation: str, action: Action) -> web.Response:
    """The shared shape: gate, body, session, one hub call."""
    refusal = await _owner_gate(request, operation)
    if refusal is not None:
        return refusal
    body, err = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES)
    if err is not None or body is None:
        return err if err is not None else _error("invalid_json", "invalid JSON", 400)
    key = _owned_session_key(request, body)
    if key is None:
        return _error("unknown_session", "session_key does not name an open session", 404)
    hub = get_live_hub()
    try:
        result = await action(hub, key, body)
    except InputEventError as exc:
        return _error(exc.code, str(exc), 400)
    except LookupError as exc:
        return _error(str(exc) or "not_available", "the live view is not accepting input", 409)
    if isinstance(result, web.Response):
        return result
    return web.json_response({"ok": True, "session_key": key, **result})


async def api_browser_live_visibility(request: web.Request) -> web.Response:
    """POST /api/browser/live/visibility: the card is on or off screen."""

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        visible = body.get("visible")
        if not isinstance(visible, bool):
            return _error("invalid_visible", "visible must be a boolean", 400)
        tab = body.get("client", "")
        if not isinstance(tab, str) or len(tab) > 64:
            return _error("invalid_client", "client must be a short string", 400)
        await hub.set_visible(key, visible, tab)
        return {"visible": visible}

    return await _run(request, "browser_live_visibility", act)


async def api_browser_live_ack(request: web.Request) -> web.Response:
    """POST /api/browser/live/ack: the card received frame ``seq``."""

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        seq = body.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            return _error("invalid_seq", "seq must be a non-negative integer", 400)
        stream = body.get("stream")
        if stream is not None and not (isinstance(stream, str) and len(stream) <= 16):
            return _error("invalid_stream", "stream must be a short string", 400)
        return {"acked": hub.ack(key, seq, stream)}

    return await _run(request, "browser_live_ack", act)


async def api_browser_input_claim(request: web.Request) -> web.Response:
    """POST /api/browser/input/claim: the person takes mouse and keyboard."""

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        if not await hub.claim(key):
            return _error("input_held", "input is held by another controller", 409)
        return {"in_control": True}

    return await _run(request, "browser_input_claim", act)


async def api_browser_input_release(request: web.Request) -> web.Response:
    """POST /api/browser/input/release: Esc or "Hand back", optionally with what the person did.

    Body ``{"session_key", "note"?, "choice"?}``. ``note`` is the person's own words
    (at most ``HANDBACK_NOTE_MAX`` characters), ``choice`` one hand-back option (at
    most ``HANDBACK_CHOICE_MAX``); either one too long, or not a string, is 400 and
    nothing is handed back. The answer's ``delivered`` says where they went:
    ``agent`` (its waiting op takes them), ``chat`` (no turn is left to: the card
    sends them as the person's next message), or ``""``.
    """

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        said: dict[str, str] = {}
        for field, limit in (("note", HANDBACK_NOTE_MAX), ("choice", HANDBACK_CHOICE_MAX)):
            raw = body.get(field)
            if raw is None:
                continue
            text = handback_text(raw, limit)
            if text is None:
                return _error(
                    f"invalid_{field}",
                    f"{field} must be a string of at most {limit} characters",
                    400,
                )
            said[field] = text
        delivered = await hub.release(key, note=said.get("note", ""), choice=said.get("choice", ""))
        return {"in_control": False, "delivered": delivered}

    return await _run(request, "browser_input_release", act)


async def api_browser_input_event(request: web.Request) -> web.Response:
    """POST /api/browser/input/event: one mouse, wheel or key event in frame pixels.

    The event is validated and mapped in ``browser_harness.takeover``; it is never
    logged here or there.
    """

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        event = body.get("event")
        if not isinstance(event, dict):
            raise InputEventError("invalid_input_event", "event must be an object")
        sent = await hub.input_event(key, event)
        out: dict[str, Any] = {"sent": sent}
        # After a release, say what took focus: a phone raises its keyboard
        # for a field (and the digit pad for a number box) and only then.
        if event.get("type") == "mouse" and event.get("action") in ("up", "click"):
            out["focus"] = await hub.focused_field(key)
        return out

    return await _run(request, "browser_input_event", act)


async def api_browser_live_open_in_browser(request: web.Request) -> web.Response:
    """POST /api/browser/live/open-in-browser: focus the real window."""

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        return {"focused": await hub.open_in_browser(key)}

    return await _run(request, "browser_live_open_in_browser", act)


#: A hub ``turn_id`` is a uuid4 hex; bounded the same way as the session key.
_TURN_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _current_relay() -> Any:
    """The running supervisor's relay, or ``None``. Never starts a browser.

    "Open live" looks at a tab that is already open; a browser that is not
    running has no such tab, so this reads ``supervisor.relay`` and nothing more.
    """
    try:
        import importlib

        from kiro_crew.browser_harness import ops as harness_ops
        from kiro_crew.config.paths import config_dir

        home = config_dir()
        if not harness_ops.browsing_available(home):
            return None
        sup = importlib.import_module("kiro_crew.browser_harness.supervisor")
        return getattr(sup.get_supervisor(home), "relay", None)
    except Exception:  # noqa: BLE001 - no relay means "the tab is gone", never a 500
        logger.debug("browser live: no current relay", exc_info=True)
        return None


async def api_browser_live_reopen(request: web.Request) -> web.Response:
    """POST /api/browser/live/reopen: the finished card's "Open live".

    Body ``{"session_key","turn_id"}``. ``turn_id`` must be the session's latest
    card (``stale_turn`` otherwise: an older card's tab is the newer card's now).
    Answers ``{"live": true}``, or 409 ``tab_gone`` (the tab is closed; the card
    says so and keeps Replay) or ``live_off`` (``browser.live.enabled`` is false).
    """

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        turn_id = body.get("turn_id")
        if not isinstance(turn_id, str) or not _TURN_ID_RE.fullmatch(turn_id):
            return _error("invalid_turn", "turn_id must be the card's turn id", 400)
        sess = hub.get(key)
        if sess is None or sess.turn_id != turn_id:
            return _error("stale_turn", "a newer browser card belongs to this chat", 409)
        refresh_live_config(hub)
        outcome = await hub.reopen(key, _current_relay())
        if outcome == REOPEN_TAB_GONE:
            return _error("tab_gone", "the tab this card showed is closed", 409)
        if outcome == REOPEN_LIVE_OFF:
            return _error("live_off", "the live view is turned off", 409)
        return {"live": True}

    return await _run(request, "browser_live_reopen", act)


async def api_browser_live_close(request: web.Request) -> web.Response:
    """POST /api/browser/live/close: fold a reopened view back to its done line."""

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        return {"closed": await hub.close_view(key)}

    return await _run(request, "browser_live_close", act)


def refresh_live_config(hub: LiveHub) -> None:
    """Give the hub the ``browser.live.*`` settings as the gateway holds them now.

    Read from the live config snapshot, so a Settings change applies to the next
    stream start (the stream lane's rule: never restart a cast under the person's
    hands). A gateway with no snapshot yet keeps the defaults.
    """
    try:
        from kiro_crew.config import live as live_config

        snap = live_config.snapshot()
    except Exception:  # noqa: BLE001 - defaults are the right answer when config is unreadable
        logger.debug("browser live: config snapshot unavailable", exc_info=True)
        return
    if snap is not None:
        hub.configure(snap)


def bind_live_hub(state: Any) -> LiveHub:
    """Wire the hub's two owner-only senders and its replay recorder. Called at startup."""
    from kiro_crew.browser_harness.recording import get_recorder

    hub = get_live_hub()
    publish = getattr(state, "deliver_ws_owners", None)
    send_frame = getattr(state, "deliver_ws_owners_bytes", None)
    hub.bind(publish, send_frame)
    hub.attach_recorder(get_recorder())
    refresh_live_config(hub)
    return hub


__all__ = [
    "api_browser_input_claim",
    "api_browser_input_event",
    "api_browser_input_release",
    "api_browser_live_ack",
    "api_browser_live_close",
    "api_browser_live_open_in_browser",
    "api_browser_live_reopen",
    "api_browser_live_visibility",
    "bind_live_hub",
    "refresh_live_config",
]
