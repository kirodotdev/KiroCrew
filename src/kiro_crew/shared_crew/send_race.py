"""First-submit-wins for collaborators sending into one session.

The typing lease (``typing_lease``) keeps a second collaborator from sending
while someone else types. This is the backstop for the moment a lease ends, or
for a send that never held one: when two collaborators send against the same
session state, the host settles it here.
Each session has a *send revision*, a counter of accepted sends. A send carries
``base_revision``, the revision its tab last saw. :meth:`SendRaceRegistry.claim`
compares it with the current revision and, when someone else's send was accepted
since that revision, refuses the send with a conflict naming that send's author
(the server-derived principal) and message id. The refused send never reaches the
queue, the steer path or a turn, and its tab keeps the draft.

This is revision-based compare-and-set, chosen for the Shared Crew POC. It is not
an approved product contract; the boundary between a refused race and accepted
work waiting for capacity is still open (spec ``shared-crew.md`` section 5).

Atomicity. A granted claim stays pending until the send handler either records
the send (:meth:`commit`) or answers without accepting it (:meth:`release`). A
second claim on the same session waits for the pending one to settle and then
compares against the settled revision, so exactly one of two racing sends is
accepted, and a send the host refuses for another reason (capacity, a closed
session) never makes a collaborator lose a race against nothing.

Rules, in order, once no claim is pending:

* a retry under an idempotency key this principal already won with is a replay:
  it answers the recorded send and accepts nothing;
* a send with no ``base_revision`` (an app, a script, an old client) is not
  compared;
* a send compares against the sends accepted after its base. Only another
  *person*'s send makes it lose, by the same rule the typing lease holds a
  lease with (``typing_lease.same_person``): one person's own tabs, and a local
  operator's own bootstrap subjects, never race each other, so a single
  operator's sessions behave exactly as before;
* a session with no accepted send since this process started accepts any base,
  and its next revision continues above the highest base it has seen, so a tab
  from before a restart still loses to a send accepted after it.

Everything is in memory and bounded: :data:`MAX_SESSIONS` sessions (idle ones
evicted oldest first), :data:`HISTORY_PER_SESSION` accepted sends per session.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.collab import FRAME_SEND_STATE
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.shared_crew.typing_lease import note_sent, same_person

#: Sessions tracked at once; the least recently used idle one is dropped first.
MAX_SESSIONS = 4096
#: Accepted sends remembered per session (for comparison and replay).
HISTORY_PER_SESSION = 64
#: Seconds a claim waits for a pending claim on its session to settle.
SETTLE_WAIT_SECONDS = 30.0
#: Longest message id kept from a caller.
MAX_MESSAGE_ID_LEN = 128


@dataclass(frozen=True)
class AcceptedSend:
    """One send the host accepted into a session."""

    revision: int
    principal: Principal
    message_id: str
    idempotency_key: str
    accepted_at: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "principal": self.principal.to_public_dict(),
            "message_id": self.message_id,
            "accepted_at": self.accepted_at,
        }


@dataclass
class Claim:
    """A granted send waiting for its handler to record or drop it."""

    session_key: str
    principal: Principal
    base_revision: int | None
    idempotency_key: str
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    result: AcceptedSend | None = None
    #: The task that claimed: the send handler's own per-request task. When it
    #: ends without a commit the claim is given back.
    owner: asyncio.Task[Any] | None = None


@dataclass(frozen=True)
class Granted:
    claim: Claim


@dataclass(frozen=True)
class Conflict:
    """The send lost: *winner* was accepted after the sender's base revision."""

    winner: AcceptedSend
    current_revision: int


@dataclass(frozen=True)
class Replay:
    """A retry of a send this principal already won with."""

    accepted: AcceptedSend
    current_revision: int


ClaimResult = Granted | Conflict | Replay


@dataclass
class _Session:
    revision: int = 0
    #: Whether a send was accepted here since this process started.
    known: bool = False
    #: The highest base a granted send carried while ``known`` was false.
    seen_base: int = 0
    history: deque[AcceptedSend] = field(default_factory=lambda: deque(maxlen=HISTORY_PER_SESSION))
    pending: Claim | None = None


