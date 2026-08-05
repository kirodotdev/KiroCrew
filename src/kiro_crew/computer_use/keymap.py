"""US-layout keycode / modifier tables and the ``press_key`` spec parser.

Pure data plus parsing — no ctypes, no platform calls, no I/O. The numbers are
Carbon virtual keycodes (``kVK_*`` from ``HIToolbox/Events.h``) and CoreGraphics
event-flag masks (``kCGEventFlagMask*``); both are stable ABI constants, which
is why they can live in a platform-free module and be unit-tested on Linux CI.

Every synthesized key event must carry an EXPLICIT flag mask built from zero and
OR-ed with only the modifiers the caller asked for. Skipping that step made a
live prototype type ``' I Abc'`` when asked for ``abc``, because the events
inherited the user's real modifier state at post time.
"""

from __future__ import annotations

from kiro_crew.computer_use.types import KeyParseError

# ── CoreGraphics event flag masks (kCGEventFlagMask*) ──
FLAG_ALPHA_SHIFT = 0x00010000
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_ALTERNATE = 0x00080000
FLAG_COMMAND = 0x00100000
FLAG_SECONDARY_FN = 0x00800000

# Modifier spellings a model might plausibly emit, all normalized to one mask.
# ``super``/``meta``/``win`` map to Command so a cross-platform prompt still
# works; ``fn`` is included because some app shortcuts require it.
MODIFIERS: dict[str, int] = {
    "cmd": FLAG_COMMAND,
    "command": FLAG_COMMAND,
    "super": FLAG_COMMAND,
    "meta": FLAG_COMMAND,
    "win": FLAG_COMMAND,
    "shift": FLAG_SHIFT,
    "option": FLAG_ALTERNATE,
    "opt": FLAG_ALTERNATE,
    "alt": FLAG_ALTERNATE,
    "control": FLAG_CONTROL,
    "ctrl": FLAG_CONTROL,
    "fn": FLAG_SECONDARY_FN,
    "function": FLAG_SECONDARY_FN,
    "capslock": FLAG_ALPHA_SHIFT,
}

# ── Virtual keycodes (kVK_*), full US layout ──
# Keys are lowercase so lookup is case-insensitive after normalization. The
# named keys carry several aliases each (``esc``/``escape``, ``enter``/
# ``return``, ``pgup``/``pageup``, …) because models are inconsistent and a
# ``KeyParseError`` for a spelling difference is a pointless failure.
KEYCODES: dict[str, int] = {
    # letters
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35,
    "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    # digits
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
    "5": 23, "6": 22, "7": 26, "8": 28, "9": 25,
    # punctuation (unshifted glyphs, plus a word alias for each)
    "-": 27, "minus": 27,
    "=": 24, "equal": 24, "equals": 24,
    "[": 33, "leftbracket": 33,
    "]": 30, "rightbracket": 30,
    "\\": 42, "backslash": 42,
    ";": 41, "semicolon": 41,
    "'": 39, "quote": 39, "apostrophe": 39,
    ",": 43, "comma": 43,
    ".": 47, "period": 47, "dot": 47,
    "/": 44, "slash": 44,
    "`": 50, "grave": 50, "backtick": 50,
    # whitespace / editing
    "space": 49, " ": 49, "spacebar": 49,
    "return": 36, "enter": 36,
    "tab": 48,
    "delete": 51, "backspace": 51,
    "forwarddelete": 117, "del": 117,
    "escape": 53, "esc": 53,
    "help": 114, "insert": 114,
    # navigation
    "left": 123, "right": 124, "down": 125, "up": 126,
    "arrowleft": 123, "arrowright": 124, "arrowdown": 125, "arrowup": 126,
    "home": 115, "end": 119,
    "pageup": 116, "pgup": 116,
    "pagedown": 121, "pgdn": 121, "pgdown": 121,
    # function keys
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79,
    "f19": 80, "f20": 90,
    # keypad
    "keypad0": 82, "keypad1": 83, "keypad2": 84, "keypad3": 85, "keypad4": 86,
    "keypad5": 87, "keypad6": 88, "keypad7": 89, "keypad8": 91, "keypad9": 92,
    "keypadclear": 71, "keypaddecimal": 65, "keypaddivide": 75,
    "keypadenter": 76, "keypadequals": 81, "keypadminus": 78,
    "keypadmultiply": 67, "keypadplus": 69,
    # media / volume (bare keycodes; no special HID handling needed)
    "mute": 74, "volumedown": 73, "volumeup": 72,
}  # fmt: skip

