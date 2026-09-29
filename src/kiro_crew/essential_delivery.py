"""Conversation-local receipts for complete, validated private context.

Building a prompt never commits a receipt. Only its successful, non-empty raw
terminal can do that; the transport retains the full candidate for retries.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar

from kiro_crew.agent_sdk import (
    CONTEXT_EVENT_AGENT_CHANGED,
    CONTEXT_EVENT_CLEAR,
    CONTEXT_EVENT_COMPACTION,
    ContextStreamEvent,
)

EventT = TypeVar("EventT", bound=ContextStreamEvent)

# Stream observations that invalidate the acknowledged snapshot: compaction
# rewrites native history, a clear empties it, and an agent switch replaces the
# identity that read it.
RECEIPT_INVALIDATING_EVENTS = frozenset(
    {CONTEXT_EVENT_COMPACTION, CONTEXT_EVENT_CLEAR, CONTEXT_EVENT_AGENT_CHANGED}
)

# Slash commands whose successful execution discards native history. Their
# status notification may arrive alongside or after the command's own stream,
# so a provider invalidates before dispatch instead of waiting for the receipt.
# Other slash commands (/help, /model, /context, …) keep prior evidence.
RECEIPT_RESETTING_COMMANDS = frozenset({"/compact", "/clear"})


@asynccontextmanager
async def _closing_events(
    events: AsyncIterator[EventT],
) -> AsyncIterator[AsyncIterator[EventT]]:
    try:
        yield events
    finally:
        if isinstance(events, AsyncGenerator):
            await events.aclose()


@dataclass(frozen=True)
class EssentialReceipt:
    envelope: str
    digest: str
    native_envelope: str
    incarnation: object
    epoch: int


class EssentialDelivery:
    """One provider's current candidate and last acknowledged conversation state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: EssentialReceipt | None = None
        self._acknowledged: tuple[object, str, int] | None = None
        self._epoch = 0

    def bind(
        self,
        envelope: str,
        *,
        scope: tuple[object, ...],
        incarnation: object,
        force: bool = False,
        native_envelope: str | None = None,
    ) -> None:
        """Stage trusted builder output; retain complete text until wire dispatch.

        Callers must serialize builds/sends with their existing session lease.
        Previews omit the delivery object and cannot change live receipts.
        """
        digest = hashlib.sha256(
            json.dumps([scope, envelope], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock:
            if force:
                self._epoch += 1
                self._acknowledged = None
            self._pending = EssentialReceipt(
                envelope, digest, native_envelope or envelope, incarnation, self._epoch
            )

    def invalidate(self) -> None:
        """Compaction, clear or replacement makes prior delivery evidence unusable."""
        with self._lock:
            self._epoch += 1
            self._acknowledged = None

    def prepare_command(self, command: str) -> bool:
        """Invalidate before a history-discarding slash command reaches the wire.

        Returns whether the command resets native history. Unrelated slash
        commands leave the receipt untouched.
        """
        head = command.split(maxsplit=1)[:1]
        resets = bool(head) and head[0] in RECEIPT_RESETTING_COMMANDS
        if resets:
            self.invalidate()
        return resets

    def _invalidate_attempt(self, candidate: EssentialReceipt | None, epoch: int) -> None:
        with self._lock:
            if candidate is not None and self._pending is candidate and self._epoch == epoch:
                self._epoch += 1
                self._acknowledged = None

    async def stream(
        self,
        message: str,
        send: Callable[..., AsyncIterator[EventT]],
        incarnation: Callable[[], object],
        on_accepted: Callable[[str, int], None] | None = None,
    ) -> AsyncGenerator[EventT, None]:
        """Deduplicate only trusted, byte-matched builder content on the wire.

        ``on_accepted`` is told the FINAL text — after the receipt substitution
        below — plus the length of the text as it ARRIVED here (the arrival
        length is what the assembler sized and the usage row reports; the
        substitution is the one step between the two, so a reader comparing them
        needs both numbers rather than a guess at the delta). It fires once the
        transport has taken the prompt and never when the write raised: when
        ``on_accepted`` is given, ``send`` is called as ``send(message,
        on_sent=...)`` and the transport calls ``on_sent`` the moment its write
        returned, before it awaits a single event — so a backend that dies
        between the write and its first event cannot un-send the prompt (the
        two transports' ``stream_events`` / ``prompt`` take ``on_sent``). A
        sender that never calls ``on_sent`` is still accepted at its first event,
        or at a completion with none. This is what :mod:`kiro_crew.prompt_trace`
        records: a prompt the pipe refused must not appear in the developer view
        as sent, and a record made before the write would have nothing to
        retract it.
        """
        from kiro_crew.agent_sdk import CONTEXT_EVENT_COMPLETED as EVENT_COMPLETE
        from kiro_crew.agent_sdk import CONTEXT_EVENT_TEXT as EVENT_TEXT_CHUNK
        from kiro_crew.agent_sdk import CONTEXT_EVENT_TOOL as EVENT_TOOL_CALL
        from kiro_crew.agent_sdk import TURN_STOP_REASON_END_TURN as STOP_REASON_END_TURN

        assembled_chars = len(message)
        identity = incarnation()
        with self._lock:
            candidate = self._pending
            epoch = self._epoch
            acknowledged = self._acknowledged
        if candidate is not None and candidate.envelope not in message:
            candidate = None
        if candidate is not None:
            if acknowledged == (identity, candidate.digest, epoch):
                replacement = ""
            elif candidate.incarnation == identity and candidate.epoch == epoch:
                replacement = candidate.native_envelope
            else:
                replacement = candidate.envelope
            message = message.replace(candidate.envelope, replacement, 1)
        productive = False
        completed = False
        accepted = False

        def _accept() -> None:
            # Once. Normally from the transport's on_sent, right after its write;
            # at the latest before the first event is yielded, since a collector
            # may close the generator right after that event.
            nonlocal accepted
            if not accepted:
                accepted = True
                if on_accepted is not None:
                    on_accepted(message, assembled_chars)

        try:
            events_in = send(message, on_sent=_accept) if on_accepted is not None else send(message)
            async with _closing_events(events_in) as events:
                async for event in events:
                    _accept()
                    if event.kind in RECEIPT_INVALIDATING_EVENTS:
                        self.invalidate()
                    if event.kind == EVENT_TOOL_CALL or (
                        event.kind == EVENT_TEXT_CHUNK and event.text and not event.control_notice
                    ):
                        productive = True
                    if event.kind == EVENT_COMPLETE:
                        completed = (
                            event.stop_reason == STOP_REASON_END_TURN
                            and not event.synthetic_completion
                            and not event.refusal
                        )
                        if (
                            candidate is not None
                            and completed
                            and productive
                            and candidate.incarnation == identity
                            and incarnation() == identity
                        ):
                            with self._lock:
                                if self._pending is candidate and self._epoch == epoch:
                                    self._acknowledged = (identity, candidate.digest, epoch)
                    yield event
            # A stream that ended with no event still completed its write.
            _accept()
        except GeneratorExit:
            # Collectors may close immediately after consuming a raw terminal.
            raise
        except BaseException:
            self._invalidate_attempt(candidate, epoch)
            raise
        finally:
            if not (completed and productive):
                self._invalidate_attempt(candidate, epoch)
