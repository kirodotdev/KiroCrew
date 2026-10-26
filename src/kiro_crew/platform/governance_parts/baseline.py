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
is in neither. ``BASELINE_INNER_LISTS`` holds the inner lists a remote policy
must name itself: one it leaves out keeps its baseline even though it named the
capability.

Two rows differ from the appendix in detail. ``tools`` holds the LOCAL read-only
built-ins only (``fs_read``, ``glob``, ``grep``): ``web_fetch`` and ``web_search``
are read-only but reach the network, and ``network.egress`` is off. The gate
judges a proven built-in in its own row (``gate_decision``'s ``own_scopes``), so
``commands`` being off does not refuse these; a search with no path is refused,
because the directory it searches cannot be checked against ``filesystem.read``.
``approval_modes`` carries the catalog's undeniable floor (``normal``, ``trust``,
``trust_reads``), so only ``yolo`` is off.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping

_OFF: Mapping[str, object] = {"mode": "allow", "allow": []}
_CAPABILITY_OFF: Mapping[str, object] = {"enabled": False}
# The appendix rows "Deploy regions", "Publish audiences" and "Deploy accounts":
# off, with no minimal set.
_PUBLISH_LISTS_OFF: Mapping[str, Mapping[str, object]] = {
    "regions": _OFF,
    "audience": _OFF,
    "accounts": _OFF,
}

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
    # The local read-only built-ins: each one's ``BUILTIN_TOOL_SCOPES`` row is
    # ``filesystem.read`` alone, so its reads still meet the row below.
    "tools": {"mode": "allow", "allow": ["fs_read", "glob", "grep"]},
    # The catalog's ``always_permitted`` floor: only ``yolo`` is off.
    "approval_modes": {"mode": "allow", "allow": ["normal", "trust", "trust_reads"]},
    "mcp": {"mode": "allow", "allow": ["@kirocrew-core"]},
    "apps": _OFF,
    "app_versions": _OFF,
    "commands": _OFF,
    "filesystem.read": {
        "mode": "allow",
        "allow": [p for root in _DEFAULT_WORKSPACE_ROOTS for p in (root, os.path.join(root, "**"))],
    },
    "filesystem.write": _OFF,
    "network.egress": _OFF,
    "channels": {"members": _OFF},
    "yolo_duration": _OFF,
    "webhooks": _OFF,
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
        "scopes": {"destinations": {"mode": "allow", "allow": ["default"]}, **_PUBLISH_LISTS_OFF},
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
    "capabilities.extension_install": _CAPABILITY_OFF,
    "capabilities.app_dev_mode": _CAPABILITY_OFF,
}

#: Inner lists of a capability that keep their baseline when the remote policy
#: names the capability but not the list. Without this, a policy that turns
#: ``publish`` on to name its destinations would leave every region, audience and
#: account open, because an unnamed inner list permits.
BASELINE_INNER_LISTS: Dict[str, Mapping[str, Mapping[str, object]]] = {
    "capabilities.publish": _PUBLISH_LISTS_OFF,
}

BASELINE_UNGATED: Dict[str, str] = {
    "approval_mode": "ordinal; never item-queried, so Crew's own approval default applies",
    "sandbox.min_level": "ordinal; the RFC appendix keeps Crew's current sandbox default",
}

# Enterprise minimal values for the policy-only fields outside ``controls``, filled
# into the authority before tiers compose (``tiers._with_enterprise_baseline``) so a
# local tier can only narrow them, and read again where each is consumed.
#: ``updates.channel`` (appendix "Update channel"): stable.
ENTERPRISE_UPDATE_CHANNEL = "stable"
#: ``yolo_max_seconds`` (appendix "Auto-approval duration"): the shortest selectable
#: duration, ``agent.yolo_duration`` ``30m``. The baseline already denies ``yolo``.
ENTERPRISE_YOLO_MAX_SECONDS = 1800
#: ``channels.posture.slack.allowed_channel_ids`` (appendix "Slack channels"): none.
ENTERPRISE_SLACK_CHANNELS: Mapping[str, object] = _OFF
