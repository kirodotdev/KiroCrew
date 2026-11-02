"""The helper worker: Harness's own helpers, one at a time, over JSON lines.

Run BY THE VENV'S INTERPRETER (``<prefix>/bin/python -I <this file>``), never imported
by the gateway: the gateway's Python has no ``browser_harness`` package, and this file
has no ``kiro_crew`` import for the same reason in reverse. ``-I`` keeps the script's
own directory off ``sys.path``, so ``import browser_harness`` below resolves to the
pinned package in the venv and never to the ``kiro_crew.browser_harness`` directory
this file happens to live in.

Protocol, one JSON object per line each way:

* request ``{"id": n, "helper": "<name>", "kwargs": {...}}`` -- run one allowlisted
  helper with those keyword arguments;
* request ``{"id": n, "ping": true}`` -- liveness;
* reply ``{"id": n, "ok": true, "result": <json>}`` or
  ``{"id": n, "ok": false, "error": "<text>"}``.

The allowlist is the whole surface: ``run.py``, ``browser_js``, ``browser_cdp``,
``http_get`` and the agent-helper loader are not reachable through it, and ``js`` is
accepted only so the gateway can run its own accessibility walker. The supervisor
checks the same list before it sends (``supervisor.HELPERS``); this copy is what holds
if anything else ever reaches the pipe.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

#: Helper name -> (helpers.py function, accepted keyword arguments). A name starting
#: with ``_local_`` is a function in THIS file built from Harness's ``cdp`` primitive:
#: the mouse moves Harness has no helper for.
_ALLOWLIST: dict[str, tuple[str, tuple[str, ...]]] = {
    "goto": ("goto_url", ("url",)),
    "click": ("click_at_xy", ("x", "y", "button", "clicks")),
    "mouse_move": ("_local_mouse_move", ("x", "y")),
    "drag": ("_local_drag", ("x", "y", "to_x", "to_y")),
    "type_text": ("type_text", ("text",)),
    "press_key": ("press_key", ("key", "modifiers")),
    "scroll": ("scroll", ("x", "y", "dy", "dx")),
    "screenshot": ("capture_screenshot", ("path", "full", "max_dim")),
    "page_info": ("page_info", ()),
    "wait_for_load": ("wait_for_load", ("timeout",)),
    "new_tab": ("new_tab", ("url",)),
    "switch_tab": ("switch_tab", ("target", "activate")),
    "list_tabs": ("list_tabs", ("include_chrome",)),
    "close_tab": ("close_tab", ("target",)),
    "current_tab": ("current_tab", ()),
    "js": ("js", ("expression", "target_id")),
    "page_clock": ("_local_page_clock", ("state",)),
}

#: Longest error text sent back; a Python traceback can be pages.
_ERROR_CAP = 4000

#: Longest reply line written to the gateway. The gateway reads with a stream limit
#: of ``supervisor.WORKER_LINE_LIMIT`` (8 MiB) and treats an over-limit line as a
#: protocol failure; a result that would serialise past this cap is replaced by an
#: error reply of a few hundred bytes instead, so the pipe never desynchronises.
#: The walker caps what it returns (nodes, console text) far below this.
REPLY_CAP_BYTES = 4 * 1024 * 1024


def _local_mouse_move(helpers: Any, x: float, y: float) -> dict[str, float]:
    """Move the pointer to a viewport point (hover) on the daemon's current tab."""
    helpers.cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    return {"x": x, "y": y}


