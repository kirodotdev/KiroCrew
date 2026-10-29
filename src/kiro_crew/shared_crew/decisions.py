"""Atomic resolution of shared approvals and questions.

Every collaborator sees a pending approval or agent question and any one may
answer it. :class:`DecisionRegistry` makes that safe: the classification
(:func:`~kiro_crew.shared_crew.collab.classify_response`), the side effect that
settles the waiting future, and the state write all run under one lock, so the
first valid response is the only one that can win. A retry carrying the winning
``response_id`` replays the same outcome without a second side effect; any other
late response gets the resolved decision back with ``already_resolved``.

The registry is in memory and bounded. It holds the authoritative state for
decisions that are open now plus a window of recent outcomes, which is what a
reconnecting tab reconciles against. It is not an audit store: the decision is
also written to the existing audit trail by the caller.

Existing authorization runs before a response reaches :meth:`resolve`; the
registry never admits a caller, it only orders the admitted ones.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Callable, Mapping

from kiro_crew.shared_crew.collab import (
    DecisionKind,
    DecisionState,
    ResolveOutcome,
    SharedDecision,
    classify_response,
    decision_frame,
)
from kiro_crew.shared_crew.contracts import (
    ActionOutcome,
    ActionType,
    ActorContext,
    Principal,
    Transport,
)

logger = logging.getLogger(__name__)

#: Decisions retained (open plus recently settled). Settled ones yield first.
MAX_RETAINED_DECISIONS = 1000
#: Longest ``response_id`` a client may send; longer is refused as malformed.
MAX_RESPONSE_ID_LEN = 128
#: The ``meta`` key a user message's author rides under (``contracts``).
_AUTHOR_META_KEY = "author"


def approval_decision_id(session_key: str, request_id: str) -> str:
    """The stable decision id for one approval request in one session.

    Built from the slot key and the request id, length-prefixed so no two pairs
    collide, so every tab (and the browser, which renders the card from the same
    two values) derives the same id without a lookup. It names nothing the
    public decision does not already carry in ``session_key``.
    """
    return f"approval:{len(session_key)}:{session_key}:{request_id}"


def state_approval_decision_id(approval_id: str) -> str:
    """The decision id for a state-level (background) approval."""
    return f"approval-state:{approval_id}"


def question_decision_id(ask_id: str) -> str:
    return f"question:{ask_id}"


def read_response_id(body: Mapping[str, Any]) -> tuple[str, bool]:
    """Return ``(response_id, ok)``. Absent is ``("", True)``; malformed is not ok."""
    raw = body.get("response_id")
    if raw is None:
        return "", True
    if not isinstance(raw, str) or not raw or len(raw) > MAX_RESPONSE_ID_LEN:
        return "", False
    return raw, True


def request_principal(request: Any) -> Principal:
    """The verified actor of an HTTP or socket request, else Unknown.

    Reads only what the auth middleware published. A body, header or query
    value is never consulted.
    """
    from kiro_crew.shared_crew.identity import actor_of

    return actor_of(request).principal


def turn_initiator(messages: object) -> Principal:
    """The author of the latest user turn in a transcript, else Unknown.

    An approval or question is raised by the turn that is running, and that turn
    was started by the most recent user message. Its author stamp is display
    data written by the server when it accepted the message.
    """
    from kiro_crew.shared_crew.contracts import principal_or_unknown

    if not isinstance(messages, list):
        return Principal.unknown()
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        meta = message.get("meta")
        if isinstance(meta, dict):
            return principal_or_unknown(meta.get(_AUTHOR_META_KEY))
        return Principal.unknown()
    return Principal.unknown()


def audit_decision_response(resolution: "Resolution", responder: Principal) -> None:
    """Write a decision response to the Shared Crew audit trail. Never raises.

    The winning response is ``applied``; a different response that lost the race
    is ``conflict``. A replay of the winning response and a response to nothing
    pending add no record. The record names the decision's initiator, this
    responder and the decision id, never the answer text.
    """
    decision = resolution.decision
    if decision is None:
        return
    if resolution.outcome is ResolveOutcome.WON:
        outcome = ActionOutcome.APPLIED
    elif resolution.outcome is ResolveOutcome.ALREADY_RESOLVED:
        outcome = ActionOutcome.CONFLICT
    else:
        return
    try:
        from kiro_crew.shared_crew.audit import schedule_action_audit

        action = (
            ActionType.QUESTION_ANSWER
            if decision.kind is DecisionKind.QUESTION
            else ActionType.APPROVAL
        )
        schedule_action_audit(
            action,
            session_key=decision.session_key,
            initiator=ActorContext(
                principal=decision.initiator,
                request_id=decision.decision_id,
                transport=Transport.HTTP,
            ),
            outcome=outcome,
            target=decision.decision_id,
            responder=ActorContext(
                principal=responder, request_id=decision.decision_id, transport=Transport.HTTP
            ),
        )
    except Exception:
        logger.debug("shared decision audit failed", exc_info=True)


@dataclass(frozen=True)
class Resolution:
    outcome: ResolveOutcome
    decision: SharedDecision | None

    @property
    def applied(self) -> bool:
        return self.outcome is ResolveOutcome.WON


class DecisionRegistry:
    """Authoritative state of shared decisions for one gateway."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        max_retained: int = MAX_RETAINED_DECISIONS,
    ) -> None:
        self._clock = clock
        self._max = max_retained
        self._lock = threading.Lock()
        self._records: OrderedDict[str, SharedDecision] = OrderedDict()

    def open(
        self,
        decision_id: str,
        kind: DecisionKind,
        session_key: str,
        initiator: Principal,
    ) -> SharedDecision:
        """Register a pending decision, or return the one already pending.

        An id whose earlier decision is settled is reused by a new request (ACP
        request ids recur), so it opens a fresh pending decision with the next
        revision.
        """
        with self._lock:
            current = self._records.get(decision_id)
            if current is not None and current.state is DecisionState.PENDING:
                return current
            revision = 1 if current is None else current.revision + 1
            decision = SharedDecision(
                decision_id=decision_id,
                kind=kind,
                session_key=session_key,
                state=DecisionState.PENDING,
                revision=revision,
                initiator=initiator,
                created_at=self._clock(),
            )
            self._store_locked(decision)
            return decision

    def get(self, decision_id: str) -> SharedDecision | None:
        with self._lock:
            return self._records.get(decision_id)

    def for_session(self, session_key: str) -> list[SharedDecision]:
        with self._lock:
            return [d for d in self._records.values() if d.session_key == session_key]

    def resolve(
        self,
        decision_id: str,
        *,
        response_id: str,
        responder: Principal,
        outcome: str,
        apply: Callable[[], bool],
    ) -> Resolution:
        """Settle a decision once.

        ``apply`` performs the real side effect (settling the waiting future) and
        returns whether it took. It runs under the lock and only for the winning
        response, so it must not await or block. When it returns ``False`` the
        thing waiting is already gone and the decision is left as it was.
        """
        if not outcome:
            raise ValueError("a resolution names its outcome")
        resolution = self._resolve_locked(decision_id, response_id, responder, outcome, apply)
        audit_decision_response(resolution, responder)
        return resolution

    def _resolve_locked(
        self,
        decision_id: str,
        response_id: str,
        responder: Principal,
        outcome: str,
        apply: Callable[[], bool],
    ) -> Resolution:
        with self._lock:
            current = self._records.get(decision_id)
            if current is None:
                return Resolution(ResolveOutcome.NOT_PENDING, None)
            verdict = classify_response(current, response_id)
            if verdict is not ResolveOutcome.WON:
                return Resolution(verdict, current)
            if not apply():
                return Resolution(ResolveOutcome.NOT_PENDING, current)
            settled = replace(
                current,
                state=DecisionState.RESOLVED,
                revision=current.revision + 1,
                responder=responder,
                response_id=response_id,
                outcome=outcome,
                resolved_at=self._clock(),
            )
            self._store_locked(settled)
            return Resolution(ResolveOutcome.WON, settled)

    def classify(self, decision_id: str, response_id: str) -> Resolution:
        """What a response would get now, without settling anything."""
        with self._lock:
            current = self._records.get(decision_id)
            if current is None:
                return Resolution(ResolveOutcome.NOT_PENDING, None)
            return Resolution(classify_response(current, response_id), current)

    def classify_late(self, decision_id: str, response_id: str, responder: Principal) -> Resolution:
        """:meth:`classify` for a response that arrived after the decision settled.

        A different response is a losing one and is audited ``conflict`` like
        one that lost inside :meth:`resolve`; a replay of the winner and a
        response to nothing add no record.
        """
        resolution = self.classify(decision_id, response_id)
        audit_decision_response(resolution, responder)
        return resolution

    def close(self, decision_id: str, state: DecisionState) -> SharedDecision | None:
        """End a pending decision nobody answered (``expired`` / ``withdrawn``)."""
        if state not in (DecisionState.EXPIRED, DecisionState.WITHDRAWN):
            raise ValueError("close ends an unanswered decision")
        with self._lock:
            current = self._records.get(decision_id)
            if current is None or current.state is not DecisionState.PENDING:
                return None
            closed = replace(current, state=state, revision=current.revision + 1)
            self._store_locked(closed)
            return closed

    def _store_locked(self, decision: SharedDecision) -> None:
        self._records[decision.decision_id] = decision
        self._records.move_to_end(decision.decision_id)
        while len(self._records) > self._max:
            victim = next(
                (k for k, d in self._records.items() if d.state is not DecisionState.PENDING),
                None,
            )
            if victim is None:
                victim = next(iter(self._records))
            del self._records[victim]


