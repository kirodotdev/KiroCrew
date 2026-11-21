"""Content-free real route event projection, shared by all Mode/Type plugins.

Routing policy and model selection stay in mode_router.  This module only
projects the already-selected decision into the bounded Runtime Trace schema.
It never fabricates an event, opens a Run, or activates an unavailable Mode.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_runtime import ExecutionPlan
    from .config import ModelSpec, ProviderSpec
    from .mode_router import RouteDecision, ReviewTarget


def selected_route_event_data(
    *,
    requested_model: str,
    selection: dict[str, Any],
    decision: RouteDecision,
    execution_plan: ExecutionPlan,
    review_target: ReviewTarget | None,
    provider: ProviderSpec,
    model: ModelSpec,
) -> dict[str, Any]:
    """Preserve the historical model.route.selected payload without side effects."""
    return {
        "requested_model": requested_model,
        "selection": selection,
        "pack": decision.pack,
        "pack_label": decision.pack_label,
        "policy": decision.policy,
        "policy_label": decision.policy_label,
        "execution_mode": execution_plan.mode,
        "execution_rationale": list(execution_plan.rationale),
        "selection_source": decision.selection_source,
        "eligible_models": len(decision.candidates),
        "in_tier_models": len(decision.execution_fallbacks),
        "excluded_models": [
            {"provider_id": p, "model": m, "reason": r}
            for p, m, r in decision.exclusions[:12]
        ],
        "secondary_stage": (
            {
                "role": review_target.role,
                "provider_id": review_target.provider.id,
                "model": review_target.model.model_id,
                "reason": review_target.reason,
            } if review_target else None
        ),
        "provider_id": provider.id,
        "resolved_model": model.model_id,
        "resolved_model_name": model.name,
        "signals": {
            "prompt_chars": decision.signals.prompt_chars,
            "history_chars": decision.signals.history_chars,
            "history_messages": decision.signals.history_messages,
            "has_image": decision.signals.has_image,
            "tools_available": decision.signals.tools_available,
            "tools_required": decision.signals.tools_required,
            # Compatibility field for older Runtime Inspector readers.
            "tools_expected": decision.signals.tools_required,
            "reasoning_effort": decision.signals.reasoning_effort,
            "code_task": decision.signals.code_task,
            "complexity": round(decision.signals.complexity, 4),
            "breadth": round(decision.signals.breadth, 4),
            "parallelizability": round(decision.signals.parallelizability, 4),
            "risk": round(decision.signals.risk, 4),
            "tool_intensity": round(decision.signals.tool_intensity, 4),
            "write_intensity": round(decision.signals.write_intensity, 4),
            "context_demand": round(decision.signals.context_demand, 4),
            "verification_value": round(decision.signals.verification_value, 4),
            "latency_sensitivity": round(decision.signals.latency_sensitivity, 4),
            "task_profile": decision.signals.task_profile,
            "estimated_input_tokens": decision.signals.estimated_input_tokens,
        },
        "reasons": list(decision.reason),
        "candidates": [
            {
                "provider_id": row.provider.id,
                "model": row.model.model_id,
                "name": row.model.name,
                "score": round(row.score, 5),
                "quality": round(row.quality, 4),
                "speed": round(row.speed, 4),
                "economy": round(row.economy, 4),
                "reliability": round(row.reliability, 4),
                "runtime_samples": row.runtime_samples,
                "runtime_confidence": row.runtime_confidence,
                "static_speed": round(row.static_speed, 4),
                "runtime_speed": (round(row.runtime_speed, 4) if row.runtime_speed is not None else None),
                "reasons": list(row.reasons),
            }
            for row in decision.candidates[:12]
        ],
    }
