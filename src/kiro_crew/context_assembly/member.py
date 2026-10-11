"""Member inclusion: the member identity section and the V2 essentials envelope.

A member's prompt is built from four layers with distinct owners -- the derived
``[MEMBER IDENTITY]``, the product protocol ``[HOW YOU WORK]``, the user's
``[PERMANENT RULES]`` and the member's own ``[CURRENT ASSIGNMENT]`` briefing -- and,
for a Memory V2 member, an essentials envelope that carries its persona, project
documents, folder steering and memory anchors. Every variable payload is scrubbed
of member-authority markers before the genuine headers are minted around it.

Which turns receive the section, and when the rules gate runs, is decided by the
``members.member_turn_context`` chokepoint at the call sites in
:mod:`kiro_crew.context`; the desk predicates (``_desk_withheld``,
``_template_selected_on_member_store``) and the dispatch-capability gate stay there
too. ``ContextBuilder._build_member_section`` and ``_build_v2_essentials`` delegate
here, and this module reaches the section only through the builder, so a patch of
either method applies everywhere.

New member layers and envelope rules belong here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.context_assembly import inclusion as _inclusion
from kiro_crew.context_assembly import markers as _markers

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.folder_steering import SteeringCollection

logger = logging.getLogger("kiro_crew.context")


def _essential_part_cost(source: str, body: str) -> int:
    """The characters one document adds, exactly as ``render_essentials`` renders it."""
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    return (
        len(f"[Essential source: {ctx._neutralize_structural_markers(source)}]\n")
        + len(ctx._neutralize_structural_markers(_markers._scrub_member_payload(body)))
        + 1
    )


def _fit_folder_steering_into_envelope(
    documents: list[tuple[str, str]],
    folder_docs: "SteeringCollection | list[tuple[str, str]]",
    *,
    identity: str,
    owner: str,
) -> list[tuple[str, str]]:
    """The prefix of *folder_docs* that fits the essentials envelope beside *documents*.

    ``render_essentials`` REFUSES an envelope over ``ESSENTIAL_MAX_CHARS`` or
    ``_MAX_DOCUMENTS`` -- correct for a member's own essentials, which must
    never be silently cut, but folder steering is operator-pointed task
    guidance that the non-member path already truncates. One plausible 64 KB
    guide must not abort every turn of every member chat in the folder, so
    folder documents are admitted in order while both bounds still hold and
    the tail is dropped. The character arithmetic mirrors the renderer part for
    part (same header, same scrub, same neutralization) so the fitted envelope
    renders without ever reaching its refusal.

    A dropped tail is never silent: whenever this fit leaves documents out, or
    the collection itself hit a ceiling, ONE extra essentials document
    (``FOLDER_STEERING_OMISSION_SOURCE``) states the counts, and its own cost is
    reserved inside both bounds -- fitted documents are given back from the
    tail until the notice fits -- so the notice is the last thing to go, not
    the first, and past the count room it may take one extra slot (that ceiling
    bounds the member's OWN declared essentials, not the envelope). A member turn
    whose essentials leave no room even for a minimal notice
    gets no folder steering at all, logged at warning.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    if isinstance(folder_docs, ctx.SteeringCollection):
        candidates = folder_docs.documents
        ceilings = folder_docs.omissions
    else:
        candidates = folder_docs
        ceilings = []
    try:
        used = len(ctx.render_essentials(documents, identity=identity))
    except ctx.MemberEssentialContextError:
        # The member's own essentials already exceed the envelope; the caller's
        # render raises with its own diagnostic. Folder steering adds nothing.
        return []
    count_room = ctx._MAX_DOCUMENTS - len(documents)
    fitted: list[tuple[str, str]] = []
    for source, body in candidates:
        if len(fitted) >= count_room:
            break
        # A folder document's source is a HOST FILENAME an agent can choose, and
        # ``render_essentials`` scrubs member-authority markers from bodies
        # only -- its source labels are trusted member sources. A folder label
        # is not one of those, so it is scrubbed here, before costing, and the
        # scrubbed spelling is what the envelope carries.
        source = _markers._scrub_member_payload(source)
        cost = _essential_part_cost(source, body)
        if used + cost > ctx.ESSENTIAL_MAX_CHARS:
            break
        used += cost
        fitted.append((source, body))
    while True:
        dropped = len(candidates) - len(fitted)
        if not dropped and not ceilings:
            return fitted
        lines = [ctx.render_omission_notice(omission) for omission in ceilings]
        if dropped:
            lines.append(
                f"[FOLDER STEERING OMISSION: {dropped} more document(s) were not loaded -- "
                f"they do not fit beside this member's own essentials. "
                f"The standards above are incomplete.]"
            )
        notice = (ctx.FOLDER_STEERING_OMISSION_SOURCE, "\n".join(lines))
        # The document-count ceiling bounds the member's OWN declared essentials
        # (member_essential_context enforces it on the declaration); the
        # renderer enforces only the character bound. So the notice may take
        # ONE slot past ``count_room`` -- otherwise a member whose own essentials
        # fill every slot would lose folder steering with no trace in the
        # envelope that replaces every prior snapshot. Folder DOCUMENTS still
        # respect the count room above; only the notice is exempt.
        if used + _essential_part_cost(*notice) <= ctx.ESSENTIAL_MAX_CHARS:
            logger.debug(
                "folder steering truncated for member %s: %d of %d documents fit the envelope",
                owner,
                len(fitted),
                len(candidates),
            )
            return [*fitted, notice]
        if not fitted:
            # Nothing left to give back. One last, minimal line: it says only
            # that folder steering exists and was omitted, so the envelope that
            # replaces every prior snapshot never drops the rules in silence.
            minimal = (
                ctx.FOLDER_STEERING_OMISSION_SOURCE,
                "[FOLDER STEERING OMISSION: this folder declares steering that does not fit "
                "beside this member's own essentials; none of it is loaded.]",
            )
            if used + _essential_part_cost(*minimal) <= ctx.ESSENTIAL_MAX_CHARS:
                logger.warning(
                    "folder steering omitted entirely for member %s: only the minimal notice fits",
                    owner,
                )
                return [minimal]
            logger.warning(
                "folder steering omitted entirely for member %s: the essentials envelope "
                "has no room even for the omission notice",
                owner,
            )
            return []
        used -= _essential_part_cost(*fitted.pop())


def _fit_member_guides_into_envelope(
    documents: list[tuple[str, str]], guides: frozenset[str], *, identity: str, owner: str
) -> tuple[list[tuple[str, str]], int]:
    """``(documents, guides left out)`` so an over-budget envelope renders, not refuses.

    Guides (*guides*: every source but the member's core) leave WHOLE from the
    tail of declaration order until the rest plus ONE in-band notice fits; then
    each left-out guide, in declaration order, is given back if it still fits,
    so a guide too large to fit does not take the smaller ones after it out.
    The notice names each left-out guide with its size, or only counts them when
    that is all that fits. A fitting envelope is returned unchanged. A core that
    does not fit beside even the short notice is returned as is, so
    ``render_essentials`` refuses it with its own diagnostic. Nothing is read
    here: a source the readers refused has already raised.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.member_essential_context import ESSENTIAL_OMISSION_SOURCE

    cap = ctx.ESSENTIAL_MAX_CHARS
    try:
        used = len(ctx.render_essentials([], identity=identity))
    except ctx.MemberEssentialContextError:
        return documents, 0  # the identity alone overflows; the caller's render refuses
    costs = [_essential_part_cost(*document) for document in documents]
    used += sum(costs)
    if used <= cap:
        return documents, 0

    def _notice(out: list[int], used: int) -> tuple[str, str] | None:
        lead = (
            f"ESSENTIAL CONTEXT INCOMPLETE. {len(out)} guide document(s) for this agent were "
            f"left out of this snapshot because the essential context is limited to {cap} "
            "characters"
        )
        tail = (
            ". They are left out for size, not removed. Do not assume their contents; if one "
            "matters for this task, read the file directly or ask the user."
        )
        listed = ", ".join(f"{documents[i][0]} ({len(documents[i][1]):,} characters)" for i in out)
        for body in (f"{lead}: {listed}{tail}", lead + tail):
            if used + _essential_part_cost(ESSENTIAL_OMISSION_SOURCE, body) <= cap:
                return (ESSENTIAL_OMISSION_SOURCE, body)
        return None

    out: list[int] = []
    notice = None
    for index in reversed([i for i, (source, _) in enumerate(documents) if source in guides]):
        out.insert(0, index)
        used -= costs[index]
        notice = _notice(out, used)
        if notice is not None:
            break
    if notice is None:
        return documents, 0
    for index in list(out):
        rest = [i for i in out if i != index]
        given_back = _notice(rest, used + costs[index]) if rest else None
        if given_back is not None:
            out, used, notice = rest, used + costs[index], given_back
    logger.warning(
        "essential context for member %s is over its %d-character limit; %d guide(s) left out: %s",
        owner,
        cap,
        len(out),
        ", ".join(f"{documents[i][0]} ({len(documents[i][1])} characters)" for i in out),
    )
    left = set(out)
    return [d for i, d in enumerate(documents) if i not in left] + [notice], len(out)


# Product-owned working protocol for crew members (layer 2 of the member
# system prompt — see ContextBuilder._build_member_section for the layer
# model). Identical for every member; per-member content lives in the
# derived identity layer above it and the rules/briefing layers below it.
_MEMBER_HOW_YOU_WORK_COMMON = """[HOW YOU WORK]
1. You do work; you are not a Q&A bot. When the user asks a question, they
   usually want something solved. Read the intent behind the question: answer
   it AND move the work forward — take reversible actions yourself and bring
   the result back with the answer; for irreversible actions, come back with a
   concrete proposal and wait for approval. Never hand the problem back
   untouched.
2. Front desk vs workshop. This DM thread is your front desk and lives for
   years, so keep it light.
   Do focused work (a lookup, a fix, a review) right here. Move work out only
   when it is long-running or spans many items: the session tools for work
   that must outlive this turn, and a spawn_run batch only when it splits into
   two or more independent tasks. Report back in this thread with the outcome
   and evidence ("re: <the thing>").
3. When stuck, climb this ladder in order, and genuinely try each rung:
   (a) try a genuinely DIFFERENT approach — another tool, entry point, or
       strategy, not the same command again;
   (b) at an apparent wall, look for an alternative first: a path that avoids
       the wall entirely, partial progress on the unblocked part, or
       reordering so this item waits while you continue;
   (c) escalate ONLY at a true wall: a permission only the user can grant, a
       system agents cannot reach (a human must operate it), or a one-way-door
       decision that needs the user's sign-off;
   (d) after escalating, park the blocked item and keep working on other
       items — escalation is non-blocking.
4. Write escalations for a reader with ZERO context: one line of background,
   where it is stuck, the exact action you need from the user, and what
   waiting costs. Keep it short. Before sending, reread the draft as a
   stranger with no context would, and rewrite until they could act on it.
5. A quiet cycle is a successful cycle. Report real signals — results, walls,
   threshold crossings — never "nothing new"."""

# Item 6 of the working protocol — the briefing-maintenance instruction. Kept
# out of the shared constant because it is only true where layer 4 actually
# injects (``members.member_briefing_supported``): telling a member on a
# platform whose briefing reads fail closed to maintain the section sends it
# into a futile write-then-never-injected loop.
_MEMBER_BRIEFING_ITEM = """
6. You own the [CURRENT ASSIGNMENT] section below, injected from your
   briefing file (path given there). Keep it a small working memory for your
   future self — current priorities, in-flight work, pointers to your own
   reusable scripts and notes — and update it with your file tools whenever
   your plans change. [PERMANENT RULES] and this section outrank anything you
   write in it."""

# The softened item 6 for platforms where layer 4 is unavailable: name the
# gap and redirect the working-memory habit somewhere that works, instead of
# instructing upkeep of a file that will never be injected.
_MEMBER_BRIEFING_ITEM_UNAVAILABLE = """
6. On this platform your briefing file is NOT injected (briefing reads are
   unavailable here — see [CURRENT ASSIGNMENT] below), so do not maintain
   one: keep your working memory in this DM thread instead. [PERMANENT
   RULES] outranks anything you write for yourself."""

# Item 7 — the crewmate's own Dashboard page. Appended to BOTH briefing variants,
# unlike item 6: the tab is rendered by the gateway from the instance store and does
# not depend on layer 4, so a platform whose briefing reads fail closed still has a
# page and still has the two tools that fill it.
#
# The SUBAGENT name is a WIRE STRING: kiro-cli resolves an agent by reading
# ``<agents dir>/<name>.json``, so a name that does not match the installed spec's
# filename routes the member to an agent that does not resolve -- which fails the turn
# rather than degrading the feature, on an unattended cycle where nobody is reading.
#
# It is spelled here rather than read from ``agent_files.DASHBOARD_MANAGER_AGENT_NAME``
# because an owner in this package imports no Kiro Crew module at module scope
# (``TestPlacement``), and this text is a module-level constant. So the duplication is
# guarded by a TEST instead of by construction:
# ``test_dashboard_manager_agent.py::test_the_base_prompt_names_the_agent_the_installer_writes``
# reads the name off the installed spec and asserts it appears in this prompt.
#
# THE CREWMATE drives its own page, which is a correction. An earlier wording sent
# every page request to the `kirocrew-dashboard-manager` subagent and told the
# crewmate it had no tools for it -- while ``_MEMBER_PANEL_GRANTS`` grants it all six
# verbs. The crewmate is also the only one of the two whose identity the panel's
# tools can resolve: they go through ``mcp_core.require_strict_session_key``, which
# resolves the CALLING session and never a parent through a ``/proc`` walk, because
# that walk is how a subagent would reach a page that is not its own. A member DM
# session has that identity; a subagent is a weaker place to stand. So the subagent is
# extra capacity for a long page job, and the crewmate's own hands are the path.
_MEMBER_DASHBOARD_ITEM = """
7. You have a Dashboard tab of your own, and it is yours to change. `dashboard_fields`
   lists its fields and `dashboard_write` fills the ones that are yours. Anything a
   person must ACT on goes into `panel_publish` and those fields, not only into your
   reply — the tab is what they read when they are not reading this thread. To change the PAGE itself, load the
   `dashboard` skill and follow it: `dashboard_templates` to search, `dashboard_preview`
   to stage one, `dashboard_apply` once they say yes, `dashboard_rollback` to go back.
   ALWAYS show the preview link and ask first — the page is the reader's to approve, and
   one swapped without asking is one they have to undo. With the 'Dynamic Dashboard'
   Feature Preview off (the default) the tab shows your `panel_publish` record; this page
   shows only for readers who turned the preview on. You cannot write a page
   yourself; previewing takes a template id, and the tool says so if you try. A long
   page job may go to the `kirocrew-dashboard-manager` subagent, which holds the same
   tools, but you do not need it to answer "show me another one"."""


# The full protocol: the shared items, the briefing item, and the page item. The
# shape every layer-4-capable platform injects, and the one the behaviour-layer
# tests pin.
_MEMBER_HOW_YOU_WORK = _MEMBER_HOW_YOU_WORK_COMMON + _MEMBER_BRIEFING_ITEM + _MEMBER_DASHBOARD_ITEM


def build_member_section(
    member: str,
    *,
    strict: bool = False,
    include_briefing: bool = True,
    desk_withheld: bool = False,
) -> str:
    """Assemble the four-layer identity for a member's bound execution.

    Layer ownership (precedence is the injection order — earlier outranks
    later — with ONE stated exception: layer 3's header explicitly claims
    precedence over the whole section, protocol included, so the user's
    safety boundary is never formally outranked by product prose):

    1. ``[MEMBER IDENTITY]`` — derived from the crew's registered config
       (name, description, triggers). Auto-generated: works even for a
       crew whose description is empty, which is exactly the case that
       needs a floor.
    2. ``[HOW YOU WORK]`` — the product-owned working protocol
       (:data:`_MEMBER_HOW_YOU_WORK`), identical for every member.
    3. ``[PERMANENT RULES]`` — user-owned. Read from the keystone-gated
       ``member-rules/`` subtree, so the member's own file tools cannot rewrite
       it; omitted entirely when the user has not written rules.
    4. ``[CURRENT ASSIGNMENT]`` — member-owned working memory, read
       (capped) from the member's own agent-writable briefing file.

    ``desk_withheld`` is the verdict of :func:`_desk_withheld` for the turn
    being built: the section is the member's identity and rules ONLY. Layers
    2 and 4 describe how the member runs its own desk — the DM thread — so
    both are withheld, with no placeholder and no briefing read, when no
    caller named this turn as that desk (an ordinary chat that resolved to
    the crew alias, a cron, channel or delegated turn) or when the store runs
    under an explicitly selected template (the desk protocol's "hand
    substantial work to a separate session" item is what a template picked
    to do that work must not be told). Layer 1 stays, minus the sentence
    that describes the DM thread, because the memory the turn reads and
    writes is that member's; layer 3 stays because the user's bounds on a
    member follow its memory, not its surface or template.

    V1 retains its existing optional-layer failure behavior. V2 passes
    ``strict=True`` with a stable member ID, never a configured-name fallback,
    and never suppresses assembly errors. An existing but
    unreadable permanent-rules file aborts both versions. Briefing retains
    its existing bounded reader and explicit truncation notice; privacy or
    memory-scope withholding skips that working-memory layer entirely.

    Blocking file IO inside — callers reach this via ``build_message``,
    which chat paths already run off-loop.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    try:
        cfg = ctx.KiroCrewConfig.load()
        from kiro_crew.execution_context import member_config_for_id

        if strict:
            alias, crew = member_config_for_id(cfg, member)
            slug = member
            member = alias
        elif member in cfg.agents and not getattr(cfg.agents[member], "member_id", ""):
            slug = ctx.slug_for_name(member)
            crew = cfg.agents[member]
        else:
            alias, crew = member_config_for_id(cfg, member)
            slug = member
            member = alias
    except (ctx.MemberSlugError, ValueError):
        if strict:
            raise
        return ""
    # Type-guarded, not just None-guarded: these fields come from an
    # operator-editable JSON file, and a hand-edited non-string value
    # (`"description": 1`) must degrade to the identity floor rather than
    # crash the member's chat turn on `.strip()`.
    _desc = getattr(crew, "description", "") if crew else ""
    _trig = getattr(crew, "triggers", "") if crew else ""
    description = _desc.strip() if isinstance(_desc, str) else ""
    triggers = _trig.strip() if isinstance(_trig, str) else ""
    # Rules are read OUTSIDE the total-degrade guard, deliberately: every
    # other failure degrades this section to an ordinary session, but for
    # the one safety-relevant layer that degrade IS the fail-open — a
    # member the user bounded would keep running with no bounds at all.
    # An unreadable rules file therefore propagates and ABORTS the turn:
    # the member does not run until the user repairs or clears the file.
    # (Missing file still reads as "" — the normal unbounded-by-choice
    # state — and the file is gateway-written atomically, so corruption
    # is an operator-level event, not a routine one.)
    rules = ctx.read_member_rules(slug, member)
    # Layer-4 availability decides both the briefing read and the wording
    # around it (item 6 above, the placeholder below): where the pinned
    # briefing read fails closed (Windows — member_briefing_supported),
    # instructing upkeep of a never-injected file is a futile loop, so the
    # section says the layer is unavailable instead. A turn with the desk
    # withheld gets neither the layer nor a placeholder, so its briefing
    # is not read at all.
    briefing_ok = include_briefing and not desk_withheld and ctx.member_briefing_supported()
    briefing = ""
    briefing_path = ""
    if briefing_ok:
        try:
            briefing = ctx.read_member_briefing(slug)
            briefing_path = str(ctx.member_briefing_path(slug))
        except Exception:
            if strict:
                raise
            logger.warning(
                "member section degraded to ordinary session for %r", member, exc_info=True
            )
            return ""

    # Every VARIABLE payload is scrubbed before the genuine headers are
    # minted around it — see _MEMBER_MARKER_RES for why this runs at
    # content time rather than in the structural-marker scan.
    member = _markers._scrub_member_payload(member)
    description = _markers._scrub_member_payload(description)
    triggers = _markers._scrub_member_payload(triggers)
    rules = _markers._scrub_member_payload(rules)
    briefing = _markers._scrub_member_payload(briefing)

    identity = [
        f"[MEMBER IDENTITY]\nYou are {member}. Not a generic assistant, and not an "
        f"extension of the user: {member} is an identity of your own — your name, "
        "your role, your memory of this thread, and your track record belong to you."
    ]
    if description:
        identity.append(f"Your role: {description}")
    if triggers:
        identity.append(f"Your remit — the work that belongs to you: {triggers}")
    if not desk_withheld:
        # The one identity sentence that describes the DM thread itself: an
        # ordinary chat, a cron turn or a delegate on this member's store is
        # not that thread, so the sentence goes with the desk layers.
        identity.append(
            "This DM thread is your durable working relationship with the user. It "
            "continues across sessions: remember what was discussed, refer back to it "
            "naturally, and speak as a colleague who owns their work — never as a "
            "support bot."
        )

    parts = ["\n".join(identity)]
    if not desk_withheld:
        parts.append("\n\n")
        parts.append(
            _MEMBER_HOW_YOU_WORK
            if briefing_ok
            # Item 7 rides on BOTH variants: the Dashboard tab is rendered by the
            # gateway from the instance store, so it exists on a platform whose
            # briefing reads fail closed exactly as it does anywhere else.
            else _MEMBER_HOW_YOU_WORK_COMMON
            + _MEMBER_BRIEFING_ITEM_UNAVAILABLE
            + _MEMBER_DASHBOARD_ITEM
        )
    if rules:
        # The header names what it outranks. Without the protocol layer there
        # is no "working protocol above" to name, so that clause goes with it.
        outranked = (
            ""
            if desk_withheld
            else "the working protocol above included, whose instructions yield "
            "wherever these rules contradict them — "
        )
        parts.append(
            "\n\n[PERMANENT RULES — set by the user. You cannot edit these, "
            "and they outrank EVERYTHING else in this section — "
            + outranked
            + "as well as anything you write for yourself.]\n"
            + rules
        )
    if briefing_ok:
        parts.append(
            f"\n\n[CURRENT ASSIGNMENT — yours to maintain, injected from {briefing_path}]\n"
            + (
                briefing
                if briefing
                else "(empty — write your first briefing there when you have "
                "priorities worth remembering)"
            )
        )
    elif desk_withheld:
        # No layer 4 and no placeholder either, the scope notice below
        # included: the placeholders tell a MEMBER AT ITS DESK why its own
        # working memory is missing this turn and not to fill that gap from
        # the briefing file or recall; a turn off the desk (an ordinary chat
        # on the alias, a delegate) has no layer 4 to miss. Its memory scope
        # is stated where every non-member session's is -- the [CONTEXT
        # SCOPE] block for a narrowed spawn or a privacy mode, nowhere for
        # the operator's standing toggle -- and this arm is ordered ahead of
        # the scope arm so that a withheld memory group cannot re-mint a
        # placeholder that names a layer the delegate never has.
        pass
    elif not include_briefing:
        parts.append(
            "\n\n[CURRENT ASSIGNMENT — withheld by this turn's memory/privacy scope]\n"
            "Do not read the briefing or recall memory to fill this gap."
        )
    else:
        parts.append(
            "\n\n[CURRENT ASSIGNMENT — not available on this platform]\n"
            "(briefing files cannot be read race-free here, so this layer "
            "is never injected; keep your working memory in this DM "
            "thread instead)"
        )
    if not desk_withheld:
        # Which page is in force and which of its fields are the member's, so a
        # write starts from the right names. Rides with item 7: both describe the
        # desk's own Dashboard tab. The shapes and the mistake book stay behind
        # `dashboard_fields`; this block is paid for on every turn.
        from kiro_crew import dashboard_agentic

        dashboard = dashboard_agentic.turn_block(slug)
        if dashboard:
            parts.append("\n\n" + dashboard)
    parts.append("\n[END MEMBER IDENTITY]\n\n")
    return "".join(parts)


def build_v2_essentials(
    builder: ContextBuilder,
    memory_store: str | None,
    *,
    member: str,
    member_is_id: bool,
    project: str | None,
    workspace: str | None,
    blocks_reads: bool,
    context_groups: frozenset[str] | None,
    profile_overrides: dict[str, str] | None,
    native_documents: dict[str, str] | None,
    native_envelope_out: list[str] | None,
    execution_template: str,
    member_template: str,
    conditional_index: bool,
    trigger_text: str,
    steering_dirs: tuple[str, ...],
    desk_withheld: bool,
    provider_type: str | None,
) -> str:
    """Refresh complete member essentials without opening learned memory.

    ``desk_withheld`` is :func:`_desk_withheld`'s verdict for the turn being
    built and is handed to the member-section builder unchanged: the
    envelope keeps the member's identity, rules, documents and anchors, and
    withholds only the desk protocol and briefing.

    ``provider_type`` names the harness serving the session. Only kiro-cli
    (:data:`PROVIDER_ACP`) honours ``chat.disableInheritingDefaultResources``,
    so its verdict is read once here, where the harness is known, and handed
    to every consumer; no consumer reads the setting itself. ``None`` means
    the harness is not known to be kiro-cli, so the opt-out is not read and
    the snapshot keeps inheriting: a caller that omits it can never drop the
    global steering on a harness that has nothing delivering it instead.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.member_essential_context import (
        MemberEssentialContextError,
        documents_for_member,
        member_context_identity,
        render_essentials,
    )
    from kiro_crew.memory import _DEFAULT_PREFERENCES, _DEFAULT_PROJECTS

    owner, template = member_context_identity(member, member_is_id=member_is_id)
    template = member_template or template
    if not owner:
        return ""
    # Same config intersection as build_session_context: this builder is a
    # second context entry point, so a group the operator disabled must be
    # withheld here too rather than only on the main path.
    #
    # EXCEPT when profile_overrides is supplied. That argument makes this a
    # VALIDATOR (_validate_private_profile_update), not a context build: the
    # candidate profile is appended only under the memory-group gate below,
    # and render_essentials' combined-budget refusal is what rejects a
    # profile that fits per-file but overflows the combined cap. Scoping the
    # groups here would drop that gate whenever injection is disabled, so an
    # oversized profile would save and then break every later member build.
    # A validation pass must see the complete candidate set regardless of
    # what the operator currently injects.
    if profile_overrides is None:
        context_groups = ctx._config_scoped_groups(context_groups)
    reads = not blocks_reads and _inclusion._group_included(
        context_groups, _inclusion.CONTEXT_GROUP_MEMORY
    )
    include_project = not blocks_reads and _inclusion._group_included(
        context_groups, _inclusion.CONTEXT_GROUP_PROJECT
    )
    identity = builder._build_member_section(
        owner, strict=True, include_briefing=reads, desk_withheld=desk_withheld
    )
    # A validation pass measures the largest envelope any harness can build,
    # so it keeps inheritance and never reads kiro-cli's opt-out. On a normal
    # turn, the setting changes what a member loads only on a session
    # kiro-cli serves: every other harness keeps inheriting. Read once, where
    # the project group applies, and pass the verdict to the snapshot and the
    # folder-steering dedup below so the two cannot disagree.
    inherits_default_resources = True
    if profile_overrides is None and include_project and provider_type == ctx.PROVIDER_ACP:
        inherits_default_resources = ctx.member_inherits_default_resources(project)
    # The member's CORE (persona prompt, SOUL.md, context settings, the
    # profile anchors and recall note appended below) is never left out; every
    # other document the readers return is a guide the fit below may drop whole.
    core_sources: set[str] = set()
    documents = documents_for_member(
        template,
        project,
        conditional_index=conditional_index,
        context_settings=True,
        trigger_text=trigger_text,
        include_project=include_project,
        inherits_default_resources=inherits_default_resources,
        core_sources_out=core_sources,
    )
    if execution_template and execution_template != template:
        sources = dict(documents)
        for source, body in documents_for_member(
            execution_template,
            project,
            conditional_index=conditional_index,
            context_settings=True,
            trigger_text=trigger_text,
            include_project=include_project,
            inherits_default_resources=inherits_default_resources,
            core_sources_out=core_sources,
        ):
            if source in sources and sources[source] != body:
                raise MemberEssentialContextError(
                    f"Essential source {source}: changed during preparation"
                )
            sources[source] = body
        documents = list(sources.items())
    # Folder-inherited steering rides INSIDE the essentials envelope for a
    # member chat (the envelope IS its session-start context), through the
    # same reader the non-member path uses. After the template/project
    # documents so global and project steering keep precedence; before the
    # memory files. None of these sources is declared host-native
    # (kiro_launch_documents never sees the folder dirs), so the native
    # envelope keeps their bodies. The envelope's bounds -- 64 documents AND
    # ``ESSENTIAL_MAX_CHARS`` rendered -- are applied to folder steering as
    # a BUDGET, not a fault: the member's own sources already occupy part
    # of both, and an operator pointing a folder at a large standards tree
    # must degrade the way the non-member path does (by dropping the tail)
    # rather than abort every turn of every member chat in that folder
    # until the folder shrinks. The character budget depends on the memory
    # documents appended below, so the candidates are collected here (their
    # position recorded) and fitted just before the envelope renders.
    #
    # The project and global ``.kiro/steering`` trees are skipped as already
    # delivered only while the snapshot above actually delivered them: a
    # kiro-cli workspace that opts out of the default resources gets them
    # only as far as the template declares them, so a folder that declares
    # one of those roots must carry the rest of its documents itself. A
    # folder document whose canonical path the template already delivered
    # is not collected again under the opt-out. Same verdict as the
    # snapshot, read once above; the non-member path takes the same
    # decision in _render_folder_steering_section.
    guide_sources = frozenset(source for source, _ in documents) - core_sources
    folder_docs: SteeringCollection = ctx.SteeringCollection()
    folder_insert_at = len(documents)
    if steering_dirs and include_project:
        folder_docs = ctx.collect_folder_steering(
            steering_dirs,
            project=project,
            skip_delivered_roots=inherits_default_resources,
            delivered_sources=(
                tuple(source for source, _ in documents) if not inherits_default_resources else ()
            ),
        )
    if reads:
        from kiro_crew.memory_stores import memory_store_dir_for

        # Manual anchors are ordinary member documents, independent of DB
        # readiness. Never initialize learned memory to render a persona.
        memory = ctx.MemoryStore(
            workspace=memory_store_dir_for(memory_store or "default"), memory_version=2
        )
        for path, empty in (
            (memory._preferences_file, _DEFAULT_PREFERENCES),
            (memory._projects_file, _DEFAULT_PROJECTS),
        ):
            if profile_overrides is not None and path.name in profile_overrides:
                body = profile_overrides[path.name]
            else:
                try:
                    entry = memory._guarded_entry(
                        path,
                        require_readable=True,
                        missing_ok=False,
                    )
                except OSError as exc:
                    raise MemberEssentialContextError(f"Essential source {path}: {exc}") from exc
                body = entry["content"]
            if body.strip() and body.strip() != empty.strip():
                documents.append((str(path), body))
        documents.append(
            (
                "on-demand memory",
                "For a specific earlier fact, decision or experience, call memory_recall "
                "with a precise question. Use only the returned relevant snippets and "
                "their sources. No search runs automatically; skip recall when the "
                "current conversation already answers the question.",
            )
        )
    # Guides first, folder steering into what is left, as before. Every guide
    # sits before ``folder_insert_at`` and the notice is appended last, so the
    # insertion point moves back by exactly the number left out.
    documents, left_out = _fit_member_guides_into_envelope(
        documents, guide_sources, identity=identity, owner=owner
    )
    folder_insert_at -= left_out
    if folder_docs:
        fitted = _fit_folder_steering_into_envelope(
            documents, folder_docs, identity=identity, owner=owner
        )
        documents[folder_insert_at:folder_insert_at] = fitted
    envelope = render_essentials(documents, identity=identity)
    if native_envelope_out is not None:
        native = native_documents or {}
        native_envelope_out.append(
            render_essentials(
                [
                    (
                        (source, "")
                        if native.get(source) == body
                        or native.get(f"template://{execution_template}#prompt") == body
                        else (source, body)
                    )
                    for source, body in documents
                ],
                identity=identity,
            )
        )
    return envelope


def operating_mode_block(agent_label: str) -> str:
    """The ``[CREW MEMBER OPERATING MODE]`` block for a member's pinned DM session.

    The member is a CONTROLLER: its DM thread stays the identity/management loop
    while real work runs in worker sessions it dispatches and patrols. The
    ``session_*`` tools this block names arrive as a per-session mount of the
    dashboard session-control server (``members.member_dispatch_session_server``),
    and the server authorizes member callers automatically
    (``dashboard/session_control.py``), bounded to sessions the member created
    itself -- so the instructions hold with zero configuration.
    """
    return (
        f"[CREW MEMBER OPERATING MODE]\n"
        f'You are the crew member "{agent_label}". This pinned conversation is '
        f"your DM thread with the user — your identity, your inbox, and your "
        f"ledger. Keep it for decisions, reports, and escalations; do NOT run "
        f"long or heavy work inline here.\n"
        f"When real work arrives (a task to implement, an investigation to "
        f"run), DISPATCH it: open a worker session with session_create, seed "
        f"it with a self-contained brief via session_send (the worker has "
        f"none of this thread's context), then PATROL your workers with "
        f"session_read_message on a monitor_start loop — you own noticing a "
        f"worker that stalled or died, restarting it, or escalating. Stop a "
        f"runaway with session_stop. You can only control sessions you "
        f"created.\n"
        f"Report outcomes back in this thread when work completes or needs "
        f"a decision only the user can make.\n\n"
    )


def resolve_turn_owner(
    execution_context: Any,
    session_key: str | None,
    member: str,
    memory_store: str | None,
    blocks_reads: bool,
) -> tuple[Any, str, str, str | None, bool]:
    """``(execution_context, desk_member, member, memory_store, blocks_reads)`` for a turn.

    The caller's ``member=`` names this turn as the member's DESK (the dashboard
    passes it for a ``mode == "member"`` slot only). It decides the desk layers and
    nothing else; the execution record decides whose identity, rules and memory the
    turn carries, and a temporary record withholds every memory read.
    """
    if execution_context is None and session_key:
        from kiro_crew.execution_context import read_session_execution

        execution_context = read_session_execution(session_key)
    desk_member = member
    if execution_context is not None:
        member = execution_context.member_id or (
            execution_context.selection_name if execution_context.selection_kind == "member" else ""
        )
        memory_store = execution_context.store.legacy_name
        blocks_reads = blocks_reads or execution_context.memory_mode == "temporary"
    return execution_context, desk_member, member, memory_store, blocks_reads