# Characters reachable only with Shift on a US layout. Used when text has to be
# typed as keystrokes (no addressable element to set a value on): the keystroke
# for ``$`` is Shift+4, and the shift flag must be applied to that event only.
SHIFTED_CHARS: dict[str, str] = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "_": "-",
    "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'",
    "<": ",", ">": ".", "?": "/",
}  # fmt: skip

# Separators accepted between modifiers and the key in a spec string. ``-`` is
# NOT one of them: it is itself a key name, so ``cmd-`` would be ambiguous.
_SPEC_SEPARATOR = "+"


def parse_key(spec: str) -> tuple[int, int]:
    """Parse a key spec like ``"cmd+shift+a"`` into ``(keycode, flag_mask)``.

    The mask is built from ZERO — never from the current modifier state — so a
    synthesized event carries exactly the modifiers that were requested.

    Raises :class:`KeyParseError` for an empty spec, an unknown modifier, an
    unknown key, or a spec with no key part (``"cmd+"``). Refusing loudly is
    correct here: silently dropping an unrecognized modifier would send a
    DIFFERENT keystroke than the caller asked for, into a live application.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise KeyParseError("empty key spec")
    raw = spec.strip()

    # A bare ``+`` (or a spec ending in one, e.g. ``shift++``) means the plus
    # key itself, which is Shift+equal on a US layout. Handle it before
    # splitting, or the split would yield an empty key part.
    parts: list[str] = []
    if raw == _SPEC_SEPARATOR:
        parts = ["+"]
    elif raw.endswith(_SPEC_SEPARATOR):
        parts = [p for p in raw[:-1].split(_SPEC_SEPARATOR) if p] + ["+"]
    else:
        parts = raw.split(_SPEC_SEPARATOR)

    tokens = [p.strip() for p in parts if p.strip()]
    if not tokens:
        raise KeyParseError(f"no key in spec {spec!r}")

    flags = 0
    for token in tokens[:-1]:
        mask = MODIFIERS.get(token.lower())
        if mask is None:
            raise KeyParseError(f"unknown modifier {token!r} in {spec!r}")
        flags |= mask

    key = tokens[-1]
    keycode, extra = _resolve_key(key)
    if keycode is None:
        raise KeyParseError(f"unknown key {key!r} in {spec!r}")
    return keycode, flags | extra


def char_keystroke(char: str) -> tuple[int, int] | None:
    """Return ``(keycode, flag_mask)`` for a single printable character.

    For the keystroke-synthesis path (typing text into a target that exposes no
    settable value). Returns ``None`` for a character the US layout cannot
    reach with one keystroke — callers must skip it rather than substitute
    something else, since typing the wrong character into a live app is worse
    than typing nothing.
    """
    if not char:
        return None
    keycode, flags = _resolve_key(char)
    return None if keycode is None else (keycode, flags)


def _resolve_key(key: str) -> tuple[int | None, int]:
    """Resolve one key token to ``(keycode | None, implied_flags)``.

    Implied flags cover the shifted glyphs (``$`` -> Shift+4) and uppercase
    letters (``A`` -> Shift+a); a caller-supplied ``shift+`` simply ORs into the
    same bit, so both spellings produce an identical event.
    """
    if not key:
        return None, 0
    if key in SHIFTED_CHARS:
        return KEYCODES.get(SHIFTED_CHARS[key]), FLAG_SHIFT
    if len(key) == 1 and key.isalpha() and key.isupper():
        return KEYCODES.get(key.lower()), FLAG_SHIFT
    return KEYCODES.get(key.lower()), 0
