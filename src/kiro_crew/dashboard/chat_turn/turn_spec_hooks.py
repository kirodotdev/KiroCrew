"""A dashboard turn's spec hooks and agent notices: reading the hooks the session's agent spec
declares, firing each hook event for the turn (``_fire``), and the welcome, run-nowhere and
withheld-hooks notice rows; and the Stop hook with the continuations it and a tool refusal
queue, and the reply's linked-Slack mirror and channel-neutral leg (``_turn_stop_hook``)."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.spec_hooks import (
    crew_fired_spec_hooks,
    session_agent,
    spec_hook_events,
    spec_hooks_run_nowhere,
    spec_project_dir,
    withheld_project_spec_hooks,
)
from kiro_crew.dashboard.chat_utils import (
    SPEC_HOOKS_NOTICE_KIND,
    RecoveryPayload,
    is_synthetic_recovery_item,
    mint_options_token,
    remember_slack_options,
)
from kiro_crew.dashboard.state import (
    DENY_CAUSE_POLICY,
    HOOK_CONTINUATION_RECOVERY_PREFIX,
    HOOK_HALTED_RECOVERY_PREFIX,
    REFUSAL_RECOVERY_PREFIX,
    build_refusal_recovery_prompt,
    parse_hook_continuations,
    should_queue_hook_continuation,
    should_queue_refusal_recovery,
)
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_STOP, pre_tool_match_names
from kiro_crew.slack.outbound import PostedOptions

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ACP_BACKEND_KAS,
        SEED_DECLINED_NOTICE_KIND,
        STOP_REASON_CANCELLED,
        SYNTHETIC_RECOVERY_KIND,
        DashboardState,
        KiroCrewConfig,
        _ChatSlot,
        agent_welcome_message,
        append_and_surface,
        capabilities_of,
        expire_slack_options,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text
    from kiro_crew.dashboard.chat_turn.recipient import (
        _cross_surface_reply_text,
        _deliver_cross_surface_reply,
        cross_surface_withheld,
        slack_publication_withheld,
    )
    from kiro_crew.dashboard.chat_turn.tool_approval import (
        _SPEC_HOOKS_UNREADABLE_BLOCK,
        _credential_tool_hint_for,
        _spec_confirm_hooks_notice,
        _spec_hooks_run_nowhere_notice,
        _spec_keys_notice,
        _spec_withheld_hooks_notice,
    )
    from kiro_crew.dashboard.chat_turn.turn_entry import _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _queue_recovery
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import (
        _turn_full_reply_text,
        _turn_reply_segments,
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


def _notice_steer_upgrade(state: "DashboardState", slot: "_ChatSlot", client: Any) -> None:
    """One notice row on a new session whose adapter is too old to take a mid-turn
    message: the chat runs, the message waits for the turn, and the row names the
    installed version, the minimum and the upgrade command."""
    notice = getattr(client, "steer_upgrade_notice", "")
    if isinstance(notice, str) and notice:
        append_and_surface(state, slot, "notice", _redact_display_text(notice), "msg msg-info")


#: Notice text per ``settings_withheld_cause`` answer (see ``LLMProvider``).
_SEED_DECLINED_NOTICES = {
    "session": (
        "ℹ Crew's tools are off in this session. Another open Crew session in this "
        "folder uses a different model or permission mode, and its "
        ".claude/settings.local.json is in use here. To get the tools back, close "
        "that session, then start a new session here."
    ),
    "project": (
        "ℹ Crew's tools are off in this session. This folder's own "
        ".claude/settings.local.json controls tool approvals here, and Crew could "
        "not safely set it aside. The gateway log names the reason."
    ),
}


async def _notice_seed_declined(state: "DashboardState", slot: "_ChatSlot", client: Any) -> None:
    """One notice row on a new session whose MCP tools are withheld by a settings file.

    Reads the live governed state off the client, the same predicate that
    withholds the array, so the row shows exactly when the tools are off and
    names whose file holds the path. Best-effort: a client without the read, or
    a read that fails, renders no row rather than failing the turn.
    """
    read = getattr(client, "settings_withheld_cause", None)
    if not callable(read):
        return
    try:
        cause = await asyncio.to_thread(read)
    except Exception:  # noqa: BLE001 - an informational row must never fail a turn
        logger.debug("settings-withheld cause could not be read for the notice", exc_info=True)
        return
    notice = _SEED_DECLINED_NOTICES.get(cause) if isinstance(cause, str) else None
    if notice:
        append_and_surface(
            state,
            slot,
            "notice",
            notice,
            "msg msg-info",
            meta={"kind": SEED_DECLINED_NOTICE_KIND},
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


async def _turn_stop_hook(turn: _ChatTurn) -> None:
    """The Stop hook, the continuations it and a tool refusal queue, the reply's Slack and cross-surface legs."""
    _answer_text = turn._answer_text
    _mirror_chan = turn._mirror_chan
    _mirror_thread = turn._mirror_thread
    _refusal_notices = turn._refusal_notices
    _refusal_notices_settled = turn._refusal_notices_settled
    _refusal_reasons = turn._refusal_reasons
    _stop_reason = turn._stop_reason
    _turn_flushed_visible_text = turn._turn_flushed_visible_text
    is_slash = turn.is_slash
    needs_session_reset = turn.needs_session_reset
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # Pass the full redacted final assistant segment (text after the last
    # tool call, end-of-turn plan/OPTIONS processing applied) to Stop hooks.
    # fire() matches Stop hooks against this and puts it on stdin as
    # ``assistant_text``; run_script_hook caps ONLY the KIROCREW_HOOK_CONTEXT
    # env var (ARG_MAX safety). The full segment is passed (not sliced to
    # [:500]) so the tail — e.g. the harness [OPTIONS:] line — reaches both
    # the matcher and the hook body.
    _final = redact_credentials(redact_exfiltration_urls(turn.assistant_text)[0])[0]
    # Report how deep this hook-continuation run is so a gate hook can
    # diagnose or apply a stricter limit than the configurable backstop.
    _stop_hook_out = await _fire(
        turn,
        HOOK_EVENT_STOP,
        _final,
        hook_continuation_count=slot._hook_continuation_depth,
    )

    # ── Stop-hook continuation ─────────────────────────────────────────
    # A Stop hook that exits 0 and prints {"decision": "block", "reason":
    # ...} asks the harness to continue with `reason` as the next message
    # (https://kiro.dev/docs/hooks/types#agent-stop), so a hook can judge the
    # finished turn and keep the session going — a test-gate hook, or one that
    # auto-continues a trivial read — without a round-trip to the user.
    # Suppressed on a user stop or a pending reset so a hook can never
    # override the Stop button. A configurable consecutive-turn backstop
    # bounds faulty always-block hooks; 0 explicitly disables that backstop.
    # The dequeue loop in _run_chat's finally dispatches accepted continuations.
    # `user_stopped` is read live: a Stop initiated during this turn
    # (streaming, completion persistence, or the hook _fire above) may have
    # resolved already -- stop_turn() reporting "idle" resets _stop_state --
    # and only the generation counter still says it happened.
    if should_queue_hook_continuation(needs_session_reset, user_stopped=_stop_pressed(turn)):
        _hook_reasons = parse_hook_continuations(_stop_hook_out)
        # No block decision -> nothing to queue; skip the cap load and
        # arithmetic on the common empty path (also what the old
        # `_hook_reasons and _nudge_cap` short-circuit did).
        if _hook_reasons:
            _nudge_cap = (await asyncio.to_thread(KiroCrewConfig.load)).agent.max_stop_hook_nudges
            # Config loading yields to the event loop. Recheck the Stop
            # boundary before mutating the queue so a Stop that lands during
            # that await cannot be bypassed by the stale outer guard.
            if not should_queue_hook_continuation(
                needs_session_reset, user_stopped=_stop_pressed(turn)
            ):
                _hook_reasons = []
        else:
            _nudge_cap = 0
        # The cap bounds TOTAL consecutive continuation turns, and one Stop
        # event can carry several block reasons, so clamp to the remaining
        # budget rather than checking depth once and queueing all of them.
        # _hook_continuation_depth only counts turns that have RUN, so also
        # subtract continuations already sitting in the queue from an earlier
        # multi-reason event: they will run and add depth, and ignoring them
        # lets each event recompute room from depth alone and overshoot.
        _pending = sum(
            1
            for _it in slot._queue
            if is_synthetic_recovery_item(_it)
            and _it["content"].startswith(HOOK_CONTINUATION_RECOVERY_PREFIX)
        )
        _room = (
            len(_hook_reasons)
            if not _nudge_cap
            else max(0, _nudge_cap - slot._hook_continuation_depth - _pending)
        )
        # queue_insert(0, …) prepends, so insert in reverse to keep several
        # hooks' instructions in firing order.
        for _reason in reversed(_hook_reasons[:_room]):
            _queue_recovery(
                turn,
                0,
                f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\n{_reason}",
                kind=SYNTHETIC_RECOVERY_KIND,
            )
        if _hook_reasons and _room < len(_hook_reasons):
            # The run reached the cap: some (or all) reasons were refused.
            # Surface an inject row (renders as a halt card carrying the
            # reached depth) but dispatch nothing for the excess. This is the
            # backstop against a buggy always-block hook looping an
            # unattended session. `0` disables the cap entirely.
            _dropped = len(_hook_reasons) - _room
            slot.append(
                "inject",
                f"{HOOK_HALTED_RECOVERY_PREFIX} #{slot._hook_continuation_depth}\n"
                f"A Stop hook asked to continue, but this run reached "
                f"agent.max_stop_hook_nudges = {_nudge_cap} "
                f"(depth {slot._hook_continuation_depth}); {_dropped} nudge(s) "
                f"were dropped and the run was halted. Raise or disable the "
                f"cap in config to allow more.",
                "msg msg-inject",
            )
            state.push_slots_update()

    # ── Tool-refusal recovery (FALLBACK) ───────────────────────────────
    # The primary path already ran: each deny steered its reason into this
    # turn before answering the permission request, so a model on a
    # steer-capable backend has been told and no extra turn is owed. This
    # continuation covers what that could not reach — a backend without
    # mid-turn steer, or a notice the backend never echoed as folded in
    # (the turn died before a model-inference boundary). Then hand the
    # reason back so the model can adapt — an allowed alternative, a
    # different tool, or a reasoned stop — instead of stalling for the user.
    # Skipped on a user stop or when a session reset is already re-queuing.
    # No turn cap by design: the model decides when to stop, and the user's
    # Stop button stays the hard breaker. The dequeue loop in _run_chat's
    # finally picks this up and dispatches it.
    #
    # `answered` decides WHICH body is sent. A turn that produced its own
    # answer despite the block did not end early, so telling it to "continue
    # where you left off" makes it answer the same question a second time —
    # once per blocked call, each a full billed turn. The reason still has to
    # be delivered (without steer this turn is its only channel), so the
    # answered variant carries it as awareness and forbids the restatement
    # instead of suppressing the turn. `_answer_text` is the turn's own answer
    # with backend control notices removed; `_produced_visible_output` covers
    # the paths that reset `assistant_text` after emitting (steer cut,
    # compaction, clear, agent switch) — the same pair the other "did this turn
    # say anything" checks in _turn_settle_answer and _turn_ending_chain use.
    # That pair alone is NOT enough here, because unlike those checks this one
    # can be reached with the answer BEFORE the block: the model answers, then
    # calls a tool, and the tool boundary flushes the answer out of
    # `assistant_text` (_on_tool_call / _on_permission_request) while the user
    # has already read it on screen. `_turn_flushed_visible_text` carries that
    # third case, so the ordering answer-then-block gets the same awareness
    # body as block-then-answer instead of being told to continue and
    # re-deriving what is already on screen.
    #
    # The user-cancel input is the host's live Stop signal, not the wire
    # stop reason: on codex the ONLY reject option a command approval
    # advertises is `cancel`, which aborts the turn with stopReason
    # "cancelled" -- the refusal's own consequence, not a Stop press, and
    # this continuation is the only channel that still reaches its model.
    if should_queue_refusal_recovery(
        _refusal_reasons,
        needs_session_reset,
        user_stopped=_stop_pressed(turn),
        notices_sent=len(_refusal_notices) + _refusal_notices_settled,
        notices_pending=len(_refusal_notices),
    ):
        _recovery_hint = ""
        for _r_title, _r_reason in _refusal_reasons:
            _recovery_hint = await _credential_tool_hint_for(_r_reason, DENY_CAUSE_POLICY, _r_title)
            if _recovery_hint:
                break
        # The hint lookup above yields to the event loop. Re-read the Stop
        # signal before the queue write, exactly as the hook-continuation
        # gate does after its config load: a Stop that lands during that
        # await must not be bypassed by the outer gate's earlier read.
        _recovery_body = (
            ""
            if _stop_pressed(turn)
            else build_refusal_recovery_prompt(
                _refusal_reasons,
                credential_tool_hint=_recovery_hint,
                answered=(
                    bool(_answer_text.strip())
                    or turn._produced_visible_output
                    or _turn_flushed_visible_text
                ),
                # The backend ended the blocked turn as cancelled: it will
                # tell the model the user interrupted, so the body must say
                # otherwise.
                turn_aborted=(_stop_reason == STOP_REASON_CANCELLED),
            )
        )
        if _recovery_body:
            _queue_recovery(
                turn,
                0,
                f"{REFUSAL_RECOVERY_PREFIX}\n{_recovery_body}",
                kind=SYNTHETIC_RECOVERY_KIND,
                payload=RecoveryPayload.CONTINUATION,
            )

    # ── Bidirectional sync: mirror response to linked Slack thread ──
    # Gated on the SAME audience fence as the channel-neutral leg below. Slack is
    # a cross-surface audience like any other: it resolves its thread owner live
    # (`get_session_for_thread`) and carries supersession handling precisely
    # because a relink can land mid-turn, so a peer steer admitted against an
    # unlinked target can have its reply published here to a conversation the
    # authorization never saw. Fencing only the non-Slack leg would leave the
    # busier surface open. The destination is the thread cached at turn start,
    # not the live binding, so it is judged as the room it is
    # (`slack_publication_withheld`) beside the live comparison.
    #
    # Publish the turn's WHOLE answer, not the post-last-tool remnant in
    # `turn.assistant_text`: a tool boundary flushes and resets that
    # accumulator, so a turn that answers before its last tool call would
    # otherwise mirror a fragment or nothing. The gate reads the full text
    # too, so an answer fully flushed before the last tool (empty
    # `assistant_text`) still publishes.
    _mirror_reply_text, _mirror_final_segment = _turn_full_reply_text(turn)
    if not _mirror_reply_text.strip() and turn._terminal_directive_applied:
        # A quiet end (``nothing_to_do``) is the turn's answer. The dashboard
        # shows it on the tool card, but the thread's only trace would be a task
        # step folded inside the collapsed stream, so a quiet patrol cycle reads
        # there as one that never ran. Post the outcome line as the reply. It
        # asks nothing, so no options control is minted from it.
        _mirror_reply_text = turn._terminal_directive_text
        _mirror_final_segment = ""
    if (
        _mirror_reply_text
        and state.slack_client
        and _mirror_thread
        and _mirror_chan
        and not cross_surface_withheld(state, slot)
        and not slack_publication_withheld(state, slot, _mirror_chan, _mirror_thread)
    ):
        try:
            from kiro_crew.slack.format import (  # circular: slack.format -> dashboard.state -> chat
                build_options_blocks,
                extract_options,
                render_for_slack,
            )

            # Extract the OPTIONS tag from the RAW text, before rendering.
            # It is a plain-text marker, so pulling it off after conversion
            # means whatever conversion did to the tail decides whether the
            # controls render at all -- and a >39,000-char turn loses the tag
            # entirely to to_slack_mrkdwn's self-truncation.
            #
            # Actionable choices come from the FINAL segment alone -- the
            # newest reply row -- matching the dashboard, which reads only the
            # last assistant row. Reading the newest row rather than
            # `turn.assistant_text` keeps a final OPTIONS ask whose row was
            # flushed before a text-less last tool call, which empties
            # `turn.assistant_text`. The mirrored BODY joins every reply row,
            # so a marker in an earlier segment is stripped from it too (a
            # single extract_options over the join takes the first and leaves
            # the rest as literal text) -- but it never becomes a control:
            # minting one for an earlier segment's choices, or for a question
            # the turn moved past and ended without a marker, puts stale
            # buttons on the thread whose click sends a superseded answer
            # back to the session.
            _, _mirror_options = extract_options(_mirror_final_segment)
            _mirror_body = _mirror_reply_text
            while True:
                _mirror_body, _these_options = extract_options(_mirror_body)
                if not _these_options:
                    break

            for _part in render_for_slack(_mirror_body):
                await state.slack_client.post_message(_mirror_chan, _part, _mirror_thread)
            if _mirror_options:
                # Keep the ts this posts: the control has to be spendable
                # later, and discarding the ts is what leaves a superseded
                # question clickable forever. build_options_blocks already
                # redacts each choice through redact_for_display, so nothing
                # extra is needed here.
                # The asker is THIS session, named explicitly. Resolving it
                # from the thread would name whoever owns the thread at mint
                # time, so a relink landing mid-turn would stamp the control
                # with a conversation that never asked the question.
                _mirror_token = await asyncio.to_thread(mint_options_token, state, session_key)
                _mirror_blocks = build_options_blocks(
                    _mirror_options, staleness_token=_mirror_token
                )
                # The thread's owner BEFORE the post. A relink landing while
                # post_blocks is in flight moves the conversation to another
                # session, and a control recorded under the key this turn
                # started with would be filed where that session's expiry
                # never looks -- and clickable into a conversation it does not
                # belong to. Same treatment the other two posting paths get.
                _pre_owner = (
                    state.sessions.get_session_for_thread(_mirror_thread) or session_key
                    if getattr(state, "sessions", None)
                    else session_key
                )
                _mirror_ts = await state.slack_client.post_blocks(
                    _mirror_chan,
                    _mirror_blocks,
                    "Options",
                    _mirror_thread,
                )
                if _mirror_ts:
                    _owner = (
                        state.sessions.get_session_for_thread(_mirror_thread) or session_key
                        if getattr(state, "sessions", None)
                        else session_key
                    )
                    remember_slack_options(
                        state,
                        _owner,
                        PostedOptions(
                            channel=_mirror_chan,
                            ts=_mirror_ts,
                            choices=tuple(_mirror_options),
                            blocks=tuple(_mirror_blocks),
                        ),
                    )
                    if _owner != _pre_owner:
                        # An owner change IS supersession: the question we just
                        # posted would be answered into a conversation that has
                        # moved on. Narrowed to OUR ts so a control the new
                        # owner recorded meanwhile survives.
                        await expire_slack_options(state, _owner, ts=_mirror_ts)
        except Exception:
            # WARNING, not DEBUG: a swallowed failure here loses the turn's
            # reply on the linked thread with no other trace, and the lost
            # reply is user-visible.
            logger.warning("Failed to mirror response to Slack", exc_info=True)

    # Channel-neutral leg: deliver the completed reply to a linked non-Slack
    # proactive channel (e.g. Telegram) via Transport.send_message. Slack is
    # handled above by its dedicated streaming mirror. Only a slash command is
    # withheld: it has no mirrored question, whereas every requeue site runs
    # downstream of the user-message leg in _turn_dispatch_gates, so a recovery reply
    # always has a preceding question on the linked surface — withholding it would strand
    # that question unanswered. The audience fence is judged INSIDE the leg, on
    # the one binding read the send resolves its target from: asked here first,
    # the decision and the delivery would be two reads with the off-loop
    # mirror-link writer free to retarget between them. The text is the turn's
    # whole answer, as on the Slack leg: `turn.assistant_text` is only the
    # segment after the last tool call.
    if not is_slash:
        _xs_reply = _cross_surface_reply_text(_turn_reply_segments(turn), turn.assistant_text)
        if not _xs_reply.strip() and turn._terminal_directive_applied:
            # Same quiet end as the Slack leg above: the outcome line is the reply.
            _xs_reply = turn._terminal_directive_text
        await _deliver_cross_surface_reply(state, session_key, _xs_reply, slot=slot)
