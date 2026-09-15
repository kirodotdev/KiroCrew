"""Runtime-only completion adapter shared by monitor delivery surfaces."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

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
MonitorAuthorizationCallback = Callable[[str, str], Awaitable[bool]]
MonitorAcceptanceCallback = Callable[[], None]
_SYNTHETIC_STOP_REASONS = frozenset(
    {
        "",
        "timeout",
        "stale_recover",
        "error: cancel unacked",
        "error: tool stall",
        "error: compaction failed",
    }
)


def is_monitor_completion_evidence(
    stop_reason: str,
    *,
    synthetic: bool = False,
) -> bool:
    """Return whether a terminal event proves that an agent turn completed."""
    return not synthetic and stop_reason not in _SYNTHETIC_STOP_REASONS


def disposition_for_stop_reason(stop_reason: str) -> MonitorActionDisposition:
    """Map an authoritative provider stop reason onto monitor accounting."""
    if stop_reason == TURN_STOP_REASON_END_TURN:
        return MonitorActionDisposition.SUCCESS
    if stop_reason == TURN_STOP_REASON_CANCELLED:
        return MonitorActionDisposition.CANCELLATION
    return MonitorActionDisposition.FAILURE


@dataclass
class MonitorCompletionHook:
    """Bind a surface's raw turn result to one monitor action identity."""

    monitor_id: str
    fingerprint: str
    callback: MonitorCompletionCallback
    authorization_callback: MonitorAuthorizationCallback | None = None
    acceptance_callback: MonitorAcceptanceCallback | None = None
    _accepted: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("monitor_id", "fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(self.callback):
            raise ValueError("callback must be callable")
        if self.authorization_callback is not None and not callable(self.authorization_callback):
            raise ValueError("authorization_callback must be callable")
        if self.acceptance_callback is not None and not callable(self.acceptance_callback):
            raise ValueError("acceptance_callback must be callable")

    async def authorize(self) -> bool:
        """Revalidate the persisted claim at a surface's turn-start boundary."""
        callback = self.authorization_callback
        if callback is None:
            return True
        return await callback(self.monitor_id, self.fingerprint)

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

    @property
    def accepted(self) -> bool:
        """Whether a surface attached this correlation to a starting turn."""
        return self._accepted

    def mark_accepted(self) -> None:
        """Record the boundary after which completion evidence owns recovery."""
        if self._accepted:
            return
        callback = self.acceptance_callback
        if callback is not None:
            callback()
        self._accepted = True


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
