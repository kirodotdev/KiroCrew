"""A permission request a dashboard turn puts to its user: the batch-rejection cascade's
auto-reject, the approval card, the wait and what the answer does."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Literal

from kiro_crew.dashboard.chat_utils import _tool_identity_fields, slack_mirror_is_paused
from kiro_crew.dashboard.state import (
    DENY_CAUSE_APPROVAL_NO_BUDGET,
    DENY_CAUSE_APPROVAL_TIMEOUT,
    DENY_CAUSE_APPROVAL_UNDELIVERABLE,
    DENY_CAUSE_BATCH_CASCADE,
    _mark_permission_resolved,
)
from kiro_crew.dashboard.turn_dispatch import (
    APPROVAL_EXPIRED_NOTE_TITLE,
    format_approval_expired_note,
    format_approval_no_budget_card,
    format_approval_timeout_card,
)
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE
from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR
from kiro_crew.security.readonly_bash import (
    command_invokes_git,
    is_read_only_bash,
    unsafe_bash_reason,
)
from kiro_crew.slack.handler import resolve_linked_approval
from kiro_crew.trust_patterns import approval_command, approval_display_command
from kiro_crew.trust_patterns import extract_bash_command as _extract_bash_command

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _MAX_TOOL_PURPOSE,
        _extract_base_command,
        _extract_full_command,
        _redact_tool_field,
        _validate_tool_name,
        crew_log_emit,
        logger,
        pin_human_approval,
        post_linked_approval,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        tool_approval_timeout_secs,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import (
        _redact_display_text,
        _reject_attributed,
        _reject_hook_blocked,
        _reject_hook_error,
        _reject_invalid_tool,
        _steer_policy_notice,
    )
    from kiro_crew.dashboard.chat_turn.tool_approval import _pre_tool_hooks_should_block
    from kiro_crew.dashboard.chat_turn.tool_rows import _tool_meta
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _wt_note_approved
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _fire


async def _on_permission_prompt(
    turn: _ChatTurn, event
) -> Literal[_Flow.BREAK, _Flow.CONTINUE] | None:
    """Put a permission request to the user: the approval card, the wait and what the answer does."""
    _batch_cascade_steered = turn._batch_cascade_steered
    _child_lf_warning = turn._child_lf_warning
    _crew_log_sid = turn._crew_log_sid
    _crew_log_turn_no = turn._crew_log_turn_no
    _declined_grant_notices = turn._declined_grant_notices
    _refusal_notices = turn._refusal_notices
    _refusal_reasons = turn._refusal_reasons
    client = turn.client
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # Auto-reject remaining tools after one rejection in a batch
    if getattr(slot, "_batch_rejected", False):
        _title = _redact_display_text(event.title)
        _cascade_cause = getattr(slot, "_batch_rejected_cause", "")
        # Audit FIRST, before any wire I/O for this decision (see
        # _reject_hook_blocked): a stalled ACP reader cancels this
        # coroutine at the turn deadline, and an SEL write sequenced
        # after the awaits never runs.
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name=_title,
            tool_kind=event.tool_kind,
            outcome="rejected",
            request_id=event.request_id,
            # Additive provenance: an auditor separating "the user
            # refused this group" from "a host auto-decline cut it
            # short" needs the cause on the cascaded members too.
            metadata=(
                {"reason": "batch_rejection", "cause": _cascade_cause}
                if _cascade_cause
                else {"reason": "batch_rejection"}
            ),
        )
        if _cascade_cause and not _batch_cascade_steered:
            # Host-caused cascade: the batch's originating decline
            # was an auto-decline (approval timeout, no budget,
            # Slack delivery failure), so kiro-cli's "User denied
            # tool execution" is FALSE for every cascaded member.
            # Steer the real cause in-band, BEFORE the rejection
            # goes on the wire (the unanswered permission request is
            # what keeps the steer queued instead of dropped). One
            # notice covers the whole cascaded remainder; the latch
            # is the steer's own return value, so later members skip
            # the send once a notice is actually on the wire, while
            # a failed or refused attempt leaves the latch clear and
            # the next member retries. A throwaway notices list on
            # purpose — the batch's ORIGINAL decline already renders
            # its own card, and a best-effort correction of cascade
            # attribution is not worth a second billed recovery turn
            # when steering is unavailable (that leaves exactly
            # today's behaviour). slot/state withheld for the same
            # reason: each cascaded tool already appends a visible
            # 🚫 row, and the inject card's summary line is keyed on
            # causes this one is not.
            _batch_cascade_steered = await _steer_policy_notice(
                client,
                _title,
                _cascade_cause,
                [],
                cause=DENY_CAUSE_BATCH_CASCADE,
            )
            turn._batch_cascade_steered = _batch_cascade_steered
        # User-originated cascade. When the
        # person themselves denied the tool that started this
        # cascade, that refusal covers the group they refused, so
        # kiro-cli's "User denied tool execution" is the TRUE
        # attribution for the cascaded remainder and steering a
        # host notice would re-attribute the user's own decision.
        # Host-caused cascades are corrected by the
        # provenance-gated steer above.
        await _reject_attributed(
            client, event.request_id, attribution="exempt: user-originated cascade"
        )
        slot.append("tool", f"🚫 {_title} (rejected)", "msg msg-tool", meta=_tool_meta(event))
        # Mark the permission as resolved so UI shows rejection
        perm_meta: dict[str, str] = {
            "request_id": str(event.request_id),
            "tool_call_id": event.tool_call_id or "",
            "resolved": "rejected",
        }
        slot.append("permission", _title, json.dumps(perm_meta))
        logger.warning("AUTO-REJECTED tool=%r (batch rejection)", event.title)
        return _Flow.CONTINUE
    # Interactive approval — send to frontend, wait for decision
    #
    # WHO/WHAT declines this tool, when it is declined. Both stay
    # empty for an interactive user refusal; each host-side
    # auto-decline below (Slack delivery failure, no turn budget,
    # approval timeout) overwrites the cause with its DENY_CAUSE_*
    # constant and the reason with a short host-authored sentence.
    # The cause gates the provenance steer at the shared reject
    # branch below, and the batch setter at the bottom copies the
    # reason onto ``slot._batch_rejected_cause`` so the cascade
    # site can tell a person's refusal from a host auto-decline.
    _host_deny_cause = ""
    _host_deny_reason = ""
    # A turn cancelled mid-approval (slot deletion cancels the task)
    # unwinds through the ``finally`` below with no cause set, so the
    # decision would read as an anonymous rejection -- which the ledger
    # scores as a person's answer. This flag lets that path attribute
    # the decision to the host, the way the timeout and no-budget
    # declines already do.
    _host_cancelled = False
    perm_meta = {
        "request_id": str(event.request_id),
        "tool_call_id": event.tool_call_id or "",
    }
    if event.tool_purpose:
        perm_meta["tool_purpose"] = _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE)
    if event.tool_input:
        # Security: scan for exfiltration URLs and credentials
        sanitized, _ = redact_exfiltration_urls(event.tool_input)
        sanitized, _ = redact_credentials(sanitized)
        perm_meta["tool_input"] = sanitized
    # Flag read-only bash commands for context-aware buttons
    cmd = _extract_bash_command(event.tool_input) if event.tool_input else ""
    if cmd:
        # Same rule as _on_permission_request's trust-reads branch, so
        # the card never offers a trust-reads grant that would not cover git.
        perm_meta["is_read_only"] = (
            "1" if is_read_only_bash(cmd) and not command_invokes_git(cmd) else ""
        )
    # Pre-compute consent fields for the TrustDropdown.  The title
    # is presentation text and can be model-authored; shell grant
    # scope therefore comes only from the canonical command in
    # ``tool_input``.  Redaction-changing values are displayed but
    # are not grantable: a user cannot consent to hidden bytes.
    _safe_title, _ = redact_exfiltration_urls(event.title)
    _safe_title, _ = redact_credentials(_safe_title)
    perm_meta["tool_title"] = _safe_title
    perm_meta["is_shell"] = "1" if event.is_shell else ""
    # Kind + trusted identity let the approval card derive the same
    # human title the tool row shows (utils/toolCallTitle). All
    # optional there: a card missing them still renders its command.
    perm_meta["tool_kind"] = _redact_tool_field(event.tool_kind or "", limit=64)
    perm_meta.update(_tool_identity_fields(event))
    _trust_key = approval_command(
        event.tool_input or "",
        is_shell=event.is_shell,
        tool_name=event.tool_name,
        mcp_server_name=event.mcp_server_name,
        raw_tool_params=event.raw_tool_params,
    )
    _display_command = approval_display_command(
        event.tool_input or "",
        is_shell=event.is_shell,
        tool_name=event.tool_name,
        mcp_server_name=event.mcp_server_name,
        raw_tool_params=event.raw_tool_params,
    )
    _full = _extract_full_command(_display_command)
    _safe_full, _ = redact_exfiltration_urls(_full)
    _safe_full, _ = redact_credentials(_safe_full)
    # ``tool_input`` is already display-redacted by both ACP
    # transports.  Re-running the redactors cannot reveal that
    # upstream removed secret bytes, so retain and enforce the
    # transport's boolean provenance as well.  It contains no
    # secret itself and prevents two different hidden commands
    # from collapsing to one durable trust pattern.
    _command_grantable = (
        bool(_full) and bool(_trust_key) and not event.tool_input_redacted and _safe_full == _full
    )
    if _command_grantable:
        perm_meta["full_command"] = _safe_full
        # Authorization authority stays distinct from the
        # wire-compatible display label.  MCP server/tool names may
        # both contain ``__``; the internal key component-encodes
        # them so two different pairs cannot share a durable grant.
        perm_meta["trust_command_key"] = _trust_key
        perm_meta["trust_command_grantable"] = "1"
    # A broad per-slot Trust click is still a durable grant, so it
    # carries an explicit server proof: an alternate approval
    # surface must not offer it merely because it received a
    # pending card.  The proof is deliberately NOT conditioned on
    # ``_command_grantable``.  The session grant names no command —
    # it auto-approves whatever this slot asks for next — so the
    # bytes of THIS call are not what the user consents to, and
    # their derivability cannot bear on it.  Conditioning it there
    # removed the entire Trust menu, every tier, from any card
    # whose command the transport redacted or could not
    # canonicalize; a plain ``cd`` request then rendered with
    # allow-once and reject alone.  The command-scoped tiers above
    # stay gated, because those DO name the bytes being consented
    # to.  This matches the fidelity split already documented on
    # ``child_unconditional_grant_eligible``, where trust-all is an
    # unconditional grant and only content-matching paths need the
    # canonical command.
    perm_meta["trust_grantable"] = "1"
    _base = _extract_base_command(_trust_key) if event.is_shell else ""
    _safe_base, _ = redact_exfiltration_urls(_base)
    _safe_base, _ = redact_credentials(_safe_base)
    if _command_grantable and _base and _safe_base == _base:
        perm_meta["base_command"] = _safe_base
        perm_meta["trust_base_grantable"] = "1"
    # A decline known before the row exists is born into the row. Spec:
    # docs/system-specs/modules/app-notifications.md, "Sound events" (permission row).
    if tool_approval_timeout_secs() <= 0:
        perm_meta["resolved"] = "rejected"
    else:
        # Only a card someone can still answer gets the held notices.
        for _notice in _declined_grant_notices.values():
            slot.append("system", _notice, "msg msg-info")
    permission_row = slot.append(
        "permission",
        f"{_child_lf_warning}{_safe_title}" if _child_lf_warning else _safe_title,
        json.dumps(perm_meta),
    )
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    slot.register_approval(str(event.request_id), fut, permission_row)
    # Push via global SSE AFTER registering the future, so the
    # slot dict reflects pending_approval=true and Board cards
    # move into the Blocked lane without a browser refresh.
    state.push_slots_update()
    # Mirror the prompt to the linked Slack thread so a user driving
    # this session from Slack can actually answer it. Without this,
    # the prompt only renders in the dashboard and a Slack-only user
    # never sees it — the turn then parks here for the whole approval
    # window, holding the slot lock and silently dropping inbound
    # messages.
    # The Slack click resolves THIS future (via state.resolve_approval);
    # we remain the sole caller of approve_tool/reject_tool below.
    _slack_approval_ts: str | None = None
    if (
        # A row pre-declined at its append is not mirrored. The
        # post is the one cancellable await between the future's
        # registration above and the ``try``/``finally`` below
        # that pops it, and its ``except Exception`` cannot see
        # the ``CancelledError`` a turn ceiling raises inside it:
        # a pre-declined prompt that went through the post could
        # leave a future nothing resolves -- the Board keeps the
        # session Blocked and Continue answers 409 until a reload
        # or a slot switch rejects it -- for a card the
        # ``finally`` would delete the moment the post returned.
        # Skipping the post leaves nothing on this path that
        # awaits before that ``finally``, so the decline is
        # recorded and the future popped exactly as the
        # interactive path does it.
        "resolved" not in perm_meta
        and slot._slack_linked
        and slot._slack_channel
        and slot._slack_thread_ts
        and state.slack_client
        # An approval prompt is turn output too, and it asks for a
        # decision. Posting one into a thread the user disconnected
        # would solicit an answer where they are not looking;
        # the dashboard carries the same prompt.
        and not slack_mirror_is_paused(state, session_key)
    ):
        try:
            _slack_approval_ts = await post_linked_approval(
                state.slack_client,
                slot._slack_channel,
                slot._slack_thread_ts,
                event.request_id,
                session_key,
                (f"{_child_lf_warning}{event.title}" if _child_lf_warning else event.title),
                event.tool_input or "",
            )
            if _slack_approval_ts is None and not fut.done():
                # Delivery failed — do not park for the whole
                # approval window.
                # Resolve the future now (reject) and tell the user
                # the prompt could not be delivered, so they retry
                # rather than wait. The dashboard still rendered the
                # prompt, so a dashboard user could also answer; but
                # resolving keeps a Slack-only user from being stuck.
                # The card is retired on every window by the
                # ``approval_resolved`` frame the ``finally`` sends
                # the moment this decline is recorded.
                logger.warning(
                    "Linked approval delivery to Slack failed; auto-rejecting tool %r",
                    event.title,
                )
                slot.append(
                    "assistant",
                    "\u26a0\ufe0f A tool approval was required but I couldn't post it to "
                    "this Slack thread, so I auto-declined it. Please retry, or "
                    "approve from the dashboard.",
                    "msg msg-a",
                )
                state.push_slots_update()
                fut.set_result("rejected")
                _host_deny_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
                _host_deny_reason = "the approval prompt could not be delivered to Slack"
            elif _slack_approval_ts is None:
                # Delivery failed, but the dashboard answered the
                # prompt while the post was in flight: that answer
                # stands, the runner declines nothing, and the user
                # gets no notice saying the prompt was auto-declined.
                logger.warning(
                    "Linked approval delivery to Slack failed after the prompt "
                    "was answered from the dashboard; keeping that answer for "
                    "tool %r",
                    event.title,
                )
        except Exception:
            # Any failure before the future is resolved (ImportError,
            # post_linked_approval raising, slot.append/push raising in
            # the delivery-failure branch) would otherwise fall through
            # to the approval wait_for with an unresolved future and wedge
            # the turn. Auto-reject so the turn unblocks,
            # mirroring the _slack_approval_ts is None branch.
            logger.warning("Error mirroring approval prompt to Slack", exc_info=True)
            if not fut.done():
                fut.set_result("rejected")
                _host_deny_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
                _host_deny_reason = "the approval prompt could not be delivered to Slack"
    # Pre-seeded so the `finally` backstop below is total over EVERY
    # exit from the await — including CancelledError, which slot
    # deletion / cleanup endpoints raise by cancelling slot.task.
    # Assigning only inside try/except would leave `outcome` unbound
    # on that path: the finally would raise UnboundLocalError,
    # replacing the CancelledError with a spurious exception and
    # skipping both the message marking and the Slack cleanup —
    # reintroducing the orphan-card bug on the cancel path.
    # "rejected" is the correct reading: a cancelled turn never
    # obtained consent.
    #
    # The wait below first consumes an answer that ALREADY arrived
    # (``fut.done()``): the row was published before the Slack post
    # and a person can answer it while the post is in flight, and
    # the window is computed only after the post from the budget
    # left then. Without that read, a post slow enough to exhaust
    # the budget let the no-budget decline overrule a decision the
    # resolver had already recorded and answer the model with a
    # rejection the transcript shows as approved. The
    # delivery-failure arms above guard their own auto-reject the
    # same way (``if not fut.done()``).
    outcome = "rejected"
    # Per-SLOT, not the global config: `approval_timeout_for` is what
    # gives an app-owned worker with no human responder the background
    # deny-fast instead of parking for the attended window and being
    # denied anyway. See DashboardState.approval_timeout_for.
    #
    # But it is only ONE of the two bounds. `approval_timeout_for`
    # returns a flat constant, so on its own it can outlive the turn
    # it belongs to: the attended 7200s dwarfs the configurable
    # `tool_approval_timeout_secs()` (600s by default), and neither is
    # clamped to what is LEFT of a long agentic turn. The outer
    # `_bounded_turn` then cancels first and the timeout branch below
    # never runs — no card, no decline line, just a turn that dies
    # mid-prompt. Taking the MINIMUM keeps both properties: the
    # unattended deny-fast, and the double bound (ceiling and
    # remaining budget) that `tool_approval_timeout_secs` applies —
    # including its 0.0, which is what the no-budget branch reads.
    # Recorded before the window computation below, and deliberately
    # not at the future's registration further up. Everything between
    # the two is cancellable -- the Slack mirror awaits a network post,
    # and its `except Exception` cannot catch the CancelledError that
    # slot deletion raises -- so an entry written up there could escape
    # the try below entirely and leave a request that is never decided,
    # in a file nothing rewrites. Written here the pair is bound by
    # control flow: nothing between this call and that `try` awaits, so
    # either both halves land or neither does. The trade is that a
    # prompt cancelled during its Slack delivery goes unrecorded, which
    # is a fact the log is missing rather than a pair it gets wrong. It
    # is still before the decision on every surviving path, including
    # the delivery failure that auto-decides above: that branch only
    # resolves the future, and no decision is recorded until this try's
    # `finally`.
    #
    # ABOVE the window computation on purpose, not just below it:
    # `test_dashboard_approval_window.py` pins this branch's source
    # shape by slicing a fixed-length window starting at that
    # assignment, so anything inserted after it pushes the awaited
    # call and both cards out of the window the test reads. For the
    # same reason this comment does not quote the assignment itself:
    # the test locates the window by searching for that text, and a
    # second copy above it would move the window to the wrong place.
    crew_log_emit.on_approval_requested(
        _crew_log_sid,
        _crew_log_turn_no,
        approval_id=str(event.request_id),
        tool=event.tool_name or "",
        reason=event.title or "",
    )
    _approval_window = min(state.approval_timeout_for(slot), tool_approval_timeout_secs())
    _unattended_wait = slot.unattended
    _approval_card: str | None = None
    try:
        if fut.done():
            outcome = fut.result()  # answered during the Slack post; see above
        elif _approval_window <= 0:
            # Too little of the turn left to both wait and report.
            # Waiting anyway guarantees the ceiling fires first and
            # relabels the unanswered approval as a turn timeout.
            logger.warning(
                "Declining approval for %r without waiting: no turn budget left",
                event.title,
            )
            _approval_card = format_approval_no_budget_card()
            _host_deny_cause = DENY_CAUSE_APPROVAL_NO_BUDGET
            _host_deny_reason = "the turn had no budget left to wait for approval"
        else:
            outcome = await asyncio.wait_for(fut, timeout=_approval_window)
    except asyncio.TimeoutError:
        outcome = "rejected"
        # Name the real cause: an unanswered prompt is otherwise
        # indistinguishable from a generic turn timeout whenever the
        # window outlives the turn ceiling and the turn dies first
        # — an unattended run burns the full ceiling and the user
        # is never told an approval was waiting.
        logger.warning(
            "Tool approval for %r went unanswered for %.0fs; declining",
            event.title,
            _approval_window,
        )
        _approval_card = format_approval_timeout_card(_approval_window)
        _host_deny_cause = DENY_CAUSE_APPROVAL_TIMEOUT
        _host_deny_reason = (
            "the approval prompt went unanswered for " f"{max(1, round(_approval_window))}s"
        )
        # The in-band correction for this expired prompt is
        # steered ONCE at the shared reject branch below, keyed on
        # _host_deny_cause — the same site that corrects the other
        # host auto-declines (no budget, Slack delivery failure).
        # Steering here as well would tell the model the same fact
        # twice in one turn.
        if _unattended_wait:
            # The card is for the human; this line is for the AGENT.
            # A denial it cannot read makes it retry the same tool
            # forever, because nothing in its transcript explains the
            # refusal.
            slot.append(
                "assistant",
                "\u26a0\ufe0f A tool needed approval and no one answered within "
                f"{int(_approval_window)}s, so it was declined. This session is "
                "running unattended — ask for the permission you need instead of "
                "retrying the same call.",
                "msg msg-a",
            )
        # Tell any monitoring loop bound to this slot that a cycle
        # could not obtain approval. This branch IS the evidence a
        # reactive stop needs: the prompt ran its full window with no
        # decision, which an auto-approved tool never reaches. The
        # loop stops on its next wake instead of spending the rest of
        # its cap on cycles that cannot act. Best-effort and
        # non-blocking — a monitoring convenience must never change
        # how this turn's denial is reported.
        try:
            from kiro_crew.autonudge import (
                get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_runner
            )

            _autonudge = _autonudge_get()
            if _autonudge is not None:
                _autonudge.notify_approval_stalled(slot.key)
        except Exception:
            logger.debug("autonudge.notify_approval_stalled failed", exc_info=True)
        # The card above reaches only someone looking at this session.
        # The bell note reaches the rest of the dashboard. It carries no
        # publisher session, so muting this session does not hide it:
        # it is a gateway report about the session, not the session's
        # own note.
        try:
            state.notify(
                "agent",
                APPROVAL_EXPIRED_NOTE_TITLE,
                format_approval_expired_note(
                    event.tool_name or "", slot.display_title, _approval_window
                ),
                meta={"slot": slot.key},
            )
        except Exception:
            logger.warning("approval-expiry notification failed", exc_info=True)
    except asyncio.CancelledError:
        # The task itself was cancelled (slot deletion, shutdown) while
        # the prompt was still open. The decision is host-driven, not a
        # person's; flag it so the ``finally`` attributes it to the host
        # rather than emitting an anonymous rejection, then re-raise so
        # the cancellation is never swallowed.
        _host_cancelled = True
        raise
    finally:
        if _approval_card is not None:
            try:
                slot.append("error", _approval_card, "msg msg-err")
            except Exception:
                logger.debug("Failed to render approval card", exc_info=True)
        _owns_approval = slot.unregister_approval(str(event.request_id), fut)
        # The decision is final here and nowhere earlier: every path
        # out of the await above converges on this ``finally`` -- the
        # human's answer, the window expiring, the no-budget decline,
        # a failed Slack delivery, and a cancelled turn. Recording it
        # at the one convergence point is what keeps a single request
        # from being closed twice under two seqs.
        #
        # ``by`` is written only for a decision the HOST made, which
        # is the one attribution this site can prove:
        # ``_host_deny_cause`` is set exactly by the gateway's own
        # auto-declines, ``_host_cancelled`` marks the task being
        # cancelled out from under an open prompt, and
        # ``_approval_stopped`` carries the ids a STOP rejected. A
        # decision that arrived through the future by none of those
        # routes came from a person at the dashboard or in Slack and
        # this site cannot tell which, so it names nobody rather than
        # guessing "user".
        #
        # A stop resolves the future with an ordinary "rejected" and
        # raises nothing, so it is invisible to both flags; the id is
        # recorded where the stop resolves the future instead. Read
        # ONCE and discarded here, so a later human rejection on this
        # slot cannot inherit the host attribution. No ``cause`` is
        # written for it: the cause vocabulary is the user-facing
        # denial-message set whose lookup falls back to the policy
        # text, so borrowing a name from it would put a sentence about
        # policy in front of someone who simply pressed Stop.
        _approval_id = str(event.request_id)
        _host_stopped = _approval_id in slot._approval_stopped
        slot._approval_stopped.discard(_approval_id)
        #
        # ``decision`` is the coarse ledger enum, not the resolving
        # surface's wording: the future can carry ``approved_trust_reads``
        # (a scoped-trust approval), which the schema does not list, so
        # it folds to ``approved`` here -- the same reading the UI-mark
        # below takes. Emitting an unlisted value is schema-refused and
        # drops the closer entirely. ``rejected_once`` IS in the enum,
        # so it is carried through as itself: the UI-mark below keeps
        # that outcome distinct too, and folding it into ``rejected``
        # would discard a distinction both the schema and this site's
        # own paths hold.
        if outcome in ("approved", "approved_trust_reads"):
            _crew_log_decision = "approved"
        elif outcome == "rejected_once":
            _crew_log_decision = "rejected_once"
        else:
            _crew_log_decision = "rejected"
        crew_log_emit.on_approval_decided(
            _crew_log_sid,
            _crew_log_turn_no,
            approval_id=_approval_id,
            decision=_crew_log_decision,
            by=("host" if (_host_deny_cause or _host_cancelled or _host_stopped) else ""),
            cause=_host_deny_cause,
        )
        # The same attribution decides the approval hold: a decision
        # no host route made came from a person, and a loop paused for
        # approval on this slot resumes because someone is back.
        if not (_host_deny_cause or _host_cancelled or _host_stopped):
            from kiro_crew.autonudge import release_approval_hold_for

            release_approval_hold_for(slot.key, why="an approval was answered")
        # Backstop: the future is now gone, so the permission
        # message MUST NOT be left reading pending — the UI would
        # keep rendering an approval bar whose every button answers
        # 404, and a history reload would resurrect it. The primary
        # resolvers (HTTP slot-approve, Slack click) already mark it
        # and record richer decisions like "trust"/"yolo", so only
        # write when still pending. This is the sole marker for the
        # paths that resolve the future in-process: the approval
        # timeout above (2h attended / 180s unattended), the
        # Slack-delivery auto-reject branches, and a no-budget
        # decline the pre-check at the row's append did not already
        # write into the row. The ``approval_resolved`` frame below
        # is what retires the card on every window for those paths.
        _approved = outcome in ("approved", "approved_trust_reads")
        if _owns_approval and _mark_permission_resolved(
            [permission_row],
            str(event.request_id),
            "approved" if _approved else "rejected",
            only_if_pending=True,
        ):
            slot._dirty = True
            state.broadcast_ws(
                "approval_resolved",
                {
                    "id": str(event.request_id),
                    "approved": _approved,
                    # Keys the frame for the slot-scoped WS gate.
                    "slot": slot.key,
                },
            )
            state.push_slots_update()
        elif _owns_approval and "resolved" in perm_meta:
            # A row born resolved (the no-budget pre-check at its
            # append) needs neither the mark nor an
            # ``approval_resolved`` frame -- but the future it
            # registered is gone only now, and the slots snapshot
            # that announced pending_approval=true has to be
            # superseded or the Board keeps the session Blocked.
            state.push_slots_update()
        # Clean up the Slack prompt: remove the registry entry and
        # delete the buttons message now the decision is in.
        if _slack_approval_ts is not None:
            try:
                resolve_linked_approval(slot._slack_channel, _slack_approval_ts)
                if state.slack_client:
                    await state.slack_client.delete_message(slot._slack_channel, _slack_approval_ts)
            except Exception:
                logger.debug(
                    "Failed to clean up linked Slack approval message",
                    exc_info=True,
                )
    if outcome == "approved_trust_reads":
        slot._trust_reads = True
        # A person chose this slot's mode: its creator's changes stop reaching it.
        slot._posture_inherited = False
        outcome = "approved"
    if outcome == "approved":
        try:
            validated_tool = _validate_tool_name(
                event.title,
                is_shell=event.is_shell,
                canonical_name=event.tool_name,
            )
        except ValueError as e:
            await _reject_invalid_tool(
                client,
                slot,
                event,
                session_key=session_key,
                error=e,
                metadata={"reason": "interactive"},
                refusal_reasons=_refusal_reasons,
                refusal_notices=_refusal_notices,
                state=state,
            )
            return _Flow.BREAK
        try:
            _parsed_input = json.loads(event.tool_input) if event.tool_input else None
        except Exception:
            _parsed_input = None
        try:
            pre_hook_results = await _fire(
                turn,
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name=validated_tool,
                tool_input=_parsed_input,
                tool_id=event.harness_tool_id,
                tool_identity=event.tool_name,
                mcp_server=event.mcp_server_name,
            )
        except Exception as hook_exc:
            await _reject_hook_error(
                client,
                slot,
                event,
                session_key=session_key,
                error=str(hook_exc),
                metadata={"reason": "interactive"},
                refusal_reasons=_refusal_reasons,
                refusal_notices=_refusal_notices,
                state=state,
            )
            return _Flow.BREAK
        if _pre_tool_hooks_should_block(pre_hook_results):
            await _reject_hook_blocked(
                client,
                slot,
                event,
                session_key=session_key,
                pre_hook_results=pre_hook_results,
                refusal_reasons=_refusal_reasons,
                refusal_notices=_refusal_notices,
                metadata={"reason": "interactive"},
            )
        else:
            # BEFORE approve_tool, not after: the approval response
            # is what starts execution, so a file swapped in that
            # window would be the one pinned -- recording a file the
            # human never saw. Off-loop because it digests the file.
            #
            # Scope kept to `cmd`, the command _on_permission_request's
            # tiers also extract. Falling back to `event.shell_command` would
            # stop a structured approval of a non-system program from
            # re-prompting, but that wider form is an undisclosed
            # persistent grant: between "prompts once more than it
            # needs to" and "records an identity from a surface the
            # human may not read as durable", the extra prompt is the
            # safe side, so the narrower form is the one that ships.
            #
            # `is_shell` is tested explicitly because
            # `extract_bash_command` reads a `command` key out of ANY
            # structured input and falls back to the raw string, so a
            # NON-shell MCP call carrying `{"command": "gh ..."}` would
            # otherwise mint a witness for the shell program `gh` --
            # a durable grant from an approval that was never about
            # running `gh` at all. Same reason the trusted-pattern tier
            # in _on_permission_request tests it.
            if event.is_shell and cmd:
                await asyncio.to_thread(pin_human_approval, cmd)
            approval_sent = await client.approve_tool(event.request_id)
            if approval_sent is False:
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name=_redact_display_text(event.title),
                    tool_kind=event.tool_kind,
                    outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                    request_id=event.request_id,
                )
                return _Flow.CONTINUE
            _wt_note_approved(turn, event)
            _approved_title = _redact_display_text(event.title)
            slot.append("tool", f"✅ {_approved_title}", "msg msg-tool", meta=_tool_meta(event))
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=_approved_title,
                tool_kind=event.tool_kind,
                outcome="approved",
                request_id=event.request_id,
                metadata={"reason": "interactive"},
            )
    else:
        # Explain WHY when the command tripped the read-only safety
        # gate, so the pill reads "Cancelled due to unsafe shell
        # pattern …" instead of the bare adapter default.
        # unsafe_bash_reason() embeds fragments of the LLM-supplied
        # command (base name / pipe target), so redact it — and the
        # title — before it reaches the dashboard or SEL metadata.
        _safety_reason = unsafe_bash_reason(cmd) if cmd else ""
        if _safety_reason:
            _safety_reason, _ = redact_exfiltration_urls(_safety_reason)
            _safety_reason, _ = redact_credentials(_safety_reason)
        _safe_reject_title, _ = redact_exfiltration_urls(event.title)
        _safe_reject_title, _ = redact_credentials(_safe_reject_title)
        # Audit FIRST, before the rejection goes on the wire (see
        # _reject_hook_blocked): a stalled ACP reader cancels this
        # coroutine at the turn deadline, and an SEL write sequenced
        # after the await never runs.
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name=_safe_reject_title,
            tool_kind=event.tool_kind,
            outcome="rejected_once" if outcome == "rejected_once" else "rejected",
            request_id=event.request_id,
            metadata={"reason": _safety_reason or "interactive"},
        )
        # Host-originated auto-declines (approval timeout, no
        # turn budget, Slack delivery failure) funnel into this
        # shared reject, where kiro-cli's "User denied tool
        # execution" is FALSE: nobody judged the call. Each arm
        # records its cause upstream where it is known; this one
        # steer, gated on that provenance, corrects the attribution
        # for whichever arm declined — BEFORE the rejection goes on
        # the wire, because the still-unanswered permission request
        # is what keeps the notice queued instead of dropped.
        #
        # A THROWAWAY list, deliberately not _refusal_notices: these
        # paths append no _refusal_reasons entry (a host
        # auto-decline is answered as an ordinary rejection, never
        # by a recovery continuation), and
        # should_queue_refusal_recovery compares the two lists by
        # COUNT, not by pairing. No slot/state either: the cards the
        # arms render are already the human's explanation, so the
        # display row the policy paths add would paint the same
        # event twice.
        if _host_deny_cause:
            await _steer_policy_notice(
                client,
                _safe_reject_title,
                _host_deny_reason,
                [],
                cause=_host_deny_cause,
            )
        # Interactive user denial. The person
        # clicked Reject (or "reject once"), so kiro-cli's "User
        # denied tool execution" is the true and correct attribution
        # here — the sharpest case in the class, because steering a
        # policy notice would tell the model a rule blocked a call
        # the user personally refused. A genuine user refusal leaves
        # _host_deny_cause empty, so the provenance-gated steer
        # above never fires for it and this exemption stays true for
        # exactly the branch it covers; the host auto-declines are
        # that steer's job, not this marker's.
        await _reject_attributed(
            client, event.request_id, attribution="exempt: interactive user denial"
        )
        if _safety_reason:
            _reject_label = f"🚫 {_safe_reject_title} (cancelled — {_safety_reason})"
        elif outcome == "rejected_once":
            _reject_label = f"🚫 {_safe_reject_title} (rejected — this call only)"
        else:
            _reject_label = f"🚫 {_safe_reject_title} (rejected)"
        slot.append("tool", _reject_label, "msg msg-tool")
        # NOTE: Do NOT append to _refusal_reasons here.
        # This is an interactive user denial — the user chose to reject
        # the tool. Refusal-recovery is only for system-side blocks —
        # the hook-deny (TOOL_DENY) path, which is the other site that
        # appends to _refusal_reasons.

    if outcome == "rejected_once":
        # Deny this one tool but do NOT cascade to remaining batch
        logger.info(
            "PERM REJECTED ONCE tool=%r — remaining batch unaffected",
            event.title,
        )
        return _Flow.CONTINUE
    if outcome != "approved":
        # mark batch_rejected as true and continue _run_chat's stream loop instead of breaking
        # This will allow for marking other batched approval requests as rejected too
        slot._batch_rejected = True
        # Provenance travels with the flag: empty means the person
        # refused this tool themselves, non-empty names the host
        # auto-decline that did. The cascade site branches on it —
        # a user's refusal keeps kiro-cli's "User denied tool
        # execution" true for the remainder, a host cause makes it
        # false and worth an in-band correction. A sentence (not the
        # DENY_CAUSE_* constant) because the cascade notice embeds
        # it verbatim as the reason the model reads — and it is
        # batch-framed here because that notice shows it under the
        # CASCADED member's title, where the bare reason would claim
        # the member's own prompt failed when it was never prompted.
        # The conditional is load-bearing: a user refusal must copy
        # "" so the cascade stays exempt, never a framed empty.
        slot._batch_rejected_cause = (
            f"the host declined an earlier tool of this batch ({_host_deny_reason})"
            if _host_deny_reason
            else ""
        )
        # New denied batch, fresh notice budget for its cascade.
        _batch_cascade_steered = False
        turn._batch_cascade_steered = _batch_cascade_steered
        logger.warning(
            "PERM REJECTED tool=%r outcome=%r — auto-rejecting remaining batch",
            event.title,
            outcome,
        )
        return _Flow.CONTINUE
    return None
