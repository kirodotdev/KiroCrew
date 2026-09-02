"""The one import surface between application code and an agent backend.

Provider-neutral turn usage and stop-reason vocabulary live here first. The
boundary is enforced by ``scripts/check_agent_sdk_boundary.py`` and
``test/test_agent_sdk_boundary.py``, so application code cannot introduce new
direct dependencies on the backend packages while the rest of the SDK is built.

The first capability to land is :mod:`kiro_crew.agent_sdk.backend_install` --
whether each backend's harness is actually installed on THIS machine, as a
three-state verdict (``installed`` / ``missing`` / ``unknown``) that names the
absent component and never reports a failed check as an absent install. Its
contract lives above the boundary; every resolve it needs is the ACP driver's.

Layering::

    consumers        dashboard/  slack/  discord/  telegram/  messaging/
                     session.py  subagent.py  apps/  cli_*.py  workflows/
                            |
                            |  import kiro_crew.agent_sdk for anything
                            |  provider-specific. A file ALREADY in the
                            |  boundary baseline keeps its direct ACP
                            |  imports; a file that is clean today is
                            |  REFUSED a new one by the gate
                            v
                     kiro_crew.agent_sdk          domain types, role protocols,
                                                  capabilities, supervisor
                            |
                            |  resolves drivers through a registry
                            v
                     kiro_crew.agent_sdk.drivers.acp
                                                  the only module INSIDE this
                                                  package that imports
                                                  kiro_crew.acp
                            v
                     kiro_crew.acp  (foundation)  wire, dialects, adapters,
                                                  session handles, worker pool

If the gate goes red you introduced a boundary violation: fix the import
direction rather than relaxing the rule, and never add or raise a line in
``.github/agent-sdk-boundary-baseline.txt`` to make it green.

Design of record, including what each later phase moves in here:
``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``.
"""

from __future__ import annotations

from typing import Protocol

from kiro_crew.agent_sdk.backend_install import (
    CACHE_TTL_SECONDS,
    COMPONENT_CLAUDE_ACP_ADAPTER,
    COMPONENT_CLAUDE_CODE_CLI,
    COMPONENT_KIRO_CLI,
    INSTALLED,
    MISSING,
    UNKNOWN,
    BackendInstallState,
    clear_probe_cache,
    probe_backend,
    probe_backends,
)

TURN_STOP_REASON_CANCELLED = "cancelled"
TURN_STOP_REASON_END_TURN = "end_turn"


class AgentTurnUsage(Protocol):
    """Provider-neutral token dimensions reported for one completed turn."""

    input_tokens: int
    output_tokens: int


__all__ = [
    "AgentTurnUsage",
    "CACHE_TTL_SECONDS",
    "COMPONENT_CLAUDE_ACP_ADAPTER",
    "COMPONENT_CLAUDE_CODE_CLI",
    "COMPONENT_KIRO_CLI",
    "INSTALLED",
    "MISSING",
    "UNKNOWN",
    "BackendInstallState",
    "clear_probe_cache",
    "probe_backend",
    "probe_backends",
    "TURN_STOP_REASON_CANCELLED",
    "TURN_STOP_REASON_END_TURN",
]
