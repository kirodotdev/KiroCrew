"""Session registry, allocation, and claim coordination.

``SessionAllocationService`` owns the live-session registry and every lock or
lease needed to allocate from it.  Warm-pool inventory, compaction, teardown,
and cleanup policy remain separate owner-facade responsibilities.  This module
never imports :mod:`kiro_crew.session` at runtime; patchable compatibility seams
are supplied through :class:`AllocationDeps`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.kiro_prerequisite import pre_spawn_identity, spawn_pid, stamp_spawn_identity
from kiro_crew.metrics.sessions import (
    END_REASON_EVICTED,
    discard_session_start,
    record_session_ended,
    record_session_started,
)
from kiro_crew.validation import bounded_session_id

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider
else:
    # ProviderFactory below subscripts LLMProvider at module scope, so a name must
    # exist at runtime; a real import would cross the agent-SDK boundary gate.
    LLMProvider = Any


ProviderFactory = Callable[..., LLMProvider]


class SessionClosingError(RuntimeError):
    """A turn was requested after manager shutdown began."""


class SessionEndingError(RuntimeError):
    """A request under a key whose run is being ended could not be held until the fence lifted.

    The per-key sibling of :class:`SessionClosingError`. While a caller holds
    the key's ending fence (``begin_ending`` / ``end_ending``, the cron reaper
    and ``cancel()`` from their kill passes through the run's terminal record
    and audit), nothing lands under the key: a claim or a new allocation is HELD
    at the door of ``get_or_create`` until the fence lifts and then proceeds
    under the recorded key, and a cold start whose reservation was already in
    flight when the fence went up is refused at registration -- its started
    provider hard-killed by the same path a closing manager uses -- after which
    the same call waits for the lift and allocates again. A held request is not
    dropped: a sub-agent completion that races the reap of its parent's run is
    delivered into the session that follows the record, as it was before the
    fence existed, only never into the run being ended.

    Raised for the two requests that cannot be held: a call whose wait outlived
    :data:`ENDING_FENCE_WAIT_SECS` (a fence held far past the bounded kill
    passes it exists for -- a defect to surface, not to hang every caller of the
    key on), and a per-step task session (``open_task_session``) under a fenced
    key, which reserves nothing and has no hard-kill path for a session already
    created on the shared runtime.
    """


class SessionBusyError(RuntimeError):
    """A caller requested an immediate turn claim while the session was held."""


#: How long a claim or cold start waits at the door of ``get_or_create`` for a
#: key's ending fence to lift before it is refused (:class:`SessionEndingError`).
#: An independent caller-refusal bound, not a multiple of the fence's length: a
#: fence covers the ending caller's kill passes (bounded resets and verified
#: kills), its terminal record (a locked store merge on a worker thread, a
#: history append) and its audit -- and a reset cancelled at its own timeout
#: still finishes its cleanup before the pass ends, so the fence's length is not
#: something this constant can be derived from. What it fixes is when a caller
#: stops waiting: well inside the 1200 s the sub-agent completion path allows
#: its whole delivery, and long enough that an ordinary ending never reaches it.
#: A fence still up at this point is a holder stuck past its own bounds: the
#: caller is refused so that the defect surfaces, rather than hanging every
#: caller of the key on it.
ENDING_FENCE_WAIT_SECS = 180.0


class SpeculativeResumeRefused(RuntimeError):
    """A speculative allocation may not consume an unrequested native resume."""


@dataclass(frozen=True, slots=True)
class AllocationConstants:
    """Behavioral constants supplied by the facade's patchable namespace."""

    max_concurrent_cold_starts: int
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
    session_model: Callable[[Any, str | None, str | None], str | None]
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
    resolve_pin_spelling: Callable[[str, list[str]], str]
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


