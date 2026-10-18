"""Session registry, allocation, and claim coordination.

``SessionAllocationService`` owns the live-session registry and every lock or
lease needed to allocate from it.  Warm-pool inventory, compaction, teardown,
and cleanup policy remain separate owner-facade responsibilities.  This module
never imports :mod:`kiro_crew.session` at runtime; patchable compatibility seams
are supplied through :class:`AllocationDeps`.

The class keeps its whole interface; the bodies of its two largest member groups
live in the :mod:`kiro_crew.session_allocation_parts` package: ``get_or_create``
(claiming or cold-starting the session for a key) and ``companions`` (companion and
run runtimes). Each member there is a function whose first parameter is ``self``,
bound on the class by name, and the refusals the class and ``get_or_create`` raise
live in its ``errors`` module. Every project name this module bound before the owners
existed still resolves here, and :mod:`kiro_crew._facade_mirror` carries a patch of
one of them through this module to every owner that reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib as _importlib
import logging
import sys as _sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.aws_project_profiles import configured_project_profiles, resolve_project_profile
from kiro_crew.messaging.queue_drain import entry_channel, wake_other_drains
from kiro_crew.metrics.sessions import discard_session_start, record_session_started
from kiro_crew.runtime_ownership import PidRefcount
from kiro_crew.session_allocation_parts.errors import (
    ENDING_FENCE_WAIT_SECS,
    SessionClosingError,
    SessionEndingError,
)
from kiro_crew.session_lifecycle import adopt_parked_queue
from kiro_crew.start_priority import PrioritySemaphore, StartPriority

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider
else:
    # ProviderFactory below subscripts LLMProvider at module scope, so a name must
    # exist at runtime; a real import would cross the agent-SDK boundary gate.
    LLMProvider = Any


ProviderFactory = Callable[..., LLMProvider]


@dataclass(frozen=True, slots=True)
class AllocationConstants:
    """Behavioral constants supplied by the facade's patchable namespace."""

    won_race_max_retries: int
    circuit_breaker_threshold: int
    agent_model_cache_ttl: Callable[[], float]
    background_key: str
    heartbeat_key: str
    background_agent: str
    subagent_prefix: str
    stateless_prefixes: tuple[str, ...]
    provider_label_default: str
    provider_label_claude: str
    # Singleton stateless sessions, matched exactly rather than by prefix.
    stateless_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AllocationDeps:
    """Injected leaf dependencies and dynamic compatibility seams.

    Functions whose source names are monkeypatched in existing tests should be
    passed as forwarding lambdas.  The service deliberately holds no copy of
    ``SessionMap``: the owner's live instance remains persistence authority.
    """

    logger: logging.Logger
    constants: AllocationConstants
    canonical_key: Callable[[str], str]
    legacy_key: Callable[[str], str | None]
    provider_has_active_turn: Callable[[LLMProvider], bool]
    provider_effectively_alive: Callable[[LLMProvider], bool]
    is_acp_provider: Callable[[LLMProvider], bool]
    is_claude_provider: Callable[[LLMProvider], bool]
    is_claude_backend: Callable[[LLMProvider], bool]
    provider_label: Callable[[LLMProvider], str]
    detect_provider_switch: Callable[[Any, str, str], bool]
    session_factory: Callable[..., Any]
    first_turn_nothing_armed: object
    first_turn_fresh: object
    first_turn_resumed: object
    runtime_types: Callable[[], tuple[Callable[..., Any], type[BaseException]]]
    session_provider_type: Callable[[], Callable[[Any, Any], LLMProvider]]
    unlink_session_queue: Callable[[Any], None]
    unlink_queued_temp_paths: Callable[[dict[str, Any]], None]
    # (cfg, agent, crew_agent, *, app_name, session_key, backend_override)
    session_model: Callable[..., str | None]
    load_config: Callable[[], Any]
    resolve_crew_identity: Callable[[Any, str | None, str | None], str]
    load_watchdog_settings: Callable[[str], object]
    advertised_model_ids: Callable[[Any], list[str]]
    model_is_unusable: Callable[[str, list[str]], bool]
    #: Whether a stored pin belongs to the harness a provider runs on
    #: (``model_scope.pin_applies``), and that harness's model-id namespace.
    #: Injected rather than imported for the same reason every other model
    #: helper here is: this module is constructed with its whole world so a test
    #: can substitute one, and reaching for ``kiro_crew.model_scope`` directly
    #: would make the pool's scope rule the only one a test cannot swap.
    model_pin_applies: Callable[[str, str, Sequence[str] | None], bool]
    provider_model_namespace: Callable[[LLMProvider], str]
    #: The pin fold, taking the PROVIDER as well so the answer can be the one
    #: that harness's wire takes: a ``<model>[<effort>]`` pair-id backend
    #: advertises rows its own ``model`` option refuses, and resolves a bare pin
    #: to the bare id that option does accept. Reading the backend HERE rather
    #: than passing a pre-resolved id keeps this site answering from the same
    #: fold the cold start uses, which is the property this dependency exists
    #: to hold.
    resolve_pin_spelling: Callable[[str, list[str], LLMProvider], str]
    to_provider_id: Callable[[str, str], str]
    to_acp_id: Callable[[str], str]
    inc_session_created: Callable[[], None]
    get_sel: Callable[[], Any]
    get_subprocess_executor: Callable[[], Executor]
    get_sync_kill_provider: Callable[[], Callable[[LLMProvider], None]]
    agents_dir_path: Callable[[], Path]
    read_agent_spec: Callable[..., dict[str, Any] | None]
    spec_model: Callable[[dict[str, Any]], str]
    agent_model_cache: Callable[[], dict[str, tuple[str, float, float]]]


# Concurrent cold starts ``SessionManager._start_sem`` allows background starts by
# default; ``agent.cold_start_concurrency`` sets it (``cold_start_sizing``). A
# provider inside ``start()`` has not published a PID yet, so the semaphore is the
# only evidence it exists, which is why the identity sweep drains it
# (``PrioritySemaphore.drain``) as its barrier.
MAX_CONCURRENT_COLD_STARTS = 4
# Cold-start permits on top of those that only a FOREGROUND start may hold (rule:
# ``kiro_crew.start_priority``). ``_start_sem`` is held across the provider's own
# spawn admission and ``session/new`` waits, so ordering alone cannot help a person
# when every permit belongs to a background start stuck behind a fan-out further
# in. With this reserve the first concurrent person start always gets an outer
# permit at once and then goes ahead at the inner queues; further concurrent person
# starts are served ahead of background ones as permits free. Additive, so
# background keeps the full width while no person is starting.
FOREGROUND_COLD_START_RESERVE = 1


def new_cold_start_semaphore(width: int = MAX_CONCURRENT_COLD_STARTS) -> PrioritySemaphore:
    """``SessionManager._start_sem``: *width* background permits plus the person reserve."""
    return PrioritySemaphore(
        width + FOREGROUND_COLD_START_RESERVE,
        foreground_reserve=FOREGROUND_COLD_START_RESERVE,
    )


