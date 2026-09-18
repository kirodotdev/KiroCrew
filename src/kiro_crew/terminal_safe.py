"""Terminal-safe rendering for untrusted text.

This module is a stdlib-only leaf so CLI, doctor, and lightweight HTTP clients
can share one control-sequence policy without importing each other.
"""

from __future__ import annotations

import re

__all__ = ["safe_terminal_line"]

# Strip complete OSC and CSI sequences, other two-byte ESC sequences, and C0/C1
# controls while preserving newlines and tabs. OSC must precede the generic ESC
# alternative so its payload is removed with its introducer and terminator.
_TERMINAL_CTRL_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC through BEL or ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI with the full ECMA-48 parameter class
    r"|\x1b[ -/]*[@-~]"  # other two-byte ESC sequences
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # C0/C1 controls (keep \n and \t)
)


_TERMINAL_TEXT_MAX = 2000


def safe_terminal_line(value: str) -> str:
    """Return bounded text confined to ONE terminal line with no live controls.

    For renderers that print a prefix per line (``✅``/``⚠️``/``❌``), a newline
    in untrusted text would start an unprefixed line that reads as the CLI's own
    output. Newlines are rendered as the visible ``\\x0a`` literal (the convention
    ``doctor_deadpath`` already uses) before the length cap, so the cap applies to
    what is actually printed; tabs are kept, and carriage returns fall in the C0
    range the pattern strips.
    """
    cleaned = _TERMINAL_CTRL_RE.sub("", value).replace("\n", "\\x0a")
    if len(cleaned) > _TERMINAL_TEXT_MAX:
        return cleaned[: _TERMINAL_TEXT_MAX - 1] + "…"
    return cleaned
