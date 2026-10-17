"""Lost-run bounds shared by the tests that drive a real auto-improvement member run.

The work before a member's provider is reached is config loads, sqlite commits and
fsyncs: ~0.3s on a quiet host, and 18-44s per test call on a Windows CI shard whose
four workers ran these tests together. None of these tests is about how long that
takes, so every bound here is a LOST-RUN guard. It turns a hang into a failure that
names itself before pytest's ``--timeout`` kills the worker, and it never decides
pass versus fail.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from time import monotonic as _monotonic
from typing import Any

from kiro_crew.testing.wait import await_until, default_timeout

#: The budget a test hands one member assignment. It stays below the lost-run bound
#: (:func:`kiro_crew.testing.wait.default_timeout`, half of ``--timeout``), so a slow
#: assignment ends with the runner's own timeout before the test's wait gives up.
MEMBER_BUDGET_SECS = 45

#: What a wait keeps over the runner budget it hands out, so the runner's own timeout
#: ends the assignment and unwinds before the test's wait gives up.
_UNWIND_SECS = 5

#: What a sized deadline keeps below pytest's ``--timeout`` (twice the lost-run bound)
#: for teardown and the failure message.
_TEARDOWN_SECS = 10


class MemberDeadline:
    """One lost-run deadline for a whole test, shared by every wait in it.

    Each wait gets the time left, never a fresh bound, and never more than one
    lost-run bound (:func:`default_timeout`): two waits that each took a full bound
    add up to pytest's ``--timeout``, so a hang would kill the worker instead of
    failing by name. Create one before the test's first member work and pass it to
    every wait. A test that runs ``assignments`` member assignments gets one more
    :data:`MEMBER_BUDGET_SECS` per further assignment, capped below ``--timeout``,
    so a slow setup on each one is not squeezed into a single bound.
    """

    def __init__(self, assignments: int = 1) -> None:
        if assignments < 1:
            raise ValueError("a member deadline covers at least one assignment")
        bound = default_timeout()
        total = bound + (assignments - 1) * MEMBER_BUDGET_SECS
        if assignments > 1:
            total = min(total, 2 * bound - _TEARDOWN_SECS)
        self._end = _monotonic() + total

    def remaining(self) -> float:
        """Seconds left before the test's lost-run bound, never negative."""
        return max(0.0, self._end - _monotonic())

    def _window(self) -> float:
        """What one wait may take: the time left, at most one lost-run bound."""
        return min(self.remaining(), default_timeout())

    def budget(self) -> float:
        """The runner budget for the next assignment: it ends before the wait does."""
        return min(MEMBER_BUDGET_SECS, max(0.0, self._window() - _UNWIND_SECS))

    async def wait(self, signal: Any, what: str) -> Any:
        """:func:`await_until` on ``signal`` in this wait's window."""
        return await await_until(signal, timeout=self._window(), what=what)

    @contextlib.asynccontextmanager
    async def settles(
        self, task: Any, what: str, *, stop: Callable[[], None]
    ) -> AsyncIterator[None]:
        """Call ``stop`` and wait out ``task`` in the time left when the block exits.

        When the block raised, that failure is the one reported: a cleanup wait that
        also runs out, or the task's own error, is suppressed, because it only restates
        that the run went wrong.
        """
        try:
            yield
        except BaseException:
            stop()
            with contextlib.suppress(Exception):
                await self.wait(task, what)
            raise
        stop()
        await self.wait(task, what)
