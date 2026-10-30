"""A code channel's workers ask for approval in the channel.

A code channel's session is often a conductor: it creates worker sessions
(``session_create``), and those run with at most the channel's approval mode. On
Ask, a worker's tool prompt otherwise shows only in the dashboard, and the
person driving the channel from Slack is never told a worker is waiting.

So when a worker that the channel's OWN session created (``_created_by`` names
that session's slot, which :func:`.surface.binding_for_slot` resolves back to a
live, non-archived binding) raises a tool approval, the dashboard runner hands
it here and the card is posted top-level in the channel, headed with the
worker's title. Its arguments go through the same outbound redaction as every
other approval card; nothing from the worker's transcript is posted.

**The answer is the dashboard's.** Approve / Reject resolve the worker's own
approval future through ``state.resolve_slot_approval`` -- the dashboard's
slot-scoped resolver, pinned to the very future the card was posted for -- so
the worker's ``_run_chat`` answers the backend exactly once, as it does for a
click in the dashboard. Nothing is ever looked up by request id state-wide.

**Trust this channel** on a worker's card puts the CHANNEL's session on Trust
through :func:`.approval_picker.set_channel_mode` (the audited dashboard
mutation, with its warning line) and approves this one call. The change reaches
the workers the channel's session is already running too (raised and lowered
with it, see :func:`.approval_picker.set_channel_mode`); a raise to Trust answers
their waiting prompts and their cards say so in place.

**Who may answer.** The owner, under the gates every code-channel control
applies (:func:`.approval_picker.authorize`); anyone else is told so
ephemerally and nothing changes. Every decision writes one SEL row
(:data:`OPERATION`).

**Answered elsewhere.** When the approval is decided in the dashboard, times
out, or the turn ends under it, the runner calls :func:`finish` and the card is
updated in place to say so, with its buttons gone.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)

APPROVE_ACTION_ID = "kc_code_channel_worker_approve"
REJECT_ACTION_ID = "kc_code_channel_worker_reject"
TRUST_ACTION_ID = "kc_code_channel_worker_trust"
ACTION_IDS = frozenset({APPROVE_ACTION_ID, REJECT_ACTION_ID, TRUST_ACTION_ID})

AUDIT_CALLER = "slack-code-channels"
OPERATION = "slack.code_channel.worker_approval"

HEADER = "🔐 *Worker: {title}* wants to run:"
FALLBACK = "Worker {title} is waiting for a tool approval"
NOT_LIVE_TEXT = "⛔ That approval isn't waiting any more, or isn't this channel's."
APPROVED_BY = "✅ Approved by <@{user}>."
REJECTED_BY = "❌ Rejected by <@{user}>."
TRUSTED_BY = "🤝 Approved, and this channel is now on Trust (<@{user}>)."
ANSWERED_ELSEWHERE = {
    "approved": "✅ Approved in the dashboard.",
    "rejected": "❌ Declined in the dashboard.",
    "timeout": "⌛ No answer in time, so it was declined.",
    "ended": "⏹ The worker stopped before anyone answered.",
}
_TITLE_LIMIT = 120


@dataclass
class WorkerCard:
    """One worker approval card posted in a code channel."""

    channel: str
    ts: str
    worker_key: str
    session_key: str
    request_id: str
    future: asyncio.Future[str]
    header: str
    answered: bool = False


#: Live cards by ``"<channel>:<ts>"``.
_cards: dict[str, WorkerCard] = {}


def reset() -> None:
    """Forget every card (tests)."""
    _cards.clear()


def _audit(outcome: str, resources: str, error: str = "") -> None:
    surface.audit(AUDIT_CALLER, OPERATION, outcome, resources, error)


def _safe(text: str) -> str:
    """Agent-authored text made safe for a Slack mrkdwn field."""
    from kiro_crew.security.exfil import redact_exfiltration_urls
    from kiro_crew.security.redaction import redact_credentials
    from kiro_crew.slack.format import escape_mrkdwn

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    text = " ".join(text.split())
    if len(text) > _TITLE_LIMIT:
        text = text[: _TITLE_LIMIT - 1] + "…"
    return escape_mrkdwn(text).replace("*", "∗")


def _worker_title(slot: Any) -> str:
    return _safe(str(getattr(slot, "title", "") or "") or str(getattr(slot, "key", "")))


def live_workers(state: Any, channel_slot: Any) -> list[Any]:
    """The live sessions *channel_slot* (a code channel's own session) created.

    The same lineage :func:`channel_of_worker` reads, from the channel's side:
    a slot whose ``_created_by`` names the channel's session and that is not
    itself linked to Slack. Nothing else: no other session's workers, no
    grandchildren.
    """
    key = str(getattr(channel_slot, "key", "") or "")
    if state is None or not key:
        return []
    return [
        s
        for s in list((getattr(state, "_slots", None) or {}).values())
        if s is not channel_slot
        and str(getattr(s, "_created_by", "") or "") == key
        and not getattr(s, "_slack_linked", False)
    ]


def claim_pending(worker_key: str) -> list[WorkerCard]:
    """Claim *worker_key*'s relayed cards still waiting, before a Trust sweep.

    A claimed card's own :func:`finish` leaves it alone, so the sweep's answer
    is not labelled "approved in the dashboard"; :func:`settle_claimed` writes it.
    """
    cards = [
        c
        for c in list(_cards.values())
        if c.worker_key == worker_key and not c.answered and not c.future.done()
    ]
    for card in cards:
        card.answered = True
    return cards


async def settle_claimed(
    cards: list[WorkerCard], *, granted: bool, line: str, skip: str = ""
) -> None:
    """Say how each claimed card's prompt ended, in place (never raises).

    *line* only for a prompt the grant approved; any other ending (rejected or
    approved elsewhere meanwhile, the turn ended) gets the line :func:`finish`
    would have written, since a claimed card's ``finish`` said nothing. A prompt
    still waiting is released to its own ``finish``. *skip* (``channel:ts``) is
    the card whose click asked: that click writes its text.
    """
    for card in cards:
        fut = card.future
        if (
            granted
            and f"{card.channel}:{card.ts}" == skip
            and fut.done()
            and not fut.cancelled()
            and str(fut.result()).startswith("approved")
        ):
            continue  # approved by this grant: the click that asked writes it
        if not fut.done():
            card.answered = False
            continue
        result = "" if fut.cancelled() else str(fut.result())
        if granted and result.startswith("approved"):
            text = line
        else:
            text = settled_line(result, deny_cause="", host_ended=fut.cancelled())
        await _update(card, text)


async def channel_of_worker(state: Any, slot: Any) -> Binding | None:
    """The code channel whose session created *slot*, or None.

    Only a direct child of the channel's CURRENT session counts, while the
    feature is on and the channel is not archived. A slot that is itself
    linked to Slack posts its own prompts and is never relayed.
    """
    if state is None or slot is None or not code_channels_on(live_config()):
        return None
    if getattr(slot, "_slack_linked", False):
        return None
    creator = str(getattr(slot, "_created_by", "") or "")
    if not creator:
        return None
    binding = await surface.binding_for_slot(state, creator)
    if binding is None or await surface.is_archived(binding):
        return None
    return binding


def _blocks(header: str, request_id: str, title: str, tool_input: str) -> list[dict[str, Any]]:
    """The approval card the channel's own session posts, re-headed and re-wired."""
    from kiro_crew.slack.handler import _build_approval_blocks, _LinkedApprovalEvent

    event = _LinkedApprovalEvent(request_id, title, tool_input)
    blocks = _build_approval_blocks(event, is_dm=False, channel_trust=True)  # type: ignore[arg-type]
    remap = {
        "approve_tool": APPROVE_ACTION_ID,
        "reject_tool": REJECT_ACTION_ID,
        "kc_code_channel_trust_call": TRUST_ACTION_ID,
    }
    for block in blocks:
        for element in block.get("elements", []) if block.get("type") == "actions" else []:
            element["action_id"] = remap.get(element.get("action_id", ""), "")
            element["value"] = request_id
    head = {"type": "section", "text": {"type": "mrkdwn", "text": header}}
    first = blocks[0] if blocks else {}
    if "Tool approval requested" in str((first.get("text") or {}).get("text", "")):
        blocks[0] = head
    else:
        blocks.insert(0, head)
    return blocks


def start(
    state: Any,
    slot: Any,
    session_key: str,
    request_id: Any,
    title: str,
    tool_input: str,
    future: asyncio.Future[str],
    *,
    perm_meta: Any,
    linked_ts: str | None,
) -> asyncio.Task[WorkerCard | None] | None:
    """Start relaying *slot*'s prompt; the task for :func:`finish`, or None.

    Synchronous on purpose: ``_run_chat`` calls it between registering the
    approval future and entering the ``try`` whose ``finally`` unregisters it,
    where an ``await`` could be cancelled and strand the future. The post runs
    as a task, which :func:`finish` (from that ``finally``) awaits or cancels.

    Nothing starts for a prompt born resolved (``"resolved"`` in *perm_meta*), one
    already mirrored to a linked Slack thread (*linked_ts*), or a slot no code
    channel's session created.
    """
    if linked_ts is not None or "resolved" in (perm_meta or {}):
        return None
    if getattr(slot, "_slack_linked", False) or not getattr(slot, "_created_by", ""):
        return None
    return asyncio.ensure_future(
        relay(state, slot, session_key, request_id, title, tool_input, future)
    )


async def relay(
    state: Any,
    slot: Any,
    session_key: str,
    request_id: Any,
    title: str,
    tool_input: str,
    future: asyncio.Future[str],
) -> WorkerCard | None:
    """Post *slot*'s approval prompt in its code channel; None when not relayed.

    Never raises: a relay that fails leaves the prompt where it always was, in
    the dashboard, and the worker's wait is unchanged.
    """
    try:
        binding = await channel_of_worker(state, slot)
        slack = surface.slack()
        if binding is None or slack is None or not await surface.outbound_permitted():
            return None
        worker_title = _worker_title(slot)
        header = HEADER.format(title=worker_title)
        rid = str(request_id)
        ts = await slack.post_blocks(
            binding.channel_id,
            _blocks(header, rid, title, tool_input),
            FALLBACK.format(title=worker_title),
            None,
        )
        if not ts:
            return None
        card = WorkerCard(binding.channel_id, str(ts), slot.key, session_key, rid, future, header)
        _cards[f"{card.channel}:{card.ts}"] = card
        _audit(
            "allowed",
            f"channel={card.channel} worker={slot.key} request={rid} message={card.ts} result=posted",
        )
        return card
    except Exception:
        logger.info("code channel worker approval not relayed", exc_info=True)
        return None


def _settled(header: str, line: str) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": line}]},
    ]


