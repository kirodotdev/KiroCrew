"""Slack Block Kit interactive payload routing.

Handles button clicks dispatched by Socket Mode:
- Tool approval (approve / trust / reject)
- OPTIONS choice buttons (LLM-generated multiple-choice)
- Cron and subagent acknowledge buttons
- Allowlist approve / deny buttons

All handlers receive the raw ``SocketModeRequest`` payload and
delegate to the appropriate service via the module-level ``_orch``
reference (set by the gateway orchestrator at startup).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib as _importlib
import json
import logging
import re
import sys as _sys
import time
import unicodedata
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew.config.loader import (
    ACTIVATION_REVIEW,
    ConfigReadError,
    config_path,
    update_config_locked,
)
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.dashboard.chat_utils import (
    forget_slack_options_for_thread,
    options_control_is_stale,
    run_config_write,
    slack_options_owner_keys_snapshot,
    slack_options_slot,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.queue_drain import entries_queued_by
from kiro_crew.messaging.renderer import count_redaction_tags, redaction_notice
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session_lifecycle import (
    STOP_DECLINED_COMPACTING_TEXT,
    compaction_in_flight,
    consume_stop_declined,
    decline_stop,
)
from kiro_crew.slack.allowlist import (
    ACTION_ALLOWLIST_APPROVE,
    ACTION_ALLOWLIST_DENY,
    ACTION_TRACK_APPROVE,
    ACTION_TRACK_DENY,
    persist_allowed_user,
    persist_tracking_channel,
)
from kiro_crew.slack.format import (
    LINK_DASHBOARD_ACTION,
    OPTIONS_ACTION_PREFIX,
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
    SESSION_LINK_ACTION,
    build_options_selected_blocks,
    escape_mrkdwn,
    replace_options_blocks,
)
from kiro_crew.slack.handler import (
    APPROVAL_INTERACTIVE,
    add_trusted_session,
    handle_interaction,
    handle_message,
    is_allowed_user,
    is_owner,
    set_allowed_users,
    set_tracking_channels,
)
from kiro_crew.slack.outbound import (
    PostedOptions,
    claim_options_answer,
    decode_options_token,
    expire_options,
    mark_options_terminal,
    options_edit_lock,
    release_options_answer,
    settle_options_answer,
)
from kiro_crew.slack.renderer import (
    TOOL_APPROVE_ACTION_PREFIX,
    TOOL_DENY_ACTION_PREFIX,
    TOOL_TRUST_ACTION_PREFIX,
    SlackApprovalDecider,
    split_approval_token,
)
from kiro_crew.slack.scope_probe import log_probe_failure, warn_unreadable_tracked_channels
from kiro_crew.start_priority import StartPriority

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def init(orchestrator: GatewayOrchestrator) -> None:
    """Bind the orchestrator so interactive handlers can reach services."""
    # Written through the facade, so the mirror sets it here and in every owner that reads it.
    setattr(_sys.modules[__name__], "_orch", orchestrator)
    fwd_cb = _get_forward_callback()
    if fwd_cb:
        register_view_handler(fwd_cb, _handle_shortcut_submission)


# ---------------------------------------------------------------------------
# Main dispatcher — called from the event router
# ---------------------------------------------------------------------------


async def dispatch(payload: dict) -> None:
    """Route a Block Kit interactive payload to the correct handler."""
    # ── View submissions and closures (modals) ──
    payload_type = payload.get("type", "")
    if payload_type == "view_submission":
        await handle_view_submission(payload)
        return
    if payload_type == "view_closed":
        await handle_view_closed(payload)
        return

    # ── Message shortcuts (right-click → "Forward to Agent") ──
    if payload_type == "message_action":
        await _handle_message_shortcut(payload)
        return

    actions = payload.get("actions", [])
    if not actions:
        return

    action = actions[0]
    action_id = action.get("action_id", "")
    channel = payload.get("channel", {}).get("id", "")
    msg_ts = payload.get("message", {}).get("ts", "")
    user_id = payload.get("user", {}).get("id", "")

    # ── Access check — deny-by-default ──
    if not is_allowed_user(user_id):
        logger.warning(
            "Rejecting interactive payload from unauthorized user %s (action=%s)",
            user_id or "unknown",
            action_id,
        )
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.interactive",
            outcome="denied",
            source="slack",
            resources=action_id,
            error="unauthorized user",
        )
        if _orch and _orch.slack and channel and user_id:
            try:
                await _orch.slack.post_ephemeral(
                    channel, user_id, "⛔ You are not authorized to use these buttons."
                )
            except Exception:
                logger.debug("Failed to send ephemeral rejection", exc_info=True)
        return

    # ── OPTIONS checkboxes toggle — no-op, wait for Send ──
    if action_id == OPTIONS_CHECKBOXES_ACTION:
        return

    # ── "Open session" deep-link button — no-op ──
    # A URL button opens its link in the browser directly; Slack still POSTs a
    # block_actions event for it, so ack it here as a no-op rather than letting it
    # fall through to the tool-approval handler (which would look up a nonexistent
    # approval). No server work, so no channels-governance gate is needed.
    if action_id == SESSION_LINK_ACTION:
        return

    # ── OPTIONS Send / legacy choice buttons ──
    # Both RESOLVE an [OPTIONS:] choice: they edit/post the selection to the
    # channel AND re-dispatch it as a fresh turn. So a ``channels`` policy that
    # denies ``slack`` must stop them BEFORE that side effect — the re-dispatched
    # handle_message() is gated, but the message edit/post precedes it. Gate here.
    # (``_done_`` is a spent-marker no-op that posts nothing, so it stays exempt.)
    _is_options = action_id == OPTIONS_SUBMIT_ACTION or (
        action_id.startswith(OPTIONS_ACTION_PREFIX) and "_done_" not in action_id
    )
    if _is_options:
        if not await channel_inbound_permitted("slack"):
            logger.info("slack OPTIONS choice dropped: denied by channels governance policy")
            return

    # ── OPTIONS Send button ──
    if action_id == OPTIONS_SUBMIT_ACTION:
        await _handle_options_submit(payload, channel, msg_ts)
        return

    # ── Legacy OPTIONS choice buttons ──
    if action_id.startswith(OPTIONS_ACTION_PREFIX):
        if "_done_" in action_id:
            return
        await _handle_options(payload, action, channel, msg_ts)
        return

    # ── Cron acknowledge ──
    from kiro_crew.slack.format import CRON_ACK_ACTION_PREFIX

    if action_id.startswith(CRON_ACK_ACTION_PREFIX):
        await _handle_cron_ack(payload, action, channel, msg_ts)
        return

    # ── Subagent acknowledge ──
    from kiro_crew.slack.format import SUBAGENT_ACK_ACTION_PREFIX

    if action_id.startswith(SUBAGENT_ACK_ACTION_PREFIX):
        await _handle_subagent_ack(payload, action, channel, msg_ts)
        return

    # ── Allowlist approve / deny (owner-only) ──
    if action_id in (ACTION_ALLOWLIST_APPROVE, ACTION_ALLOWLIST_DENY):
        if not is_owner(user_id):
            logger.warning("Rejecting allowlist action from non-owner %s", user_id)
            sel().log_api_access(
                caller=user_id,
                operation="slack.allowlist.button",
                outcome="denied",
                source="slack",
                resources=action_id,
                error="non-owner",
            )
            return
        await _handle_allowlist(payload, action, action_id, channel, msg_ts, user_id)
        return

    # ── Track channel approve / deny (owner-only) ──
    if action_id in (ACTION_TRACK_APPROVE, ACTION_TRACK_DENY):
        if not is_owner(user_id):
            logger.warning("Rejecting track-channel action from non-owner %s", user_id)
            sel().log_api_access(
                caller=user_id,
                operation="slack.track_channel.button",
                outcome="denied",
                source="slack",
                resources=action_id,
                error="non-owner",
            )
            return
        await _handle_track_channel(payload, action, action_id, channel, msg_ts, user_id)
        return

    # ── Stop confirm / cancel ──
    if action_id == "mc_stop_confirm":
        await _handle_stop_confirm(payload, channel, msg_ts, user_id)
        return
    if action_id == "mc_stop_cancel":
        await _handle_stop_cancel(payload, channel, msg_ts)
        return

    # ── Kill Now (ephemeral stop escalation) ──
    if action_id == "stop_kill_now":
        await _handle_stop_kill_now(payload, action, channel, msg_ts, user_id)
        return

    # ── Dashboard copy link ──
    if action_id == "mc_dashboard_copy":
        url = action.get("value", "")
        response_url = payload.get("response_url", "")
        if response_url and url:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "replace_original": False,
                        "response_type": "ephemeral",
                        "text": f"📋 Copy this link:\n```{url}```",
                    },
                )
        return

    # ── Link to Dashboard button ──
    if action_id == LINK_DASHBOARD_ACTION:
        user_id = payload.get("user", {}).get("id", "")
        if not is_allowed_user(user_id):
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="mc_link_dashboard",
                tool_kind="interaction",
                outcome="denied",
                metadata={"user_id": user_id, "reason": "not_allowed_user"},
            )
            return
        # Inbound channels-governance gate: linking imports the Slack thread's
        # content into a dashboard slot (``_import_thread_to_slot`` below), so a
        # ``channels`` policy that denies ``slack`` must stop it — otherwise a stale
        # link button issued before the deny would move denied Slack content into
        # the dashboard. Same gate as an inbound message / the OPTIONS + review
        # actions; gated here BEFORE the import side effect.
        if not await channel_inbound_permitted("slack"):
            logger.info("slack dashboard-link dropped: denied by channels governance policy")
            return
        thread_ts = payload.get("message", {}).get("thread_ts") or payload.get("container", {}).get(
            "thread_ts", ""
        )
        if not thread_ts:
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="mc_link_dashboard",
                tool_kind="interaction",
                outcome="failure",
                metadata={"user_id": user_id, "reason": "no_thread_ts"},
            )
            return
        ds = _orch.dashboard_state if _orch else None
        if not ds or not hasattr(ds, "get_or_create_slot"):
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="mc_link_dashboard",
                tool_kind="interaction",
                outcome="failure",
                metadata={"user_id": user_id, "reason": "no_dashboard"},
            )
            return
        if not _orch or not _orch.slack:
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="mc_link_dashboard",
                tool_kind="interaction",
                outcome="failure",
                metadata={"user_id": user_id, "reason": "no_slack_client"},
            )
            return
        slot = await _import_thread_to_slot(_orch.slack, ds, channel, thread_ts)
        if not slot:
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="mc_link_dashboard",
                tool_kind="interaction",
                outcome="failure",
                metadata={"channel": channel, "thread_ts": thread_ts, "reason": "empty_thread"},
            )
            response_url = payload.get("response_url", "")
            if response_url and response_url.startswith("https://hooks.slack.com/"):
                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url,
                        json={
                            "replace_original": False,
                            "response_type": "ephemeral",
                            "text": "⚠️ Could not import thread history.",
                        },
                    )
            return
        sel().log_tool_invocation(
            session_key=slot.key,
            agent="kirocrew",
            source="slack",
            tool_name="mc_link_dashboard",
            tool_kind="interaction",
            outcome="success",
            metadata={"slot": slot.key, "channel": channel, "thread_ts": thread_ts},
        )
        # Replace the button with confirmation
        response_url = payload.get("response_url", "")
        if response_url and response_url.startswith("https://hooks.slack.com/"):
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "replace_original": False,
                        "response_type": "ephemeral",
                        "text": f"Linked to dashboard session *{slot.key}* -- messages sync both ways.",
                    },
                )
        return

    # ── Agent select dropdown ──
    if action_id == "mc_agent_select":
        await _handle_agent_select(payload, action, channel, msg_ts, user_id)
        return

    # ── Users multi-select ──
    if action_id == "mc_users_select":
        await _handle_users_select(payload, action, channel, msg_ts, user_id)
        return

    # ── Channels multi-select ──
    if action_id == "mc_channels_select":
        await _handle_channels_select(payload, action, channel, msg_ts, user_id)
        return

    # ── Session resume choice buttons ──
    if action_id.startswith("mc_resume_thread_"):
        await _handle_resume_choice(payload, action, channel, msg_ts, user_id, mode="thread")
        return
    if action_id.startswith("mc_resume_dm_"):
        await _handle_resume_choice(payload, action, channel, msg_ts, user_id, mode="dm")
        return

    # ── Session resume/end/new buttons ──
    if action_id.startswith("mc_session_resume_"):
        await _handle_session_resume(payload, action, channel, msg_ts, user_id)
        return
    if action_id.startswith("mc_session_end_"):
        await _handle_session_end(payload, action, channel, msg_ts, user_id)
        return
    if action_id.startswith("mc_inline_stop_"):
        await _handle_inline_stop(payload, action, channel, msg_ts, user_id)
        return
    if action_id == "mc_session_new":
        await _handle_session_new(payload, action, channel, msg_ts, user_id)
        return

    # ── Channel modal: activation change ──
    if action_id.startswith("mc_ch_activation_"):
        await _handle_ch_activation(payload, action)
        return

    # ── Channel modal: agent change ──
    if action_id.startswith("mc_ch_agent_"):
        await _handle_ch_agent(payload, action)
        return

    # ── Channel modal: remove channel ──
    if action_id.startswith("mc_ch_remove_"):
        await _handle_ch_remove(payload, action)
        return

    # ── Channel modal: add channel ──
    if action_id == "mc_ch_add":
        await _handle_ch_add(payload, action)
        return

    # ── Review mode: approve / edit / cancel ──
    # Inbound channels-governance gate for the CONTENT-POSTING review actions
    # (approve/edit/revise all post the stored agent draft to the channel). A
    # ``channels`` policy that denies ``slack`` must stop a stale review button
    # from posting agent content to a now-denied channel — same gate as an inbound
    # message. ``cancel`` only discards the draft (posts nothing), so it is exempt.
    if action_id in ("mc_review_approve", "mc_review_edit", "mc_review_revise"):
        if not await channel_inbound_permitted("slack"):
            logger.info("slack review action dropped: denied by channels governance policy")
            return
    if action_id == "mc_review_approve":
        await _handle_review_approve(payload, action)
        return
    if action_id == "mc_review_edit":
        await _handle_review_edit(payload, action)
        return
    if action_id == "mc_review_revise":
        await _handle_review_revise(payload, action)
        return
    if action_id == "mc_review_cancel":
        await _handle_review_cancel(payload, action)
        return

    # ── Allowlist / channel list remove buttons ──
    if action_id.startswith("mc_allowlist_remove_"):
        await _handle_allowlist_remove(payload, action, channel, msg_ts, user_id)
        return
    if action_id.startswith("mc_channel_remove_"):
        await _handle_channel_remove(payload, action, channel, msg_ts, user_id)
        return

    # ── Messaging-transport interactive tool approval (decider-backed) ──
    # These buttons come from the new transport path's SlackRenderer
    # (build_approval_blocks → mc_tool_approve_/trust_/deny_<rid>).
    # Resolve the per-turn SlackApprovalDecider via its process-global registry.
    if (
        action_id.startswith(TOOL_APPROVE_ACTION_PREFIX)
        or action_id.startswith(TOOL_TRUST_ACTION_PREFIX)
        or action_id.startswith(TOOL_DENY_ACTION_PREFIX)
    ):
        # Defense-in-depth auth gate: dispatch() already denies non-allowed
        # users at the top, but re-check here (deny-by-default) so a tool
        # approval / Trust escalation can never be resolved by an unauthorized
        # actor even if this branch is ever reached via another path. Mirrors
        # native _handle_tool_approval's explicit trust-escalation check.
        if not is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id or "unknown",
                operation="slack.transport_tool_approval",
                outcome="denied",
                source="slack",
                resources=f"action={action_id} unauthorized",
                error="unauthorized user",
            )
            return
        is_trust = action_id.startswith(TOOL_TRUST_ACTION_PREFIX)
        approved = is_trust or action_id.startswith(TOOL_APPROVE_ACTION_PREFIX)
        # value / action_id suffix carry the session-namespaced approval token
        # (session_key:request_id) so a click resolves ONLY its own session's
        # pending tool — kiro-cli request ids restart at 1 per session — plus the
        # prompt's own nonce, which is what distinguishes these buttons from an
        # earlier prompt's still clickable at the same key. Split rather than
        # forwarded whole so the audit line records the request, not the secret.
        approval_key, press_nonce = split_approval_token(
            action.get("value", "") or action_id.rsplit("_", 1)[-1]
        )
        # Inbound channels-governance gate: a button press resolves a tool approval
        # (executes the governed tool) or a Trust escalation, so a channels policy
        # that denies ``slack`` must stop it — same gate as an inbound message.
        # EXCEPTION: an explicit REJECT (not approve, not trust) is a DENIAL of the
        # tool, which is exactly what a channels-deny wants anyway — so resolve it as
        # False rather than silently dropping. Silently returning would strand the
        # kiro-cli approval future until it times out (~300s) with the tool neither
        # run nor cleanly refused. Only approve/trust are blocked outright.
        if approved and not await channel_inbound_permitted("slack"):
            logger.info("slack tool-approval dropped: denied by channels governance policy")
            # Resolve the pending future as DENIED so the tool is refused promptly
            # instead of left pending until timeout.
            SlackApprovalDecider.resolve_global(approval_key, False, nonce=press_nonce)
            sel().log_api_access(
                caller=user_id,
                operation="slack.transport_tool_approval",
                outcome="denied",
                source="slack",
                resources=f"approval_key={approval_key} channels_policy",
            )
            return
        # Trust grants per-session auto-approve BEFORE resolving, so subsequent
        # tools in this session are auto-approved (mirrors native trust_tool).
        if is_trust:
            sess_key = SlackApprovalDecider.session_for(approval_key, nonce=press_nonce)
            # No live prompt answers to this press, so there is no session to widen.
            # Granting on the empty key would escalate whatever later reads it.
            if sess_key:
                add_trusted_session(sess_key, _orch.sessions if _orch else None)
        resolved = SlackApprovalDecider.resolve_global(approval_key, approved, nonce=press_nonce)
        if not resolved:
            label = "⏱ This approval already expired."
            outcome = "expired"
        elif is_trust:
            label = "🔓 Trusted this session — tools auto-approved"
            outcome = "trusted"
        elif approved:
            label = "✅ Approved"
            outcome = "approved"
        else:
            label = "🚫 Denied"
            outcome = "denied"
        sel().log_api_access(
            caller=user_id,
            operation="slack.transport_tool_approval",
            outcome=outcome,
            source="slack",
            resources=f"approval_key={approval_key}",
        )
        if _orch and _orch.slack and channel and msg_ts:
            try:
                await _orch.slack.update_message(channel, msg_ts, text=label)
            except Exception:
                logger.debug("Failed to update transport approval message", exc_info=True)
        return

    # ── Tool approval buttons (approve / trust / reject) ──
    if channel and msg_ts:
        await _handle_tool_approval(payload, action_id, channel, msg_ts, user_id)


_resume_locks: dict[str, LoopBoundLock] = {}
_RESUME_TAB_CLOSED_MSG = (
    "Resumed in Slack. The dashboard tab stays closed; reopen it from History to see it there."
)


async def _handle_resume_choice(
    payload: dict,
    action: dict,
    channel: str,
    msg_ts: str,
    user_id: str,
    mode: str,
) -> None:
    """Dispatch session resume to thread or DM based on user choice."""
    if not is_owner(user_id):
        logger.warning("resume_choice rejected: non-owner %s", user_id)
        sel().log_api_access(
            caller=user_id,
            operation="slack.session_resume_choice",
            outcome="denied",
            source="slack",
        )
        return
    if not (_orch and _orch.sessions and _orch.slack):
        return

    try:
        val = json.loads(action.get("value", "{}"))
    except ValueError:
        return

    session_key = val.get("key", "")
    title = redact_and_truncate(val.get("title", session_key[:20]), max_chars=200)
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    src_channel = val.get("src_channel", channel)

    if not session_key:
        return

    # Bounded eviction to prevent unbounded memory growth
    if len(_resume_locks) > 1000:
        evicted = 0
        for k in list(_resume_locks):
            if evicted >= 200:
                break
            if not _resume_locks[k].locked():
                _resume_locks.pop(k, None)
                evicted += 1

    lock = _resume_locks.setdefault(session_key, LoopBoundLock())
    async with lock:
        # Re-check: session may have been linked while user was choosing
        existing_thread, existing_channel = _orch.sessions.get_slack_link(session_key)
        if existing_thread and existing_channel:
            link = (
                f"https://slack.com/archives/{existing_channel}/p{existing_thread.replace('.', '')}"
            )
            label = f"\U0001f9f5 Already active: <{link}|Go to conversation>"
            response_url = payload.get("response_url", "")
            if response_url:
                try:
                    async with aiohttp.ClientSession() as sess:
                        await sess.post(
                            response_url, json={"replace_original": True, "text": label}
                        )
                except Exception:
                    pass
            return

        # Inbound channels-governance gate: resuming a session POSTS the stored
        # transcript — including agent (assistant) content — to the channel (same
        # class of side effect as the review actions above). A ``channels`` policy
        # that denies ``slack`` must stop it, so stale agent content is never
        # published to a now-denied channel. Owner-initiated, but the content leak
        # is what the gate guards. (``!stop``-style cancellation exemptions do not
        # apply — a resume is not cancellation.)
        if not await channel_inbound_permitted("slack"):
            logger.info("slack session resume dropped: denied by channels governance policy")
            return

        # A closed dashboard chat still resumes in Slack (its session key survives
        # the close); only its dashboard tab stays shut, so say that, not more.
        ds, slot = _orch.dashboard_state, session_key.split(":", 1)[-1]
        is_dash = session_key.startswith("dashboard:")
        tab_closed = ds is not None and is_dash and not ds.slot_exists(slot)
        if mode == "thread":
            target_channel = src_channel
            thread_msg = f"\U0001f9f5 *{title}*\n" + (
                _RESUME_TAB_CLOSED_MSG
                if tab_closed
                else "Session resumed. Continue the conversation in this thread."
            )
            try:
                thread_ts = await _orch.slack.post_message(target_channel, thread_msg)
            except Exception:
                logger.debug("Failed to create session thread", exc_info=True)
                return
            if not thread_ts:
                return
            link_ts, link_channel = thread_ts, target_channel
            label = f"\u25b6\ufe0f Resumed *{title}* in thread."
        elif mode == "dm":
            try:
                dm_channel = await _orch.slack.open_dm(user_id)
            except Exception:
                logger.debug("Failed to open DM for session resume", exc_info=True)
                return
            if not dm_channel:
                return
            header = "\u2500" * 15 + "\n" f"\U0001f504 Resumed: *{title}*\n" + "\u2500" * 15
            try:
                header_ts = await _orch.slack.post_message(dm_channel, header)
            except Exception:
                logger.debug("Failed to post DM resume header", exc_info=True)
                return
            if not header_ts:
                return
            link_ts, link_channel = header_ts, dm_channel
            thread_ts = header_ts
            target_channel = dm_channel
            label = f"\u25b6\ufe0f Resumed *{title}* in DM."
        else:
            return
        if tab_closed:
            label = f"\u25b6\ufe0f *{title}*: {_RESUME_TAB_CLOSED_MSG}"

        _orch.sessions.set_slack_link(session_key, link_ts, link_channel)
        sel().log_api_access(
            caller=user_id,
            operation="slack.session_resume",
            outcome="allowed",
            source="slack",
            resources=session_key,
        )
        if _orch.dashboard_state:
            slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
            _orch.dashboard_state.link_slack(slot_name, link_ts, link_channel)

        # Post last 5 messages as context
        try:
            from kiro_crew.config.loader import data_home

            sess_dir = data_home() / "sessions"
            stem = session_key.split(":", 1)[-1] if ":" in session_key else session_key
            jsonl = sess_dir / f"{stem}.jsonl"
            if not jsonl.exists() and not stem.startswith("dashboard_"):
                jsonl = sess_dir / f"dashboard_{stem}.jsonl"
            if jsonl.exists():
                # Whole-transcript read, bounded only by conversation length
                # (multi-MB for long sessions) — off-loop so it cannot stall
                # the event loop and its watchdog heartbeat.
                raw = await asyncio.to_thread(jsonl.read_text, encoding="utf-8")
                lines = raw.splitlines()
                msgs: list[tuple[str, str]] = []
                for ln in lines:
                    try:
                        d = json.loads(ln.strip())
                    except ValueError:
                        continue
                    if d.get("_type"):
                        continue
                    role = d.get("role", "")
                    txt = (d.get("content") or "")[:2000]
                    if role in ("user", "assistant") and txt:
                        msgs.append((role, txt))
                for role, txt in msgs[-5:]:
                    txt, _ = redact_exfiltration_urls(txt)
                    txt, _ = redact_credentials(txt)
                    icon = "\U0001f9d1" if role == "user" else "\U0001f916"
                    try:
                        await _orch.slack.post_message(
                            target_channel,
                            f"{icon} {txt}",
                            thread_ts,
                        )
                    except Exception:
                        logger.debug("Failed to post context message", exc_info=True)
        except Exception:
            logger.debug("Failed to post session context", exc_info=True)

        # Update the choice message
        response_url = payload.get("response_url", "")
        if response_url:
            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url,
                        json={"replace_original": True, "text": label},
                    )
            except Exception:
                pass


# The handlers live in the ``interactions_*`` owner modules beside this one, lowest layer
# first. A read of ``kiro_crew.slack.interactions.<name>`` is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch``, ``init``'s own
# write of ``_orch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`).
# ``dispatch`` and ``_handle_resume_choice`` stay here: the resume choice's session-file
# lookup is a stat inside an ``async def`` that the sync-io gate's fs baseline records
# against this file, and ``dispatch`` is its caller. Owners never import this module,
# and they log as ``kiro_crew.slack.interactions``. New work goes to the owner of its
# responsibility, listed in the slack-gateway spec.
# isort: off
from kiro_crew.slack.interactions_context import _handle_cron_ack  # noqa: E402
from kiro_crew.slack.interactions_context import _handle_subagent_ack  # noqa: E402
from kiro_crew.slack.interactions_context import _orch  # noqa: E402
from kiro_crew.slack.interactions_context import register_view_handler  # noqa: E402
from kiro_crew.slack.interactions_modals import _get_forward_callback  # noqa: E402
from kiro_crew.slack.interactions_modals import _handle_message_shortcut  # noqa: E402
from kiro_crew.slack.interactions_modals import _handle_shortcut_submission  # noqa: E402
from kiro_crew.slack.interactions_modals import handle_view_closed  # noqa: E402
from kiro_crew.slack.interactions_modals import handle_view_submission  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_agent_select  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_allowlist  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_allowlist_remove  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_ch_activation  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_ch_add  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_ch_agent  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_ch_remove  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_channel_remove  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_channels_select  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_track_channel  # noqa: E402
from kiro_crew.slack.interactions_settings import _handle_users_select  # noqa: E402
from kiro_crew.slack.interactions_options import _handle_options  # noqa: E402
from kiro_crew.slack.interactions_options import _handle_options_submit  # noqa: E402
from kiro_crew.slack.interactions_options import _import_thread_to_slot  # noqa: E402
from kiro_crew.slack.interactions_sessions import _handle_inline_stop  # noqa: E402
from kiro_crew.slack.interactions_sessions import _handle_session_end  # noqa: E402
from kiro_crew.slack.interactions_sessions import _handle_session_new  # noqa: E402
from kiro_crew.slack.interactions_sessions import _handle_session_resume  # noqa: E402
from kiro_crew.slack.interactions_sessions import _handle_stop_cancel  # noqa: E402
from kiro_crew.slack.interactions_sessions import _handle_stop_confirm  # noqa: E402
from kiro_crew.slack.interactions_sessions import _handle_stop_kill_now  # noqa: E402
from kiro_crew.slack.interactions_approvals import _handle_review_approve  # noqa: E402
from kiro_crew.slack.interactions_approvals import _handle_review_cancel  # noqa: E402
from kiro_crew.slack.interactions_approvals import _handle_review_edit  # noqa: E402
from kiro_crew.slack.interactions_approvals import _handle_review_revise  # noqa: E402
from kiro_crew.slack.interactions_approvals import _handle_tool_approval  # noqa: E402

# isort: on
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.slack.interactions_context",
    "kiro_crew.slack.interactions_modals",
    "kiro_crew.slack.interactions_settings",
    "kiro_crew.slack.interactions_options",
    "kiro_crew.slack.interactions_sessions",
    "kiro_crew.slack.interactions_approvals",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name the module and its owners bind.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
# isort: off
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.slack.interactions_context import VIEW_REGISTRY  # noqa: F401
    from kiro_crew.slack.interactions_context import ViewHandler  # noqa: F401
    from kiro_crew.slack.interactions_context import _probe_tracked_channel_scope  # noqa: F401
    from kiro_crew.slack.interactions_context import ack_button  # noqa: F401
    from kiro_crew.slack.interactions_modals import _FENCE_MARKER_NEUTRALIZED  # noqa: F401
    from kiro_crew.slack.interactions_modals import _FENCE_MARKER_RE  # noqa: F401
    from kiro_crew.slack.interactions_modals import _FENCE_WORD_JOIN  # noqa: F401
    from kiro_crew.slack.interactions_modals import _extend_fence_span_left  # noqa: F401
    from kiro_crew.slack.interactions_modals import _fence_fold  # noqa: F401
    from kiro_crew.slack.interactions_modals import _handle_config_submission  # noqa: F401
    from kiro_crew.slack.interactions_modals import _handle_voice_config_submission  # noqa: F401
    from kiro_crew.slack.interactions_modals import _neutralize_fence_markers  # noqa: F401
    from kiro_crew.slack.interactions_settings import _refresh_channels_modal  # noqa: F401
    from kiro_crew.slack.interactions_options import _ACTION_PAYLOAD_CAP  # noqa: F401
    from kiro_crew.slack.interactions_options import _ACTION_PREFIX  # noqa: F401
    from kiro_crew.slack.interactions_options import _extract_selected_value  # noqa: F401
    from kiro_crew.slack.interactions_options import _forget_options_control  # noqa: F401
    from kiro_crew.slack.interactions_options import _mark_button_clicked  # noqa: F401
    from kiro_crew.slack.interactions_options import _options_block_id  # noqa: F401
    from kiro_crew.slack.interactions_options import _options_choices_from_payload  # noqa: F401
    from kiro_crew.slack.interactions_options import _refuse_stale_options  # noqa: F401
    from kiro_crew.slack.interactions_options import _route_action_to_session  # noqa: F401
    from kiro_crew.slack.interactions_sessions import _clear_session_dismissed  # noqa: F401
    from kiro_crew.slack.interactions_sessions import _record_session_dismissed  # noqa: F401
    from kiro_crew.slack.interactions_approvals import _REVIEW_AUTH_DENIED_MSG  # noqa: F401
    from kiro_crew.slack.interactions_approvals import _can_act_on_review_draft  # noqa: F401
    from kiro_crew.slack.interactions_approvals import _delete_review_placeholder  # noqa: F401
    from kiro_crew.slack.interactions_approvals import _handle_review_edit_submit  # noqa: F401
    from kiro_crew.slack.interactions_approvals import _handle_review_revise_submit  # noqa: F401
    from kiro_crew.slack.interactions_approvals import _parse_draft_key  # noqa: F401
    from kiro_crew.slack.interactions_approvals import _post_review_auth_error  # noqa: F401
# isort: on
