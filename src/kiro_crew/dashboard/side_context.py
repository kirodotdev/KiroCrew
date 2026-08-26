"""Build the message string for a side turn.

Reads parent state directly from ``slot.messages``; does NOT call
``context.build_message`` (kept off the main path for baseline parity).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.dashboard.side_prompts import (
    SIDE_BOUNDARY_PROMPT,
    build_side_system_prompt,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot


_PARENT_SNAPSHOT_HEADER = "[Main conversation history — read-only background]"
_PARENT_SNAPSHOT_FOOTER = "[End of main conversation history]"
_SIDE_HISTORY_HEADER = "[Side conversation so far]"
_SIDE_HISTORY_FOOTER = "[End of side conversation history]"
_MAX_PARENT_SNAPSHOT_CHARS = 32_000
_MAX_SIDE_HISTORY_CHARS = 32_000
_PARENT_LINE_TRUNCATE = 500


def _format_parent_snapshot(slot: _ChatSlot) -> str:
    """Render parent user/assistant turns as a text block; tool/error rows skipped.

    Iterates newest-first and accumulates under the char cap, then re-reverses
    so the block stays chronological. This keeps the *most recent* turns when
    the transcript exceeds the cap — a side follow-up almost always concerns
    what just happened, so dropping the tail would starve the model of exactly
    the relevant context. Mirrors
    ``_format_side_history``.
    """
    lines: list[str] = []
    total = 0
    for entry in reversed(slot.messages):
        role = entry.get("role", "")
        if role not in ("user", "assistant"):
            continue
        label = "User" if role == "user" else "Assistant"
        text = entry.get("content", "") or ""
        text = text[:_PARENT_LINE_TRUNCATE]
        if role != "user":
            # Defence in depth. The restore path redacts `content` on load, so
            # slot.messages should already be clean here — but this block goes into
            # the side-chat PROMPT, which leaves the dashboard's own storage and is
            # persisted by kiro-cli into its session file. That is an egress path,
            # not an internal read, so it does not rely solely on an upstream
            # guarantee. Redaction is idempotent and this text is truncated to
            # _PARENT_LINE_TRUNCATE, so the cost is bounded.
            text, _ = redact_exfiltration_urls(text)
            text, _ = redact_credentials(text)
        line = f"{label}: {text}"
        if total + len(line) > _MAX_PARENT_SNAPSHOT_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    lines.reverse()
    body = "\n".join(lines)
    return f"{_PARENT_SNAPSHOT_HEADER}\n{body}\n{_PARENT_SNAPSHOT_FOOTER}"


def _format_side_history(slot: _ChatSlot) -> str:
    """Render prior side turns as a transcript block (first-turn cold-start only).

    Excludes a trailing user message — ``build_side_message`` appends the
    current question separately.
    """
    if slot._side is None:
        return ""
    entries = slot._side.messages
    if entries and entries[-1].get("role") == "user":
        entries = entries[:-1]
    # Iterate reversed to keep the most recent turns under the cap;
    # re-reverse before join so chronological order is preserved.
    lines: list[str] = []
    total = 0
    for entry in reversed(entries):
        role = entry.get("role", "")
        if role not in ("user", "assistant"):
            continue
        label = "User" if role == "user" else "Assistant"
        text = entry.get("content", "") or ""
        text = text[:_PARENT_LINE_TRUNCATE]
        line = f"{label}: {text}"
        if total + len(line) > _MAX_SIDE_HISTORY_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    lines.reverse()
    body = "\n".join(lines)
    return f"{_SIDE_HISTORY_HEADER}\n{body}\n{_SIDE_HISTORY_FOOTER}"


def build_side_message(
    slot: _ChatSlot,
    question: str,
    *,
    is_first_turn: bool,
) -> str:
    """First turn: full envelope (instructions + parent + side history + question).
    Subsequent turns: bare question (kiro-cli session retains framing).
    """
    question = question.strip()
    if not is_first_turn:
        return question
    parts: list[str] = [build_side_system_prompt()]
    parent_block = _format_parent_snapshot(slot)
    if parent_block:
        parts.append(parent_block)
    side_block = _format_side_history(slot)
    if side_block:
        parts.append(side_block)
    parts.append(SIDE_BOUNDARY_PROMPT)
    parts.append(f"User: {question}")
    return "\n\n".join(parts)
