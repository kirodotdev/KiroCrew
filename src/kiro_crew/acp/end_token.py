"""Drop the quiet-end token where the model's text enters Kiro Crew.

After ``nothing_to_do`` the model replies with exactly
:data:`~kiro_crew.session_directive.QUIET_END_TOKEN`, so the backend's history
ends on an assistant message. Every caller of the two ACP turn loops
(``AcpClient._dispatch_events``, ``AcpSessionHandle._dispatch_events``) reads
them through :func:`drop_end_token`, so no runner ever sees the token: each gets
what it gets today when the model writes nothing after the call.

A quiet turn that also folded a mid-turn steer writes the token and then
kiro-cli's ``[STEERING steer-…]`` ack in the same text run. The token is
dropped there too and the ack goes on unchanged, so the steer is still recorded
and the chat draws what it draws for an ack-only reply.
"""

from __future__ import annotations

import dataclasses
from contextlib import aclosing
from typing import AsyncGenerator

from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.session_directive import QUIET_END_TOKEN

# A token reply padded past this many chars is released as text, so the hold stays bounded.
_HELD_CHARS_MAX = 64
# The acks after a token are held up to this many chars; past it the segment is released.
_HELD_ACK_CHARS_MAX = 16_384


def _text(chunks: list[AcpEvent]) -> str:
    return "".join(c.text for c in chunks).strip()


def _ack_tail(text: str, *, final: bool) -> bool | None:
    """Judge the text after the token: ``True`` when it is only steer acks and
    whitespace, ``False`` when it cannot become that, ``None`` while it still could."""
    from kiro_crew.messaging.driver import _SteeringMarkerFilter

    parser = _SteeringMarkerFilter()
    frames = parser.feed(text)
    if final:
        frames.extend(parser.flush())
    if any(kind == "text" and payload.strip() for kind, payload in frames):
        return False
    if not final:
        return None
    # flush drops an unclosed marker, so text that ends inside one is not all acks.
    return any(kind == "steer" for kind, _ in frames) and text.rstrip().endswith("]")


def _after_token(chunks: list[AcpEvent]) -> str | None:
    """The segment's text after a leading token, or ``None`` when it does not
    lead with one within ``_HELD_CHARS_MAX`` characters."""
    raw = "".join(c.text for c in chunks)
    joined = raw.lstrip()
    lead = len(raw) - len(joined) + len(QUIET_END_TOKEN)
    if lead > _HELD_CHARS_MAX or not joined.startswith(QUIET_END_TOKEN):
        return None
    return joined[len(QUIET_END_TOKEN) :]


async def drop_end_token(
    events: AsyncGenerator[AcpEvent, None],
) -> AsyncGenerator[AcpEvent, None]:
    """Yield *events* minus every text segment that is exactly the end token,
    and minus the token that leads a segment holding nothing else but steer acks.

    A segment is the run of text chunks between two other events, or before the
    end of the stream. Its chunks are held only while their stripped text is
    still a prefix of the token and their text is at most ``_HELD_CHARS_MAX``
    characters long, or, once the token is complete, while what follows it can
    still be only steer acks and the segment is at most ``_HELD_ACK_CHARS_MAX``
    characters long. Once either fails, they go out unchanged and in order.
    A segment that equals the token when it ends is dropped; one that is the
    token plus acks goes out as one chunk holding the text after the token.
    """
    held: list[AcpEvent] | None = []  # None: this segment stopped matching

    def _settle() -> list[AcpEvent]:
        if not held or _text(held) == QUIET_END_TOKEN:
            return []
        tail = _after_token(held)
        if tail is not None and _ack_tail(tail, final=True):
            return [dataclasses.replace(held[0], text=tail)]
        return held

    async with aclosing(events) as stream:
        async for event in stream:
            if event.kind != EVENT_TEXT_CHUNK:
                for chunk in _settle():
                    yield chunk
                held = []
                yield event
            elif held is None:
                yield event
            else:
                held.append(event)
                size = sum(len(c.text) for c in held)
                stripped = _text(held)
                if QUIET_END_TOKEN.startswith(stripped) and size <= _HELD_CHARS_MAX:
                    continue
                tail = _after_token(held)
                if (
                    tail is not None
                    and size <= _HELD_CHARS_MAX + _HELD_ACK_CHARS_MAX
                    and _ack_tail(tail, final=False) is None
                ):
                    continue
                for chunk in held:
                    yield chunk
                held = None
    for chunk in _settle():
        yield chunk
