"""kiro-cli ``toolsSettings`` key spellings, as Crew's KAS translation reads them.

A LEAF: stdlib only, so every reader can import it with no cycle. The KAS
translation (:mod:`kiro_crew.acp.kas_permissions`, which re-exports
:func:`canonical_tools_settings`), its notice and tool view, the spawn gate's
``availableAgents`` reader, the read-only side spec and the direct client's
spec allowlists (:mod:`kiro_crew.acp.spec_tools_allowlists`) all read one key per
kiro-cli alias group, the one kiro-cli keeps.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: ``toolsSettings`` tool key -> the kiro-cli family it configures.
_TOOLS_SETTINGS_FAMILY: dict[str, str] = {
    "execute_bash": "shell",
    "shell": "shell",
    "fs_read": "read",
    "read": "read",
    "glob": "read",
    "grep": "read",
    "fs_write": "write",
    "write": "write",
    "web_fetch": "web_fetch",
    "subagent": "subagent",
    "use_subagent": "subagent",
}

#: kiro-cli's ``toolsSettings`` alias groups, in its own order
#: (``ToolsSettings::ALIAS_GROUPS``, agent_config/definitions.rs). kiro-cli keeps
#: the spelling that comes FIRST in its group and drops the others
#: (``canonicalize_tools_settings_aliases``, agent_config/load.rs).
_TOOLS_SETTINGS_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("read", "fsRead", "fs_read"),
    ("write", "fsWrite", "fs_write"),
    ("shell", "execute_bash", "executeBash", "executeCmd", "execute_cmd"),
    ("use_aws", "useAws", "aws"),
    ("crew", "agent_crew", "agentCrew", "subagent", "use_subagent"),
    ("web_fetch", "webFetch"),
)

#: The spelling a kept alias is filed under when Crew's readers do not know the
#: one the spec used (``executeBash`` -> ``shell``), so a notice names that
#: spelling. A spelling Crew already reads keeps its own name.
_TOOLS_SETTINGS_CANONICAL: dict[str, str] = {
    alias: ("subagent" if group[0] == "crew" else group[0])
    for group in _TOOLS_SETTINGS_ALIAS_GROUPS
    for alias in group
}


def canonical_tools_settings(tools_settings: Any) -> tuple[Any, list[str]]:
    """*tools_settings* with each kiro-cli alias group folded to one key.

    Returns ``(settings, dropped)``. Per group the spelling kiro-cli reads first
    wins, the others are dropped and listed in *dropped*, the way kiro-cli
    loads the spec. The winner keeps its own name when Crew reads that spelling,
    else it is renamed (:data:`_TOOLS_SETTINGS_CANONICAL`); the sub-agent group
    is always filed under ``subagent``. Every other key is
    kept as is. A value that is not an object is returned unchanged. Idempotent.
    """
    if not isinstance(tools_settings, dict):
        return tools_settings, []
    out = dict(tools_settings)
    dropped: list[str] = []
    for group in _TOOLS_SETTINGS_ALIAS_GROUPS:
        present = [key for key in group if key in out]
        if not present:
            continue
        winner, *rest = present
        for key in rest:
            del out[key]
            dropped.append(key)
        target = _TOOLS_SETTINGS_CANONICAL[winner]
        # The sub-agent group is always filed under `subagent`, the one key the
        # spawn gate and the unapplied-settings notice both read, so the two can
        # never disagree about a `use_subagent` spelling.
        renamed = winner not in _TOOLS_SETTINGS_FAMILY and winner != "use_aws"
        if winner != target and (renamed or target == "subagent"):
            out[target] = out.pop(winner)
    if dropped:
        logger.info(
            "agent spec has duplicate `toolsSettings` alias keys; kept the one "
            "kiro-cli reads first and dropped %s",
            ", ".join(sorted(dropped)),
        )
    return out, sorted(dropped)
