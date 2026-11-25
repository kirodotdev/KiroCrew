"""How long each client-facing resource subscription call may take, the lock wait included.

``rfc-mcp-resource-subscriptions.md`` §4.9.3: a subscribe or a removal answers within
its budget (:data:`SUBSCRIBE_BUDGET_SECS`, :data:`UNSUBSCRIBE_BUDGET_SECS`), each below
the timeout its caller gives up at, so no route reports failure for a change that later
commits. :class:`_Budget` is one call's deadline, :func:`_locked_within` a lock wait
inside it, and :func:`_holder_call_within` a holder call bounded by what is left.

An owner of :mod:`kiro_crew.mcp_gateway.resource_subscriptions`, which stays the import
path and the patch surface. The refusal these raise,
:class:`~kiro_crew.mcp_gateway.resource_subscriptions.SubscribeRefused`, is read from
that facade at call time.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from types import ModuleType
from typing import AsyncIterator, Awaitable

from kiro_crew.mcp_gateway.backend_wire import _log_safe_identifier

# Logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.mcp_gateway.resource_subscriptions")


def _facade() -> ModuleType:
    """The facade, read from ``sys.modules`` when called; it imports this module first."""
    return sys.modules["kiro_crew.mcp_gateway.resource_subscriptions"]


#: Every client-facing route answers within one of these budgets, the wait for
#: ``_lock`` included, whatever a backend does. Each stays strictly below the
#: timeout its caller gives up at, so a caller is never told a change failed
#: while that change goes on to commit. ``BUDGET_MARGIN_SECS`` is the margin a
#: contract test holds them to, against the caller timeouts read with
#: :mod:`inspect`.

#: A subscribe's whole server-side budget. Below the 150 s
#: ``mcp_tools.resources._SUBSCRIBE_TIMEOUT_S`` that ``resource_subscribe`` posts with.
SUBSCRIBE_BUDGET_SECS = 120.0

#: Of a subscribe's budget, what is kept back for the store commit: every holder
#: wait before it is bounded by what is left above this, so the record of a
#: subscribe the server accepted still lands inside the budget.
SUBSCRIBE_COMMIT_RESERVE_SECS = 15.0

#: A removal's whole server-side budget. Below the 30 s default timeout of
#: ``mcp_core._post``, which ``resource_unsubscribe`` posts with, and below the
#: 30 s ``cli_resource_subs._TIMEOUT_SECS`` the operator route answers.
UNSUBSCRIBE_BUDGET_SECS = 20.0

#: How far under its caller's timeout a budget must stay.
BUDGET_MARGIN_SECS = 5.0

#: A holder call after a commit is given at least this long, so the holder always
#: reaches the step where it forgets the URI. A wait of zero would not start it,
#: and it would re-subscribe a subscription that is already removed.
HOLDER_CALL_FLOOR_SECS = 0.1


class _Budget:
    """How long one call may take in all, as a deadline on the running loop."""

    def __init__(self, total: float) -> None:
        self.total = total
        self._deadline = asyncio.get_running_loop().time() + total

    def remaining(self) -> float:
        """Seconds left; zero or less once the deadline has passed."""
        return self._deadline - asyncio.get_running_loop().time()

    def allowance(self, what: str, *, reserve: float = 0.0) -> float:
        """Seconds *what* may take, keeping *reserve* for what follows it.

        Raises :class:`SubscribeRefused` rather than start *what* with nothing
        left, so no step of a change runs once the caller has given up on it.
        """
        left = self.remaining() - reserve
        if left <= 0:
            rs = _facade()
            raise rs.SubscribeRefused(
                f"this call reached its bound of {self.total:g}s before {what}; try again",
                rs.REFUSED_GATEWAY_UNAVAILABLE,
            )
        return left

    def holder_call(self) -> float:
        """What is left for a holder call after a commit, never zero."""
        return max(self.remaining(), HOLDER_CALL_FLOOR_SECS)

    def store_deadline(self) -> float:
        """This budget's end as a ``time.monotonic()`` instant, for a store write.

        The write runs on a worker thread, where the loop clock is not read, so
        its file-lock wait is bounded by this instead: a write still waiting for
        the lock when the caller's answer has gone out never commits.
        """
        return time.monotonic() + self.remaining()


@asynccontextmanager
async def _locked_within(
    lock: asyncio.Lock,
    budget: _Budget,
    what: str,
    *,
    reserve: float = 0.0,
) -> AsyncIterator[None]:
    """Hold *lock*, taken inside *budget*, or refuse before anything moves.

    The acquire is awaited inline, so a wait that ends at the bound or is
    cancelled leaves the lock unheld: a cancelled ``Lock.acquire`` passes a
    lock it was just granted on to the next waiter.
    """
    bound = budget.allowance(f"the {what} lock", reserve=reserve)
    try:
        async with asyncio.timeout(bound):
            await lock.acquire()
    except TimeoutError:
        rs = _facade()
        raise rs.SubscribeRefused(
            f"another resource subscription change is still in progress, so this {what} "
            f"did not start within {budget.total:g}s; try again",
            rs.REFUSED_GATEWAY_UNAVAILABLE,
        ) from None
    try:
        yield
    finally:
        lock.release()


def _holder_failure(exc: BaseException) -> str:
    """Why a holder or gateway call failed, for a refusal reason. Never empty."""
    if isinstance(exc, asyncio.TimeoutError):
        return "it did not answer in time"
    return str(exc) or type(exc).__name__


#: Holder closes a removal left running at its bound, held so nothing collects
#: them. The holder is out of ``_holders`` before the wait, so each is already
#: detached from the service.
_LATE_HOLDER_CLOSES: set["asyncio.Future[None]"] = set()


async def _holder_call_within(call: Awaitable[None], bound: float, what: str) -> None:
    """Await *call* for at most *bound*, cancelling it rather than overrun.

    A cancelled unsubscribe ends the holder's session, exactly as one that timed
    out on the wire does, so what the server still holds goes with the connection.
    """
    try:
        await asyncio.wait_for(call, bound)
    except asyncio.TimeoutError:
        logger.warning(
            "resource subscriptions: %s did not finish inside the answer's bound",
            _log_safe_identifier(what),
        )
