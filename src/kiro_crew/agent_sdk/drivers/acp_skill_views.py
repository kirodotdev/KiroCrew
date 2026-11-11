"""Skill-view provenance read from the view FILE, for callers outside the ACP tree.

:func:`kiro_crew.agent_sdk.drivers.acp.skill_view_source_agent` answers for a view
NAME. A caller about to treat a file on disk as Kiro Crew's own render needs the
stricter answer this module gives: the source only when the projection's
ownership record still binds the file as it is now.
"""

from __future__ import annotations

from pathlib import Path


def skill_view_owned_source(path: Path) -> str | None:
    """The agent the skill-view file at *path* was built from, or ``None``.

    ``None`` when no ownership record of this home binds the file's current
    bytes or view identity, so a spec someone else wrote at a view's name has no
    source. Which record answers is the projection module's rule. Blocking: it
    reads the file and one sidecar.
    """
    from kiro_crew.acp.skill_projection import owned_view_source

    return owned_view_source(path)
