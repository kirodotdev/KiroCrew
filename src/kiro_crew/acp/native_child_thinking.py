"""A Claude native child's thinking, read for that child's Subagents card.

claude-agent-acp 0.107.0 sends the thinking of an Agent-tool child as
``agent_thought_chunk`` on the child's own session: a non-empty ``thinking`` block
becomes the chunk (``sdk-to-acp.js`` 218-232), stamped
``_meta.claudeCode.parentToolUseId`` (504-507) and re-addressed to the child's
sessionId (``native-subagents.js`` ``routeUpdate`` 317-420, ``toChild`` 1017-1027),
for a client that declares ``clientCapabilities.subagents`` and is not JetBrains
AIR (``v1/acp-agent.js`` 4545-4556, 7273-7277). An empty block (display
``omitted``) sends nothing.

``client_native_children`` consumes every frame of a child it tracks;
:func:`child_thought_event` is the reading that turns such a thought into
``EVENT_SUBAGENT_THINKING`` naming the child, on a backend in
``ACP_BACKENDS_NATIVE_CHILD_THINKING``. A thought never becomes the parent's
thinking or the card's output text.

The event's text leaves here unredacted, as the parent's own thinking leaves
``AcpClient``: the dashboard's native-card sink stream-redacts it per card
(``dashboard.chat_turn.native_cards._on_subagent_thinking``). Any other reader of
the kind must redact it too; a reader that does not know the kind ignores it.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp._dispatch import parse_text_chunk
from kiro_crew.acp.types import UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK, AcpEvent
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_NATIVE_CHILD_THINKING

#: A native child's thinking, for its card only: ``sub_session_id`` names the child
#: and ``text`` is one thought delta, unredacted.
EVENT_SUBAGENT_THINKING = "subagent_thinking"


def child_thought_event(
    backend: str, child_sid: str, kind: object, update: dict[str, Any]
) -> AcpEvent | None:
    """The thinking event a frame of the tracked child *child_sid* makes, or None.

    A thought chunk, or a message chunk whose content is a reasoning type, with
    text, on a *backend* in ``ACP_BACKENDS_NATIVE_CHILD_THINKING``. Anything else
    is None, and the caller keeps its own handling of the frame.
    """
    if backend not in ACP_BACKENDS_NATIVE_CHILD_THINKING:
        return None
    if kind not in (UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK):
        return None
    text, is_thinking = parse_text_chunk(update)
    if not text or not is_thinking:
        return None
    return AcpEvent(kind=EVENT_SUBAGENT_THINKING, sub_session_id=child_sid, text=text)
