"""What a session's context includes: context-group scope, skills and steering.

A spawning parent narrows the switchable context groups for its sub-agent, the
agent's own spec can drop the memory and lessons groups (:func:`_spec_scoped_groups`)
and the operator's toggles narrow them further; :func:`_build_context_scope_section`
names what a parent withheld. :func:`_render_folder_steering_section` is the one folder
steering renderer, :func:`skill_parts` the one loader call, and
:func:`steering_resources_part` the one steering-resources gate for a harness that
reads no agent ``resources`` (Claude Code, Codex, OpenCode and Pi), that session start and
post-compaction re-injection share.

Three inclusion rules stay on :mod:`kiro_crew.context`: ``_skills_injection_plan``
(a doc pins its return expression there), ``_config_scoped_groups`` (callers rebind
it) and ``_project_steering_delivered`` (the provider-identity guard scans that
file). The steering loader lives in
:mod:`kiro_crew.context_assembly.agent_spec`, beside the agent-spec read it shares
with the agent prompt.

New inclusion rules -- which group, skill or steering source reaches a session --
belong here.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.context_assembly import agent_spec as _agent_spec
from kiro_crew.context_assembly import budget as _budgets

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder

# ── Switchable context groups ──
#
# A spawning parent decides which of these groups its sub-agent inherits (the
# ``include_memory`` / ``include_lessons`` / ``include_project`` flags on
# spawn_run). ``None`` means every group and is what every other caller passes,
# so the dashboard / Slack / cron / eval paths are unaffected.
#
# The unlisted fourth group is conduct — critical rules, date, agent identity,
# runtime, UI language, workspace identity, skills index. It is not switchable:
# every member is an output contract or a capability pointer, so a sub-agent
# without it cannot discover what it can do or format what it reports back.
CONTEXT_GROUP_MEMORY = "memory"
CONTEXT_GROUP_LESSONS = "lessons"
CONTEXT_GROUP_PROJECT = "project"
SWITCHABLE_CONTEXT_GROUPS = (
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_PROJECT,
)

_GROUP_DESCRIPTIONS = {
    CONTEXT_GROUP_MEMORY: "memory (user preferences, projects, prior sessions)",
    CONTEXT_GROUP_LESSONS: "lessons (learned corrections, user profile)",
    CONTEXT_GROUP_PROJECT: "project (docs pointer, steering files, project directory)",
}


def _group_included(groups: frozenset[str] | None, group: str) -> bool:
    """True when *group* is in scope; ``None`` ⇒ every group."""
    return groups is None or group in groups


def _build_context_scope_section(groups: frozenset[str] | None) -> str:
    """Name the groups a parent withheld, or ``""`` when nothing was withheld.

    A sub-agent that silently lacks a group guesses at what it cannot see —
    inventing user preferences is the specific failure. Naming the gap converts
    that into an honest "not provided", which is what makes an aggressive
    opt-out cheap to recover from.
    """
    if groups is None:
        return ""
    missing = [g for g in SWITCHABLE_CONTEXT_GROUPS if g not in groups]
    if not missing:
        return ""
    return (
        "[CONTEXT SCOPE] Your parent withheld: "
        + "; ".join(_GROUP_DESCRIPTIONS[g] for g in missing)
        + ".\nIf the task needs any of it, say it was not provided and ask the "
        "parent — do not guess.\n[End of context scope]\n\n"
    )


def _agent_includes_cross_session_context(
    agent: str | None, project: str | None = None, session_key: str | None = None
) -> bool:
    """Whether this session of *agent* gets the memory and lessons kept from other sessions.

    ``False`` only for a custom agent whose effective spec sets
    ``includeCrossSessionContext`` to anything but ``true``, or cannot be read
    (:func:`_agent_spec._read_include_cross_session_context`). *project* is where
    the session's agent template is looked up ahead of the global directories, and
    *session_key* finds the file a pinned session runs from. No agent and a
    built-in agent always answer ``True``: its spec is generated, so it has no
    author to opt out.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.agent_files import is_kirocrew_default_agent

    if not agent or is_kirocrew_default_agent(agent):
        return True
    return _agent_spec._read_include_cross_session_context(
        agent, project, spec_path=ctx.pinned_session_skill_spec(session_key)
    )


