"""``_bounded_turn`` must survive being finalized outside its own context.

The wrapper publishes the turn's deadline on a ``ContextVar`` so anything
running inside the turn can size its waits against the budget that is actually
left. Restoring that with a ``Token`` assumes the ``finally`` runs in the
context that entered the ``try`` — and for an ABANDONED wrapper it does not.

A gateway shutdown that closes the loop with a turn still pending, or a
coroutine that is never awaited, leaves the coroutine object to the garbage
collector, which throws ``GeneratorExit`` in from whatever context is current
when it runs. ``Token.reset`` refuses that with ``ValueError: ... was created in
a different Context``, and because it raises inside ``__del__`` it is
*unraisable*: no caller can catch it, it is only printed. Under pytest each one
becomes a ``PytestUnraisableExceptionWarning``, and enough of them take an xdist
worker down with them.

``ContextVar.set`` has no such affinity, so save-and-restore is correct in both
directions: it restores properly on the normal path, and a restore that lands in
a foreign context during finalization writes to a context that is about to be
discarded.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from kiro_crew.dashboard.turn_dispatch import _TURN_DEADLINE, _bounded_turn


async def _never() -> None:
    await asyncio.Event().wait()


def _step_into_the_turn(wrapper):
    """Advance *wrapper* past the deadline publish, to its first suspension."""
    wrapper.send(None)


class TestFinalizationOutsideTheOwningContext:
    @pytest.mark.asyncio
    async def test_closing_an_abandoned_wrapper_from_another_context_is_quiet(self):
        """The exact shape the collector produces, made deterministic.

        Stepped far enough to publish the deadline in THIS context, then closed
        from a different ``contextvars.Context`` — which is what a GC pass
        running on an unrelated stack does.
        """
        wrapper = _bounded_turn(_never(), timeout_secs=30.0)
        _step_into_the_turn(wrapper)

        foreign = contextvars.copy_context()
        try:
            foreign.run(wrapper.close)  # must not raise
        finally:
            await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_an_abandoned_dispatched_turn_leaves_the_caller_clean(self):
        """Production shape: ``spawn_guarded_turn`` dispatches the wrapper as a
        TASK, so its publish and its restore both happen in the task's own
        context copy. Cancelling and dropping it must raise nothing and must
        leave the dispatching context untouched — the next turn dispatched here
        would otherwise inherit a budget that is already spent."""
        assert _TURN_DEADLINE.get() is None

        task = asyncio.ensure_future(_bounded_turn(_never(), timeout_secs=30.0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _TURN_DEADLINE.get() is None

    @pytest.mark.asyncio
    async def test_closing_in_the_owning_context_still_works(self):
        """The ordinary abandonment, for symmetry: same coroutine, closed where
        it was started."""
        wrapper = _bounded_turn(_never(), timeout_secs=30.0)
        _step_into_the_turn(wrapper)

        wrapper.close()
        await asyncio.sleep(0)

        assert _TURN_DEADLINE.get() is None


class TestTheDeadlineContractIsUnchanged:
    @pytest.mark.asyncio
    async def test_the_deadline_is_published_for_the_turn(self):
        """What the ContextVar exists for: code inside the turn can read it."""
        seen: list[float | None] = []

        async def _observe() -> str:
            seen.append(_TURN_DEADLINE.get())
            return "done"

        result = await _bounded_turn(_observe(), timeout_secs=30.0)

        assert result == "done"
        assert seen and seen[0] is not None

    @pytest.mark.asyncio
    async def test_a_completed_turn_does_not_leak_its_deadline(self):
        """A direct ``await _bounded_turn(...)`` shares the caller's context, so
        a spent deadline left behind would starve the next turn dispatched
        there."""
        assert _TURN_DEADLINE.get() is None

        await _bounded_turn(asyncio.sleep(0), timeout_secs=30.0)

        assert _TURN_DEADLINE.get() is None

    @pytest.mark.asyncio
    async def test_a_nested_wrapper_restores_the_outer_turns_deadline(self):
        """Save-and-restore must put back what was there, not merely clear.

        A bare ``set(None)`` would pass the leak test above while silently
        blanking an enclosing turn's budget.
        """
        outer_seen: list[float | None] = []

        async def _outer() -> None:
            published = _TURN_DEADLINE.get()
            assert published is not None
            await _bounded_turn(asyncio.sleep(0), timeout_secs=5.0)
            outer_seen.append(_TURN_DEADLINE.get())
            assert _TURN_DEADLINE.get() == published

        await _bounded_turn(_outer(), timeout_secs=30.0)

        assert outer_seen and outer_seen[0] is not None
        assert _TURN_DEADLINE.get() is None

    @pytest.mark.asyncio
    async def test_the_ceiling_still_fires(self):
        """The wrapper's actual job, unchanged by the restore mechanism."""
        with pytest.raises(TimeoutError):
            await _bounded_turn(_never(), timeout_secs=0.01)
