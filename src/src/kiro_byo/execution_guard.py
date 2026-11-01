"""Adaptive runaway/stagnation guard for long agent turns.

Normal productive work is intentionally *not* bounded by a small model-round
counter.  The guard watches actual tool actions instead: repeated identical
work and short repeating cycles trigger one re-planning warning, and only
continued stagnation stops the turn.  A very high emergency tool-call cap is
kept solely as a final backstop if every higher-level detector fails.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
from typing import Any


# Similar in spirit to mature coding agents: crossing the adaptive threshold
# does not stop a productive run; it only marks the run as long and increases
# observability.  The emergency cap should be unreachable during normal work.
ADAPTIVE_TOOL_CALL_THRESHOLD = 100
EMERGENCY_TOOL_CALL_LIMIT = 1000

# Repeating exactly the same tool+arguments+result four times is suspicious.
# After the model has been warned/re-planned, two more identical observations
# are enough to classify the run as stagnant.
_EXACT_WARN_COUNT = 4
_EXACT_HALT_COUNT = 6
_RECENT_WINDOW = 18


@dataclass(frozen=True)
class GuardDecision:
    action: str = "continue"  # continue | warn | halt
    reason: str = ""
    adaptive_threshold_reached: bool = False
    tool_calls_seen: int = 0
    repeated_count: int = 0
    cycle_length: int = 0


@dataclass(frozen=True)
class _Action:
    signature: str
    tool_name: str


class ExecutionGuard:
    """Detect actual lack of progress without penalising long useful work."""

    def __init__(self) -> None:
        self.tool_calls_seen = 0
        self._recent: deque[_Action] = deque(maxlen=_RECENT_WINDOW)
        self._warned = False
        self._threshold_reported = False
        self._warning_signature = ""

    @staticmethod
    def _canonical_arguments(arguments: Any) -> str:
        try:
            return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return repr(arguments)

    @classmethod
    def action_signature(cls, tool_name: str, arguments: Any, result_text: str) -> str:
        payload = "\x1f".join(
            (
                str(tool_name or "").strip(),
                cls._canonical_arguments(arguments),
                hashlib.sha256(str(result_text or "").encode("utf-8", "replace")).hexdigest(),
            )
        )
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()

    def _exact_repeat_count(self, signature: str) -> int:
        return sum(1 for row in self._recent if row.signature == signature)

    def _cycle(self) -> tuple[int, int]:
        """Return (pattern length, repetitions) for a repeated recent tail."""
        seq = [row.signature for row in self._recent]
        # Detect short A/B and A/B/C-style loops.  Four-pattern cycles are also
        # useful for agentic read/search loops without being overly sensitive.
        for pattern_len in (2, 3, 4):
            max_reps = len(seq) // pattern_len
            if max_reps < 3:
                continue
            pattern = seq[-pattern_len:]
            reps = 1
            cursor = len(seq) - 2 * pattern_len
            while cursor >= 0 and seq[cursor:cursor + pattern_len] == pattern:
                reps += 1
                cursor -= pattern_len
            if reps >= 3:
                return pattern_len, reps
        return 0, 0

    def reset_stagnation_window(self) -> None:
        """Forget local repetition evidence while preserving total run usage."""
        self._recent.clear()
        self._warned = False
        self._warning_signature = ""

    def observe(self, tool_name: str, arguments: Any, result_text: str) -> GuardDecision:
        """Record one completed tool action and decide whether intervention is needed."""
        self.tool_calls_seen += 1
        signature = self.action_signature(tool_name, arguments, result_text)
        self._recent.append(_Action(signature, str(tool_name or "")))

        threshold = False
        if (
            not self._threshold_reported
            and self.tool_calls_seen >= ADAPTIVE_TOOL_CALL_THRESHOLD
        ):
            self._threshold_reported = True
            threshold = True

        # Absolute backstop.  It is intentionally independent of productivity:
        # if this fires, something exceptional has happened and the turn must
        # yield control to the user rather than spend without bound.
        if self.tool_calls_seen >= EMERGENCY_TOOL_CALL_LIMIT:
            return GuardDecision(
                "halt",
                "emergency_tool_call_limit",
                threshold,
                self.tool_calls_seen,
            )

        exact_count = self._exact_repeat_count(signature)
        cycle_len, cycle_reps = self._cycle()
        suspicious = exact_count >= _EXACT_WARN_COUNT or cycle_reps >= 3

        if not suspicious:
            # A genuinely new/progressive action after a warning means the
            # model successfully re-planned; clear the escalation state.
            if self._warned and signature != self._warning_signature:
                self._warned = False
                self._warning_signature = ""
            return GuardDecision(
                "continue", "", threshold, self.tool_calls_seen, exact_count, cycle_len
            )

        if not self._warned:
            self._warned = True
            self._warning_signature = signature
            reason = "repeated_identical_action" if exact_count >= _EXACT_WARN_COUNT else "repeating_action_cycle"
            return GuardDecision(
                "warn", reason, threshold, self.tool_calls_seen, exact_count, cycle_len
            )

        # After warning, continued exact repetition or another full repeated
        # cycle demonstrates that the re-plan did not make progress.
        if exact_count >= _EXACT_HALT_COUNT or cycle_reps >= 4:
            reason = "stagnant_identical_action" if exact_count >= _EXACT_HALT_COUNT else "stagnant_action_cycle"
            return GuardDecision(
                "halt", reason, threshold, self.tool_calls_seen, exact_count, cycle_len
            )

        return GuardDecision(
            "continue", "", threshold, self.tool_calls_seen, exact_count, cycle_len
        )
