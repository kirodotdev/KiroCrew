"""The XR page's live panels: the socket leg and the two input routes.

A panel shows one of the dashboard's own pages, live, in the owner's XR page
(``browser_harness/panel_hub.py``). Two legs reach the hub, and both are the
dashboard OWNER's and nobody else's:

* **The socket leg** (``xr_panel`` text frames on ``/api/ws``): ``open``,
  ``close``, ``ack``, ``visible``, ``detail``, ``size``, ``watch`` (the page
  he looks at), ``way`` (which way holds: ``browser.xr_pages_guarded``), and
  ``hello`` and ``input`` (a wheel or a pointer move of his hand, so that a
  scroll does not wait behind a request: :func:`_input_frame`). The socket was
  admitted by the sign-in gate and the origin check at its upgrade; a panel is
  taken only from a socket whose upgrade request was the owner's own cookie
  sign-in (:func:`panel_socket`): never the internal secret, an app token or a
  non-owner session. Pictures go back on that same socket and no other.
* **The input routes** (``POST /api/browser/panel/input`` and ``/ask``): like
  the live card's routes (``browser_live.py``) they refuse the verified internal
  secret with 403 ``internal_secret_required``, then anyone who is not the
  owner with 403 ``owner_only``, then ``capabilities.browse`` under a deny. The
  hub then takes the event only for a panel the caller's own sign-in opened,
  or that same sign-in renewed (403 ``not_your_panel`` for any other). The CSRF origin check runs before any of this, in
  the dashboard's middleware.

Neither path is on the internal-secret list, so an agent's secret does not even
reach the handlers through the gate. Input events are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness.panel_hub import (
    FAILED,
    PANEL_MESSAGE_TYPE,
    PanelRefused,
    PanelSocket,
    get_panel_hub,
)
from kiro_crew.browser_harness.panel_page import loopback_origin
from kiro_crew.browser_harness.takeover import MAX_DELTA, InputEventError
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.browser_live import _error, _owner_gate
from kiro_crew.dashboard.handlers.browser_policy import browse_refusal
from kiro_crew.dashboard.refresh_tokens import ACCESS_COOKIE_PREFIX

logger = logging.getLogger(__name__)

#: Largest input body: a typed text of the hub's cap plus its envelope.
_MAX_BODY_BYTES = 16 * 1024
#: How long one send to the panel's socket may take before the picture is dropped.
_SEND_TIMEOUT_S = 10.0

#: The most input events of one socket that wait for the page (the one being sent is not counted).
_INPUT_LINE = 4
#: The largest number a socket's input event may carry: the XR page counts its events with it.
_INPUT_N_MAX = 2**31

_SOCKET_KEY = "_xr_panel_socket"
_CHAIN_KEY = "_xr_panel_chain"
_INPUT_KEY = "_xr_panel_input"


def panel_socket(ws: web.WebSocketResponse, request: web.Request) -> PanelSocket | None:
    """The hub's view of *ws*, or ``None`` when its sign-in may not hold a panel.

    Built from the socket's own upgrade request, which the sign-in gate
    verified: the owner's cookie session and nothing else. The tab's address is
    this gateway's own listener, on the loopback address this socket was
    accepted on (``panel_page.loopback_origin``): a socket accepted on any other
    address, or on a unix socket, holds no panel.
    """
    existing = ws.get(_SOCKET_KEY)
    if isinstance(existing, PanelSocket):
        return existing
    if request.get("internal_auth") is True:
        return None
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        return None
    token = request.get("auth_token")
    origin = _listener_origin(request)
    if not isinstance(token, str) or not token or origin is None:
        return None

    async def send_json(data: dict[str, Any]) -> None:
        await asyncio.wait_for(ws.send_str(json.dumps(data)), _SEND_TIMEOUT_S)

    async def send_bytes(blob: bytes) -> None:
        await asyncio.wait_for(ws.send_bytes(blob), _SEND_TIMEOUT_S)

    socket = PanelSocket(
        key=id(ws),
        origin=origin,
        cookie_name=f"{ACCESS_COOKIE_PREFIX}{origin.rsplit(':', 1)[1]}",
        send_json=send_json,
        send_bytes=send_bytes,
        token=token,
    )
    ws[_SOCKET_KEY] = socket
    return socket


def _listener_origin(request: web.Request) -> str | None:
    """This gateway's own origin on the loopback address the request was accepted on."""
    transport = request.transport
    name = transport.get_extra_info("sockname") if transport is not None else None
    if isinstance(name, tuple) and len(name) >= 2:
        return loopback_origin(name[0], name[1])
    return None


