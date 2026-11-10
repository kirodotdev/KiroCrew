"""The audit an inbound bridge action writes (RFC notification-bridge-actions, B4b).

Still inert: nothing calls it until a transport reader lands. Every attempt, admitted
or refused, writes two records:

* a crew-log ``approval/bridged`` entry beside the decision's own approval entries,
  when the token names a live binding that knows its crew-log session. A token that
  names nothing (forged, stale, or after a restart) has no session to file under, so
  it gets the SEL record alone;
* an SEL ``log_api_access`` record, attributed to the bound session, so a bridged answer is as
  auditable as a dashboard one and a refused attempt is visible to a reviewer.

An admitted action adds no ``approval/decided`` of its own: it resolved the decision
future, and the waiter on that future writes the same entry it writes for a
dashboard click. Neither write may raise into the action path.
"""

from __future__ import annotations

import json
import logging

from kiro_crew.notifications.bridge_actions import ActionOutcome, BridgeActionRegistry

logger = logging.getLogger(__name__)

#: The SEL operation an inbound bridge action is recorded under, per transport.
SEL_OPERATION = "notification_bridge.action"

#: How much of a chat-supplied answer the SEL record keeps.
_MAX_CHOICE_CHARS = 32

#: The crew-log outcome an admitted answer records, in the RFC's words.
_ADMITTED_AS = {"approve": "approved", "deny": "denied"}


def crew_log_outcome(outcome: ActionOutcome, choice: str) -> str:
    """The RFC's outcome word for one action: approved, denied, expired or refused."""
    if outcome is ActionOutcome.ADMITTED:
        return _ADMITTED_AS.get(choice, "refused")
    if outcome is ActionOutcome.EXPIRED:
        return "expired"
    return "refused"


def audit_action(
    registry: BridgeActionRegistry,
    token: str,
    outcome: ActionOutcome,
    *,
    transport: str,
    choice: str = "",
    actor: str = "",
    channel: str = "",
) -> None:
    """Write the crew-log and SEL records for one action on *token*. Never raises.

    *choice* is the answer a refused action gave; an admitted one is recorded with
    the answer the registry admitted. *actor* is the transport identity that took
    the action, whether or not it was the owner, so a refused stranger is on the
    record beside the bound ``owner``. The crew-log entry records the RFC's outcome
    word, with the precise :class:`ActionOutcome` as its ``reason`` when the action
    was not admitted.
    """
    context = registry.context_for(token)
    approval_id = context.approval_id if context is not None else ""
    owner = context.owner if context is not None else ""
    if outcome is ActionOutcome.ADMITTED and context is not None:
        # The registry's record of what resolved the decision, not the caller's word.
        choice = context.answered
    if context is not None and context.crew_log_session:
        try:
            # Lazy: the crew-log facade pulls in its whole writer, which the
            # notifications package does not otherwise load.
            from kiro_crew.crew_log import emit as crew_log_emit

            crew_log_emit.on_approval_bridged(
                context.crew_log_session,
                turn=context.turn,
                approval_id=approval_id,
                transport=transport,
                outcome=crew_log_outcome(outcome, choice),
                reason="" if outcome is ActionOutcome.ADMITTED else outcome.value,
                channel=channel,
                actor=actor,
                owner=owner,
            )
        except Exception:
            logger.debug("bridge action: crew-log write failed", exc_info=True)
    try:
        # Lazy, like bridge_egress: governance pulls in the platform, and tests patch
        # kiro_crew.sel.sel at its source module.
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
        from kiro_crew.sel import sel

        caller = context.session_key if context is not None else HOST_SESSION_KEY
        details = {
            "transport": transport,
            "approval_id": approval_id,
            # Chat-supplied, so bounded like the crew log's id fields.
            "choice": choice[:_MAX_CHOICE_CHARS],
            "channel": channel,
            "actor": actor,
            "owner": owner,
        }
        sel().log_api_access(
            caller=caller or HOST_SESSION_KEY,
            operation=f"{SEL_OPERATION}:{transport}",
            outcome=outcome.value,
            source=transport,
            # JSON, so a value carrying spaces or "key=" cannot pose as another field.
            resources=json.dumps({k: v for k, v in details.items() if v}, sort_keys=True),
        )
    except Exception:
        logger.debug("bridge action: SEL write failed", exc_info=True)