def decisions_for(state: Any) -> DecisionRegistry:
    """The gateway's decision registry, created on first use."""
    registry = getattr(state, "_shared_crew_decisions", None)
    if not isinstance(registry, DecisionRegistry):
        registry = DecisionRegistry()
        state._shared_crew_decisions = registry
    return registry


def broadcast_decision(state: Any, decision: SharedDecision) -> None:
    """Push the authoritative decision to the sockets that see its card. Never raises.

    An approval goes where approval frames go (``broadcast_ws``, slot scoped).
    A question goes to the owner and every collaborator socket
    (``collaborators.broadcast_to_collaborators``), the same audience its card
    has, so a decision frame never reveals a question to a socket the card is
    hidden from.
    """
    from kiro_crew.shared_crew.collaborators import broadcast_to_collaborators

    frame = decision_frame(decision)
    try:
        if decision.kind is DecisionKind.QUESTION:
            broadcast_to_collaborators(state, frame["type"], frame)
        else:
            state.broadcast_ws(frame["type"], frame)
    except Exception:
        log = getattr(state, "_log", None)
        if log is not None:
            log.warning("shared decision broadcast failed", exc_info=True)


def open_and_broadcast(
    state: Any,
    decision_id: str,
    kind: DecisionKind,
    session_key: str,
    initiator: Principal,
) -> SharedDecision | None:
    """Register a pending decision and push it. Never raises into the caller."""
    try:
        decision = decisions_for(state).open(decision_id, kind, session_key, initiator)
    except Exception:
        log = getattr(state, "_log", None)
        if log is not None:
            log.warning("shared decision open failed", exc_info=True)
        return None
    broadcast_decision(state, decision)
    return decision


