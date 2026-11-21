"""The helper worker: Harness's own helpers, one at a time, over JSON lines.

Run BY THE VENV'S INTERPRETER (``<prefix>/bin/python -I <this file>``), never imported
by the gateway: the gateway's Python carries no ``browser_harness`` package unless its
runtime bundles the harness, and this file has no ``kiro_crew`` import for the same
reason in reverse. ``-I`` keeps the script's own directory off ``sys.path``, so
``import browser_harness`` below resolves to the package in the venv (installed from
the lock or copied from the runtime), never to the ``kiro_crew.browser_harness``
directory this file happens to live in.

Protocol, one JSON object per line each way:

* request ``{"id": n, "helper": "<name>", "kwargs": {...}}`` -- run one allowlisted
  helper with those keyword arguments;
* request ``{"id": n, "ping": true}`` -- liveness;
* reply ``{"id": n, "ok": true, "result": <json>}`` or
  ``{"id": n, "ok": false, "error": "<text>"}``.

The allowlist is the whole surface: ``run.py``, Harness's MCP server and the
agent-helper loader are not reachable through it. ``js`` runs the gateway's own
accessibility walker and, with ``cdp`` and ``http_get``, the agent's ``js``, ``cdp``
and ``http_get`` ops (``browser_harness.raw_ops``). The supervisor checks the same
list before it sends (``supervisor.HELPERS``); this copy is what holds if anything
else ever reaches the pipe.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Callable

#: Helper name -> (helpers.py function, accepted keyword arguments). A name starting
#: with ``_local_`` is a function in THIS file, built from Harness's ``cdp`` primitive
#: (the mouse moves Harness has no helper for) or, for ``http_get``, from urllib.
_ALLOWLIST: dict[str, tuple[str, tuple[str, ...]]] = {
    "goto": ("goto_url", ("url",)),
    "click": ("click_at_xy", ("x", "y", "button", "clicks")),
    "mouse_move": ("_local_mouse_move", ("x", "y")),
    "drag": ("_local_drag", ("x", "y", "to_x", "to_y")),
    "type_text": ("type_text", ("text",)),
    "fill_input": ("fill_input", ("selector", "text", "clear_first")),
    "dispatch_key": ("dispatch_key", ("selector", "key", "event")),
    "press_key": ("press_key", ("key", "modifiers")),
    "scroll": ("scroll", ("x", "y", "dy", "dx")),
    "screenshot": ("capture_screenshot", ("path", "full", "max_dim")),
    "screenshot_clip": (
        "_local_screenshot_clip",
        ("path", "x", "y", "width", "height", "max_dim", "magnify"),
    ),
    "page_info": ("page_info", ()),
    "dialog": ("_local_dialog", ("action", "text")),
    "wait_for_load": ("wait_for_load", ("timeout",)),
    "wait_for_element": ("wait_for_element", ("selector", "timeout", "visible")),
    "wait_for_network_idle": ("wait_for_network_idle", ("timeout", "idle_ms")),
    "new_tab": ("new_tab", ("url",)),
    "switch_tab": ("switch_tab", ("target", "activate")),
    "list_tabs": ("list_tabs", ("include_chrome",)),
    "close_tab": ("close_tab", ("target",)),
    "current_tab": ("current_tab", ()),
    "js": ("js", ("expression", "target_id")),
    "cdp": ("_local_cdp", ("method", "params")),
    "http_get": ("_local_http_get", ("url", "headers", "timeout")),
    "js_isolated": ("_local_js_isolated", ("expression",)),
    "frame_owner_count": ("_local_frame_owner_count", ()),
    "closed_secret_count": ("_local_closed_secret_count", ()),
    "page_clock": ("_local_page_clock", ("state",)),
    "set_files": ("_local_set_files", ("expression", "files")),
    "chooser_files": ("_local_chooser_files", ("x", "y", "files")),
    "drop_files": ("_local_drop_files", ("x", "y", "files")),
}

#: The world :func:`_local_js_isolated` evaluates in: the top frame's own isolated
#: world, where page script cannot replace the DOM functions the walker reads with.
#: The walker keeps its refs there, and the gateway's stream session reaches the same
#: world by this name (``frames.TOP_WALKER_WORLD``, kept equal by a test).
_GUARD_WORLD = "kirocrew_guard_walker"

#: Longest error text sent back; a Python traceback can be pages.
_ERROR_CAP = 4000

#: Longest reply line written to the gateway. The gateway reads with a stream limit
#: of ``supervisor.WORKER_LINE_LIMIT`` (8 MiB) and treats an over-limit line as a
#: protocol failure; a result that would serialise past this cap is replaced by an
#: error reply of a few hundred bytes instead, so the pipe never desynchronises.
#: The walker caps what it returns (nodes, console text) far below this.
REPLY_CAP_BYTES = 4 * 1024 * 1024
#: Most body bytes one ``http_get`` reads. JSON-escaped at its worst (six bytes a
#: character) it still fits :data:`REPLY_CAP_BYTES`.
HTTP_BODY_CAP_BYTES = 512 * 1024


def _local_mouse_move(helpers: Any, x: float, y: float) -> dict[str, float]:
    """Move the pointer to a viewport point (hover) on the daemon's current tab."""
    helpers.cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    return {"x": x, "y": y}


