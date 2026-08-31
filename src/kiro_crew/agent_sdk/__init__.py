"""The one import surface between application code and an agent backend.

This package was declared before it had contents, on purpose: the boundary it
names is enforced from the moment it exists, by
``scripts/check_agent_sdk_boundary.py`` and ``test/test_agent_sdk_boundary.py``,
so the leak it is meant to drain cannot grow while the rest is still being
designed.

The first capability to land is :mod:`kiro_crew.agent_sdk.backend_install` --
whether each backend's harness is actually installed on THIS machine, as a
three-state verdict (``installed`` / ``missing`` / ``unknown``) that names the
absent component and never reports a failed check as an absent install. Its
contract lives above the boundary; every resolve it needs is the ACP driver's.

Layering::

    consumers        dashboard/  slack/  discord/  telegram/  messaging/
                     session.py  subagent.py  apps/  cli_*.py  workflows/
                            |
                            |  may import ONLY kiro_crew.agent_sdk
                            v
                     kiro_crew.agent_sdk          domain types, role protocols,
                                                  capabilities, supervisor
                            |
                            |  resolves drivers through a registry
                            v
                     kiro_crew.agent_sdk.drivers.acp
                                                  the ONLY module permitted to
                                                  import kiro_crew.acp
                            v
                     kiro_crew.acp   (private)    wire, dialects, adapters,
                                                  session handles, worker pool

If the gate goes red you introduced a boundary violation: fix the import
direction rather than relaxing the rule, and never add or raise a line in
``.github/agent-sdk-boundary-baseline.txt`` to make it green.

Design of record, including what each later phase moves in here:
``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``.
"""

from __future__ import annotations

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

__all__ = [
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
]