async def _update(card: WorkerCard, line: str) -> None:
    slack = surface.slack()
    if slack is None:
        return
    try:
        await slack.update_message(
            card.channel, card.ts, text=line, blocks=_settled(card.header, line)
        )
    except Exception:
        logger.debug("worker approval card not updated", exc_info=True)


def settled_line(outcome: str, *, deny_cause: str, host_ended: bool) -> str:
    """What a card decided outside Slack says, from ``_run_chat``'s own attribution.

    A host decline (*deny_cause*: the window ran out, no budget left, ...) or a
    turn the host ended under the prompt (*host_ended*: cancelled, stopped) is
    never reported as a person's answer in the dashboard.
    """
    from kiro_crew.dashboard.state import DENY_CAUSE_APPROVAL_TIMEOUT

    if deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT:
        return ANSWERED_ELSEWHERE["timeout"]
    if deny_cause or host_ended:
        return ANSWERED_ELSEWHERE["ended"]
    if str(outcome).startswith("approved"):
        return ANSWERED_ELSEWHERE["approved"]
    return ANSWERED_ELSEWHERE["rejected"]


async def finish(
    relay_task: asyncio.Task[WorkerCard | None] | None,
    outcome: str,
    *,
    deny_cause: str,
    host_ended: bool,
) -> None:
    """The worker's prompt is decided: retire its card and, unless a click here
    already said who answered, update it in place to say how. Never raises."""
    if relay_task is None:
        return
    try:
        # Shielded: cancelling THIS turn must not read as the relay's own
        # cancellation (an unshielded await cancels the awaited task with it).
        card = await asyncio.shield(relay_task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if relay_task.cancelled() and not (current is not None and current.cancelling()):
            return  # the relay alone was cancelled: nothing posted to retire
        # This turn is being cancelled: stop the relay so no card is posted
        # late, and never swallow the turn's own cancellation.
        relay_task.cancel()
        raise
    except Exception:
        return
    if card is None:
        return
    try:
        _cards.pop(f"{card.channel}:{card.ts}", None)
        if card.answered:
            return
        await _update(card, settled_line(outcome, deny_cause=deny_cause, host_ended=host_ended))
    except Exception:
        logger.debug("worker approval card not retired", exc_info=True)


async def handle_click(orch: Any, payload: dict[str, Any]) -> bool:
    """Approve / Reject / Trust this channel on a worker's card; False when the gate is shut."""
    if not code_channels_on(live_config(orch)):
        return False
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.slack.code_channels.approval_picker import (
        _ephemeral,
        _ids,
        authorize,
        set_channel_mode,
    )

    channel, user, msg_ts, team, action = _ids(payload)
    action_id = str(action.get("action_id", ""))
    resources = f"channel={channel} user={user} action={action_id} message={msg_ts}"
    card = _cards.get(f"{channel}:{msg_ts}")
    binding = await asyncio.to_thread(store().get_binding, channel) if channel else None
    state = surface.dashboard_state()
    worker = state.get_slot(card.worker_key) if card is not None and state is not None else None
    live_binding = await channel_of_worker(state, worker) if worker is not None else None
    if (
        card is None
        or binding is None
        or state is None
        or worker is None
        or live_binding is None
        or live_binding.channel_id != channel
        or effective_session_key(worker) != card.session_key
        or card.future.done()
    ):
        _audit("denied", resources, "not a live worker approval card here")
        await _ephemeral(channel, user, NOT_LIVE_TEXT)
        return True
    resources += f" worker={card.worker_key} request={card.request_id}"
    refusal = await authorize(orch, binding, user, team, resources)
    if refusal is not None:
        await _ephemeral(channel, user, refusal)
        return True
    line = APPROVED_BY if action_id == APPROVE_ACTION_ID else REJECTED_BY
    if action_id == TRUST_ACTION_ID:

        def _live() -> bool:
            # Still this channel's worker, still waiting on this very prompt.
            from kiro_crew.slack.code_channels.binding import bound_slot

            channel_slot = bound_slot(state, binding)
            return (
                not card.future.done()
                and worker._approval_futures.get(card.request_id) is card.future
                and channel_slot is not None
                and getattr(worker, "_created_by", "") == getattr(channel_slot, "key", None)
            )

        outcome = await set_channel_mode(
            binding,
            "trust",
            user,
            via="worker_card",
            still_live=_live,
            origin_card=f"{channel}:{msg_ts}",
        )
        if outcome.result not in ("set", "unchanged"):
            await _ephemeral(channel, user, outcome.text)
            return True
        line = TRUSTED_BY
    approved = action_id != REJECT_ACTION_ID
    # Trust this channel also puts the worker on Trust, whose sweep may already
    # have approved this very call: that is this click's answer.
    resolved = (
        action_id == TRUST_ACTION_ID
        and card.future.done()
        and not card.future.cancelled()
        and str(card.future.result()).startswith("approved")
    )
    try:
        if not resolved:
            resolved = bool(
                state.resolve_slot_approval(
                    worker, card.request_id, approved, expected_future=card.future
                )
            )
    except Exception:
        logger.warning("code channel %s: worker approval not resolved", channel, exc_info=True)
    if not resolved:
        _audit("denied", resources, "already answered")
        await _ephemeral(channel, user, NOT_LIVE_TEXT)
        return True
    card.answered = True
    _audit("allowed", f"{resources} result={'approved' if approved else 'rejected'}")
    await _update(card, line.format(user=user))
    return True


async def dispatch_card_click(orch: Any, payload: dict[str, Any], action_id: str) -> bool:
    """Route a Slack Code approval control (a card's buttons, the kickoff's Approval
    mode select); True when it was one (handled or dropped).

    Called by ``interactions.dispatch`` after its owner check. The ``channels``
    inbound ceiling applies first, as it does to every other control there.
    """
    from kiro_crew.slack.code_channels import approval_picker

    picker_ids = (approval_picker.TRUST_CALL_ACTION_ID, approval_picker.APPROVAL_SELECT_ACTION_ID)
    if action_id not in ACTION_IDS and action_id not in picker_ids:
        return False
    from kiro_crew.messaging.identity import channel_inbound_permitted

    if not await channel_inbound_permitted("slack"):
        logger.info("slack code channel approval control dropped: denied by channels policy")
        return True
    if action_id in ACTION_IDS:
        await handle_click(orch, payload)
    elif action_id == approval_picker.APPROVAL_SELECT_ACTION_ID:
        await approval_picker.handle_select(orch, payload)
    else:
        await approval_picker.handle_trust_call(orch, payload)
    return True
