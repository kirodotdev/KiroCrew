"""The ``skill://`` mapping a generated agent's own prompt requires.

A built-in agent whose prompt says "read the ``goal-conductor`` skill first" needs
that skill in its ``resources``, because a mapping is the ONLY thing that puts a
skill in reach of a generated spec. Under the scoping rule, an agent other than the
default ``kirocrew`` template sees exactly its own ``skill://`` entries: with none,
``session_skill_globs`` answers ``[]`` and the catalog answers nothing -- search,
the paginated list, an exact read and a ``$skill`` token alike -- and the startup
skills directory stays off, so the absolute path the skill's own procedure asks the
agent to work from is nowhere it can read.

That rule is deliberate and stays. The mapping is what this module owns, derived
from :func:`skills_dir` at install time rather than spelled as a literal, because
every generated spec is rewritten on each boot and ``KIROCREW_HOME`` decides where
the skills tree lives. A hardcoded ``~/.kiro/crew/skills`` maps nothing on a
relocated home.

Each installer declares the keys ITS OWN prompt names, and nothing more: a mapping
is also the agent's search scope, so a key no prompt names is surface the charter
cannot account for.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from kiro_crew.agent_discovery import skill_uri_for_path


def prompt_skill_uris(*keys: str) -> list[str]:
    """``skill://`` URIs for the catalog *keys*, in order, de-duplicated.

    A key is a catalog key as :mod:`kiro_crew.skills` reports it, so a nested
    builtin carries its family directory in the key itself
    (``kirocrew-dev/dashboard-template``). A key is slash-separated on every
    platform, which is what ``PurePosixPath`` says and an OS-flavoured split would
    not: on Windows a backslash in a key is a NAME character, not a separator.
    Rendered by :func:`agent_discovery.skill_uri_for_path`, the same renderer the
    Agent Templates editor writes through.

    The path is NOT checked for existence. The skills tree is installed by the same
    rebuild that writes these specs, and the two orders are not guaranteed against
    each other, so a probe here would drop a mapping on a cold first boot and leave
    it dropped until the next one -- whereas a URI whose file is not there yet
    simply matches nothing until it is.
    """
    from kiro_crew.skills import skills_dir  # noqa: PLC0415 -- cycle: skills imports agent

    base = skills_dir()
    return list(
        dict.fromkeys(
            skill_uri_for_path(base.joinpath(*PurePosixPath(key).parts) / "SKILL.md")
            for key in keys
        )
    )


def map_prompt_skills(config: dict[str, Any], *keys: str) -> None:
    """Add the *keys* mapping to *config*'s ``resources``, in place, by MERGE.

    Appends only what is missing and moves nothing: an entry already in
    ``resources`` keeps its index, whether it is a shipped ``file://`` steering
    glob or a mapping a person saved in Capabilities. The installers that carry a
    user's saved ``resources`` forward (``_install_dashboard_author_agent``) call
    this after that carry-over, so the person's selection is what this merges into.

    So a person who removes this skill in Capabilities gets it back on the next
    boot. That is deliberate and it is the narrow cost of the fix: the prompt this
    installer writes in the same breath cannot be followed without the skill, so a
    spec carrying one and not the other is not a configuration -- it is an agent
    told to read a document it has no way to open. Removing a skill the prompt does
    NOT name still holds, because this adds only what that prompt names.
    """
    existing = config.get("resources")
    entries: list[Any] = list(existing) if isinstance(existing, list) else []
    present = {e for e in entries if isinstance(e, str)}
    entries.extend(uri for uri in prompt_skill_uris(*keys) if uri not in present)
    config["resources"] = entries