def settle_after_wait(
    state: Any, decision_id: str, outcome: str | None, closed: DecisionState
) -> None:
    """Close the books on a decision whose wait has ended.

    When a response came through the registry the decision is already resolved
    and this does nothing. When the wait ended through a path that carries no
    verified responder, the decision resolves with an Unknown responder (it was
    answered, and by whom is not known). With no ``outcome`` nobody answered and
    the decision ends as ``closed``.
    """
    registry = decisions_for(state)
    current = registry.get(decision_id)
    if current is None or current.state is not DecisionState.PENDING:
        return
    if outcome:
        settled = registry.resolve(
            decision_id,
            response_id="",
            responder=Principal.unknown(),
            outcome=outcome,
            apply=lambda: True,
        ).decision
    else:
        settled = registry.close(decision_id, closed)
    if settled is not None:
        broadcast_decision(state, settled)


def open_slot_approval(state: Any, slot: Any, request_id: str) -> str:
    """Open the shared decision for a slot permission prompt the moment it shows.

    The initiator is the author of the turn that asked. Returns the decision id
    for :func:`settle_slot_approval`. Never raises into the caller.
    """
    decision_id = approval_decision_id(str(slot.key), str(request_id))
    open_and_broadcast(
        state,
        decision_id,
        DecisionKind.APPROVAL,
        str(slot.key),
        turn_initiator(getattr(slot, "messages", None)),
    )
    return decision_id


