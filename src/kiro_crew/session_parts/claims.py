"""Claiming, leasing and reading live sessions through the allocation service.

Key folding, registry reads, the per-key ending fence, ``get_or_create``, turn
admission, the circuit breaker, continuable and resumable conversations, the message
queue behind a busy turn, and the per-session approval policy.

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: ``_allocation``.

Instance fields read: none.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kiro_crew import shutdown_event
from kiro_crew.session_allocation import SessionAllocationService, _collect_parent_runtime_kwargs
from kiro_crew.session_allocation import parent_work_scratch_dir as _parent_work_scratch_dir
from kiro_crew.session_parts.policy import BACKGROUND_AGENT, BACKGROUND_KEY
from kiro_crew.start_priority import StartPriority

if TYPE_CHECKING:
    from kiro_crew.session import LLMProvider, SessionManager
    from kiro_crew.session_allocation import InboundCallbackReservation
    from kiro_crew.session_parts.record import _Session


def process_shutdown_requested(sessions: SessionManager | None) -> bool:
    if shutdown_event.is_set():
        return True
    return sessions is not None and sessions.final_drain_started is True


def _allocation_boundary(self: SessionManager) -> SessionAllocationService:
    service = self.__dict__.get("_allocation")
    if service is None:
        service = SessionAllocationService(
            cast(Any, self),
            self._allocation_deps(),
            state=self._registry_state(),
        )
        self.__dict__["_allocation"] = service
    return service


def _fold_key(self: SessionManager, key: str) -> str:
    """Resolve exact, canonical, then legacy aliases onto a live key."""
    return self._allocation_boundary()._fold_key(key)


def set_lifecycle_turn_active(self: SessionManager, key: str, active: bool) -> bool:
    """Record whether a lifecycle holder is taking a turn.

    A holder that keeps its lease across an idle life has to say when it is WORKING,
    because the pre-stream setup runs before the provider registers a turn and a probe
    reading the provider alone would tear the session down mid-setup. Returns whether a
    registered session was updated.
    """
    session = self._allocation_boundary()._sessions.get(self._fold_key(key))
    if session is None:
        return False
    session.lifecycle_turn_active = active
    return True


def mark_lifecycle_lease(self: SessionManager, key: str) -> bool:
    """Declare that this key's lease is held for a LIFETIME, not for one turn.

    A holder that keeps the lease across an idle listening life must say so, because a
    busy probe reading the lease alone would otherwise refuse every teardown on the key
    for as long as the holder exists. Returns whether a registered session was marked.
    """
    session = self._allocation_boundary()._sessions.get(self._fold_key(key))
    if session is None:
        return False
    session.lifecycle_lease = True
    return True


def has_session(self: SessionManager, key: str) -> bool:
    """Return whether a live session exists for the folded key."""
    return self._allocation_boundary().has_session(key)


def get_provider(self: SessionManager, key: str) -> LLMProvider | None:
    """Return the live provider for a folded key."""
    return self._allocation_boundary().get_provider(key)


def session_generation(self: SessionManager, key: str) -> int:
    """Capture the monotonic logical-key generation for conditional destruction."""
    return self._allocation_boundary().session_generation(key)


def _advance_session_generation(self: SessionManager, key: str) -> int:
    """Advance ownership generation after registry publication/removal."""
    return self._allocation_boundary().advance_ownership_generation(key)


def session_keys(self: SessionManager) -> frozenset[str]:
    """Snapshot current and in-flight registry keys for a same-tick fence."""
    return self._allocation_boundary().session_keys()


def _has_allocation_reservation(self: SessionManager, key: str) -> bool:
    """Return whether allocation/claim ownership is reserved for *key*."""
    return self._allocation_boundary().has_allocation_reservation(key)


def _spawn_in_flight(self: SessionManager, key: str) -> str | None:
    """Why a cold start under *key* is a process an ending caller must name, or None.

    The read a holder of the key's ending fence (:meth:`ending_key`) makes
    after its kill passes. Not every reservation is a process: a claim
    waiting on the live session's turn, or a cold start still ahead of the
    pre-spawn fence check, has started nothing and is held or refused before
    it does. What this names is a start the fence invalidated past that door
    -- its ``provider.start()`` in flight, nothing published that any map
    read could see, refused at registration and hard-killed when the start
    returns -- and a start that already returned during the passes, refused
    and hard-killed by the allocation path with an outcome nothing here reads
    back. Either is a process the holder's own passes did not answer, and the
    phrase is the reason its record carries.
    """
    return self._allocation_boundary().spawn_in_flight(key)


async def try_acquire(self: SessionManager, key: str) -> bool:
    """Try to acquire an exact-key idle session."""
    return await self._allocation_boundary().try_acquire(key)


def capability_runtime_view(
    self: SessionManager, member: str, saved_revision: str
) -> dict[str, Any]:
    """Return capability adoption without exposing mutable allocation state."""
    return self._allocation_boundary().capability_runtime_view(member, saved_revision)


def active_providers(self: SessionManager) -> list[LLMProvider]:
    """Return all currently registered providers."""
    return self._allocation_boundary().active_providers()


def any_active_turn(self: SessionManager) -> bool:
    """Return whether any provider reports an active turn."""
    return self._allocation_boundary().any_active_turn()


def get_pid(self: SessionManager, key: str) -> int | None:
    """Return the host PID for a folded session key."""
    return self._allocation_boundary().get_pid(key)


async def _reacquire_and_validate(
    self: SessionManager,
    key: str,
    sess: "_Session",
    *,
    wait_if_busy: bool = True,
    reservation: object | None = None,
) -> bool:
    """Acquire outside the registry lock, revalidate identity and meet the key's ending fence again."""
    return await self._allocation_boundary()._reacquire_and_validate(
        key,
        sess,
        wait_if_busy=wait_if_busy,
        reservation=reservation,
    )


