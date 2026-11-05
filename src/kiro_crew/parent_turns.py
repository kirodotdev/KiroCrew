"""Settling a returned sub-agent result with the parent turn that received it.

A blocking ``spawn_sub_agents`` call returns its children's results inside the
parent's own turn, and ``POST /api/spawn/mark-collected`` flags each child once
the reply carrying it reached kiro-cli's pipe. That is not the parent turn
finishing with it: a cron deadline, a stop, a reap or a failure can still end
the turn before its answer is recorded. So for every parent that is not
dashboard-born (:func:`settles_at_release`), a dashboard tab open on it or not, a
mark is only *pending* until the turn that received it gives the session back:

* a turn whose engine noted that it landed **confirms** its marks, and the
  completions they cover are acknowledged without a second turn;
* any other end **revokes** them, and those completions are delivered as
  ordinary ones.

A completion that arrives while its mark is pending is parked by the route and
settled here, so it never waits inside ``_on_done``.

The unit is one *attempt*: one turn inside one holding of a session's turn
permit. Its token is the canonical session key, the task holding the permit
(``_Session.turn_owner`` when the child is read done, the noting or releasing
task otherwise) and an epoch that names the attempt. The done read or the
landing note opens an attempt; ``SessionManager.settle_turn`` ends it (an
in-claim retry ends the failed attempt there) and ``release`` ends the last one.
The next attempt, in the same claim or the same task's next claim, takes a new
epoch. A mark is held only under the attempt that spawned its child
(``POST /api/spawn`` records the attempt that held the session, with its prompt
in flight, when the request arrived, and for every member of one
``spawn_sub_agents`` call only the attempt its first member named,
:meth:`ParentTurnLedger.call_attempt`) and that read it done
(``GET /api/spawn/{id}`` from the parent's own tool stamps it), and only while
that attempt is open. A mark that arrives after its attempt ended, under no
attempt or under a later one (a retry, a successor turn, a sibling's injection,
a compaction), flags nothing: the completion is delivered (one duplicate turn,
never a loss). An engine that never notes landing has every mark revoked, which
also costs a duplicate turn and never a result.

The ledger lives on the ``SessionManager`` (``parent_turns``) and is touched
only on the gateway's loop.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: One attempt of one holding of a session's turn permit: (canonical key,
#: holding task, attempt epoch).
HoldingToken = tuple[str, Any, int]

#: Called with the attempt, whether it landed, and the child ids it held marks for.
ReleaseListener = Callable[[HoldingToken, bool, list[str]], None]

#: How many ``spawn_sub_agents`` calls the ledger remembers the spawning attempt
#: of. One call posts its members back to back, so only the newest calls are ever
#: asked about; the oldest is forgotten first.
CALL_ANCHOR_LIMIT = 1024


class ParentTurnLedger:
    """What each attempt of a parent session's holding owes when it ends."""

    def __init__(self) -> None:
        self._landed: set[HoldingToken] = set()
        self._marked: dict[HoldingToken, list[str]] = {}
        #: The open attempt of each (canonical key, task) holding, by its epoch.
        self._attempts: dict[tuple[str, Any], int] = {}
        #: One counter for the whole ledger: no two attempts share an epoch.
        self._epochs = itertools.count(1)
        #: The gateway's settle hook (``_settle_inline``); None until it is wired.
        self.on_released: ReleaseListener | None = None
        #: The attempt the first member POST of each (canonical key, call id)
        #: named, ``None`` included (:meth:`call_attempt`).
        self._call_anchors: OrderedDict[tuple[str, str], HoldingToken | None] = OrderedDict()

    def open_attempt(self, key: str, task: Any) -> HoldingToken:
        """The open attempt of ``task``'s holding of ``key``, opened if none is.

        A new attempt takes the next epoch, which no earlier attempt of any key or
        task had, so a token is never named twice. Attempts that finished tasks
        left open are ended first (:meth:`_end_finished`).
        """
        epoch = self._attempts.get((key, task))
        if epoch is None:
            self._end_finished()
            epoch = self._attempts[(key, task)] = next(self._epochs)
        return (key, task, epoch)

    def open_token(self, key: str, task: Any) -> HoldingToken | None:
        """The open attempt of ``task``'s holding of ``key``, or None. Opens nothing."""
        epoch = self._attempts.get((key, task))
        return None if epoch is None else (key, task, epoch)

    def end_attempt(self, key: str, task: Any) -> None:
        """End the open attempt of ``task``'s holding of ``key`` and settle it.

        The next attempt of the same holding, or of the same task's next claim,
        opens with a new epoch, so a mark stamped under this one is never held
        under it. Nothing to end when this holding opened no attempt.
        """
        epoch = self._attempts.pop((key, task), None)
        if epoch is not None:
            self.settle((key, task, epoch))

    def _end_finished(self) -> None:
        """End every open attempt whose task has finished.

        Such an attempt cannot end itself: a compaction, for one, gives the
        permit back with a raw ``semaphore.release()``. Its marks are settled
        as its release would have settled them.
        """
        finished = [
            (key, task)
            for key, task in self._attempts
            if isinstance(task, asyncio.Future) and task.done()
        ]
        for key, task in finished:
            self.end_attempt(key, task)

    def call_attempt(
        self, key: str, call_id: str, holding: HoldingToken | None
    ) -> HoldingToken | None:
        """The spawning attempt a member of ``spawn_sub_agents`` call ``call_id`` records.

        The call's first member POST anchors the call to the attempt it named
        (``holding``, ``None`` when it named none) and records it. A later member
        records ``holding`` only when it equals a non-None anchor, else ``None``: a
        call cut between two of its POSTs never records the attempt that holds the
        permit after the cut. An anchor outlives its attempt for that reason; the
        map keeps the newest :data:`CALL_ANCHOR_LIMIT` calls.
        """
        anchor_key = (key, call_id)
        if anchor_key not in self._call_anchors:
            self._call_anchors[anchor_key] = holding
            while len(self._call_anchors) > CALL_ANCHOR_LIMIT:
                self._call_anchors.popitem(last=False)
            return holding
        self._call_anchors.move_to_end(anchor_key)
        anchor = self._call_anchors[anchor_key]
        return holding if anchor is not None and holding == anchor else None

    def note_landed(self, token: HoldingToken) -> None:
        """The engine holding ``token`` decided its turn landed."""
        self._landed.add(token)

    def hold_marks(self, token: HoldingToken, child_ids: list[str]) -> None:
        """Marks that ``token``'s release confirms or revokes."""
        if child_ids:
            self._marked.setdefault(token, []).extend(child_ids)

    def pending(self, token: HoldingToken) -> bool:
        return token in self._marked

    def settle(self, token: HoldingToken) -> None:
        """End ``token``'s attempt: hand its marks and landing to the listener.

        Never raises: a listener failure is logged, and the release it runs
        inside goes on.
        """
        landed = token in self._landed
        self._landed.discard(token)
        child_ids = self._marked.pop(token, [])
        listener = self.on_released
        if not child_ids or listener is None:
            return
        try:
            listener(token, landed, child_ids)
        except Exception:
            logger.exception("settling inline-returned results for %s failed", token[0])


