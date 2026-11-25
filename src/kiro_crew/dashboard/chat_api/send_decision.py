"""The send path's ``message.steer`` decision: whether a busy slot's send asks Jev
to choose between steering and queueing, and the choice it gets back.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import STEER_AUTO, effective_session_key, logger


def steer_is_auto(value: object) -> bool:
    """Whether a send's ``steer`` flag asks Jev to choose the path.

    Only the exact string, case- and space-insensitively. A boolean ``True`` is a
    MANUAL steer and must never read as auto: that flag is what every existing
    client sends, and reading it as a request to decide would put an oracle on a
    path the sender already answered.
    """
    return isinstance(value, str) and value.strip().lower() == STEER_AUTO


async def decided_message_handling(slot: Any, message: str) -> tuple[bool, dict | None]:
    """Ask ``message.steer`` whether *message* queues instead of steering.

    Returns ``(queues, record)``: whether to take the QUEUE path, and the decision
    row to stamp on the persisted user row (``None`` when nothing was decided, or
    when the row itself was refused).

    A BOOLEAN rather than the point's choice string, so the caller holds no copy of
    that vocabulary and cannot drift from it: every refusal -- the seam off, the
    session unsampled, a timeout, an answer outside the two options, a failed
    import -- is ``False``, which is the steer path a manual Steer and the
    composer's default have always taken.

    Called for a send the dashboard's own human made while a turn is running, and
    nowhere else (the busy branch's ``_person_send`` conjunct): an app token, an
    integration or a cron has nobody watching the reply, and every such send
    already falls through to the fail-closed queue; an agent-origin send takes the
    manual steer path as a non-person steer. Consent and sampling are the
    seam's own gates, re-checked inside ``decide``.

    Never raises except cancellation. The seam may cost an observation and must
    never cost a send.
    """
    try:
        # Imported HERE, not at module scope: this module is on the gateway's boot
        # path and the decisions package is optional, off by default, and pulls the
        # config loader in behind it.
        from kiro_crew.decisions.points import message_steer

        session_key = effective_session_key(slot)
        decided = await message_steer.steer_or_queue(
            message,
            session_key=session_key,
            # The live list, sliced and read off the loop by the point; nothing is
            # written back.
            rows=getattr(slot, "messages", ()) or (),
        )
        if decided is None:
            return False, None
        # The row is written BEFORE the path is taken, and the receipt is stamped
        # only when it landed: the strip's thumbs POST this turn id, so a receipt
        # from a row `append` refused would invite a verdict about a decision the
        # log does not hold. Off the loop -- the append locks a file.
        record = await asyncio.to_thread(message_steer.record_outcome, session_key, decided)
        return decided.get("choice") == message_steer.CHOICE_QUEUE, record
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("message.steer: taking the shipped default", exc_info=True)
        return False, None
