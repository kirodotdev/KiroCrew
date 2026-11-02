"""The LTS built-in baseline policy: each governed scope's minimal set.

Data only. In an LTS build a scope the remote policy does not name is
answered from this table instead of being permitted (RFC
``docs/request-for-change/rfc-crew-lts-edition.md``, decision D3 and the
appendix "Minimal set" column). The remote policy may widen past it; nothing
local can, because a profile only ever narrows the policy level it sits under.

``BASELINE_CONTROLS`` maps a ``SCOPE_CATALOG`` scope to the raw control a policy
document would carry for it, parsed by the same per-control parser a remote
policy goes through. ``BASELINE_UNGATED`` lists the scopes that keep Crew's own
default, with the reason: the two ordinals, and the open controls of D3
(appendix "Crew default"), which the organisation's
governance is meant to control. A remote policy that names an ungated scope still
answers it.
Every catalog scope (aliases aside) is in exactly one of the two;
``test_governance_lts_baseline.py`` fails on a new scope that is in
neither. ``BASELINE_INNER_LISTS`` holds the inner lists a remote policy must name
itself: one it leaves out keeps its baseline even though it named the capability.

``approval_modes`` allows ``normal``, ``trust`` and ``trust_reads``, so only ``yolo``
is off; a remote ``permission_mode`` rule may narrow it down to ``normal``. The
approval mode default stays ``normal``: under it an open ``filesystem.write`` call, and an open ``commands``
call that is not read-only, still asks first.
"""

from __future__ import annotations

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

BASELINE_CONTROLS: Dict[str, Mapping[str, object]] = {
    # Only ``yolo`` is off; ``normal`` is the catalog floor a rule cannot remove.
    "approval_modes": {"mode": "allow", "allow": ["normal", "trust", "trust_reads"]},
    # Kept off by the owner (D3), while ``apps`` opened.
    "app_versions": _OFF,
    "network.egress": _OFF,
    # Kept off by the owner (D3): chat services send data off the machine, and
    # their KGL control has not shipped.
    "channels": {"members": _OFF},
    "yolo_duration": _OFF,
    "webhooks": _OFF,
    # KAS (RFC D6). The Kiro CLI harness (``POLICY_ID_KIRO``) needs a remote rule.
    # KAS is also the LTS floor (``acp_backends.governance_floor_backend``),
    # so it is never submitted to this scope; the row records the minimal set.
    "agent_backend": {"mode": "allow", "allow": ["kas"]},
    "capabilities.browse": _CAPABILITY_OFF,
    "capabilities.script_hooks": _CAPABILITY_OFF,
    "capabilities.messaging": _CAPABILITY_OFF,
    "capabilities.agentcore": _CAPABILITY_OFF,
    # ``publish_provider.DEFAULT_PROVIDER``.
    "capabilities.publish": {
        "enabled": True,
        "scopes": {"destinations": {"mode": "allow", "allow": ["default"]}, **_PUBLISH_LISTS_OFF},
    },
    "capabilities.theme_install": _CAPABILITY_OFF,
    "capabilities.telemetry": _CAPABILITY_OFF,
    "capabilities.tailnet_origin": _CAPABILITY_OFF,
    # ``dashboard.urls._BIND_LOCAL``; never queried, since loopback is the floor.
    "bind_address": {"mode": "allow", "allow": ["127.0.0.1"]},
    # This machine's loopback origins are the floor and are never queried.
    "dashboard_origins": _OFF,
    "capabilities.computer_use": _CAPABILITY_OFF,
    "capabilities.mobile_connect": _CAPABILITY_OFF,
    "capabilities.social_share": _CAPABILITY_OFF,
    "capabilities.feature_videos_download": _CAPABILITY_OFF,
    "capabilities.decisions": _CAPABILITY_OFF,
    # Kept off by the owner (D3), with ``app_versions``.
    "capabilities.extension_install": _CAPABILITY_OFF,
    # Kept off by the owner (D3), with ``app_versions``.
    "capabilities.app_dev_mode": _CAPABILITY_OFF,
    # The session's agent and tools run in Kiro's cloud, outside this ceiling.
    "capabilities.kiro_cloud_sessions": _CAPABILITY_OFF,
    # Credential redaction stays on in the Files view.
    "capabilities.credential_redaction_off": _CAPABILITY_OFF,
}

#: Inner lists of a capability that keep their baseline when the remote policy
#: names the capability but not the list. Without this, a policy that turns
#: ``publish`` on to name its destinations would leave every region, audience and
#: account open, because an unnamed inner list permits.
BASELINE_INNER_LISTS: Dict[str, Mapping[str, Mapping[str, object]]] = {
    "capabilities.publish": _PUBLISH_LISTS_OFF,
}

_OPEN = "open control (RFC D3): "

BASELINE_UNGATED: Dict[str, str] = {
    "approval_mode": "ordinal; never item-queried, so Crew's own approval default applies",
    "sandbox.min_level": "ordinal; unnamed keeps Crew's sandbox default, and the organisation's "
    "crew_sandbox_min_level can only raise it (RFC D7)",
    "mcp": _OPEN + "MCP servers are meant to be governed by KAS and KGL",
    "tools": _OPEN + "a remote policy that names tools still decides; each call meets its own rows",
    "commands": _OPEN
    + "under the default `normal` mode a command that is not read-only still asks",
    "filesystem.read": _OPEN + "the local floor still locks credential files",
    "filesystem.write": _OPEN
    + "under `normal` each write still asks; the local floor still applies",
    "capabilities.spawn": _OPEN + "a sub-agent runs under its parent's governance",
    "capabilities.cron": _OPEN + "scheduled jobs are meant to be governed by KGL (`cron`)",
    "apps": _OPEN + "app versions and extension install stay off",
    "capabilities.memory_writes": _OPEN
    + "memory is a core feature; its data is local and never leaves the machine",
    "capabilities.theme_persona": _OPEN + "it changes tone only, from a theme installed locally",
    "capabilities.milestones": _OPEN
    + "local UI only; the user still opts in through Feature Preview and settings",
    "capabilities.decisions_local": _OPEN
    + "a local model; it stays inert while `capabilities.decisions` is off",
    "external_access.registry_hosts": _OPEN
    + "Discover queries every registry unless a policy names the key",
}

# LTS minimal values for the policy-only fields outside ``controls``, filled
# into the authority before tiers compose (``tiers._with_lts_baseline``) so a
# local tier can only narrow them, and read again where each is consumed.
#: ``updates.channel`` (appendix "Update channel", D11): ``lts``, the edition's own
#: channel. Fixed: no tier, the central one included, moves it. Not one of
#: ``carriers.UPDATE_CHANNELS``, so an ``oss`` policy cannot pin it.
LTS_UPDATE_CHANNEL = "lts"
#: ``yolo_max_seconds`` (appendix "Auto-approval duration"): the shortest selectable
#: duration, ``agent.yolo_duration`` ``30m``. The baseline already denies ``yolo``.
LTS_YOLO_MAX_SECONDS = 1800
#: ``channels.posture.slack.allowed_channel_ids`` (appendix "Slack channels"): none.
LTS_SLACK_CHANNELS: Mapping[str, object] = _OFF
