"""Takeover: the person's input lock on a live session, and the card's events as CDP input.

:class:`InputLock` is one lock per session. The dashboard owner claims it from the
card (``POST /api/browser/input/claim``); while it is held every agent ``browser``
op answers ``user_in_control`` at once instead of waiting, and the lock refuses a
second claimant. Esc or "Hand back" releases it.

:func:`dispatch_input` maps one card event, in frame pixels, to
``Input.dispatchMouseEvent`` / ``Input.dispatchKeyEvent`` on the relay's own
session for the target, dividing positions by the stream's frame-to-CSS scale on
the way (CDP takes viewport CSS pixels; the JPEG is smaller at every rung but the
top). Every field is bounded here, at the boundary, and nothing in this module
logs an event: keystrokes never enter a log or the transcript.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from kiro_crew.browser_harness.screencast import Relay

#: The owner the card claims as. The only claimant the routes ever pass.
USER = "user"
#: What an agent op would claim as; :meth:`InputLock.claim` refuses it while held.
AGENT = "agent"

#: Error code an agent op returns while the lock is held.
USER_IN_CONTROL = "user_in_control"

#: Windows virtual-key codes by DOM ``code``. ``Input.dispatchKeyEvent`` leaves
#: ``keyCode`` at 0 without one, and Chrome runs a key's editing command
#: (Backspace, Delete, arrows, Enter) from it: with 0 Backspace deletes nothing,
#: and a page that filters keys by ``keyCode`` (a digits-only field, a one-time
#: code box) drops every digit.
_VK_NAMED = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "NumpadEnter": 13,
    "ShiftLeft": 16,
    "ShiftRight": 16,
    "ControlLeft": 17,
    "ControlRight": 17,
    "AltLeft": 18,
    "AltRight": 18,
    "Pause": 19,
    "CapsLock": 20,
    "Escape": 27,
    "Space": 32,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Insert": 45,
    "Delete": 46,
    "MetaLeft": 91,
    "MetaRight": 92,
    "ContextMenu": 93,
    "NumpadMultiply": 106,
    "NumpadAdd": 107,
    "NumpadSubtract": 109,
    "NumpadDecimal": 110,
    "NumpadDivide": 111,
    "Semicolon": 186,
    "Equal": 187,
    "Comma": 188,
    "Minus": 189,
    "Period": 190,
    "Slash": 191,
    "Backquote": 192,
    "BracketLeft": 219,
    "Backslash": 220,
    "BracketRight": 221,
    "Quote": 222,
}
#: The same, by DOM ``key``, for an event that came without a ``code``.
_VK_BY_KEY = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "Escape": 27,
    " ": 32,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Insert": 45,
    "Delete": 46,
}
#: Text a named key types: Chrome inserts a line break or submits on ``\r``.
_KEY_TEXT = {"Enter": "\r"}


def virtual_key_code(key: str, code: str) -> int:
    """The Windows virtual-key code for a DOM ``key``/``code`` pair, or 0.

    A named ``key`` wins over the ``code``: Numpad2 with Num Lock off is
    ``ArrowDown`` and must act as one.
    """
    if key in _VK_BY_KEY:
        return _VK_BY_KEY[key]
    if code in _VK_NAMED:
        return _VK_NAMED[code]
    if len(code) == 4 and code.startswith("Key") and "A" <= code[3] <= "Z":
        return ord(code[3])
    if len(code) == 6 and code.startswith("Digit") and code[5].isdigit():
        return ord(code[5])
    if len(code) == 7 and code.startswith("Numpad") and code[6].isdigit():
        return 96 + int(code[6])
    if code.startswith("F") and code[1:].isdigit() and 1 <= int(code[1:]) <= 24:
        return 111 + int(code[1:])
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    return 0


#: Largest frame coordinate accepted; a 16k-wide frame is far past any ladder rung.
MAX_COORD = 16_384
#: Largest wheel delta per event.
MAX_DELTA = 16_384
MAX_CLICKS = 3
MAX_KEY_CHARS = 32
MAX_TEXT_CHARS = 64
#: Alt | Ctrl | Meta | Shift, the four CDP modifier bits.
MAX_MODIFIERS = 15

_BUTTONS = frozenset({"none", "left", "middle", "right", "back", "forward"})
_MOUSE_ACTIONS = frozenset({"click", "move", "down", "up"})
_KEY_ACTIONS = frozenset({"press", "down", "up"})


class InputEventError(ValueError):
    """A card event the mapping refuses; ``code`` is the route's error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class InputLock:
    """One session's input lock: held by at most one owner at a time."""

    def __init__(self) -> None:
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        return self._owner

    def held(self) -> bool:
        return self._owner is not None

    def claim(self, owner: str) -> bool:
        """Take the lock for ``owner``. ``False`` when another owner holds it."""
        if not owner:
            return False
        if self._owner is None or self._owner == owner:
            self._owner = owner
            return True
        return False

    def release(self) -> None:
        self._owner = None


