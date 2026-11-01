"""Read-only unified Records projection over existing bounded ledgers.

No new writable datastore, no changes to adaptive route evidence, and never
mutates the recovery/fencing ledger. Counts describe retained records, not an
unbounded lifetime total. Runtime Trace content is never re-exported here.
"""
from __future__ import annotations

from typing import Any

from .durable_runs import DurableRunStore
from .runtime_trace import RuntimeTraceStore
from .telemetry import TurnTelemetryStore

_RECOVERY_STATES = frozenset({"pending_restart", "resuming", "retrying", "replanning", "unknown_side_effect"})
_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def is_recovery_run(run: dict[str, Any]) -> bool:
    return (any(int(run.get(k) or 0) > 0 for k in ("retry_count", "resume_count", "replan_count"))
            or str(run.get("recovery_state") or "") in _RECOVERY_STATES)


def _event_time(row: dict[str, Any], key: str = "timestamp") -> float:
    try:
        return max(0.0, float(row.get(key) or 0.0))
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _public_event(row: dict[str, Any]) -> dict[str, Any]:
    """Whitelist event metadata only; never return trace data or content."""
    event = str(row.get("event") or "")[:128]
    selected = event == "model.route.selected"
    details = row.get("data") if isinstance(row.get("data"), dict) else {}
    return {
        "kind": "routing" if selected else "runtime",
        "timestamp": _event_time(row),
        "event": event,
        "session_id": str(row.get("session_id") or "")[:160],
        "turn_id": str(row.get("turn_id") or "")[:160],
        "run_id": str(row.get("run_id") or "")[:160],
        # These are known catalog identifiers; do not reflect arbitrary error,
        # tool or prompt metadata in a summary listing.
        "mode": str(details.get("pack") or "")[:64] if selected else "",
        "type": str(details.get("policy") or "")[:64] if selected else "",
        "model": str(details.get("resolved_model") or "")[:128] if selected else "",
    }


def _public_turn(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "turn", "timestamp": _event_time(row, "completed_at") or _event_time(row),
        "event": "turn." + str(row.get("outcome") or "unknown")[:32],
        "session_id": str(row.get("session_id") or "")[:160],
        "turn_id": str(row.get("turn_id") or "")[:160],
        "run_id": str(row.get("run_id") or "")[:160],
        "mode": str(row.get("mode_pack") or "")[:64],
        "type": str(row.get("mode_type") or "")[:64],
        "model": str(row.get("served_model") or "")[:128],
        "duration_ms": row.get("total_ms"),
    }


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "recovery", "timestamp": _event_time(row, "updated_at"),
        "event": "run." + str(row.get("recovery_state") or row.get("status") or "unknown")[:64],
        "session_id": str(row.get("session_id") or "")[:160],
        "turn_id": "", "run_id": str(row.get("run_id") or "")[:160],
        "mode": str(row.get("mode") or "")[:64],
        "type": str(row.get("selection") or "")[:128],
        "model": "", "status": str(row.get("status") or "")[:32],
    }


def records_snapshot(*, limit: int = 100, kind: str = "", mode: str = "") -> dict[str, Any]:
    """A bounded, content-free snapshot of all retained record families.

    No reader claims a consistent cross-ledger transaction. The snapshot can
    observe a new event in one ledger before its related Turn is appended.
    """
    limit = max(1, min(int(limit), 200))
    kind = str(kind or "").lower().strip()
    if kind and kind not in {"turn", "routing", "runtime", "recovery"}:
        raise ValueError("invalid record kind")
    mode = str(mode or "").strip().lower()
    if len(mode) > 64:
        raise ValueError("mode filter is too long")
    telemetry = TurnTelemetryStore()
    traces = RuntimeTraceStore()
    durable = DurableRunStore()
    # Each source already has a finite retention limit. Read that retained
    # window before merging, otherwise sparse events vanish from the timeline.
    turns = telemetry.recent(telemetry.max_records)
    trace_rows = traces.retained_snapshot()
    run_rows = durable.list(limit=500)
    recovered = [r for r in run_rows if is_recovery_run(r)]
    protected = sum(1 for r in run_rows if str(r.get("status") or "") not in _TERMINAL
                    or str(r.get("recovery_state") or "") == "unknown_side_effect")
    events = ([_public_turn(x) for x in turns]
              + [_public_event(x) for x in trace_rows]
              + [_public_run(x) for x in recovered])
    if kind:
        events = [e for e in events if e["kind"] == kind]
    if mode:
        events = [e for e in events if e["mode"].lower() == mode]
    events.sort(key=lambda item: item["timestamp"], reverse=True)
    routes_count = sum(1 for row in trace_rows if row.get("event") == "model.route.selected")
    return {
        "summary": {
            "turns": len(turns),
            "routing": routes_count,
            "runtime": len(trace_rows) - routes_count,
            "recovery": len(recovered),
            "protected_runs": protected,
            "runs_loaded": len(run_rows),
            # The run listing is page-bounded. Never present a saturated
            # page's protected count as an exhaustive global count.
            "run_page_saturated": len(run_rows) >= 500,
            "scope": "retained",
            "trace_mode": traces.mode,
            "retention": {"turns": telemetry.max_records, "runtime": traces.max_records,
                          "runs_page_limit": 500},
        },
        "events": events[:limit], "has_more": len(events) > limit,
        "consistency": "eventual",
    }
