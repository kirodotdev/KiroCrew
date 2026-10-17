"""A key's turn across a Stop or a reset.

The per-key Stop record a turn runner reads at its end-of-turn gates, the replay gap that
holds a key while a turn resets its session and replays on the successor, the orphaned
permit a popped session's holder still owes, ``cancel_current`` / ``cancel_owned_turn``,
``stop_turn``'s cooperative stop with its escalation to abort, reset and respawn, and the
successor respawns. A new rule for what a Stop does to a running turn goes here.

Instance fields written: none.

Instance fields read: ``_deps``, ``_owner``, ``state``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from kiro_crew.messaging.link import canonical_key
from kiro_crew.session_lifecycle_parts.parked_queue import _park_queue
from kiro_crew.session_lifecycle_parts.stop_decline import StopOutcome, _compaction_waits_for_turn

if TYPE_CHECKING:
    from kiro_crew.session_lifecycle import SessionLifecycleService


CancelOutcome = Literal["acked", "timeout", "no_turn", "error"]


@dataclass(slots=True)
class _ReplayGap:
    """One open reset-to-reacquire window; see ``replay_gaps``."""

    owner: asyncio.Task[Any] | None
    closed: asyncio.Event = field(default_factory=asyncio.Event)


# Named by the path callers import it from, which is what tracebacks and
# reprs render as ``module.qualname``.
_ReplayGap.__module__ = "kiro_crew.session_lifecycle"


def stop_generation(self: SessionLifecycleService, key: str) -> int:
    """How many Stop requests have been recorded for *key*.

    Monotonic per folded key; 0 for a key never stopped. A turn runner
    snapshots this at turn start and treats any later change as a user
    Stop, whichever surface issued it. Recorded by :meth:`stop_turn` and by
    :meth:`note_stop`, which the channel stop paths that cancel the provider
    directly call instead.
    """
    return self.state.stop_requests.get(self._stop_bucket(key), 0)


def _stop_bucket(self: SessionLifecycleService, key: str) -> str:
    """The ``stop_requests`` entry *key* reads and writes.

    The folded live key when a session exists -- the same bucket the
    sibling per-key dicts use -- else the canonical key. Folding needs a
    live owner to resolve aliases, so during a replay gap a Slack Stop
    issued under the bare thread ts and the turn reading under
    ``slack:<ts>`` would otherwise land in two different buckets.
    """
    folded = self._owner._fold_key(key)
    if folded in self._owner._sessions:
        return folded
    return canonical_key(key)


def note_stop(self: SessionLifecycleService, key: str) -> bool:
    """Record one user Stop against *key*; True when it was recorded.

    Recorded when the key has a live session, or while it sits in a replay
    gap (:meth:`open_replay_gap`). A Stop on a key that has neither is not
    recorded, so the per-key dict grows only with conversations that exist.
    Recording happens before anything is awaited on every caller, so a
    turn's end-of-turn gates can see the Stop as soon as it was issued.
    """
    bucket = self._stop_bucket(key)
    if bucket not in self._owner._sessions and bucket not in self.state.replay_gaps:
        return False
    self.state.stop_requests[bucket] = self.state.stop_requests.get(bucket, 0) + 1
    return True


def open_replay_gap(self: SessionLifecycleService, key: str) -> None:
    """Hold *key* for the calling task across a reset-to-reacquire window.

    Called by a turn that is about to reset its session and replay the
    same message on the successor; paired with :meth:`close_replay_gap`
    once that turn has settled and released its permit. While open,
    Stops for the key stay recordable (:meth:`note_stop`) and every OTHER
    task's ``get_or_create`` for the key waits (:meth:`await_replay_gap`).
    Reopening an open gap keeps the existing record.
    """
    gap_key = canonical_key(key)
    if gap_key not in self.state.replay_gaps:
        self.state.replay_gaps[gap_key] = _ReplayGap(owner=asyncio.current_task())


def close_replay_gap(self: SessionLifecycleService, key: str) -> None:
    """End the window :meth:`open_replay_gap` opened. Idempotent.

    Only the task that opened the gap can close it this way. The pipelines
    close from a ``finally`` that runs on EVERY turn for the key, so a
    concurrent turn on the same key (a hook auto-reply that never acquires
    a session, an interaction-originated Slack turn) would otherwise end the
    owner's gap early and let a newer message claim the successor ahead of
    the replay. Teardown paths and ``close_all`` use the forced discard.
    """
    gap = self.state.replay_gaps.get(canonical_key(key))
    if gap is None:
        return
    if gap.owner is not None and gap.owner is not self._calling_task():
        return
    self._discard_replay_gap(canonical_key(key))


def _discard_replay_gap(self: SessionLifecycleService, gap_key: str) -> None:
    gap = self.state.replay_gaps.pop(gap_key, None)
    if gap is not None:
        gap.closed.set()


def _orphan_turn_holder(self: SessionLifecycleService, key: str, session: Any) -> None:
    """Remember the task holding a popped session's permit; see ``orphaned_holders``."""
    holder = getattr(session, "turn_owner", None)
    if holder is None or not getattr(session, "semaphore").locked():
        return
    self.state.orphaned_holders.setdefault(canonical_key(key), set()).add(holder)


