"""Drop the quiet-end token where the model's text enters Kiro Crew.

After ``nothing_to_do`` the model replies with exactly
:data:`~kiro_crew.session_directive.QUIET_END_TOKEN`, so the backend's history
ends on an assistant message. Every caller of the two ACP turn loops
(``AcpClient._dispatch_events``, ``AcpSessionHandle._dispatch_events``) reads
them through :func:`drop_end_token`, so no runner ever sees the token: each gets
what it gets today when the model writes nothing after the call.
"""

from __future__ import annotations

from contextlib import aclosing
from typing import AsyncGenerator

from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.session_directive import QUIET_END_TOKEN


def _text(chunks: list[AcpEvent]) -> str:
    return "".join(c.text for c in chunks).strip()


async def drop_end_token(
    events: AsyncGenerator[AcpEvent, None],
) -> AsyncGenerator[AcpEvent, None]:
    """Yield *events* minus every text segment that is exactly the end token.

    A segment is the run of text chunks between two other events, or before the
    end of the stream. Its chunks are held only while their stripped text is
    still a prefix of the token; once it is not, they go out unchanged and in
    order. A segment that equals the token when it ends is dropped.
    """
    held: list[AcpEvent] | None = []  # None: this segment stopped matching
    async with aclosing(events) as stream:
        async for event in stream:
            if event.kind != EVENT_TEXT_CHUNK:
                if held and _text(held) != QUIET_END_TOKEN:
                    for chunk in held:
                        yield chunk
                held = []
                yield event
            elif held is None:
                yield event
            else:
                held.append(event)
                if not QUIET_END_TOKEN.startswith(_text(held)):
                    for chunk in held:
                        yield chunk
                    held = None
    if held and _text(held) != QUIET_END_TOKEN:
        for chunk in held:
            yield chunk
