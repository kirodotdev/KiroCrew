"""The ``press_key`` spec vocabulary: a platform-free grammar over macOS codes.

Two layers, and the split is load-bearing rather than tidiness:

* **The grammar is platform-free.** :func:`parse_spec` resolves a spec string
  (``"cmd+shift+a"``) into a :class:`KeySpec` of ABSTRACT names, and
  :data:`KEY_NAMES` / :data:`MODIFIER_NAMES` are the vocabulary every platform
  shares. ``tools._perform`` validates through this layer inside the dispatch
  chokepoint, so an unknown key or modifier is refused BEFORE any driver
  synthesizes a keystroke into a live application — on every platform, including
  one whose numeric tables live elsewhere.
* **The numbers below are macOS-only.** They are Carbon virtual keycodes
  (``kVK_*`` from ``HIToolbox/Events.h``) and CoreGraphics event-flag masks
  (``kCGEventFlagMask*``). They are stable ABI constants and need no framework to
  read, which is why they are unit-testable on a Linux runner — but they are not
  portable, and :func:`parse_key`'s ``(keycode, flags)`` pair is a macOS shape. A
  second platform MUST add its own table keyed off :class:`KeySpec` rather than
  reinterpret this one: a Windows ``VK_*`` code and a ``kVK_*`` code are different
  numbers for the same key, and a tuple that silently means different things per
  OS is how a wrong keystroke reaches a live window.

Every synthesized key event must carry an EXPLICIT flag mask built from zero and
OR-ed with only the modifiers the caller asked for. Skipping that step made a
live prototype type ``' I Abc'`` when asked for ``abc``, because the events
inherited the user's real modifier state at post time.
"""

from __future__ import annotations

from dataclasses import dataclass

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


#: The abstract modifier vocabulary — the spellings, without any platform's
#: numeric mask. A model is inconsistent about these, and refusing a spelling
#: difference is a pointless failure, so every plausible alias is admitted.
#: ``super``/``meta``/``win`` are deliberately listed as their own names rather
#: than pre-folded into one: what they MEAN is a per-platform decision (Command
#: on macOS, and a Windows table must decide for itself whether ``cmd+s`` means
#: the Windows logo key or Ctrl — the model writing it means SAVE).
MODIFIER_NAMES: frozenset[str] = frozenset(MODIFIERS)

#: Modifier alias -> canonical modifier name. Hand-written rather than derived from
#: :data:`MODIFIERS` by mask identity, because that mapping is macOS's ANSWER and
#: not the question: ``super``/``meta``/``win`` share ``FLAG_COMMAND`` there, so a
#: mask-derived table would canonicalize ``win`` to ``cmd`` and pre-decide for
#: Windows that the logo key means Ctrl.
#:
#: Canonicalizing at all is what makes :class:`KeySpec` usable as the cross-platform
#: seam. Without it a platform table doing the natural ``"ctrl" in spec.modifiers``
#: silently drops ``control+c`` — sending a bare ``c`` into a live window, which is
#: the "a different keystroke than the caller asked for" failure
#: :func:`parse_spec` exists to prevent.
#:
#: **The mapping follows INTENT, not the key's name.** ``cmd``/``command``/``super``/
#: ``meta`` all canonicalize to ``ctrl``, because what a caller means by ``cmd+s`` is
#: Save and on Windows that is Ctrl+S. Sending the logo key instead opens Search,
#: leaves the document unsaved, and a close after it loses the edits — a data-loss
#: path from a spec the model was told to write. ``win`` is the one spelling that
#: names the physical logo key and therefore stays itself, so ``win+d`` still reaches
#: Show Desktop. macOS is unaffected: ``MODIFIERS`` maps every one of these to
#: ``FLAG_COMMAND`` there, and ``parse_key`` reads that table, not this one.
MODIFIER_ALIASES: dict[str, str] = {
    # ``cmd``/``command`` canonicalize to CTRL, not to the logo key. What a caller
    # MEANS by cmd+s is Save, and on Windows that is Ctrl+S — Win+S opens Search,
    # leaves the document unsaved, and a close after it loses the edits. So the
    # cross-platform INTENT wins over the literal key name.
    "cmd": "ctrl", "command": "ctrl",
    # ``super``/``meta`` are the X11/emacs spellings of the same intent and follow it.
    "super": "ctrl", "meta": "ctrl",
    # ``win`` is the ONE spelling that names the physical logo key, so it stays
    # itself: a caller who writes win+d wants Show Desktop, not Ctrl+D.
    "win": "win",
    "shift": "shift",
    "option": "alt", "opt": "alt", "alt": "alt",
    "control": "ctrl", "ctrl": "ctrl",
    "fn": "fn", "function": "fn",
    "capslock": "capslock",
}  # fmt: skip


