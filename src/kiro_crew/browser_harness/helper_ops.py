"""Ops that are one Browser Harness helper each, with the tool's own checks around it.

* ``fill {ref, text, clear?}`` -- Harness's ``fill_input``: types the text with real
  key events, then fires ``input`` and ``change``, so a framework-managed field
  (React, Vue) sees the new value where ``type`` (``Input.insertText``) may not.
  The ref is focused and judged first, with ``type``'s refusals (``ops._focus_field``:
  never a password box, a field the person filled through ``ask``, or an unread
  cross-origin frame), and the helper is then pointed at ``:focus``, the field just
  judged. ``clear`` (default true) selects all and deletes before typing.
* ``dispatch_key {selector, key, event?}`` -- Harness's ``dispatch_key``: a page-made
  DOM ``KeyboardEvent`` on the first element the CSS selector matches. It types no
  text. A selector that matches nothing is ``not_found``.
* ``wait_for_element {selector, timeout?, visible?}`` and
  ``wait_for_network_idle {timeout?, idle_ms?}`` -- Harness's waits of the same names.

Harness evaluates its own fixed expressions with the selector as JSON data, never
the agent's script. A selector the walker's secret-probe guard refuses (a value
attribute or an escape, while the page holds a filled secret) is ``bad_selector``
before Harness sees it. ``ops`` reaches this module by a call-time import.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.browser_harness import ops as _ops
from kiro_crew.browser_harness.frames import split_frame_ref
from kiro_crew.browser_harness.keys import known_key, unknown_key_error

#: Longest ``fill`` text: Harness presses one key per character, about 6 ms each on a
#: plain page in headless Chromium, so 1,000 stay well inside the slow-op budget.
#: shortcut: one fixed cap; raise it if real forms need longer text in one op.
FILL_CAP = 1_000
SELECTOR_MAX = 500
#: The DOM events ``dispatch_key`` may make.
KEY_EVENTS = ("keydown", "keypress", "keyup")
#: How long ``dispatch_key`` looks for its element before it answers ``not_found``.
FIND_S = 0.5
IDLE_MS_MIN = 50
IDLE_MS_MAX = 10_000


async def _refuse_probe(ctx: _ops.OpContext, op: str, selector: str) -> dict[str, Any] | None:
    """A refusal when *selector* could test a filled secret's value, else ``None``.

    Harness runs the selector with its own ``querySelector``, outside the walker, so
    the walker's guard (``probeSelectorBlocked``) is asked first, as ``cells`` does.
    """
    if await _ops._page(ctx, "selectorBlocked", selector) is False:
        return None
    return _ops.refusal(
        "bad_selector",
        f"{op}: while the page holds a filled secret field, a selector may not test "
        "a value attribute or use an escape",
    )


def _selector(args: dict[str, Any]) -> str | None:
    selector = args.get("selector")
    if isinstance(selector, str) and selector.strip() and len(selector) <= SELECTOR_MAX:
        return selector
    return None


def _timeout(args: dict[str, Any]) -> float:
    """``timeout`` in seconds, as ``wait_for`` reads it: default 5, at most 30."""
    timeout = _ops._number(args.get("timeout"))
    if timeout is None:
        return _ops.WAIT_DEFAULT_S
    return max(0.0, min(timeout, _ops.WAIT_MAX_S))


async def fill(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    text = args.get("text")
    if not isinstance(text, str):
        return _ops.refusal("bad_text", "fill needs a `text` string")
    if len(text) > FILL_CAP:
        return _ops.refusal("bad_text", f"fill takes at most {FILL_CAP} characters; use type")
    clear = args.get("clear", True)
    if not isinstance(clear, bool):
        return _ops.refusal("bad_args", "`clear` must be true or false")
    ref = args.get("ref")
    if not _ops._is_ref(ref):
        return _ops.refusal("bad_ref", f"fill needs a `ref` from a snapshot, got {ref!r}")
    if split_frame_ref(ref) is not None:
        return _ops.refusal(
            "bad_ref", f"fill cannot reach {ref}, a field in a cross-origin frame; use type"
        )
    field = await _ops._focus_field(ctx, ref)
    if not field["ok"]:
        return field
    await _ops._step(ctx, f"Fill {ref}")
    await ctx.helper("fill_input", selector=":focus", text=text, clear_first=clear)
    return {"ok": True, "ref": ref, "chars": len(text), "cleared": clear}


async def dispatch_key(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    selector = _selector(args)
    if selector is None:
        return _ops.refusal(
            "bad_selector",
            f"dispatch_key needs a CSS `selector`, {SELECTOR_MAX} characters at most",
        )
    key = args.get("key", "Enter")
    if not known_key(key):
        return _ops.refusal("bad_key", unknown_key_error(key))
    event = args.get("event", "keypress")
    if event not in KEY_EVENTS:
        return _ops.refusal("bad_event", f"event must be one of {', '.join(KEY_EVENTS)}")
    refused = await _refuse_probe(ctx, "dispatch_key", selector)
    if refused is not None:
        return refused
    if not await ctx.helper("wait_for_element", selector=selector, timeout=FIND_S, visible=False):
        return _ops.refusal("not_found", f"no element matches {selector[:120]!r}")
    await _ops._step(ctx, f"Send {key} to {selector[:60]}")
    await ctx.helper("dispatch_key", selector=selector, key=key, event=event)
    return {"ok": True, "selector": selector, "key": key, "event": event}


async def wait_for_element(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    selector = _selector(args)
    if selector is None:
        return _ops.refusal(
            "bad_selector",
            f"wait_for_element needs a CSS `selector`, {SELECTOR_MAX} characters at most",
        )
    visible = args.get("visible", False)
    if not isinstance(visible, bool):
        return _ops.refusal("bad_args", "`visible` must be true or false")
    timeout = _timeout(args)
    refused = await _refuse_probe(ctx, "wait_for_element", selector)
    if refused is not None:
        return refused
    await _ops._step(ctx, "Wait for an element")
    if await ctx.helper("wait_for_element", selector=selector, timeout=timeout, visible=visible):
        return {"ok": True, "selector": selector, "visible": visible}
    shown = " visible" if visible else ""
    return _ops.refusal(
        "wait_timeout", f"no{shown} element matched {selector[:120]!r} within {timeout:g}s"
    )


async def wait_for_network_idle(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    idle_ms = args.get("idle_ms", 500)
    if isinstance(idle_ms, bool) or not isinstance(idle_ms, int):
        return _ops.refusal("bad_args", "`idle_ms` must be a whole number of milliseconds")
    idle_ms = max(IDLE_MS_MIN, min(idle_ms, IDLE_MS_MAX))
    timeout = _timeout(args)
    await _ops._step(ctx, "Wait for the network to go quiet")
    if await ctx.helper("wait_for_network_idle", timeout=timeout, idle_ms=idle_ms):
        return {"ok": True, "idle_ms": idle_ms}
    return _ops.refusal("wait_timeout", f"the page was still loading requests after {timeout:g}s")
