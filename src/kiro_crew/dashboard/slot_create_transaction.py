"""One transaction for a chat-slot create: reserve, authorize, assign, persist, publish.

``POST /api/chat/slots`` registers a slot, authorizes the request against it,
binds its member and persists its metadata before anything is broadcast. Each of
those steps can be refused after an earlier one already changed state. This
module is the journal that takes those partial changes back; the binding
restore the assign step uses lives with the rest of binding rollback in
``kiro_crew.session_agent_selection``.

A :class:`SlotCreateTransaction` keeps a journal of the steps that changed
state (today the reserve and the assign), each with the undo that restores
exactly what that step changed. Leaving the
transaction without :meth:`~SlotCreateTransaction.commit` (a refusal response,
or an exception) runs those undos in reverse order. The locks the create holds
are taken through :meth:`~SlotCreateTransaction.hold` and released only after
the rollback finishes, so a request queued on them never sees a half-undone
create. Publication (the actions registered with
:meth:`~SlotCreateTransaction.on_publish`) runs only after commit, and the
create's broadcast is flushed by the caller's slot-push suspension, which the
transaction sits inside.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from kiro_crew.dashboard.chat_utils import run_to_completion

logger = logging.getLogger(__name__)

Action = Callable[[], Awaitable[None] | None]
# Takes a step's effect out of reach without destroying it, and returns how to
# put it back.
Fence = Callable[[], Callable[[], None]]


@dataclass
class _Step:
    name: str
    undo: Action
    # Asked once, synchronously, when the rollback starts. True means the step's
    # effect now belongs to someone else and must survive the rollback.
    kept: Callable[[], bool] | None = None
    # Run synchronously, in the same step that decides the fates, for a step
    # that will be undone: nothing can start depending on its effect while the
    # newer undos suspend. What it returns reinstates the effect if a newer undo
    # fails and the step is kept after all.
    fence: Fence | None = None


async def _run(action: Action) -> None:
    result = action()
    if inspect.isawaitable(result):
        await result


class SlotCreateTransaction:
    """Journal of a create's completed steps, undone in reverse unless committed."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._journal: list[_Step] = []
        self._publish: list[tuple[str, Action]] = []
        self._held = contextlib.AsyncExitStack()
        self._settled = False

    async def hold(self, lock: AbstractAsyncContextManager[Any]) -> None:
        """Acquire *lock* until the transaction has committed or rolled back."""
        await self._held.enter_async_context(lock)

    def completed(
        self,
        name: str,
        undo: Action,
        *,
        kept: Callable[[], bool] | None = None,
        fence: Fence | None = None,
    ) -> None:
        """Record that step *name* changed state, and how to take that change back.

        *kept* says, when a rollback starts, whether the effect now belongs to
        someone else. *fence* takes the effect out of reach at that same moment
        (see :meth:`rollback`).
        """
        if self._settled:
            raise RuntimeError(
                f"{self._label}: step {name!r} recorded after the transaction settled"
            )
        self._journal.append(_Step(name, undo, kept, fence))

    def keep(self, name: str) -> None:
        """Hand step *name*'s effect over: a later rollback leaves it in place."""
        self._journal = [step for step in self._journal if step.name != name]

    async def undo(self, name: str) -> None:
        """Undo step *name* now, ahead of the rest, and drop it from the journal.

        For a refusal that takes back one step while handing others over. A
        failing undo is logged, as in :meth:`rollback`.
        """
        steps = [step for step in self._journal if step.name == name]
        self.keep(name)
        for step in reversed(steps):
            try:
                await _run(step.undo)
            except Exception:
                logger.warning("%s: could not undo step %r", self._label, step.name, exc_info=True)

    def on_publish(self, name: str, action: Action) -> None:
        """Run *action* after commit only. A rolled-back create publishes nothing."""
        self._publish.append((name, action))

    async def commit(self) -> None:
        """Make every completed step final, then run the publish actions in order.

        Publication follows a committed create, so a failing action is logged and
        the rest still run: the create exists, and an error response for it would
        be the worse answer.
        """
        if self._settled:
            return
        self._settled = True
        self._journal.clear()
        publish, self._publish = self._publish, []
        for name, action in publish:
            try:
                await _run(action)
            except Exception:
                logger.warning(
                    "%s: committed, but publish step %r failed", self._label, name, exc_info=True
                )

    async def rollback(self) -> None:
        """Undo every completed step, newest first.

        Which steps are kept is decided for all of them before the first undo
        runs, and in the same synchronous run every step that will be undone is
        fenced: its effect is taken out of reach (the newborn slot leaves the
        registry), so nothing can start depending on it while a newer undo
        suspends. A decision made here therefore stays true: a turn cannot start
        on a slot whose binding is being restored, and leave that slot running
        without the binding it was admitted under.

        An undo that fails stops the rollback: it is logged, and it and every
        older step are kept, their fences lifted, because the older steps are
        what the failed one was built on (a binding whose undo failed keeps the
        slot it belongs to rather than outliving it).
        """
        if self._settled:
            return
        self._settled = True
        self._publish.clear()
        journal, self._journal = self._journal, []
        undo = [step for step in journal if step.kept is None or not step.kept()]
        reinstate: list[tuple[str, Callable[[], None]] | None] = []
        try:
            for step in undo:
                reinstate.append((step.name, step.fence()) if step.fence is not None else None)
        except Exception:
            logger.warning(
                "%s: could not fence the rollback; keeping every step", self._label, exc_info=True
            )
            self._lift(reinstate)
            return
        for position in range(len(undo) - 1, -1, -1):
            step = undo[position]
            try:
                await _run(step.undo)
            except Exception:
                logger.warning(
                    "%s: could not undo step %r; keeping the steps before it",
                    self._label,
                    step.name,
                    exc_info=True,
                )
                self._lift(reinstate[: position + 1])
                return
            except BaseException:
                # Cancelled mid-undo: keep this step and the older ones, as for
                # a failed undo, so no fence outlives the rollback.
                self._lift(reinstate[: position + 1])
                raise

    def _lift(self, fences: list[tuple[str, Callable[[], None]] | None]) -> None:
        """Reinstate the fenced effects of steps a rollback now keeps, oldest first."""
        for entry in fences:
            if entry is None:
                continue
            name, lift = entry
            try:
                lift()
            except Exception:
                logger.warning("%s: could not reinstate step %r", self._label, name, exc_info=True)

    async def __aenter__(self) -> SlotCreateTransaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if not self._settled:
                # A cancellation that arrives while an undo is in flight (an
                # undo's worker thread finishes its restore before the await
                # raises) must not read as "that undo did not run": the rollback
                # finishes, and the cancellation is raised afterwards.
                await run_to_completion(self.rollback())
        finally:
            await self._held.aclose()
