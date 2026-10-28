"""Input half of the Linux computer-use driver: XTest through ctypes.

X11 has no per-window input route that applications honour. ``XSendEvent`` marks
the event synthetic and GTK, Qt and Chromium ignore it for most purposes, so the
only reliable keyboard and pointer route is XTest, which is GLOBAL: a keystroke goes
to whatever holds focus and a click moves the operator's real cursor. That is the
same situation as Windows ``SendInput``, and the driver treats it the same way
(focus is verified before typing, the pointer route must be named explicitly).

Everything here is an in-process ``libXtst`` call on the reader's display
connection. No helper binary is spawned, so the package keeps its "nothing in the
observation/input path shells out" property.

Typing a character the current keyboard map cannot produce (an umlaut on a US map,
any non-Latin script) binds its keysym to a spare keycode for the duration of the
press and restores the keycode afterwards, the same technique ``xdotool type`` uses.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time
from typing import Iterable, Sequence

from kiro_crew.computer_use import linux_x11
from kiro_crew.computer_use.types import ComputerUseUnsupported

#: Pause between the events of one character or click, so a slow client's input
#: queue sees discrete presses rather than a burst it may coalesce.
_EVENT_GAP_SECS = 0.006
_CLICK_GAP_SECS = 0.08
_DRAG_STEP_SECS = 0.01
#: How long a remapped keycode is given to propagate (MappingNotify) before use.
_REMAP_SETTLE_SECS = 0.03
#: How long ``activate_window`` waits for the window manager to honour the request.
_ACTIVATE_TIMEOUT_SECS = 1.0

_NO_SYMBOL = 0
_CLIENT_MESSAGE = 33
_SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # SubstructureNotify | SubstructureRedirect

#: Canonical ``keymap`` key names -> X keysym names. Letters, digits and the space key
#: map to themselves and are handled in :func:`keysym_for`.
_KEYSYMS: dict[str, str] = {
    "return": "Return",
    "tab": "Tab",
    "space": "space",
    "spacebar": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "forwarddelete": "Delete",
    "escape": "Escape",
    "help": "Help",
    "insert": "Insert",
    "arrowleft": "Left",
    "arrowright": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "minus": "minus",
    "equals": "equal",
    "leftbracket": "bracketleft",
    "rightbracket": "bracketright",
    "backslash": "backslash",
    "semicolon": "semicolon",
    "apostrophe": "apostrophe",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
    "backtick": "grave",
    "keypadclear": "KP_Begin",
    "keypaddecimal": "KP_Decimal",
    "keypaddivide": "KP_Divide",
    "keypadenter": "KP_Enter",
    "keypadequals": "KP_Equal",
    "keypadminus": "KP_Subtract",
    "keypadmultiply": "KP_Multiply",
    "keypadplus": "KP_Add",
    "mute": "XF86AudioMute",
    "volumedown": "XF86AudioLowerVolume",
    "volumeup": "XF86AudioRaiseVolume",
}

#: Canonical modifier names -> the keysym held for them. ``cmd`` already arrived here
#: as ``ctrl`` (``keymap.MODIFIER_ALIASES`` maps intent: cmd+s means Save), and ``win``
#: is the physical logo key, which X calls Super.
_MODIFIERS: dict[str, str] = {
    "ctrl": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "win": "Super_L",
}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")
_UNHOLDABLE: dict[str, str] = {
    "fn": "the fn modifier is handled by keyboard firmware and never reaches X11",
    "capslock": "capslock is a lock, not a held modifier; send it as its own key press",
}

_BUTTONS = {"left": 1, "middle": 2, "right": 3}


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


class _XEvent(ctypes.Union):
    # The C union is 24 longs wide; XSendEvent copies the whole thing.
    _fields_ = [("xclient", _XClientMessageEvent), ("pad", ctypes.c_long * 24)]


_xtst: "ctypes.CDLL | None" = None
_bound = False


def _bind() -> "tuple[ctypes.CDLL, ctypes.CDLL]":
    """``(libX11, libXtst)`` with the input entry points typed. Raises OSError."""
    global _xtst, _bound
    x11 = linux_x11._ensure_lib()
    if _xtst is None:
        _xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
        for name, args in (
            ("XTestFakeKeyEvent", [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]),
            (
                "XTestFakeButtonEvent",
                [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong],
            ),
            (
                "XTestFakeMotionEvent",
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong],
            ),
        ):
            fn = getattr(_xtst, name)
            fn.argtypes = args
            fn.restype = ctypes.c_int
    if not _bound:
        x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x11.XStringToKeysym.restype = ctypes.c_ulong
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XkbKeycodeToKeysym.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.c_int,
        ]
        x11.XkbKeycodeToKeysym.restype = ctypes.c_ulong
        x11.XDisplayKeycodes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        x11.XGetKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        x11.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        x11.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
        ]  # fmt: skip
        x11.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.c_void_p,
        ]
        x11.XSendEvent.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(_XkbStateRec)]
        x11.XkbGetState.restype = ctypes.c_int
        _bound = True
    return x11, _xtst


class _XkbStateRec(ctypes.Structure):
    _fields_ = [
        ("group", ctypes.c_ubyte),
        ("locked_group", ctypes.c_ubyte),
        ("base_group", ctypes.c_ushort),
        ("latched_group", ctypes.c_ushort),
        ("mods", ctypes.c_ubyte),
        ("base_mods", ctypes.c_ubyte),
        ("latched_mods", ctypes.c_ubyte),
        ("locked_mods", ctypes.c_ubyte),
        ("compat_state", ctypes.c_ubyte),
        ("grab_mods", ctypes.c_ubyte),
        ("compat_grab_mods", ctypes.c_ubyte),
        ("lookup_mods", ctypes.c_ubyte),
        ("compat_lookup_mods", ctypes.c_ubyte),
        ("ptr_buttons", ctypes.c_ushort),
    ]


_XKB_USE_CORE_KBD = 0x0100
_LOCK_MASK = 0x02


def _kb_state(x11: ctypes.CDLL, dpy: int) -> "tuple[int, int]":
    """``(active group, locked modifiers)`` of the core keyboard. ``(0, 0)`` on failure."""
    state = _XkbStateRec()
    if x11.XkbGetState(dpy, _XKB_USE_CORE_KBD, ctypes.byref(state)) != 0:
        return 0, 0
    return int(state.group), int(state.locked_mods)


def caps_lock_conflict(text: str) -> bool:
    """True when Caps Lock is on and *text* has a cased letter it would flip.

    XTest sends key PRESSES, not characters, so a locked Caps Lock turns a requested
    ``a`` into ``A`` while the tool reports success. The driver refuses instead of
    typing the wrong text; toggling the user's lock state behind their back is worse.
    """
    if not any(ch.lower() != ch.upper() for ch in text):
        return False
    x11, _xtst = _need()
    with linux_x11._lock:
        _group, locked = _kb_state(x11, linux_x11._dpy())
    return bool(locked & _LOCK_MASK)


def available() -> "tuple[bool, str]":
    try:
        _bind()
    except linux_x11.X11Unavailable as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"libXtst is not loadable ({exc}); install libxtst6"
    return True, ""


def _need() -> "tuple[ctypes.CDLL, ctypes.CDLL]":
    try:
        return _bind()
    except linux_x11.X11Unavailable as exc:
        raise ComputerUseUnsupported(str(exc)) from exc
    except OSError as exc:
        raise ComputerUseUnsupported(f"libXtst is not loadable ({exc}); install libxtst6") from exc


def keysym_for(key: str) -> str:
    if len(key) == 1 and key.isalnum():
        return key
    sym = _KEYSYMS.get(key)
    if sym:
        return sym
    if key.startswith("keypad") and key[6:].isdigit():
        return f"KP_{key[6:]}"
    if key.startswith("f") and key[1:].isdigit():
        return f"F{key[1:]}"
    raise ComputerUseUnsupported(f"key {key!r} has no X11 keysym mapping")


def chord(key: str, modifiers: Iterable[str]) -> str:
    """The chord as ``Control_L+Shift_L+Return``: modifiers first, in a stable order."""
    mods = set(modifiers)
    for name in mods:
        if name in _UNHOLDABLE:
            raise ComputerUseUnsupported(_UNHOLDABLE[name])
        if name not in _MODIFIERS:
            raise ComputerUseUnsupported(f"modifier {name!r} has no X11 mapping")
    parts = [_MODIFIERS[name] for name in _MODIFIER_ORDER if name in mods]
    parts.append(keysym_for(key))
    return "+".join(parts)


def char_keysym(ch: str) -> int:
    """The X keysym for one character: Latin-1 is its code point, the rest Unicode."""
    code = ord(ch)
    if ch == "\n":
        return 0xFF0D  # Return
    if ch == "\t":
        return 0xFF09  # Tab
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 | code


def _sym(x11: ctypes.CDLL, name: str) -> int:
    sym = int(x11.XStringToKeysym(name.encode()))
    if sym == _NO_SYMBOL:
        raise ComputerUseUnsupported(f"X11 has no keysym named {name!r}")
    return sym


def _key(xtst: ctypes.CDLL, dpy: int, keycode: int, down: bool) -> None:
    xtst.XTestFakeKeyEvent(dpy, keycode, 1 if down else 0, 0)


def _spare_keycode(x11: ctypes.CDLL, dpy: int) -> int:
    """A keycode with no keysym bound at all, scanning from the top. 0 when none."""
    lo, hi = ctypes.c_int(), ctypes.c_int()
    x11.XDisplayKeycodes(dpy, ctypes.byref(lo), ctypes.byref(hi))
    per = ctypes.c_int()
    count = hi.value - lo.value + 1
    table = x11.XGetKeyboardMapping(dpy, lo.value, count, ctypes.byref(per))
    if not table:
        return 0
    try:
        for offset in range(count - 1, -1, -1):
            row = table[offset * per.value : (offset + 1) * per.value]
            if all(s == _NO_SYMBOL for s in row):
                return lo.value + offset
    finally:
        x11.XFree(table)
    return 0


def _remap(x11: ctypes.CDLL, dpy: int, keycode: int, sym: int) -> None:
    syms = (ctypes.c_ulong * 2)(sym, sym)
    x11.XChangeKeyboardMapping(dpy, keycode, 2, syms, 1)
    x11.XSync(dpy, 0)


def _press_sym(
    x11: ctypes.CDLL, xtst: ctypes.CDLL, dpy: int, sym: int, spare: "list[int]", group: int = 0
) -> None:
    """Press and release whatever produces *sym*, shifting or remapping as needed.

    Levels are read in the ACTIVE keyboard group: a user switched to a second layout
    gets that layout's symbols from a key press, so group 0 would pick wrong keys.
    """
    keycode = int(x11.XKeysymToKeycode(dpy, sym))
    shift = False
    if keycode:
        if int(x11.XkbKeycodeToKeysym(dpy, keycode, group, 0)) == sym:
            pass
        elif int(x11.XkbKeycodeToKeysym(dpy, keycode, group, 1)) == sym:
            shift = True
        else:
            keycode = 0  # only reachable through AltGr or another group: remap instead
    if not keycode:
        if not spare[0]:
            spare[0] = _spare_keycode(x11, dpy)
            if not spare[0]:
                raise RuntimeError("no spare X keycode to bind an unmapped character to")
        keycode = spare[0]
        _remap(x11, dpy, keycode, sym)
        time.sleep(_REMAP_SETTLE_SECS)
    shift_code = int(x11.XKeysymToKeycode(dpy, 0xFFE1)) if shift else 0  # Shift_L
    if shift_code:
        _key(xtst, dpy, shift_code, True)
    _key(xtst, dpy, keycode, True)
    _key(xtst, dpy, keycode, False)
    if shift_code:
        _key(xtst, dpy, shift_code, False)
    x11.XSync(dpy, 0)
    time.sleep(_EVENT_GAP_SECS)


def type_text(text: str) -> None:
    x11, xtst = _need()
    spare = [0]
    with linux_x11._lock:
        dpy = linux_x11._dpy()
        group, _locked = _kb_state(x11, dpy)
        try:
            for ch in text:
                _press_sym(x11, xtst, dpy, char_keysym(ch), spare, group)
        finally:
            if spare[0]:
                # Give the last press time to be read, then unbind the spare keycode.
                time.sleep(_REMAP_SETTLE_SECS)
                _remap(x11, dpy, spare[0], _NO_SYMBOL)


def send_chord(spec: str) -> None:
    """Hold each modifier of ``Control_L+Return``-style *spec*, tap the last key."""
    x11, xtst = _need()
    names = spec.split("+")
    with linux_x11._lock:
        dpy = linux_x11._dpy()
        codes = []
        for name in names:
            code = int(x11.XKeysymToKeycode(dpy, _sym(x11, name)))
            if not code:
                raise ComputerUseUnsupported(f"no key on this keyboard map produces {name!r}")
            codes.append(code)
        held: list[int] = []
        try:
            for code in codes[:-1]:
                _key(xtst, dpy, code, True)
                held.append(code)
            _key(xtst, dpy, codes[-1], True)
            _key(xtst, dpy, codes[-1], False)
        finally:
            for code in reversed(held):
                _key(xtst, dpy, code, False)
            x11.XSync(dpy, 0)


def activate_window(xid: int) -> None:
    """Ask the window manager to raise and focus *xid* (EWMH ``_NET_ACTIVE_WINDOW``)."""
    x11, _ = _need()
    with linux_x11._lock:
        dpy = linux_x11._dpy()
        event = _XEvent()
        msg = event.xclient
        msg.type = _CLIENT_MESSAGE
        msg.send_event = 1
        msg.display = dpy
        msg.window = xid
        msg.message_type = linux_x11._atom("_NET_ACTIVE_WINDOW")
        msg.format = 32
        msg.data[0] = 2  # source indication: a pager/tool, so the WM does not refuse it
        msg.data[1] = 0  # CurrentTime
        x11.XSendEvent(dpy, linux_x11._root(), 0, _SUBSTRUCTURE_MASK, ctypes.byref(event))
        x11.XFlush(dpy)
    deadline = time.monotonic() + _ACTIVATE_TIMEOUT_SECS
    while linux_x11.active_window() != xid and time.monotonic() < deadline:
        time.sleep(0.02)


def _move(xtst: ctypes.CDLL, dpy: int, x: float, y: float) -> None:
    xtst.XTestFakeMotionEvent(dpy, -1, int(round(x)), int(round(y)), 0)


def click(x: float, y: float, *, button: str, count: int) -> None:
    x11, xtst = _need()
    b = _BUTTONS[button]
    with linux_x11._lock:
        dpy = linux_x11._dpy()
        _move(xtst, dpy, x, y)
        x11.XSync(dpy, 0)
        for i in range(count):
            if i:
                time.sleep(_CLICK_GAP_SECS)
            xtst.XTestFakeButtonEvent(dpy, b, 1, 0)
            xtst.XTestFakeButtonEvent(dpy, b, 0, 0)
            x11.XSync(dpy, 0)


def drag(points: "Sequence[tuple[float, float]]", *, button: str) -> None:
    if len(points) < 2:
        raise ValueError("a drag needs at least two points")
    x11, xtst = _need()
    b = _BUTTONS[button]
    with linux_x11._lock:
        dpy = linux_x11._dpy()
        _move(xtst, dpy, *points[0])
        x11.XSync(dpy, 0)
        xtst.XTestFakeButtonEvent(dpy, b, 1, 0)
        try:
            for px, py in points[1:]:
                _move(xtst, dpy, px, py)
                x11.XSync(dpy, 0)
                time.sleep(_DRAG_STEP_SECS)
        finally:
            xtst.XTestFakeButtonEvent(dpy, b, 0, 0)
            x11.XSync(dpy, 0)
