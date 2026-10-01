"""What a session's context includes: context-group scope, skills and steering.

A spawning parent narrows the switchable context groups for its sub-agent and the
operator's toggles narrow them further; :func:`_build_context_scope_section` names
what a parent withheld. :func:`_render_folder_steering_section` is the one folder
steering renderer, and :func:`skill_parts` the one loader call, that session start
and post-compaction re-injection share.

Four inclusion rules stay on :mod:`kiro_crew.context`: ``_skills_injection_plan``
(a doc pins its return expression there), ``_config_scoped_groups`` (callers rebind
it), ``_project_steering_delivered`` (the provider-identity guard scans that file)
and the CC-backend steering loader, beside the agent-spec read it shares with the
agent prompt.

New inclusion rules -- which group, skill or steering source reaches a session --
belong here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

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


def _render_folder_steering_section(
    steering_dirs: tuple[str, ...],
    project: str | None,
    cap: int,
    *,
    skip_delivered_roots: bool = True,
) -> str:
    """The folder-steering prompt section, capped like the steering section.

    One helper for the fresh-session path and the post-compaction reinjection
    path so the two cannot drift in what they read or how they truncate. The
    reader itself lives in :mod:`kiro_crew.folder_steering`; ``cap`` is
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
            steering_dirs, project=project, skip_delivered_roots=skip_delivered_roots
        ),
        max_chars=cap,
        scrub=ctx._neutralize_structural_markers,
    )


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
