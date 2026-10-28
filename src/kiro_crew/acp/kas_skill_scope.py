"""Which skills a KAS session may load: Crew's skill scope as ``skill`` deny rules.

kiro-cli (v2) and KAS (v3) build a session's skill catalog differently. On kiro-cli
Crew's native launch turns ``chat.disableInheritingDefaultResources`` on, so the
session loads only the skills its spec maps, and Crew's ``skill_search`` covers the
scope :func:`kiro_crew.agent_discovery.session_skill_globs` resolves. KAS reads no
``cli.json``: it loads every skill under ``~/.kiro/skills`` and the workspace's
``.kiro/skills`` on top of the injected agent's ``resources`` (measured on kiro-cli
2.35.1: a spec mapping 3 skills ran with 45 on KAS and with 3 through Crew's v2 path).

The ``customAgents`` wire has no field that turns that off, but its ``permissions``
does reach the catalog: KAS drops every skill the policy denies under the ``skill``
capability, keyed on the skill's name, from the list its skill tool offers. So the
v2 scope is re-expressed as one deny rule: ``deny skill`` with the in-scope names as
``exclude``. Measured on the same spec, the rule took the session from 45 skills to
the spec's 3. A deny only narrows, so it needs no pass through the governance ceiling.

KAS ANDs ``exclude`` patterns onto every statement it compiles and does not split
them, so a long exclude list is the shape that overflows its policy compiler
(``kas_permissions._MAX_PATTERNS_PER_RULE``). Past that bound the rule is written the
other way round: one ``deny`` per out-of-scope name found in the trees KAS loads by
default, which splits like any ``match`` list. That form cannot see a skill added to
those trees after session start.

See ``docs/system-specs/modules/memory-skills-hooks.md`` (KAS skill scope).
"""

from __future__ import annotations

import glob
import logging
from pathlib import Path
from typing import Any

from kiro_crew.acp.kas_permissions import _MAX_PATTERN_LENGTH, _MAX_PATTERNS_PER_RULE

logger = logging.getLogger(__name__)

#: KAS's capability for a skill (``CAPABILITY_BY_TOOL["disclose_context"]``).
SKILL_CAPABILITY = "skill"

#: How many skill files one scope resolution reads, at most.
_MAX_SKILL_FILES = 2048

#: How much of a skill file is read for its frontmatter.
_FRONTMATTER_BYTES = 64 * 1024

#: Characters KAS's matcher reads as a pattern; a name carrying one cannot be an
#: exact ``exclude`` entry, so it is left out (left denied) rather than widened.
_PATTERN_CHARS = frozenset("*?[]{}")


def _skill_name(path: Path) -> str | None:
    """The name KAS gives the skill at *path*: its frontmatter ``name``, else its
    directory's name (``NodeProgressiveContextSource`` uses the directory as the
    fallback for a ``SKILL.md``)."""
    from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(_FRONTMATTER_BYTES)
    except OSError:
        return None
    name = (parse_frontmatter(head, SKILL_LOADER).get("name") or "").strip()
    if not name:
        name = path.parent.name if path.name == "SKILL.md" else path.stem
    return name or None


def skill_names(globs: list[str]) -> set[str]:
    """The names of the skill files *globs* match, bounded by :data:`_MAX_SKILL_FILES`."""
    names: set[str] = set()
    seen = 0
    for pattern in globs:
        for match in sorted(glob.glob(pattern, recursive=True)):
            path = Path(match)
            if not path.is_file():
                continue
            seen += 1
            if seen > _MAX_SKILL_FILES:
                logger.warning(
                    "KAS skill scope: more than %d skill files matched; the rest are left out",
                    _MAX_SKILL_FILES,
                )
                return names
            name = _skill_name(path)
            if name:
                names.add(name)
    return names


def _default_tree_globs(work_dir: str | Path | None) -> list[str]:
    """The skill trees KAS loads for every session, whatever the agent maps."""
    from kiro_crew.config import paths
    from kiro_crew.skills import project_skill_tree_present

    globs = [str(paths.kiro_home() / "skills" / "*" / "SKILL.md")]
    if work_dir and project_skill_tree_present(work_dir):
        globs.append(str(Path(work_dir) / ".kiro" / "skills" / "*" / "SKILL.md"))
    return globs