def _local_drag(helpers: Any, x: float, y: float, to_x: float, to_y: float) -> dict[str, Any]:
    """Press at one point, move in a few steps, release at another (drag and drop)."""
    helpers.cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    helpers.cdp(
        "Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1
    )
    steps = 8
    for i in range(1, steps + 1):
        px = x + (to_x - x) * i / steps
        py = y + (to_y - y) * i / steps
        helpers.cdp("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py, button="left")
    helpers.cdp(
        "Input.dispatchMouseEvent",
        type="mouseReleased",
        x=to_x,
        y=to_y,
        button="left",
        clickCount=1,
    )
    return {"from": [x, y], "to": [to_x, to_y]}


#: ``page_clock`` states: hold the page's timers and animation frames, let them run
#: again, or let them run and stop holding the page at all.
_CLOCK_STATES = ("hold", "run", "release")
#: The page callbacks a hold stops at: each one pauses the page's script at its
#: first statement, so nothing the page schedules runs until ``run``.
_HELD_CALLBACKS = (
    "requestAnimationFrame.callback",
    "setTimeout.callback",
    "setInterval.callback",
)


def _local_page_clock(helpers: Any, state: str) -> dict[str, str]:
    """Hold or run the tab's timers and animation frames: ``reflex`` ``virtual_time``.

    A break on the page's next timer or animation-frame callback, not CDP virtual
    time: Chromium's virtual-time policy cannot be handed back (``advance`` then
    fast-forwards an idle page and ``pause`` leaves it, navigation included,
    frozen). And not a bare ``Debugger.pause``: on an idle page that lands in the
    next script to run, which is the gateway's own walker call, and the call never
    returns. While held, the walker still reads the page (its call is not a
    callback); input is not delivered, so the loop runs before it acts.
    """
    if state not in _CLOCK_STATES:
        raise ValueError(f"page_clock state must be one of {_CLOCK_STATES}")
    if state == "hold":
        helpers.cdp("Debugger.enable")
        for name in _HELD_CALLBACKS:
            helpers.cdp("EventBreakpoints.setInstrumentationBreakpoint", eventName=name)
        return {"state": state}
    for name in _HELD_CALLBACKS:
        helpers.cdp("EventBreakpoints.removeInstrumentationBreakpoint", eventName=name)
    try:
        helpers.cdp("Debugger.resume")
    except Exception:  # noqa: BLE001 - not paused (the page had no callback due): nothing to resume
        pass
    if state == "release":
        helpers.cdp("Debugger.disable")
    return {"state": state}


_LOCAL: dict[str, Callable[..., Any]] = {
    "_local_mouse_move": _local_mouse_move,
    "_local_drag": _local_drag,
    "_local_page_clock": _local_page_clock,
}


def _resolve(helpers: Any, name: str) -> tuple[Callable[..., Any], tuple[str, ...]]:
    entry = _ALLOWLIST.get(name)
    if entry is None:
        raise ValueError(f"helper not allowed: {name}")
    attr, accepted = entry
    local = _LOCAL.get(attr)
    if local is not None:
        return (lambda **kw: local(helpers, **kw)), accepted
    fn = getattr(helpers, attr, None)
    if not callable(fn):
        raise ValueError(f"helper missing in this Harness release: {attr}")
    return fn, accepted


def _handle(helpers: Any, request: dict[str, Any]) -> dict[str, Any]:
    rid = request.get("id")
    if request.get("ping"):
        return {"id": rid, "ok": True, "result": {"pong": True}}
    try:
        name = str(request.get("helper", ""))
        fn, accepted = _resolve(helpers, name)
        kwargs = request.get("kwargs") or {}
        if not isinstance(kwargs, dict):
            raise ValueError("kwargs must be an object")
        unknown = sorted(set(kwargs) - set(accepted))
        if unknown:
            raise ValueError(f"{name}: unexpected arguments {unknown}")
        result = fn(**kwargs)
        return _bounded({"id": rid, "ok": True, "result": _jsonable(result)})
    except Exception as exc:  # noqa: BLE001 - every failure is reported to the gateway
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return {"id": rid, "ok": False, "error": detail[:_ERROR_CAP]}


def _bounded(reply: dict[str, Any]) -> dict[str, Any]:
    """*reply* if it serialises under :data:`REPLY_CAP_BYTES`, else an error reply."""
    size = len(json.dumps(reply, default=str).encode("utf-8"))
    if size <= REPLY_CAP_BYTES:
        return reply
    return {
        "id": reply.get("id"),
        "ok": False,
        "error": f"helper result too large ({size} bytes, limit {REPLY_CAP_BYTES})",
    }


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def main() -> int:
    try:
        from browser_harness import helpers  # the pinned package in the venv
    except Exception as exc:  # noqa: BLE001 - reported on the pipe, then exit
        sys.stdout.write(
            json.dumps({"id": None, "ok": False, "error": f"import failed: {exc}"}) + "\n"
        )
        sys.stdout.flush()
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            reply: dict[str, Any] = {"id": None, "ok": False, "error": "invalid JSON"}
        else:
            reply = _handle(helpers, request if isinstance(request, dict) else {})
        sys.stdout.write(json.dumps(reply, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
