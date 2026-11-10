"""A kiro-cli ``subagent`` ``toolsSettings`` entry as KAS ``subagent`` rules.

``subagent.availableAgents`` becomes a deny on every sub-agent it does not name.
Split out of ``kas_permissions`` by size: that module's
``tools_settings_to_permissions`` calls these, and owns the bounds read here.
"""

from __future__ import annotations

import re
from typing import Any

#: A sub-agent name pattern this module carries to KAS: an agent id, with ``*``
#: as the only wildcard. ``*`` is the one glob character kiro-cli's ``fnmatch``
#: and KAS's matcher read alike; ``?``, ``[...]``, braces and ``!`` are left out,
#: so a pattern using them is reported rather than read two ways.
_AGENT_GLOB_RE = re.compile(r"[A-Za-z0-9_.\-*]+")


def _agent_glob(raw: str) -> str | None:
    """A ``subagent`` name pattern as KAS reads it, or ``None`` when it differs."""
    return raw if _AGENT_GLOB_RE.fullmatch(raw) else None


def _available_agents_rules(where: str, value: Any, lost: list[str]) -> list[dict[str, Any]]:
    """``subagent.availableAgents`` as a deny on every sub-agent it does not name.

    Every failure narrows. A value that is not a list, a pattern that does not
    translate, and a list over one rule's pattern bound are each named in *lost*,
    and the rule denies what it cannot express, so the session spawns no more than
    kiro-cli would let it. A list holding ``"*"`` restricts nothing and emits nothing.
    """
    # circular import: kas_permissions imports this module for its translation.
    from kiro_crew.acp.kas_permissions import _MAX_PATTERN_LENGTH, _MAX_PATTERNS_PER_RULE

    if not isinstance(value, list):
        lost.append(where)
        return [{"capability": "subagent", "effect": "deny"}]
    patterns: list[str] = []
    for raw in value:
        glob = _agent_glob(raw.strip()) if isinstance(raw, str) else None
        if glob is None or len(glob) > _MAX_PATTERN_LENGTH:
            lost.append(where)
        else:
            patterns.append(glob)
    patterns = list(dict.fromkeys(patterns))
    if "*" in patterns:
        return []
    if not patterns:
        # `[]` is kiro-cli's "spawn nothing". KAS takes an unconditional deny off
        # the agent's tool surface, which refuses the same spawns.
        return [{"capability": "subagent", "effect": "deny"}]
    if len(patterns) > _MAX_PATTERNS_PER_RULE:
        # Two deny-all-but rules with different excludes deny everything between
        # them, so the list cannot be split; it is refused whole instead.
        lost.append(where)
        return [{"capability": "subagent", "effect": "deny"}]
    return [{"capability": "subagent", "match": ["*"], "exclude": patterns, "effect": "deny"}]