def _spec_scoped_groups(
    keeps_stored: bool, context_groups: frozenset[str] | None
) -> frozenset[str] | None:
    """The caller-passed scope, minus the memory and lessons groups unless *keeps_stored*.

    *keeps_stored* is :func:`_agent_includes_cross_session_context`'s answer for
    the session. An agent that sets ``includeCrossSessionContext: false`` loses
    the two groups that carry content from the user's other sessions: stored
    memory, the activity index, recent-session snippets, a memory provider's
    blocks, learned corrections and experience, per-message lessons and the user
    profile. The result feeds the same group gates the operator's
    ``memory.inject_memory`` / ``memory.inject_lessons`` toggles feed
    (``_config_scoped_groups``), so the spec key is those two toggles for one agent.
    It can only remove groups, never add one back, and it leaves the project group
    and this conversation's own history alone. On-demand recall tools are not
    context injection and are not touched.

    Callers keep the caller-passed value for the ``[CONTEXT SCOPE]`` block, whose
    "your parent withheld" text is about a spawn's choice, not the agent's own.
    """
    if keeps_stored:
        return context_groups
    base = SWITCHABLE_CONTEXT_GROUPS if context_groups is None else context_groups
    return frozenset(base) - {CONTEXT_GROUP_MEMORY, CONTEXT_GROUP_LESSONS}


def _render_folder_steering_section(
    steering_dirs: tuple[str, ...],
    project: str | None,
    cap: int,
    *,
    skip_delivered_roots: bool = True,
    delivered_sources: Collection[str] = (),
) -> str:
    """The folder-steering prompt section, capped like the steering section.

    One helper for the fresh-session path and the post-compaction reinjection
    path so the two cannot drift in what they skip as already delivered or how
    they truncate. The caller takes the provider-delivery verdict once:
    :func:`_project_steering_delivered` says whether the provider delivers the
    project and global ``.kiro/steering`` trees wholesale (then they are
    skipped by root). A kiro-cli chat whose workspace opts out gets them
    through the folder instead, minus *delivered_sources*: the paths of the
    guides the serving process's launch record names, which kiro-cli loads
    itself and re-reads for every request, so the collector skips each by path
    and an edited guide reaches the model through kiro-cli. If no provider or
    launch record exists, the caller supplies no delivered sources and the
    folder carries the trees whole, preferring a duplicate over dropping a
    document.

    The reader itself lives in :mod:`kiro_crew.folder_steering`; ``cap`` is
    ``caps.steering`` ALWAYS, not only under ``skills.lazy_load``: the section
    is appended as required (protected from budget trims), so without its own
    finite bound an operator-pointed tree of up to 64 x 256 KB would be handed
    to the model whole and reject every turn. The bound is applied BY the
    renderer, which reserves the omission notices and footer before spending
    the budget on bodies; a bare slice would cut off exactly the lines that
    say the section is incomplete.

    The bodies and labels are scrubbed with :func:`_neutralize_structural_markers`
    INSIDE the renderer, before the genuine ``[FOLDER STEERING -- ...]`` frame
    is minted around them; the frame itself is in ``_STRUCTURAL_MARKER_RES``,
    so the returned section must be appended AFTER any scrub of the
    surrounding text, never inside the scrubbed session-context tail. Both
    callers do exactly that.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    return ctx.render_folder_steering(
        ctx.collect_folder_steering(
            steering_dirs,
            project=project,
            skip_delivered_roots=skip_delivered_roots,
            delivered_sources=delivered_sources,
        ),
        max_chars=cap,
        scrub=ctx._neutralize_structural_markers,
    )


def _folder_steering_delivery_snapshot(
    *,
    provider_type: str | None,
    native_steering: bool,
    project: str | None,
    native_launch_sources: Collection[str],
    agent: str | None,
) -> tuple[bool, Collection[str]]:
    """Resolve root dedup and the launch record the folder skips by path.

    ``native_launch_sources`` names the documents in the serving process's
    launch record, which kiro-cli loads and re-reads for every request.
    ``agent`` is the chat's agent, as :func:`steering_resources_part` receives
    it: Crew's steering load serves only the default agent, so the folder skips
    the trees for that load only when it runs.
    ``provider_type`` is the harness as the caller named it. ``None`` is not
    kiro-cli: unless the provider reports ``native_steering``, nothing is
    skipped as delivered and no launch record applies, so the folder carries
    the trees whole (a duplicate at worst, never a loss).
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    skip_delivered_roots = ctx._project_steering_delivered(
        provider_type, native_steering, project, agent=agent
    )
    if skip_delivered_roots or not ctx.label_inherits_kiro_defaults(provider_type):
        return skip_delivered_roots, ()
    return skip_delivered_roots, native_launch_sources


