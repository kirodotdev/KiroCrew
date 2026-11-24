"""``set_appearance`` and ``resize``: how the agent's own tab renders.

* ``set_appearance {scheme: light | dark | reset}`` -- the page's
  ``prefers-color-scheme`` (``Emulation.setEmulatedMedia``); ``reset`` drops the
  override, so the page follows the browser again.
* ``resize {width, height}`` or ``{reset: true}`` -- the viewport in CSS pixels
  (``Emulation.setDeviceMetricsOverride``, bounded by :data:`WIDTH_MIN` ..
  :data:`HEIGHT_MAX`), or back to the window's own size
  (``Emulation.clearDeviceMetricsOverride``).

They are not Browser Harness helpers: the owner asked for them (the browser
dark-mode and viewport entry in ``docs/decisions/``).

Chrome keeps an Emulation override per CDP session, and a reset sent on another
session leaves it in place. Harness attaches a new session on every
``switch_tab`` and never detaches the old one, so its ``cdp`` helper could set an
override no later call can clear. These ops therefore never go through Harness:
``ctx.emulate`` sends on the relay's own session of the tab
(``CdpRelay.emulate``), the one session every set and reset of that tab share.
The relay holds such a session only on a tab the agent opened, so a tab the
person opened is never reached. Closing the tab, which a chat's end does, ends
the session and every override on it.

``ops`` reaches this module by a call-time import.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.browser_harness import ops as _ops

SCHEMES = ("light", "dark", "reset")
#: Viewport bounds in CSS pixels: a small phone to a 4K screen.
WIDTH_MIN, WIDTH_MAX = 320, 3840
HEIGHT_MIN, HEIGHT_MAX = 240, 2160


_Send = Callable[[dict[str, Any] | None], Awaitable[Any]]


async def _own_tab(ctx: _ops.OpContext, op: str, method: str) -> _Send | dict[str, Any]:
    """A sender of *method* to the bound tab, or the refusal when it is not one of the caller's."""
    emulate = ctx.emulate
    if emulate is None:
        return _ops.refusal("not_available", f"{op} is not available here")
    tab = await ctx.helper("current_tab")
    target = (tab.get("targetId") or tab.get("target_id")) if isinstance(tab, dict) else None
    if not isinstance(target, str) or target not in ctx.agent_targets():
        return _ops.refusal("not_agent_tab", f"{op} works only on a tab you opened")

    async def send(params: dict[str, Any] | None) -> Any:
        return await emulate(target, method, params or {})

    return send


async def set_appearance(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    scheme = args.get("scheme")
    if scheme not in SCHEMES:
        return _ops.refusal("bad_scheme", "set_appearance needs `scheme`: light, dark or reset")
    send = await _own_tab(ctx, "set_appearance", "Emulation.setEmulatedMedia")
    if isinstance(send, dict):
        return send
    label = (
        "Follow the browser's colors" if scheme == "reset" else f"Show the page in {scheme} mode"
    )
    await _ops._step(ctx, label)
    features = [] if scheme == "reset" else [{"name": "prefers-color-scheme", "value": scheme}]
    await send({"features": features})
    return {"ok": True, "scheme": scheme}


def _size(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


async def resize(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    reset = args.get("reset", False)
    if not isinstance(reset, bool):
        return _ops.refusal("bad_size", "`reset` must be true or false")
    width = _size(args.get("width"), WIDTH_MIN, WIDTH_MAX)
    height = _size(args.get("height"), HEIGHT_MIN, HEIGHT_MAX)
    given = "width" in args or "height" in args
    if reset == given or (not reset and (width is None or height is None)):
        return _ops.refusal(
            "bad_size",
            f"resize needs whole-number `width` {WIDTH_MIN}-{WIDTH_MAX} and `height` "
            f"{HEIGHT_MIN}-{HEIGHT_MAX}, or `reset: true` alone",
        )
    method = (
        "Emulation.clearDeviceMetricsOverride" if reset else "Emulation.setDeviceMetricsOverride"
    )
    send = await _own_tab(ctx, "resize", method)
    if isinstance(send, dict):
        return send
    if reset:
        await _ops._step(ctx, "Return the page to the window's size")
        await send(None)
        return {"ok": True, "reset": True}
    await _ops._step(ctx, f"Resize the page to {width} x {height}")
    # deviceScaleFactor 0 keeps the screen's own; mobile false keeps desktop layout.
    params = {"width": width, "height": height, "deviceScaleFactor": 0, "mobile": False}
    await send(params)
    return {"ok": True, "width": width, "height": height}


__all__ = [
    "HEIGHT_MAX",
    "HEIGHT_MIN",
    "SCHEMES",
    "WIDTH_MAX",
    "WIDTH_MIN",
    "resize",
    "set_appearance",
]