def handle_panel_frame(ws: web.WebSocketResponse, request: web.Request, data: Any) -> None:
    """One ``xr_panel`` frame of the owner's socket. Never raises, never blocks the socket.

    An ack is answered at once; every other op of a socket runs in the order it
    arrived, behind the one before it, so a ``size`` sent right after an
    ``open`` finds its panel.
    """
    if not isinstance(data, dict):
        return
    op, panel_id = data.get("op"), data.get("id")
    hub = get_panel_hub()
    socket = panel_socket(ws, request)
    if socket is None:
        if op == "open" and isinstance(panel_id, str) and len(panel_id) <= 64:
            # Not the owner's own sign-in: no browser, no picture, and the panel says so.
            refusal = {"op": "status", "id": panel_id, "status": FAILED, "why": "owner_only"}
            _spawn(ws, ws.send_str(json.dumps({"type": PANEL_MESSAGE_TYPE, "data": refusal})))
        return
    if op == "ack":
        hub.ack(socket.key, panel_id, data.get("seq"), data.get("stream"), data.get("kind"))
        return
    if op == "hello":
        # An XR page asks what this gateway takes on the socket. One that does not answer takes no input here.
        _send(socket, {"op": "hello", "input": True})
        return
    if op == "input":
        _input_frame(ws, socket, data)
        return
    key = socket.key
    if op == "open":
        step: Any = hub.open(
            socket,
            panel_id,
            path=data.get("path"),
            width=data.get("w"),
            height=data.get("h"),
            texels=data.get("detail"),
            prefs=data.get("prefs"),
        )
    elif op == "way":
        step = hub.way(socket)
    elif op == "close":
        step = hub.close(key, panel_id)
    elif op == "visible":
        step = hub.set_visible(key, panel_id, data.get("visible"))
    elif op == "detail":
        step = hub.set_detail(key, panel_id, data.get("detail"))
    elif op == "watch":
        step = hub.set_watched(key, panel_id, data.get("on"))
    elif op == "size":
        step = hub.resize(key, panel_id, data.get("w"), data.get("h"))
    else:
        return
    _spawn(ws, step)


def _send(socket: PanelSocket, data: dict[str, Any]) -> None:
    """One ``xr_panel`` message to the socket, in a task of its own. A socket that does not take it loses it."""

    async def run() -> None:
        try:
            await socket.send_json({"type": PANEL_MESSAGE_TYPE, "data": data})
        except Exception:  # noqa: BLE001 - a closed or slow socket never raises into its handler
            logger.debug("xr panel: a message was not sent", exc_info=True)

    asyncio.ensure_future(run())


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _input_frame(ws: web.WebSocketResponse, socket: PanelSocket, data: dict[str, Any]) -> None:
    """A wheel or a pointer move of the owner's hand, on the owner's socket.

    ``{"op": "input", "handle": <the panel's handle>, "n": <the page's count>,
    "event": {"type": "wheel" | "mouse", ...}}``. It goes where the input route
    sends it, by the same door: ``hub.input`` with the sign-in this socket was
    opened with, so only a panel that sign-in opened takes it, and
    ``capabilities.browse`` is asked first. The answer comes back on the socket
    as ``{"op": "input", "handle", "n", ...}`` with what the route would answer,
    or ``error`` with the route's code.

    Only a wheel and a move: a press, a key and a text keep the route. The
    events of a socket go to the page one at a time, in the order they came, in
    a line of their own: not behind an ``open`` or a ``size``. At most
    ``_INPUT_LINE`` wait. A wheel that comes while another wheel for the same
    panel waits is added to it, and a move takes the place of a waiting move
    (the older one is answered ``merged``); with the line full of other events
    the new one is answered ``dropped``. A waiting wheel adds up only to the
    hub's own bound for one wheel (``MAX_DELTA``): what is over waits as the
    next wheel, so no sum is one that the hub refuses. A frame larger than the
    route's body (``_MAX_BODY_BYTES``) is answered ``too_large`` and goes
    nowhere. Input events are never logged.
    """
    handle, n, event = data.get("handle"), data.get("n"), data.get("event")
    handle_ok = isinstance(handle, str) and 0 < len(handle) <= 64
    n_ok = isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= _INPUT_N_MAX
    if len(json.dumps(data).encode("utf-8")) > _MAX_BODY_BYTES:
        # The route reads at most this much; the socket's own frames may be far larger.
        refusal: dict[str, Any] = {"op": "input", "error": "too_large"}
        if handle_ok:
            refusal["handle"] = handle
        if n_ok:
            refusal["n"] = n
        _send(socket, refusal)
        return
    if not handle_ok or not n_ok:
        return
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    if kind != "wheel" and not (kind == "mouse" and event.get("action") == "move"):
        _send(socket, {"op": "input", "handle": handle, "n": n, "error": "not_on_the_socket"})
        return
    line: dict[str, Any] = ws.setdefault(_INPUT_KEY, {"waiting": [], "task": None})
    waiting: list[list[Any]] = line["waiting"]
    for item in reversed(waiting):
        if item[0] == handle and item[2].get("type") == kind:
            # The newest of its kind for this panel that still waits: the wheels add up, the newer move stands.
            merged = dict(event)
            over: dict[str, Any] = {}
            if kind == "wheel":
                for axis in ("dx", "dy"):
                    old, new = item[2].get(axis, 0), event.get(axis, 0)
                    if _number(old) and _number(new):
                        merged[axis] = max(-MAX_DELTA, min(MAX_DELTA, old + new))
                        over[axis] = old + new - merged[axis]
            if any(over.values()):
                # The waiting wheel is full: it keeps its place and its count, and what is over is the new wheel.
                item[2] = {**item[2], **{axis: merged[axis] for axis in over}}
                event = {**event, **over}
                break
            _send(socket, {"op": "input", "handle": handle, "n": item[1], "merged": True})
            item[1], item[2] = n, merged
            return
    if len(waiting) >= _INPUT_LINE:
        _send(socket, {"op": "input", "handle": handle, "n": n, "dropped": True})
        return
    waiting.append([handle, n, dict(event)])
    task = line["task"]
    if task is None or task.done():
        line["task"] = asyncio.ensure_future(_run_inputs(socket, waiting))


