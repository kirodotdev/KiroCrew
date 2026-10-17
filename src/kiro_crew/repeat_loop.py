"""Spot a turn that keeps making the same tool call and getting the same result.

An agent stuck in a doom loop re-runs one action that does not work, over and
over, instead of changing strategy. The signal is narrow on purpose: the SAME
tool with the SAME input coming back with the SAME result (status and output)
:data:`REPEAT_LOOP_THRESHOLD` times in one turn. Repeating such a call again
cannot change its outcome, so the turn is told so once, in-band, and left to
decide. Nothing is aborted: a call that is deliberately polled for an outside
change trips the same signal, and the notice says to carry on in that case.

A leaf: stdlib only, so every surface that runs a turn can hold one tracker
per turn without importing the dashboard.
"""

from __future__ import annotations

import hashlib

#: Identical results of one identical call, in one turn, before the notice.
REPEAT_LOOP_THRESHOLD = 3

#: Bound on the call ids and signatures one tracker holds; a turn past it stops
#: tracking new ones rather than growing without limit.
_MAX_TRACKED = 512

#: Longest slice of the call's title quoted back in the notice.
_TITLE_LIMIT = 200


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def build_repeat_loop_notice(title: str, count: int) -> str:
    """The in-band notice for a call repeated *count* times with one result."""
    shown = " ".join((title or "").split())[:_TITLE_LIMIT] or "a tool call"
    return (
        f"[Kiro Crew host notice] You have made the same tool call {count} times "
        f"this turn and got the same result each time: {shown}\n\n"
        "Running it again will not change the outcome. Stop and diagnose: read "
        "the result, check what you assumed, then try a genuinely different "
        "approach, or say plainly what blocks you. If you are deliberately "
        "waiting for something outside this turn to change, carry on."
    )


class RepeatLoopTracker:
    """Per-turn count of identical calls that return identical results."""

    def __init__(self) -> None:
        self._calls: dict[str, tuple[str, str]] = {}
        self._outputs: dict[str, str] = {}
        self._last_outcome: dict[str, str] = {}
        self._streak: dict[str, int] = {}
        self._warned: set[str] = set()

    def note_call(self, tool_call_id: str, tool_name: str, tool_input: str, title: str) -> None:
        """Record what a call is, so its result can be matched to its input.

        Called again for the same id (an input refinement) it replaces the
        earlier record, because the refined input is the real one.
        """
        if not tool_call_id or not (tool_input or "").strip():
            return
        key = _digest(tool_call_id)
        if key not in self._calls and len(self._calls) >= _MAX_TRACKED:
            return
        shown = " ".join((title or tool_name or "").split())[:_TITLE_LIMIT]
        self._calls[key] = (_digest(tool_name or "", tool_input), shown)

    def note_result(
        self,
        tool_call_id: str,
        *,
        status: str,
        output: str,
        output_digest: str = "",
        terminal: bool = True,
    ) -> str:
        """Feed one result frame; return the notice once the loop is seen.

        A backend may send a call's output and its terminal status in separate
        frames, so a non-terminal frame only keeps its output for the terminal
        one. Returns "" otherwise, and at most one notice per call signature
        per turn.
        """
        key = _digest(tool_call_id) if tool_call_id else ""
        if not key or key not in self._calls:
            return ""
        seen = output_digest or (_digest(output) if output else "")
        if not terminal:
            if seen:
                self._outputs[key] = seen
            return ""
        signature, title = self._calls.pop(key)
        kept = self._outputs.pop(key, "")
        outcome = _digest(status or "", seen or kept)
        if signature not in self._streak and len(self._streak) >= _MAX_TRACKED:
            return ""
        if self._last_outcome.get(signature) == outcome:
            self._streak[signature] += 1
        else:
            self._last_outcome[signature] = outcome
            self._streak[signature] = 1
        if self._streak[signature] < REPEAT_LOOP_THRESHOLD or signature in self._warned:
            return ""
        self._warned.add(signature)
        return build_repeat_loop_notice(title, self._streak[signature])