def note_turn_landed(sessions: Any, session_key: str) -> None:
    """Tell the session that this engine's turn landed.

    Call it where the engine decides that its stop reason succeeded, from the task
    holding the session's permit. The end of that attempt then confirms the inline
    results the turn received, so their completions play no second turn. An engine
    that never calls it has them revoked and delivered again: a duplicate turn,
    never a lost result. Defensive on the accessor: a stand-in without it gets a
    no-op.
    """
    note = getattr(sessions, "note_turn_landed", None)
    if callable(note):
        note(session_key)


def settle_turn(sessions: Any, session_key: str) -> None:
    """End this engine's current attempt on the session without releasing its permit.

    For an engine that runs a second turn inside one claim: the first turn's
    inline marks are settled now, by its own landing or lack of one, and the
    second turn is a new attempt with a new token, so a late mark of the first is
    never held under it. Defensive on the accessor, like :func:`note_turn_landed`.
    """
    settle = getattr(sessions, "settle_turn", None)
    if callable(settle):
        settle(session_key)


def ledger_of(sessions: Any) -> ParentTurnLedger | None:
    """The session manager's ledger, or None for a stand-in that has none."""
    ledger = getattr(sessions, "parent_turns", None)
    return ledger if isinstance(ledger, ParentTurnLedger) else None


def settles_at_release(session_key: str) -> bool:
    """Whether a returned result for *session_key* settles at its turn's release.

    True for every parent but a dashboard-born one (``dashboard:<tab>``, or its
    folded ``dashboard_<tab>`` spelling): a cron job, a channel conversation, a
    task runner, whether or not a dashboard tab shows it. The mark
    (``_flag_collected_inline``) and every completion route ask this one
    predicate, so the tab a completion is routed to never changes which rule its
    parent gets. A dashboard-born parent's tab route acknowledges on the mark.
    """
    return not session_key.startswith(("dashboard:", "dashboard_"))