async def _evict_stale_session(self: SessionManager, key: str, sess: "_Session") -> None:
    """Evict and close the exact stale session."""
    await self._allocation_boundary()._evict_stale_session(key, sess)


def _get_session_agent(self: SessionManager, session_key: str) -> str:
    """Return the agent recorded on an exact session key."""
    return self._allocation_boundary()._get_session_agent(session_key)


def _parent_runtime_kwargs(self: SessionManager, parent_session_key: str) -> dict:
    """Return the parent runtime security and backend posture."""
    return _collect_parent_runtime_kwargs(cast(Any, self), parent_session_key)


def parent_work_scratch_dir(self: SessionManager, parent_session_key: str) -> Path | None:
    """The ``$KIROCREW_SCRATCH`` directory of the exact parent's session tree, or None."""
    return _parent_work_scratch_dir(cast(Any, self), parent_session_key)


def is_session_sharing_eligible(self: SessionManager, parent_session_key: str) -> bool:
    """Return whether the exact parent can share a runtime."""
    return self._allocation_boundary().is_session_sharing_eligible(parent_session_key)


def aws_profile_split_possible(self: SessionManager, parent_session_key: str) -> bool:
    """Whether a subagent of the parent could bind another AWS profile; reads no path."""
    return self._allocation_boundary().aws_profile_split_possible(parent_session_key)


def project_aws_profile(self: SessionManager, cwd: str) -> str | None:
    """The profile *cwd* is bound to; resolves paths, so call it off the event loop."""
    return self._allocation_boundary().project_aws_profile(cwd)


def aws_profile_differs_from_parent(
    self: SessionManager, parent_session_key: str, child_profile: str | None
) -> bool:
    """Whether a resolved *child_profile* differs from the parent process's profile."""
    return self._allocation_boundary().aws_profile_differs_from_parent(
        parent_session_key, child_profile
    )


