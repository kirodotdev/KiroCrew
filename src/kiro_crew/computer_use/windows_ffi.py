"""The Windows native layer: its import path, and its ``SendInput`` keyboard and mouse input.

Everything native for Windows lives here: the UI Automation COM client, its
vtable dispatch and the library loader in :mod:`~kiro_crew.computer_use.windows_ffi_com`;
VARIANT/BSTR marshalling, element reads and actions and the bounded walk in
:mod:`~kiro_crew.computer_use.windows_ffi_uia`; window enumeration and the DPI awareness
scope in :mod:`~kiro_crew.computer_use.windows_ffi_window`; and the keyboard and mouse
input below. Every other Windows computer-use module talks to the OS through these
helpers, read and patched as ``windows_ffi.<name>`` (see :mod:`kiro_crew._facade_mirror`),
so the FFI hazards are audited in one place — the same property ``macos_ffi`` holds for
macOS.

``import ctypes`` is a top-level import statement, but **no ``WinDLL`` runs at
module scope**. The libraries load inside :func:`_libraries`, cached in a module
global, and raise :class:`ComputerUseUnsupported` off Windows — so this family
imports cleanly on the macOS and Linux CI shards, and a test can exercise the
binding logic with a fake loader. ``from ctypes import wintypes`` supplies type
ALIASES only and imports cleanly on POSIX, which is what lets the structures
sit at module scope on every platform.

UI Automation is COM-only — there is no C API — so every call is a manual vtable
dispatch. That makes the hazards here different in kind from the macOS ones:
**the slot index IS the ABI**, and a wrong index is an access violation that
kills the interpreter with no Python traceback, taking the gateway (and with it
cron, Slack and every session) down. Six hazards this family exists to contain,
each measured on Windows 11:

1. **A wrong vtable slot is a process abort, not an exception.** A blind slot
   sweep produced a real ``EXIT=139``. Hence :data:`_VTABLE`: every slot index in
   this package appears exactly once, in that table, and :func:`vcall` resolves by METHOD
   NAME so no call site can hand-roll an index.
2. **Two slots return ``S_OK`` while doing the WRONG thing.**
   ``ElementFromHandle`` is slot 6; slot 7 is ``ElementFromPoint``, and handed an
   HWND it reads it as a screen coordinate and cheerfully resolves to the whole
   DESKTOP. A desktop-scoped walk then reads every application's tree while
   looking like it worked, so :func:`element_from_hwnd` treats a NULL result as a
   hard error and never falls through.
3. **A missing ``argtypes`` truncates a 64-bit pointer to 32 bits.** An HWND
   passed as a bare Python int is the common form of this; every handle crosses
   the boundary as ``c_void_p``. :data:`_FN_SPECS` carries both ``argtypes`` and
   ``restype`` on every row and :func:`_bind` RAISES on ``argtypes=None`` rather
   than accepting the ctypes default.
4. **A BSTR must be decoded BEFORE ``VariantClear``.** Afterwards the freed block
   is reused, and different properties read back byte-identical — which looks
   exactly like a wrong-property bug and is not. :func:`variant_value` copies out
   first, with ``SysStringLen`` rather than a NUL scan so an embedded NUL cannot
   truncate a value.
5. **``restype`` must be ``c_long``, never ``ctypes.HRESULT``.** ``HRESULT``
   raises on a failure code, which turns an EXPECTED ``E_INVALIDARG`` — the
   normal answer for an unsupported property id — into an exception that reads
   like a crash.
6. **A ``ctypes.Structure`` declared in a function body leaks forever.**
   ``ctypes.POINTER`` memoises the type in a module-level dict with no eviction,
   so every call pins a fresh pair of type objects. Every structure and union
   in this family is at module scope.

**The secure-field decision is the security-critical function in this family.**
:func:`is_secure_element` gates three separate floors — value redaction, input
refusal and whole-window screenshot suppression — so it fails CLOSED. See its
docstring for the decision table and for why a cached value may never decide it.
"""

from __future__ import annotations

import ctypes
import importlib as _importlib
import math
import sys as _sys

# ``ManualClock.install(monkeypatch, windows_ffi)`` finds the clock in this module's own
# namespace; the mirror carries the patch to ``windows_ffi_uia``, whose walk reads it.
import time  # noqa: F401
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from typing import TYPE_CHECKING, Sequence

from kiro_crew import platform_compat  # noqa: F401
from kiro_crew import _facade_mirror
from kiro_crew.computer_use.types import ComputerUseUnsupported
from kiro_crew.computer_use.windows_ffi_com import _libraries, release
from kiro_crew.computer_use.windows_ffi_window import quantize_point

