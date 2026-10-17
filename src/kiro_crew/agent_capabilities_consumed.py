"""Confirm the saved member spec a projecting session actually consumed.

Split from :mod:`kiro_crew.agent_capabilities` to keep that module under the line
cap. Every projecting backend (Claude, Codex, DeepSeek) confirms through this one
body, so the Capabilities pane reports one vocabulary whichever vendor runs the
member.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import agent_state
from kiro_crew.agent_capabilities import CapabilityError, _digest


def consumed_spec_matches(spec: dict[str, Any] | None, intent: dict[str, Any]) -> bool:
    """Whether the spec a session CONSUMED is the one the saved intent materialized.

    The spec must be the projection's own parse, the one the session's array was
    built from, never a re-read after startup: a re-read proves the file, not the
    session.
    """
    return spec is not None and _digest(spec) == intent.get("materialized")


def confirm_consumed_projection(
    template: str, spec: dict[str, Any] | None, *, withheld: bool = False
) -> tuple[str, ...] | None:
    """Confirm a projecting session consumed *template*'s saved spec. Blocking.

    The one body behind every projecting backend's confirmation. ``None`` when the
    template has no saved intent (not a member: nothing to confirm); otherwise the
    projection gaps of the consumed spec. Raises ``CapabilityError`` with
    ``capability_state_unreadable`` when the saved intent cannot be read, and with
    ``capability_runtime_unverified`` when the array was *withheld* or was built
    from a different spec than the one saved.
    """
    try:
        intent = agent_state.get_capabilities(template)
    except (OSError, ValueError) as exc:
        raise CapabilityError("capability_state_unreadable") from exc
    if intent is None:
        return None
    if withheld or not consumed_spec_matches(spec, intent):
        raise CapabilityError("capability_runtime_unverified")
    assert spec is not None
    return projection_gaps(spec)


def projection_gaps(spec: dict[str, Any]) -> tuple[str, ...]:
    """Saved fields a projected session declares but no projecting host enforces.

    Shared by every backend that loads a member by projection rather than natively,
    so the Capabilities pane reports one vocabulary whichever vendor runs the member.
    """
    gaps = []
    for field in ("hooks", "toolsSettings", "excludedTools"):
        if spec.get(field):
            gaps.append(field)
    tools = spec.get("tools", [])
    if tools != "*" and (not isinstance(tools, list) or "*" not in tools):
        gaps.append("native_tools")
    if isinstance(tools, list) and any(
        isinstance(ref, str) and ref.startswith("@") and "/" in ref for ref in tools
    ):
        gaps.append("per_tool_mounts")
    if spec.get("allowedTools") or any(
        isinstance(server, dict) and server.get("autoApprove")
        for server in spec.get("mcpServers", {}).values()
    ):
        gaps.append("auto_approval")
    return tuple(gaps)
