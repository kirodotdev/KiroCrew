"""Whether a harness runtime is fenced tighter than a gateway backend.

A backend gatewayd starts runs outside the runtime's own sandbox. A runtime
whose sandbox tier is stricter than ``standard``, or whose enforced harness
runs under Kiro Crew's credential mask, would reach a process fenced less than
itself through any backend the daemon serves it. The gateway says so in the
``claim`` it pushes for that runtime (``claim.build_claim_frame``), and gatewayd
then refuses every Register from under it (``daemon.identity._caller_confined``),
so each of its servers runs per chat inside the runtime's sandbox instead.

Read only off the gateway's own runtime objects, never off a frame: the agent
inside the runtime cannot choose the answer.
"""

from __future__ import annotations


def runtime_confined(acp_backend: str, sandbox_mode: str) -> bool:
    """Whether a runtime of *acp_backend* spawned at *sandbox_mode* is confined.

    True when the tier ``wrap_argv`` applies for *sandbox_mode* (the governed
    ``sandbox.min_level`` floor included) is stricter than ``standard``, or when
    *acp_backend* is an enforced harness whose credential mask applies at that
    tier. An empty *sandbox_mode* (a caller that names no runtime) is not
    confined. A tier that cannot be read answers True: the cost is a per-chat
    server, never a fence lost.
    """
    if not sandbox_mode:
        return False
    try:
        from kiro_crew.agent_sdk.tool_gate import is_enforced
        from kiro_crew.sandbox_spawn import credential_mask_applies
        from kiro_crew.sandbox_tiers import _TIER_ORDINALS, _mode_to_level, effective_sandbox_mode

        level = _mode_to_level(effective_sandbox_mode(sandbox_mode))
        if _TIER_ORDINALS[level] > _TIER_ORDINALS["standard"]:
            return True
        return (
            bool(acp_backend) and is_enforced(acp_backend) and credential_mask_applies(sandbox_mode)
        )
    except Exception:  # fail closed: an unreadable tier is treated as confined
        return True