def settle_slot_approval(state: Any, decision_id: str, fut: Any, *, withdrawn: bool) -> None:
    """Close a slot approval's decision once its wait ended. Never raises.

    A future a person settled through the approve route is already resolved,
    so this does nothing; one the host settled resolves with an Unknown
    responder; one nobody settled ends ``withdrawn`` (the turn was cancelled)
    or ``expired``.
    """
    try:
        outcome: str | None = None
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            outcome = str(fut.result() or "") or None
        closed = DecisionState.WITHDRAWN if withdrawn else DecisionState.EXPIRED
        settle_after_wait(state, decision_id, outcome, closed)
    except Exception:
        log = getattr(state, "_log", None)
        if log is not None:
            log.warning("shared decision settle failed", exc_info=True)


def system_initiator(source: str) -> Principal:
    """The bounded automation principal behind a background request source."""
    component = (source or "").split(":", 1)[0].strip() or "dashboard"
    return Principal.system(component[:64])


def conflict_body(resolution: Resolution) -> dict[str, Any]:
    """The 409 body a losing response receives: the error envelope plus the decision."""
    body: dict[str, Any] = {
        "error": "this decision was already answered",
        "code": "already_resolved",
    }
    if resolution.decision is not None:
        body["decision"] = resolution.decision.to_public_dict()
    return body


# -- Approval handler seams ------------------------------------------------
#
# ``dashboard.chat_api.slot_approval`` calls these after every existing gate has
# admitted the caller. Replies are ``(status, body)`` so the handler keeps its
# own ``web.json_response`` calls.

Reply = tuple[int, dict[str, Any]]


def respond(reply: Reply) -> Any:
    """The HTTP answer for a :data:`Reply`: 200 with the body, or 409 ``already_resolved``."""
    from aiohttp import web

    status, body = reply
    if status == 409:
        return web.json_response(
            {
                "error": "this decision was already answered",
                "code": "already_resolved",
                "decision": body.get("decision"),
            },
            status=409,
        )
    return web.json_response(body)


def _settle_future(fut: Any, resolved: str) -> bool:
    if fut.done():
        return False
    fut.set_result(resolved)
    return True


def resolve_slot_approval(
    state: Any,
    request: Any,
    owner: Any,
    request_id: str,
    fut: Any,
    resolved: str,
    response_id: str,
) -> Resolution:
    """Settle a slot approval future through the registry.

    The registry's lock covers the pending check and ``set_result``, so of two
    collaborators clicking at once exactly one settles the future.
    """
    registry = decisions_for(state)
    decision_id = approval_decision_id(owner.key, request_id)
    registry.open(decision_id, DecisionKind.APPROVAL, owner.key, turn_initiator(owner.messages))
    return registry.resolve(
        decision_id,
        response_id=response_id,
        responder=request_principal(request),
        outcome=resolved,
        apply=lambda: _settle_future(fut, resolved),
    )


def resolve_state_approval(
    state: Any,
    request: Any,
    request_id: str,
    approved: bool,
    resolved: str,
    response_id: str,
) -> Reply | None:
    """Resolve a state-level approval once, or ``None`` when none is pending."""
    record = state._pending_approvals.get(request_id)
    if not isinstance(record, dict):
        if state.resolve_state_approval(request_id, approved):
            return 200, {"ok": True}
        return None
    registry = decisions_for(state)
    decision_id = state_approval_decision_id(request_id)
    session_key = str(record.get("slot") or "state")
    registry.open(
        decision_id,
        DecisionKind.APPROVAL,
        session_key,
        system_initiator(str(record.get("source") or "")),
    )
    resolution = registry.resolve(
        decision_id,
        response_id=response_id,
        responder=request_principal(request),
        outcome=resolved,
        apply=lambda: bool(state.resolve_state_approval(request_id, approved)),
    )
    if resolution.applied and resolution.decision is not None:
        broadcast_decision(state, resolution.decision)
        return 200, {"ok": True, "decision": resolution.decision.to_public_dict()}
    return lost_race_reply(resolution, response_id)