def _local_cdp(helpers: Any, method: str, params: dict[str, Any] | None = None) -> Any:
    """Harness ``cdp(method, **params)`` on the daemon's current tab.

    A param named like one of the helper's own arguments would be taken as that
    argument (a ``session_id`` would move the command to another tab), so it is refused.
    """
    params = dict(params or {})
    taken = {"session_id", "_response_timeout"} & set(params)
    if taken:
        raise ValueError(f"cdp: params may not carry {sorted(taken)}")
    return helpers.cdp(method, **params)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _local_http_get(
    helpers: Any, url: str, headers: dict[str, str] | None = None, timeout: float = 20.0
) -> Any:
    """Harness ``http_get``'s plain urllib GET, without following a redirect.

    The gateway judges only the URL it was asked for, so a redirect is answered as
    ``{"status", "location"}`` and the next hop is a new call that is judged and
    asked again. At most :data:`HTTP_BODY_CAP_BYTES` of the body are read (after
    gzip), so a huge response cannot fill this process; the gateway cuts far below
    that. Harness's hosted fetch proxy is not used.
    """
    del helpers
    h = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            body: Any = r
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.GzipFile(fileobj=r)
            data = body.read(HTTP_BODY_CAP_BYTES)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            location = exc.headers.get("Location", "")
            exc.close()
            return {"status": exc.code, "location": location}
        raise
    return data.decode("utf-8", errors="replace")


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


def _local_dialog(
    helpers: Any, action: str | None = None, text: str | None = None
) -> dict[str, Any]:
    """Read the tab's open JavaScript dialog and, given an *action*, answer it.

    Harness's daemon records the dialog the page opened (``page_info`` returns it
    in place of the page's size while it is open). ``accept`` sends *text* to a
    prompt; ``dismiss`` never does. With no dialog open nothing is sent.
    """
    if action not in (None, "accept", "dismiss"):
        raise ValueError("dialog action must be accept or dismiss")
    info = helpers.page_info()
    pending = info.get("dialog") if isinstance(info, dict) else None
    if not isinstance(pending, dict):
        return {"dialog": None}
    if action is None:
        return {"dialog": pending}
    params: dict[str, Any] = {"accept": action == "accept"}
    if action == "accept" and text is not None:
        params["promptText"] = text
    helpers.cdp("Page.handleJavaScriptDialog", **params)
    return {"dialog": pending, "answered": action}


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