def runtime_pids(self: SessionManager) -> list[dict[str, object]]:
    """Return process-identity snapshots for session runtimes."""
    return self._allocation_boundary().runtime_pids()


def _append_companion_runtime_rows(self: SessionManager, rows: list[dict[str, object]]) -> None:
    """Append background and subagent runtime process rows."""
    self._allocation_boundary()._append_companion_runtime_rows(rows)


async def get_or_create(
    self: SessionManager,
    key: str,
    agent: str | None = None,
    channel_id: str | None = None,
    approval_policy: str = "",
    model: str | None = None,
    cwd: str | None = None,
    extra_env: dict[str, str] | None = None,
    speculative: bool = False,
    speculative_resume: bool = False,
    wait_if_busy: bool = True,
    _won_race_retries: int = 0,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    **extra_factory_kwargs: Any,
) -> tuple[LLMProvider, bool, bool]:
    """Claim or allocate a session and return its held lease.

    ``start_priority`` orders a cold start in the start queues; a caller passes
    FOREGROUND only where a person waits on it (rule: ``kiro_crew.start_priority``).
    """
    if key == BACKGROUND_KEY:
        # ``_bg`` is one persistent session shared by every background job,
        # so it is always the background agent, whichever caller reaches it
        # first: a caller naming its own agent (the task runner's lesson
        # extraction) gets the shared session, never a session created as
        # that agent. A live ``_bg`` already ignores the requested agent.
        agent = BACKGROUND_AGENT
        if key not in self._sessions:
            # Created on first use on a shared-runtime backend, and after a
            # failed boot start on any other: through the same path the boot
            # uses, so it keeps its start stamping.
            await self._ensure_background(start_priority=start_priority)
    return await self._allocation_boundary().get_or_create(
        key,
        agent=agent,
        channel_id=channel_id,
        approval_policy=approval_policy,
        model=model,
        cwd=cwd,
        extra_env=extra_env,
        speculative=speculative,
        speculative_resume=speculative_resume,
        wait_if_busy=wait_if_busy,
        _won_race_retries=_won_race_retries,
        start_priority=start_priority,
        **extra_factory_kwargs,
    )


@contextmanager
def ending_key(self: SessionManager, key: str) -> Iterator[None]:
    """Hold the per-key ending fence from a run's kill passes through its terminal record.

    The per-key sibling of the manager-wide closing check. While the fence is
    up, a claim or a new allocation under *key* is HELD at the front door of
    :meth:`get_or_create` -- it waits for the fence to lift, bounded by
    :data:`kiro_crew.session_allocation.ENDING_FENCE_WAIT_SECS`, then
    proceeds -- and every allocation reservation already in flight under the
    key -- a cold start caught inside ``provider.start()``, which has published
    nothing a pass could see -- is invalidated: it is refused at registration
    when it gets there, fence up or lifted, the provider it started is
    hard-killed by the closing manager's own path, and the call then waits for
    the lift and allocates again. Nothing is dropped: a sub-agent completion
    that races the reap of its parent's run lands in the session that follows
    the run's record, never in the run being ended. The cron reaper and
    ``cancel()`` open this before their reset-then-kill passes and hold it
    until the run's terminal record is persisted and audited: a held caller
    wakes to a key whose run is RECORDED, never to one that is neither being
    ended nor recorded -- the record is what the caller's own session follows.
    Synchronous: raised before the holder's first await, lifted however the
    block ends. :class:`SessionEndingError` reaches a caller only when its
    wait outlives the bound, or from ``open_task_session``, which is refused
    while the fence is up rather than held (it reserves only around its create
    on a shared runtime).
    """
    boundary = self._allocation_boundary()
    boundary.begin_ending(key)
    try:
        yield
    finally:
        boundary.end_ending(key)