@dataclass(frozen=True)
class InputEvent:
    """A validated card event, ready to map onto CDP."""

    type: str
    action: str
    x: int = 0
    y: int = 0
    button: str = "none"
    clicks: int = 1
    dx: int = 0
    dy: int = 0
    key: str = ""
    code: str = ""
    text: str = ""
    modifiers: int = 0
    #: Width of the frame the card measured ``x``/``y`` in (``w`` on the wire), or 0
    #: when the card did not say. See :func:`frame_scale`.
    frame_w: int = 0


def frame_scale(event: InputEvent, device_width: float, fallback: float) -> float:
    """Frame-pixels-per-CSS-pixel for *event*: from the frame the card showed.

    The screencast's pictures differ in width (the ladder's rungs, the sharp
    settle still), so the stream's latest scale can belong to a frame the card
    never drew: a click mapped against a 1280 px frame and divided by a 640 px
    frame's scale lands twice as far down and right, off the field or off the
    page. The card sends the width it mapped against; with the viewport's CSS
    width that gives the exact ratio. *fallback* (the stream's own) is for a
    card that sends no width.
    """
    if event.frame_w > 0 and device_width > 0:
        return event.frame_w / device_width
    return fallback


def _int_field(event: dict[str, Any], name: str, *, lo: int, hi: int, default: int = 0) -> int:
    value = event.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputEventError("invalid_input_event", f"{name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        # JSON bodies may carry NaN / Infinity; int() of those raises, not refuses.
        raise InputEventError("invalid_input_event", f"{name} must be a finite number")
    value = int(value)
    if value < lo or value > hi:
        raise InputEventError("invalid_input_event", f"{name} out of range")
    return value


def _str_field(event: dict[str, Any], name: str, *, max_len: int) -> str:
    value = event.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_len:
        raise InputEventError("invalid_input_event", f"{name} must be a short string")
    return value


def parse_input_event(raw: Any) -> InputEvent:
    """Bound every field of a card event; refuse anything outside the contract."""
    if not isinstance(raw, dict):
        raise InputEventError("invalid_input_event", "event must be an object")
    kind = raw.get("type")
    if kind not in ("mouse", "wheel", "key"):
        raise InputEventError("invalid_input_event", "type must be mouse, wheel or key")
    modifiers = _int_field(raw, "modifiers", lo=0, hi=MAX_MODIFIERS)
    if kind == "key":
        action = raw.get("action", "press")
        if action not in _KEY_ACTIONS:
            raise InputEventError("invalid_input_event", "key action must be press, down or up")
        key = _str_field(raw, "key", max_len=MAX_KEY_CHARS)
        code = _str_field(raw, "code", max_len=MAX_KEY_CHARS)
        text = _str_field(raw, "text", max_len=MAX_TEXT_CHARS)
        if not key and not text:
            raise InputEventError("invalid_input_event", "key event needs key or text")
        return InputEvent(
            type="key", action=str(action), key=key, code=code, text=text, modifiers=modifiers
        )
    x = _int_field(raw, "x", lo=0, hi=MAX_COORD)
    y = _int_field(raw, "y", lo=0, hi=MAX_COORD)
    frame_w = _int_field(raw, "w", lo=0, hi=MAX_COORD)
    if kind == "wheel":
        return InputEvent(
            type="wheel",
            action="wheel",
            x=x,
            y=y,
            dx=_int_field(raw, "dx", lo=-MAX_DELTA, hi=MAX_DELTA),
            dy=_int_field(raw, "dy", lo=-MAX_DELTA, hi=MAX_DELTA),
            modifiers=modifiers,
            frame_w=frame_w,
        )
    button = raw.get("button", "none") or "none"
    if button not in _BUTTONS:
        raise InputEventError("invalid_input_event", "unknown mouse button")
    action = raw.get("action")
    if action is None:
        action = "move" if button == "none" else "click"
    if action not in _MOUSE_ACTIONS:
        raise InputEventError("invalid_input_event", "mouse action must be click, move, down or up")
    if action != "move" and button == "none":
        raise InputEventError("invalid_input_event", "a mouse press needs a button")
    # A move carries no click count (the card sends 0); a press or release carries 1-3.
    if action == "move":
        clicks = _int_field(raw, "clicks", lo=0, hi=MAX_CLICKS, default=0)
    else:
        clicks = _int_field(raw, "clicks", lo=1, hi=MAX_CLICKS, default=1)
    return InputEvent(
        type="mouse",
        action=str(action),
        x=x,
        y=y,
        button=str(button),
        clicks=clicks,
        modifiers=modifiers,
        frame_w=frame_w,
    )


#: CDP ``buttons`` bitmask for the button a move reports as held.
_BUTTONS_MASK = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}


