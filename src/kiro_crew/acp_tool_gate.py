"""Re-export shim: the tool-gate routing verdict now lives behind the agent-SDK boundary.

The implementation moved to :mod:`kiro_crew.agent_sdk.tool_gate` in RFC PR 3,
alongside the backend registry it reads. Read that module for what a verdict means
and what it does NOT guarantee; this file exists so ``acp/client.py`` and the
tests that monkeypatch this module needed no edit in the same commit as the move.

**Re-export, never a copy**, and one consequence is worth naming: ``acp/client.py``
imports this module and calls through it by attribute
(``acp_tool_gate.enforce_sandbox_floor(...)``), so a test that patches an attribute
HERE still changes what the client calls. Patching a name on
``agent_sdk.tool_gate`` instead reaches the internal callers too. Both work; they
are not the same reach.

The four names re-exported from ``agent_sdk.backends`` (``Routing``,
``routing_for``, ``permission_config_for``, ``ACP_BACKEND_CODEX``) were reachable
through the old module as well, because it imported them into its own namespace.
They stay reachable, so a caller reading ``acp_tool_gate.permission_config_for``
keeps working -- ``acp/client.py`` is one.
"""

from __future__ import annotations

from kiro_crew.agent_sdk.backends import (  # noqa: F401 - re-exported for existing importers
    ACP_BACKEND_CODEX,
    Routing,
    permission_config_for,
    routing_for,
)
from kiro_crew.agent_sdk.tool_gate import (  # noqa: F401 - re-exported for existing importers
    ADAPTER_EXPOSED_CREDENTIAL_LEAVES,
    ADAPTER_OWN_CREDENTIAL_LEAVES,
    ENFORCED_ROUTINGS,
    UNENFORCED_CONTROLS,
    ToolGateUnroutable,
    Verdict,
    adapter_expose_files,
    adapter_hidden_credential_dirs,
    enforce_runtime_routing,
    enforce_sandbox_floor,
    is_enforced,
    label_for,
    remediation_for,
    routing_verdict,
    session_config_issue,
)

__all__ = [
    "ACP_BACKEND_CODEX",
    "ADAPTER_EXPOSED_CREDENTIAL_LEAVES",
    "ADAPTER_OWN_CREDENTIAL_LEAVES",
    "ENFORCED_ROUTINGS",
    "Routing",
    "ToolGateUnroutable",
    "UNENFORCED_CONTROLS",
    "Verdict",
    "adapter_expose_files",
    "adapter_hidden_credential_dirs",
    "enforce_runtime_routing",
    "enforce_sandbox_floor",
    "is_enforced",
    "label_for",
    "permission_config_for",
    "remediation_for",
    "routing_for",
    "routing_verdict",
    "session_config_issue",
]