def _local_screenshot_clip(
    helpers: Any,
    path: str,
    x: float,
    y: float,
    width: float,
    height: float,
    max_dim: int,
    magnify: float,
) -> dict[str, Any]:
    """Write a PNG of one box of the page: an element, or a region to read up close.

    *x*, *y*, *width*, *height* are CSS pixels from the top left of the visible
    page (the pixels a click uses). The image is scaled so its longest side is at
    most *max_dim* device pixels, and is magnified past one-to-one by at most
    *magnify* (1 keeps an element at its own size).
    """
    metrics = helpers.cdp("Page.getLayoutMetrics")
    view = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
    scale_now = helpers.cdp(
        "Runtime.evaluate", expression="window.devicePixelRatio", returnByValue=True
    )
    ratio = (scale_now.get("result") or {}).get("value")
    ratio = float(ratio) if isinstance(ratio, (int, float)) and ratio > 0 else 1.0
    scale = min(magnify, max_dim / (max(width, height) * ratio))
    shot = helpers.cdp(
        "Page.captureScreenshot",
        format="png",
        captureBeyondViewport=True,
        fromSurface=True,
        clip={
            "x": x + float(view.get("pageX") or 0),
            "y": y + float(view.get("pageY") or 0),
            "width": width,
            "height": height,
            "scale": scale,
        },
    )
    data = shot.get("data") if isinstance(shot, dict) else None
    if not isinstance(data, str) or not data:
        raise RuntimeError("screenshot_clip: the browser returned no image")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(data))
    return {"path": path, "scale": scale}


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


def _local_js_isolated(helpers: Any, expression: str) -> Any:
    """Evaluate *expression* in an isolated world of the current tab's top frame.

    The walker reads the page, and the guards count asked and password fields, here and
    not in the page's own world: a page that replaces ``document.querySelectorAll`` or
    ``getAttribute`` there cannot change what the walker sees in this one. Any step
    that does not give a value raises, so the gateway's guard fails closed instead of
    reading an empty answer as zero.
    """
    out = helpers.cdp(
        "Runtime.evaluate",
        expression=expression,
        contextId=_isolated_context(helpers, "js_isolated"),
        returnByValue=True,
    )
    if out.get("exceptionDetails"):
        raise RuntimeError("js_isolated: the expression threw")
    return (out.get("result") or {}).get("value")


def _local_set_files(helpers: Any, expression: str, files: Any) -> dict[str, bool]:
    """Set one ``<input type=file>`` to *files* (1 to :data:`_MAX_FILES` gateway-staged paths).

    *expression* is the walker's ``fileInput(ref, true)``, evaluated in the same
    isolated world as :func:`_local_js_isolated` (where the refs live); it gives the
    element only when it is a visible, enabled file input, else ``null``, and then
    nothing is set (``{"ok": false}``). ``DOM.setFileInputFiles`` on that node fires
    the page's own ``input`` and ``change`` events.
    """
    _check_files(files, "set_files")
    out = helpers.cdp(
        "Runtime.evaluate",
        expression=expression,
        contextId=_isolated_context(helpers, "set_files"),
        returnByValue=False,
    )
    if out.get("exceptionDetails"):
        raise RuntimeError("set_files: the expression threw")
    found = out.get("result") or {}
    object_id = found.get("objectId")
    if found.get("subtype") != "node" or not isinstance(object_id, str):
        return {"ok": False}
    try:
        helpers.cdp("DOM.setFileInputFiles", files=files, objectId=object_id)
    finally:
        try:
            helpers.cdp("Runtime.releaseObject", objectId=object_id)
        except Exception:  # noqa: BLE001 - the page dropped it already: nothing to free
            pass
    return {"ok": True}


#: Most paths one helper call hands the browser; the gateway's ``upload``
#: (``UPLOAD_MAX_TREE_FILES``) is the same, kept equal by a test.
_MAX_FILES = 100
#: How long a click is given to open a file chooser before ``no_chooser``.
_CHOOSER_WAIT_S = 3.0
_POLL_S = 0.05


def _check_files(files: Any, who: str) -> None:
    if not (
        isinstance(files, list)
        and 1 <= len(files) <= _MAX_FILES
        and all(isinstance(f, str) and f for f in files)
    ):
        raise ValueError(f"{who} takes 1 to {_MAX_FILES} file paths")


