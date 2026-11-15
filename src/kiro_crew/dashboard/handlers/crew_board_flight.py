"""Single flight for ``GET /api/crew-board``: concurrent reads of one board share a build.

The board answer is built from every item and event tail of one ledger, then redacted:
pure Python, several seconds on a large board. Concurrent reads each repeated the whole
build. Nothing is kept after a build: every answer is a build that began after the
request arrived, so it is exactly what a fresh read on main would return.

The rule, per board:

* A request joins the board's NEXT build if that build has not started yet; otherwise
  it creates the next build. A build that is already running is never joined, because
  it may have read the ledger before the request arrived (before a write or a Stop the
  caller just made).
* The next build starts when the running one finishes, so a board has at most one build
  running and one waiting, however many requests arrive.
* Only a 200 is shared. A waiter whose shared build answered anything else, or raised,
  runs its own build, so a refusal or error is never handed to a caller it was not
  built for.

The access check and request validation run in the handler BEFORE this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import NamedTuple


class BoardAnswer(NamedTuple):
    status: int
    body: bytes
    #: Items on the answer, for the read's audit line.
    items: int = 0


Build = Callable[[], Awaitable[BoardAnswer]]

#: Board key -> the build that has not started yet (requests join it).
_waiting: dict[tuple[str, str], asyncio.Future[BoardAnswer]] = {}
#: Board key -> the build that is running (never joined).
_running: dict[tuple[str, str], asyncio.Future[None]] = {}


async def _run(ck: tuple[str, str], answer: asyncio.Future[BoardAnswer], build: Build) -> None:
    previous = _running.get(ck)
    if previous is not None:
        await asyncio.wait({previous})
    if _waiting.get(ck) is answer:
        del _waiting[ck]
    done = asyncio.get_running_loop().create_future()
    _running[ck] = done
    try:
        answer.set_result(await build())
    except Exception as exc:  # handed to the waiters, who each build again
        answer.set_exception(exc)
        answer.exception()  # a build nobody still awaits is not an unretrieved error
    finally:
        if _running.get(ck) is done:
            del _running[ck]
        done.set_result(None)


async def shared_board(board: str, requested: str, build: Build) -> BoardAnswer:
    """The answer for (*board*, *requested*) from a build that starts after this call."""
    ck = (board, requested)
    joined = ck in _waiting
    if not joined:
        _waiting[ck] = asyncio.get_running_loop().create_future()
        # A task of its own: the first caller going away must not cancel the shared build.
        task = asyncio.ensure_future(_run(ck, _waiting[ck], build))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
    future = _waiting[ck]
    if not joined:
        return await asyncio.shield(future)
    try:
        answer = await asyncio.shield(future)
    except Exception:
        return await build()
    return answer if answer.status == 200 else await build()