def widen_cold_start_semaphore(sem: PrioritySemaphore, width: int) -> None:
    """Grow *sem* to *width* background permits (plus the reserve); never shrinks."""
    sem.widen_to(width + FOREGROUND_COLD_START_RESERVE)


@dataclass(slots=True)
class SessionRegistryState:
    """Mutable state exclusively owned by the allocation boundary."""

    sessions: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False
    update_pause_owned: bool = False
    update_restart_fenced: bool = False
    start_sem: PrioritySemaphore = field(default_factory=new_cold_start_semaphore)
    #: COUNTED, not listed: two starts can legitimately shield one pid (the
    #: allocator carries a race budget for starting one session twice), and as a
    #: plain set the first to finish tore the shield off a process the other was
    #: still cold-starting. Reads like the set it replaces.
    starting_pids: PidRefcount = field(default_factory=PidRefcount)
    allocation_reservations: dict[str, set[object]] = field(default_factory=dict)
    inbound_callback_reservations: set[object] = field(default_factory=set)
    ownership_generations: dict[str, int] = field(default_factory=dict)
    subagent_runtimes: dict[str, Any] = field(default_factory=dict)
    subagent_runtime_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    #: Busy wrong-account companion runtimes displaced out of
    #: ``subagent_runtimes`` by the spawn-identity gate: unclaimable while
    #: parked (a fresh acquisition spawns a replacement under the live
    #: account), so their in-flight work drains before the reap kills them.
    draining_subagent_runtimes: list[Any] = field(default_factory=list)
    continuable_keys: set[str] = field(default_factory=set)
    capability_failures: dict[str, dict[str, str]] = field(default_factory=dict)
    continuable_fallback: Callable[[str], bool] | None = None
    #: Keys whose run is being ended (``begin_ending``): a claim or cold start
    #: under one is HELD at the door until the fence lifts. The value is the set
    #: of allocation reservation tokens that were in flight when the fence went
    #: up -- a cold start caught inside ``provider.start()`` -- kept so
    #: ``end_ending`` can tell them apart from reservations that never met the
    #: fence.
    ending_keys: dict[str, set[object]] = field(default_factory=dict)
    #: Reservation tokens invalidated by a fence: their cold start is refused at
    #: registration even after the fence lifts, and its provider hard-killed.
    #: A token leaves the set when its reservation is removed.
    invalidated_reservations: set[object] = field(default_factory=set)
    #: Per fenced key, the event ``end_ending`` sets when the fence lifts: what a
    #: request held at the door waits on. Created by ``begin_ending``, removed
    #: with the fence (waiters hold their own reference to it).
    ending_lifted: dict[str, asyncio.Event] = field(default_factory=dict)
    #: Reservation tokens past the cold start's SPAWN DOOR (the pre-spawn fence
    #: check in ``get_or_create``) -- or holding a warm-pool claim, a live process
    #: from the claim on -- and not yet through registration: the span in
    #: which a process is owned that no map read can see. A reservation
    #: under a fenced key that is NOT here is a claim waiting on a live session's
    #: turn or a cold start still ahead of its spawn door -- no process of its
    #: own, so nothing an ending caller must answer for. Read by
    #: ``spawn_in_flight``; a token leaves at registration (published, or a
    #: won race) or with its reservation.
    spawning_reservations: set[object] = field(default_factory=set)
    #: Per fenced key, a receipt for every start the fence caught past its spawn
    #: door and whose reservation was then removed WHILE the fence was still up:
    #: the start returned during the ending caller's passes, its registration
    #: was refused and its provider hard-killed by the allocation path
    #: (``_dispatch_hard_kill``, dispatched off the loop with no outcome this
    #: process reads back). Kept so the ending caller's post-pass read still
    #: names that process -- without the receipt the reservation cleanup erased
    #: every trace of it before the read, and a kill the dispatch could not land
    #: left a process alive behind a record that said ``reaped``. Cleared by
    #: ``end_ending``, once the caller has written its record.
    refused_spawns: dict[str, int] = field(default_factory=dict)


class InboundCallbackReservation:
    """One counted inbound callback claim with idempotent release."""

    __slots__ = ("_reservations", "_token")

    def __init__(self, reservations: set[object], token: object) -> None:
        self._reservations = reservations
        self._token: object | None = token

    def release(self) -> None:
        token = self._token
        if token is None:
            return
        self._token = None
        self._reservations.discard(token)


class _AllocationOwner(Protocol):
    """Facade and cross-service surface consumed by allocation."""

    _cfg: Any
    _provider_factory: ProviderFactory | None
    _session_map: Any
    _recycling: dict[str, Any]
    _pool_size: int
    _pool_agent: str
    _pool_cwd: str
    _warm_pool: asyncio.Queue[tuple[LLMProvider, float]]
    _background_tasks: set[asyncio.Task[Any]]
    _bg_runtime: Any | None

    def _fold_key(self, key: str) -> str: ...

    def _bg_backend_supports_runtime(self) -> bool: ...

    async def await_replay_gap(self, key: str) -> None: ...

    def absorb_orphaned_release(self, key: str) -> bool: ...

    def adopt_turn(self, key: str) -> None: ...

    def get_provider(self, key: str) -> LLMProvider | None: ...

    async def get_subagent_runtime(
        self,
        parent_session_key: str,
        agent: str | None = None,
        *,
        start_priority: StartPriority = StartPriority.BACKGROUND,
        on_gate_queued: Callable[..., None] | None = None,
        on_gate_acquired: Callable[..., None] | None = None,
    ) -> Any: ...

    async def _get_or_bootstrap_run_runtime(
        self,
        parent_session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
        start_priority: StartPriority = StartPriority.BACKGROUND,
    ) -> Any: ...

    async def _reacquire_and_validate(
        self,
        key: str,
        session: Any,
        *,
        wait_if_busy: bool = True,
        reservation: object | None = None,
    ) -> bool: ...

    async def _evict_stale_session(self, key: str, session: Any) -> None: ...

    async def open_task_session(
        self, parent_session_key: str, session_key: str, **kwargs: Any
    ) -> Any: ...

    def _get_session_agent(self, session_key: str) -> str: ...

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict[str, Any]: ...

    async def _drain_and_claim(self, agent: str | None) -> LLMProvider | None: ...

    def _record_pool_decision(self, decision: str, key: str) -> None: ...

    def _schedule_replenish(self) -> None: ...

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None: ...

    def _resolve_agent_model(self, agent: str) -> str: ...

    def _ensure_cleanup_task(self) -> None: ...

    async def get_or_create(self, key: str, **kwargs: Any) -> Any: ...

    async def reset(self, key: str, **kwargs: Any) -> bool: ...

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None: ...

    def mark_continuable(self, key: str) -> None: ...

    def _is_continuable_key(self, folded: str) -> bool: ...

    def _append_companion_runtime_rows(self, rows: list[dict[str, object]]) -> None: ...


