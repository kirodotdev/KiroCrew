"""DSH-inspired, *read-only* Crew work lineage from BYO's canonical Run ledger.

Crew still owns tickets, worker assignments, permissions and task completion.
This projection never drives scheduling or recovery and must not infer a Crew
Task's final outcome from one BYO model turn. Explicit host_work_link metadata
is the ONLY source of task IDs. A known Mate 2.0 service-agent projection may
be listed as an *unlinked execution*, never reverse-mapped into a task.
"""
from __future__ import annotations

from typing import Any, Mapping

from .durable_runs import DurableRunStore
from .host_work_link import projected_work_link, validate_work_link
from .crew_role_evidence import crew_role_from_run_metadata

# An actionable state here refers to *BYO execution evidence*, not Crew tasks.
_RANK = {"unknown_side_effect": 5, "needs_review": 4, "failed_run": 3,
         "working": 2, "completed_run": 1, "cancelled_run": 1, "unknown": 0}


def _current_evidence_signal(runs: list[dict[str, Any]]) -> str:
    """Select current evidence without promoting a harmless *past* failure.

    DSH's panel treats the latest assignment/report as the current stage, not
    every historical failure as a fresh request for attention. BYO has only
    Run evidence, so this is explicitly NOT a Crew task-status decision.
    An unresolved side-effect risk is different: a later successful Run does
    not prove the earlier tool effect was safe, and must never hide it.
    """
    if any(row["executionSignal"] == "unknown_side_effect" for row in runs):
        return "unknown_side_effect"
    if not runs:
        return "unknown"
    newest = max(runs, key=lambda row: (row["updatedAt"], row["runId"]))
    return newest["executionSignal"]


def execution_signal(run: Mapping[str, Any]) -> str:
    """Conservative diagnosis; never turns a stale lease into 'finished'."""
    status = str(run.get("status") or "")
    # A completed Run may have performed *confirmed* mutating tool calls.
    # side_effect_seen is a replay barrier, not evidence of an unfinished side
    # effect after the Run is safely sealed. An explicit unsafe state still wins.
    if str(run.get("recovery_state") or "") in {
        "unknown_side_effect", "manual_verification_required", "unsafe",
    } or (status not in {"completed", "cancelled"} and
          DurableRunStore.restart_recovery_state(run) == "unknown_side_effect"):
        return "unknown_side_effect"
    if status == "running":
        return "working" if DurableRunStore.is_lease_active(run) else "needs_review"
    if status == "failed":
        return "failed_run"
    if status == "completed":
        return "completed_run"
    if status == "cancelled":
        return "cancelled_run"
    return "unknown"