# ── Keyboard: SendInput ──
#
# **This is the one input path on Windows that is not confined to the target
# application.** ``SendInput`` takes no hwnd and no pid: it injects at the session
# level and delivery follows whatever holds focus, so unlike macOS's
# ``CGEventPostToPid`` there is no per-process keystroke route at all. Two
# consequences the callers must honour, both enforced in ``windows_driver``:
#
# * the target must be FOCUSED first (``set_element_focus``) and the result must
#   SAY the focus moved, because the operator's caret really did move;
# * a keystroke is never a fallback for a failed focus. If focus did not land where
#   the model aimed, the keystrokes would go to whatever the application had
#   focused — which can be the password field the secure check just refused.


def _vk_table() -> "dict[str, int]":
    """Canonical key name -> Windows virtual-key code.

    Keyed on :data:`keymap.KEY_ALIASES`' canonical names, which is what makes this
    table small enough to audit: every alias the model may spell (``esc``,
    ``pgdn``, ``arrowleft``) resolves to one entry here.

    Built by a function rather than written out so the 26 letters, 10 digits and 20
    function keys are generated instead of transcribed — three ranges where a typo
    is invisible on review and sends a neighbouring key at runtime.

    ``test_computer_use_windows_input.py`` asserts this table covers every canonical
    name ``keymap`` can produce, so a key added there cannot ship unmapped.
    """
    letters = {chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)}
    digits = {chr(c): c for c in range(ord("0"), ord("9") + 1)}
    named = {
        # Editing / whitespace. ``delete`` and ``backspace`` are DIFFERENT keys
        # here, which is why keymap refuses to fold them onto one canonical name.
        "backspace": 0x08,
        "tab": 0x09,
        "return": 0x0D,
        "escape": 0x1B,
        "space": 0x20,
        "spacebar": 0x20,
        " ": 0x20,
        "delete": 0x2E,  # VK_DELETE — forward delete
        "forwarddelete": 0x2E,
        "insert": 0x2D,
        "help": 0x2F,  # VK_HELP
        # Navigation
        "pageup": 0x21,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
        "arrowleft": 0x25,
        "arrowup": 0x26,
        "arrowright": 0x27,
        "arrowdown": 0x28,
        # Punctuation (OEM codes, US layout)
        "semicolon": 0xBA,
        "equals": 0xBB,
        "comma": 0xBC,
        "minus": 0xBD,
        "period": 0xBE,
        "slash": 0xBF,
        "backtick": 0xC0,
        "leftbracket": 0xDB,
        "backslash": 0xDC,
        "rightbracket": 0xDD,
        "apostrophe": 0xDE,
        # Keypad
        "keypadmultiply": 0x6A,
        "keypadplus": 0x6B,
        "keypadminus": 0x6D,
        "keypaddecimal": 0x6E,
        "keypaddivide": 0x6F,
        "keypadclear": 0x0C,  # VK_CLEAR
        "keypadenter": 0x0D,  # no distinct VK; Return with the extended flag
        "keypadequals": 0xBB,
        # Media
        "mute": 0xAD,
        "volumedown": 0xAE,
        "volumeup": 0xAF,
    }
    named.update({f"keypad{n}": 0x60 + n for n in range(10)})
    named.update({f"f{n}": 0x70 + (n - 1) for n in range(1, 21)})
    return {**letters, **digits, **named}


#: Canonical key name -> VK code. See :func:`_vk_table`.
VK_CODES: "dict[str, int]" = _vk_table()

#: Canonical modifier name -> VK code. Two names in the platform-free vocabulary are
#: deliberately ABSENT, and a spec naming either is refused rather than silently
#: dropped — dropping a modifier sends a DIFFERENT chord than the caller asked for.
#:
#: * ``fn`` has no virtual-key code at all; it is handled in keyboard firmware below
#:   the OS.
#: * ``capslock`` is a LOCK on Windows, not a holdable modifier. macOS models it as
#:   ``FLAG_ALPHA_SHIFT``, a per-event flag that applies to the one synthesized event
#:   and changes no machine state — but ``VK_CAPITAL`` down/up TOGGLES the lock, and
#:   the flip persists after the chord, so ``capslock+a`` would leave every subsequent
#:   keystroke the OPERATOR types capitalized. That is exactly the kind of residual
#:   state this driver must not leave behind, and unlike the other modifiers it cannot
#:   be undone by releasing the key. Refusing is the honest answer: a caller who wants
#:   a capital letter should send ``shift+a`` or the character itself.
VK_MODIFIERS: "dict[str, int]" = {
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "win": 0x5B,  # VK_LWIN
}

