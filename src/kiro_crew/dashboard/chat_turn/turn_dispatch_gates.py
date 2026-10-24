"""A dashboard turn's prompt and the gates it passes before its stream starts."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.context_blocks import (
    PHASE_PER_TURN,
    PHASE_SESSION_START,
    USER_LABEL,
    attributable_user_chars,
    split_blocks,
)
from kiro_crew.dashboard.chat_utils import (
    _SYNTHETIC_RECOVERY_MSGS,
    STEER_POSSIBLY_DELIVERED_NOTE,
    slack_mirror_is_paused,
)
from kiro_crew.dashboard.recovery_replays import ReplayRevocation, cancel_notice, replays_of
from kiro_crew.dashboard.state import row_mid
from kiro_crew.hooks import HOOK_EVENT_AGENT_SPAWN, HOOK_EVENT_USER_PROMPT_SUBMIT

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        RefusalInfo,
        ReplayFamily,
        SessionClosingError,
        StructuredStatus,
        classify_stop_reason,
        crew_log_emit,
        logger,
        read_context_tokens,
        sel,
        telemetry_channel_of,
    )
    from kiro_crew.dashboard.chat_turn.crew_log_attribution import _crew_log_model
    from kiro_crew.dashboard.chat_turn.decisions_hooks import (
        _jev_route_armed,
        _route_model_for_turn,
    )
    from kiro_crew.dashboard.chat_turn.model_fallback import _probe_fallback_restore_for_slot
    from kiro_crew.dashboard.chat_turn.prompt_assembly import (
        _checklist_resync,
        _expand_dollar_skills,
        _scrub_dashboard_prefix,
    )
    from kiro_crew.dashboard.chat_turn.recipient import (
        _deliver_cross_surface_user_message,
        _prepare_mirror_msg,
    )
    from kiro_crew.dashboard.chat_turn.steer_queue import _has_user_queued_followup
    from kiro_crew.dashboard.chat_turn.turn_context import _detach_appended_context
    from kiro_crew.dashboard.chat_turn.turn_entry import _stop_pressed
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _fire


async def _turn_dispatch_prep(turn: _ChatTurn) -> None:
    """Load a person's recovery context, expand the input's skill mentions and settle the replay before the prompt is built."""
    slot_ctx_blocks: dict[str, int]
    slot_ctx_omitted: dict[str, dict[str, Any]]
    from kiro_crew.dashboard import responsive_chat

    _prompt_depth = turn._prompt_depth
    _replay_pending = turn._replay_pending
    _request_prefix_context = turn._request_prefix_context
    _responsive_human = turn._responsive_human
    _responsive_recovery = turn._responsive_recovery
    _turn_project = turn._turn_project
    client = turn.client
    is_slash = turn.is_slash
    message = turn.message
    prompt_expanded = turn.prompt_expanded
    resumed = turn.resumed
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    if not is_slash and (_responsive_human or _responsive_recovery):
        _request_prefix_context = await asyncio.to_thread(
            responsive_chat.prompt_context, _responsive_recovery
        )
        turn._request_prefix_context = _request_prefix_context

    # ── $skill expansion: resolve $name tokens anywhere → load skill body ──
    # Operates ONLY on the user's typed message, never on @prompt-substituted
    # content: `prompt_expanded` is True when an @prompt body replaced `message`
    # in _turn_admit (at the same _prompt_depth=0), so we skip $skill here to prevent a
    # prompt author's embedded $tokens from silently loading extra skills into
    # the context (expand-what-the-user-typed, principle of least surprise).
    # Skipped for slash commands; _prompt_depth<1 blocks the recursive _run_chat
    # path. Token is left literal; resolved bodies are appended.
    if "$" in message and not is_slash and not prompt_expanded and _prompt_depth < 1:
        # Offloaded: expansion walks the skills tree(s) and reads skill
        # bodies, which is filesystem work that must not run on the event
        # loop — a large tree would stall the gateway heartbeat and every
        # other chat. The walk predates project-aware resolution; adding a
        # trusted project's own root made an existing on-loop cost worse
        # rather than introducing it, so the fix is to move the whole call
        # off the loop instead of narrowing what it may discover.
        expanded_message, _n_skills = await asyncio.to_thread(
            _expand_dollar_skills, message, state, slot, session_key, _turn_project
        )
        message, skill_context = _detach_appended_context(message, expanded_message)
        turn.message = message
        _request_prefix_context += skill_context
        turn._request_prefix_context = _request_prefix_context
        if _n_skills:
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="skill_dollar_expansion",
                tool_kind="prompt",
                outcome="ok",
                metadata={"count": str(_n_skills), "slot": slot.key},
            )

    # Change-card outcomes since this slot's last turn (a card proposed here
    # was applied, cancelled, undone...). Read and marked heard in one step,
    # so the next turn does not repeat them. Reference data only: it never
    # starts a turn of its own, and a card never carries a secret value.
    if not is_slash and _prompt_depth < 1:
        try:
            from kiro_crew.context_blocks import render_change_card_results
            from kiro_crew.dashboard.change_cards import card_store_for

            _card_store = card_store_for(state)
            await _card_store.warm()
            _card_outcomes = _card_store.take_unreported(slot.key)
            if _card_outcomes:
                _request_prefix_context += render_change_card_results(_card_outcomes)
                turn._request_prefix_context = _request_prefix_context
                await _card_store.flush()
        except Exception:
            logger.debug("change-card results block skipped", exc_info=True)
        # Likewise an Undo the user pressed on a change the crewmate made to
        # itself (``update_self``): told once, as reference data, so the next
        # reply does not speak of a face or name the user took back.
        try:
            from kiro_crew.context_blocks import render_self_change_undos
            from kiro_crew.dashboard import member_self_changes

            _self_undos = member_self_changes.take_unreported_undos(slot.key)
            if _self_undos:
                _request_prefix_context += render_self_change_undos(_self_undos)
                turn._request_prefix_context = _request_prefix_context
        except Exception:
            logger.debug("self-change undo block skipped", exc_info=True)

    # Ensure the mirror-source message is always bound before both the Slack
    # and channel-neutral user-message mirror legs run. The assignment in _turn_build_prompt's
    # context build that refines it only executes for non-slash turns that have a
    # context_builder; without this default, a non-slash turn with no
    # context_builder would hit UnboundLocalError at the mirror legs.
    _user_msg_for_mirror = message
    turn._user_msg_for_mirror = _user_msg_for_mirror

    # What the PERSON asked, after `@prompt`/`$skill` expansion and before any
    # prepend this runner authors -- no cancelled-turn preamble, no sub-agent
    # failure text, no app context -- so the consented history budget is the only
    # prior transcript `model.route` can ever see.
    _jev_route_text = message
    turn._jev_route_text = _jev_route_text

    # Per-turn injection breakdown, recorded on the usage row at turn end.
    # Empty when this turn injected nothing (no context builder / raw path).
    # Annotated at this phase's head, initialized in _turn_open; a plain reassignment here.
    slot_ctx_blocks = {}
    turn.slot_ctx_blocks = slot_ctx_blocks
    slot_ctx_phase = ""
    turn.slot_ctx_phase = slot_ctx_phase
    slot_ctx_omitted = {}
    turn.slot_ctx_omitted = slot_ctx_omitted
    # Chars this turn prepends between the request header and the user's
    # text; set in _turn_build_prompt's context_builder branch, 0 elsewhere.
    _user_prepend_offset = 0
    turn._user_prepend_offset = _user_prepend_offset
    # Exact bounds of the user's text in the final prompt, reported by
    # build_message. Empty on the non-context-builder paths. The probe/base
    # pair re-derives the bounds after the later prepends (see _turn_dispatch_gates).
    _user_span: list[int] = []
    turn._user_span = _user_span
    _span_probe = ""
    turn._span_probe = _span_probe
    _span_base_len = -1
    turn._span_base_len = _span_base_len
    # Snapshot the ContextBuilder output before any dashboard-only prefixes
    # are added. Final prefix scrubbing can then preserve this trusted tail
    # (including its sole minted reply-format marker) byte-for-byte.
    _trusted_prompt_tail: str | None = None
    turn._trusted_prompt_tail = _trusted_prompt_tail
    # The (id, text, completed) rows of the checklist sync block this turn's
    # prompt carries, to be marked stated once the provider starts
    # answering. Empty = none.
    _todo_sync_rendered: tuple[tuple[str, str, bool], ...] = ()
    turn._todo_sync_rendered = _todo_sync_rendered
    # True when THIS turn's prompt carries the cold-start recovery block, so
    # the debt is settled only by the turn that delivers it. A slash command
    # or a warm turn never carries it and must not clear it.
    _todo_recovery_carried = False
    turn._todo_recovery_carried = _todo_recovery_carried
    # The interrupted turn a natively resumed session lost (see the restore in
    # _turn_build_prompt's context build); minted after the egress scrub.
    _interrupted_turn_preamble = ""
    turn._interrupted_turn_preamble = _interrupted_turn_preamble
    _provider_has_history = resumed
    turn._provider_has_history = _provider_has_history
    if _replay_pending:
        # A replay lease means the native session does NOT hold this
        # conversation: allocation arms it only when the provider started
        # fresh (a Tool Search resume that replaced session/load, a failed
        # load, or a provider switch). A ``resumed`` observation cannot
        # outrank it, or the turn takes the slim-resume path, drops the owed
        # replay, and tells the model its history was restored.
        _provider_has_history = False
        turn._provider_has_history = _provider_has_history
    elif not _provider_has_history:
        # An ACP provider exposes its native client; ``resumed`` is True only
        # after a successful session/load. ``is True`` keeps a mock's truthy
        # attribute from counting as a resume.
        if getattr(getattr(client, "client", None), "resumed", None) is True:
            _provider_has_history = True
            turn._provider_has_history = _provider_has_history


async def _turn_dispatch_gates(turn: _ChatTurn) -> _Flow | None:
    """Pass the dispatch gates from the session's first turn up to the stream's start."""
    _mirror_stream_ts: str
    injected: list[str]
    _attachments = turn._attachments
    _context_is_new = turn._context_is_new
    _crew_log_sid = turn._crew_log_sid
    _crew_log_step = turn._crew_log_step
    _crew_log_turn_no = turn._crew_log_turn_no
    _interrupted_turn_preamble = turn._interrupted_turn_preamble
    _is_refusal_retry_turn = turn._is_refusal_retry_turn
    _is_synthetic = turn._is_synthetic
    _jev_route_text = turn._jev_route_text
    _mirror_chan = turn._mirror_chan
    _mirror_thread = turn._mirror_thread
    _prompt_depth = turn._prompt_depth
    _provider_has_history = turn._provider_has_history
    _span_base_len = turn._span_base_len
    _span_probe = turn._span_probe
    _steer_possibly_delivered = turn._steer_possibly_delivered
    _stop_reason = turn._stop_reason
    _synthetic_recovery_turn = turn._synthetic_recovery_turn
    _todo_recovery_carried = turn._todo_recovery_carried
    _todo_sync_rendered = turn._todo_sync_rendered
    _trusted_prompt_tail = turn._trusted_prompt_tail
    _turn_msg_boundary = turn._turn_msg_boundary
    _turn_provenance_restored = turn._turn_provenance_restored
    _turn_t0 = turn._turn_t0
    _user_msg_for_mirror = turn._user_msg_for_mirror
    _user_prepend_offset = turn._user_prepend_offset
    _user_span = turn._user_span
    agent_model = turn.agent_model
    client = turn.client
    full_message = turn.full_message
    is_new = turn.is_new
    is_slash = turn.is_slash
    message = turn.message
    monitor_completion = turn.monitor_completion
    prompt_expanded = turn.prompt_expanded
    provider_name = turn.provider_name
    regenerate_hint = turn.regenerate_hint
    session_key = turn.session_key
    slot = turn.slot
    slot_ctx_blocks = turn.slot_ctx_blocks
    slot_ctx_omitted = turn.slot_ctx_omitted
    slot_ctx_phase = turn.slot_ctx_phase
    state = turn.state
    turn_exit = turn.turn_exit
    user_typed_len = turn.user_typed_len
    user_typed_message = turn.user_typed_message
    if is_new:
        spawn_injected = await _fire(turn, HOOK_EVENT_AGENT_SPAWN, session_key)
    else:
        spawn_injected = []

    injected = await _fire(turn, HOOK_EVENT_USER_PROMPT_SUBMIT, message)
    all_injected = spawn_injected + injected
    if all_injected:
        hook_ctx = "\n\n".join(all_injected)
        full_message = f"[Hook context]\n{hook_ctx}\n[End hook context]\n\n{full_message}"
        turn.full_message = full_message

    if regenerate_hint:
        full_message = f"[System: {regenerate_hint}]\n\n{full_message}"
        turn.full_message = full_message

    # Enforce every structural boundary once more at provider egress.
    # ContextBuilder owns its trusted tail; everything added here is a pure
    # PREPEND. Scrub that complete dashboard-only prefix in one off-loop
    # pass so history, hook output, and future prepend sources cannot forge
    # reply, request, critical-rules, or session-context authority.
    if not is_slash:
        full_message = await _scrub_dashboard_prefix(full_message, _trusted_prompt_tail)
        turn.full_message = full_message

    if _interrupted_turn_preamble:
        full_message = f"{_interrupted_turn_preamble}\n\n{full_message}"
        turn.full_message = full_message
    if _steer_possibly_delivered and not is_slash:
        # Model input only: dispatch, the mirror legs and the row read the
        # user's own text.
        full_message = STEER_POSSIBLY_DELIVERED_NOTE + full_message
        turn.full_message = full_message

    # Checklist resync. The pill's snapshot outlives the native conversation
    # that produced it (agent switch, failed session/load, poisoned discard,
    # /clear all cold-start a fresh one), and kiro-cli's todo_list state lives
    # in that conversation. Same gate as the transcript replay in _turn_build_prompt's
    # context build — a cold start the provider did not resume itself — because
    # that is exactly when the agent's own list is empty while the pill is not.
    #
    # On a warm turn the agent still holds its list, so it gets only the rows
    # the person ticked in the pill since its last snapshot (todo_sync_prompt),
    # which is how a click reaches a live agent instead of being reverted by
    # its next full snapshot.
    if not is_slash:
        full_message, _todo_sync_rendered, _todo_recovery_carried = await _checklist_resync(
            slot,
            full_message,
            _context_is_new=_context_is_new,
            _provider_has_history=_provider_has_history,
            _todo_sync_rendered=_todo_sync_rendered,
            _todo_recovery_carried=_todo_recovery_carried,
        )
        turn._todo_recovery_carried = _todo_recovery_carried
        turn._todo_sync_rendered = _todo_sync_rendered
        turn.full_message = full_message

    # Slash commands use _kiro.dev/commands/execute for full native output;
    # regular messages use session/prompt.
    # Attribute the FINAL prompt back to the blocks that produced it, after every
    # prefix from _turn_build_prompt's context build and this phase is applied. Classifying
    # the OUTPUT rather than counting at each of the ~30 append sites means the
    # breakdown cannot drift from what was actually sent, and an unrecognised
    # block surfaces as `unclassified` instead of being folded into a neighbour.
    ctx_len = len(full_message) - len(message)
    if ctx_len > 0:
        # Re-derive the user span against the FINAL prompt: everything added
        # after build_message is a pure prepend, so the whole shift is the
        # length delta. Accept it only when the shifted slice still holds the
        # same text — otherwise fall back to the reconstruction below.
        _span_arg: tuple[int, int] | None = None
        if len(_user_span) == 2 and _span_base_len >= 0:
            _shift = len(full_message) - _span_base_len
            _s, _e = _user_span[0] + _shift, _user_span[1] + _shift
            if 0 <= _s <= _e <= len(full_message) and full_message[_s:_e] == _span_probe:
                _span_arg = (_s, _e)
            else:
                logger.warning(
                    "context breakdown: user span did not survive post-assembly "
                    "prefixes (shift=%d); falling back to reconstruction",
                    _shift,
                )
        slot_ctx_blocks = split_blocks(
            full_message,
            user_chars=attributable_user_chars(user_typed_len, prompt_expanded=prompt_expanded),
            user_offset=_user_prepend_offset,
            user_span=_span_arg,
        )
        turn.slot_ctx_blocks = slot_ctx_blocks
        slot_ctx_phase = PHASE_SESSION_START if _context_is_new else PHASE_PER_TURN
        turn.slot_ctx_phase = slot_ctx_phase
        # Named rather than counted: naming only four blocks by hand
        # under-describes most of the bytes being reported.
        _named = ", ".join(
            label.replace("_", " ")
            for label, _ in sorted(slot_ctx_blocks.items(), key=lambda kv: -kv[1])
            if label != USER_LABEL
        )
        state.broadcast_ws(
            "activity_event",
            {
                "slot": slot.key,
                "kind": "context",
                "text": (
                    f"Injected {ctx_len:,} chars of context ({_named})"
                    if _named
                    else f"Injected {ctx_len:,} chars of context"
                ),
            },
        )

    # ── Model-fallback restore probe (agent.fallback_model) ──
    # A prior turn's throttle fallback is sticky for the session; at the
    # start of each GENUINE user turn try once to move back to the primary.
    # Quiet on success — recovery is the expected state (log only, no chat
    # card); a still-throttled primary keeps the fallback for this turn.
    # Two guards keep the probe off recovery turns: a mid-cycle fallback
    # replay arrives with a non-zero `_fallback_candidate_idx` (the walk
    # state resets only when the cycle lands or terminates), and a
    # post-token CONTINUE replay is a runner-authored continuation of an
    # interrupted turn — restoring there would swap the model mid-answer.
    if (
        slot._active_fallback_model
        and slot._fallback_candidate_idx == 0
        and message not in _SYNTHETIC_RECOVERY_MSGS
    ):
        await _probe_fallback_restore_for_slot(slot, client)

    # ── Jev model routing (decisions/points/model_route.py) ──
    # For a slot the owner chose not to pin -- the owner picked "Auto (Jev)", or
    # the slot is on plain ``auto`` while the preview is on -- and only for a
    # NORMAL chat turn: `_crew_log_actor` is the turn's structural origin, so
    # cron deliveries, sub-agent turns, crew-relayed turns, app injections and
    # autonudge wakes are all excluded -- none has an owner watching the price
    # of the answer, and each already resolves its model through its own tier. A
    # runner-authored recovery continuation and a harness slash command are
    # excluded too: neither is a request whose difficulty is a question, and
    # re-routing mid-answer would swap the model under a turn already in
    # progress.
    # `_jev_route_armed` is in-memory; whether the PREVIEW is on is a keystone
    # read and is asked off the loop inside `_route_model_for_turn`, which is
    # also the refusal that keeps the shipped default free.
    # ``_directive_user_origin`` is deliberately NOT a condition, so a turn
    # delivered into this slot by its conductor (`session_send`, which queues
    # with `user_origin=False`) routes like one typed into the composer. The two
    # facts routing actually spends against are both the owner's and neither is
    # agent-writable: the keystone the preview reads, and the `decisions.
    # model_route` tier map that names every model a turn may land on. An agent
    # that can write its own slot's model cannot widen either, and the actor
    # check beside this still excludes every producer that declares itself.
    # The text sent is ``_jev_route_text``, not ``message``: Jev classifies the
    # difficulty of what the PERSON asked, and by here ``message`` carries every
    # prepend this turn made -- a cancelled-turn preamble, sub-agent failure text,
    # the drained app context. The mirror's own snapshot is taken after the first
    # two, so it is not the right source either: a preamble is prior transcript,
    # and prior transcript reaches this send only through the consented history
    # budget, whose shipped value is 0.
    # ``_synthetic_recovery_turn`` rides beside the text check for the reason its
    # _run_chat parameter comment gives and the two sibling guards apply: a runner
    # requeue of the USER'S OWN words is a recovery turn that fixed-text
    # membership cannot recognize, and re-routing it would re-answer a turn
    # already in progress on a model the owner is billed for twice.
    if (
        _jev_route_armed(slot)
        and turn._crew_log_actor == "user"
        and not _turn_provenance_restored
        and not _is_synthetic
        and not is_slash
        and message not in _SYNTHETIC_RECOVERY_MSGS
        and not _synthetic_recovery_turn
    ):
        # Two texts, kept apart on purpose: the person's own words are what the
        # tier is classified from, and ``full_message`` is what the turn SENDS, so
        # it is what the fit rule has to size.
        # A regenerate is the same request asked again, which is what
        # derived-tier escalation steps up on (``retried``); it changes
        # nothing while ``decisions.model_route_derive`` is off.
        await _route_model_for_turn(
            state,
            slot,
            client,
            _jev_route_text,
            session_key,
            prompt=full_message,
            retried=bool(regenerate_hint),
        )

    state.broadcast_ws("chat_status", {"slot": slot.key, "status": "Thinking…"})
    state.broadcast_ws("activity_event", {"slot": slot.key, "kind": "status", "text": "Thinking…"})

    # ── Bidirectional sync: mirror user message to linked Slack thread ──
    # Resolving the link is deliberately NOT gated on syntheticness — only the
    # user ECHO below is runner-authored. A recovery continuation still owes its
    # ANSWER to the thread that asked: gating the whole setup leaves
    # `_mirror_thread` empty, the reply leg downstream silently no-ops, and the
    # question already sitting on Slack is never answered at all.
    # A DISCONNECTED thread stops here and nowhere else: `_mirror_thread` and
    # `_mirror_chan` stay empty, which is what silences the echo, the tool
    # stream, the assistant reply and the stream teardown together. Disconnect
    # is the user saying "not into this conversation", which applies to the
    # answer as much as to the echo — so it is one gate, not four.
    if state.slack_client and not is_slash and not slack_mirror_is_paused(state, session_key):
        _mirror_thread, _mirror_chan = state.sessions.get_slack_link(session_key)
        turn._mirror_chan = _mirror_chan
        turn._mirror_thread = _mirror_thread
        if _mirror_thread and _mirror_chan:
            try:
                if not _is_synthetic:
                    from kiro_crew.slack.format import (  # circular: slack.format -> dashboard.state -> chat
                        escape_mrkdwn,
                    )

                    # The prompt is not only the operator's keystrokes (a
                    # session_send peer lands here too), and Slack parses
                    # <!channel> / <@U…> in `text`, so escape at this sink.
                    _mirror_msg = escape_mrkdwn(_prepare_mirror_msg(_user_msg_for_mirror))
                    await state.slack_client.post_message(
                        _mirror_chan, f"💬 _{_mirror_msg}_", _mirror_thread
                    )
                # Start a stream for real-time tool animations
                _mirror_stream_ts = (
                    await state.slack_client.start_stream(
                        _mirror_chan, _mirror_thread, initial_text="Thinking…"
                    )
                    or ""
                )
                turn._mirror_stream_ts = _mirror_stream_ts
            except Exception:
                logger.debug("Failed to mirror user message to Slack", exc_info=True)

    # Channel-neutral leg: mirror the user message to a linked non-Slack
    # proactive channel (e.g. Telegram) so the remote conversation reads
    # coherently (question then reply), matching the Slack echo above.
    if not is_slash and not _is_synthetic:
        await _deliver_cross_surface_user_message(state, session_key, _user_msg_for_mirror)

    _stop_reason = ""
    turn._stop_reason = _stop_reason
    # Class of the turn's completion (acp.types.classify_stop_reason); the
    # absent-reason default is `succeeded`, refined on EVENT_COMPLETE.
    _stop_class = classify_stop_reason("")
    turn._stop_class = _stop_class
    # Cleared at turn START so post-turn consumers never read the PREVIOUS
    # turn's value: a turn that dies before EVENT_COMPLETE (ACP crash, auth
    # expiry, transport drop) never reaches the assignment in _on_complete (turn_stats), and a
    # stale "end_turn" from the last successful turn would make the failed
    # turn look cleanly finished (e.g. to the session-summary gate).
    slot._last_stop_reason = ""
    # Tool-stall metadata forwarded by the ACP watchdog on its terminal
    # event (title / redacted command / evidence) — feeds the dedicated
    # tool-stall recovery nudge in _turn_stop_recovery (stop_recovery).
    _stall_tool_title = ""
    turn._stall_tool_title = _stall_tool_title
    _stall_command = ""
    turn._stall_command = _stall_command
    _stall_evidence = ""
    turn._stall_evidence = _stall_evidence
    _stall_status: StructuredStatus | None = None
    turn._stall_status = _stall_status
    # Structured refusal forwarded on the terminal (``AcpEvent.refusal``).
    # ``None`` unless the turn ended in a model-side refusal; read by the refusal card of
    # _turn_settle_answer (segment_flush), which renders the same shape for every harness.
    _turn_refusal: RefusalInfo | None = None
    turn._turn_refusal = _turn_refusal

    # ── Per-turn stats (elapsed / credits) ──
    # Wall-clock start of the turn. kiro (acp) leaves TurnUsage.duration_ms
    # at 0, so elapsed is measured here; claude_code's API-reported
    # duration_ms is preferred when present. Captured at EVENT_COMPLETE and
    # attached to the final assistant message via _attach_turn_stats so the
    # dashboard shows the same end-of-turn stats kiro-cli prints natively.
    # _turn_msg_boundary scopes the attach to THIS turn's messages so an
    # error-only turn can't overwrite the previous turn's stats.
    _turn_t0 = time.monotonic()
    turn._turn_t0 = _turn_t0
    _turn_elapsed_ms = 0
    turn._turn_elapsed_ms = _turn_elapsed_ms
    _turn_credits = 0.0
    turn._turn_credits = _turn_credits
    _turn_cost_usd = 0.0
    turn._turn_cost_usd = _turn_cost_usd
    _turn_model = ""
    turn._turn_model = _turn_model
    # Set once EVENT_COMPLETE has filled the four figures above.
    _turn_stats_captured = False
    turn._turn_stats_captured = _turn_stats_captured
    _turn_msg_boundary = len(slot.messages)
    turn._turn_msg_boundary = _turn_msg_boundary
    _turn_boundary_set = True
    turn._turn_boundary_set = _turn_boundary_set
    _turn_start_mid = row_mid(slot.messages[-1]) if slot.messages else ""
    turn._turn_start_mid = _turn_start_mid
    # The crew log ordinal is the ABSOLUTE durable position, not the window
    # length. `slot.messages` is front-trimmed at `_MAX_SLOT_MESSAGES`, so past
    # that cap its length stops growing and every later turn drew the SAME
    # ordinal -- the emitter then read distinct turns as retries of one and
    # incremented `attempt`, quietly merging a long session's whole tail into
    # one turn. `_disk_older_durable_count` is a position base with no disk
    # contract: it counts the entire evicted slice, including rows that never
    # reached disk, precisely so a later absolute position cannot shift down.
    # It is restored with the slot, so the ordinal also survives a resume, which
    # a per-process counter would not.
    #
    # `_turn_msg_boundary` stays the WINDOW index above: it slices this turn's
    # messages for the stats attach, which is a different question from which
    # turn this is. The two were already decoupled once, for the stats bug.
    #
    # Regenerate and rewind keep their retry semantics for free. Both truncate
    # the window before dispatching, so _turn_admit's hoisted expression returns the ordinal of
    # the turn being re-run and the emitter's `attempt` increments -- no ordinal
    # has to be threaded through either path.
    # `durable_row_count` is the SHARED counting rule every site that sets or
    # advances that base uses, so the ordinal cannot disagree with the base
    # about which rows are durable. It is resolved ONCE, in _turn_admit above its
    # @prompt expansion gate (see the hoist there), and reused here because the
    # accepted path may append one or more durable rows between the two points (the
    # @prompt chip and the $skill load row); recomputing here could therefore
    # read a different value, so the single pre-expansion ordinal is shared on
    # purpose.

    # Append-only the session's log: the body the gateway accepted. This names
    # ``_crew_log_turn_no``, the one ordinal every entry of this turn uses, and
    # is written BEFORE the dispatch gates below -- so even a refused turn still
    # shows what was asked, which is exactly the turn a reader most needs
    # explained. The two facts DERIVED from this request (``request/configured``
    # and ``context/composed``) are NOT written here: they are emitted past the
    # gates (see the block below where ``on_request_configured`` /
    # ``on_context_composed`` are called), because a refused dispatch assembled a
    # prompt nothing ever saw and must not publish a composition for it.
    crew_log_emit.on_message_received(
        _crew_log_sid,
        _crew_log_turn_no,
        role="user",
        text=user_typed_message,
        source=telemetry_channel_of(session_key),
        attachments=_attachments,
    )
    # Lease-dispatch race gate: this session's semaphore lease was taken
    # by get_or_create in _turn_admit, but the provider turn only opens on
    # the first iteration of _run_chat's stream loop. If a gateway restart / Make-Live
    # cutover moved the SessionManager into the closing state during the
    # async prep between, dispatching now would open a turn ABSENT from the
    # shutdown drain snapshot → killed mid-turn with its native lock held
    # (empty-response bug). Re-check SYNCHRONOUSLY here — no await between
    # this check and _run_chat's async-for — so the _closing read and the stream's
    # turn registration (AcpClient.stream_events clears _turn_done before its
    # first await) are one atomic span, strictly ordered w.r.t. close_all's
    # _closing set. Abort (lease released by _turn_release_session in
    # chat_turn/session_reset.py, from _run_chat's finally) if closing.
    try:
        if monitor_completion is not None:
            if not await monitor_completion.authorize():
                crew_log_emit.on_turn_refused(
                    _crew_log_sid,
                    _crew_log_turn_no,
                    "not_authorized",
                    turn._crew_log_actor,
                    depth=_prompt_depth,
                )
                return _Flow.END
        state.sessions.begin_turn(session_key)
    except SessionClosingError:
        logger.info("Aborting dispatch for %s — gateway is shutting down", session_key)
        crew_log_emit.on_turn_refused(
            _crew_log_sid,
            _crew_log_turn_no,
            "gateway_closing",
            turn._crew_log_actor,
            depth=_prompt_depth,
        )
        return _Flow.END
    # Stop-before-dispatch gate: a Stop pressed during the turn's async prep
    # (session cold start, context build) finds no session to cancel —
    # SessionManager.stop_turn answers "idle" and the stop card resolves —
    # so nothing downstream would ever honor it and the turn would open and
    # stream to completion behind a card that says stopped. The
    # point-in-time _stop_state is useless here (the idle resolution has
    # already snapped it back), so read the live Stop signal, which compares
    # the monotonic stop generations (slot-scoped and session-scoped, so a
    # stop issued on a linked channel surface counts) against their
    # turn-entry snapshots — the same durable signal the stop-hook
    # suppression uses. Synchronous, beside the begin_turn gate, so no await
    # separates the read from the stream's turn registration.
    if _stop_pressed(turn):
        logger.info(
            "Aborting dispatch for %s — Stop was pressed while the turn "
            "was still being prepared (no session existed to cancel yet)",
            session_key,
        )
        crew_log_emit.on_turn_refused(
            _crew_log_sid,
            _crew_log_turn_no,
            "stopped_before_dispatch",
            turn._crew_log_actor,
            depth=_prompt_depth,
        )
        return _Flow.END
    # The consume-seam supersession check ran at turn ENTRY, but the whole
    # preparation path (session acquisition, prompt assembly) is awaited
    # between there and this point. A correction the user queued during
    # that window must win over the refusal replay — a stale replay
    # dispatching first could run destructive tools the correction
    # cancelled. Synchronous, beside the stop gate, before the stream
    # registers the turn. Stops are already covered by _stop_pressed above.
    if _is_refusal_retry_turn and (
        bool(getattr(slot, "_pending_steers", None)) or _has_user_queued_followup(slot)
    ):
        replays_of(slot).disarm(ReplayFamily.CONTENT_FILTER)
        slot.append(
            "notice",
            cancel_notice(ReplayFamily.CONTENT_FILTER, ReplayRevocation(superseded=True)),
            "msg msg-info",
        )
        logger.info(
            "Aborting refusal replay for %s — a correction was queued while "
            "the turn was being prepared",
            session_key,
        )
        crew_log_emit.on_turn_refused(
            _crew_log_sid,
            _crew_log_turn_no,
            "replay_superseded_before_dispatch",
            turn._crew_log_actor,
            depth=_prompt_depth,
        )
        # This phase runs inside _run_chat's try: its finally's tail runs the newer message.
        return _Flow.END
    if monitor_completion is not None:
        monitor_completion.mark_accepted()
    # Append-only the session's log (flag-gated, fail-soft). Emitted HERE, after
    # every gate above has passed, because ``turn/started`` asserts that the
    # turn RAN: a start written before authorization leaves an orphan for each
    # refusal, and the interrupted-turn repair would later close it as though
    # the turn had died mid-flight. Each refusing gate records its own
    # ``turn/refused`` instead, so a refused turn is still visible and still
    # says which gate refused it. This adds no suspension point between the
    # stop-generation read above and the turn registration in _run_chat's stream loop --
    # the emitter hands the write to its own thread and returns -- so the
    # atomic span those two gates rely on is unchanged.
    #
    # The two request facts are emitted HERE for the same reason, and not where the
    # prompt is assembled in _turn_build_prompt's context build. ``context/composed`` states
    # what the gateway PUT IN FRONT OF THE MODEL, so a refused dispatch must not write one: the
    # prompt was built, but nothing ever saw it. Written before the gates, every
    # refusal left a composition the ``usage`` fold published as a per-turn row, and
    # the Context chart drew a bar for a turn that never ran -- with no way to
    # retract it, since ``USAGE_TYPES`` carries no ``turn/refused``. _turn_admit's
    # expansion gate already states this contract for itself ("the contract on a
    # refusal is the accepted input plus the refusal, not facts derived from a
    # request that was never assembled"); these two writes now honour it at the
    # later gates too. The previous guard was the completion-gated row store, which
    # only ever wrote a composition for a turn that finished.
    #
    # ``request/configured`` stays immediately ahead of ``context/composed``: the
    # fold reads the configured window from it to stamp each composition row, so the
    # order between these two is load-bearing even though their order against
    # ``turn/started`` is not. Neither adds a suspension point, for the reason above.
    crew_log_emit.on_request_configured(
        _crew_log_sid,
        _crew_log_turn_no,
        model=_crew_log_model(slot, slot.model or agent_model),
        provider=provider_name,
        context_window=read_context_tokens(client)[1],
    )
    crew_log_emit.on_context_composed(
        _crew_log_sid,
        _crew_log_turn_no,
        blocks=slot_ctx_blocks,
        phase=slot_ctx_phase,
        omitted=slot_ctx_omitted,
    )
    crew_log_emit.on_turn_started(
        _crew_log_sid,
        _crew_log_turn_no,
        turn._crew_log_actor,
        depth=_prompt_depth,
    )
    # From here the turn owes a closer, and _turn_close_crew_log
    # (chat_turn/crew_log_attribution.py), from _run_chat's finally, is what pays it: a
    # `turn/started` left open in the file is read as a turn whose writer died,
    # which this process being alive contradicts.
    _crew_log_turn_open = True
    turn._crew_log_turn_open = _crew_log_turn_open
    # And the cycle has a provider-backed reply to title and summarize.
    turn_exit.reached_provider = True
    # The turn's first model call, opened only once the turn is AUTHORIZED and
    # immediately before the stream. Written any earlier, every refused
    # dispatch would leave a step/started for a model call that never ran and
    # never completes -- the same reason turn/started waits for the gates -- and
    # the step's duration would include the time the gates took.
    _crew_log_step = crew_log_emit.on_step_started(_crew_log_sid, _crew_log_turn_no)
    turn._crew_log_step = _crew_log_step
    _crew_log_step_t0 = time.monotonic()
    turn._crew_log_step_t0 = _crew_log_step_t0
    return None
