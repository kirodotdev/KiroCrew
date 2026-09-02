"""Pure admission, queue, and slot-accounting policy for subagents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SlotOwner(Protocol):
    """Minimal mutable state required for one-shot slot ownership."""

    _slot_released: bool


@dataclass(frozen=True)
class AdmissionDecision:
    should_queue: bool
    slot_free: bool
    retry_after: float | None


@dataclass(frozen=True)
class DrainDecision:
    entry: dict[str, Any] | None
    retry_after: float | None


class SubagentScheduler:
    """Own subagent capacity, stagger timing, and the opaque FIFO queue."""

    def __init__(self, *, max_concurrent: int, stagger_seconds: float) -> None:
        self.max_concurrent = max_concurrent
        self.stagger_seconds = max(0.0, stagger_seconds)
        self.running_count = 0
        self.last_start = 0.0
        self.queue: list[dict[str, Any]] = []

    def admission(self, now: float) -> AdmissionDecision:
        slot_free = self.running_count < self.max_concurrent
        retry_after = max(0.0, self.stagger_seconds - (now - self.last_start))
        if not slot_free:
            return AdmissionDecision(True, False, None)
        return AdmissionDecision(retry_after > 0.0, True, retry_after)

    def enqueue(self, entry: dict[str, Any]) -> None:
        self.queue.append(entry)

    def take_ready(self, now: float) -> DrainDecision:
        decision = self.admission(now)
        if not self.queue or not decision.slot_free:
            return DrainDecision(None, decision.retry_after)
        if decision.should_queue:
            return DrainDecision(None, decision.retry_after)
        return DrainDecision(self.queue.pop(0), 0.0)

    def continuation_delay(self) -> float | None:
        if not self.queue or self.running_count >= self.max_concurrent:
            return None
        return self.stagger_seconds

    def occupy(self, owner: SlotOwner, now: float) -> None:
        owner._slot_released = False
        self.running_count += 1
        self.last_start = now

    def reoccupy(self, owner: SlotOwner) -> None:
        owner._slot_released = False
        self.running_count += 1

    def try_reoccupy(self, owner: SlotOwner) -> bool:
        if self.running_count >= self.max_concurrent:
            return False
        self.reoccupy(owner)
        return True

    @staticmethod
    def claim_release(owner: SlotOwner) -> bool:
        if owner._slot_released:
            return False
        owner._slot_released = True
        return True

    def release(self, owner: SlotOwner) -> bool:
        if not self.claim_release(owner):
            return False
        self.running_count = max(0, self.running_count - 1)
        return True

    def remove(self, run_id: str) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        for entry in self.queue:
            target = str(entry.get("_preassigned_id") or "") == run_id
            (removed if target else retained).append(entry)
        self.queue = retained
        return removed

    def queued_depth(self, parent_session_key: str) -> int:
        return sum(
            1 for entry in self.queue if entry.get("parent_session_key", "") == parent_session_key
        )

    def contains_batch(self, batch_id: str) -> bool:
        return any(entry.get("batch_id") == batch_id for entry in self.queue)

    def find_conversation(self, conversation_key: str) -> dict[str, Any] | None:
        for entry in self.queue:
            key = str(entry.get("conversation_key") or "") or (
                f"subagent:{entry.get('_preassigned_id', '')}"
            )
            if key == conversation_key:
                return entry
        return None
