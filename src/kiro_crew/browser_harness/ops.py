"""The ``browser`` MCP tool's operations, on top of ``HarnessSupervisor.call``.

One function, :func:`run_op`, serves every op the tool advertises (:data:`OPS`).
It runs in the GATEWAY (the MCP tool is a separate stdio process that posts here
through ``/api/browser/op``), and it reaches the browser only two ways:

* ``ctx.helper(helper, **kwargs)`` -- one allowlisted Harness helper over the
  daemon's IPC (``goto``, ``click``, ``type_text``, ``press_key``, ``scroll``,
  ``mouse_move``, ``drag``, ``screenshot``, ``page_info``, ``wait_for_load``,
  ``new_tab``, ``switch_tab``, ``list_tabs``, ``js``, and the ``reflex`` op's
  ``page_clock``);
* the ``js`` helper, ONLY with :data:`WALKER_SOURCE` -- the page-side half of
  this module (``snapshot_walker.js``) -- so every read or write in the page is
  a method on that one script. No arbitrary expression is ever evaluated.

Every op answers ``{"ok": True, ...}`` or ``{"ok": False, "code", "error"}``.
A refusal is a VALUE, never an exception: a stale ref, a password field, a tab
the agent does not own, a wait that timed out. A transport failure (the daemon
is gone, a helper raised) propagates to the route, which maps it to its own
code, so the two can never be confused and a refusal is never retried elsewhere.

Takeover: before anything else an op asks ``ctx.in_control()``; while the person
holds the browser the op returns ``user_in_control`` rather than touching their
page. The route above it (``dashboard/handlers/browser_ops.py``) is what HOLDS an
op until the hand-back and runs it then, so this check is the backstop under the
route's fence, not the agent's wait. Each op that runs reports itself to the
live card through ``ctx.step(label)``.

Statelessness: nothing here holds per-caller data. The ref map lives in the page
(``window.__kcRefs``), the tab ledger in the relay, the input lock in the stream
lane. This module is a pure function of ``(op, args, ctx)``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.browser_harness.frames import FRAME_READ_CAP, split_frame_ref
from kiro_crew.browser_snapshots import SNAPSHOT_DIR_NAME

logger = logging.getLogger(__name__)

# The ops the tool advertises, in the order the schema lists them. An enum on the
# wire, so the model cannot invent a verb. ``evaluate`` is deliberately absent:
# arbitrary in-page JS is not needed to drive a page and widens the surface an
# untrusted page could influence.
OPS: tuple[str, ...] = (
    "navigate",
    "back",
    "snapshot",
    "click",
    "drag",
    "scroll",
    "type",
    "press_key",
    "hover",
    "select_option",
    "wait_for",
    "console",
    "screenshot",
    "tabs",
    "reflex",
    # Served by the gateway route itself (``dashboard/handlers/browser_ask.py``):
    # it needs the live card, which this module never sees. ``run_op`` refuses it.
    "ask",
)

# Ops that legitimately take longer than one CDP round trip (a page load, an
# explicit wait); the route gives them the long budget and everything else the
# short one.
SLOW_OPS: frozenset[str] = frozenset({"navigate", "back", "wait_for", "reflex"})

WALKER_PATH = Path(__file__).with_name("snapshot_walker.js")

# ``eN`` names an element of the page (same-origin frames included); ``f<tag>eN`` one
# inside a cross-origin frame the gateway reads itself (``browser_harness.frames``).
_REF_RE = re.compile(r"^(?:f[1-9][0-9]{0,3})?e[1-9][0-9]*$")
_MOUSE_BUTTONS = frozenset({"left", "middle", "right", "back", "forward"})
# CDP takes modifiers as a bitmask: Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8.
_MODIFIER_BITS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "command": 4, "cmd": 4, "shift": 8}
_NAMED_KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Backspace",
        "Escape",
        "Delete",
        " ",
        "Space",
        "ArrowLeft",
        "ArrowUp",
        "ArrowRight",
        "ArrowDown",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)

NAV_SETTLE_S = 10.0
NAV_POLL_S = 0.05
WAIT_DEFAULT_S = 5.0
WAIT_MAX_S = 30.0
WAIT_POLL_S = 0.1
CONSOLE_CAP = 500
SCROLL_DEFAULT_PX = 300
TEXT_CAP = 20_000
_URL_MAX = 4096

_SleepFn = Callable[[float], Awaitable[None]]


def _load_walker() -> str:
    """The walker as one JavaScript expression: the file minus its leading comments."""
    lines = WALKER_PATH.read_text(encoding="utf-8").splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("//")):
        lines.pop(0)
    return "\n".join(lines).strip()


# Read once per process: the file ships in the package and never changes at runtime.
WALKER_SOURCE: str = _load_walker()


async def _no_sleep(_seconds: float) -> None:  # pragma: no cover - replaced by the route
    return None


#: Ops that accept ``target`` -- the element described in words -- in place of a
#: ``ref``. Only the optional ``browser.step`` decision hook (``browser_harness.decide``)
#: can resolve one; without it such a call is refused with ``needs_ref``.
TARGET_OPS: frozenset[str] = frozenset({"click", "hover", "type", "select_option"})


def names_target_only(op: str, args: dict[str, Any]) -> bool:
    """Whether *args* name the element only by ``target``: no ``ref`` and no ``x``/``y``."""
    target = args.get("target")
    return (
        op in TARGET_OPS
        and isinstance(target, str)
        and bool(target.strip())
        and args.get("ref") is None
        and args.get("x") is None
        and args.get("y") is None
    )


def needs_ref() -> dict[str, Any]:
    return refusal(
        "needs_ref",
        "`target` could not be matched to one element; call snapshot and pass `ref`",
    )


@dataclass
class OpContext:
    """What one op needs from the gateway, all injected so tests fake it.

    ``helper`` is ``HarnessSupervisor.call``; ``step`` and ``in_control`` are the
    live hub's, bound to the caller's session; ``agent_targets`` is
    ``CdpRelay.agent_targets``; ``screenshot_dir`` is where ``screenshot``
    writes; ``sleep`` and ``now`` are the one clock, so a test drives a wait
    without a real delay.
    """

    helper: Callable[..., Awaitable[Any]]
    step: Callable[[str], Any] = lambda _label: None
    in_control: Callable[[], Any] = lambda: False
    agent_targets: Callable[[], list[str]] = list
    #: The navigate gate, applied to the URL a navigation COMMITTED on (after every
    #: redirect), not only to the one the agent asked for. The route wires the same
    #: predicate the shim applies before the op (``browser_harness.url_gate``).
    url_allowed: Callable[[str], bool] = lambda _url: True
    screenshot_dir: Path = field(default_factory=lambda: Path(os.getcwd()))
    sleep: _SleepFn = _no_sleep
    now: Callable[[], float] = time.monotonic
    #: The optional ``browser.step`` hooks (``browser_harness.decide.BrowserDecisions``)
    #: for this caller's session, or ``None``: then every op runs exactly as written.
    decisions: Any = None
    #: Where a loop op (``reflex``) reports its counters, for the live card's loop
    #: line (``LiveHub.loop``). No-ops by default; no single op calls them.
    loop: Callable[[Mapping[str, Any]], Any] = lambda _info: None
    #: The turn replay's seam for an action a loop op runs inside itself:
    #: ``mark()`` before it, ``record(op, args, result, mark)`` after it
    #: (``LiveHub.inner_mark`` / ``LiveHub.record_op``). No-ops by default.
    mark: Callable[[], Any] = lambda: None
    record: Callable[..., Any] = lambda *_args: None
    #: The cross-origin frames of the caller's tab (``browser_harness.frames.FrameOps``),
    #: read at call time, or ``None`` when no stream holds the tab: then a frame ref
    #: is stale and the outline marks those frames as not read.
    frames: Callable[[], Any] = lambda: None


def screenshot_dir(home: Path) -> Path:
    """Where ``screenshot`` writes its PNGs: a fixed leaf of the data home.

    The leaf is :data:`kiro_crew.browser_snapshots.SNAPSHOT_DIR_NAME`, the one
    name the ``file_send`` admission (``messaging/outbound_files.py``) and the
    sandbox leaf lists spell too. Fixed so the agent gets an absolute path it
    can show in chat. Created owner-only on first use by :func:`_screenshot_path`.
    """
    return home / SNAPSHOT_DIR_NAME


def browsing_available(home: Path) -> bool:
    """Whether the Harness is installed for this data home. Fail-CLOSED.

    The prompt's Browser section and the tool's own call-time check both read
    this. The install plane is the core lane's; an import or read error means
    "not set up", never a working browser the prompt describes wrongly.
    """
    try:
        # By name: the install plane is a sibling lane's module, resolved at call time.
        install = importlib.import_module("kiro_crew.browser_harness.install")
        return bool(getattr(install.install_state(home), "installed", False))
    except Exception:
        logger.debug("browser harness install state unreadable", exc_info=True)
        return False


def refusal(code: str, error: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error}


def _is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_REF_RE.match(value))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def _modifier_mask(modifiers: Any) -> int:
    if isinstance(modifiers, int) and not isinstance(modifiers, bool):
        return modifiers & 15
    if not isinstance(modifiers, list):
        return 0
    mask = 0
    for m in modifiers:
        mask |= _MODIFIER_BITS.get(str(m or "").lower(), 0)
    return mask


def format_outline(nodes: list[dict[str, Any]]) -> str:
    """Render walker nodes as the indented outline the model reads."""
    lines: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        indent = "  " * _depth(n.get("depth"))
        role = n.get("role") or "generic"
        name = "" if n.get("name") is None else str(n.get("name"))
        line = f'{indent}- {role} "{name}"'
        if n.get("ref"):
            line += f" [ref={n['ref']}]"
        if n.get("frame"):
            line += f" [frame {n['frame']}]"
        st = n.get("state") or {}
        if isinstance(st, dict):
            if st.get("unread"):
                line += " [cross-origin, not read]"
            if st.get("password"):
                line += " [password]"
            if st.get("unusable") == "covered" or st.get("covered"):
                line += " [covered]"
            elif st.get("unusable"):
                line += " [not usable yet]"
            if st.get("checked"):
                line += " [checked]"
            if st.get("disabled"):
                line += " [disabled]"
            if st.get("expanded") is True:
                line += " [expanded]"
            elif st.get("expanded") is False:
                line += " [collapsed]"
        lines.append(line)
    return "\n".join(lines)


async def _page(ctx: OpContext, method: str, *args: Any) -> Any:
    """Call one method of the walker in the page: the ONLY use of the ``js`` helper.

    A ``f<tag>eN`` ref runs the method in that cross-origin frame instead, through
    the gateway's session (``resolve`` then answers page pixels). The guards'
    reads -- what holds focus, how many asked or password fields there are --
    take the cross-origin frames into account, failing closed when one cannot
    be read.
    """
    framed = split_frame_ref(args[0]) if args else None
    if framed is not None:
        frames = ctx.frames()
        value = (
            await frames.call(framed[0], method, framed[1], *args[1:])
            if frames is not None
            else None
        )
        return value if value is not None else {"ok": False, "code": "ref_stale"}
    rendered = ", ".join(json.dumps(a) for a in args)
    value = await ctx.helper("js", expression=f"{WALKER_SOURCE}.{method}({rendered})")
    if method in ("askedValues", "passwordBoxes") and isinstance(value, int):
        frames = ctx.frames()
        if frames is not None:
            value += await frames.count(method)
    if method == "activeField" and isinstance(value, dict) and value.get("frame"):
        frames = ctx.frames()
        inner = await frames.focused() if frames is not None else None
        # Focus is inside a cross-origin frame: judge the field there, or nothing.
        value = inner if inner is not None else {**value, "unreadable": True}
    return value


async def _in_control(ctx: OpContext) -> bool:
    held = ctx.in_control()
    if hasattr(held, "__await__"):
        held = await held
    return bool(held)


async def _step(ctx: OpContext, label: str) -> None:
    try:
        result = ctx.step(label[:120])
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001 - the card is a mirror; it never fails an op
        logger.debug("live hub step failed", exc_info=True)


async def _resolve(ctx: OpContext, ref: Any) -> dict[str, Any]:
    if not _is_ref(ref):
        return refusal("bad_ref", f"not a valid ref: {ref!r}; call snapshot first")
    found = await _page(ctx, "resolve", ref)
    if not isinstance(found, dict) or not found.get("ok"):
        return refusal("ref_stale", f"ref {ref} is not on the current page; re-snapshot")
    return {"ok": True, "x": float(found["x"]), "y": float(found["y"]), "tag": found.get("tag")}


async def _point(ctx: OpContext, args: dict[str, Any], ref_key: str, x_key: str, y_key: str) -> Any:
    """A ref OR a coordinate pair -> ``(x, y)``, or a refusal dict."""
    ref = args.get(ref_key)
    if ref is not None:
        resolved = await _resolve(ctx, ref)
        if not resolved["ok"]:
            return resolved
        return resolved["x"], resolved["y"]
    x, y = _number(args.get(x_key)), _number(args.get(y_key))
    if x is None or y is None or x < 0 or y < 0:
        return refusal(
            "bad_target",
            f"give either `{ref_key}` from a snapshot or non-negative `{x_key}`,`{y_key}` pixels",
        )
    return x, y


async def _wait_for_commit(ctx: OpContext, token: str, target: str | None) -> dict[str, Any]:
    """Wait for a navigation started by ``goto``/``back`` to actually commit.

    Two settle signals: the stamp being gone proves a NEW document committed
    (``readyState`` alone reads ``complete`` on the OLD document at once), and
    the href matching covers a same-document navigation (``#section``) where the
    stamp survives. Bounded by both the clock and an iteration count so an
    injected constant clock still terminates.
    """
    deadline = ctx.now() + NAV_SETTLE_S
    last = ""
    for _ in range(max(1, int(NAV_SETTLE_S / NAV_POLL_S))):
        if ctx.now() > deadline:
            break
        await ctx.sleep(NAV_POLL_S)
        try:
            state = await _page(ctx, "state")
        except Exception:  # noqa: BLE001 - the context is torn down mid-navigation
            continue
        if not isinstance(state, dict):
            continue
        ready = str(state.get("readyState", ""))
        new_doc = state.get("stale") != token
        href = str(state.get("href", ""))
        last = f"{ready}{' (still old doc)' if not new_doc else ''} {href}"
        if (new_doc or (target is not None and href == target)) and ready in (
            "interactive",
            "complete",
        ):
            return {"ok": True, "url": href, "title": str(state.get("title", ""))}
    return refusal(
        "nav_timeout", f"navigation did not commit within {NAV_SETTLE_S:g}s (last: {last})"
    )


def _stamp_token(ctx: OpContext) -> str:
    return f"kcnav_{secrets.token_hex(6)}_{int(ctx.now() * 1000)}"


# -- ops ---------------------------------------------------------------------


async def _navigate(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip() or len(url) > _URL_MAX:
        return refusal("bad_url", "navigate needs a `url` string")
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return refusal("bad_url", f"refused non-web URL: {url[:80]}")
    await _step(ctx, f"Open {url}")
    token = _stamp_token(ctx)
    try:
        await _page(ctx, "stamp", token)
    except Exception:  # noqa: BLE001 - an unstampable old page still navigates
        logger.debug("navigate: could not stamp the old document", exc_info=True)
    started = await ctx.helper("goto", url=url)
    if isinstance(started, dict) and started.get("errorText"):
        return refusal("nav_error", str(started["errorText"]))
    committed = await _wait_for_commit(ctx, token, url)
    if not committed["ok"]:
        return committed
    refused = await _refuse_committed_url(ctx, committed["url"])
    if refused is not None:
        return refused
    await _arm_console(ctx)
    return {"ok": True, "url": committed["url"], "title": committed.get("title", "")}


async def _refuse_committed_url(ctx: OpContext, url: str) -> dict[str, Any] | None:
    """The gate, one hop past the request: judge where the navigation LANDED.

    The shim judged the URL the agent asked for; a public page that redirects to a
    loopback, private or link-local address would otherwise be read back through the
    browser. The tab is moved to ``about:blank`` so the landed page is not left in
    front of the live view or the next op, and the refusal names the host.
    """
    if ctx.url_allowed(url):
        return None
    try:
        await ctx.helper("goto", url="about:blank")
    except Exception:  # noqa: BLE001 - the refusal stands even if the blank page fails
        logger.debug("navigate: could not leave the refused page", exc_info=True)
    return refusal(
        "redirect_refused",
        f"the page redirected to {url[:120]}, which is not a public http(s) URL or a "
        "loopback origin the user listed; the tab was moved to about:blank",
    )


async def _arm_console(ctx: OpContext) -> None:
    """Install the page's console hook as early as the new document allows.

    Best effort: the ``console`` op installs it too, so a failure here only
    loses the messages logged before the agent's next page-side call.
    """
    try:
        await _page(ctx, "consoleMessages", 0)
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("navigate: console hook not installed", exc_info=True)


async def _back(ctx: OpContext, _args: dict[str, Any]) -> dict[str, Any]:
    await _step(ctx, "Go back")
    token = _stamp_token(ctx)
    await _page(ctx, "stamp", token)
    went = await _page(ctx, "back")
    if not isinstance(went, dict) or not went.get("ok"):
        return refusal("no_history", "no previous page in history")
    committed = await _wait_for_commit(ctx, token, None)
    if not committed["ok"]:
        return committed
    refused = await _refuse_committed_url(ctx, committed["url"])
    if refused is not None:
        return refused
    return {"ok": True, "url": committed["url"], "title": committed.get("title", "")}


#: The walker's node cap, shared by the page and every frame read after it.
NODE_CAP = 1500


_DEPTH_MAX = 64


def _depth(value: Any) -> int:
    """A walker node's depth, from page script: a small whole number or 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0
    return max(0, min(_DEPTH_MAX, int(value)))


async def _with_frames(ctx: OpContext, nodes: list[Any]) -> list[Any]:
    """Fill each cross-origin frame's ``slot`` node with that frame's own outline.

    The node budget is shared: each frame gets what the page and the frames before
    it left. A frame that cannot be read stays one ``iframe`` line marked so.
    """
    slots = [n for n in nodes if isinstance(n, dict) and isinstance(n.get("slot"), int)]
    frames = ctx.frames() if slots else None
    if not slots:
        return nodes
    ids = await frames.slot_frames(len(slots)) if frames is not None else []
    budget = NODE_CAP - len(nodes)
    out: list[Any] = []
    for n in nodes:
        if not (isinstance(n, dict) and isinstance(n.get("slot"), int)):
            out.append(n)
            continue
        slot = int(n["slot"])
        frame_id = ids[slot] if slot < len(ids) and slot < FRAME_READ_CAP else ""
        read = await frames.outline(frame_id, budget) if frames is not None and frame_id else None
        head = {k: v for k, v in n.items() if k != "slot"}
        if read is None:
            head["state"] = {"unread": True}
            out.append(head)
            continue
        tag, url, inner = read
        # Origin and path only: a frame's query or fragment can carry a token.
        head["frame"] = f"f{tag} {url.split('?', 1)[0].split('#', 1)[0][:120]}"
        out.append(head)
        depth = _depth(n.get("depth")) + 1
        for m in inner:
            out.append({**m, "depth": min(_DEPTH_MAX, depth + _depth(m.get("depth")))})
        budget -= len(inner)
    return out


async def _snapshot(ctx: OpContext, _args: dict[str, Any]) -> dict[str, Any]:
    await _step(ctx, "Read the page")
    data = await _page(ctx, "snapshot")
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return refusal("snapshot_error", "snapshot walker returned no data")
    nodes = await _with_frames(ctx, data["nodes"])
    out: dict[str, Any] = {
        "ok": True,
        "url": data.get("url", ""),
        "title": data.get("title", ""),
        "snapshot": format_outline(nodes)[:TEXT_CAP],
    }
    unread = sum(1 for n in nodes if isinstance(n, dict) and (n.get("state") or {}).get("unread"))
    if unread:
        out["note"] = (
            f"{unread} cross-origin iframe(s) could not be read, so their fields have no "
            "refs. To sign in there, use `ask` kind credentials with no targets: the user "
            "types into Kiro's sheet and the browser fills the site where it can, else "
            "they sign in on the page themselves. Never ask for a password in chat."
        )
    if data.get("error"):
        out["partial"] = str(data["error"])
    return out


async def _click(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    point = await _point(ctx, args, "ref", "x", "y")
    if isinstance(point, dict):
        return point
    x, y = point
    button = args.get("button", "left")
    if button not in _MOUSE_BUTTONS:
        return refusal("bad_button", f"button must be one of {sorted(_MOUSE_BUTTONS)}")
    clicks = args.get("clicks", 2 if args.get("doubleClick") else 1)
    if clicks not in (1, 2) or isinstance(clicks, bool):
        return refusal("bad_clicks", "clicks must be 1 or 2")
    await _step(ctx, f"Click {args.get('ref') or f'({x:.0f}, {y:.0f})'}")
    await ctx.helper("click", x=x, y=y, button=button, clicks=clicks)
    return {"ok": True, "x": x, "y": y, "button": button, "clicks": clicks, "ref": args.get("ref")}


async def _hover(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    point = await _point(ctx, args, "ref", "x", "y")
    if isinstance(point, dict):
        return point
    x, y = point
    await _step(ctx, f"Hover {args.get('ref') or f'({x:.0f}, {y:.0f})'}")
    await ctx.helper("mouse_move", x=x, y=y)
    return {"ok": True, "x": x, "y": y, "ref": args.get("ref")}


async def _drag(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    start = await _point(ctx, args, "ref", "x", "y")
    if isinstance(start, dict):
        return start
    end = await _point(ctx, args, "to_ref", "to_x", "to_y")
    if isinstance(end, dict):
        return end
    await _step(ctx, "Drag")
    await ctx.helper("drag", x=start[0], y=start[1], to_x=end[0], to_y=end[1])
    return {"ok": True, "from": list(start), "to": list(end)}


async def _scroll(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    dx = _number(args.get("dx")) or 0.0
    dy = _number(args.get("dy"))
    if dy is None:
        dy = float(SCROLL_DEFAULT_PX)
    if args.get("ref") is not None or args.get("x") is not None or args.get("y") is not None:
        point = await _point(ctx, args, "ref", "x", "y")
        if isinstance(point, dict):
            return point
        x, y = point
    else:
        info = await ctx.helper("page_info")
        if not isinstance(info, dict) or "w" not in info:
            return refusal("page_error", "could not read the viewport size")
        x, y = float(info["w"]) / 2, float(info["h"]) / 2
    await _step(ctx, "Scroll")
    await ctx.helper("scroll", x=x, y=y, dy=dy, dx=dx)
    return {"ok": True, "x": x, "y": y, "dx": dx, "dy": dy}


async def _type(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    text = args.get("text")
    if not isinstance(text, str):
        return refusal("bad_text", "type needs a `text` string")
    if len(text) > TEXT_CAP:
        return refusal("bad_text", f"text longer than {TEXT_CAP} characters")
    ref = args.get("ref")
    if ref is not None:
        if not _is_ref(ref):
            return refusal("bad_ref", f"not a valid ref: {ref!r}; call snapshot first")
        focused = await _page(ctx, "focus", ref)
        if not isinstance(focused, dict) or not focused.get("ok"):
            code = focused.get("code") if isinstance(focused, dict) else None
            if code == "password_field":
                return _password_refusal(ref)
            if code == "asked_field":
                return _asked_refusal(ref)
            return refusal("ref_stale", f"ref {ref} is not on the current page; re-snapshot")
        if not focused.get("focused"):
            why = " (disabled)" if focused.get("disabled") else ""
            why += " (readonly)" if focused.get("readOnly") else ""
            if focused.get("activeTag"):
                why += f"; focus stayed on <{focused['activeTag']}>"
            return refusal(
                "not_focusable",
                f"ref {ref} did not take focus{why} -- refusing to type into another field",
            )
    # Whatever holds focus now is where the text lands: never a password box.
    active = await _page(ctx, "activeField")
    if not isinstance(active, dict) or not active.get("focused"):
        return refusal("no_focus", "no field has focus; pass a `ref` from a snapshot")
    if active.get("unreadable"):
        return _frame_focus_refusal()
    if active.get("password"):
        return _password_refusal(ref)
    if active.get("asked"):
        return _asked_refusal(ref)
    await _step(ctx, f"Type into {ref or active.get('tag') or 'the field'}")
    await ctx.helper("type_text", text=text)
    submitted = bool(args.get("submit"))
    if submitted:
        await ctx.helper("press_key", key="Enter", modifiers=0)
    return {"ok": True, "ref": ref, "chars": len(text), "submitted": submitted}


def _password_refusal(ref: Any) -> dict[str, Any]:
    return refusal(
        "password_field",
        f"{ref or 'the focused field'} is a password field. The agent never types a "
        "password: use the `ask` op (kind credentials) so the user fills it in on the "
        "live card, or ask them to take over and sign in themselves, then continue.",
    )


def _frame_focus_refusal() -> dict[str, Any]:
    return refusal(
        "focus_in_frame",
        "focus is inside a cross-origin frame that could not be read, so what it holds "
        "is unknown; snapshot and pass a `ref` from the frame (f<n>e<n>), or use `ask`",
    )


def _asked_refusal(ref: Any) -> dict[str, Any]:
    return refusal(
        "asked_field",
        f"{ref or 'the focused field'} holds what the user entered through a quick ask; "
        "the agent does not type into, select or copy it. Move on (Tab) or snapshot.",
    )


#: Modifier bits under which a single-character key inserts no text (Ctrl, Meta).
_NON_TEXT_MODIFIERS = _MODIFIER_BITS["control"] | _MODIFIER_BITS["meta"]


async def _press_key(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("key")
    if not isinstance(key, str) or not key:
        return refusal("bad_key", "press_key needs a `key`")
    if key == "Space":
        key = " "
    if key not in _NAMED_KEYS and len(key) != 1:
        return refusal("bad_key", f"unknown key: {key!r} (named keys or a single character)")
    modifiers = _modifier_mask(args.get("modifiers"))
    if len(key) == 1 or modifiers:
        # A printable key is a character typed into whatever holds focus: the same
        # refusal ``type`` applies, so a password cannot be spelled one key at a time.
        # Ctrl/Meta chords insert nothing into a password box and stay allowed there.
        # On the person's quick-ask field every modified key is refused (Shift+Home,
        # Shift+Delete cut it as surely as Ctrl+X); plain named keys (Tab, Escape,
        # arrows) are never checked, so the agent can always leave a field.
        active = await _page(ctx, "activeField")
        focused = isinstance(active, dict) and active.get("focused")
        if focused and active.get("unreadable"):
            # Unknown field: neither a character nor a modified key, like an asked one.
            return _frame_focus_refusal()
        if focused and active.get("asked"):
            # Neither a character nor any modified key (select, copy, cut) on the
            # person's field: a copy would carry its value into one the agent reads.
            return _asked_refusal(None)
        if (
            focused
            and active.get("password")
            and len(key) == 1
            and not modifiers & _NON_TEXT_MODIFIERS
        ):
            return _password_refusal(None)
    await _step(ctx, f"Press {key.strip() or 'Space'}")
    await ctx.helper("press_key", key=key, modifiers=modifiers)
    return {"ok": True, "key": key, "modifiers": modifiers}


async def _select_option(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    ref = args.get("ref")
    if not _is_ref(ref):
        return refusal("bad_ref", f"not a valid ref: {ref!r}; call snapshot first")
    raw = args.get("values")
    if raw is None:
        raw = args.get("value")
    values = [str(v) for v in raw] if isinstance(raw, list) else [] if raw is None else [str(raw)]
    if not values:
        return refusal("bad_values", "select_option needs `values` (a list) or `value`")
    await _step(ctx, f"Select in {ref}")
    result = await _page(ctx, "selectOption", ref, values)
    if not isinstance(result, dict) or not result.get("ok"):
        code = result.get("code") if isinstance(result, dict) else None
        if code == "not_select":
            return refusal("not_select", f"ref {ref} is not a <select> element")
        return refusal("ref_stale", f"ref {ref} is not on the current page; re-snapshot")
    return {"ok": True, "ref": ref, "selected": result.get("selected", [])}


async def _wait_for(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    seconds = _number(args.get("time"))
    if seconds is not None and seconds > 0:
        waited = min(seconds, WAIT_MAX_S)
        await _step(ctx, f"Wait {waited:g}s")
        await ctx.sleep(waited)
        return {"ok": True, "waited": waited}
    timeout = _number(args.get("timeout"))
    # ``timeout`` is in SECONDS, like ``time``; a millisecond-looking value is read as such.
    if timeout is None:
        timeout = WAIT_DEFAULT_S
    elif timeout > WAIT_MAX_S * 10:
        timeout = timeout / 1000.0
    timeout = max(0.0, min(timeout, WAIT_MAX_S))
    if args.get("load"):
        await _step(ctx, "Wait for the page to load")
        loaded = await ctx.helper("wait_for_load", timeout=timeout)
        return (
            {"ok": True, "loaded": bool(loaded)}
            if loaded
            else refusal("wait_timeout", f"page did not finish loading within {timeout:g}s")
        )
    text = args.get("text") if isinstance(args.get("text"), str) and args.get("text") else None
    gone = (
        args.get("textGone")
        if isinstance(args.get("textGone"), str) and args.get("textGone")
        else None
    )
    if text is None and gone is None:
        return refusal("bad_wait", "wait_for needs one of: text, textGone, time, load")
    target = text if text is not None else str(gone)
    want_present = text is not None
    await _step(ctx, f"Wait for {'text' if want_present else 'text to go'}")
    start = ctx.now()
    for _ in range(max(1, int(WAIT_MAX_S / WAIT_POLL_S)) + 1):
        present = bool(await _page(ctx, "textPresent", target))
        if want_present and present:
            return {"ok": True, "matched": "present", "text": target}
        if not want_present and not present:
            return {"ok": True, "matched": "absent", "text": target}
        if ctx.now() - start >= timeout:
            break
        await ctx.sleep(WAIT_POLL_S)
    state = "present" if want_present else "absent"
    return refusal(
        "wait_timeout", f"timed out after {timeout:g}s waiting for text {state}: {target}"
    )


async def _console(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = args.get("limit", CONSOLE_CAP)
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = CONSOLE_CAP
    limit = max(1, min(limit, CONSOLE_CAP))
    await _step(ctx, "Read the console")
    messages = await _page(ctx, "consoleMessages", limit)
    return {"ok": True, "messages": messages if isinstance(messages, list) else []}


def _screenshot_path(ctx: OpContext) -> Path:
    directory = ctx.screenshot_dir
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover - Windows ACLs
        pass
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return directory / f"shot-{stamp}-{secrets.token_hex(3)}.png"


async def _screenshot(ctx: OpContext, _args: dict[str, Any]) -> dict[str, Any]:
    # A field the person filled through `ask` would be in the pixels (a code, an
    # email; a password once a "show" toggle is clicked). The snapshot withholds it.
    if await _page(ctx, "askedValues"):
        return refusal(
            "asked_value_on_page",
            "a field the user filled through `ask` still holds its value; use snapshot "
            "instead, or take the screenshot after the page moves on",
        )
    await _step(ctx, "Take a screenshot")
    path = _screenshot_path(ctx)
    await ctx.helper("screenshot", path=str(path))
    if not await asyncio.to_thread(path.exists):
        return refusal("screenshot_error", "the browser wrote no image")
    return {"ok": True, "path": str(path)}


async def _tabs(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action", "list")
    owned = set(ctx.agent_targets())
    if action == "list":
        tabs = await ctx.helper("list_tabs")
        mine = [
            {
                "target_id": t.get("target_id") or t.get("targetId"),
                "url": t.get("url"),
                "title": t.get("title"),
            }
            for t in (tabs if isinstance(tabs, list) else [])
            if isinstance(t, dict) and (t.get("target_id") or t.get("targetId")) in owned
        ]
        return {"ok": True, "tabs": mine}
    if action == "switch":
        target = args.get("target_id")
        if not isinstance(target, str) or target not in owned:
            return refusal("not_agent_tab", "only a tab the agent opened can be switched to")
        await _step(ctx, "Switch tab")
        await ctx.helper("switch_tab", target=target)
        return {"ok": True, "target_id": target}
    if action == "new":
        url = args.get("url", "about:blank")
        if not isinstance(url, str) or not url or len(url) > _URL_MAX:
            return refusal("bad_url", "tabs new needs a `url` string")
        if url != "about:blank" and not url.lower().startswith(("http://", "https://")):
            return refusal("bad_url", f"refused non-web URL: {url[:80]}")
        await _step(ctx, f"Open a new tab {url}")
        target = await ctx.helper("new_tab", url=url)
        return {"ok": True, "target_id": target}
    return refusal("bad_action", "tabs action must be one of: list, switch, new")


async def _reflex(ctx: OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """The fast loop (``browser_harness.reflex``), resolved at call time: it imports
    this module, and it is only ever needed when the agent asks for it."""
    module = importlib.import_module("kiro_crew.browser_harness.reflex")
    return await module.run(ctx, args)  # type: ignore[no-any-return]


async def _ask_elsewhere(_ctx: OpContext, _args: dict[str, Any]) -> dict[str, Any]:
    """``ask`` needs the live card; the op route serves it and never calls here."""
    return refusal("not_available", "ask is served by the gateway's op route")


_HANDLERS: dict[str, Callable[[OpContext, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "navigate": _navigate,
    "back": _back,
    "snapshot": _snapshot,
    "click": _click,
    "drag": _drag,
    "scroll": _scroll,
    "type": _type,
    "press_key": _press_key,
    "hover": _hover,
    "select_option": _select_option,
    "wait_for": _wait_for,
    "console": _console,
    "screenshot": _screenshot,
    "tabs": _tabs,
    "reflex": _reflex,
    "ask": _ask_elsewhere,
}


async def run_op(op: str, args: dict[str, Any], ctx: OpContext) -> dict[str, Any]:
    """Run one op. Unknown op -> ``bad_op``; person in control -> ``user_in_control``."""
    handler = _HANDLERS.get(op)
    if handler is None:
        return refusal("bad_op", f"op must be one of: {', '.join(OPS)}")
    if await _in_control(ctx):
        return refusal(
            "user_in_control",
            "the user has taken over the browser; wait for them to hand it back",
        )
    if ctx.decisions is not None:
        return await ctx.decisions.run(op, args, ctx, handler)  # type: ignore[no-any-return]
    if names_target_only(op, args):
        return needs_ref()
    return await handler(ctx, args)
