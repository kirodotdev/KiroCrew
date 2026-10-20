"""Linux input synthesis: the X11 keysym table and the keystroke/action helpers.

The Linux twin of the input half of :mod:`windows_driver`, split out of the
driver for the same reason the Windows side keeps its vtable table in
:mod:`windows_ffi`: the data that is easy to get wrong — a per-key X keysym, the
mapping from an abstract modifier to its ``*_L`` keysym — lives in ONE table a
test can enumerate against :data:`keymap.KEY_NAMES`, rather than scattered through
the verbs that use it.

**Nothing native is imported at module scope.** The X11 and AT-SPI calls go
through :mod:`kiro_crew.computer_use.linux_ffi`, whose every ``CDLL`` lives behind
a function — so importing this module succeeds on a host with no libraries and no
display, which is what keeps the package import-safe on the CI fleet.

The platform fact that shapes everything here is the same one the module
docstring of :mod:`windows_driver` opens with, stated for X11:

* **AT-SPI actions and ``EditableText`` writes are the SAFE form.** A
  ``do_action`` or an ``insert_text`` is performed by the toolkit INSIDE the
  target widget — no pointer moves, no global focus changes — so it is the only
  input route that cannot touch a window other than the one addressed, and it is
  what an element-addressed verb prefers.
* **XTest is GLOBAL.** ``xtest_key`` / ``xtest_button`` / ``xtest_motion`` move
  the real pointer and take the real focus, exactly as ``SendInput`` does on
  Windows — there is no per-window delivery for them. So every XTest path is
  opt-in by name (``global``) or is the keystroke fallback that MUST take focus
  first and say so.
* **``XSendEvent`` is per-window but UNRELIABLE.** It posts a synthetic event to
  one window with ``send_event`` set, which many toolkits (GTK4 among them)
  ignore outright. It is the ``app_post`` route; a caller gets it only by naming
  ``app_post``, and the driver reports honestly when a toolkit swallowed it.

The X keysyms below are the standard ``X11/keysymdef.h`` values. They are ABI
constants (a keysym is not a keycode — the keycode is looked up live per keyboard
via ``linux_ffi.x_keysym_to_keycode``), so they are safe to hardcode and
unit-testable with no X server.
"""

from __future__ import annotations

from kiro_crew.computer_use import linux_ffi
from kiro_crew.computer_use.keymap import KeySpec