def _scope(principal: Principal, key: str) -> tuple[str, str]:
    return (principal.id, key)


class SendRaceRegistry:
    """Per-session send revisions and the claim that settles a send race."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        on_commit: Callable[[str, AcceptedSend], None] | None = None,
        settle_wait_seconds: float = SETTLE_WAIT_SECONDS,
        owner_id: str = "",
    ) -> None:
        self._clock = clock
        self._on_commit = on_commit
        self._settle_wait = settle_wait_seconds
        self._owner_id = owner_id
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    def _session(self, session_key: str) -> _Session:
        entry = self._sessions.get(session_key)
        if entry is None:
            entry = _Session()
            self._sessions[session_key] = entry
            self._evict()
        else:
            self._sessions.move_to_end(session_key)
        return entry

    def _evict(self) -> None:
        if len(self._sessions) <= MAX_SESSIONS:
            return
        for key in list(self._sessions):
            if len(self._sessions) <= MAX_SESSIONS:
                return
            if self._sessions[key].pending is None:
                del self._sessions[key]

    def state(self, session_key: str) -> dict[str, Any]:
        """The whole-value send state of a session: the GET body and the frame body."""
        entry = self._sessions.get(session_key)
        last = entry.history[-1] if entry is not None and entry.history else None
        return {
            "schema_version": SCHEMA_VERSION,
            "session_key": session_key,
            "revision": entry.revision if entry is not None else 0,
            "last": last.to_public_dict() if last is not None else None,
        }

    async def claim(
        self,
        session_key: str,
        *,
        principal: Principal,
        base_revision: int | None,
        idempotency_key: str = "",
    ) -> ClaimResult:
        """Decide one send against the session's current state.

        Waits while another claim on the session is pending, then decides with
        no suspension between the comparison and taking the claim.
        """
        entry = self._session(session_key)
        current = asyncio.current_task()
        while entry.pending is not None:
            pending = entry.pending
            if current is not None and pending.owner is current:
                # The same task claims again, so its earlier send has finished
                # without recording (one task runs its sends in order).
                self.release(pending)
                continue
            try:
                await asyncio.wait_for(pending.settled.wait(), self._settle_wait)
            except asyncio.TimeoutError:
                # The pending send has been settling for too long to wait on; it
                # was submitted first, so this send loses to it.
                return Conflict(
                    winner=AcceptedSend(
                        revision=entry.revision + 1,
                        principal=pending.principal,
                        message_id="",
                        idempotency_key="",
                        accepted_at=self._clock(),
                    ),
                    current_revision=entry.revision,
                )
            entry = self._session(session_key)
        if idempotency_key:
            scope = _scope(principal, idempotency_key)
            for accepted in entry.history:
                if _scope(accepted.principal, accepted.idempotency_key) == scope:
                    return Replay(accepted=accepted, current_revision=entry.revision)
        if base_revision is not None and entry.known:
            winner = self._first_unseen(entry, principal, base_revision)
            if winner is not None:
                return Conflict(winner=winner, current_revision=entry.revision)
        claim = Claim(
            session_key=session_key,
            principal=principal,
            base_revision=base_revision,
            idempotency_key=idempotency_key,
            owner=current,
        )
        entry.pending = claim
        if current is not None:
            # A send answered without being recorded (refused for capacity, a
            # closed session, a cancelled request) gives the claim back when its
            # handler ends, so nobody loses a race against nothing.
            current.add_done_callback(lambda _task: self.release(claim))
        return Granted(claim)

    def _first_unseen(
        self, entry: _Session, principal: Principal, base: int
    ) -> AcceptedSend | None:
        """The earliest send by someone else accepted after *base*, or None.

        "Someone else" is the typing lease's own rule
        (:func:`~kiro_crew.shared_crew.typing_lease.same_person`), so the subjects
        that fold into one holder there fold into one sender here.
        """
        # A base above the current revision comes from before a restart, so every
        # send accepted since is unseen.
        unseen = [a for a in entry.history if base > entry.revision or a.revision > base]
        others = [
            a for a in unseen if not same_person(a.principal, principal, owner_id=self._owner_id)
        ]
        return others[0] if others else None

    def commit(self, claim: Claim, *, message_id: str = "") -> AcceptedSend:
        """Record *claim*'s send as accepted and bump the revision. Idempotent."""
        if claim.result is not None:
            return claim.result
        entry = self._session(claim.session_key)
        floor = entry.revision
        if not entry.known and claim.base_revision is not None:
            floor = max(floor, claim.base_revision)
        mid = message_id[:MAX_MESSAGE_ID_LEN] if message_id else ""
        accepted = AcceptedSend(
            revision=floor + 1,
            principal=claim.principal,
            message_id=mid or f"send_{uuid.uuid4().hex[:16]}",
            idempotency_key=claim.idempotency_key,
            accepted_at=self._clock(),
        )
        entry.revision = accepted.revision
        entry.known = True
        entry.history.append(accepted)
        claim.result = accepted
        self._settle(entry, claim)
        if self._on_commit is not None:
            try:
                self._on_commit(claim.session_key, accepted)
            except Exception:
                pass
        return accepted

    def release(self, claim: Claim) -> None:
        """Drop *claim* without accepting it. A no-op once committed or released."""
        entry = self._sessions.get(claim.session_key)
        if entry is not None:
            self._settle(entry, claim)
        else:
            claim.settled.set()

    @staticmethod
    def _settle(entry: _Session, claim: Claim) -> None:
        if entry.pending is claim:
            entry.pending = None
        claim.settled.set()


