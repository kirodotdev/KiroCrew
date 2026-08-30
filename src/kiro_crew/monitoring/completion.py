"""Runtime-only completion adapter shared by monitor delivery surfaces."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kiro_crew.agent_sdk import (
    TURN_STOP_REASON_CANCELLED,
    TURN_STOP_REASON_END_TURN,
    AgentTurnUsage,
)
from kiro_crew.monitoring.models import (
    MonitorActionCompletion,
    MonitorActionDisposition,
)

MonitorCompletionCallback = Callable[[MonitorActionCompletion], Awaitable[None]]
_SYNTHETIC_STOP_REASONS = frozenset(
    {
        "",
        TURN_STOP_REASON_END_TURN,
        "timeout",
        "stale_recover",
        "error: cancel unacked",
        "error: tool stall",
        "error: compaction failed",
    }
)


def is_monitor_completion_evidence(stop_reason: str) -> bool:
    """Return whether a terminal event proves that an agent turn completed."""
    return stop_reason not in _SYNTHETIC_STOP_REASONS


def disposition_for_stop_reason(stop_reason: str) -> MonitorActionDisposition:
    """Map an authoritative provider stop reason onto monitor accounting."""
    if stop_reason == TURN_STOP_REASON_END_TURN:
        return MonitorActionDisposition.SUCCESS
    if stop_reason == TURN_STOP_REASON_CANCELLED:
        return MonitorActionDisposition.CANCELLATION
    return MonitorActionDisposition.FAILURE


@dataclass(frozen=True)
class MonitorCompletionHook:
    """Bind a surface's raw turn result to one monitor action identity."""

    monitor_id: str
    fingerprint: str
    callback: MonitorCompletionCallback

    def __post_init__(self) -> None:
        for name in ("monitor_id", "fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(self.callback):
            raise ValueError("callback must be callable")

    async def complete(
        self,
        disposition: MonitorActionDisposition,
        usage: AgentTurnUsage | None = None,
        *,
        completed_ts: float | None = None,
    ) -> None:
        """Deliver one normalized record to the controller callback."""
        input_tokens, output_tokens = _authoritative_token_counts(usage)
        await self.callback(
            MonitorActionCompletion(
                monitor_id=self.monitor_id,
                fingerprint=self.fingerprint,
                disposition=disposition,
                completed_ts=time.time() if completed_ts is None else completed_ts,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )


def _authoritative_token_counts(
    usage: AgentTurnUsage | None,
) -> tuple[int | None, int | None]:
    """Return token dimensions only when the provider reported real counts."""
    if usage is None:
        return None, None
    try:
        input_tokens = int(usage.input_tokens)
        output_tokens = int(usage.output_tokens)
    except (TypeError, ValueError, OverflowError):
        return None, None
    if input_tokens < 0 or output_tokens < 0:
        return None, None
    if input_tokens == 0 and output_tokens == 0:
        # Kiro ACP bills in credits and leaves both token fields at their
        # dataclass defaults. Zero therefore means unavailable on that seam,
        # not authoritative proof that a completed agent turn used no tokens.
        return None, None
    return input_tokens, output_tokens