async def _run_inputs(socket: PanelSocket, waiting: list[list[Any]]) -> None:
    hub = get_panel_hub()
    while waiting:
        handle, n, event = waiting.pop(0)
        answer: dict[str, Any]
        try:
            refusal = await browse_refusal(audit=False)
            if refusal is not None:
                answer = {"error": "browse_denied"}
            else:
                answer = await hub.input(socket.token, handle, event)
        except InputEventError as exc:
            answer = {"error": exc.code}
        except PanelRefused as exc:
            answer = {"error": exc.code}
        except Exception:  # noqa: BLE001 - one failed event never ends the socket's line
            logger.debug("xr panel: an input on the socket failed", exc_info=True)
            answer = {"error": "failed"}
        try:
            await socket.send_json(
                {
                    "type": PANEL_MESSAGE_TYPE,
                    "data": {**answer, "op": "input", "handle": handle, "n": n},
                }
            )
        except Exception:  # noqa: BLE001 - a closed or slow socket never ends the line
            logger.debug("xr panel: an input's answer was not sent", exc_info=True)


def _spawn(ws: web.WebSocketResponse, step: Any) -> None:
    previous = ws.get(_CHAIN_KEY)

    async def run() -> None:
        if previous is not None:
            await asyncio.wait({previous})
        try:
            await step
        except Exception:  # noqa: BLE001 - one failed op never ends the socket's chain
            logger.debug("xr panel op failed", exc_info=True)

    ws[_CHAIN_KEY] = asyncio.ensure_future(run())


async def close_panel_socket(ws: web.WebSocketResponse) -> None:
    """The socket closed: its panels and the sign-in handed to them go with it."""
    socket = ws.get(_SOCKET_KEY)
    if not isinstance(socket, PanelSocket):
        return
    line = ws.get(_INPUT_KEY)
    if isinstance(line, dict):
        # The events that still wait go nowhere: the socket's panels close below.
        line["waiting"].clear()
    try:
        await get_panel_hub().close_socket(socket.key)
    except Exception:  # noqa: BLE001 - a socket's teardown never raises into its handler
        logger.debug("xr panel: socket teardown failed", exc_info=True)


async def _run(request: web.Request, operation: str, question: bool) -> web.Response:
    refusal = await _owner_gate(request, operation)
    if refusal is not None:
        return refusal
    refusal = await browse_refusal(audit=False)
    if refusal is not None:
        return refusal
    token = request.get("auth_token")
    if not isinstance(token, str) or not token:
        return _error("owner_only", "forbidden", 403)
    body, err = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES)
    if err is not None or body is None:
        return err if err is not None else _error("invalid_json", "invalid JSON", 400)
    hub = get_panel_hub()
    try:
        if question:
            answer = await hub.ask(
                token, body.get("panel"), body.get("question"), body.get("x", 0), body.get("y", 0)
            )
            return web.json_response({"ok": True, "answer": answer})
        return web.json_response(
            {"ok": True, **await hub.input(token, body.get("panel"), body.get("event"))}
        )
    except InputEventError as exc:
        return _error(exc.code, str(exc), 400)
    except PanelRefused as exc:
        return _error(exc.code, "the panel does not take this", exc.status)


async def api_browser_panel_input(request: web.Request) -> web.Response:
    """POST /api/browser/panel/input: one pointer, wheel, key or text event, in CSS pixels."""
    return await _run(request, "browser_panel_input", question=False)


async def api_browser_panel_ask(request: web.Request) -> web.Response:
    """POST /api/browser/panel/ask: what is under a point, where the focus is, the page's state."""
    return await _run(request, "browser_panel_ask", question=True)


async def close_panel_hub(_app: web.Application) -> None:
    """Gateway shutdown: every panel tab and the panel browser stop."""
    await get_panel_hub().close_all()


__all__ = [
    "api_browser_panel_ask",
    "api_browser_panel_input",
    "close_panel_hub",
    "close_panel_socket",
    "handle_panel_frame",
    "panel_socket",
]
