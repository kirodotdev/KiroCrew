"""The bound on ``tool_response.output`` handed to a ``PostToolUse`` hook.

Callers pass the redacted full output; the hook is told whether it was cut.
"""

from __future__ import annotations

#: The longest ``tool_response.output`` a ``PostToolUse`` hook Kiro Crew fires is handed.
HOOK_OUTPUT_CHAR_CAP = 2000


def bounded_hook_output(text: str) -> dict[str, object]:
    """``text`` cut to the cap, with ``truncated`` and the uncut ``output_length``."""
    return {
        "output": text[:HOOK_OUTPUT_CHAR_CAP],
        "truncated": len(text) > HOOK_OUTPUT_CHAR_CAP,
        "output_length": len(text),
    }
