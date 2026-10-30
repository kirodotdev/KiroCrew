"""Which skills a KAS session may load: Crew's skill scope as ``skill`` deny rules.

kiro-cli (v2) and KAS (v3) build a session's skill catalog differently. On kiro-cli
Crew's native launch turns ``chat.disableInheritingDefaultResources`` on, so the
session loads only the skills its spec maps (or the inherited trees, served by Crew's
``skill_search``). KAS reads no
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
them; its compiler records that a condition tree past roughly 114 patterns overflows
cedar-wasm's stack, and Crew bounds every rule it sends at
``kas_permissions._MAX_PATTERNS_PER_RULE`` (64) for that reason (measured live here
up to 35). Past that bound the rule is written the other way round: one ``deny`` per
out-of-scope name found in the trees KAS loads by default, which splits like any
``match`` list. That form cannot see a skill added to those trees after session
start, and a name with glob characters cannot be denied literally, so such a skill
stays offered.

The scope is read from the spec the session is projected with (``skill://``
entries, else the inherited trees for a spec that may inherit them) -- the rules
``agent_discovery.agent_skill_globs`` applies on kiro-cli. Skill files are read
through ``hooks.safe_read_prefix``.

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
    from kiro_crew.hooks import safe_read_prefix

    # A ``skill://`` path comes from a spec, which may point anywhere: the read
    # goes through the sensitive-path fence and refuses a credential store.
    raw = safe_read_prefix(str(path), _FRONTMATTER_BYTES)
    if raw is None:
        return None
    head = raw.decode("utf-8", errors="replace")
    name = (parse_frontmatter(head, SKILL_LOADER).get("name") or "").strip()
    if not name:
        name = path.parent.name if path.name == "SKILL.md" else path.stem
    return name or None


def _literal(name: str) -> bool:
    """Whether *name* can be a policy pattern that matches only itself."""
    return len(name) <= _MAX_PATTERN_LENGTH and not (_PATTERN_CHARS & set(name))


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


def _spec_globs(
    agent: str, spec: dict[str, Any], spec_path: Path, work_dir: Any
) -> list[str] | None:
    """The skill globs *spec* resolves to under ``agent_skill_globs``' rules, or
    ``None`` for an unmapped whole-catalog template.

    Its ``skill://`` entries, else the inherited trees for a spec that may inherit
    them (kiro-cli's rule, so inherited trees count).
    """
    from kiro_crew.agent_discovery import (
        WHOLE_CATALOG_TEMPLATES,
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
    if globs:
        return globs
    if agent in WHOLE_CATALOG_TEMPLATES:
        return None
    return inherited_skill_globs(work_dir) if can_inherit_skills(spec) else []


def scope_globs(
    agent: str,
    spec: dict[str, Any],
    *,
    agents_dir: Path,
    work_dir: str | Path | None,
) -> list[str] | None:
    """The skill globs this session loads on kiro-cli, or ``None`` when KAS needs no rule.

    ``None`` is the whole catalog (an unmapped ``kirocrew`` / ``kirocrew-worker``)
    or a scope that is exactly the default trees KAS loads by itself.

    The scope is read from *spec*, the spec this session is projected with, and
    from nothing else. Never re-resolved by name: a checkout spec that shadows a
    user-level name is not the one KAS runs (``harness/kas.resolve_projected_spec``).
    Never from the session key's skill-scope pin (``skill_scope_pin``) either: the
    gateway sets that pin per turn, after the session is built, so a session
    started for a turn that switched away from an app agent would still read the
    app's file. An app-bound session's projected spec IS its registered file
    (``apps.agent_launch.read_bound_app_spec``), and a crewmate worker's is already
    its merged session spec.
    """
    globs = _spec_globs(agent, spec, agents_dir / f"{agent}.json", work_dir)
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
    names = sorted(n for n in skill_names(globs) if _literal(n))
    if not names:
        return [{"capability": SKILL_CAPABILITY, "effect": "deny"}]
    if len(names) <= _MAX_PATTERNS_PER_RULE:
        return [{"capability": SKILL_CAPABILITY, "exclude": names, "effect": "deny"}]
    in_scope = set(names)
    # Only literal names become ``match`` entries: KAS reads them as globs, so a
    # name like ``*`` would deny the in-scope skills too. Such a skill stays offered.
    outside = sorted(
        n for n in skill_names(_default_tree_globs(work_dir)) if n not in in_scope and _literal(n)
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
