"""A session's skill scope pinned to one agent spec FILE, read by its path.

Skill scope is otherwise resolved from a session's agent NAME
(``agent_discovery.agent_skill_globs``), and a name resolves project-first: a
checkout's spec declaring the same name would decide which skills the agent sees.
A palette-opened app agent runs its app's registered ``<app>--<agent>.json``: on
kiro-cli as the app's launch copy (``apps.agent_launch``), whose skills are served
through ``skill_search`` from that file, and on KAS as the user-level spec Crew sends.
That file, and no name lookup, is its scope.

The gateway sets the pin once a turn holds its session's lease: the registered file
for an admitted app-owned turn, none for every other turn of the key
(``dashboard.chat_turn.turn_admission``). Set no earlier, so a sibling turn waiting
for the lease cannot change the scope of the turn that is running. Every skill-scope
reader asks :func:`pinned_skill_globs` first: the ``skill_search`` route and dollar
skills (through ``agent_discovery.session_skill_globs``), the session-start and
post-compaction injection (``context._skills_injection_plan``) and the per-turn
triggered skills (``context_assembly.turn_message``). Crew's injected agent prompt
reads the pinned file too (``context_assembly.agent_spec._resolve_agent_prompt``).
Process-local, like the sessions it scopes: a gateway restart starts every session
afresh, and the first turn pins again before any skill is read.
"""

from __future__ import annotations

import threading
from pathlib import Path

_PINNED: dict[str, Path] = {}
_PINNED_LOCK = threading.Lock()


def pin_session_skill_spec(session_key: str, path: Path | None) -> None:
    """Scope *session_key*'s skills to the spec at *path*; ``None`` removes the pin."""
    if not session_key:
        return
    with _PINNED_LOCK:
        if path is None:
            _PINNED.pop(session_key, None)
        else:
            _PINNED[session_key] = Path(path)


def pinned_session_skill_spec(session_key: str | None) -> Path | None:
    """The spec *session_key*'s skills are pinned to, or ``None``."""
    if not session_key:
        return None
    with _PINNED_LOCK:
        return _PINNED.get(session_key)


def pinned_skill_globs(
    session_key: str | None,
    *,
    project_dir: str | Path | None = None,
    strict: bool = False,
    inherit_defaults: bool = False,
) -> list[str] | None:
    """The skill globs of *session_key*'s pinned spec, or ``None`` when nothing is pinned.

    The same rules ``agent_discovery.agent_skill_globs`` applies to the spec a name
    resolves to, applied to the pinned file: its ``skill://`` resources, expanded
    against that file, else the inherited trees for a spec that may inherit them.
    ``strict`` raises ``SkillScopeResolutionError`` for an unreadable file, as a
    session caller of the name lookup does for an unresolvable name.
    """
    from kiro_crew.agent_discovery import (
        SkillScopeResolutionError,
        _read_agent_spec,
        can_inherit_skills,
        expand_skill_uri,
        inherited_skill_globs,
        skill_resource_uris,
    )

    path = pinned_session_skill_spec(session_key)
    if path is None:
        return None
    data = _read_agent_spec(path, operation="agent_skill_globs", source="unknown")
    if data is None:
        if strict:
            raise SkillScopeResolutionError(f"Cannot read skill scope from {path}")
        return []
    globs = [
        g
        for uri in skill_resource_uris(data)
        if (g := expand_skill_uri(uri, path, project_dir=project_dir))
    ]
    if not globs and inherit_defaults and can_inherit_skills(data):
        return inherited_skill_globs(project_dir)
    return globs