# ── X keysyms, keyed on keymap's CANONICAL key names ──
# Every value in ``keymap.KEY_ALIASES`` (i.e. every canonical key a ``press_key``
# spec can resolve to) MUST have an entry here, pinned by
# ``test_computer_use_linux_input.py::test_every_canonical_key_has_a_keysym`` — a
# key ``keymap`` accepts but this table lacks would be refused at the driver AFTER
# the chokepoint said yes, a dead end the model cannot act on.
#
# The names are from ``X11/keysymdef.h`` (the ``XK_*`` constants with the ``XK_``
# prefix stripped). A keysym is layout-independent; the per-keyboard keycode is
# resolved live, so these are stable ABI numbers.
KEYSYMS: dict[str, int] = {
    # letters (lowercase keysyms; Shift is applied separately for an uppercase glyph)
    "a": 0x0061, "b": 0x0062, "c": 0x0063, "d": 0x0064, "e": 0x0065, "f": 0x0066,
    "g": 0x0067, "h": 0x0068, "i": 0x0069, "j": 0x006A, "k": 0x006B, "l": 0x006C,
    "m": 0x006D, "n": 0x006E, "o": 0x006F, "p": 0x0070, "q": 0x0071, "r": 0x0072,
    "s": 0x0073, "t": 0x0074, "u": 0x0075, "v": 0x0076, "w": 0x0077, "x": 0x0078,
    "y": 0x0079, "z": 0x007A,
    # digits
    "0": 0x0030, "1": 0x0031, "2": 0x0032, "3": 0x0033, "4": 0x0034,
    "5": 0x0035, "6": 0x0036, "7": 0x0037, "8": 0x0038, "9": 0x0039,
    # punctuation (the unshifted glyph each canonical name denotes)
    "minus": 0x002D, "equals": 0x003D, "leftbracket": 0x005B, "rightbracket": 0x005D,
    "backslash": 0x005C, "semicolon": 0x003B, "apostrophe": 0x0027, "comma": 0x002C,
    "period": 0x002E, "slash": 0x002F, "backtick": 0x0060,
    # whitespace / editing
    "space": 0x0020, "spacebar": 0x0020, "return": 0xFF0D, "tab": 0xFF09,
    " ": 0x0020,
    "backspace": 0xFF08, "delete": 0xFFFF, "forwarddelete": 0xFFFF,
    "escape": 0xFF1B, "help": 0xFF6A, "insert": 0xFF63,
    # navigation
    "arrowleft": 0xFF51, "arrowup": 0xFF52, "arrowright": 0xFF53, "arrowdown": 0xFF54,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    # function keys
    "f1": 0xFFBE, "f2": 0xFFBF, "f3": 0xFFC0, "f4": 0xFFC1, "f5": 0xFFC2,
    "f6": 0xFFC3, "f7": 0xFFC4, "f8": 0xFFC5, "f9": 0xFFC6, "f10": 0xFFC7,
    "f11": 0xFFC8, "f12": 0xFFC9, "f13": 0xFFCA, "f14": 0xFFCB, "f15": 0xFFCC,
    "f16": 0xFFCD, "f17": 0xFFCE, "f18": 0xFFCF, "f19": 0xFFD0, "f20": 0xFFD1,
    # keypad
    "keypad0": 0xFFB0, "keypad1": 0xFFB1, "keypad2": 0xFFB2, "keypad3": 0xFFB3,
    "keypad4": 0xFFB4, "keypad5": 0xFFB5, "keypad6": 0xFFB6, "keypad7": 0xFFB7,
    "keypad8": 0xFFB8, "keypad9": 0xFFB9, "keypadclear": 0xFF0B,
    "keypaddecimal": 0xFFAE, "keypaddivide": 0xFFAF, "keypadenter": 0xFF8D,
    "keypadequals": 0xFFBD, "keypadminus": 0xFFAD, "keypadmultiply": 0xFFAA,
    "keypadplus": 0xFFAB,
    # media / volume (XF86 keysyms)
    "mute": 0x1008FF12, "volumedown": 0x1008FF11, "volumeup": 0x1008FF13,
}  # fmt: skip

# ── Modifier canonical name -> its X keysym ──
# ``keymap.MODIFIER_ALIASES`` canonicalizes to ``ctrl``/``shift``/``alt``/``win``/
# ``fn``/``capslock``; ``cmd``/``super``/``meta`` already fold to ``ctrl`` there, so
# what reaches here is one of those canonical names. We map to the LEFT-hand
# modifier key, matching how every other synthesizer picks one physical key for an
# abstract modifier.
#
# ``fn`` and ``capslock`` are deliberately ABSENT, mirroring the Windows refusal:
# ``fn`` is handled in keyboard firmware below the OS and has no keysym to send, and
# a ``capslock`` press TOGGLES a machine-wide lock whose flip OUTLIVES the call —
# so ``capslock+a`` would capitalize everything the operator types next, and
# releasing the key would not undo it. Both stay valid in the platform-free
# vocabulary (macOS serves them), so this is a per-platform refusal.
_MODIFIER_KEYSYMS: dict[str, int] = {
    "ctrl": 0xFFE3,  # Control_L
    "shift": 0xFFE1,  # Shift_L
    "alt": 0xFFE9,  # Alt_L
    "win": 0xFFEB,  # Super_L — the one spelling that names the physical logo key
}

# AT-SPI action names (toolkit strings) that mean "activate this control", most
# specific first. ``do_action`` is tried against these by name; a FAILED action is
# never followed by the next rung, matching the Windows pattern ladder — falling
# onward from a failure performs a different gesture than the one requested.
_ACTIVATE_ACTIONS: tuple[str, ...] = ("click", "press", "activate")
#: The toggle action for a checkbox/radio, tried for those roles only.
_TOGGLE_ACTION = "toggle"

#: X mouse buttons for a scroll gesture — Button4/5 scroll vertically, 6/7
#: horizontally — so a scroll can be delivered to a window with ``XSendEvent``
#: without moving the pointer.
SCROLL_BUTTONS: dict[str, int] = {
    "up": 4,
    "down": 5,
    "left": 6,
    "right": 7,
}