# The class below binds its largest member groups from these owner modules, and the
# end of this module installs the mirror over them, so they load here.
from kiro_crew import _facade_mirror  # noqa: E402
from kiro_crew.session_allocation_parts import companions as _sa_companions  # noqa: E402
from kiro_crew.session_allocation_parts import get_or_create as _sa_get_or_create  # noqa: E402


class SessionAllocationService:
    """Allocate providers and serialize claims while the manager stays facade."""

    def __init__(
        self,
        owner: _AllocationOwner,
        deps: AllocationDeps,
        *,
        state: SessionRegistryState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self.state = state

    # Compatibility properties preserve identity for maps, locks, and sets.
    @property
    def _sessions(self) -> dict[str, Any]:
        return self.state.sessions

    @_sessions.setter
    def _sessions(self, value: dict[str, Any]) -> None:
        self.state.sessions = value

    @property
    def _lock(self) -> asyncio.Lock:
        return self.state.lock

    @_lock.setter
    def _lock(self, value: asyncio.Lock) -> None:
        self.state.lock = value

    @property
    def _closing(self) -> bool:
        return self.state.closing

    @_closing.setter
    def _closing(self, value: bool) -> None:
        self.state.closing = value

    @property
    def _start_sem(self) -> PrioritySemaphore:
        return self.state.start_sem

    @_start_sem.setter
    def _start_sem(self, value: PrioritySemaphore) -> None:
        self.state.start_sem = value

    @property
    def _starting_pids(self) -> PidRefcount:
        return self.state.starting_pids

    @_starting_pids.setter
    def _starting_pids(self, value: PidRefcount) -> None:
        self.state.starting_pids = value

    @property
    def _allocation_reservations(self) -> dict[str, set[object]]:
        return self.state.allocation_reservations

    @_allocation_reservations.setter
    def _allocation_reservations(self, value: dict[str, set[object]]) -> None:
        self.state.allocation_reservations = value

    @property
    def _inbound_callback_reservations(self) -> set[object]:
        return self.state.inbound_callback_reservations

    @property
    def _ownership_generations(self) -> dict[str, int]:
        return self.state.ownership_generations

    @_ownership_generations.setter
    def _ownership_generations(self, value: dict[str, int]) -> None:
        self.state.ownership_generations = value

    @property
    def _subagent_runtimes(self) -> dict[str, Any]:
        return self.state.subagent_runtimes

    @_subagent_runtimes.setter
    def _subagent_runtimes(self, value: dict[str, Any]) -> None:
        self.state.subagent_runtimes = value

    @property
    def _subagent_runtime_locks(self) -> dict[str, asyncio.Lock]:
        return self.state.subagent_runtime_locks

    @_subagent_runtime_locks.setter
    def _subagent_runtime_locks(self, value: dict[str, asyncio.Lock]) -> None:
        self.state.subagent_runtime_locks = value

    @property
    def _draining_subagent_runtimes(self) -> list[Any]:
        return self.state.draining_subagent_runtimes

    @_draining_subagent_runtimes.setter
    def _draining_subagent_runtimes(self, value: list[Any]) -> None:
        self.state.draining_subagent_runtimes = value

    @property
    def _continuable_keys(self) -> set[str]:
        return self.state.continuable_keys

    @_continuable_keys.setter
    def _continuable_keys(self, value: set[str]) -> None:
        self.state.continuable_keys = value

    @property
    def _continuable_fallback(self) -> Callable[[str], bool] | None:
        return self.state.continuable_fallback

    @_continuable_fallback.setter
    def _continuable_fallback(self, value: Callable[[str], bool] | None) -> None:
        self.state.continuable_fallback = value

    def _fold_key(self, key: str) -> str:
        """Resolve exact, canonical, then legacy aliases across live and reserved keys."""

        def owned(candidate: str) -> bool:
            return candidate in self._sessions or bool(self._allocation_reservations.get(candidate))

        if owned(key):
            return key
        canonical = self._deps.canonical_key(key)
        if canonical != key and owned(canonical):
            return canonical
        bare = self._deps.legacy_key(key)
        if bare is not None and owned(bare):
            return bare
        return key

    def has_session(self, key: str) -> bool:
        return self._owner._fold_key(key) in self._sessions

    def get_provider(self, key: str) -> LLMProvider | None:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.provider if session else None

    def _generation_key(self, key: str) -> str:
        """Stable generation bucket shared by canonical and legacy Slack aliases."""
        return self._deps.canonical_key(key)

    def advance_ownership_generation(self, key: str) -> int:
        """Advance and return *key*'s monotonic ownership generation."""
        bucket = self._generation_key(key)
        generation = self._ownership_generations.get(bucket, 0) + 1
        self._ownership_generations[bucket] = generation
        return generation

    def session_generation(self, key: str) -> int:
        """Return the monotonic logical-key ownership generation.

        Zero is the first absence generation, not a reusable sentinel: every
        reservation publication/removal advances the canonical key's counter,
        so an absent -> successor -> absent ABA cannot match a stale capture.
        """
        return self._ownership_generations.get(self._generation_key(key), 0)

    def session_keys(self) -> frozenset[str]:
        """Snapshot live and in-flight registry keys on the event-loop thread."""
        reserved = {
            key for key, reservations in self._allocation_reservations.items() if reservations
        }
        return frozenset(self._sessions.keys() | reserved)

    def has_allocation_reservation(self, key: str) -> bool:
        """Return whether a folded alias has an allocation/claim in flight."""
        return bool(self._allocation_reservations.get(self._owner._fold_key(key)))

    def spawn_in_flight(self, key: str) -> str | None:
        """Why a cold start under *key* is a process the ending caller must name, or None.

        The read an ending caller (``begin_ending`` holder) makes after its kill
        passes. A reservation alone is not a process: a claim waiting on the live
        session's turn semaphore, or a cold start still ahead of the pre-spawn
        fence check, has started nothing and will be held or refused before it
        does. Two things are named. A reservation in ``spawning_reservations`` --
        past that door, its ``provider.start()`` in flight or returned but
        unregistered -- is a process the passes could not see: refused at
        registration and hard-killed there, but not answered by the caller's own
        passes. And a receipt in ``refused_spawns``: a start the fence caught that
        already returned during the passes, was refused and had its provider
        hard-killed by the allocation path -- a kill dispatched off the loop
        whose outcome nothing here reads back, so the record cannot call that
        process gone either. The phrase returned is the reason the caller records.
        """
        reasons: list[str] = []
        folded = self._owner._fold_key(key)
        spawning = self.state.spawning_reservations
        if spawning and any(
            token in spawning for token in self._allocation_reservations.get(folded, ())
        ):
            reasons.append("refused at registration, its provider hard-killed there")
        refused = self.state.refused_spawns.get(folded, 0)
        if refused:
            reasons.append(
                f"{refused} refused at registration during the ending, the provider hard-killed "
                "there by the allocation path -- an outcome this record does not confirm"
            )
        return "; ".join(reasons) if reasons else None

    def begin_ending(self, key: str) -> None:
        """Raise the per-key ending fence: nothing lands under *key* until ``end_ending``.

        The run that owned the key is being ended by a caller that holds its
        claim (the cron reaper, ``cancel()``), and that caller is about to record
        the run as reaped or cancelled. From here to ``end_ending`` -- held from
        before the caller's kill passes until the run's terminal record and
        audit are written -- a claim or a new allocation under the key is HELD
        at the door of ``get_or_create`` (it waits for the fence to lift, then
        proceeds; see :func:`wait_for_ending_fence`), and every allocation
        reservation already in flight under the key is INVALIDATED: a cold start
        caught inside ``provider.start()`` has published nothing the caller's
        passes could see, so it is refused at registration when it gets there,
        even after the fence lifts, the provider it started is hard-killed by the
        same path a closing manager uses, and its call then waits for the lift
        and allocates again. Without this the record would say ``reaped`` while
        that process published afterwards and the injection that started it ran
        on. Nothing held is dropped: the request lands under the key once the
        run is recorded, as it did before the fence existed -- only never inside
        the run being ended, and never under a key that is neither being ended
        nor recorded. Synchronous and lock-free by design: the reservation map
        only moves between awaits on the loop, so a snapshot taken here is
        consistent, and the caller must be able to raise the fence before its
        first await. One fence per key, owned by the run claim's taker; a
        second ``begin_ending`` on a fenced key adds the reservations it now
        sees.
        """
        folded = self._owner._fold_key(key)
        pending = set(self._allocation_reservations.get(folded, ()))
        self.state.ending_keys.setdefault(folded, set()).update(pending)
        self.state.invalidated_reservations.update(pending)
        self.state.ending_lifted.setdefault(folded, asyncio.Event())

    def end_ending(self, key: str) -> None:
        """Lift the ending fence for *key*; reservations it invalidated stay invalidated.

        Called once the run's terminal record and audit are written. Every
        request held at the door wakes and proceeds under the recorded key, while
        a cold start that was in flight when the fence went up still cannot
        register: its token stays in ``invalidated_reservations`` until the
        reservation itself is removed -- and its call, refused there, allocates
        again. The key's receipts of starts refused during the ending
        (``refused_spawns``) go with the fence: the caller has consumed them.
        """
        folded = self._owner._fold_key(key)
        self.state.ending_keys.pop(folded, None)
        self.state.refused_spawns.pop(folded, None)
        lifted = self.state.ending_lifted.pop(folded, None)
        if lifted is not None:
            lifted.set()

    async def wait_for_ending_fence(self, key: str, deadline: float | None) -> float | None:
        """Hold the caller until *key*'s ending fence lifts, bounded; return the wait's deadline.

        ``key`` is already folded. Returns at once for a key that is not fenced.
        ``deadline`` is the loop-time bound the caller carries across its own
        retries (``None`` the first time, when it is set from
        :data:`ENDING_FENCE_WAIT_SECS`): one budget for the whole call, so a fence
        that goes up again after a retry does not restart it. A fence still up at
        the deadline is a holder stuck past the bounded passes it exists for; the
        caller is refused with :class:`SessionEndingError` so the defect surfaces.
        """
        lifted = self.state.ending_lifted.get(key)
        if lifted is None or key not in self.state.ending_keys:
            return deadline
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + ENDING_FENCE_WAIT_SECS
        remaining = deadline - loop.time()
        if remaining > 0:
            try:
                await asyncio.wait_for(lifted.wait(), timeout=remaining)
                return deadline
            except asyncio.TimeoutError:
                pass
        raise SessionEndingError(
            f"session key {key!r} is still being ended after {ENDING_FENCE_WAIT_SECS:.0f}s "
            "(its run's reap or cancel has not released the key); refusing to claim or "
            "start a session under it"
        )

    def _refuse_if_ending(self, key: str, reservation: object | None) -> None:
        """Refuse a claim or allocation under a fenced key, or one whose reservation a fence invalidated.

        ``key`` is already folded. Called at the doors of ``get_or_create``'s
        allocation body -- the claim of a live session, again after that
        claim's semaphore wait (:meth:`_reacquire_and_validate`, since the wait
        can outlast a fence rising), the cold start before it spawns, and the
        registration after ``provider.start()`` -- with the
        call's own reservation token, so a start that was in flight when the
        fence went up is refused even after ``end_ending``; ``get_or_create``
        turns that refusal into a wait for the lift and a fresh allocation, so
        the request is held, not dropped. And at the entry of
        ``open_task_session``, the other publication door, with no token and
        no retry: that path reserves only around its create on a shared runtime,
        so it is refused while the fence is up. The front door of
        ``get_or_create`` does not call this: it holds the caller instead
        (:meth:`wait_for_ending_fence`).
        """
        if key in self.state.ending_keys:
            raise SessionEndingError(
                f"session key {key!r} is being ended (its run is being reaped or "
                "cancelled); refusing to claim or start a session under it"
            )
        if reservation is not None and reservation in self.state.invalidated_reservations:
            raise SessionEndingError(
                f"session key {key!r} was ended while this allocation was starting; "
                "refusing to register the session it started"
            )

    def _install_work_dir_claim_probe(self, key: str, provider: LLMProvider) -> None:
        """Guard *provider*'s final work-dir reclaim with registry ownership.

        The context manager deliberately retains ``_lock`` through the reclaim
        operation. A bool sampled under the lock and acted on afterwards would
        reopen the same pop-then-shutdown race this guard closes.
        """

        def provider_cwd(candidate: LLMProvider) -> str:
            try:
                value = candidate.cwd
            except Exception:
                return ""
            return value if isinstance(value, str) else ""

        target_cwd = provider_cwd(provider)

        @contextlib.asynccontextmanager
        async def claim():
            async with self._lock:
                claimed = not bool(self._allocation_reservations.get(key))
                if claimed:
                    for registered_key, session in self._sessions.items():
                        other = session.provider
                        if other is provider:
                            continue
                        if registered_key == key:
                            claimed = False
                            break
                        if target_cwd and provider_cwd(other) == target_cwd:
                            claimed = False
                            break
                yield claimed

        setter = getattr(type(provider), "set_work_dir_claim_probe", None)
        if callable(setter):
            setter(provider, claim)

    async def try_acquire(self, key: str) -> bool:
        """Acquire only an exact-key idle session; alias folding is intentional absent."""
        session = self._sessions.get(key)
        if session is None or session.semaphore.locked():
            return False
        # Idle Semaphore(1).acquire completes without suspending, keeping the
        # locked check and decrement atomic on the event loop.
        await session.semaphore.acquire()
        session.turn_owner = asyncio.current_task()
        return True

    def capability_runtime_view(self, member: str, saved_revision: str) -> dict[str, Any]:
        """Read capability adoption from this boundary's registry on the event loop."""
        from kiro_crew.session_capabilities import runtime_view

        return runtime_view(self.state, member, saved_revision)

    def active_providers(self) -> list[LLMProvider]:
        return [session.provider for session in self._sessions.values()]

    def any_active_turn(self) -> bool:
        return any(
            self._deps.provider_has_active_turn(session.provider)
            for session in self._sessions.values()
        )

    def get_pid(self, key: str) -> int | None:
        session = self._sessions.get(self._owner._fold_key(key))
        if not session:
            return None
        try:
            return session.provider.client._pid
        except AttributeError:
            return None

    # Companion and run runtimes, the parent-sharing decision and the runtimes' process
    # rows: ``kiro_crew.session_allocation_parts.companions``.
    get_subagent_runtime = _sa_companions.get_subagent_runtime
    release_subagent_runtime = _sa_companions.release_subagent_runtime
    _get_or_bootstrap_run_runtime = _sa_companions._get_or_bootstrap_run_runtime
    _get_session_agent = _sa_companions._get_session_agent
    _parent_runtime_kwargs = _sa_companions._parent_runtime_kwargs
    is_session_sharing_eligible = _sa_companions.is_session_sharing_eligible
    _parent_aws_profile = _sa_companions._parent_aws_profile
    aws_profile_split_possible = _sa_companions.aws_profile_split_possible
    project_aws_profile = _sa_companions.project_aws_profile
    aws_profile_differs_from_parent = _sa_companions.aws_profile_differs_from_parent
    _runtime_pid = staticmethod(_sa_companions._runtime_pid)
    runtime_pids = _sa_companions.runtime_pids
    _append_companion_runtime_rows = _sa_companions._append_companion_runtime_rows
    # Claiming the live session for a key or cold-starting one:
    # ``kiro_crew.session_allocation_parts.get_or_create``.
    _reacquire_and_validate = _sa_get_or_create._reacquire_and_validate
    _release_reacquired = _sa_get_or_create._release_reacquired
    _evict_stale_session = _sa_get_or_create._evict_stale_session
    _is_member_key = staticmethod(_sa_get_or_create._is_member_key)
    _crew_pins_effort = _sa_get_or_create._crew_pins_effort
    _dispatch_hard_kill = _sa_get_or_create._dispatch_hard_kill
    _remove_reservation_now = _sa_get_or_create._remove_reservation_now
    _remove_reservation_cancellation_drained = (
        _sa_get_or_create._remove_reservation_cancellation_drained
    )
    _held_start_permit = _sa_get_or_create._held_start_permit
    get_or_create = _sa_get_or_create.get_or_create
    _remember_capability_failure = _sa_get_or_create._remember_capability_failure
    _get_or_create_impl = _sa_get_or_create._get_or_create_impl

    async def open_task_session(
        self,
        parent_session_key: str,
        session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
        approval_policy: str = "",
        _won_race_retries: int = 0,
        start_priority: StartPriority = StartPriority.BACKGROUND,
    ) -> tuple[LLMProvider, bool, bool]:
        """Open a per-step session on the task run's shared runtime.

        A descriptor-bound macOS runtime cannot safely serve a later exact cwd
        through ACP's string-only session request. In that case the facade's
        normal dedicated-provider path binds a runtime at the requested cwd.
        """
        # Circular import: runtime imports the session provider path indirectly.
        from kiro_crew.acp.runtime import AcpWorkspaceBindingError

        owner = self._owner
        key = owner._fold_key(session_key)
        from kiro_crew.execution_context import read_session_execution

        execution = await asyncio.to_thread(read_session_execution, key)
        if execution is not None and execution.memory_mode != "persistent":
            # A restricted task starts a fresh native conversation whose
            # retention policy is fixed before launch; do not borrow a parent.
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
                start_priority=start_priority,
            )
        if not owner._bg_backend_supports_runtime():
            # Dispatch on the SAME membership rule ``get_bg_session`` uses: only
            # a backend in ``ACP_BACKENDS_ACP_RUNTIME`` has a shared multiplexed
            # runtime to open a per-step session on. A harness outside that set
            # has no such runtime to share, so ``_get_or_bootstrap_run_runtime``
            # would bootstrap one -- always a kiro-family process -- under the
            # task runner's own key, spawning kiro-cli under a foreign backend
            # label (and failing outright when kiro-cli is not installed). Route
            # those to the dedicated per-session path instead, exactly as
            # ``get_bg_session`` serves them a provider-backed session.
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
                start_priority=start_priority,
            )
        project_profiles = configured_project_profiles(owner._cfg)
        if (
            project_profiles
            and cwd
            and await asyncio.to_thread(resolve_project_profile, project_profiles, cwd)
        ):
            # The run's shared runtime is one process with one environment, so a
            # step in a project bound to an AWS profile takes the dedicated path,
            # where get_or_create sets that profile for its own process.
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
                start_priority=start_priority,
            )
        async with self._lock:
            # The other publication door: a key whose run is being ended
            # (``begin_ending``) admits no per-step session either. Refused here
            # -- not held like ``get_or_create``'s front door -- before anything
            # is created for it, because this path reserves only around its
            # create, never refuses at registration, and has no hard-kill handler
            # for a session already created on the shared runtime. A create already in
            # flight when the fence goes up publishes under the task runner's own
            # ``taskrunner:`` key, which no cron reap ends; the ending caller's
            # post-pass read of its key names whatever else lands there.
            self._refuse_if_ending(key, None)
            existing = self._sessions.get(key)
            if existing is not None:
                existing.last_used = time.monotonic()
                if approval_policy:
                    existing.approval_policy = approval_policy
        if existing is not None:
            if await owner._reacquire_and_validate(key, existing):
                return existing.provider, False, False
            await owner._evict_stale_session(key, existing)

        from kiro_crew.session_capabilities import prepare_runtime

        prepared = await asyncio.to_thread(prepare_runtime, agent, None, cwd)
        if prepared.revision:
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
                start_priority=start_priority,
            )
        runtime = await owner._get_or_bootstrap_run_runtime(
            parent_session_key, agent=agent, cwd=cwd, start_priority=start_priority
        )
        # Reserve the key for the create-and-register window, as
        # ``get_or_create`` does, so ``has_allocation_reservation`` and
        # ``session_keys`` see a per-step session that is still being created.
        token = object()
        async with self._lock:
            self._refuse_if_ending(key, None)
            self._allocation_reservations.setdefault(key, set()).add(token)
            self.advance_ownership_generation(key)
        try:
            handle = await runtime.create_session(
                cwd=cwd or None,
                agent=agent or None,
                # A per-step session on the RUN's shared runtime: without its
                # owner, its broker stubs carry a token no claim names and
                # resolve to nothing (fail closed), and before the token they
                # resolved to the run's parent session.
                session_key=key,
                memory_mode=execution.memory_mode if execution is not None else "persistent",
                start_priority=start_priority,
            )
        except AcpWorkspaceBindingError:
            self._remove_reservation_now(key, token)
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
                start_priority=start_priority,
            )
        except BaseException:
            self._remove_reservation_now(key, token)
            raise
        try:
            provider = self._deps.session_provider_type()(handle, runtime)
            setattr(
                provider,
                "memory_mode",
                execution.memory_mode if execution is not None else "persistent",
            )

            duplicate: LLMProvider | None = None
            won_race_session: Any | None = None
            async with self._lock:
                current = self._sessions.get(key)
                if current is not None:
                    session = current
                    session.last_used = time.monotonic()
                    if approval_policy:
                        session.approval_policy = approval_policy
                    duplicate = provider
                else:
                    session = self._deps.session_factory(
                        provider=provider,
                        first_turn=self._deps.first_turn_fresh,
                        approval_policy=approval_policy,
                        agent=agent or "",
                    )
                    session.capability_member = prepared.member
                    self._install_work_dir_claim_probe(key, provider)
                    self._sessions[key] = session
                    self.advance_ownership_generation(key)
                    won_race_session = session
                    try:
                        await record_session_started(key)
                    except BaseException:
                        # This await is the only suspension point between registering
                        # the session and returning it. Cancelled here, the caller
                        # hard-kills the provider while the entry stays visible, so a
                        # claimant can be handed a session whose process is already
                        # dying -- and the crumb would outlive it into a false crash.
                        if self._sessions.get(key) is session:
                            del self._sessions[key]
                            self.advance_ownership_generation(key)
                        await discard_session_start(key)
                        raise
                    # After the registration has committed: the rollback above is
                    # behind us, so a cancelled start cannot take parked entries
                    # down with the session it removes.
                    adopt_parked_queue(session, key)
        finally:
            self._remove_reservation_now(key, token)
        if duplicate is not None:
            # ``current`` holds this key and runs in the directory this
            # provider derived from it; the loser must not reclaim it.
            duplicate.disown_work_dir()
            try:
                await duplicate.shutdown()
            except Exception:
                self._deps.logger.debug(
                    "open_task_session: duplicate session teardown failed",
                    exc_info=True,
                )
            if await owner._reacquire_and_validate(key, session):
                return session.provider, False, False
            await owner._evict_stale_session(key, session)
            maximum = self._deps.constants.won_race_max_retries
            if _won_race_retries >= maximum:
                raise RuntimeError(
                    f"open_task_session({key!r}) exceeded {maximum} won-race "
                    "retries — session kept going stale between acquire and re-validate"
                )
            return await owner.open_task_session(
                parent_session_key,
                session_key,
                agent=agent,
                cwd=cwd,
                approval_policy=approval_policy,
                _won_race_retries=_won_race_retries + 1,
                start_priority=start_priority,
            )
        assert won_race_session is session
        await session.semaphore.acquire()
        session.turn_owner = asyncio.current_task()
        return session.provider, True, False

    def record_success(self, key: str) -> None:
        session = self._sessions.get(self._owner._fold_key(key))
        if session:
            session.consecutive_failures = 0

    async def record_failure(self, key: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        session.consecutive_failures += 1
        if session.consecutive_failures >= self._deps.constants.circuit_breaker_threshold:
            self._deps.logger.error(
                "Circuit breaker tripped for %s (%d consecutive failures) — resetting",
                key,
                session.consecutive_failures,
            )
            await self._owner.reset(key)
            return True
        return False

    def reserve_inbound_callback(self) -> InboundCallbackReservation | None:
        """Claim one pre-turn callback atomically against update/shutdown admission."""
        if self._closing:
            return None
        token = object()
        self._inbound_callback_reservations.add(token)
        return InboundCallbackReservation(self._inbound_callback_reservations, token)

    @property
    def inbound_callback_count(self) -> int:
        return len(self._inbound_callback_reservations)

    def begin_turn(self, key: str) -> None:
        """Yield-free pre-dispatch closing gate for an already-issued lease."""
        if self._closing:
            raise SessionClosingError(
                "SessionManager is closing (gateway restart/shutdown in "
                "progress); refusing to start a turn"
            )

    def mark_continuable(self, key: str) -> None:
        self._continuable_keys.add(self._owner._fold_key(key))

    def unmark_continuable(self, key: str) -> None:
        self._continuable_keys.discard(self._owner._fold_key(key))

    def set_continuable_fallback(self, callback: Callable[[str], bool] | None) -> None:
        self._continuable_fallback = callback

    def _is_continuable_key(self, folded: str) -> bool:
        if folded in self._continuable_keys:
            return True
        fallback = self._continuable_fallback
        if fallback is None:
            return False
        try:
            if fallback(folded):
                self._continuable_keys.add(folded)
                return True
        except Exception:
            self._deps.logger.debug("continuable fallback failed for %s", folded, exc_info=True)
        return False

    def is_continuable(self, key: str) -> bool:
        return self._owner._is_continuable_key(self._owner._fold_key(key))

    # Persistence forwarding deliberately uses the owner's one SessionMap.
    def resumable_sid(self, key: str) -> str | None:
        return self._owner._session_map.get(self._owner._fold_key(key))

    def resumable_hint(self, key: str) -> bool:
        return self._owner._session_map.has_hint(self._owner._fold_key(key))

    def mapped_sid(self, key: str) -> str:
        return self._owner._session_map.mapped_sid(self._owner._fold_key(key))

    def allocation_predecessor(self, key: str) -> str:
        """The store *key*'s live session superseded, or ``""``.

        The id the mapping named -- live, or the stash a recycle left -- at the
        instant this boundary registered the key's live session, read under the same
        lock and in the same tick as the registration itself and stamped on the
        session (``_Session.predecessor_sid``). That is what makes it safe to
        consume after ``get_or_create`` returns: a caller that read the mapping
        around its own call could be suspended inside the allocation while a
        concurrent turn on the key allocated and recycled an intermediate session,
        and would then cite the store before that one. Read off the live session,
        like ``requested_model``, so a warm claim reads the value its session was
        registered with and ordinary teardown releases it -- a table keyed by
        session key would grow by one entry per ``/new`` or generation rotation for
        the life of the gateway. Empty for a key with no live session, and for a
        session whose registration found no earlier store.
        """
        session = self._sessions.get(self._owner._fold_key(key))
        return session.predecessor_sid if session is not None else ""

    def mapped_session_keys(self) -> frozenset[str]:
        return frozenset(self._owner._session_map.mapped_sids_by_key())

    def seed_conversation(
        self,
        key: str,
        sid: str,
        *,
        provider: str = "",
        cwd: str = "",
    ) -> None:
        if sid:
            self._owner._session_map.set(
                self._owner._fold_key(key),
                sid,
                provider=provider,
                cwd=cwd,
            )

    def forget_conversation(self, key: str) -> str | None:
        folded = self._owner._fold_key(key)
        sid = self._owner._session_map.get(folded)
        self._owner._session_map.delete(folded)
        self._continuable_keys.discard(folded)
        return sid

    def forget_conversation_if_sid(self, key: str, expected_sid: str) -> tuple[bool, str | None]:
        """Forget the resumable pointer only while it is still *expected_sid*.

        ``SessionMap.delete_if_sid`` compares and deletes in one step under its
        own lock; a mismatch deletes nothing and preserves the successor's
        pointer. This is the method's only mutation. Returns the map's
        ``(removed, current_sid)``.
        """
        folded = self._owner._fold_key(key)
        return self._owner._session_map.delete_if_sid(folded, expected_sid)

    def conversation_provider(self, key: str) -> str:
        return self._owner._session_map.get_provider(self._owner._fold_key(key))

    def release(self, key: str, *, cleanup: bool = False) -> None:
        """Release the current registry occupant's semaphore.

        This intentionally preserves the existing key-only lease identity: it
        does not repair the known stale-release window when a locked replacement
        occupies the same key.
        """
        key = self._owner._fold_key(key)
        if self._owner.absorb_orphaned_release(key):
            # The permit this task held died with a session ``reset`` popped;
            # the occupant under the key now (if any) is a successor whose
            # permit belongs to someone else.
            self._deps.logger.debug(
                "release(%s): permit already died with a reset session; not unlocking the successor",
                key,
            )
            return
        session = self._sessions.get(key)
        if session:
            if (
                cleanup
                and key.startswith(self._deps.constants.subagent_prefix)
                and not self._owner._is_continuable_key(key)
            ):
                try:
                    session_id = session.provider.session_id
                    if session_id:
                        asyncio.ensure_future(
                            self._owner._safe_cleanup(session.provider, session_id)
                        )
                except Exception:
                    self._deps.logger.debug("Failed to get session_id for cleanup", exc_info=True)
            try:
                session.semaphore.release()
            except ValueError:
                self._deps.logger.warning(
                    "release(%s): session was replaced under us; dropping "
                    "stray semaphore release instead of over-releasing the "
                    "new occupant's",
                    key,
                )
            else:
                # The holding this release ends is over, so its record goes in the
                # same step. A waiter the permit was just handed to names itself
                # when it next runs; until then the record names nobody, rather
                # than a task that has let the permit go
                # (``compaction_waits_for_turn``, ``cancel_owned_turn``).
                session.turn_owner = None
                # A release marks the end of a live turn, not the start of
                # idleness: refresh liveness so the idle sweep measures from
                # when the session went quiet rather than when it was
                # acquired. A run that never returns still goes stale and is
                # reaped; a run working between tasks is not mistaken for one.
                session.last_used = time.monotonic()
            if getattr(session, "adopted_parked", False):
                # The claim that just ended adopted entries a forced stop parked
                # (``adopt_parked_queue``). Their channels' drains are woken
                # NOW, after the lease is free, or the entries sit on the queue
                # until an unrelated message happens to start a turn. Off this
                # synchronous call, as ``wake_other_drains`` requires.
                session.adopted_parked = False
                channels = [entry_channel(e[2]) for e in session.queue]
                if any(channels):
                    task = asyncio.ensure_future(
                        wake_other_drains(waker="", session_key=key, channels=channels)
                    )
                    self._owner._background_tasks.add(task)
                    task.add_done_callback(self._owner._background_tasks.discard)

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None:
        try:
            await provider.cleanup_session(session_id)
            self._deps.logger.debug("Cleaned up session files for %s", session_id)
        except Exception:
            self._deps.logger.warning(
                "Failed to clean up session files for %s",
                session_id,
                exc_info=True,
            )

    def is_busy(self, key: str) -> bool:
        session = self._sessions.get(self._owner._fold_key(key))
        return bool(session and session.semaphore.locked())

    def touch(self, key: str) -> bool:
        session = self._sessions.get(self._owner._fold_key(key))
        if session is None:
            return False
        session.last_used = time.monotonic()
        return True

    def enqueue(
        self,
        key: str,
        msg_ts: str,
        text: str,
        *,
        force: bool = False,
        **kwargs: object,
    ) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if force or session.semaphore.locked():
            session.queue.append((msg_ts, text, kwargs))
            return True
        return False

    def dequeue(self, key: str) -> tuple[str, str, dict[str, Any]] | None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return None
        while session.queue:
            msg_ts, text, kwargs = session.queue.popleft()
            if msg_ts not in session.cancelled:
                return msg_ts, text, kwargs
            session.cancelled.discard(msg_ts)
            self._deps.unlink_queued_temp_paths(kwargs)
        return None

    def cancel_queued(self, key: str, msg_ts: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        for index, (queued_ts, _, kwargs) in enumerate(session.queue):
            if queued_ts == msg_ts:
                self._deps.unlink_queued_temp_paths(kwargs)
                del session.queue[index]
                return True
        if session.semaphore.locked():
            session.cancelled.add(msg_ts)
        return False

    def is_cancelled(self, key: str, msg_ts: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if msg_ts in session.cancelled:
            session.cancelled.discard(msg_ts)
            return True
        return False

    def detach_queue(self, key: str) -> tuple[Any, ...]:
        """Take every queued entry OUT of the live queue, keeping its files.

        For a Stop that must neither run nor lose what was queued when it was
        pressed: taken before the first await, so an end-of-turn drain during the
        stop finds nothing to start; then either ``clear_queue(only=...)`` drops
        it (the stop went through) or ``restore_queue`` puts it back (declined).
        """
        session = self._sessions.get(self._owner._fold_key(key))
        if session is None:
            return ()
        taken = tuple(session.queue)
        session.queue.clear()
        return taken

    def restore_queue(self, key: str, entries: tuple[Any, ...]) -> None:
        """Put ``detach_queue``'s entries back at the head, ahead of newer arrivals."""
        session = self._sessions.get(self._owner._fold_key(key))
        if session is None or not entries:
            return
        session.queue.extendleft(reversed(entries))

    def clear_queue(
        self,
        key: str,
        owned_by: Callable[[dict], bool] | None = None,
        *,
        only: tuple[Any, ...] | None = None,
    ) -> None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if only is not None:
            # Identity, not equality: two entries can carry the same text. The
            # entries need not still be IN the queue -- ``detach_queue`` takes
            # them out first -- so their files are unlinked from the HANDLES,
            # before the session guard: a hard stop pops the session between
            # the detach and this clear, and the detached entries were never on
            # the popped queue the teardown unlinks.
            for _, _, kwargs in only:
                self._deps.unlink_queued_temp_paths(kwargs)
            if session is None:
                return
            wanted = {id(item) for item in only}
            kept = [item for item in session.queue if id(item) not in wanted]
            if len(kept) != len(session.queue):
                session.queue.clear()
                session.queue.extend(kept)
            # ``cancelled`` left alone, as on the ``owned_by`` branch below.
            return
        if session is None:
            return
        if owned_by is None:
            for _, _, kwargs in session.queue:
                self._deps.unlink_queued_temp_paths(kwargs)
            session.queue.clear()
            session.cancelled.clear()
            return
        # Partitioned BEFORE anything is mutated, so a predicate that raises leaves the
        # queue exactly as it was. Every entry is already dequeued nowhere else -- this
        # runs under the caller's receipt lock -- so the pass costs one walk.
        dropped = [item for item in session.queue if owned_by(item[2])]
        if not dropped:
            return
        kept = [item for item in session.queue if not owned_by(item[2])]
        for _, _, kwargs in dropped:
            self._deps.unlink_queued_temp_paths(kwargs)
        session.queue.clear()
        session.queue.extend(kept)
        # ``cancelled`` is deliberately LEFT ALONE. It holds bare message timestamps a
        # mid-turn cancel asked ``dequeue`` to skip, with nothing on them saying whose
        # they are, so clearing it here would un-cancel somebody else's cancel request.
        # The whole-session branch above may clear it because it empties the queue those
        # timestamps describe.

    async def is_provider_alive(self, key: str) -> bool | None:
        """Whether *key*'s provider process is up, or None when there is no session.

        Forwards a PROCESS-level answer (``provider.is_process_alive``) under a
        per-key question. On a shared runtime every co-tenant key returns the
        same value, so a True here does not establish that *key* itself is still
        served -- only that the process behind it has not exited.
        """
        key = self._owner._fold_key(key)
        async with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return None
        return session.provider.is_process_alive()

    def get_approval_policy(self, key: str) -> str:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.approval_policy if session else ""

    def get_agent(self, key: str) -> str:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.agent if session else ""

    def get_agent_selection(self, key: str) -> tuple[str, str]:
        """Copy the live allocation's selection without reinterpreting its name."""
        session = self._sessions.get(self._owner._fold_key(key))
        if session is None:
            return "template", ""
        member = getattr(session, "capability_member", None)
        agent = getattr(session, "agent", None)
        if not isinstance(member, str) or not isinstance(agent, str):
            raise ValueError("resume_failed: parent agent selection unavailable")
        # prepare_runtime captures the member even before capability enrollment.
        # An empty member means this allocation selected the provider template;
        # subsequent roster changes must not reinterpret that literal.
        return ("member", member) if member else ("template", agent)

    def set_approval_policy(self, key: str, policy: str) -> None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if session:
            previous = session.approval_policy
            session.approval_policy = policy
            if previous != policy:
                self._deps.get_sel().log_tool_invocation(
                    session_key=key,
                    source="session",
                    tool_name="set_approval_policy",
                    outcome=policy or "default",
                    metadata={"old_policy": previous, "new_policy": policy},
                )

    def _resolve_agent_model(self, agent: str) -> str:
        """Resolve an agent JSON model with directory-mtime and TTL invalidation."""
        agents_dir = self._deps.agents_dir_path()
        try:
            directory_mtime = agents_dir.stat().st_mtime
        except OSError:
            directory_mtime = 0.0
        now = time.monotonic()
        cache = self._deps.agent_model_cache()

        entry = cache.get(agent)
        if entry is not None:
            cached_model, cached_mtime, cached_at = entry
            if (
                cached_mtime == directory_mtime
                and now - cached_at < self._deps.constants.agent_model_cache_ttl()
            ):
                return cached_model

        model = "auto"
        try:
            for agent_file in iter_agent_spec_files(agents_dir, ordered=False):
                data = self._deps.read_agent_spec(
                    agent_file,
                    operation="resolve_agent_model",
                    source="unknown",
                )
                if data is None:
                    continue
                if data.get("name") == agent or agent_file.stem == agent:
                    model = self._deps.spec_model(data)
                    break
        except Exception:
            pass
        cache[agent] = (model, directory_mtime, now)
        return model


# The owner modules above hold code that was this module's own: the
# ``SessionAllocationService`` members the class binds, the refusals and the two
# companion-posture helpers. Every project name this module bound before the owners
# existed still resolves here. The ones this module's own code reads it still binds; the
# ones only the owners' code reads now (``_SERVED_NAMES``) it serves from the first owner
# binding them. Either way ``_facade_mirror`` sends a write or delete of the name to every
# owner binding the same object, so a patch of ``kiro_crew.session_allocation.<name>``
# reaches the moved code that reads it, as it reached the one module before. The
# surface is this module's own namespace plus those names, so an owner's own function
# is never served here. ``test/test_session_allocation_bindings.py`` pins the reach.
#: The allocation owner modules, lowest layer first.
_ALLOCATION_OWNERS: tuple[str, ...] = (
    "kiro_crew.session_allocation_parts.errors",
    "kiro_crew.session_allocation_parts.companions",
    "kiro_crew.session_allocation_parts.get_or_create",
)
#: Project names this module bound before the owners existed that only the owners read.
_SERVED_NAMES: frozenset[str] = frozenset(
    {
        "AWS_PROFILE_ENV",
        "END_REASON_EVICTED",
        "START_QUEUE_COLD_START",
        "START_QUEUE_COMPANION",
        "START_QUEUE_LOG_MIN_MS",
        "SessionBusyError",
        "SpeculativeResumeRefused",
        "_collect_parent_runtime_kwargs",
        "acquire_session_lease",
        "bound_profile_env",
        "bounded_session_id",
        "notify_start_queue",
        "parent_work_scratch_dir",
        "pre_spawn_identity",
        "record_session_ended",
        "release_session_lease",
        "spawn_pid",
        "stamp_spawn_identity",
    }
)
# Every owner is loaded before the mirror indexes the family; ``install`` refuses one that
# is not, since an owner imported later would be missing from the index for good.
for _owner in _ALLOCATION_OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__],
    _ALLOCATION_OWNERS,
    excluded=(),
    surface=frozenset(globals()) | _SERVED_NAMES,
)
_EXPORTS.update(_MIRROR.served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: the unsplit module's public names, less the standard-library
#: and typing imports only the owners read now.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # the names in _SERVED_NAMES, for type checkers and IDEs
    from kiro_crew.aws_project_profiles import AWS_PROFILE_ENV, bound_profile_env  # noqa: F401
    from kiro_crew.kiro_prerequisite import (  # noqa: F401
        pre_spawn_identity,
        spawn_pid,
        stamp_spawn_identity,
    )
    from kiro_crew.metrics.sessions import END_REASON_EVICTED, record_session_ended  # noqa: F401
    from kiro_crew.runtime_ownership import (  # noqa: F401
        acquire_session_lease,
        release_session_lease,
    )
    from kiro_crew.session_allocation_parts.companions import (  # noqa: F401
        _collect_parent_runtime_kwargs,
        parent_work_scratch_dir,
    )
    from kiro_crew.session_allocation_parts.errors import (  # noqa: F401
        SessionBusyError,
        SpeculativeResumeRefused,
    )
    from kiro_crew.start_priority import (  # noqa: F401
        START_QUEUE_COLD_START,
        START_QUEUE_COMPANION,
        START_QUEUE_LOG_MIN_MS,
        notify_start_queue,
    )
    from kiro_crew.validation import bounded_session_id  # noqa: F401
