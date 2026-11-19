"""Exactly one answer per workflow-step request, and no allow after its step's Stop.

Every step request the router takes is registered as an :class:`AnswerClaim` the
moment it is read (``_on_step_request``, synchronous, on the reader loop), before
any await. The claim is the single source of truth for that request: it names the
runtime and id to answer, and it alone says whether the request was answered or
withdrawn. Every answer the router writes goes through :func:`_send`, and every
caller hands it the claim it was given (admission is given its claim; a held
approval or question carries its own). :func:`_send` passes the runtime a
``withdrawal_check`` (``AcpRuntime.send_response``, ``acp/runtime_wire.py``) that
runs under the runtime's stdin write lock, in the same critical section as the
write, and reads and sets the claim's own flags there. So:

- a request is written to at most once, whichever writer gets the lock first;
- once a step is marked stopped (:func:`_mark_stopped`, synchronous, before Stop's
  ``session/cancel`` is even queued), every claim open on it is latched
  ``withdrawn``, and so is every claim read while it stays stopped: no allow and no
  other non-cancel answer is written for one, even one already queued on the lock
  or one whose step a later re-drive reopened; only the ``cancelled`` answer of
  :func:`_withdraw_session_asks` or :func:`_answer_late`;
- a request read after the Stop (its frame was already on the pipe) is answered
  ``cancelled`` at once and never admitted, as the chat path's
  ``acp.disposed_session.answer_late_permission`` answers one asked after a cancel;
- each ``cancelled`` answer writes one security-log row first, as the chat path's
  ``_audit_cancelled_approvals`` does.

No answer is ever written without a claim: :func:`_send` refuses one, because the
claim is what names the request (the chat path's lock-time check is likewise bound
to its own request, ``acp/client_permissions.py`` ``before_write``). Nothing is
remembered about a request once it is answered, so no bounded history can let a
second answer through.

Each function here whose first parameter is ``self`` is a
:class:`~kiro_crew.kas_workflows.router.KasWorkflowRouter` method the class binds by
name. Nothing here redacts: an id is logged only in a fixed, safe shape (:func:`log_id`).
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import re
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.kas_workflows.router import KasWorkflowRouter

logger = logging.getLogger("kiro_crew.kas_workflows.router")

#: Stopped step sessions remembered (oldest dropped past it).
MAX_STOPPED_SESSIONS = 512
#: Open claims kept before those of dead runtimes are pruned.
MAX_OPEN_CLAIMS = 1024

#: Each Stop's value: strictly increasing for the process's life.
_STOP_GENERATION = itertools.count(1)

#: A string request id logged as it is; any other is logged as its digest.
_PLAIN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


@dataclass(eq=False)
class AnswerClaim:
    """One step request the router owes exactly one answer: the only record of it."""

    session_id: str
    request_id: int | str
    method: str
    runtime: Any  # weakref.ref to the runtime the request came on
    #: The router's stopped-step map (``_stopped``), read live.
    stopped: Any
    #: Set under the write lock when its answer is written; never cleared.
    answered: bool = False
    #: Its ``cancelled`` answer is being logged and sent (one row, one answer).
    cancelling: bool = False
    #: Open at its step's Stop, or read while stopped: never allowed, even after
    #: a re-drive ends the Stop (only claims read after that are the step's own).
    withdrawn: bool = False
    #: The registry key it was registered under (``_key``), kept for its removal.
    key: Any = None

    def owed(self) -> bool:
        """Whether a non-cancel answer may still be sent for it (checked again under the lock)."""
        return not (self.answered or self.withdrawn or self.session_id in self.stopped)


class _Withdrawn(Exception):
    """Raised under the write lock: this answer must not be written."""


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def log_id(request_id: int | str) -> str:
    """A request id as a security-log row records it: a plain short id, else its digest."""
    if isinstance(request_id, int):
        return str(request_id)
    text = str(request_id)
    return text if _PLAIN_ID.fullmatch(text) else _digest(text)


#: Each live runtime's serial, never reused: ``id()`` is, once a runtime is collected,
#: so a claim keyed on it could be taken for a later runtime's request.
_RUNTIME_SERIALS: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_RUNTIME_SERIAL = itertools.count(1)


def _key(runtime: Any, request_id: int | str) -> tuple[int, int | str] | None:
    """The bookkeeping key; an over-long id is kept as a fixed-size digest."""
    from kiro_crew.kas_workflows.asks import MAX_ID_CHARS

    live = runtime() if runtime is not None else None
    if live is None:
        return None
    serial = _RUNTIME_SERIALS.get(live)
    if serial is None:
        serial = _RUNTIME_SERIALS[live] = next(_RUNTIME_SERIAL)
    if isinstance(request_id, str) and len(request_id) > MAX_ID_CHARS:
        request_id = _digest(request_id)
    return (serial, request_id)


def _register(
    self: KasWorkflowRouter, runtime: Any, request_id: int | str, session_id: str, method: str
) -> AnswerClaim:
    """The claim for this request, registered now (synchronous).

    A request read again while its first reading is still open (a ``session/load``
    replay) shares that claim, so the two readings answer once between them.
    """
    key = _key(runtime, request_id)
    existing = self._claims.get(key) if key is not None else None
    if existing is not None:
        return existing
    claim = AnswerClaim(session_id, request_id, method, runtime, self._stopped)
    claim.withdrawn = session_id in self._stopped
    claim.key = key
    if key is not None:
        if len(self._claims) >= MAX_OPEN_CLAIMS:
            _prune_dead_claims(self)
        self._claims[key] = claim
    return claim


def _prune_dead_claims(self: KasWorkflowRouter) -> None:
    for key in [k for k, c in self._claims.items() if c.runtime() is None]:
        self._claims.pop(key, None)


def _forget(self: KasWorkflowRouter, claim: AnswerClaim) -> None:
    # By the key it was registered under: its runtime may be gone by now.
    if claim.key is not None and self._claims.get(claim.key) is claim:
        self._claims.pop(claim.key, None)


def _open_claim(self: KasWorkflowRouter, runtime: Any, request_id: int | str) -> AnswerClaim | None:
    """The claim already open under this runtime and request id, if any."""
    key = _key(runtime, request_id)
    return self._claims.get(key) if key is not None else None


def _drop_claim(self: KasWorkflowRouter, claim: AnswerClaim | None) -> None:
    """Forget the claim of a request dropped unanswered (its run was deleted)."""
    if claim is not None:
        _forget(self, claim)


def _is_stopped(self: KasWorkflowRouter, session_id: str) -> bool:
    return session_id in self._stopped


def _mark_stopped(self: KasWorkflowRouter, session_id: str) -> None:
    """Mark *session_id*'s step stopped. Synchronous; called once per Stop, before
    its ``session/cancel`` (``cancel_step``).

    Each mark takes a new, ever larger value, so a re-drive that read an older
    one (:func:`_stops_of_run`) never ends this Stop (:func:`_end_stops`).
    """
    self._stopped.pop(session_id, None)
    self._stopped[session_id] = next(_STOP_GENERATION)
    for claim in self._claims.values():
        if claim.session_id == session_id:
            claim.withdrawn = True  # latched: a later re-drive does not reopen it
    while len(self._stopped) > MAX_STOPPED_SESSIONS:
        self._stopped.popitem(last=False)


def _stops_of_session(self: KasWorkflowRouter, session_id: str) -> dict[str, int]:
    """The Stop a reply to *session_id* may end: read BEFORE the reply is sent."""
    value = self._stopped.get(session_id)
    return {} if value is None else {session_id: value}


def _stops_of_run(self: KasWorkflowRouter, workflow_id: str) -> dict[str, int]:
    """The Stops a re-drive of *workflow_id* may end: read BEFORE it is sent."""
    stops: dict[str, int] = {}
    for session_id, value in self._stopped.items():
        run = self.mirror.run_for_step_session(session_id)
        if run is not None and run[0] == workflow_id:
            stops[session_id] = value
    return stops


def _end_stops(self: KasWorkflowRouter, stops: dict[str, int]) -> None:
    """Kiro Agent took Crew's re-drive: end the Stops it was sent over, no newer one.

    Only this clears a Stop. A frame Kiro Agent sends (even a ``turn_start``)
    may be the stopped turn's, still buffered on the pipe; a Stop marked while
    the re-drive awaited its reply has a newer value and stays.
    """
    for session_id, value in stops.items():
        if self._stopped.get(session_id) == value:
            self._stopped.pop(session_id, None)


async def _send(
    self: KasWorkflowRouter,
    claim: AnswerClaim | None,
    result: dict[str, Any],
    *,
    cancel: bool = False,
) -> bool:
    """Write *claim*'s one answer; True when it was written.

    The claim's own flags are read and set under the runtime's write lock, right
    before the frame is written: an answered claim is never written again, and a
    non-cancel answer for a withdrawn claim (or one whose step is stopped, which
    latches it withdrawn) is never written. No claim, no write.
    """
    from kiro_crew.kas_workflows.router_base import _alive

    if claim is None:
        logger.warning("workflow step answer with no claim refused")
        return False
    owed: AnswerClaim = claim
    live = _alive(owed.runtime)
    if live is None:
        return False

    def withdrawal_check() -> None:
        if owed.answered:
            raise _Withdrawn  # one answer per request
        if not cancel:
            if owed.session_id in self._stopped:
                owed.withdrawn = True
            if owed.withdrawn:
                raise _Withdrawn
        owed.answered = True
        _forget(self, owed)

    try:
        await live.send_response(owed.request_id, result, withdrawal_check=withdrawal_check)
    except _Withdrawn:
        return False
    except Exception:  # noqa: BLE001 -- a dead runtime fails every wait anyway
        logger.debug("workflow step answer not written", exc_info=True)
        return False
    return True


async def _audit_cancels(self: KasWorkflowRouter, claims: list[AnswerClaim], cause: str) -> None:
    """One security-log row per request answered ``cancelled``; best effort, before the send."""

    def _write() -> None:
        for claim in claims:
            workflow = self.mirror.run_for_step_session(claim.session_id)
            self._audit(
                session_key=(self.slot_of(workflow[0]) if workflow else None) or "kas-workflow",
                agent="kas-workflow",
                source="dashboard",
                tool_name="approval_cancel",
                tool_kind="workflow_step",
                outcome="rejected_on_cancel",
                request_id=log_id(claim.request_id),
                metadata={"reason": cause, "workflowId": workflow[0] if workflow else None},
            )

    if not claims:
        return
    try:
        await asyncio.to_thread(_write)
    except Exception:  # noqa: BLE001 -- the cancels go out regardless
        logger.warning("workflow step cancellations not logged", exc_info=True)


async def _admit(
    self: KasWorkflowRouter,
    claim: AnswerClaim,
    params: dict[str, Any],
    *,
    identity: tuple[Any, str | None] | None = None,
) -> None:
    """Admit one step request under the claim it was registered with when read."""
    if claim.answered:
        return  # a replay of a request already answered (a Stop withdrew it)
    if not claim.owed():
        await _answer_late(self, claim)
        return
    await self._admit_request(claim, params, identity=identity)


async def _answer_late(self: KasWorkflowRouter, claim: AnswerClaim) -> None:
    """Answer ``cancelled`` to a request read after its step was stopped (audited)."""
    from kiro_crew.kas_workflows import asks

    if claim.answered or claim.cancelling:
        return  # answered, or a Stop's withdrawal already logs and cancels it
    claim.withdrawn = True
    claim.cancelling = True
    await _audit_cancels(self, [claim], "asked_after_stop")
    await _send(self, claim, asks.dismiss_result_for(claim.method), cancel=True)


async def _withdraw_session_asks(self: KasWorkflowRouter, session_id: str) -> None:
    """Answer ``cancelled`` to every ask of *session_id* its Stop withdrew, once each.

    The Stop's one mark (:func:`_mark_stopped`, in ``cancel_step``) latched them;
    this never marks again, so a re-drive sent meanwhile still ends that Stop. In
    flight, held, mid-sweep or a question: each is answered through its own claim,
    so a request already answered (an allow written before the Stop) is skipped.
    Each answer writes its security-log row first.
    """
    from kiro_crew.kas_workflows import asks

    owed = {
        id(c): c
        for c in self._claims.values()
        if c.session_id == session_id and c.withdrawn and not (c.answered or c.cancelling)
    }
    results: dict[int, dict[str, Any]] = {
        key: asks.dismiss_result_for(claim.method) for key, claim in owed.items()
    }
    for key in [k for k, a in self._pending.items() if id(a.claim) in owed]:
        self._pending.pop(key)
        self._decided_under.pop(key, None)
        self._retire(key)
    for key in [k for k, a in self._sweeping.items() if id(a.claim) in owed]:
        self._sweeping.pop(key)  # never restored by the sweep
        self._retire(key)
    for key in [k for k, q in self._questions.items() if id(q.claim) in owed]:
        question = self._questions.pop(key)
        self._retire(key)
        results[id(question.claim)] = question.dismiss_result()
    claims = list(owed.values())
    for claim in claims:
        claim.cancelling = True  # synchronous: a late answer for it now logs nothing
    await _audit_cancels(self, claims, "step_stopped")
    for claim in claims:
        await _send(self, claim, results[id(claim)], cancel=True)
    if claims:
        self.revision += 1


def new_stopped() -> OrderedDict[str, int]:
    """Stopped step session -> its Stop's value (:data:`_STOP_GENERATION`)."""
    return OrderedDict()
