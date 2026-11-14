"""Tool batch execution and runaway-guard observation for one turn.

This leaf owns a *single* batch after the Turn Engine has durably admitted
it. It neither grants tool permission nor commits the Crew ticket state.
Admission and durable receipts remain the Turn Engine's responsibility;
individual dispatch stays behind the existing native/MCP permission gates.
"""
from __future__ import annotations

from typing import Any

from .execution_guard import ExecutionGuard
from .mcp_client import DEFERRED_TOOL_SEARCH_NAME
from .session_events import append_lifecycle
from .turn_loop_types import _ToolBatchOutcome


class ToolBatchExecutionMixin:
    def _observe_tool_execution_guard(
        self,
        session: Any,
        execution_guard: ExecutionGuard,
        *,
        round_role: str,
        call: Any,
        arguments: Any,
        result_text: str,
        step: int,
        provider_rounds_total: int,
    ) -> tuple[Any, str, str]:
        """Observe one tool result and return decision + optional notices."""
        # A display-only description must not make repeated native executions
        # look like new work to the runaway/stagnation guard. MCP tools may
        # legitimately define their own "description" executable argument.
        signature_args = arguments
        if (isinstance(arguments, dict) and session.native is not None
                and session.native.has_tool(getattr(call, "name", ""))):
            signature_args = {key: value for key, value in arguments.items() if key != "description"}
        decision = execution_guard.observe(
            f"{round_role}:{getattr(call, 'name', '')}", signature_args, result_text
        )
        pending_replan_notice = ""
        execution_guard_reason = ""
        if decision.adaptive_threshold_reached:
            self._runtime_event(
                "execution.guard.adaptive_threshold",
                session,
                data={
                    "tool_calls_seen": decision.tool_calls_seen,
                    "step": step,
                    "attempt": provider_rounds_total,
                    "role": round_role,
                },
            )
        if decision.action == "warn":
            pending_replan_notice = (
                "Execution guard: recent tool actions are repeating without new "
                "information. Re-evaluate the plan before using another tool. "
                "Do not repeat the same call/result cycle; either take a different "
                "productive action or conclude/hand back with what is already known."
            )
            self._runtime_event(
                "execution.guard.replan_requested",
                session,
                data={
                    "reason": decision.reason,
                    "tool_calls_seen": decision.tool_calls_seen,
                    "repeated_count": decision.repeated_count,
                    "cycle_length": decision.cycle_length,
                    "step": step,
                    "role": round_role,
                    "tool": getattr(call, "name", ""),
                },
            )
        elif decision.action == "halt":
            execution_guard_reason = decision.reason
            self._runtime_event(
                "execution.guard.halt_requested",
                session,
                data={
                    "reason": decision.reason,
                    "tool_calls_seen": decision.tool_calls_seen,
                    "repeated_count": decision.repeated_count,
                    "cycle_length": decision.cycle_length,
                    "step": step,
                    "role": round_role,
                    "tool": getattr(call, "name", ""),
                },
            )
        return decision, pending_replan_notice, execution_guard_reason

    def _execute_tool_batch_impl(
        self,
        session: Any,
        *,
        tool_calls: tuple[Any, ...] | list[Any],
        round_role: str,
        round_provider: Any,
        round_model: Any,
        step: int,
        execution_guard: ExecutionGuard,
        provider_rounds_total: int,
    ) -> _ToolBatchOutcome:
        """Execute one model-emitted tool batch without deciding turn control flow."""
        for call in tool_calls:
            effect_class = self._tool_effect_class(session, call.name)
            append_lifecycle(
                session,
                "tool/call",
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "role": round_role,
                    "provider_id": round_provider.id,
                    "model": round_model.model_id,
                    "arguments_chars": len(call.arguments_text or ""),
                    "effect_class": effect_class,
                },
            )
            self._runtime_event(
                "tool.effect.classified",
                session,
                data={
                    "call_id": call.call_id,
                    "tool": call.name,
                    "role": round_role,
                    "effect_class": effect_class,
                },
            )

        steered_during_tools = False
        guard_replan_requested = False
        guard_stop_requested = False
        guard_reason = ""
        pending_replan_notice = ""
        tool_ms = 0.0
        mcp_wait_ms = 0.0
        native_calls = 0
        tool_search_calls = 0
        tool_search_loaded = 0
        batch_call_ids: set[str] = set()

        def cancelled_batch_outcome() -> _ToolBatchOutcome:
            # Preserve the counters accumulated *before* interruption. Both
            # cancellation checkpoints must report the same evidence shape.
            return _ToolBatchOutcome(
                cancelled=True,
                steered=steered_during_tools,
                replan_requested=guard_replan_requested,
                stop_requested=guard_stop_requested,
                guard_reason=guard_reason,
                pending_replan_notice=pending_replan_notice,
                tool_ms=tool_ms,
                mcp_wait_ms=mcp_wait_ms,
                native_calls=native_calls,
                tool_search_calls=tool_search_calls,
                tool_search_loaded=tool_search_loaded,
            )

        for call in tool_calls:
            if session.cancel_event.is_set():
                return cancelled_batch_outcome()

            if guard_stop_requested:
                # Deliberately NOT observed by the execution guard: the guard
                # already halted this batch, and feeding a fixed skip-text back
                # would produce a constant signature (name+args+sha256(text))
                # that could be misread as fresh stagnation. Skips are not model
                # progress and must stay out of the stagnation window.
                self._append_tool_result(
                    session,
                    call.call_id,
                    "Skipped because the execution guard stopped repeated/unbounded tool work.",
                )
                continue
            if guard_replan_requested:
                # Deliberately NOT observed (see guard_stop_requested above):
                # a re-plan was requested, the rest of the batch is skipped, and
                # a constant skip-text signature must not pollute the guard.
                self._append_tool_result(
                    session,
                    call.call_id,
                    "Skipped because the execution guard requested a re-plan before more tool work.",
                )
                continue

            if steered_during_tools or session.steer_event.is_set():
                steered_during_tools = True
                # Deliberately NOT observed: this is a USER steering event, not
                # model stagnation. Counting it would charge user control
                # against the guard's repetition window and could falsely halt.
                self._append_tool_result(
                    session,
                    call.call_id,
                    "Skipped before execution because user steering changed the task.",
                )
                continue

            # A single provider batch must not dispatch two operations with
            # the same call id. In particular this prevents duplicate writes
            # caused by malformed/repeated tool-call frames. This is NOT a
            # cross-restart idempotency guarantee.
            if call.call_id in batch_call_ids:
                skipped = "Error: duplicate tool-call id in this batch; execution skipped."
                self._emit_tool_status(session, call.call_id, status="refused", text=skipped)
                self._runtime_event(
                    "tool.dispatch.blocked", session,
                    data={"kind": "batch", "call_id": call.call_id,
                          "tool": call.name, "reason": "duplicate_call_id"},
                )
                self._append_tool_result(session, call.call_id, skipped)
                continue
            batch_call_ids.add(call.call_id)
            arguments = call.arguments()
            if call.name == DEFERRED_TOOL_SEARCH_NAME:
                tool_search_calls += 1
                result_text, waited_ms, newly_loaded = self._deferred_tool_search_result(
                    session, arguments
                )
                mcp_wait_ms += waited_ms
                tool_search_loaded += newly_loaded
                self._append_tool_result(session, call.call_id, result_text)
                decision, notice, reason = self._observe_tool_execution_guard(
                    session,
                    execution_guard,
                    round_role=round_role,
                    call=call,
                    arguments=arguments,
                    result_text=result_text,
                    step=step,
                    provider_rounds_total=provider_rounds_total,
                )
                pending_replan_notice = notice or pending_replan_notice
                guard_reason = reason or guard_reason
                guard_replan_requested = guard_replan_requested or decision.action == "warn"
                guard_stop_requested = guard_stop_requested or decision.action == "halt"
                continue

            if session.native is not None and session.native.has_tool(call.name):
                native_calls += 1
                result_text, elapsed_ms, outcome = self._execute_native_tool(
                    session, call, arguments
                )
            else:
                result_text, elapsed_ms, outcome = self._execute_mcp_tool(
                    session, call, arguments
                )
            tool_ms += elapsed_ms
            if outcome == "cancelled":
                return cancelled_batch_outcome()
            self._append_tool_result(session, call.call_id, result_text)
            decision, notice, reason = self._observe_tool_execution_guard(
                session,
                execution_guard,
                round_role=round_role,
                call=call,
                arguments=arguments,
                result_text=result_text,
                step=step,
                provider_rounds_total=provider_rounds_total,
            )
            pending_replan_notice = notice or pending_replan_notice
            guard_reason = reason or guard_reason
            guard_replan_requested = guard_replan_requested or decision.action == "warn"
            guard_stop_requested = guard_stop_requested or decision.action == "halt"
            if outcome == "steered":
                steered_during_tools = True

        return _ToolBatchOutcome(
            cancelled=False,
            steered=steered_during_tools,
            replan_requested=guard_replan_requested,
            stop_requested=guard_stop_requested,
            guard_reason=guard_reason,
            pending_replan_notice=pending_replan_notice,
            tool_ms=tool_ms,
            mcp_wait_ms=mcp_wait_ms,
            native_calls=native_calls,
            tool_search_calls=tool_search_calls,
            tool_search_loaded=tool_search_loaded,
        )
