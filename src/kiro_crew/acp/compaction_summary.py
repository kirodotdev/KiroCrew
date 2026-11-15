"""A native compaction's summary, carried to the dashboard's compaction card, on ``AcpClient``.

claude-agent-acp reports a compaction to a client that declares ``session.compaction``
as a ``compaction_update`` lifecycle keyed by ``compactionId``, and reports the summary
Claude Code kept of the conversation in two frames (0.107.0 ``context-compaction.js``)::

    {"sessionUpdate": "compaction_summary_chunk", "compactionId": ...,
     "content": {"type": "text", "text": ...}}
    {"sessionUpdate": "compaction_update", "compactionId": ..., "status": "completed",
     "summary": [{"type": "text", "text": ...}]}

The chunks are the API's compaction block as it streams, sent only into a compaction
the adapter opened with its ``in_progress`` update (``heartbeat``). The ``summary`` is
the PostCompact hook's text with its ``<analysis>`` block and tags removed
(``recordSummary``, ``compactionSummaryText``), sent on the first ``completed`` update
only and left out when the chunks carried that same text (``finish``). So a
``completed`` update's own summary wins, and the chunks stand in when it carries none.

:func:`note_chunk` keeps the chunks of each compaction in flight
(:class:`CompactionSummaries`, bounded in compactions and in characters), and
:func:`completed_summary` is the text the ``completed`` compaction event carries as
``AcpEvent.title``: the field the dashboard writes after "Conversation compacted:"
(``chat_utils._broadcast_compaction_result``) and the compaction card folds, as it
folds kiro-cli's summary. :func:`forget` drops the chunks of a compaction that ended
with no summary to show. The text is redacted and cut at
:data:`COMPACTION_SUMMARY_MAX_CHARS`; a cut first drops the word it would split, so no
partial credential or URL slips past the redaction.

Gated on ``ACP_BACKENDS_COMPACTION_SUMMARY``, and only a frame of this client's own
session is read (a frame that names no session is the session's own).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import liveness_key, redact_text
from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_COMPACTION_SUMMARY

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The session update a compaction's summary streams in, and the one its lifecycle
#: is reported in.
UPDATE_COMPACTION_SUMMARY_CHUNK = "compaction_summary_chunk"
UPDATE_COMPACTION = "compaction_update"

#: The most of one summary kept and shown, in characters. A Claude Code summary is a
#: few kilobytes; the card folds it behind a chevron and scrolls it.
COMPACTION_SUMMARY_MAX_CHARS = 65_536

#: Compactions whose chunks are kept at once. One is in flight at a time; the rest
#: are ones the adapter never closed, dropped oldest first.
COMPACTION_SUMMARIES_MAX = 4

#: The in-flight status ``NativeCompactionStates`` records for an open compaction.
_STARTED = "started"


def _bounded(text: str) -> str:
    """*text*, cut to the cap without splitting a word, so the redaction sees whole tokens."""
    if len(text) <= COMPACTION_SUMMARY_MAX_CHARS:
        return text
    head = text[: COMPACTION_SUMMARY_MAX_CHARS + 1]
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    return head[:cut] if cut > 0 else ""


def _shown(text: str) -> str:
    """*text* as the card may show it: bounded, redacted, trimmed."""
    return redact_text(_bounded(text))[:COMPACTION_SUMMARY_MAX_CHARS].strip()


class CompactionSummaries:
    """The summary text each compaction in flight has streamed, by ``compactionId``.

    Keyed by ``liveness_key``, as ``NativeCompactionStates`` is. Each compaction keeps
    at most one character past :data:`COMPACTION_SUMMARY_MAX_CHARS`, which tells
    :func:`_bounded` the text was cut; past :data:`COMPACTION_SUMMARIES_MAX`
    compactions the oldest is dropped.
    """

    def __init__(self) -> None:
        self._parts: dict[str, list[str]] = {}
        self._sizes: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._parts)

    def add(self, compaction_id: str, text: str) -> None:
        key = liveness_key(compaction_id)
        parts = self._parts.pop(key, [])
        size = self._sizes.pop(key, 0)
        room = COMPACTION_SUMMARY_MAX_CHARS + 1 - size
        if room > 0:
            parts.append(text[:room])
            size += min(len(text), room)
        self._parts[key] = parts
        self._sizes[key] = size
        while len(self._parts) > COMPACTION_SUMMARIES_MAX:
            oldest = next(iter(self._parts))
            del self._parts[oldest]
            del self._sizes[oldest]

    def take(self, compaction_id: str) -> str:
        """The text *compaction_id* streamed, forgotten as it is read."""
        key = liveness_key(compaction_id)
        self._sizes.pop(key, None)
        return "".join(self._parts.pop(key, ()))


def _own_update(client: AcpClient, msg: JsonRpcMessage, kind: str) -> dict[str, Any] | None:
    """The *kind* update of a ``session/update`` of *client*'s own session, else None."""
    if client.backend not in ACP_BACKENDS_COMPACTION_SUMMARY:
        return None
    params = msg.params
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != kind:
        return None
    if not msg.is_method(METHOD_SESSION_UPDATE):
        return None
    session_id = params.get("sessionId")
    own = client._session_id
    if isinstance(session_id, str) and session_id and own and session_id != own:
        return None
    return update


def note_chunk(client: AcpClient, msg: JsonRpcMessage) -> bool:
    """Keep the text of a ``compaction_summary_chunk``; True when *msg* is one.

    Only a chunk of a compaction in flight is kept: the adapter streams none into a
    compaction it has not opened, and one that has ended has had its event.
    """
    update = _own_update(client, msg, UPDATE_COMPACTION_SUMMARY_CHUNK)
    if update is None:
        return False
    compaction_id = update.get("compactionId")
    content = update.get("content")
    if not isinstance(content, dict) or content.get("type", "text") != "text":
        return True
    text = content.get("text")
    if (
        isinstance(compaction_id, str)
        and compaction_id
        and isinstance(text, str)
        and text
        and client._native_compaction_states.get(compaction_id) == _STARTED
    ):
        client._compaction_summaries.add(compaction_id, text)
    return True


def _frame_summary(update: dict[str, Any]) -> str:
    """The text blocks of a ``completed`` update's ``summary``, joined, or ""."""
    blocks = update.get("summary")
    if not isinstance(blocks, list):
        return ""
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict)
        and block.get("type", "text") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    return "\n\n".join(texts)


def completed_summary(client: AcpClient, msg: JsonRpcMessage) -> str:
    """The summary a ``completed`` ``compaction_update`` reports, or "".

    The update's own ``summary``, else what its compaction streamed. Either way the
    streamed text is forgotten.
    """
    update = _own_update(client, msg, UPDATE_COMPACTION)
    compaction_id = update.get("compactionId") if update is not None else None
    if update is None or not isinstance(compaction_id, str) or not compaction_id:
        return ""
    streamed = client._compaction_summaries.take(compaction_id)
    return _shown(_frame_summary(update) or streamed)


def forget(client: AcpClient, msg: JsonRpcMessage) -> None:
    """Drop what the compaction *msg* ends streamed: it ended with no summary to show."""
    update = _own_update(client, msg, UPDATE_COMPACTION)
    compaction_id = update.get("compactionId") if update is not None else None
    if isinstance(compaction_id, str) and compaction_id:
        client._compaction_summaries.take(compaction_id)
