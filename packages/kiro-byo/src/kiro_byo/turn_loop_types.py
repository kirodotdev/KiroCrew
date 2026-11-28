"""Typed turn-loop state and directives; no provider or ACP dispatch logic.

Re-exported from turn_runner for compatibility with earlier extensions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .execution_guard import ExecutionGuard
from .turn_state import RoundOutcome, TurnState

@dataclass(frozen=True)
class _PostStageOutcome:
    text: str
    provider_id: str
    model_id: str
    usage: Any | None
    thinking: str = ""
    effort: str = ""
    elapsed_ms: float = 0.0
    applied: bool = False

@dataclass(frozen=True)
class _ProviderRoundOutcome:
    """Concrete provider result plus the small Turn Engine control summary."""

    turn: Any
    elapsed_ms: float
    control: RoundOutcome


@dataclass(frozen=True)
class _ToolBatchOutcome:
    """Aggregated effects of executing one already-emitted tool-call batch."""

    cancelled: bool = False
    steered: bool = False
    replan_requested: bool = False
    stop_requested: bool = False
    guard_reason: str = ""
    pending_replan_notice: str = ""
    tool_ms: float = 0.0
    mcp_wait_ms: float = 0.0
    native_calls: int = 0
    tool_search_calls: int = 0
    tool_search_loaded: int = 0


class _LoopDirective(str, Enum):
    """Explicit control-flow edges of ``_run_provider_tool_loop``.

    The names intentionally mirror the ten historical ``continue`` branches so
    characterization tests and code review can prove that step rewind semantics
    did not change while the 0.2.2 loop is split into smaller responsibilities.
    """

    NONE = "none"
    SUBAGENTS_HAND_BACK = "subagents_hand_back"
    STEER_AFTER_INTERRUPT = "steer_after_interrupt"
    WORKER_FAILED_HAND_BACK = "worker_failed_hand_back"
    RETRY_SAME_MODEL = "retry_same_model"
    PRISTINE_FALLBACK = "pristine_fallback"
    WORKER_FINDINGS_HAND_BACK = "worker_findings_hand_back"
    QUEUED_STEER_AFTER_FINAL = "queued_steer_after_final"
    WORKER_GUARD_HAND_BACK = "worker_guard_hand_back"
    EXECUTION_GUARD_REPLAN = "execution_guard_replan"
    STEER_AFTER_TOOLS = "steer_after_tools"
    FINAL = "final"
    STOP = "stop"
    RETURN = "return"


@dataclass
class _ProviderLoopState:
    """Mutable state shared across provider/tool rounds for one user turn."""

    provider: Any
    model: Any
    mode_fallbacks: list[tuple[Any, Any]]
    relay: Any
    turn_effort: str
    turn_thinking: str
    turn_run_id: str
    turn_trace_id: str

    step: int = 0
    relay_next_role: str = "lead"
    relay_rounds: int = 0
    first_visible_at: float | None = None
    last_visible_at: float | None = None
    previous_visible_at: float | None = None
    upstream_ms: float = 0.0
    tool_ms: float = 0.0
    mcp_wait_ms: float = 0.0
    tool_calls_total: int = 0
    provider_rounds_total: int = 0
    transport_retries_total: int = 0
    fallbacks_total: int = 0
    side_effect_risk_seen: bool = False
    side_effect_classes_seen: set[str] = field(default_factory=set)
    execution_guard_halted: bool = False
    execution_guard_reason: str = ""
    pending_replan_notice: str = ""
    execution_guard: ExecutionGuard = field(default_factory=ExecutionGuard)
    native_tool_calls_total: int = 0
    tool_search_calls_total: int = 0
    tool_search_loaded_total: int = 0
    tool_schema_bytes: int = 0
    active_tools_count: int = 0
    stream_chunks: int = 0
    stream_chars: int = 0
    max_chunk_chars: int = 0
    max_gap_ms: float = 0.0
    round_text_parts: list[str] = field(default_factory=list)
    round_reasoning_emitted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cache_usage_reported: bool = False
    cache_write_usage_reported: bool = False
    reasoning_usage_reported: bool = False
    served_model_id: str = ""
    served_provider_id: str = ""
    telemetry_recorded: bool = False
    final_text: str = ""
    observed_thinking_modes: set[str] = field(default_factory=set)
    observed_efforts: set[str] = field(default_factory=set)
    turn_state: TurnState = field(default_factory=TurnState)

    def add_usage(self, usage: Any) -> None:
        """Accumulate provider, reviewer and child usage with identical semantics.

        A child reports a dictionary, while model adapters return UsageInfo.
        Missing counters stay unknown rather than being treated as reported zero.
        """
        read = usage.get if isinstance(usage, dict) else lambda key, default=0: getattr(usage, key, default)
        for field_name in (
            "input_tokens", "output_tokens", "cached_tokens",
            "cache_write_tokens", "reasoning_tokens",
        ):
            setattr(self, field_name, getattr(self, field_name) + int(read(field_name, 0) or 0))
        for source, target in (
            ("cached_tokens_reported", "cache_usage_reported"),
            ("cache_write_tokens_reported", "cache_write_usage_reported"),
            ("reasoning_tokens_reported", "reasoning_usage_reported"),
        ):
            setattr(self, target, getattr(self, target) or bool(read(source, False)))


@dataclass(frozen=True)
class _ProviderRoundContext:
    provider: Any
    model: Any
    role: str
    stage: str
    effort: str
    thinking: str
    reasoning_signals: Any
    tools: list[dict[str, Any]]
    replan_notice: str = ""


class DurableCheckpointError(RuntimeError):
    """Raised when a fail-closed (pre-execution) durable checkpoint cannot be
    committed. The turn must abort before running side-effecting tools so a
    crash cannot leave an unclassifiable unknown_side_effect."""