def _chooser_opened(helpers: Any) -> dict[str, Any]:
    """The first ``Page.fileChooserOpened`` buffered since the last read, or ``{}``.

    Harness's daemon keeps the page's events in one buffer that every read empties;
    the gateway reads no other event from it, so the ones read past here are dropped.
    """
    for event in helpers.drain_events() or []:
        if isinstance(event, dict) and event.get("method") == "Page.fileChooserOpened":
            params = event.get("params")
            return params if isinstance(params, dict) else {}
    return {}


def _own_frame(helpers: Any, frame_id: str) -> bool:
    """Whether *frame_id* is this tab's top frame or a frame of the top frame's origin.

    A chooser opened in a frame from another origin (the click landed on an iframe
    inside the ref) would give a third party the files; a frame that runs in its own
    process is not in this tree at all. The tree also names this tab's frames only,
    so a chooser another tab opened never answers here.
    """
    tree = helpers.cdp("Page.getFrameTree").get("frameTree") or {}
    top = tree.get("frame") or {}
    origin = top.get("securityOrigin")
    stack = [tree]
    while stack:
        node = stack.pop()
        frame = node.get("frame") or {}
        if frame.get("id") == frame_id:
            return bool(origin) and frame.get("securityOrigin") == origin
        stack.extend(node.get("childFrames") or [])
    return False


#: Element names that draw another document: a drop on one lands in that page.
_FRAME_NODES = frozenset({"IFRAME", "FRAME", "OBJECT", "EMBED", "PORTAL", "FENCEDFRAME"})


def _local_drop_files(helpers: Any, x: float, y: float, files: Any) -> dict[str, Any]:
    """Drop *files* (gateway-staged paths) at ``(x, y)``, pressing nothing.

    The browser is told a drag carrying those files entered, moved over and dropped
    there, so a drop zone with no ``<input type=file>`` gets them in its ``drop``
    event's ``dataTransfer`` (a staged folder arrives as a folder entry). A point
    over a frame, or inside one, is ``frame_refused``: the drop would land in that
    frame's page.
    """
    _check_files(files, "drop_files")
    # One point for the check and the drop, so they cannot land a pixel apart.
    x, y = int(x), int(y)
    # The browser's own hit test, through every shadow root (a closed one too): the
    # node must be in the tab's top frame and not itself an embedded document. It
    # honours ``pointer-events: none`` as the drop does, so an overlay that lets the
    # drop through to a frame below cannot stand in for it.
    tree = helpers.cdp("Page.getFrameTree").get("frameTree") or {}
    top = (tree.get("frame") or {}).get("id")
    data = {"items": [], "files": files, "dragOperationsMask": 1}
    for kind in ("dragEnter", "dragOver", "drop"):
        # Again before each event: the page's own drag handlers run in between and
        # can put a frame under the point.
        if not top or not _drop_point_is_top(helpers, top, x, y):
            if kind != "dragEnter":
                helpers.cdp("Input.dispatchDragEvent", type="dragCancel", x=x, y=y, data=data)
            return {"ok": False, "code": "frame_refused"}
        helpers.cdp("Input.dispatchDragEvent", type=kind, x=x, y=y, data=data)
    return {"ok": True}


def _drop_point_is_top(helpers: Any, top: str, x: int, y: int) -> bool:
    """Whether the node the browser hits at ``(x, y)`` is in frame *top* and not a frame."""
    hit = helpers.cdp(
        "DOM.getNodeForLocation",
        x=x,
        y=y,
        includeUserAgentShadowDOM=True,
        ignorePointerEventsNone=False,
    )
    node = hit.get("backendNodeId")
    if hit.get("frameId") != top or not isinstance(node, int):
        return False
    described = helpers.cdp("DOM.describeNode", backendNodeId=node).get("node") or {}
    return str(described.get("nodeName") or "").upper() not in _FRAME_NODES


