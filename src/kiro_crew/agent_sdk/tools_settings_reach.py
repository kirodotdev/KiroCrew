"""Which parts of an agent spec's ``toolsSettings`` a session runs without.

On a backend that is not handed ``toolsSettings`` (every one but kiro-cli and
KAS), the field is applied nowhere with one exception:
``toolsSettings.subagent.availableAgents``, which Crew's own spawn gate reads
from the parent's spec on every backend
(:func:`kiro_crew.subagent_parts.agent_policy.spawn_allowlist`). A session-start
notice that names ``toolsSettings`` as having no effect must leave that entry
out, or it tells the author a restriction Crew enforces does nothing. On a backend
in ``ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS`` Crew's gate also applies the
shell ``denyByDefault`` and the write ``fallbackAction: "deny"`` switches, so those
two are left out there as well.
"""

from __future__ import annotations

from typing import Any

#: The ``toolsSettings`` tool key whose ``availableAgents`` Crew's spawn gate reads.
_SPAWN_GATE_TOOL = "subagent"
#: The ``toolsSettings.subagent`` key Crew's spawn gate reads on every backend.
_SPAWN_GATE_KEY = "availableAgents"
#: The shell keys Crew's gate reads as the ``denyByDefault`` allowlist.
_SHELL_ALLOWLIST_KEYS = ("denyByDefault", "allowedCommands")
#: The write keys Crew's gate reads as the ``fallbackAction: "deny"`` allowlist.
_WRITE_ALLOWLIST_KEYS = ("fallbackAction", "allowedPaths")


def _gate_applied_keys(tools_settings: dict[str, Any]) -> set[tuple[str, str]]:
    """The ``(tool, key)`` pairs Crew's gate reads as the shell and write allowlists.

    Read across both alias keys of each, as the gate's readers read them
    (``kas_permissions.spec_shell_allowlist`` and ``spec_write_allowlist``). Empty
    when the field sets neither switch.
    """
    # circular import: the acp package imports this module through kas_agents.
    from kiro_crew.acp.kas_permissions import (
        _SHELL_SETTINGS_KEYS,
        _WRITE_SETTINGS_KEYS,
        shell_denies_by_default,
        write_denies_by_default,
    )

    applied: set[tuple[str, str]] = set()
    if shell_denies_by_default(tools_settings):
        applied.update((t, k) for t in _SHELL_SETTINGS_KEYS for k in _SHELL_ALLOWLIST_KEYS)
    if write_denies_by_default(tools_settings):
        applied.update((t, k) for t in _WRITE_SETTINGS_KEYS for k in _WRITE_ALLOWLIST_KEYS)
    return applied


def _unapplied_beside_gate(
    tools_settings: dict[str, Any], applied: set[tuple[str, str]]
) -> list[str]:
    """Each set ``toolsSettings`` entry but the *applied* allowlist keys."""
    lost: list[str] = []
    for tool, entry in tools_settings.items():
        if not entry:
            continue
        if not isinstance(entry, dict):
            lost.append(f"toolsSettings.{tool}")
            continue
        lost.extend(
            f"toolsSettings.{tool}.{key}"
            for key, value in entry.items()
            if value
            and not (tool == _SPAWN_GATE_TOOL and key == _SPAWN_GATE_KEY)
            and (tool, key) not in applied
        )
    return sorted(lost)


def tools_settings_unapplied(tools_settings: Any, *, gate_allowlists: bool = False) -> list[str]:
    """The ``toolsSettings`` names a session that is not handed the field runs without.

    ``["toolsSettings"]`` when *tools_settings* is set and declares no
    ``subagent.availableAgents``, so the field is named whole. When it does
    declare one, that entry is applied by the spawn gate, so each other set
    entry is named instead: ``toolsSettings.<tool>``, and
    ``toolsSettings.subagent.<key>`` for the sibling keys under ``subagent``.
    Empty when nothing else is set. An unset or empty value is ``[]``.

    *gate_allowlists* says the session's backend is one Crew's gate enforces the
    shell and write allowlists on. When the field sets either switch, each other
    set entry is named as ``toolsSettings.<tool>.<key>``, so the notice never says
    an allowlist that refuses commands or writes goes unapplied.
    """
    if not tools_settings:
        return []
    if not isinstance(tools_settings, dict):
        return ["toolsSettings"]
    applied = _gate_applied_keys(tools_settings) if gate_allowlists else set()
    if applied:
        return _unapplied_beside_gate(tools_settings, applied)
    gated = tools_settings.get(_SPAWN_GATE_TOOL)
    if not isinstance(gated, dict) or _SPAWN_GATE_KEY not in gated:
        return ["toolsSettings"]
    lost = [
        f"toolsSettings.{tool}"
        for tool, entry in tools_settings.items()
        if entry and tool != _SPAWN_GATE_TOOL
    ]
    lost.extend(
        f"toolsSettings.{_SPAWN_GATE_TOOL}.{key}"
        for key, value in gated.items()
        if value and key != _SPAWN_GATE_KEY
    )
    return sorted(lost)
