"""Provider-loop terminal completion and failure handling.

This module owns *only* terminal cancellation/provider/internal completion. It must
not choose transport retries, attempt model fallback, replay tools or revise
Durable Run admission. The provider/tool loop delegates here after its existing
RecoveryPolicy decisions have been exhausted.
"""
from __future__ import annotations

import time
from typing import Any

from .turn_state import TurnPhase
from .turn_loop_types import _LoopDirective
from .session_events import append_lifecycle
from .telemetry import now_timestamp
from .turn_telemetry_builder import build_turn_telemetry


class ProviderLoopTerminalMixin:
    def _finish_interrupted_provider_round(
        self, req_id: Any, session: Any, state: Any,
        interruption: Any, *, history_start: int,
        t_prompt: float, routing_decision: Any,
    ) -> _LoopDirective:
        """Resolve cancellation/steer before any network recovery is allowed."""
        action = self._handle_provider_interruption(
            session, history_start=history_start, interruption=interruption,
            preserve_side_effects=state.side_effect_risk_seen,
        )
        if action == "continue":
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.CONTINUE,
                reason="steer_after_interrupt",
            )
            return _LoopDirective.STEER_AFTER_INTERRUPT
        cancelled = action == "cancelled"
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.COMPLETE,
            reason="provider_cancelled" if cancelled else "provider_interrupted",
        )
        self._record_provider_turn_telemetry(
            session, state, outcome="cancelled" if cancelled else "interrupted",
            t_prompt=t_prompt, routing_decision=routing_decision,
            error_category="control",
        )
        self.result(req_id, self._provider_prompt_result(state, "cancelled" if cancelled else "end_turn"))
        return _LoopDirective.RETURN

    def _finish_provider_loop_cancelled_before_invoke(
        self, req_id: Any, session: Any, state: Any, *,
        history_start: int, t_prompt: float, routing_decision: Any,
    ) -> None:
        self._recover_current_turn(
            session,
            history_start,
            reason="cancelled_before_invoke",
            preserve_side_effects=state.side_effect_risk_seen,
        )
        # A steer consumed earlier this turn (before the cancel landed and the
        # loop short-circuited here, ahead of the next provider invoke) must be
        # rolled back to "cleared", same as the mid-invoke cancel path: a
        # cancelled turn never reports a steer consumed.
        self._rollback_turn_injected_steers(session)
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.COMPLETE,
            reason="cancelled_before_invoke",
        )
        self._record_provider_turn_telemetry(
            session, state, outcome="cancelled", t_prompt=t_prompt,
            routing_decision=routing_decision, error_category="control",
        )
        self.result(req_id, self._provider_prompt_result(state, "cancelled"))

    def _finish_provider_loop_provider_error(
        self, req_id: Any, session_id: str, session: Any,
        state: Any, exc: Exception, *,
        history_start: int, t_prompt: float, t_ready: float,
        routing_decision: Any,
    ) -> None:
        error_category = str(getattr(exc, "category", "provider") or "provider")
        self._recover_current_turn(
            session,
            history_start,
            reason=f"{error_category}_error",
            preserve_side_effects=state.side_effect_risk_seen,
        )
        if not state.turn_state.complete:
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.COMPLETE,
                reason="provider_error",
            )
        self.emit_runtime_error(session_id, str(exc))
        self._record_provider_turn_telemetry(
            session,
            state,
            outcome="error",
            t_prompt=t_prompt,
            routing_decision=routing_decision,
            error_kind=type(exc).__name__,
            error_category=error_category,
        )
        self.result(req_id, self._provider_prompt_result(state, "end_turn"))
        t_end = time.perf_counter()
        self.trace(
            f"session/prompt ERROR total={(t_end-t_prompt)*1000:.0f}ms "
            f"pre_upstream={(t_ready-t_prompt)*1000:.0f}ms "
            f"mcp_wait={state.mcp_wait_ms:.0f}ms upstream={state.upstream_ms:.0f}ms tool={state.tool_ms:.0f}ms "
            f"provider_rounds={state.provider_rounds_total} tool_calls={state.tool_calls_total} native_calls={state.native_tool_calls_total} "
            f"tool_search_calls={state.tool_search_calls_total} "
            f"loaded_tools={len(session.loaded_tool_names)} model={state.model.model_id} "
            f"error_category={error_category} side_effects={sorted(state.side_effect_classes_seen)} error={exc}"
        )

    def _finish_provider_loop_internal_error(
        self, req_id: Any, session_id: str, session: Any,
        state: Any, exc: Exception, *,
        history_start: int, t_prompt: float, routing_decision: Any,
    ) -> None:
        self._recover_current_turn(
            session,
            history_start,
            reason="internal_error",
            preserve_side_effects=state.side_effect_risk_seen,
        )
        if not state.turn_state.complete:
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.COMPLETE,
                reason="internal_error",
            )
        # Preserve a metadata-only location for otherwise opaque failures
        # such as AttributeError. Never store traceback locals or secrets.
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        last_frame = frames[-1] if frames else None
        error_site = (
            f"{last_frame.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{last_frame.lineno}:{last_frame.name}"
            if last_frame is not None else ""
        )
        self._runtime_event(
            "runtime.internal_error",
            session,
            data={
                "error": type(exc).__name__,
                "site": error_site,
                "provider_id": state.served_provider_id,
                "model": state.served_model_id,
                "side_effects": sorted(state.side_effect_classes_seen),
            },
        )
        self.trace(
            f"recovered internal turn error session={session_id} "
            f"{type(exc).__name__}: {exc}"
        )
        self.emit_runtime_error(
            session_id,
            f"Kiro BYO runtime error ({type(exc).__name__}). The session was recovered and can continue.",
        )
        self._record_provider_turn_telemetry(
            session,
            state,
            outcome="error",
            t_prompt=t_prompt,
            routing_decision=routing_decision,
            error_kind=type(exc).__name__,
            error_category="internal",
            error_site=error_site,
        )
        self.result(req_id, self._provider_prompt_result(state, "end_turn"))


    def _record_provider_turn_telemetry(
        self,
        session: Any,
        state: Any,
        *,
        outcome: str,
        t_prompt: float,
        routing_decision: Any = None,
        error_kind: str = "",
        error_category: str = "",
        error_site: str = "",
    ) -> None:
        """Record exactly one terminal telemetry/lifecycle projection."""
        if state.telemetry_recorded:
            return
        state.telemetry_recorded = True
        # Durable Run owns execution completion. Its fenced disk commit must
        # succeed before session/Agent projections announce a terminal turn.
        # Otherwise a failed fsync can leave a completed transcript and plan
        # pointing at a still-running attempt that must be reviewed on restart.
        committed = self._finish_durable_run(
            session,
            outcome=outcome,
            error_kind=error_kind,
            error_category=error_category,
            provider_id=state.served_provider_id,
            model_id=state.served_model_id,
            side_effect_risk=state.side_effect_risk_seen,
        )
        if committed is False:
            # The ledger is still nonterminal. Do not emit a successful
            # turn.completed event or persist misleading terminal telemetry.
            self.emit_runtime_error(
                session.session_id,
                "The turn ended, but durable completion could not be recorded. "
                "Check the run ledger before retrying tools or trusting recovery.",
            )
            return
        terminal_event = (
            "turn/failed" if outcome == "error"
            else "turn/cancelled" if outcome == "cancelled"
            else "turn/interrupted" if outcome == "interrupted"
            else ""
        )
        if terminal_event:
            details = {
                "outcome": outcome,
                "error_kind": error_kind,
                "error_category": error_category,
                "cancel_source": str(getattr(session, "cancel_source", "") or ""),
            }
            append_lifecycle(session, terminal_event, details)
            self._runtime_event(
                terminal_event.replace("/", "."), session,
                data={**details, "provider_id": state.served_provider_id,
                      "model": state.served_model_id},
            )
        self._finalize_agent_plan(
            session, outcome,
            provider_id=state.served_provider_id,
            model_id=state.served_model_id,
        )
        append_lifecycle(
            session, "turn/end",
            {
                "outcome": outcome,
                "error_kind": error_kind,
                "error_category": error_category,
                "provider_id": state.served_provider_id,
                "model": state.served_model_id,
                "tool_calls": state.tool_calls_total,
                "provider_rounds": state.provider_rounds_total,
            },
        )
        now = time.perf_counter()
        first_ms = (
            (state.first_visible_at - t_prompt) * 1000.0
            if state.first_visible_at is not None
            else -1.0
        )
        try:
            completed_at = now_timestamp()
            total_ms = (now - t_prompt) * 1000.0
            self.telemetry.record(
                build_turn_telemetry(
                    session, state, outcome=outcome, completed_at=completed_at,
                    total_ms=total_ms, first_ms=first_ms,
                    routing_decision=routing_decision, error_kind=error_kind,
                    error_category=error_category, error_site=error_site,
                    reasoning_summary=self._reasoning_summary,
                )
            )
        except OSError as exc:
            self.trace(f"telemetry write skipped: {type(exc).__name__}")
        self._runtime_event(
            "turn.completed",
            session,
            turn_id=state.turn_trace_id,
            run_id=state.turn_run_id,
            data={
                "outcome": outcome,
                "error_kind": error_kind,
                "error_category": error_category,
                "provider_id": state.served_provider_id,
                "requested_model": session.model,
                "served_model": state.served_model_id,
                "tool_calls": state.tool_calls_total,
                "provider_rounds": state.provider_rounds_total,
                "execution_guard_tool_calls": state.execution_guard.tool_calls_seen,
                "execution_guard_halted": state.execution_guard_halted,
                "execution_guard_reason": state.execution_guard_reason,
                "total_ms": (now - t_prompt) * 1000.0,
            },
        )
        try:
            self._persist_session(session)
        except OSError as exc:
            self.trace(f"agent/turn final persist skipped: {type(exc).__name__}")