def _css(value: int, scale: float) -> float:
    """A frame-pixel coordinate as viewport CSS pixels, to a hundredth."""
    return round(value / scale, 2) if scale > 0 else float(value)


def cdp_calls(event: InputEvent, scale: float = 1.0) -> list[tuple[str, dict[str, Any]]]:
    """The CDP commands one validated event becomes, in order. Pure.

    *scale* is the stream's frame-pixels-per-CSS-pixel: the card measures in
    JPEG pixels, ``Input.dispatch*`` takes viewport CSS pixels, and a screencast
    at ``maxWidth`` below the viewport makes the two differ. Positions and wheel
    deltas are divided by it here, at the one place the event becomes CDP.
    """
    if event.type == "wheel":
        return [
            (
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": _css(event.x, scale),
                    "y": _css(event.y, scale),
                    "deltaX": _css(event.dx, scale),
                    "deltaY": _css(event.dy, scale),
                    "modifiers": event.modifiers,
                },
            )
        ]
    if event.type == "mouse":
        base = {"x": _css(event.x, scale), "y": _css(event.y, scale), "modifiers": event.modifiers}
        if event.action == "move":
            moved: dict[str, Any] = {"type": "mouseMoved", **base}
            if event.button != "none":
                # A move with a button held is a drag; Chrome needs both fields.
                moved["button"] = event.button
                moved["buttons"] = _BUTTONS_MASK.get(event.button, 0)
            return [("Input.dispatchMouseEvent", moved)]
        pressed = {"button": event.button, "clickCount": event.clicks, **base}
        if event.action == "down":
            return [("Input.dispatchMouseEvent", {"type": "mousePressed", **pressed})]
        if event.action == "up":
            return [("Input.dispatchMouseEvent", {"type": "mouseReleased", **pressed})]
        calls: list[tuple[str, dict[str, Any]]] = []
        for n in range(1, event.clicks + 1):
            p = {**pressed, "clickCount": n}
            calls.append(("Input.dispatchMouseEvent", {"type": "mousePressed", **p}))
            calls.append(("Input.dispatchMouseEvent", {"type": "mouseReleased", **p}))
        return calls
    if not event.key and event.text:
        # Text with no key behind it: what a phone's soft keyboard or an IME
        # commits. It goes in as typed, the way a paste would.
        return [] if event.action == "up" else [("Input.insertText", {"text": event.text})]
    base_key: dict[str, Any] = {"modifiers": event.modifiers}
    if event.key:
        base_key["key"] = event.key
    if event.code:
        base_key["code"] = event.code
    vk = virtual_key_code(event.key, event.code)
    if vk:
        base_key["windowsVirtualKeyCode"] = vk
        base_key["nativeVirtualKeyCode"] = vk
    if event.code.startswith("Numpad"):
        base_key["location"] = 3
    down: dict[str, Any] = {**base_key}
    # Ctrl/Meta chords type nothing (Ctrl+A selects); Enter types "\r" otherwise.
    text = event.text or ("" if event.modifiers & 6 else _KEY_TEXT.get(event.key, ""))
    if text:
        down["type"] = "keyDown"
        down["text"] = text
        down["unmodifiedText"] = text
    else:
        down["type"] = "rawKeyDown"
    up = {"type": "keyUp", **base_key}
    if event.action == "down":
        return [("Input.dispatchKeyEvent", down)]
    if event.action == "up":
        return [("Input.dispatchKeyEvent", up)]
    return [("Input.dispatchKeyEvent", down), ("Input.dispatchKeyEvent", up)]


