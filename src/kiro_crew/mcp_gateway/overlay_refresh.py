"""Rebuild the agent overlays after agent specs change, without a restart.

The gateway builds ``<mcp_gateway.overlay_dir>/*.json`` once, when its broker
starts. An app registered later (install, reinstall, enable, update) writes its
agent specs into ``~/.kiro/agents`` after that pass, so its agents had no
overlay -- and no gateway-routed MCP servers -- until the next gateway start.
A Bot's own file edited later (a change card that adds or changes a
gateway-routed server on it, or **Add abilities** copying one into it) likewise
left its ``mates/<id>.json`` overlay on the start pass's content.

The broker owner (the gateway process) registers one refresher here while it
serves a broker. Three callers run their spec write as the refresh's
``register`` step: the app lifecycle, when it registers agents, and the
capabilities save and the worker-kit add (``save_and_refresh_overlays`` in
``dashboard/handlers/agent_capabilities.py``), which ask for a rewrite only when
the write gave a Bot that owns its file a gateway-routed server; that change
card, or the Add abilities pane, says a restart is needed unless the refresh
applied and its ``serving_stub_servers`` holds every such server. With no
broker running nothing is registered and the call does nothing: there is no
overlay to keep current, and the next broker start builds it from scratch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.launch_approval import LaunchApprovals

logger = logging.getLogger(__name__)

_Refresher = Callable[[Callable[[], Awaitable[bool]]], Awaitable[Any]]
_refresher: _Refresher | None = None

#: Fingerprint inputs a refresh is EXPECTED to differ in: the agent specs it
#: exists to pick up, the crewmate files beside them (``mate_sources``), the
#: settings file, the approval digest, the overlay directory (a probe pass
#: writes to a scratch one), and the servers a config scope mutes
#: (``scope_muted``), which a toggle in MCP Management changes at any time. A
#: mute only removes stubs; an unmute that adds one needs a target the running
#: daemon does not hold, and the probe's target check refuses it.
#: A roots exclusion (``roots_excluded``) only removes stubs, like a mute. An
#: observed roots request (``project_roots``) can turn a shared stub per-project;
#: a per-project target the running daemon does not hold makes that stub start
#: its server in the chat.
#: Every other input --
#: socket, work dir, sandbox and approval modes, stub set, sharing, forwarded
#: env, identity keys, read limit, interpreter -- is fixed when the broker
#: starts, and a pass that sees a different value would apply a restart-only
#: setting early. A crewmate file whose stub needs a target the running daemon
#: does not hold is still refused, by the probe's target check.
_REFRESH_VOLATILE_INPUTS = frozenset(
    {
        "sources",
        "mate_sources",
        "settings",
        "overlay_dir",
        "launch_approvals",
        "scope_muted",
        "roots_excluded",
        "project_roots",
    }
)


def set_overlay_refresher(refresher: _Refresher | None) -> None:
    """Register (or clear, with ``None``) the running broker's overlay refresh."""
    global _refresher
    _refresher = refresher


async def refresh_after_agent_change(register: Callable[[], Awaitable[bool]]) -> Any:
    """Run *register* (True if it wrote specs), then the refresh, under one lock.

    Never raises: a failed refresh leaves the overlays as they were, which is what the
    next gateway start repairs, and it must not fail the action that called it
    (a caller keeps what *register* raised itself). Returns the refresher's
    result, or ``None``.
    """
    refresher = _refresher
    if refresher is None:
        return None
    try:
        return await refresher(register)
    except Exception:  # noqa: BLE001 - best-effort; the next start rebuilds it
        logger.warning("mcp-gateway: overlay refresh after an agent change failed", exc_info=True)
        return None


def rewrite_policy(overlay_dir: Path) -> dict[str, Any] | None:
    """The fixed-at-start settings the latest pass into *overlay_dir* was built from.

    Taken from the fingerprint inputs that pass already computed, so reading it
    costs no second scan of the agent specs, and a new input the rewriter learns
    to fingerprint is compared without being named here. ``None`` when no pass
    into *overlay_dir* recorded its inputs: a refresh then cannot prove it
    matches the start, and refuses.
    """
    from kiro_crew.mcp_gateway import rewriter

    inputs = rewriter.take_pass_inputs(overlay_dir)
    if inputs is None:
        return None
    return {k: v for k, v in inputs.items() if k not in _REFRESH_VOLATILE_INPUTS}


def changes_a_launch_decision(approvals: LaunchApprovals) -> bool:
    """Whether a pass made a launch decision the approval store does not hold.

    A refresh never writes launch approvals: any such decision belongs to the
    next gateway start, which records and audits it on its own path. A refusal
    the store already records, with the same launch content, is not a new
    decision: a server still pending approval must not block every refresh.
    """
    from kiro_crew.mcp_gateway.launch_approval import _refusal_record

    if approvals.captured or approvals.rebound:
        return True
    return any(
        approvals.stored_refused.get(stem) != _refusal_record(approvals, stem, reason)
        for stem, reason in approvals.refused.items()
    )
