"""Ending sessions through the lifecycle service: reset, retire, destroy, stop, close.

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: ``_lifecycle``.

Instance fields read: none.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.session_lifecycle import SessionLifecycleService

if TYPE_CHECKING:
    from kiro_crew.session import CancelOutcome, SessionManager
    from kiro_crew.session_lifecycle import TornDown
    from kiro_crew.session_parts.policy import _RecycleCallback
    from kiro_crew.session_parts.record import StopOutcome, _Session


def _lifecycle_boundary(self: SessionManager) -> SessionLifecycleService:
    service = self.__dict__.get("_lifecycle")
    if service is None:
        service = SessionLifecycleService(
            cast(Any, self),
            self._lifecycle_deps(),
            state=self._lifecycle_state_boundary(),
        )
        self.__dict__["_lifecycle"] = service
    return service


async def reset(
    self: SessionManager,
    key: str,
    *,
    expect_session: _Session | None = None,
    skip_if_busy: bool = False,
    skip_if_injecting: bool = False,
    refuse_only_on_active_turn: bool = False,
    clear_conversation: bool = False,
    ends_conversation: bool = False,
    scope: Any | None = None,
) -> bool:
    """Reset a live session while preserving its persistence entry.

    The session the reset pops stays readable through :meth:`tearing_down`
    for exactly the life of its teardown: the scope opened here records it at
    the pop and releases it when the call ends, however it ends. A caller that
    must know exactly which session THIS reset popped -- and read it
    atomically with the pop -- opens the scope itself with
    :meth:`teardown_scope` and passes it as ``scope``; it is entered and
    released here all the same, and what its ``on_pop(session)`` hook read
    off that session in the pop's lock hold survives the release.
    """
    lifecycle = self._lifecycle_boundary()
    with scope if scope is not None else lifecycle.teardown_scope() as opened:
        return await lifecycle.reset(
            key,
            expect_session=cast(Any, expect_session),
            skip_if_busy=skip_if_busy,
            skip_if_injecting=skip_if_injecting,
            refuse_only_on_active_turn=refuse_only_on_active_turn,
            clear_conversation=clear_conversation,
            ends_conversation=ends_conversation,
            scope=opened,
        )


def teardown_scope(self: SessionManager, on_pop: Callable[[Any], None] | None = None) -> Any:
    """A teardown scope to hand :meth:`reset`, with an optional hook run at its pop.

    ``on_pop(session)`` runs in the same registry-lock hold as the pop, so what
    it reads off the session is read atomically with the pop, timed-out reset
    or not. The cron reaper uses it to take the process handle of the exact
    session its reset pops.
    """
    return self._lifecycle_boundary().teardown_scope(on_pop)


def tearing_down(self: SessionManager, key: str) -> "list[TornDown]":
    """Every teardown in flight under *key*, in pop order: the popped session and the process handle read at its pop.

    The live map stops naming a session at the pop, before the teardown's
    awaits; a caller that must still reach those sessions' processes -- the
    cron reaper, after a run's own finally reset popped the session and hung,
    and after a successor's reset popped it and hung as well -- reads them
    here for exactly the life of each teardown, and kills on each entry's
    ``handle``: the identity captured at the pop, before the teardown's own
    awaits could clear the provider's pid with the process still standing.
    Empty when none is in flight.
    """
    return self._lifecycle_boundary().tearing_down(key)


def set_child_teardown_handler(self: SessionManager, handler: Any) -> None:
    """Register the hook that ends a parent's sub-agent runs at parent end.

    Called once at wiring time with the ``SubagentManager`` itself, which
    supplies both halves: a synchronous snapshot of the runs a key owns, and
    the cancellation that stops exactly those. Every parent-end path in
    :mod:`kiro_crew.session_lifecycle` drives them, so a surface that ends a
    conversation — the dashboard, a channel command, the idle sweep —
    inherits the behaviour without a call of its own.
    """
    self._lifecycle_boundary().set_child_teardown_handler(handler)


def set_recycle_callback(self: SessionManager, cb: _RecycleCallback | None) -> None:
    """Register the lifecycle recycle callback."""
    self._lifecycle_boundary().set_recycle_callback(cb)


async def _fire_recycle_callback(self: SessionManager, key: str, *, reason: str) -> None:
    """Dispatch a lifecycle recycle callback through the lifecycle boundary."""
    await self._lifecycle_boundary()._fire_recycle_callback(key, reason=reason)


async def remove(self: SessionManager, key: str) -> None:
    """Retire a session while preserving its resumable mapping."""
    await self._lifecycle_boundary().remove(key)


async def retire_kiro_identity_sessions(
    self: SessionManager, fingerprint: str = "", *, spare_children: bool = False
) -> tuple[list[str], bool]:
    """Retire idle processes that loaded a superseded Kiro identity."""
    return await self._lifecycle_boundary().retire_kiro_identity_sessions(
        fingerprint, spare_children=spare_children
    )


async def flag_identity_stamp_mismatches(self: SessionManager, live: str) -> list[str]:
    """Mark sessions whose child provably spawned under a different account."""
    return await self._lifecycle_boundary().flag_identity_stamp_mismatches(live)


async def _retire_kiro_subagent_runtimes(self: SessionManager, *, live: str = "") -> bool:
    """Retire idle companion runtimes that use Kiro's identity store.

    ``live`` spares runtimes whose spawn stamp equals it (see the sweep).
    """
    return await self._lifecycle_boundary()._retire_kiro_subagent_runtimes(live=live)


async def _retire_kiro_bg_runtime(self: SessionManager, *, live: str = "") -> bool:
    """Retire the idle shared background runtime after an identity change.

    ``live`` spares a runtime whose spawn stamp equals it (see the sweep).
    """
    return await self._lifecycle_boundary()._retire_kiro_bg_runtime(live=live)


async def remove_if_unclaimed(self: SessionManager, key: str) -> bool:
    """Remove a speculative session only before its first real claimant."""
    return await self._lifecycle_boundary().remove_if_unclaimed(key)


async def end_children_for(self: SessionManager, key: str) -> None:
    """End *key*'s sub-agent runs without tearing its process down.

    For a caller that replaces the conversation on a live process: the children of the
    conversation that ended have nowhere to report, and the process surviving does not
    change that.
    """
    return await self._lifecycle_boundary().end_children_for(key)


async def destroy(self: SessionManager, key: str) -> None:
    """Permanently destroy a session and its persistence entry."""
    await self._lifecycle_boundary().destroy(key)


async def destroy_if(
    self: SessionManager,
    key: str,
    expected_generation: int,
    should_destroy: Callable[[], bool],
    *,
    preserve_autocompact_override: bool = False,
) -> bool:
    """Destroy the captured idle generation if its slot guard stays true."""
    return await self._lifecycle_boundary().destroy_if(
        key,
        expected_generation,
        should_destroy,
        preserve_autocompact_override=preserve_autocompact_override,
    )


async def discard_conversation(
    self: SessionManager,
    key: str,
    *,
    replay: bool = True,
    skip_if_busy: bool = False,
    refuse_only_on_active_turn: bool = False,
) -> bool:
    """Drop native conversation state while retaining channel linkage.

    Returns whether a session was actually torn down; False means
    ``skip_if_busy`` refused because a turn was in flight. See
    :meth:`SessionLifecycleService.discard_conversation` for the guard's
    atomicity contract.
    """
    return await self._lifecycle_boundary().discard_conversation(
        key,
        replay=replay,
        skip_if_busy=skip_if_busy,
        refuse_only_on_active_turn=refuse_only_on_active_turn,
    )


async def drain_active_turns(self: SessionManager, timeout: float | None = None) -> int:
    """Bring unfinished native turns to a bounded safe boundary."""
    return await self._lifecycle_boundary().drain_active_turns(timeout=timeout)


async def close_all(self: SessionManager, drain_timeout: float | None = None) -> None:
    """Drain turns, persist resumable state, and close every owned runtime."""
    await self._lifecycle_boundary().close_all(drain_timeout=drain_timeout)


def absorb_orphaned_release(self: SessionManager, key: str) -> bool:
    """Whether the calling task's release belongs to a session already reset."""
    return self._lifecycle_boundary().absorb_orphaned_release(key)


