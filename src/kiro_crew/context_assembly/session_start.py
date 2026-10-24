"""The session-start context block: ``ContextBuilder.build_session_context``.

One function assembles the whole first-turn context and decides which blocks it
carries and in which order, admitted against the background budget. Its sections are
stamped with ``_mark`` (member, preamble, profile, workspace, docs, steering, thread
history, stop notes, memory, skills, lessons, provenance) and recorded by
:func:`_emit_context_section_timings`: the block is built after the user's message
arrives, so its cost lands on time-to-first-token. The member section is delivered
through the ``members.member_turn_context`` chokepoint.

Replayed thread history is redacted here before it is framed; the transcript reads
behind it stay in :mod:`kiro_crew.context` (``_recall_rows``,
``_build_stop_event_notes``). Every name callers rebind on the facade is read through
it at call time.

Builder state read: ``conversation_log``. The member section, the essentials envelope
and the shown-lesson record are reached through the builder's own methods
(``_build_member_section``, ``_build_v2_essentials``, ``_remember_startup_lessons``), so a
patch of any of them applies.

New session-start blocks belong in the owner that renders them; their place in the
order is decided here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiro_crew.context_assembly import budget as _budgets
from kiro_crew.context_assembly import inclusion as _inclusion
from kiro_crew.context_assembly import member as _member
from kiro_crew.context_assembly import replay as _replay
from kiro_crew.context_assembly import sections as _sections
from kiro_crew.context_assembly import store_admission as _store_admission
from kiro_crew.context_assembly import tool_names as _tool_names

logger = logging.getLogger("kiro_crew.context")


def _emit_context_section_timings(
    marks: list[tuple[str, float]],
    *,
    scope: str,
    is_custom: bool,
    total_chars: int = 0,
) -> None:
    """Log and record per-section durations for a first-turn context build.

    The first-turn context block is assembled AFTER the user's message arrives
    and the caller awaits it before dispatching the prompt, so its cost lands
    directly on time-to-first-token. Only a per-section breakdown can attribute
    that latency; without one, the whole assembly is a single opaque interval.

    *marks* is an ordered list of ``(label, monotonic)`` checkpoints. The first
    entry labels nothing and only stamps the start, so a section's duration is
    the delta from its predecessor. Repeated labels accumulate.

    ``custom`` is recorded as a bool rather than the agent name deliberately: a
    populated install has dozens of agents, and one series per agent per section
    would multiply the series count for no diagnostic gain.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    if len(marks) < 2:
        return
    timings: dict[str, float] = {}
    for (_, prev), (label, current) in zip(marks, marks[1:]):
        timings[label] = timings.get(label, 0.0) + (current - prev) * 1000.0
    total_ms = (marks[-1][1] - marks[0][1]) * 1000.0
    ranked = sorted(timings.items(), key=lambda kv: kv[1], reverse=True)
    # Sub-millisecond sections are omitted from the line to keep it readable;
    # they are still recorded as metric points below. A build whose every
    # section rounds to zero would log a header with no sections at all, which
    # is noise on the hottest path.
    reportable = [(label, ms) for label, ms in ranked if ms >= 1.0]
    if reportable:
        logger.info(
            "Context timings [%s]: total=%.0fms chars=%d %s",
            scope,
            total_ms,
            total_chars,
            " ".join(f"{label}={ms:.0f}ms" for label, ms in reportable),
        )
    try:
        recorder = ctx.get_recorder()
        for label, ms in timings.items():
            recorder.histogram(
                "kirocrew.context.section.duration",
                ms,
                unit="ms",
                attrs={"section": label, "custom": is_custom},
            )
    except Exception:
        logger.debug("Context section metric emission failed", exc_info=True)


