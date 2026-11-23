"""The last step of a KAS session's ``customAgents`` batch: who its sub-agent tool may run.

``KasHarness.session_extras`` hands this the session agent's projected entries and
the targets ``kas_subagent_targets.project_subagent_targets`` registered. Kiro
Crew's own agents are denied to every entry that mounts the tool, listed or not
(``kas_subagent_targets.deny_own_agents``).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.kas_subagent_targets import deny_own_agents


def native_batch(
    custom_agents: list[dict[str, Any]], targets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The session's batch: its own entries, then its targets, own agents denied."""
    return deny_own_agents([*custom_agents, *targets])


__all__ = ["native_batch"]