#: Modifiers absent from :data:`VK_MODIFIERS` for a REASON rather than an omission,
#: each with the refusal that says which. A caller told only "no virtual-key code"
#: would reasonably read it as a gap to work around; these are decisions, and the
#: message names the working alternative so the model spends no turn guessing.
_UNHOLDABLE_MODIFIERS: "dict[str, str]" = {
    "capslock": (
        "'capslock' cannot be used as a modifier on Windows: VK_CAPITAL TOGGLES a "
        "machine-wide lock rather than being held, so the flip would persist after "
        "this call and capitalize everything the operator types next. Send 'shift+<key>' "
        "for one capital, or type the character directly with computer_type_text"
    ),
    "fn": (
        "'fn' has no virtual-key code on Windows: it is handled in keyboard firmware "
        "below the OS, so no synthesized event can carry it. Name the key the "
        "combination produces instead (for example 'f5' or 'home')"
    ),
}

#: Keys that MUST carry ``KEYEVENTF_EXTENDEDKEY``. Without it the injected scan
#: code is the numpad twin of the same VK, so an arrow key moves the caret only
#: while NumLock happens to be off and Insert/Delete hit the keypad instead.
_EXTENDED_KEYS: frozenset[str] = frozenset(
    {
        "arrowleft",
        "arrowright",
        "arrowup",
        "arrowdown",
        "home",
        "end",
        "pageup",
        "pagedown",
        "insert",
        "delete",
        "forwarddelete",
        "keypadenter",
        "keypaddivide",
        "mute",
        "volumeup",
        "volumedown",
    }
)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001


class KEYBDINPUT(ctypes.Structure):
    """Module scope: a Structure in a function body leaks (hazard 6)."""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    """Module scope: a Structure in a function body leaks (hazard 6)."""

    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    # BOTH arms are declared, and the size is what matters: ``SendInput`` is passed
    # ``sizeof(INPUT)`` as ``cbSize`` and rejects the whole call with
    # ERROR_INVALID_PARAMETER if it disagrees. A union missing the larger arm would
    # under-report that size and every call would silently fail.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key_event(vk: int, scan: int, flags: int) -> INPUT:
    """One INPUT record. ``dwExtraInfo`` stays NULL, which is what we inject with."""
    record = INPUT()
    record.type = INPUT_KEYBOARD
    record.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return record


def _send(records: "Sequence[INPUT]") -> int:
    """Submit records in ONE ``SendInput`` call. Returns how many were accepted.

    One call rather than a loop per key, and that is required rather than an
    optimization: ``SendInput`` guarantees the batch is not interleaved with other
    threads' input, so a chord cannot have a foreign keystroke land between its
    modifier-down and key-down. A loop offers no such guarantee.
    """
    if not records:
        return 0
    libs = _libraries()
    count = len(records)
    array = (INPUT * count)(*records)
    return int(libs["user32"].SendInput(count, array, ctypes.sizeof(INPUT)))


#: How many times a RELEASE batch is retried before giving up. A release is not
#: optional — see :func:`_send_release` — so it is worth retrying, but not forever:
#: an unbounded loop against a permanently-blocking hook would spin a pooled worker.
_RELEASE_ATTEMPTS = 3


def _send_release(records: "Sequence[INPUT]", *, atomic: bool = False) -> None:
    """Submit RELEASE records, retrying, and raise if any stays unreleased.

    **A release is the one batch whose acceptance cannot be assumed.** ``SendInput``
    returns how many records it accepted and can stop early — a lower-level hook, or
    UIPI — and it returns NORMALLY, so a discarded count means a modifier or mouse
    button is left HELD on the operator's own machine. Nothing later un-holds it: a
    stuck Ctrl alters every keystroke they type next, and a stuck mouse button turns
    every motion of their hand into a drag, which on a file manager moves their files.

    So the count is checked and the unaccepted records are resubmitted. ``SendInput``
    accepts a prefix, so by default the retry is exactly ``records[accepted:]`` — resending
    the whole batch would re-release keys already up, which is harmless but muddies what
    failed. Retrying is worth it here precisely because the alternative is not "the
    action failed" but "the operator's hardware is now wrong".

    **``atomic=True`` resends the WHOLE batch instead**, and a drag's release requires it.
    That batch is ``[move(end), button_up]``: the records are not independent, because the
    move is what AIMS the release. A tail-only retry after a partial acceptance resends the
    bare ``button_up``, which carries no coordinate and therefore releases wherever the
    cursor happens to be by then — the drop lands outside the window
    ``hwnd_owns_point`` authorized, which is precisely the defect the aimed release exists
    to prevent. Re-sending the move first is idempotent (a cursor position is state, not an
    edge), so the only cost is one redundant record.

    Raises :class:`ComputerUseUnsupported` when a record is still unreleased after
    :data:`_RELEASE_ATTEMPTS`. The caller must NOT swallow that: reporting success
    over a stuck modifier is what turns a failed keystroke into corrupted input.
    """
    batch = list(records)
    pending = batch
    for _ in range(_RELEASE_ATTEMPTS):
        if not pending:
            return
        accepted = _send(pending)
        pending = batch if (atomic and accepted < len(pending)) else pending[accepted:]
    if pending:
        raise ComputerUseUnsupported(
            f"{len(pending)} release record(s) were rejected after {_RELEASE_ATTEMPTS} "
            "attempts (likely a lower-level hook or UIPI); a key or mouse button may "
            "still be held — press it physically to clear it"
        )


