"""A worker's conductor-chosen trigger skills (``session_create(skills=...)``).

A conductor that dispatches a worker may name the skills whose triggers fire in
that worker's session. The list does one thing: per-message trigger matching
(``context_assembly.turn_message``) runs for the worker over those keys only.
Nothing else is narrowed. ``skill_search``, ``$key``, file reads and the
session-start skill directory keep the worker's whole scope, and a session with
no list matches exactly as it did before.

Only the two worker templates take a list (:data:`TRIGGER_LIST_AGENTS`), so a
conductor session never gets one. A list never widens what a worker reaches.
The create route checks every key against the scope the child will have
(:func:`unknown_keys`), and each turn cuts the list to the session's own scope
again before it matches (:func:`in_session_scope`), because the list is kept on
the transcript's metadata line, a file the worker's own tools can write.
``skills.max_triggered`` still caps the match, so at its shipped default of 0
nothing triggers.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.agent_files import MATE_WORKER_AGENT_FILENAME, WORKER_AGENT_FILENAME
from kiro_crew.validation import MAX_TRIGGER_SKILL_KEY_LEN, MAX_TRIGGER_SKILLS

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

logger = logging.getLogger(__name__)

#: The templates a list applies to: the conductor-mode worker and a Bot's own worker.
TRIGGER_LIST_AGENTS = frozenset(
    {Path(WORKER_AGENT_FILENAME).stem, Path(MATE_WORKER_AGENT_FILENAME).stem}
)
#: The transcript metadata key the list is kept under.
META_KEY = "trigger_skills"


class TriggerSkillsRefused(ValueError):
    """A ``skills`` argument the create route refuses, with its refusal code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def parse_trigger_skills(raw: object) -> tuple[str, ...] | None:
    """*raw* as a list of keys, de-duplicated in order; ``None`` when absent.

    Raises :class:`TriggerSkillsRefused` for anything that is not a non-empty list
    of at most ``MAX_TRIGGER_SKILLS`` strings of at most ``MAX_TRIGGER_SKILL_KEY_LEN``
    characters (both in :mod:`kiro_crew.validation`). An empty list is refused:
    omitting ``skills`` is how a caller asks for the default matching.
    """
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(key, str) for key in raw):
        raise TriggerSkillsRefused("skills must be a list of skill keys", "invalid_field_type")
    keys = tuple(dict.fromkeys(raw))
    if not keys:
        raise TriggerSkillsRefused(
            "skills must name at least one skill key; omit it for the default matching",
            "invalid_skills",
        )
    longest = MAX_TRIGGER_SKILL_KEY_LEN
    if len(keys) > MAX_TRIGGER_SKILLS or any(len(key) > longest for key in keys):
        raise TriggerSkillsRefused(
            f"skills takes at most {MAX_TRIGGER_SKILLS} keys of at most {longest} "
            "characters each",
            "invalid_skills",
        )
    return keys


def restored_trigger_skills(value: object) -> tuple[str, ...] | None:
    """The list a metadata line carries, or ``None`` for none or a malformed one."""
    try:
        return parse_trigger_skills(value)
    except TriggerSkillsRefused:
        return None


def listed_for(agent: str | None, trigger_skills: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """The list a turn of *agent* matches over, or ``None`` when it has none.

    A session switched onto another template keeps the list on its line, and it
    stops applying: only :data:`TRIGGER_LIST_AGENTS` take one.
    """
    return trigger_skills if trigger_skills is not None and agent in TRIGGER_LIST_AGENTS else None


def scope_keys(
    skills: SkillsLoader, project_dir: str | Path | None, globs: list[str] | None
) -> set[str]:
    """The keys a scope of *globs* reaches (``None`` is the whole catalog).

    The same filter the trigger path applies to a mapping: a key whose mapped copy
    is not the copy that wins the key would inject the other copy's body.
    """
    return {
        row["key"]
        for row in skills.scoped_skills(project_dir=project_dir, only=globs)
        if not row.get("mapping_root")
    }


def unknown_keys(
    keys: Collection[str],
    skills: SkillsLoader,
    *,
    template: str,
    member_id: str | None,
    project_dir: str | Path | None,
    provider_type: str | None,
) -> list[str]:
    """The *keys* a new session of *template* on *member_id*'s record cannot load.

    Every key is unknown when that scope cannot be read. Raises
    :class:`TriggerSkillsRefused` while the scope's catalog is building, because
    a partial enumeration cannot classify any key. Blocking: reads specs and the
    skills tree.
    """
    # Read at call time from the facade, which is the patch surface tests use.
    from kiro_crew.agent_discovery import child_skill_globs

    try:
        globs = child_skill_globs(
            template, member_id, project_dir=project_dir, provider_type=provider_type
        )
    except Exception:  # noqa: BLE001 -- an unreadable scope admits no key
        logger.info("trigger skills: scope of %s unreadable", template, exc_info=True)
        return list(keys)
    known = scope_keys(skills, project_dir, globs)
    if skills.catalog_status(project_dir) == "building":
        raise TriggerSkillsRefused(
            "the skill catalog is still loading. Retry shortly. Nothing was created",
            "skills_catalog_building",
        )
    return [key for key in keys if key not in known]


def in_session_scope(
    skills: SkillsLoader,
    keys: tuple[str, ...],
    *,
    session_key: str,
    agent: str,
    project_dir: str | Path | None,
    scope_root: str | Path | None,
    provider_type: str | None,
) -> tuple[str, ...]:
    """*keys* cut to what *session_key*'s own scope reaches.

    The reader ``skill_search`` and ``$key`` use (``session_skill_globs``), so a
    list edited on disk cannot reach past them. Fails closed: an unreadable scope
    keeps nothing. Blocking.
    """
    # Read at call time from the facade, which is the patch surface tests use.
    from kiro_crew.agent_discovery import session_skill_globs

    try:
        globs = session_skill_globs(
            session_key, agent, project_dir=scope_root, provider_type=provider_type
        )
    except Exception:  # noqa: BLE001 -- an unreadable scope triggers nothing
        logger.info("trigger skills: scope of %s unreadable", session_key, exc_info=True)
        return ()
    if globs is None:
        return keys
    known = scope_keys(skills, project_dir, globs)
    return tuple(key for key in keys if key in known)
