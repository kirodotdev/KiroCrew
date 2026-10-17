"""Adaptive concurrency control for the gateway (RFC overload-resilience §5).

Three modules, one direction of dependency:

* :mod:`.signals` -- what the controller looks at: the :class:`Sample` shape,
  the thresholds, and the pure classifier that turns one sample into a
  :class:`PressureReport` (which signals fired, whether they corroborate each
  other, whether the pressure is severe, which provider scopes are throttled).
* :mod:`.policy` -- :class:`AdaptivePolicy`, the deterministic AIMD state
  machine. It takes samples and returns :class:`Decision` objects; it never
  touches a clock, a socket or a manager.
* :mod:`.controller` -- :class:`AdaptiveController`, the asyncio task that
  samples the host, feeds the policy and drives the two actuators: the
  gateway's execution cap (``SubagentManager.set_effective_cap``) and the
  daemon's spawn gate (``GatewayManager.set_spawn_capacity``).

The user's ``agent.max_subagents`` is the ceiling and is never written; the
controller only ever moves a runtime value beneath it.
"""

from .policy import AdaptivePolicy, Decision, PolicyParams
from .signals import PressureReport, Sample, SpawnGateStats, Thresholds, classify

__all__ = [
    "AdaptivePolicy",
    "Decision",
    "PolicyParams",
    "PressureReport",
    "Sample",
    "SpawnGateStats",
    "Thresholds",
    "classify",
]
