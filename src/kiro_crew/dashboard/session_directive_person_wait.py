"""``monitor_update``'s ``waiting_on_person``: a legacy loop declares it waits on a person.

The applier half of :mod:`kiro_crew.autonudge_service.person_wait`. It takes the field
off the patch, lets every other field go through the ordinary ``monitor_update`` path
first, and then sets or ends the hold through ``AutoNudgeService.declare_person_wait``.

Who may set it, and where:

* **A legacy loop only.** A structured monitor wakes only when its subject needs its
  owner, so it has no cycles to save; it is refused.
* **A running loop only.** A paused or stopped loop fires nothing already, and this
  field is not a way to revive one.
* **``true`` from the session's own turn**: a turn a person sent, or the loop's own
  live wake (``self_arm_ok``). A cron injection, an app-driven turn or a sub-agent
  sharing the slot is refused, because the hold silences the loop until a person
  acts, and only the session's own work may decide it has asked one something.
* **``true`` in an unlinked dashboard chat only.** A reply in a Slack or Discord
  thread does not reach the loop's release, so a channel-bound loop or a dashboard
  slot mirrored to any channel would hold until someone opened the dashboard; it is
  refused.
* **``false``** from any producer that may revise the loop. It ends only a declared
  hold: a hold the work ledger found is the ledger's, and the reply says so.

The race it closes: a person can answer while the turn that asks them is still
running. Their message runs the release before the hold exists, so the hold would
then wait for a second message. The applier reads two facts instead: a human message
recorded during the producing turn (the slot's turn generation at the person's last
send, kept by ``responsive_chat.notify_user_input``), and a human message still
queued for the next turn. Either one means the person is already back. The applier
checks before the declaration and after its write lands, releasing a hold if the
person answers while that write is awaited.
"""

from __future__ import annotations

import asyncio
from typing import Any


def person_already_back(slot: Any) -> bool:
    """Whether a person sent a message in *slot* during this turn, or has one queued."""
    from kiro_crew.dashboard.chat_delivery import queue_entry_is_user_origin
    from kiro_crew.dashboard.responsive_chat import turn_state

    if slot is None:
        return False
    generation = getattr(slot, "_turn_generation", None)
    marked = turn_state(slot).human_input_generation
    if generation is not None and marked == generation:
        return True
    return any(
        queue_entry_is_user_origin(entry) for entry in list(getattr(slot, "_queue", None) or ())
    )