def canonical_modifier(name: str) -> str:
    """The canonical spelling of modifier *name*, or *name* unchanged if unknown.

    Total in its argument for the same reason as :func:`canonical_key`:
    :func:`parse_spec` is the layer that refuses, this only normalizes.
    """
    return MODIFIER_ALIASES.get(name, name)


#: The abstract key vocabulary: every name a spec may use, aliases included.
KEY_NAMES: frozenset[str] = frozenset(KEYCODES)

#: Names that share a macOS keycode while being DIFFERENT PHYSICAL KEYS elsewhere.
#: Each is its own canonical name, so :data:`KEY_ALIASES` cannot fold it into a
#: neighbour.
#:
#: ``KEYCODES`` is a macOS table, and macOS resolves several distinct keys onto one
#: ``kVK_*`` code: ``delete``/``backspace`` are both 51 and ``help``/``insert`` are
#: both 114. Deriving canonical names purely by keycode identity therefore made
#: ``canonical_key("delete") == "backspace"`` and ``canonical_key("insert") ==
#: "help"`` — correct for macOS and wrong for every other platform, where
#: ``VK_DELETE`` and ``VK_BACK`` are different keys. A Windows table keyed on the
#: collapsed name would send Backspace for ``press_key("delete")``, destroying the
#: character BEFORE the caret instead of after it, and leave ``VK_DELETE`` and
#: ``VK_INSERT`` inexpressible.
#:
#: ``space``/``spacebar`` is also excluded — not because the keys differ, but so the
#: canonical spelling is the ordinary one a platform table would be written with.
_SELF_CANONICAL: frozenset[str] = frozenset(
    {"delete", "backspace", "help", "insert", "space", "spacebar"}
)

#: Alias -> canonical name, derived from :data:`KEYCODES` by keycode identity except
#: for :data:`_SELF_CANONICAL`. Built rather than hand-written so it cannot drift
#: from the table above, and so adding an alias there needs no second edit here.
#:
#: This is what stops every platform table from having to repeat all of
#: ``esc``/``escape``, ``pgup``/``pageup``/``pagedown``/``pgdn``/``pgdown``,
#: ``enter``/``return``, ``del``/``forwarddelete`` and the rest: a Windows table
#: keyed on canonical names covers all of them, and a missing alias cannot
#: silently refuse a key the model legitimately spelled.
#:
#: The canonical name is the LONGEST spelling for a keycode, which picks the
#: descriptive form (``escape`` over ``esc``, ``pagedown`` over ``pgdn``) so a
#: table read by a human states what it means. Ties break alphabetically, so the
#: choice is deterministic rather than dict-order dependent.
KEY_ALIASES: dict[str, str] = {
    name: (
        name
        if name in _SELF_CANONICAL
        else max(
            (
                other
                for other, code in KEYCODES.items()
                if code == keycode and other not in _SELF_CANONICAL
            ),
            key=lambda other: (len(other), other),
        )
    )
    for name, keycode in KEYCODES.items()
}


def canonical_key(name: str) -> str:
    """The canonical spelling of key *name*, or *name* unchanged if unknown.

    Total in its argument: an unknown name is returned as-is rather than raising,
    because :func:`parse_spec` is the layer that refuses and this is only a
    normalizer.
    """
    return KEY_ALIASES.get(name, name)


@dataclass(frozen=True)
class KeySpec:
    """One parsed key spec, in ABSTRACT names — no platform numbers.

    ``modifiers`` holds CANONICAL modifier names (never masks, never a caller's
    alias — see :data:`MODIFIER_ALIASES`) and ``key`` is a canonical key name from
    :data:`KEY_NAMES`.

    **A caller-supplied ``shift+`` and an implied one are the SAME spec.** A key
    token can demand Shift by itself (``$`` is Shift+4, ``A`` is Shift+a), and
    ``parse_spec`` folds that into ``modifiers`` rather than reporting it separately,
    so ``parse_spec("A") == parse_spec("shift+a")``.

    That is why there is no ``implied_shift`` field. Shift is a requirement of the
    keystroke, not a property of how the caller spelled it, so a platform table reads
    ``"shift" in spec.modifiers`` and cannot forget a second flag. A provenance field
    would also break the equality above, since two specs that produce the same
    keystroke must compare equal.
    """

    key: str
    modifiers: frozenset[str] = frozenset()


