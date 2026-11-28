"""Pure projection of a provider turn into a diagnostic telemetry record.

This module has no ACP transport, permission, session persistence, or tool
execution responsibilities. It does not mutate the run/turn state.
"""

from __future__ import annotations

from typing import Any, Callable

from .mode_router import RouteDecision, route_diagnostics
from .telemetry import TurnTelemetry


def build_turn_telemetry(
    session: Any, state: Any, *, outcome: str,
    completed_at: float, total_ms: float, first_ms: float,
    routing_decision: Any = None, error_kind: str = "",
    error_category: str = "", error_site: str = "",
    reasoning_summary: Callable[[set[str]], str],
) -> TurnTelemetry:
    """Build a record without committing it to storage."""
    selected_route = None
    if routing_decision is not None:
        selected_route = next((
            row for row in getattr(routing_decision, "candidates", ())
            if row.provider.id == state.served_provider_id
            and row.model.model_id == state.served_model_id
        ), None)
    served_spec = (
        getattr(selected_route, "model", None)
        if selected_route is not None else state.model
    )
    return TurnTelemetry(
        timestamp=completed_at,
        session_id=session.session_id,
        outcome=outcome,
        requested_model=session.model,
        is_background_role=(str(getattr(getattr(session, "agent", None), "agent_id", "") or "") == "kirocrew-lite"),
        provider_id=state.served_provider_id,
        served_model=state.served_model_id,
        effort=reasoning_summary(state.observed_efforts),
        thinking_mode=reasoning_summary(state.observed_thinking_modes),
        total_ms=total_ms,
        first_visible_ms=first_ms,
        upstream_ms=state.upstream_ms,
        tool_ms=state.tool_ms,
        mcp_wait_ms=state.mcp_wait_ms,
        tool_calls=state.tool_calls_total,
        native_calls=state.native_tool_calls_total,
        tool_search_calls=state.tool_search_calls_total,
        active_tools=state.active_tools_count,
        tool_schema_bytes=state.tool_schema_bytes,
        stream_chunks=state.stream_chunks,
        stream_chars=state.stream_chars,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        cached_tokens=state.cached_tokens,
        cache_write_tokens=state.cache_write_tokens,
        reasoning_tokens=state.reasoning_tokens,
        cache_usage_reported=state.cache_usage_reported,
        cache_write_usage_reported=state.cache_write_usage_reported,
        reasoning_usage_reported=state.reasoning_usage_reported,
        error_kind=error_kind,
        error_category=error_category,
        error_site=error_site,
        transport_retries=state.transport_retries_total,
        fallbacks=state.fallbacks_total,
        cancel_source=str(getattr(session, "cancel_source", "") or ""),
        run_id=state.turn_run_id,
        turn_id=state.turn_trace_id,
        mode_pack=(
            str(getattr(routing_decision, "pack", ""))
            if routing_decision is not None else ""
        ),
        mode_type=(
            str(getattr(routing_decision, "policy", ""))
            if routing_decision is not None else "concrete"
        ) or "concrete",
        task_profile=(
            str(getattr(getattr(routing_decision, "signals", None), "task_profile", ""))
            if routing_decision is not None else "concrete"
        ) or "concrete",
        routing_score=(
            float(selected_route.score) if selected_route is not None else 0.0
        ),
        routing_confidence=(
            selected_route.runtime_confidence if selected_route is not None else ""
        ),
        started_at=max(0.0, completed_at - (total_ms / 1000.0)),
        completed_at=completed_at,
        selection_kind=str(getattr(session, "selection_kind", "") or ""),
        selection_source=str(getattr(session, "selection_source", "") or ""),
        selection_requested=str(getattr(session, "selection_requested", "") or ""),
        thinking_preference=str(getattr(served_spec, "thinking_preference", "") or ""),
        route_diagnostics=(
            route_diagnostics(routing_decision, served_provider=state.served_provider_id,
                              served_model=state.served_model_id)
            if isinstance(routing_decision, RouteDecision) else None
        ),
        route_signals=(
            {
                key: round(max(0.0, min(1.0, float(getattr(routing_decision.signals, key, 0.0)))), 4)
                for key in (
                    "complexity", "breadth", "parallelizability", "risk",
                    "tool_intensity", "write_intensity", "context_demand",
                    "verification_value", "latency_sensitivity",
                )
                if isinstance(getattr(routing_decision.signals, key, None), (int, float))
            }
            if routing_decision is not None else None
        ),
    )