def _calling_task() -> asyncio.Task[Any] | None:
    """The current task, or None off the loop (``release`` is also called
    synchronously from non-async code, where nothing can be orphaned)."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def absorb_orphaned_release(self: SessionLifecycleService, key: str) -> bool:
    """True when the calling task's release is owed to a session already reset.

    The permit that task held died with the popped session; letting the
    key-only release through would unlock whatever now occupies the key --
    a successor mid-turn -- so the release is consumed here instead.
    """
    holders = self.state.orphaned_holders.get(canonical_key(key))
    task = self._calling_task()
    if not holders or task is None or task not in holders:
        return False
    holders.discard(task)
    if not holders:
        self.state.orphaned_holders.pop(canonical_key(key), None)
    return True


def adopt_turn(self: SessionLifecycleService, key: str) -> None:
    """The calling task acquired *key*'s live permit; its release is genuine.

    A turn that reset its own session and then reacquired (a replay) owes
    the successor exactly the release it will make, so the orphan record
    from the reset must not swallow it.
    """
    holders = self.state.orphaned_holders.get(canonical_key(key))
    task = self._calling_task()
    if holders and task is not None:
        holders.discard(task)
        if not holders:
            self.state.orphaned_holders.pop(canonical_key(key), None)


def _wake_turn_waiters(session: Any) -> None:
    """Let tasks parked on a just-popped session's turn permit move on.

    A claimant that found the key busy waits on ``session.semaphore``
    (``_reacquire_and_validate``). Popping the session from the registry
    does not wake it: the permit stays held by the turn that is being torn
    down, and that turn's own release lands on the SUCCESSOR (``release``
    folds the key), so the waiter would hang until a restart. Releasing the
    popped permit once wakes the first waiter, whose re-validation sees the
    session is gone and re-enters the claim; if more are queued, its own
    release wakes the next. Called in the same tick as the pop, so the
    woken task's validation cannot observe the session still registered.
    A permit nobody holds has nobody waiting on it and is left alone.
    """
    semaphore = getattr(session, "semaphore", None)
    if semaphore is not None and semaphore.locked():
        semaphore.release()


async def await_replay_gap(self: SessionLifecycleService, key: str) -> None:
    """Wait out an open replay gap on *key*, unless this task owns it.

    The allocation path calls this before it claims a session, so a
    message arriving while an older one is between its reset and its
    replay claims the successor AFTER the replay has run and released it.
    The owning task passes straight through: its own successor claims are
    the replay.
    """
    gap = self.state.replay_gaps.get(canonical_key(key))
    if gap is None or gap.owner is asyncio.current_task():
        return
    await gap.closed.wait()


async def cancel_current(
    self: SessionLifecycleService,
    key: str,
    *,
    wait_ack_timeout: float = 0.0,
) -> CancelOutcome:
    """Cancel the in-flight operation without destroying its session."""
    owner = self._owner
    key = owner._fold_key(key)
    session = owner._sessions.get(key)
    if not session:
        return "no_turn"
    outcome = await session.provider.cancel(wait_ack_timeout=wait_ack_timeout)
    self._deps.logger.info("Cancelled in-flight operation for %s: %s", key, outcome)
    return outcome


async def cancel_owned_turn(
    self: SessionLifecycleService,
    key: str,
    owner_task: Any,
    *,
    wait_ack_timeout: float = 0.0,
) -> CancelOutcome | None:
    """Cancel only when *owner_task* still owns this session's turn lease."""
    owner = self._owner
    key = owner._fold_key(key)
    session = owner._sessions.get(key)
    if session is None or session.turn_owner is not owner_task:
        return None
    # The Python owner can still be finishing transcript teardown after the
    # native prompt reached its done boundary. Prove that state independently
    # before arming replay or sending session/cancel; ``no_turn`` alone is not
    # proof because a cancel request can make a still-unfinished turn inactive.
    # Unknown providers stay on the fail-closed cancellation path.
    if self._deps.provider_turn_is_known_terminal(session.provider):
        self._deps.logger.info(
            "Owner-fenced turn already terminal for %s; native cancel skipped",
            key,
        )
        return None
    # No await occurs between the owner check and entering provider.cancel,
    # so a successor cannot acquire this lease before the native request
    # starts. The provider object remains the exact target even if teardown
    # later replaces the registry entry under the folded key.
    # ACP discards cancelled turns from its native transcript. Arm replay
    # before the first await so an acknowledgement cannot release this lease
    # to a successor before the interrupted context is marked. On an
    # unacknowledged outcome this is deliberately fail-closed: the warning
    # forbids resume, and a forced successor still receives the context
    # instead of silently losing it.
    session.prev_turn_cancelled = True
    outcome = await session.provider.cancel(wait_ack_timeout=wait_ack_timeout)
    self._deps.logger.info(
        "Cancelled owner-fenced in-flight operation for %s: %s",
        key,
        outcome,
    )
    return outcome