def send_key_chord(key: str, modifiers: "Sequence[str]") -> None:
    """Press *key* with *modifiers* held, then release everything.

    Verified against an instrumented WinForms window that logs every ``KeyDown`` it
    receives: ``a`` arrived as ``key=A``, ``cmd+a`` as ``key=A ctrl=True`` (the
    intent mapping — see ``keymap.MODIFIER_ALIASES``), ``backspace`` as ``key=Back``,
    and ``shift+home`` moved the selection to 0..10 read back through ``EM_GETSEL``.

    One measured non-result worth recording, so it is not mistaken for a bug here: a
    WinForms ``TextBox`` does NOT implement Ctrl+A. The chord arrives — the probe logs
    it — and the selection stays empty, because select-all is not a default binding on
    that control. Whether a chord DOES anything is the target's decision; this function
    is responsible only for delivering the one that was asked for.

    Releases in REVERSE order, and releases even when the key press was rejected:
    a modifier left down is a stuck Ctrl/Alt on the operator's real keyboard, which
    affects every subsequent keystroke they type themselves. That is why the release
    batch is unconditional.

    Raises :class:`ComputerUseUnsupported` for a key or modifier with no VK code
    rather than dropping it — a silently dropped modifier sends a DIFFERENT chord,
    and the whole point of routing through ``keymap.parse_spec`` upstream is that a
    keystroke the caller did not ask for never reaches a live window.
    """
    vk = VK_CODES.get(key)
    if vk is None:
        raise ComputerUseUnsupported(f"no Windows virtual-key code for {key!r}")
    mod_vks: list[int] = []
    for name in modifiers:
        mod_vk = VK_MODIFIERS.get(name)
        if mod_vk is None:
            if name in _UNHOLDABLE_MODIFIERS:
                raise ComputerUseUnsupported(_UNHOLDABLE_MODIFIERS[name])
            raise ComputerUseUnsupported(f"the {name!r} modifier has no Windows virtual-key code")
        mod_vks.append(mod_vk)

    flags = KEYEVENTF_EXTENDEDKEY if key in _EXTENDED_KEYS else 0
    down = [_key_event(m, 0, 0) for m in mod_vks] + [_key_event(vk, 0, flags)]
    up = [_key_event(vk, 0, flags | KEYEVENTF_KEYUP)] + [
        _key_event(m, 0, KEYEVENTF_KEYUP) for m in reversed(mod_vks)
    ]
    try:
        _send(down)
    finally:
        # Unconditional AND verified: a modifier left held would alter every keystroke
        # the OPERATOR types next, so it is not enough to submit the release — the
        # release batch can itself be truncated, and ``_send_release`` retries the
        # unaccepted tail and raises rather than letting that pass silently.
        _send_release(up)


