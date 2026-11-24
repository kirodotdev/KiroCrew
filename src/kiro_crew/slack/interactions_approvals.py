"""Approvals pressed in Slack: the native tool-approval buttons and review-mode drafts.

``_handle_tool_approval`` routes a native approve, trust or reject press to the handler
(``dispatch`` resolves the transport path's tool-approval buttons itself). A
review-mode draft is approved, edited, cancelled or revised by its requester or the bot
owner (``_can_act_on_review_draft``); the edit and revise modals register their
submissions here.

Split out of ``kiro_crew.slack.interactions``, which stays the import path and the
patch target: every name here is reached as ``kiro_crew.slack.interactions.<name>``,
and a patch through that facade reaches this module (``kiro_crew._facade_mirror``).
Nothing here imports the facade. New approval work goes here.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from kiro_crew.config.loader import ACTIVATION_REVIEW
from kiro_crew.http_proxy import resolve_proxy
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.renderer import count_redaction_tags, redaction_notice
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.channel_policy import channel_refusal
from kiro_crew.slack.events_queue import _configured_approval_mode
from kiro_crew.slack.handler import (
    handle_interaction,
    handle_message,
    is_allowed_user,
    is_owner,
    is_shared_listed,
    is_shared_user,
    shared_user_refused_for_session,
)
from kiro_crew.slack.interactions_context import _orch, register_view_handler
from kiro_crew.slack.shared_access import SHARED_OWNER_ONLY_TEXT, shared_command_refused
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger("kiro_crew.slack.interactions")

#: What a person whose Approve / Reject / Trust press changed nothing is told,
#: privately, so the card never looks like a dead button. ``{owner}`` is
#: ``surface.owner_name()``; an ephemeral mention notifies nobody.
APPROVAL_OWNER_ONLY_TEXT = (
    "Only {owner} can answer this approval. Ask them to answer it here or in the dashboard."
)
#: The same, when the prompt came from a shared user's own turn: they may answer it.
APPROVAL_OWNER_OR_ASKER_TEXT = (
    "Only {owner} or <@{asker}>, whose message asked for it, can answer this approval."
)
APPROVAL_TRUST_OWNER_ONLY_TEXT = "Only {owner} can turn on Trust."
APPROVAL_NOT_WAITING_TEXT = (
    "This approval isn't waiting for an answer any more, so nothing changed."
)


def _refused_press_text(key: str, action_id: str) -> str:
    """Why a press ``handle_interaction`` answered with ``None`` changed nothing.

    Read after the refusal: a prompt still registered under *key* was live and
    refused to this clicker (the owner's press on a live prompt always resolves
    it, and so does its requester's Approve or Reject); one that is gone was
    already answered, timed out or never belonged to this gateway process.
    """
    from kiro_crew.slack import handler
    from kiro_crew.slack.turn_requester import live_card_requester

    linked = handler._linked_approvals.get(key)
    entry = linked or handler._pending_approvals.get(key)
    if entry is None:
        return APPROVAL_NOT_WAITING_TEXT
    asker = live_card_requester(linked) if linked is not None else getattr(entry, "requester", "")
    return _owner_only_text(action_id == "trust_tool", asker)


def _owner_only_text(trust: bool, asker: str = "") -> str:
    """The refusal for a live approval card: who may answer it (Trust: the owner)."""
    from kiro_crew.config.sections_slack import is_slack_user_id
    from kiro_crew.slack.code_channels.surface import owner_name

    if trust:
        return APPROVAL_TRUST_OWNER_ONLY_TEXT.format(owner=owner_name())
    # Only a well-formed member id becomes a mention, so it cannot inject markup.
    if isinstance(asker, str) and is_slack_user_id(asker):
        return APPROVAL_OWNER_OR_ASKER_TEXT.format(owner=owner_name(), asker=asker)
    return APPROVAL_OWNER_ONLY_TEXT.format(owner=owner_name())


async def _handle_tool_approval(
    payload: dict, action_id: str, channel: str, msg_ts: str, user_id: str
) -> None:
    """Route approve / trust / reject to the handler."""
    # Inbound channels-governance gate: resolving a tool approval executes the
    # governed tool, so a channels policy denying ``slack`` must stop it (same
    # gate as an inbound message). Covers the native approval path; the transport
    # path is gated at its own branch in dispatch().
    # EXCEPTION: a REJECT is a denial of the tool — exactly what a channels-deny
    # wants — so let it through to resolve the pending approval as refused, rather
    # than silently dropping it (which strands the kiro-cli future until timeout).
    # Only approve/trust are blocked outright.
    _is_reject = action_id == "reject_tool"
    if not _is_reject:
        if not (
            await channel_inbound_permitted("slack")
            and not channel_refusal(channel, operation="slack.interactive", caller=user_id)
        ):
            logger.info(
                "slack tool-approval (native) dropped: denied by channels governance policy"
            )
            return
    # Trust is restricted to DMs — fail-closed if orchestrator not ready
    if action_id == "trust_tool":
        if not _orch or not _orch.slack:
            logger.warning("trust_tool: orchestrator not ready — rejecting")
            return
        is_dm = await _orch.slack.is_dm(channel)
        if not is_dm:
            logger.warning("Rejecting trust_tool in non-DM channel %s (user=%s)", channel, user_id)
            return

    thread_ts = payload.get("message", {}).get("thread_ts", "")
    slack_ops = _orch.slack if _orch else None
    effective_action = await handle_interaction(
        channel,
        msg_ts,
        action_id,
        user_id=user_id,
        thread_ts=thread_ts,
        slack=slack_ops,
        sessions=_orch.sessions if _orch else None,
    )

    # Replace buttons with outcome label — only when an action was processed.
    # When effective_action is None (a refused clicker, or an approval that is
    # not waiting for an answer), preserve buttons so the authorized owner can still click, and tell
    # the clicker privately why nothing happened.
    if effective_action is None:
        if _orch and _orch.slack and user_id:
            try:
                await _orch.slack.post_ephemeral(
                    channel,
                    user_id,
                    _refused_press_text(f"{channel}:{msg_ts}", action_id),
                    thread_ts=thread_ts or None,
                )
            except Exception:
                logger.debug("Failed to send the refused-approval notice", exc_info=True)
        return
    if _orch and _orch.slack:
        label = {
            "approve_tool": "✅ Approved",
            "trust_tool": "🤝 Trusted",
            "reject_tool": "🚫 Rejected",
        }.get(effective_action, "")
        if label:
            try:
                await _orch.slack.update_message(channel, msg_ts, text=label)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Review mode handlers
# ---------------------------------------------------------------------------

# Shown when a non-authorized user clicks a review-mode button.
_REVIEW_AUTH_DENIED_MSG = (
    "⚠️ Only the bot owner or the user who requested this draft can act on it."
)


async def _delete_review_placeholder(channel: str, thread_ts: str) -> None:
    """Clear the 'Awaiting review…' thread status indicator."""
    if not _orch or not _orch.slack:
        return
    try:
        await _orch.slack.set_thread_status(channel, thread_ts, "")
    except Exception:
        logger.debug("Failed to clear review thread status", exc_info=True)


async def _post_review_auth_error(response_url: str) -> None:
    """Reply with an ephemeral error via response_url (replaces the draft)."""
    if not response_url:
        return
    try:
        async with aiohttp.ClientSession() as sess:
            await sess.post(
                response_url,
                proxy=resolve_proxy(response_url),
                json={
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "text": _REVIEW_AUTH_DENIED_MSG,
                },
            )
    except Exception:
        logger.debug("Failed to post review auth-denied ephemeral", exc_info=True)


def _parse_draft_key(meta: str) -> tuple[str, str, str] | None:
    """Parse draft key 'channel|thread_ts|uuid' → (channel, thread_ts, draft_key) or None."""
    parts = meta.split("|")
    if len(parts) < 2:
        return None
    channel, thread_ts = parts[0], parts[1]
    return channel, thread_ts, meta


def _can_act_on_review_draft(caller: str, requester: str) -> bool:
    """Authorize a review-mode action: bot owner OR the requester who triggered the draft.

    The requester must still be admitted (``is_allowed_user``): a shared user removed
    while their draft's modal was open posts nothing.
    """
    return bool(caller) and (
        is_owner(caller)
        or (caller == requester and (is_allowed_user(caller) or is_shared_user(caller)))
    )


# Draft-key suffix _post_review_draft adds for a channel-level (unthreaded) card.
_REVIEW_ROOT_SUFFIX = "|root"


async def _restore_review_card(
    channel: str, thread_ts: str, draft_key: str, text: str, requester: str, caller: str
) -> None:
    """Store ``text`` as the draft again and re-post its review card to ``caller``.

    Used after a failed post from the edit modal: the modal has closed and the
    original card was deleted when it opened, so a fresh card is the only way
    back to the text.
    """
    if not _orch or not _orch.slack:
        return
    from kiro_crew.slack.blocks import review_draft_blocks
    from kiro_crew.slack.handler import _review_drafts_set

    _review_drafts_set(draft_key, text, requester)
    try:
        await _orch.slack.post_ephemeral(
            channel,
            caller,
            text,
            blocks=review_draft_blocks(text, draft_key),
            thread_ts=None if draft_key.endswith(_REVIEW_ROOT_SUFFIX) else thread_ts,
        )
    except Exception:
        logger.exception("Failed to re-post review card for %s", draft_key)


async def _handle_review_approve(payload: dict, action: dict) -> None:
    """Post the approved draft to the channel."""
    if not _orch or not _orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from slack_sdk.errors import SlackApiError

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop, _review_drafts_set

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        sel().log_api_access(
            caller=caller,
            operation="slack.review_approve",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return

    draft, _requester = _review_drafts_pop(draft_key)
    if not draft:
        logger.warning("Review approve: no draft found for %s", draft_key)
        return
    original = draft
    draft, _ = redact_exfiltration_urls(draft)
    draft, _ = redact_credentials(draft)
    try:
        await _orch.slack.post_message(channel, draft, thread_ts)
    except SlackApiError:
        # Popped first so a double press cannot post twice; put it back when
        # Slack answered with an error, so the buttons still on screen can
        # retry. Only then: a dropped connection may have posted it already.
        _review_drafts_set(draft_key, original, _requester)
        raise
    # Approving a draft posts it publicly to the channel, so this egress carries
    # the same silent-corruption hazard as the streaming reply path: the two lines
    # above replaced a credential in the draft with a placeholder, and a channel
    # member who copies the command hits an opaque downstream failure with no hint
    # the text was rewritten. Count the tags in the redacted draft that actually
    # shipped (`count_redaction_tags`) and post one best-effort follow-up notice.
    # The notice carries only counts, never secret bytes, and its failure must not
    # undo the posted draft -- the draft
    # is already public, so raising here would lose the warning and the approve's
    # remaining teardown too.
    _cred_redactions, _url_redactions = count_redaction_tags(draft)
    if _cred_redactions > 0 or _url_redactions > 0:
        try:
            await _orch.slack.post_message(
                channel, redaction_notice(_cred_redactions, _url_redactions), thread_ts
            )
        except Exception:
            logger.debug("Failed to post review-approve redaction notice", exc_info=True)
    await _delete_review_placeholder(channel, thread_ts)
    # Delete the ephemeral draft message
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url, proxy=resolve_proxy(response_url), json={"delete_original": True}
                )
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    sel().log_api_access(
        caller=caller,
        operation="slack.review_approve",
        outcome="allowed",
        source="slack",
        resources=channel,
    )
    logger.info("Review approved by %s in %s", caller, channel)


async def _handle_review_edit(payload: dict, action: dict) -> None:
    """Open a modal pre-filled with the draft for editing."""
    if not _orch or not _orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    trigger_id = payload.get("trigger_id", "")
    if not trigger_id:
        logger.warning("Review edit: no trigger_id in payload")
        return
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.blocks import review_edit_modal
    from kiro_crew.slack.handler import _review_drafts_get

    draft, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        sel().log_api_access(
            caller=caller,
            operation="slack.review_edit",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return
    if not draft:
        logger.warning("Review edit: no draft found for %s", draft_key)
        return
    modal = review_edit_modal(draft, draft_key)
    await _orch.slack.views_open(trigger_id, modal)
    # Delete the ephemeral draft message
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url, proxy=resolve_proxy(response_url), json={"delete_original": True}
                )
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    sel().log_api_access(
        caller=caller,
        operation="slack.review_edit",
        outcome="allowed",
        source="slack",
        resources=channel,
    )


async def _handle_review_cancel(payload: dict, action: dict) -> None:
    """Discard the draft and delete the ephemeral message."""
    if not _orch or not _orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        sel().log_api_access(
            caller=caller,
            operation="slack.review_cancel",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return

    _review_drafts_pop(draft_key)
    await _delete_review_placeholder(channel, thread_ts)
    # Delete the ephemeral draft message
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url, proxy=resolve_proxy(response_url), json={"delete_original": True}
                )
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    sel().log_api_access(
        caller=caller,
        operation="slack.review_cancel",
        outcome="allowed",
        source="slack",
        resources=channel,
    )
    logger.info("Review cancelled by %s in %s", caller, channel)


async def _handle_review_edit_submit(payload: dict) -> None:
    """Post the edited text from the review edit modal."""
    if not _orch or not _orch.slack:
        return
    # Inbound channels-governance gate: the modal may have opened while slack was
    # permitted, then a profile hot-reload denied it before submit. Re-check HERE
    # (not just at the button click that opened the modal) so a denied channel
    # can't receive the edited agent content via a stale-modal submission.
    if not await channel_inbound_permitted("slack"):
        logger.info("slack review-edit submit dropped: denied by channels governance policy")
        return
    caller = payload.get("user", {}).get("id", "")
    view = payload.get("view", {})
    meta = view.get("private_metadata", "")
    parsed = _parse_draft_key(meta)
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed
    if channel_refusal(channel, operation="slack.review_submit", caller=caller):
        # The channel itself (``channels.posture.slack.allowed_channel_ids``).
        logger.info("slack review-edit submit dropped: channel refused by policy")
        return

    from kiro_crew.slack.code_channels.routing import shared_user_refused_here
    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    # A code channel is the owner's (slack.shared_access): the modal may have opened
    # before Slack Code went on, so a shared user's edit is not posted there.
    if not _can_act_on_review_draft(caller, requester) or (
        is_shared_listed(caller) and await shared_user_refused_here(_orch.slack, channel)
    ):
        sel().log_api_access(
            caller=caller,
            operation="slack.review_edit_submit",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        return
    values = view.get("state", {}).get("values", {})
    edited = values.get("mc_review_edit_block", {}).get("mc_review_edit_input", {}).get("value", "")
    if not edited:
        return

    # Claim the draft: a stale modal whose draft was already approved, cancelled
    # or expired must not post.
    stored, requester = _review_drafts_pop(draft_key)
    if not stored:
        logger.warning("Review edit submit: no draft found for %s", draft_key)
        return
    to_post, _ = redact_exfiltration_urls(edited)
    to_post, _ = redact_credentials(to_post)
    from slack_sdk.errors import SlackApiError

    try:
        await _orch.slack.post_message(channel, to_post, thread_ts)
    except SlackApiError:
        # Only when Slack answered with an error: a dropped connection may have
        # posted it already, and a second card would invite a double post.
        logger.exception("Review edit submit: post failed for %s", draft_key)
        await _restore_review_card(channel, thread_ts, draft_key, edited, requester, caller)
        return
    await _delete_review_placeholder(channel, thread_ts)
    sel().log_api_access(
        caller=caller,
        operation="slack.review_edit_submit",
        outcome="allowed",
        source="slack",
        resources=channel,
    )
    logger.info("Review edited and posted by %s in %s", caller, channel)


register_view_handler("mc_review_edit_submit", _handle_review_edit_submit)


async def _handle_review_revise(payload: dict, action: dict) -> None:
    """Open a modal for the user to provide revision feedback."""
    if not _orch or not _orch.slack:
        return
    caller = payload.get("user", {}).get("id", "")
    trigger_id = payload.get("trigger_id", "")
    if not trigger_id:
        logger.warning("Review revise: no trigger_id in payload")
        return
    parsed = _parse_draft_key(action.get("value", ""))
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed

    from kiro_crew.slack.blocks import review_revise_modal
    from kiro_crew.slack.handler import _review_drafts_get

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
        sel().log_api_access(
            caller=caller,
            operation="slack.review_revise",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        await _post_review_auth_error(payload.get("response_url", ""))
        return

    modal = review_revise_modal(draft_key)
    await _orch.slack.views_open(trigger_id, modal)
    # Delete the ephemeral draft message (new one will appear after revision)
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url, proxy=resolve_proxy(response_url), json={"delete_original": True}
                )
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    sel().log_api_access(
        caller=caller,
        operation="slack.review_revise",
        outcome="allowed",
        source="slack",
        resources=channel,
    )


def _revision_destination(thread_ts: str) -> str:
    """The session a revision in *thread_ts* runs in: its linked slot, else its owner."""
    from kiro_crew.slack.handler import get_dashboard_state

    state = get_dashboard_state()
    slot = state.get_linked_slot(thread_ts) if hasattr(state, "get_linked_slot") else None
    if slot is not None:
        return str(getattr(slot, "key", "") or "")
    sessions = getattr(state, "sessions", None) or getattr(_orch, "sessions", None)
    owner = sessions.get_session_for_thread(thread_ts) if sessions is not None else None
    return owner if isinstance(owner, str) else ""


async def _handle_review_revise_submit(payload: dict) -> None:
    """Take revision feedback, send to LLM with draft context, post new ephemeral draft."""
    if not _orch or not _orch.slack:
        return
    # Inbound channels-governance gate (same rationale as the edit-submit handler):
    # a hot-reload deny after the modal opened must stop a revise from driving a
    # new LLM turn + posting on the denied Slack channel.
    if not await channel_inbound_permitted("slack"):
        logger.info("slack review-revise submit dropped: denied by channels governance policy")
        return
    caller = payload.get("user", {}).get("id", "")
    view = payload.get("view", {})
    meta = view.get("private_metadata", "")
    parsed = _parse_draft_key(meta)
    if not parsed:
        return
    channel, thread_ts, draft_key = parsed
    if channel_refusal(channel, operation="slack.review_submit", caller=caller):
        # The channel itself (``channels.posture.slack.allowed_channel_ids``).
        logger.info("slack review-revise submit dropped: channel refused by policy")
        return

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    from kiro_crew.slack.code_channels.routing import shared_user_refused_here

    # Before the pop, as the edit submit: a refused revision keeps the draft. The
    # revision runs in the thread's session, which may be a code channel's; one
    # relinked while it is judged is refused too (the draft is kept).
    _dest = _revision_destination(thread_ts)
    if not _can_act_on_review_draft(caller, requester) or (
        is_shared_listed(caller)
        and (
            await shared_user_refused_here(_orch.slack, channel)
            or await shared_user_refused_for_session(caller, _dest, thread_ts, channel)
            or _revision_destination(thread_ts) != _dest
        )
    ):
        sel().log_api_access(
            caller=caller,
            operation="slack.review_revise_submit",
            outcome="denied",
            source="slack",
            error="not owner or requester",
        )
        return
    values = view.get("state", {}).get("values", {})
    feedback = (
        values.get("mc_review_revise_block", {}).get("mc_review_revise_input", {}).get("value", "")
    )
    if not feedback:
        return
    # A shared user's feedback is what they typed, judged before the draft is
    # consumed; the draft in the synthesized prompt is the model's, never a command.
    if is_shared_listed(caller) and shared_command_refused(caller, feedback):
        sel().log_api_access(
            caller=caller,
            operation="slack.review_revise_submit",
            outcome="denied",
            source="slack",
            error="shared user command",
        )
        try:
            await _orch.slack.post_ephemeral(
                channel, caller, SHARED_OWNER_ONLY_TEXT, thread_ts=thread_ts
            )
        except Exception:
            logger.debug("review revise: refusal notice failed", exc_info=True)
        return

    draft, _requester = _review_drafts_pop(draft_key)
    if not draft:
        logger.warning("Review revise: no draft found for %s", draft_key)
        return

    # Send revision request through handle_message with context
    revision_prompt = (
        f"I asked you a question and you drafted this response:\n\n"
        f"---\n{draft}\n---\n\n"
        f"Please revise it based on this feedback: {feedback}\n\n"
        f"Respond ONLY with the revised response text, nothing else."
    )
    # Use handle_message so the revision goes through the full pipeline
    # (including review mode interception → new ephemeral draft)
    # Fire-and-forget: Slack requires view_submission response within ~3s
    # Audit the permission decision before spawning the task so it's always recorded.
    sel().log_api_access(
        caller=caller,
        operation="slack.review_revise_submit",
        outcome="allowed",
        source="slack",
        resources=channel,
    )

    async def _do_revise() -> None:
        try:
            await handle_message(
                _orch.slack,  # type: ignore[arg-type]
                _orch.sessions,  # type: ignore[arg-type]
                channel,
                revision_prompt,
                thread_ts,
                thread_ts,  # msg_ts = thread_ts for revision
                caller,
                approval_mode=_configured_approval_mode(_orch),
                context_builder=_orch.ctx_builder,
                cron_service=_orch.cron_svc,
                conversation_log=_orch.conv_log,
                consolidator=_orch.consolidator,
                subagent_manager=_orch.subagent_mgr,
                task_runner=_orch.task_runner,
                channel_activation=ACTIVATION_REVIEW,
                start_priority=StartPriority.FOREGROUND,
                spool_text=feedback if is_shared_listed(caller) else None,
            )
            logger.info("Review revision requested by %s in %s", caller, channel)
        except Exception:
            sel().log_api_access(
                caller=caller,
                operation="slack.review_revise_submit",
                outcome="error",
                source="slack",
                resources=channel,
                error="handle_message failed",
            )
            logger.exception("Review revision failed for %s in %s", caller, channel)

    t = asyncio.create_task(_do_revise())
    _orch._handler_tasks.add(t)
    t.add_done_callback(_orch._handler_tasks.discard)


register_view_handler("mc_review_revise_submit", _handle_review_revise_submit)


#: What the owner is told, privately, about a stale bridged press. Anyone else is told
#: nothing, so a press reveals nothing about a decision to someone who is not the owner.
BRIDGE_ACTION_STALE_NOTICE = "This request was already answered or has expired."


async def _handle_bridge_action(
    payload: dict, action: dict, action_id: str, channel: str, user_id: str
) -> None:
    """Resolve a bridged notification's action button (RFC bridge actions, B4c).

    The button's ``value`` is the token ``BridgeActionRegistry.bind`` minted and its
    ``action_id`` names the answer. ``dispatch`` admits only allow-listed users; this
    adds the inbound ``channels`` gate an approval runs a tool behind, and the
    registry adds the rest: owner-only (``is_owner``, the configured ``owner_id``),
    single-use, an offered answer, before its deadline. ``user_id`` is the payload's
    ``user.id``, which only Slack writes: interactions arrive on the app-token Socket
    Mode connection, never as text a user typed. Every press that reaches the
    registry is audited through ``bridge_action_audit.audit_action``, a refused or
    stale one included. Fail closed: with the ``bridge_approvals`` preview off, no
    registry, or a press that does not parse, nothing is resolved.
    """
    # Lazy: the bridge's names stay out of this owner's globals, so the
    # interactions facade does not serve them.
    from kiro_crew.notifications.bridge_action_audit import audit_action
    from kiro_crew.notifications.bridge_actions import (
        SLACK_ACTION_PREFIX,
        ActionOutcome,
        actions_enabled,
    )

    # The live config is read off the loop, so turning the preview off stops a
    # press on a button posted before it.
    if not await asyncio.to_thread(actions_enabled):
        return
    state = _orch.dashboard_state if _orch else None
    registry = getattr(state, "bridge_actions", None)
    if registry is None:
        return
    token = action.get("value")
    choice = action_id.removeprefix(SLACK_ACTION_PREFIX)
    if not isinstance(token, str) or not token or not user_id:
        return  # Does not parse: no answer is given.
    # Resolving an approval runs a governed tool, so a channels policy denying
    # ``slack`` stops the press here, as it stops the native approval buttons. A
    # denied press resolves nothing; the dashboard can still answer.
    if not (
        await channel_inbound_permitted("slack")
        and not channel_refusal(channel, operation="slack.bridge_action", caller=user_id)
    ):
        logger.info("slack bridged action dropped: denied by channels governance policy")
        return
    from_owner = is_owner(user_id)
    outcome = registry.resolve(token, choice, from_owner=from_owner)
    audit_action(
        registry, token, outcome, transport="slack", choice=choice, actor=user_id, channel=channel
    )
    if outcome is ActionOutcome.ADMITTED or not from_owner:
        return
    thread_ts = payload.get("message", {}).get("thread_ts") or ""
    if _orch and _orch.slack and channel and user_id:
        try:
            await _orch.slack.post_ephemeral(
                channel, user_id, BRIDGE_ACTION_STALE_NOTICE, thread_ts=thread_ts or None
            )
        except Exception:
            logger.debug("Failed to send a bridged-action notice", exc_info=True)