def build_session_context(
    self,
    session_key: str | None = None,
    agent: str | None = None,
    resumed: bool = False,
    workspace: str | None = None,
    memory_store: str | None = None,
    compressed_history: str | None = None,
    mode: str = "",
    blocks_reads: bool = False,
    provider_type: str | None = None,
    minimal_context: bool = False,
    *,
    runtime_source: str | None = None,
    exclude_last_n: int = 0,
    model_window: int | None = None,
    context_groups: frozenset[str] | None = None,
    query_text: str = "",
    project: str | None = None,
    member: str = "",
    execution_context: Any = None,
    steering_dirs: tuple[str, ...] = (),
    _v2_essentials: str | None = None,
    member_dispatch_mounted: bool | None = None,
) -> str:
    """Build context for a new session (memory + skills + history).

    Injected once at session start, not on every message.

    When *compressed_history* is provided, it replaces the naive
    truncation of thread history. An empty string explicitly suppresses the
    fallback; only None requests a fallback read. build_message uses that
    suppression when it owns a separately budgeted outer replay.

    *model_window* remains accepted for caller compatibility. Crew background
    admission is fixed independently of it; replay and provider metadata are
    separate domains, not extra capacity for background facts.

    All providers — including Claude Code — receive the
    same injected context (critical rules, thread history, memory, skills,
    lessons); steering files are the one exception (see below). This keeps
    Claude Code at parity with kiro so dashboard/Slack UI contracts (diff
    blocks, OPTIONS buttons, file links) and prior conversation context
    behave identically across providers.

    *provider_type* names the harness serving the session, when the caller
    knows it. It reaches :meth:`_build_v2_essentials` as given, so a caller
    that names none (``None``) hands the essentials builder an unknown
    harness and a private member keeps inheriting kiro-cli's default
    resources. A harness that is not named is not assumed to be kiro-cli.
    Beyond the essentials it is consumed for the steering gate only: the
    steering block below is injected solely on the CC backend
    (``is_claude_code(provider_type)``). kiro-cli loads an agent's
    ``resources`` natively when spawned with ``--agent`` (acp/client_start.py
    ``_spawn``), so re-injecting steering on the ACP/kiro backend would
    duplicate what kiro already loaded; the CC backend (claude-agent-acp)
    does NOT read agent ``resources`` and still needs the explicit load.
    Everything else stays at CC/ACP parity.

    *context_groups* selects which switchable groups are injected (see
    ``SWITCHABLE_CONTEXT_GROUPS``). ``None`` — every caller except a
    sub-agent whose parent opted a group out — injects all of them, so the
    output is unchanged. Omitting a group skips its sections entirely rather
    than capping them to zero: a zero cap yields a truncation marker, not an
    empty string. A sub-agent that had a group withheld is told so by name
    (``_build_context_scope_section``) so it reports the gap instead of
    guessing.

    For custom agents (non-kirocrew), skills and workspace identity are
    skipped — the agent loads its own prompt via kiro-cli. The dashboard
    critical-rules contract is injected by DEFAULT for every agent, but a
    custom agent can opt out of it (and the dashboard tool nudges) by setting
    ``includeCrewContext: false`` in its materialized JSON. Memory, lessons,
    and hooks are injected for all agents.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.agent_files import is_kirocrew_default_agent

    execution_context, desk_member, member, memory_store, blocks_reads = _member.resolve_turn_owner(
        execution_context, session_key, member, memory_store, blocks_reads
    )
    is_custom = bool(agent) and not is_kirocrew_default_agent(agent)
    # A harness that is not named is not assumed to be kiro-cli: the
    # essentials builder, the folder dedup and the ``is_cc`` gate all read
    # the value as given.
    is_cc = ctx.is_claude_code(provider_type)
    caps = ctx._resolve_caps(model_window)
    blocks = _budgets.ContextParts()
    parts = blocks.parts
    append_required = blocks.append_required

    essentials = _v2_essentials
    if essentials is None:
        essentials = self._build_v2_essentials(
            memory_store,
            member=member,
            member_is_id=bool(execution_context and execution_context.member_id),
            project=project,
            workspace=workspace,
            blocks_reads=blocks_reads,
            context_groups=context_groups,
            member_template=execution_context.template_id if execution_context else "",
            steering_dirs=steering_dirs,
            desk_withheld=ctx._desk_withheld(execution_context, desk_member),
            provider_type=provider_type,
        )

    # Minimal V1 stays date/time + agent identity. Private V2 also carries
    # the complete essential envelope, including member-bound cron jobs.
    # Saves ~30-50k tokens per cron run for simple polling jobs.
    if minimal_context:
        _, tz = ctx.get_local_tz()
        now = ctx.datetime.now(tz)
        parts.append(f"[CURRENT DATE] {now.strftime('%A, %Y-%m-%d %H:%M %Z')}\n\n")
        agent_label = agent or "kirocrew"
        parts.append(f"[CURRENT AGENT] {agent_label}\n")
        if session_key:
            runtime = ctx._runtime_display_name(session_key, runtime_source)
            parts.append(f"[RUNTIME] {runtime}\n")
        parts.append("\n")
        # Cron/minimal runs still render tool-call pills in the dashboard
        # timeline, so the UI-language contract belongs here for the same
        # reason [CURRENT AGENT]/[RUNTIME] do — it is chrome, not style.
        # ~40 tokens against the 30-50k this mode saves, and nothing at all
        # for installs on the default (auto) language.
        _min_cfg = ctx.KiroCrewConfig.load()
        parts.append(ctx._build_ui_language_section(_min_cfg))
        # A minimal run still calls Kiro Crew's tools, so it gets the same
        # spelling rule; nothing at all on a harness that needs none.
        parts.append(_tool_names.tool_names_block(provider_type))
        logger.debug(
            "Minimal session context: agent=%s, %d chars",
            agent_label,
            sum(len(p) for p in parts),
        )
        return "".join(parts) + essentials

    # Whether this agent gets skills is decided by the injection plan, not by
    # ``is_custom`` alone: a custom agent that maps skills via ``skill://``
    # resources — or, on kiro-cli, one that inherits the default skill trees —
    # DOES receive a skill directory, so logging "skipping skills" for every
    # custom agent misread exactly the case skill-scope debugging cares about.
    # Compute the plan once and reuse it at the skills block below so the log
    # and the injection cannot diverge.
    inject_skills, skill_globs = ctx._skills_injection_plan(
        agent,
        is_cc=is_cc,
        project_dir=project,
        provider_type=provider_type,
        session_key=session_key,
    )
    if is_custom:
        logger.info(
            "Custom agent %r: injecting memory/lessons/rules, %s",
            agent,
            "injecting skills" if inject_skills else "skipping skills",
        )
    else:
        logger.debug("Building session context for kirocrew agent")

    # Section timings: monotonic checkpoints, one per assembled block, so the
    # first-turn build's cost can be attributed per section instead of read as
    # one opaque interval. Flat marks rather than nested timers keep the
    # assembly flow unchanged.
    _marks: list[tuple[str, float]] = [("", time.monotonic())]

    def _mark(label: str) -> None:
        _marks.append((label, time.monotonic()))

    # Critical rules (diff rendering, OPTIONS buttons, absolute-path file
    # links). These are the built-in kirocrew assistant's dashboard/Slack UI
    # contracts and apply to ALL providers — including Claude Code. The
    # dashboard renders clickable input-box options only from the
    # [OPTIONS: ...] text tag (see dashboard/state_parts/slot_transcript.py and the frontend
    # AssistantMessage), so CC must be told to emit it too or the options
    # never render.
    #
    # A CUSTOM app agent can OPT OUT: it ships its own system prompt that
    # defines its own output contract (e.g. an agent that writes prose
    # through its own MCP tools, with no diff block or [OPTIONS:] footer),
    # and injecting the kirocrew assistant's mandates on top both
    # conflicts with that contract and — on a safety-tuned model — reads as
    # an attempt to override the agent's identity, which the model then
    # refuses as prompt injection. The opt-out is per-agent via
    # ``includeCrewContext: false``; DEFAULT is to inject (a plain custom
    # agent with no flag still gets the rules, same as the built-in). The
    # tags still RENDER for any agent that emits them (the dashboard parses
    # them regardless); this only stops the host from MANDATING them where an
    # agent has declared it does not want them.
    if ctx._agent_includes_crew_context(agent):
        append_required(ctx._critical_rules_for(session_key, runtime_source))

    # Current date/time — inject for ALL agents so the LLM knows "today".
    # Honour KiroCrewConfig.timezone (e.g. "Asia/Tokyo") so the LLM sees
    # the user's local time instead of the gateway host's system TZ, which
    # is often UTC on Cloud Desktops and makes "today" ambiguous.

    _, tz = ctx.get_local_tz()
    now = ctx.datetime.now(tz)
    append_required(f"[CURRENT DATE] {now.strftime('%A, %Y-%m-%d %H:%M %Z')}\n\n")

    # Agent identity and runtime — inject for ALL agents so the LLM
    # knows which agent it is and where it's running.  Without this,
    # the LLM cannot distinguish dashboard from kiro-cli and may
    # incorrectly tell the user to "go to the dashboard" when it IS
    # the dashboard.
    #
    # Prefer the trusted per-turn source supplied by the dispatcher. The
    # session-key fallback preserves callers that do not carry one.
    agent_label = agent or "kirocrew"
    if session_key:
        runtime = ctx._runtime_display_name(session_key, runtime_source)
        append_required(_sections.runtime_identity_block(agent_label, runtime))

    # How to spell an MCP tool call on a harness that resolves only qualified
    # names (KAS). Required, not budgeted: without it a worker's first
    # ``work_brief`` call fails and it never reports.
    tool_names = _tool_names.tool_names_block(provider_type)
    if tool_names:
        append_required(tool_names)

    # Crew-member operating mode — injected only for a member's pinned DM
    # session (mode carries the slot's mode; "member" slots are born only
    # through the members thread route), and only where the member backend
    # can mount the session_* tools the block teaches.
    #
    # circular import: members' module graph is heavy and this file
    # sits below it in the layering (the same cycle-break
    # chat_persistence uses for the members module).
    from kiro_crew.members import DM_SLOT_MODE as _member_mode

    # User-profile / skills config, loaded once and ALSO consulted by the
    # member capability gate below — one read per context build.
    _cfg = ctx.KiroCrewConfig.load()

    # Config-driven injection toggles (memory.inject_memory /
    # memory.inject_lessons, with memory.persistence_enabled as the global
    # switch): a group the operator disabled is withheld on EVERY surface —
    # dashboard, channels, cron, heartbeat, task runner, eval, subagents —
    # by intersecting here, the one method all context builds pass through,
    # rather than at the eleven call sites that would each have to remember
    # to pass ``context_groups``. The caller-passed ``context_groups`` keeps
    # driving the [CONTEXT SCOPE] block below: its "Your parent withheld"
    # prose describes subagent narrowing, and a config withholding is the
    # operator's standing choice, not the parent's per-spawn one, so it is
    # deliberately silent there.
    effective_groups = ctx._config_scoped_groups(context_groups, _cfg)

    if mode == _member_mode and ctx._member_dispatch_held(member_dispatch_mounted, _cfg):
        append_required(_member.operating_mode_block(agent_label))

    # Legacy member-DM identity. Private V2 has already derived its owner
    # from the memory binding and reserved its separate envelope. Four
    # layers with distinct ownership, in fixed precedence order (a layer
    # outranks everything injected below it):
    #   1. identity   — derived from the crew's own config, nobody hand-writes it
    #   2. behavior   — product-owned working protocol (the constant below)
    #   3. rules      — user-owned, stored under the gateway-only member-records/
    #                   directory so nothing the member runs can rewrite
    #                   its own safety boundary
    #   4. briefing   — member-owned working memory, agent-writable by design
    #
    # Ordering vs the operating-mode block above: that block is the
    # dispatch-capability mechanics from the per-session session_* mount
    # (product-owned, backend-gated) and reads first so the four layers —
    # including the user-owned rules — rank below product protocol, per
    # the earlier-outranks-later convention of this preamble.
    #
    # FRESH leg of the member lifecycle: reaching this line means a full
    # (non-minimal) session-start build — the minimal path early-returned
    # above — so the verdict comes from the same chokepoint every other
    # delivery branch consults (kiro_crew.members.member_turn_context).
    # Delivery enforces the rules gate: the section builder reads
    # [PERMANENT RULES] fresh and fails closed on an unreadable file.
    if (
        ctx.member_turn_context(member, ctx.MemberLifecycle.FRESH).deliver_section
        and not essentials
    ):
        _member_section = self._build_member_section(
            member, desk_withheld=ctx._desk_withheld(execution_context, desk_member)
        )
        if _member_section:
            append_required(_member_section)
    _mark("member")

    # User profile — onboarding answers (role + technical comfort).
    # Injected for ALL agents like date/agent identity: it describes the
    # person, not the project or workspace. Empty (no block at all) when
    # the user skipped the questions. Uses the ``_cfg`` loaded above the
    # member block, shared with the skills lazy-load gate further down.

    # UI language — a rendering contract like [RUNTIME] above, not a
    # communication-style hint: it tells the model which language the
    # chrome around its tool calls is in. Empty (no block) when the user
    # never picked a language explicitly.
    append_required(ctx._build_ui_language_section(_cfg))
    _mark("preamble")

    # Name any group the parent withheld, before the sections themselves, so
    # the sub-agent reads the scope as framing rather than discovering a gap.
    append_required(ctx._build_context_scope_section(context_groups))

    if ctx._group_included(effective_groups, ctx.CONTEXT_GROUP_LESSONS):
        profile_ctx = ctx._build_user_profile_section(_cfg)
        if profile_ctx:
            parts.append(profile_ctx)
    _mark("profile")

    # Workspace identity — kirocrew-only (custom agents don't use workspaces)
    if not is_custom:
        ws_name = workspace or "default"
        ws_path = ctx.workspace_dir_for(ws_name)
        parts.append(_sections.workspace_identity_block(ws_name, ws_path))
    _mark("workspace")

    # Documentation pointer — kirocrew-only, lightweight reference
    if not is_custom and ctx._group_included(context_groups, ctx.CONTEXT_GROUP_PROJECT):
        docs_ctx = ctx._build_docs_section()
        if docs_ctx:
            parts.append(docs_ctx)
    _mark("docs")

    # Discovery is bounded by default. The legacy setting may request a
    # ranked index, but neither value expands the shared background budget.
    lazy_skills = bool(getattr(_cfg.skills, "lazy_load", False))
    max_context_chars = caps.max_context

    # Steering files from agent config resources.
    # kiro-cli loads an agent's ``resources`` natively when spawned with
    # ``--agent`` (see acp/client_start.py ``_spawn``) — the same mechanism that
    # lets us skip this for custom agents above. The CC backend
    # (claude-agent-acp) does NOT read agent ``resources``, so only it needs
    # the explicit load. Injecting on the ACP/kiro backend would duplicate
    # what kiro-cli already loaded.
    if (
        not essentials
        and not is_custom
        and is_cc
        and ctx._group_included(context_groups, ctx.CONTEXT_GROUP_PROJECT)
    ):
        steering_ctx = ctx._load_steering_resources()
        if steering_ctx:
            append_required(
                "[Steering resources]\n" + steering_ctx + "\n[End of steering resources]\n\n"
            )
    # Folder-inherited steering is NOT appended here. Its frame is in
    # ``_STRUCTURAL_MARKER_RES`` (a forged copy in a channel message or a
    # steering body must not read as operator-selected folder rules), and
    # the caller scrubs this whole tail with _neutralize_structural_markers,
    # so a genuine section placed here would be erased along with any
    # forgery. ``build_message`` mints it right after that scrub, gated on
    # the same conditions (non-member chat, project context group, not a
    # minimal/slim run); member chats carry it inside the essentials
    # envelope built above.
    _mark("steering")

    # Thread conversation history — highest priority context.
    # Use pre-computed LLM compression when available; fall back to truncation.
    # Inject for CC too: a fresh dashboard/Slack session maps to a new CC
    # subprocess with no in-process history, so the thread transcript must
    # be supplied for parity with kiro (which gets it natively).
    if session_key and self.conversation_log and not resumed:
        _history_header = (
            "[THREAD CONVERSATION HISTORY — this is the PRIMARY context.\n"
            "When the user says 'just now', 'earlier', 'the task', 'try again', "
            "or refers to something discussed — ALWAYS look here first. "
            "Do NOT say 'there is no previous context' if content exists below. "
            "Do NOT re-execute past actions unprompted.]\n"
        )
        if compressed_history:

            compressed_history, _ = ctx.redact_exfiltration_urls(compressed_history)
            compressed_history, _ = ctx.redact_credentials(compressed_history)
            compressed_history = ctx._MODE_IDENTITY_RE.sub("", compressed_history)
            logger.info(
                "🔍 build_session_context: session_key=%s LLM-compressed " "history (%d chars)",
                session_key,
                len(compressed_history),
            )
            parts.append(_history_header + compressed_history + "\n[End of thread history]\n\n")
        elif compressed_history is None:
            recent = ctx._recall_rows(
                self.conversation_log,
                session_key,
                conv_max=ctx._RECALL_FALLBACK_MAX_ROWS,
                exclude_last_n=exclude_last_n,
            )
            logger.info(
                "🔍 build_session_context: session_key=%s resumed=%s "
                "conv_log_entries=%d (fallback truncation)",
                session_key,
                resumed,
                len(recent),
            )
            if recent:
                history_block = _replay.thread_history_text(recent, caps)
                if history_block:
                    history_block, _ = ctx.redact_exfiltration_urls(history_block)
                    history_block, _ = ctx.redact_credentials(history_block)
                    parts.append(_history_header + history_block + "\n[End of thread history]\n\n")
    elif session_key and resumed:
        logger.info(
            "🔍 build_session_context: session_key=%s RESUMED — "
            "skipping thread history (kiro-cli has native history)",
            session_key,
        )
    _mark("thread_history")

    # Stop event context — inject notes for recent stop events so the
    # LLM knows prior turns were cancelled by the user.
    if session_key and self.conversation_log:
        _stop_notes = ctx._build_stop_event_notes(self.conversation_log, session_key)
        if _stop_notes:
            append_required(_stop_notes)
    _mark("stop_notes")

    # Memory and lessons: inject for ALL agents (including custom).
    # The user's preferences, project context, and learned corrections
    # are valuable regardless of which agent is running.
    # Temporary sessions skip all memory reads.
    # Two arguments, not one key. A store name and a workspace name are
    # separate namespaces: collapsing them meant a crew bound to store "acme"
    # and a workspace also called "acme" shared one cache slot, so whichever
    # was built first decided where the other one read.
    from kiro_crew.member_essential_context import member_context_identity  # noqa: F811

    private = bool(
        member_context_identity(
            member, member_is_id=bool(execution_context and execution_context.member_id)
        )[0]
    )
    memory, member_vectors, activity_ranked = _store_admission.session_memory_parts(
        self,
        blocks,
        private=private,
        blocks_reads=blocks_reads,
        effective_groups=effective_groups,
        workspace=workspace,
        memory_store=memory_store,
        caps=caps,
        essentials=essentials,
        cfg=_cfg,
        query_text=query_text,
    )
    _mark("memory")

    # Skills. Three cases, in precedence order:
    #
    # 1. The agent template maps skills via ``skill://`` resources. On the
    #    ACP/kiro backend kiro-cli loads those SKILL.md files ITSELF when
    #    spawned with ``--agent`` (acp/client_start.py ``_spawn``), so injecting
    #    them again here would duplicate every mapped skill's content —
    #    exactly the reason the steering block below is CC-only. On the CC
    #    backend (claude-agent-acp) nothing reads agent ``resources``, so
    #    Kiro Crew injects the mapped set itself, scoped by ``only=``.
    # 2. No mapping + the kirocrew agent -> bounded discovery and project bodies.
    # 3. No mapping + a custom agent -> on kiro-cli, the skill trees it
    #    inherits (kiro-cli's own inheritance is off on native launches);
    #    elsewhere nothing.
    #
    # The section budget makes the loader inject a usage-ranked top-K of
    # on-demand skills (plus always:true pinned) and leave the tail to
    # skill_search, keeping the block bounded instead of dumping every
    # skill's summary. The slice below is a defensive backstop only.
    # Mapped agents get scoped discovery on both backends. Unmapped: kirocrew only.
    # ``inject_skills``/``skill_globs`` come from the plan computed at the
    # section top, so the "skipping skills" log and the injection here read the
    # same decision. Shared with the post-compaction re-injection in build_message.
    if inject_skills:
        required_skills, skills_ctx = _inclusion.skill_parts(
            self, globs=skill_globs, project=project, caps=caps, lazy_skills=lazy_skills
        )
        for required_skill in required_skills:
            append_required(required_skill)
        if skills_ctx:
            append_required(skills_ctx)
    _mark("skills")

    # Lessons: injected for ALL agents (skipped for temporary sessions), gated
    # by the same project scope the skill loader applies; the store that
    # answers is chosen by population, not by what a render returned.
    lessons_renderer, lessons_part_index = _store_admission.session_lessons_part(
        self,
        blocks,
        memory=memory,
        member_vectors=member_vectors,
        activity_ranked=activity_ranked,
        effective_groups=effective_groups,
        workspace=workspace,
        memory_store=memory_store,
        project=project,
        caps=caps,
        essentials=essentials,
        query_text=query_text,
    )
    # V2 essential rules are query-free; V1 retains its query-ranked lessons.
    _mark("lessons")

    # Source snippets may exist only in this conversation's log, not in the
    # vector index. Retain that already-bounded base projection verbatim.
    if (
        session_key
        and self.conversation_log
        and not blocks_reads
        and ctx._group_included(effective_groups, ctx.CONTEXT_GROUP_MEMORY)
    ):
        provenance = self.conversation_log.recent_with_provenance(
            session_key, exclude_last_n=exclude_last_n
        )
        if provenance:
            append_required(
                "## Recent Session Context\n"
                + "\n".join(
                    f"- [thread {p['source_thread']}, {p['ts'][:16]}] {p['snippet']}"
                    for p in provenance
                )
                + "\n\n"
            )
    _mark("provenance")

    # If a later protected block consumed part of the model-safe allowance,
    # re-render lessons against the exact remaining room. Preferences,
    # identity, and safety rules are never sliced to make space.
    protected_chars = _store_admission.refit_lessons(
        blocks,
        essentials=essentials,
        caps=caps,
        renderer=lessons_renderer,
        part_index=lessons_part_index,
    )
    if session_key and _cfg.memory.inject_lessons_per_turn:
        # The block as sent, after any trim: per-message lessons skip what it holds.
        self._remember_startup_lessons(
            session_key, parts[lessons_part_index] if lessons_part_index is not None else ""
        )

    # Admit background as whole source blocks, never by slicing the joined
    # prompt. Protected rules/preferences are outside this discretionary pool.
    context = _budgets.admit_background(
        blocks,
        protected_chars=protected_chars,
        max_context_chars=max_context_chars,
        caps=caps,
        compressed_history=compressed_history,
    )

    if essentials:
        prefix = next(
            (
                r
                for r in (ctx._CRITICAL_RULES, ctx._CRITICAL_RULES_CHANNEL)
                if context.startswith(r)
            ),
            "",
        )
        context = prefix + essentials + context[len(prefix) :]

    logger.debug(
        "Session context: agent=%s, custom=%s, %d chars",
        agent or "kirocrew",
        is_custom,
        len(context),
    )
    _mark("finalize")
    _emit_context_section_timings(
        _marks,
        scope="build_session_context",
        is_custom=bool(is_custom),
        total_chars=len(context),
    )
    return context
