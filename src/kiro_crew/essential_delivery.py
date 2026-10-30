"""Conversation-local receipts for complete, validated private context.

Building a prompt never commits a receipt. Only its successful, non-empty raw
terminal can do that; the transport retains the full candidate for retries.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
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
from kiro_crew.agent_sdk.context import CONTEXT_EVENT_TOOL_RESULT, CONTEXT_TOOL_OPEN_STATUSES

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


def _tool_call_ended(event: object) -> bool:
    """Whether *event* is the update that ends a tool call, in any outcome."""
    if getattr(event, "kind", None) != CONTEXT_EVENT_TOOL_RESULT:
        return False
    if getattr(event, "tool_final", False) is True:
        return True
    status = getattr(event, "tool_status", "")
    return isinstance(status, str) and bool(status) and status not in CONTEXT_TOOL_OPEN_STATUSES


class EssentialDelivery:
    """One provider's current candidate and last acknowledged conversation state.

    It also records :attr:`request_floor`, the latest monotonic moment of the
    last stream that made a model request, known to be no later than the start of
    that stream's last request. The stream's own start sets it once the stream
    yields model output (text or a tool call); a stream with none, such as a
    ``/context`` command, leaves the previous value, and so does a stream whose
    caller says its transport makes no model request (``requests_model=False``). A tool call that ends moves it
    forward only once model output follows the end, because only that output
    shows a request started after the tool finished; a tool that ends a
    cancelled turn leaves it where it was.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._pending: EssentialReceipt | None = None
        self._acknowledged: tuple[object, str, int] | None = None
        self._epoch = 0
        self._clock = clock
        #: ``None`` until the first stream starts.
        self.request_floor: float | None = None

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
        send: Callable[[str], AsyncIterator[EventT]],
        incarnation: Callable[[], object],
        *,
        requests_model: bool = True,
    ) -> AsyncGenerator[EventT, None]:
        """Deduplicate only trusted, byte-matched builder content on the wire.

        *requests_model* is False when the transport makes no model request at
        all, as kiro's native ``_kiro.dev/commands/execute`` does: its output is
        the command's own result, so it never moves :attr:`request_floor`.
        """
        from kiro_crew.agent_sdk import CONTEXT_EVENT_COMPLETED as EVENT_COMPLETE
        from kiro_crew.agent_sdk import CONTEXT_EVENT_TEXT as EVENT_TEXT_CHUNK
        from kiro_crew.agent_sdk import CONTEXT_EVENT_TOOL as EVENT_TOOL_CALL
        from kiro_crew.agent_sdk import TURN_STOP_REASON_END_TURN as STOP_REASON_END_TURN

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
        # Before the send, so the stream's first request starts no earlier. Held
        # until model output arrives: a slash command such as ``/context`` makes
        # no model request, and must not move the floor past the last one that did.
        # A transport that makes no model request never holds one.
        pending_floor: float | None = self._clock() if requests_model else None
        tool_ended_at: float | None = None
        try:
            async with _closing_events(send(message)) as events:
                async for event in events:
                    if event.kind in RECEIPT_INVALIDATING_EVENTS:
                        self.invalidate()
                    if event.kind == EVENT_TOOL_CALL or (
                        event.kind == EVENT_TEXT_CHUNK and event.text and not event.control_notice
                    ):
                        productive = True
                        if tool_ended_at is not None:
                            # Model output after a tool ended: a request
                            # started after that end.
                            pending_floor = tool_ended_at
                            tool_ended_at = None
                        if pending_floor is not None:
                            self.request_floor = pending_floor
                            pending_floor = None
                    elif requests_model and _tool_call_ended(event):
                        tool_ended_at = self._clock()
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
        except GeneratorExit:
            # Collectors may close immediately after consuming a raw terminal.
            raise
        except BaseException:
            self._invalidate_attempt(candidate, epoch)
            raise
        finally:
            if not (completed and productive):
                self._invalidate_attempt(candidate, epoch)
