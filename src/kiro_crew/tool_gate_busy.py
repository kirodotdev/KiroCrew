"""The answer a caller gives when its tool-gate call never got a worker.

``executors.run_in_tool_gate_pool`` bounds how long a gate call waits for an
``mc-toolgate`` worker and, when none frees in time, returns the caller's own
``on_queue_timeout(waited)`` instead of a verdict.  The gate never judged that
call, so the answer is always a refusal.  It is a policy-state refusal, not a
security one (``ToolHookResult.deny_policy``): the attempt is not the problem, a
saturated pool is, and a durable refusal budget (cron auto-pause) must not count
it against the job.
"""

from __future__ import annotations

from kiro_crew.hooks import ToolHookResult

#: Why a call the gate never ran was refused. The seconds waited go to the log
#: line ``run_in_tool_gate_pool`` writes, not to the agent.
TOOL_GATE_BUSY_REASON = (
    "Blocked: the security gate was busy with every tool-gate worker occupied, "
    "so this call was refused without being checked. Retry it."
)


def hook_gate_busy(waited: float) -> ToolHookResult:
    """``on_queue_timeout`` for a ``hooks.on_tool_call`` gate call."""
    return ToolHookResult.deny_policy(TOOL_GATE_BUSY_REASON)
