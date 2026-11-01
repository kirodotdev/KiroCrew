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


class ProviderLoopTerminalMixin:
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
