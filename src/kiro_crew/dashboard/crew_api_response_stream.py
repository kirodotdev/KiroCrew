"""The events of ``POST /v1/responses`` with ``stream: true``: rows in, events out.

No I/O here. :class:`Events` turns one turn's text and its final ``Response`` into the
Responses stream events (``docs/request-for-change/rfc-crew-api.md``, section 3.3;
``ResponseStreamEvent`` in the contract), each with its ``sequence_number``. The route
in ``crew_api_responses.py`` writes them.

The terminal event follows the JSON answer's status (:func:`terminal_event`), but its
``Response`` carries the concatenation of this stream's redacted text deltas.
"""

from __future__ import annotations

import json
from typing import Any, Final

from kiro_crew.dashboard.openai_compat import _redact
from kiro_crew.security import StreamRedactor

#: The event that ends a stream, by the ``Response``'s status. The contract and the
#: ``openai`` client have no event for a stopped turn: it ends ``response.incomplete``
#: and the ``Response`` inside says ``cancelled``, as the JSON answer does.
_TERMINAL_EVENTS: Final = {
    "completed": "response.completed",
    "failed": "response.failed",
    "incomplete": "response.incomplete",
    "cancelled": "response.incomplete",
}


def terminal_event(status: str) -> str:
    """The event type that ends the stream of a ``Response`` with *status*."""
    return _TERMINAL_EVENTS[status]


def encode(event: dict[str, Any]) -> bytes:
    """*event* as one SSE frame: its type on the ``event:`` line, the object as ``data:``."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class Events:
    """One response's stream events, in order. The message item is *item_id*."""

    def __init__(self, item_id: str) -> None:
        self._item_id = item_id
        self._sequence = 0
        self._redactor = StreamRedactor(_redact)
        self._sent = ""
        self._open = False

    def _event(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {"type": kind, "sequence_number": self._sequence, **fields}
        self._sequence += 1
        return event

    def _part(self, text: str) -> dict[str, Any]:
        return {"type": "output_text", "text": text, "annotations": []}

    def _item(self, status: str, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "message",
            "id": self._item_id,
            "role": "assistant",
            "status": status,
            "content": content,
        }

    def _at(self) -> dict[str, Any]:
        return {"item_id": self._item_id, "output_index": 0, "content_index": 0}

    def _delta(self, delta: str) -> list[dict[str, Any]]:
        """Emit safe text, opening the item first."""
        if not delta:
            return []
        events = []
        if not self._open:
            self._open = True
            events = [
                self._event(
                    "response.output_item.added",
                    output_index=0,
                    item=self._item("in_progress", []),
                ),
                self._event("response.content_part.added", **self._at(), part=self._part("")),
            ]
        self._sent += delta
        return [*events, self._event("response.output_text.delta", **self._at(), delta=delta)]

    def start(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """The two events that open a stream; *response* is the turn still running."""
        return [
            self._event("response.created", response=response),
            self._event("response.in_progress", response=response),
        ]

    def text(self, content: str) -> list[dict[str, Any]]:
        """Feed reply text through the stream redactor; emit only its safe prefix."""
        return self._delta(self._redactor.feed(content))

    def end(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Flush held text and close with exactly the text the deltas carried."""
        events = self._delta(self._redactor.flush())
        output = []
        if self._open:
            part = self._part(self._sent)
            # A failed JSON answer has no item; its stream still closes the opened item.
            item = (
                {**response["output"][0], "content": [part]}
                if response["output"]
                else self._item("incomplete", [part])
            )
            output.append(item)
            events += [
                self._event("response.output_text.done", **self._at(), text=self._sent),
                self._event("response.content_part.done", **self._at(), part=part),
                self._event("response.output_item.done", output_index=0, item=item),
            ]
        response = {**response, "output": output}
        return [*events, self._event(terminal_event(response["status"]), response=response)]

    def error(self, message: str) -> list[dict[str, Any]]:
        """The event for a turn that could not be run; the stream closes after it."""
        return [self._event("error", code="internal_error", message=message, param=None)]
