"""Steps that build a dashboard turn's prompt around the user's message, and the prompt
and skill mentions it expands.

The turn keeps the order the steps run in, the ContextBuilder call and the
context-composition record; each step here reads what it is handed and returns
what it adds, so the order stays readable in ``_run_chat``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.dashboard.chat_runner import (
        MAX_PROMPT_BYTES,
        DashboardState,
        FileTooLargeError,
        _ChatSlot,
        _find_prompt,
        _get_skills,
        _prompt_read_within_root,
        is_sensitive_path,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        safe_read_file_bytes_nolink,
        sel,
        served_provider_label,
        session_skill_globs,
        validate_file_path,
    )


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
                )
                or ""
            )
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

    content, _ = redact_credentials(content)
    content, _ = redact_exfiltration_urls(content)

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
        body, _ = redact_credentials(body)
        body, _ = redact_exfiltration_urls(body)
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
