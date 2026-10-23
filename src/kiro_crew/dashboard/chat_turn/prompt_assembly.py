"""Steps that build a dashboard turn's prompt around the user's message, and the prompt
and skill mentions it expands.

The prompt phase ``_turn_build_prompt`` runs the steps in order and makes the
ContextBuilder call; each step reads what it is handed and returns what it adds. The
context-composition record is written in ``_turn_dispatch_gates``, once the gates pass.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.dashboard.chat_runner import (
        _BUSY_RECOVER_MSG,
        _CONN_RECOVER_MSG,
        _MANUAL_RESUME_MSG,
        MAX_PROMPT_BYTES,
        TURN_OPENING_INJECT_KINDS,
        DashboardState,
        FileTooLargeError,
        _apply_incognito_prefix,
        _ChatSlot,
        _find_prompt,
        _get_skills,
        _maybe_inject_persona,
        _prewarm_startup_query,
        _prompt_read_within_root,
        _resolve_folder_steering_dirs,
        consume_reinjection,
        context_omissions,
        is_sensitive_path,
        logger,
        prepare_store_vectors,
        refresh_tag_grants_cache,
        resolve_board_tags,
        run_in_embed_pool,
        safe_read_file_bytes_nolink,
        sel,
        served_provider_label,
        session_skill_globs,
        slot_steering_principal,
        user_text_span,
        validate_file_path,
    )
    from kiro_crew.dashboard.chat_turn.context_cap import (
        CONTEXT_CAP_TAIL_CHARS,
        _context_cap_seed_head,
        _drop_context_cap_seed,
        _take_context_cap_seed,
    )
    from kiro_crew.dashboard.chat_turn.queue_drain import settle_drained_subagent_notices
    from kiro_crew.dashboard.chat_turn.turn_context import (
        _detach_appended_context,
        _folder_steering_turn,
        drain_pending_context,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


async def _session_replay_history(
    state: DashboardState,
    context_builder: ContextBuilder,
    slot: _ChatSlot,
    client: Any,
    session_key: str,
    *,
    _context_is_new: bool,
    _provider_has_history: bool,
    _current_replay_message: dict | None,
) -> str | None:
    """The provider-agnostic history replay a cold-start turn carries, or ``""``.

    Built off the loop from the conversation log plus the slot's live tail, unless
    the provider resumed its own native session or an explicit conversation reset
    asked for a cold start without history.
    """
    compressed: str | None = ""
    # Provider-agnostic session replay: Kiro Crew's conversation_log
    # is the canonical history source. Skip only when the provider
    # successfully resumed its own native session (same provider,
    # full-fidelity history already loaded via ACP session/load).
    if _context_is_new and not _provider_has_history:
        # Consumed HERE rather than before the branch, so only a real cold
        # start can spend the flag: a warm turn that never rebuilds history
        # must not burn the one chance the reset asked for.
        if state.sessions.consume_replay_suppression(session_key):
            # Suppression arm of the FRESH first-turn history debt: a reset asked
            # to forget, so there is no history to deliver and nothing to re-queue.
            # Pay the debt NOW, unconditionally — even if this turn then ends
            # pre-token, the re-queue must not replay the conversation the reset
            # dropped. (The build arm's debt is settled by the finally in
            # ``_run_chat``, gated on ``_first_turn_history_assembled`` and a
            # durably-kept turn, because its replay only reaches the provider when
            # the stream starts.)
            state.sessions.consume_first_turn_history_owed(session_key)
            # The reset dropped what a context-cap trim before it would have kept.
            _drop_context_cap_seed(session_key)
            logger.info(
                "Session replay suppressed by an explicit conversation reset: %s",
                session_key,
            )
            compressed = ""
        else:
            from kiro_crew.context import (  # circular: context -> chat
                build_session_replay,
                window_for_provider_client,
            )

            # The cold start a conductor context-cap trim began (context_cap)
            # replays a longer tail, behind the trim note and the ledger snapshot.
            _trim_seed = _take_context_cap_seed(session_key)
            _budget = {"budget_chars": CONTEXT_CAP_TAIL_CHARS} if _trim_seed else {}
            # Merge the disk transcript and a frozen live-window tail
            # before one budget pass. Exclude this request by identity,
            # whether or not the periodic flush has persisted it yet.
            compressed = (
                await asyncio.to_thread(
                    build_session_replay,
                    context_builder.conversation_log,
                    session_key,
                    pending_messages=list(slot.messages),
                    current_message=_current_replay_message,
                    model_window=window_for_provider_client(client),
                    **_budget,
                )
                or ""
            )
            if _trim_seed and compressed:
                compressed = await _context_cap_seed_head(session_key) + compressed
            logger.info(
                "Session replay: key=%s result=%s",
                session_key,
                f"{len(compressed)} chars" if compressed else "None (no history)",
            )
    return compressed


async def _scrub_dashboard_prefix(full_message: str, _trusted_prompt_tail: str | None) -> str:
    """*full_message* with every structural marker in its dashboard-added prefix neutralized.

    ContextBuilder's own prompt is the trusted tail; everything the dashboard
    prepended after it is scrubbed in one off-loop pass. A prompt whose tail is no
    longer ContextBuilder's is scrubbed whole, failing safe.
    """
    from kiro_crew.context import (  # circular: context -> dashboard.chat
        _neutralize_structural_markers,
    )

    if _trusted_prompt_tail is None:
        full_message = await asyncio.to_thread(
            _neutralize_structural_markers,
            full_message,
        )
    elif full_message.endswith(_trusted_prompt_tail):
        prefix_end = len(full_message) - len(_trusted_prompt_tail)
        if prefix_end:
            safe_prefix = await asyncio.to_thread(
                _neutralize_structural_markers,
                full_message[:prefix_end],
            )
            full_message = safe_prefix + _trusted_prompt_tail
    else:
        # Every post-builder mutation ``_run_chat`` makes before this scrub is
        # documented as a pure prepend. If that invariant changes, fail safe by removing all
        # structural authority rather than preserving an unknown copy.
        logger.warning(
            "prompt boundary: ContextBuilder prompt is no longer the "
            "final prompt tail; neutralizing every structural candidate"
        )
        full_message = await asyncio.to_thread(
            _neutralize_structural_markers,
            full_message,
        )
    return full_message


async def _checklist_resync(
    slot: _ChatSlot,
    full_message: str,
    *,
    _context_is_new: bool,
    _provider_has_history: bool,
    _todo_sync_rendered: tuple[tuple[str, str, bool], ...],
    _todo_recovery_carried: bool,
) -> tuple[str, tuple[tuple[str, str, bool], ...], bool]:
    """Prefix the checklist the agent must re-learn, and what the turn now owes.

    A cold start the provider did not resume gets the whole recovery block; a warm
    turn gets only the rows the person ticked since the agent's last snapshot.
    Returns the prompt and the turn's ``(sync_rendered, recovery_carried)`` debt,
    settled on the provider's first event.
    """
    # The cold-start `is_new` observation is a one-shot the session claim
    # consumes, so a turn that builds the recovery block but dies before
    # the provider's first event (a pre-dispatch Stop, an expired
    # non-persistent session) would never deliver it and later turns,
    # seeing is_new=False, would omit it — leaving the agent's empty list
    # diverged for good. `slot.todo_recovery_pending` keeps the trigger
    # armed across such an abort; it is cleared on the first provider
    # event of ``_run_chat``'s stream, once delivery is confirmed.
    if (_context_is_new and not _provider_has_history) or slot.todo_recovery_pending:
        # The rebuild's `create` echoes an all-open list; pin the rows
        # already done so that echo cannot erase them if the turn dies
        # before the agent's `complete` calls.
        slot.pin_completed_todo_rows()
        _todo_resync = slot.todo_recovery_prompt()
        if _todo_resync:
            # Owed until the provider accepts it (first event), not here:
            # the dispatch gates in ``_run_chat`` can still abort before the prompt is sent.
            slot.mark_todo_recovery_pending()
            _todo_recovery_carried = True
    else:
        _todo_resync = slot.todo_sync_prompt()
        # Marked stated on the provider's first event, not here: the
        # dispatch gates in ``_run_chat`` can still abort before the prompt is sent.
        _todo_sync_rendered = slot.todo_sync_rendered if _todo_resync else ()
    if _todo_resync:
        full_message = f"{_todo_resync}\n\n{full_message}"
    return full_message, _todo_sync_rendered, _todo_recovery_carried


def _strip_yaml_frontmatter(content: str) -> str:
    """Strip a leading YAML frontmatter block from prompt/SOP *content*.

    Frontmatter carries display metadata (title, description) for the prompt
    library UI; only the body is meant to reach the model, so injecting the
    block would leak that metadata into the agent turn. Recognized only when
    the first line is exactly ``---`` (an optional UTF-8 BOM is tolerated) and
    removed through the next line that starts with ``---`` or is exactly
    ``...``, plus any blank lines that follow the terminator. Deliberately
    line-based — no YAML parser — so untrusted prompt files are never parsed,
    and fail-open: with no terminator the whole file is treated as body and
    returned unchanged rather than silently dropping content on malformed
    frontmatter.

    A fence LOCATOR, not a field parser — deliberately outside
    ``kiro_crew.frontmatter`` (same stance as ``SkillsLoader.strip_frontmatter``).
    The grammar that DECIDES what the prompt library shows as frontmatter is
    ``frontmatter._COLUMN0_BLOCK_RE`` (the ``column0_fence`` extraction, reached
    via ``_extract_sop_description`` → ``SkillsLoader._parse_frontmatter``),
    whose closer only has to start with ``---`` — so this closer test mirrors
    that, or a ``--- `` / ``---junk`` closer would display as metadata yet be
    injected verbatim, reintroducing the leak. Editing either grammar means
    revisiting the other. This locator strips a superset on purpose (BOM/CRLF
    openers, a ``...`` closer): where the two disagree, erring toward stripping
    withholds display metadata from the model, never body the UI treats as
    content.
    """
    text = content.removeprefix("\ufeff")
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return content
    for idx in range(1, len(lines)):
        probe = lines[idx].rstrip("\r").rstrip()
        if probe.startswith("---") or probe == "...":
            body_start = idx + 1
            while body_start < len(lines) and not lines[body_start].strip():
                body_start += 1
            return "\n".join(lines[body_start:])
    return content


def _resolve_prompt_mention(
    message: str,
    project_dir: Path | None,
) -> tuple[str, str, str]:
    """Resolve and READ ``@prompt-name rest``, touching no shared state.

    Returns ``(expanded_message, "ok", chip)`` if a prompt was resolved,
    ``(original_message, "blocked", "")`` if blocked by sensitive-path check,
    ``(original_message, "too_large", "")`` if file exceeds size limit, or
    ``(original_message, "not_found", "")`` if no match. *chip* is the
    user-visible "Loaded prompt" line the caller is expected to surface, empty
    unless the status is ``ok``.

    Returning the chip rather than appending it is what makes this function safe
    to run in ``asyncio.to_thread``: ``slot.append`` sets an ``asyncio.Event``,
    whose ``set()`` resolves loop-owned futures through the loop's
    non-thread-safe ``call_soon``, and ``push_slots_update`` broadcasts. Neither
    belongs on a worker thread, so the split is by which THREAD may do the work
    rather than by taste: everything here is filesystem and CPU work over a
    caller-supplied path, and it can see neither the slot nor the state. Its two
    callers — :func:`_expand_prompt_mention` on the loop's own thread and
    :func:`_expand_prompt_mention_off_loop` from a worker — surface the chip
    themselves, both on the loop.
    """
    if not message.startswith("@"):
        return message, "not_found", ""

    # Parse @name from start of message — name ends at first whitespace or EOL
    body = message[1:]  # strip leading @
    parts = body.split(None, 1)
    mention = parts[0] if parts else body
    user_text = parts[1].strip() if len(parts) > 1 else ""

    try:
        match = _find_prompt(mention, project_dir)
    except Exception:
        return message, "not_found", ""
    if not match:
        return message, "not_found", ""

    if is_sensitive_path(match["path"]):
        return message, "blocked", ""

    # Read the path the resolver CANONICALIZED, and re-check it there. The check
    # above tests the name as addressed, which for a link is not the file a read
    # by that name would return — so a project shipping
    # ``.kiro/prompts/creds.md -> ~/.aws/credentials`` would pass a check on the
    # link and have its target injected into the turn.
    resolved = validate_file_path(match["path"])
    if resolved is None:
        return message, "blocked", ""

    # Read through the same hardlink-rejecting gate as the scoped HTTP read
    # rather than by name: validating a path and then opening that name leaves a
    # window in which the final component is swapped for a link, so the bytes
    # injected into the turn are not the bytes any check ran against. The gate
    # opens FIRST with ``O_NOFOLLOW`` and validates the descriptor it actually
    # read — a leaf swapped for a symlink cannot be opened at all, and
    # ``st_nlink > 1`` or a non-regular inode is refused — so the inode checked
    # is the inode injected. Every entry reaching here was minted by the listing
    # gate, which refuses a link outright, so this closes the swap window rather
    # than a standing hole.
    #
    # ``within_root`` comes from the shared `_prompt_read_within_root`, which both
    # HTTP detail branches also use — the root that pins an entry is a property of
    # the entry, so deriving it per reader is how two readers of one directory come
    # to disagree. For a user-scope entry it re-runs that scope's own root gate, so
    # the read confines itself to a root something has checked since the entry was
    # minted rather than to one assembled here; ``None`` there means the root is no
    # longer serveable and the read is REFUSED rather than pinned inside the
    # directory a swap named. See that function for the residual window that a
    # path-based ``within_root`` cannot close, and for why a package SOP gets the
    # canonical path's own parent instead.
    read_root = _prompt_read_within_root(match, project_dir, resolved)
    if read_root is None:
        return message, "not_found", ""
    try:
        raw = safe_read_file_bytes_nolink(
            resolved,
            within_root=read_root,
            max_bytes=MAX_PROMPT_BYTES,
        )
    except FileTooLargeError:
        logger.warning(
            "Prompt %s exceeds max size (%d bytes)",
            mention,
            MAX_PROMPT_BYTES,
        )
        return message, "too_large", ""
    if raw is None:
        # The gate refuses and reads through one descriptor, so it cannot say
        # which of the two happened — and both are a prompt this turn does not
        # get. Reported as the miss the plain read already reported for an
        # unreadable file, so a refusal reveals nothing a link's target could be
        # probed with.
        return message, "not_found", ""
    content = raw.decode("utf-8", errors="replace")
    # Strip display-metadata frontmatter BEFORE redaction and the char count,
    # so both the redaction pass and the user-visible "Loaded prompt … chars"
    # line operate on exactly what the agent receives.
    content = _strip_yaml_frontmatter(content)

    from kiro_crew.security import redact

    content = redact(content)

    # Inject SOP as instructions the agent must follow
    expanded = f"Execute the following instructions:\n\n{content}"
    if user_text:
        expanded += f"\n\n---\nAdditional context from user: {user_text}"

    # Show the user what happened — handed back for the caller to surface on the
    # loop, since this may be running on a worker thread.
    return expanded, "ok", f"📜 Loaded prompt **@{match['fullName']}** ({len(content):,} chars)"


def _slot_prompt_project(slot: _ChatSlot, project: str | None = None) -> Path | None:
    """The project *slot*'s ``local`` prompts resolve against, or ``None``.

    Read on the loop by both entry points below and passed into the resolver, so
    the on-loop and off-loop expansions can never drift on where "local" is for a
    given chat — the property the HTTP prompt surface is documented to match.
    An explicit *project* (a running turn's start-of-turn snapshot, ``""`` for
    none) wins over the live ``slot.project``, which a pick made mid-reply may
    already have moved.
    """
    root = slot.project if project is None else project
    return Path(root) if root else None


def _surface_prompt_chip(state: DashboardState, slot: _ChatSlot, chip: str) -> None:
    """Append *chip* to the slot and broadcast, on the event loop's own thread.

    Both sinks are loop-owned: ``slot.append`` ends in ``slot.event.set()``, an
    ``asyncio.Event`` whose waiters are resolved through the loop's
    ``call_soon`` — not its threadsafe variant — so a foreign-thread caller
    queues a callback the loop is never woken for, and raises outright under
    ``asyncio`` debug mode. Every caller therefore runs this after its own
    ``await``, never inside the worker.
    """
    if not chip:
        return
    slot.append("system", chip, "msg msg-info")
    state.push_slots_update()


def _expand_prompt_mention(
    message: str,
    state: DashboardState,
    slot: _ChatSlot,
) -> tuple[str, str]:
    """Expand ``@prompt-name rest`` into SOP content + user instructions.

    Returns ``(expanded_message, "ok")`` if a prompt was resolved,
    ``(original_message, "blocked")`` if blocked by sensitive-path check,
    ``(original_message, "too_large")`` if file exceeds size limit, or
    ``(original_message, "not_found")`` if no match.

    Local prompts resolve against THIS chat slot's project (per-slot), the same
    directory the slot's agent runs in, so an ``@mention`` of a local prompt
    matches the caller's checkout rather than a gateway-global dir. A slot with
    no project resolves to ``None`` -> local prompts simply are not matched
    (fail-closed), the same as when there is no gateway project. It reads
    ``slot.project`` directly (this slot, no cross-slot fallback) — the SAME
    question the HTTP prompt surface asks via ``requesting_slot_project`` — so
    the chat and HTTP surfaces agree on where "local" is for a given chat.

    This is the ON-LOOP entry point, kept for callers that are already on a
    thread allowed to touch the slot. A coroutine must use
    :func:`_expand_prompt_mention_off_loop` instead: the resolve-and-read half
    is filesystem work under a directory the gateway does not own.
    """
    expanded, status, chip = _resolve_prompt_mention(message, _slot_prompt_project(slot))
    _surface_prompt_chip(state, slot, chip)
    return expanded, status


def _expand_dollar_skills(
    message: str,
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    project: str,
) -> tuple[str, int]:
    """Expand ``$skillname`` tokens anywhere in *message* into appended skill bodies.

    Leaves the literal ``$token`` in place (decision (a)) and appends a
    ``[Skill: name]`` block per resolved skill after the user's message, so the
    agent sees both the user's intent marker and the loaded procedure. Unknown
    tokens are left untouched. The transcript row snapshots each redacted,
    frontmatter-free body in ``meta.skills`` so the dashboard can show exactly
    what this turn loaded even if the source skill changes or disappears later.

    Resolution + security live in ``SkillsLoader.resolve_dollar_skills`` (allowlist
    match, no path construction — per input-validation guidance). This function adds
    the runner-side concerns: redaction of the loaded content, a user-visible chip,
    and SEL audit.

    Returns ``(expanded_message, count)`` where *count* is the number of skills
    appended (0 if none resolved).

    *project* is the project to resolve against (``""`` for none): the running
    turn's start-of-turn snapshot, so a pick made mid-reply cannot load another
    project's skills into this reply.
    """
    if "$" not in message:
        return message, 0
    skills = _get_skills(state)
    project_dir = project or None
    try:
        served = state.sessions.get_provider(session_key) if session_key else None
        only = session_skill_globs(
            session_key,
            slot.agent or "kirocrew",
            project_dir=project_dir,
            provider_type=served_provider_label(served),
        )
        resolved = skills.resolve_dollar_skills(message, project_dir, only=only)
    except Exception:
        logger.exception("dollar-skill resolution failed")
        # Audit the failed resolution attempt — the security-controls guideline
        # requires every tool invocation/permission decision to emit a SEL event,
        # including failures (mirrors the prompt-expansion not_found/error path).
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name="skill_dollar_expansion",
            tool_kind="prompt",
            outcome="error",
            metadata={"reason": "exception", "slot": slot.key},
        )
        return message, 0
    if not resolved:
        if skills.has_dollar_candidate(message):
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="skill_dollar_expansion",
                tool_kind="prompt",
                outcome="not_found",
                metadata={"slot": slot.key},
            )
        return message, 0

    blocks: list[str] = []
    names: list[str] = []
    snapshots: list[dict[str, str]] = []
    for _token, name, body in resolved:
        from kiro_crew.security import redact

        body = redact(body)
        blocks.append(f"[Skill: {name}]\n\n{body}")
        names.append(name)
        snapshots.append({"name": name, "body": body})

    expanded = message + "\n\n" + "\n\n---\n\n".join(blocks)

    slot.append(
        "system",
        f"📎 Loaded skill(s) via `$`: **{', '.join(names)}**",
        "msg msg-info",
        meta={"kind": "skill_load", "skills": snapshots},
    )
    state.push_slots_update()
    return expanded, len(names)


async def _turn_build_prompt(turn: _ChatTurn) -> None:
    """Build the prompt the turn sends: a slash command's, the context builder's or the bare message."""
    _trusted_prompt_tail: str | None
    slot_ctx_omitted: dict[str, dict[str, Any]]
    _context_is_new = turn._context_is_new
    _current_replay_message = turn._current_replay_message
    _is_quick_prompt = turn._is_quick_prompt
    _is_synthetic = turn._is_synthetic
    _jev_route_text = turn._jev_route_text
    _provider_has_history = turn._provider_has_history
    _request_prefix_context = turn._request_prefix_context
    _responsive_recovery = turn._responsive_recovery
    _synthetic_recovery_turn = turn._synthetic_recovery_turn
    _turn_project = turn._turn_project
    _user_span = turn._user_span
    client = turn.client
    execution_context = turn.execution_context
    first_word = turn.first_word
    is_new = turn.is_new
    is_slash = turn.is_slash
    kiro_agent = turn.kiro_agent
    memory_store = turn.memory_store
    message = turn.message
    prompt_expanded = turn.prompt_expanded
    provider_name = turn.provider_name
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    user_typed_len = turn.user_typed_len
    if is_slash:
        full_message = message
        turn.full_message = full_message
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name="slash_command",
            tool_kind="slash",
            outcome="bypass",
            metadata={"command": first_word, "slot": slot.key},
        )
    elif state.context_builder:

        # Length of the message as the user's text stands NOW (after any
        # @prompt/$skill expansion, before the context this branch prepends).
        # The difference against len(message) at build_message time is how
        # far the user's text was pushed down — its offset for split_blocks.
        _core_msg_len = len(message)

        # Record that a non-slash context-bearing turn ASSEMBLED its history
        # replay — this is the ``elif state.context_builder:`` arm, so a slash
        # command (the ``if is_slash:`` arm above) never reaches it. The build
        # arm's replay only reaches the provider when the stream starts, so a
        # pre-output backend 5xx fails AFTER assembly and the re-queue needs the
        # debt — _run_chat's finally settles it (gated on this flag) only once the
        # provider durably keeps the prompt. The suppression arm (a reset asked
        # to forget) pays the debt directly inside the helper.
        if _context_is_new and not _provider_has_history:
            _first_turn_history_assembled = True
            turn._first_turn_history_assembled = _first_turn_history_assembled
        compressed = await _session_replay_history(
            state,
            state.context_builder,
            slot,
            client,
            session_key,
            _context_is_new=_context_is_new,
            _provider_has_history=_provider_has_history,
            _current_replay_message=_current_replay_message,
        )
        # After a soft-cancel, kiro-cli drops the cancelled turn from its
        # conversation log — but everything BEFORE the cancel is preserved.
        # Re-inject just the cancelled turn (user prompt + partial assistant)
        # as a preamble so the LLM remembers what was interrupted, without
        # duplicating older history. Flag lives on the session (set by
        # SessionManager.stop_turn), consumed one-shot here. Use getattr
        # for prev_turn_cancelled so test doubles don't raise on access.
        _session = getattr(state.sessions, "_sessions", {}).get(session_key)
        if _session is not None and getattr(_session, "prev_turn_cancelled", False):
            _session.prev_turn_cancelled = False
            if (
                not _responsive_recovery
                and state.context_builder
                and state.context_builder.conversation_log
            ):
                from kiro_crew.context import (
                    build_cancelled_turn_preamble,  # circular: context -> dashboard.chat -> chat_runner (can't top-level: context imports chat at module load); circular: context -> chat -> chat_runner; circular: context -> chat
                )

                preamble = build_cancelled_turn_preamble(
                    state.context_builder.conversation_log, session_key
                )
                if preamble:
                    message = preamble + "\n\n" + message
                    turn.message = message
        # A turn that CONTINUES an interrupted one, on a session this turn
        # natively resumed (session/load). kiro-cli logs a prompt only once it
        # is answered, so the turn cut off by the process dying is missing
        # from what the load restored, and a resumed session gets no replay
        # above. Re-inject that turn from the slot's own transcript, or the
        # model is told to finish a request it cannot see.
        if (
            is_new
            and _provider_has_history
            and _synthetic_recovery_turn
            and message in (_MANUAL_RESUME_MSG, _CONN_RECOVER_MSG, _BUSY_RECOVER_MSG)
        ):
            from kiro_crew.context import (  # circular: context -> chat
                build_interrupted_turn_preamble,
            )

            preamble = build_interrupted_turn_preamble(
                list(slot.messages),
                current=_current_replay_message,
                opener_inject_kinds=TURN_OPENING_INJECT_KINDS,
            )
            if preamble:
                logger.info(
                    "Restoring the interrupted turn for natively resumed session %s",
                    session_key,
                )
                # Minted after the egress scrub in _turn_dispatch_gates, which would
                # otherwise neutralize its own (registered) frame.
                _interrupted_turn_preamble = preamble
                turn._interrupted_turn_preamble = _interrupted_turn_preamble
        logger.info("🔍 Chat slot=%s is_new=%s mode=%r", slot.key, is_new, slot.mode)
        # Drain any pending subagent delivery failures so the LLM knows
        # about timed-out results and can read them from disk.
        if slot._pending_subagent_failures:
            failures = slot._pending_subagent_failures[:]
            slot._pending_subagent_failures.clear()
            settle_drained_subagent_notices(state, slot, failures)
            message = "\n\n".join(failures) + "\n\n" + message
            turn.message = message
        # Save raw user message before context/persona prepend for Slack
        # mirror — avoids leaking injected context to the linked thread.
        _user_msg_for_mirror = message
        turn._user_msg_for_mirror = _user_msg_for_mirror
        # Drain pending context injections (silent background context
        # from apps/subagents).  Expired entries are discarded.
        _ctx_prefix = drain_pending_context(slot)
        if _ctx_prefix:
            message = _ctx_prefix + message
            turn.message = message
        # Use resolved kiro agent name (e.g. "kirocrew"), not the slot
        # name (e.g. "default"), so build_message's is_custom check
        # correctly identifies kirocrew sessions and enables skills.
        # Generated $skill/persona bytes travel through
        # request_prefix_context below; ``message`` itself therefore keeps
        # the actual current request at its tail.
        # Folder breadcrumb: inject once per session, and again after a
        # folder move (no session reset — it's just a label refresh).
        folder_path = None
        if _context_is_new or slot._folder_changed:
            folder_path = state.folder_breadcrumb(slot.folder_id) or None
            slot._folder_changed = False
        # Board tags: resolve the slot's tag ids to (id, agent-policy) via
        # the live vocabulary so the [BOARD] context line can list the tags
        # and which are agent-writable. Canonical IDs, never the free-form
        # ``name`` field: names are agent-writable prose and an
        # instruction-shaped name must never enter trusted model context;
        # ids are also the exact handles the ``chat_tag``
        # directive consumes. Best-effort — a resolution failure must never
        # break message assembly.
        board_tags: list[tuple[str, str]] | None = None
        try:
            _slot_tags = list(getattr(slot, "tags", None) or [])
            if _slot_tags:
                # Grants-store read+parse off the loop; the per-tag
                # resolutions below serve from the warmed cache.
                await asyncio.to_thread(refresh_tag_grants_cache)
                # Row-backed resolution: only ids with a protected grant
                # row reach the trusted rail — see resolve_board_tags.
                board_tags = resolve_board_tags(_slot_tags, state._tags) or None
        except Exception:
            logger.debug("board_tags resolution failed", exc_info=True)
        _color_theme = getattr(slot, "color_theme", "")
        # Governance gate: installed-pack persona injection
        # is a governable capability. A policy can force-disable it wholesale
        # (default-allow standalone). Only consult when a persona could
        # actually be injected (new turn + installed "custom-" theme) to
        # avoid a governance call on every ordinary turn. fail_closed=True:
        # unlike the neighboring capability sites (spawn/messaging/...),
        # which have always-on chokepoint checks behind governance, this
        # gate is the ONLY enforcement of the enterprise persona
        # off-switch — a degraded permissive Decision would silently
        # bypass a policy that disables capabilities.theme_persona.
        # A governance-evaluation error therefore denies (persona skipped
        # for that turn; the chat itself is unaffected).
        _persona_permitted = True
        if _context_is_new and isinstance(_color_theme, str) and _color_theme.startswith("custom-"):
            from kiro_crew.platform.governance_profiles import governance_permits

            _decision = governance_permits(
                "capabilities.theme_persona",
                "",
                session_key=session_key,
                log_warning=False,
                fail_closed=True,
            )
            _persona_permitted = getattr(_decision, "permitted", False)
            if not _persona_permitted:
                logger.info(
                    "theme persona injection skipped: capabilities."
                    "theme_persona denied by governance policy"
                )
        if _persona_permitted:
            persona_message = _maybe_inject_persona(
                message,
                _color_theme,
                _context_is_new,
                theme_consent_sha=getattr(slot, "theme_consent_sha", None),
            )
            message, persona_context = _detach_appended_context(message, persona_message)
            turn.message = message
            _request_prefix_context += persona_context
            turn._request_prefix_context = _request_prefix_context
        # Scale the injected-context budget to the active model's context
        # window so a 200K model gets one-fifth the memory/lessons/history
        # chars a 1M model gets (same share of the window). Resolve from the
        # live session client (prefers its usage-reported window, else its
        # resolved model id) — the same helper Slack uses, so both surfaces
        # share one strategy. Unset/Auto ⇒ None ⇒ the 1M reference (unchanged
        # default behavior).
        from kiro_crew.context import window_for_provider_client  # circular: context -> chat

        model_window = window_for_provider_client(client)
        # Everything this branch PREPENDED (cancelled-turn preamble,
        # subagent failures, drained pending context) sits between the
        # request header and the user's text; record its current length.
        # Generated skill/persona context is passed separately and never
        # shifts the authoritative user slice.
        _user_prepend_offset = max(0, len(message) - _core_msg_len)
        turn._user_prepend_offset = _user_prepend_offset
        # build_message resolves where the user's own text ENDS UP (it owns
        # the hook rewrite, the neutralization and the multibyte fold) and
        # writes the exact bounds into _user_span.
        # build_message performs blocking work (episodic query embed via
        # urllib to Ollama, file reads) — run off-loop (mc-embed bulkhead)
        # so a slow embedding endpoint can't stall the gateway event loop.
        # A compaction on the PREVIOUS turn dropped the session-start
        # context, taking the skills index with it. Read-and-clear the flag
        # here so this turn re-injects the index exactly once.
        _needs_reinjection = consume_reinjection(state.sessions, session_key)
        turn._needs_reinjection = _needs_reinjection
        # Folder steering directories, resolved LIVE from the committed
        # folder tree rather than from a value cached on the slot, so a
        # folder edit or a re-file reaches the chats already inside it at
        # their next session-start context with no cache to invalidate.
        # For a template chat only the two turns that carry session-start
        # context read the tree (a fresh provider session, or a reinjection
        # after compaction); warm turns never touch it. A V2 MEMBER chat is
        # different: build_message rebuilds the member's essentials
        # envelope on EVERY turn, and that envelope declares itself the
        # complete replacement for all prior snapshots ("do not keep
        # applying removed sources"). Folder steering rides inside that
        # envelope, so a warm member turn that passed no directories would
        # hand the model a snapshot that silently withdraws the folder's
        # guides. Resolve on every turn that rebuilds the envelope. The
        # resolver re-validates every stored path — stat calls and
        # realpath — so it runs off-loop. A resolution error degrades to
        # no folder steering and a warning naming the slot; it must never
        # fail the turn.
        _folder_steering_dirs: tuple[str, ...] = ()
        if slot.folder_id and _folder_steering_turn(
            slot,
            execution_context,
            context_is_new=_context_is_new,
            provider_has_history=_provider_has_history,
            needs_reinjection=_needs_reinjection,
        ):
            _folder_snapshot = await state.read_folders(
                lambda folders: [dict(folder) for folder in folders]
            )
            _resolved_dirs, _steering_err = await asyncio.to_thread(
                _resolve_folder_steering_dirs,
                _folder_snapshot,
                slot.folder_id,
                slot_app=slot_steering_principal(slot, execution_context),
            )
            if _steering_err:
                logger.warning(
                    "Folder steering unavailable for slot %s: %s", slot.key, _steering_err
                )
            else:
                _folder_steering_dirs = tuple(_resolved_dirs)
        # Stand up this crew's OWN vector store before the offloaded build.
        # It has to happen here, on the loop, because init() is blocking file
        # IO (sqlite connect, migrations, a FAISS load) that build_message's
        # sync resolver may not perform. A no-op for the default store.
        #
        # Learned V2 memory is optional here: an unavailable member database
        # leaves manual essentials available and the builder reports that
        # learned memory could not be loaded. Temporary turns skip preparation.
        # Legacy V1 named stores retain their Markdown/keyword fallback.
        if not slot.blocks_reads:
            try:
                await prepare_store_vectors(
                    state.context_builder, memory_store, session_key=session_key
                )
            except (OSError, ValueError):
                if execution_context is None or execution_context.member_id is None:
                    raise
                # The builder keeps essentials and reports unavailable learned memory.
                logger.info("Member learned memory unavailable for context preparation")
        # Every session-start build starts its message's embedding here, just
        # ahead of itself, so the build joins it and waits within its deadline. A
        # pre-warmed session gets its only early embed here; after the cold-start embed in
        # `_turn_admit` (turn_admission), the same text is a cache hit or joins that inference.
        if _context_is_new and not _provider_has_history and not is_slash:
            await _prewarm_startup_query(
                state,
                slot,
                message,
                memory_store=memory_store,
                execution_context=execution_context,
            )
        # The budgets that cut a block record the cut as they write its notice;
        # the collection gathers them for the ``context/composed`` _turn_dispatch_gates writes.
        # It opens around the build alone, so only THIS turn's composition lands in
        # it, and the worker thread sees it through the context the pool copies.
        with context_omissions.collect() as _ctx_omitted:
            full_message, _ = await run_in_embed_pool(
                state.context_builder.build_message,
                message,
                _context_is_new,
                session_key,
                agent=kiro_agent or slot.agent or None,
                resumed=_provider_has_history,
                workspace=slot.workspace or None,
                project=_turn_project or None,
                memory_store=memory_store,
                compressed_history=compressed,
                mode=slot.mode,
                blocks_reads=slot.blocks_reads,
                provider_type=provider_name,
                runtime_source="dashboard",
                request_prefix_context=_request_prefix_context or None,
                exclude_last_n=1,
                folder_path=folder_path,
                board_tags=board_tags,
                model_window=model_window,
                # Member DM threads get the four-layer member identity block.
                # `slot.agent` is the member the human picked (the crew name);
                # the `agent=` above is the TEMPLATE it resolved to, which is
                # why the member identity travels separately.
                member=slot.agent if slot.mode == "member" and slot.agent else "",
                execution_context=execution_context,
                user_text_range=user_text_span(
                    _user_prepend_offset,
                    user_typed_len,
                    quick_prompt=_is_quick_prompt,
                    prompt_expanded=prompt_expanded,
                ),
                user_span_out=_user_span,
                needs_reinjection=_needs_reinjection,
                steering_dirs=_folder_steering_dirs,
                context_provider=client,
                # Skill triggers read only what the person asked: none of
                # the context prepended above, and nothing at all on a turn
                # whose text the person did not write (a recovery or Continue
                # continuation, the synthesis prompt, an MCP App's message).
                skill_trigger_text="" if _is_synthetic else _jev_route_text,
            )
            turn.full_message = full_message
        slot_ctx_omitted = dict(_ctx_omitted)
        turn.slot_ctx_omitted = slot_ctx_omitted
        # The reported span is valid for the message as build_message
        # returned it. Several later steps PREPEND to the finished prompt
        # (incognito/temporary notice, re-injected history, hook context, a
        # regenerate system line), each of which slides the span. Rather than
        # patch every site, snapshot the length and the spanned text here and
        # re-derive the offset once, in _turn_dispatch_gates just before classification,
        # and verify the shifted span still holds the same text, so a future transform
        # that breaks the assumption degrades to the legacy reconstruction
        # instead of silently persisting a wrong attribution.
        if len(_user_span) == 2:
            _span_probe = full_message[_user_span[0] : _user_span[1]]
            turn._span_probe = _span_probe
            _span_base_len = len(full_message)
            turn._span_base_len = _span_base_len
        full_message = _apply_incognito_prefix(slot, full_message)
        turn.full_message = full_message
        _trusted_prompt_tail = full_message
        turn._trusted_prompt_tail = _trusted_prompt_tail
    else:
        full_message = _request_prefix_context + message
        turn.full_message = full_message
        if _context_is_new and not _provider_has_history:
            # Mirror the builder arm (above): this is the one place a
            # context-bearing turn in the builder-less fallback handles its
            # history, so settle the FRESH first-turn debt here too. A reset
            # that asked to forget pays it now; otherwise record that history
            # was assembled and let _run_chat's finally settle it on a durably-kept
            # turn. Without this a standalone dashboard with no context_builder
            # leaves the debt armed and re-prepends full history every turn.
            _first_turn_history_assembled = True
            turn._first_turn_history_assembled = _first_turn_history_assembled
            if state.sessions.consume_replay_suppression(session_key):
                state.sessions.consume_first_turn_history_owed(session_key)
                logger.info(
                    "Session replay suppressed by an explicit conversation reset: %s",
                    session_key,
                )
            else:
                from kiro_crew.dashboard.chat_persistence import _build_history_prefix

                history = await asyncio.to_thread(
                    _build_history_prefix,
                    slot,
                    conversation_log=state.conversation_log,
                    current_message=_current_replay_message,
                )
                full_message = history + full_message
                turn.full_message = full_message
