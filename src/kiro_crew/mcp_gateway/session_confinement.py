"""Whether a session may use the gateway's shared MCP backends.

A shared backend runs in the global ``standard`` sandbox
(``backend._sandboxed_backend_argv``). A session confined beyond that tier, or
whose harness runs under Kiro Crew's credential mask, would reach a process
fenced less than itself. The provider factory puts :data:`PER_CHAT_ENV` in such a
session's environment, and each of its stubs then takes its local per-chat
fallback (``stub_fallback.fallback_exec``) without contacting the gateway: the
server runs inside the session's own sandbox, and a ``secret://`` value, which
only the gateway resolves, refuses the launch instead of reaching it.
"""

from __future__ import annotations

#: Set to ``"1"`` in a confined session's environment; read by the stub. The
#: session's own process environment is outside the agent's reach.
PER_CHAT_ENV = "KIROCREW_MCP_PER_CHAT"


def is_per_chat_env(env: object) -> bool:
    """Whether a session's extra environment *env* carries the per-chat mark."""
    return isinstance(env, dict) and env.get(PER_CHAT_ENV) == "1"


def uses_shared_backends(backend: str, tier: str) -> bool:
    """Whether a session of *backend* at sandbox *tier* may attach to shared backends.

    *tier* is the tier the session starts at (``harness_sandbox_mode``). False when
    the tier the wrap applies (the governed ``sandbox.min_level`` floor included) is
    stricter than ``standard``, or when *backend* is an enforced harness whose
    credential mask applies at that tier.
    """
    from kiro_crew.agent_sdk.tool_gate import is_enforced
    from kiro_crew.sandbox_spawn import credential_mask_applies
    from kiro_crew.sandbox_tiers import _TIER_ORDINALS, _mode_to_level, effective_sandbox_mode

    level = _mode_to_level(effective_sandbox_mode(tier))
    if _TIER_ORDINALS[level] > _TIER_ORDINALS["standard"]:
        return False
    return not (is_enforced(backend) and credential_mask_applies(tier))