def allocation_requested_model(self: SessionManager, key: str) -> str:
    """Return the model *key*'s live allocation selected, or ``""``.

    The selection a caller makes is the caller's to record. This answers the
    tier BELOW every caller: an allocation handed ``model=None`` resolves an
    id from config itself, hands it to the provider, and reports only the
    provider, ``is_new`` and ``resumed`` — so the caller's own record of what
    was asked for is blank while the session runs on a concrete model. Reading
    the stamp the allocation left is what closes that, and it is the SAME
    value the provider received, not a second resolution.

    A caller composes this OVER its own selection (``this or own``) rather
    than under it. ``is_new`` with ``resumed`` false says the caller consumed a
    fresh first-turn observation, NOT that the caller allocated the session: a
    prewarmed session that started fresh arms exactly that observation, so a
    claim of one is indistinguishable from a cold start in the return value.
    This stamp is the allocation's own selection by construction and is right
    for both cases; the caller's own resolution is right only for the cold
    start, since on a prewarmed claim it re-resolves a config that may have
    moved since the session was allocated.

    ``""`` for an unknown key, a session with no selection, and a session made
    by a registration site that resolves no models. All three are "nothing to
    report", which is what an empty value says.
    """
    session = self._sessions.get(self._fold_key(key))
    return session.requested_model if session is not None else ""


def record_effort_override(self: SessionManager, key: str, level: str) -> None:
    """Record *level*, picked on *key*'s live session, as its effort override.

    The pick supersedes the level the allocation recorded, so a restart or
    reset successor (``allocation_identity``) starts on the level the session
    runs. This record is the pick's only carrier there: only kiro-cli also
    reads a level back from the workspace ``cli.json``, and KAS and the adapter
    harnesses take theirs from the allocation alone. ``""`` (back to the
    default) drops the record. No-op if *key* is unknown.
    """
    session = self._sessions.get(self._fold_key(key))
    if session is not None:
        session.reasoning_effort_override = level or None


# ── Circuit breaker ──


def record_success(self: SessionManager, key: str) -> None:
    """Clear the folded session failure count."""
    self._allocation_boundary().record_success(key)


async def record_failure(self: SessionManager, key: str) -> bool:
    """Record a failure and apply the circuit breaker."""
    return await self._allocation_boundary().record_failure(key)


def reserve_inbound_callback(self: SessionManager) -> InboundCallbackReservation | None:
    """Claim one callback before any pre-turn command or card handling."""
    return self._allocation_boundary().reserve_inbound_callback()


def begin_turn(self: SessionManager, key: str) -> None:
    """Apply the yield-free pre-dispatch closing gate."""
    self._allocation_boundary().begin_turn(key)


async def pause_turn_admission_for_update(self: SessionManager) -> bool:
    """Block new turns for update apply without overriding real shutdown.

    Refused once a gateway stop is signalled: an apply admitted then would
    be stopped mid-write by the shutdown that follows. Checked under the
    lock, so a stop landing while the lock is contended is still seen.
    """
    async with self._lock:
        if shutdown_event.is_set():
            return False
        if self._closing and not self._update_pause_owned:
            return False
        self._closing = True
        self._update_pause_owned = True
        self.update_restart_fenced = False
        return True


def fence_update_restart(self: SessionManager) -> bool:
    """Commit inline refusal persistence before the final restart drain."""
    if not self._closing or not self._update_pause_owned:
        return False
    self.update_restart_fenced = True
    return True


async def resume_turn_admission_after_update(self: SessionManager) -> bool:
    """Release this caller's temporary update pause, if it still owns it.

    Returns True only when admission actually reopened. Kept once a gateway
    stop is signalled: the shutdown stops the update first, so this runs at
    its start, and a paused admission keeps refusing (and spooling) inbound
    turns that reopened admission would admit only for the teardown to
    cancel. Checked under the same lock as the release, so a stop landing
    while the lock is contended is still seen.
    """
    async with self._lock:
        if not self._update_pause_owned or shutdown_event.is_set():
            return False
        self.update_restart_fenced = False
        self._update_pause_owned = False
        self._closing = False
        return True


