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


# ── JSON Parsing ──


_JSON_DECODER = json.JSONDecoder()


def _extract_json_of_type(
    text: str,
    expected_type: type | tuple[type, ...],
    prefer: Callable[[Any], bool] | None = None,
) -> dict | list | None:
    """Extract the first top-level JSON value of *expected_type* embedded in prose.

    Scans successive ``{`` (dict) or ``[`` (list) offsets and uses the stdlib
    ``raw_decode`` to parse a complete JSON value at each — this validates the
    full JSON grammar and correctly handles nesting and string escapes. Returns
    the first value that matches *expected_type*, or None.

    Scanning successive offsets (rather than committing to the first delimiter)
    is what makes this robust to a stray structural brace in the prose preamble
    (e.g. ``"use {placeholder}: {\\"a\\": 1}"``). Only TOP-LEVEL matches count: a
    ``{`` nested inside an earlier-starting ``[ ... ]`` is consumed by that
    array's decode, so a dict request never digs a nested object out of a
    surrounding array.

    When *prefer* is given, a preferred value is returned only when the choice
    is UNAMBIGUOUS: all preferred matches in the text must be equal (a model
    restating the same payload twice is not ambiguity). Two or more DIFFERENT
    preferred matches return None — the caller cannot know which one is the
    real payload, and guessing (e.g. executing a worked example that precedes
    the actual plan) is worse than failing. When no preferred match exists,
    the first type-matching value is returned as a fallback.
    """
    preferred: list[dict | list] = []
    fallback: dict | list | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Only attempt a decode at a JSON container start. Scanning BOTH
        # delimiters in positional order (not just the expected one) is what
        # prevents digging a nested object out of a surrounding array: a
        # leading "[ ... ]" is decoded as a list, found to be the wrong type,
        # and skipped past in full — so a dict request on "[1, {\\"a\\":2}]"
        # returns None rather than the inner {"a":2}.
        if ch not in "{[":
            i += 1
            continue
        try:
            data, end = _JSON_DECODER.raw_decode(text, i)
        except RecursionError:
            # Adversarially deep nesting (e.g. "[" * 100_000 in prose): the
            # stdlib decoder recurses per nesting level and overflows long
            # before any structural bound. This text is untrusted model output,
            # and callers handle only JSONDecodeError (parse_json's ValueError
            # contract, the spine extractor's never-raises contract) — so the
            # error must not escape. Fail the WHOLE scan closed: a truncated
            # scan cannot certify a preferred match as unambiguous, so keeping
            # candidates collected before the bomb would let a worked example
            # launder past the ambiguity refusal.
            # Callers already have recovery paths for None (schema retry loop,
            # the spine's forcing re-emit); salvaging a prefix of a reply that
            # contains a nesting bomb is not worth defeating them.
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

    Background turns (e.g. memory consolidation) run on a shared lite session.
    On the Claude Code backend that session is not tool/persona-scoped the way
    kiro's no-tools lite agent is, so the model may wrap the JSON in prose. To
    keep consolidation from silently no-opping, fall back to extracting the
    first top-level JSON value of the expected type when a strict parse fails.
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
        # Fallback: extract the first top-level JSON value of the expected type
        # embedded in prose (scans successive delimiters, validates via stdlib).
        result = _extract_json_of_type(text, expected_type)
        if result is None:
            logger.debug("Failed to parse LLM JSON: %.200s", text)
        return result


def parse_llm_json(text: str) -> dict | None:
    """Parse JSON dict from LLM output, stripping markdown fences if present."""
    return _parse_llm(text, dict)  # type: ignore[return-value]


def parse_llm_json_list(text: str) -> list | None:
    """Parse a JSON array from LLM output, stripping markdown fences."""
    return _parse_llm(text, list)  # type: ignore[return-value]


# ── Conversation History Helpers ──


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

    Consolidates the repeated pattern of appending user and assistant
    messages with provenance tracking.  When *agent* is supplied it is
    recorded in the session metadata on file creation so that
    ``/kirocrew sessions`` displays the correct agent name.
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

    Returns the ``ts`` of the row this turn ended on, read back INSIDE the atomic
    hold so it is this turn's own row and not some later writer's. A caller that
    stamps something with "how far the conversation had got" needs that value, and
    re-reading the tail afterwards is not the same thing: the permit for this
    session is released before the caller gets here, so a queued second turn can
    land its rows in between and the re-read would return ITS position. Taking the
    value under the lock we already hold costs nothing and removes the window
    rather than narrowing it.

    :func:`save_conversation_turn` makes TWO ``ConversationLog.append`` calls, and
    append acquires a cross-process flock and writes to disk -- ~12 ms each on a
    large transcript. Called directly from an ``async def`` that is worse than
    slow: on a running loop ``_locked`` makes a single NON-blocking acquire and
    raises :class:`~kiro_crew.history.HistoryLockTimeout` on any concurrent
    holder, and most callers swallow that, so the durable copy was dropped
    exactly when another writer was active. Off the loop the same primitive takes
    the patient poll-to-deadline path instead.

    This is the single choke point for every async caller, so the offload cannot
    be forgotten at a new call site and the ten Slack sites do not each restate
    it.

    Unlike :func:`~kiro_crew.history.append_off_loop`, this **awaits** the write
    rather than firing it at the executor and returning. That difference is
    deliberate: callers here go on to refresh a dashboard tab or hand the session
    to consolidation, both of which read the transcript back, so the turn has to
    be on disk before the caller continues. ``append_off_loop`` has no such
    reader and can afford to be fire-and-forget.

    The whole turn is written under one :meth:`~kiro_crew.history.ConversationLog.atomic_appends`
    hold. ``append`` locks per ROW, so without it two concurrent turns for the
    same session could interleave into ``user_A, user_B, assistant_A,
    assistant_B`` -- turns that do not pair up, which no timestamp ordering can
    repair because each row's ``ts`` is individually correct. On the loop that was
    impossible (a synchronous caller never yields between its two appends), so the
    hazard is introduced BY offloading and has to be closed here rather than
    inherited.
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
            # ``atomic_appends``), so this reuses the hold rather than
            # deadlocking on it -- and reading it here rather than after the
            # hold is released is the whole point.
            return log.last_row_ts(key)

    return await asyncio.to_thread(_write)
