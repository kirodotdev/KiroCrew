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
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.renderer import count_redaction_tags, redaction_notice
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.handler import (
    APPROVAL_INTERACTIVE,
    handle_interaction,
    handle_message,
    is_owner,
)
from kiro_crew.slack.interactions_context import _orch, register_view_handler
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger("kiro_crew.slack.interactions")


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
        if not await channel_inbound_permitted("slack"):
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
    # When effective_action is None (unauthorized user or already resolved),
    # preserve buttons so the authorized owner can still click.
    if _orch and _orch.slack and effective_action:
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
    """Authorize a review-mode action: bot owner OR the requester who triggered the draft."""
    return bool(caller) and (caller == requester or is_owner(caller))


async def _handle_review_approve(payload: dict, action: dict) -> None:
    """Post the approved draft to the channel."""
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
    draft, _ = redact_exfiltration_urls(draft)
    draft, _ = redact_credentials(draft)
    await _orch.slack.post_message(channel, draft, thread_ts)
    # Approving a draft posts it publicly to the channel, so this egress carries
    # the same silent-corruption hazard as the streaming reply path: the two lines
    # above replaced a credential or a suspicious URL in the draft with a
    # placeholder, and a channel member who copies the command hits an opaque
    # downstream failure with no hint the text was rewritten. Count the tags in
    # the redacted draft that actually shipped -- credential tags exactly, the URL
    # tag by `EXFILTRATION_REDACTION_TAG_PREFIX` prefix since it interpolates the
    # domain -- and post one best-effort follow-up notice worded by kind (the
    # remedies differ). The notice carries only counts, never secret bytes or the
    # redacted domain, and its failure must not undo the posted draft -- the draft
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
                await sess.post(response_url, json={"delete_original": True})
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
                await sess.post(response_url, json={"delete_original": True})
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
                await sess.post(response_url, json={"delete_original": True})
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

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
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

    _review_drafts_pop(draft_key)
    edited, _ = redact_exfiltration_urls(edited)
    edited, _ = redact_credentials(edited)
    await _orch.slack.post_message(channel, edited, thread_ts)
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
                await sess.post(response_url, json={"delete_original": True})
        except Exception:
            logger.debug("Failed to delete review ephemeral", exc_info=True)
    sel().log_api_access(
        caller=caller,
        operation="slack.review_revise",
        outcome="allowed",
        source="slack",
        resources=channel,
    )


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

    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_pop

    _, requester = _review_drafts_get(draft_key)
    if not _can_act_on_review_draft(caller, requester):
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
                approval_mode=APPROVAL_INTERACTIVE,
                context_builder=_orch.ctx_builder,
                cron_service=_orch.cron_svc,
                conversation_log=_orch.conv_log,
                consolidator=_orch.consolidator,
                subagent_manager=_orch.subagent_mgr,
                task_runner=_orch.task_runner,
                channel_activation=ACTIVATION_REVIEW,
                start_priority=StartPriority.FOREGROUND,
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

    asyncio.create_task(_do_revise())


register_view_handler("mc_review_revise_submit", _handle_review_revise_submit)