async def monitor_update(
    state: Any,
    slot: Any,
    session_key: str,
    args: dict[str, Any],
    *,
    self_arm_ok: bool = False,
    allow_rearm: bool = True,
    producer_is_channel: bool = False,
) -> str:
    """Apply a ``monitor_update`` directive, handling ``waiting_on_person`` here.

    A patch without the field goes to ``_monitor_update`` unchanged. With it, the
    other fields are applied through that same path first, so a combined update
    either lands whole or is refused before the hold is touched.
    """
    from kiro_crew.autonudge import get_instance, is_channel_key, is_structured_monitor_loop
    from kiro_crew.autonudge_authz import (
        _EXTERNAL_ARM_REFUSED_MODES,
        external_arm_refusal,
        is_self_arm,
    )
    from kiro_crew.dashboard import session_directive_apply as apply

    patch = dict(args.get("patch") or {})
    if "waiting_on_person" not in patch:
        return await apply._monitor_update(
            state,
            session_key,
            args,
            self_arm_ok=self_arm_ok,
            allow_rearm=allow_rearm,
            producer_is_channel=producer_is_channel,
        )
    wait = bool(patch.pop("waiting_on_person"))
    svc = get_instance()
    if svc is None:
        raise apply._DirectiveDenied(
            "Cannot update monitor loop: auto-nudge is disabled on this host."
        )
    binding = apply._binding(session_key)
    if not binding:
        raise apply._DirectiveDenied("monitor_update is not supported from this session type.")
    loop = svc.get_by_slot(binding)
    if not loop:
        raise apply._DirectiveDenied("No active monitor loop on this session to update.")
    authorized_loop_id = loop.id
    if is_structured_monitor_loop(loop):
        raise apply._DirectiveDenied(
            "monitor_update: waiting_on_person applies only to a monitor_start loop; a "
            "structured monitor already wakes only when its subject needs you."
        )
    if wait and not self_arm_ok:
        raise apply._DirectiveDenied(
            "monitor_update: waiting_on_person=true needs a turn a person sent in this "
            "session, or this loop's own wake."
        )
    if wait:
        from kiro_crew.dashboard.session_control_parts.delivery_containment import (
            _has_channel_mirror,
        )

        if (
            is_channel_key(binding)
            or producer_is_channel
            or await asyncio.to_thread(_has_channel_mirror, state, slot)
        ):
            # A Slack thread or channel-neutral mirror linked to a dashboard slot
            # runs on the dashboard key, so the binding and producer alone do not
            # say where the person will answer.
            raise apply._DirectiveDenied(
                "monitor_update: waiting_on_person=true works only in a dashboard chat; a "
                "reply in a Slack or Discord thread does not resume the loop."
            )
        current_loop = svc.get_by_slot(binding)
        if current_loop is None or current_loop.id != authorized_loop_id:
            raise apply._DirectiveDenied(
                "monitor_update: the active loop changed while its chat-link status was "
                "checked, so no person-wait hold was recorded."
            )
        loop = current_loop
    if not self_arm_ok and not is_channel_key(binding):
        current = (getattr(state, "_slots", None) or {}).get(binding)
        mode = str(getattr(current, "mode", ""))
        if mode in _EXTERNAL_ARM_REFUSED_MODES and not is_self_arm(binding, ""):
            raise apply._DirectiveDenied(
                f"Failed to update monitor loop: {external_arm_refusal(mode)}"
            )
    summary = ""
    if patch:
        summary = await apply._monitor_update(
            state,
            session_key,
            {**args, "patch": patch},
            self_arm_ok=self_arm_ok,
            allow_rearm=allow_rearm,
            producer_is_channel=producer_is_channel,
        )
        current_loop = svc.get_by_slot(binding)
        if current_loop is None or current_loop.id != authorized_loop_id:
            applied = f" The other fields were applied: {summary}" if summary else ""
            raise apply._DirectiveDenied(
                "monitor_update: the active loop changed before the person-wait hold "
                "was recorded." + applied
            )
        loop = current_loop
    if not loop.active:
        raise apply._DirectiveDenied(
            f"Monitor loop {loop.id} is PAUSED, so it fires nothing already; "
            "waiting_on_person applies only to a running loop."
        )
    not_held_note = (
        summary + f" Monitor loop {loop.id} was NOT held: a person already sent a "
        "message in this chat during this turn, so the loop keeps its schedule."
    ).strip()
    if wait and person_already_back(slot):
        return not_held_note
    if not wait and loop.waiting_on_person and not loop.person_wait_declared:
        # The work ledger found this hold, so only the ledger moving ends it.
        return (
            summary + f" Monitor loop {loop.id} still waits on a person: the hold is "
            "the work ledger's, not a declared one, and it lasts until the ledger "
            "moves."
        ).strip()
    if not await svc.declare_person_wait(binding, held=wait, expected_loop_id=authorized_loop_id):
        applied = f" The other fields were applied: {summary}" if summary else ""
        raise apply._DirectiveDenied(
            "monitor_update: the person-wait hold could not be recorded, so the loop "
            "keeps its schedule." + applied
        )
    if wait and loop.approval_stalled and not loop.person_wait_declared:
        return (
            summary + f" Monitor loop {loop.id} already waits on a person: a tool "
            "approval in this chat is unanswered, so it fires nothing until someone "
            "answers it, sends a message or presses Nudge now. Nothing more was recorded."
        ).strip()
    if wait and person_already_back(slot):
        from kiro_crew.autonudge_service.model import PERSON_WAIT_PERSON

        if not await svc.declare_person_wait(
            binding,
            held=False,
            release=PERSON_WAIT_PERSON,
            expected_loop_id=authorized_loop_id,
        ):
            raise apply._DirectiveDenied(
                "monitor_update: the person-wait hold was recorded but could not be "
                "released after the person answered."
            )
        return not_held_note
    if wait:
        note = (
            f"Monitor loop {loop.id} is waiting on a person: it fires nothing and spends "
            "no cycles until they reply in this chat, answer an approval or press Nudge "
            "now, or the wait expires -- after a day at most, and never past the loop's "
            "runtime budget, which an expired wait counts against."
        )
    else:
        note = f"Monitor loop {loop.id} is no longer waiting on a person."
    return (summary + " " + note).strip()