def lost_race_reply(resolution: Resolution, response_id: str) -> Reply | None:
    """The answer for a response that did not settle the decision.

    A retry of the winning click replays its success. Any other response from a
    client that sends ``response_id`` gets 409 with the resolved decision; a
    client that sends none keeps the handler's existing 404.
    """
    decision = resolution.decision
    if decision is None:
        return None
    if resolution.outcome is ResolveOutcome.REPLAYED:
        return 200, {"ok": True, "replayed": True, "decision": decision.to_public_dict()}
    if resolution.outcome is ResolveOutcome.ALREADY_RESOLVED and response_id:
        return 409, conflict_body(resolution)
    return None


def settled_reply(
    state: Any,
    slot_key: str,
    request_id: object,
    response_id: str,
    responder: Principal | None = None,
) -> Reply | None:
    """Answer a response whose future is gone from the decision that settled it.

    With *responder*, a different response is audited as the losing one.
    """
    if not isinstance(request_id, str) or not request_id:
        return None
    registry = decisions_for(state)
    for decision_id in (
        approval_decision_id(slot_key, request_id),
        state_approval_decision_id(request_id),
    ):
        resolution = (
            registry.classify(decision_id, response_id)
            if responder is None or not response_id
            else registry.classify_late(decision_id, response_id, responder)
        )
        if resolution.decision is not None:
            return lost_race_reply(resolution, response_id)
    return None


def resolve_approval_by_id(
    state: Any,
    request: Any,
    approval_id: str,
    *,
    approved: bool,
    rejected_once: bool,
    response_id: str,
) -> Reply | None:
    """``POST /api/approvals/{id}/{action}`` through the registry.

    Called after the route's own gates admitted the caller. Resolves the
    state-level approval under that id first, then a slot's, exactly the order
    ``DashboardState.resolve_approval`` uses, so it never widens which request an
    id can reach. Returns ``None`` when nothing is pending and nothing settled is
    known, which the route answers with its existing 404.
    """
    decision = "approved" if approved else ("rejected_once" if rejected_once else "rejected")
    state_future = state._approval_futures.get(approval_id)
    if state_future is not None and not state_future.done():
        return resolve_state_approval(state, request, approval_id, approved, decision, response_id)
    for slot in list(state._slots.values()):
        future = slot._approval_futures.get(approval_id)
        if future is None or future.done():
            continue
        registry = decisions_for(state)
        decision_id = approval_decision_id(slot.key, approval_id)
        registry.open(decision_id, DecisionKind.APPROVAL, slot.key, turn_initiator(slot.messages))
        resolution = registry.resolve(
            decision_id,
            response_id=response_id,
            responder=request_principal(request),
            outcome=decision,
            apply=partial(
                _settle_slot_by_id, state, slot, approval_id, approved, rejected_once, future
            ),
        )
        if resolution.applied and resolution.decision is not None:
            broadcast_decision(state, resolution.decision)
            return 200, {"ok": True, "decision": resolution.decision.to_public_dict()}
        return lost_race_reply(resolution, response_id)
    responder = request_principal(request)
    for slot_key in [s.key for s in state._slots.values()]:
        settled = settled_reply(state, slot_key, approval_id, response_id, responder)
        if settled is not None:
            return settled
    return settled_reply(state, "", approval_id, response_id, responder)


def _settle_slot_by_id(
    state: Any,
    slot: Any,
    approval_id: str,
    approved: bool,
    rejected_once: bool,
    future: Any,
) -> bool:
    return bool(
        state.resolve_slot_approval(
            slot, approval_id, approved, rejected_once=rejected_once, expected_future=future
        )
    )
