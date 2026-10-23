"""Settling a returned sub-agent result with the parent turn that received it.

A blocking ``spawn_sub_agents`` call returns its children's results inside the
parent's own turn, and ``POST /api/spawn/mark-collected`` flags each child once
the reply carrying it reached kiro-cli's pipe. That is not the parent turn
finishing with it: a cron deadline, a stop, a reap or a failure can still end
the turn before its answer is recorded. So for a cron or channel parent a mark is
only *pending* until the turn that received it gives the session back:

* a turn whose engine noted that it landed **confirms** its marks, and the
  completions they cover are acknowledged without a second turn;
* any other end **revokes** them, and those completions are delivered as
  ordinary ones, as main always did.

A completion that arrives while its mark is pending is parked by the route and
settled here, so it never waits inside ``_on_done``.

The unit is one *holding* of a session's turn permit: the canonical session key
and the task holding the permit (``_Session.turn_owner`` when the mark arrives,
the releasing task at ``SessionManager.release``). A mark that arrives while no
turn holds the permit is never pending: the turn it belonged to already ended,
so the completion is delivered (one duplicate turn, never a loss). An engine
that never notes landing has every mark revoked, which also costs a duplicate
turn and never a result.

The ledger lives on the ``SessionManager`` (``parent_turns``) and is touched
only on the gateway's loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: One holding of a session's turn permit: (canonical key, holding task).
HoldingToken = tuple[str, Any]

#: Called with the holding, whether it landed, and the child ids it held marks for.
ReleaseListener = Callable[[HoldingToken, bool, list[str]], None]


class ParentTurnLedger:
    """What each holding of a parent session's permit owes at its release."""

    def __init__(self) -> None:
        self._landed: set[HoldingToken] = set()
        self._marked: dict[HoldingToken, list[str]] = {}
        #: The gateway's settle hook (``_settle_inline``); None until it is wired.
        self.on_released: ReleaseListener | None = None

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
        """End ``token``'s holding: hand its marks and landing to the listener.

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
    holding the session's permit. That holding's release then confirms the inline
    results the turn received, so their completions play no second turn. An engine
    that never calls it has them revoked and delivered again: a duplicate turn,
    never a lost result. Defensive on the accessor: a stand-in without it gets a
    no-op.
    """
    note = getattr(sessions, "note_turn_landed", None)
    if callable(note):
        note(session_key)


def ledger_of(sessions: Any) -> ParentTurnLedger | None:
    """The session manager's ledger, or None for a stand-in that has none."""
    ledger = getattr(sessions, "parent_turns", None)
    return ledger if isinstance(ledger, ParentTurnLedger) else None
