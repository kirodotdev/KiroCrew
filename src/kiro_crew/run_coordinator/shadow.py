"""Primary-preserving shadow adapter for additive coordinator migration."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from enum import Enum
from typing import Any, cast

from .models import (
    CommandClaim,
    CoordinatorDecision,
    CoordinatorResult,
    DeliveryFence,
    OutboxEvent,
    OwnerLease,
    RunCommand,
    RunCompletion,
    RunCoordinator,
    RunFence,
    RunRecord,
    SubmitReceipt,
    SubmitRun,
)

logger = logging.getLogger(__name__)

_VOLATILE_FIELDS = frozenset(
    {
        "available_at",
        "claim_expires_at",
        "created_at",
        "delivered_at",
        "lease_expires_at",
        "terminal_at",
        "updated_at",
    }
)
_GENERATED_IDENTIFIER_FIELDS = frozenset({"event_id"})
_IGNORED_PARITY_FIELDS = _VOLATILE_FIELDS | _GENERATED_IDENTIFIER_FIELDS
_MAX_MISMATCH_FIELDS = 8


def _normalize(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _normalize(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.name not in _IGNORED_PARITY_FIELDS
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _mismatch_fields(primary: Any, shadow: Any) -> frozenset[str]:
    """Return a bounded set of structural paths without including field values."""

    fields: set[str] = set()

    def collect(left: Any, right: Any, path: str) -> None:
        if len(fields) >= _MAX_MISMATCH_FIELDS or left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                child_path = f"{path}.{key}" if path else key
                if key not in left or key not in right:
                    fields.add(child_path)
                else:
                    collect(left[key], right[key], child_path)
                if len(fields) >= _MAX_MISMATCH_FIELDS:
                    return
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                fields.add(path or "value")
                return
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                collect(left_item, right_item, f"{path}[{index}]")
                if len(fields) >= _MAX_MISMATCH_FIELDS:
                    return
            return
        fields.add(path or "value")

    collect(_normalize(primary), _normalize(shadow), "")
    return frozenset(fields)


class ShadowRunCoordinator:
    """Return primary decisions while best-effort checking a shadow store."""

    def __init__(
        self,
        primary: RunCoordinator,
        shadow: RunCoordinator,
        *,
        on_mismatch: Callable[[str, frozenset[str]], None] | None = None,
    ) -> None:
        self._primary = primary
        self._shadow = shadow
        self._on_mismatch = on_mismatch

    async def _mirror(self, operation: str, *args: Any) -> Any:
        primary = await getattr(self._primary, operation)(*args)
        if (
            isinstance(primary, CoordinatorResult)
            and primary.decision is CoordinatorDecision.REJECTED
        ):
            return primary
        if primary is False:
            return primary
        try:
            shadow = await getattr(self._shadow, operation)(*args)
            fields = _mismatch_fields(primary, shadow)
            if fields:
                logger.warning(
                    "run coordinator shadow mismatch at boundary=%s fields=%s",
                    operation,
                    ",".join(sorted(fields)),
                )
                if self._on_mismatch is not None:
                    self._on_mismatch(operation, fields)
        except BaseException:
            # The primary decision is already committed. Shadow execution,
            # structural comparison, and observability are all subordinate and
            # must not replace it, including when cancellation lands in this
            # best-effort leg.
            logger.warning("run coordinator shadow failed at boundary=%s", operation)
        return primary

    async def submit(self, request: SubmitRun) -> CoordinatorResult[SubmitReceipt]:
        return cast(CoordinatorResult[SubmitReceipt], await self._mirror("submit", request))

    async def claim_commands(self, owner: OwnerLease, limit: int) -> list[CommandClaim]:
        return cast(list[CommandClaim], await self._mirror("claim_commands", owner, limit))

    async def mark_starting(
        self, command: RunCommand, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        return cast(
            CoordinatorResult[RunRecord],
            await self._mirror("mark_starting", command, fence, expected_version),
        )

    async def mark_running(
        self, run_id: str, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]:
        return cast(
            CoordinatorResult[RunRecord],
            await self._mirror("mark_running", run_id, fence, expected_version),
        )

    async def complete(
        self, completion: RunCompletion, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[OutboxEvent]:
        return cast(
            CoordinatorResult[OutboxEvent],
            await self._mirror("complete", completion, fence, expected_version),
        )

    async def renew(self, run_id: str, fence: RunFence, until: float) -> bool:
        return cast(bool, await self._mirror("renew", run_id, fence, until))

    async def claim_outbox(self, owner: OwnerLease, limit: int) -> list[OutboxEvent]:
        return cast(list[OutboxEvent], await self._mirror("claim_outbox", owner, limit))

    async def release_outbox(
        self, fence: DeliveryFence, available_at: float
    ) -> CoordinatorResult[OutboxEvent]:
        return cast(
            CoordinatorResult[OutboxEvent],
            await self._mirror("release_outbox", fence, available_at),
        )

    async def mark_delivered(self, fence: DeliveryFence) -> CoordinatorResult[OutboxEvent]:
        return cast(
            CoordinatorResult[OutboxEvent],
            await self._mirror("mark_delivered", fence),
        )

    async def get_run(self, run_id: str) -> RunRecord | None:
        return cast(RunRecord | None, await self._mirror("get_run", run_id))