#: The focus-taking note every XTest keystroke path carries. The operator's real
#: focus moved, and a model told only "typed 5 characters" would not know their
#: caret is now elsewhere — exactly as the Windows ``_FOCUS_NOTE`` says.
FOCUS_NOTE = " (keyboard focus was taken — X11 has no per-window key route for this)"
#: The note an ``insert_text`` / ``set_text_contents`` write carries: it did NOT
#: steal focus, which is the better outcome and worth telling the model so it does
#: not assume the caret moved.
NO_FOCUS_NOTE = " (no focus change — written through the accessibility interface)"


class LinuxInputError(Exception):
    """A key spec names a modifier this platform cannot synthesize.

    Its own type so the driver can turn it into a ``DriverResult(ok=False, ...)``
    whose text names the alternative, rather than a generic failure. Raised only
    for ``fn``/``capslock``, which are refused BY DECISION (see
    :data:`_MODIFIER_KEYSYMS`); an unknown key is already refused by
    :func:`keymap.parse_spec` before a spec reaches here.
    """


_REFUSE_CAPSLOCK = (
    "capslock cannot be sent as a modifier on Linux: pressing it toggles a "
    "machine-wide lock that outlives the call, so it would change what the operator "
    "types next and releasing it would not undo it. Use shift+<key> instead"
)
_REFUSE_FN = (
    "fn has no X keysym — it is handled in keyboard firmware below the OS, so there "
    "is nothing to synthesize"
)


def activate_actions_for(role: str) -> tuple[str, ...]:
    """The ordered AT-SPI action names to try for an element of *role*.

    A checkbox/radio additionally offers ``toggle`` AFTER the activate names, so a
    checkbox whose toolkit exposes ``click`` is clicked, and one that exposes only
    ``toggle`` still flips. The order is by specificity, exactly like the Windows
    ladder: the explicit activate verbs first, the state-flip last.
    """
    role = (role or "").strip().lower()
    if role in ("checkbox", "radiobutton"):
        return _ACTIVATE_ACTIONS + (_TOGGLE_ACTION,)
    return _ACTIVATE_ACTIONS


def perform_activate(obj: int, role: str) -> "str | None":
    """Click *obj* through the AT-SPI action ladder. Returns the action that WON, or ``None``.

    The safe, pointer-free, focus-free form: the toolkit performs the action inside
    the widget. Mirrors ``windows_driver._press_element``'s rules exactly:

    * the candidate action names come from :func:`activate_actions_for`, so a
      checkbox can fall to ``toggle`` while a button cannot;
    * if the element advertises exactly ONE action and none of the named ones
      matched, that single action is performed (an idiosyncratic toolkit verb);
    * a FAILED ``do_action`` STOPS the ladder — it is never followed by the next
      candidate, because the right action was attempted and rejected, and falling
      onward would perform a different gesture.

    Returns the winning action name so the driver can name it in the result, the
    way the Windows ladder names the pattern that succeeded.
    """
    available = linux_ffi.actions(obj)
    if not available:
        return None
    lowered = {name.strip().lower(): name for name in available}
    for candidate in activate_actions_for(role):
        actual = lowered.get(candidate)
        if actual is None:
            # ABSENT — advance to the next candidate.
            continue
        index = available.index(actual)
        if linux_ffi.do_action(obj, index):
            return actual
        # PRESENT but FAILED: stop, exactly as the Windows ladder does on a failed
        # pattern. Returning None here would advance to the next candidate.
        return None
    # No named action matched. If the element exposes exactly one action at all,
    # perform it — a toolkit may name its only activation something unexpected.
    if len(available) == 1:
        if linux_ffi.do_action(obj, 0):
            return available[0]
    return None


def keysym_for_spec(spec: KeySpec) -> int:
    """The X keysym for the KEY of a parsed :class:`KeySpec`.

    Total over a spec that already passed :func:`keymap.parse_spec`: the key is
    canonical and therefore in :data:`KEYSYMS`. Raises :class:`KeyError` only if the
    table is missing a canonical key, which the enumeration test forbids.
    """
    return KEYSYMS[spec.key]