def send_race_for(state: Any) -> SendRaceRegistry:
    """The gateway's send-race registry, created on first use.

    An accepted send pushes the session's send state to every socket, the same
    audience the session's chat frames reach.
    """
    registry = getattr(state, "_shared_crew_send_race", None)
    if isinstance(registry, SendRaceRegistry):
        return registry
    created: list[SendRaceRegistry] = []

    def _announce(session_key: str, _accepted: AcceptedSend) -> None:
        frame = {"type": FRAME_SEND_STATE, **created[0].state(session_key)}
        state.broadcast_ws(FRAME_SEND_STATE, frame)

    created.append(
        SendRaceRegistry(on_commit=_announce, owner_id=str(getattr(state, "owner_id", "") or ""))
    )
    state._shared_crew_send_race = created[0]
    return created[0]


# -- the chat send handler's side --------------------------------------------------

#: Request key holding the send's granted :class:`Claim`.
CLAIM_REQUEST_KEY = "shared_crew_send_claim"


class BaseRevisionError(ValueError):
    """``base_revision`` was present and not a non-negative integer."""


def base_revision_of(raw: object) -> int | None:
    """A send body's ``base_revision`` value, or None when it sent none."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise BaseRevisionError("base_revision must be a non-negative integer")
    return raw


def conflict_body(conflict: Conflict, *, session_key: str) -> dict[str, Any]:
    """The 409 body of a lost race. The draft stays with the sender."""
    from kiro_crew.shared_crew.admission import ReasonCode

    winner = conflict.winner.to_public_dict()
    return {
        "error": "someone else sent first",
        "code": ReasonCode.SEND_RACE.value,
        "session_key": session_key,
        "current_revision": conflict.current_revision,
        "winner": winner,
    }


def replay_body(replay: Replay, *, slot_key: str) -> dict[str, Any]:
    return {
        "ok": True,
        "slot": slot_key,
        "replayed": True,
        "send_revision": replay.accepted.revision,
        "message_id": replay.accepted.message_id,
    }


def commit_request(request: Any, *, message_id: str = "") -> AcceptedSend | None:
    """Record the request's claimed send as accepted. None when it holds no claim."""
    claim = request.get(CLAIM_REQUEST_KEY) if hasattr(request, "get") else None
    if not isinstance(claim, Claim):
        return None
    state = request.app["state"]
    first = claim.result is None
    accepted = send_race_for(state).commit(claim, message_id=message_id)
    if first:
        # The sender's typing lease ends with the send, for everyone at once.
        try:
            note_sent(state, claim.session_key, claim.principal)
        except Exception:
            pass
    return accepted