def adopt_turn(self: SessionManager, key: str) -> None:
    """The calling task now holds *key*'s live permit; forget any orphan record."""
    self._lifecycle_boundary().adopt_turn(key)


# ── Cancel ──


async def cancel_current(
    self: SessionManager,
    key: str,
    *,
    wait_ack_timeout: float = 0.0,
) -> CancelOutcome:
    """Cancel the in-flight operation without destroying its session."""
    return await self._lifecycle_boundary().cancel_current(
        key,
        wait_ack_timeout=wait_ack_timeout,
    )


async def cancel_owned_turn(
    self: SessionManager,
    key: str,
    owner_task: Any,
    *,
    wait_ack_timeout: float = 0.0,
) -> CancelOutcome | None:
    """Cancel only if *owner_task* still owns the session's turn lease."""
    return await self._lifecycle_boundary().cancel_owned_turn(
        key,
        owner_task,
        wait_ack_timeout=wait_ack_timeout,
    )


async def stop_turn(
    self: SessionManager,
    key: str,
    *,
    force: bool = False,
    preserve_queue: bool = False,
    owned_by: Callable[[dict], bool] | None = None,
    on_soft: Callable[[], Awaitable[None]] | None = None,
    on_hard: Callable[[], Awaitable[None]] | None = None,
) -> StopOutcome:
    """Cooperatively stop a turn, escalating to reset and eager respawn.

    *owned_by* scopes the queue clear to one presser's entries
    (``SessionLifecycle.stop_turn``).
    """
    return await self._lifecycle_boundary().stop_turn(
        key,
        force=force,
        preserve_queue=preserve_queue,
        owned_by=owned_by,
        on_soft=on_soft,
        on_hard=on_hard,
    )


def stop_generation(self: SessionManager, key: str) -> int:
    """Monotonic count of user Stop requests recorded for *key*."""
    return self._lifecycle_boundary().stop_generation(key)


def note_stop(self: SessionManager, key: str) -> bool:
    """Record a user Stop for *key* without cancelling anything."""
    return self._lifecycle_boundary().note_stop(key)


def open_replay_gap(self: SessionManager, key: str) -> None:
    """Keep Stops recordable for *key* across a reset-then-replay window."""
    self._lifecycle_boundary().open_replay_gap(key)


def close_replay_gap(self: SessionManager, key: str) -> None:
    """End the window :meth:`open_replay_gap` opened."""
    self._lifecycle_boundary().close_replay_gap(key)


async def await_replay_gap(self: SessionManager, key: str) -> None:
    """Wait out another task's open replay gap on *key* before claiming."""
    await self._lifecycle_boundary().await_replay_gap(key)


async def _send_abort_for_session(self: SessionManager, key: str, session: Any) -> None:
    """Best-effort abort gateway work before hard session teardown."""
    await self._lifecycle_boundary()._send_abort_for_session(key, session)


async def _eager_respawn(self: SessionManager, key: str) -> None:
    """Respawn after hard teardown and release the acquired lease."""
    await self._lifecycle_boundary()._eager_respawn(key)


async def drain_all_providers(self: SessionManager) -> list:
    """Pop all registered sessions and return their providers."""
    return await self._lifecycle_boundary().drain_all_providers()