def send_text(text: str) -> int:
    """Type *text* as UNICODE key events. Returns the characters accepted.

    ``KEYEVENTF_UNICODE`` rather than a per-character VK lookup, for the same
    reason macOS posts Unicode events: it is layout-INDEPENDENT and can emit
    characters no single US keystroke reaches. A VK table would type the wrong
    character on a non-US layout, silently.

    Surrogate pairs are submitted as two records, which is how a non-BMP character
    (an emoji) has to cross this API.
    """
    if not text:
        return 0
    records: list[INPUT] = []
    for char in text:
        for code in _utf16_units(char):
            records.append(_key_event(0, code, KEYEVENTF_UNICODE))
            records.append(_key_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    accepted = _send(records)
    if accepted == len(records):
        return len(text)
    # A SHORT accept, reported in CHARACTERS the caller would recognise. Dividing the
    # record count by two is wrong for any non-BMP character: an emoji is a surrogate
    # PAIR, so it costs two code units and four records. ``"<emoji>a"`` with only the
    # first four records accepted has dropped the ``a``, yet ``accepted // 2`` is 2 —
    # exactly ``len(text)`` — so the caller's ``sent < len(text)`` check passes and the
    # partial write is reported as complete.
    #
    # So walk the same records back and count only the characters whose units ALL
    # landed. A character half-written is not written.
    sent_chars = 0
    consumed = 0
    for char in text:
        needed = len(_utf16_units(char)) * 2
        if consumed + needed > accepted:
            break
        consumed += needed
        sent_chars += 1
    return sent_chars


def _utf16_units(char: str) -> "list[int]":
    """The UTF-16 code units of one character (two for a non-BMP codepoint)."""
    encoded = char.encode("utf-16-le")
    return [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]


# ── Mouse: the REAL pointer ──
#
# **Every function below moves the operator's physical cursor.** There is no
# app-scoped mouse route on Windows — ``SendInput`` carries no hwnd — so unlike
# macOS, where ``app_post`` delivers to one pid without touching the pointer, a
# coordinate click here IS a pointer warp. That is why:
#
# * ``policy.resolve_click_method`` must never resolve ``auto`` onto ``global``, and
#   the Windows driver refuses ``auto`` + coordinates outright rather than picking
#   the only method that could serve it;
# * the driver runs ``apps_windows.hwnd_owns_point`` before every call here. A
#   global event lands on whatever owns the pixel, so an authorized app's grant
#   does not authorize the coordinate.

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

#: ``SendInput``'s absolute coordinates are normalized to 0..65535 across the
#: VIRTUAL desktop, NOT pixels — a pixel value passed here lands near the top-left
#: corner on any real screen.
_ABSOLUTE_RANGE = 65535

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


#: Button name -> (down flag, up flag). Names match ``types.MOUSE_BUTTONS``.
_MOUSE_FLAGS: "dict[str, tuple[int, int]]" = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _virtual_screen() -> "tuple[int, int, int, int]":
    """``(left, top, width, height)`` of the whole virtual desktop, in pixels.

    Read from ``GetSystemMetrics`` rather than assumed to start at (0, 0): a
    secondary monitor placed left of or above the primary gives the virtual desktop
    a NEGATIVE origin, and normalizing against a (0, 0) origin would land every
    click on the wrong monitor.

    **MUST be called inside :func:`dpi_awareness_scope`**, because these metrics are
    virtualized exactly like every other coordinate. Measured on a 125% display:
    ``(0, 0, 1536, 960)`` unaware against ``(0, 0, 1920, 1200)`` aware — so the same
    element rect normalizes to 32768 in one state and 26214 in the other, a 25%
    error that puts the click ~480px from the target. The caller cannot detect it
    afterwards: both answers look like valid coordinates.
    """
    libs = _libraries()
    get = libs["user32"].GetSystemMetrics
    return (
        int(get(SM_XVIRTUALSCREEN)),
        int(get(SM_YVIRTUALSCREEN)),
        int(get(SM_CXVIRTUALSCREEN)),
        int(get(SM_CYVIRTUALSCREEN)),
    )


def _normalized(x: float, y: float) -> "tuple[int, int]":
    """Screen pixels -> ``SendInput``'s 0..65535 absolute space.

    Rounds to the nearest unit rather than truncating: on a 1920-wide desktop one
    normalized unit is ~0.03px, so truncation is harmless, but on a small virtual
    desktop it can shift the point a whole pixel — enough to miss a 1px border.
    """
    left, top, width, height = _virtual_screen()
    if width <= 0 or height <= 0:
        raise ComputerUseUnsupported("the virtual screen reported a zero extent")
    # ``ceil`` over ``extent - 1``, and BOTH halves were measured rather than derived:
    # the cursor was moved to real pixels and read back with ``GetCursorPos``, sweeping
    # 13 x values across a 1920-wide desktop.
    #
    #     round(x * 65535 / width)       10 of 13 wrong (every point 1px low)
    #     round(x * 65535 / (width-1))    3 of 13 wrong (small x still 1px low)
    #     ceil (x * 65535 / (width-1))    0 of 13 wrong
    #
    # The OS FLOORS when it maps the normalized value back, so ceiling is its inverse;
    # rounding lands short for any x whose quotient falls below the .5 boundary. Plain
    # arithmetic makes the round form look correct because it is exact at 0 and at the
    # centre — the two points a spot check picks.
    #
    # A 1px error is not cosmetic: it is the difference between the edge pixel of the
    # authorized window and the first pixel of whatever sits behind it, and
    # ``hwnd_owns_point`` validated the point we were ASKED for, not the one we send.
    #
    # A single-pixel extent has no span to divide by, so it degenerates to 0 — the only
    # coordinate such a screen has.
    span_x = width - 1
    span_y = height - 1
    # The SAME pixel the confinement check authorized — see ``quantize_point``.
    px, py = quantize_point(x, y)
    nx = 0 if span_x <= 0 else math.ceil((px - left) * _ABSOLUTE_RANGE / span_x)
    ny = 0 if span_y <= 0 else math.ceil((py - top) * _ABSOLUTE_RANGE / span_y)
    return max(0, min(_ABSOLUTE_RANGE, nx)), max(0, min(_ABSOLUTE_RANGE, ny))


def _mouse_event(flags: int, *, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    record = INPUT()
    record.type = INPUT_MOUSE
    record.u.mi = MOUSEINPUT(dx=x, dy=y, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=None)
    return record


def _move_to(x: float, y: float) -> INPUT:
    """An absolute move record. ``VIRTUALDESK`` is required for a multi-monitor desktop."""
    nx, ny = _normalized(x, y)
    return _mouse_event(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, x=nx, y=ny
    )


def post_mouse_click(x: float, y: float, *, button: str, count: int) -> None:
    """Move the REAL cursor to the point and click it *count* times.

    The move and the button events go in ONE ``SendInput`` batch so no other
    thread's input can interleave between them — without that, a click can land at
    wherever the pointer was moved to next rather than where it was aimed.

    Raises for an unknown button rather than defaulting to left: substituting a
    button performs a different gesture (a left click activates where a right click
    would have opened a menu), which is the same reason ``keymap`` refuses an
    unknown modifier.

    **A TRUNCATED batch is released, not ignored.** ``SendInput`` returns how many
    records it accepted and can stop early — a lower-level hook rejecting the
    injection, or ``UIPI`` blocking it mid-batch — so a batch cut between a
    button-down and its button-up leaves the operator's real mouse button PHYSICALLY
    HELD. Nothing later un-holds it: every subsequent motion of their own hand becomes
    a drag, which on a file manager or a canvas moves or destroys their data. So the
    count is checked, any accepted-but-unreleased button is released, and the caller is
    told the click failed rather than being allowed to read silence as success. This is
    the mouse twin of ``send_key_chord``'s unconditional reverse release.
    """
    flags = _MOUSE_FLAGS.get(button)
    if flags is None:
        raise ComputerUseUnsupported(f"unknown mouse button {button!r}")
    down, up = flags
    records = [_move_to(x, y)]
    for _ in range(max(1, count)):
        records.append(_mouse_event(down))
        records.append(_mouse_event(up))
    try:
        accepted = _send(records)
    except Exception:
        # The batch may have been partly applied before the failure, so the release
        # runs on this path too — for the same reason as the truncation path below.
        _send_release([_mouse_event(up)])
        raise
    if accepted < len(records):
        # A press is at an ODD index (0 is the move) and its release at the next even
        # one, so an odd accepted count means the last thing delivered was a DOWN.
        # Release unconditionally rather than only then: an extra up on an already-up
        # button is a no-op, while a missed one is a stuck button.
        _send_release([_mouse_event(up)])
        raise ComputerUseUnsupported(
            f"the click was only partly delivered ({accepted} of {len(records)} input "
            "records accepted, likely a lower-level hook or UIPI); the button was "
            "released so the pointer is not stuck"
        )


def post_mouse_drag(
    start: "tuple[float, float]",
    end: "tuple[float, float]",
    *,
    button: str,
    points: "Sequence[tuple[float, float]] | None" = None,
) -> None:
    """Press at *start*, travel through *points*, release at the end — REAL cursor.

    *points* is the whole path including both endpoints; ``None`` means the
    two-point form ``(start, end)``. ``start`` and ``end`` are still taken
    separately, and are what the press and release are aimed at, so a caller that
    passes a path cannot accidentally press somewhere the confinement check did not
    authorize.

    **Each path point is submitted as its OWN ``SendInput`` call, and that is the
    difference between drawing a curve and drawing a polyline.** It looks like the
    opposite of what :func:`_send`'s docstring asks for, so the measurement is worth
    stating: 65 move records in one batch drew a visibly corner-y polyline, while the
    same 65 points as 65 separate calls drew a smooth curve. A ``WH_MOUSE_LL`` hook
    counted 65 of 65 events reaching the queue in BOTH shapes, so nothing is dropped
    — Windows synthesizes ``WM_MOUSEMOVE`` from queue *state* when the target's
    message pump asks, rather than queueing one message per injected event, so a
    single batch mutates the cursor position many times before the target samples it
    even once. Separate calls yield between them, which is what lets the pump observe
    the intermediate positions. An inter-call *sleep* was measured to add nothing (0ms,
    2ms and 8ms drew identically), so there is none: it would only cost wall-clock
    while the operator's mouse button is held.

    **The press and the release are each AIMED, in one batch with their own move.** The
    press is ``[move(start), down]`` and the release is ``[move(end), up]``, and the
    aimed release is not decoration: a bare ``up`` record carries no coordinate, so it
    releases wherever the cursor happens to be when the OS processes it. With the path
    now submitted as N separate calls there are N yield points where the old single
    batch had one, so a foreign mouse move — the operator's own hand, another injector —
    can land between the last path point and the release. Since a release is where a
    DROP lands, and ``start``/``end`` are the two points ``hwnd_owns_point``
    authorized, re-aiming immediately before the ``up`` is what keeps the drop inside
    the window that was checked. It costs one extra record.

    The release is submitted even if the press or any move was rejected, for the same
    reason the keyboard releases unconditionally: a mouse button left down is a stuck
    button on the operator's real mouse, and every subsequent motion of their own hand
    becomes a drag, which on a file manager moves their files. The ``finally`` covers
    an exception; the accepted counts cover the quieter case of a ``SendInput``
    truncated mid-way (a lower-level hook, or UIPI), which returns NORMALLY. Both end
    with the button released, and a short delivery is reported rather than passed off
    as done — a drag that pressed and moved but never released is exactly the
    stuck-button state, and reporting success would leave the caller believing the drop
    landed.

    The release batch is built BEFORE the travel loop, so the ``finally`` cannot fail
    while constructing it: ``_move_to`` normalizes against the virtual screen and can
    raise, and a release that raised on the way to being sent would leave the button
    held — the one outcome this whole discipline exists to prevent.
    """
    flags = _MOUSE_FLAGS.get(button)
    if flags is None:
        raise ComputerUseUnsupported(f"unknown mouse button {button!r}")
    down, up = flags
    path = list(points) if points else [start, end]
    # The endpoints are the caller's, not the path's: they are the two points
    # ``hwnd_owns_point`` authorized for the press and the release specifically, so a
    # path whose own ends drifted by a float epsilon must not move them.
    path[0], path[-1] = start, end

    # Built BEFORE the travel, so the ``finally`` below cannot fail while CONSTRUCTING
    # the release: ``_move_to`` normalizes against the virtual screen and raises on a
    # zero extent, and a release that raised on its way to being sent would leave the
    # button held — the one outcome the whole release discipline exists to prevent.
    release = [_move_to(*path[-1]), _mouse_event(up)]

    # The aim and the press in one batch (see _send: nothing may interleave between
    # them), then one call per remaining point, then the aimed release.
    submitted = 0
    accepted = 0
    try:
        first = [_move_to(*path[0]), _mouse_event(down)]
        submitted += len(first)
        accepted += _send(first)
        for point in path[1:]:
            submitted += 1
            accepted += _send([_move_to(*point)])
    finally:
        # Unconditional AND verified: ``_send_release`` retries the unaccepted tail
        # and RAISES rather than letting a held button pass silently. A failed release
        # raising from ``finally`` replaces whatever the travel raised, and that
        # precedence is deliberate — a held button is worse than a failed drag and is
        # the condition the operator has to act on.
        #
        # AIMED at ``end``, not a bare ``up``: a button-up record carries no coordinate,
        # so it releases wherever the cursor is when the OS processes it. The path now
        # goes out as N separate calls, so there are N yield points where a foreign
        # mouse move can land — and a release is where a DROP lands, at a point
        # ``hwnd_owns_point`` authorized.
        #
        # ``atomic`` because the two records are not independent: the move is what aims the
        # up. A tail-only retry after a partial acceptance would resend the bare ``up`` and
        # give back exactly the unaimed release this batch exists to prevent.
        _send_release(release, atomic=True)
    if accepted < submitted:
        raise ComputerUseUnsupported(
            f"the drag was only partly delivered ({accepted} of {submitted} input "
            "records accepted, likely a lower-level hook or UIPI); the button was "
            "released so the pointer is not stuck"
        )


__all__ = [
    "CACHED_WALK_PROPERTIES",
    "E_ACCESSDENIED",
    "MAX_WALK_SECS",
    "ExpandCollapseState_Collapsed",
    "ExpandCollapseState_Expanded",
    "ExpandCollapseState_LeafNode",
    "ScrollAmount_LargeDecrement",
    "ScrollAmount_LargeIncrement",
    "ScrollAmount_NoAmount",
    "WalkResult",
    "do_default_action",
    "expand_collapse_state",
    "expand_element",
    "invoke_element",
    "pattern",
    "pattern_action",
    "scroll_element",
    "select_element",
    "element_has_focus",
    "set_element_focus",
    "set_element_value",
    "toggle_element",
    "UIA_IsPasswordPropertyId",
    "VARIANT",
    "WindowInfo",
    "add_ref",
    "automation",
    "available",
    "bstr_value",
    "cached_prop",
    "create_cache_request",
    "dpi_awareness_scope",
    "element_from_hwnd",
    "is_secure_element",
    "libraries",
    "owned",
    "prop_value_ex",
    "release",
    "release_all",
    "reset_libraries",
    "release_all_clients",
    "reset_thread_state",
    "root_window_at_point",
    "variant_value",
    "walk_bounded",
    "window_bounds",
    "window_class",
    "window_is_live",
    "window_is_minimized",
    "window_list",
    "window_is_on_screen",
    "window_pid",
    "window_render_scale",
    "window_root",
    "window_text",
]


# The owner modules, lowest layer first: the first one that binds a name answers a read
# of it. ``kiro_crew.computer_use.windows_ffi.<name>`` stays the import path and the patch
# surface for every name they define: a patch through this module reaches every owner that
# binds the name (see :mod:`kiro_crew._facade_mirror`).
_OWNERS = (
    "kiro_crew.computer_use.windows_ffi_com",
    "kiro_crew.computer_use.windows_ffi_uia",
    "kiro_crew.computer_use.windows_ffi_window",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.computer_use.windows_ffi_com import (  # noqa: F401
        _FN_SPECS,
        _LIB_NAMES,
        _VTABLE,
        CLSCTX_INPROC_SERVER,
        COINIT_MULTITHREADED,
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        DPI_AWARENESS_CONTEXT_SYSTEM_AWARE,
        DPI_AWARENESS_CONTEXT_UNAWARE,
        E_ACCESSDENIED,
        GA_ROOT,
        GUID,
        POINT,
        PROCESS_QUERY_LIMITED_INFORMATION,
        RECT,
        RPC_E_CHANGED_MODE,
        S_OK,
        VARIANT,
        VT_ARRAY,
        VT_ARRAY_R8,
        VT_BOOL,
        VT_BSTR,
        VT_EMPTY,
        VT_I2,
        VT_I4,
        VT_I8,
        VT_NULL,
        VT_R8,
        VT_UNKNOWN,
        AutomationElementMode_Full,
        CLSID_CUIAutomation_STR,
        ExpandCollapseState_Collapsed,
        ExpandCollapseState_Expanded,
        ExpandCollapseState_LeafNode,
        ExpandCollapseState_PartiallyExpanded,
        IID_IUIAutomation_STR,
        ScrollAmount_LargeDecrement,
        ScrollAmount_LargeIncrement,
        ScrollAmount_NoAmount,
        ScrollAmount_SmallDecrement,
        TreeScope_Children,
        TreeScope_Descendants,
        TreeScope_Element,
        TreeScope_Subtree,
        UIA_BoundingRectanglePropertyId,
        UIA_ControlTypePropertyId,
        UIA_ExpandCollapseExpandCollapseStatePropertyId,
        UIA_ExpandCollapsePatternId,
        UIA_HasKeyboardFocusPropertyId,
        UIA_InvokePatternId,
        UIA_IsEnabledPropertyId,
        UIA_IsExpandCollapsePatternAvailablePropertyId,
        UIA_IsInvokePatternAvailablePropertyId,
        UIA_IsPasswordPropertyId,
        UIA_IsSelectionItemPatternAvailablePropertyId,
        UIA_IsTogglePatternAvailablePropertyId,
        UIA_LegacyIAccessiblePatternId,
        UIA_NamePropertyId,
        UIA_ScrollPatternId,
        UIA_ScrollPatternNoScroll,
        UIA_SelectionItemIsSelectedPropertyId,
        UIA_SelectionItemPatternId,
        UIA_TogglePatternId,
        UIA_ValueIsReadOnlyPropertyId,
        UIA_ValuePatternId,
        UIA_ValueValuePropertyId,
        WindowInfo,
        _bind,
        _clients,
        _clients_generation,
        _clients_lock,
        _ct,
        _ensure_apartment,
        _guid,
        _libs,
        _libs_lock,
        _thread_state,
        _VariantUnion,
        add_ref,
        automation,
        available,
        libraries,
        logger,
        owned,
        release_all,
        release_all_clients,
        reset_libraries,
        reset_thread_state,
        vcall,
    )
    from kiro_crew.computer_use.windows_ffi_uia import (  # noqa: F401
        CACHED_WALK_PROPERTIES,
        MAX_WALK_SECS,
        WalkResult,
        _any_has_children,
        _cached_children,
        _safearray_doubles,
        _variant,
        bstr_value,
        cached_prop,
        create_cache_request,
        do_default_action,
        element_from_hwnd,
        element_has_focus,
        expand_collapse_state,
        expand_element,
        invoke_element,
        is_secure_element,
        pattern,
        pattern_action,
        prop_value_ex,
        scroll_element,
        select_element,
        set_element_focus,
        set_element_value,
        toggle_element,
        variant_value,
        walk_bounded,
    )
    from kiro_crew.computer_use.windows_ffi_window import (  # noqa: F401
        DWMWA_CLOAKED,
        MDT_EFFECTIVE_DPI,
        MONITOR_DEFAULTTONEAREST,
        _exe_name,
        dpi_awareness_scope,
        root_window_at_point,
        window_bounds,
        window_class,
        window_is_live,
        window_is_minimized,
        window_is_on_screen,
        window_list,
        window_pid,
        window_render_scale,
        window_root,
        window_text,
    )
