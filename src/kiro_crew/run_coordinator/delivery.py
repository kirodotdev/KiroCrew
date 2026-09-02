"""Fenced delivery of committed run-coordinator outbox events."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import (
    CoordinatorDecision,
    CoordinatorResult,
    DeliveryFence,
    DeliveryState,
    OutboxEvent,
    OwnerLease,
    RunCoordinator,
)

logger = logging.getLogger(__name__)

# The destination includes the manager's 20-minute parent-injection cap plus
# up to 60 seconds of teardown. A claim must remain fenced for that entire
# valid attempt or another drainer can deliver the same completion concurrently.
_DELIVERY_LEASE_SECONDS = 22 * 60.0
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 300.0
_MAX_RETRY_EXPONENT = 63


@dataclass(frozen=True)
class DeliveryAttempt:
    """Observable result of one claimed delivery event."""

    event_id: str
    status: DeliveryState


class OutboxDeliveryAdapter:
    """Claim, deliver, and acknowledge events through one fenced boundary.

    The destination returns ``True`` only after it has durably accepted the
    event. ``False`` means delivery was intentionally deferred (for example, a
    dashboard turn queue owns it); the event becomes pending after one lease
    window while the eventual consumer may acknowledge the same stable identity.
    """

    def __init__(
        self,
        coordinator: RunCoordinator,
        destination: Callable[[OutboxEvent], Awaitable[bool]],
        *,
        owner_id: str | None = None,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = _DELIVERY_LEASE_SECONDS,
        retry_base_seconds: float = _RETRY_BASE_SECONDS,
        retry_max_seconds: float = _RETRY_MAX_SECONDS,
    ) -> None:
        self._coordinator = coordinator
        self._destination = destination
        self._owner_id = owner_id or f"delivery:{uuid.uuid4().hex}"
        self._clock = clock
        self._lease_seconds = max(1.0, lease_seconds)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._retry_max_seconds = max(self._retry_base_seconds, retry_max_seconds)
        self._lock = asyncio.Lock()
        self._inflight: dict[str, DeliveryFence] = {}
        self._accepted: set[str] = set()

    def _owner_lease(self) -> OwnerLease:
        return OwnerLease(self._owner_id, self._clock() + self._lease_seconds)

    @staticmethod
    def _fence(event: OutboxEvent) -> DeliveryFence:
        return DeliveryFence(event.event_id, event.claim_owner, event.claim_epoch)

    def _retry_at(self, attempts: int) -> float:
        exponent = min(max(0, attempts - 1), _MAX_RETRY_EXPONENT)
        delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2**exponent),
        )
        return self._clock() + delay

    async def _acknowledge_accepted(self, fence: DeliveryFence) -> CoordinatorResult[OutboxEvent]:
        """Retain an accepted event's fence until its durable ack settles."""

        failures = 0
        while True:
            try:
                result = await self._coordinator.mark_delivered(fence)
                if result.decision is not CoordinatorDecision.REJECTED:
                    self._accepted.discard(fence.event_id)
                    if self._inflight.get(fence.event_id) == fence:
                        self._inflight.pop(fence.event_id, None)
                    return result
                async with self._lock:
                    claimed = await self._coordinator.claim_outbox(
                        self._owner_lease(),
                        1,
                        event_id=fence.event_id,
                        acknowledgement=True,
                    )
                    if not claimed:
                        return result
                    fence = self._fence(claimed[0])
                    self._inflight[fence.event_id] = fence
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.warning(
                    "Outbox acknowledgement failed for accepted event %s",
                    fence.event_id,
                    exc_info=True,
                )
                exponent = min(failures - 1, _MAX_RETRY_EXPONENT)
                delay = min(
                    self._retry_max_seconds,
                    self._retry_base_seconds * (2**exponent),
                )
                await asyncio.sleep(delay)

    async def drain_once(self, limit: int = 16, event_id: str = "") -> list[DeliveryAttempt]:
        """Deliver one bounded claim batch, optionally for one exact event."""

        if limit <= 0:
            return []
        attempts: list[DeliveryAttempt] = []
        for _ in range(limit):
            # Claim only when the destination is about to run. A batch claim
            # would spend later events' lease while an earlier callback waits.
            async with self._lock:
                claimed = await self._coordinator.claim_outbox(
                    self._owner_lease(),
                    1,
                    event_id=event_id,
                )
                if not claimed:
                    break
                event = claimed[0]
                self._inflight[event.event_id] = self._fence(event)
            fence = self._fence(event)
            settled = False
            try:
                if event.event_id in self._accepted:
                    result = await self._acknowledge_accepted(fence)
                else:
                    try:
                        accepted = await self._destination(event)
                    except asyncio.CancelledError:
                        released = await self._coordinator.release_outbox(
                            fence,
                            self._retry_at(event.attempts),
                        )
                        settled = released.decision is not CoordinatorDecision.REJECTED
                        raise
                    except Exception:
                        logger.warning(
                            "Outbox delivery failed for event %s",
                            event.event_id,
                            exc_info=True,
                        )
                        released = await self._coordinator.release_outbox(
                            fence,
                            self._retry_at(event.attempts),
                        )
                        settled = released.decision is not CoordinatorDecision.REJECTED
                        if released.value is not None:
                            attempts.append(DeliveryAttempt(event.event_id, released.value.status))
                        continue

                    if accepted:
                        self._accepted.add(event.event_id)
                        result = await self._acknowledge_accepted(fence)
                    else:
                        result = await self._coordinator.release_outbox(
                            fence,
                            self._clock() + self._lease_seconds,
                        )
                settled = result.decision is not CoordinatorDecision.REJECTED
                if result.value is not None:
                    attempts.append(DeliveryAttempt(event.event_id, result.value.status))
            finally:
                if settled and self._inflight.get(event.event_id) == fence:
                    self._inflight.pop(event.event_id, None)
            if event_id:
                break
        return attempts

    async def acknowledge(self, event_id: str) -> OutboxEvent | None:
        """Acknowledge an event already accepted by a deferred destination."""

        fence = self._inflight.get(event_id)
        if fence is not None:
            result = await self._coordinator.mark_delivered(fence)
            if result.decision is not CoordinatorDecision.REJECTED:
                if self._inflight.get(event_id) == fence:
                    self._inflight.pop(event_id, None)
                return result.value
            # A deferred destination can settle just after drain_once releases
            # its claim. Re-claim the stable event identity instead of dropping
            # that acknowledgement because the captured fence became stale.
        async with self._lock:
            fence = self._inflight.get(event_id)
            if fence is not None:
                result = await self._coordinator.mark_delivered(fence)
                if result.decision is not CoordinatorDecision.REJECTED:
                    if self._inflight.get(event_id) == fence:
                        self._inflight.pop(event_id, None)
                    return result.value
            claimed = await self._coordinator.claim_outbox(
                self._owner_lease(),
                1,
                event_id=event_id,
                acknowledgement=True,
            )
            if not claimed:
                return None
            result = await self._coordinator.mark_delivered(self._fence(claimed[0]))
            if result.decision is CoordinatorDecision.REJECTED:
                return None
            return result.value
