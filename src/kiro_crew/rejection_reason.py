"""The reason a person may type when they reject a tool call.

Its own module so the dashboard route that accepts the reason and the ACP layer
that sends it read one rule without the dashboard importing the ACP layer.
"""

from __future__ import annotations

#: The longest reason a person may give with a tool rejection. KAS reads it from
#: ``_meta.kiro.rejectionReason`` on a one-time Deny and hands it to the model as
#: "The user rejected this tool call: <reason>", with no cap of its own.
REJECTION_REASON_MAX_CHARS = 1000


def rejection_reason_text(value: object) -> str | None:
    """A person's rejection reason, cleaned, or None when it cannot be sent.

    Control characters other than newline and tab are dropped and the ends
    trimmed; ``""`` means no reason. None means *value* is not text or is longer
    than :data:`REJECTION_REASON_MAX_CHARS`, which a caller refuses rather than
    cuts, so the model never reads half of what the person wrote.
    """
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch in "\n\t" or not _is_control(ch)).strip()
    return text if len(text) <= REJECTION_REASON_MAX_CHARS else None


def _is_control(ch: str) -> bool:
    return ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0
