"""The one import surface between application code and an agent backend.

Provider-neutral turn usage and stop-reason vocabulary live here first. The
boundary is enforced by ``scripts/check_agent_sdk_boundary.py`` and
``test/test_agent_sdk_boundary.py``, so application code cannot introduce new
direct dependencies on the backend packages while the rest of the SDK is built.

Which backends this build can serve, and every capability set a harness may
claim, live in :mod:`kiro_crew.agent_sdk.backends`; the per-session record a
consumer asks instead of naming a harness is
:class:`kiro_crew.agent_sdk.capabilities.SessionCapabilities`. Whether a harness's
tool calls reach the PreToolUse gate is :mod:`kiro_crew.agent_sdk.tool_gate`.

Machine-local backend readiness lives in :mod:`kiro_crew.agent_sdk.backend_install`.
How a harness signs in -- which store holds its entitlement, and the remedy to
print when it does not -- is declared once per harness in
:mod:`kiro_crew.agent_sdk.host_auth`, and the credential floor, the sandbox mask,
the doctor row and the backend panel all project from that one declaration.
Promptless structured command batches live in
:mod:`kiro_crew.agent_sdk.native_commands`; their ACP process lifecycle and
exception translation stay in the driver, and only plain data crosses upward.

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
from kiro_crew.agent_sdk.capabilities import (
    MODEL_NAMESPACE_ACP,
    UNKNOWN_BACKEND_CAPABILITIES,
    SessionCapabilities,
    capabilities_for,
    capabilities_of,
)
from kiro_crew.agent_sdk.host_auth import (
    UNKNOWN_AGENT_AUTH,
    AgentAuthDeclaration,
    AgentInteractiveLogin,
    declaration_for,
    entitlement_label,
    signs_in_separately,
)
from kiro_crew.agent_sdk.native_commands import NativeCommandBatch, run_kiro_native_commands

TURN_STOP_REASON_CANCELLED = "cancelled"
TURN_STOP_REASON_END_TURN = "end_turn"


class AgentTurnUsage(Protocol):
    """Provider-neutral token dimensions reported for one completed turn."""

    input_tokens: int
    output_tokens: int


__all__ = [
    "AgentTurnUsage",
    "MODEL_NAMESPACE_ACP",
    "SessionCapabilities",
    "UNKNOWN_BACKEND_CAPABILITIES",
    "capabilities_for",
    "capabilities_of",
    "UNKNOWN_AGENT_AUTH",
    "AgentAuthDeclaration",
    "AgentInteractiveLogin",
    "declaration_for",
    "entitlement_label",
    "signs_in_separately",
    "CACHE_TTL_SECONDS",
    "COMPONENT_CLAUDE_ACP_ADAPTER",
    "COMPONENT_CLAUDE_CODE_CLI",
    "COMPONENT_KIRO_CLI",
    "INSTALLED",
    "MISSING",
    "UNKNOWN",
    "BackendInstallState",
    "NativeCommandBatch",
    "clear_probe_cache",
    "probe_backend",
    "probe_backends",
    "run_kiro_native_commands",
    "TURN_STOP_REASON_CANCELLED",
    "TURN_STOP_REASON_END_TURN",
]