def _local_chooser_files(helpers: Any, x: float, y: float, files: Any) -> dict[str, Any]:
    """Click at ``(x, y)`` and answer the file chooser it opens with *files*.

    File-chooser interception is on only for this call: the chooser never shows
    (on the person's own Chrome it would be a native dialog nobody answers), and the
    browser names the input it was opened for, hidden or not even in the document,
    which then gets the files. ``no_chooser`` when the click opened none within
    :data:`_CHOOSER_WAIT_S`; ``not_multiple`` for several files on a single-file
    chooser; nothing is set then.
    """
    _check_files(files, "chooser_files")
    helpers.cdp("Page.setInterceptFileChooserDialog", enabled=True)
    try:
        helpers.drain_events()
        helpers.click_at_xy(x, y)
        opened: dict[str, Any] = {}
        deadline = time.monotonic() + _CHOOSER_WAIT_S
        while True:
            opened = _chooser_opened(helpers)
            if opened or time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S)
        node = opened.get("backendNodeId")
        if not isinstance(node, int) or isinstance(node, bool):
            return {"ok": False, "code": "no_chooser"}
        mode = str(opened.get("mode") or "")
        if not _own_frame(helpers, str(opened.get("frameId") or "")):
            return {"ok": False, "code": "frame_refused"}
        if len(files) > 1 and mode != "selectMultiple":
            return {"ok": False, "code": "not_multiple", "mode": mode}
        helpers.cdp("DOM.setFileInputFiles", files=files, backendNodeId=node)
        return {"ok": True, "mode": mode}
    finally:
        try:
            helpers.cdp("Page.setInterceptFileChooserDialog", enabled=False)
        except Exception:  # noqa: BLE001 - the tab went away: nothing left to intercept
            pass


def _isolated_context(helpers: Any, who: str) -> int:
    """The execution context of the walker's isolated world in the tab's top frame."""
    tree = helpers.cdp("Page.getFrameTree")
    frame_id = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
    if not isinstance(frame_id, str) or not frame_id:
        raise RuntimeError(f"{who}: the tab has no top frame")
    made = helpers.cdp(
        "Page.createIsolatedWorld",
        frameId=frame_id,
        worldName=_GUARD_WORLD,
        grantUniveralAccess=False,
    )
    context_id = made.get("executionContextId")
    if not isinstance(context_id, int) or isinstance(context_id, bool):
        raise RuntimeError(f"{who}: no isolated world was created")
    return context_id


def _pierced_document(helpers: Any, who: str) -> dict[str, Any]:
    """The tab's whole DOM as the browser sees it (``DOM.getDocument`` with ``pierce``).

    Read by the browser, not by page script, so closed shadow roots are included,
    and so is the owner element of a frame that runs out of process. The DOM
    domain is turned off again afterwards, so the session is not sent mutation
    events for the rest of its life. Raises when there is no document.
    """
    try:
        reply = helpers.cdp("DOM.getDocument", depth=-1, pierce=True)
    finally:
        try:
            helpers.cdp("DOM.disable")
        except Exception:  # noqa: BLE001 - already off: nothing to undo
            pass
    root = reply.get("root") if isinstance(reply, dict) else None
    if not isinstance(root, dict):
        raise RuntimeError(f"{who}: the tab has no document")
    return root


def _walk_dom(root: dict[str, Any], who: str) -> Any:
    """Every node under *root* with whether it sits inside a closed shadow root.

    Descends into children, shadow roots and documents the browser hands back
    inline. Raises on any malformed node, so a guard built on it fails closed.
    """
    pending: list[tuple[Any, bool]] = [(root, False)]
    while pending:
        node, closed = pending.pop()
        if not isinstance(node, dict) or not isinstance(node.get("nodeName"), str):
            raise RuntimeError(f"{who}: malformed node")
        yield node, closed
        kids = node.get("children")
        if kids is not None:
            if not isinstance(kids, list):
                raise RuntimeError(f"{who}: malformed node")
            pending.extend((k, closed) for k in kids)
        roots = node.get("shadowRoots")
        if roots is not None:
            if not isinstance(roots, list):
                raise RuntimeError(f"{who}: malformed node")
            for r in roots:
                inner = closed or (isinstance(r, dict) and r.get("shadowRootType") == "closed")
                pending.append((r, inner))
        doc = node.get("contentDocument")
        if doc is not None:
            pending.append((doc, closed))


