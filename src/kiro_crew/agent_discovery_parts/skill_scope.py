"""Which skill trees an agent, and a session's agent, may load.

Why a spec withholds Crew's ``skill_search``, whether an agent inherits the default skill
trees, the inherited globs, the resolved globs per agent and per session (with the error a
strict resolution raises), and the label of the harness a live provider serves, which
decides whether inherited trees count.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

from kiro_crew.agent_discovery_parts.agent_roster import list_agents
from kiro_crew.agent_discovery_parts.spec_fields import (
    _SKILL_SEARCH_TOOL,
    SCOPE_PROJECT,
    expand_skill_uri,
    skill_resource_uris,
)
from kiro_crew.agent_discovery_parts.spec_reads import _kiro_agents_dir, _read_agent_spec
from kiro_crew.agent_discovery_parts.spec_snapshot import parsed_agent_specs
from kiro_crew.config.paths import project_agents_dir

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.agent_discovery")


class SkillScopeResolutionError(ValueError):
    """The bound agent has an unavailable skill scope."""


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
SkillScopeResolutionError.__module__ = "kiro_crew.agent_discovery"

#: The templates that see the whole catalog while they map no skill. The worker
#: maps exactly the default's ``skill://`` entries (``_mirror_worker_skill_resources``),
#: so an unmapped worker has an unmapped dispatcher and sees what it sees.
WHOLE_CATALOG_TEMPLATES = frozenset({"kirocrew", "kirocrew-worker"})


def skill_search_refusal(spec: dict[str, Any]) -> str | None:
    """Why *spec* withholds Crew's ``skill_search``, or ``None`` when it allows it.

    THE one spelling of the check. The skill projection refuses a mapped agent's
    view with this reason, and :func:`can_inherit_skills` reads it to decide
    whether an unmapped agent can take its inherited skill trees.
    """
    excluded = spec.get("excludedTools", [])
    if isinstance(excluded, list) and any(
        isinstance(t, str) and (t == "@kirocrew-core" or fnmatch.fnmatchcase(_SKILL_SEARCH_TOOL, t))
        for t in excluded
    ):
        return "skill_search is explicitly excluded; bounded skill discovery requires it"
    servers = spec.get("mcpServers", {})
    if not isinstance(servers, dict):
        return "mcpServers must be an object"
    core = servers.get("kirocrew-core", {})
    if not isinstance(core, dict):
        return "kirocrew-core must be a server object"
    disabled = core.get("disabled", False)
    disabled_tools = core.get("disabledTools", [])
    if not isinstance(disabled, bool):
        return "kirocrew-core.disabled must be a boolean"
    if not isinstance(disabled_tools, list) or any(
        not isinstance(tool, str) for tool in disabled_tools
    ):
        return "kirocrew-core.disabledTools must be a list of strings"
    if disabled or "skill_search" in disabled_tools:
        return "skill_search is disabled; bounded skill discovery requires it"
    return None


def can_inherit_skills(spec: dict[str, Any]) -> bool:
    """Whether an unmapped custom agent's spec can take its inherited skill trees.

    Crew delivers inherited skills through ``skill_search``, so a spec that
    declares ``tools: []`` (the zero-tool contract) or withholds that tool
    (:func:`skill_search_refusal`) gets none, and so does one with no
    ``kirocrew-core`` entry whose tools are missing or ``"*"``: wiring search
    there would grant every Crew tool. Each would change what its author allowed;
    such an agent keeps the spawn it had and simply gets no skills.
    """
    tools = spec.get("tools")
    if isinstance(tools, list) and not tools:
        return False
    if skill_search_refusal(spec) is not None:
        return False
    servers = spec.get("mcpServers", {})
    if isinstance(servers, dict) and "kirocrew-core" in servers:
        return True
    # Without its own kirocrew-core entry the view gains one. Only an explicit
    # tool list keeps that to skill_search: a missing list or a "*" would hand
    # the agent every kirocrew-core tool its author never asked for.
    return isinstance(tools, list) and "*" not in tools


def inherited_skill_globs(
    project_dir: str | Path | None, *, kiro_home_dir: Path | None = None
) -> list[str]:
    """The skills a custom agent inherits from kiro-cli's default resources.

    Since kiro-cli 2.7 a custom agent inherits the default skill trees,
    ``~/.kiro/skills`` and the workspace's ``.kiro/skills``, unless
    ``chat.disableInheritingDefaultResources`` turns that off. Native launches
    turn it off to bound the startup catalog (``acp/skill_projection_parts/preparation.py``),
    so an agent with no ``skill://`` entry of its own would get no skills at all. Crew
    supplies those trees instead, through the same bounded directory and
    ``skill_search`` a mapped agent uses. Returns ``[]`` when the workspace opted
    out of inheritance. *kiro_home_dir* is the kiro-cli home the caller already
    resolved; it defaults to :func:`kiro_crew.config.paths.kiro_home`.

    A tree that does not exist is left out. The workspace tree is
    workspace-controlled (a link to a network share, for one), so it is asked
    through :func:`kiro_crew.skills.project_skill_tree_present`: trust first,
    then the no-follow traversal, never a path-following probe.
    """
    from kiro_crew.agent_sdk.drivers import acp as acp_driver
    from kiro_crew.config import paths

    if not acp_driver.inherits_default_resources(project_dir):
        return []
    home = kiro_home_dir if kiro_home_dir is not None else paths.kiro_home()
    global_tree = home / "skills"
    globs = [str(global_tree / "*" / "SKILL.md")] if global_tree.is_dir() else []
    from kiro_crew.skills import project_skill_tree_present

    if project_dir and project_skill_tree_present(project_dir):
        globs.append(str(Path(project_dir) / ".kiro" / "skills" / "*" / "SKILL.md"))
    return globs


def agent_skill_globs(
    agent: str,
    agents_dir: Path | None = None,
    *,
    project_dir: str | Path | None = None,
    strict: bool = False,
    inherit_defaults: bool = False,
    session_key: str = "",
) -> list[str]:
    """Return fnmatch globs for the skills mapped to *agent*, or ``[]``.

    An empty list means "this agent has no explicit skill mapping". Session
    discovery distinguishes the default catalog from an empty custom scope.
    Listing callers are best-effort: an unreadable, invalid, or sensitive-path
    agent file yields ``[]``. Session callers use ``strict=True`` so a missing
    custom template cannot silently widen its scope to the global catalog.
    Session callers also pass ``inherit_defaults=True``: a custom agent whose
    spec resolves with no ``skill://`` entry then gets
    :func:`inherited_skill_globs` rather than ``[]``.
    Resolved from the :func:`parsed_agent_specs` snapshot when no project is supplied.
    The crewmate conductor and worker resolve from the crewmate folder; *session_key*
    names the worker's crewmate (``mate_worker.mate_service_scope_spec``).
    """
    if not agent:
        return []
    from kiro_crew.mate_worker import mate_service_scope_spec

    mate = mate_service_scope_spec(agent, session_key) if agents_dir is None else None
    if mate is not None:
        data, path = mate
        if data is None:
            if strict:
                raise SkillScopeResolutionError(f"Cannot read skill scope for agent {agent!r}")
            return []
        globs = [
            g
            for uri in skill_resource_uris(data)
            if (g := expand_skill_uri(uri, path, project_dir=project_dir))
        ]
        if not globs and inherit_defaults and can_inherit_skills(data):
            return inherited_skill_globs(project_dir)
        return globs
    if project_dir:
        rows = list_agents(agents_dir=agents_dir, project_dir=str(project_dir))
        winner = next((row for row in rows if row.name == agent), None)
        if winner is None or not winner.filename:
            if strict and agent not in WHOLE_CATALOG_TEMPLATES:
                raise SkillScopeResolutionError(f"Cannot resolve skill scope for agent {agent!r}")
            return []
        if winner.scope == SCOPE_PROJECT:
            directory = project_agents_dir(str(project_dir))
        elif agents_dir is not None:
            directory = agents_dir
        else:
            directory = _kiro_agents_dir()
        path = directory / winner.filename
        data = _read_agent_spec(path, operation="agent_skill_globs", source="unknown")
        if strict and data is None:
            raise SkillScopeResolutionError(f"Cannot read skill scope for agent {agent!r}")
        globs = [
            g
            for uri in skill_resource_uris(data or {})
            if (g := expand_skill_uri(uri, path, project_dir=project_dir))
        ]
        if (
            not globs
            and inherit_defaults
            and agent not in WHOLE_CATALOG_TEMPLATES
            and data is not None
            and can_inherit_skills(data)
        ):
            return inherited_skill_globs(project_dir)
        return globs
    # ``f`` is the ORIGINAL path: ``f.stem`` and ``expand_skill_uri`` below
    # must see it so a symlinked spec's relative globs stay anchored where
    # the symlink lives.
    for data, f in parsed_agent_specs(agents_dir, operation="agent_skill_globs", source="unknown"):
        if data.get("name") != agent and f.stem != agent:
            continue
        globs = [g for uri in skill_resource_uris(data) if (g := expand_skill_uri(uri, f))]
        if (
            not globs
            and inherit_defaults
            and agent not in WHOLE_CATALOG_TEMPLATES
            and can_inherit_skills(data)
        ):
            return inherited_skill_globs(project_dir)
        return globs
    if strict and agent not in WHOLE_CATALOG_TEMPLATES:
        raise SkillScopeResolutionError(f"Cannot resolve skill scope for agent {agent!r}")
    return []


def served_provider_label(provider: object) -> str | None:
    """The label of the harness a live *provider* serves, or ``None`` without one.

    The same value ``context._skills_injection_plan`` is handed for a session
    (``context_provider_type``): ``"acp"`` for kiro-cli, ``"kas"``, ``"claude_code"``,
    ``"codex"`` and so on. ``None`` (no live session) is not kiro-cli.
    """
    from kiro_crew.agent_sdk import context_provider_of

    context_provider = context_provider_of(provider)
    if context_provider is None:
        return None
    try:
        return context_provider.context_provider_type
    except Exception:  # noqa: BLE001 -- an unreadable label is "unknown", not kiro-cli
        logger.debug("served_provider_label: label unreadable", exc_info=True)
        return None


# This is called on the discovery/expansion worker, never on the event loop.
def session_skill_globs(
    session_key: str,
    fallback_agent: str,
    *,
    project_dir: str | Path | None = None,
    provider_type: str | None = None,
) -> list[str] | None:
    """Use a member's bound template, not its dashboard display alias.

    An unmapped custom agent's inherited skill trees count only when
    *provider_type* names kiro-cli, the same rule as
    ``context._skills_injection_plan``. Callers pass the served harness
    (:func:`served_provider_label`); ``None`` grants no inherited trees.
    """
    from kiro_crew.execution_context import read_session_execution

    execution = read_session_execution(session_key) if session_key else None
    template = execution.template_id if execution and execution.template_id else fallback_agent
    from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP
    from kiro_crew.skill_scope_pin import pinned_skill_globs

    inherit = provider_type == PROVIDER_ACP
    pinned = pinned_skill_globs(
        session_key, project_dir=project_dir, strict=True, inherit_defaults=inherit
    )
    mapped = (
        pinned
        if pinned is not None
        else agent_skill_globs(
            template,
            project_dir=project_dir,
            strict=True,
            inherit_defaults=inherit,
            session_key=session_key,
        )
    )
    return None if template in WHOLE_CATALOG_TEMPLATES and not mapped else mapped