def skill_parts(
    builder: ContextBuilder,
    *,
    globs: list[str],
    project: str | Path | None,
    caps: _budgets._ResolvedCaps,
    lazy_skills: bool,
) -> tuple[list[str], str]:
    """``(required skill bodies, bounded discovery block)`` for one injection.

    Session start and post-compaction re-injection both call this, so the two
    cannot drift in budget, scoping or the discovery-only choice: a mapped agent
    receives only its mapped set, and a pinned body is never sliced.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    required_skills: list[str] = []
    skills_ctx = builder.skills.get_context(
        budget=caps.skills,
        only=globs or None,
        project_dir=project,
        project_body_budget=ctx._PINNED_PROJECT_BODY_CAP,
        discovery_only=not lazy_skills and not globs,
        required_parts_out=required_skills,
    )
    return required_skills, skills_ctx


def steering_resources_part(
    *,
    agent: str | None,
    provider_type: str | None,
    essentials: str,
    context_groups: frozenset[str] | None,
    project: str | None = None,
    steering_off: Collection[str] = frozenset(),
) -> str:
    """The agent config's ``file://`` steering text for this session, or ``""``.

    Session start frames it as ``[Steering resources]``; post-compaction
    re-injection frames it as a ``[REINJECTED AFTER COMPACTION ...]`` block. One
    gate for both, so the two cannot drift in who receives it.

    kiro-cli loads an agent's ``resources`` natively when spawned with
    ``--agent`` (see acp/client_start.py ``_spawn``) and keeps them across its own
    compaction, so only a harness that does NOT read agent ``resources`` needs the
    explicit load: *provider_type*, the session's provider label, names one in
    ``ACP_BACKENDS_CREW_LOADS_SPEC_STEERING`` (Claude Code, Codex, OpenCode and Pi,
    a positive membership). A custom agent loads its own prompt, a member chat (*essentials*
    non-empty) carries its steering inside the essentials envelope, and the
    ``project`` context group decides whether steering is in scope at all.
    *project* is the session's project: relative entries anchor there, as
    kiro-cli anchors them at its working directory, and it adds the project's
    steering and ``AGENTS.md`` that kiro-cli inherits. A harness that reads that
    ``AGENTS.md`` itself from its working directory (Codex, OpenCode, Pi) does not get it from
    this load. *steering_off* holds the resolved paths this chat switched off;
    only a harness whose capability record says it honours the switch
    (``SessionCapabilities.honors_chat_steering_off``, Claude Code today) leaves
    them out, and a chat with none takes exactly the old call.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.agent_files import is_kirocrew_default_agent
    from kiro_crew.steering_off import honoured_for as steering_off_honoured

    if essentials or not ctx.acp_driver.crew_loads_spec_steering(provider_type):
        return ""
    if agent and not is_kirocrew_default_agent(agent):
        return ""
    if not _group_included(context_groups, CONTEXT_GROUP_PROJECT):
        return ""
    reads_agents_md = ctx.acp_driver.reads_project_agents_md(provider_type)
    if steering_off and steering_off_honoured(provider_type):
        return ctx._load_steering_resources(
            project, steering_off, harness_reads_agents_md=reads_agents_md
        )
    return ctx._load_steering_resources(project, harness_reads_agents_md=reads_agents_md)
