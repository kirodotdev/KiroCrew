"""What a native Slack turn leaves in the thread once its answer is out.

The timing footer and the OPTIONS / Link-to-Dashboard controls it carries, the review-mode
draft post and its store, the copy of the answer a linked dashboard slot receives, and the
thread auto-title task.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _NO_RESPONSE,
        _REVIEW_DRAFT_MAX,
        _REVIEW_DRAFT_TTL,
        _REVIEW_PLACEHOLDER_TS,
        HUMAN_TURN_META_KEY,
        ConversationLog,
        LLMProvider,
        SessionManager,
        SlackClientOps,
        _AnswerStream,
        _dashboard_state,
        _review_drafts,
        auto_title,
        logger,
    )


def _review_drafts_get(key: str) -> tuple[str, str]:
    """Get (draft, requester_user_id), returning ("","") if missing or expired."""
    entry = _review_drafts.get(key)
    if entry is None:
        return "", ""
    draft, requester, ts = entry
    if time.monotonic() - ts > _REVIEW_DRAFT_TTL:
        _review_drafts.pop(key, None)
        return "", ""
    return draft, requester


def _review_drafts_set(key: str, draft: str, requester_user_id: str) -> None:
    """Store a draft with TTL + requester id, evicting oldest if at capacity."""
    now = time.monotonic()
    # Evict expired entries
    expired = [k for k, (_, _, ts) in _review_drafts.items() if now - ts > _REVIEW_DRAFT_TTL]
    for k in expired:
        _review_drafts.pop(k, None)
    # Evict oldest if still at capacity
    if len(_review_drafts) >= _REVIEW_DRAFT_MAX:
        oldest_key = min(_review_drafts, key=lambda k: _review_drafts[k][2])
        _review_drafts.pop(oldest_key, None)
    _review_drafts[key] = (draft, requester_user_id, now)


def _review_drafts_pop(key: str) -> tuple[str, str]:
    """Pop (draft, requester_user_id), returning ("","") if missing or expired."""
    entry = _review_drafts.pop(key, None)
    if entry is None:
        return "", ""
    draft, requester, ts = entry
    if time.monotonic() - ts > _REVIEW_DRAFT_TTL:
        return "", ""
    return draft, requester


def build_timing_footer(
    elapsed: float,
    client: LLMProvider | None = None,
) -> tuple[list[dict], str]:
    """Build the timing/context footer blocks for a Slack response.

    Returns ``(blocks, fallback_text)`` suitable for ``post_blocks``.
    """
    if elapsed < 60:
        duration = f"{int(elapsed)}s"
    else:
        mins, secs = divmod(int(elapsed), 60)
        duration = f"{mins}m {secs}s"
    footer_text = f"Finished in {duration}"
    if client is not None:
        try:
            ctx_pct = round(client.context_usage_pct())
            if ctx_pct >= 70:
                ctx_icon = "🔴"
            elif ctx_pct >= 50:
                ctx_icon = "🟠"
            elif ctx_pct >= 30:
                ctx_icon = "🟡"
            else:
                ctx_icon = "🟢"
            footer_text = f"Finished in {duration} · {ctx_icon} ctx {ctx_pct}%"
        except Exception:
            logger.debug("Failed to retrieve context usage", exc_info=True)
    blocks: list[dict] = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer_text}]}
    ]
    return blocks, footer_text


def _append_footer_actions(
    footer_blocks: list[dict],
    options: list[str] | None,
    thread_ts: str | None,
    linked_session_key: str | None,
    dashboard_state: object | None,
    staleness_token: str | None = None,
) -> list[dict]:
    """Append OPTIONS checkboxes and/or Link to Dashboard button to footer blocks.

    *staleness_token* must be minted by the caller, which is async and can do the
    transcript read off the event loop. Absent it the control posts untokened and
    clicks on it are honoured unconditionally.
    """
    if options:
        from kiro_crew.slack.format import build_options_blocks

        footer_blocks.extend(build_options_blocks(options, staleness_token=staleness_token))
    if thread_ts and not linked_session_key and dashboard_state:
        from kiro_crew.slack.format import build_link_dashboard_button

        if footer_blocks and footer_blocks[-1].get("type") == "actions":
            footer_blocks[-1]["elements"].append(build_link_dashboard_button())
        else:
            footer_blocks.append({"type": "actions", "elements": [build_link_dashboard_button()]})
    return footer_blocks


async def _maybe_auto_title_slack(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    session_key: str,
    conversation_log: ConversationLog | None,
    user_text: str,
    assistant_text: str,
    *,
    pin: auto_title.RecordPin,
) -> None:
    """Generate and set a Slack thread title after the first response.

    ``pin`` is captured by the CALLER before this task is scheduled, and is
    required rather than defaulted -- see ``auto_title.pin_record``.
    """

    async def _set_thread_title(title: str) -> None:
        await slack.set_thread_title(channel, session_key, title)

    await auto_title.maybe_auto_title(
        sessions,
        conversation_log,
        session_key,
        user_text,
        assistant_text,
        pin=pin,
        source="slack",
        resources=f"{channel}:{session_key}",
        set_channel_title=_set_thread_title,
    )


async def _post_review_draft(
    answer: _AnswerStream,
    slack: SlackClientOps,
    channel: str,
    reply_ts: str,
    thread_ts: str | None,
    user_id: str,
    session_key: str,
    clean_text: str,
) -> bool:
    """Replace a review-mode turn's visible output with an ephemeral draft.

    Stops and deletes the hidden stream, shows "Awaiting review…", and posts the draft
    with its approve / edit / cancel buttons to the requester alone. Returns whether
    the draft post landed: it is the review path's answer-carrying delivery, so the
    caller books a failure when it did not.
    """
    from kiro_crew.slack.blocks import review_draft_blocks

    # Stop streaming, delete placeholder, set status indicator
    if answer.stream_ts and answer.stream_ts != _REVIEW_PLACEHOLDER_TS:
        if answer.use_slack_stream:
            try:
                await slack.stop_stream(channel, answer.stream_ts)
            except Exception:
                pass
        try:
            await slack.delete_message(channel, answer.stream_ts)
        except Exception:
            logger.debug("Failed to delete stream msg in review mode", exc_info=True)
    await slack.set_thread_status(channel, reply_ts, "Awaiting review…")
    # Post ephemeral draft with approve/edit/cancel buttons. This is the
    # review path's answer-carrying delivery: its failure means the reader
    # got no draft, so it books a failure rather than leaving the breaker
    # with no verdict at all.
    draft = clean_text or _NO_RESPONSE
    draft_key = f"{channel}|{reply_ts}|{uuid.uuid4().hex[:8]}"
    blocks = review_draft_blocks(draft, draft_key)
    try:
        await slack.post_ephemeral(
            channel,
            user_id,
            draft,
            blocks=blocks,
            thread_ts=reply_ts if thread_ts else None,
        )
    except Exception:
        logger.exception("Slack review-draft post failed for %s", session_key)
        return False
    # Store draft for button handlers (requester can act on their own draft)
    _review_drafts_set(draft_key, draft, user_id)
    logger.info("Review mode: ephemeral draft sent to %s in %s", user_id, channel)
    return True


def _mirror_to_dashboard(linked_session_key: str, text: str, accumulated: str) -> None:
    """Append this Slack turn to the dashboard slot its thread is linked to."""
    try:
        ds = _dashboard_state
        slot_name = linked_session_key.removeprefix("dashboard:")
        slot = getattr(ds, "_slots", {}).get(slot_name)
        if slot:
            # The person typed this in Slack; mirroring it into the
            # linked slot keeps it a human turn (see
            # history.HUMAN_TURN_META_KEY).
            slot.append("user", text, "msg msg-u", meta={HUMAN_TURN_META_KEY: True})
            slot.append("assistant", accumulated, "msg msg-a")
            if slot._on_message:
                slot._on_message(slot.key, {"role": "user", "content": text, "cls": "msg msg-u"})
                slot._on_message(
                    slot.key,
                    {"role": "assistant", "content": accumulated, "cls": "msg msg-a"},
                )
            # ``ds`` is set: the caller mirrors only while ``_dashboard_state`` is.
            ds.push_slots_update()  # type: ignore[attr-defined,union-attr]
    except Exception:
        logger.debug("Failed to mirror Slack message to dashboard", exc_info=True)
