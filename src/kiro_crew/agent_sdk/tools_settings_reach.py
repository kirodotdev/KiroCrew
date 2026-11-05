"""Which parts of an agent spec's ``toolsSettings`` a session runs without.

On a backend that is not handed ``toolsSettings`` (every one but kiro-cli and
KAS), the field is applied nowhere with one exception:
``toolsSettings.subagent.availableAgents``, which Crew's own spawn gate reads
from the parent's spec on every backend
(:func:`kiro_crew.subagent_parts.agent_policy.spawn_allowlist`). A session-start
notice that names ``toolsSettings`` as having no effect must leave that entry
out, or it tells the author a restriction Crew enforces does nothing.
"""

from __future__ import annotations

from typing import Any

#: The ``toolsSettings`` tool key whose ``availableAgents`` Crew's spawn gate reads.
_SPAWN_GATE_TOOL = "subagent"
#: The ``toolsSettings.subagent`` key Crew's spawn gate reads on every backend.
_SPAWN_GATE_KEY = "availableAgents"


def tools_settings_unapplied(tools_settings: Any) -> list[str]:
    """The ``toolsSettings`` names a session that is not handed the field runs without.

    ``["toolsSettings"]`` when *tools_settings* is set and declares no
    ``subagent.availableAgents``, so the field is named whole. When it does
    declare one, that entry is applied by the spawn gate, so each other set
    entry is named instead: ``toolsSettings.<tool>``, and
    ``toolsSettings.subagent.<key>`` for the sibling keys under ``subagent``.
    Empty when nothing else is set. An unset or empty value is ``[]``.
    """
    if not tools_settings:
        return []
    if not isinstance(tools_settings, dict):
        return ["toolsSettings"]
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
