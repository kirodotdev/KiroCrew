"""What an inbound Slack message resolves to before a turn runs.

Its privacy mode (the ``!temporary`` / ``!incognito`` modifiers, applied through
``messaging.privacy_mode``), the agent and project a thread or channel has selected and
where those selections are persisted, and the dashboard slot a linked thread hands the
message to instead of running it here.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.slack.handler import _cached_default_agent  # noqa: F401 - set via ``global``
    from kiro_crew.slack.handler import (
        _BANG_TO_SLASH,
        _INCOGNITO_TOKEN_RE,
        _TEMPORARY_TOKEN_RE,
        ConfigReadError,
        ConversationLog,
        KiroCrewConfig,
        SessionManager,
        SlackClientOps,
        _dashboard_state,
        _hydrated_sessions,
        _is_sessions_keyword,
        _thread_agents,
        _thread_projects,
        append_and_surface,
        config_path,
        is_allowed_user,
        is_sensitive_path,
        logger,
        privacy_mode,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        update_config_locked,
    )


def _is_slack_restricted(session_key: str) -> bool:
    """Return True if this Slack session should skip memory writes.

    The predicate itself is namespace-agnostic (see
    :func:`kiro_crew.messaging.privacy_mode.is_restricted`); the Slack spelling
    survives because this package's enforcement sites are named for it.
    """
    return privacy_mode.is_restricted(session_key)


def _hydrate_conv_flags(sessions: object, session_key: str) -> None:
    """Restore persisted temporary/incognito flags into the in-memory caches.

    Called once per session in ``handle_message`` so a thread marked temporary
    or incognito stays so across a gateway restart (the in-memory LRU is rebuilt
    from the durable ``SessionMap`` entry).
    """
    privacy_mode.hydrate(sessions, session_key)


async def _apply_privacy_mode(
    mode: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
    link_thread: bool = True,
) -> None:
    """Mark a session as *mode* and notify the user (idempotent).

    Everything platform-shaped is a callback into this module, which is what lets
    the shared applier own the ordering (``privacy_mode._commit_mode``, whose
    docstring is the one statement of it: the durable records first, awaited,
    then the publication -- or a refusal that publishes nothing).
    """

    async def _notify(message: str) -> None:
        await slack.post_message(channel, message, reply_ts or None)

    async def _on_applied(_mode: str) -> None:
        # Register thread so follow-up messages pass the in_active_thread
        # gate in mention/observe channels without needing another @mention.
        # reply_ts is the bare Slack thread_ts; session_key may be namespaced.
        # Skipped when there is no thread, and when the caller says this session
        # is not thread-scoped at all (``link_thread=False`` -- a flat 1:1 DM,
        # whose session is keyed by the channel): claiming a thread there would
        # hand the dashboard mirror one branch to post into. Posting is a
        # separate decision, so the confirmation still lands where the modifier
        # was typed.
        if reply_ts and link_thread:
            sessions.set_slack_link(session_key, reply_ts, channel)

    await privacy_mode.apply_mode(
        mode,
        session_key,
        source="slack",
        caller=user_id,
        resources=f"{channel}:{session_key}",
        sessions=sessions,
        notify=_notify,
        on_applied=_on_applied,
    )


async def maybe_apply_privacy_modifiers(
    text: str,
    cmd_text: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
    link_thread: bool = True,
) -> tuple[str, str, bool]:
    """Strip and apply the ``!temporary`` / ``!incognito`` privacy modifiers.

    Shared by the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path so the privacy controls behave identically
    on both (and the modifier token never leaks into the LLM prompt).

    Returns ``(text, cmd_text, only_modifier)``:
    - *text* — the LLM-facing message with the modifier token(s) removed.
    - *cmd_text* — the mention-stripped command text with the token removed
      (the native path reuses it for its subsequent ``!compact``/``!bang``
      checks; the transport path ignores it).
    - *only_modifier* — True when there is nothing left to run: the message was
      nothing but the modifier(s), OR the modifier was REFUSED (the gateway's
      private-conversation limit, or an over-long key -- ``apply_mode`` has
      already audited the denial and told the user the message was not
      processed). The caller MUST then return without starting an LLM turn:
      running the message with the mode silently dropped would be the leak the
      modifier exists to prevent.

    Slack's TWO texts are why this drives ``privacy_mode``'s primitives rather
    than its single-text ``strip_and_apply``: only *cmd_text* decides whether the
    message was nothing BUT a modifier, while *text* is what reaches the model.
    Ordering (temporary, then incognito) and the early return as soon as nothing
    remains match the shipped behaviour.
    """
    for mode, pattern in (
        (privacy_mode.MODE_TEMPORARY, _TEMPORARY_TOKEN_RE),
        (privacy_mode.MODE_INCOGNITO, _INCOGNITO_TOKEN_RE),
    ):
        cmd_stripped, had_mode = privacy_mode.strip_token(cmd_text, mode)
        if not had_mode:
            continue
        try:
            await _apply_privacy_mode(
                mode, session_key, user_id, channel, slack, sessions, reply_ts, link_thread
            )
        except privacy_mode.PrivacyModeRefused:
            # Audited and announced by apply_mode; nothing is left to run.
            return text, cmd_stripped, True
        cmd_text = cmd_stripped
        text = pattern.sub("", text)
        text = " ".join(text.split()) or text  # collapse whitespace
        if not cmd_text:
            # Message was *only* the modifier(s), with no remaining content.
            return text, cmd_text, True

    return text, cmd_text, False


def _get_default_agent() -> str:
    """Read persisted default agent, cached to avoid disk I/O on every message."""
    global _cached_default_agent
    if _cached_default_agent is None:
        _cached_default_agent = KiroCrewConfig.load().agent.default_agent
    return _cached_default_agent


def _read_thread_overrides(
    session_key: str, conversation_log: ConversationLog | None
) -> tuple[str, str]:
    """Read and resolve persisted overrides without mutating the live maps."""
    if not conversation_log:
        return "", ""
    try:
        meta = conversation_log.get_metadata(session_key)
    except Exception:
        logger.debug("Failed to hydrate thread overrides for %s", session_key, exc_info=True)
        return "", ""
    from kiro_crew.messaging.session_resume import session_agent_from_metadata

    resolved_agent = session_agent_from_metadata(meta) or meta.get("agent") or ""
    project = ""
    if meta.get("project"):
        # Defense-in-depth: re-validate the persisted path at this input
        # boundary. Conversation-log metadata is normally written through the
        # guarded !project handler, but if it is ever corrupted or tampered
        # with, a sensitive credential path (~/.aws, ~/.ssh, …) must never be
        # loaded into the in-memory cache.
        if not is_sensitive_path(meta["project"]):
            project = meta["project"]
        else:
            logger.warning(
                "Ignoring sensitive project path from thread metadata for %s",
                session_key,
            )
    return resolved_agent, project


async def _hydrate_thread_overrides(
    session_key: str, conversation_log: ConversationLog | None
) -> None:
    """Resolve private identity off-loop, then preserve any newer live selections."""
    if session_key in _hydrated_sessions:
        return
    if not conversation_log:
        _hydrated_sessions.add(session_key)
        return
    agent_before = _thread_agents.get(session_key)
    project_before = _thread_projects.get(session_key)
    agent, project = await asyncio.to_thread(_read_thread_overrides, session_key, conversation_log)
    # A concurrent hydration/command may have settled this session while the
    # worker read it. Never publish a stale result over that live selection.
    if session_key in _hydrated_sessions:
        return
    _hydrated_sessions.add(session_key)
    if agent and _thread_agents.get(session_key) == agent_before:
        _thread_agents[session_key] = agent
    if project and _thread_projects.get(session_key) == project_before:
        _thread_projects[session_key] = project


def _get_agent_for_session(session_key: str) -> str:
    """Return agent for a session: thread override first, then global default."""
    return _thread_agents.get(session_key) or _get_default_agent()


def _set_default_agent(name: str) -> None:
    """Persist default agent to config (shared with dashboard)."""
    global _cached_default_agent
    path = config_path()
    if is_sensitive_path(str(path)):
        raise ValueError(f"Refusing to write to sensitive path: {path}")

    def _apply(data: dict) -> dict:
        data.setdefault("agent", {})["default_agent"] = name
        return data

    try:
        # Locked read-modify-write: holds the sidecar advisory lock so a
        # concurrent config writer (dashboard PATCH, CLI, the boot-time meta
        # refresh) cannot land between this read and write and get reverted.
        update_config_locked(path, mutate=_apply)
    except ConfigReadError as e:
        # Fail closed: writing back a {} baseline would drop every other setting.
        raise ValueError(f"Failed to read config: {e}") from e
    except OSError as e:
        raise ValueError(f"Failed to write config: {e}") from e
    _cached_default_agent = name


def _persist_channel_config(
    channel_id: str,
    activation: str | None = None,
    agent: str | None = None,
) -> None:
    """Update a single channel's config in config.json (merge, not overwrite)."""
    path = config_path()
    if is_sensitive_path(str(path)):
        raise ValueError(f"Refusing to write to sensitive path: {path}")

    def _apply(data: dict) -> dict:
        slack_data = data.setdefault("slack", {})
        channels = slack_data.setdefault("channels", {})
        ch = channels.setdefault(channel_id, {})
        if activation is not None:
            ch["activation"] = activation
        if agent is not None:
            ch["agent"] = agent
        return data

    try:
        # Locked read-modify-write (see _set_default_agent): without the
        # sidecar lock, a `!channel always` racing any other config writer
        # could be silently reverted by the loser's stale snapshot.
        update_config_locked(path, mutate=_apply)
    except ConfigReadError as e:
        # Fail closed: writing back a {} baseline would drop every other setting.
        raise ValueError(f"Failed to read config: {e}") from e
    except OSError as e:
        raise ValueError(f"Failed to write config: {e}") from e


