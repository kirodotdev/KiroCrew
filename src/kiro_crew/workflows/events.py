"""Run-event construction + JSON serialization for dynamic workflows (GATES A7, B4).

The frozen event *vocabulary* — ``WorkflowEvent`` and ``EVENT_TYPES`` — lives in
``kiro_crew.workflows.__init__`` (the contract). This module is the layer the
runner uses to *build* well-formed events with the correct per-type ``data``
fields (the run event stream table in ``docs/system-specs/modules/workflows.md``),
assign monotonic ``seq`` numbers, and serialize to/from JSON.

GATE A7: the runner emits exactly the documented event stream; ``EventStream``
factory methods produce every type with its required ``data`` keys.

GATE B4: persistence is JSON only — ``serialize_events``/``deserialize_events``
go through ``json`` (never ``pickle``/``marshal``). ``loads`` rejects non-JSON.

This module holds no LLM/agent logic and has no side effects on import.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from . import EVENT_TYPES, WorkflowEvent

# Required ``data`` keys per event type — the spec's run-event table, executable.
# The factory methods below populate exactly these; the validator enforces them.
REQUIRED_DATA_KEYS: dict[str, tuple[str, ...]] = {
    "run_started": ("name", "args", "script_hash", "budget_total"),
    "phase_started": ("title",),
    "agent_started": ("agent_id", "label", "phase", "call_index"),
    "agent_progress": ("agent_id", "turns", "last_tool", "elapsed_s"),
    "agent_finished": ("agent_id", "result_summary", "ok"),
    "log": ("message",),
    "budget_update": ("spent", "remaining"),
    "approval_requested": ("prompt", "approval_id"),
    "run_finished": ("result", "duration_s"),
    "run_failed": ("error", "where"),
    "run_cancelled": ("reason",),
}


def validate_event(event: WorkflowEvent) -> None:
    """Raise ``ValueError`` if the event's type or ``data`` shape is off-contract.

    Checks the type is in ``EVENT_TYPES`` and that every required ``data`` key for
    that type is present. Extra keys are allowed (forward-compatible); missing
    required keys are not.
    """
    if event.type not in EVENT_TYPES:
        raise ValueError(f"unknown workflow event type: {event.type!r}")
    required = REQUIRED_DATA_KEYS[event.type]
    missing = [k for k in required if k not in event.data]
    if missing:
        raise ValueError(f"event '{event.type}' missing required data keys: {missing}")


class EventStream:
    """Builds the ordered, monotonically-sequenced event stream for one run.

    The runner owns one ``EventStream`` per ``run_id``; each ``emit_*`` returns the
    built ``WorkflowEvent`` (already validated) and assigns the next ``seq``. The
    ``ts`` for each event is supplied by the caller (the runner derives it from the
    deterministic run clock, not ``time`` — keeps the stream resume-stable).
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._seq = 0

    def _emit(self, ts: str, type_: str, data: dict[str, Any]) -> WorkflowEvent:
        event = WorkflowEvent(run_id=self.run_id, seq=self._seq, ts=ts, type=type_, data=data)
        validate_event(event)
        self._seq += 1
        return event

    # --- lifecycle ---
    def run_started(
        self, ts: str, *, name: str, args: dict, script_hash: str, budget_total: int | None
    ) -> WorkflowEvent:
        return self._emit(
            ts,
            "run_started",
            {"name": name, "args": args, "script_hash": script_hash, "budget_total": budget_total},
        )

    def run_finished(self, ts: str, *, result: Any, duration_s: float) -> WorkflowEvent:
        return self._emit(ts, "run_finished", {"result": result, "duration_s": duration_s})

    def run_failed(self, ts: str, *, error: str, where: str) -> WorkflowEvent:
        return self._emit(ts, "run_failed", {"error": error, "where": where})

    def run_cancelled(self, ts: str, *, reason: str) -> WorkflowEvent:
        return self._emit(ts, "run_cancelled", {"reason": reason})

    # --- phases / progress ---
    def phase_started(self, ts: str, *, title: str) -> WorkflowEvent:
        return self._emit(ts, "phase_started", {"title": title})

    def log(self, ts: str, *, message: str) -> WorkflowEvent:
        return self._emit(ts, "log", {"message": message})

    def budget_update(self, ts: str, *, spent: int, remaining: int | str) -> WorkflowEvent:
        return self._emit(ts, "budget_update", {"spent": spent, "remaining": remaining})

    # --- agents ---
    def agent_started(
        self, ts: str, *, agent_id: str, label: str, phase: str, call_index: int
    ) -> WorkflowEvent:
        return self._emit(
            ts,
            "agent_started",
            {"agent_id": agent_id, "label": label, "phase": phase, "call_index": call_index},
        )

    def agent_progress(
        self, ts: str, *, agent_id: str, turns: int, last_tool: str, elapsed_s: float
    ) -> WorkflowEvent:
        return self._emit(
            ts,
            "agent_progress",
            {"agent_id": agent_id, "turns": turns, "last_tool": last_tool, "elapsed_s": elapsed_s},
        )

    def agent_finished(
        self, ts: str, *, agent_id: str, result_summary: str, ok: bool
    ) -> WorkflowEvent:
        return self._emit(
            ts,
            "agent_finished",
            {"agent_id": agent_id, "result_summary": result_summary, "ok": ok},
        )

    # --- approval ---
    def approval_requested(self, ts: str, *, prompt: str, approval_id: str) -> WorkflowEvent:
        return self._emit(ts, "approval_requested", {"prompt": prompt, "approval_id": approval_id})


# --- JSON-only persistence (GATE B4 — never pickle/marshal) ---


def serialize_events(events: Iterable[WorkflowEvent]) -> str:
    """Serialize a run's events to a JSON array string (the on-disk journal form)."""
    return json.dumps([e.to_json() for e in events])


def deserialize_events(text: str) -> list[WorkflowEvent]:
    """Parse a JSON journal back into events. Raises on non-JSON or bad shape."""
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("event journal must be a JSON array")
    events = [WorkflowEvent.from_json(obj) for obj in raw]
    for event in events:
        validate_event(event)
    return events
