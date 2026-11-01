"""Provider-neutral turn phase contracts for the Kiro BYO harness.

The 0.1.3 core-refinement work is splitting the historical provider/tool loop
into explicit phases.  This module defines the state vocabulary first, without
changing runtime behavior.  Concrete provider transports, native/MCP tools and
Tiga policy names intentionally do not appear here.

Keeping the contract provider-neutral makes the eventual Turn Engine boring:
it advances through a small lifecycle while routing, provider invocation, tool
execution and post-stage review remain replaceable capabilities beside it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TurnPhase(str, Enum):
    """Stable phase names for one user turn."""

    PREPARE = "prepare"
    ROUTE = "route"
    INVOKE = "invoke"
    STREAM = "stream"
    TOOL = "tool"
    CONTINUE = "continue"
    POST_STAGE = "post_stage"
    COMPLETE = "complete"


class RoundDisposition(str, Enum):
    """Provider-neutral result category for one model invocation."""

    FINAL = "final"
    TOOLS = "tools"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True)
class RoundOutcome:
    """Small decision-facing summary of one provider round.

    It deliberately stores identities and counts rather than provider-specific
    response objects.  Provider continuation blobs stay behind the provider
    seam; the Turn Engine only needs enough information to decide whether the
    turn completes, executes tools, continues after steering, or fails.
    """

    disposition: RoundDisposition
    provider_id: str = ""
    model_id: str = ""
    served_model_id: str = ""
    text: str = ""
    tool_call_count: int = 0
    interruption: str = ""
    error_kind: str = ""

    def __post_init__(self) -> None:
        if self.tool_call_count < 0:
            raise ValueError("tool_call_count must be non-negative")
        if self.disposition is RoundDisposition.TOOLS and self.tool_call_count <= 0:
            raise ValueError("tools outcome requires at least one tool call")
        if self.disposition is not RoundDisposition.TOOLS and self.tool_call_count:
            raise ValueError("only tools outcomes may carry tool calls")

    @property
    def requires_tool_batch(self) -> bool:
        return self.disposition is RoundDisposition.TOOLS

    @property
    def is_terminal_candidate(self) -> bool:
        return self.disposition in {
            RoundDisposition.FINAL,
            RoundDisposition.FAILED,
        }


@dataclass(frozen=True)
class PhaseTransition:
    """One in-memory state-machine transition for diagnostics/tests."""

    source: TurnPhase
    target: TurnPhase
    reason: str = ""


class TurnPhaseError(ValueError):
    """Raised when code attempts a lifecycle transition outside the contract."""


_ALLOWED_TRANSITIONS: dict[TurnPhase, frozenset[TurnPhase]] = {
    TurnPhase.PREPARE: frozenset({TurnPhase.ROUTE, TurnPhase.COMPLETE}),
    TurnPhase.ROUTE: frozenset({TurnPhase.INVOKE, TurnPhase.COMPLETE}),
    # INVOKE may return directly without a visible stream, fail back to routing
    # on a pristine first-round fallback, or be interrupted into CONTINUE.
    TurnPhase.INVOKE: frozenset({
        TurnPhase.ROUTE,
        TurnPhase.STREAM,
        TurnPhase.TOOL,
        TurnPhase.CONTINUE,
        TurnPhase.POST_STAGE,
        TurnPhase.COMPLETE,
    }),
    TurnPhase.STREAM: frozenset({
        TurnPhase.TOOL,
        TurnPhase.CONTINUE,
        TurnPhase.POST_STAGE,
        TurnPhase.COMPLETE,
    }),
    TurnPhase.TOOL: frozenset({TurnPhase.CONTINUE, TurnPhase.COMPLETE}),
    # CONTINUE is intentionally generic: a steer, relay hand-back, execution
    # guard re-plan or ordinary tool result can all lead to another invocation;
    # a future router implementation may also re-enter ROUTE explicitly.
    TurnPhase.CONTINUE: frozenset({
        TurnPhase.ROUTE,
        TurnPhase.INVOKE,
        TurnPhase.POST_STAGE,
        TurnPhase.COMPLETE,
    }),
    TurnPhase.POST_STAGE: frozenset({TurnPhase.COMPLETE}),
    TurnPhase.COMPLETE: frozenset(),
}


@dataclass
class TurnState:
    """Mutable lifecycle state for a single user turn.

    This type is intentionally free of Provider, Tool, Session and Policy Pack
    objects.  Integration into the live loop is a later refactor step; defining
    it first lets characterization tests freeze behavior before extraction.
    """

    phase: TurnPhase = TurnPhase.PREPARE
    round_index: int = 0
    provider_rounds: int = 0
    tool_batches: int = 0
    terminal_reason: str = ""
    transitions: list[PhaseTransition] = field(default_factory=list)

    def transition(self, target: TurnPhase, *, reason: str = "") -> None:
        target = TurnPhase(target)
        allowed = _ALLOWED_TRANSITIONS[self.phase]
        if target not in allowed:
            raise TurnPhaseError(
                f"invalid turn phase transition: {self.phase.value} -> {target.value}"
            )
        source = self.phase
        self.phase = target
        self.transitions.append(PhaseTransition(source, target, str(reason or "")))
        if target is TurnPhase.COMPLETE:
            self.terminal_reason = str(reason or self.terminal_reason or "complete")

    def note_provider_round(self) -> None:
        self.provider_rounds += 1
        self.round_index += 1

    def note_tool_batch(self) -> None:
        self.tool_batches += 1

    @property
    def complete(self) -> bool:
        return self.phase is TurnPhase.COMPLETE


def allowed_next_phases(phase: TurnPhase) -> frozenset[TurnPhase]:
    """Expose the contract for tests/diagnostics without mutable internals."""
    return _ALLOWED_TRANSITIONS[TurnPhase(phase)]