def parse_spec(spec: str) -> KeySpec:
    """Parse a key spec into a platform-free :class:`KeySpec`.

    THE validation seam. ``tools._perform`` calls this in the dispatch
    chokepoint, so an unknown modifier or key is refused before a driver on any
    platform can synthesize it into a live window. Raises
    :class:`KeyParseError` for an empty spec, an unknown modifier, an unknown
    key, or a spec with no key part (``"cmd+"``).

    Refusing loudly is correct: silently dropping an unrecognized modifier would
    send a DIFFERENT keystroke than the caller asked for.

    Both halves of the returned spec are CANONICAL — modifiers through
    :data:`MODIFIER_ALIASES`, the key through :data:`KEY_ALIASES` — so a platform
    table is written against one spelling per key and per modifier. Returning the
    caller's raw spelling instead would recreate the failure this seam prevents at
    the layer below it: a table testing ``"ctrl" in spec.modifiers`` would miss
    ``control+c`` and send a bare ``c``.
    """
    tokens = _tokenize(spec)
    names: set[str] = set()
    for token in tokens[:-1]:
        name = token.lower()
        if name not in MODIFIERS:
            raise KeyParseError(f"unknown modifier {token!r} in {spec!r}")
        names.add(canonical_modifier(name))

    key = tokens[-1]
    resolved, implied = _resolve_name(key)
    if resolved is None:
        raise KeyParseError(f"unknown key {key!r} in {spec!r}")
    if implied:
        # Folded in, so an implied shift and a written one are the same spec. A
        # platform table reads ``modifiers`` alone and cannot forget a second flag.
        names.add(canonical_modifier("shift"))
    return KeySpec(key=resolved, modifiers=frozenset(names))


def _resolve_name(key: str) -> tuple[str | None, bool]:
    """Resolve one key token to ``(canonical name | None, implied_shift)``.

    Mirrors :func:`_resolve_key` but stays in name space, so the shifted-glyph
    and uppercase rules are stated once and every platform table inherits them.
    """
    if not key:
        return None, False
    if key in SHIFTED_CHARS:
        base = SHIFTED_CHARS[key]
        return (canonical_key(base) if base in KEYCODES else None), True
    if len(key) == 1 and key.isalpha() and key.isupper():
        lowered = key.lower()
        return (canonical_key(lowered) if lowered in KEYCODES else None), True
    lowered = key.lower()
    return (canonical_key(lowered) if lowered in KEYCODES else None), False


def _tokenize(spec: str) -> list[str]:
    """Split a spec into ``[*modifiers, key]``, or raise :class:`KeyParseError`.

    Shared by both parsers so the grammar — including the ``+``-as-a-key
    special case — cannot drift between the abstract and the macOS path.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise KeyParseError("empty key spec")
    raw = spec.strip()

    # A bare ``+`` (or a spec ending in one, e.g. ``shift++``) means the plus
    # key itself, which is Shift+equal on a US layout. Handle it before
    # splitting, or the split would yield an empty key part.
    parts: list[str]
    if raw == _SPEC_SEPARATOR:
        parts = ["+"]
    elif raw.endswith(_SPEC_SEPARATOR):
        parts = [p for p in raw[:-1].split(_SPEC_SEPARATOR) if p] + ["+"]
    else:
        parts = raw.split(_SPEC_SEPARATOR)

    tokens = [p.strip() for p in parts if p.strip()]
    if not tokens:
        raise KeyParseError(f"no key in spec {spec!r}")
    return tokens


def parse_key(spec: str) -> tuple[int, int]:
    """Parse a key spec into macOS ``(keycode, flag_mask)``.

    The macOS-shaped counterpart to :func:`parse_spec`: the numbers are Carbon
    keycodes and CoreGraphics flag masks, so this is for the macOS driver and its
    tests, NOT for a cross-platform caller.

    The mask is built from ZERO — never from the current modifier state — so a
    synthesized event carries exactly the modifiers that were requested.

    Raises :class:`KeyParseError` for an empty spec, an unknown modifier, an
    unknown key, or a spec with no key part (``"cmd+"``). Refusing loudly is
    correct here: silently dropping an unrecognized modifier would send a
    DIFFERENT keystroke than the caller asked for, into a live application.
    """
    tokens = _tokenize(spec)

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
