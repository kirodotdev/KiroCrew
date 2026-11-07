"""ACP durable-run recovery and diagnostics control plane.

BYO owns only its run recovery and evidence.  Work IDs supplied by KiroCrew
are *correlation*, never permission to resume or a declaration that a host
Ticket finished.  The ACP server keeps request dispatch and session ownership.
"""
from __future__ import annotations

from typing import Any

from .durable_runs import DurableRunStore
from .recovery_policy import RecoveryPolicy
from .run_diagnostics import diagnose_run
from .work_trace import handle_work_list_request


class RunRecoveryMixin:
    def _run_recovery_payload(self, record: dict[str, Any], *, include_timeline: bool = False) -> dict[str, Any]:
        # No second durable log: join the canonical Run ledger with the
        # optional, bounded runtime trace only for diagnostics. List queries
        # avoid scanning the trace once per row.
        trace_store = getattr(self, "runtime_trace", None)
        events: list[dict[str, Any]] = []
        mode = "off"
        if trace_store is not None:
            try:
                mode = str(trace_store.mode)
                if include_timeline:
                    events = trace_store.recent(100, run_id=str(record.get("run_id") or ""))
            except OSError:
                mode = "unavailable"
        diagnostics = diagnose_run(record, trace_events=events, trace_mode=mode, trace_window_scanned=include_timeline)
        safe_record = DurableRunStore.public_record(record)
        return {
            **safe_record,
            "recovery": {
                "resume": diagnostics["recovery"]["resume"],
                "replan": diagnostics["recovery"]["replan"],
            },
            "diagnostics": diagnostics,
        }

    def _handle_run_recovery_request(
        self, req_id: Any, params: dict[str, Any], *, strategy: str
    ) -> None:
        session_id = str(params.get("sessionId") or "")
        run_id = str(params.get("runId") or "")
        if not session_id or not run_id:
            self.error(req_id, -32602, "sessionId and runId are required")
            return
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        if session.active_turn:
            self.error(req_id, -32002, "session is busy; wait before resuming a durable run")
            return
        record = self._durable_run_store().get(run_id)
        if record is None or str(record.get("session_id") or "") != session_id:
            self.error(req_id, -32602, "unknown durable run for this session")
            return
        def trace_rejection(reason_code: str) -> None:
            # Diagnostics cannot become a new blocker of the ACP control path.
            try:
                self.runtime_trace.record(
                    "run.recovery.rejected", session_id=session_id, run_id=run_id,
                    data={"strategy": strategy, "reason_code": reason_code},
                )
            except OSError:
                pass

        decision = RecoveryPolicy.durable_run(record, strategy=strategy)
        if not decision.safe:
            trace_rejection("recovery_policy_denied")
            self.error(req_id, -32010, f"durable run cannot be {strategy}d safely: {decision.reason}")
            return
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        try:
            event_seq = int(metadata.get("user_event_seq", -1))
        except (TypeError, ValueError):
            event_seq = -1
        if not (0 <= event_seq < len(session.events)):
            trace_rejection("user_anchor_missing")
            self.error(req_id, -32010, "durable recovery anchor is missing from the persisted session")
            return
        event = session.events[event_seq]
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        message = data.get("message") if isinstance(data.get("message"), dict) else None
        if str(event.get("type") or "") != "user/message" or message is None:
            trace_rejection("user_anchor_invalid")
            self.error(req_id, -32010, "durable recovery anchor is not a user message")
            return
        blocks = self._prompt_blocks_from_user_message(message)
        if not blocks:
            trace_rejection("prompt_reconstruction_failed")
            self.error(req_id, -32010, "durable recovery prompt cannot be reconstructed")
            return

        try:
            self.runtime_trace.record(
                "run.recovery.requested", session_id=session_id, run_id=run_id,
                data={"strategy": strategy, "reason_code": "policy_allowed", "user_event_seq": event_seq},
            )
        except OSError:
            pass
        synthetic = {
            "sessionId": session_id,
            "prompt": blocks,
            "_kiro_resume_run_id": run_id,
            "_kiro_recovery_strategy": strategy,
        }
        self._handle_prompt_request(req_id, synthetic)

    def _handle_run_resume_request(self, req_id: Any, params: dict[str, Any]) -> None:
        self._handle_run_recovery_request(req_id, params, strategy="resume")

    def _handle_run_retry_request(self, req_id: Any, params: dict[str, Any]) -> None:
        self._handle_run_recovery_request(req_id, params, strategy="retry")

    def _handle_run_replan_request(self, req_id: Any, params: dict[str, Any]) -> None:
        self._handle_run_recovery_request(req_id, params, strategy="replan")

    def _handle_run_list_request(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        try:
            limit = max(1, min(int(params.get("limit", 50) or 50), 200))
        except (TypeError, ValueError):
            limit = 50
        runs = self._durable_run_store().list(limit=limit, session_id=session_id)
        self.result(req_id, {"runs": [self._run_recovery_payload(row) for row in runs]})

    def _handle_run_get_request(self, req_id: Any, params: dict[str, Any]) -> None:
        run_id = str(params.get("runId") or "")
        if not run_id:
            self.error(req_id, -32602, "runId is required")
            return
        record = self._durable_run_store().get(run_id)
        if record is None:
            self.error(req_id, -32602, "unknown durable run")
            return
        self.result(req_id, {"run": self._run_recovery_payload(record, include_timeline=True)})


    _handle_work_list_request = handle_work_list_request
