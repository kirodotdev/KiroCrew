"""The notification bridge's action binding: a bridged reply resolves a live decision.

RFC ``rfc-notification-bridge-actions.md``, phases B4a, B4c and B4d. The readers
are the Slack Approve and Deny buttons on a pending-approval note and a Telegram
``approve`` / ``deny`` reply to it, behind the default-off ``bridge_approvals``
preview (:func:`actions_enabled`).

A pending decision (a tool-approval prompt's future) is BOUND to a token when the
bridge posts it, and the posted message is LINKED to that token, so a press on the
card or a reply to the message can name the decision without the chat app
supplying anything but the token. The token is the shipped Slack approval token
(``slack.approval_tokens.build_approval_token``): the session-scoped registry key
plus a fresh nonce, so a button minted for an earlier prompt resolves nothing.

Resolution goes through the decision future itself, never a copy, so whichever
surface answers first (the dashboard, a Slack click, a bridged reply) wins and
every other one sees it resolved. Each attempt returns an :class:`ActionOutcome`
for the caller to acknowledge and audit:

* only the verified owner may act; a refused actor does not consume the binding,
  so a stranger cannot burn the owner's answer;
* a bridged prompt offers Approve and Deny only (:data:`BRIDGED_CHOICES`), so a
  ``trust`` reply can never become a session-wide auto-approve;
* a binding is single-use: once its decision resolves, every later answer reads
  "already answered";
* it expires at the deadline its surface passes in, measured on
  :func:`bridge_clock`, the one clock every bridged expiry reads; a decision
  resolved anywhere else answers "already answered" until then, and the binding is
  forgotten after it;
* the state is in-process only, so a gateway restart leaves nothing to resolve.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from kiro_crew.messaging.approval import TRUST
from kiro_crew.messaging.renderer import nonce_eq
from kiro_crew.slack.approval_tokens import (
    _approval_registry_key,
    build_approval_token,
    split_approval_token,
)

#: The only answers a bridged prompt may offer. Trust is not one of them: the
#: signed direction covers approve and deny, and Trust from chat would be its own
#: action for a maintainer to decide.
BRIDGED_CHOICES = frozenset({"approve", "deny"})

#: The answers a bridged ``ask_question`` card may offer: one per option, by
#: position (``option_0`` is the first). Bounded by the card's own option cap.
QUESTION_CHOICES = frozenset(f"option_{i}" for i in range(6))

#: Each kind of bridged decision, and the answers it may offer.
_CHOICES_BY_KIND = {"approval": BRIDGED_CHOICES, "question": QUESTION_CHOICES}


def bridge_clock() -> float:
    """Monotonic seconds: the one clock every bridged expiry reads.

    The deadline is per surface on main (the Slack race on
    ``DashboardState._APPROVAL_TIMEOUT`` / ``_BACKGROUND_APPROVAL_TIMEOUT_SECS``,
    the reply path on ``messaging.approval.TEXT_APPROVAL_TIMEOUT_S``), so each
    caller passes its own ``ttl_s`` and the expiry tests drive this seam instead.
    """
    return time.monotonic()


#: The Slack ``action_id`` prefix of a bridged action button; the answer follows
#: it (``bridge_action_approve``) and the button's ``value`` is the token.
SLACK_ACTION_PREFIX = "bridge_action_"


def actions_enabled(cfg: Any = None) -> bool:
    """Whether a bridged action may be offered or resolve anything. Default off.

    It reads the ``bridge_approvals`` feature preview from *cfg*, or from the live
    config when none is given. Any failure reads as off, so no answer is given.
    """
    try:
        if cfg is None:
            from kiro_crew.config import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        from kiro_crew.notifications.bridge_approval_note import enabled

        return enabled(cfg)
    except Exception:
        return False


class ActionOutcome(str, Enum):
    """What one inbound action did."""

    #: The answer resolved the decision.
    ADMITTED = "admitted"
    #: The decision was already resolved, here or on another surface.
    ALREADY_ANSWERED = "already_answered"
    #: The binding passed its deadline before the answer arrived.
    EXPIRED = "expired"
    #: The sender is not the verified owner; the binding is kept for the owner.
    NOT_OWNER = "not_owner"
    #: The answer is not one of the choices the bound prompt offers.
    INVALID_CHOICE = "invalid_choice"
    #: No live binding matches the token or message (forged, stale, or restarted).
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionContext:
    """Where a bound decision lives, for the audit an action on it writes.

    ``crew_log_session`` and ``turn`` are the crew-log coordinates the decision's own
    ``approval/requested`` entry was written under; ``approval_id`` is that entry's id.
    """

    session_key: str
    approval_id: str
    crew_log_session: str = ""
    turn: int | None = None
    #: The verified owner identity the prompt was bridged to, as the transport names it.
    owner: str = ""
    #: The answer that resolved the decision, once one was admitted.
    answered: str = ""


#: Expired bindings remembered for their audit, so a late press on one is recorded
#: as expired against its own session. Oldest first past this bound.
EXPIRED_MEMORY = 256


@dataclass
class _Binding:
    decision: asyncio.Future[Any]
    nonce: str
    choices: dict[str, Any]
    deadline: float
    context: ActionContext
    settle: Callable[[Any], bool] | None = None


class BridgeActionRegistry:
    """Bindings from bridged messages to the live decisions they ask about.

    One registry per gateway process; it holds no state across a restart.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, _Binding] = {}
        self._messages: dict[tuple[str, str], str] = {}
        self._expired: OrderedDict[str, tuple[str, ActionContext]] = OrderedDict()

    def bind(
        self,
        session_key: str,
        request_id: str | int,
        decision: asyncio.Future[Any],
        *,
        choices: Mapping[str, Any],
        ttl_s: float,
        crew_log_session: str = "",
        turn: int | None = None,
        owner: str = "",
        kind: str = "approval",
        settle: Callable[[Any], bool] | None = None,
    ) -> str:
        """Bind *decision* and return the token its bridged affordances carry.

        *choices* maps each answer the bridged prompt may give to the value the
        decision future takes for it, in that future's own terms (``True`` /
        ``False``, or ``"approved"`` / ``"rejected"``), so a bridged deny can never
        land as a truthy value. A tool approval (*kind* ``"approval"``) offers a
        subset of :data:`BRIDGED_CHOICES`; an ``ask_question`` card (*kind*
        ``"question"``) a subset of :data:`QUESTION_CHOICES`, each mapped to the
        answers the dashboard would submit for that option. Any other answer is
        refused. *settle*, when given, settles the decision in place of setting the
        future, and returns False when it was already settled; a question needs it,
        because its owner resolves a card, not a bare future. *ttl_s* is the deadline
        of the surface that owns the decision. Rebinding the same request replaces
        the earlier token. *crew_log_session* and *turn* are where the decision's own
        crew-log entries live, so an action's audit lands beside them
        (``bridge_action_audit``). *owner* is the verified owner the prompt is
        bridged to, recorded with every action so a refusal names whose prompt a
        stranger tried to answer.
        """
        if not choices:
            raise ValueError("a bridged decision needs at least one choice")
        allowed = _CHOICES_BY_KIND.get(kind)
        if allowed is None:
            raise ValueError(f"unknown bridged decision kind {kind!r}")
        if (kind == "question") != (settle is not None):
            raise ValueError("a bridged question is settled through its card, and only it")
        offered = set(choices) - allowed
        if offered:
            raise ValueError(
                f"a bridged {kind} offers {sorted(allowed)} only, not {sorted(offered)}"
            )
        if any(value == TRUST for value in choices.values()):
            raise ValueError("a bridged answer can never resolve to the trust verdict")
        if not math.isfinite(ttl_s) or ttl_s <= 0:
            raise ValueError("a bridged decision needs a finite, positive deadline")
        if decision.done():
            raise ValueError("cannot bind a decision that is already resolved")
        self._prune()
        key = _approval_registry_key(session_key, request_id)
        nonce = secrets.token_urlsafe(16)
        self._bindings[key] = _Binding(
            decision=decision,
            nonce=nonce,
            choices=dict(choices),
            deadline=bridge_clock() + ttl_s,
            settle=settle,
            context=ActionContext(
                session_key=session_key,
                approval_id=str(request_id),
                crew_log_session=crew_log_session,
                turn=turn,
                owner=owner,
            ),
        )
        self._expired.pop(key, None)
        return build_approval_token(session_key, request_id, nonce)

    def context_for(self, token: str) -> ActionContext | None:
        """The audit context *token* names, live or expired, or None for an unknown token."""
        binding = self._live(token)
        if binding is not None:
            return binding.context
        return self._expired_context(token)

    def link_message(self, transport: str, message_ref: str, token: str) -> None:
        """Record that *message_ref* on *transport* carries *token*.

        A reply-keyword transport has only the message being replied to, so this
        link is what lets such a reply name its decision.
        """
        self._messages[(transport, message_ref)] = token

    def token_for_message(self, transport: str, message_ref: str) -> str:
        """The token linked to a posted message, or ``""`` when the registry forgot it.

        A link outlives its binding as long as the expired binding is remembered
        (:data:`EXPIRED_MEMORY`), so a late reply still names its decision and is
        resolved and audited as ``expired``, never read as an ordinary message.
        """
        token = self._messages.get((transport, message_ref), "")
        if token and self._live(token) is None and self._expired_context(token) is None:
            del self._messages[(transport, message_ref)]
            return ""
        return token

    def resolve(self, token: str, choice: str, *, from_owner: bool) -> ActionOutcome:
        """Answer the decision *token* names with *choice*, if every check passes.

        *from_owner* is the caller's owner check: Slack binds its owner through
        ``slack.handler_runtime.access.is_owner``; every other transport must be in
        ``constants.CHANNEL_OWNER_DM_NAMESPACES`` and name its owner through
        ``messaging.transport.sole_direct_target``.
        """
        binding = self._live(token)
        if binding is None:
            # A binding pruned past its deadline is still known to have expired.
            if self._expired_context(token) is not None:
                return ActionOutcome.EXPIRED
            return ActionOutcome.UNKNOWN
        if not from_owner:
            return ActionOutcome.NOT_OWNER
        # A decision whose waiter timed out is cancelled, not answered.
        if binding.decision.cancelled():
            return ActionOutcome.EXPIRED
        if binding.decision.done():
            return ActionOutcome.ALREADY_ANSWERED
        if bridge_clock() >= binding.deadline:
            return ActionOutcome.EXPIRED
        if choice not in binding.choices:
            return ActionOutcome.INVALID_CHOICE
        # The binding stays until its deadline, so a second press reads as already
        # answered rather than as a token nobody minted.
        if binding.settle is None:
            binding.decision.set_result(binding.choices[choice])
        elif not binding.settle(binding.choices[choice]):
            return ActionOutcome.ALREADY_ANSWERED
        binding.context = replace(binding.context, answered=choice)
        return ActionOutcome.ADMITTED

    def _live(self, token: str) -> _Binding | None:
        """The binding *token* names when its nonce matches, else None."""
        key, nonce = split_approval_token(token)
        binding = self._bindings.get(key)
        if binding is None or not nonce:
            return None
        # The shared press compare: total over every str, so a malformed forged
        # nonce answers UNKNOWN instead of raising.
        if not nonce_eq(binding.nonce, nonce):
            return None
        return binding

    def _expired_context(self, token: str) -> ActionContext | None:
        """The context of the expired binding *token* names when its nonce matches."""
        key, nonce = split_approval_token(token)
        remembered = self._expired.get(key)
        if remembered is None or not nonce_eq(remembered[0], nonce):
            return None
        return remembered[1]

    def _prune(self) -> None:
        """Forget bindings past their deadline, so the registry stays bounded."""
        now = bridge_clock()
        for key in [k for k, b in self._bindings.items() if now >= b.deadline]:
            binding = self._bindings.pop(key)
            self._expired[key] = (binding.nonce, binding.context)
        while len(self._expired) > EXPIRED_MEMORY:
            self._expired.popitem(last=False)
        for ref in [
            r
            for r, token in self._messages.items()
            if self._live(token) is None and self._expired_context(token) is None
        ]:
            del self._messages[ref]