async def stop_turn(
    self: SessionLifecycleService,
    key: str,
    *,
    force: bool = False,
    preserve_queue: bool = False,
    owned_by: Callable[[dict], bool] | None = None,
    on_soft: Callable[[], Awaitable[None]] | None = None,
    on_hard: Callable[[], Awaitable[None]] | None = None,
) -> StopOutcome:
    """Cooperatively stop a turn, escalating to reset and eager respawn.

    *owned_by* narrows the queue clear to the entries it selects, for a Stop
    pressed by ONE member of a shared session: every member of a Slack thread
    queues under that thread's key, so a whole-queue clear drops messages the
    others are still owed an answer to. It runs at the same point as the
    whole-queue clear, before the cancel, so the cancelled turn's end-of-turn
    drain cannot start a message this Stop was aimed at. Ignored with
    *preserve_queue*, which clears nothing.
    """
    owner = self._owner
    logger = self._deps.logger
    key = owner._fold_key(key)
    session = owner._sessions.get(key)
    # BEFORE the Stop is recorded: a Stop this method declines is not a Stop
    # the turn saw, and recording it would make the compaction path below
    # read its own later failure as user-cancelled. ``getattr`` because the
    # owner protocol does not declare the compacting set; a double without it
    # keeps the old behaviour. Not while the compaction still waits for the
    # turn permit: the turn holding it owns the session, and this Stop is aimed
    # at that turn (the same answer ``compaction_in_flight`` gives).
    if (
        not force
        and session is not None
        and key in getattr(owner, "_compacting", ())
        and not _compaction_waits_for_turn(owner, key)
    ):
        logger.info("stop_turn outcome=compacting session=%s (cooperative stop declined)", key)
        return "compacting"
    # Record the Stop against the session key before anything is awaited:
    # the runner's end-of-turn gates may run as soon as the provider's
    # cancel lands, and `prev_turn_cancelled` (set only after the ack) is
    # too late for them. Before the idle return too: a key inside a replay
    # gap has no session yet still owes the record, or the replay that
    # follows would run the prompt this Stop was aimed at.
    self.note_stop(key)
    if not session:
        return "idle"

    if not preserve_queue:
        owner.clear_queue(key, owned_by)
    budget: float = owner._cfg.agent.soft_stop_budget_secs
    t0 = self._deps.monotonic()

    if not force:
        outcome = await session.provider.cancel(wait_ack_timeout=budget)
        logger.debug("stop_turn: provider.cancel outcome=%r for %s", outcome, key)
        if outcome == "acked":
            elapsed = self._deps.monotonic() - t0
            logger.info(
                "stop_turn outcome=soft-acked session=%s elapsed=%.2fs",
                key,
                elapsed,
            )
            # The native harness discards cancelled turns from its log; the
            # next prompt must therefore re-inject the cancelled context.
            session.prev_turn_cancelled = True
            if on_soft:
                try:
                    await on_soft()
                except Exception:
                    logger.warning("on_soft hook failed for %s", key, exc_info=True)
            return "soft"
        if outcome == "no_turn":
            logger.info("stop_turn outcome=idle session=%s (no active turn)", key)
            return "idle"
        logger.info(
            "stop_turn outcome=escalated-to-hard session=%s " "cancel_result=%r elapsed=%.2fs",
            key,
            outcome,
            self._deps.monotonic() - t0,
        )

    # Abort pooled gateway work before killing the owning provider.
    await owner._send_abort_for_session(key, session)
    if preserve_queue:
        # ``reset`` pops the session and unlinks whatever its queue still
        # holds. A caller that asked to keep the queue has usually detached
        # it already, but an entry admitted between that detach and this
        # line sits on the session now: parked here, so the successor
        # handoff (``hand_queue_to_successor``) delivers it too.
        late = tuple(session.queue)
        if late:
            session.queue.clear()
            _park_queue(owner, key, late)
    await owner.reset(key)
    elapsed = self._deps.monotonic() - t0
    logger.info(
        "stop_turn outcome=hard-done session=%s elapsed=%.2fs",
        key,
        elapsed,
    )
    # Retain the task strongly until completion; the event loop alone keeps
    # only a weak reference.
    task = asyncio.create_task(owner._eager_respawn(key))
    owner._background_tasks.add(task)
    task.add_done_callback(owner._background_tasks.discard)
    if on_hard:
        try:
            await on_hard()
        except Exception:
            logger.warning("on_hard hook failed for %s", key, exc_info=True)
    return "hard"


