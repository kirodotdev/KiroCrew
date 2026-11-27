"""The lead word of a ``tool`` row's content, written and read in one place.

Every tool call gets one ``tool`` row, ``"Tool: <title>"``. Some calls get a
hidden sibling sharing its ``tool_call_id``: ``"Approved: <title>"`` when the
person approved it, ``"Refused: <title> ..."`` when it did not run. Readers (the
crew timeline here, the dashboard in ``website/src/lib/toolRowContract.ts``)
tell the rows apart by that lead.

Older gateways led the same rows with a glyph (U+1F527 wrench, U+2705 check,
U+1F6AB no-entry). Saved history keeps those forever, so every reader accepts
both spellings. The two lists must change together.
"""

from __future__ import annotations

from typing import Literal

ToolRowKind = Literal["call", "approved", "refused"]

TOOL_CALL = "Tool:"
TOOL_APPROVED = "Approved:"
TOOL_REFUSED = "Refused:"

_LEADS: tuple[tuple[str, ToolRowKind], ...] = (
    (TOOL_CALL, "call"),
    ("\U0001f527", "call"),  # emoji-ok: old saved lead
    (TOOL_APPROVED, "approved"),
    ("\u2705", "approved"),  # emoji-ok: old saved lead
    (TOOL_REFUSED, "refused"),
    ("\U0001f6ab", "refused"),  # emoji-ok: old saved lead
)

_WORD: dict[ToolRowKind, str] = {
    "call": TOOL_CALL,
    "approved": TOOL_APPROVED,
    "refused": TOOL_REFUSED,
}


def _lead_of(text: str) -> tuple[str, ToolRowKind] | None:
    for lead in _LEADS:
        if text.startswith(lead[0]):
            return lead
    return None


def tool_row(kind: ToolRowKind, body: str) -> str:
    """The content of a tool row of *kind*: its lead word, a space, *body*."""
    return f"{_WORD[kind]} {body}"


def tool_row_kind(text: str) -> ToolRowKind | None:
    """Which tool row *text* is, from its lead (either spelling), else None."""
    lead = _lead_of(text or "")
    return lead[1] if lead else None


def strip_tool_row_lead(text: str) -> str:
    """*text* without its lead and the whitespace after it."""
    text = text or ""
    lead = _lead_of(text)
    return text[len(lead[0]) :].lstrip() if lead else text
