"""Shared plumbing for composed SubagentManager coordinators."""

from __future__ import annotations

from types import FunctionType
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    import asyncio

    from ..subagent import SubagentManager


class ManagerComponent:
    """Hold the facade that owns all mutable manager state."""

    _manager: SubagentManager
    __slots__ = ("_manager",)

    def __init__(self, manager: SubagentManager) -> None:
        object.__setattr__(self, "_manager", manager)

    def _hold_announce_task(self, key: str, task: asyncio.Task[None]) -> None:
        """Register a one-shot announce *task* in ``_tasks`` until it finishes.

        The entry is what lets shutdown (``cancel_all``) cancel and await an
        announce still in flight. Once the task is done the entry has no use, so
        the done callback drops it, but only while *key* still maps to this task.
        """
        self._manager._tasks[key] = task

        def _forget(done: asyncio.Task[None]) -> None:
            if self._manager._tasks.get(key) is done:
                del self._manager._tasks[key]

        task.add_done_callback(_forget)

    def _record_crew_log_approval_decided(
        self,
        origin: "tuple[str, int]",
        *,
        approval_id: str,
        decision: str,
        by: str = "",
        cause: str = "",
    ) -> None:
        """Write how *approval_id* resolved, under its own request's origin.

        Shared by every component that asks a human to approve something: the
        spawn gate's prompt before a child starts, and the run coordinator's
        prompts raised by a child already running. Nothing in the body is
        specific to either -- it files a decision under an origin -- so one copy
        lives here and the askers differ only in how they obtain that origin.

        *origin* is what the matching request writer returned, so a request that
        was not written answers itself with nothing and a written one is always
        answered: the pair is all-or-nothing by construction rather than by two
        separate checks at each call site. Re-reading the emitter's pin here
        would not do, because the paths that drop or open a pin on their way out
        would leave the closer with nothing to file itself under.

        ``by`` names WHO decided, and only the host can be named with certainty.
        An answer that came back through an approval future or callback was given
        by a person at a surface no call site can see, so it omits the field
        rather than asserting ``user`` for something it did not observe. The host
        IS named for the exits no answer reached.

        ``cause`` is WHY, and only a host decline has one: a reason code for
        deciding without a human. It rides in its own field instead of replacing
        ``decision``, so a reader still learns what was decided without knowing
        the reason vocabulary.

        Every name is imported inside the body on purpose. A component method
        that does NOT end in ``_impl`` keeps its defining module's globals, where
        a facade import may exist only under ``TYPE_CHECKING``.
        """
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.subagent import logger as _logger

        sid, asked_turn = origin
        if not sid:
            return
        try:
            crew_log_emit.on_approval_decided(
                sid,
                asked_turn,
                approval_id=approval_id,
                decision=decision,
                by=by,
                cause=cause,
            )
        except Exception:
            _logger.debug("crew log: recording an approval decision failed", exc_info=True)


def bind_component_globals(
    component_types: Iterable[type[ManagerComponent]], namespace: dict[str, Any]
) -> None:
    """Bind implementations to ``subagent`` globals for patch compatibility.

    A rebound function keeps its own code but runs on ``namespace``, so an import at the
    top of its defining module is inert for it. Every global it loads must resolve in
    ``namespace`` -- add the name there, or import it inside the function.
    """
    for component_type in component_types:
        for name, implementation in tuple(vars(component_type).items()):
            if not name.endswith("_impl") or not isinstance(implementation, FunctionType):
                continue
            if implementation.__globals__ is namespace:
                continue
            rebound = FunctionType(
                implementation.__code__,
                namespace,
                implementation.__name__,
                implementation.__defaults__,
                implementation.__closure__,
            )
            rebound.__kwdefaults__ = implementation.__kwdefaults__
            rebound.__annotations__ = implementation.__annotations__
            rebound.__dict__.update(implementation.__dict__)
            rebound.__doc__ = implementation.__doc__
            rebound.__module__ = implementation.__module__
            rebound.__qualname__ = implementation.__qualname__
            setattr(component_type, name, rebound)


def copy_component_docs(
    facade_type: type[Any], component_types: Iterable[type[ManagerComponent]]
) -> None:
    """Keep the facade's runtime method documentation intact."""
    for component_type in component_types:
        for name, implementation in vars(component_type).items():
            if not name.endswith("_impl") or not isinstance(implementation, FunctionType):
                continue
            facade_method = getattr(facade_type, name.removesuffix("_impl"))
            facade_method.__doc__ = implementation.__doc__
