"""Member gates: which member layers a turn receives, and the builder's way into them.

The desk predicates decide whether a member section carries the member DESK (``[HOW
YOU WORK]`` and the ``[CURRENT ASSIGNMENT]`` briefing) or identity and permanent rules
only: :func:`_desk_withheld` and :func:`_template_selected_on_member_store`.
:func:`_member_dispatch_held` decides whether the member operating-mode block may teach
the ``session_*`` dispatch tools, from the live session's own mount when it reported one.
The capability check behind it, ``_member_backend_can_dispatch``, stays on
:mod:`kiro_crew.context` because callers rebind it there.

``ContextBuilder._build_member_section`` and ``_build_v2_essentials`` are the builder's
two entry points into :mod:`kiro_crew.context_assembly.member`. They are bound on the
class, so a patch of either method still applies to every caller; neither reads builder
state (``_build_v2_essentials`` hands the builder on).

New member gates belong here; new member layers belong in ``member``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.context_assembly import member as _member

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig


def _member_dispatch_held(
    mounted: bool | None, cfg: "KiroCrewConfig | None" = None, crew: str = ""
) -> bool:
    """Whether this member session actually holds the ``session_*`` tools.

    *mounted* is the live session's own answer (``member_dispatch_mounted`` on
    the context provider), recorded by the composer that built its MCP array. It
    wins whenever it exists: the configured backend can be dispatch-capable while
    THIS session withheld the mount (the dashboard server switched off, a
    per-tool restriction on a withhold-only backend, a permission surface Crew
    does not own, an unresolved entry), and teaching the block then would send
    the member after tools it never received. ``None`` means no session evidence
    reached the build, and only then does the configured capability decide,
    for crewmate *crew*'s own harness when it pins one.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    if mounted is not None:
        return mounted
    return ctx._member_backend_can_dispatch(cfg, crew)


def _template_selected_on_member_store(execution_context: Any) -> bool:
    """Whether *execution_context* runs a member's store under a selected TEMPLATE.

    One of the two reasons :func:`_desk_withheld` gives for delivering a member
    section without its desk layers. A member's memory identity and its
    persona are two fields of one record: ``member_id`` (bound to the store) says
    whose memory this is, ``selection_kind`` says what was picked to run it. A
    template picked on a member's store — a ``session_create(agent=...)`` child
    or a ``spawn_run(agent=...)`` delegate of a member — is that member's
    delegate sent to do the work: it keeps the member's identity, its
    ``[PERMANENT RULES]`` and its memory, but not the desk protocol, whose
    "front desk vs workshop" item (move work out only when it is long-running
    or spans many items) would only make the delegate hand the work on again.

    A member with no persisted ``member_id`` is never in this position. Its
    record names it by ``selection_kind == "member"`` and ``selection_name``
    alone, and no record field can say "this member, under that template"
    without losing the member -- and losing the member drops its rules along
    with its persona. The ``session_create`` arm therefore keeps such a member's
    selection and changes only the template. That child is not a template
    selection, so this predicate is False for it; it still loses the desk
    layers, because the surface half of :func:`_desk_withheld` answers for it
    (no caller names its desk). A ``spawn_run(agent=...)`` child of such a member
    is a plain template run on the parent's store and has no member section at
    all.
    """
    return (
        execution_context is not None
        and execution_context.member_id is not None
        and execution_context.selection_kind == "template"
    )


def _desk_withheld(execution_context: Any, desk_member: str) -> bool:
    """Whether this turn's member section is identity and permanent rules ONLY.

    The one predicate behind withholding the member DESK — layer 2
    (``[HOW YOU WORK]``) and layer 4 (``[CURRENT ASSIGNMENT]``, the agent-writable
    briefing) — read by every ``_build_member_section`` caller. Identity and
    ``[PERMANENT RULES]`` follow the member's STORE: the execution record's owner
    (``member_id``, or ``selection_name`` when ``selection_kind == "member"``)
    receives them on every surface, because the user's bounds on a member follow
    its memory. The desk follows the SURFACE: the two layers describe how the
    member runs its own DM thread ("this DM thread is your front desk", "keep
    your working memory in the briefing file"), so they are delivered only where
    the caller names that thread by passing ``member=`` — the dashboard's
    ``mode == "member"`` slot does, an ordinary chat that merely resolved to a
    crew alias does not, and neither does a cron, channel or delegated turn whose
    owner comes from the record alone.

    Two reasons withhold, either suffices:

    * *desk_member* is empty — no caller named this turn as the member's desk. The
      stock ``default`` alias resolves every plain dashboard chat to
      ``selection_kind == "member"``; without this half every such chat became a
      member desk that rewrote the briefing file from an ordinary conversation.
    * :func:`_template_selected_on_member_store` — the record runs the member's
      store under an explicitly selected template (the member's delegate).
    """
    return not desk_member or _template_selected_on_member_store(execution_context)


def _build_member_section(
    self,
    member: str,
    *,
    strict: bool = False,
    include_briefing: bool = True,
    desk_withheld: bool = False,
) -> str:
    """Assemble the four-layer identity for a member's bound execution.

    Contract and rationale: ``context_assembly.member.build_member_section``.
    """
    return _member.build_member_section(
        member,
        strict=strict,
        include_briefing=include_briefing,
        desk_withheld=desk_withheld,
    )


def _build_v2_essentials(
    self,
    memory_store: str | None,
    *,
    member: str = "",
    member_is_id: bool = True,
    project: str | None = None,
    template_dir: str | None = None,
    workspace: str | None = None,
    blocks_reads: bool = False,
    context_groups: frozenset[str] | None = None,
    profile_overrides: dict[str, str] | None = None,
    native_documents: dict[str, str] | None = None,
    native_envelope_out: list[str] | None = None,
    execution_template: str = "",
    member_template: str = "",
    conditional_index: bool = False,
    trigger_text: str = "",
    steering_dirs: tuple[str, ...] = (),
    desk_withheld: bool = False,
    provider_type: str | None = None,
) -> str:
    """Refresh complete member essentials without opening learned memory.

    Contract and rationale: ``context_assembly.member.build_v2_essentials``.
    """
    return _member.build_v2_essentials(
        self,
        memory_store,
        member=member,
        member_is_id=member_is_id,
        project=project,
        template_dir=template_dir,
        workspace=workspace,
        blocks_reads=blocks_reads,
        context_groups=context_groups,
        profile_overrides=profile_overrides,
        native_documents=native_documents,
        native_envelope_out=native_envelope_out,
        execution_template=execution_template,
        member_template=member_template,
        conditional_index=conditional_index,
        trigger_text=trigger_text,
        steering_dirs=steering_dirs,
        desk_withheld=desk_withheld,
        provider_type=provider_type,
    )