async def maybe_route_linked_thread(
    text: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    reply_ts: str,
    target_slot: Any | None = None,
    route_pinned: bool = False,
) -> bool:
    """Route a Slack message to a linked dashboard slot, if one is linked.

    Shared by the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path so a thread linked via
    ``/kirocrew link-to-dashboard`` behaves identically on both.

    Returns ``True`` when the caller MUST return without further handling —
    either the message was routed into the linked dashboard slot, or an
    unauthorized user was denied. Returns ``False`` when normal routing should
    continue: no dashboard state, no linked slot, a ``!``-bang command, or the
    bare ``sessions`` keyword (both intentionally allowed to fall through to
    normal handling, so control commands stay reachable in a linked thread).

    *route_pinned* makes *target_slot* authoritative instead of resolving the
    thread's CURRENT owner. An OPTIONS answer is accepted against the
    conversation that asked the question, but the dispatch runs as a separate
    task -- so re-resolving here would let a link, relink or unlink landing in
    between deliver that answer into a different conversation. Pinning is
    tri-state on purpose: a pinned ``None`` means "this answer belongs to no
    slot", so a thread linked AFTER acceptance cannot capture a native answer
    either.
    """
    if not (_dashboard_state and hasattr(_dashboard_state, "get_linked_slot")):
        return False
    if route_pinned:
        _linked_slot = target_slot
    else:
        # The dashboard _slack_to_slot map is keyed by the bare Slack thread_ts
        # (reply_ts), NOT the namespaced session key — look up with reply_ts so
        # canonical ``slack:<ts>`` session keys still hit linked slots. session_key
        # is kept for the SEL logging below.
        _linked_slot = _dashboard_state.get_linked_slot(reply_ts)
    if not _linked_slot:
        return False

    # Auth check FIRST — deny all messages from unauthorized users.
    if not is_allowed_user(user_id):
        logger.warning("Unauthorized user %s in linked thread %s", user_id, session_key)
        sel().log_tool_invocation(
            session_key=session_key,
            agent="kirocrew",
            source="slack",
            tool_name="linked_thread_intercept",
            tool_kind="permission",
            outcome="denied",
            metadata={"user_id": user_id, "reason": "not_allowed_user"},
        )
        await slack.post_message(channel, "Not authorized.", reply_ts)
        return True

    # Let bang commands and the bare ``sessions`` keyword fall through to
    # normal handling. The predicate matches the keyword branches exactly
    # (whole stripped, lower-cased message), so "sessions please" still routes
    # to the linked slot. Other keywords (status, spawn, cron, ...) remain
    # link-routed on purpose. A pinned OPTIONS answer is exempt: its text is a
    # selected label being DELIVERED to the conversation that asked, and
    # dropping it into the picker would strand that conversation forever.
    _first_word = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if _first_word in _BANG_TO_SLASH:
        return False
    if not route_pinned and _is_sessions_keyword(text):
        return False

    _linked_slot_key = _linked_slot.key
    # Redact for UI display only — LLM receives original text so it can process
    # user intent fully (redaction strips URLs/creds that may be relevant
    # context). The LLM's own output is redacted before display.
    _safe_text, _ = redact_exfiltration_urls(text)
    _safe_text, _ = redact_credentials(_safe_text)
    # Nothing rendered this Slack-typed row optimistically in the dashboard, so
    # broadcast_user=True: append delivers the ONE identity-carrying frame
    # (a frame without ``meta.mid`` lets a client receiving the row through a
    # second door render it a second time as a duplicate).
    append_and_surface(
        _dashboard_state, _linked_slot, "user", _safe_text, "msg msg-u", broadcast_user=True  # type: ignore[arg-type]
    )
    if not _linked_slot.running:
        from kiro_crew.dashboard.chat import _run_chat

        _chat_task = asyncio.create_task(
            _run_chat(
                _dashboard_state,  # type: ignore[arg-type]
                _linked_slot,
                text,
                _directive_user_origin=True,
                _directive_channel_origin=True,
            )
        )
        _linked_slot.task = _chat_task
        _dashboard_state._background_tasks.add(_chat_task)  # type: ignore[attr-defined]
        _chat_task.add_done_callback(_dashboard_state._background_tasks.discard)  # type: ignore[attr-defined]
    else:
        # circular import: session_control pulls in dashboard modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Stamp the admission-time containment. A linked slot records
        # linked=True here, so its own channel's queued messages keep draining;
        # only a constraint that appears AFTER this enqueue drops the entry.
        _linked_slot.queue_append(
            text,
            meta=containment_meta(_dashboard_state, _linked_slot),  # type: ignore[arg-type]
            directive_user_origin=True,
            directive_channel_origin=True,
        )
    _dashboard_state.push_slots_update()  # type: ignore[attr-defined]
    sel().log_tool_invocation(
        session_key=session_key,
        agent="kirocrew",
        source="slack",
        tool_name="linked_thread_intercept",
        tool_kind="permission",
        outcome="allowed",
        metadata={"user_id": user_id, "slot": _linked_slot_key},
    )
    logger.info("Routed linked Slack message to dashboard slot %s", _linked_slot_key)
    return True
