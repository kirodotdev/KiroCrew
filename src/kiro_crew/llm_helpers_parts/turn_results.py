"""What callers do with a finished turn's text: parse JSON out of it, save it to history.

Pulling a JSON object or array out of model output with fences or prose around it, and
persisting a conversation turn, off the event loop through the one choke point.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

logger = logging.getLogger("kiro_crew.llm_helpers")


_JSON_DECODER = json.JSONDecoder()


def _extract_json_of_type(
    text: str,
    expected_type: type | tuple[type, ...],
    prefer: Callable[[Any], bool] | None = None,
) -> dict | list | None:
    """Extract the first top-level JSON value of *expected_type* embedded in prose.

    Tries the stdlib ``raw_decode`` at each successive ``{`` / ``[`` offset, so
    a stray brace in the preamble (``"use {placeholder}: {\\"a\\": 1}"``) does
    not stop the scan. Only TOP-LEVEL values count: a ``{`` inside an earlier
    ``[ ... ]`` is consumed by that array's decode.

    With *prefer*, a preferred value is returned only when UNAMBIGUOUS: all
    preferred matches must be equal (a restated payload is fine). Different
    preferred matches return None, because guessing (say, running a worked
    example that precedes the real plan) is worse than failing. With no
    preferred match, the first value of the type is returned.
    """
    preferred: list[dict | list] = []
    fallback: dict | list | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Decode at BOTH delimiters in positional order: a leading "[ ... ]" is
        # decoded, found the wrong type and skipped in full, so a dict request
        # on "[1, {\\"a\\":2}]" returns None rather than the inner {"a":2}.
        if ch not in "{[":
            i += 1
            continue
        try:
            data, end = _JSON_DECODER.raw_decode(text, i)
        except RecursionError:
            # Deep nesting ("[" * 100_000) overflows the decoder, and callers
            # handle only JSONDecodeError. Fail the WHOLE scan closed: a truncated
            # scan cannot prove a preferred match unambiguous. Callers recover
            # from None.
            return None
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(data, expected_type):
            if prefer is None:
                return data  # type: ignore[return-value]
            if prefer(data):
                preferred.append(data)
            elif fallback is None:
                fallback = data  # type: ignore[assignment]
        # Valid JSON that is not an immediate result — skip past its full extent.
        i = end
    if preferred:
        first = preferred[0]
        if all(candidate == first for candidate in preferred[1:]):
            return first
        return None
    return fallback


def _parse_llm(text: str, expected_type: type) -> dict | list | None:
    """Parse JSON from LLM output, tolerating fences and surrounding prose.

    Background turns (e.g. memory consolidation) run on a shared lite session,
    which on the Claude Code backend is not persona-scoped like kiro's no-tools
    lite agent, so the model may wrap the JSON in prose. When a strict parse
    fails, extract the first top-level value of the type so consolidation does
    not silently no-op.
    """
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        if isinstance(data, expected_type):
            return data  # type: ignore[return-value]
        return None
    except json.JSONDecodeError:
        result = _extract_json_of_type(text, expected_type)
        if result is None:
            # The reply can quote the user's chat or a secret from it, so the
            # line names its size and the type wanted, never its text.
            logger.debug(
                "Failed to parse LLM JSON: %d chars, expected %s",
                len(text),
                expected_type.__name__,
            )
        return result


def parse_llm_json(text: str) -> dict | None:
    """Parse JSON dict from LLM output, stripping markdown fences if present."""
    return _parse_llm(text, dict)  # type: ignore[return-value]


def parse_llm_json_list(text: str) -> list | None:
    """Parse a JSON array from LLM output, stripping markdown fences."""
    return _parse_llm(text, list)  # type: ignore[return-value]


def save_conversation_turn(
    log: ConversationLog,
    key: str,
    user_text: str,
    assistant_text: str,
    source_thread: str | None = None,
    source_user: str | None = None,
    agent: str | None = None,
) -> None:
    """Save a user+assistant conversation turn to the history log.

    *agent*, when given, is recorded in the session metadata on file creation
    so ``/kirocrew sessions`` shows the right agent name.
    """
    log.append(
        key,
        "user",
        user_text,
        source_thread=source_thread,
        source_user=source_user,
        agent=agent,
    )
    if assistant_text:
        log.append(
            key,
            "assistant",
            assistant_text,
            source_thread=source_thread,
            source_user=source_user,
        )


async def save_conversation_turn_off_loop(
    log: ConversationLog,
    key: str,
    user_text: str,
    assistant_text: str,
    source_thread: str | None = None,
    source_user: str | None = None,
    agent: str | None = None,
) -> str | None:
    """Save a turn without blocking (or fail-fast-dropping on) the event loop.

    Returns the ``ts`` of this turn's last row, read INSIDE the atomic hold; a
    re-read afterwards could see a queued second turn's row instead.

    On a running loop ``_locked`` makes one NON-blocking acquire and raises
    :class:`~kiro_crew.history.HistoryLockTimeout` on any concurrent holder,
    dropping the durable copy; off the loop it polls to its deadline. Every async
    caller goes through here so a new call site cannot forget the offload.

    Unlike :func:`~kiro_crew.history.append_off_loop`, this **awaits** the write:
    callers read the transcript back next (a dashboard refresh, consolidation).

    The whole turn is written under one
    :meth:`~kiro_crew.history.ConversationLog.atomic_appends` hold. ``append``
    locks per ROW, so two concurrent turns could otherwise interleave into
    ``user_A, user_B, assistant_A, assistant_B``, which no ``ts`` sort repairs.
    """

    def _write() -> str | None:
        with log.atomic_appends(key):
            save_conversation_turn(
                log,
                key,
                user_text,
                assistant_text,
                source_thread=source_thread,
                source_user=source_user,
                agent=agent,
            )
            # Reentrant for the same key on the same thread (see
            # ``atomic_appends``), so this reuses the hold, not deadlocks.
            return log.last_row_ts(key)

    return await asyncio.to_thread(_write)
