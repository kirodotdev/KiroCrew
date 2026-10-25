"""OPTIONS controls and agent action buttons: a click becomes the user's answer to the agent.

``_handle_options`` takes one OPTIONS choice and ``_handle_options_submit`` a multi-select
submission: each refuses a superseded control (``_refuse_stale_options``), posts the
styled selection and sends it to the agent session. An ``action::`` button reaches
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
from typing import Any

from kiro_crew.dashboard.chat_utils import (
    forget_slack_options_for_thread,
    options_control_is_stale,
    slack_options_owner_keys_snapshot,
    slack_options_slot,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_CHECKBOXES_ACTION,
    build_options_selected_blocks,
    escape_mrkdwn,
    replace_options_blocks,
)
from kiro_crew.slack.handler import APPROVAL_INTERACTIVE, handle_message, is_allowed_user
from kiro_crew.slack.interactions_context import _orch
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
    thread_ts: str, ts: str | None = None, keys: tuple[str, ...] | None = None
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
    """
    if not _orch or not thread_ts:
        return
    try:

        forget_slack_options_for_thread(_orch.dashboard_state, thread_ts, ts, keys=keys)
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

    Checked in three places because the two click paths deliver it differently: a
    button click carries it on the action, and the multi-select block also keys
    ``state.values``, which is recoverable even from a click that omitted it.
    """
    if action:
        bid = action.get("block_id")
        if isinstance(bid, str) and bid:
            return bid
    for entry in payload.get("actions") or []:
        bid = entry.get("block_id") if isinstance(entry, dict) else None
        if isinstance(bid, str) and bid:
            return bid
    values = (payload.get("state") or {}).get("values") or {}
    for block_id, vals in values.items():
        if (
            isinstance(vals, dict)
            and OPTIONS_CHECKBOXES_ACTION in vals
            and isinstance(block_id, str)
        ):
            return block_id
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
            if action_id == OPTIONS_CHECKBOXES_ACTION:
                return [
                    (o.get("text") or {}).get("text") or o.get("value") or ""
                    for o in el.get("options") or []
                    if isinstance(o, dict)
                ]
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
        mark_options_terminal(channel, msg_ts)
        try:
            await expire_options(
                _orch.slack,
                PostedOptions(
                    channel=channel,
                    ts=msg_ts,
                    choices=tuple(_options_choices_from_payload(blocks)),
                    blocks=tuple(blocks),
                ),
            )
        except Exception:
            logger.debug(
                "could not strike through the stale OPTIONS control %s/%s",
                channel,
                msg_ts,
                exc_info=True,
            )


async def _handle_options_submit(payload: dict, channel: str, msg_ts: str) -> None:
    """User clicked Send on multi-select OPTIONS checkboxes."""
    if not (_orch and _orch.slack):
        return

    thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
    user_id = payload.get("user", {}).get("id", "")
    team_id = (payload.get("team") or {}).get("id", "")

    if not is_allowed_user(user_id):
        sel().log_tool_invocation(
            session_key=thread_ts,
            agent="kirocrew",
            source="slack",
            tool_name="options_submit",
            tool_kind="interaction",
            outcome="denied",
            metadata={"user_id": user_id, "reason": "not_allowed_user"},
        )
        return

    # One rule, checked before any work: does this control still belong to the
    # question the conversation is actually on? Judged from the token in the
    # message plus the transcript on disk, so it holds across a restart -- and it
    # is the ONLY thing standing between a superseded button and a dispatched
    # answer, since nothing retires controls ahead of time any more.
    if await options_control_is_stale(
        _orch.dashboard_state if _orch else None, _options_block_id(payload), thread_ts
    ):
        sel().log_tool_invocation(
            session_key=thread_ts,
            agent="kirocrew",
            source="slack",
            tool_name="options_submit",
            tool_kind="interaction",
            outcome="denied",
            metadata={"reason": "superseded_control", "channel": channel},
        )
        await _refuse_stale_options(channel, msg_ts, payload)
        return

    state_values = payload.get("state", {}).get("values", {})
    selected: list[str] = []
    for block_vals in state_values.values():
        cb_state = block_vals.get(OPTIONS_CHECKBOXES_ACTION)
        if cb_state:
            selected = [o["value"] for o in cb_state.get("selected_options", [])]
            break

    if not selected:
        sel().log_tool_invocation(
            session_key=thread_ts,
            agent="kirocrew",
            source="slack",
            tool_name="options_submit",
            tool_kind="interaction",
            outcome="skipped",
            metadata={"reason": "empty_selection"},
        )
        return  # nothing checked, ignore

    # Extract all choices for the styled summary
    blocks = payload.get("message", {}).get("blocks", [])
    all_choices: list[str] = []
    for b in blocks:
        if b.get("type") != "actions":
            continue
        for el in b.get("elements", []):
            if el.get("action_id") == OPTIONS_CHECKBOXES_ACTION:
                all_choices = [o["value"] for o in el.get("options", [])]
                break

    # Compute indices BEFORE redaction — deduplicate to handle identical choices
    selected_set = set(selected)
    selected_indices: list[int] = []
    seen: set[str] = set()
    for i, c in enumerate(all_choices):
        if c in selected_set and c not in seen:
            selected_indices.append(i)
            seen.add(c)

    # Redact
    selected = [redact_credentials(redact_exfiltration_urls(s)[0])[0] for s in selected]
    all_choices = [redact_credentials(redact_exfiltration_urls(c)[0])[0] for c in all_choices]

    combined = ", ".join(selected)
    # The Slack-facing FALLBACK TEXT, escaped. Slack parses entities in a
    # message's top-level `text` too, not just in mrkdwn blocks -- and `text` is
    # what notifications and block-less clients render, so an unescaped
    # `<!channel>` here pages a whole channel even though the blocks are safe.
    #
    # A SEPARATE variable on purpose: `combined` itself must stay raw, because it
    # is also the answer echoed back into the session below. Escaping in place
    # would change what the user actually picked -- the same trap that keeps the
    # escape out of `_redact_choices`.
    combined_fallback = escape_mrkdwn(combined)

    # Edit-in-place: replace only the OPTIONS actions block(s) with the
    # styled selection, preserving every other surrounding block. Falls back
    # to post-and-delete if update_message raises (resilience).
    selected_blocks = build_options_selected_blocks(all_choices, selected_indices)
    parent_blocks = payload.get("message", {}).get("blocks", [])
    new_blocks = replace_options_blocks(parent_blocks, selected_blocks)
    new_ts = msg_ts
    edited = False
    # Set when the original message survives our attempt to remove it: the
    # control is then STILL on screen and still clickable, so its record has to
    # outlive this submit for a later turn to expire it.
    original_still_live = False
    # Serialize against a concurrent expiry of this SAME message. The expiry
    # re-reads the record inside this lock and skips its edit once the forget
    # below has dropped it, so the user's selection cannot be overwritten by an
    # expiry that started first. Held across the edit AND the forget: releasing
    # between them would let an expiry observe a still-tracked record and edit
    # over the selection we just wrote.
    async with options_edit_lock(channel, msg_ts):
        # One answer per control. A second Send click on this message would
        # otherwise render the selection again and dispatch a second turn -- a
        # duplicate, or once the first turn has moved on, a superseded one.
        # Claimed under the lock so the check and the claim cannot interleave,
        # and BEFORE the edit so a loser touches nothing at all.
        if not claim_options_answer(channel, msg_ts):
            logger.debug(
                "options_submit: control %s/%s was already answered; dropping the "
                "duplicate click",
                channel,
                msg_ts,
            )
            return
        # Whose record this control is, captured BEFORE the edit. A relink landing
        # during the edit would move the thread to another session, so resolving
        # after the fact names the NEW owner and leaves the previous owner's record
        # in place -- and that session's next turn would edit over this selection.
        # Pin the conversation that ASKED, read from the control's own token --
        # NOT whoever owns the thread now. The two diverge in exactly the case
        # the pin exists to survive: after a handover, resolving from the thread
        # names the new owner and would deliver this answer to a conversation
        # that never asked the question.
        #
        # A pinned None is meaningful: it says the asker holds no slot (a native
        # Slack or cron conversation), so a thread linked after acceptance cannot
        # capture the answer either. An untokened control -- one posted before
        # this shipped -- pins nothing and keeps today's resolution, matching the
        # rest of the rule, which honours what it cannot judge.
        _asker = decode_options_token(_options_block_id(payload))
        _asker_key = _asker[0] if _asker else None
        _pinned_slot = (
            slack_options_slot(_orch.dashboard_state, _asker_key)
            if (_asker_key and _orch and _orch.dashboard_state)
            else None
        )
        _pinned_slot_name = getattr(_pinned_slot, "key", None)
        _route_pinned = _asker_key is not None
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
                    "update_message failed for options_submit, falling back to post+delete",
                    exc_info=True,
                )

            if not edited:
                posted_ts = await _orch.slack.post_blocks(
                    channel, selected_blocks, combined_fallback, thread_ts
                )
                if not posted_ts:
                    logger.warning("Failed to post options choice — aborting")
                    sel().log_tool_invocation(
                        session_key=thread_ts,
                        agent="kirocrew",
                        source="slack",
                        tool_name="options_submit",
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
                        "and the new selection message",
                        exc_info=True,
                    )
        finally:
            # Give the claim back only when the selection never reached Slack at
            # all -- neither the in-place edit nor the replacement post. Holding it
            # then would refuse every retry forever, leaving a control permanently
            # visible and permanently unanswerable. Once the selection IS on screen
            # the claim stays, even if a later step stumbles: the answer is
            # rendered and the turn is on its way.
            if not edited and new_ts == msg_ts:
                release_options_answer(channel, msg_ts)

        # The control is spent only once the original is actually gone — either
        # rewritten in place, or posted-and-deleted. When the delete failed those
        # buttons are still sitting in the channel, so the record has to stay:
        # dropping it is exactly what leaves a permanently clickable control.
        #
        # Inside the lock with the edit above: an expiry that observed a
        # still-tracked record between the two would edit straight over the
        # selection we just wrote.
        if not original_still_live:
            # The buttons are provably off screen, so the claim on this control no
            # longer has to be pinned against a late click and may be evicted if
            # the map fills. While the original IS still live the claim stays put.
            settle_options_answer(channel, msg_ts)
            _forget_options_control(thread_ts, msg_ts, keys=_owner_keys)

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
        tool_name="options_submit",
        tool_kind="interaction",
        outcome="success",
        metadata={"selected": combined, "channel": channel},
    )


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

    # ── Standard OPTIONS choice: delete message, post value, new session ──

    # Determine which button was clicked
    try:
        selected_index = int(action_id.replace(OPTIONS_ACTION_PREFIX, ""))
    except (ValueError, TypeError):
        selected_index = 0

    # Extract all choices from the original message
    blocks = payload.get("message", {}).get("blocks", [])
    all_choices = [
        el.get("value", "")
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
        if el.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
    ]

    # The same rule the multi-select path applies, on the same token. A control
    # posted before this build carries no token and is honoured, so an upgrade
    # does not strand buttons that are still legitimately answerable.
    if await options_control_is_stale(
        _orch.dashboard_state if _orch else None, _options_block_id(payload, action), thread_ts
    ):
        await _refuse_stale_options(channel, msg_ts, payload)
        return

    # Redact LLM-generated content before any external use
    choice, _ = redact_exfiltration_urls(choice)
    choice, _ = redact_credentials(choice)
    all_choices = [redact_credentials(redact_exfiltration_urls(c)[0])[0] for c in all_choices]

    # Edit-in-place: replace only the OPTIONS actions block with the styled
    # selection, preserving every other surrounding block. Falls back to
    # post-and-delete if update_message raises.
    # Every guarantee the multi-select submit path has, this path needs too: it
    # renders a selection into the SAME message and dispatches a turn, so two
    # rapid clicks on a legacy single-click control would otherwise produce two
    # dispatches and two turns. The lock serialises against the turn-start
    # expiry's edit; the claim makes the answer once-only.
    async with options_edit_lock(channel, msg_ts):
        if not claim_options_answer(channel, msg_ts):
            logger.debug(
                "options click: control %s/%s was already answered; dropping the " "duplicate",
                channel,
                msg_ts,
            )
            return
        # Owner keys BEFORE the edit -- a relink landing during it would move the
        # thread, and forgetting against the new owner orphans the old record.
        # Pin the conversation that ASKED, read from the control's own token --
        # NOT whoever owns the thread now. The two diverge in exactly the case
        # the pin exists to survive: after a handover, resolving from the thread
        # names the new owner and would deliver this answer to a conversation
        # that never asked the question.
        #
        # A pinned None is meaningful: it says the asker holds no slot (a native
        # Slack or cron conversation), so a thread linked after acceptance cannot
        # capture the answer either. An untokened control -- one posted before
        # this shipped -- pins nothing and keeps today's resolution, matching the
        # rest of the rule, which honours what it cannot judge.
        _asker = decode_options_token(_options_block_id(payload, action))
        _asker_key = _asker[0] if _asker else None
        _pinned_slot = (
            slack_options_slot(_orch.dashboard_state, _asker_key)
            if (_asker_key and _orch and _orch.dashboard_state)
            else None
        )
        _pinned_slot_name = getattr(_pinned_slot, "key", None)
        _route_pinned = _asker_key is not None
        _owner_keys = slack_options_owner_keys_snapshot(
            _orch.dashboard_state if _orch else None, thread_ts
        )
        edited = False
        new_ts = msg_ts
        try:
            selected_blocks = build_options_selected_blocks(all_choices, selected_index)
            new_blocks = replace_options_blocks(blocks, selected_blocks)
            new_ts = msg_ts
            edited = False
            # See the multi-select submit path: set when the original message outlives
            # our attempt to remove it, so its still-clickable control keeps a record.
            original_still_live = False
            # Escaped for the FALLBACK text only. Slack parses entities in a
            # message's top-level `text` -- which is what notifications render --
            # so a legacy choice containing `<!channel>` would ping the whole
            # channel from a click. The blocks are already escaped by
            # `build_options_selected_blocks`; `choice` itself stays RAW below,
            # because that is the answer echoed into the session.
            _choice_fallback = escape_mrkdwn(choice)
            try:
                await _orch.slack.update_message(
                    channel, msg_ts, text=_choice_fallback, blocks=new_blocks
                )
                edited = True
            except Exception:
                logger.debug(
                    "update_message failed for options choice, falling back to post+delete",
                    exc_info=True,
                )

            if not edited:
                posted_ts = await _orch.slack.post_blocks(
                    channel, selected_blocks, _choice_fallback, thread_ts
                )
                if not posted_ts:
                    logger.warning("Failed to post options choice — aborting")
                    sel().log_tool_invocation(
                        session_key=thread_ts,
                        agent="kirocrew",
                        source="slack",
                        tool_name="options",
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
                        "and the new selection message",
                        exc_info=True,
                    )

            # Same rule as the multi-select submit path: only forget the control once
            # the original message is genuinely gone. A failed delete leaves the buttons
            # live, and a forgotten record can never be expired.
            if not original_still_live:
                # Buttons provably gone: the claim may be reclaimed under pressure.
                settle_options_answer(channel, msg_ts)
                _forget_options_control(thread_ts, msg_ts, keys=_owner_keys)
        finally:
            # Only when the selection never reached Slack at all. Once it is on
            # screen the claim stays, or a duplicate click is re-admitted.
            if not edited and new_ts == msg_ts:
                release_options_answer(channel, msg_ts)

    t = asyncio.create_task(
        handle_message(
            _orch.slack,
            _orch.sessions,  # type: ignore[arg-type]
            channel,
            choice,
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
            target_slot_name=_pinned_slot_name,
            route_pinned=_route_pinned,
            asker_key=_asker_key,
            start_priority=StartPriority.FOREGROUND,
        )
    )
    _orch._handler_tasks.add(t)
    t.add_done_callback(_orch._handler_tasks.discard)
