"""OPTIONS controls and agent action buttons: a click becomes the user's answer to the agent.

``_handle_options`` takes a one-click OPTIONS choice, ``_open_options_several`` opens the
"Choose several..." modal and ``_handle_options_several_submission`` takes its answer.
Both answers go through ``_answer_options``: it refuses a superseded or already answered
control (``_refuse_stale_options``, an "Already answered" ephemeral), edits the card in
place to "<who> chose <choice>" and sends the choice to the agent session. An ``action::`` button reaches
the session through ``_route_action_to_session``. ``_import_thread_to_slot`` fetches a
Slack thread, redacts it and imports it into a new dashboard slot.

Split out of ``kiro_crew.slack.interactions``, which stays the import path and the
patch target: every name here is reached as ``kiro_crew.slack.interactions.<name>``,
and a patch through that facade reaches this module (``kiro_crew._facade_mirror``).
Nothing here imports the facade. New OPTIONS work goes here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from kiro_crew.dashboard.chat_utils import (
    forget_slack_options_for_thread,
    options_control_is_stale,
    slack_options_owner_keys_snapshot,
    slack_options_slot,
)
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.channel_policy import channel_refusal
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_SEVERAL_BLOCK,
    OPTIONS_SEVERAL_CALLBACK,
    OPTIONS_SEVERAL_INPUT,
    build_options_answered_blocks,
    build_options_several_modal,
    escape_mrkdwn,
    replace_options_blocks,
)
from kiro_crew.slack.handler import APPROVAL_INTERACTIVE, handle_message, is_allowed_user
from kiro_crew.slack.interactions_context import _orch, register_view_handler
from kiro_crew.slack.outbound import (
    OptionsSeveralRequest,
    PostedOptions,
    claim_options_answer,
    decode_options_token,
    expire_options,
    mark_options_terminal,
    options_answered,
    options_edit_lock,
    release_options_answer,
    remember_options_several,
    settle_options_answer,
    take_options_several,
)
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger("kiro_crew.slack.interactions")


# ---------------------------------------------------------------------------
# Individual action handlers
# ---------------------------------------------------------------------------


def _mark_button_clicked(blocks: list[dict], clicked_action_id: str, label: str) -> list[dict]:
    """Replace a clicked button with a ✓ context block in the Block Kit message.

    Walks *blocks* looking for an ``actions`` block containing *clicked_action_id*.
    Removes that button element and inserts a ``context`` block with
    ``✓ {label}`` immediately before the actions block.  If no elements
    remain, the empty actions block is dropped entirely.
    """
    result: list[dict] = []
    for block in blocks:
        if block.get("type") != "actions":
            result.append(block)
            continue
        elements = block.get("elements", [])
        remaining = [e for e in elements if e.get("action_id") != clicked_action_id]
        if len(remaining) == len(elements):
            # Clicked button not in this actions block — keep as-is
            result.append(block)
            continue
        # Insert ✓ context block before the (possibly empty) actions block
        result.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"✓ {label}"}]})
        if remaining:
            result.append({**block, "elements": remaining})
    return result


_ACTION_PREFIX = "action::"


def _forget_options_control(
    thread_ts: str,
    ts: str | None = None,
    keys: tuple[str, ...] | None = None,
    channel: str | None = None,
) -> None:
    """Drop the recorded OPTIONS control for *thread_ts*'s conversation.

    A click has just re-rendered the message with the user's selection, so the
    turn-start expiry must not run over it afterwards — striking every choice
    through would erase the choice they made. A thread can be owned either by a
    Slack-born session or by a dashboard session mirroring into it, so this
    clears every key that one conversation can be recorded under.

    *keys* is a snapshot taken BEFORE the Slack edit. A relink landing during that
    edit moves the thread to another session, so resolving the keys afterwards
    names the NEW owner and leaves the previous owner's record behind — whose next
    turn then edits over the selection.

    *channel* with *ts* also drops the record of that one message under whatever
    key holds it. A card posted top-level (a code channel's own replies) carries
    no ``thread_ts``, so the thread a click falls back to is the card itself and
    the keys resolved from it name no session at all.
    """
    if not _orch or not thread_ts:
        return
    try:

        forget_slack_options_for_thread(
            _orch.dashboard_state, thread_ts, ts, keys=keys, channel=channel
        )
    except Exception:
        logger.debug("Failed to clear recorded OPTIONS control", exc_info=True)


def _extract_selected_value(action: dict) -> tuple[str, str]:
    """Return ``(raw_value, display_text)`` from an extended element payload."""
    opt = action.get("selected_option")
    if opt:
        return opt.get("value", ""), opt.get("text", {}).get("text", "")
    for field in ("selected_date", "selected_time"):
        val = action.get(field)
        if val:
            return val, val
    dt = action.get("selected_date_time")
    if dt is not None:
        return str(dt), str(dt)
    return "", ""


_ACTION_PAYLOAD_CAP = 4000


async def _route_action_to_session(
    channel: str,
    msg_ts: str,
    thread_ts: str,
    user_id: str,
    team_id: str,
    label: str,
    payload_str: str,
    context_tag: str,
    action_id_value: str,
    blocks: list[dict],
) -> None:
    """Shared logic for routing an action:: interaction to the agent session."""
    assert _orch and _orch.slack  # caller already checked  # noqa: S101

    # Redact label before any Slack surface
    label, _ = redact_exfiltration_urls(label)
    label = redact_credentials(label)[0]

    # Update the message: replace clicked element with ✓ label
    updated_blocks = _mark_button_clicked(blocks, action_id_value, label)
    try:
        await _orch.slack.update_message(channel, msg_ts, text=label, blocks=updated_blocks)
    except Exception:
        logger.debug("Failed to update action message", exc_info=True)

    # Post display text as visible user message
    new_ts = await _orch.slack.post_message(channel, label, thread_ts)
    if not new_ts:
        logger.warning("Failed to post action label — aborting action routing")
        return

    # Redact and cap payload before embedding in context
    payload_str, _ = redact_exfiltration_urls(payload_str)
    payload_str = redact_credentials(payload_str)[0]
    if len(payload_str) > _ACTION_PAYLOAD_CAP:
        payload_str = payload_str[:_ACTION_PAYLOAD_CAP] + "… [truncated]"

    # SEL audit trail
    sel().log_api_access(
        caller=user_id,
        operation=f"slack.{context_tag.split()[0].lower()}",
        outcome="allowed",
        source="slack",
        resources=action_id_value,
    )

    action_context = (
        "--- CONTEXT ENTRY BEGIN ---\n"
        f"[{context_tag}: {payload_str}]\n"
        "--- CONTEXT ENTRY END ---"
    )

    t = asyncio.create_task(
        handle_message(
            _orch.slack,
            _orch.sessions,  # type: ignore[arg-type]
            channel,
            label,
            thread_ts,
            new_ts,
            user_id,
            team_id=team_id,
            approval_mode=APPROVAL_INTERACTIVE,
            context_builder=_orch.ctx_builder,
            cron_service=_orch.cron_svc,
            conversation_log=_orch.conv_log,
            consolidator=_orch.consolidator,
            subagent_manager=_orch.subagent_mgr,
            task_runner=_orch.task_runner,
            action_context=action_context,
            start_priority=StartPriority.FOREGROUND,
        )
    )
    _orch._handler_tasks.add(t)
    t.add_done_callback(_orch._handler_tasks.discard)


async def _import_thread_to_slot(slack: Any, ds: Any, channel: str, thread_ts: str) -> Any:
    """Fetch a Slack thread, redact messages, and import into a new dashboard slot."""
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    # Idempotency: return existing slot if already linked
    existing = ds.get_linked_slot(thread_ts)
    if existing:
        return existing

    msgs = await slack.fetch_thread_replies(channel, thread_ts)
    if not msgs:
        return None
    # Pre-filter: drop empty text and !link-to-dashboard messages
    msgs = [
        m
        for m in msgs
        if m.get("text", "").strip() and not m.get("text", "").startswith("!link-to-dashboard")
    ]
    if not msgs:
        return None
    # Cap to last 50 messages to avoid bloating the slot
    truncated = len(msgs) > 50
    if truncated:
        msgs = msgs[-50:]
    slot = ds.get_or_create_slot()
    slot.title = f"Slack thread {thread_ts[:10]}" + (" (truncated)" if truncated else "")
    bot_id = getattr(ds, "_self_bot_id", None) or ""
    for m in msgs:
        is_bot = bool(m.get("bot_id")) or m.get("user") == bot_id
        role = "assistant" if is_bot else "user"
        text_content = m.get("text", "")
        text_content, _ = redact_exfiltration_urls(text_content)
        text_content, _ = redact_credentials(text_content)
        slot.append(role, text_content, f"msg msg-{'a' if is_bot else 'u'}")
    ds.link_slack(slot.key, thread_ts, channel)
    await save_slot_off_loop(ds, slot)
    ds.push_slots_update()
    return slot


def _options_block_id(payload: dict, action: dict | None = None) -> str | None:
    """The ``block_id`` Slack echoed back for the clicked OPTIONS control.

    A click carries it on the action (and on the payload's ``actions`` list). On a
    card of several rows it carries the row's id -- the token plus a ``~<row>``
    suffix, which :func:`kiro_crew.slack.outbound.decode_options_token` reads past.
    """
    if action:
        bid = action.get("block_id")
        if isinstance(bid, str) and bid:
            return bid
    for entry in payload.get("actions") or []:
        bid = entry.get("block_id") if isinstance(entry, dict) else None
        if isinstance(bid, str) and bid:
            return bid
    return None


def _options_choices_from_payload(blocks: list) -> list[str]:
    """The choices shown on a posted control, read back off its own blocks.

    Recovering them from the message the user clicked is what lets a stale click
    be struck through without the gateway having kept a record of the control --
    which is the point: a record held in memory is exactly what a restart loses.
    """
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        for el in block.get("elements") or []:
            if not isinstance(el, dict):
                continue
            action_id = el.get("action_id", "")
            if action_id.startswith(OPTIONS_ACTION_PREFIX):
                return [
                    e.get("value") or ""
                    for b in blocks
                    if isinstance(b, dict)
                    for e in b.get("elements") or []
                    if isinstance(e, dict)
                    and e.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
                ]
    return []


async def _refuse_stale_options(channel: str, msg_ts: str, payload: dict) -> None:
    """Strike a superseded control through and answer nothing.

    Correctness is already settled by the time this runs -- the caller returned
    without dispatching -- so the edit here is presentation only and its failure
    is swallowed. An un-struck control is untidy, not unsafe: the next click on it
    is judged by the same rule and refused again.

    Runs under the message's edit lock and takes the answer claim, because a
    concurrent click that was ACCEPTED renders the user's selection into this same
    message. Editing without the lock could overwrite that selection with a
    strike-through, destroying a legitimate answer to satisfy a stale one.
    """
    if not (_orch and _orch.slack):
        return
    blocks = (payload.get("message") or {}).get("blocks") or []
    async with options_edit_lock(channel, msg_ts):
        if not claim_options_answer(channel, msg_ts):
            # An accepted click already holds the claim and has rendered its
            # selection. Leave the message exactly as that click left it.
            return
        # The claim, held unsettled, already refuses every later click. It is
        # marked terminal (settled) only once the strike-through lands: a failed
        # strike leaves the buttons on screen, so a later turn's expiry must still
        # find the control unsettled and strike it.
        try:
            struck = await expire_options(
                _orch.slack,
                PostedOptions(
                    channel=channel,
                    ts=msg_ts,
                    choices=tuple(_options_choices_from_payload(blocks)),
                    blocks=tuple(blocks),
                ),
            )
        except Exception:
            struck = False
            logger.debug(
                "could not strike through the stale OPTIONS control %s/%s",
                channel,
                msg_ts,
                exc_info=True,
            )
        if struck:
            mark_options_terminal(channel, msg_ts)


_ALREADY_ANSWERED = "Already answered."
_NOT_AUTHORIZED = "⛔ You are not authorized to use these buttons."


async def _tell_user(channel: str, user_id: str, thread_ts: str, text: str) -> None:
    """A private note to *user_id* about their click. Best-effort."""
    if not (_orch and _orch.slack and channel and user_id):
        return
    try:
        await _orch.slack.post_ephemeral(channel, user_id, text, thread_ts=thread_ts)
    except Exception:
        logger.debug("Failed to send OPTIONS ephemeral", exc_info=True)


async def _answer_options(
    *,
    channel: str,
    msg_ts: str,
    thread_ts: str,
    user_id: str,
    team_id: str,
    block_id: str | None,
    blocks: list[dict],
    selected_indices: list[int],
    tool_name: str,
) -> None:
    """Answer the OPTIONS card posted as *msg_ts* with the choices at *selected_indices*.

    The one answer path for a one-click button and for the "Choose several..." modal,
    so both send exactly the same text down the same path: the chosen button values,
    joined with ", ", as the user's message, with an ``[OPTIONS multi-select: ...]``
    context entry. The choices are read back off the card's own buttons -- the full
    text, not the possibly cut label.

    Answers once. A superseded control (the conversation moved on) or one already
    answered is refused with a private "Already answered" and sends nothing; the
    claim is taken under the message's edit lock, so two quick clicks cannot both
    send. On success the card is edited in place: its buttons give way to a
    ``✅ <who> chose <choice> · <time>`` line.
    """
    if not (_orch and _orch.slack):
        return
    all_choices = _options_choices_from_payload(blocks)
    picked: list[int] = []
    for i in selected_indices:
        if 0 <= i < len(all_choices) and i not in picked:
            picked.append(i)
    picked.sort()
    if not picked:
        sel().log_tool_invocation(
            session_key=thread_ts,
            agent="kirocrew",
            source="slack",
            tool_name=tool_name,
            tool_kind="interaction",
            outcome="skipped",
            metadata={"reason": "empty_selection"},
        )
        return

    # One rule, checked before any work: does this control still belong to the
    # question the conversation is actually on? Judged from the token in the
    # message plus the transcript on disk, so it holds across a restart -- and it
    # is the ONLY thing standing between a superseded button and a dispatched
    # answer, since nothing retires controls ahead of time any more.
    if await options_control_is_stale(
        _orch.dashboard_state if _orch else None, block_id, thread_ts
    ):
        sel().log_tool_invocation(
            session_key=thread_ts,
            agent="kirocrew",
            source="slack",
            tool_name=tool_name,
            tool_kind="interaction",
            outcome="denied",
            metadata={"reason": "superseded_control", "channel": channel},
        )
        await _refuse_stale_options(channel, msg_ts, {"message": {"blocks": blocks}})
        await _tell_user(channel, user_id, thread_ts, _ALREADY_ANSWERED)
        return

    # Redact LLM-generated content before any external use.
    selected = [redact_credentials(redact_exfiltration_urls(all_choices[i])[0])[0] for i in picked]
    combined = ", ".join(selected)
    # The Slack-facing FALLBACK TEXT, escaped. Slack parses entities in a
    # message's top-level `text` too, not just in mrkdwn blocks -- and `text` is
    # what notifications and block-less clients render, so an unescaped
    # `<!channel>` here pages a whole channel even though the blocks are safe.
    #
    # A SEPARATE variable on purpose: `combined` itself must stay raw, because it
    # is also the answer echoed back into the session below. Escaping in place
    # would change what the user actually picked.
    combined_fallback = escape_mrkdwn(combined)

    # Edit-in-place: replace only the OPTIONS rows with the answered line,
    # preserving every other surrounding block. Falls back to post-and-delete if
    # update_message raises (resilience).
    answered_blocks = build_options_answered_blocks(user_id, selected, time.time())
    new_blocks = replace_options_blocks(blocks, answered_blocks)
    new_ts = msg_ts
    edited = False
    # Set when the original message survives our attempt to remove it: the
    # control is then STILL on screen and still clickable, so its record has to
    # outlive this answer for a later turn to expire it.
    original_still_live = False
    # Serialize against a concurrent expiry of this SAME message. The expiry
    # re-reads the record inside this lock and skips its edit once the forget
    # below has dropped it, so the user's answer cannot be overwritten by an
    # expiry that started first. Held across the edit AND the forget: releasing
    # between them would let an expiry observe a still-tracked record and edit
    # over the answer we just wrote.
    async with options_edit_lock(channel, msg_ts):
        # One answer per control. A second click on this message would otherwise
        # render the answer again and dispatch a second turn -- a duplicate, or
        # once the first turn has moved on, a superseded one. Claimed under the
        # lock so the check and the claim cannot interleave, and BEFORE the edit
        # so a loser touches nothing at all.
        if not claim_options_answer(channel, msg_ts):
            logger.debug(
                "%s: control %s/%s was already answered; dropping the duplicate click",
                tool_name,
                channel,
                msg_ts,
            )
            sel().log_tool_invocation(
                session_key=thread_ts,
                agent="kirocrew",
                source="slack",
                tool_name=tool_name,
                tool_kind="interaction",
                outcome="denied",
                metadata={"reason": "already_answered", "channel": channel},
            )
            await _tell_user(channel, user_id, thread_ts, _ALREADY_ANSWERED)
            return
        # Pin the conversation that ASKED, read from the control's own token --
        # NOT whoever owns the thread now. The two diverge in exactly the case
        # the pin exists to survive: after a handover, resolving from the thread
        # names the new owner and would deliver this answer to a conversation
        # that never asked the question.
        #
        # A pinned None is meaningful: it says the asker holds no slot (a native
        # Slack or cron conversation), so a thread linked after acceptance cannot
        # capture the answer either. An untokened control -- one posted before
        # tokens shipped -- pins nothing and keeps today's resolution, matching the
        # rest of the rule, which honours what it cannot judge.
        _asker = decode_options_token(block_id)
        _asker_key = _asker[0] if _asker else None
        _pinned_slot = (
            slack_options_slot(_orch.dashboard_state, _asker_key)
            if (_asker_key and _orch and _orch.dashboard_state)
            else None
        )
        _pinned_slot_name = getattr(_pinned_slot, "key", None)
        _route_pinned = _asker_key is not None
        # Whose record this control is, captured BEFORE the edit. A relink landing
        # during the edit would move the thread to another session, so resolving
        # after the fact names the NEW owner and leaves the previous owner's record
        # in place -- and that session's next turn would edit over this answer.
        _owner_keys = slack_options_owner_keys_snapshot(
            _orch.dashboard_state if _orch else None, thread_ts
        )
        try:
            try:
                await _orch.slack.update_message(
                    channel, msg_ts, text=combined_fallback, blocks=new_blocks
                )
                edited = True
            except Exception:
                logger.debug(
                    "update_message failed for %s, falling back to post+delete",
                    tool_name,
                    exc_info=True,
                )

            if not edited:
                posted_ts = await _orch.slack.post_blocks(
                    channel, answered_blocks, combined_fallback, thread_ts
                )
                if not posted_ts:
                    logger.warning("Failed to post options choice — aborting")
                    sel().log_tool_invocation(
                        session_key=thread_ts,
                        agent="kirocrew",
                        source="slack",
                        tool_name=tool_name,
                        tool_kind="interaction",
                        outcome="failure",
                        metadata={"reason": "post_blocks_failed"},
                    )
                    return
                new_ts = posted_ts
                try:
                    await _orch.slack.delete_message(channel, msg_ts)
                except Exception:
                    original_still_live = True
                    logger.warning(
                        "Failed to delete original OPTIONS message after fallback "
                        "post_blocks succeeded; user may see both the original "
                        "and the new answer message",
                        exc_info=True,
                    )
        finally:
            # Give the claim back only when the answer never reached Slack at all
            # -- neither the in-place edit nor the replacement post. Holding it
            # then would refuse every retry forever, leaving a control permanently
            # visible and permanently unanswerable. Once the answer IS on screen
            # the claim stays, even if a later step stumbles: the answer is
            # rendered and the turn is on its way.
            if not edited and new_ts == msg_ts:
                release_options_answer(channel, msg_ts)

        # The control is spent only once the original is actually gone — either
        # rewritten in place, or posted-and-deleted. When the delete failed those
        # buttons are still sitting in the channel, so the record has to stay.
        # Inside the lock with the edit above: an expiry that observed a
        # still-tracked record between the two would edit straight over the
        # answer we just wrote.
        if not original_still_live:
            # The buttons are provably off screen, so the claim on this control no
            # longer has to be pinned against a late click and may be evicted if
            # the map fills. While the original IS still live the claim stays put.
            settle_options_answer(channel, msg_ts)
            _forget_options_control(thread_ts, msg_ts, keys=_owner_keys, channel=channel)

    action_context = (
        "--- CONTEXT ENTRY BEGIN ---\n"
        f"[OPTIONS multi-select: {combined}]\n"
        "--- CONTEXT ENTRY END ---"
    )

    t = asyncio.create_task(
        handle_message(
            _orch.slack,
            _orch.sessions,  # type: ignore[arg-type]
            channel,
            combined,
            thread_ts,
            new_ts,
            user_id,
            team_id=team_id,
            approval_mode=APPROVAL_INTERACTIVE,
            context_builder=_orch.ctx_builder,
            cron_service=_orch.cron_svc,
            conversation_log=_orch.conv_log,
            consolidator=_orch.consolidator,
            subagent_manager=_orch.subagent_mgr,
            task_runner=_orch.task_runner,
            action_context=action_context,
            target_slot_name=_pinned_slot_name,
            route_pinned=_route_pinned,
            asker_key=_asker_key,
            start_priority=StartPriority.FOREGROUND,
        )
    )
    _orch._handler_tasks.add(t)
    t.add_done_callback(_orch._handler_tasks.discard)
    sel().log_tool_invocation(
        session_key=thread_ts,
        agent="kirocrew",
        source="slack",
        tool_name=tool_name,
        tool_kind="interaction",
        outcome="success",
        metadata={"selected": combined, "channel": channel},
    )


# ---------------------------------------------------------------------------
# "Choose several..." -- the overflow menu's modal
# ---------------------------------------------------------------------------


async def _open_options_several(payload: dict, action: dict, channel: str, msg_ts: str) -> None:
    """The ⋯ menu's "Choose several..." item: open the multi-select modal.

    Refuses an already answered card at once with a private "Already answered"
    instead of opening a form that could only lose. Staleness is judged when the
    modal is SUBMITTED, by the same check a click gets, since that is when an
    answer would be sent -- and because Slack's trigger expires in seconds.
    """
    if not (_orch and _orch.slack):
        return
    message = payload.get("message") or {}
    thread_ts = message.get("thread_ts") or msg_ts
    user_id = (payload.get("user") or {}).get("id", "")
    blocks = message.get("blocks") or []
    if options_answered(channel, msg_ts):
        await _tell_user(channel, user_id, thread_ts, _ALREADY_ANSWERED)
        return
    choices = _options_choices_from_payload(blocks)
    trigger_id = payload.get("trigger_id", "")
    if len(choices) < 2 or not trigger_id:
        return
    nonce = remember_options_several(
        OptionsSeveralRequest(
            channel=channel,
            msg_ts=msg_ts,
            thread_ts=thread_ts,
            team_id=(payload.get("team") or {}).get("id", ""),
            user_id=user_id,
            block_id=_options_block_id(payload, action),
            blocks=tuple(blocks),
            opened=time.monotonic(),
        )
    )
    try:
        await _orch.slack.views_open(trigger_id, build_options_several_modal(choices, nonce))
    except Exception:
        take_options_several(nonce)
        logger.warning("Failed to open the OPTIONS multi-select modal", exc_info=True)


async def _handle_options_several_submission(payload: dict) -> None:
    """The "Choose several..." modal was submitted: answer the card with every pick."""
    if not (_orch and _orch.slack):
        return
    view = payload.get("view") or {}
    user_id = (payload.get("user") or {}).get("id", "")
    request = take_options_several(view.get("private_metadata", ""))
    if request is None:
        logger.info("OPTIONS multi-select submitted for an unknown or expired card")
        return
    # The same gate every OPTIONS click passes in ``dispatch``: an allowed user,
    # then the channels governance policy -- both before anything is edited or sent.
    if user_id != request.user_id or not is_allowed_user(user_id):
        sel().log_tool_invocation(
            session_key=request.thread_ts,
            agent="kirocrew",
            source="slack",
            tool_name="options_several",
            tool_kind="interaction",
            outcome="denied",
            metadata={"user_id": user_id, "reason": "not_allowed_user"},
        )
        await _tell_user(request.channel, user_id, request.thread_ts, _NOT_AUTHORIZED)
        return
    channel = request.channel
    if not (
        await channel_inbound_permitted("slack")
        and not channel_refusal(channel, operation="slack.interactive", caller=user_id)
    ):
        logger.info("slack OPTIONS choice dropped: denied by channels governance policy")
        return
    values = (view.get("state") or {}).get("values") or {}
    picked = ((values.get(OPTIONS_SEVERAL_BLOCK) or {}).get(OPTIONS_SEVERAL_INPUT) or {}).get(
        "selected_options"
    ) or []
    indices: list[int] = []
    for option in picked:
        try:
            indices.append(int((option or {}).get("value", "")))
        except (TypeError, ValueError):
            continue
    await _answer_options(
        channel=request.channel,
        msg_ts=request.msg_ts,
        thread_ts=request.thread_ts,
        user_id=user_id,
        team_id=request.team_id,
        block_id=request.block_id,
        blocks=list(request.blocks),
        selected_indices=indices,
        tool_name="options_several",
    )


async def _handle_options_several_closed(payload: dict) -> None:
    """The "Choose several..." modal was cancelled: drop the card it was holding."""
    take_options_several((payload.get("view") or {}).get("private_metadata", ""))


register_view_handler(OPTIONS_SEVERAL_CALLBACK, _handle_options_several_submission)
register_view_handler(OPTIONS_SEVERAL_CALLBACK + "_closed", _handle_options_several_closed)


async def _handle_options(payload: dict, action: dict, channel: str, msg_ts: str) -> None:
    """User picked an OPTIONS choice — delete footer, post styled selection."""
    choice = action.get("value", "")
    # Overflow menus nest the value under selected_option
    if not choice:
        choice = (action.get("selected_option") or {}).get("value", "")
    action_id = action.get("action_id", "")
    if not ((choice or action_id.startswith(_ACTION_PREFIX)) and channel and _orch and _orch.slack):
        return

    thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
    user_id = payload.get("user", {}).get("id", "")
    team_id = (payload.get("team") or {}).get("id", "")
    blocks = payload.get("message", {}).get("blocks", [])

    # ── Standard OPTIONS choice: one click answers with that button's choice ──
    # Checked FIRST, whatever the value says: a numbered button is a choice on an
    # OPTIONS card, and its click sends that choice once, through the guarded
    # answer path. Reading an ``action::`` value off it would let a choice the
    # model wrote that way skip the staleness check, the once-only claim and the
    # asker pin.
    _suffix = action_id[len(OPTIONS_ACTION_PREFIX) :]
    if action_id.startswith(OPTIONS_ACTION_PREFIX) and _suffix.isascii() and _suffix.isdigit():
        await _answer_options(
            channel=channel,
            msg_ts=msg_ts,
            thread_ts=thread_ts,
            user_id=user_id,
            team_id=team_id,
            block_id=_options_block_id(payload, action),
            blocks=blocks,
            selected_indices=[int(_suffix)],
            tool_name="options",
        )
        return

    # ── Action button: route payload to existing session as context ──
    if choice.startswith(_ACTION_PREFIX):
        action_payload = choice[len(_ACTION_PREFIX) :]
        label = action.get("text", {}).get("text", "")
        # Overflow menus: label is on the selected_option
        if not label:
            label = (action.get("selected_option") or {}).get("text", {}).get("text", "")
        action_id_value = action.get("action_id", "")
        await _route_action_to_session(
            channel,
            msg_ts,
            thread_ts,
            user_id,
            team_id,
            label,
            action_payload,
            "Action button clicked",
            action_id_value,
            blocks,
        )
        return

    # ── Extended element: action_id carries the action:: prefix ──
    action_id_value = action.get("action_id", "")
    if action_id_value.startswith(_ACTION_PREFIX):
        base_json = action_id_value[len(_ACTION_PREFIX) :]
        raw_value, display_text = _extract_selected_value(action)

        # Merge selected_value into base payload
        try:
            merged = json.loads(base_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid JSON in action_id: %s", base_json[:200])
            return
        if not isinstance(merged, dict):
            logger.warning("Expected dict from action_id JSON, got %s", type(merged).__name__)
            return
        merged["selected_value"] = raw_value
        merged_json = json.dumps(merged)

        # Derive display label: placeholder + selected text
        placeholder = action.get("placeholder", {}).get("text", "")
        label = f"{placeholder}: {display_text}" if placeholder else display_text

        await _route_action_to_session(
            channel,
            msg_ts,
            thread_ts,
            user_id,
            team_id,
            label,
            merged_json,
            "Action element selected",
            action_id_value,
            blocks,
        )
        return
