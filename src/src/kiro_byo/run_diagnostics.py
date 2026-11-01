"""Read-only, content-free diagnostics for a durable BYO Run.

The DurableRunStore and session event log remain authoritative. Runtime Trace
is a bounded, optional diagnostic index; neither trace absence nor a missing
trace event is evidence that a tool did or did not execute.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .durable_runs import DurableRunStore
from .recovery_policy import RecoveryPolicy


def _seq(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def diagnose_run(
    record: Mapping[str, Any],
    *,
    trace_events: Iterable[Mapping[str, Any]] = (),
    trace_mode: str = "off",
    trace_window_scanned: bool = False,
    max_events: int = 40,
) -> dict[str, Any]:
    """Explain Run progress/recovery without exposing prompts, tools or paths.

    No write, no retry, no state transition. The returned timeline is a small
    *observability* projection; safe recovery decisions are recomputed from the
    canonical Run ledger and must be checked again at the execution boundary.
    """
    resume = RecoveryPolicy.durable_run(dict(record), strategy="resume")
    retry = RecoveryPolicy.durable_run(dict(record), strategy="retry")
    replan = RecoveryPolicy.durable_run(dict(record), strategy="replan")
    steps = record.get("steps") if isinstance(record.get("steps"), list) else []
    latest_step = next((s for s in reversed(steps) if isinstance(s, Mapping)), {})
    attempts = latest_step.get("attempts") if isinstance(latest_step.get("attempts"), list) else []
    latest_attempt = next((a for a in reversed(attempts) if isinstance(a, Mapping)), {})
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    # Stable Run -> Step -> Attempt lineage even if the bounded trace rotates.
    # Keep only identifiers, terminal states, and controlled failure labels.
    execution_history: list[dict[str, str]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        for attempt in (step.get("attempts") if isinstance(step.get("attempts"), list) else []):
            if not isinstance(attempt, Mapping):
                continue
            execution_history.append({
                "step_id": str(step.get("step_id") or ""),
                "step_status": str(step.get("status") or ""),
                "attempt_id": str(attempt.get("attempt_id") or ""),
                "attempt_status": str(attempt.get("status") or ""),
            })
    observed_side_effect = DurableRunStore.restart_recovery_state(record) == "unknown_side_effect"
    # Failure kinds are controlled enums/identifiers from the attempt ledger;
    # error messages and tool contents are intentionally never included.
    events = []
    for row in trace_events:
        if not isinstance(row, Mapping) or str(row.get("run_id") or "") != str(record.get("run_id") or ""):
            continue
        data = row.get("data") if isinstance(row.get("data"), Mapping) else {}
        events.append({
            "event": str(row.get("event") or ""),
            "timestamp": row.get("timestamp") if isinstance(row.get("timestamp"), (int, float)) else None,
            "turn_id": str(row.get("turn_id") or ""),
            "step_id": str(row.get("step_id") or ""),
            "attempt_id": str(row.get("attempt_id") or ""),
            "session_event_seq": _seq(data.get("session_event_seq")),
        })
    size = max(1, min(int(max_events), 100))
    return {
        "run_id": str(record.get("run_id") or ""),
        "session_id": str(record.get("session_id") or ""),
        "status": str(record.get("status") or ""),
        "recovery_state": str(record.get("recovery_state") or ""),
        "last_recovery_action": str(record.get("last_recovery_action") or ""),
        "identity": {
            "step_id": str(latest_step.get("step_id") or ""),
            "step_status": str(latest_step.get("status") or ""),
            "attempt_id": str(latest_attempt.get("attempt_id") or ""),
            "attempt_status": str(latest_attempt.get("status") or ""),
        },
        "anchors": {
            "user_message_seq": _seq(metadata.get("user_event_seq")),
            "latest_session_event_seq": _seq(record.get("session_event_seq")),
        },
        "attempt_history": execution_history[-32:],
        "attempt_history_truncated": len(execution_history) > 32,
        "last_error": {
            "kind": str(latest_attempt.get("error_kind") or ""),
            "category": str(latest_attempt.get("error_category") or ""),
        },
        "side_effect_may_have_occurred": observed_side_effect,
        "recovery": {
            "resume": {"allowed": resume.safe, "reason": resume.reason},
            "retry": {"allowed": retry.safe, "reason": retry.reason},
            "replan": {"allowed": replan.safe, "reason": replan.reason},
            "execution_owner": "client_explicit_request",
            "automatic_retry_scheduled": False,
        },
        "trace": {
            "mode": str(trace_mode or "off"),
            "window_scanned": bool(trace_window_scanned),
            "source": "bounded_runtime_trace_non_authoritative",
            "events": events[-size:],
            "has_more_in_supplied_window": len(events) > size,
            "absence_proves_no_execution": False,
        },
    }
