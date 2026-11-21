"""The keys and modifiers the ``browser`` tool may press.

``press_key``, ``type {steps}`` and ``reflex {keys}`` all check a key against
:data:`NAMED_KEYS` (or a single character), and ``press_key`` and ``click`` turn
``modifiers`` into CDP's bitmask with :func:`modifier_mask`.
"""

from __future__ import annotations

from typing import Any

#: CDP takes modifiers as a bitmask: Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8.
MODIFIER_BITS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "command": 4, "cmd": 4, "shift": 8}

#: Modifier bits under which a single-character key inserts no text (Ctrl, Meta).
NON_TEXT_MODIFIERS = MODIFIER_BITS["control"] | MODIFIER_BITS["meta"]

#: Keys pressed by name. Harness's ``press_key`` has no table row for the last three
#: rows, so it sends them with ``keyCode`` 0: a page reading ``event.key`` or
#: ``event.code`` sees them, one reading only ``keyCode`` does not. ContextMenu sends
#: the key only: Chromium opens no menu for it, so a page menu needs a right click.
NAMED_KEYS = frozenset(
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
        "Insert",
        "ContextMenu",
        *(f"F{n}" for n in range(1, 13)),
    }
)


def modifier_mask(modifiers: Any) -> int:
    """``modifiers`` as CDP's bitmask: a whole number, or a list of names."""
    if isinstance(modifiers, int) and not isinstance(modifiers, bool):
        return modifiers & 15
    if not isinstance(modifiers, list):
        return 0
    mask = 0
    for m in modifiers:
        mask |= MODIFIER_BITS.get(str(m or "").lower(), 0)
    return mask


def known_key(key: Any) -> bool:
    """A named key or a single character: what ``press_key`` accepts."""
    return isinstance(key, str) and (key == "Space" or key in NAMED_KEYS or len(key) == 1)