def work_trace_snapshot(
    *, limit: int = 50, task_id: str = "", store: DurableRunStore | None = None,
) -> dict[str, Any]:
    """A bounded, content-free snapshot of explicitly correlated execution.

    The 500-row history window is not an exhaustive task inventory.  Task IDs
    may contain multiple assignments and runs across sessions; they only mean
    what the ACP host explicitly supplied at session bind time.
    """
    limit = max(1, min(int(limit), 100))
    if task_id:
        task_id = validate_work_link({"taskId": task_id})["taskId"]
    ledger = store if store is not None else DurableRunStore()
    rows = ledger.list(limit=500)
    groups: dict[str, dict[str, Any]] = {}
    linked_run_count = 0
    unlinked_crew_runs: list[dict[str, Any]] = []
    for record in rows:
        metadata = record.get("metadata")
        link = projected_work_link(metadata.get("host_work_link")) if isinstance(metadata, Mapping) else {}
        key = link.get("taskId", "")
        if not key:
            # The selected *spec* is positive diagnostic evidence of the Crew
            # service-agent profile; it is NOT an attestation of a Crew task.
            # Never guess a Ticket from the prompt, member name or Session ID.
            role = crew_role_from_run_metadata(metadata)
            if role and not task_id:
                public = DurableRunStore.public_record(record)
                unlinked_crew_runs.append({
                    "runId": str(public.get("run_id") or "")[:160],
                    "sessionId": str(public.get("session_id") or "")[:160],
                    "projectedRole": role,
                    "modelId": str(public.get("metadata", {}).get("model_id") or "")[:256],
                    "status": str(public.get("status") or "")[:32],
                    "executionSignal": execution_signal(record),
                    "updatedAt": float(public.get("updated_at") or 0),
                    "taskCorrelation": "not_provided_by_host",
                })
            continue
        if task_id and task_id != key:
            continue
        linked_run_count += 1
        signal = execution_signal(record)
        public = DurableRunStore.public_record(record)
        run = {
            "runId": str(public.get("run_id") or "")[:160],
            "sessionId": str(public.get("session_id") or "")[:160],
            "assignmentId": link.get("assignmentId", ""),
            "role": link.get("role", ""),
            "parentSessionId": link.get("parentSessionId", ""),
            "modelId": str(public.get("metadata", {}).get("model_id") or "")[:256],
            "selection": str(public.get("selection") or "")[:256],
            "status": str(public.get("status") or "")[:32],
            "executionSignal": signal,
            "updatedAt": float(public.get("updated_at") or 0),
            "recoveryState": str(public.get("recovery_state") or "")[:64],
        }
        group = groups.setdefault(key, {"taskId": key, "runs": [], "executionSignal": "unknown", "lastUpdatedAt": 0.0})
        group["runs"].append(run)
        group["lastUpdatedAt"] = max(group["lastUpdatedAt"], run["updatedAt"])

    ordered = sorted(groups.values(), key=lambda group: (-group["lastUpdatedAt"], group["taskId"]))
    for group in ordered:
        group["runs"].sort(key=lambda run: (-run["updatedAt"], run["runId"]))
        group["executionSignal"] = _current_evidence_signal(group["runs"])
        group["runCount"] = len(group["runs"])
        group["assignmentCount"] = len({r["assignmentId"] for r in group["runs"] if r["assignmentId"]})
        group["sessionCount"] = len({r["sessionId"] for r in group["runs"] if r["sessionId"]})
        group["requiresReview"] = group["executionSignal"] in {"needs_review", "unknown_side_effect", "failed_run"}
    unlinked_crew_runs.sort(key=lambda r: (-r["updatedAt"], r["runId"]))
    # Attention is a *bounded read-only projection*, not another queue or
    # task state machine.  One item per linked task, and one per otherwise
    # unlinked Crew session; avoid asking the user to watch every Run/window.
    attention: list[dict[str, Any]] = []
    for group in ordered:
        if not group["requiresReview"]:
            continue
        attention.append({
            "kind": "linked_task", "taskId": group["taskId"],
            "executionSignal": group["executionSignal"],
            "updatedAt": group["lastUpdatedAt"],
            "runCount": group["runCount"],
        })
    # The same Crew worker session can have many historic Runs. A successful
    # later Run supersedes an ordinary previous failure for display purposes,
    # but NEVER an unresolved side-effect signal. Do not invent a task ID.
    by_session_runs: dict[str, list[dict[str, Any]]] = {}
    for run in unlinked_crew_runs:
        by_session_runs.setdefault(run["sessionId"], []).append(run)
    by_session: dict[str, dict[str, Any]] = {}
    for session_id, runs in by_session_runs.items():
        signal = _current_evidence_signal(runs)
        if signal not in {"needs_review", "unknown_side_effect", "failed_run"}:
            continue
        latest = max(runs, key=lambda row: (row["updatedAt"], row["runId"]))
        by_session[session_id] = {
            "kind": "unlinked_crew_session", "sessionId": session_id,
            "runId": latest["runId"], "executionSignal": signal,
            "updatedAt": latest["updatedAt"],
        }
    attention.extend(by_session.values())
    attention.sort(key=lambda r: (-_RANK[r["executionSignal"]], -r["updatedAt"],
                                  r.get("taskId", r.get("sessionId", ""))))
    # Caution: these are execution *signals*, possibly historical; Crew must
    # verify actual task status/permissions before notifying or acting.
    attention_limit = min(limit, 5)
    # DSH distinguishes a question awaiting a person from an executor report.
    # BYO has no trusted Crew Attention/Ticket state: only uncertain external
    # effects warrant a local *safety verification* suggestion. Other failures
    # remain execution diagnostics, not fabricated human-facing assignments.
    manual_verification = sum(
        item["executionSignal"] == "unknown_side_effect" for item in attention
    )
    # Keep the legacy combined list for existing ACP consumers. The two
    # projections classify only the *visible, bounded* items; the totals below
    # describe the whole scanned window, not the limited display subset.
    visible_attention = attention[:attention_limit]
    safety_items = [item for item in visible_attention if item["executionSignal"] == "unknown_side_effect"]
    diagnostic_items = [item for item in visible_attention if item["executionSignal"] != "unknown_side_effect"]
    window_saturated = len(rows) >= 500
    return {
        "attention": {
            "items": visible_attention,
            "safetyVerificationItems": safety_items,
            "executionDiagnosticItems": diagnostic_items,
            "totalInWindow": len(attention),
            "hasMore": len(attention) > attention_limit,
            "scope": "byo_execution_evidence_only",
            "taskCompletionAuthoritative": False,
            "selectionPolicy": "latest_run_except_unresolved_side_effects",
            "triage": {
                "manualVerificationInWindow": manual_verification,
                "executionDiagnosticsInWindow": len(attention) - manual_verification,
                "hostHumanAttentionKnown": False,
                "owner": "kirocrew",
                "windowComplete": not window_saturated,
            },
        },
        "tasks": ordered[:limit],
        "unlinkedCrewRuns": unlinked_crew_runs[:limit],
        "summary": {
            "linkedTasksInWindow": len(ordered), "linkedRunsInWindow": linked_run_count,
            "unlinkedCrewRunsInWindow": len(unlinked_crew_runs),
            "unlinkedHasMore": len(unlinked_crew_runs) > limit,
            "runsScanned": len(rows), "scanLimit": 500,
            "windowSaturated": window_saturated,
            "hasMore": len(ordered) > limit,
            "scope": "explicit_task_links_plus_unlinked_crew_agent_profiles",
            "taskCompletionAuthoritative": False,
        },
        "consistency": "eventual",
    }


def handle_work_list_request(server: Any, req_id: Any, params: dict[str, Any]) -> None:
    """ACP read-only diagnostic extension; binds as an AcpServer method."""
    try:
        limit = int(params.get("limit", 50))
        task_id = str(params.get("taskId") or "")
        result = work_trace_snapshot(limit=limit, task_id=task_id, store=server._durable_run_store())
    except (ValueError, TypeError) as exc:
        server.error(req_id, -32602, str(exc))
        return
    server.result(req_id, result)