# ── Per-session semaphore ──


def mark_continuable(self: SessionManager, key: str) -> None:
    """Mark a folded conversation as continuable."""
    self._allocation_boundary().mark_continuable(key)


def unmark_continuable(self: SessionManager, key: str) -> None:
    """Remove a folded continuable mark."""
    self._allocation_boundary().unmark_continuable(key)


def set_continuable_fallback(self: SessionManager, fn: Callable[[str], bool] | None) -> None:
    """Set the disk-truth continuable fallback."""
    self._allocation_boundary().set_continuable_fallback(fn)


def _is_continuable_key(self: SessionManager, folded: str) -> bool:
    """Resolve a folded continuable key through cache and fallback."""
    return self._allocation_boundary()._is_continuable_key(folded)


def is_continuable(self: SessionManager, key: str) -> bool:
    """Return whether a folded conversation is continuable."""
    return self._allocation_boundary().is_continuable(key)


def resumable_sid(self: SessionManager, key: str) -> str | None:
    """Return the persisted resume SID for a folded key."""
    return self._allocation_boundary().resumable_sid(key)


def resumable_hint(self: SessionManager, key: str) -> bool:
    """Return whether a folded key has resumable state."""
    return self._allocation_boundary().resumable_hint(key)


def mapped_sid(self: SessionManager, key: str) -> str:
    """The session ID a folded key maps to, in memory, without pruning.

    For a caller recording HISTORY rather than deciding a resume. Use
    :meth:`resumable_sid` for the latter: its file check is what makes the
    answer a resumable session, and this one deliberately omits it.
    """
    return self._allocation_boundary().mapped_sid(key)


def allocation_predecessor(self: SessionManager, key: str) -> str:
    """The store *key*'s current cold-started session superseded, or ``""``.

    Captured by the allocation boundary inside the registration's own critical
    section (see :meth:`SessionAllocationService.allocation_predecessor`), so a
    caller consumes it AFTER ``get_or_create`` returns instead of reading the
    mapping around its own call -- the read that a concurrent turn's
    allocate-and-recycle can stale while the caller waits inside the allocation.
    The ``previous_sid`` source for ``crew_log_emit.on_session_opened``.
    """
    return self._allocation_boundary().allocation_predecessor(key)


def mapped_session_keys(self: SessionManager) -> frozenset[str]:
    """Every folded key this gateway holds a session ID for, in memory.

    The in-memory answer, which is the only correct one for a caller that
    goes on to WRITE through this manager: a detached ``SessionMap`` reads
    the file, and the file lags this map by whatever it has not flushed, so
    an enumeration taken there can omit a key whose pointer already exists.
    Pair it with :meth:`session_keys` to cover a key whose allocation is
    still in flight and has not reached the map yet.
    """
    return self._allocation_boundary().mapped_session_keys()


def seed_conversation(
    self: SessionManager, key: str, sid: str, *, provider: str = "", cwd: str = ""
) -> None:
    """Seed a persisted conversation mapping."""
    self._allocation_boundary().seed_conversation(key, sid, provider=provider, cwd=cwd)


def forget_conversation(self: SessionManager, key: str) -> str | None:
    """Delete a persisted conversation and continuable mark."""
    return self._allocation_boundary().forget_conversation(key)


def forget_conversation_if_sid(
    self: SessionManager, key: str, expected_sid: str
) -> tuple[bool, str | None]:
    """Delete a persisted conversation only if its SID still matches."""
    return self._allocation_boundary().forget_conversation_if_sid(key, expected_sid)


def conversation_provider(self: SessionManager, key: str) -> str:
    """Return the persisted provider label for a folded key."""
    return self._allocation_boundary().conversation_provider(key)


def release(self: SessionManager, key: str, *, cleanup: bool = False) -> None:
    """Release the key-based session lease."""
    self._allocation_boundary().release(key, cleanup=cleanup)


