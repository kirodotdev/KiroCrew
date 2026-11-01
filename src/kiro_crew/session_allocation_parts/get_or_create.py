"""``SessionAllocationService.get_or_create``: claim the live session for a key, or cold-start one.

``get_or_create`` holds a request at a fenced key's door, then runs
``_get_or_create_impl``: claim the live session when one is registered
(``_reacquire_and_validate``, evicting a dead one with ``_evict_stale_session``), or
reserve the key, take a cold-start permit (``_held_start_permit``), claim a warm-pool
provider or start a new one on the resolved model and AWS profile, and register it --
refusing the registration, and hard-killing the started provider
(``_dispatch_hard_kill``), when the manager closed or a fence caught the start. The
reservation is removed on every exit (``_remove_reservation_now``,
``_remove_reservation_cancellation_drained``).

Each function here is a ``SessionAllocationService`` member: its body is the method's,
its first parameter is ``self`` (``_is_member_key`` is a static method), and the class
binds it by name, so the instance and its registry state stay on the class. This module
never imports :mod:`kiro_crew.session_allocation` at run time.

Instance fields written: ``_allocation_reservations``, ``_sessions``, ``_starting_pids``.

Instance fields read: ``_closing``, ``_deps``, ``_lock``, ``_owner``, ``_start_sem``, ``state``.

Through ``state`` it also changes the ending fence's and the capability records in place
(``spawning_reservations``, ``invalidated_reservations``, ``refused_spawns``,
``capability_failures``) and reads ``ending_keys``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_AT_LAUNCH
from kiro_crew.agent_sdk.kas_floor_wait import await_kas_floor_verdict
from kiro_crew.aws_project_profiles import (
    AWS_PROFILE_ENV,
    bound_profile_env,
    configured_project_profiles,
)
from kiro_crew.kiro_prerequisite import pre_spawn_identity, spawn_pid, stamp_spawn_identity
from kiro_crew.metrics.sessions import (
    END_REASON_EVICTED,
    discard_session_start,
    record_session_ended,
    record_session_started,
)
from kiro_crew.runtime_ownership import acquire_session_lease, release_session_lease
from kiro_crew.session_allocation_parts.errors import (
    SessionBusyError,
    SessionClosingError,
    SessionEndingError,
    SpeculativeResumeRefused,
)
from kiro_crew.session_lifecycle import adopt_parked_queue
from kiro_crew.start_priority import (
    START_QUEUE_COLD_START,
    START_QUEUE_LOG_MIN_MS,
    StartPriority,
    notify_start_queue,
)

if TYPE_CHECKING:
    from kiro_crew.session_allocation import LLMProvider, ProviderFactory, SessionAllocationService


def _model_is_a_launch_argument(provider: Any) -> bool:
    """Whether *provider*'s harness fixes its model at launch and refuses a switch."""
    backend = getattr(getattr(provider, "client", None), "backend", None)
    return isinstance(backend, str) and backend in ACP_BACKENDS_MODEL_AT_LAUNCH


async def _session_runs_remotely(session_key: str) -> bool:
    """Whether this session is bound to a remote session environment.

    Fails closed: an unreadable choice record answers True, so the session
    cold-starts and the runtime seam reports the error instead of a warm local
    engine quietly serving it.
    """
    from kiro_crew.session_environment import session_runs_remotely

    return await asyncio.to_thread(session_runs_remotely, session_key)


async def _reacquire_and_validate(
    self: SessionAllocationService,
    key: str,
    session: Any,
    *,
    wait_if_busy: bool = True,
    reservation: object | None = None,
) -> bool:
    """Acquire with the global lock released, then validate exact identity -- and meet the key's ending fence again.

    The semaphore wait below can last a whole turn. A fence that rose
    while this claimant waited is met HERE, under the lock, before the
    session is handed back: the busy turn's ordinary release can hand the
    semaphore to this waiter before the ending caller's reset pops the
    session, and the claim would otherwise run under a key being ended and
    be torn down by that reset mid-turn. ``reservation`` is the caller's
    allocation token, so a fence that rose and lifted during the wait
    (which invalidates the token) refuses too; the refusal releases the
    semaphore and ``get_or_create`` turns it into a wait for the lift and a
    fresh allocation. ``open_task_session`` passes no token and is refused
    while the fence is up, as at its entry.
    """
    if not wait_if_busy and session.semaphore.locked():
        raise SessionBusyError(key)
    # An idle Semaphore(1) acquires without suspension, so this is the
    # authoritative non-waiting claim boundary after the locked check.
    await session.semaphore.acquire()
    # Named before the lock wait below: a reset that pops this session
    # during that wait reads ``turn_owner`` to learn whose permit it
    # releases, and from here on that is this task's.
    session.turn_owner = asyncio.current_task()
    try:
        async with self._lock:
            self._refuse_if_ending(key, reservation)
            still_valid = (
                self._sessions.get(key) is session
                and not session.retire_on_identity_change
                and self._deps.provider_effectively_alive(session.provider)
            )
    except BaseException:
        # The held-semaphore contract was never returned to the caller
        # (a fence refusal above included).
        self._release_reacquired(key, session)
        raise
    if not still_valid:
        self._release_reacquired(key, session)
    return still_valid


def _release_reacquired(self: SessionAllocationService, key: str, session: Any) -> None:
    """Give back the permit :meth:`_reacquire_and_validate` took, unless a teardown already did.

    A teardown that pops the session while this task waits for the lock records
    this task as the permit's holder and releases the permit to wake the
    claimants parked on it (``session_lifecycle.retire_turn_permit``), so a
    second release here would over-release it. Only a popped session can
    carry that record, so a session still registered is always released.
    """
    # The holding ends here whichever branch gives the permit back, so the record
    # goes first: a teardown that pops the session later must not name this task
    # as the holder of a permit it has given back (or one already handed to the
    # next claimant), or it would release that permit a second time.
    if session.turn_owner is asyncio.current_task():
        session.turn_owner = None
    if self._sessions.get(key) is not session and self._owner.absorb_orphaned_release(key):
        return
    session.semaphore.release()


