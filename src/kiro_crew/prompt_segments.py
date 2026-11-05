"""An outgoing prompt as segments that each say where their text came from.

A prompt is mostly injected context -- memory, lessons, a member's rules and
notes, steering, replayed history, a work ledger, a cron job's previous result
-- with the user's own turn near the end. The prompt builder
(``kiro_crew.acp.prompt_blocks.build_prompt_blocks``) upgrades text into extra
content blocks (today: a readable image path becomes an image block), and an
upgrade asserts that the user sent it with THIS request. So the builder reads
only :data:`SOURCE_USER` segments; every :data:`SOURCE_INJECTED` segment goes
out as text, as written.

:class:`SegmentedPrompt` is a ``str`` whose value is exactly the concatenation of
its segments, so every transport step that treats the prompt as text keeps
working unchanged. A string operation on it (concatenation, ``replace``) returns
a plain ``str``, and a plain ``str`` reads as ONE user segment: the behaviour every
caller had before segments existed. A step that rewrites a segmented prompt and
must keep its provenance uses :func:`replace_in_injected`.

A leaf module: ``kiro_crew.context`` builds segmented prompts and the ACP layer
reads them, and the agent-sdk boundary forbids application code from importing
``kiro_crew.acp``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

#: The user's own words in this turn: the only text a builder may upgrade.
SOURCE_USER = "user"
#: Everything the assembly added around them; sent as text, never upgraded.
SOURCE_INJECTED = "injected"
_SOURCES = (SOURCE_USER, SOURCE_INJECTED)


class PromptSegment(NamedTuple):
    source: str
    text: str


class SegmentedPrompt(str):
    """A prompt string that also carries its segments' provenance."""

    segments: tuple[PromptSegment, ...]

    def __new__(cls, segments: Iterable[PromptSegment | tuple[str, str]]) -> SegmentedPrompt:
        merged: list[PromptSegment] = []
        for source, text in segments:
            if source not in _SOURCES:
                raise ValueError(f"unknown prompt segment source: {source!r}")
            if not text:
                continue
            if merged and merged[-1].source == source:
                merged[-1] = PromptSegment(source, merged[-1].text + text)
            else:
                merged.append(PromptSegment(source, text))
        self = super().__new__(cls, "".join(text for _, text in merged))
        self.segments = tuple(merged)
        return self

    def __reduce__(self):  # type: ignore[override]
        # Persisted or pickled, a prompt is text; provenance is per send.
        return (str, (str(self),))


def segments_of(message: str) -> tuple[PromptSegment, ...]:
    """*message*'s segments; a plain string is one user segment."""
    if isinstance(message, SegmentedPrompt):
        return message.segments
    return (PromptSegment(SOURCE_USER, message),) if message else ()


def prepend_injected(prefix: str, message: str) -> str:
    """Prepend injected text without losing an assembled prompt's provenance."""
    if not prefix:
        return message
    if isinstance(message, SegmentedPrompt):
        return SegmentedPrompt([(SOURCE_INJECTED, prefix), *message.segments])
    return prefix + message


def around(message: str, start: int, end: int) -> SegmentedPrompt:
    """*message* with ``[start:end]`` as the user's text and the rest injected."""
    if not 0 <= start <= end <= len(message):
        raise ValueError("user span outside the prompt")
    return SegmentedPrompt(
        [
            PromptSegment(SOURCE_INJECTED, message[:start]),
            PromptSegment(SOURCE_USER, message[start:end]),
            PromptSegment(SOURCE_INJECTED, message[end:]),
        ]
    )


def replace_in_injected(message: str, old: str, new: str) -> str:
    """*message* with the first *old* inside one injected segment replaced by *new*.

    Provenance survives: the user segment is never searched or touched. A plain
    string is a single user segment, so it falls back to ``str.replace``.
    """
    if not isinstance(message, SegmentedPrompt):
        return message.replace(old, new, 1)
    out: list[PromptSegment] = []
    done = False
    for source, text in message.segments:
        if not done and source == SOURCE_INJECTED and old in text:
            text = text.replace(old, new, 1)
            done = True
        out.append(PromptSegment(source, text))
    return SegmentedPrompt(out) if done else message