async def _safe_cleanup(self: SessionManager, provider: LLMProvider, session_id: str) -> None:
    """Best-effort cleanup of provider session files."""
    await self._allocation_boundary()._safe_cleanup(provider, session_id)


# ── Message queue (Slack thread serialization) ──


def is_busy(self: SessionManager, key: str) -> bool:
    """Return whether a folded session lease is held."""
    return self._allocation_boundary().is_busy(key)


def touch(self: SessionManager, key: str) -> bool:
    """Refresh a folded live session timestamp."""
    return self._allocation_boundary().touch(key)


def enqueue(
    self: SessionManager, key: str, msg_ts: str, text: str, *, force: bool = False, **kwargs: object
) -> bool:
    """Queue a message behind a folded session turn."""
    return self._allocation_boundary().enqueue(key, msg_ts, text, force=force, **kwargs)


def dequeue(self: SessionManager, key: str) -> tuple[str, str, dict] | None:
    """Pop the next non-cancelled queued message."""
    return self._allocation_boundary().dequeue(key)


def cancel_queued(self: SessionManager, key: str, msg_ts: str) -> bool:
    """Remove or mark one queued message as cancelled."""
    return self._allocation_boundary().cancel_queued(key, msg_ts)


def is_cancelled(self: SessionManager, key: str, msg_ts: str) -> bool:
    """Consume a queued-message cancellation marker."""
    return self._allocation_boundary().is_cancelled(key, msg_ts)


def detach_queue(self: SessionManager, key: str) -> tuple[Any, ...]:
    """Take the queued entries out of the live queue, keeping their files."""
    return self._allocation_boundary().detach_queue(key)


def restore_queue(self: SessionManager, key: str, entries: tuple[Any, ...]) -> None:
    """Put ``detach_queue``'s entries back at the head of the queue."""
    self._allocation_boundary().restore_queue(key, entries)


def clear_queue(
    self: SessionManager,
    key: str,
    owned_by: Callable[[dict], bool] | None = None,
    *,
    only: tuple[Any, ...] | None = None,
) -> None:
    """Clear queued messages and their temporary paths.

    *only* narrows the clear to the handles ``detach_queue`` returned, so a
    Stop drops what was queued when it was pressed and nothing admitted since.

    *owned_by* narrows the clear to the entries it selects, for a caller acting for
    ONE principal rather than for the whole session: under
    ``messaging.dm_scope = "unified"`` every allow-listed person's direct messages
    share one key and therefore one queue, so clearing all of it on one person's
    ``/stop`` discards messages other people are still waiting for an answer to. The
    predicate reads a queue entry's keyword arguments and nothing here interprets
    them, so which fields name a principal stays with the channels that wrote them
    (``messaging/queue_drain.py``).

    Omitted, the whole queue goes, which is what a whole-session request means:
    teardown, a generation bump, a fresh conversation.
    """
    self._allocation_boundary().clear_queue(key, owned_by, only=only)


async def is_provider_alive(self: SessionManager, key: str) -> bool | None:
    """Probe a folded session provider outside the registry lock."""
    return await self._allocation_boundary().is_provider_alive(key)


def get_approval_policy(self: SessionManager, key: str) -> str:
    """Return a folded session approval policy."""
    return self._allocation_boundary().get_approval_policy(key)


def get_agent(self: SessionManager, key: str) -> str:
    """Return a folded session agent name."""
    return self._allocation_boundary().get_agent(key)


def get_agent_selection(self: SessionManager, key: str) -> tuple[str, str]:
    """Snapshot the allocation-owned namespace and name for inheritance."""
    return self._allocation_boundary().get_agent_selection(key)


def set_approval_policy(self: SessionManager, key: str, policy: str) -> None:
    """Update a folded session approval policy with audit logging."""
    self._allocation_boundary().set_approval_policy(key, policy)
