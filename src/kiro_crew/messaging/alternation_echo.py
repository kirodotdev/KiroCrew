"""The backend's alternation placeholder, as the model repeats it in a reply.

A turn that ends on a tool result (a quiet end, a tool-only turn, an interrupted
turn) leaves the backend's history with no assistant message after it. On the
next request the backend fills that gap with a self-describing assistant
placeholder, "(No assistant response was recorded for the preceding turn. ...)",
so the roles still alternate. The model then reads it as its own earlier reply
and opens a later turn by writing it again. That text is not a reply: it must
never become a transcript row.

A turn loop that reads the stream's text itself streams each prompt through
:func:`drop_echo`: a team-channel agent's post (``channel._stream_task``),
``kirocrew chat`` (``cli_chat._send_and_print``), a task-runner step
(``task_executor.execute_task``), a sub-agent's prompts
(``subagent_manager/run_turn.py``), an eval scenario turn (``eval/runner.py``) and
a webhook hook turn (``dashboard/handlers/hooks.py``). The native Slack turn
streams each prompt through it too, and the channel turn driver each timed-out
call's continuation: their own hold, below, covers only a turn's first text.

The live stream holds a segment while it could still be this text and drops it
when it is: the dashboard runner (``stream_chunks._on_text_chunk``), the channel
turn driver (``messaging.driver.TurnDriver``), the shared collector
(``llm_helpers.stream_and_collect``) and the native Slack turn
(``slack/handler_runtime/stream.py``) alike. A turn whose only text was the
placeholder then gets the usual empty-reply handling. A row an older build
stored with only that text (``is_stored_echo_row``) stays where it is on
disk; the dashboard sends it as the say-nothing reply and the scans that pick
what a person sees pass over it. The model's own history is untouched: the
backend keeps its placeholder.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import replace
from typing import Any

from kiro_crew.agent_sdk.context import (
    CONTEXT_EVENT_COMPLETED,
    CONTEXT_EVENT_TEXT,
    CONTEXT_EVENT_TOOL,
)

#: How every spelling of the placeholder opens, the backend's long form and the
#: short form a model repeats alike.
OPENER = "(No assistant response was recorded"

#: The whole placeholder: the opener, then text with no parenthesis, then ``)``.
_ECHO_RE = re.compile(r"\s*" + re.escape(OPENER) + r"[^()]*\)")

#: Past this many characters an unclosed opener is not the placeholder (its long
#: form is under 200), so a held segment that grows longer is released.
_MAX_ECHO_CHARS = 400


async def drop_echo(events: AsyncIterable[Any]) -> AsyncIterator[Any]:
    """Yield one prompt's *events* without the placeholder its text opens with.

    For a turn loop that reads the stream's text itself. The prompt's opening
    text chunks are held while they could still be the placeholder; a tool call,
    the completion and the stream's end settle them: the whole placeholder is
    dropped, anything else goes out as it came. Words after the placeholder go
    out on the chunk that ended it. Every other event passes as it arrives.
    """
    held: list[Any] | None = []  # None once the prompt's text has started
    async for event in events:
        if held is not None and event.kind == CONTEXT_EVENT_TEXT:
            held.append(event)
            text = "".join(e.text for e in held)
            if could_be_echo(text):
                continue
            cut = echo_prefix_len(text)
            # The placeholder, then the model's own words after it.
            out = [replace(event, text=text[cut:].lstrip())] if cut else held
            held = None
            for chunk in out:
                yield chunk
            continue
        if held and event.kind in (CONTEXT_EVENT_TOOL, CONTEXT_EVENT_COMPLETED):
            out = _unless_echo(held)
            held = None if out else []
            for chunk in out:
                yield chunk
        yield event
    for chunk in _unless_echo(held or []):
        yield chunk


def _unless_echo(chunks: list[Any]) -> list[Any]:
    """*chunks*, or none when their text is the whole placeholder."""
    return [] if is_echo("".join(e.text for e in chunks)) else chunks


def echo_prefix_len(text: str) -> int:
    """Length of the placeholder that *text* starts with, or 0 when it does not."""
    m = _ECHO_RE.match(text)
    return m.end() if m else 0


def is_echo(text: str) -> bool:
    """Whether *text* is the placeholder and nothing else but whitespace."""
    n = echo_prefix_len(text)
    return bool(n) and not text[n:].strip()


def could_be_echo(text: str) -> bool:
    """Whether *text* is the placeholder, or the start of it as it streams in."""
    if len(text) > _MAX_ECHO_CHARS:
        return False
    if is_echo(text):
        return True
    t = text.lstrip()
    if len(t) <= len(OPENER):
        return OPENER.startswith(t)
    if not t.startswith(OPENER):
        return False
    return "(" not in t[len(OPENER) :] and ")" not in t[len(OPENER) :]


def is_stored_echo_row(row: dict) -> bool:
    """Whether *row* is an assistant row of only the placeholder, with no variants or file changes."""
    content = row.get("content")
    meta = row.get("meta")
    file_changes = meta.get("file_changes") if isinstance(meta, dict) else None
    return (
        row.get("role") == "assistant"
        and isinstance(content, str)
        and is_echo(content)
        and not row.get("variants")
        and not (isinstance(file_changes, list) and file_changes)
    )
