"""Compiled, product-neutral execution intent for the Turn Engine.

Policy packs may expose rich user-facing Mode/Type semantics.  The Turn Engine
must not depend on those product names.  This module compiles one parsed policy
into the small immutable machine contract consumed by routing/execution.
"""
from __future__ import annotations

from dataclasses import dataclass

from .policy_packs import (
    AutoReasoningProfile,
    ExecutionPolicy,
    OrchestrationProfile,
    PolicyDefinition,
    RoutingProfile,
    SecondaryProfile,
)


@dataclass(frozen=True)
class ExecutionIntent:
    """Pure machine policy; deliberately contains no Mode/Type display names."""

    routing: RoutingProfile
    orchestration: OrchestrationProfile
    secondary: SecondaryProfile | None
    reasoning: AutoReasoningProfile
    primary_reasoning: str
    sticky: bool
    collaboration: str
    collaboration_tier_pool: str
    collaboration_routing: RoutingProfile | None
    collaboration_provider_affinity: float

    @property
    def auto_reasoning(self) -> AutoReasoningProfile:
        """Compatibility alias used by existing reasoning helpers."""
        return self.reasoning


def compile_execution_intent(policy: PolicyDefinition | ExecutionPolicy) -> ExecutionIntent:
    execution = policy.execution if isinstance(policy, PolicyDefinition) else policy
    return ExecutionIntent(
        routing=execution.routing,
        orchestration=execution.orchestration,
        secondary=execution.secondary,
        reasoning=execution.auto_reasoning,
        primary_reasoning=execution.primary_reasoning,
        sticky=execution.sticky,
        collaboration=execution.collaboration,
        collaboration_tier_pool=execution.collaboration_tier_pool,
        collaboration_routing=execution.collaboration_routing,
        collaboration_provider_affinity=execution.collaboration_provider_affinity,
    )