#: Most keys and buttons one takeover tracks as held. A hand-back sends one release
#: per entry under one short deadline, so the count bounds how long it takes; a
#: press past the cap reaches the page but is not tracked.
MAX_HELD_INPUTS = 32

_KEY_DOWN_TYPES = frozenset({"keyDown", "rawKeyDown"})


class HeldInput:
    """The keys and mouse buttons the person's forwarded events left down.

    The card sends a press and its release as separate events, so a hand-back can
    land between the two: the release then reaches a lock that is already open and
    is refused, and the page would keep the key or button down. :meth:`note_call`
    follows every CDP command the page acknowledged, one transition at a time, so a
    click whose press went out and whose release failed still counts as down.
    :meth:`drain` returns the matching releases for whatever is still down, which
    the hub sends before it gives the lock up.
    """

    def __init__(self) -> None:
        self._keys: dict[str, InputEvent] = {}
        self._buttons: dict[str, InputEvent] = {}

    def __bool__(self) -> bool:
        return bool(self._keys or self._buttons)

    def __len__(self) -> int:
        return len(self._keys) + len(self._buttons)

    def note_call(self, event: InputEvent, method: str, params: dict[str, Any]) -> None:
        """Follow one CDP command of *event* that the page acknowledged."""
        kind = params.get("type")
        if method == "Input.dispatchKeyEvent":
            ident = event.code or event.key
            if kind in _KEY_DOWN_TYPES:
                if ident in self._keys or len(self) < MAX_HELD_INPUTS:
                    self._keys[ident] = InputEvent(
                        type="key", action="down", key=event.key, code=event.code
                    )
            elif kind == "keyUp":
                self._keys.pop(ident, None)
            return
        if method != "Input.dispatchMouseEvent":
            return  # Input.insertText holds nothing down
        if kind == "mousePressed":
            if event.button in self._buttons or len(self) < MAX_HELD_INPUTS:
                self._buttons[event.button] = event
        elif kind == "mouseReleased":
            self._buttons.pop(event.button, None)
        elif kind == "mouseMoved":
            # A release goes where the pointer is, not where the press was.
            for name, held in self._buttons.items():
                self._buttons[name] = replace(held, x=event.x, y=event.y, frame_w=event.frame_w)

    def drain(self) -> list[InputEvent]:
        """The release for everything still down (keys, then buttons); forgets it all."""
        ups = [
            InputEvent(type="key", action="up", key=e.key, code=e.code) for e in self._keys.values()
        ]
        ups += [
            InputEvent(
                type="mouse",
                action="up",
                x=e.x,
                y=e.y,
                button=e.button,
                clicks=1,
                frame_w=e.frame_w,
            )
            for e in self._buttons.values()
        ]
        self._keys.clear()
        self._buttons.clear()
        return ups


