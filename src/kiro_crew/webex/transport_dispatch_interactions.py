"""The answers a user gives to what a turn posted: a typed approval reply and a card press.

``_maybe_answer_approval`` reads a ``1`` / ``2`` reply as the answer to a pending tool
approval; ``_handle_card_press`` routes an Adaptive Card submit -- an approval or an
options choice -- once its sender clears the allow-list; and ``_resolve_card_approval``
applies an approval press through the nonce the registry minted. ``_APPROVALS`` is the
channel's process-global pending-approval registry, which ``handle_message`` also
reserves and discards around each turn, and ``_APPROVE_REPLIES`` / ``_DENY_REPLIES`` are
the exact typed answers a prompt accepts. Each function here whose first parameter is
``self`` is a ``WebexDispatcher`` method: the class in
:mod:`kiro_crew.webex.transport_dispatch` binds it by name, so it reads and writes the
dispatcher's own state on the instance, and a patch of the method on the class or on an
instance reaches every caller. Instance fields read (a call on one included):
``_choices``. Instance fields assigned, or whose mapping is changed: none. New answer
handling goes here.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from kiro_crew.messaging.approval import PendingApprovals
from kiro_crew.sel import sel
from kiro_crew.webex import cards
from kiro_crew.webex.cards import read_press
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport_dispatch_origin import _route_of

if TYPE_CHECKING:
    from kiro_crew.webex.transport_dispatch import WebexDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.webex.transport_dispatch")


# What to say when an answer arrives with no prompt waiting. Deliberately does
# NOT claim a denial: this is reached both when the window timed out (denied) and
# when the OTHER affordance already answered (possibly approved) — and the second
# is the common case, because Webex refuses to edit a message carrying an
# attachment, so a resolved card's buttons stay clickable forever. Telling a user
# their approved tool was denied is worse than telling them nothing changed.
_APPROVAL_NOT_PENDING = (
    "⌛ That prompt is already answered or timed out — no new decision was sent."
)

# The typed answers an approval prompt accepts: EXACTLY what the prompt asks for,
# and nothing else.
#
# The temptation is to be generous — "ok", "yes", "y", "sure" — and it is the
# wrong instinct in this one place, asymmetrically so. A too-narrow set costs the
# user a retry after an explicit "that is not 1 or 2". A too-wide one silently
# APPROVES A TOOL: "ok" is an extremely common thing to type into a chat, a
# pending prompt makes any message a candidate answer, and the user who typed it
# as conversation gets no signal that they just authorised a write. So the
# accepted words are the two digits the prompt names, plus the two verbs that
# cannot be mistaken for conversational filler.
_APPROVE_REPLIES = frozenset(("1", "approve"))
_DENY_REPLIES = frozenset(("2", "deny"))

#: Process-global approval registry for this channel. Module scope because a
#: pending prompt must outlive the dispatcher method that created it and be
#: resolvable from a later inbound frame, which is a different call stack.
_APPROVALS = PendingApprovals("webex")


async def _maybe_answer_approval(
    self: WebexDispatcher, inbound: "WebexInbound", session_key: str, *, permitted: bool
) -> bool:
    """Try to read *inbound* as the answer to a pending approval prompt.

    Returns whether the message was consumed. A message that is not a
    recognised answer falls through to the ordinary mid-turn path, so a user
    who ignores the prompt and sends a real instruction still gets it
    steered or queued rather than swallowed.

    A channels-governance deny blocks an APPROVE but still resolves a DENY.
    Dropping the deny too would leave the provider's permission request
    stranded for the whole approval window with the turn holding the
    semaphore — and a policy that forbids this channel has no interest in
    keeping a tool request alive.
    """
    answer = (inbound.text or "").strip().lower()
    approved = answer in _APPROVE_REPLIES
    denied = answer in _DENY_REPLIES
    if not approved and not denied:
        return False
    if approved and not permitted:
        logger.info("Webex: approve reply dropped by channels governance policy")
        return True
    resolved = _APPROVALS.resolve(self._approval_key(_route_of(inbound)), approved)
    sel().log_api_access(
        caller=f"webex:{inbound.person_email}",
        operation="webex.tool_approval",
        outcome="approved" if approved else "denied",
        source="webex",
        resources=f"session={session_key} resolved={resolved}",
    )
    if not resolved:
        await self._reply(inbound, _APPROVAL_NOT_PENDING)
        return True
    await self._reply(inbound, "✅ Approved." if approved else "🚫 Denied.")
    return True


async def _handle_card_press(
    self: WebexDispatcher, inbound: "WebexInbound", *, permitted: bool
) -> None:
    """Route an Adaptive Card submit to whatever it was rendered for.

    A press is authorized the same way a message is — by the sender's EMAIL
    against the allow-list — because the card lives in a room and Webex lets
    anyone in that room press it. The transport's room gate has already run;
    this adds the sender check, which for a press has to resolve a person id
    to an email first.

    The nonce is what makes a press safe to honour at all: Webex will not let
    a card carrying an attachment be edited into a terminal state, so a
    resolved prompt's buttons stay clickable forever. Only the live card's
    nonce is accepted, so a later press is inert instead of answering
    whatever prompt is open now. It is validated INSIDE the approval registry,
    as a precondition of resolving — checking it here, around the resolve call,
    would approve the tool first and only then discover the press was stale.
    """
    kind, choice, nonce, request_id = read_press(inbound.card_inputs)
    if not kind:
        return
    if not self._sender_allowed(inbound.person_email):
        sel().log_api_access(
            caller=inbound.person_email or "unknown",
            operation="webex.card_press",
            outcome="denied",
            source="webex",
            # Bounded and quoted: ``kind`` is attacker-chosen here (the press
            # came from a sender this branch is refusing), and an audit record
            # is read by an operator and by log tooling. An unbounded value can
            # pad the record, and an unescaped newline can forge a second
            # line in it.
            resources=f"kind={kind[:32]!r}",
        )
        return
    if kind == cards.KIND_APPROVAL:
        approved = choice == "approve"
        # Governance may block an approve while still letting a deny through:
        # a policy that forbids this channel has no interest in keeping a tool
        # request alive for its whole window.
        if approved and not permitted:
            logger.info("Webex: card approve dropped by channels governance policy")
            return
        await self._resolve_card_approval(inbound, approved, nonce, request_id)
        return
    if kind == cards.KIND_OPTIONS:
        # A choice is turn CONTENT, so a policy-denied channel drops it whole
        # rather than echoing it. Unlike an approval there is nothing waiting
        # to be released: `drive_turn` would silently drop the turn anyway, and
        # the echo alone would claim the channel is answering.
        if not permitted:
            logger.info("Webex: card choice dropped by channels governance policy")
            return
        text = self._choices.take(self._approval_key(_route_of(inbound)), choice, nonce)
        if not text:
            await self._reply(inbound, "⌛ Those options are no longer current — just reply.")
            return
        # Echo the choice first: a press leaves no trace in the room, so
        # without this the next answer arrives with nothing above it saying
        # which option it is answering.
        await self._reply(inbound, f"> {text}")
        # Run the chosen option as turn CONTENT, never as a command. The label
        # is model-authored, so an `[OPTIONS: Keep going | /yolo on]` trailer
        # would otherwise render a button whose single press takes the
        # process-global auto-approve grant — and more commonly, any label
        # whose first token merely looks like `/…` would be answered with the
        # unknown-command card and the choice dropped on the floor.
        await self.handle_message(
            replace(inbound, text=text, card_inputs=None), interpret_commands=False
        )


async def _resolve_card_approval(
    self: WebexDispatcher, inbound: "WebexInbound", approved: bool, nonce: str, request_id: str
) -> None:
    """Apply one authorized approval-card press, and say what happened.

    Every path here emits a SEL record. A press that is refused is exactly the
    event an operator needs in the audit log — a forged or replayed press
    leaves no other trace, since the reply it draws is indistinguishable from
    the one a genuinely expired card draws.
    """
    session_key = self._session_key(_route_of(inbound))
    approval_scope = self._approval_key(_route_of(inbound))
    # Fail CLOSED on a press that carries no nonce or no request id. Both are
    # minted by us and every real press echoes them back, so their absence is
    # either a forgery or a card from a build that predates them; an empty
    # nonce means "typed answer" to the registry and would skip the guard.
    if not nonce or not request_id:
        outcome, resolved = "denied", False
    else:
        resolved = _APPROVALS.resolve(
            approval_scope, approved, request_id=request_id, expected_nonce=nonce
        )
        # Only a resolve that actually landed can record an approval: a press
        # the registry refused (stale nonce, wrong request id) decided nothing.
        outcome = "approved" if resolved and approved else "denied"
    sel().log_api_access(
        caller=f"webex:{inbound.person_email}",
        operation="webex.tool_approval",
        outcome=outcome,
        source="webex",
        resources=f"session={session_key} via=card resolved={resolved}",
    )
    if resolved:
        await self._reply(inbound, "✅ Approved." if approved else "🚫 Denied.")
        return
    # A prompt still waiting means the CARD went stale, not the decision — so
    # point at the affordance that still works instead of claiming the request
    # is over, which would leave the turn hanging with no way to answer it.
    if _APPROVALS.has_pending(approval_scope):
        await self._reply(
            inbound,
            "⌛ That card is no longer current — reply **1** to approve or **2** to deny.",
        )
        return
    await self._reply(inbound, _APPROVAL_NOT_PENDING)
