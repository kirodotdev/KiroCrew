"""What a registered KAS batch entry carries, read back for the session that runs it.

The projection (:func:`kiro_crew.acp.kas_agents.to_client_custom_agent`) records on
each ``_meta.kiro.customAgents`` entry what Crew answers and what KAS may decide
alone. A live session keeps the batch it registered (``set_mode`` activates, it does
not re-send), so the session handle reads those records here: ``projected_*`` for the
agent the batch was built for, matched by crewmate id, and ``switched_*`` after a KAS
mode switch, matched by wire id (``kas_wire_agent_id``).

:mod:`kiro_crew.acp.kas_agents` imports every name back, so it stays the import path
callers and tests use.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.kas_permissions import (
    HOOK_GATEABLE_CAPABILITIES,
    _entry_crew_answered,
    _entry_crew_answered_commands,
    _entry_shell_allowlist,
    unprompted_capabilities,
)
from kiro_crew.acp.kas_spec_grants import _entry_shell_readonly
from kiro_crew.agent_files import kas_wire_agent_id


def projected_auto_approved(custom_agents: Any, agent_id: str) -> frozenset[str] | None:
    """What the projection sent for *agent_id* lets KAS decide without Crew
    (``unprompted_capabilities``), or ``None`` with no projection (spawn-time agent).

    Recorded on the session when the batch is handed over, because a live session
    keeps the batch it registered: ``set_mode`` activates, it does not re-send. The
    turn loop compares this with what the PreToolUse hooks cover NOW (see
    :func:`kiro_crew.agent_sdk.spec_hooks.hook_projection_stale`).
    """
    if not isinstance(custom_agents, list) or not custom_agents:
        return None
    entries = [a for a in custom_agents if isinstance(a, dict)]
    if not entries:
        return None
    entry = next((a for a in entries if a.get("id") == agent_id), entries[0])
    return unprompted_capabilities(entry.get("permissions"))


def _registered_entry(custom_agents: Any, agent_id: str) -> Any:
    entries = [a for a in custom_agents if isinstance(a, dict)] if custom_agents else []
    return next((a for a in entries if a.get("id") == agent_id), None)


def _switched_entry(custom_agents: Any, mode_id: str) -> Any:
    entries = [a for a in custom_agents if isinstance(a, dict)] if custom_agents else []
    return next((a for a in entries if kas_wire_agent_id(str(a.get("id") or "")) == mode_id), None)


def active_registered_entry(
    custom_agents: Any, agent_id: str, mode_id: str
) -> dict[str, Any] | None:
    """The registered entry the session runs now, or ``None`` when Crew registered none for it.

    *mode_id* is the last mode KAS reported (``""`` before any switch); it is
    matched by wire id, as :func:`switched_auto_approved` matches it, so a KAS
    built-in mode never reads a crewmate's entry of the same name. Before any
    switch the entry is the batch's own *agent_id*, matched exactly.
    """
    entry = (
        _switched_entry(custom_agents, mode_id)
        if mode_id
        else _registered_entry(custom_agents, agent_id)
    )
    return entry if isinstance(entry, dict) else None


def projected_crew_answered_commands(custom_agents: Any, agent_id: str) -> tuple[str, ...]:
    """The ``allowedCommands`` patterns Crew answers for *agent_id*'s registered
    entry, matched like :func:`projected_crew_answered`."""
    return _entry_crew_answered_commands(_registered_entry(custom_agents, agent_id))


def switched_crew_answered_commands(custom_agents: Any, mode_id: str) -> tuple[str, ...]:
    """Those patterns once a KAS mode switch moves the session to *mode_id*,
    matched like :func:`switched_crew_answered`."""
    return _entry_crew_answered_commands(_switched_entry(custom_agents, mode_id))


def projected_shell_allowlist(custom_agents: Any, agent_id: str) -> tuple[str, ...] | None:
    """The ``denyByDefault`` shell allowlist of *agent_id*'s registered entry, or
    ``None``, matched like :func:`projected_crew_answered_commands`."""
    return _entry_shell_allowlist(_registered_entry(custom_agents, agent_id))


def switched_shell_allowlist(custom_agents: Any, mode_id: str) -> tuple[str, ...] | None:
    """That allowlist once a KAS mode switch moves the session to *mode_id*."""
    return _entry_shell_allowlist(_switched_entry(custom_agents, mode_id))


def projected_shell_readonly(custom_agents: Any, agent_id: str) -> bool:
    """Whether *agent_id*'s registered entry lets a read-only command past its
    ``denyByDefault`` allowlist, matched like :func:`projected_shell_allowlist`."""
    return _entry_shell_readonly(_registered_entry(custom_agents, agent_id))


def switched_shell_readonly(custom_agents: Any, mode_id: str) -> bool:
    """That once a KAS mode switch moves the session to *mode_id*."""
    return _entry_shell_readonly(_switched_entry(custom_agents, mode_id))


def projected_crew_answered(custom_agents: Any, agent_id: str) -> frozenset[str]:
    """The built-ins Crew answers itself for *agent_id*'s registered entry.

    Matched by the crewmate id exactly. Unlike :func:`projected_auto_approved` there
    is no fallback to the first entry: a session whose active agent is not in the
    batch has nothing Crew may answer for it, and empty keeps every call prompting.
    """
    entries = [a for a in custom_agents if isinstance(a, dict)] if custom_agents else []
    entry = next((a for a in entries if a.get("id") == agent_id), None)
    return _entry_crew_answered(entry)


def switched_crew_answered(custom_agents: Any, mode_id: str) -> frozenset[str]:
    """What Crew answers itself once a KAS mode switch moves the session to *mode_id*.

    *mode_id* is the engine's wire id, matched like :func:`switched_auto_approved`
    so a KAS built-in mode of the same name never inherits a crewmate's grants. A
    mode the batch does not carry answers empty: Crew never projected it.
    """
    entries = [a for a in custom_agents if isinstance(a, dict)] if custom_agents else []
    entry = next((a for a in entries if kas_wire_agent_id(str(a.get("id") or "")) == mode_id), None)
    return _entry_crew_answered(entry)


def switched_auto_approved(custom_agents: Any, agent_id: str) -> frozenset[str]:
    """What the session auto-approves once a KAS mode switch moves it to *agent_id*.

    *agent_id* is the mode id the engine reported, so entries are matched by the
    id they were sent under (``kas_wire_agent_id``). ``set_mode`` activates a
    definition KAS already holds, so the answer is the registered entry for
    *agent_id* when the batch carries one. A mode the batch
    does not carry (a KAS built-in) runs permissions Crew never projected, so every
    gateable capability is assumed approved: any PreToolUse hook then reads the
    session as stale, which is the side that re-projects.
    """
    entries = [a for a in custom_agents if isinstance(a, dict)] if custom_agents else []
    entry = next(
        (a for a in entries if kas_wire_agent_id(str(a.get("id") or "")) == agent_id), None
    )
    if entry is None:
        return frozenset(HOOK_GATEABLE_CAPABILITIES)
    return unprompted_capabilities(entry.get("permissions"))
