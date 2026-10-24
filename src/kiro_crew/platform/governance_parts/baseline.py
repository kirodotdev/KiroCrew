"""The enterprise built-in baseline policy: each governed scope's minimal set.

Data only. In an enterprise build a scope the remote policy does not name is
answered from this table instead of being permitted (RFC
``docs/request-for-change/rfc-crew-enterprise-edition.md``, decision D3 and the
appendix "Minimal set" column). The remote policy may widen past it; nothing
local can, because a profile only ever narrows the policy level it sits under.

``BASELINE_CONTROLS`` maps a ``SCOPE_CATALOG`` scope to the raw control a policy
document would carry for it, parsed by the same per-control parser a remote
policy goes through. ``BASELINE_UNGATED`` lists the scopes that keep Crew's own
default, with the reason. Every catalog scope (aliases aside) is in exactly one
of the two; ``test_governance_enterprise_baseline.py`` fails on a new scope that
is in neither.

Two rows differ from the appendix. ``tools`` is off, not read-only tools: read-only
tools wait on the tools/commands classifier fix (a separate item), because the gate
also asks ``commands`` about every bare tool name and ``commands`` is off.
``approval_modes`` carries the catalog's undeniable floor (``normal``, ``trust``,
``trust_reads``), so only ``yolo`` is off.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping

_OFF: Mapping[str, object] = {"mode": "allow", "allow": []}
_CAPABILITY_OFF: Mapping[str, object] = {"enabled": False}

# Crew's default workspace root: ``config.paths._default_workspace_base()`` joined
# with ``_WORKSPACE_DIR_NAME``. The base is ``~/workplace`` everywhere, or
# ``/Volumes/workplace`` on macOS when that volume exists. Fixed here on purpose:
# the configured root comes from ``KIROCREW_WORKSPACE``, the saved
# ``workspace_dir`` file or ``workspaces[].dir`` in config, all local sources
# D3 says may not widen a control. ``~`` is expanded at match time.
_DEFAULT_WORKSPACE_ROOTS = (
    os.path.join("~", "workplace", "kirocrew-workspace"),
    os.path.join("/Volumes", "workplace", "kirocrew-workspace"),
)

BASELINE_CONTROLS: Dict[str, Mapping[str, object]] = {
    # Off, although the appendix's minimal set is read-only tools. The gate asks
    # ``commands`` about every bare tool name too (``classify_tool_title``), so
    # with ``commands`` off a read-only set here could never pass. Read-only tools
    # wait on the tools/commands classifier fix, a separate item.
    "tools": _OFF,
    # The catalog's ``always_permitted`` floor: only ``yolo`` is off.
    "approval_modes": {"mode": "allow", "allow": ["normal", "trust", "trust_reads"]},
    "mcp": {"mode": "allow", "allow": ["@kirocrew-core"]},
    "apps": _OFF,
    "commands": _OFF,
    "filesystem.read": {
        "mode": "allow",
        "allow": [p for root in _DEFAULT_WORKSPACE_ROOTS for p in (root, os.path.join(root, "**"))],
    },
    "filesystem.write": _OFF,
    "network.egress": _OFF,
    "channels": {"members": _OFF},
    "yolo_duration": _OFF,
    # ``acp_backends.POLICY_ID_KIRO``.
    "agent_backend": {"mode": "allow", "allow": ["kiro"]},
    "capabilities.spawn": _CAPABILITY_OFF,
    "capabilities.memory_writes": _CAPABILITY_OFF,
    "capabilities.browse": _CAPABILITY_OFF,
    "capabilities.script_hooks": _CAPABILITY_OFF,
    "capabilities.cron": _CAPABILITY_OFF,
    "capabilities.messaging": _CAPABILITY_OFF,
    "capabilities.agentcore": _CAPABILITY_OFF,
    # ``publish_provider.DEFAULT_PROVIDER``.
    "capabilities.publish": {
        "enabled": True,
        "scopes": {"destinations": {"mode": "allow", "allow": ["default"]}},
    },
    "capabilities.theme_persona": _CAPABILITY_OFF,
    "capabilities.theme_install": _CAPABILITY_OFF,
    "capabilities.telemetry": _CAPABILITY_OFF,
    "capabilities.tailnet_origin": _CAPABILITY_OFF,
    "capabilities.mobile_connect": _CAPABILITY_OFF,
    "capabilities.social_share": _CAPABILITY_OFF,
    "capabilities.feature_videos_download": _CAPABILITY_OFF,
    "capabilities.decisions": _CAPABILITY_OFF,
    "capabilities.decisions_local": _CAPABILITY_OFF,
}

BASELINE_UNGATED: Dict[str, str] = {
    "approval_mode": "ordinal; never item-queried, so Crew's own approval default applies",
    "sandbox.min_level": "ordinal; the RFC appendix keeps Crew's current sandbox default",
}