def modifier_keysyms(spec: KeySpec) -> list[int]:
    """The X keysyms for *spec*'s modifiers, in a deterministic order.

    Raises :class:`LinuxInputError` for ``fn``/``capslock`` — refused BY DECISION,
    with a message that names the working alternative. Sorted by canonical name so
    the press/release order is stable and testable.
    """
    out: list[int] = []
    for name in sorted(spec.modifiers):
        if name == "capslock":
            raise LinuxInputError(_REFUSE_CAPSLOCK)
        if name == "fn":
            raise LinuxInputError(_REFUSE_FN)
        keysym = _MODIFIER_KEYSYMS.get(name)
        if keysym is None:
            # An unexpected canonical modifier: refuse rather than silently drop it,
            # since a dropped modifier sends a DIFFERENT chord than the caller asked.
            raise LinuxInputError(f"no X keysym for modifier {name!r}")
        out.append(keysym)
    return out


def send_key_chord(spec: KeySpec) -> None:
    """Send one key chord through XTest: modifiers down, key tap, modifiers up (reverse).

    GLOBAL — this takes the real keyboard focus. The driver only reaches it after
    moving focus to the target and reports that it did so.

    The press order is modifiers then key, the release order is key then modifiers
    in REVERSE, so a chord leaves no modifier stuck down. Keysyms are resolved to
    this keyboard's keycodes live via :func:`linux_ffi.x_keysym_to_keycode`.
    """
    mods = modifier_keysyms(spec)
    key_keysym = keysym_for_spec(spec)
    mod_codes = [linux_ffi.x_keysym_to_keycode(k) for k in mods]
    key_code = linux_ffi.x_keysym_to_keycode(key_keysym)
    for code in mod_codes:
        linux_ffi.xtest_key(code, True)
    linux_ffi.xtest_key(key_code, True)
    linux_ffi.xtest_key(key_code, False)
    for code in reversed(mod_codes):
        linux_ffi.xtest_key(code, False)
    linux_ffi.x_flush()


def type_text_via_xtest(text: str) -> int:
    """Type *text* as XTest keystrokes, one Unicode character at a time. GLOBAL.

    Returns the number of characters actually sent — a character the keymap cannot
    reach is SKIPPED rather than substituted, because typing the wrong character
    into a live application is worse than typing nothing (the Windows ``send_text``
    rule). The driver treats a short count as a partial write and refuses.

    Each character is resolved as the Unicode keysym ``U<HHHH>`` via
    :func:`linux_ffi.x_string_to_keysym`, then to a keycode; Shift is pressed around
    the tap when the keymap says this keycode needs it for this keysym.
    """
    sent = 0
    for char in text:
        keysym = linux_ffi.x_string_to_keysym(f"U{ord(char):04X}")
        if keysym == 0:
            # NoSymbol — unreachable on this layout. Skip it, never substitute.
            continue
        keycode = linux_ffi.x_keysym_to_keycode(keysym)
        if keycode == 0:
            continue
        needs_shift = linux_ffi.x_keycode_needs_shift(keycode, keysym)
        shift_code = linux_ffi.x_keysym_to_keycode(_MODIFIER_KEYSYMS["shift"]) if needs_shift else 0
        if needs_shift:
            linux_ffi.xtest_key(shift_code, True)
        linux_ffi.xtest_key(keycode, True)
        linux_ffi.xtest_key(keycode, False)
        if needs_shift:
            linux_ffi.xtest_key(shift_code, False)
        sent += 1
    linux_ffi.x_flush()
    return sent


def write_text_via_atspi(obj: int, text: str) -> bool:
    """Write *text* into an editable AT-SPI object WITHOUT taking focus. Returns success.

    The preferred text route when the element is editable: ``set_text_contents``
    replaces the field's contents through the ``EditableText`` interface, so the
    operator's caret and the global focus are untouched. Returns False when the
    object does not implement ``EditableText`` (``linux_ffi.set_text_contents``
    answers False), so the driver can fall back to the focus-taking keystroke path.
    """
    return linux_ffi.set_text_contents(obj, text)


def is_capslock_or_fn_refusal(exc: LinuxInputError) -> str:
    """The refusal text carried by a :class:`LinuxInputError` (its own message).

    A tiny helper so the driver does not re-spell the message; the exception's
    ``str`` already carries the actionable text.
    """
    return str(exc)
