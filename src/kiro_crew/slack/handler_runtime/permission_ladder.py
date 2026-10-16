"""How a native Slack turn answers a tool call and a permission request.

``_screen_tool_call`` warns and audits a call the provider is already running.
``_answer_permission_request`` is the enforcing ladder: the hook verdict, the
``auto_approve_subagent_spawn`` rung (``_should_auto_approve_spawn``), the approval
mode, per-session Trust and owner YOLO, then the interactive prompt
(``_request_approval``) whose click ``handle_interaction`` answers. Every
``approve_tool`` and ``reject_tool`` call of the native path is in this module, with
the steer that tells the model a host refusal was not the person's
(``_steer_host_deny``) and the fallback reject that keeps a request from being
stranded (``_reject_orphaned_tool``).

Reads off the ``_SlackTurn``: the routed session and agent, the approval mode, the hook
set and the turn's status controller, task and answer stream.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _ACTION_APPROVE,
        _ACTION_REJECT,
        _ACTION_TRUST,
        _APPROVAL_TIMEOUT,
        _OUTCOME_APPROVED,
        _OUTCOME_REJECTED,
        _STEER_NOTICE_BOUND_SECS,
        APPROVAL_AUTO,
        DENY_CAUSE_APPROVAL_TIMEOUT,
        DENY_CAUSE_POLICY,
        OUTCOME_REJECTED_TRANSPORT_FLOOR,
        TOOL_ALLOW,
        TOOL_AUTO_APPROVE,
        TOOL_DENY,
        LLMEvent,
        LLMProvider,
        SessionManager,
        SlackClientOps,
        Stats,
        ToolHookResult,
        _build_approval_blocks,
        _grant_late_trust,
        _linked_approvals,
        _orphan_rejects,
        _pending_approvals,
        _PendingApproval,
        _resolve_linked_click,
        _safe_update,
        _SlackTurn,
        _trusted_sessions,
        add_trusted_session,
        event_is_spawn_run,
        hook_gate_busy,
        hook_gate_kwargs,
        is_allowed_user,
        is_yolo_mode,
        logger,
        name_grant,
        redact_credentials,
        redact_exfiltration_urls,
        run_in_tool_gate_pool,
        sel,
        steer_refusal_notice,
        tool_approval_timeout_secs,
    )


def _should_auto_approve_spawn(context_builder, event) -> bool:
    """Check if a spawn_run tool call should be auto-approved.

    Takes the PERMISSION EVENT, not the title: the title is model-authored
    (a shell command's title can be forged to ``spawn_run``), so the check
    keys on ``event_is_spawn_run``'s canonical identity.
    """
    return bool(
        context_builder
        and context_builder.hooks
        and context_builder.hooks.auto_approve_subagent_spawn
        and event_is_spawn_run(event)
    )


async def _screen_tool_call(turn: _SlackTurn, event: LLMEvent) -> None:
    """Screen and audit a tool call the provider is already running.

    ``_run_turn``'s ``EVENT_TOOL_CALL`` branch: the hook check here can only warn,
    because the call is informational and the tool is already executing.
    """
    context_builder = turn.context_builder
    session_key = turn.session_key
    _agent = turn._agent
    answer = turn.answer
    answer.tool_gap = True
    # Check tool hooks. NOTE: EVENT_TOOL_CALL is informational —
    # the tool has already been auto-approved by the provider and
    # is executing; this branch cannot reject_tool(). The real
    # enforceable gate is EVENT_PERMISSION_REQUEST below. So we do
    # NOT arm deny-by-default here (is_shell omitted): a shell tool
    # with an unrecoverable command would otherwise render a
    # misleading "blocked" message while the tool actually runs.
    # For the same reason this site deliberately does NOT use
    # ``hook_gate_kwargs`` (the shared extraction every enforcing
    # permission-request site threads): the params/diff-path tiers
    # it would arm can also deny a call that is already executing,
    # and this warning must never claim to have blocked one. The
    # structural test in test_hooks.py names this site as the one
    # informational exception. A genuine deny-list / sensitive-path
    # match still surfaces a (best-effort, non-enforcing) warning +
    # audit.
    if context_builder:
        tool_result = await run_in_tool_gate_pool(
            context_builder.hooks.on_tool_call,
            event.title,
            # Unjudged, so nothing to warn about: this site only
            # warns on a deny and cannot stop the running tool.
            on_queue_timeout=lambda _waited: ToolHookResult(action=TOOL_ALLOW),
            session_key=session_key,
            agent=_agent or "",
            command=event.shell_command,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            mcp_identity_trusted=event.mcp_identity_trusted,
        )
        if tool_result.action == TOOL_DENY:
            # event.title is LLM-authored (select_tool_title prefers
            # the model's description) — never post it to Slack raw.
            _flagged_title, _ = redact_exfiltration_urls(event.title)
            _flagged_title, _ = redact_credentials(_flagged_title)
            answer.accumulated += (
                f"\n⚠️ _Tool `{_flagged_title}` flagged by security "
                f"hooks (already executing; cannot be stopped here)._"
            )
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name=event.title,
                tool_kind=event.tool_kind,
                outcome="flagged_unenforceable",
                error="hook_deny",
            )
            return

    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name=event.title,
        tool_kind=event.tool_kind,
        outcome="invoked",
    )
    await answer.on_tool_call(event)


async def _answer_permission_request(
    turn: _SlackTurn, client: LLMProvider, event: LLMEvent
) -> bool:
    """Answer one permission request: True when the person rejected it.

    ``_run_turn``'s ``EVENT_PERMISSION_REQUEST`` branch, the enforcing gate, in its
    order: the hook verdict (an auto-approve by name, or a deny steered and rejected),
    the ``auto_approve_subagent_spawn`` rung, the approval mode, Trust and YOLO, then
    the interactive prompt. A rejected prompt ends the stream (``True``).
    """
    slack = turn.slack
    channel = turn.channel
    reply_ts = turn.reply_ts
    approval_mode = turn.approval_mode
    context_builder = turn.context_builder
    session_key = turn.session_key
    _agent = turn._agent
    status_ctrl = turn.status_ctrl
    answer = turn.answer
    task = turn.task
    # Check tool hooks for auto-approve
    if context_builder:
        tool_result = await run_in_tool_gate_pool(
            context_builder.hooks.on_tool_call,
            event.title,
            on_queue_timeout=hook_gate_busy,
            session_key=session_key,
            agent=_agent or "",
            **hook_gate_kwargs(event),
        )
        if tool_result.action == TOOL_AUTO_APPROVE:
            # The hook granted this by NAME (its `auto_approve_tools`
            # globs, or the read-only allowlist). Honour it only
            # while each program name in the command still resolves
            # to the program it appears to name; a shadowed,
            # agent-tree or unidentified resolution DOWNGRADES to
            # the remaining rungs below (spawn hook, approval mode,
            # trust/YOLO, the interactive buttons) — never a hard
            # block.
            _ng_refusal = await name_grant.refusal_for_event(event)
            if _ng_refusal is None:
                approval_sent = await client.approve_tool(event.request_id)
                if approval_sent is False:
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="slack",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                        request_id=event.request_id,
                    )
                    return False
                Stats().inc_tool_auto_approved()
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="slack",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="auto_approved",
                    request_id=event.request_id,
                    metadata={"reason": "hook_auto_approve"},
                )
                return False
            logger.warning(
                "declining a hook auto-approve: %s; the request "
                "falls through to the Slack handler's normal "
                "approval ladder",
                _ng_refusal.log_text,
            )
            name_grant.log_decline(
                source="slack",
                session_key=session_key,
                event=event,
                refusal=_ng_refusal,
                tier="hook_auto_approve",
                sel_factory=sel,
            )
        if tool_result.action == TOOL_DENY:
            # Audit FIRST, then steer, then reject: the steer and
            # the reject both await the ACP pipe, and a backend that
            # stops reading stdin cancels this coroutine at the
            # turn deadline -- an SEL row sequenced after them
            # never runs (the chat runner's audit-first rule).
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name=event.title,
                tool_kind=event.tool_kind,
                outcome="denied",
                request_id=event.request_id,
                error="hook_deny",
            )
            # A hook deny is a HOST verdict on the call, not the
            # person's: tell the model so in-band before the reject
            # hands it kiro-cli's "User denied tool execution".
            await _steer_host_deny(
                client,
                event,
                tool_result.reason,
                cause=DENY_CAUSE_POLICY,
                audited=True,
            )
            await client.reject_tool(event.request_id)
            Stats().inc_tool_denial()
            # event.title is LLM-authored — redact before posting.
            _blocked_title, _ = redact_exfiltration_urls(event.title)
            _blocked_title, _ = redact_credentials(_blocked_title)
            answer.accumulated += f"\n🚫 _Tool `{_blocked_title}` blocked by hooks._"
            return False

    # auto_approve_subagent_spawn → auto-approve spawn_run tool calls
    if _should_auto_approve_spawn(context_builder, event):
        approval_sent = await client.approve_tool(event.request_id)
        if approval_sent is False:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name=event.title,
                tool_kind=event.tool_kind,
                outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                request_id=event.request_id,
            )
            return False
        Stats().inc_tool_auto_approved()
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome="auto_approved",
            request_id=event.request_id,
            metadata={"reason": "auto_approve_subagent_spawn"},
        )
        return False

    if approval_mode == APPROVAL_AUTO:
        approval_sent = await client.approve_tool(event.request_id)
        if approval_sent is False:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name=event.title,
                tool_kind=event.tool_kind,
                outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                request_id=event.request_id,
            )
            return False
        Stats().inc_tool_auto_approved()
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome="auto_approved",
            request_id=event.request_id,
            metadata={"reason": "approval_mode_auto"},
        )
        return False

    # Trust mode (per-session) or YOLO mode (owner-only global) → auto-approve
    _yolo_now = is_yolo_mode()
    if _yolo_now or session_key in _trusted_sessions:
        approval_sent = await client.approve_tool(event.request_id)
        if approval_sent is False:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name=event.title,
                tool_kind=event.tool_kind,
                outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                request_id=event.request_id,
            )
            return False
        Stats().inc_tool_auto_approved()
        logger.info(
            "Auto-approved %s (%s)",
            event.title,
            "yolo" if _yolo_now else "trust",
        )
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome="auto_approved",
            request_id=event.request_id,
            metadata={"reason": "yolo" if _yolo_now else "trust"},
        )
        return False

    logger.info("Permission request: tool=%s req_id=%s", event.title, event.request_id)
    status_ctrl.pause_stall_watchdog()
    task.await_approval()
    # The stream-prep Slack calls below run BEFORE _request_approval
    # answers the permission. If any raises (rate-limit, network),
    # the ACP permission request would be orphaned and the
    # subprocess would wedge — reject it before propagating so the
    # turn unblocks. _request_approval guards its own post failure.
    try:
        await answer.prepare_for_approval()
    except Exception:
        await _reject_orphaned_tool(client, event.request_id)
        raise

    outcome = await _request_approval(
        slack,
        client,
        channel,
        reply_ts,
        event,
        session_key,
        is_dm=channel.startswith("D"),
    )
    task.resume()
    status_ctrl.resume_stall_watchdog()
    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name=event.title,
        tool_kind=event.tool_kind,
        outcome="approved" if outcome != _OUTCOME_REJECTED else "rejected",
        request_id=event.request_id,
        metadata={"reason": "interactive"},
    )
    if outcome == _OUTCOME_REJECTED:
        await answer.on_tool_rejected()
        return True
    return False


async def _reject_orphaned_tool(
    provider: LLMProvider, request_id: "str | int", *, audit: bool = True
) -> bool:
    """Reject a pending ACP permission request that cannot be surfaced.

    Both the pre-approval stream-prep and the approval-prompt post happen BEFORE
    the permission is answered; if either raises, the ACP request would be left
    unanswered and the agent subprocess wedges forever (every later turn blocks
    behind it). Callers invoke this on failure, then re-raise. Swallows any
    reject failure, and audit failure after a successful rejection, so the
    original error still propagates. ``audit=False`` is for a caller whose
    decision already has its SEL row (the audit-first deny sites): the wire
    still gets answered, but the ledger is append-only and a second row for
    one decision would be a duplicate nothing reconciles.
    """
    try:
        await provider.reject_tool(request_id)
    except Exception:
        logger.warning("Failed to reject orphaned tool %s", request_id, exc_info=True)
        return False
    # The fallback arms re-raise past the normal permission audit, so record
    # the denial here: a rejection that reached the wire but never reached the
    # audit trail is a silent gap in a security control.
    if not audit:
        return True
    try:
        sel().log_tool_invocation(
            session_key="",
            source="slack",
            tool_name="",
            outcome="rejected",
            request_id=request_id,
            metadata={"reason": "orphaned_fallback_reject"},
        )
    except Exception:
        logger.warning("Failed to audit orphaned tool %s", request_id, exc_info=True)
    return True


async def _steer_host_deny(
    provider: Any, event: Any, reason: str, *, cause: str, audited: bool
) -> None:
    """Tell the model, in-band, that the HOST denied this call -- not the person.

    A rejected permission reaches the model as kiro-cli's fixed "User denied
    tool execution", so without this it reads a refusal that never happened.
    Awaited immediately BEFORE a host-deny ``reject_tool`` in this module: while
    the permission request is unanswered the turn is provably in flight, which
    is what gets the notice queued rather than dropped (``kiro_crew.deny_notice``).
    The Slack handler has two host denies -- a hook ``deny`` on the message
    path (``DENY_CAUSE_POLICY``, the hook's reason) and the approval prompt
    expiring unanswered (``DENY_CAUSE_APPROVAL_TIMEOUT``). *cause* is REQUIRED
    because the wrong noun sends the model the wrong way. The two genuine USER
    rejections (a Deny click in ``handle_interaction``) and the teardown-only
    ``_reject_orphaned_tool`` must NOT call this: there kiro-cli's wording is
    the truth, and "this was NOT a user action" would be a lie.
    ``test_messaging_deny_notice`` walks the file to keep both halves honest.

    *reason* may echo agent-authored text (a hook's reason quotes the matched
    path), so it is redacted here; the shared helper redacts the title.
    Best-effort by construction: ``steer_refusal_notice`` probes the capability
    and swallows every failure, so a backend without a steer channel behaves
    exactly as before and the caller's reject always runs.

    Cancellation mid-steer (teardown) must still answer the wire: a stranded
    ``session/request_permission`` blocks the subprocess forever and wedges
    every later turn behind it. The reject is scheduled as a strongly referenced
    referenced task and awaited through ``asyncio.shield`` so it is stepped
    while this coroutine unwinds; ``_reject_orphaned_tool`` retrieves its
    exception so teardown stays quiet. *audited* is REQUIRED and says whether
    the caller wrote the decision's SEL row BEFORE this await (the hook deny
    does) or writes it after the wire (the approval-timeout arm, whose
    caller audits both outcomes once the request is answered). The orphan
    reject audits only in the second case: the SEL ledger is append-only,
    and a decision already on it must not gain a second row nothing
    reconciles.
    """
    safe_reason, _ = redact_exfiltration_urls(reason or "")
    safe_reason, _ = redact_credentials(safe_reason)
    try:
        await steer_refusal_notice(
            provider,
            str(getattr(event, "title", "") or ""),
            safe_reason,
            cause=cause,
            bound_secs=_STEER_NOTICE_BOUND_SECS,
        )
    except asyncio.CancelledError:
        reject = asyncio.ensure_future(
            _reject_orphaned_tool(provider, event.request_id, audit=not audited)
        )
        _orphan_rejects.add(reject)
        reject.add_done_callback(_orphan_rejects.discard)
        with contextlib.suppress(BaseException):
            if await asyncio.shield(reject):
                Stats().inc_tool_denial()
        raise


async def _request_approval(
    slack: SlackClientOps,
    provider: LLMProvider,
    channel: str,
    thread_ts: str,
    event: LLMEvent,
    session_key: str = "",
    is_dm: bool = True,
) -> str:
    """Post approval buttons, wait for click, return 'approved' or 'rejected'."""
    blocks = _build_approval_blocks(event, is_dm=is_dm)
    # If posting the approval prompt fails, the ACP permission request would
    # otherwise be left unanswered — the subprocess blocks forever and every
    # later turn wedges behind it. Reject the tool before re-raising so the
    # turn unblocks and the caller's error path can run.
    try:
        approval_ts = await slack.post_blocks(
            channel, blocks, "Manual approval required", thread_ts
        )
    except Exception:
        await _reject_orphaned_tool(provider, event.request_id)
        raise

    key = f"{channel}:{approval_ts}"
    pending = _PendingApproval(provider, event.request_id, session_key)
    _pending_approvals[key] = pending
    approval_timeout = (
        _APPROVAL_TIMEOUT if _APPROVAL_TIMEOUT is not None else tool_approval_timeout_secs()
    )

    try:
        # shield: on timeout, wait_for would otherwise CANCEL the future, and a
        # click that claimed the entry just before the deadline could then
        # never deliver its real outcome (its set_result guards on done()).
        outcome = await asyncio.wait_for(asyncio.shield(pending.future), timeout=approval_timeout)
    except asyncio.TimeoutError:
        outcome = _OUTCOME_REJECTED
        # Claim the decision BEFORE awaiting anything: while the entry stays
        # registered, a Slack click landing inside the steer window would take
        # the live-approval branch and answer the same permission request a
        # second time. The pop's result says who won: a click that claimed the
        # entry first is answering (or already answered) the request itself, so
        # steering "expired unanswered" then would hand the model a false
        # cause. The finally pop is idempotent, and a late click hits the
        # already-resolved path.
        claimed = _pending_approvals.pop(key, None) is not None
        # Steer FIRST, reject SECOND: while the permission request is still
        # unanswered the turn is provably in flight, so the notice is queued
        # rather than dropped, and the model learns the denial was an expired
        # prompt instead of concluding a human refused the call, matching the
        # dashboard chat runner's host-decline arms. On Slack the driver stops
        # rendering after a rejection, so this corrects the model-side
        # transcript attribution only; the notice's continue-guidance has no
        # Slack consumer. Best-effort: _steer_host_deny (capability probe,
        # redaction, build, bounded send -- the same shared helper the
        # messaging TurnDriver uses) swallows every failure, so the reject
        # below still runs; a cancellation mid-steer schedules the orphan
        # reject itself before re-raising, so teardown still answers the wire.
        if claimed:
            await _steer_host_deny(
                provider,
                event,
                "the Slack approval prompt went unanswered for "
                f"{max(1, round(approval_timeout))}s",
                cause=DENY_CAUSE_APPROVAL_TIMEOUT,
                # The caller audits this outcome after the wire is answered;
                # a cancellation here would skip that row, so the orphan
                # reject writes it.
                audited=False,
            )
        if claimed:
            # Only the claim winner answers the wire. A lost claim means a
            # click is answering (or answered) this request itself; a second
            # answer would hit the ACP client's popped-options fallback, whose
            # cancelled outcome cancels the WHOLE turn. The click's own wire
            # failure cannot strand the request either: handle_interaction
            # answers the wire itself when its approve/reject raises.
            await provider.reject_tool(event.request_id)
            Stats().inc_tool_denial()
        else:
            # A click beat the deadline and owns the answer. The future was
            # shielded from the timeout's cancellation, so it still carries the
            # click's REAL decision: await it until the click resolves it, and
            # report THAT. No bound and no fabricated fallback: the click is the
            # sole responder and every way it can end resolves this future --
            # its approve/reject completes (set_result in handle_interaction),
            # its write raises (handle_interaction self-answers the wire, then
            # set_result), or the backend stops reading stdin for good, which
            # the ACP client's tool-stall watchdog turns into a transport close
            # that raises out of the parked write and lands on the same path.
            # Returning "rejected" on a timer instead would close this stream
            # over a tool the person approved and that still executes.
            outcome = await asyncio.shield(pending.future)
    finally:
        _pending_approvals.pop(key, None)

    try:
        await slack.delete_message(channel, approval_ts)
    except Exception:
        status = "✅ Approved" if outcome == _OUTCOME_APPROVED else "🚫 Rejected"
        title_safe, _ = redact_exfiltration_urls(event.title)
        title_safe, _ = redact_credentials(title_safe)
        await _safe_update(slack, channel, approval_ts, f"🔐 *{title_safe}* — {status}")

    return outcome


async def handle_interaction(
    channel: str,
    msg_ts: str,
    action_id: str,
    user_id: str = "",
    thread_ts: str = "",
    slack: SlackClientOps | None = None,
    sessions: SessionManager | None = None,
) -> str | None:
    """Handle a Block Kit button click for tool approval.

    Supports four actions:
    - approve_tool: approve this one tool call
    - trust_tool: auto-approve all tools for this session (thread)
    - reject_tool: reject this tool call

    Security: rejects non-owner clicks. Trust requires DM channel
    (verified via conversations.info by the gateway caller).
    """

    # Deny-by-default: reject unless positively confirmed as allowed
    if not user_id or not is_allowed_user(user_id):
        logger.warning(
            "Rejecting interactive action from unauthorized user %s (action=%s)", user_id, action_id
        )
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.interactive.approval",
            outcome="denied",
            source="slack",
            resources=action_id,
            error="unauthorized user",
        )
        return None

    key = f"{channel}:{msg_ts}"

    # Linked-dashboard-slot approval: the dashboard's _run_chat owns the ACP
    # answer (it is parked on the slot's approval future). Resolve ONLY that
    # future here via state.resolve_approval — do NOT call approve_tool/reject
    # (that would answer the JSON-RPC request twice). Anything that isn't an
    # explicit reject approves THIS call; a Trust click additionally widens the
    # session, and only a widening that actually took counts as an approval.
    linked_entry = _linked_approvals.get(key)
    if linked_entry is not None:
        return _resolve_linked_click(linked_entry, key, action_id, user_id, sessions)

    # Claim-before-await, symmetric with the timeout arm: popping here (not
    # at the end) means a timeout firing while this click awaits the wire
    # sees a lost claim and stays entirely off it — only the claim winner may
    # answer, because a second answer to the same request id lands in the ACP
    # client's popped-options cancelled-outcome fallback and cancels the whole
    # turn. It also means the timeout arm's own pop cannot leave this path
    # deleting a missing key.
    pending = _pending_approvals.pop(key, None)
    if not pending:
        # Approval already resolved (approved/rejected/timed out).
        # For trust clicks, still set trust using the thread as session key.
        # Replicate session_key derivation from handle_message: thread_ts,
        # then check for linked dashboard session override.
        if action_id == _ACTION_TRUST and thread_ts:
            return await _grant_late_trust(channel, thread_ts, user_id, slack, sessions)
        else:
            logger.warning("No pending approval for %s", key)
            sel().log_api_access(
                caller=user_id or "unknown",
                operation="slack.interactive.approval",
                outcome="denied",
                source="slack",
                resources=key,
                error="no_pending_approval",
            )
        return None

    # Everything below runs with the entry CLAIMED: the pop above means no
    # later claimer exists, and _request_approval's lost-claim arm is awaiting
    # ``pending.future`` unbounded on the promise that every way this click can
    # end resolves it. The wire calls kept that promise through their own
    # fallback arms, but the synchronous bookkeeping between the claim and
    # ``set_result`` (trust grant, audit, stats) could raise and return with
    # the wire unanswered and the future unresolved — parking that waiter
    # permanently. One guard over the WHOLE claimed region keeps the promise
    # on every exit; it subsumes the two per-wire-call fallback arms it
    # replaces. approve_tool pops the recorded options before sending, so the
    # guard's fallback reject can land as a cancelled outcome (ends the turn's
    # remaining tool calls) — still strictly better than a wedged subprocess.
    floor_refused = False
    try:
        if action_id in (_ACTION_APPROVE, _ACTION_TRUST):
            # Set trust state BEFORE approving (so subsequent tools auto-approve)
            if action_id == _ACTION_TRUST:
                if not is_allowed_user(user_id):
                    logger.error("Rejecting trust escalation from non-allowed user %s", user_id)
                    sel().log_api_access(
                        caller=user_id,
                        operation="slack.interactive.trust_denied",
                        outcome="denied",
                        source="slack",
                        resources=pending.session_key or "",
                        error="non-allowed user",
                    )
                    if not pending.future.done():
                        pending.future.set_result(_OUTCOME_REJECTED)
                    return _ACTION_REJECT
                elif pending.session_key:
                    add_trusted_session(pending.session_key, sessions)
                    logger.info("Trust mode ON for session %s", pending.session_key)
                else:
                    logger.warning(
                        "No session_key on pending approval %s; approving without trust", key
                    )
            approval_sent = True
            if pending.provider:
                approval_sent = await pending.provider.approve_tool(pending.request_id)
            if not pending.future.done():
                pending.future.set_result(
                    _OUTCOME_APPROVED if approval_sent is not False else _OUTCOME_REJECTED
                )
            if approval_sent is not False:
                Stats().inc_tool_approval()
            else:
                # The transport's gate refused the call: the card must not
                # be relabelled as approved.
                floor_refused = True
            sel().log_api_access(
                caller=user_id,
                operation="slack.interactive.approval",
                outcome=(
                    "allowed" if approval_sent is not False else OUTCOME_REJECTED_TRANSPORT_FLOOR
                ),
                source="slack",
                resources=action_id,
            )
        else:
            if pending.provider:
                await pending.provider.reject_tool(pending.request_id)
            if not pending.future.done():
                pending.future.set_result(_OUTCOME_REJECTED)
            sel().log_api_access(
                caller=user_id,
                operation="slack.interactive.approval",
                outcome="denied",
                source="slack",
                resources=action_id,
            )
    except BaseException:
        # An unresolved future is the signal the exit was abnormal: both arms
        # resolve it immediately after their wire call, so reaching here with
        # it pending means the wire may be unanswered and the lost-claim
        # waiter is still parked. Answer best-effort and release the waiter
        # before propagating; _reject_orphaned_tool swallows its own failure,
        # so the original error still surfaces.
        if not pending.future.done():
            if pending.provider:
                await _reject_orphaned_tool(pending.provider, pending.request_id)
            pending.future.set_result(_OUTCOME_REJECTED)
        raise

    return _ACTION_REJECT if floor_refused else action_id