async def _evict_stale_session(self: SessionAllocationService, key: str, session: Any) -> None:
    """Pop only the observed stale object and close it outside the lock."""
    dead: LLMProvider | None = None
    async with self._lock:
        if self._sessions.get(key) is session:
            del self._sessions[key]
            self.advance_ownership_generation(key)
            dead = session.provider
            # Same lock hold as the removal, before any await: a turn still
            # holding this permit is recorded and its parked claimants woken,
            # exactly as every lifecycle teardown verb does.
            self._owner.retire_turn_permit(key, session)
            # Same tick as the removal. Left unrecorded, the start crumb
            # survives and the next boot calls this a crash.
            await record_session_ended(key, end_reason=END_REASON_EVICTED)
    if dead is not None:
        await asyncio.to_thread(self._deps.unlink_session_queue, session)
        try:
            await dead.shutdown()
        except Exception:
            self._deps.logger.warning(
                "Failed to shut down stale provider for %s", key, exc_info=True
            )


def _is_member_key(key: str) -> bool:
    """Whether *key* addresses a crew member's pinned DM session.

    Wrapper so the pool-bypass arm stays readable and the import stays off
    module top level (circular import: members' module graph is heavy and
    imports config, which sits below this module).
    """
    from kiro_crew.members import is_member_session_key

    return is_member_session_key(key)


async def _crew_pins_effort(
    self: SessionAllocationService, agent: str | None, crew_agent: object
) -> bool:
    """True when the crew this session runs as pins its own reasoning effort.

    Read off the event loop: ``load_config`` parses (and deep-copies, even on
    a cache hit) the whole config, which is not work to do inline. Only the
    warm-pool decision calls this, so the cost lands once per cold start
    rather than once per turn, and never on a session that was already
    skipping the pool for a cheaper reason.

    Failure answers False -- the pre-field behaviour. An unreadable config
    must not stop a session from starting, and pooling it is only wrong for a
    crew that pins an effort, which is exactly what could not be read.
    """
    try:
        config = await asyncio.to_thread(self._deps.load_config)
        crew = crew_agent if isinstance(crew_agent, str) else None
        return bool(config.crew_pinned_effort(agent, crew))
    except Exception:
        self._deps.logger.warning(
            "Could not read the crew effort pin for agent=%r; pooling as before",
            agent,
            exc_info=True,
        )
        return False


def _dispatch_hard_kill(self: SessionAllocationService, provider: LLMProvider) -> None:
    """Dispatch blocking provider teardown away from the event-loop thread."""
    kill = self._deps.get_sync_kill_provider()
    try:
        asyncio.get_running_loop().run_in_executor(
            self._deps.get_subprocess_executor(),
            kill,
            provider,
        )
    except RuntimeError:
        # During executor shutdown, a daemon thread is safer than running
        # waitpid/taskkill inline and wedging the event loop.
        threading.Thread(target=kill, args=(provider,), daemon=True).start()


def _remove_reservation_now(self: SessionAllocationService, key: str, token: object) -> None:
    """Remove a token in the yield-free span after a successful claim."""
    # A fence's invalidation lives exactly as long as the reservation it
    # invalidated: the door checks have run by the time the token is removed.
    self.state.invalidated_reservations.discard(token)
    # A start still marked past its spawn door when its reservation goes is
    # one the body refused or that raised, its provider hard-killed by
    # dispatch. While the key's fence is up, the ending caller has not read
    # the key yet: leave it a receipt, or the cleanup erases the process
    # before the caller can name it. After the lift the caller's record is
    # written and nothing can consume a receipt.
    if token in self.state.spawning_reservations and key in self.state.ending_keys:
        self.state.refused_spawns[key] = self.state.refused_spawns.get(key, 0) + 1
    self.state.spawning_reservations.discard(token)
    fence = self.state.ending_keys.get(key)
    if fence is not None:
        fence.discard(token)
    reservations = self._allocation_reservations.get(key)
    if reservations is not None and token in reservations:
        reservations.remove(token)
        self.advance_ownership_generation(key)
        if not reservations:
            self._allocation_reservations.pop(key, None)


async def _remove_reservation_cancellation_drained(
    self: SessionAllocationService, key: str, token: object
) -> None:
    """Remove one failed/cancelled reservation despite caller cancellation."""

    async def remove() -> None:
        async with self._lock:
            self._remove_reservation_now(key, token)

    cleanup = asyncio.create_task(remove())
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = exc
    await cleanup
    if cancellation is not None:
        raise cancellation


@contextlib.asynccontextmanager
async def _held_start_permit(
    self: SessionAllocationService,
    key: str,
    priority: StartPriority,
    *,
    on_queued: Callable[..., None] | None,
    on_acquired: Callable[..., None] | None,
) -> AsyncIterator[None]:
    """Hold a ``_start_sem`` permit at *priority* for one cold start.

    ``on_queued`` / ``on_acquired`` are a subagent run's start-clock callbacks
    (``on_gate_queued`` / ``on_gate_acquired``), fired with
    :data:`START_QUEUE_COLD_START` so its watchdog clock pauses while it waits
    here, as it does at the provider's admission and ``session/new`` queues.
    """
    sem = self._start_sem
    notify_start_queue(self._deps.logger, on_queued, START_QUEUE_COLD_START)
    started = time.monotonic()
    async with sem.held(priority):
        wait_ms = (time.monotonic() - started) * 1000.0
        notify_start_queue(self._deps.logger, on_acquired, wait_ms, START_QUEUE_COLD_START)
        if wait_ms >= START_QUEUE_LOG_MIN_MS:
            self._deps.logger.info(
                "session_cold_start stage=queue_wait priority=%s wait_ms=%.1f key=%s %s",
                priority.value,
                wait_ms,
                key,
                sem.describe(),
            )
        yield