@dataclass(slots=True)
class SessionRegistryState:
    """Mutable state exclusively owned by the allocation boundary."""

    sessions: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False
    update_pause_owned: bool = False
    update_restart_fenced: bool = False
    start_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    starting_pids: set[int] = field(default_factory=set)
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

    async def await_replay_gap(self, key: str) -> None: ...

    def absorb_orphaned_release(self, key: str) -> bool: ...

    def adopt_turn(self, key: str) -> None: ...

    def get_provider(self, key: str) -> LLMProvider | None: ...

    async def get_subagent_runtime(
        self, parent_session_key: str, agent: str | None = None
    ) -> Any: ...

    async def _get_or_bootstrap_run_runtime(
        self,
        parent_session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
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


def _collect_parent_runtime_kwargs(
    owner: _AllocationOwner,
    parent_session_key: str,
) -> dict[str, Any]:
    """Mirror the parent client's sandbox, gateway, env, and backend posture."""
    provider = owner.get_provider(parent_session_key)
    if provider is None:
        return {}
    client = getattr(provider, "client", None) or getattr(provider, "_client", None)
    if client is None:
        return {}
    kwargs: dict[str, Any] = {}
    for attribute, key in (
        ("_sandbox_mode", "sandbox_mode"),
        ("_extra_env", "extra_env"),
        ("_mcp_gateway_overlay", "mcp_gateway_overlay"),
        ("_mcp_gateway_socket", "mcp_gateway_socket"),
        ("backend", "acp_backend"),
    ):
        value = getattr(client, attribute, None)
        if value is not None:
            kwargs[key] = value
    # The MCP Tool Search choice rides the runtime constructor on a wire-settings
    # host, so a companion runtime built without it would run with the setting
    # left to the host's default rather than the explicit value the parent sent.
    # Read off the LLMProvider capability (safe default None), never probed.
    tool_search = provider.tool_search_settings
    if tool_search is not None:
        kwargs["tool_search"] = tool_search
    # The parent's session tree keeps ONE work directory across every process
    # it spans: a companion runtime is handed the parent's ``$KIROCREW_SCRATCH``
    # as a second private window into the masked scratch root, so a brief the
    # parent staged there is readable by the subagents the runtime hosts (see
    # ``agent_scratch``); at spawn it joins the tree's owner marker beside the parent.
    shared_scratch = parent_work_scratch_dir(owner, parent_session_key)
    if shared_scratch is not None:
        kwargs["shared_scratch"] = shared_scratch
    return kwargs


def parent_work_scratch_dir(owner: _AllocationOwner, parent_session_key: str) -> Path | None:
    """The work directory of *parent_session_key*'s session tree, or None.

    Read off the parent's live provider through the ``LLMProvider``
    capability (``work_scratch_dir``, harness-parity H14 -- declared on the
    ABC with a ``None`` default, never probed for a private name); None when
    the parent has no live provider or its process carries no scratch. The
    caller passes it as ``shared_scratch`` to the spawn it makes on the
    parent's behalf -- a companion runtime here, a dedicated subagent process
    in ``subagent_manager/run.py`` -- and the spawn re-validates it at mount
    time (``agent_scratch.shared_scratch_window``).
    """
    provider = owner.get_provider(parent_session_key)
    if provider is None:
        return None
    path = provider.work_scratch_dir
    return path if isinstance(path, Path) else None


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
    def _start_sem(self) -> asyncio.Semaphore:
        return self.state.start_sem

    @_start_sem.setter
    def _start_sem(self, value: asyncio.Semaphore) -> None:
        self.state.start_sem = value

    @property
    def _starting_pids(self) -> set[int]:
        return self.state.starting_pids

    @_starting_pids.setter
    def _starting_pids(self, value: set[int]) -> None:
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
        no retry: that path reserves nothing and creates on a shared runtime,
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

    async def get_subagent_runtime(self, parent_session_key: str, agent: str | None = None) -> Any:
        """Get or spawn the canonical shared companion runtime for a parent."""
        runtime_type, runtime_dead = self._deps.runtime_types()
        max_retries = 1
        attempt = 0
        selected_agent = agent
        while True:
            lock = self._subagent_runtime_locks.setdefault(parent_session_key, asyncio.Lock())
            async with lock:
                if self._subagent_runtime_locks.get(parent_session_key) is not lock:
                    # release_subagent_runtime removed the lock while we waited;
                    # retry under the newly-canonical lock without spending a
                    # process-spawn retry.
                    continue
                existing = self._subagent_runtimes.get(parent_session_key)
                if existing is not None and existing.is_alive():
                    return existing
                if existing is not None:
                    try:
                        await existing.kill(
                            reason="reaping a dead shared subagent runtime before respawn"
                        )
                    except Exception:
                        self._deps.logger.debug(
                            "get_subagent_runtime: dead runtime kill failed for %s",
                            parent_session_key,
                            exc_info=True,
                        )
                selected_agent = (
                    selected_agent
                    or self._owner._get_session_agent(parent_session_key)
                    or "kirocrew"
                )
                kwargs = self._owner._parent_runtime_kwargs(parent_session_key)
                runtime = runtime_type(agent=selected_agent, **kwargs)
                # Bracket the spawn with identity reads so this runtime carries
                # a spawn stamp: every subagent session demuxed onto it
                # inherits its credential, and the registry pass in
                # ``flag_identity_stamp_mismatches`` can only compare a stamp
                # that was recorded. The stamp lands on the runtime object
                # itself (the gate reads ``runtime.spawn_identity`` directly).
                pre_spawn = await pre_spawn_identity(
                    getattr(self._owner, "spawn_identity_reader", None)
                )
                try:
                    await runtime.spawn()
                except runtime_dead:
                    if attempt >= max_retries:
                        raise
                    attempt += 1
                    self._deps.logger.warning(
                        "Subagent runtime spawn failed for %s (attempt %d/%d), retrying",
                        parent_session_key,
                        attempt,
                        max_retries + 1,
                        exc_info=True,
                    )
                    continue
                # Best-effort spawn-account record; see flag_identity_stamp_mismatches.
                # A cancellation landing in this read would leak the spawned
                # runtime before it reaches the registry below, so tear it
                # down on the way out. The read is also a suspension point
                # before the registry (the orphan sweep's companion-PID union)
                # can see this runtime, so shield its PID for the span.
                starting_pid = spawn_pid(runtime)
                if starting_pid is not None:
                    self._starting_pids.add(starting_pid)
                try:
                    try:
                        await stamp_spawn_identity(
                            getattr(self._owner, "spawn_identity_reader", None),
                            runtime,
                            pre_spawn=pre_spawn,
                        )
                    except BaseException:
                        with contextlib.suppress(Exception):
                            await runtime.kill(
                                expected=True,
                                reason="spawn-stamp interrupted before registration",
                            )
                        raise
                    self._subagent_runtimes[parent_session_key] = runtime
                finally:
                    if starting_pid is not None:
                        self._starting_pids.discard(starting_pid)
                return runtime

    async def release_subagent_runtime(self, parent_session_key: str) -> None:
        """Serialize release with spawn and kill the detached runtime off-map."""
        lock = self._subagent_runtime_locks.get(parent_session_key)
        if lock is not None:
            async with lock:
                runtime = self._subagent_runtimes.pop(parent_session_key, None)
                # A waiter on this removed lock re-checks canonical identity in
                # get_subagent_runtime and retries under the live lock.
                self._subagent_runtime_locks.pop(parent_session_key, None)
        else:
            runtime = self._subagent_runtimes.pop(parent_session_key, None)
        if runtime is not None:
            try:
                await runtime.kill(expected=True, reason="subagent runtime released")
            except Exception:
                self._deps.logger.warning(
                    "Failed to kill subagent runtime for %s",
                    parent_session_key,
                    exc_info=True,
                )

    async def _get_or_bootstrap_run_runtime(
        self,
        parent_session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
    ) -> Any:
        """Adopt a configured bootstrap provider's runtime for a task run."""
        owner = self._owner
        if not owner._provider_factory:
            # Outside the per-key lock: get_subagent_runtime takes that lock and
            # asyncio.Lock is not reentrant.
            return await owner.get_subagent_runtime(parent_session_key, agent=agent)

        if parent_session_key not in self._subagent_runtime_locks:
            self._subagent_runtime_locks[parent_session_key] = asyncio.Lock()
        lock = self._subagent_runtime_locks[parent_session_key]
        async with lock:
            existing = self._subagent_runtimes.get(parent_session_key)
            if existing is not None and existing.is_alive():
                return existing
            provider = owner._provider_factory(parent_session_key, agent=agent, cwd=cwd)
            pre_spawn = await pre_spawn_identity(getattr(owner, "spawn_identity_reader", None))
            await provider.start()
            # The stamp read below suspends before this provider's runtime is
            # visible to the orphan sweep's companion-PID union -- shield the
            # freshly-published PID for the whole start-to-registration span.
            starting_pid = spawn_pid(provider)
            if starting_pid is not None:
                self._starting_pids.add(starting_pid)
            try:
                # Record which account the store held as this runtime spawned:
                # every session later demuxed onto it inherits this credential, so
                # the stamp lives on the runtime's wrapping provider and is read
                # back through the ``_runtime`` fallback in
                # ``flag_identity_stamp_mismatches``. Best-effort; an unstamped
                # runtime keeps the pre-stamping protections. A cancellation
                # landing in this read would leak the started provider before it
                # reaches the registry below, so kill it on the way out.
                try:
                    await stamp_spawn_identity(
                        getattr(owner, "spawn_identity_reader", None), provider, pre_spawn=pre_spawn
                    )
                except BaseException:
                    owner._dispatch_hard_kill(provider)
                    raise
                session_provider = getattr(provider, "_client", None)
                runtime = getattr(session_provider, "_runtime", None)
                if session_provider is not None and runtime is not None:
                    # Sessions demuxed onto this runtime wrap it as ``_runtime``
                    # on their own providers, so the stamp must live on the
                    # runtime object itself for the ``_runtime`` fallback in
                    # ``flag_identity_stamp_mismatches`` to find it.
                    spawn_stamp = getattr(provider, "spawn_identity", "")
                    if spawn_stamp:
                        try:
                            runtime.spawn_identity = spawn_stamp
                        except Exception:
                            self._deps.logger.debug(
                                "run runtime refused the spawn identity stamp", exc_info=True
                            )
                    try:
                        session_provider._owns_runtime = False
                    except Exception:
                        self._deps.logger.debug(
                            "run runtime ownership transfer failed", exc_info=True
                        )
                    self._subagent_runtimes[parent_session_key] = runtime
                    try:
                        handle = getattr(session_provider, "_handle", None)
                        session_id = getattr(handle, "session_id", None) or getattr(
                            handle, "_session_id", None
                        )
                        if session_id:
                            await runtime.terminate_session(session_id)
                    except Exception:
                        self._deps.logger.debug(
                            "run runtime bootstrap-session terminate failed", exc_info=True
                        )
                    return runtime
                try:
                    await provider.shutdown()
                except Exception:
                    self._deps.logger.debug(
                        "run runtime bootstrap provider shutdown failed", exc_info=True
                    )
            finally:
                if starting_pid is not None:
                    self._starting_pids.discard(starting_pid)
        return await owner.get_subagent_runtime(parent_session_key, agent=agent)

    async def _reacquire_and_validate(
        self,
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
            session.semaphore.release()
            raise
        if not still_valid:
            session.semaphore.release()
        else:
            session.turn_owner = asyncio.current_task()
        return still_valid

    async def _evict_stale_session(self, key: str, session: Any) -> None:
        """Pop only the observed stale object and close it outside the lock."""
        dead: LLMProvider | None = None
        async with self._lock:
            if self._sessions.get(key) is session:
                del self._sessions[key]
                self.advance_ownership_generation(key)
                dead = session.provider
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

    async def open_task_session(
        self,
        parent_session_key: str,
        session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
        approval_policy: str = "",
        _won_race_retries: int = 0,
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
                key, agent=agent, approval_policy=approval_policy, cwd=cwd
            )
        async with self._lock:
            # The other publication door: a key whose run is being ended
            # (``begin_ending``) admits no per-step session either. Refused here
            # -- not held like ``get_or_create``'s front door -- before anything
            # is created for it, because this path holds no allocation
            # reservation to invalidate and has no hard-kill handler for a
            # session already created on the shared runtime. A create already in
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
                key, agent=agent, approval_policy=approval_policy, cwd=cwd
            )
        runtime = await owner._get_or_bootstrap_run_runtime(
            parent_session_key, agent=agent, cwd=cwd
        )
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
            )
        except AcpWorkspaceBindingError:
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
            )
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
        if duplicate is not None:
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
            )
        assert won_race_session is session
        await session.semaphore.acquire()
        session.turn_owner = asyncio.current_task()
        return session.provider, True, False

    def _get_session_agent(self, session_key: str) -> str:
        session = self._sessions.get(session_key)
        if session is None:
            return ""
        return getattr(session, "agent", "") or ""

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict[str, Any]:
        return _collect_parent_runtime_kwargs(self._owner, parent_session_key)

    def is_session_sharing_eligible(self, parent_session_key: str) -> bool:
        # Exact-key lookup is current behavior; do not fold this seam here.
        session = self._sessions.get(parent_session_key)
        if session is None:
            return False
        if session.loaded_capabilities is not None:
            return False
        return getattr(session.provider, "is_session_sharing_eligible", False)

    @staticmethod
    def _runtime_pid(runtime: Any) -> int | None:
        pid = getattr(runtime, "pid", None)
        return pid if isinstance(pid, int) and pid > 0 else None

    def runtime_pids(self) -> list[dict[str, object]]:
        """Return process-identity snapshots without performing OS sampling."""
        rows: list[dict[str, object]] = []
        for key, session in self._sessions.items():
            client = getattr(session.provider, "_client", None)
            if client is None:
                client = session.provider
            runtime = getattr(client, "_runtime", None)
            rows.append(
                {
                    "key": key,
                    "agent": session.agent,
                    # The ACP session id, which is also the id of this session's
                    # crew log unit; the Sessions table's lineage reader joins on
                    # it. Backend-authored, so bounded here where it is retained
                    # (the bound every other store of this id applies); an
                    # oversize or empty value is carried as None, not truncated.
                    "sid": bounded_session_id(getattr(session.provider, "session_id", None)),
                    "pid": self._runtime_pid(runtime),
                    "owns_runtime": bool(getattr(client, "_owns_runtime", True)),
                    "created_at": session.created_at,
                    "prompts": session.prompt_count,
                }
            )
        self._owner._append_companion_runtime_rows(rows)
        return rows

    def _append_companion_runtime_rows(self, rows: list[dict[str, object]]) -> None:
        """Append manager-owned background and subagent runtime process rows."""
        now_wall = time.time()
        now_monotonic = time.monotonic()

        def add(label: str, runtime: object, agent: str) -> None:
            try:
                if runtime is None or not runtime.is_alive():  # type: ignore[attr-defined]
                    return
                pid = self._runtime_pid(runtime)
                if pid is None:
                    return
                spawned = getattr(runtime, "_spawn_monotonic", None)
                created = (
                    now_wall - (now_monotonic - spawned)
                    if isinstance(spawned, (int, float))
                    else None
                )
                rows.append(
                    {
                        "key": label,
                        "agent": agent,
                        "pid": pid,
                        "owns_runtime": True,
                        "created_at": created,
                        "prompts": None,
                    }
                )
            except Exception:
                self._deps.logger.debug("runtime_pids: probe failed for %s", label, exc_info=True)

        add(
            "Background runtime",
            self._owner._bg_runtime,
            self._deps.constants.background_agent,
        )
        for parent_key, runtime in list(self._subagent_runtimes.items()):
            add(f"Subagent runtime ({parent_key})", runtime, "")

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

    def clear_queue(self, key: str, owned_by: Callable[[dict], bool] | None = None) -> None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
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

    @staticmethod
    def _is_member_key(key: str) -> bool:
        """Whether *key* addresses a crew member's pinned DM session.

        Wrapper so the pool-bypass arm stays readable and the import stays off
        module top level (circular import: members' module graph is heavy and
        imports config, which sits below this module).
        """
        from kiro_crew.members import is_member_session_key

        return is_member_session_key(key)

    async def _crew_pins_effort(self, agent: str | None, crew_agent: object) -> bool:
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

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None:
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

    def _remove_reservation_now(self, key: str, token: object) -> None:
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

    async def _remove_reservation_cancellation_drained(self, key: str, token: object) -> None:
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

    async def get_or_create(
        self,
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

    def _remember_capability_failure(self, key: str, preparation: Any) -> None:
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
        self,
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
                **extra_factory_kwargs,
            )

        resume_sid: str | None = None
        is_stateless = (
            key in (constants.background_key, constants.heartbeat_key)
            or any(key.startswith(prefix) for prefix in constants.stateless_prefixes)
        ) and not owner._is_continuable_key(key)
        if not is_stateless:
            resume_sid = owner._session_map.get(key)
        if speculative and resume_sid and not speculative_resume:
            raise SpeculativeResumeRefused(key)

        from kiro_crew.session_capabilities import prepare_runtime

        effective_cwd = cwd
        if not effective_cwd and resume_sid:
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

        # Reconciliation can publish a new template model. Resolve only after
        # that boundary, while retaining an explicit caller model unchanged.
        if model is None:

            def resolve_model() -> str | None:
                cfg = self._deps.load_config() if preparation.revision else owner._cfg
                selected = preparation.member or agent
                return self._deps.session_model(cfg, selected, claim_crew)

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
        elif resume_sid:
            pool_decision = "bypass_resume"
        elif is_stateless:
            pool_decision = "bypass_stateless"
        elif self._is_member_key(key):
            # A pooled child was spawned with no session key, so it runs the
            # factory's DEFAULT backend and none of the member construction
            # route (per-session dispatch-tool mount, member backend). A warm
            # hit would silently hand a member DM a session that cannot mount
            # its tools; cold-starting through the factory is what makes the
            # member route real. String check — as cheap as the arms above.
            pool_decision = "bypass_member"
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
        owner._record_pool_decision(pool_decision, key)
        # Assigned by the cold-start branch inside its semaphore section (the
        # spawn-identity stamp read there must already run under the shield);
        # the pool-claim branch leaves it None and is shielded after the
        # branches converge below.
        starting_pid: int | None = None
        if pooled is not None:
            provider = pooled
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
                            if advertised and self._deps.model_is_unusable(
                                switch_model, advertised
                            ):
                                # A literal miss can be a stale `<namespace>::`
                                # qualifier on a model the backend fully serves:
                                # resolve to the advertised spelling and
                                # send THAT — the same fold the cold-start spawn
                                # and the display verdict use, so a warm claim
                                # runs exactly what a cold start of the same pin
                                # runs. A pin absent under either spelling still
                                # takes the withhold below.
                                _send_model = self._deps.resolve_pin_spelling(
                                    switch_model, advertised
                                )
                            if not _send_model:
                                self._deps.logger.warning(
                                    "Pool post-claim: model %s is not available to this "
                                    "account; leaving the claimed process on %s",
                                    switch_model,
                                    pool_model,
                                )
                            else:
                                await cast(Any, provider).client.set_model(_send_model)
                                self._deps.logger.info(
                                    "Pool post-claim: switched model to %s",
                                    _send_model,
                                )
                    _effort_override = extra_factory_kwargs.get("reasoning_effort_override")
                    if _effort_override:
                        _eff = str(_effort_override)
                        try:
                            if not await provider.change_effort(_eff):
                                _cur_model = (
                                    getattr(cast(Any, provider).client, "_model", None) or ""
                                )
                                self._deps.logger.warning(
                                    "reasoning effort '%s' will not be applied (session %s) — "
                                    "model '%s' does not support effort configuration",
                                    _eff,
                                    key or "?",
                                    _cur_model or "auto",
                                )
                            else:
                                _cur_model = (
                                    getattr(cast(Any, provider).client, "_model", None) or ""
                                )
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
                self._deps.logger.info(
                    "Claimed warm-pool process for %s (agent=%s)",
                    key,
                    agent or owner._pool_agent,
                )
                owner._schedule_replenish()
            except (asyncio.CancelledError, Exception):
                owner._dispatch_hard_kill(provider)
                raise
        else:
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
            if memory_mode != "persistent":
                resume_sid = None
            provider_switched = False
            if resume_sid:
                is_claude_now = self._deps.is_claude_provider(
                    provider
                ) or self._deps.is_claude_backend(provider)
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
                    self._deps.logger.info(
                        "Attempting session/load for %s (sid=%s)", key, resume_sid
                    )
                elif self._deps.is_claude_provider(provider):
                    cast(Any, provider).set_resume_session_id(resume_sid)
                    self._deps.logger.info("CC resume for %s (sid=%s)", key, resume_sid)
            async with self._start_sem:
                try:
                    if preparation.revision:
                        from kiro_crew.session_capabilities import (
                            CapabilityStartupError,
                            verify_saved,
                        )

                        if provider.member_capabilities_supported is not True:
                            raise CapabilityStartupError("capability_harness_unsupported")
                        if provider.process_instance:
                            raise CapabilityStartupError("capability_runtime_not_fresh")
                        await asyncio.to_thread(verify_saved, preparation, provider.cwd)
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
                    pre_spawn = await pre_spawn_identity(
                        getattr(owner, "spawn_identity_reader", None)
                    )
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
                    self._sessions[key] = session
                    self.advance_ownership_generation(key)
                    try:
                        await record_session_started(key)
                    except BaseException:
                        # See open_task_session: a cancellation here would leave a
                        # registered session whose provider the caller is about to
                        # kill, plus a crumb the next boot reads as a crash.
                        if self._sessions.get(key) is session:
                            del self._sessions[key]
                            self.advance_ownership_generation(key)
                        await discard_session_start(key)
                        raise
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
            owner._dispatch_hard_kill(provider)
            raise
        finally:
            if starting_pid is not None:
                self._starting_pids.discard(starting_pid)

        if won_race_session is not None:
            if duplicate_provider is not None:
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
                extra_env=extra_env,
                speculative=speculative,
                speculative_resume=speculative_resume,
                wait_if_busy=wait_if_busy,
                _won_race_retries=_won_race_retries + 1,
                **extra_factory_kwargs,
            )

        return result