async def _send_abort_for_session(self: SessionLifecycleService, key: str, session: Any) -> None:
    """Best-effort gateway abort for the runtime serving ``key``.

    The provider answers with an opaque target or with ``None``, and this
    layer never opens it. Holding one already means the address is complete
    and routable, so there is nothing here to validate and no pid to read:
    the runtime is one process that may serve several sessions, and a session
    able to name that process could attribute it to itself.
    """
    logger = self._deps.logger
    try:
        target = session.provider.runtime_abort_target()

        if target is not None:
            # Audit at the decision point: downstream logging happens only
            # if the fire-and-forget gateway abort eventually succeeds. The
            # target names itself, so the record says which runtime was asked
            # without this layer deriving it.
            try:
                self._deps.get_audit_logger().log_api_access(
                    caller="session",
                    operation="mcp-gateway.abort-initiated",
                    outcome="initiated",
                    source="session",
                    resources=f"{target.audit_label} session={key}",
                    error="reason=hard-stop",
                )
            except Exception:  # pragma: no cover - audit cannot block kill
                logger.debug("SEL audit for abort initiation failed", exc_info=True)
            self._deps.schedule_runtime_abort(
                target,
                reason=f"hard-stop session={key}",
            )
        else:
            logger.warning(
                "abort-push skipped for %s: the provider named no reachable "
                "runtime — in-flight tool calls will not be cancelled",
                key,
            )
    except Exception:
        logger.debug("_send_abort_for_session failed for %s", key, exc_info=True)


async def _eager_respawn(self: SessionLifecycleService, key: str) -> None:
    """Respawn after hard kill and release its acquired turn semaphore.

    A start that succeeds here also delivers what a forced channel stop parked:
    ``get_or_create`` adopts it at registration or claim, and the ``release``
    wakes the adopted entries' channel drains.
    """
    try:
        await self._owner.get_or_create(key)
        self._owner.release(key)
    except Exception:
        self._deps.logger.debug("Eager respawn failed for %s", key, exc_info=True)


async def _respawn_as(self: SessionLifecycleService, key: str, identity: dict[str, Any]) -> None:
    """Start a successor for *key* under *identity* and release its lease.

    The reset's counterpart of :meth:`_eager_respawn`: registration adopts the
    entries the reset parked, and the release wakes their channels' drains.
    The successor's first-turn observation belongs to the first queued turn,
    not to this call: a non-speculative creator registers its session with
    nothing armed and keeps the observation for itself, so it is re-armed
    here from the flags the start returned. A failed native resume then
    still reads as a fresh start and the queued turn replays the history.
    """
    owner = self._owner
    try:
        provider, is_new, resumed = await owner.get_or_create(key, **identity)
    except Exception:
        self._deps.logger.debug("Reset respawn failed for %s", key, exc_info=True)
        return
    try:
        live = owner._sessions.get(key)
        constants = self._deps.constants()
        if (
            is_new
            and live is not None
            and live.provider is provider
            and live.first_turn is constants.first_turn_nothing_armed
        ):
            live.first_turn = (
                constants.first_turn_resumed if resumed else constants.first_turn_fresh
            )
    finally:
        owner.release(key)