async def dispatch_input(
    relay: Relay,
    session_id: str,
    event: dict[str, Any],
    *,
    scale: float = 1.0,
    held: HeldInput | None = None,
) -> int:
    """Validate one card event and send it to the page. Returns the CDP calls made.

    *scale* is the stream's frame-to-CSS ratio (see :func:`cdp_calls`). Raises
    :class:`InputEventError` for a malformed event before anything is sent. Each
    command the page acknowledges is noted on *held* as it lands, so a failure
    part way through a click or a key press leaves *held* matching the page.
    """
    parsed = parse_input_event(event)
    calls = cdp_calls(parsed, scale)
    for method, params in calls:
        await relay.send(method, params, session_id)
        if held is not None:
            held.note_call(parsed, method, params)
    return len(calls)


#: The page's focused field after a press: its kind, never its value. Follows
#: open shadow roots and same-origin frames; a cross-origin frame cannot be
#: looked into and answers "frame" (it may well hold the field the person
#: tapped, so a phone keeps its keyboard up for it).
_FOCUS_JS = (
    "(()=>{let e=document.activeElement;"
    "for(let i=0;i<8&&e;i++){"
    "if(e.shadowRoot&&e.shadowRoot.activeElement){e=e.shadowRoot.activeElement;continue;}"
    "if(e.tagName==='IFRAME'||e.tagName==='FRAME'){let d=null;"
    "try{d=e.contentDocument;}catch(_){d=null;}"
    "if(!d)return {editable:true,kind:'frame',mode:''};"
    "if(d.activeElement&&d.activeElement!==d.body){e=d.activeElement;continue;}}"
    "break;}"
    "if(!e)return {editable:false,kind:'',mode:''};"
    "const mode=(e.getAttribute&&e.getAttribute('inputmode'))||'';"
    "if(e.isContentEditable)return {editable:true,kind:'text',mode:mode};"
    "if(e.tagName==='TEXTAREA')return {editable:!e.readOnly&&!e.disabled,kind:'textarea',mode:mode};"
    "if(e.tagName==='INPUT'){const t=(e.type||'text').toLowerCase();"
    "return {editable:!e.readOnly&&!e.disabled&&FIELDS.includes(t),kind:t,mode:mode};}"
    "return {editable:false,kind:'',mode:''};})()"
).replace("FIELDS", "['text','search','url','tel','email','password','number']")

#: The kinds and ``inputmode`` values the card may hear back; anything else is "".
_FOCUS_KINDS = frozenset(
    {"text", "search", "url", "tel", "email", "password", "number", "textarea", "frame"}
)
_INPUT_MODES = frozenset({"text", "decimal", "numeric", "tel", "search", "email", "url", "none"})


async def focused_field(relay: Relay, session_id: str) -> dict[str, Any]:
    """Whether the page's focused element takes typing, and what kind it is.

    The card asks after a press so a phone can raise its keyboard only for a
    field (and the right keyboard: digits for a number box). Answers
    ``{"editable": bool, "kind": str, "mode": str}``; any failure is "not editable".
    """
    try:
        reply = await relay.send(
            "Runtime.evaluate", {"expression": _FOCUS_JS, "returnByValue": True}, session_id
        )
        value = reply.get("result", {}).get("value")
    except Exception:  # noqa: BLE001 - a focus probe never fails the input it follows
        value = None
    if not isinstance(value, dict):
        return {"editable": False, "kind": "", "mode": ""}
    kind = value.get("kind") if value.get("kind") in _FOCUS_KINDS else ""
    mode = value.get("mode") if value.get("mode") in _INPUT_MODES else ""
    return {"editable": value.get("editable") is True and bool(kind), "kind": kind, "mode": mode}


__all__ = [
    "AGENT",
    "HeldInput",
    "InputEvent",
    "InputEventError",
    "InputLock",
    "MAX_HELD_INPUTS",
    "USER",
    "USER_IN_CONTROL",
    "cdp_calls",
    "dispatch_input",
    "focused_field",
    "parse_input_event",
    "virtual_key_code",
]