async def get_or_create(
    self: SessionAllocationService,
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
    """Reserve logical ownership for the complete claim/allocation call, held while the key is being ended."""
    # One wait budget for the whole call (``wait_for_ending_fence``): a fence
    # that goes up again after a retry below does not restart it.
    fence_deadline: float | None = None
    while True:
        # An older message between its reset and its replay holds the key: a
        # claim made now would run -- and persist -- ahead of it. Waited out
        # BEFORE the reservation so the ownership generation does not move for
        # a claimant that has not been admitted yet; the replay's own task
        # passes.
        await self._owner.await_replay_gap(key)
        token = object()
        held: bool
        async with self._lock:
            if self._closing:
                raise SessionClosingError(
                    "SessionManager is closing (gateway restart/shutdown in "
                    "progress); refusing to start or resume a turn"
                )
            reserved_key = self._owner._fold_key(key)
            # The per-key sibling of the closing check: a key whose run is
            # being ended admits no claim and no cold start until its record
            # is written. The caller is HELD, not refused -- it takes no
            # reservation (the ending caller's post-pass read must not count
            # it), waits outside the lock, and comes back to this door.
            held = reserved_key in self.state.ending_keys
            if not held:
                self._allocation_reservations.setdefault(reserved_key, set()).add(token)
                self.advance_ownership_generation(reserved_key)
        if held:
            fence_deadline = await self.wait_for_ending_fence(reserved_key, fence_deadline)
            continue
        try:
            result = await self._get_or_create_impl(
                reserved_key,
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
                _reservation=token,
                start_priority=start_priority,
                **extra_factory_kwargs,
            )
        except SessionEndingError:
            # The key was fenced while this allocation was in flight: a door
            # of the body refused it -- at registration, with the provider
            # it started already hard-killed by the body's own handler, or
            # earlier, before anything was started. Nothing of it landed.
            # The request is not dropped: wait for the fence to lift and
            # allocate again under the recorded key, as a caller that met
            # the fence at the front door does.
            await self._remove_reservation_cancellation_drained(reserved_key, token)
            fence_deadline = await self.wait_for_ending_fence(reserved_key, fence_deadline)
            continue
        except BaseException:
            await self._remove_reservation_cancellation_drained(reserved_key, token)
            raise
        self._remove_reservation_now(reserved_key, token)
        # This task now holds the key's live permit. If it reset its previous
        # session on this key (a replay), the release it will make is for THIS
        # permit and must not be swallowed as the old one's.
        self._owner.adopt_turn(reserved_key)
        return result


def _remember_capability_failure(
    self: SessionAllocationService, key: str, preparation: Any
) -> None:
    # Only closed error vocabulary reaches the owner API; provider errors
    # can contain transport credentials. Successful retry removes this row.
    failures = self.state.capability_failures
    failures.pop(key, None)
    failures[key] = {
        "member": preparation.member,
        "status": "failed",
        "saved_revision": preparation.revision,
        "error_code": "capability_startup_failed",
    }
    if len(failures) > 128:
        failures.pop(next(iter(failures)))


async def _get_or_create_impl(
    self: SessionAllocationService,
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
    _reservation: object | None = None,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    **extra_factory_kwargs: Any,
) -> tuple[LLMProvider, bool, bool]:
    """Claim a live session or cold-start one, returning its held lease.

    The returned tuple is ``(provider, is_new, resumed)``.  A successful
    return always owns the session semaphore and must be paired with
    ``release``.  First-turn observation is consumed only by the real
    claimant that actually wins that semaphore. ``_reservation`` is the
    allocation reservation token ``get_or_create`` took for this call; the
    ending fence (:meth:`_refuse_if_ending`) reads it at every door below.
    """
    owner = self._owner
    constants = self._deps.constants
    key = owner._fold_key(key)
    # The app that owns this session (a dashboard app slot), an input to the
    # session's own model resolution only. Taken out of the factory kwargs so
    # no provider factory is handed a key it does not know; each retry below
    # hands it back.
    app_name = str(extra_factory_kwargs.pop("app_name", "") or "")
    # The app whose governance profile the session runs under, for a caller
    # (a subagent's spawn) that must carry an app's governance without taking
    # its model. Defaults to ``app_name``: an app slot's own session is both.
    owning_app = str(extra_factory_kwargs.pop("owning_app", "") or "") or app_name
    # The app launch copy kiro-cli is asked for in place of ``agent``
    # (``apps.agent_launch``); ``agent`` stays the session's identity. Stamped
    # on the cold-started provider below, never handed to a factory.
    app_launch_agent = str(extra_factory_kwargs.pop("app_launch_agent", "") or "")
    from kiro_crew.execution_context import read_session_execution

    execution = await asyncio.to_thread(read_session_execution, key)
    member_context = execution is not None and execution.member_id is not None
    memory_mode = execution.memory_mode if execution is not None else "persistent"
    stale_provider: LLMProvider | None = None
    stale_session: Any | None = None
    claimed: Any | None = None
    factory: ProviderFactory
    try:
        async with self._lock:
            if self._closing:
                raise SessionClosingError(
                    "SessionManager is closing (gateway restart/shutdown in "
                    "progress); refusing to start or resume a turn"
                )
            self._refuse_if_ending(key, _reservation)

            existing = self._sessions.get(key)
            recycling = existing is not None and owner._recycling.get(key) is existing
            if existing is not None and not recycling:
                session = existing
                if getattr(session.provider, "memory_mode", "persistent") != memory_mode:
                    raise RuntimeError(
                        "This conversation's privacy mode changed; open a new conversation"
                    )
                alive = session.provider.is_process_alive()
                if not alive:
                    if (
                        self._deps.is_claude_provider(session.provider)
                        and session.provider.connection_mode == "per_session"
                    ):
                        self._deps.logger.info(
                            "Session %s CC process dead — will reconnect on next stream()",
                            key,
                        )
                        alive = True
                    else:
                        self._deps.logger.warning(
                            "Session %s has dead provider — removing stale entry",
                            key,
                        )
                        stale_provider = session.provider
                        stale_session = session
                        del self._sessions[key]
                        self.advance_ownership_generation(key)
                        # Same lock hold as the removal, before any await: a
                        # turn still holding the dead session's permit is
                        # recorded and its parked claimants woken, as in
                        # ``_evict_stale_session``.
                        owner.retire_turn_permit(key, session)
                        # Same tick as the removal. Left unrecorded, the
                        # start crumb survives and the next boot calls this
                        # a crash rather than an eviction.
                        await record_session_ended(key, end_reason=END_REASON_EVICTED)
                if alive:
                    session.last_used = time.monotonic()
                    if (
                        self._deps.is_claude_provider(session.provider)
                        and session.provider.session_id
                        and not owner._session_map.get(key)
                    ):
                        owner._session_map.set(
                            key,
                            session.provider.session_id,
                            provider=constants.provider_label_claude,
                            cwd=session.provider.cwd,
                        )
                    # The semaphore may be held for a full turn; claim it
                    # only after releasing the global registry lock.
                    claimed = session

            if claimed is None:
                if not owner._provider_factory:
                    raise RuntimeError("No provider factory configured")
                factory = owner._provider_factory
    finally:
        if stale_provider is not None:
            if stale_session is not None:
                await asyncio.to_thread(self._deps.unlink_session_queue, stale_session)
            try:
                await stale_provider.shutdown()
            except Exception:
                self._deps.logger.warning(
                    "Failed to shut down stale provider for %s",
                    key,
                    exc_info=True,
                )

    if claimed is not None:
        session = claimed
        if await owner._reacquire_and_validate(
            key,
            session,
            wait_if_busy=wait_if_busy,
            reservation=_reservation,
        ):
            first_turn = session.first_turn
            if not speculative:
                session.first_turn = self._deps.first_turn_nothing_armed
            # A session that registered before a forced stop parked entries
            # for this key adopted nothing at its registration; the claim
            # that holds its lease now is the moment they can land. The
            # release that ends this claim wakes their channels' drains.
            if adopt_parked_queue(session, key):
                session.adopted_parked = True
            return session.provider, first_turn.is_new, first_turn.resumed
        await owner._evict_stale_session(key, session)
        # Re-enter the claim rather than cold-start in place. The session
        # this claimant waited on was replaced or retired under it -- a reset
        # wakes its waiters exactly so they get here -- and the key may now
        # hold a successor, or sit inside a replay gap that must be waited
        # out; only the front door sees either. Bounded like the won-race
        # retry it mirrors. A key that simply has no session any more takes
        # the same cold start it would have taken here, one hop later.
        maximum = constants.won_race_max_retries
        if _won_race_retries >= maximum:
            raise RuntimeError(
                f"get_or_create({key!r}) exceeded {maximum} won-race retries — "
                "session kept going stale between acquire and re-validate"
            )
        return await owner.get_or_create(
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
            _won_race_retries=_won_race_retries + 1,
            start_priority=start_priority,
            app_name=app_name,
            app_launch_agent=app_launch_agent,
            owning_app=owning_app,
            **extra_factory_kwargs,
        )

    resume_sid: str | None = None
    # A rewind's (source sid, fork point): restores history like a resume, so
    # it is gated, placed and kept off the warm pool like one.
    native_fork: tuple[str, str] | None = None
    is_stateless = (
        key in (constants.background_key, constants.heartbeat_key)
        or key in constants.stateless_keys
        or any(key.startswith(prefix) for prefix in constants.stateless_prefixes)
    ) and not owner._is_continuable_key(key)
    if not is_stateless:
        resume_sid = owner._session_map.get(key)
        if not resume_sid:
            native_fork = owner._session_map.get_native_fork(key)
    if speculative and (resume_sid or native_fork) and not speculative_resume:
        raise SpeculativeResumeRefused(key)

    from kiro_crew.session_capabilities import prepare_runtime

    effective_cwd = cwd
    if not effective_cwd and (resume_sid or native_fork):
        stored_cwd = owner._session_map.get_cwd(key)
        if stored_cwd and await asyncio.to_thread(Path(stored_cwd).is_dir):
            effective_cwd = stored_cwd
    claim_crew = extra_factory_kwargs.get("crew_agent")
    session_agent = agent
    preparation = await asyncio.to_thread(prepare_runtime, agent, claim_crew, effective_cwd)
    if preparation.revision:
        # The factory may retain the pre-reconciliation config snapshot.
        # Pass the prepared template explicitly, keeping the member namespace.
        agent = preparation.template
        effective_cwd = preparation.project or effective_cwd
        extra_factory_kwargs["crew_agent"] = preparation.member

    # The caller's own map, recorded on the session and passed to a won-race
    # retry: a successor's allocation and the retry apply the binding again.
    caller_env = extra_env
    # A project bound to an AWS profile (aws.project_profiles) starts its
    # agent with AWS_PROFILE set, unless the caller chose a profile itself.
    # Set before the pool decision so a bound session never takes a pooled
    # process spawned without it. An empty map leaves extra_env untouched.
    project_profiles = configured_project_profiles(owner._cfg)
    # Stamped on the session this call registers, so a subagent asking to
    # share its process can compare against the profile the process holds.
    bound_profile: str | None = None
    if project_profiles and effective_cwd:
        bound_env = await asyncio.to_thread(
            bound_profile_env, project_profiles, effective_cwd, extra_env
        )
        if bound_env is not extra_env and bound_env is not None:
            bound_profile = bound_env[AWS_PROFILE_ENV]
            # The profile NAME only: the binding never holds a credential.
            self._deps.logger.info(
                "session %s: AWS profile %r from aws.project_profiles",
                key,
                bound_profile,
            )
        extra_env = bound_env

    # Reconciliation can publish a new template model. Resolve only after
    # that boundary, while retaining an explicit caller model unchanged.
    if model is None:

        def resolve_model() -> str | None:
            cfg = self._deps.load_config() if preparation.revision else owner._cfg
            selected = preparation.member or agent
            return self._deps.session_model(
                cfg,
                selected,
                claim_crew,
                app_name=app_name,
                session_key=key,
                backend_override=extra_factory_kwargs.get("backend_override"),
            )

        model = await asyncio.to_thread(resolve_model)

    self._deps.logger.info(
        "Pool decision: key=%s resume_sid=%s model=%s agent=%s "
        "pool_size=%d pool_qsize=%d cwd=%s pool_cwd=%s",
        key,
        resume_sid,
        model,
        agent,
        owner._pool_size,
        owner._warm_pool.qsize(),
        cwd,
        owner._pool_cwd,
    )
    provider_switched = False
    cwd_blocks_pool = bool(cwd and cwd != owner._pool_cwd)
    if not owner._pool_size:
        pool_decision = "disabled"
    elif preparation.revision:
        pool_decision = "bypass_member_capabilities"
    elif resume_sid or native_fork:
        pool_decision = "bypass_resume"
    elif is_stateless:
        pool_decision = "bypass_stateless"
    elif self._is_member_key(key) or _is_crewmate_worker_key(key):
        # A pooled child was spawned with no session key, so it runs the
        # factory's DEFAULT backend and none of the member construction
        # route (per-session dispatch-tool mount, member backend). A warm
        # hit would silently hand a member DM a session that cannot mount
        # its tools; cold-starting through the factory is what makes the
        # member route real. String check — as cheap as the arms above. A
        # crewmate's own worker takes the member backend too, so it bypasses
        # for the same reason (an in-memory set lookup).
        pool_decision = "bypass_member"
    elif app_launch_agent:
        # A pooled child was spawned as the pool's agent, never as this copy.
        pool_decision = "bypass_app_launch"
    elif memory_mode != "persistent":
        pool_decision = "bypass_restricted_context"
    elif member_context:
        # Native launch documents are captured before session creation.
        # An already launched generic pool cannot supply that receipt.
        pool_decision = "bypass_member_context"
    elif cwd_blocks_pool:
        pool_decision = "bypass_cwd"
    elif extra_env:
        pool_decision = "bypass_env"
    elif extra_factory_kwargs.get("shared_scratch") is not None:
        # A dedicated subagent joining its parent's session tree needs the
        # parent's work directory MOUNTED, and a pooled child's mounts were
        # fixed when it was pre-spawned with no parent. Cold-starting is what
        # makes ``$KIROCREW_SCRATCH`` name the same place as the parent's.
        pool_decision = "bypass_shared_scratch"
    elif extra_factory_kwargs.get("backend_override") is not None:
        # An explicit backend (a sub-agent spawned on another harness). A
        # pooled child was spawned on the factory's DEFAULT backend -- a
        # different process, which no post-claim switch can change -- so a
        # warm hit would run the work on the wrong harness. ``is not None``:
        # ``""`` is a Kiro request, and under a non-Kiro default the pooled
        # child is on the wrong backend for it too.
        pool_decision = "bypass_backend_override"
    elif await _session_runs_remotely(key):
        # A pooled child is a LOCAL engine: claiming it would run a remote session here.
        pool_decision = "bypass_remote_environment"
    elif await self._crew_pins_effort(agent, extra_factory_kwargs.get("crew_agent")):
        # A CREW's pinned effort is fixed at spawn time and the warm-pool
        # claim path never re-pushes it, so a warm hit would silently run
        # this crew at the wrong depth.  Cold-starting is what makes the
        # pin real.  (Caller-supplied reasoning_effort_override is handled
        # post-claim via provider.change_effort instead — see below.)
        #
        # Last in the chain on purpose: it is the only arm that needs to
        # read config, so every cheaper reason to skip the pool is settled
        # first and a bypassing session never pays for the lookup.
        pool_decision = "bypass_effort"
    else:
        pool_decision = ""

    pooled = None if pool_decision else await owner._drain_and_claim(agent)
    if not pool_decision:
        pool_decision = "hit" if pooled is not None else "miss_empty"
    if pooled is None:
        owner._record_pool_decision(pool_decision, key)
    # Assigned by the cold-start branch inside its semaphore section (the
    # spawn-identity stamp read there must already run under the shield);
    # the pool-claim branch leaves it None and is shielded after the
    # branches converge below.
    starting_pid: int | None = None
    if pooled is not None:
        provider = pooled
        launch_pin = False
        # A warm-pool claim owns a LIVE process from this line on -- the pool
        # path's spawn door. Marked exactly like a cold start past its
        # pre-spawn check, so a claim the ending fence refuses at registration
        # (its provider hard-killed there) leaves the same receipt for the
        # ending caller's read, and is never a process the record forgets.
        if _reservation is not None:
            self.state.spawning_reservations.add(_reservation)
        cast(Any, provider).memory_mode = memory_mode
        if self._deps.is_acp_provider(provider):
            cast(Any, provider).member_context = member_context
            # Trusted: the caller's slot app, never transcript metadata.
            cast(Any, provider).owning_app = owning_app
        try:
            if self._deps.is_acp_provider(provider):
                claim_kwarg = extra_factory_kwargs.get("crew_agent")

                def resolve_claim_watchdog() -> tuple[str, object]:
                    # Resolve from a fresh config off-loop.  AcpClient.rekey
                    # resets prompt cost/context state while rebinding the
                    # handle and watchdog to the claiming crew.
                    config = self._deps.load_config()
                    crew = self._deps.resolve_crew_identity(
                        config,
                        agent,
                        None if claim_kwarg is None else str(claim_kwarg),
                    )
                    return crew, self._deps.load_watchdog_settings(crew)

                claim_crew, claim_watchdog = await asyncio.to_thread(resolve_claim_watchdog)
                cast(Any, provider).client.rekey(
                    key,
                    channel_id,
                    crew_agent=claim_crew,
                    watchdog=claim_watchdog,
                )
                if model:
                    # A cache miss walks the agents directory and reads specs
                    # until the pool agent matches: filesystem work, off the
                    # loop like the config load above.
                    pool_model = (
                        await asyncio.to_thread(owner._resolve_agent_model, owner._pool_agent)
                        if owner._pool_agent
                        else None
                    )
                    if pool_model:
                        try:
                            advertised = self._deps.advertised_model_ids(
                                provider.available_models()
                            )
                        except Exception:  # pragma: no cover - defensive
                            advertised = []
                        _namespace = self._deps.provider_model_namespace(provider)
                        _foreign_scope = not self._deps.model_pin_applies(
                            model,
                            _namespace,
                            advertised,
                        )
                    if self._deps.is_claude_backend(provider):
                        switch_model = self._deps.to_provider_id(model, "claude_code")
                        comparable_pool = (
                            self._deps.to_provider_id(pool_model, "claude_code")
                            if pool_model
                            else pool_model
                        )
                    else:
                        switch_model = self._deps.to_acp_id(model)
                        comparable_pool = (
                            self._deps.to_acp_id(pool_model) if pool_model else pool_model
                        )
                    if pool_model and _foreign_scope:
                        # Harness ownership and account entitlement are
                        # separate decisions. A foreign pin inherits this
                        # harness's current pooled model.
                        self._deps.logger.info(
                            "Pool post-claim: model %s belongs to another harness, "
                            "not %s; leaving the claimed process on %s",
                            model,
                            _namespace,
                            pool_model,
                        )
                    elif pool_model and switch_model != comparable_pool:
                        _send_model = switch_model
                        if advertised and self._deps.model_is_unusable(switch_model, advertised):
                            # A literal miss can be a stale `<namespace>::`
                            # qualifier on a model the backend fully serves:
                            # resolve to the advertised spelling and
                            # send THAT — the same fold the cold-start spawn
                            # and the display verdict use, so a warm claim
                            # runs exactly what a cold start of the same pin
                            # runs. A pin absent under either spelling still
                            # takes the withhold below.
                            _send_model = self._deps.resolve_pin_spelling(
                                switch_model, advertised, provider
                            )
                        if not _send_model:
                            self._deps.logger.warning(
                                "Pool post-claim: model %s is not available to this "
                                "account; leaving the claimed process on %s",
                                switch_model,
                                pool_model,
                            )
                        elif _model_is_a_launch_argument(provider):
                            # The harness takes its model only at launch and
                            # refuses a live switch: this claim cannot honour
                            # the pin, so the chat cold-starts on it instead.
                            launch_pin = True
                        else:
                            await cast(Any, provider).client.set_model(_send_model)
                            self._deps.logger.info(
                                "Pool post-claim: switched model to %s",
                                _send_model,
                            )
                _effort_override = extra_factory_kwargs.get("reasoning_effort_override")
                if _effort_override and not launch_pin:
                    _eff = str(_effort_override)
                    try:
                        if not await provider.change_effort(_eff):
                            _cur_model = getattr(cast(Any, provider).client, "_model", None) or ""
                            self._deps.logger.warning(
                                "reasoning effort '%s' will not be applied (session %s) — "
                                "model '%s' does not support effort configuration",
                                _eff,
                                key or "?",
                                _cur_model or "auto",
                            )
                        else:
                            _cur_model = getattr(cast(Any, provider).client, "_model", None) or ""
                            self._deps.logger.info(
                                "Pool post-claim: applied reasoning effort %s to model %s",
                                _eff,
                                _cur_model,
                            )
                    except Exception:
                        self._deps.logger.warning(
                            "Pool post-claim: failed to apply reasoning effort '%s' (session %s)",
                            _eff,
                            key or "?",
                            exc_info=True,
                        )
            if launch_pin:
                # Same teardown as any failed claim, then the cold start below. The
                # reservation stays in the spawning set the claim put it in, so an
                # ending caller's read still sees a process in flight; the cold
                # start's own fence check and cleanup take it from there exactly as
                # for a fresh spawn.
                owner._dispatch_hard_kill(provider)
                owner._schedule_replenish()
                pooled = None
                pool_decision = "bypass_model_at_launch"
            else:
                self._deps.logger.info(
                    "Claimed warm-pool process for %s (agent=%s)",
                    key,
                    agent or owner._pool_agent,
                )
                owner._schedule_replenish()
        except (asyncio.CancelledError, Exception):
            owner._record_pool_decision(pool_decision, key)
            owner._dispatch_hard_kill(provider)
            raise
        owner._record_pool_decision(pool_decision, key)
    if pooled is None:
        # A defaulted KAS waits (bounded) for the kiro-cli floor's verdict, so
        # the factory below picks the backend the verdict decided.
        await await_kas_floor_verdict(member=_is_member_key(key))
        # The verdict can install a new factory before it releases this wait (a
        # confirmed v3 reloads the live config), so a waiting spawn takes the
        # factory in force now, not the one it copied before waiting.
        factory = owner._provider_factory or factory
        provider = factory(
            key,
            agent=agent,
            channel_id=channel_id,
            model_override=model,
            cwd=effective_cwd,
            extra_env=extra_env,
            **extra_factory_kwargs,
        )
        cast(Any, provider).memory_mode = memory_mode
        if self._deps.is_acp_provider(provider):
            cast(Any, provider).member_context = member_context
            cast(Any, provider).app_launch_agent = app_launch_agent
            # Trusted: the caller's slot app, never transcript metadata.
            cast(Any, provider).owning_app = owning_app
        if memory_mode != "persistent":
            resume_sid = None
        provider_switched = False
        if resume_sid:
            is_claude_now = self._deps.is_claude_provider(provider) or self._deps.is_claude_backend(
                provider
            )
            current_provider = (
                constants.provider_label_claude
                if is_claude_now
                else self._deps.provider_label(provider)
            )
            if self._deps.detect_provider_switch(owner._session_map, key, current_provider):
                resume_sid = None
                provider_switched = True
                owner._session_map.clear_sid(key)

        if resume_sid:
            if self._deps.is_acp_provider(provider):
                cast(Any, provider).client.set_resume_session_id(resume_sid)
                self._deps.logger.info("Attempting session/load for %s (sid=%s)", key, resume_sid)
            elif self._deps.is_claude_provider(provider):
                cast(Any, provider).set_resume_session_id(resume_sid)
                self._deps.logger.info("CC resume for %s (sid=%s)", key, resume_sid)
        elif native_fork:
            # One attempt: a fork that fails falls back to a fresh session.
            # Never for a restricted mode, which restores no native history.
            owner._session_map.clear_native_fork(key)
            if memory_mode == "persistent" and self._deps.is_claude_backend(provider):
                cast(Any, provider).client.set_fork_request(*native_fork)
                self._deps.logger.info(
                    "Attempting session/fork for %s (source=%s)", key, native_fork[0]
                )
        # Ordered by start priority (rule: ``kiro_crew.start_priority``); the
        # provider's own spawn and session/new queues read the same answer.
        provider.start_priority = start_priority
        async with self._held_start_permit(
            key,
            start_priority,
            on_queued=extra_factory_kwargs.get("on_gate_queued"),
            on_acquired=extra_factory_kwargs.get("on_gate_acquired"),
        ):
            try:
                if preparation.revision:
                    from kiro_crew.session_capabilities import (
                        CapabilityStartupError,
                        verify_saved,
                    )

                    if provider.member_capabilities_supported is not True:
                        raise CapabilityStartupError("capability_harness_unsupported")
                    if (
                        preparation.tool_limited_servers
                        and provider.member_tool_limits_supported is not True
                    ):
                        raise CapabilityStartupError("capability_tool_limits_unsupported")
                    if provider.process_instance:
                        raise CapabilityStartupError("capability_runtime_not_fresh")
                    await asyncio.to_thread(verify_saved, preparation, provider.cwd)
                # A crewmate's worker runs with its per-session spec, or not at
                # all, under the member chat's two codes (H6 opt-in): a backend
                # that cannot carry that spec (``ACP_BACKENDS_MATE_WORKER_KIT``),
                # and, for a crewmate whose file turns off single tools, one that
                # cannot honour them for a worker (``ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS``:
                # KAS carries them as the worker spec's ``excludedTools``).
                from kiro_crew.members import is_crewmate_worker_session

                if is_crewmate_worker_session(key):
                    from kiro_crew.session_capabilities import CapabilityStartupError

                    if provider.mate_worker_kit_supported is not True:
                        raise CapabilityStartupError("capability_harness_unsupported")
                    # The prompt channel is this provider's, fixed for the session.
                    from kiro_crew.members import note_crewmate_worker_prompt_channel

                    note_crewmate_worker_prompt_channel(
                        key, provider.mate_worker_prompt_in_context is True
                    )
                    # A fast path: the projection refuses on the spec it builds too.
                    if provider.mate_worker_tool_limits_supported is not True:
                        limited = await asyncio.to_thread(_worker_tool_limited_servers, key)
                        if limited:
                            raise CapabilityStartupError("capability_tool_limits_unsupported")
                # The key may have been fenced while this call waited for the
                # semaphore or the reads above: refuse before a process is
                # spawned that the registration door would only refuse later.
                self._refuse_if_ending(key, _reservation)
                # Past the spawn door: from here until registration a process
                # is being started that no map read can see -- what an
                # ending caller's post-pass read (``spawn_in_flight``)
                # must answer for. Same loop step as the check above.
                if _reservation is not None:
                    self.state.spawning_reservations.add(_reservation)
                pre_spawn = await pre_spawn_identity(getattr(owner, "spawn_identity_reader", None))
                await provider.start()
            except (asyncio.CancelledError, Exception):
                if preparation.revision:
                    self._remember_capability_failure(key, preparation)
                owner._dispatch_hard_kill(provider)
                raise
            # start() has published the PID, and the stamp read below is a
            # real suspension point before registry ownership becomes
            # visible in the lock section further down -- shield the PID
            # from the periodic orphan sweep for that whole span (on
            # Windows the sweep has no age grace, so an unshielded child
            # caught mid-stamp would be killed as an orphan).
            starting_pid = spawn_pid(provider)
            if starting_pid is not None:
                self._starting_pids.add(starting_pid)
            # Record which account the store held as this child spawned
            # (first-stamp-wins, so a warm-pool claim keeps its fill-time
            # stamp). Best-effort: an unstamped child keeps the
            # pre-stamping protections. The stamp read is a real
            # suspension point between start() and PID registration, so a
            # cancellation landing here must kill the started child the
            # same way the start guard above would -- otherwise the
            # provider leaks unmanaged until the orphan sweep's grace
            # window expires.
            try:
                await stamp_spawn_identity(
                    getattr(owner, "spawn_identity_reader", None),
                    provider,
                    pre_spawn=pre_spawn,
                )
            except BaseException:
                owner._dispatch_hard_kill(provider)
                if starting_pid is not None:
                    self._starting_pids.discard(starting_pid)
                raise

    # ``starting_pid`` was shielded inside the semaphore section above on
    # the cold-start path and stays shielded until registry ownership is
    # visible: the ``finally`` at the end of the registration section
    # below discards it. A pool-claimed provider took the other branch --
    # and left ``_pool_pids`` at claim -- so shield it here for the same
    # start-to-registration span.
    if starting_pid is None:
        starting_pid = spawn_pid(provider)
        if starting_pid is not None:
            self._starting_pids.add(starting_pid)

    won_race_session: Any | None = None
    duplicate_provider: LLMProvider | None = None
    try:
        stamp = None
        if preparation.revision:
            from kiro_crew.session_capabilities import loaded_stamp, verify_saved

            observed = loaded_stamp(provider, preparation)
            await asyncio.to_thread(verify_saved, preparation, provider.cwd)
            stamp = loaded_stamp(provider, preparation)
            if stamp != observed:
                raise RuntimeError("capability_process_changed_during_verification")
        resumed = False
        if self._deps.is_acp_provider(provider):
            resumed = cast(Any, provider).client.resumed
        if speculative and speculative_resume and not resumed:
            raise SpeculativeResumeRefused(key)

        async with self._lock:
            # start() can span the complete close_all snapshot, so closing
            # must be checked a second time immediately before registration.
            if self._closing:
                raise SessionClosingError(
                    "SessionManager began closing during provider startup; "
                    "refusing to register a session behind the shutdown snapshot"
                )
            # And the key may have been ended during start(): a reservation
            # the fence invalidated registers nothing, fence up or lifted --
            # the provider it started is hard-killed by the handler below,
            # and ``get_or_create`` allocates again once the fence has
            # lifted, so the request lands under the recorded key.
            self._refuse_if_ending(key, _reservation)
            # Through the spawn door's far side: from here to the end of
            # this lock hold there is no await, and the process is either
            # published below (visible to any map read) or a won race's
            # duplicate this call shuts down itself -- from here on it is not
            # a start an ending caller must be told about.
            self.state.spawning_reservations.discard(_reservation)

            existing = self._sessions.get(key)
            recycling = existing is not None and owner._recycling.get(key) is existing
            if existing is not None and not recycling:
                session = existing
                session.last_used = time.monotonic()
                if approval_policy:
                    session.approval_policy = approval_policy
                if session_agent and not preparation.revision:
                    session.agent = session_agent
                won_race_session = session
                duplicate_provider = provider
            else:
                if not speculative:
                    first_turn = self._deps.first_turn_nothing_armed
                elif resumed:
                    first_turn = self._deps.first_turn_resumed
                else:
                    first_turn = self._deps.first_turn_fresh
                session = self._deps.session_factory(
                    provider=provider,
                    first_turn=first_turn,
                    approval_policy=approval_policy,
                    agent=session_agent or "",
                )
                session.capability_member = preparation.member
                # The store this cold start supersedes, captured HERE -- inside
                # the registration's critical section, before this session is
                # registered and before its sid is mapped (or its mapping
                # deferred). ``mapped_sid`` still answers a recycled id from the
                # stash the recycle left, so this names the store the key was
                # last serving whatever ended it. A caller reading the mapping
                # around its own ``get_or_create`` cannot get this right: it may
                # be suspended inside the allocation while a concurrent turn on
                # the key allocates and recycles an intermediate session, and
                # would then cite the store before that one. Stamped on the
                # session, like ``requested_model``, so teardown releases it.
                session.predecessor_sid = owner._session_map.mapped_sid(key)
                # The id the provider above was constructed with, kept
                # readable for the allocation's caller. ``model`` is resolved
                # from config when the caller passed none, and that resolution
                # is invisible in this call's return value, so a caller
                # recording the session's selection has no other source for it.
                # Stamped from the same local rather than re-resolved, which is
                # what keeps the id sent and the id read identical.
                session.requested_model = model or ""
                session.backend_override = extra_factory_kwargs.get("backend_override")
                session.app_launch_agent = app_launch_agent
                session.extra_env = dict(caller_env) if caller_env else None
                session.reasoning_effort_override = extra_factory_kwargs.get(
                    "reasoning_effort_override"
                )
                session.aws_project_profile = bound_profile
                session.loaded_capabilities = stamp
                self.state.capability_failures.pop(key, None)
                replay_needed = getattr(provider, "_history_replay_needed", False) is True
                provider_label = self._deps.provider_label(provider)
                defer_sid_promotion = (
                    replay_needed
                    and provider.defer_replay_sid_promotion is True
                    and provider_label == constants.provider_label_default
                )
                if provider_switched or replay_needed:
                    session.provider_switch_replay = True
                if replay_needed and provider_label != constants.provider_label_default:
                    owner._session_map.clear_sid(key)
                self._install_work_dir_claim_probe(key, provider)
                self._sessions[key] = session
                self.advance_ownership_generation(key)
                # Registered: from here a live session is using this runtime,
                # so record the claim the kill gate reads. Paired with the
                # release inside the provider's own shutdown, and with the
                # release on the failure arm below that deregisters without
                # one. Before this line no tenant exists, which is why every
                # earlier cleanup path may kill unconditionally.
                await acquire_session_lease(provider)
                try:
                    await record_session_started(key, provider)
                except BaseException:
                    # See open_task_session: a cancellation here would leave a
                    # registered session whose provider the caller is about to
                    # kill, plus a crumb the next boot reads as a crash.
                    #
                    # No release here: this re-raises into the handler at the
                    # end of this method, which releases before it kills. A
                    # second release would be a call whose effect is already
                    # guaranteed, and one the ordering test cannot protect.
                    if self._sessions.get(key) is session:
                        del self._sessions[key]
                        self.advance_ownership_generation(key)
                    await discard_session_start(key)
                    raise
                # After the registration has committed: the rollback above
                # is behind us, so a cancelled start cannot take the parked
                # entries down with the session it removes. What a
                # forced stop parked for this key lands at the queue head;
                # the drains for those entries' channels are woken by the
                # release that ends this claim (``_wake_adopted_drains``).
                if adopt_parked_queue(session, key):
                    session.adopted_parked = True
                self._deps.logger.info(
                    "New session: %s agent=%s resumed=%s provider_switch=%s (total=%d)",
                    key,
                    agent or "kirocrew",
                    resumed,
                    provider_switched,
                    len(self._sessions),
                )

                provider_cwd = provider.cwd
                if not is_stateless and self._deps.is_acp_provider(provider):
                    sid = cast(Any, provider).client._session_id
                    if sid and not defer_sid_promotion:
                        owner._session_map.set(
                            key,
                            sid,
                            provider=provider_label,
                            cwd=provider_cwd,
                        )
                    elif sid:
                        self._deps.logger.info(
                            "Deferring fresh SID promotion for replay-pending "
                            "session %s; prior resumable SID stays durable",
                            key,
                        )
                elif not is_stateless and self._deps.is_claude_provider(provider):
                    sid = provider.session_id
                    if sid:
                        owner._session_map.set(
                            key,
                            sid,
                            provider=constants.provider_label_claude,
                            cwd=provider_cwd,
                        )

                # Cleanup owns its task slot; allocation only asks the
                # facade to ensure it at the original registration point.
                owner._ensure_cleanup_task()
                # Fresh semaphore acquisition is synchronous and cannot
                # wait, so doing it under _lock does not invert lock order.
                await session.semaphore.acquire()
                session.turn_owner = asyncio.current_task()
                self._deps.inc_session_created()
                result = (provider, True, resumed)
    except BaseException:
        if preparation.revision:
            self._remember_capability_failure(key, preparation)
        # The ONE of these cleanup paths that can be past registration: this
        # handler spans the lock section that registers the session, so a
        # failure after it leaves a tenant holding a lease. Release before the
        # kill or the gate refuses it and the process leaks -- the cleanup
        # would be refusing its own teardown. Every earlier hard-kill site in
        # this file is pre-registration and holds no lease.
        await release_session_lease(provider)
        owner._dispatch_hard_kill(provider)
        raise
    finally:
        if starting_pid is not None:
            self._starting_pids.discard(starting_pid)

    if won_race_session is not None:
        if duplicate_provider is not None:
            # ``existing`` won this key and runs in the very directory the
            # loser derived from it: a reclaim at the loser's shutdown would
            # pull the live session's cwd out from under it.
            duplicate_provider.disown_work_dir()
            try:
                await duplicate_provider.shutdown()
            except Exception:
                self._deps.logger.warning(
                    "Failed to shut down duplicate provider for %s",
                    key,
                    exc_info=True,
                )
        if await owner._reacquire_and_validate(
            key,
            won_race_session,
            wait_if_busy=wait_if_busy,
            reservation=_reservation,
        ):
            first_turn = won_race_session.first_turn
            if not speculative:
                won_race_session.first_turn = self._deps.first_turn_nothing_armed
            return (
                won_race_session.provider,
                first_turn.is_new,
                first_turn.resumed,
            )
        maximum = constants.won_race_max_retries
        if _won_race_retries >= maximum:
            raise RuntimeError(
                f"get_or_create({key!r}) exceeded {maximum} won-race retries — "
                "session kept going stale between acquire and re-validate"
            )
        return await owner.get_or_create(
            key,
            agent=session_agent,
            channel_id=channel_id,
            approval_policy=approval_policy,
            model=model,
            cwd=cwd,
            extra_env=caller_env,
            speculative=speculative,
            speculative_resume=speculative_resume,
            wait_if_busy=wait_if_busy,
            _won_race_retries=_won_race_retries + 1,
            start_priority=start_priority,
            app_name=app_name,
            app_launch_agent=app_launch_agent,
            owning_app=owning_app,
            **extra_factory_kwargs,
        )

    return result


def _worker_tool_limited_servers(key: str) -> tuple[str, ...]:
    """The servers whose single tools worker *key*'s crewmate file turns off. Blocking."""
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker import crewmate_tool_limited_servers

    execution = read_session_execution(key)
    member_id = str(getattr(execution, "member_id", "") or "") if execution is not None else ""
    return crewmate_tool_limited_servers(member_id)


def _is_crewmate_worker_key(key: str) -> bool:
    """Whether *key* is a crewmate's own worker session (``members.is_crewmate_worker_session``)."""
    from kiro_crew.members import is_crewmate_worker_session

    return is_crewmate_worker_session(key)
