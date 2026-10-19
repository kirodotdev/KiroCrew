"""A dashboard turn's spec hooks and agent notices: reading the hooks the session's agent spec
declares, firing each hook event for the turn (``_fire``), and the welcome, run-nowhere and
withheld-hooks notice rows."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ACP_BACKEND_KAS,
        HOOK_EVENT_PRE_TOOL_USE,
        SPEC_HOOKS_NOTICE_KIND,
        DashboardState,
        _ChatSlot,
        agent_welcome_message,
        append_and_surface,
        capabilities_of,
        crew_fired_spec_hooks,
        logger,
        pre_tool_match_names,
        session_agent,
        spec_hook_events,
        spec_hooks_run_nowhere,
        spec_project_dir,
        withheld_project_spec_hooks,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text
    from kiro_crew.dashboard.chat_turn.tool_approval import (
        _SPEC_HOOKS_UNREADABLE_BLOCK,
        _spec_confirm_hooks_notice,
        _spec_hooks_run_nowhere_notice,
        _spec_keys_notice,
        _spec_withheld_hooks_notice,
    )


async def _surface_agent_welcome(
    state: "DashboardState",
    slot: "_ChatSlot",
    agent: str,
) -> None:
    """Render *agent*'s ``welcomeMessage`` into *slot*, at most once per activation.

    The one consumer of the agent-config field. An agent JSON may carry a
    ``welcomeMessage`` — a usage hint its author wants the user to read when the
    agent takes over — and this is the only place it reaches a user. Kiro Crew's
    own bundled agents ship one, so the field is not a niche extension point.

    Three properties are the whole design, and each one rules out an
    alternative that looks simpler:

    * **A transcript row, not prompt context.** Role ``notice``: durable (not in
      ``_TRANSIENT_ROLES``, so it persists and survives a reload) but NOT one of
      the ``user``/``assistant`` roles the history replay feeds back to the
      model. Inlining the hint into the system prompt is the workaround the
      issue reports as the thing to remove — it re-sends the text every turn.
    * **One shot per activation.** Both call sites clear through
      ``slot._welcomed_agent``, because a switch and the session start its own
      reset produces are two events for ONE activation, and each path would
      otherwise emit for the other.
    * **Untrusted text.** The field is read from a user-writable, tool-shared
      directory. ``NoticeCard`` renders a ``notice`` row as plain text (no
      markdown, no HTML), the reader caps its length, and it is passed through
      the same display redactors as any other foreign string reaching a
      transcript — so a hint carrying a credential or an exfil URL cannot ship
      one into the persisted window.

    Best-effort: the disk read is offloaded (this runs on the gateway's shared
    event loop) and a failure renders nothing rather than failing the turn.
    """
    if not agent:
        # The default crew has no spec, so there is nothing to render — but the
        # claim is RELEASED rather than merely skipped: leaving it set made
        # A -> default -> A read as "already welcomed A" and swallow A's second
        # activation, which the one-shot rule is per activation, not per slot.
        slot._welcomed_agent = ""
        return
    if slot._welcomed_agent == agent:
        return
    # Claimed BEFORE the await: two events for one activation can both reach
    # this point, and the offload yields the loop between the guard and the
    # append. Recording the failure case as "welcomed" too is deliberate — a
    # hint that could not be read is not worth retrying on every later event
    # for the same agent.
    slot._welcomed_agent = agent
    try:
        text = await asyncio.to_thread(agent_welcome_message, agent, project=slot.project or None)
    except Exception:  # noqa: BLE001 - decoration must never fail a turn
        logger.debug("Failed to read welcomeMessage for agent %r", agent, exc_info=True)
        return
    if not text:
        return
    append_and_surface(state, slot, "notice", _redact_display_text(text), "msg msg-info")


async def _prepare_spec_hooks(
    state: "DashboardState",
    slot: "_ChatSlot",
    client: Any,
    agent: str,
    *,
    is_new: bool,
) -> tuple[list, bool, str | None]:
    """This turn's spec hooks for ``_fire``, whether they could not be read, and
    the session workspace they run in (``None`` for the gateway's own).

    Only for a backend whose capabilities say Crew fires the spec's hooks: kiro-cli
    runs the field itself, and firing it here too would run every hook twice, so
    every other session gets ``([], False, None)``.

    On a new session, a spec that sets a key nothing carries to this backend gets
    one notice row, and so does a spec with ``confirm: true`` hooks, which Crew
    skips because it cannot ask for the confirmation. A new session on a backend
    where nothing runs the spec's hooks (``spec_hooks_run_nowhere``) gets one
    notice row naming their events. None of them runs silently.
    """
    if not capabilities_of(client).crew_fires_spec_hooks:
        if is_new and spec_hooks_run_nowhere(client):
            await _notice_spec_hooks_run_nowhere(state, slot, client, agent)
        return [], False, None
    cwd = getattr(client, "cwd", "")
    work_dir = cwd if isinstance(cwd, str) and cwd else None
    agent = session_agent(client, agent)
    if not agent:
        # Whose hooks apply is unknown, so PreToolUse fails closed.
        logger.warning("no agent is known for this KAS session; tool calls are blocked")
        return [], True, work_dir
    try:
        hooks, lost, unconfirmable = await asyncio.to_thread(
            functools.partial(
                crew_fired_spec_hooks,
                agent,
                spec_project_dir(client),
                # Only KAS receives `toolsSettings` as `permissions` rules.
                translates_tools_settings=capabilities_of(client).backend == ACP_BACKEND_KAS,
            )
        )
    except Exception:  # noqa: BLE001 - the caller fails PreToolUse closed
        logger.warning(
            "agent spec hooks for %r could not be read; tool calls are blocked",
            agent,
            exc_info=True,
        )
        return [], True, work_dir
    if is_new and lost:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_keys_notice(agent, lost)),
            "msg msg-info",
        )
    if is_new and unconfirmable:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_confirm_hooks_notice(agent, unconfirmable)),
            "msg msg-info",
        )
    if is_new:
        # A withheld project spec yields the user-level spec in its place, so
        # these hooks are exactly the user-level ones that still run.
        await _surface_withheld_project_hooks(
            state, slot, client, agent, user_hooks_run=bool(hooks)
        )
    return hooks, False, work_dir


async def _notice_spec_hooks_run_nowhere(
    state: "DashboardState", slot: "_ChatSlot", client: Any, agent: str
) -> None:
    """One notice row naming the spec hook events a session on *client* never runs.

    Best-effort: nothing is gated on these hooks, so a spec that cannot be read
    renders no row rather than failing the turn.
    """
    agent = session_agent(client, agent)
    if not agent:
        return
    try:
        events = await asyncio.to_thread(spec_hook_events, agent, spec_project_dir(client))
    except Exception:  # noqa: BLE001 - an informational row must never fail a turn
        logger.debug(
            "agent spec hooks for %r could not be read for the notice", agent, exc_info=True
        )
        return
    if events:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_hooks_run_nowhere_notice(agent, events)),
            "msg msg-info",
            meta={"kind": SPEC_HOOKS_NOTICE_KIND},
        )


async def _surface_withheld_project_hooks(
    state: "DashboardState",
    slot: "_ChatSlot",
    client: Any,
    agent: str,
    *,
    user_hooks_run: bool,
) -> None:
    """One notice row when the checkout's own spec sets ``hooks`` Crew does not run.

    The hooks are withheld by :func:`trusted_project_agent_spec`; this only says so,
    so a repository's hooks never stop running unseen. *user_hooks_run* says
    whether the user-level spec read in its place carries hooks this session runs,
    which the notice states only when true. Best-effort: a failed check renders
    nothing rather than failing the turn.
    """
    project_dir = spec_project_dir(client)
    if not project_dir:
        return
    try:
        withheld = await asyncio.to_thread(withheld_project_spec_hooks, agent, project_dir)
    except Exception:  # noqa: BLE001 - a notice must never fail a turn
        logger.debug("could not check %r's project spec for withheld hooks", agent, exc_info=True)
        return
    if withheld:
        append_and_surface(
            state,
            slot,
            "notice",
            _redact_display_text(_spec_withheld_hooks_notice(agent, user_hooks_run=user_hooks_run)),
            "msg msg-info",
            meta={"kind": SPEC_HOOKS_NOTICE_KIND},
        )


async def _fire(
    turn,
    event: str,
    context: str = "",
    tool_name: str = "",
    tool_input: dict | None = None,
    tool_response: dict | None = None,
    hook_continuation_count: int = 0,
    tool_id: str | None = None,
    tool_identity: str = "",
    mcp_server: str = "",
) -> list[str]:
    """Fire script hooks. Returns stdout texts from exit-0 hooks (for context injection).

    ``tool_id`` is the id the harness stated for the call
    (``AcpEvent.harness_tool_id``). The spec's own hooks match their tool
    matcher against it, translated by ``spec_hook_tool_names``, because their
    matchers name tools and ``tool_name`` is the call's title. A Hooks-page
    hook matches the title and those names both, so a ``web_fetch`` hook meets
    KAS's "Fetch URL" too. ``tool_identity`` and ``mcp_server`` are the call's
    canonical name and trusted MCP server, and join both, as on the subagent
    gate (``hooks.pre_tool_match_names``).
    """
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    injected: list[str] = []
    if state._hook_store is None:
        if event == HOOK_EVENT_PRE_TOOL_USE:
            injected.append("BLOCKED:system:hook store not initialized")
            logger.error("Hook store not initialized for PRE_TOOL_USE - blocking tool")
        return injected
    if turn._spec_hooks_unreadable and event == HOOK_EVENT_PRE_TOOL_USE:
        injected.append(_SPEC_HOOKS_UNREADABLE_BLOCK)
        logger.error("Agent spec hooks unreadable for PRE_TOOL_USE - blocking tool")
        return injected
    try:
        # A call KAS named no tool for keeps title matching (None).
        _match_names, _spec_tool_names = pre_tool_match_names(
            tool_name,
            tool_identity=tool_identity,
            mcp_server=mcp_server,
            harness_tool_id=tool_id or "",
        )
        if not turn._spec_hooks:
            _spec_tool_names = None
        results = await state._hook_store.fire(
            event,
            context,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_response=tool_response,
            # This session's OWN key, for per-session attribution and the
            # governance scope. Not ``parent_session_key``: that names the
            # session that spawned a subagent, and a hook reading it as
            # such would take every dashboard turn for a subagent's.
            session_key=session_key,
            hook_continuation_count=hook_continuation_count,
            extra_hooks=turn._spec_hooks,
            extra_hooks_cwd=turn._spec_hooks_cwd,
            extra_hooks_tool_names=_spec_tool_names,
            tool_match_names=_match_names,
        )
        for r in results:
            # Anchoring rule for the bounded hook excerpts below: text the
            # hook AUTHORED for a reader (its stdout, the exit-2 deny reason
            # -- "STDERR returned to the LLM") starts at the head, so those
            # keep ``[:N]``; a hook that CRASHED (any other non-zero exit)
            # prints its diagnosis last, so its stderr excerpt is ``[-N:]``.
            # ``r.stdout``/``r.stderr`` are already redacted over the full
            # stream by run_script_hook, so slicing here cannot cut a secret.
            if r.exit_code == 0 and r.stdout:
                injected.append(r.stdout)
                logger.info("Hook %s stdout: %s", r.hook_name, r.stdout[:200])
                state.broadcast_ws(
                    "activity_event",
                    {
                        "slot": slot.key,
                        "kind": "hook",
                        "text": f"Hook {r.hook_name}: injected {len(r.stdout)} chars",
                    },
                )
            elif r.exit_code == 2:
                injected.append(
                    f"BLOCKED:{r.hook_name}:{r.stderr[:200] if r.stderr else 'hook denied'}"
                )
                logger.warning(
                    "Hook %s blocked tool: %s",
                    r.hook_name,
                    r.stderr[:200] if r.stderr else "exit 2",
                )
                state.broadcast_ws(
                    "activity_event",
                    {
                        "slot": slot.key,
                        "kind": "hook",
                        "text": f"Hook {r.hook_name} BLOCKED: {r.stderr[:100] if r.stderr else 'denied'}",
                    },
                )
            elif r.exit_code not in (0, 2):
                detail = (
                    r.error[:200]
                    if r.error
                    else (r.stderr[-200:] if r.stderr else f"exited with code {r.exit_code}")
                )
                if event == HOOK_EVENT_PRE_TOOL_USE:
                    # Fail closed. A PreToolUse hook has a two-valued
                    # contract — exit 0 is a delivered allow, exit 2 a
                    # delivered deny — so every other code means the gate
                    # did not decide, and for a gate that resolves to deny.
                    # Treating it as a pass would mean breaking, slowing, or
                    # deleting the deny hook silently disables the policy it
                    # enforces. Same shape as the hook-store and
                    # fire()-raised denials on this path.
                    #
                    # This deliberately covers more than the undelivered
                    # shapes (timeout and crash → -1, unexecutable → 126/127):
                    # a hook that runs to completion and exits 1 also blocks
                    # here. A hook's own uncaught error surfaces as exit 1
                    # too and is
                    # indistinguishable from a deliberate one, and the exit
                    # code a failed exec produces is shell- and
                    # platform-specific (cmd /c yields 9009 or 1 where
                    # /bin/sh yields 127), so an allowlist of "real" failure
                    # codes would fail open on Windows for exactly this
                    # class. The hook store already calls every nonzero
                    # non-2 exit an error (``last_status = "error"``); this
                    # branch gives the gate the matching direction.
                    injected.append(f"BLOCKED:{r.hook_name}:{detail}")
                    logger.error(
                        "Hook %s could not deliver a verdict (%s) - blocking tool",
                        r.hook_name,
                        detail,
                    )
                    state.broadcast_ws(
                        "activity_event",
                        {
                            "slot": slot.key,
                            "kind": "hook",
                            "text": f"Hook {r.hook_name} BLOCKED (no verdict): {detail[:100]}",
                        },
                    )
                elif r.stderr:
                    # Non-zero, non-block on a non-gating event: warn only.
                    logger.warning("Hook %s warning: %s", r.hook_name, r.stderr[-200:])
    except Exception as exc:
        if event == HOOK_EVENT_PRE_TOOL_USE:
            logger.warning("Hook fire error during blocking event %s: %s", event, exc)
            raise
        logger.warning("Hook fire error: %s", exc)
    return injected