def _local_frame_owner_count(helpers: Any) -> int:
    """How many ``iframe``/``frame``/``object``/``embed`` elements the tab's DOM holds.

    Counted from :func:`_pierced_document`, so an element in a closed shadow root
    is counted, and so is one whose frame runs out of process (the frame tree of
    this session would miss that one).
    """
    root = _pierced_document(helpers, "frame_owner_count")
    return sum(
        1
        for node, _closed in _walk_dom(root, "frame_owner_count")
        if node["nodeName"].upper() in _FRAME_OWNERS
    )


def _local_closed_secret_count(helpers: Any) -> int:
    """How many secret-looking fields sit inside closed shadow roots.

    Page-world and isolated-world walks cannot enter a closed root, so a password,
    one-time-code or quick-ask field there is invisible to the screenshot guards
    and the replay mask. Counted from the browser's own DOM by the walker's own
    rule (:func:`_secret_attrs`), whether or not the field holds a value (the DOM
    gives attributes, not the live value), so the guard refuses rather than passes.
    A frame owner in a closed root with no document handed back inline runs out of
    process: nothing here looked inside it, so it counts as one secret field.
    """
    root = _pierced_document(helpers, "closed_secret_count")
    return sum(
        1
        for node, closed in _walk_dom(root, "closed_secret_count")
        if closed and (_secret_attrs(node) or _uninspected_frame(node))
    )


def _uninspected_frame(node: dict[str, Any]) -> bool:
    """Whether *node* owns a document the browser did not hand back inline."""
    return node["nodeName"].upper() in _FRAME_OWNERS and node.get("contentDocument") is None


def _secret_attrs(node: dict[str, Any]) -> bool:
    """Whether a DOM node is a field the walker's ``secretish`` names a secret."""
    if node["nodeName"].upper() not in ("INPUT", "TEXTAREA"):
        return False
    flat = node.get("attributes") or []
    if not isinstance(flat, list):
        raise RuntimeError("closed_secret_count: malformed node")
    attrs = {str(flat[i]).lower(): str(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}
    if attrs.get("type", "").lower() == "password" or "data-kc-asked" in attrs:
        return True
    auto = attrs.get("autocomplete", "").lower()
    if any(word in auto.split() for word in _SECRET_AUTOCOMPLETE):
        return True
    named = " ".join((attrs.get("name", ""), attrs.get("id", ""), attrs.get("autocomplete", "")))
    return bool(_SECRET_NAME_RE.search(named) or _SECRET_NAME_RE.search(_split_camel(named)))


#: The elements that can hold a document of their own.
_FRAME_OWNERS = frozenset({"IFRAME", "FRAME", "OBJECT", "EMBED"})
#: ``autocomplete`` tokens that name a secret (the replay mask's rule).
_SECRET_AUTOCOMPLETE = ("current-password", "new-password", "one-time-code", "cc-csc")
#: The walker's ``SECRET_NAME_RE`` (snapshot_walker.js), kept equal by a test.
_SECRET_NAME_RE = re.compile(
    r"(^|[^a-z])(password|passwd|pwd|otp|totp|mfa|cvv|cvc|secret|token|apikey)([^a-z]|$)",
    re.IGNORECASE,
)


def _split_camel(text: str) -> str:
    """``dom_census.split_camel``, kept equal by a test."""
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)


_LOCAL: dict[str, Callable[..., Any]] = {
    "_local_cdp": _local_cdp,
    "_local_http_get": _local_http_get,
    "_local_dialog": _local_dialog,
    "_local_mouse_move": _local_mouse_move,
    "_local_drag": _local_drag,
    "_local_page_clock": _local_page_clock,
    "_local_screenshot_clip": _local_screenshot_clip,
    "_local_js_isolated": _local_js_isolated,
    "_local_set_files": _local_set_files,
    "_local_chooser_files": _local_chooser_files,
    "_local_drop_files": _local_drop_files,
    "_local_frame_owner_count": _local_frame_owner_count,
    "_local_closed_secret_count": _local_closed_secret_count,
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