def _declared_globs(spec: dict[str, Any], spec_path: Path, work_dir: Any) -> list[str]:
    """The skill globs *spec* maps itself, for when Crew's scope resolution refuses."""
    from kiro_crew.agent_discovery import (
        can_inherit_skills,
        expand_skill_uri,
        inherited_skill_globs,
        skill_resource_uris,
    )

    globs = [
        g
        for uri in skill_resource_uris(spec)
        if (g := expand_skill_uri(uri, spec_path, project_dir=work_dir))
    ]
    if not globs and can_inherit_skills(spec):
        return inherited_skill_globs(work_dir)
    return globs


def scope_globs(
    agent: str,
    spec: dict[str, Any],
    *,
    agents_dir: Path,
    session_key: str,
    work_dir: str | Path | None,
) -> list[str] | None:
    """The skill globs this session loads on kiro-cli, or ``None`` when KAS needs no rule.

    ``None`` is the whole catalog (an unmapped ``kirocrew`` / ``kirocrew-worker``)
    or a scope that is exactly the default trees KAS loads by itself.

    The same call Crew's ``skill_search`` makes for a kiro-cli session (inherited
    trees count), so the KAS session matches its v2 twin rather than the narrower
    scope a non-kiro harness gets. A scope that cannot be resolved falls back to
    the skills *spec* maps itself.
    """
    from kiro_crew.agent_discovery import SkillScopeResolutionError, session_skill_globs
    from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP

    try:
        globs = session_skill_globs(
            session_key, agent, project_dir=work_dir, provider_type=PROVIDER_ACP
        )
    except SkillScopeResolutionError:
        logger.warning(
            "KAS skill scope for %r could not be resolved; using the spec's own skills",
            agent,
            exc_info=True,
        )
        globs = _declared_globs(spec, agents_dir / f"{agent}.json", work_dir)
    if globs is None or _inherits_default_trees(agent, spec, globs, work_dir):
        return None
    return globs


def _inherits_default_trees(
    agent: str, spec: dict[str, Any], globs: list[str], work_dir: str | Path | None
) -> bool:
    """Whether the scope IS the default trees, which KAS loads on its own.

    Asked on the trees, not on the glob list alone: with neither tree on disk an
    inheriting agent resolves to ``[]``, the same answer as an agent that may not
    inherit, and only the second is held to no skills.
    """
    from kiro_crew.agent_discovery import (
        WHOLE_CATALOG_TEMPLATES,
        can_inherit_skills,
        inherited_skill_globs,
        skill_resource_uris,
    )
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    if globs:
        return sorted(globs) == sorted(inherited_skill_globs(work_dir))
    return (
        agent not in WHOLE_CATALOG_TEMPLATES
        and not skill_resource_uris(spec)
        and can_inherit_skills(spec)
        and acp_driver.inherits_default_resources(work_dir)
    )


def skill_scope_rules(
    globs: list[str] | None, *, work_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """The ``permissions`` rules that hold a KAS session to the skills *globs* match.

    ``None`` (:func:`scope_globs`: a whole-catalog template, or a scope that is the
    default trees KAS already loads) adds nothing. An empty scope denies every skill.
    """
    if globs is None:
        return []
    names = sorted(
        n
        for n in skill_names(globs)
        if len(n) <= _MAX_PATTERN_LENGTH and not (_PATTERN_CHARS & set(n))
    )
    if not names:
        return [{"capability": SKILL_CAPABILITY, "effect": "deny"}]
    if len(names) <= _MAX_PATTERNS_PER_RULE:
        return [{"capability": SKILL_CAPABILITY, "exclude": names, "effect": "deny"}]
    in_scope = set(names)
    outside = sorted(
        n
        for n in skill_names(_default_tree_globs(work_dir))
        if n not in in_scope and len(n) <= _MAX_PATTERN_LENGTH
    )
    logger.info(
        "KAS skill scope maps %d skills, over the %d an exclude list may hold; "
        "denying the %d default-tree skills outside it by name instead",
        len(names),
        _MAX_PATTERNS_PER_RULE,
        len(outside),
    )
    step = _MAX_PATTERNS_PER_RULE
    return [
        {"capability": SKILL_CAPABILITY, "match": outside[i : i + step], "effect": "deny"}
        for i in range(0, len(outside), step)
    ]
